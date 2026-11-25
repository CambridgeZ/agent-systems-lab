"""多会话超长程 Horizon Harness：Initializer / 增量 Coding Worker 接力 + 不可篡改 Feature 账本 + 机械化架构边界 Lint。

================================================================================
模块导读（Module Guide）
================================================================================
1. 对标业界 SOTA：
   - Anthropic《Effective harnesses for long-running agents》：当工程任务跨越多个上下文窗口时，单靠
     窗口内压缩会丢失全局验收清单与隐性回归。本模块引入两阶段接力架构：
     (a) Initializer 阶段生成受防篡改保护的 `feature_list.json`（所有特性初始 `passes: false`）、
         幂等冒烟脚手架 `init.sh` 与只追加交接日志 `progress.md`；
     (b) 增量 Worker 阶段每次以全新干净上下文启动，注入 `git log` + `progress.md` + 冒烟结果 + 唯一锁定
         的下一个 `passes: false` 特性，完成实现、通过端到端验证与架构边界校验后凭 SHA-256 验收收据
         翻转 `passes: true` 并交接下一窗口。
   - OpenAI《Harness engineering》：将架构边界规则编码为确定性机械检查（`MechanicalInvariantChecker`），
     每条违规诊断均附带**模型可直接执行的修复指引（remediation）**，把隐性规范变成可自动验证的护栏。
2. 核心安全与完整性不变量：
   - 特性防删减不变量：已有 Feature 不可被静默删除或篡改描述；`passes` 仅在真实 `exit_code == 0` 且
     非空验证输出产生 SHA-256 收据（`receipt_sha256`）后才允许翻转为 `True`；
   - 文件系统安全：所有账本写入均经符号链接检测与 `mkstemp + fsync + os.replace` 原子替换。
================================================================================
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from .host_tools import is_host_secret_segment
from .permissions import build_scrubbed_env

HORIZON_SCHEMA_VERSION = 1
_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_FEATURE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
_SKIP_WALK_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".agent-lab"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert_no_symlink(target: Path, *, stop_at: Path) -> None:
    root = stop_at.resolve()
    cur = target
    while True:
        if cur.is_symlink():
            raise ValueError(f"安全防护拒绝符号链接路径: {cur}")
        if cur.resolve() == root or cur == cur.parent:
            break
        cur = cur.parent


def _atomic_write_text(target: Path, text: str, *, root: Path, mode: int = 0o644) -> None:
    _assert_no_symlink(target, stop_at=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink(target.parent, stop_at=root)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(text)
            tf.flush()
            os.fsync(tf.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def compute_verification_receipt(
    *,
    campaign_id: str,
    feature_id: str,
    run_id: str,
    exit_code: int,
    verification_output: str,
    commit_sha: str | None = None,
) -> str:
    """为通过验证的特性计算防伪 SHA-256 验收收据。"""
    payload = json.dumps(
        {
            "campaign_id": campaign_id,
            "feature_id": feature_id,
            "run_id": run_id,
            "exit_code": int(exit_code),
            "output_sha256": hashlib.sha256(verification_output.encode("utf-8")).hexdigest(),
            "commit_sha": commit_sha or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# OpenAI Harness Engineering 风格：机械化架构边界 Lint（附带模型可执行修复指引）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvariantRule:
    rule_id: str
    kind: str  # "forbidden_import" | "max_file_lines" | "forbidden_pattern"
    file_glob: str
    remediation: str
    forbidden_module_prefix: str = ""
    max_lines: int = 0
    pattern: str = ""


class MechanicalInvariantChecker:
    """确定性架构边界检查器：用 AST / 行数 / 正则验证仓库不变量，并输出带修复指引的结构化诊断。"""

    def __init__(self, rules: Sequence[InvariantRule] = ()) -> None:
        self.rules = list(rules)

    @classmethod
    def from_workspace(cls, workspace: str | Path) -> MechanicalInvariantChecker:
        ws = Path(workspace).resolve()
        cfg = ws / ".agent-lab" / "invariants.json"
        if not cfg.is_file() or cfg.is_symlink():
            return cls([])
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        raw_rules = raw.get("rules") if isinstance(raw, Mapping) else raw
        if not isinstance(raw_rules, list):
            return cls([])
        parsed: list[InvariantRule] = []
        for idx, item in enumerate(raw_rules):
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in {"forbidden_import", "max_file_lines", "forbidden_pattern"}:
                continue
            parsed.append(
                InvariantRule(
                    rule_id=str(item.get("rule_id") or f"INV-{idx + 1:03d}"),
                    kind=kind,
                    file_glob=str(item.get("file_glob") or "**/*.py"),
                    remediation=str(
                        item.get("remediation") or "请按仓库架构边界规范重构受影响模块并重新运行检查。"
                    ),
                    forbidden_module_prefix=str(item.get("forbidden_module_prefix") or "").strip(),
                    max_lines=int(item.get("max_lines") or 0),
                    pattern=str(item.get("pattern") or ""),
                )
            )
        return cls(parsed)

    @staticmethod
    def _matches_glob(rel_posix: str, pattern: str) -> bool:
        clean = pattern.strip()
        if clean.startswith("**/"):
            tail = clean[3:]
            if fnmatch.fnmatchcase(rel_posix, tail) or fnmatch.fnmatchcase(Path(rel_posix).name, tail):
                return True
        return fnmatch.fnmatchcase(rel_posix, clean)

    def check(self, workspace: str | Path) -> dict[str, Any]:
        ws = Path(workspace).resolve()
        violations: list[dict[str, Any]] = []
        if not self.rules:
            return {"ok": True, "rules_checked": 0, "violations_count": 0, "violations": []}

        files: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(ws, followlinks=False):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in _SKIP_WALK_DIRS
                and not d.startswith(".")
                and not is_host_secret_segment(d)
                and not (Path(dirpath) / d).is_symlink()
            )
            for fname in sorted(filenames):
                if is_host_secret_segment(fname):
                    continue
                fp = Path(dirpath) / fname
                if fp.is_symlink():
                    continue
                files.append((fp.relative_to(ws).as_posix(), fp))

        for rule in self.rules:
            compiled_re = re.compile(rule.pattern) if rule.kind == "forbidden_pattern" and rule.pattern else None
            for rel, fp in files:
                if not self._matches_glob(rel, rule.file_glob):
                    continue
                try:
                    raw = fp.read_bytes()
                    if b"\x00" in raw or len(raw) > 2 * 1024 * 1024:
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                if rule.kind == "max_file_lines" and rule.max_lines > 0:
                    line_count = len(text.splitlines())
                    if line_count > rule.max_lines:
                        violations.append(
                            {
                                "rule_id": rule.rule_id,
                                "kind": rule.kind,
                                "path": rel,
                                "line": rule.max_lines + 1,
                                "message": f"{rel} 共 {line_count} 行，超过架构上限 {rule.max_lines} 行",
                                "remediation": rule.remediation,
                            }
                        )
                elif rule.kind == "forbidden_pattern" and compiled_re is not None:
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if compiled_re.search(line):
                            violations.append(
                                {
                                    "rule_id": rule.rule_id,
                                    "kind": rule.kind,
                                    "path": rel,
                                    "line": lineno,
                                    "message": f"{rel}:{lineno} 命中禁止模式 {rule.pattern!r}",
                                    "remediation": rule.remediation,
                                }
                            )
                elif rule.kind == "forbidden_import" and rule.forbidden_module_prefix and rel.endswith(".py"):
                    prefix = rule.forbidden_module_prefix
                    try:
                        tree = ast.parse(text, filename=rel)
                    except SyntaxError:
                        continue
                    for node in ast.walk(tree):
                        imported: list[tuple[str, int]] = []
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                imported.append((alias.name, node.lineno))
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            imported.append((node.module, node.lineno))
                        for mod_name, lineno in imported:
                            if mod_name == prefix or mod_name.startswith(prefix + "."):
                                violations.append(
                                    {
                                        "rule_id": rule.rule_id,
                                        "kind": rule.kind,
                                        "path": rel,
                                        "line": lineno,
                                        "message": f"{rel}:{lineno} 违反分层边界：禁止导入 {mod_name!r}",
                                        "remediation": rule.remediation,
                                    }
                                )

        return {
            "ok": len(violations) == 0,
            "rules_checked": len(self.rules),
            "violations_count": len(violations),
            "violations": violations,
        }


# ---------------------------------------------------------------------------
# Anthropic Long-Running Harness：Campaign / Feature Ledger / Progress Handoff
# ---------------------------------------------------------------------------


class HorizonCampaignManager:
    """跨会话超长程任务编排管理器（持久化 `feature_list.json`、`init.sh` 与 `progress.md`）。"""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.horizons_root = self.workspace / ".agent-lab" / "horizons"

    def _campaign_dir(self, campaign_id: str) -> Path:
        clean = str(campaign_id or "").strip()
        if not _CAMPAIGN_ID_RE.match(clean):
            raise ValueError(f"非法 campaign_id: {campaign_id!r}")
        cdir = self.horizons_root / clean
        _assert_no_symlink(cdir, stop_at=self.workspace)
        return cdir

    def init_campaign(
        self,
        goal: str,
        features: Sequence[Mapping[str, Any]],
        *,
        smoke_cmd: str = "pytest -q",
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Initializer 阶段：生成不可变基线 `feature_list.json`、`init.sh` 与 `progress.md`。"""
        clean_goal = str(goal or "").strip()
        if not clean_goal:
            raise ValueError("campaign goal 不能为空")
        if not features:
            raise ValueError("features 列表不能为空")
        cid = campaign_id or f"hz-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        cdir = self._campaign_dir(cid)
        ledger_path = cdir / "feature_list.json"
        if ledger_path.exists():
            raise ValueError(f"campaign {cid!r} 已存在，不能覆盖已有验收账本")

        now = _now_iso()
        seen_ids: set[str] = set()
        norm_features: list[dict[str, Any]] = []
        for idx, raw_f in enumerate(features, 1):
            fid = str(raw_f.get("id") or f"F{idx:03d}").strip()
            if not _FEATURE_ID_RE.match(fid) or fid in seen_ids:
                raise ValueError(f"非法或重复的 feature id: {fid!r}")
            seen_ids.add(fid)
            title = str(raw_f.get("title") or "").strip()
            if not title:
                raise ValueError(f"feature {fid!r} 缺少 title")
            norm_features.append(
                {
                    "id": fid,
                    "title": title[:200],
                    "description": str(raw_f.get("description") or title).strip()[:1200],
                    "verification_cmd": str(raw_f.get("verification_cmd") or smoke_cmd).strip()[:400],
                    "passes": False,
                    "status": "pending",
                    "receipt_sha256": None,
                    "completed_run_id": None,
                    "commit_sha": None,
                    "attempts": 0,
                    "updated_at": now,
                }
            )

        doc: dict[str, Any] = {
            "schema_version": HORIZON_SCHEMA_VERSION,
            "campaign_id": cid,
            "goal": clean_goal[:1200],
            "smoke_cmd": str(smoke_cmd or "").strip()[:400],
            "created_at": now,
            "updated_at": now,
            "features": norm_features,
        }
        _atomic_write_text(ledger_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", root=self.workspace)

        init_script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"# Horizon Campaign {cid} — Baseline Smoke Check\n"
            f"{doc['smoke_cmd'] or 'true'}\n"
        )
        _atomic_write_text(cdir / "init.sh", init_script, root=self.workspace, mode=0o700)

        progress_header = (
            f"# Horizon Progress Ledger · {cid}\n\n"
            f"- **Goal**: {clean_goal}\n"
            f"- **Initialized**: {now}\n"
            f"- **Features**: {len(norm_features)} (0 passed)\n\n"
            "## Session Handoffs\n"
        )
        _atomic_write_text(cdir / "progress.md", progress_header, root=self.workspace)
        return doc

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        cdir = self._campaign_dir(campaign_id)
        ledger_path = cdir / "feature_list.json"
        if not ledger_path.is_file():
            raise FileNotFoundError(f"未找到 horizon campaign: {campaign_id}")
        _assert_no_symlink(ledger_path, stop_at=self.workspace)
        doc = json.loads(ledger_path.read_text(encoding="utf-8"))
        progress_path = cdir / "progress.md"
        doc["progress_markdown"] = (
            progress_path.read_text(encoding="utf-8") if progress_path.is_file() else ""
        )
        passed = sum(1 for f in doc.get("features", []) if f.get("passes") is True)
        total = len(doc.get("features", []))
        doc["summary"] = {
            "total_features": total,
            "passed_features": passed,
            "remaining_features": max(0, total - passed),
            "completed": total > 0 and passed == total,
        }
        return doc

    def list_campaigns(self) -> list[dict[str, Any]]:
        if not self.horizons_root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for child in sorted(self.horizons_root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            if not (child / "feature_list.json").is_file():
                continue
            try:
                doc = self.get_campaign(child.name)
                out.append(
                    {
                        "campaign_id": doc["campaign_id"],
                        "goal": doc["goal"],
                        "created_at": doc.get("created_at"),
                        "updated_at": doc.get("updated_at"),
                        "summary": doc["summary"],
                    }
                )
            except Exception:
                continue
        return out

    def append_feature(self, campaign_id: str, feature: Mapping[str, Any]) -> dict[str, Any]:
        """只允许追加新特性，严禁删除或修改既有特性验收条件。"""
        cdir = self._campaign_dir(campaign_id)
        ledger_path = cdir / "feature_list.json"
        doc = json.loads(ledger_path.read_text(encoding="utf-8"))
        existing_ids = {f["id"] for f in doc["features"]}
        fid = str(feature.get("id") or f"F{len(doc['features']) + 1:03d}").strip()
        if not _FEATURE_ID_RE.match(fid) or fid in existing_ids:
            raise ValueError(f"非法或重复的 feature id: {fid!r}")
        title = str(feature.get("title") or "").strip()
        if not title:
            raise ValueError("新特性缺少 title")
        now = _now_iso()
        doc["features"].append(
            {
                "id": fid,
                "title": title[:200],
                "description": str(feature.get("description") or title).strip()[:1200],
                "verification_cmd": str(feature.get("verification_cmd") or doc.get("smoke_cmd") or "pytest -q").strip()[:400],
                "passes": False,
                "status": "pending",
                "receipt_sha256": None,
                "completed_run_id": None,
                "commit_sha": None,
                "attempts": 0,
                "updated_at": now,
            }
        )
        doc["updated_at"] = now
        _atomic_write_text(ledger_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", root=self.workspace)
        return doc

    def next_pending_feature(self, campaign_id: str) -> dict[str, Any] | None:
        doc = self.get_campaign(campaign_id)
        for feat in doc.get("features", []):
            if not feat.get("passes"):
                return feat
        return None

    def mark_feature_passed(
        self,
        campaign_id: str,
        feature_id: str,
        *,
        run_id: str,
        verification_output: str,
        exit_code: int,
        commit_sha: str | None = None,
    ) -> dict[str, Any]:
        """仅当真实命令退出码为 0 且携带非空验证输出时，才签发 SHA-256 收据并翻转 `passes=True`。"""
        if exit_code != 0:
            raise ValueError(f"防篡改拒绝：验证退出码 {exit_code} != 0，不能将 {feature_id} 标记为通过")
        if not str(verification_output or "").strip():
            raise ValueError(f"防篡改拒绝：缺少真实测试/验收输出，不能将 {feature_id} 标记为通过")
        cdir = self._campaign_dir(campaign_id)
        ledger_path = cdir / "feature_list.json"
        doc = json.loads(ledger_path.read_text(encoding="utf-8"))
        target = next((f for f in doc["features"] if f["id"] == feature_id), None)
        if target is None:
            raise KeyError(f"未找到 feature_id={feature_id!r}")
        now = _now_iso()
        receipt = compute_verification_receipt(
            campaign_id=campaign_id,
            feature_id=feature_id,
            run_id=run_id,
            exit_code=exit_code,
            verification_output=verification_output,
            commit_sha=commit_sha,
        )
        target["passes"] = True
        target["status"] = "passed"
        target["receipt_sha256"] = receipt
        target["completed_run_id"] = run_id
        target["commit_sha"] = commit_sha
        target["attempts"] = int(target.get("attempts") or 0) + 1
        target["updated_at"] = now
        doc["updated_at"] = now
        _atomic_write_text(ledger_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", root=self.workspace)
        return target

    def record_session_handoff(
        self,
        campaign_id: str,
        *,
        run_id: str,
        feature_id: str,
        outcome: str,
        summary: str,
        commit_sha: str | None = None,
        invariant_report: Mapping[str, Any] | None = None,
    ) -> str:
        cdir = self._campaign_dir(campaign_id)
        ledger_path = cdir / "feature_list.json"
        doc = json.loads(ledger_path.read_text(encoding="utf-8"))
        now = _now_iso()
        for feat in doc["features"]:
            if feat["id"] == feature_id and not feat.get("passes"):
                feat["attempts"] = int(feat.get("attempts") or 0) + 1
                feat["status"] = "in_progress" if outcome != "blocked" else "blocked"
                feat["updated_at"] = now
                break
        doc["updated_at"] = now
        _atomic_write_text(ledger_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", root=self.workspace)

        inv_line = ""
        if invariant_report is not None:
            inv_line = (
                f"- **Architectural Invariants**: "
                f"{'PASS' if invariant_report.get('ok') else 'FAIL'} "
                f"(violations={invariant_report.get('violations_count', 0)})\n"
            )
        entry = (
            f"\n### Session `{run_id[:12]}` · {now}\n"
            f"- **Feature**: `{feature_id}` · **Outcome**: `{outcome}`\n"
            + (f"- **Git Commit**: `{commit_sha}`\n" if commit_sha else "")
            + inv_line
            + f"- **Handoff Summary**: {summary.strip()[:800]}\n"
        )
        progress_path = cdir / "progress.md"
        existing = progress_path.read_text(encoding="utf-8") if progress_path.is_file() else ""
        _atomic_write_text(progress_path, existing + entry, root=self.workspace)
        return entry

    def build_session_boot_prompt(
        self,
        campaign_id: str,
        *,
        feature: Mapping[str, Any] | None = None,
        git_log_excerpt: str = "",
        smoke_result: Mapping[str, Any] | None = None,
    ) -> str:
        """为全新上下文窗口构建结构化接力启动提示（聚焦单一未完成特性，避免一次摊大饼）。"""
        doc = self.get_campaign(campaign_id)
        target = dict(feature) if feature is not None else self.next_pending_feature(campaign_id)
        if target is None:
            return f"Horizon Campaign `{campaign_id}` 所有特性均已通过验收，无需继续执行。"
        prog_tail = "\n".join((doc.get("progress_markdown") or "").splitlines()[-28:])
        s = doc["summary"]
        parts = [
            f"[HORIZON_SESSION_BOOT] Campaign `{campaign_id}` ({s['passed_features']}/{s['total_features']} features passed)",
            f"总体目标：{doc['goal']}",
            "",
            f"## 本会话唯一锁定目标特性：`{target['id']}` — {target['title']}",
            f"- 需求描述：{target['description']}",
            f"- 必须通过的验证命令：`{target['verification_cmd']}`",
            "- 纪律约束：本会话只实现并验证这一项特性；禁止删除或弱化已有测试；完成后确保工作区整洁、验收命令全绿。",
        ]
        if git_log_excerpt.strip():
            parts.extend(["", "## 最近 Git 提交记录", git_log_excerpt.strip()[:1000]])
        if smoke_result is not None:
            parts.extend(
                [
                    "",
                    f"## 启动前基线冒烟检查（exit_code={smoke_result.get('exit_code')}）",
                    str(smoke_result.get("output") or "")[:600],
                ]
            )
        if prog_tail.strip():
            parts.extend(["", "## 近期会话交接日志（progress.md）", prog_tail])
        return "\n".join(parts)


async def run_horizon_session(
    workspace: str | Path,
    campaign_id: str,
    runtime_factory: Callable[[], Any],
    *,
    run_smoke: bool = False,
    run_verification_cmd: bool = False,
    invariant_checker: MechanicalInvariantChecker | None = None,
) -> dict[str, Any]:
    """执行单次全新上下文窗口的 Horizon 增量会话，完成单特性推进、边界检查与交接落盘。"""
    ws = Path(workspace).resolve()
    mgr = HorizonCampaignManager(ws)
    target = mgr.next_pending_feature(campaign_id)
    if target is None:
        return {"ok": True, "campaign_id": campaign_id, "done": True, "message": "所有特性已通过验收"}

    smoke_res: dict[str, Any] | None = None
    if run_smoke:
        init_sh = mgr._campaign_dir(campaign_id) / "init.sh"
        if init_sh.is_file():
            proc = subprocess.run(
                ["bash", str(init_sh)],
                cwd=str(ws),
                env=build_scrubbed_env(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            smoke_res = {"exit_code": proc.returncode, "output": (proc.stdout + proc.stderr)[:1200]}

    git_log_txt = ""
    git_proc = subprocess.run(
        ["git", "log", "-n", "5", "--oneline"],
        cwd=str(ws),
        env=build_scrubbed_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if git_proc.returncode == 0:
        git_log_txt = git_proc.stdout.strip()

    boot_prompt = mgr.build_session_boot_prompt(
        campaign_id, feature=target, git_log_excerpt=git_log_txt, smoke_result=smoke_res
    )
    rt = runtime_factory()
    try:
        created = rt.create(
            boot_prompt,
            extra_metadata={"horizon_campaign_id": campaign_id, "horizon_feature_id": target["id"]},
        )
        final_run = await rt.execute(created["id"])
    finally:
        if hasattr(rt, "aclose"):
            await rt.aclose()

    checker = invariant_checker or MechanicalInvariantChecker.from_workspace(ws)
    inv_report = checker.check(ws)

    passed = False
    ver_out = ""
    ver_code = -1
    if final_run.get("status") == "completed" and inv_report["ok"]:
        if run_verification_cmd and target.get("verification_cmd"):
            vproc = subprocess.run(
                ["bash", "-c", str(target["verification_cmd"])],
                cwd=str(ws),
                env=build_scrubbed_env(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            ver_code = vproc.returncode
            ver_out = (vproc.stdout + vproc.stderr).strip() or f"exit_code={vproc.returncode}"
        else:
            ver_code = 0
            ver_out = str(final_run.get("answer") or "completed").strip()
        if ver_code == 0:
            mgr.mark_feature_passed(
                campaign_id,
                target["id"],
                run_id=final_run["id"],
                verification_output=ver_out,
                exit_code=0,
            )
            passed = True

    outcome = "passed" if passed else ("invariant_failed" if not inv_report["ok"] else str(final_run.get("status")))
    mgr.record_session_handoff(
        campaign_id,
        run_id=final_run["id"],
        feature_id=target["id"],
        outcome=outcome,
        summary=str(final_run.get("answer") or final_run.get("error") or outcome),
        invariant_report=inv_report,
    )
    updated_doc = mgr.get_campaign(campaign_id)
    return {
        "ok": passed,
        "campaign_id": campaign_id,
        "feature_id": target["id"],
        "run_id": final_run["id"],
        "outcome": outcome,
        "invariants": inv_report,
        "summary": updated_doc["summary"],
    }

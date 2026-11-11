"""多任务编码能力评测（Coding Benchmark）：独立 Git Worktree + 容器测试真值 + 失败分类。

设计边界与诚实性原则：
1. 与 evals.py（测 harness 控制流不变量）互补：本模块面向多任务真实仓库修复，使用受保护的
   unittest 套件在隔离 Docker 容器内判定业务正确性，绝不靠字符串匹配或模型自述判分。
2. 严格区分两类失败：
   - model_failure：补丁未修通测试、未产生有效修改、超出模型步数/修复轮次预算等。
   - infrastructure_failure：Docker 不可用、API 密钥缺失、模型网关连接失败等环境问题；
     基础设施失败单独统计，绝不混入模型失分，也不伪装成通过。
3. 提供 reference 确定性参考执行器仅用于离线/CI 验证评测流水线与修复循环本身，
   报告中明确标注 evaluation_scope = "benchmark_harness_verification_only"，绝不冒充真实 LLM 分数。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import uuid
from typing import Any

from .evals import pass_at_k
from .git_repo import create_task_worktree, init_git_repo_from_dir, worktree_status
from .providers import Provider
from .runtime import AgentRuntime
from .sandbox import SandboxRunner, discover_test_oracles
from .storage import Storage, digest

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = ROOT / "data/evals/coding_benchmark"

_INFRA_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "PermissionDeniedError",
    "InternalServerError",
    "RateLimitError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "HTTPStatusError",
    "TimeoutError",
}


def _call(name: str, **arguments: object) -> dict:
    return {"type": "tool_use", "id": "bench_" + uuid.uuid4().hex[:14], "name": name, "input": arguments}


def _resp(content: list[dict]) -> dict:
    return {
        "content": content,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


class BenchmarkReferenceProvider:
    """仅用于自检评测基础设施（含跨文件变更集与 1 轮测试失败自动修复），不代表真实模型能力。"""

    async def complete(
        self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict:
        results: list[dict] = []
        for msg in messages:
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        try:
                            results.append(json.loads(block["content"]))
                        except (ValueError, TypeError):
                            pass
        if not any("map" in r and "indexed_files" in r for r in results):
            return _resp([_call("repo_map", max_chars=2000), _call("repo_list")])

        files = next((r.get("files", []) for r in results if "files" in r), [])
        reads = {r["path"]: r["content"] for r in results if "path" in r and "content" in r}
        # 当文件结果超过 2600 字符触发有界引用投影时，按协议调用 tool_result_get 分页拉取完整源码。
        omitted_reads = [
            r for r in results if r.get("content_omitted") and r.get("tool") == "repo_read" and r.get("call_id")
        ]
        for omitted in omitted_reads:
            cid = omitted["call_id"]
            pages = [
                r
                for r in results
                if r.get("content_format") == "json_text_page" and r.get("call_id") == cid
            ]
            if not pages:
                return _resp([_call("tool_result_get", call_id=cid, offset=0, limit=1200)])
            if pages[-1].get("next_offset") is not None:
                return _resp(
                    [_call("tool_result_get", call_id=cid, offset=pages[-1]["next_offset"], limit=1200)]
                )
            assembled = json.loads("".join(p["content"] for p in pages))
            if "path" in assembled and "content" in assembled:
                reads[assembled["path"]] = assembled["content"]
        sandbox_runs = [
            r
            for r in results
            if r.get("code") in {"passed", "tests_failed", "no_tests_ran", "test_count_mismatch"}
            or str(r.get("code", "")).startswith("sandbox_")
        ]
        latest_sandbox = sandbox_runs[-1] if sandbox_runs else None
        if latest_sandbox and (
            latest_sandbox.get("code") == "passed"
            or str(latest_sandbox.get("code", "")).startswith("sandbox_")
        ):
            return _resp(
                [{"type": "text", "text": f"基准流水线参考修复结束，沙箱状态：{latest_sandbox.get('code')}。"}]
            )

        # 场景 1：coupon_settlement（多文件原子变更集）
        if "checkout.py" in files and "discounts.py" in files:
            if "discounts.py" not in reads or "checkout.py" not in reads:
                return _resp([_call("repo_read", path="discounts.py"), _call("repo_read", path="checkout.py")])
            if not sandbox_runs and any(r.get("proposal_id") for r in results):
                return _resp([_call("sandbox_test")])
            return _resp(
                [
                    _call(
                        "propose_changeset",
                        summary="修复优惠券上限截断与折后总额扣减",
                        edits=[
                            {
                                "path": "discounts.py",
                                "old_text": "    return raw_discount",
                                "new_text": "    if coupon.max_discount_cents is not None:\n        return min(raw_discount, coupon.max_discount_cents)\n    return raw_discount",
                            },
                            {
                                "path": "checkout.py",
                                "old_text": "    total = discount if coupon is not None else subtotal\n    applied_discount = subtotal - total",
                                "new_text": "    applied_discount = min(subtotal, discount)\n    total = subtotal - applied_discount",
                            },
                        ],
                    )
                ]
            )

        # 场景 2：cursor_pagination
        if "paginator.py" in files:
            if "paginator.py" not in reads:
                return _resp([_call("repo_read", path="paginator.py")])
            if not sandbox_runs and any(r.get("proposal_id") for r in results):
                return _resp([_call("sandbox_test")])
            return _resp(
                [
                    _call(
                        "propose_patch",
                        path="paginator.py",
                        old_text='        start_idx = matched[0]\n    window = records[start_idx : start_idx + limit]\n    has_more = len(window) >= limit',
                        new_text='        start_idx = matched[0] + 1\n    probe = records[start_idx : start_idx + limit + 1]\n    has_more = len(probe) > limit\n    window = probe[:limit]',
                    )
                ]
            )

        # 场景 3：retry_policy（演示第 1 次补丁漏掉 max_ms 上限 -> 容器测试失败 -> 第 1 轮自动修复通过）
        if "http_retry.py" in files:
            if "http_retry.py" not in reads:
                return _resp([_call("repo_read", path="http_retry.py")])
            proposals_count = sum(1 for r in results if r.get("proposal_id"))
            if proposals_count > len(sandbox_runs):
                return _resp([_call("sandbox_test")])
            if proposals_count == 0:
                # 首轮修复状态码与指数起点，但故意漏掉 min(max_ms, ...)，触发真实测试失败回传
                return _resp(
                    [
                        _call(
                            "propose_patch",
                            path="http_retry.py",
                            old_text='    retryable = status_code >= 400\n    if not retryable or attempt >= max_attempts:\n        return {"retry": False, "delay_ms": 0}\n    delay = base_ms * (2 ** attempt)',
                            new_text='    retryable = status_code == 429 or 500 <= status_code < 600\n    if not retryable or attempt >= max_attempts:\n        return {"retry": False, "delay_ms": 0}\n    delay = base_ms * (2 ** (attempt - 1))',
                        )
                    ]
                )
            return _resp(
                [
                    _call(
                        "propose_patch",
                        path="http_retry.py",
                        old_text="    delay = base_ms * (2 ** (attempt - 1))",
                        new_text="    delay = min(max_ms, base_ms * (2 ** (attempt - 1)))",
                    )
                ]
            )

        # 场景 4：window_merger（多文件原子变更集）
        if "intervals.py" in files and "merger.py" in files:
            if "intervals.py" not in reads or "merger.py" not in reads:
                return _resp([_call("repo_read", path="intervals.py"), _call("repo_read", path="merger.py")])
            if not sandbox_runs and any(r.get("proposal_id") for r in results):
                return _resp([_call("sandbox_test")])
            return _resp(
                [
                    _call(
                        "propose_changeset",
                        summary="按区间起点与终点联合排序，并合并相邻闭区间端点",
                        edits=[
                            {
                                "path": "intervals.py",
                                "old_text": "    return sorted(cleaned, key=lambda pair: pair[1])",
                                "new_text": "    return sorted(cleaned, key=lambda pair: (pair[0], pair[1]))",
                            },
                            {
                                "path": "merger.py",
                                "old_text": "        if start < cur_end:",
                                "new_text": "        if start <= cur_end:",
                            },
                        ],
                    )
                ]
            )

        # 场景 5：ttl_lru_cache
        if "cache.py" in files:
            if "cache.py" not in reads:
                return _resp([_call("repo_read", path="cache.py")])
            if not sandbox_runs and any(r.get("proposal_id") for r in results):
                return _resp([_call("sandbox_test")])
            return _resp(
                [
                    _call(
                        "propose_patch",
                        path="cache.py",
                        old_text="""    def _purge_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for key in expired:
            self._store.pop(key, None)

    def get(self, key: str, now: float) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if now > expires_at:
            self._store.pop(key, None)
            return None
        return value""",
                        new_text="""    def _purge_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._store.items() if now >= exp]
        for key in expired:
            self._store.pop(key, None)

    def get(self, key: str, now: float) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if now >= expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value""",
                    )
                ]
            )
        return _resp([{"type": "text", "text": "未识别的基准仓库。"}])


def _oracles_intact(workspace: Path, oracles: dict[str, str]) -> bool:
    for rel_path, expected_sha in oracles.items():
        target = workspace / rel_path
        if not target.is_file() or target.is_symlink():
            return False
        if digest(target.read_text(encoding="utf-8")) != expected_sha:
            return False
    return True


async def _run_single_benchmark_trial(
    task: dict[str, Any],
    trial: int,
    provider: str | Provider,
    benchmark_dir: Path,
    root: Path,
    max_steps: int,
    max_tokens: int,
    max_repair_rounds: int,
    verify_baseline: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    template_dir = benchmark_dir / task["repo_dir"]
    with tempfile.TemporaryDirectory(prefix=f"agent-bench-{task['id']}-") as temp:
        temp_root = Path(temp).resolve()
        origin_repo = temp_root / "origin_repo"
        worktree = temp_root / "worktree"
        base_commit = init_git_repo_from_dir(template_dir, origin_repo)
        git_meta = create_task_worktree(origin_repo, worktree, ref=base_commit)
        oracles, expected_tests = discover_test_oracles(worktree)

        baseline_check: dict[str, Any] = {"checked": False}
        if verify_baseline:
            base_res = SandboxRunner().run_suite(worktree, oracles, expected_tests=expected_tests)
            baseline_check = {"checked": True, "code": base_res.get("code")}
            if str(base_res.get("code", "")).startswith("sandbox_"):
                return {
                    "task_id": task["id"],
                    "title": task["title"],
                    "multi_file": bool(task.get("multi_file")),
                    "trial": trial,
                    "outcome": "infrastructure_failure",
                    "failure_category": "sandbox_unavailable",
                    "error_detail": base_res.get("code"),
                    "baseline_check": baseline_check,
                    "base_commit": base_commit,
                    "committed_sha": None,
                    "verification_status": "unavailable",
                    "tests_executed": False,
                    "repair_rounds_used": 0,
                    "steps": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "changed_files": [],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            if base_res.get("code") != "tests_failed":
                return {
                    "task_id": task["id"],
                    "title": task["title"],
                    "multi_file": bool(task.get("multi_file")),
                    "trial": trial,
                    "outcome": "infrastructure_failure",
                    "failure_category": "invalid_benchmark_baseline",
                    "error_detail": f"基线仓库预期失败，实际返回 {base_res.get('code')}",
                    "baseline_check": baseline_check,
                    "base_commit": base_commit,
                    "committed_sha": None,
                    "verification_status": "unknown",
                    "tests_executed": bool(base_res.get("tests_executed")),
                    "repair_rounds_used": 0,
                    "steps": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "changed_files": [],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }

        storage = Storage(temp_root / "state.db")
        resolved_provider: str | Provider = (
            BenchmarkReferenceProvider() if provider == "reference" else provider
        )
        try:
            runtime = AgentRuntime(
                storage=storage,
                workspace=worktree,
                knowledge_dir=root / "data/knowledge",
                skills_dir=root / "skills",
                provider=resolved_provider,
                max_steps=max_steps,
                max_tokens=max_tokens,
                max_repair_rounds=max_repair_rounds,
                commit_on_pass=True,
                auto_approve_proposals=True,
            )
        except ValueError as exc:
            return {
                "task_id": task["id"],
                "title": task["title"],
                "multi_file": bool(task.get("multi_file")),
                "trial": trial,
                "outcome": "infrastructure_failure",
                "failure_category": "missing_credentials",
                "error_detail": str(exc),
                "baseline_check": baseline_check,
                "base_commit": base_commit,
                "committed_sha": None,
                "verification_status": "not_run",
                "tests_executed": False,
                "repair_rounds_used": 0,
                "steps": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "changed_files": [],
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        try:
            created = runtime.create(
                task["prompt"],
                mode="agent",
                extra_metadata={
                    "git": git_meta,
                    "test_oracles": oracles,
                    "expected_tests": expected_tests,
                },
            )
            run = await runtime.execute(created["id"])
        finally:
            await runtime.aclose()

        ver = run.get("metadata", {}).get("verification", {})
        git_after = worktree_status(worktree, base_commit=base_commit)
        committed_sha = ver.get("committed_sha") or run.get("metadata", {}).get("git", {}).get("committed_sha")
        changed_files = sorted(
            {
                item["path"]
                for proposal in storage.proposals(run["id"])
                if proposal.get("applied")
                for item in (proposal.get("files") or [{"path": proposal["path"]}])
            }
        )

        # 独立考官重验：先检查测试文件哈希零篡改；若模型未运行容器测试但修改了源码，主动补跑容器套件判分。
        if not _oracles_intact(worktree, oracles):
            outcome = "model_failure"
            failure_category: str | None = "test_oracle_tampered"
            final_ver_status = "failed"
            tests_executed = False
        elif run["status"] == "failed" and run.get("error") in _INFRA_ERROR_NAMES:
            outcome = "infrastructure_failure"
            failure_category = "provider_network_or_auth_error"
            final_ver_status = ver.get("status", "not_run")
            tests_executed = bool(ver.get("tests_executed"))
        elif ver.get("status") == "unavailable":
            outcome = "infrastructure_failure"
            failure_category = "sandbox_unavailable"
            final_ver_status = "unavailable"
            tests_executed = False
        else:
            final_ver_status = ver.get("status", "not_run")
            tests_executed = bool(ver.get("tests_executed"))
            if final_ver_status != "passed" and changed_files:
                audit_res = SandboxRunner().run_suite(worktree, oracles, expected_tests=expected_tests)
                code = str(audit_res.get("code", ""))
                if code == "passed" and audit_res.get("tests_executed") is True:
                    final_ver_status = "passed"
                    tests_executed = True
                elif code.startswith("sandbox_"):
                    final_ver_status = "unavailable"
                    tests_executed = False
                else:
                    final_ver_status = "failed"
                    tests_executed = bool(audit_res.get("tests_executed"))

            if final_ver_status == "unavailable":
                outcome = "infrastructure_failure"
                failure_category = "sandbox_unavailable"
            elif final_ver_status == "passed" and tests_executed:
                outcome = "passed"
                failure_category = None
            else:
                outcome = "model_failure"
                if run["status"] == "budget_exceeded":
                    failure_category = "budget_exceeded"
                elif ver.get("exhausted"):
                    failure_category = "repair_exhausted"
                elif not changed_files:
                    failure_category = "no_patch_applied"
                else:
                    failure_category = "tests_still_failing"

        return {
            "task_id": task["id"],
            "title": task["title"],
            "multi_file": bool(task.get("multi_file")),
            "trial": trial,
            "outcome": outcome,
            "failure_category": failure_category,
            "run_status": run["status"],
            "run_error": run.get("error", ""),
            "verification_status": final_ver_status,
            "tests_executed": tests_executed,
            "expected_tests": expected_tests,
            "repair_rounds_used": int(ver.get("repair_rounds_used", 0)),
            "steps": int(run.get("steps", 0)),
            "usage": {
                "input_tokens": int(run.get("input_tokens", 0)),
                "output_tokens": int(run.get("output_tokens", 0)),
            },
            "baseline_check": baseline_check,
            "base_commit": base_commit,
            "committed_sha": committed_sha,
            "changed_files": changed_files,
            "diff_chars": len(git_after.get("diff", "")),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }


async def run_model_evals(
    provider: str | Provider = "anthropic",
    report_path: Path | None = None,
    trials: int = 1,
    task_ids: list[str] | None = None,
    max_steps: int = 16,
    max_tokens: int = 40000,
    max_repair_rounds: int = 2,
    verify_baseline: bool = True,
    root: Path | None = None,
) -> dict[str, Any]:
    """运行 5 任务编码基准评测，输出区分模型能力失分与基础设施异常的完整 JSON 报告。"""
    if isinstance(trials, bool) or not isinstance(trials, int) or not 1 <= trials <= 10:
        raise ValueError("trials 必须为 1—10 整数")
    root_dir = Path(root) if root is not None else ROOT
    bench_dir = root_dir / "data/evals/coding_benchmark"
    all_tasks = json.loads((bench_dir / "tasks.json").read_text(encoding="utf-8"))
    selected = [t for t in all_tasks if not task_ids or t["id"] in set(task_ids)]
    if not selected:
        raise ValueError("没有匹配的 benchmark 任务")

    provider_label = provider if isinstance(provider, str) else type(provider).__name__
    is_real_model = provider_label not in {"reference", "demo", "BenchmarkReferenceProvider"}
    model_name = None
    if provider_label == "anthropic":
        model_name = os.environ.get("ANTHROPIC_MODEL") or None
    elif provider_label in {"openai", "openai_compat"}:
        model_name = os.environ.get("OPENAI_MODEL") or None

    results = []
    for task in selected:
        for trial in range(1, trials + 1):
            item = await _run_single_benchmark_trial(
                task=task,
                trial=trial,
                provider=provider,
                benchmark_dir=bench_dir,
                root=root_dir,
                max_steps=max_steps,
                max_tokens=max_tokens,
                max_repair_rounds=max_repair_rounds,
                verify_baseline=verify_baseline,
            )
            results.append(item)

    passed_runs = sum(r["outcome"] == "passed" for r in results)
    model_failures = sum(r["outcome"] == "model_failure" for r in results)
    infra_failures = sum(r["outcome"] == "infrastructure_failure" for r in results)
    valid_runs = len(results) - infra_failures

    per_task_pass_at_1 = {}
    for task in selected:
        task_runs = [r for r in results if r["task_id"] == task["id"] and r["outcome"] != "infrastructure_failure"]
        if len(task_runs) == trials:
            successes = sum(r["outcome"] == "passed" for r in task_runs)
            per_task_pass_at_1[task["id"]] = round(pass_at_k(trials, successes, 1), 4)

    summary = {
        "tasks": len(selected),
        "trials_per_task": trials,
        "total_runs": len(results),
        "valid_runs": valid_runs,
        "passed_runs": passed_runs,
        "model_failures": model_failures,
        "infrastructure_failures": infra_failures,
        "pass_rate_valid": round(passed_runs / valid_runs, 4) if valid_runs > 0 else None,
        "pass_at_1_by_task": per_task_pass_at_1,
        "multi_file_passed": sum(r["outcome"] == "passed" and r["multi_file"] for r in results),
        "runs_repaired_after_failure": sum(
            r["outcome"] == "passed" and r["repair_rounds_used"] > 0 for r in results
        ),
        "total_repair_rounds_used": sum(r["repair_rounds_used"] for r in results),
        "total_input_tokens": sum(r["usage"]["input_tokens"] for r in results),
        "total_output_tokens": sum(r["usage"]["output_tokens"] for r in results),
        "avg_duration_ms": round(
            sum(r["duration_ms"] for r in results) / len(results), 2
        )
        if results
        else 0.0,
    }

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_label,
        "model": model_name,
        "evaluation_scope": (
            "real_model_coding_benchmark"
            if is_real_model
            else "benchmark_harness_verification_only"
        ),
        "honesty_note": (
            "真实模型评测：结果由独立 Git worktree 与隔离 Docker 容器测试判定，基础设施异常单独归类。"
            if is_real_model
            else "当前使用内置确定性 reference 执行器验证评测流水线、Git worktree 与测试驱动修复闭环；该分数不代表任何 LLM 的真实编码能力。"
        ),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "max_steps": max_steps,
            "max_tokens": max_tokens,
            "max_repair_rounds": max_repair_rounds,
            "baseline_verified_in_docker": verify_baseline,
        },
        "summary": summary,
        "results": results,
    }
    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report

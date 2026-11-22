"""带证据来源与哈希校验的结构化工作记忆（Working Memory）。

为什么需要它：
1. 超长任务在裁剪早期对话轮次后，模型容易遗忘业务约束、待办进度和已验证事实。
2. 如果直接让模型自由写文本摘要，错误摘要或过期判断会污染后续推理（Memory Poisoning）。
3. 本模块要求每条 verified_fact 必须绑定真实文件（SHA-256）或已提交工具调用（result_sha256）；
   一旦底层源码发生变化，再次构建上下文时自动将其标记为 stale，绝不冒充当前事实或替代审批记录。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .storage import Storage, digest

_MAX_MEMORY_CHARS = 2400
_MAX_FILE_BYTES = 512 * 1024
_MAX_BOUND_FILES = 64
_FORBIDDEN_DOT_PARTS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".kube", ".agent-lab"})
_SECRET_FILENAMES = frozenset(
    {
        "credentials.py",
        "secrets.py",
        "secret.py",
        "private_key.py",
        "tokens.py",
    }
)
_SECRET_STEMS = frozenset(
    {
        "credentials",
        "secrets",
        "secret",
        "private_key",
        "tokens",
        "api_key",
        "apikey",
    }
)
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".env"})


class WorkingMemoryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def empty_working_memory() -> dict[str, Any]:
    return {"goal": "", "constraints": [], "todos": [], "facts": []}


def _is_secret_segment(name: str) -> bool:
    lowered = name.casefold()
    stem = Path(name).stem.casefold()
    first_stem = lowered.split(".", 1)[0] if not lowered.startswith(".") else lowered[1:].split(".", 1)[0]
    return (
        lowered in _SECRET_FILENAMES
        or stem in _SECRET_STEMS
        or first_stem in _SECRET_STEMS
        or lowered.startswith((".env", "id_rsa", "id_ed25519", ".agent-apply-", ".agent-patch-"))
        or any(lowered.endswith(sfx) for sfx in _SECRET_SUFFIXES)
    )


def _safe_workspace_file_sha256(workspace: Path, relative: str) -> str | None:
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(
        part in ("", ".", "..") or part in _FORBIDDEN_DOT_PARTS or _is_secret_segment(part)
        for part in pure.parts
    ):
        return None
    root = Path(workspace).resolve()
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        return None
    info = candidate.stat()
    if info.st_nlink != 1 or info.st_size > _MAX_FILE_BYTES:
        return None
    try:
        return digest(candidate.read_text(encoding="utf-8"))
    except UnicodeError:
        return None


def _extract_tool_call_paths(
    storage: Storage, run_id: str, call: dict[str, Any], *, _depth: int = 0
) -> list[str]:
    """提取工具调用直接读取或定位的工作区文件路径（repo_list 仅列目录名，不绑定文件内容哈希）。"""
    name = call.get("name")
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    result = call.get("result")
    raw_paths: list[str] = []

    if name in {"repo_read", "verify_python", "propose_patch", "fs_read", "fs_write", "fs_edit"}:
        if isinstance(args.get("path"), str):
            raw_paths.append(args["path"])
        elif isinstance(result, dict) and isinstance(result.get("path"), str):
            raw_paths.append(result["path"])
    elif name == "propose_changeset":
        edits = args.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict) and isinstance(edit.get("path"), str):
                    raw_paths.append(edit["path"])
    elif name == "symbol_search" and isinstance(result, dict):
        for key in ("definitions", "references"):
            items = result.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        raw_paths.append(item["path"])
    elif name in {"repo_search", "fs_grep"} and isinstance(result, dict):
        matches = result.get("matches")
        if isinstance(matches, list):
            for item in matches:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    raw_paths.append(item["path"])
    elif name == "tool_result_get" and _depth < 1 and isinstance(args.get("call_id"), str):
        origin = storage.get_call(run_id, args["call_id"])
        if isinstance(origin, dict):
            raw_paths.extend(_extract_tool_call_paths(storage, run_id, origin, _depth=_depth + 1))

    seen: set[str] = set()
    unique_paths: list[str] = []
    for rel in raw_paths:
        rel_clean = rel.strip()
        if rel_clean and rel_clean not in seen:
            seen.add(rel_clean)
            unique_paths.append(rel_clean)
            if len(unique_paths) >= _MAX_BOUND_FILES:
                break
    return unique_paths


def _verify_fact_source(
    workspace: Path,
    storage: Storage,
    run_id: str,
    source_type: str,
    source_ref: str,
    expected_sha: str | None = None,
) -> tuple[str, dict[str, str]]:
    bound_files: dict[str, str] = {}
    if source_type == "file":
        actual = _safe_workspace_file_sha256(workspace, source_ref)
        if actual is None:
            raise WorkingMemoryError("unverified_source", f"无法核验工作区文件来源: {source_ref}")
    elif source_type == "tool_call":
        call = storage.get_call(run_id, source_ref)
        if call is None:
            raise WorkingMemoryError("unverified_source", f"工具调用尚未落盘或不存在: {source_ref}")
        result = call.get("result")
        if (
            call.get("status") != "succeeded"
            or call.get("result_sha256") is None
            or (isinstance(result, dict) and result.get("ok") is False)
        ):
            raise WorkingMemoryError("unverified_source", f"不能绑定失败或未成功的工具调用: {source_ref}")
        actual = call["result_sha256"]
        for rel in _extract_tool_call_paths(storage, run_id, call):
            file_sha = _safe_workspace_file_sha256(workspace, rel)
            if file_sha is not None:
                bound_files[rel] = file_sha
    else:
        raise WorkingMemoryError("invalid_source_type", "source_type 必须是 file 或 tool_call")
    if expected_sha is not None and expected_sha != actual:
        raise WorkingMemoryError(
            "source_hash_mismatch",
            f"提供的来源摘要与当前真实摘要不一致（expected={expected_sha[:12]}, actual={actual[:12]}）",
        )
    return actual, bound_files


def _fact_stale_reason(
    workspace: Path, storage: Storage, run_id: str, item: dict[str, Any]
) -> str | None:
    source_type = item.get("source_type")
    source_ref = item.get("source_ref", "")
    recorded_sha = item.get("source_sha256")
    if source_type == "file":
        current_sha = _safe_workspace_file_sha256(workspace, source_ref)
        if current_sha is None:
            return "file_missing_or_unsafe"
        if current_sha != recorded_sha:
            return "file_hash_changed"
        return None
    if source_type == "tool_call":
        call = storage.get_call(run_id, source_ref)
        if call is None or call.get("result_sha256") is None:
            return "tool_call_missing"
        result = call.get("result")
        if call.get("status") != "succeeded" or (
            isinstance(result, dict) and result.get("ok") is False
        ):
            return "tool_call_not_succeeded"
        if call["result_sha256"] != recorded_sha:
            return "tool_result_changed"
        bound_files = item.get("bound_files")
        if isinstance(bound_files, dict):
            for rel_path, recorded_file_sha in bound_files.items():
                if (
                    not isinstance(rel_path, str)
                    or not isinstance(recorded_file_sha, str)
                    or _safe_workspace_file_sha256(workspace, rel_path) != recorded_file_sha
                ):
                    return "bound_file_hash_changed"
        return None
    return "invalid_source_type"


def _budget_serialized_len(memory: dict[str, Any]) -> int:
    projected = {
        "goal": memory.get("goal", ""),
        "constraints": memory.get("constraints", []),
        "todos": memory.get("todos", []),
        "facts": [
            {
                "id": item.get("id", ""),
                "claim": item.get("claim", ""),
                "source_type": item.get("source_type", ""),
                "source_ref": item.get("source_ref", ""),
            }
            for item in memory.get("facts", [])
        ],
    }
    return len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")))


def update_working_memory(
    workspace: Path,
    storage: Storage,
    run_id: str,
    current: dict[str, Any] | None,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """校验来源真实性后更新结构化工作记忆，支持显式删除与过期事实自动淘汰。"""
    if not isinstance(patch, dict):
        raise WorkingMemoryError("working_memory_invalid", "patch 必须是字典")
    base = json.loads(json.dumps(current or empty_working_memory()))
    base.setdefault("goal", "")
    base.setdefault("constraints", [])
    base.setdefault("todos", [])
    base.setdefault("facts", [])

    if "goal" in patch and patch["goal"] is not None:
        goal = str(patch["goal"]).strip()
        if len(goal) > 300:
            raise WorkingMemoryError("working_memory_invalid", "goal 最多 300 字符")
        base["goal"] = goal

    if "constraints" in patch and patch["constraints"] is not None:
        if not isinstance(patch["constraints"], list):
            raise WorkingMemoryError("working_memory_invalid", "constraints 必须是列表")
        items = [str(item).strip() for item in patch["constraints"] if str(item).strip()]
        if len(items) > 8 or any(len(item) > 200 for item in items):
            raise WorkingMemoryError("working_memory_invalid", "最多保留 8 条约束，每条不超过 200 字符")
        base["constraints"] = items

    has_todo_ops = any(
        key in patch and patch[key] is not None
        for key in ("prune_done_todos", "remove_todo_ids", "todos")
    )
    if has_todo_ops:
        todo_map: dict[str, dict[str, Any]] = {item["id"]: item for item in base["todos"]}
        if patch.get("prune_done_todos") is not None and not isinstance(patch["prune_done_todos"], bool):
            raise WorkingMemoryError("working_memory_invalid", "prune_done_todos 必须是布尔值")
        if patch.get("prune_done_todos"):
            todo_map = {tid: item for tid, item in todo_map.items() if item.get("status") != "done"}
        if patch.get("remove_todo_ids") is not None:
            if not isinstance(patch["remove_todo_ids"], list):
                raise WorkingMemoryError("working_memory_invalid", "remove_todo_ids 必须是列表")
            for raw_tid in patch["remove_todo_ids"]:
                tid = str(raw_tid).strip()
                if tid:
                    todo_map.pop(tid, None)
        if "todos" in patch and patch["todos"] is not None:
            if not isinstance(patch["todos"], list):
                raise WorkingMemoryError("working_memory_invalid", "todos 必须是列表")
            for item in patch["todos"]:
                if not isinstance(item, dict):
                    raise WorkingMemoryError("working_memory_invalid", "todo 条目必须是对象")
                tid = str(item.get("id", "")).strip()
                status = str(item.get("status", "pending")).strip()
                if status == "deleted":
                    if not tid or len(tid) > 40:
                        raise WorkingMemoryError("working_memory_invalid", "todo 需要非空 id")
                    todo_map.pop(tid, None)
                    continue
                task = str(item.get("task", "")).strip()
                if not tid or len(tid) > 40 or not task or len(task) > 200:
                    raise WorkingMemoryError("working_memory_invalid", "todo 需要非空 id 与 task")
                if status not in {"pending", "in_progress", "done"}:
                    raise WorkingMemoryError(
                        "working_memory_invalid", "todo status 必须是 pending/in_progress/done/deleted"
                    )
                todo_map[tid] = {"id": tid, "task": task, "status": status}
        if len(todo_map) > 10:
            raise WorkingMemoryError("working_memory_budget_exceeded", "最多保存 10 个待办项")
        base["todos"] = list(todo_map.values())

    if patch.get("prune_stale_facts") is not None and not isinstance(patch["prune_stale_facts"], bool):
        raise WorkingMemoryError("working_memory_invalid", "prune_stale_facts 必须是布尔值")
    if patch.get("auto_evict_stale") is not None and not isinstance(patch["auto_evict_stale"], bool):
        raise WorkingMemoryError("working_memory_invalid", "auto_evict_stale 必须是布尔值")

    fact_map: dict[str, dict[str, Any]] = {item["id"]: item for item in base["facts"]}
    if patch.get("prune_stale_facts"):
        fact_map = {
            fid: item
            for fid, item in fact_map.items()
            if _fact_stale_reason(workspace, storage, run_id, item) is None
        }
    if patch.get("remove_fact_ids") is not None:
        if not isinstance(patch["remove_fact_ids"], list):
            raise WorkingMemoryError("working_memory_invalid", "remove_fact_ids 必须是列表")
        for raw_fid in patch["remove_fact_ids"]:
            fid = str(raw_fid).strip()
            if fid:
                fact_map.pop(fid, None)

    touched_fact_ids: set[str] = set()
    if "facts" in patch and patch["facts"] is not None:
        if not isinstance(patch["facts"], list):
            raise WorkingMemoryError("working_memory_invalid", "facts 必须是列表")
        now = datetime.now(timezone.utc).isoformat()
        for item in patch["facts"]:
            if not isinstance(item, dict):
                raise WorkingMemoryError("working_memory_invalid", "fact 条目必须是对象")
            fid = str(item.get("id", "")).strip()
            if str(item.get("status", "")).strip() == "deleted":
                if not fid or len(fid) > 40:
                    raise WorkingMemoryError("working_memory_invalid", "fact 需要非空 id")
                fact_map.pop(fid, None)
                continue
            claim = str(item.get("claim", "")).strip()
            source_type = str(item.get("source_type", "")).strip()
            source_ref = str(item.get("source_ref", "")).strip()
            supplied_sha = item.get("source_sha256")
            if not fid or len(fid) > 40 or not claim or len(claim) > 260 or not source_ref:
                raise WorkingMemoryError("working_memory_invalid", "fact 需要非空 id、claim 与 source_ref")
            verified_sha, bound_files = _verify_fact_source(
                workspace,
                storage,
                run_id,
                source_type,
                source_ref,
                str(supplied_sha) if supplied_sha else None,
            )
            entry: dict[str, Any] = {
                "id": fid,
                "claim": claim,
                "source_type": source_type,
                "source_ref": source_ref,
                "source_sha256": verified_sha,
                "recorded_at": now,
            }
            if bound_files:
                entry["bound_files"] = bound_files
            fact_map.pop(fid, None)
            fact_map[fid] = entry
            touched_fact_ids.add(fid)

    base["facts"] = list(fact_map.values())
    auto_evict_stale = patch.get("auto_evict_stale", True)
    if auto_evict_stale is None:
        auto_evict_stale = True

    if auto_evict_stale and (len(fact_map) > 12 or _budget_serialized_len(base) > _MAX_MEMORY_CHARS):
        stale_candidates = [
            (idx, item)
            for idx, item in enumerate(fact_map.values())
            if item["id"] not in touched_fact_ids
            and _fact_stale_reason(workspace, storage, run_id, item) is not None
        ]
        stale_candidates.sort(key=lambda pair: (str(pair[1].get("recorded_at", "")), pair[0]))
        for _, stale_item in stale_candidates:
            if len(fact_map) <= 12 and _budget_serialized_len(base) <= _MAX_MEMORY_CHARS:
                break
            fact_map.pop(stale_item["id"], None)
            base["facts"] = list(fact_map.values())

    if len(fact_map) > 12:
        raise WorkingMemoryError("working_memory_budget_exceeded", "最多保存 12 条带证据事实")

    if _budget_serialized_len(base) > _MAX_MEMORY_CHARS:
        raise WorkingMemoryError(
            "working_memory_budget_exceeded",
            f"结构化工作记忆超过 {_MAX_MEMORY_CHARS} 字符预算，请精简旧条目。",
        )
    return base


def audit_working_memory(
    workspace: Path, storage: Storage, run_id: str, memory: dict[str, Any] | None
) -> dict[str, Any]:
    """每次读取或注入上下文前实时重验来源哈希，把底层已改变的事实自动降级为 stale。"""
    if not memory:
        return {
            "goal": "",
            "constraints": [],
            "todos": [],
            "valid_facts": [],
            "stale_facts": [],
            "total_facts": 0,
            "stale_count": 0,
        }
    valid_facts: list[dict[str, Any]] = []
    stale_facts: list[dict[str, Any]] = []
    for item in memory.get("facts", []):
        stale_reason = _fact_stale_reason(workspace, storage, run_id, item)
        enriched = {**item, "status": "stale" if stale_reason else "valid"}
        if stale_reason:
            enriched["stale_reason"] = stale_reason
            stale_facts.append(enriched)
        else:
            valid_facts.append(enriched)
    return {
        "goal": memory.get("goal", ""),
        "constraints": list(memory.get("constraints", [])),
        "todos": list(memory.get("todos", [])),
        "valid_facts": valid_facts,
        "stale_facts": stale_facts,
        "total_facts": len(valid_facts) + len(stale_facts),
        "stale_count": len(stale_facts),
    }


def has_working_memory_content(memory: dict[str, Any] | None) -> bool:
    if not memory:
        return False
    return bool(
        memory.get("goal")
        or memory.get("constraints")
        or memory.get("todos")
        or memory.get("facts")
    )


def prompt_view_working_memory(audited: dict[str, Any]) -> dict[str, Any]:
    """生成供模型推理使用的确定性工作记忆视图：剥离 recorded_at 等墙钟时间戳噪声。"""
    def _clean_fact(item: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {
            "id": item.get("id", ""),
            "claim": item.get("claim", ""),
            "source_type": item.get("source_type", ""),
            "source_ref": item.get("source_ref", ""),
            "source_sha256": item.get("source_sha256", ""),
            "status": item.get("status", "valid"),
        }
        if item.get("stale_reason"):
            cleaned["stale_reason"] = item["stale_reason"]
        return cleaned

    return {
        "goal": audited.get("goal", ""),
        "constraints": list(audited.get("constraints", [])),
        "todos": list(audited.get("todos", [])),
        "valid_facts": [_clean_fact(f) for f in audited.get("valid_facts", [])],
        "stale_facts": [_clean_fact(f) for f in audited.get("stale_facts", [])],
        "total_facts": int(audited.get("total_facts", 0)),
        "stale_count": int(audited.get("stale_count", 0)),
    }

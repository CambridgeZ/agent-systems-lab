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
_MAX_FILE_BYTES = 64 * 1024


class WorkingMemoryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def empty_working_memory() -> dict[str, Any]:
    return {"goal": "", "constraints": [], "todos": [], "facts": []}


def _safe_workspace_file_sha256(workspace: Path, relative: str) -> str | None:
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ("", ".", "..") or part.startswith(".") for part in pure.parts):
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


def _verify_fact_source(
    workspace: Path,
    storage: Storage,
    run_id: str,
    source_type: str,
    source_ref: str,
    expected_sha: str | None = None,
) -> str:
    if source_type == "file":
        actual = _safe_workspace_file_sha256(workspace, source_ref)
        if actual is None:
            raise WorkingMemoryError("unverified_source", f"无法核验工作区文件来源: {source_ref}")
    elif source_type == "tool_call":
        call = storage.get_call(run_id, source_ref)
        if call is None or call.get("result_sha256") is None:
            raise WorkingMemoryError("unverified_source", f"工具调用尚未落盘或不存在: {source_ref}")
        actual = call["result_sha256"]
    else:
        raise WorkingMemoryError("invalid_source_type", "source_type 必须是 file 或 tool_call")
    if expected_sha is not None and expected_sha != actual:
        raise WorkingMemoryError(
            "source_hash_mismatch",
            f"提供的来源摘要与当前真实摘要不一致（expected={expected_sha[:12]}, actual={actual[:12]}）",
        )
    return actual


def update_working_memory(
    workspace: Path,
    storage: Storage,
    run_id: str,
    current: dict[str, Any] | None,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """校验来源真实性后更新结构化工作记忆，超出字符预算时明确拒绝而非静默截断。"""
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
        items = [str(item).strip() for item in patch["constraints"] if str(item).strip()]
        if len(items) > 8 or any(len(item) > 200 for item in items):
            raise WorkingMemoryError("working_memory_invalid", "最多保留 8 条约束，每条不超过 200 字符")
        base["constraints"] = items

    if "todos" in patch and patch["todos"] is not None:
        todo_map = {item["id"]: item for item in base["todos"]}
        for item in patch["todos"]:
            tid = str(item.get("id", "")).strip()
            task = str(item.get("task", "")).strip()
            status = str(item.get("status", "pending")).strip()
            if not tid or len(tid) > 40 or not task or len(task) > 200:
                raise WorkingMemoryError("working_memory_invalid", "todo 需要非空 id 与 task")
            if status not in {"pending", "in_progress", "done"}:
                raise WorkingMemoryError("working_memory_invalid", "todo status 必须是 pending/in_progress/done")
            todo_map[tid] = {"id": tid, "task": task, "status": status}
        if len(todo_map) > 10:
            raise WorkingMemoryError("working_memory_budget_exceeded", "最多保存 10 个待办项")
        base["todos"] = list(todo_map.values())

    if "facts" in patch and patch["facts"] is not None:
        fact_map = {item["id"]: item for item in base["facts"]}
        now = datetime.now(timezone.utc).isoformat()
        for item in patch["facts"]:
            fid = str(item.get("id", "")).strip()
            claim = str(item.get("claim", "")).strip()
            source_type = str(item.get("source_type", "")).strip()
            source_ref = str(item.get("source_ref", "")).strip()
            supplied_sha = item.get("source_sha256")
            if not fid or len(fid) > 40 or not claim or len(claim) > 260 or not source_ref:
                raise WorkingMemoryError("working_memory_invalid", "fact 需要非空 id、claim 与 source_ref")
            verified_sha = _verify_fact_source(
                workspace,
                storage,
                run_id,
                source_type,
                source_ref,
                str(supplied_sha) if supplied_sha else None,
            )
            fact_map[fid] = {
                "id": fid,
                "claim": claim,
                "source_type": source_type,
                "source_ref": source_ref,
                "source_sha256": verified_sha,
                "recorded_at": now,
            }
        if len(fact_map) > 12:
            raise WorkingMemoryError("working_memory_budget_exceeded", "最多保存 12 条带证据事实")
        base["facts"] = list(fact_map.values())

    serialized = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > _MAX_MEMORY_CHARS:
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
        source_type = item.get("source_type")
        source_ref = item.get("source_ref", "")
        recorded_sha = item.get("source_sha256")
        stale_reason = None
        if source_type == "file":
            current_sha = _safe_workspace_file_sha256(workspace, source_ref)
            if current_sha is None:
                stale_reason = "file_missing_or_unsafe"
            elif current_sha != recorded_sha:
                stale_reason = "file_hash_changed"
        elif source_type == "tool_call":
            call = storage.get_call(run_id, source_ref)
            if call is None or call.get("result_sha256") is None:
                stale_reason = "tool_call_missing"
            elif call["result_sha256"] != recorded_sha:
                stale_reason = "tool_result_changed"
        else:
            stale_reason = "invalid_source_type"

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

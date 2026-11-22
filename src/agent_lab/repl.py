"""交互式终端工作台（`agent-lab code`）：类似 Claude Code / Codex CLI 的本地多轮会话与内联审批驱动器。"""

from __future__ import annotations

from typing import Any, Callable

from .git_repo import is_git_worktree, worktree_status
from .permissions import normalize_permission_mode
from .runtime import AgentRuntime


def _format_proposal_preview(proposal: dict[str, Any]) -> str:
    kind = proposal.get("kind", "single_file")
    lines = [f"\n┌── 待人工确认动作 [{kind}] id={proposal['id'][:8]} ──"]
    if kind == "command":
        args = proposal.get("arguments") or {}
        lines.append(f"│ 目录 (cwd) : {args.get('cwd', '.')}")
        lines.append(f"│ 风险级别   : {proposal.get('risk_level', 'mutating')}")
        lines.append(f"│ 终端命令   : $ {args.get('command', '')}")
    elif kind == "gui_action":
        lines.append(f"│ GUI 工具   : {proposal.get('tool', '')}")
        lines.append(f"│ 参数       : {proposal.get('arguments', {})}")
    elif kind == "mcp_call":
        lines.append(f"│ MCP 工具   : {proposal.get('tool', '')}")
        lines.append(f"│ 参数       : {proposal.get('arguments', {})}")
    else:
        paths = proposal.get("paths") or ([proposal["path"]] if proposal.get("path") else [])
        lines.append(f"│ 目标文件   : {', '.join(paths)}")
        if proposal.get("summary"):
            lines.append(f"│ 修改说明   : {proposal['summary']}")
        preview = (proposal.get("new_content") or "")[:1200]
        if preview:
            lines.append("│ 内容预览   :")
            for pline in preview.splitlines()[:24]:
                lines.append(f"│   {pline}")
    lines.append("└" + "─" * 46)
    return "\n".join(lines)


async def drain_approvals_interactively(
    runtime: AgentRuntime,
    run: dict[str, Any],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    """当任务处于 waiting_approval 时，在终端内联提示确认，支持一键切换权限档位。"""
    current = run
    while current.get("status") == "waiting_approval":
        waiting_ids = (current.get("metadata") or {}).get("waiting_proposal_ids") or []
        if not waiting_ids:
            break
        pid = waiting_ids[0]
        proposal = runtime.storage.get_proposal(pid)
        if not proposal:
            break
        output_fn(_format_proposal_preview(proposal))
        try:
            raw_choice = input_fn(
                "允许执行？ [y=批准 / n=拒绝 / a=切至auto-edit并批准 / f=切至full-auto并批准]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            output_fn("\n已暂停审批，稍后可通过 --resume 恢复。")
            return current

        if raw_choice in {"a", "auto", "auto-edit"}:
            runtime.set_permission_mode(current["id"], "auto-edit")
            output_fn("[权限已切换为 auto-edit]")
            approved = True
        elif raw_choice in {"f", "full", "full-auto", "yolo"}:
            runtime.set_permission_mode(current["id"], "full-auto")
            output_fn("[权限已切换为 full-auto]")
            approved = True
        elif raw_choice in {"y", "yes", ""}:
            approved = True
        else:
            approved = False

        current = await runtime.approve(current["id"], pid, approved)
    return current


def _render_turn_events(
    runtime: AgentRuntime,
    run_id: str,
    last_event_id: int,
    output_fn: Callable[[str], None],
) -> int:
    events = runtime.storage.events(run_id, after=last_event_id)
    new_cursor = last_event_id
    for ev in events:
        new_cursor = max(new_cursor, int(ev["id"]))
        kind = ev["kind"]
        payload = ev.get("payload") or {}
        if kind == "tool_result":
            tname = payload.get("name", "tool")
            res = payload.get("result") or {}
            status_tag = "OK" if res.get("ok", True) else f"ERR:{res.get('code', 'failed')}"
            if res.get("requires_approval"):
                status_tag = "WAITING_APPROVAL"
            output_fn(f"  ● {tname} -> {status_tag}")
            if tname == "bash_exec" and isinstance(res.get("stdout"), str) and res["stdout"].strip():
                snippet = "\n".join("    │ " + ln for ln in res["stdout"].strip().splitlines()[:8])
                output_fn(snippet)
        elif kind == "subagents_finished":
            output_fn(f"  ● subagent_run 完成 ({payload.get('count')} 个子 Agent: {payload.get('roles')})")
        elif kind == "memory_note_written":
            output_fn(f"  ● [记忆写入] {payload.get('layer')}:{payload.get('path')} ({payload.get('status')})")
        elif kind == "memory_dreamed":
            output_fn(
                f"  ● [Dreaming 完成] 纠错={payload.get('corrections_count', 0)} · "
                f"Wiki更新={payload.get('pages_written_count', 0)} · "
                f"断链修复={payload.get('links_repaired_count', 0)}"
            )
    return new_cursor


async def run_interactive_repl(
    runtime: AgentRuntime,
    *,
    initial_prompt: str | None = None,
    resume_run_id: str | None = None,
    mode: str = "agent",
    extra_metadata: dict[str, Any] | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict[str, Any] | None:
    """运行多轮交互式终端工作台，返回最后一次任务的最终字典状态。"""
    current_run: dict[str, Any] | None = None
    event_cursor = 0
    meta_template = dict(extra_metadata or {})
    meta_template["permission_mode"] = runtime.permission_mode

    output_fn(
        f"Agent Systems Lab · 本地操控工作台 | 目录: {runtime.workspace} | "
        f"档位: {runtime.execution_profile} | 权限: {runtime.permission_mode} | 模型: {runtime.provider_name}"
    )
    output_fn(
        "输入任务开始执行；斜杠命令：/mode <ask|auto-edit|full-auto> · /dream [goal] · /memory · /status · /diff · /clear · /exit"
    )

    if resume_run_id:
        current_run = await runtime.resume(resume_run_id)
        resumed_perm = (current_run.get("metadata") or {}).get("permission_mode")
        if isinstance(resumed_perm, str) and resumed_perm:
            runtime.permission_mode = normalize_permission_mode(resumed_perm)
            meta_template["permission_mode"] = runtime.permission_mode
        event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
        current_run = await drain_approvals_interactively(
            runtime, current_run, input_fn=input_fn, output_fn=output_fn
        )
        event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
        if current_run.get("answer"):
            output_fn(f"\n{current_run['answer']}\n")

    pending_first = initial_prompt.strip() if initial_prompt and initial_prompt.strip() else None

    while True:
        if pending_first is not None:
            user_text = pending_first
            pending_first = None
        else:
            try:
                user_text = input_fn(f"\n[{runtime.permission_mode}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                output_fn("\n退出会话。")
                break

        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            output_fn("退出会话。")
            break
        if user_text == "/help":
            output_fn(
                "可用命令：\n"
                "  /mode <ask|auto-edit|full-auto>  切换当前会话权限策略\n"
                "  /dream [focus_goal]              立即运行离线 Dreaming（Transcript 纠错 + Wiki 合成 + 断链修复）\n"
                "  /memory                          查看当前全局与项目 MEMORY.md 顶层索引与 Wiki 统计\n"
                "  /status                          查看当前会话轮次、token 与缓存命中率\n"
                "  /diff                            查看当前 Git 工作区变更摘要\n"
                "  /clear                           开启全新会话上下文\n"
                "  /exit                            退出交互工作台"
            )
            continue
        if user_text == "/memory":
            ov = runtime._memory_store(current_run).overview()
            gs = ov["graph"]["stats"]
            output_fn(
                f"记忆库概览 | 项目标识: {ov['project_slug']} | "
                f"MEMORY.md 行数: {ov['index_budget']['used_lines']}/{ov['index_budget']['max_lines']} | "
                f"Wiki页: {gs['pages']} (已核验={gs['confirmed']}, 假设={gs['hypothesis']}, 边={gs['edges']}, 断链={gs['broken_links']})"
            )
            idx_preview = runtime._memory_store(current_run).render_pinned_index()
            if idx_preview:
                output_fn(idx_preview)
            continue
        if user_text.startswith("/dream"):
            goal_arg = user_text[6:].strip() or None
            rep = await runtime.dream(
                run_id=current_run["id"] if current_run else None,
                focus_goal=goal_arg,
                trigger="repl",
            )
            output_fn(
                f"[Dreaming 整理完成] 重放会话={rep['sessions_replayed']} · "
                f"纠错={rep['corrections_count']} · Wiki更新={rep['pages_written_count']} · "
                f"断链修复={rep['links_repaired_count']}"
            )
            continue
        if user_text.startswith("/mode"):
            parts = user_text.split(maxsplit=1)
            if len(parts) < 2:
                output_fn(f"当前权限模式：{runtime.permission_mode}（可选：ask / auto-edit / full-auto）")
                continue
            try:
                next_mode = normalize_permission_mode(parts[1])
            except ValueError as exc:
                output_fn(str(exc))
                continue
            if current_run is not None:
                current_run = runtime.set_permission_mode(current_run["id"], next_mode)
            else:
                runtime.permission_mode = next_mode
            meta_template["permission_mode"] = runtime.permission_mode
            output_fn(f"权限模式已切换为：{runtime.permission_mode}")
            continue
        if user_text == "/clear":
            current_run = None
            event_cursor = 0
            output_fn("已清空当前会话上下文，下一条输入将创建新会话。")
            continue
        if user_text == "/status":
            if current_run is None:
                output_fn(f"尚未发起任务 | 目录: {runtime.workspace} | 权限: {runtime.permission_mode}")
            else:
                fresh = runtime.storage.get_run(current_run["id"]) or current_run
                kv = (fresh.get("metadata") or {}).get("kv_cache") or {}
                hit_pct = round(float(kv.get("cumulative_hit_rate", 0.0)) * 100, 1)
                output_fn(
                    f"Run ID: {fresh['id']} | 状态: {fresh['status']} | 轮次: {fresh['steps']} | "
                    f"Tokens: {fresh['input_tokens'] + fresh['output_tokens']} | KV 命中率: {hit_pct}%"
                )
            continue
        if user_text == "/diff":
            if is_git_worktree(runtime.workspace):
                st = worktree_status(runtime.workspace)
                output_fn(st.get("diff") or "（Git 工作区当前干净，无未提交差异）")
            else:
                output_fn("当前目录不是 Git 仓库。")
            continue

        if current_run is None:
            meta_template["permission_mode"] = runtime.permission_mode
            created = runtime.create(user_text, mode=mode, extra_metadata=meta_template)
            current_run = await runtime.execute(created["id"])
        else:
            if current_run.get("status") == "waiting_approval":
                current_run = await drain_approvals_interactively(
                    runtime, current_run, input_fn=input_fn, output_fn=output_fn
                )
                if current_run.get("status") == "waiting_approval":
                    continue
            current_run = await runtime.send_message(current_run["id"], user_text)

        event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
        if current_run.get("status") == "waiting_approval":
            current_run = await drain_approvals_interactively(
                runtime, current_run, input_fn=input_fn, output_fn=output_fn
            )
            event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)

        if current_run.get("answer"):
            output_fn(f"\n{current_run['answer']}")
        if current_run.get("error"):
            output_fn(f"\n[停止原因: {current_run['status']} — {current_run['error']}]")

    return current_run

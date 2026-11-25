"""Deterministic offline regression tests for REPL upgrades and Web UI security/streaming guards."""

from __future__ import annotations

from collections import Counter
import asyncio
import os
from pathlib import Path
import re
import signal
from typing import Any
import uuid

from fastapi.testclient import TestClient

from agent_lab import repl as repl_mod
from agent_lab.api import SESSION_COOKIE_NAME, create_app
from agent_lab.repl import (
    _expand_at_file_mentions,
    _format_proposal_preview,
    drain_approvals_interactively,
    run_interactive_repl,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
WEB_INDEX = ROOT / "web" / "index.html"
WEB_APP_JS = ROOT / "web" / "app.js"

_JS_COMMENT_OR_STRING_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|`(?:\\.|[^`\\])*`"
    r"|/\*[\s\S]*?\*/"
    r"|//[^\n]*"
)


def _call(name: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": "upg_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict[str, Any]], *, in_tok: int = 20, out_tok: int = 12) -> dict[str, Any]:
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def _make_runtime(
    tmp_path: Path,
    provider: Any,
    *,
    permission_mode: str = "ask",
    execution_profile: str = "host",
    auto_dream: bool = False,
    max_steps: int = 6,
) -> tuple[Path, Storage, AgentRuntime]:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / "state.db")
    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=provider,
        execution_profile=execution_profile,
        permission_mode=permission_mode,
        auto_dream=auto_dream,
        max_steps=max_steps,
    )
    return ws, storage, runtime


def _strip_js_comments(source: str) -> str:
    return _JS_COMMENT_OR_STRING_RE.sub(
        lambda m: m.group(0) if m.group(0)[0] in {'"', "'", "`"} else "",
        source,
    )


def _strip_html_comments(source: str) -> str:
    return re.sub(r"<!--[\s\S]*?-->", "", source)


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


# ---------------------------------------------------------------------------
# 1. Blank-line anti-auto-approval hazard in drain_approvals_interactively
# ---------------------------------------------------------------------------


async def test_repl_blank_line_does_not_auto_approve_and_rejects_on_n(tmp_path: Path) -> None:
    class WriteOnceProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp([_call("fs_write", path="danger.txt", content="should_not_write\n")])
            return _resp([{"type": "text", "text": "已尊重拒绝决定，未写入文件。"}])

    ws, storage, runtime = _make_runtime(tmp_path, WriteOnceProvider(), permission_mode="ask")
    created = runtime.create("写入 danger.txt")
    paused = await runtime.execute(created["id"])
    assert paused["status"] == "waiting_approval"
    pid = paused["metadata"]["waiting_proposal_ids"][0]

    inputs = iter(["", "   ", "n"])
    prompt_calls: list[str] = []
    printed: list[str] = []

    def _input(prompt: str) -> str:
        prompt_calls.append(prompt)
        return next(inputs)

    final_run = await drain_approvals_interactively(
        runtime, paused, input_fn=_input, output_fn=printed.append
    )
    await runtime.aclose()

    assert len(prompt_calls) == 3
    joined = "\n".join(printed)
    assert joined.count("空回车不再自动批准") == 2
    assert final_run["status"] == "completed"
    proposal = storage.get_proposal(pid)
    assert proposal is not None
    assert proposal["status"] == "rejected"
    assert proposal["applied"] is False
    assert not (ws / "danger.txt").exists()


async def test_repl_three_blank_lines_pause_without_approving(
    tmp_path: Path, monkeypatch
) -> None:
    class WriteOnceProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([_call("fs_write", path="danger.txt", content="mutated\n")])

    ws, storage, runtime = _make_runtime(tmp_path, WriteOnceProvider(), permission_mode="ask")
    created = runtime.create("写入 danger.txt")
    paused = await runtime.execute(created["id"])
    assert paused["status"] == "waiting_approval"
    pid = paused["metadata"]["waiting_proposal_ids"][0]

    approve_calls: list[tuple[Any, ...]] = []
    orig_approve = runtime.approve

    async def _spy_approve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        approve_calls.append((args, kwargs))
        return await orig_approve(*args, **kwargs)

    monkeypatch.setattr(runtime, "approve", _spy_approve)

    inputs = iter(["", " ", "\t"])
    printed: list[str] = []
    after_pause = await drain_approvals_interactively(
        runtime,
        paused,
        input_fn=lambda _p: next(inputs),
        output_fn=printed.append,
    )
    await runtime.aclose()

    assert approve_calls == []
    assert after_pause["status"] == "waiting_approval"
    proposal = storage.get_proposal(pid)
    assert proposal is not None
    assert proposal["status"] == "pending"
    assert proposal["applied"] is False
    assert not (ws / "danger.txt").exists()
    joined = "\n".join(printed)
    assert joined.count("空回车不再自动批准") == 3
    assert "连续空输入，已安全暂停审批（未批准任何操作）" in joined


# ---------------------------------------------------------------------------
# 2. Ctrl-C (KeyboardInterrupt / SIGINT) during an active turn keeps REPL alive
# ---------------------------------------------------------------------------


async def test_repl_ctrl_c_keyboard_interrupt_during_active_turn_cancels_and_stays_alive(
    tmp_path: Path, monkeypatch
) -> None:
    class IdleProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([{"type": "text", "text": "unreachable"}])

    _, storage, runtime = _make_runtime(tmp_path, IdleProvider(), permission_mode="auto-edit")

    cancel_calls: list[tuple[str, str]] = []
    orig_cancel = runtime.cancel_active_run

    def _spy_cancel(run_id: str, *, actor_id: str | None = None, reason: str = "user_requested"):
        cancel_calls.append((run_id, reason))
        return orig_cancel(run_id, actor_id=actor_id, reason=reason)

    monkeypatch.setattr(runtime, "cancel_active_run", _spy_cancel)

    # Deliver KeyboardInterrupt directly to `await turn_task` inside _execute_interruptible_turn
    # (CPython's Task._step re-raises KeyboardInterrupt out of the event loop, so using a Future
    # deterministically exercises the `except (KeyboardInterrupt, asyncio.CancelledError):` path.)
    def _raise_ki_task(coro: Any) -> asyncio.Future[dict[str, Any]]:
        coro.close()
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        fut.set_exception(KeyboardInterrupt())
        return fut

    monkeypatch.setattr(repl_mod.asyncio, "create_task", _raise_ki_task)

    prompt_actions = iter(["/status", KeyboardInterrupt, "/exit"])
    printed: list[str] = []

    def _input(_prompt: str) -> str:
        item = next(prompt_actions)
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt()
        return str(item)

    final_run = await run_interactive_repl(
        runtime,
        initial_prompt="执行耗时推理并被 KeyboardInterrupt 中断",
        input_fn=_input,
        output_fn=printed.append,
    )
    await runtime.aclose()

    assert final_run is not None
    run_id = final_run["id"]
    assert cancel_calls == [(run_id, "repl_keyboard_interrupt")]

    stored = storage.get_run(run_id)
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["metadata"].get("cancel_requested") is True
    assert stored["metadata"].get("cancel_reason") == "repl_keyboard_interrupt"

    joined = "\n".join(printed)
    assert "已中断当前生成回合，会话仍保持活跃" in joined
    assert f"Run ID: {run_id}" in joined
    assert "状态: cancelled" in joined
    assert "输入 /exit 或按 Ctrl-D 退出工作台" in joined
    assert "退出会话。" in joined


async def test_repl_sigint_during_live_provider_turn_cancels_and_stays_alive(
    tmp_path: Path, monkeypatch
) -> None:
    provider_entered = False

    class SigintDuringTurnProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            nonlocal provider_entered
            provider_entered = True
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(5)
            return _resp([{"type": "text", "text": "should_be_cancelled"}])

    _, storage, runtime = _make_runtime(
        tmp_path, SigintDuringTurnProvider(), permission_mode="auto-edit"
    )

    cancel_calls: list[tuple[str, str]] = []
    orig_cancel = runtime.cancel_active_run

    def _spy_cancel(run_id: str, *, actor_id: str | None = None, reason: str = "user_requested"):
        cancel_calls.append((run_id, reason))
        return orig_cancel(run_id, actor_id=actor_id, reason=reason)

    monkeypatch.setattr(runtime, "cancel_active_run", _spy_cancel)
    monkeypatch.setattr(
        repl_mod, "_build_tty_prompt_reader", lambda _rt, *, input_fn, output_fn: input_fn
    )
    monkeypatch.setattr(repl_mod, "_is_interactive_tty", lambda _in, _out: True)

    prev_handler = signal.getsignal(signal.SIGINT)
    scripted = iter(["/status", "/exit"])
    printed: list[str] = []

    final_run = await run_interactive_repl(
        runtime,
        initial_prompt="在真实活跃回合中发送 SIGINT",
        input_fn=lambda _p: next(scripted),
        output_fn=printed.append,
    )
    await runtime.aclose()

    assert provider_entered is True
    assert signal.getsignal(signal.SIGINT) is prev_handler
    assert final_run is not None
    run_id = final_run["id"]
    assert any(rid == run_id and reason == "repl_sigint" for rid, reason in cancel_calls)

    stored = storage.get_run(run_id)
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["metadata"].get("cancel_requested") is True

    joined = "\n".join(printed)
    assert "已中断当前生成回合，会话仍保持活跃" in joined
    assert f"Run ID: {run_id}" in joined
    assert "退出会话。" in joined


# ---------------------------------------------------------------------------
# 3. Unified diff preview, [p] pattern grant, @path expansion, new slash cmds
# ---------------------------------------------------------------------------


def test_repl_unified_diff_preview_shows_changes_past_line_24(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    orig_lines = [f"ITEM_{i:02d} = {i}" for i in range(1, 46)]
    orig_text = "\n".join(orig_lines) + "\n"
    (ws / "src" / "service.py").write_text(orig_text, encoding="utf-8")

    new_lines = list(orig_lines)
    new_lines[35] = "ITEM_36 = 3600"  # line 36 (well past line 24)
    new_text = "\n".join(new_lines) + "\n"

    # Single-file proposal without precomputed diff; old_content omitted so workspace file is read
    proposal_single = {
        "id": "prop12345678",
        "kind": "single_file",
        "path": "src/service.py",
        "summary": "更新第 36 行常量",
        "new_content": new_text,
        "suggested_grant_pattern": "Edit(src/**)",
    }
    plain_card = _format_proposal_preview(proposal_single, workspace=ws, use_color=False)
    assert "统一差异 (Unified Diff)" in plain_card
    assert "@@ -33," in plain_card
    assert "-ITEM_36 = 36" in plain_card
    assert "+ITEM_36 = 3600" in plain_card
    assert "ITEM_01 = 1" not in plain_card
    assert "会话模式授权: Edit(src/**)" in plain_card

    colored_card = _format_proposal_preview(proposal_single, workspace=ws, use_color=True)
    assert "\x1b[36m@@" in colored_card
    assert "\x1b[31m-ITEM_36 = 36\x1b[0m" in colored_card
    assert "\x1b[32m+ITEM_36 = 3600\x1b[0m" in colored_card

    # Multi-file changeset modifying line 30
    multi_lines = [f"CFG_{i:02d} = {i}" for i in range(1, 38)]
    multi_old = "\n".join(multi_lines) + "\n"
    multi_new_lines = list(multi_lines)
    multi_new_lines[29] = "CFG_30 = 999"
    proposal_multi = {
        "id": "multi98765432",
        "kind": "changeset",
        "paths": ["src/config.py"],
        "files": [
            {
                "path": "src/config.py",
                "old_content": multi_old,
                "new_content": "\n".join(multi_new_lines) + "\n",
            }
        ],
    }
    multi_card = _format_proposal_preview(proposal_multi, workspace=ws, use_color=False)
    assert "@@ -27," in multi_card
    assert "-CFG_30 = 30" in multi_card
    assert "+CFG_30 = 999" in multi_card


async def test_repl_session_pattern_grant_p_approves_and_persists_grant_pattern(
    tmp_path: Path, monkeypatch
) -> None:
    class TwoEditsProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp(
                    [_call("fs_edit", path="src/alpha.py", old_text="VAL = 1", new_text="VAL = 2")]
                )
            if self.calls == 2:
                return _resp(
                    [_call("fs_edit", path="src/beta.py", old_text="VAL = 10", new_text="VAL = 20")]
                )
            return _resp([{"type": "text", "text": "两次 src/ 编辑均已完成。"}])

    ws, storage, runtime = _make_runtime(tmp_path, TwoEditsProvider(), permission_mode="ask")
    (ws / "src").mkdir()
    (ws / "src" / "alpha.py").write_text("VAL = 1\n", encoding="utf-8")
    (ws / "src" / "beta.py").write_text("VAL = 10\n", encoding="utf-8")

    created = runtime.create("依次修改 src/alpha.py 和 src/beta.py")
    paused = await runtime.execute(created["id"])
    assert paused["status"] == "waiting_approval"
    pid = paused["metadata"]["waiting_proposal_ids"][0]
    expected_pattern = paused["metadata"]["proposal_grant_suggestions"][pid]
    assert expected_pattern == "Edit(src/**)"

    orig_get_proposal = storage.get_proposal

    def _enriched_get_proposal(proposal_id: str) -> dict[str, Any] | None:
        prop = orig_get_proposal(proposal_id)
        if prop is not None and "suggested_grant_pattern" not in prop:
            run_doc = storage.get_run(prop["run_id"])
            sug = ((run_doc or {}).get("metadata") or {}).get("proposal_grant_suggestions", {}).get(
                proposal_id
            )
            if sug:
                prop = {**prop, "suggested_grant_pattern": sug}
        return prop

    monkeypatch.setattr(storage, "get_proposal", _enriched_get_proposal)

    approve_calls: list[tuple[str, str, bool, str | None]] = []
    orig_approve = runtime.approve

    async def _spy_approve(
        run_id: str, proposal_id: str, approved: bool, grant_pattern: str | None = None
    ) -> dict[str, Any]:
        approve_calls.append((run_id, proposal_id, approved, grant_pattern))
        return await orig_approve(run_id, proposal_id, approved, grant_pattern=grant_pattern)

    monkeypatch.setattr(runtime, "approve", _spy_approve)

    prompts_seen: list[str] = []
    printed: list[str] = []
    inputs = iter(["p"])

    def _input(prompt: str) -> str:
        prompts_seen.append(prompt)
        return next(inputs)

    final_run = await drain_approvals_interactively(
        runtime, paused, input_fn=_input, output_fn=printed.append
    )
    await runtime.aclose()

    # Only prompted once: the second fs_edit under src/ was auto-allowed by Edit(src/**)
    assert len(prompts_seen) == 1
    assert "p=本会话允许该模式" in prompts_seen[0]
    assert approve_calls == [(created["id"], pid, True, "Edit(src/**)")]
    assert final_run["status"] == "completed"
    assert "Edit(src/**)" in final_run["metadata"].get("session_grants", [])
    assert (ws / "src" / "alpha.py").read_text(encoding="utf-8") == "VAL = 2\n"
    assert (ws / "src" / "beta.py").read_text(encoding="utf-8") == "VAL = 20\n"
    joined = "\n".join(printed)
    assert "会话模式授权: Edit(src/**)" in joined
    assert "[已为本会话添加模式放行规则：Edit(src/**)]" in joined

    # Negative guard: pressing 'p' when no suggested_grant_pattern exists must NOT approve
    class SingleWriteProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp([_call("fs_write", path="no_pattern.txt", content="blocked\n")])
            return _resp([{"type": "text", "text": "已拒绝。"}])

    ws2, storage2, runtime2 = _make_runtime(
        tmp_path / "no_pat", SingleWriteProvider(), permission_mode="ask"
    )
    r2 = runtime2.create("写入无模式提案")
    p2 = await runtime2.execute(r2["id"])
    pid2 = p2["metadata"]["waiting_proposal_ids"][0]
    # Storage proposal body has no suggested_grant_pattern: pressing 'p' must NOT approve
    # and must NOT silently reject (re-prompts and safely pauses in waiting_approval)
    done2 = await drain_approvals_interactively(
        runtime2, p2, input_fn=lambda _p: "p", output_fn=lambda _s: None
    )
    assert done2["status"] == "waiting_approval"
    prop2_mid = storage2.get_proposal(pid2)
    assert prop2_mid is not None and prop2_mid["status"] == "pending"
    assert not (ws2 / "no_pattern.txt").exists()

    # Explicit 'n' cleanly rejects the proposal
    done2_rejected = await drain_approvals_interactively(
        runtime2, done2, input_fn=lambda _p: "n", output_fn=lambda _s: None
    )
    await runtime2.aclose()
    assert done2_rejected["status"] == "completed"
    prop2 = storage2.get_proposal(pid2)
    assert prop2 is not None and prop2["status"] == "rejected"
    assert not (ws2 / "no_pattern.txt").exists()


async def test_repl_at_file_mention_safe_workspace_expansion_and_blocks(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "config").mkdir()
    (ws / ".git").mkdir()

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("OUTSIDE_TOP_SECRET\n", encoding="utf-8")
    (ws / "src" / "module.py").write_text("SAFE_TOKEN = 'visible'\n", encoding="utf-8")
    (ws / ".env").write_text("API_KEY=SUPER_SECRET_ENV\n", encoding="utf-8")
    (ws / "config" / "api_key.txt").write_text("SECRET_API_KEY_FILE\n", encoding="utf-8")
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (ws / "link_escape.txt").symlink_to(outside)

    hardlink_created = False
    try:
        os.link(outside, ws / "hard_escape.txt")
        hardlink_created = True
    except OSError:
        hardlink_created = False

    # 1. Valid workspace relative path expands cleanly
    ok_expanded = _expand_at_file_mentions("请检查 @src/module.py 的常量。", ws)
    assert "[已引用工作区文件上下文]" in ok_expanded
    assert "--- @src/module.py ---" in ok_expanded
    assert "SAFE_TOKEN = 'visible'" in ok_expanded

    # 2. Path traversal, absolute paths, secrets, .git, symlinks, hardlinks are ALL blocked
    blocked_prompt = (
        "尝试读取 @../outside_secret.txt @src/../../outside_secret.txt "
        "@/etc/hosts @.env @config/api_key.txt @.git/HEAD @link_escape.txt"
        + (" @hard_escape.txt" if hardlink_created else "")
    )
    blocked_out = _expand_at_file_mentions(blocked_prompt, ws)
    assert blocked_out == blocked_prompt
    assert "[已引用工作区文件上下文]" not in blocked_out
    for forbidden in (
        "OUTSIDE_TOP_SECRET",
        "SUPER_SECRET_ENV",
        "SECRET_API_KEY_FILE",
        "refs/heads/main",
    ):
        assert forbidden not in blocked_out

    # 3. Email-like token must not trigger expansion
    email_text = "联系 dev@src/module.py 获取支持"
    assert _expand_at_file_mentions(email_text, ws) == email_text

    # 4. End-to-end REPL run expands safe @file and strips/blocks unsafe mentions
    seen_prompts: list[str] = []

    class CapturePromptProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            for msg in messages:
                if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                    seen_prompts.append(msg["content"])
            return _resp([{"type": "text", "text": "已收到安全上下文。"}])

    storage = Storage(tmp_path / "at_state.db")
    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=CapturePromptProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    inputs = iter(["/exit"])
    await run_interactive_repl(
        runtime,
        initial_prompt="审查 @src/module.py 同时跳过 @../outside_secret.txt 和 @.env",
        input_fn=lambda _p: next(inputs),
        output_fn=lambda _s: None,
    )
    await runtime.aclose()

    combined = "\n".join(seen_prompts)
    assert "SAFE_TOKEN = 'visible'" in combined
    assert "OUTSIDE_TOP_SECRET" not in combined
    assert "SUPER_SECRET_ENV" not in combined


async def test_repl_new_slash_commands_rewind_fork_cost_model_hooks_bg_compact(
    tmp_path: Path,
) -> None:
    class MultiStepSlashProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp(
                    [_call("fs_edit", path="app.py", old_text="VERSION = 1", new_text="VERSION = 2")],
                    in_tok=120,
                    out_tok=40,
                )
            if self.calls == 2:
                return _resp([{"type": "text", "text": "第一轮完成：VERSION=2"}], in_tok=150, out_tok=35)
            return _resp(
                [{"type": "text", "text": f"第 {self.calls} 次回复确认。"}],
                in_tok=80,
                out_tok=25,
            )

    ws, storage, runtime = _make_runtime(
        tmp_path,
        MultiStepSlashProvider(),
        permission_mode="full-auto",
        execution_profile="host",
        auto_dream=False,
    )
    (ws / "app.py").write_text("VERSION = 1\n", encoding="utf-8")

    class _FakeBgMgr:
        def list_processes(self) -> list[dict[str, Any]]:
            return [
                {
                    "proc_id": "bg_demo_1",
                    "alive": True,
                    "use_pty": True,
                    "command": "python3 -m http.server 0",
                }
            ]

    runtime._bg_process_managers["demo12345678"] = _FakeBgMgr()  # type: ignore[assignment]

    compact_calls: list[dict[str, Any]] = []
    orig_compact = runtime.compact_context

    def _spy_compact(
        run_id: str, *, keep_recent_groups: int = 2, focus: str = "", use_llm: bool = False
    ) -> dict[str, Any]:
        compact_calls.append(
            {"run_id": run_id, "keep_recent_groups": keep_recent_groups, "focus": focus}
        )
        return orig_compact(
            run_id, keep_recent_groups=keep_recent_groups, focus=focus, use_llm=use_llm
        )

    runtime.compact_context = _spy_compact  # type: ignore[method-assign]

    scripted = iter(
        [
            "第2轮背景说明：接口契约保持兼容",
            "第3轮确认：测试通过",
            "/cost",
            "/model",
            "/model claude-sonnet-4-6",
            "/model",
            "/model default",
            "/hooks",
            "/bg",
            "/compact 保留接口契约与错误码",
            "/rewind",
            "/rewind 0 files",
            "/fork 实验分支A",
            "/exit",
        ]
    )
    printed: list[str] = []

    final_run = await run_interactive_repl(
        runtime,
        initial_prompt="将 app.py 中的 VERSION 改为 2",
        input_fn=lambda _p: next(scripted),
        output_fn=printed.append,
    )
    await runtime.aclose()

    assert final_run is not None
    joined = "\n".join(printed)

    # /cost
    assert "费用与用量统计" in joined
    assert "累计费用: $" in joined
    assert "KV 命中率:" in joined

    # /model (inspect, override, verify override reflected, reset to default)
    assert "当前会话模型已设置为：claude-sonnet-4-6" in joined
    assert "当前模型覆盖：claude-sonnet-4-6" in joined
    assert "已重置为默认服务端模型。" in joined
    assert "model" not in (final_run.get("metadata") or {})

    # /hooks
    assert "Hooks & 权限规则概览：" in joined
    assert "Hooks 配置:" in joined
    assert "声明式规则:" in joined
    assert "session_grants" in joined

    # /bg
    assert "后台进程与异步子 Agent 状态：" in joined
    assert "[proc bg_demo_1]" in joined
    assert "pty=True" in joined
    assert "后台异步子 Agent：无" in joined

    # /compact <focus>
    assert "[上下文压缩完成]" in joined
    assert len(compact_calls) == 1
    assert compact_calls[0]["focus"] == "保留接口契约与错误码"
    assert "压缩聚焦方向：保留接口契约与错误码" in joined

    # /rewind (list + file-only rewind restores app.py back to VERSION = 1)
    assert "当前会话检查点" in joined
    assert "[Rewind 完成]" in joined
    assert "scope=files" in joined
    assert (ws / "app.py").read_text(encoding="utf-8") == "VERSION = 1\n"

    # /fork created a distinct new run and switched REPL active run to it
    assert "[会话分叉成功] 已切换到新分支 Run ID:" in joined
    all_runs = storage.list_runs()
    assert len(all_runs) == 2
    source_ids = [r["id"] for r in all_runs if r["id"] != final_run["id"]]
    assert len(source_ids) == 1
    source_full = storage.get_run(source_ids[0])
    assert source_full is not None
    assert final_run["metadata"].get("forked_from_run_id") == source_full["id"]
    # Compaction summary with focus was persisted on source run and inherited on fork
    comp_summary = (
        (source_full.get("metadata") or {})
        .get("compaction_summaries", {})
        .get("coordinator", "")
    )
    assert "保留接口契约与错误码" in comp_summary
    assert "保留接口契约与错误码" in (
        (final_run.get("metadata") or {})
        .get("compaction_summaries", {})
        .get("coordinator", "")
    )


async def test_repl_dream_dry_run_and_rollback_slash_commands(tmp_path: Path) -> None:
    class SimpleProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([{"type": "text", "text": "已完成本轮记录。"}])

    ws, storage, runtime = _make_runtime(
        tmp_path,
        SimpleProvider(),
        permission_mode="full-auto",
        execution_profile="host",
        auto_dream=False,
    )
    (ws / "notes.py").write_text("RATE = 10\n", encoding="utf-8")

    # Seed a project memory page with a broken wikilink so Dreaming has concrete repair work
    store = runtime._memory_store()
    store.write_page(
        layer="project",
        path="rules/broken-ref.md",
        title="含断链规则",
        content="请参考 [[nonexistent-target]] 核对费率。",
        status="confirmed",
        sources=["file:notes.py"],
        index_summary="含断链测试页",
    )
    created = runtime.create("检查 notes.py 费率")
    done_run = await runtime.execute(created["id"])
    assert done_run["status"] == "completed"

    before_files = set(_snapshot_tree(store.memory_base).keys())
    before_content = store.read_page("project", "rules/broken-ref.md", record_access=False)["content"]
    before_dream_count = len(
        storage.list_dream_runs(store.tenant_id, project_slug=store.project_slug)
    )

    # 1. Run /dream --dry-run via REPL: plans link repair without creating files, rewriting body, or auditing
    dry_inputs = iter(["/dream --dry-run", "/exit"])
    dry_printed: list[str] = []
    run_after_dry = await run_interactive_repl(
        runtime,
        resume_run_id=done_run["id"],
        input_fn=lambda _p: next(dry_inputs),
        output_fn=dry_printed.append,
    )
    assert run_after_dry is not None
    dry_joined = "\n".join(dry_printed)
    assert "[Dreaming 预演完成（零落盘）]" in dry_joined
    assert "计划修复断链=1" in dry_joined
    assert set(_snapshot_tree(store.memory_base).keys()) == before_files
    assert (
        store.read_page("project", "rules/broken-ref.md", record_access=False)["content"]
        == before_content
    )
    assert (
        len(storage.list_dream_runs(store.tenant_id, project_slug=store.project_slug))
        == before_dream_count
    )

    # 2. Run /dream: repairs broken link, creates stub wiki page, and records backup snapshot
    dream_inputs = iter(["/dream", "/exit"])
    dream_printed: list[str] = []
    await run_interactive_repl(
        runtime,
        resume_run_id=done_run["id"],
        input_fn=lambda _p: next(dream_inputs),
        output_fn=dream_printed.append,
    )
    assert "[Dreaming 整理完成]" in "\n".join(dream_printed)
    assert "断链修复=1" in "\n".join(dream_printed)
    repaired_page = store.read_page("project", "rules/broken-ref.md", record_access=False)
    assert "[[nonexistent-target]]" not in repaired_page["content"]
    assert (store.project_dir / "wiki" / "nonexistent-target.md").is_file()

    # 3. Run /dream --rollback: restores pre-dream page body and removes newly created stub
    rb_inputs = iter(["/dream --rollback", "/exit"])
    rb_printed: list[str] = []
    await run_interactive_repl(
        runtime,
        resume_run_id=done_run["id"],
        input_fn=lambda _p: next(rb_inputs),
        output_fn=rb_printed.append,
    )
    await runtime.aclose()

    assert "[Dreaming 已回滚]" in "\n".join(rb_printed)
    restored_page = store.read_page("project", "rules/broken-ref.md", record_access=False)
    assert "[[nonexistent-target]]" in restored_page["content"]
    assert not (store.project_dir / "wiki" / "nonexistent-target.md").exists()


# ---------------------------------------------------------------------------
# 4. web/index.html + web/app.js security, DOM, SSE, Blob, and Cookie guards
# ---------------------------------------------------------------------------


def test_web_assets_cache_busting_xss_sinks_dom_bindings_and_event_names() -> None:
    html = WEB_INDEX.read_text(encoding="utf-8")
    js = WEB_APP_JS.read_text(encoding="utf-8")

    # 1. Cache-busting ?v=11 on local static assets
    assert 'href="/static/style.css?v=11"' in html
    assert 'src="/static/app.js?v=11"' in html
    static_urls = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert static_urls, "expected local /static/ assets in web/index.html"
    assert all(url.endswith("?v=11") for url in static_urls), f"missing ?v=11 on {static_urls}"

    # 2. Zero innerHTML / outerHTML / insertAdjacentHTML / document.write sinks in non-comment code
    js_code = _strip_js_comments(js)
    html_code = _strip_html_comments(html)
    forbidden_sinks = [
        r"\binnerHTML\b",
        r"\bouterHTML\b",
        r"\binsertAdjacentHTML\b",
        r"\bdocument\s*\.\s*write(?:ln)?\b",
    ]
    for pattern in forbidden_sinks:
        assert not re.search(pattern, js_code), f"forbidden DOM sink {pattern} found in web/app.js"
        assert not re.search(pattern, html_code), f"forbidden DOM sink {pattern} found in web/index.html"

    # Zero inline <script> blocks or inline on* event attributes in HTML
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html_code, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html_code, re.I)

    # 3. Zero missing $("id") bindings and zero duplicate HTML id attributes
    all_html_ids = re.findall(r'\bid=["\']([^"\']+)["\']', html_code)
    dup_html_ids = sorted(k for k, v in Counter(all_html_ids).items() if v > 1)
    assert dup_html_ids == [], f"duplicate HTML ids: {dup_html_ids}"

    html_id_set = set(all_html_ids)
    js_id_refs = set(re.findall(r'\$\(\s*["\']([^"\']+)["\']\s*\)', js_code))
    assert len(js_id_refs) >= 50
    missing_ids = sorted(js_id_refs - html_id_set)
    assert missing_ids == [], f"missing DOM ids referenced by $(): {missing_ids}"

    # 4. No duplicate keys in eventNames
    ev_match = re.search(r"const\s+eventNames\s*=\s*\{(.*?)\n\};", js_code, re.S)
    assert ev_match is not None, "const eventNames = {...} block not found in web/app.js"
    raw_keys = re.findall(r"^\s*([A-Za-z0-9_]+|\"[^\"]+\"|'[^']+')\s*:", ev_match.group(1), re.M)
    norm_keys = [k.strip("\"'") for k in raw_keys]
    assert len(norm_keys) >= 30
    dup_event_keys = sorted(k for k, v in Counter(norm_keys).items() if v > 1)
    assert dup_event_keys == [], f"duplicate keys in eventNames: {dup_event_keys}"


def test_web_sse_blob_cleanup_incremental_timeline_and_cookie_auth_flow(
    tmp_path: Path, monkeypatch
) -> None:
    html = WEB_INDEX.read_text(encoding="utf-8")
    js = WEB_APP_JS.read_text(encoding="utf-8")
    js_code = _strip_js_comments(js)
    html_code = _strip_html_comments(html)

    # 1. EventSource SSE streaming with credentials, monotonic after_seq cursor, and fallback cleanup
    assert "new EventSource(" in js_code
    assert "/stream?after_seq=" in js_code
    assert "streamAfterSeqByRun" in js_code
    assert "withCredentials: true" in js_code
    for sse_event in ("thinking_delta", "text_delta", "bash_output_delta", "run_status", "done"):
        assert f'addEventListener("{sse_event}"' in js_code
    assert "activeEventSource.close()" in js_code

    # 2. URL.revokeObjectURL cleanup on cache eviction, run switch, and beforeunload
    assert "URL.revokeObjectURL(" in js_code
    assert "function revokeArtifactBlobs(" in js_code
    assert 'addEventListener("beforeunload"' in js_code
    assert "revokeArtifactBlobs(" in js_code

    # 3. Incremental renderedEventIds timeline updates (preserving expanded <details>)
    assert "const renderedEventIds = new Set();" in js_code
    render_events_match = re.search(r"function renderEvents\(events\)\s*\{([\s\S]*?)\n\}", js_code)
    assert render_events_match is not None
    re_body = render_events_match.group(1)
    assert "timelineRunId !== current" in re_body
    assert "if (renderedEventIds.has(key)) continue;" in re_body
    assert "renderedEventIds.add(key);" in re_body
    assert "tl.append(buildEventNode(event));" in re_body

    # 4. Cookie POST /api/auth/session flow and ZERO window.prompt
    assert not re.search(r"\bwindow\s*\.\s*prompt\b", js_code)
    assert not re.search(r"(?<!\.)\bprompt\s*\(", js_code)
    assert not re.search(r"\bwindow\s*\.\s*prompt\b", html_code)
    assert 'id="auth-modal"' in html_code
    assert 'id="auth-token-input"' in html_code
    assert 'type="password"' in html_code
    assert 'credentials: "same-origin"' in js_code
    assert 'api("/auth/session", {\n        method: "POST"' in js_code
    assert 'api("/auth/session", { method: "DELETE" })' in js_code

    # 5. Live offline FastAPI TestClient verification of ?v=9 page and cookie POST /api/auth/session
    monkeypatch.delenv("AGENT_LAB_TOKENS_JSON", raising=False)
    monkeypatch.setenv("AGENT_LAB_TOKEN", "web-upgrade-cookie-secret")
    app = create_app(state_dir=tmp_path / "web_state", provider="demo")

    with TestClient(app) as client:
        index_res = client.get("/")
        assert index_res.status_code == 200
        assert "/static/app.js?v=11" in index_res.text
        assert "/static/style.css?v=11" in index_res.text

        assert client.get("/api/runs").status_code == 401

        login = client.post("/api/auth/session", json={"token": "web-upgrade-cookie-secret"})
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        set_cookie_hdr = login.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in set_cookie_hdr
        assert "httponly" in set_cookie_hdr.lower()

        # Cookie alone authenticates subsequent requests (no Authorization header needed)
        sess = client.get("/api/auth/session")
        assert sess.status_code == 200 and sess.json()["authenticated"] is True
        assert client.get("/api/runs").status_code == 200

        logout = client.delete("/api/auth/session")
        assert logout.status_code == 200
        assert client.get("/api/runs").status_code == 401

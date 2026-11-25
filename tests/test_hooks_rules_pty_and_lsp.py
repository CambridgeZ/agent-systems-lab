"""Tests for HookManager, PermissionRuleEngine, expanded bash classification, BackgroundProcessManager (pipe & PTY), LspClientManager, and parallel read-only tool waves."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import textwrap
import threading
import time
from typing import Any

import pytest

from agent_lab.hooks import HookManager
from agent_lab.host_tools import (
    BackgroundProcessManager,
    execute_bash_kill,
    execute_bash_list,
    execute_bash_output,
    execute_bash_write,
)
from agent_lab.lsp_client import LspClientManager
from agent_lab.permissions import (
    PermissionRuleEngine,
    classify_bash_command,
    evaluate_tool_permission,
    suggest_session_grant_pattern,
    trust_workspace,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _tool_use(call_id: str, name: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": kwargs}


def _resp(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    has_tools = any(b.get("type") == "tool_use" for b in blocks)
    return {
        "content": blocks,
        "stop_reason": "tool_use" if has_tools else "end_turn",
        "usage": {"input_tokens": 15, "output_tokens": 15},
    }


def _poll_output_until(
    mgr: BackgroundProcessManager,
    proc_id: str,
    needle: str,
    *,
    offset: int = 0,
    timeout: float = 3.0,
) -> tuple[str, int, dict[str, Any]]:
    """Incrementally read background process output using monotonic offsets until needle appears."""
    deadline = time.monotonic() + timeout
    acc = ""
    cur_offset = offset
    last_res: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_res = execute_bash_output(mgr, proc_id=proc_id, offset=cur_offset, max_chars=4096)
        assert last_res["ok"] is True
        if last_res["output"]:
            acc += last_res["output"]
            assert last_res["next_offset"] == cur_offset + len(last_res["output"])
            cur_offset = last_res["next_offset"]
        if needle in acc:
            return acc, cur_offset, last_res
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {needle!r}; got so far: {acc!r}")


async def test_hook_manager_pre_post_stop_veto_and_scrubbed_command_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / "state.db")

    # 1. Direct HookManager PreToolUse (updated_input -> allow / deny), PostToolUse, and command-hook scrubbing
    hm = HookManager(workspace=ws)

    def _rewrite_input(payload: dict[str, Any]) -> dict[str, Any]:
        inp = dict(payload["tool_input"])
        if inp.get("path") == "rewritten.txt":
            inp["content"] = inp.get("content", "") + "# hook-stamped\n"
            return {"decision": "allow", "updated_input": inp}
        return {}

    def _deny_secret_target(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["tool_input"].get("path") == "blocked.txt":
            return {"decision": "deny", "reason": "policy forbids writing blocked.txt"}
        return {}

    def _post_diag(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "diagnostics": [{"severity": "warning", "message": f"checked {payload['tool_name']}"}],
            "additional_context": "Post-hook lint check complete.",
        }

    hm.register("PreToolUse", _rewrite_input, matcher="fs_write")
    hm.register("PreToolUse", _deny_secret_target, matcher="fs_*")
    hm.register("PostToolUse", _post_diag, matcher="fs_write")

    # Verify direct PreToolUse deny short-circuit
    pre_deny = hm.run_pre_tool_use("fs_write", {"path": "blocked.txt", "content": "nope"})
    assert pre_deny["decision"] == "deny"
    assert "forbids writing blocked.txt" in pre_deny["reason"]

    # Verify direct PreToolUse updated_input + allow
    pre_ok = hm.run_pre_tool_use("fs_write", {"path": "rewritten.txt", "content": "hello\n"})
    assert pre_ok["decision"] == "allow"
    assert pre_ok["explicit_decision"] == "allow"
    assert pre_ok["updated_input"]["content"] == "hello\n# hook-stamped\n"

    # 2. Command-hook execution with secret-scrubbed environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-top-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-top-secret")
    monkeypatch.setenv("AGENT_LAB_TOKEN", "lab-admin-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-key")

    hook_script = tmp_path / "inspect_hook.py"
    hook_script.write_text(
        textwrap.dedent(
            """\
            import json, os, sys
            payload = json.load(sys.stdin)
            leaked = [
                k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AGENT_LAB_TOKEN", "AWS_SECRET_ACCESS_KEY")
                if k in os.environ
            ]
            out = {
                "additional_context": json.dumps({
                    "event": os.environ.get("AGENT_LAB_HOOK_EVENT"),
                    "workspace": os.environ.get("AGENT_LAB_WORKSPACE"),
                    "tool_name": payload.get("tool_name"),
                    "leaked": leaked,
                })
            }
            print(json.dumps(out))
            """
        ),
        encoding="utf-8",
    )
    cmd_hm = HookManager(workspace=ws)
    cmd_hm.register_command("PostToolUse", f'"{sys.executable}" "{hook_script}"', matcher="fs_write")
    cmd_res = await cmd_hm.arun_post_tool_use("fs_write", {"path": "a.py"}, {"ok": True})
    assert cmd_res["hooks_run"] == 1
    assert cmd_res["errors"] == []
    observed = json.loads(cmd_res["additional_context"])
    assert observed["event"] == "PostToolUse"
    assert observed["workspace"] == str(ws.resolve())
    assert observed["tool_name"] == "fs_write"
    assert observed["leaked"] == []

    # Failing command hook with on_error="deny" fails closed
    fail_hm = HookManager(workspace=ws)
    fail_hm.register_command("PreToolUse", f'"{sys.executable}" -c "import sys; sys.exit(2)"', on_error="deny")
    fail_res = fail_hm.run_pre_tool_use("fs_write", {"path": "x.py"})
    assert fail_res["decision"] == "deny"
    assert len(fail_res["errors"]) == 1

    # 3. End-to-end AgentRuntime integration: PreToolUse (allow in ask mode, updated_input, deny),
    #    PostToolUse (diagnostics + additional_context), and bounded Stop veto (max 2 retries)
    stop_calls = 0

    def _always_veto_stop(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal stop_calls
        stop_calls += 1
        return {"veto": True, "reason": f"Verification gate veto #{stop_calls}"}

    hm.register("Stop", _always_veto_stop, matcher="*")

    class HookExerciseProvider:
        def __init__(self) -> None:
            self.turns = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.turns += 1
            if self.turns == 1:
                return _resp(
                    [
                        _tool_use("hk_allow_rewrite", "fs_write", path="rewritten.txt", content="base\n"),
                        _tool_use("hk_deny", "fs_write", path="blocked.txt", content="forbidden\n"),
                    ]
                )
            return _resp([{"type": "text", "text": f"Final answer attempt {self.turns}"}])

    provider = HookExerciseProvider()
    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=provider,
        execution_profile="host",
        permission_mode="ask",  # PreToolUse explicit allow must bypass ask pause for rewritten.txt
        hook_manager=hm,
        auto_dream=False,
    )
    try:
        run = await rt.start("Exercise PreToolUse, PostToolUse, and Stop veto hooks")
        assert run["status"] == "completed"
        # rewritten.txt was allowed despite ask mode and modified by updated_input
        assert (ws / "rewritten.txt").read_text(encoding="utf-8") == "base\n# hook-stamped\n"
        # blocked.txt was denied by PreToolUse hook and never created
        assert not (ws / "blocked.txt").exists()

        allow_rec = storage.get_call(run["id"], "hk_allow_rewrite")
        assert allow_rec is not None
        assert allow_rec["result"]["ok"] is True

        # PostToolUse diagnostics & additional_context are attached to the tool result seen by the model and event timeline
        tool_result_msg = next(
            m
            for m in run["messages"]
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(b.get("tool_use_id") == "hk_allow_rewrite" for b in m["content"])
        )
        allow_block = next(b for b in tool_result_msg["content"] if b.get("tool_use_id") == "hk_allow_rewrite")
        allow_payload = json.loads(allow_block["content"])
        assert allow_payload["ok"] is True
        assert allow_payload["additional_context"] == "Post-hook lint check complete."
        assert any(d["message"] == "checked fs_write" for d in allow_payload["diagnostics"])

        tool_res_events = [
            e
            for e in storage.events(run["id"])
            if e["kind"] == "tool_result" and e["payload"].get("call_id") == "hk_allow_rewrite"
        ]
        assert len(tool_res_events) == 1
        assert tool_res_events[0]["payload"]["result"]["additional_context"] == "Post-hook lint check complete."

        deny_rec = storage.get_call(run["id"], "hk_deny")
        assert deny_rec is not None
        assert deny_rec["result"]["ok"] is False
        assert deny_rec["result"]["code"] == "hook_denied"

        # Stop hook veto is strictly bounded to 2 retries -> 1 tool turn + 3 final attempts = 4 model calls
        assert stop_calls == 2
        assert run["metadata"]["stop_veto_count"] == 2
        assert provider.turns == 4
        veto_events = [e for e in storage.events(run["id"]) if e["kind"] == "stop_hook_vetoed"]
        assert len(veto_events) == 2
    finally:
        await rt.aclose()


def test_permission_rule_engine_settings_grants_suggestions_and_bash_classifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_LAB_TRUSTED_WORKSPACES", raising=False)
    state_dir = tmp_path / "state"
    ws = tmp_path / "repo"
    (state_dir).mkdir()
    (ws / ".agent-lab").mkdir(parents=True)

    # Global settings deny curl & ask on Edit(deploy/**)
    (state_dir / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": ["Bash(curl:*)"],
                    "ask": ["Edit(deploy/**)"],
                    "allow": ["WebFetch(docs.python.org)"],
                }
            }
        ),
        encoding="utf-8",
    )
    # Project settings try to allow curl (must NOT override global deny!) and allow Edit(src/**)
    (ws / ".agent-lab" / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Bash(curl:*)", "Edit(src/**)", "Bash(make test:*)"],
                    "ask": ["Bash(docker:*)"],
                }
            }
        ),
        encoding="utf-8",
    )

    # 1. Untrusted workspace ignores project .agent-lab/settings.json
    untrusted_eng = PermissionRuleEngine.from_settings(state_dir=state_dir, workspace=ws)
    assert "project" not in untrusted_eng.sources
    assert untrusted_eng.evaluate("fs_edit", {"path": "src/main.py"}) is None

    # 2. Trusting the workspace loads project settings and preserves deny -> ask -> allow precedence
    trust_workspace(ws, state_dir=state_dir)
    eng = PermissionRuleEngine.from_settings(state_dir=state_dir, workspace=ws)
    assert eng.sources == ("global", "project")

    # Global deny beats project allow even inside chained command
    v_deny = eng.evaluate("bash_exec", {"command": "echo ok && curl https://evil.test"})
    assert v_deny is not None and v_deny["decision"] == "deny"

    # Ask rule beats allow / session_grants
    v_ask = eng.evaluate(
        "fs_edit",
        {"path": "deploy/prod.yaml"},
        session_grants=["Edit(deploy/**)"],
    )
    assert v_ask is not None and v_ask["decision"] == "ask"

    # Project allow matches glob and prefix rules
    v_allow_edit = eng.evaluate("fs_edit", {"path": "src/pkg/mod.py"})
    assert v_allow_edit is not None and v_allow_edit["decision"] == "allow"
    v_allow_make = eng.evaluate("bash_exec", {"command": "make test -j4 | tail -n 20"})
    assert v_allow_make is not None and v_allow_make["decision"] == "allow"
    # Anti-chaining invariant: allow prefix NEVER allows && chaining
    assert eng.evaluate("bash_exec", {"command": "make test && rm -rf build"}) is None

    # 3. session_grants evaluated after deny/ask/allow and integrated with evaluate_tool_permission
    grants = ["Bash(cargo build:*)", "Edit(tests/**)"]
    v_grant = eng.evaluate("bash_exec", {"command": "cargo build --release"}, session_grants=grants)
    assert v_grant is not None and v_grant["decision"] == "allow"
    # session_grant cannot bypass chaining or global deny
    assert eng.evaluate("bash_exec", {"command": "cargo build && whoami"}, session_grants=grants) is None
    perm_with_grant = evaluate_tool_permission(
        "bash_exec",
        {"command": "cargo build --release"},
        permission_mode="ask",
        rule_engine=eng,
        session_grants=grants,
    )
    assert perm_with_grant["decision"] == "allow"

    # 4. suggest_session_grant_pattern
    assert suggest_session_grant_pattern("bash_exec", {"command": "cargo test --lib"}) == "Bash(cargo test:*)"
    assert suggest_session_grant_pattern("bash_exec", {"command": "npm run lint"}) == "Bash(npm run lint:*)"
    assert suggest_session_grant_pattern("bash_exec", {"command": "python3 -m pytest tests/"}) == "Bash(python3 -m pytest:*)"
    assert suggest_session_grant_pattern("bash_exec", {"command": "echo a && echo b"}) is None
    assert suggest_session_grant_pattern("bash_exec", {"command": "rm -rf /"}) is None
    assert suggest_session_grant_pattern("fs_edit", {"path": "src/app/core.py"}) == "Edit(src/**)"
    assert suggest_session_grant_pattern("fs_write", {"path": "README.md"}) == "Write(README.md)"
    assert suggest_session_grant_pattern("fs_edit", {"path": "../outside.py"}) is None
    assert suggest_session_grant_pattern("web_fetch", {"url": "https://docs.python.org/3/library/"}) == "WebFetch(docs.python.org)"
    assert suggest_session_grant_pattern("mcp__github__list_issues", {}) == "MCP(github:list_issues)"

    # 5. Expanded safe read-only classify_bash_command & strict rejection of unsafe pipes/redirects/subshells
    for safe_cmd in (
        "cargo check",
        "cargo test --lib",
        "cargo clippy",
        "go test ./...",
        "go vet ./...",
        "npm test",
        "pnpm test",
        "yarn test",
        "tsc --noEmit",
        "npx --no-install tsc --noEmit",
        "git status | head -n 5",
        "pytest -q | wc -l",
        "cargo check | grep warning | head -n 10",
    ):
        assert classify_bash_command(safe_cmd)["classification"] == "safe_readonly", safe_cmd

    for unsafe_cmd in (
        "cargo clippy --fix",
        "go test -exec sh ./...",
        "git status > out.txt",
        "echo $(whoami)",
        "(git status)",
        "git status | tee leaked.txt",
        "git status | xargs rm",
        "git status | grep SECRET .env",
    ):
        assert classify_bash_command(unsafe_cmd)["classification"] == "mutating", unsafe_cmd


async def test_background_process_manager_pipe_pty_and_runtime_aclose_teardown(tmp_path: Path) -> None:
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / "state.db")

    echo_loop = (
        f'"{sys.executable}" -u -c "'
        "import sys; "
        "print('READY_LOOP', flush=True); "
        "[print('ECHO:' + line.strip(), flush=True) for line in sys.stdin]"
        '"'
    )

    mgr = BackgroundProcessManager(ws, sandbox_mode="off")
    try:
        # 1. Pipe mode: start, incremental offset read, write stdin, list, kill
        pipe_started = mgr.start(echo_loop, use_pty=False)
        assert pipe_started["ok"] is True
        assert pipe_started["use_pty"] is False
        pipe_id = pipe_started["proc_id"]

        out1, off1, _ = _poll_output_until(mgr, pipe_id, "READY_LOOP", offset=0)
        assert "READY_LOOP" in out1

        wr_pipe = execute_bash_write(mgr, proc_id=pipe_id, data="hello-pipe\n")
        assert wr_pipe["ok"] is True
        assert wr_pipe["bytes_written"] > 0

        out2, off2, _ = _poll_output_until(mgr, pipe_id, "ECHO:hello-pipe", offset=off1)
        assert "READY_LOOP" not in out2
        assert off2 > off1

        listed = execute_bash_list(mgr)
        assert listed["ok"] is True
        assert any(p["proc_id"] == pipe_id and p["running"] is True and p["use_pty"] is False for p in listed["processes"])

        killed_pipe = execute_bash_kill(mgr, proc_id=pipe_id)
        assert killed_pipe["ok"] is True
        assert killed_pipe["status"] == "killed"

        # 2. PTY mode: start, incremental offset read, write stdin, list, kill
        pty_started = mgr.start(echo_loop, use_pty=True)
        assert pty_started["ok"] is True
        assert pty_started["use_pty"] is True
        pty_id = pty_started["proc_id"]

        pty_out1, pty_off1, _ = _poll_output_until(mgr, pty_id, "READY_LOOP", offset=0)
        assert "READY_LOOP" in pty_out1

        wr_pty = execute_bash_write(mgr, proc_id=pty_id, data="hello-pty\n")
        assert wr_pty["ok"] is True

        pty_out2, pty_off2, _ = _poll_output_until(mgr, pty_id, "ECHO:hello-pty", offset=pty_off1)
        assert "ECHO:hello-pty" in pty_out2
        assert pty_off2 > pty_off1

        listed_pty = execute_bash_list(mgr)
        assert any(p["proc_id"] == pty_id and p["use_pty"] is True and p["running"] is True for p in listed_pty["processes"])

        killed_pty = execute_bash_kill(mgr, proc_id=pty_id)
        assert killed_pty["ok"] is True
        assert killed_pty["status"] == "killed"
    finally:
        mgr.close_all()

    # 3. Guaranteed teardown on AgentRuntime.aclose()
    class SpawnBgProvider:
        def __init__(self) -> None:
            self.step = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.step += 1
            if self.step == 1:
                return _resp(
                    [
                        _tool_use(
                            "bg_sleep",
                            "bash_exec",
                            command="sleep 30",
                            run_in_background=True,
                        )
                    ]
                )
            return _resp([{"type": "text", "text": "Background process started."}])

    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=SpawnBgProvider(),
        execution_profile="host",
        permission_mode="full-auto",
        auto_dream=False,
    )
    run = await rt.start("Spawn a background sleep process")
    assert run["status"] == "completed"
    rec = storage.get_call(run["id"], "bg_sleep")
    assert rec is not None and rec["result"]["ok"] is True
    bg_pid = int(rec["result"]["pid"])

    # Process is alive prior to aclose()
    os.kill(bg_pid, 0)

    await rt.aclose()

    # Process must be completely reaped after aclose()
    dead = False
    for _ in range(50):
        try:
            os.kill(bg_pid, 0)
            time.sleep(0.02)
        except ProcessLookupError:
            dead = True
            break
    assert dead, f"Background process {bg_pid} survived AgentRuntime.aclose()"


async def test_lsp_client_manager_mock_jsonrpc_server_and_missing_binary_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "lsp_ws"
    ws.mkdir()
    sample_py = ws / "calc.py"
    sample_py.write_text(
        "class Calculator:\n    def compute(self, x: int) -> int:\n        return BUG + x\n",
        encoding="utf-8",
    )

    mock_server = tmp_path / "mock_lsp_server.py"
    mock_server.write_text(
        textwrap.dedent(
            """\
            import json
            import sys

            def send(obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")
                sys.stdout.buffer.write(header + body)
                sys.stdout.buffer.flush()

            def read_msg():
                content_length = None
                while True:
                    line = sys.stdin.buffer.readline()
                    if not line:
                        return None
                    if line in (b"\\r\\n", b"\\n"):
                        break
                    decoded = line.decode("ascii", errors="ignore").strip()
                    if ":" in decoded:
                        k, v = decoded.split(":", 1)
                        if k.strip().lower() == "content-length":
                            content_length = int(v.strip())
                if not content_length:
                    return None
                raw = sys.stdin.buffer.read(content_length)
                if len(raw) < content_length:
                    return None
                return json.loads(raw.decode("utf-8"))

            while True:
                msg = read_msg()
                if msg is None:
                    break
                method = msg.get("method")
                req_id = msg.get("id")
                params = msg.get("params") or {}
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {"hoverProvider": True}}})
                elif method == "textDocument/didOpen":
                    td = params["textDocument"]
                    diags = []
                    if "BUG" in td.get("text", ""):
                        diags.append({
                            "range": {"start": {"line": 2, "character": 15}, "end": {"line": 2, "character": 18}},
                            "severity": 1,
                            "code": "E001",
                            "source": "mock-lsp",
                            "message": "未定义标识符 BUG",
                        })
                    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": td["uri"], "diagnostics": diags}})
                elif method == "textDocument/didChange":
                    uri = params["textDocument"]["uri"]
                    text = params["contentChanges"][0]["text"]
                    diags = []
                    if "BUG" in text:
                        diags.append({
                            "range": {"start": {"line": 2, "character": 15}, "end": {"line": 2, "character": 18}},
                            "severity": 1,
                            "code": "E001",
                            "source": "mock-lsp",
                            "message": "未定义标识符 BUG",
                        })
                    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diags}})
                elif method == "textDocument/definition":
                    uri = params["textDocument"]["uri"]
                    send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "uri": uri,
                            "range": {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 15}},
                        },
                    })
                elif method == "textDocument/hover":
                    send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "contents": {"kind": "markdown", "value": "```python\\ndef compute(self, x: int) -> int\\n```"},
                            "range": {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 15}},
                        },
                    })
                elif method == "textDocument/documentSymbol":
                    send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [
                            {
                                "name": "Calculator",
                                "kind": 5,
                                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 22}},
                                "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 16}},
                                "children": [
                                    {
                                        "name": "compute",
                                        "kind": 6,
                                        "detail": "(self, x: int) -> int",
                                        "range": {"start": {"line": 1, "character": 4}, "end": {"line": 2, "character": 22}},
                                        "selectionRange": {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 15}},
                                    }
                                ],
                            }
                        ],
                    })
                elif method == "shutdown":
                    send({"jsonrpc": "2.0", "id": req_id, "result": None})
                elif method == "exit":
                    break
            """
        ),
        encoding="utf-8",
    )

    async with LspClientManager(
        workspace=ws,
        server_overrides={"python": [sys.executable, "-u", str(mock_server)]},
    ) as mgr:
        # 1. goToDefinition (0-based LSP -> 1-based normalized + workspace-relative path)
        def_res = await mgr.query("goToDefinition", "calc.py", line=3, character=16)
        assert def_res["ok"] is True
        assert def_res["available"] is True
        assert len(def_res["result"]) == 1
        assert def_res["result"][0]["path"] == "calc.py"
        assert def_res["result"][0]["line"] == 2
        assert def_res["result"][0]["character"] == 9

        # 2. hover
        hover_res = await mgr.query("hover", "calc.py", line=2, character=9)
        assert hover_res["ok"] is True
        assert "def compute(self, x: int) -> int" in hover_res["result"]["contents"]
        assert hover_res["result"]["range"]["line"] == 2

        # 3. documentSymbol (hierarchical flattened with container)
        sym_res = await mgr.query("documentSymbol", "calc.py")
        assert sym_res["ok"] is True
        names_and_kinds = [(s["name"], s["kind"], s.get("container")) for s in sym_res["result"]]
        assert ("Calculator", "class", None) in names_and_kinds
        assert ("compute", "method", "Calculator") in names_and_kinds

        # 4. get_file_diagnostics (initial error with UTF-8 Chinese message -> fixed after edit)
        diags = await mgr.get_file_diagnostics("calc.py", timeout=2.0)
        assert len(diags) == 1
        assert diags[0]["severity"] == "error"
        assert diags[0]["code"] == "E001"
        assert "未定义标识符 BUG" in diags[0]["message"]
        assert diags[0]["line"] == 3

        sample_py.write_text(
            "class Calculator:\n    def compute(self, x: int) -> int:\n        return x + 1\n",
            encoding="utf-8",
        )
        diags_after = await mgr.get_file_diagnostics("calc.py", timeout=2.0)
        assert diags_after == []

    # 5. Graceful degradation when no LSP binary is installed
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    missing_mgr = LspClientManager(workspace=ws)
    try:
        assert await missing_mgr.get_file_diagnostics("calc.py", timeout=0.5) == []
        unavail = await missing_mgr.query("goToDefinition", "calc.py", line=1, character=1)
        assert unavail["ok"] is True
        assert unavail["available"] is False
        assert unavail["result"] is None
        assert unavail["diagnostics"] == []
    finally:
        await missing_mgr.close_all()


async def test_parallel_readonly_tool_waves_concurrent_reads_and_sequential_writes(tmp_path: Path) -> None:
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir(parents=True, exist_ok=True)
    for idx in range(3):
        (ws / f"r{idx}.txt").write_text(f"read-content-{idx}\n", encoding="utf-8")
    storage = Storage(tmp_path / "state.db")

    class WaveProvider:
        def __init__(self) -> None:
            self.step = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.step += 1
            if self.step == 1:
                return _resp(
                    [
                        _tool_use("ro_1", "fs_read", path="r0.txt"),
                        _tool_use("ro_2", "fs_read", path="r1.txt"),
                        _tool_use("ro_3", "fs_read", path="r2.txt"),
                        _tool_use("wr_1", "fs_write", path="out1.txt", content="first\n"),
                        _tool_use("wr_2", "fs_write", path="out2.txt", content="second\n"),
                    ]
                )
            return _resp([{"type": "text", "text": "Waves finished."}])

    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=WaveProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    try:
        probe_run = rt.create("Verify wave partitioning")
        sample_calls = [
            _tool_use("c1", "fs_read", path="r0.txt"),
            _tool_use("c2", "fs_read", path="r1.txt"),
            _tool_use("c3", "fs_write", path="w.txt", content="x"),
            _tool_use("c4", "fs_read", path="r2.txt"),
            _tool_use("c5", "fs_glob", pattern="*.txt"),
        ]
        waves = rt._partition_tool_waves(probe_run, sample_calls)
        assert [(is_par, [c["id"] for c in batch]) for is_par, batch in waves] == [
            (True, ["c1", "c2"]),
            (False, ["c3"]),
            (True, ["c4", "c5"]),
        ]

        # Instrument _execute_call: a 3-party threading.Barrier proves all 3 fs_read calls
        # truly run concurrently in parallel (sequential execution would time out/fail!),
        # while a write concurrency monitor proves the 2 fs_write calls NEVER overlap.
        read_barrier = threading.Barrier(3, timeout=3.0)
        state_lock = threading.Lock()
        active_writes = 0
        max_concurrent_writes = 0
        write_order: list[str] = []
        orig_execute = rt._execute_call

        def _instrumented_execute(
            run: dict[str, Any],
            registry: Any,
            call: dict[str, Any],
            journal_id: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            nonlocal active_writes, max_concurrent_writes
            if call["name"] == "fs_read":
                # Only succeeds if all 3 fs_read calls are in flight at the exact same instant
                read_barrier.wait()
            elif call["name"] == "fs_write":
                with state_lock:
                    active_writes += 1
                    max_concurrent_writes = max(max_concurrent_writes, active_writes)
                    write_order.append(call["id"])
                try:
                    time.sleep(0.03)
                    return orig_execute(run, registry, call, journal_id, **kwargs)
                finally:
                    with state_lock:
                        active_writes -= 1
            return orig_execute(run, registry, call, journal_id, **kwargs)

        rt._execute_call = _instrumented_execute  # type: ignore[method-assign]

        finished = await rt.start("Run parallel read wave followed by sequential write wave")
        assert finished["status"] == "completed"
        assert max_concurrent_writes == 1
        assert write_order == ["wr_1", "wr_2"]
        assert (ws / "out1.txt").read_text(encoding="utf-8") == "first\n"
        assert (ws / "out2.txt").read_text(encoding="utf-8") == "second\n"

        # Tool results in conversation history preserve exact original pending_calls order
        tool_result_msg = next(
            m
            for m in finished["messages"]
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        )
        assert [b["tool_use_id"] for b in tool_result_msg["content"]] == [
            "ro_1",
            "ro_2",
            "ro_3",
            "wr_1",
            "wr_2",
        ]
    finally:
        await rt.aclose()

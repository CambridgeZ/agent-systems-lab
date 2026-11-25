"""Tests for CheckpointManager, runtime.rewind, session/checkpoint/attachment APIs, HostKernelSandbox, and SSRF-safe web_fetch."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import socket
import struct
import sys
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_lab import host_tools
from agent_lab.api import create_app
from agent_lab.checkpoints import CheckpointManager
from agent_lab.host_tools import (
    HostKernelSandbox,
    HostToolError,
    build_sandboxed_shell_argv,
    execute_web_fetch,
    run_host_bash_command,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _minimal_png_bytes(width: int = 4, height: int = 2) -> bytes:
    """Build a valid PNG signature + IHDR chunk for dimension probing tests."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_chunk = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    iend_chunk = struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    return sig + ihdr_chunk + iend_chunk


def _tool_use(call_id: str, name: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": kwargs}


def _resp(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    has_tools = any(b.get("type") == "tool_use" for b in blocks)
    return {
        "content": blocks,
        "stop_reason": "tool_use" if has_tools else "end_turn",
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }


async def test_checkpoint_manager_and_runtime_rewind_scopes_and_dangling_heal(tmp_path: Path) -> None:
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir(parents=True, exist_ok=True)
    (ws / "app.py").write_text("VERSION = 1\n", encoding="utf-8")

    storage = Storage(tmp_path / "state.db")

    class MultiTurnEditProvider:
        def __init__(self) -> None:
            self.step = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.step += 1
            if self.step == 1:
                # Turn 1: edit existing app.py (v1 -> v2), edit it a second time (v2 -> v2b), and create new file
                return _resp(
                    [
                        _tool_use("t1_edit_a", "fs_edit", path="app.py", old_text="VERSION = 1", new_text="VERSION = 2"),
                        _tool_use("t1_edit_b", "fs_edit", path="app.py", old_text="VERSION = 2", new_text="VERSION = 20"),
                        _tool_use("t1_create", "fs_write", path="created_turn1.py", content="TURN = 1\n"),
                    ]
                )
            if self.step == 2:
                return _resp([{"type": "text", "text": "Turn 1 edits done."}])
            if self.step == 3:
                # Turn 2: edit app.py again (v20 -> v30) and create untracked file
                return _resp(
                    [
                        _tool_use("t2_edit", "fs_edit", path="app.py", old_text="VERSION = 20", new_text="VERSION = 30"),
                        _tool_use("t2_create", "fs_write", path="untracked_turn2.py", content="TEMP = True\n"),
                    ]
                )
            return _resp([{"type": "text", "text": "Turn 2 edits done."}])

    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=MultiTurnEditProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    try:
        turn1 = await rt.start("First edit turn")
        assert turn1["status"] == "completed"
        run_id = turn1["id"]
        assert (ws / "app.py").read_text(encoding="utf-8") == "VERSION = 20\n"
        assert (ws / "created_turn1.py").read_text(encoding="utf-8") == "TURN = 1\n"

        turn2 = await rt.send_message(run_id, "Second edit turn")
        assert turn2["status"] == "completed"
        assert (ws / "app.py").read_text(encoding="utf-8") == "VERSION = 30\n"
        assert (ws / "untracked_turn2.py").exists()

        cps = rt.list_checkpoints(run_id)
        assert len(cps) == 2
        cp0, cp1 = cps[0], cps[1]
        assert cp0["turn_index"] == 0
        assert cp1["turn_index"] == 1
        msgs_count_after_turn2 = len(storage.get_run(run_id)["messages"])

        # 1. scope="files" to cp1: restores turn-2 edits & deletes untracked_turn2.py, keeping messages intact
        rep_files = rt.rewind(run_id, cp1["checkpoint_id"], scope="files")
        assert rep_files["ok"] is True
        assert rep_files["scope"] == "files"
        assert "app.py" in rep_files["restored_files"]
        assert "untracked_turn2.py" in rep_files["deleted_files"]
        assert (ws / "app.py").read_text(encoding="utf-8") == "VERSION = 20\n"
        assert not (ws / "untracked_turn2.py").exists()
        assert (ws / "created_turn1.py").exists()
        assert len(storage.get_run(run_id)["messages"]) == msgs_count_after_turn2

        # 2. scope="both" to cp0: restores pre-edit v1 (first-touch snapshot!) and deletes created_turn1.py + truncates messages
        rep_both = rt.rewind(run_id, cp0["checkpoint_id"], scope="both")
        assert rep_both["ok"] is True
        assert rep_both["scope"] == "both"
        assert "app.py" in rep_both["restored_files"]
        assert "created_turn1.py" in rep_both["deleted_files"]
        assert (ws / "app.py").read_text(encoding="utf-8") == "VERSION = 1\n"
        assert not (ws / "created_turn1.py").exists()
        assert not (ws / "untracked_turn2.py").exists()
        assert rep_both["messages_after"] == cp0["message_idx"]
        assert rep_both["deleted_checkpoints"] == 1
        assert len(rt.list_checkpoints(run_id)) == 1

        # 3. scope="messages" directly via CheckpointManager: leaves files untouched AND heals dangling tool_use
        dangling_run = storage.create_run("Dangling tool_use healing", mode="agent", provider="custom")
        dangling_run["messages"] = [
            {"role": "user", "content": "Start"},
            {
                "role": "assistant",
                "content": [_tool_use("orphan_tu_1", "fs_write", path="keep.txt", content="x")],
            },
        ]
        storage.save_run(dangling_run)
        cp_mgr = CheckpointManager(storage, tmp_path)
        cp_dangling = cp_mgr.begin_turn_checkpoint(dangling_run, label="cut right after dangling tool_use")
        # Append later turn messages and write a file that must remain untouched under scope="messages"
        dangling_run["messages"].extend(
            [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "orphan_tu_1", "content": "{}"}],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "Finished."}]},
            ]
        )
        (ws / "keep_disk.txt").write_text("MUST_STAY", encoding="utf-8")
        cp_mgr.record_file_before_mutation(
            dangling_run["id"], cp_dangling["checkpoint_id"], ws, "keep_disk.txt", b"OLD", existed=True
        )

        rep_msgs = cp_mgr.rewind(dangling_run, cp_dangling["checkpoint_id"], ws, scope="messages")
        assert rep_msgs["ok"] is True
        assert rep_msgs["scope"] == "messages"
        assert rep_msgs["healed_dangling_tool_use"] is True
        assert rep_msgs["restored_files"] == []
        # Disk file is completely untouched under scope="messages"
        assert (ws / "keep_disk.txt").read_text(encoding="utf-8") == "MUST_STAY"
        # Truncated messages end with a 1:1 synthetic tool_result closing orphan_tu_1
        last_msg = dangling_run["messages"][-1]
        assert last_msg["role"] == "user"
        assert len(last_msg["content"]) == 1
        assert last_msg["content"][0]["type"] == "tool_result"
        assert last_msg["content"][0]["tool_use_id"] == "orphan_tu_1"
        assert last_msg["content"][0]["is_error"] is True
        assert "rewound_checkpoint" in last_msg["content"][0]["content"]
    finally:
        await rt.aclose()


async def test_api_checkpoints_rewind_fork_search_attachments_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_LAB_TOKENS_JSON", raising=False)
    monkeypatch.delenv("AGENT_LAB_ALLOWED_WORKSPACES", raising=False)

    state_dir = tmp_path / "api_state"
    ws = tmp_path / "project_ws"
    ws.mkdir()
    (ws / "config.py").write_text("DEBUG = False\n", encoding="utf-8")

    app = create_app(state_dir=state_dir, provider="demo")

    class TwoTurnHostProvider:
        def __init__(self) -> None:
            self.n = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.n += 1
            if self.n == 1:
                return _resp(
                    [_tool_use("api_e1", "fs_edit", path="config.py", old_text="DEBUG = False", new_text="DEBUG = True")]
                )
            if self.n == 2:
                return _resp([{"type": "text", "text": "Enabled debug."}])
            if self.n == 3:
                return _resp([_tool_use("api_w2", "fs_write", path="extra.txt", content="extra\n")])
            return _resp([{"type": "text", "text": "Added extra.txt."}])

    rt = AgentRuntime(
        app.state.storage,
        ws,
        app.state.knowledge.directory,
        app.state.skills.directory,
        provider=TwoTurnHostProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    rt.provider_name = "demo"
    try:
        r1 = await rt.start("Enable debug mode in config.py")
        run_id = r1["id"]
        await rt.send_message(run_id, "Create extra.txt")
    finally:
        await rt.aclose()

    assert (ws / "config.py").read_text(encoding="utf-8") == "DEBUG = True\n"
    assert (ws / "extra.txt").exists()

    # Create a second run with different status & prompt to test search/filter
    other_run = app.state.storage.create_run("Database migration planning", mode="agent", provider="demo")
    other_run["status"] = "failed"
    app.state.storage.save_run(other_run)

    with TestClient(app) as client:
        # 1. GET /api/runs/{id}/checkpoints
        cp_resp = client.get(f"/api/runs/{run_id}/checkpoints")
        assert cp_resp.status_code == 200
        cp_body = cp_resp.json()
        assert cp_body["ok"] is True
        assert cp_body["count"] == 2
        cp0_id = cp_body["checkpoints"][0]["checkpoint_id"]
        cp1_id = cp_body["checkpoints"][1]["checkpoint_id"]

        # 2. POST /api/runs/{id}/fork (fork at message_idx=2 with custom title)
        fork_resp = client.post(
            f"/api/runs/{run_id}/fork",
            json={"up_to_message_idx": 2, "title": "Branch before extra.txt"},
        )
        assert fork_resp.status_code == 200
        fork_body = fork_resp.json()
        fork_id = fork_body["id"]
        assert fork_id != run_id
        assert fork_body["forked_from_run_id"] == run_id
        assert fork_body["title"] == "Branch before extra.txt"
        assert fork_body["messages_count"] <= 3  # may include synthetic tool_result if cut at tool_use

        # 3. POST /api/runs/{id}/rewind (rewind to cp1 first -> deletes extra.txt, then cp0 -> restores config.py)
        rw1 = client.post(f"/api/runs/{run_id}/rewind", json={"checkpoint_id": cp1_id, "scope": "files"})
        assert rw1.status_code == 200
        assert "extra.txt" in rw1.json()["deleted_files"]
        assert not (ws / "extra.txt").exists()
        assert (ws / "config.py").read_text(encoding="utf-8") == "DEBUG = True\n"

        rw0 = client.post(f"/api/runs/{run_id}/rewind", json={"checkpoint_id": cp0_id, "scope": "both"})
        assert rw0.status_code == 200
        assert "config.py" in rw0.json()["restored_files"]
        assert (ws / "config.py").read_text(encoding="utf-8") == "DEBUG = False\n"

        # 4. PATCH /api/runs/{id} (rename & archive) + GET /api/runs?q=&status=
        patch_title = client.patch(f"/api/runs/{run_id}", json={"title": "Debug Config Session"})
        assert patch_title.status_code == 200
        assert patch_title.json()["title"] == "Debug Config Session"

        search_q = client.get("/api/runs", params={"q": "Debug Config", "status": "completed"})
        assert search_q.status_code == 200
        ids_q = [r["id"] for r in search_q.json()]
        assert run_id in ids_q
        assert other_run["id"] not in ids_q

        search_failed = client.get("/api/runs", params={"status": "failed"})
        assert search_failed.status_code == 200
        assert [r["id"] for r in search_failed.json()] == [other_run["id"]]

        # Archiving hides the run from default listing unless include_archived=true
        arch_resp = client.patch(f"/api/runs/{run_id}", json={"archived": True})
        assert arch_resp.status_code == 200
        assert arch_resp.json()["archived"] is True
        assert run_id not in [r["id"] for r in client.get("/api/runs").json()]
        assert run_id in [r["id"] for r in client.get("/api/runs", params={"include_archived": "true"}).json()]

        # 5. POST /api/runs/{id}/attachments (PNG + TXT) & artifact fetch
        png_b64 = base64.b64encode(_minimal_png_bytes(16, 8)).decode("ascii")
        att_png = client.post(
            f"/api/runs/{run_id}/attachments",
            json={"filename": "ui_shot.png", "media_type": "image/png", "content_base64": png_b64},
        )
        assert att_png.status_code == 200
        att_rec = att_png.json()["attachment"]
        assert att_rec["media_type"] == "image/png"
        assert att_rec["width"] == 16
        assert att_rec["height"] == 8

        txt_b64 = base64.b64encode(b"trace line 1\n").decode("ascii")
        att_txt = client.post(
            f"/api/runs/{run_id}/attachments",
            json={"filename": "notes.txt", "content_base64": txt_b64},
        )
        assert att_txt.status_code == 200
        assert att_txt.json()["attachments_count"] == 2

        art_resp = client.get(f"/api/runs/{run_id}/artifacts/{att_rec['artifact_name']}")
        assert art_resp.status_code == 200
        assert art_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

        # Mismatched MIME/extension is rejected with 400
        bad_att = client.post(
            f"/api/runs/{run_id}/attachments",
            json={"filename": "evil.sh", "media_type": "text/plain", "content_base64": txt_b64},
        )
        assert bad_att.status_code == 400

        # 6. DELETE /api/runs/{id} deletes run & state artifacts without touching user workspace
        del_resp = client.delete(f"/api/runs/{run_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        assert not (state_dir / "artifacts" / run_id).exists()
        assert not (state_dir / "checkpoints" / run_id).exists()
        # User's direct workspace and its files remain completely intact
        assert (ws / "config.py").read_text(encoding="utf-8") == "DEBUG = False\n"


def test_host_kernel_sandbox_sbpl_bwrap_modes_and_darwin_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "sandbox_ws"
    ws.mkdir()
    HostKernelSandbox.reset_probe_cache()

    # 1. AGENT_LAB_HOST_SANDBOX=off bypasses OS sandbox wrapper
    monkeypatch.setenv("AGENT_LAB_HOST_SANDBOX", "off")
    argv_off = build_sandboxed_shell_argv("/bin/bash", "echo hi", ws)
    assert argv_off == ["/bin/bash", "-c", "echo hi"]

    # 2. Invalid mode raises invalid_sandbox_mode
    with pytest.raises(HostToolError) as exc_invalid:
        build_sandboxed_shell_argv("/bin/bash", "echo hi", ws, mode="bogus")
    assert exc_invalid.value.code == "invalid_sandbox_mode"

    # 3. auto vs strict when neither macOS sandbox-exec nor Linux bwrap is available
    monkeypatch.setattr(HostKernelSandbox, "_probe_macos", classmethod(lambda cls: False))
    monkeypatch.setattr(HostKernelSandbox, "_probe_linux_bwrap", classmethod(lambda cls: None))

    monkeypatch.setenv("AGENT_LAB_HOST_SANDBOX", "auto")
    assert build_sandboxed_shell_argv("/bin/bash", "echo hi", ws) == ["/bin/bash", "-c", "echo hi"]

    monkeypatch.setenv("AGENT_LAB_HOST_SANDBOX", "strict")
    with pytest.raises(HostToolError) as exc_strict:
        build_sandboxed_shell_argv("/bin/bash", "echo hi", ws)
    assert exc_strict.value.code == "sandbox_unavailable"

    # 4. macOS SBPL profile generation: credential denies, workspace write confinement, network toggle
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sbpl_no_net = HostKernelSandbox.build_macos_sbpl(ws, allow_network=False)
    assert "(version 1)" in sbpl_no_net
    assert "(deny file-read*" in sbpl_no_net
    assert f'(subpath "{(fake_home / ".ssh").resolve()}")' in sbpl_no_net
    assert f'(subpath "{(fake_home / ".aws").resolve()}")' in sbpl_no_net
    assert f'(subpath "{(fake_home / ".gnupg").resolve()}")' in sbpl_no_net
    assert f'(literal "{(fake_home / ".netrc").resolve()}")' in sbpl_no_net
    assert "(deny file-write*)" in sbpl_no_net
    assert f'(subpath "{ws.resolve()}")' in sbpl_no_net
    assert "(deny network-outbound)" in sbpl_no_net

    sbpl_with_net = HostKernelSandbox.build_macos_sbpl(ws, allow_network=True)
    assert "(deny network-outbound)" not in sbpl_with_net

    # Verify build_sandboxed_shell_argv returns sandbox-exec argv when macOS probe succeeds
    monkeypatch.setattr(HostKernelSandbox, "_probe_macos", classmethod(lambda cls: True))
    argv_mac = build_sandboxed_shell_argv("/bin/bash", "echo mac", ws, allow_network=False, mode="strict")
    assert argv_mac[0] == "/usr/bin/sandbox-exec"
    assert argv_mac[1] == "-p"
    assert "(deny network-outbound)" in argv_mac[2]
    assert argv_mac[-3:] == ["/bin/bash", "-c", "echo mac"]

    # 5. Linux bwrap argv generation: ro-bind root, rw-bind workspace, credential masking, --unshare-net
    (fake_home / ".ssh").mkdir()
    (fake_home / ".netrc").write_text("machine example.com\n", encoding="utf-8")
    monkeypatch.setattr(HostKernelSandbox, "_probe_macos", classmethod(lambda cls: False))
    monkeypatch.setattr(HostKernelSandbox, "_probe_linux_bwrap", classmethod(lambda cls: "/usr/bin/bwrap"))
    argv_bwrap_nonet = build_sandboxed_shell_argv("/bin/bash", "echo lin", ws, allow_network=False, mode="auto")
    assert argv_bwrap_nonet[0] == "/usr/bin/bwrap"
    assert "--ro-bind" in argv_bwrap_nonet
    assert "--unshare-net" in argv_bwrap_nonet
    assert str(ws.resolve()) in argv_bwrap_nonet
    assert str((fake_home / ".ssh").resolve()) in argv_bwrap_nonet
    assert str((fake_home / ".netrc").resolve()) in argv_bwrap_nonet

    argv_bwrap_net = build_sandboxed_shell_argv("/bin/bash", "echo lin", ws, allow_network=True, mode="auto")
    assert "--unshare-net" not in argv_bwrap_net

    # 6. Live sandboxed execution probe on Darwin when /usr/bin/sandbox-exec is present and operational
    monkeypatch.undo()
    HostKernelSandbox.reset_probe_cache()
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec") and HostKernelSandbox._probe_macos():
        live_home = tmp_path / "live_home"
        (live_home / ".ssh").mkdir(parents=True)
        secret_file = live_home / ".ssh" / "id_probe_secret"
        secret_file.write_text("TOP_SECRET_SSH_KEY\n", encoding="utf-8")
        allowed_tmp = tmp_path / "allowed_tmp"
        allowed_tmp.mkdir()
        outside_dir = tmp_path / "outside_denied_dir"
        outside_dir.mkdir()

        monkeypatch.setenv("HOME", str(live_home))
        monkeypatch.setattr(host_tools, "_collect_writable_tmp_roots", lambda: [allowed_tmp.resolve()])

        # Writing inside workspace is allowed
        ok_res = run_host_bash_command(
            ws,
            "echo sandbox_ok > inside.txt && cat inside.txt",
            sandbox_mode="strict",
            allow_network=False,
        )
        assert ok_res["ok"] is True
        assert ok_res["sandbox"] == "macos-sandbox-exec"
        assert "sandbox_ok" in ok_res["stdout"]
        assert (ws / "inside.txt").read_text(encoding="utf-8").strip() == "sandbox_ok"

        # Reading denied credential path under $HOME/.ssh is blocked by Seatbelt kernel policy
        read_denied = run_host_bash_command(
            ws,
            f"cat '{secret_file.resolve()}'",
            sandbox_mode="strict",
            allow_network=False,
        )
        assert read_denied["ok"] is False
        assert "TOP_SECRET_SSH_KEY" not in read_denied["stdout"]

        # Writing outside workspace & allowed tmp is blocked by Seatbelt kernel policy
        outside_target = outside_dir.resolve() / "escaped.txt"
        write_denied = run_host_bash_command(
            ws,
            f"echo escaped > '{outside_target}'",
            sandbox_mode="strict",
            allow_network=False,
        )
        assert write_denied["ok"] is False
        assert not outside_target.exists()


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://127.0.0.1/secret",
        "http://127.0.0.1:8080/health",
        "http://localhost/admin",
        "https://localhost:8443/api",
        "http://[::1]/loopback",
        "http://10.0.0.1/internal",
        "http://192.168.1.1/router",
        "http://172.16.0.10/private",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "ftp://example.com/file.txt",
    ],
)
def test_execute_web_fetch_ssrf_blocks_loopback_private_imds_and_file_urls(blocked_url: str) -> None:
    with pytest.raises(HostToolError) as exc_info:
        execute_web_fetch(blocked_url)
    assert exc_info.value.code == "ssrf_blocked"


def test_execute_web_fetch_ssrf_blocks_dns_rebinding_and_redirect_and_wraps_valid_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Domain resolving to private/loopback IP is blocked before any HTTP request
    def _fake_dns_private(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        if host == "rebind.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns_private)

    with pytest.raises(HostToolError) as exc_rebind:
        execute_web_fetch("https://rebind.example.test/data")
    assert exc_rebind.value.code == "ssrf_blocked"

    # 2. Public URL redirecting (302) to AWS IMDS metadata endpoint is blocked on redirect hop
    def _mock_http(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect-imds":
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data/iam/"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><title>Docs</title><script>evil()</script></head>"
            "<body><h1>Guide</h1><p>Hello <code>world</code>.</p></body></html>",
        )

    with httpx.Client(transport=httpx.MockTransport(_mock_http), follow_redirects=False) as mock_client:
        with pytest.raises(HostToolError) as exc_redir:
            execute_web_fetch("https://docs.example.test/redirect-imds", client=mock_client)
        assert exc_redir.value.code == "ssrf_blocked"

        # 3. Valid public URL succeeds offline, strips scripts, extracts Markdown, wraps untrusted_web_content
        ok = execute_web_fetch("https://docs.example.test/guide", client=mock_client)
        assert ok["ok"] is True
        assert ok["title"] == "Docs"
        assert ok["trust"] == "untrusted_web_content"
        assert ok["content"].startswith('<untrusted_web_content url="https://docs.example.test/guide">')
        assert ok["content"].endswith("</untrusted_web_content>")
        assert "# Guide" in ok["content"]
        assert "`world`" in ok["content"]
        assert "evil()" not in ok["content"]

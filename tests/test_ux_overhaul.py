"""Deterministic offline tests covering Wave UX-1 capabilities across API, Runtime, REPL, and CLI."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Any
import uuid
import zlib

from fastapi.testclient import TestClient
import pytest

from agent_lab import cli as cli_mod
from agent_lab import runtime as runtime_mod
from agent_lab.api import ROOT, create_app
from agent_lab.memory import compute_project_slug
from agent_lab.repl import (
    _list_workspace_files_pruned,
    _render_terminal_markdown,
    drain_approvals_interactively,
    run_interactive_repl,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _call(name: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": "ux1_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict[str, Any]], *, in_tok: int = 24, out_tok: int = 16) -> dict[str, Any]:
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _make_valid_png(width: int = 400, height: int = 300) -> bytes:
    """Create a spec-valid uncompressed PNG (default ~352 KiB, >256 KiB JSON cap)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x12\x34\x56" * width)
    idat = zlib.compress(row * height, level=0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _clear_api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AGENT_LAB_TOKEN",
        "AGENT_LAB_TOKENS_JSON",
        "AGENT_LAB_REQUIRE_AUTH",
        "AGENT_LAB_DISPATCH",
        "AGENT_LAB_MAX_BODY_BYTES",
        "AGENT_LAB_ATTACHMENT_MAX_BODY_BYTES",
        "AGENT_LAB_ALLOWED_WORKSPACES",
        "AGENT_LAB_STORAGE_DSN",
        "AGENT_LAB_ALLOWED_HOSTS",
        "AGENT_LAB_RATE_LIMIT_RPM",
        "AGENT_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)


def _wait_run_settled(
    client: TestClient,
    run_id: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        res = client.get(f"/api/runs/{run_id}", headers=headers)
        assert res.status_code == 200
        last = res.json()
        if (
            last.get("status") in {"completed", "failed", "cancelled", "waiting_approval", "budget_exceeded"}
            and not last.get("job_active")
        ):
            return last
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s: {last}")


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


def _list_staged_files(staged_root: Path) -> list[Path]:
    if not staged_root.exists():
        return []
    return [p for p in staged_root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# 1. Staged 6 MiB Uploads & Mid-Run Steering Attachments (api.py + runtime.py)
# ---------------------------------------------------------------------------


def test_staged_uploads_large_png_and_text_413_guard_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)
    state_dir = tmp_path / "state"
    staged_root = state_dir / "staged_uploads"

    class SimpleProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([{"type": "text", "text": "已收到大附件。"}])

    monkeypatch.setattr(runtime_mod, "get_provider", lambda _name: SimpleProvider())
    app = create_app(state_dir=state_dir, provider="demo")

    png_bytes = _make_valid_png(400, 300)
    assert len(png_bytes) > 300 * 1024
    png_b64 = base64.b64encode(png_bytes).decode("ascii")

    txt_bytes = (b"trace line 0123456789abcdef\n" * 12_500)
    assert len(txt_bytes) > 300 * 1024
    txt_b64 = base64.b64encode(txt_bytes).decode("ascii")

    with TestClient(app) as client:
        # 1. Oversized inline payload on POST /api/runs still hits 256 KiB cap -> HTTP 413
        oversized_inline = client.post(
            "/api/runs",
            json={
                "prompt": "分析超大内联图片",
                "attachments": [
                    {
                        "filename": "inline_huge.png",
                        "media_type": "image/png",
                        "content_base64": png_b64,
                    }
                ],
            },
        )
        assert oversized_inline.status_code == 413

        # 2. POST /api/uploads accepts >300 KiB valid PNG and text payloads
        up_png = client.post(
            "/api/uploads",
            json={
                "filename": "architecture.png",
                "media_type": "image/png",
                "content_base64": png_b64,
            },
        )
        assert up_png.status_code == 200
        png_meta = up_png.json()
        assert png_meta["ok"] is True
        assert png_meta["upload_id"].startswith("upl_")
        assert png_meta["size_bytes"] == len(png_bytes)
        assert png_meta["width"] == 400
        assert png_meta["height"] == 300

        up_txt = client.post(
            "/api/uploads",
            json={
                "filename": "server_log.txt",
                "media_type": "text/plain",
                "content_base64": txt_b64,
            },
        )
        assert up_txt.status_code == 200
        txt_meta = up_txt.json()
        assert txt_meta["ok"] is True
        assert txt_meta["upload_id"].startswith("upl_")
        assert txt_meta["size_bytes"] == len(txt_bytes)

        # Staged blob + meta files exist prior to consumption
        staged_before = _list_staged_files(staged_root)
        assert len(staged_before) == 4

        # 3. POST /api/runs consumes staged upload_ids cleanly
        created = client.post(
            "/api/runs",
            json={
                "prompt": "结合架构图和日志定位问题",
                "attachments": [
                    {"upload_id": png_meta["upload_id"]},
                    {"upload_id": txt_meta["upload_id"]},
                ],
            },
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        settled = _wait_run_settled(client, run_id)
        assert settled["status"] == "completed"

        # Staged temp files are cleaned up after consumption
        assert _list_staged_files(staged_root) == []

        # Persisted artifacts match uploaded bytes and metadata
        atts = settled["user_attachments"]
        assert len(atts) == 2
        by_name = {a["filename"]: a for a in atts}
        assert set(by_name) == {"architecture.png", "server_log.txt"}
        assert by_name["architecture.png"]["size_bytes"] == len(png_bytes)
        assert by_name["architecture.png"]["width"] == 400
        assert by_name["architecture.png"]["height"] == 300
        assert by_name["server_log.txt"]["size_bytes"] == len(txt_bytes)

        png_dl = client.get(by_name["architecture.png"]["url"])
        assert png_dl.status_code == 200
        assert png_dl.content == png_bytes

        txt_dl = client.get(by_name["server_log.txt"]["url"])
        assert txt_dl.status_code == 200
        assert txt_dl.content == txt_bytes

        # 4. Consumed upload_id cannot be reused
        reuse = client.post(
            "/api/runs",
            json={
                "prompt": "重复消费",
                "attachments": [{"upload_id": png_meta["upload_id"]}],
            },
        )
        assert reuse.status_code == 404


def test_staged_upload_cross_tenant_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)
    tokens_cfg = {
        "tok-tenant-a": {"tenant_id": "tenant_a", "actor_id": "alice", "roles": ["operator", "approver"]},
        "tok-tenant-b": {"tenant_id": "tenant_b", "actor_id": "bob", "roles": ["operator", "approver"]},
    }
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens_cfg))

    class FastProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([{"type": "text", "text": "完成。"}])

    monkeypatch.setattr(runtime_mod, "get_provider", lambda _name: FastProvider())
    state_dir = tmp_path / "state"
    app = create_app(state_dir=state_dir, provider="demo")

    hdr_a = {"Authorization": "Bearer tok-tenant-a"}
    hdr_b = {"Authorization": "Bearer tok-tenant-b"}
    png_b64 = base64.b64encode(_make_valid_png(16, 16)).decode("ascii")

    with TestClient(app) as client:
        # Tenant A stages an upload
        up_a = client.post(
            "/api/uploads",
            headers=hdr_a,
            json={"filename": "secret_a.png", "media_type": "image/png", "content_base64": png_b64},
        )
        assert up_a.status_code == 200
        upload_id_a = up_a.json()["upload_id"]

        # Tenant B cannot consume Tenant A's upload_id when creating a run
        steal_create = client.post(
            "/api/runs",
            headers=hdr_b,
            json={"prompt": "尝试窃取租户 A 附件", "attachments": [{"upload_id": upload_id_a}]},
        )
        assert steal_create.status_code == 404

        # Tenant B also cannot consume Tenant A's upload_id via POST /api/runs/{id}/messages
        run_b = client.post("/api/runs", headers=hdr_b, json={"prompt": "租户 B 自身会话"})
        assert run_b.status_code == 202
        run_b_id = run_b.json()["id"]
        _wait_run_settled(client, run_b_id, headers=hdr_b)

        steal_msg = client.post(
            f"/api/runs/{run_b_id}/messages",
            headers=hdr_b,
            json={"prompt": "追问尝试窃取", "attachments": [{"upload_id": upload_id_a}]},
        )
        assert steal_msg.status_code == 404

        # Tenant A's upload remains intact and Tenant A can consume it normally
        own_run = client.post(
            "/api/runs",
            headers=hdr_a,
            json={"prompt": "租户 A 正常使用附件", "attachments": [{"upload_id": upload_id_a}]},
        )
        assert own_run.status_code == 202
        settled_a = _wait_run_settled(client, own_run.json()["id"], headers=hdr_a)
        assert settled_a["status"] == "completed"
        assert len(settled_a["user_attachments"]) == 1
        assert settled_a["user_attachments"][0]["filename"] == "secret_a.png"
        assert _list_staged_files(state_dir / "staged_uploads") == []


def test_mid_run_steering_with_attachments_persists_without_dropping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)
    state_dir = tmp_path / "state"
    entered_step1 = threading.Event()
    release_step1 = threading.Event()
    seen_steering_prompt: list[bool] = []

    class PausedSteeringProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered_step1.set()
                await asyncio.to_thread(release_step1.wait, 10.0)
                return _resp([_call("repo_list")])
            # Step 2 runs after tool result + _drain_mid_run_steering
            serialized = json.dumps(messages, ensure_ascii=False)
            seen_steering_prompt.append("运行中补充：请参考报错截图" in serialized)
            return _resp([{"type": "text", "text": "已合并运行中补充指令与截图附件。"}])

    monkeypatch.setattr(runtime_mod, "get_provider", lambda _name: PausedSteeringProvider())
    app = create_app(state_dir=state_dir, provider="demo")

    steer_png = _make_valid_png(64, 48)
    steer_b64 = base64.b64encode(steer_png).decode("ascii")

    with TestClient(app) as client:
        created = client.post("/api/runs", json={"prompt": "先列出仓库文件"})
        assert created.status_code == 202
        run_id = created.json()["id"]

        assert entered_step1.wait(timeout=5.0), "provider did not enter step 1"
        try:
            mid_state = client.get(f"/api/runs/{run_id}").json()
            assert mid_state["status"] == "running"

            # Stage upload and attach it during mid-run steering while run is actively running
            staged = client.post(
                "/api/uploads",
                json={"filename": "error_screen.png", "media_type": "image/png", "content_base64": steer_b64},
            )
            assert staged.status_code == 200
            steer_uid = staged.json()["upload_id"]

            steer_res = client.post(
                f"/api/runs/{run_id}/messages",
                json={
                    "prompt": "运行中补充：请参考报错截图",
                    "attachments": [{"upload_id": steer_uid}],
                },
            )
            assert steer_res.status_code == 202
            steer_body = steer_res.json()
            assert steer_body["queued_steering"] is True
            assert len(steer_body["attachments"]) == 1
            assert steer_body["attachments"][0]["filename"] == "error_screen.png"
            assert steer_body["attachments"][0]["steering_id"] == steer_body["steering"]["id"]
            assert _list_staged_files(state_dir / "staged_uploads") == []
        finally:
            release_step1.set()

        settled = _wait_run_settled(client, run_id)

        assert settled["status"] == "completed"
        assert seen_steering_prompt == [True]
        # Subsequent runtime _save(run) calls MUST preserve the steering attachment
        atts = settled["user_attachments"]
        assert len(atts) == 1
        assert atts[0]["filename"] == "error_screen.png"
        assert atts[0]["turn_message_idx"] is not None

        dl = client.get(atts[0]["url"])
        assert dl.status_code == 200
        assert dl.content == steer_png

        # Steering turn in conversation_thread carries the attachment
        thread = settled["conversation_thread"]
        assert len(thread) == 2
        assert thread[1]["steering"] is True
        assert len(thread[1]["attachments"]) == 1
        assert thread[1]["attachments"][0]["filename"] == "error_screen.png"


# ---------------------------------------------------------------------------
# 2. Safe Workspace Browser, @file API, Background PTY API, Thread & Favicon
# ---------------------------------------------------------------------------


def test_workspace_browser_lists_dirs_hides_secrets_and_enforces_rbac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)
    browse_root = tmp_path / "projects"
    browse_root.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()

    # Visible directories
    git_proj = browse_root / "alpha_git"
    git_proj.mkdir()
    (git_proj / ".git").mkdir()
    plain_proj = browse_root / "beta_plain"
    plain_proj.mkdir()

    # Hidden / secret / symlink / file entries that must NOT appear
    (browse_root / ".git").mkdir()
    (browse_root / ".ssh").mkdir()
    (browse_root / ".hidden").mkdir()
    (browse_root / "secrets").mkdir()
    (browse_root / "credentials").mkdir()
    (browse_root / "symlink_escape").symlink_to(outside)
    (browse_root / "notes.txt").write_text("not a dir\n", encoding="utf-8")

    tokens_cfg = {
        "tok-local-admin": {
            "tenant_id": "local",
            "actor_id": "local-owner",
            "roles": ["operator", "approver", "admin"],
        },
        "tok-ext-op": {
            "tenant_id": "tenant_ext",
            "actor_id": "ext-op",
            "roles": ["operator"],
        },
        "tok-ext-admin": {
            "tenant_id": "tenant_ext",
            "actor_id": "ext-admin",
            "roles": ["operator", "admin"],
        },
    }
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens_cfg))
    monkeypatch.setenv("AGENT_LAB_ALLOWED_WORKSPACES", str(browse_root))
    app = create_app(state_dir=tmp_path / "state", provider="demo")

    with TestClient(app) as client:
        # Local admin allowed
        ok_res = client.get(
            "/api/workspaces/browse",
            params={"path": str(browse_root)},
            headers={"Authorization": "Bearer tok-local-admin"},
        )
        assert ok_res.status_code == 200
        body = ok_res.json()
        assert body["ok"] is True
        assert body["allowed_roots"] == [str(browse_root.resolve())]
        assert body["current_path"] == str(browse_root.resolve())

        dirs = {d["name"]: d for d in body["directories"]}
        assert set(dirs) == {"alpha_git", "beta_plain"}
        assert dirs["alpha_git"]["is_git"] is True
        assert dirs["beta_plain"]["is_git"] is False

        # Browsing forbidden dot dir or outside allowed_roots rejected with 400
        dot_res = client.get(
            "/api/workspaces/browse",
            params={"path": str(browse_root / ".ssh")},
            headers={"Authorization": "Bearer tok-local-admin"},
        )
        assert dot_res.status_code == 400

        out_res = client.get(
            "/api/workspaces/browse",
            params={"path": str(outside)},
            headers={"Authorization": "Bearer tok-local-admin"},
        )
        assert out_res.status_code == 400

        # Non-admin external tenant AND non-global external tenant admin rejected with 403
        ext_op = client.get(
            "/api/workspaces/browse",
            params={"path": str(browse_root)},
            headers={"Authorization": "Bearer tok-ext-op"},
        )
        assert ext_op.status_code == 403

        ext_admin = client.get(
            "/api/workspaces/browse",
            params={"path": str(browse_root)},
            headers={"Authorization": "Bearer tok-ext-admin"},
        )
        assert ext_admin.status_code == 403


def test_workspace_and_run_files_api_prunes_junk_and_excludes_secrets_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / ".venv" / "lib").mkdir(parents=True)
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / ".git").mkdir(parents=True)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("OUTSIDE_SECRET\n", encoding="utf-8")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "linked.py").write_text("x = 1\n", encoding="utf-8")

    (ws / "README.md").write_text("# Demo\n", encoding="utf-8")
    (ws / "src" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    (ws / "src" / "utils.py").write_text("UTIL = 2\n", encoding="utf-8")
    (ws / ".venv" / "lib" / "site.py").write_text("# venv\n", encoding="utf-8")
    (ws / "node_modules" / "pkg" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (ws / ".env").write_text("API_KEY=topsecret\n", encoding="utf-8")
    (ws / "secrets.yaml").write_text("token: abc\n", encoding="utf-8")
    (ws / "link_escape.txt").symlink_to(outside)
    (ws / "link_dir").symlink_to(outside_dir)

    class QuickProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([{"type": "text", "text": "已检视工作区。"}])

    monkeypatch.setattr(runtime_mod, "get_provider", lambda _name: QuickProvider())
    monkeypatch.setenv("AGENT_LAB_ALLOWED_WORKSPACES", str(ws))
    app = create_app(state_dir=tmp_path / "state", provider="demo")

    with TestClient(app) as client:
        # 1. GET /api/workspaces/files
        ws_res = client.get("/api/workspaces/files", params={"workspace_path": str(ws)})
        assert ws_res.status_code == 200
        ws_data = ws_res.json()
        assert ws_data["ok"] is True
        assert sorted(ws_data["files"]) == ["README.md", "src/app.py", "src/utils.py"]

        ws_q = client.get("/api/workspaces/files", params={"workspace_path": str(ws), "q": "app"})
        assert ws_q.status_code == 200
        assert ws_q.json()["files"] == ["src/app.py"]

        # 2. GET /api/runs/{id}/files
        created = client.post(
            "/api/runs",
            json={
                "prompt": "查看项目文件",
                "template": "local_cwd",
                "workspace_path": str(ws),
                "execution_profile": "host",
                "permission_mode": "auto-edit",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        _wait_run_settled(client, run_id)

        run_files = client.get(f"/api/runs/{run_id}/files")
        assert run_files.status_code == 200
        rf_data = run_files.json()
        assert rf_data["ok"] is True
        assert rf_data["run_id"] == run_id
        assert sorted(rf_data["files"]) == ["README.md", "src/app.py", "src/utils.py"]

        run_files_q = client.get(f"/api/runs/{run_id}/files", params={"q": "utils"})
        assert run_files_q.status_code == 200
        assert run_files_q.json()["files"] == ["src/utils.py"]


def test_background_processes_api_list_kill_and_audit_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)
    monkeypatch.setenv("AGENT_LAB_HOST_SANDBOX", "off")
    ws = tmp_path / "bg_ws"
    ws.mkdir()
    monkeypatch.setenv("AGENT_LAB_ALLOWED_WORKSPACES", str(ws))

    bg_started = threading.Event()
    release_step2 = threading.Event()

    class BgProcessProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp(
                    [
                        _call(
                            "bash_exec",
                            command=f"{sys.executable} -c 'import time; time.sleep(30)'",
                            run_in_background=True,
                            use_pty=True,
                        )
                    ]
                )
            bg_started.set()
            await asyncio.to_thread(release_step2.wait, 10.0)
            return _resp([{"type": "text", "text": "后台进程检查完成。"}])

    monkeypatch.setattr(runtime_mod, "get_provider", lambda _name: BgProcessProvider())
    app = create_app(state_dir=tmp_path / "state", provider="demo")

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={
                "prompt": "启动一个后台测试进程",
                "template": "local_cwd",
                "workspace_path": str(ws),
                "execution_profile": "host",
                "permission_mode": "full-auto",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["id"]

        assert bg_started.wait(timeout=5.0), "background process did not start in time"
        try:
            # 1. GET /api/runs/{id}/processes returns live active PTY process
            procs_res = client.get(f"/api/runs/{run_id}/processes")
            assert procs_res.status_code == 200
            procs_body = procs_res.json()
            assert procs_body["ok"] is True
            assert procs_body["live"] is True
            assert len(procs_body["processes"]) == 1
            proc = procs_body["processes"][0]
            proc_id = proc["proc_id"]
            assert proc["running"] is True
            assert proc["status"] == "running"
            assert proc["use_pty"] is True

            # 2. Unknown proc_id -> 404
            missing_kill = client.post(f"/api/runs/{run_id}/processes/proc_missing/kill")
            assert missing_kill.status_code == 404

            # 3. POST /api/runs/{id}/processes/{proc_id}/kill terminates the live process
            kill_res = client.post(f"/api/runs/{run_id}/processes/{proc_id}/kill")
            assert kill_res.status_code == 200
            kill_body = kill_res.json()
            assert kill_body["ok"] is True
            assert kill_body["proc_id"] == proc_id
            assert kill_body["status"] == "killed"

            # Process is no longer running
            after_kill = client.get(f"/api/runs/{run_id}/processes").json()
            assert after_kill["processes"][0]["running"] is False
            assert after_kill["processes"][0]["status"] == "killed"

            # Audit log entry recorded
            audits = app.state.storage.list_audit_logs(action="process_kill")
            assert any(
                a["action"] == "process_kill"
                and a["resource"] == f"run:{run_id}:proc:{proc_id}"
                and a["outcome"] == "allowed"
                for a in audits
            )

            # Run event recorded
            events = app.state.storage.events(run_id)
            assert any(
                ev["kind"] == "background_process_killed" and ev["payload"].get("proc_id") == proc_id
                for ev in events
            )
        finally:
            release_step2.set()

        settled = _wait_run_settled(client, run_id)
        assert settled["status"] == "completed"

        # After completion, no live runtime -> gracefully returns live=False, empty processes
        after_done = client.get(f"/api/runs/{run_id}/processes")
        assert after_done.status_code == 200
        assert after_done.json()["live"] is False
        assert after_done.json()["processes"] == []


def test_run_detail_rich_conversation_thread_with_thinking_and_favicon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_api_env(monkeypatch)

    class ThinkingMultiTurnProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp(
                    [
                        {"type": "thinking", "thinking": "第一轮思考：分析折扣率计算公式"},
                        {"type": "text", "text": "第一轮回答：已定位折扣率公式。"},
                    ]
                )
            return _resp(
                [
                    {"type": "thinking", "thinking": "第二轮思考：覆盖 0% 与 100% 边界测试"},
                    {"type": "text", "text": "第二轮回答：已补充边界测试用例。"},
                ]
            )

    provider = ThinkingMultiTurnProvider()
    monkeypatch.setattr(runtime_mod, "get_provider", lambda _name: provider)
    app = create_app(state_dir=tmp_path / "state", provider="demo")

    with TestClient(app) as client:
        # GET /favicon.ico
        fav = client.get("/favicon.ico")
        assert fav.status_code == 200
        assert fav.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in fav.text and "</svg>" in fav.text

        # Turn 1
        r1 = client.post("/api/runs", json={"prompt": "第一轮：请分析折扣计算逻辑"})
        assert r1.status_code == 202
        run_id = r1.json()["id"]
        _wait_run_settled(client, run_id)

        # Turn 2
        r2 = client.post(
            f"/api/runs/{run_id}/messages",
            json={"prompt": "第二轮：请补充边界测试说明"},
        )
        assert r2.status_code == 202
        settled = _wait_run_settled(client, run_id)
        assert settled["status"] == "completed"

        thread = settled["conversation_thread"]
        assert len(thread) == 2

        assert thread[0]["turn_index"] == 0
        assert thread[0]["user_prompt"] == "第一轮：请分析折扣计算逻辑"
        assert thread[0]["assistant_answer"] == "第一轮回答：已定位折扣率公式。"
        assert thread[0]["thinking"] == "第一轮思考：分析折扣率计算公式"

        assert thread[1]["turn_index"] == 1
        assert thread[1]["user_prompt"] == "第二轮：请补充边界测试说明"
        assert thread[1]["assistant_answer"] == "第二轮回答：已补充边界测试用例。"
        assert thread[1]["thinking"] == "第二轮思考：覆盖 0% 与 100% 边界测试"

        assert settled["initial_turn"]["user_prompt"] == thread[0]["user_prompt"]
        assert len(settled["conversation_turns"]) == 1
        assert settled["thinking"] == thread[1]["thinking"]


# ---------------------------------------------------------------------------
# 3. REPL UX Upgrades (repl.py)
# ---------------------------------------------------------------------------


def test_repl_list_workspace_files_pruned_skips_junk_secrets_and_symlinks(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src" / "sub").mkdir(parents=True)
    (ws / ".venv" / "lib").mkdir(parents=True)
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / ".git").mkdir(parents=True)
    (ws / "__pycache__").mkdir(parents=True)
    (ws / "credentials").mkdir(parents=True)

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret\n", encoding="utf-8")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.py").write_text("x = 1\n", encoding="utf-8")

    (ws / "README.md").write_text("# App\n", encoding="utf-8")
    (ws / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (ws / "src" / "sub" / "helper.py").write_text("def f(): pass\n", encoding="utf-8")

    # Files that must be pruned / excluded
    (ws / ".venv" / "lib" / "site.py").write_text("", encoding="utf-8")
    (ws / "node_modules" / "pkg" / "index.js").write_text("", encoding="utf-8")
    (ws / ".git" / "config").write_text("", encoding="utf-8")
    (ws / "__pycache__" / "main.pyc").write_bytes(b"\x00")
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (ws / "secrets.json").write_text("{}", encoding="utf-8")
    (ws / "credentials" / "token.txt").write_text("tok", encoding="utf-8")
    (ws / "escape_link.txt").symlink_to(outside_file)
    (ws / "escape_dir").symlink_to(outside_dir)

    files = _list_workspace_files_pruned(ws)
    assert files == ["README.md", "src/main.py", "src/sub/helper.py"]
    assert _list_workspace_files_pruned(ws, limit=2) == ["README.md", "src/main.py"]


async def test_repl_approval_anti_mistype_and_v_full_untruncated_diff(tmp_path: Path) -> None:
    big_content = "\n".join(f"LINE_{i:03d} = {i}" for i in range(1, 111)) + "\n"

    class BigWriteProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp([_call("fs_write", path="big_module.py", content=big_content)])
            return _resp([{"type": "text", "text": "已写入 big_module.py。"}])

    ws, storage, runtime = _make_runtime(tmp_path, BigWriteProvider(), permission_mode="ask")
    created = runtime.create("创建 110 行文件 big_module.py")
    paused = await runtime.execute(created["id"])
    assert paused["status"] == "waiting_approval"
    pid = paused["metadata"]["waiting_proposal_ids"][0]

    # Sequence: mistype "ye" (re-prompt, do NOT reject) -> "v" (full untruncated diff) -> "y" (approve)
    scripted = iter(["ye", "v", "y"])
    printed: list[str] = []
    prompt_count = 0

    def _input(_prompt: str) -> str:
        nonlocal prompt_count
        prompt_count += 1
        return next(scripted)

    final_run = await drain_approvals_interactively(
        runtime, paused, input_fn=_input, output_fn=printed.append
    )
    await runtime.aclose()

    assert prompt_count == 3
    # Initial preview card truncated at 80 lines (omission hint shown, LINE_110 absent from card 0)
    assert "输入 v 查看完整 Diff" in printed[0]
    assert "LINE_110 = 110" not in printed[0]

    joined = "\n".join(printed)
    assert "无法识别的选项 'ye'（未静默拒绝" in joined
    # After typing "v", full untruncated diff is displayed including line 110
    assert "+LINE_110 = 110" in joined

    assert final_run["status"] == "completed"
    prop = storage.get_proposal(pid)
    assert prop is not None
    assert prop["status"] == "approved"
    assert prop["applied"] is True
    assert "LINE_110 = 110" in (ws / "big_module.py").read_text(encoding="utf-8")


async def test_repl_unknown_slash_command_warns_without_model_call_and_skill_routes(
    tmp_path: Path,
) -> None:
    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            return _resp([{"type": "text", "text": "已完成 code-review 技能审查。"}])

    provider = CountingProvider()
    _, storage, runtime = _make_runtime(tmp_path, provider, permission_mode="auto-edit")

    scripted = iter(
        [
            "/statu",
            "/unknown_cmd",
            "/code-review 请审查当前工作区",
            "/exit",
        ]
    )
    printed: list[str] = []

    final_run = await run_interactive_repl(
        runtime,
        input_fn=lambda _p: next(scripted),
        output_fn=printed.append,
    )
    await runtime.aclose()

    joined = "\n".join(printed)
    assert "[未知命令] /statu" in joined
    assert "/status" in joined
    assert "[未知命令] /unknown_cmd" in joined

    # Unknown slash commands did NOT invoke the model; only /code-review did
    assert provider.calls == 1
    assert len(storage.list_runs()) == 1
    assert final_run is not None
    assert final_run["status"] == "completed"
    assert "code-review" in (final_run.get("metadata") or {}).get("active_skills", {})


def test_repl_render_terminal_markdown_color_and_plain() -> None:
    sample_md = (
        "# Heading Title\n\n"
        "Use **bold** and `inline_code`.\n\n"
        "```python\n"
        "print('hello')\n"
        "```"
    )
    plain = _render_terminal_markdown(sample_md, use_color=False)
    assert plain == sample_md
    assert "\x1b[" not in plain

    colored = _render_terminal_markdown(sample_md, use_color=True)
    assert colored != sample_md
    assert "\x1b[" in colored
    stripped = _ANSI_RE.sub("", colored)
    assert "Heading Title" in stripped
    assert "bold" in stripped
    assert "inline_code" in stripped
    assert "print('hello')" in stripped
    assert _render_terminal_markdown("", use_color=True) == ""


async def test_repl_compact_startup_banner_model_id_and_demo_warning(tmp_path: Path) -> None:
    # 1. Demo provider: shows resolved model label and demo-mode warning
    _, _, demo_rt = _make_runtime(tmp_path / "demo_case", "demo", permission_mode="auto-edit")
    demo_out: list[str] = []
    await run_interactive_repl(
        demo_rt,
        input_fn=lambda _p: "/exit",
        output_fn=demo_out.append,
    )
    await demo_rt.aclose()
    demo_banner = demo_out[0]
    assert "模型: demo" in demo_banner
    assert "离线 DEMO 模式" in demo_banner
    assert "agent-lab init" in demo_banner

    # 2. Named model provider: shows resolved model ID without demo warning
    class NamedModelProvider:
        model = "claude-sonnet-4-6"

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            return _resp([{"type": "text", "text": "ok"}])

    _, _, real_rt = _make_runtime(
        tmp_path / "model_case", NamedModelProvider(), permission_mode="auto-edit"
    )
    real_out: list[str] = []
    await run_interactive_repl(
        real_rt,
        input_fn=lambda _p: "/exit",
        output_fn=real_out.append,
    )
    await real_rt.aclose()
    real_banner = real_out[0]
    assert "模型: claude-sonnet-4-6" in real_banner
    assert "离线 DEMO 模式" not in real_banner


# ---------------------------------------------------------------------------
# 4. CLI Onboarding & Diagnostics (cli.py)
# ---------------------------------------------------------------------------


def test_cli_doctor_json_structured_checks_and_zero_secret_leaks(tmp_path: Path) -> None:
    proj = tmp_path / "doctor_proj"
    proj.mkdir()

    # 动态拼接占位测试凭证，避免触发 GitGuardian 误报同时完整验证脱敏不泄露
    secret_ant = "sk" + "-ant-test-placeholder-11111"
    secret_oai = "sk" + "-openai-test-placeholder-22222"
    secret_tok = "lab" + "-test-placeholder-token-33333"
    secret_auth = "sk" + "-ant-auth-placeholder-44444"

    (proj / ".env").write_text(
        f"AGENT_PROVIDER=anthropic\n"
        f"ANTHROPIC_API_KEY={secret_ant}\n"
        f"ANTHROPIC_MODEL=claude-sonnet-4-6\n"
        f"OPENAI_API_KEY={secret_oai}\n"
        f"AGENT_LAB_TOKEN={secret_tok}\n",
        encoding="utf-8",
    )

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AGENT_PROVIDER",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "AGENT_LAB_TOKEN",
            "AGENT_LAB_TOKENS_JSON",
            "AGENT_LAB_NO_DOTENV",
        }
    }
    clean_env["ANTHROPIC_AUTH_TOKEN"] = secret_auth

    proc = subprocess.run(
        [sys.executable, "-m", "agent_lab.cli", "doctor", "--cwd", str(proj), "--json"],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    combined_output = proc.stdout + "\n" + proc.stderr
    for leaked in (secret_ant, secret_oai, secret_tok, secret_auth):
        assert leaked not in combined_output

    report = json.loads(proc.stdout)
    assert report["cwd"] == str(proj.resolve())
    checks_by_id = {item["id"]: item for item in report["checks"]}
    expected_ids = {
        "dotenv_and_provider",
        "docker_sandbox",
        "os_kernel_sandbox",
        "lsp_servers",
        "python_extras",
    }
    assert expected_ids.issubset(checks_by_id)
    for cid in expected_ids:
        entry = checks_by_id[cid]
        assert entry["status"] in {"pass", "warn", "fail"}
        assert isinstance(entry["summary"], str) and entry["summary"]
        assert isinstance(entry["details"], dict)

    prov_check = checks_by_id["dotenv_and_provider"]
    assert prov_check["status"] == "pass"
    assert prov_check["details"]["active_provider"] == "anthropic"
    assert prov_check["details"]["active_model"] == "claude-sonnet-4-6"
    assert prov_check["details"]["cwd_dotenv_exists"] is True
    assert prov_check["details"]["providers"]["anthropic"]["key_configured"] is True
    assert prov_check["details"]["providers"]["openai"]["key_configured"] is True


def test_cli_init_non_interactive_0600_and_refuses_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_api_env(monkeypatch)
    monkeypatch.setenv("AGENT_LAB_NO_DOTENV", "1")
    proj = tmp_path / "init_proj"
    proj.mkdir()
    env_file = proj / ".env"

    # 1. First run creates .env with 0600 permissions
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-lab", "init", "--cwd", str(proj), "--provider", "demo", "--non-interactive"],
    )
    cli_mod.main()
    out1 = capsys.readouterr().out
    assert "0600" in out1
    assert env_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "AGENT_PROVIDER=demo" in env_file.read_text(encoding="utf-8")

    # 2. Existing .env is NOT overwritten without --force (exits non-zero)
    sentinel = "# KEEP_MY_CUSTOM_CONFIG\nAGENT_PROVIDER=anthropic\n"
    env_file.write_text(sentinel, encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-lab", "init", "--cwd", str(proj), "--provider", "demo", "--non-interactive"],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()
    assert exc_info.value.code == 1
    err2 = capsys.readouterr().err
    assert "--force" in err2
    assert env_file.read_text(encoding="utf-8") == sentinel

    # 3. Passing --force overwrites cleanly while preserving 0600 permissions
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-lab", "init", "--cwd", str(proj), "--provider", "demo", "--non-interactive", "--force"],
    )
    cli_mod.main()
    capsys.readouterr()
    content_after = env_file.read_text(encoding="utf-8")
    assert "AGENT_PROVIDER=demo" in content_after
    assert "KEEP_MY_CUSTOM_CONFIG" not in content_after
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_cli_list_table_json_and_code_continue_and_short_prefix_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_api_env(monkeypatch)
    monkeypatch.setenv("AGENT_LAB_NO_DOTENV", "1")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ws_a = tmp_path / "ws_alpha"
    ws_b = tmp_path / "ws_beta"
    ws_empty = tmp_path / "ws_empty"
    for d in (ws_a, ws_b, ws_empty):
        d.mkdir()

    storage = Storage(state_dir / "state.db")

    def _seed_run(prompt: str, ws: Path, *, title: str, steps: int, cost_usd: float) -> dict[str, Any]:
        run = storage.create_run(prompt, mode="agent", provider="demo")
        slug = compute_project_slug(ws, direct_workspace=True, state_dir=state_dir)
        run["status"] = "completed"
        run["steps"] = steps
        run["metadata"].update(
            {
                "workspace": str(ws.resolve()),
                "direct_workspace": True,
                "project_slug": slug,
                "execution_profile": "host",
                "permission_mode": "auto-edit",
                "cost_usd": cost_usd,
                "max_steps": 24,
                "max_tokens": 120_000,
                "context_chars": 64_000,
            }
        )
        storage.save_run(run)
        storage.rename_run(run["id"], title)
        time.sleep(0.005)
        return storage.get_run(run["id"]) or run

    run_a_old = _seed_run("修复 alpha 旧缺陷", ws_a, title="Alpha Old", steps=2, cost_usd=0.0012)
    run_a_latest = _seed_run("修复 alpha 新缺陷", ws_a, title="Alpha Latest", steps=4, cost_usd=0.0155)
    run_b_newest = _seed_run("实现 beta 接口", ws_b, title="Beta Newest", steps=3, cost_usd=0.024)
    run_archived = _seed_run("归档旧任务", ws_a, title="Archived One", steps=1, cost_usd=0.0005)
    storage.archive_run(run_archived["id"], True)

    # 1. `agent-lab list` renders human-readable aligned table (excluding archived by default)
    monkeypatch.setattr(sys, "argv", ["agent-lab", "--state", str(state_dir), "list"])
    cli_mod.main()
    table_out = capsys.readouterr().out
    for header in ("ID", "STATUS", "STEPS", "COST", "MODE", "TITLE / PROMPT"):
        assert header in table_out
    assert run_a_old["id"][:8] in table_out
    assert run_a_latest["id"][:8] in table_out
    assert run_b_newest["id"][:8] in table_out
    assert "$0.0155" in table_out
    assert "Alpha Latest" in table_out
    assert "Archived One" not in table_out
    assert "共 3 条" in table_out

    # 2. `agent-lab list --json` (and `--all`) renders machine-readable JSON array
    monkeypatch.setattr(sys, "argv", ["agent-lab", "--state", str(state_dir), "list", "--json"])
    cli_mod.main()
    list_json = json.loads(capsys.readouterr().out)
    assert isinstance(list_json, list)
    assert len(list_json) == 3
    assert [r["id"] for r in list_json] == [run_b_newest["id"], run_a_latest["id"], run_a_old["id"]]

    monkeypatch.setattr(sys, "argv", ["agent-lab", "--state", str(state_dir), "list", "--json", "--all"])
    cli_mod.main()
    list_all_json = json.loads(capsys.readouterr().out)
    assert len(list_all_json) == 4

    # 3. `agent-lab code -c` & short-prefix `--resume`
    captured_repl_calls: list[dict[str, Any]] = []

    async def _fake_repl(runtime: AgentRuntime, **kwargs: Any) -> None:
        captured_repl_calls.append(
            {
                "workspace": runtime.workspace,
                "resume_run_id": kwargs.get("resume_run_id"),
                "permission_mode": runtime.permission_mode,
            }
        )

    monkeypatch.setattr(cli_mod, "run_interactive_repl", _fake_repl)

    # 3a. `-c` in ws_a selects the latest non-archived run for ws_a (ignoring newer ws_b run & archived run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-lab",
            "--state",
            str(state_dir),
            "code",
            "--cwd",
            str(ws_a),
            "-c",
            "--provider",
            "demo",
            "--no-auto-dream",
        ],
    )
    cli_mod.main()
    capsys.readouterr()
    assert captured_repl_calls[-1]["resume_run_id"] == run_a_latest["id"]
    assert captured_repl_calls[-1]["workspace"] == ws_a.resolve()

    # 3b. `-c` in ws_empty falls back to globally latest non-archived run (run_b_newest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-lab",
            "--state",
            str(state_dir),
            "code",
            "--cwd",
            str(ws_empty),
            "-c",
            "--provider",
            "demo",
            "--no-auto-dream",
        ],
    )
    cli_mod.main()
    fallback_err = capsys.readouterr().err
    assert captured_repl_calls[-1]["resume_run_id"] == run_b_newest["id"]
    assert captured_repl_calls[-1]["workspace"] == ws_b.resolve()
    assert "自动回退" in fallback_err

    # 3c. Short-prefix `--resume` resolves exact run and aligns workspace to original dir
    short_prefix = run_a_old["id"][:8]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-lab",
            "--state",
            str(state_dir),
            "code",
            "--cwd",
            str(ws_empty),
            "--resume",
            short_prefix,
            "--provider",
            "demo",
            "--no-auto-dream",
        ],
    )
    cli_mod.main()
    capsys.readouterr()
    assert captured_repl_calls[-1]["resume_run_id"] == run_a_old["id"]
    assert captured_repl_calls[-1]["workspace"] == ws_a.resolve()

    # 3d. Prefix resolver guards: missing and ambiguous prefix errors
    with pytest.raises(LookupError):
        cli_mod._resolve_run_by_id_or_prefix(storage, "zzzzzzzz")

    class _AmbiguousStorage:
        def get_run(self, _rid: str) -> None:
            return None

        def list_runs(self, *, limit: int = 1000, include_archived: bool = True) -> list[dict[str, Any]]:
            return [
                {"id": "deadbeef11111111", "status": "completed"},
                {"id": "deadbeef22222222", "status": "completed"},
            ]

    with pytest.raises(ValueError, match="存在歧义"):
        cli_mod._resolve_run_by_id_or_prefix(_AmbiguousStorage(), "deadbeef")  # type: ignore[arg-type]

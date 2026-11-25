"""Fast, deterministic, 100% offline tests for bidirectional A2A (Agent2Agent) protocol support."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_lab.a2a import (
    A2A_PROTOCOL_VERSION,
    A2A_REDACTED_MARKER,
    A2A_TASK_NOT_CANCELABLE,
    A2A_TASK_NOT_FOUND,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    A2AClientManager,
    default_a2a_template,
    discover_a2a_config_path,
    load_a2a_config,
    redact_a2a_document,
    save_a2a_config,
    validate_a2a_document,
    validate_a2a_url,
)
from agent_lab.api import ROOT, create_app
import agent_lab.cli as cli_mod
from agent_lab.permissions import (
    PermissionRuleEngine,
    evaluate_tool_permission,
    suggest_session_grant_pattern,
)
from agent_lab.repl import run_interactive_repl
from agent_lab.runtime import AgentRuntime, canonical_json
from agent_lab.storage import Storage

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "AGENT_LAB_SSE_KEEPALIVE_SECONDS",
        "AGENT_PROVIDER",
        "AGENT_GATEWAY_PROFILE",
        "AGENT_LAB_GATEWAYS_FILE",
        "AGENT_LAB_A2A_CONFIG",
        "AGENT_LAB_A2A_ALLOW_PRIVATE",
        "AGENT_LAB_TRUSTED_WORKSPACES",
        "AGENT_LAB_NO_DOTENV",
        "A2A_AGENT_B_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def _extract_user_prompts(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if content.startswith(
            ("HARNESS_STATE\n", "[PINNED_", "[HARNESS_STATE]", "[WORKING_MEMORY]", "[A2A_PEER_DIRECTORY]")
        ):
            continue
        out.append(content)
    return out


def _resp(
    content: list[dict[str, Any]],
    *,
    input_tokens: int = 20,
    output_tokens: int = 10,
) -> dict[str, Any]:
    return {
        "content": content,
        "stop_reason": "tool_use" if any(c.get("type") == "tool_use" for c in content) else "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    req_id: str | int = "req-1",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    r = client.post("/a2a", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _user_msg(
    text: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": "msg-test-1",
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
    }
    if task_id is not None:
        msg["taskId"] = task_id
    if context_id is not None:
        msg["contextId"] = context_id
    return msg


def _make_runtime(
    tmp_path: Path,
    provider: Any,
    *,
    execution_profile: str = "host",
    permission_mode: str = "ask",
    a2a_manager: A2AClientManager | None = None,
    a2a_config: Path | None = None,
    rule_engine: PermissionRuleEngine | None = None,
    ws: Path | None = None,
    db_name: str = "state.db",
) -> tuple[Path, Storage, AgentRuntime]:
    workspace = ws if ws is not None else (tmp_path / "ws")
    workspace.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / db_name)
    runtime = AgentRuntime(
        storage,
        workspace,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=provider,
        execution_profile=execution_profile,
        permission_mode=permission_mode,
        a2a_manager=a2a_manager,
        a2a_config=a2a_config,
        rule_engine=rule_engine,
        auto_dream=False,
        max_steps=8,
    )
    return workspace, storage, runtime


# ---------------------------------------------------------------------------
# 1. A2A AgentCard Discovery & Extended Card
# ---------------------------------------------------------------------------


def test_a2a_public_agent_cards_and_authenticated_extended_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    secret_token = "tok" + "-a2a-placeholder-999"
    secret_ant = "sk" + "-ant-placeholder-" + ("a" * 10)
    secret_peer_key = "sk" + "-peer-placeholder-777"
    secret_peer_hdr = "hdr" + "-peer-placeholder-666"
    monkeypatch.setenv("AGENT_LAB_TOKEN", secret_token)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_ant)

    state_dir = tmp_path / "state_card_secret_dir"
    state_dir.mkdir()
    save_a2a_config(
        state_dir / "a2a.json",
        {
            "schema_version": 1,
            "peers": {
                "hidden-peer": {
                    "name": "hidden-peer",
                    "url": "https://peer.example.com",
                    "api_key": secret_peer_key,
                    "headers": {"X-API-Key": secret_peer_hdr},
                    "enabled": True,
                }
            },
        },
    )

    app = create_app(state_dir=state_dir, provider="demo")
    with TestClient(app) as client:
        r_modern = client.get("/.well-known/agent-card.json")
        r_legacy = client.get("/.well-known/agent.json")
        assert r_modern.status_code == 200
        assert r_legacy.status_code == 200

        card = r_modern.json()
        assert card == r_legacy.json()
        assert card["name"] == "Agent Systems Lab"
        assert card["url"].endswith("/a2a")
        assert card["protocolVersion"] == A2A_PROTOCOL_VERSION
        assert card["capabilities"]["streaming"] is True
        assert card["capabilities"]["stateTransitionHistory"] is True
        assert card["supportsAuthenticatedExtendedCard"] is True

        skill_ids = {s["id"] for s in card["skills"]}
        assert "code-engineering" in skill_ids
        assert "architecture-research" in skill_ids

        interfaces = card["supportedInterfaces"]
        assert isinstance(interfaces, list) and len(interfaces) >= 1
        assert interfaces[0]["protocolBinding"] == "JSONRPC"
        assert interfaces[0]["protocolVersion"] == "1.0"
        assert interfaces[0]["url"] == card["url"]

        assert "bearerAuth" in card["securitySchemes"]
        assert card["securitySchemes"]["bearerAuth"]["type"] == "http"
        assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
        assert card["security"] == [{"bearerAuth": []}]
        assert "metadata" not in card

        # Extended AgentCard strictly requires authentication
        r_ext_unauth = client.get("/extendedAgentCard")
        assert r_ext_unauth.status_code == 401
        r_ext_wrong = client.get("/extendedAgentCard", headers={"Authorization": "Bearer wrong-token"})
        assert r_ext_wrong.status_code == 401

        r_ext_ok = client.get(
            "/extendedAgentCard",
            headers={"Authorization": f"Bearer {secret_token}"},
        )
        assert r_ext_ok.status_code == 200
        ext_card = r_ext_ok.json()
        assert ext_card["name"] == card["name"]
        assert ext_card["capabilities"]["streaming"] is True
        assert "metadata" in ext_card
        assert ext_card["metadata"]["gatewayProtocol"] == "demo"
        methods = set(ext_card["metadata"]["supportedRpcMethods"])
        for required_method in (
            "message/send",
            "SendMessage",
            "message/stream",
            "SendStreamingMessage",
            "tasks/get",
            "GetTask",
            "tasks/list",
            "ListTasks",
            "tasks/cancel",
            "CancelTask",
        ):
            assert required_method in methods

        # Zero secret or internal filesystem path leakage across both public and extended cards
        for serialized in (json.dumps(card), json.dumps(ext_card)):
            for forbidden in (
                secret_token,
                secret_ant,
                secret_peer_key,
                secret_peer_hdr,
                str(state_dir),
                "hidden-peer",
            ):
                assert forbidden not in serialized


# ---------------------------------------------------------------------------
# 2. A2A JSON-RPC 2.0 Server (Dual v0.2/v0.3 & v1.0)
# ---------------------------------------------------------------------------


async def test_a2a_jsonrpc_send_blocking_and_non_blocking_dual_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    gate_started = asyncio.Event()
    gate_release = asyncio.Event()
    gate_enabled = False

    class DualEchoProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            user_texts = _extract_user_prompts(messages)
            last_prompt = user_texts[-1] if user_texts else ""
            if gate_enabled:
                gate_started.set()
                await gate_release.wait()
            return _resp([{"type": "text", "text": f"Echoed: {last_prompt}"}])

    app = create_app(state_dir=tmp_path / "state", provider=DualEchoProvider())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
            # 1) v0.2/v0.3 message/send (blocking=True)
            r1 = await client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": "send-v03",
                    "method": "message/send",
                    "params": {
                        "message": _user_msg("Hello v0.3", context_id="ctx-v03"),
                        "configuration": {"blocking": True},
                    },
                },
            )
            assert r1.status_code == 200
            body1 = r1.json()
            assert body1["jsonrpc"] == "2.0"
            assert body1["id"] == "send-v03"
            t1 = body1["result"]
            assert t1["kind"] == "task"
            assert t1["id"]
            assert t1["contextId"] == "ctx-v03"
            assert t1["status"]["state"] == "completed"
            assert t1["status"]["timestamp"]
            assert any(
                a["name"] == "answer" and "Echoed: Hello v0.3" in a["parts"][0]["text"]
                for a in t1["artifacts"]
            )
            assert len(t1["history"]) >= 2
            assert any(h["role"] == "user" for h in t1["history"])
            assert any(h["role"] == "agent" for h in t1["history"])

            # 2) v1.0 SendMessage (blocking=True)
            r2 = await client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": "send-v10",
                    "method": "SendMessage",
                    "params": {
                        "message": _user_msg("Hello v1.0", context_id="ctx-v10"),
                        "configuration": {"blocking": True},
                    },
                },
            )
            assert r2.status_code == 200
            t2 = r2.json()["result"]
            assert t2["kind"] == "task"
            assert t2["contextId"] == "ctx-v10"
            assert t2["status"]["state"] == "completed"
            assert any(
                a["name"] == "answer" and "Echoed: Hello v1.0" in a["parts"][0]["text"]
                for a in t2["artifacts"]
            )

            # 3) Non-blocking message/send & SendMessage (blocking=False)
            for idx, method_name in enumerate(("message/send", "SendMessage"), start=1):
                gate_started.clear()
                gate_release.clear()
                gate_enabled = True
                r_nb = await client.post(
                    "/a2a",
                    json={
                        "jsonrpc": "2.0",
                        "id": f"nb-{idx}",
                        "method": method_name,
                        "params": {
                            "message": _user_msg(f"Async work {idx}", context_id=f"ctx-nb-{idx}"),
                            "configuration": {"blocking": False},
                        },
                    },
                )
                assert r_nb.status_code == 200
                t_nb = r_nb.json()["result"]
                assert t_nb["id"]
                assert t_nb["status"]["state"] in {"submitted", "working"}

                await asyncio.wait_for(gate_started.wait(), timeout=3.0)
                gate_release.set()

                get_method = "tasks/get" if method_name == "message/send" else "GetTask"
                final_task: dict[str, Any] = {}
                for _ in range(100):
                    rg = await client.post(
                        "/a2a",
                        json={
                            "jsonrpc": "2.0",
                            "id": f"poll-{idx}",
                            "method": get_method,
                            "params": {"id": t_nb["id"], "historyLength": 1},
                        },
                    )
                    final_task = rg.json()["result"]
                    if final_task["status"]["state"] == "completed":
                        break
                    await asyncio.sleep(0.02)

                assert final_task["status"]["state"] == "completed"
                assert len(final_task["history"]) == 1
                assert any(
                    a["name"] == "answer" and f"Echoed: Async work {idx}" in a["parts"][0]["text"]
                    for a in final_task["artifacts"]
                )


def test_a2a_jsonrpc_multiturn_waiting_approval_input_required_and_supersede(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    class ApprovalThenSummaryProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            user_texts = [
                m["content"]
                for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ]
            if any("Skip the patch" in t for t in user_texts):
                return _resp([{"type": "text", "text": "Superseded patch approval and summarized bug."}])

            has_proposed = any(
                isinstance(m.get("content"), list)
                and any(
                    b.get("type") == "tool_result" and "requires_approval" in str(b.get("content", ""))
                    for b in m["content"]
                    if isinstance(b, dict)
                )
                for m in messages
                if m.get("role") == "user"
            )
            if not has_proposed:
                return _resp(
                    [
                        {
                            "type": "tool_use",
                            "id": "call_patch_pricing",
                            "name": "propose_patch",
                            "input": {
                                "path": "pricing.py",
                                "old_text": "return subtotal * (1 - discount_percent)",
                                "new_text": "return subtotal * (1 - discount_percent / 100)",
                            },
                        }
                    ]
                )
            return _resp([{"type": "text", "text": "Awaiting human approval on pricing.py patch."}])

    app = create_app(state_dir=tmp_path / "state", provider=ApprovalThenSummaryProvider())
    with TestClient(app) as client:
        # Turn 1 triggers waiting_approval -> A2A state == "input-required"
        res1 = _rpc(
            client,
            "message/send",
            {
                "message": _user_msg("Fix pricing.py discount calculation", context_id="ctx-approval-mt"),
                "configuration": {"blocking": True},
            },
            req_id="mt-1",
        )
        t1 = res1["result"]
        assert t1["status"]["state"] == "input-required"
        assert t1["contextId"] == "ctx-approval-mt"
        status_msg = t1["status"]["message"]["parts"][0]["text"]
        assert "pricing.py" in status_msg or "审批" in status_msg

        # Turn 2 continues via taskId, superseding waiting_approval
        res2 = _rpc(
            client,
            "message/send",
            {
                "message": _user_msg(
                    "Skip the patch and just summarize the bug.",
                    task_id=t1["id"],
                    context_id="ctx-approval-mt",
                ),
                "configuration": {"blocking": True},
            },
            req_id="mt-2",
        )
        t2 = res2["result"]
        assert t2["id"] == t1["id"]
        assert t2["contextId"] == "ctx-approval-mt"
        assert t2["status"]["state"] == "completed"
        assert any(
            a["name"] == "answer" and "Superseded patch approval" in a["parts"][0]["text"]
            for a in t2["artifacts"]
        )
        user_hist = [h["parts"][0]["text"] for h in t2["history"] if h["role"] == "user"]
        assert len(user_hist) >= 2
        assert any("Fix pricing.py" in u for u in user_hist)
        assert any("Skip the patch" in u for u in user_hist)


def test_a2a_jsonrpc_get_list_cancel_and_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    class SelectiveApprovalProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            user_texts = [
                m["content"]
                for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ]
            if any("PAUSE_FOR_CANCEL" in t for t in user_texts):
                return _resp(
                    [
                        {
                            "type": "tool_use",
                            "id": "call_patch_cancel",
                            "name": "propose_patch",
                            "input": {
                                "path": "pricing.py",
                                "old_text": "return subtotal * (1 - discount_percent)",
                                "new_text": "return subtotal * (1 - discount_percent / 100)",
                            },
                        }
                    ]
                )
            return _resp([{"type": "text", "text": f"Done: {user_texts[-1]}"}])

    app = create_app(state_dir=tmp_path / "state", provider=SelectiveApprovalProvider())
    with TestClient(app) as client:
        t_alpha = _rpc(
            client,
            "message/send",
            {"message": _user_msg("Task Alpha", context_id="ctx-alpha"), "configuration": {"blocking": True}},
            req_id="a1",
        )["result"]
        t_beta = _rpc(
            client,
            "SendMessage",
            {"message": _user_msg("Task Beta", context_id="ctx-beta"), "configuration": {"blocking": True}},
            req_id="b1",
        )["result"]
        t_paused = _rpc(
            client,
            "message/send",
            {
                "message": _user_msg("PAUSE_FOR_CANCEL", context_id="ctx-alpha"),
                "configuration": {"blocking": True},
            },
            req_id="p1",
        )["result"]
        assert t_alpha["status"]["state"] == "completed"
        assert t_beta["status"]["state"] == "completed"
        assert t_paused["status"]["state"] == "input-required"

        # tasks/get & GetTask
        g1 = _rpc(client, "tasks/get", {"id": t_alpha["id"]}, req_id="g1")
        assert g1["result"]["id"] == t_alpha["id"]
        g2 = _rpc(client, "GetTask", {"id": t_beta["id"]}, req_id="g2")
        assert g2["result"]["id"] == t_beta["id"]

        # Unknown task ID -> -32001 TaskNotFoundError
        g_missing = _rpc(client, "tasks/get", {"id": "nonexistent-task-id"}, req_id="gm")
        assert g_missing["error"]["code"] == A2A_TASK_NOT_FOUND

        # tasks/list & ListTasks filtering by contextId
        list_all = _rpc(client, "tasks/list", {}, req_id="la")["result"]["tasks"]
        assert {t["id"] for t in list_all} == {t_alpha["id"], t_beta["id"], t_paused["id"]}

        list_alpha = _rpc(client, "tasks/list", {"contextId": "ctx-alpha"}, req_id="l-a")["result"]["tasks"]
        assert {t["id"] for t in list_alpha} == {t_alpha["id"], t_paused["id"]}

        list_beta = _rpc(client, "ListTasks", {"contextId": "ctx-beta"}, req_id="l-b")["result"]["tasks"]
        assert [t["id"] for t in list_beta] == [t_beta["id"]]

        # tasks/cancel & CancelTask error contracts
        c_missing = _rpc(client, "tasks/cancel", {"id": "no-such-task"}, req_id="cm")
        assert c_missing["error"]["code"] == A2A_TASK_NOT_FOUND
        c_missing_v1 = _rpc(client, "CancelTask", {"id": "no-such-task"}, req_id="cm1")
        assert c_missing_v1["error"]["code"] == A2A_TASK_NOT_FOUND

        c_done = _rpc(client, "tasks/cancel", {"id": t_alpha["id"]}, req_id="cd")
        assert c_done["error"]["code"] == A2A_TASK_NOT_CANCELABLE
        c_done_v1 = _rpc(client, "CancelTask", {"id": t_beta["id"]}, req_id="cd1")
        assert c_done_v1["error"]["code"] == A2A_TASK_NOT_CANCELABLE

        # Cancel non-terminal task succeeds -> state == "canceled"
        c_ok = _rpc(client, "CancelTask", {"id": t_paused["id"]}, req_id="cok")
        assert c_ok["result"]["id"] == t_paused["id"]
        assert c_ok["result"]["status"]["state"] == "canceled"

        # Unknown JSON-RPC method -> -32601 MethodNotFound
        m_unknown = _rpc(client, "unknown/method", {}, req_id="unk")
        assert m_unknown["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

        # Invalid JSON body -> -32700 ParseError
        r_bad = client.post("/a2a", content=b"{invalid json", headers={"Content-Type": "application/json"})
        assert r_bad.status_code == 200
        assert r_bad.json()["error"]["code"] == JSONRPC_PARSE_ERROR


async def test_a2a_jsonrpc_sse_streaming_message_stream_and_send_streaming_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    class StreamingChunkProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, on_event=None, **kwargs):
            chunks = ["Alpha ", "Beta ", "Gamma!"]
            if on_event is not None:
                for chunk in chunks:
                    await asyncio.sleep(0.01)
                    res = on_event({"type": "text_delta", "delta": chunk, "text": chunk})
                    if asyncio.iscoroutine(res):
                        await res
            return _resp([{"type": "text", "text": "".join(chunks)}])

    app = create_app(state_dir=tmp_path / "state", provider=StreamingChunkProvider())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
            for req_id, method_name in (
                ("stream-v03", "message/stream"),
                ("stream-v10", "SendStreamingMessage"),
            ):
                frames: list[dict[str, Any]] = []
                async with client.stream(
                    "POST",
                    "/a2a",
                    json={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": method_name,
                        "params": {
                            "message": _user_msg(f"Stream via {method_name}", context_id=f"ctx-{req_id}")
                        },
                    },
                ) as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            frames.append(json.loads(line[len("data: ") :]))

                assert len(frames) >= 3
                assert all(f["jsonrpc"] == "2.0" and f["id"] == req_id for f in frames)

                results = [f["result"] for f in frames]
                status_events = [e for e in results if e["kind"] == "status-update"]
                artifact_events = [e for e in results if e["kind"] == "artifact-update"]

                assert len(status_events) >= 2
                assert status_events[0]["status"]["state"] == "working"
                assert status_events[0]["final"] is False

                assert len(artifact_events) >= 1
                combined_text = "".join(
                    part["text"]
                    for ev in artifact_events
                    for part in ev["artifact"]["parts"]
                    if part.get("kind") == "text"
                )
                assert combined_text == "Alpha Beta Gamma!"

                assert results[-1]["kind"] == "status-update"
                assert results[-1]["status"]["state"] == "completed"
                assert results[-1]["final"] is True


def test_a2a_jsonrpc_multi_tenant_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    tokens_cfg = {
        "tok-a": {"tenant_id": "tenant_a", "actor_id": "alice", "roles": ["operator", "approver"]},
        "tok-b": {"tenant_id": "tenant_b", "actor_id": "bob", "roles": ["operator", "approver"]},
    }
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens_cfg))

    class TenantProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            user_texts = [
                m["content"]
                for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ]
            if any("HOLD_APPROVAL" in t for t in user_texts):
                return _resp(
                    [
                        {
                            "type": "tool_use",
                            "id": "call_t_patch",
                            "name": "propose_patch",
                            "input": {
                                "path": "pricing.py",
                                "old_text": "return subtotal * (1 - discount_percent)",
                                "new_text": "return subtotal * (1 - discount_percent / 100)",
                            },
                        }
                    ]
                )
            return _resp([{"type": "text", "text": "Tenant A confidential answer."}])

    app = create_app(state_dir=tmp_path / "state", provider=TenantProvider())
    hdr_a = {"Authorization": "Bearer tok-a"}
    hdr_b = {"Authorization": "Bearer tok-b"}

    with TestClient(app) as client:
        # Unauthenticated POST /a2a -> 401
        r_unauth = client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": "u1", "method": "tasks/list", "params": {}},
        )
        assert r_unauth.status_code == 401

        # Tenant A creates both a completed task and an input-required task
        t_a_done = _rpc(
            client,
            "message/send",
            {"message": _user_msg("Secret work A", context_id="ctx-tenant-a")},
            req_id="ta1",
            headers=hdr_a,
        )["result"]
        t_a_wait = _rpc(
            client,
            "message/send",
            {"message": _user_msg("HOLD_APPROVAL", context_id="ctx-tenant-a")},
            req_id="ta2",
            headers=hdr_a,
        )["result"]
        assert t_a_done["status"]["state"] == "completed"
        assert t_a_wait["status"]["state"] == "input-required"

        # Tenant B cannot GetTask / CancelTask / continue Tenant A's tasks -> -32001 TaskNotFoundError
        for method_name, tid in (
            ("tasks/get", t_a_done["id"]),
            ("GetTask", t_a_done["id"]),
            ("tasks/cancel", t_a_wait["id"]),
            ("CancelTask", t_a_wait["id"]),
        ):
            err_resp = _rpc(client, method_name, {"id": tid}, req_id=f"b-{method_name}", headers=hdr_b)
            assert err_resp["error"]["code"] == A2A_TASK_NOT_FOUND

        cont_err = _rpc(
            client,
            "message/send",
            {"message": _user_msg("Hijack attempt", task_id=t_a_wait["id"])},
            req_id="b-cont",
            headers=hdr_b,
        )
        assert cont_err["error"]["code"] == A2A_TASK_NOT_FOUND

        # Tenant B's ListTasks never sees Tenant A's tasks
        b_list = _rpc(client, "ListTasks", {}, req_id="b-list", headers=hdr_b)["result"]["tasks"]
        assert b_list == []

        # Tenant A retains intact access to both tasks
        a_get = _rpc(client, "GetTask", {"id": t_a_done["id"]}, req_id="a-get", headers=hdr_a)
        assert a_get["result"]["id"] == t_a_done["id"]
        a_wait = _rpc(client, "GetTask", {"id": t_a_wait["id"]}, req_id="a-wait", headers=hdr_a)
        assert a_wait["result"]["status"]["state"] == "input-required"


# ---------------------------------------------------------------------------
# 3. A2A Peer Config Security & Management APIs
# ---------------------------------------------------------------------------


def test_a2a_example_json_parses_and_matches_default_template() -> None:
    example_path = REPO_ROOT / "a2a.example.json"
    assert example_path.is_file()
    raw = json.loads(example_path.read_text(encoding="utf-8"))
    validated = validate_a2a_document(raw)
    assert validated == default_a2a_template()
    assert validated["schema_version"] == 1
    assert set(validated["peers"]) == {"local-reviewer", "remote-researcher"}


def test_a2a_url_and_document_security_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)

    # Cloud IMDS & link-local addresses rejected unconditionally (even with allow_private_network=True)
    for imds_url in (
        "http://169.254.169.254/latest/meta-data",
        "https://169.254.169.254",
        "http://[::ffff:169.254.169.254]/latest",
        "http://metadata.google.internal/computeMetadata/v1",
        "https://metadata.google.internal",
        "http://metadata.aws.internal",
    ):
        with pytest.raises(ValueError):
            validate_a2a_url(imds_url, allow_private_network=True)

    # URL embedded credentials rejected
    for cred_url in (
        "https://user:pass@agent.example.com",
        "https://token@agent.example.com/a2a",
    ):
        with pytest.raises(ValueError):
            validate_a2a_url(cred_url, allow_private_network=True)

    # Non-private plain http:// rejected
    with pytest.raises(ValueError):
        validate_a2a_url("http://agent.example.com", allow_private_network=False)
    with pytest.raises(ValueError):
        validate_a2a_url("http://agent.example.com", allow_private_network=True)

    # Loopback & private IPs rejected when allow_private_network=False
    for priv_url in (
        "http://127.0.0.1:8788",
        "https://127.0.0.1:8788",
        "http://localhost:8788",
        "http://[::1]:8788",
        "http://10.0.0.10:8788",
    ):
        with pytest.raises(ValueError):
            validate_a2a_url(priv_url, allow_private_network=False)

    # Valid URLs pass
    assert validate_a2a_url("https://agent.example.com/", allow_private_network=False) == (
        "https://agent.example.com"
    )
    assert validate_a2a_url("http://127.0.0.1:8788/", allow_private_network=True) == (
        "http://127.0.0.1:8788"
    )

    # CRLF header injection rejected in both header values and names
    for bad_headers in (
        {"X-Custom": "ok\r\nX-Injected: evil"},
        {"X-Bad\nName": "value"},
        {"X-Null": "val\x00ue"},
    ):
        with pytest.raises(ValueError):
            validate_a2a_document(
                {
                    "schema_version": 1,
                    "peers": {
                        "bad-peer": {
                            "name": "bad-peer",
                            "url": "https://agent.example.com",
                            "headers": bad_headers,
                        }
                    },
                }
            )


def test_a2a_config_file_0600_symlinks_hardlinks_and_redaction(tmp_path: Path) -> None:
    doc = default_a2a_template()
    cfg_path = tmp_path / "a2a.json"
    saved = save_a2a_config(cfg_path, doc)
    assert Path(saved["config_path"]) == cfg_path
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

    # Symlink target file rejected on save and refused on discovery/load
    symlink_file = tmp_path / "a2a-link.json"
    symlink_file.symlink_to(cfg_path)
    with pytest.raises(ValueError):
        save_a2a_config(symlink_file, doc)
    assert discover_a2a_config_path(explicit_path=symlink_file) is None
    loaded_sym = load_a2a_config(explicit_path=symlink_file)
    assert loaded_sym["config_path"] is None
    assert loaded_sym["peers"] == {}

    # Symlink parent directory rejected on save and refused on discovery/load
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    save_a2a_config(real_dir / "a2a.json", doc)
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError):
        save_a2a_config(link_dir / "a2a.json", doc)
    assert discover_a2a_config_path(explicit_path=link_dir / "a2a.json") is None

    # Hardlink (st_nlink > 1) rejected on load
    hard_src = tmp_path / "hard_a2a.json"
    save_a2a_config(hard_src, doc)
    hard_alias = tmp_path / "hard_a2a_alias.json"
    os.link(hard_src, hard_alias)
    with pytest.raises(ValueError):
        load_a2a_config(explicit_path=hard_alias)
    with pytest.raises(ValueError):
        load_a2a_config(explicit_path=hard_src)

    # Redaction masks inline api_key, sensitive headers, and Bearer values without mutating source
    secret_doc = {
        "schema_version": 1,
        "peers": {
            "secure-peer": {
                "name": "secure-peer",
                "url": "https://secure.example.com",
                "api_key": "sk-inline-peer-secret-111",
                "headers": {
                    "Authorization": "Bearer auth-secret-222",
                    "X-API-Key": "x-api-secret-333",
                    "X-Auth-Token": "tok-secret-444",
                    "X-Note": "Bearer embedded-bearer-555",
                    "X-Client-App": "agent-systems-lab",
                },
                "enabled": True,
            },
            "unconfigured-peer": {
                "name": "unconfigured-peer",
                "url": "https://other.example.com",
                "api_key_env": "MISSING_A2A_ENV_VAR",
                "headers": {},
                "enabled": True,
            },
        },
    }
    redacted = redact_a2a_document(secret_doc, environ={})
    assert secret_doc["peers"]["secure-peer"]["api_key"] == "sk-inline-peer-secret-111"

    sp = redacted["peers"]["secure-peer"]
    assert sp["api_key"] == A2A_REDACTED_MARKER
    assert sp["headers"]["Authorization"] == A2A_REDACTED_MARKER
    assert sp["headers"]["X-API-Key"] == A2A_REDACTED_MARKER
    assert sp["headers"]["X-Auth-Token"] == A2A_REDACTED_MARKER
    assert sp["headers"]["X-Note"] == f"Bearer {A2A_REDACTED_MARKER}"
    assert sp["headers"]["X-Client-App"] == "agent-systems-lab"
    assert sp["credential_configured"] is True
    assert redacted["peers"]["unconfigured-peer"]["credential_configured"] is False

    serialized = json.dumps(redacted)
    for leaked in (
        "sk-inline-peer-secret-111",
        "auth-secret-222",
        "x-api-secret-333",
        "tok-secret-444",
        "embedded-bearer-555",
    ):
        assert leaked not in serialized


def test_a2a_peers_management_api_rbac_secret_preservation_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    tokens_cfg = {
        "tok-op": {"tenant_id": "tenant_a", "actor_id": "op1", "roles": ["operator"]},
        "tok-tadmin": {
            "tenant_id": "tenant_a",
            "actor_id": "tadmin1",
            "roles": ["admin", "operator"],
            "global_admin": False,
        },
        "tok-gadmin": {
            "tenant_id": "local",
            "actor_id": "gadmin1",
            "roles": ["admin", "operator"],
            "global_admin": True,
        },
    }
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens_cfg))

    state_dir = tmp_path / "state"
    app = create_app(state_dir=state_dir, provider="demo")
    hdr_op = {"Authorization": "Bearer tok-op"}
    hdr_tadmin = {"Authorization": "Bearer tok-tadmin"}
    hdr_gadmin = {"Authorization": "Bearer tok-gadmin"}

    initial_doc = {
        "schema_version": 1,
        "peers": {
            "partner-agent": {
                "name": "partner-agent",
                "url": "https://partner.example.com",
                "api_key": "sk-live-inline-secret-abc",
                "headers": {
                    "X-API-Key": "hdr-live-secret-def",
                    "X-Debug-Header": "Bearer bearer-live-secret-ghi",
                    "X-Trace-Tag": "v1",
                },
                "timeout_seconds": 45.0,
                "allow_private_network": False,
                "enabled": True,
                "description": "Initial partner description",
            }
        },
    }

    with TestClient(app) as client:
        # Unauthenticated blocked
        assert client.get("/api/a2a/peers").status_code == 401
        assert client.put("/api/a2a/peers", json=initial_doc).status_code == 401

        # Operator & non-global admin cannot PUT /api/a2a/peers
        assert client.put("/api/a2a/peers", json=initial_doc, headers=hdr_op).status_code == 403
        assert client.put("/api/a2a/peers", json=initial_doc, headers=hdr_tadmin).status_code == 403

        # Global admin saves initial peers with inline secrets
        put1 = client.put("/api/a2a/peers", json=initial_doc, headers=hdr_gadmin)
        assert put1.status_code == 200, put1.text
        put1_json = put1.json()
        assert put1_json["ok"] is True
        assert put1_json["config_path"] == str(state_dir / "a2a.json")
        assert stat.S_IMODE((state_dir / "a2a.json").stat().st_mode) == 0o600

        # Non-admin GET hides config_path and redacts secrets
        get_op = client.get("/api/a2a/peers", headers=hdr_op)
        assert get_op.status_code == 200
        assert get_op.json()["config_path"] is None
        assert "config_path" not in get_op.json()["config"]

        # Admin GET returns redacted config with ***REDACTED*** placeholders
        get_admin = client.get("/api/a2a/peers", headers=hdr_gadmin)
        assert get_admin.status_code == 200
        redacted_cfg = get_admin.json()["config"]
        peer_view = redacted_cfg["peers"]["partner-agent"]
        assert peer_view["api_key"] == A2A_REDACTED_MARKER
        assert peer_view["headers"]["X-API-Key"] == A2A_REDACTED_MARKER
        assert peer_view["headers"]["X-Debug-Header"] == f"Bearer {A2A_REDACTED_MARKER}"
        assert peer_view["headers"]["X-Trace-Tag"] == "v1"
        for secret_val in ("sk-live-inline-secret-abc", "hdr-live-secret-def", "bearer-live-secret-ghi"):
            assert secret_val not in json.dumps(put1_json)
            assert secret_val not in json.dumps(redacted_cfg)

        # Second PUT submits redacted payload back with edited metadata -> preserves plaintext secrets on disk!
        edited_doc = json.loads(json.dumps(redacted_cfg))
        edited_doc["peers"]["partner-agent"]["description"] = "Updated partner description"
        edited_doc["peers"]["partner-agent"]["timeout_seconds"] = 75.0
        put2 = client.put("/api/a2a/peers", json=edited_doc, headers=hdr_gadmin)
        assert put2.status_code == 200, put2.text

        on_disk = load_a2a_config(explicit_path=state_dir / "a2a.json")
        disk_peer = on_disk["peers"]["partner-agent"]
        assert disk_peer["description"] == "Updated partner description"
        assert disk_peer["timeout_seconds"] == 75.0
        assert disk_peer["api_key"] == "sk-live-inline-secret-abc"
        assert disk_peer["headers"]["X-API-Key"] == "hdr-live-secret-def"
        assert disk_peer["headers"]["X-Debug-Header"] == "Bearer bearer-live-secret-ghi"
        assert disk_peer["headers"]["X-Trace-Tag"] == "v1"

        # Audit log records updates without leaking secrets
        audits = app.state.storage.list_audit_logs(action="a2a_peers_config_update")
        assert len(audits) == 2
        assert all(a["outcome"] == "allowed" and a["resource"] == "/api/a2a/peers" for a in audits)
        audit_blob = json.dumps(audits)
        for secret_val in ("sk-live-inline-secret-abc", "hdr-live-secret-def", "bearer-live-secret-ghi"):
            assert secret_val not in audit_blob

        # Hardlink at target_path rejected by PUT /api/a2a/peers
        hard_alias = tmp_path / "hardlink_a2a.json"
        os.link(state_dir / "a2a.json", hard_alias)
        try:
            put_hard = client.put("/api/a2a/peers", json=edited_doc, headers=hdr_gadmin)
            assert put_hard.status_code == 400
        finally:
            hard_alias.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Live Bidirectional Agent-to-Agent Delegation (Agent A -> Agent B over ASGITransport)
# ---------------------------------------------------------------------------


async def test_live_agent_a_to_agent_b_delegation_and_3tier_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    agent_b_token = "tok-agent-b-bridge-42"
    monkeypatch.setenv("AGENT_LAB_TOKEN", agent_b_token)
    monkeypatch.setenv("A2A_AGENT_B_TOKEN", agent_b_token)

    agent_b_received: list[str] = []

    class AgentBProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            user_texts = _extract_user_prompts(messages)
            prompt = user_texts[-1] if user_texts else ""
            agent_b_received.append(prompt)
            return _resp([{"type": "text", "text": f"[Agent B Result #{len(agent_b_received)}] {prompt}"}])

    app_b = create_app(state_dir=tmp_path / "agent_b_state", provider=AgentBProvider())

    peers_doc = {
        "schema_version": 1,
        "peers": {
            "agent-b": {
                "name": "agent-b",
                "url": "http://127.0.0.1:8788",
                "api_key_env": "A2A_AGENT_B_TOKEN",
                "allow_private_network": True,
                "enabled": True,
                "description": "Live in-process Agent B specialist",
            }
        },
    }

    # Verify 3-tier permission matrix & rule precedence for A2A tools directly
    for ro_tool in ("a2a_list_agents", "a2a_discover_agent", "a2a_get_task"):
        for mode in ("ask", "auto-edit", "full-auto"):
            v = evaluate_tool_permission(ro_tool, {"peer": "agent-b"}, permission_mode=mode)
            assert v["decision"] == "allow"
    assert (
        evaluate_tool_permission("a2a_send_task", {"peer": "agent-b"}, permission_mode="ask")["decision"]
        == "ask"
    )
    assert (
        evaluate_tool_permission("a2a_send_task", {"peer": "agent-b"}, permission_mode="auto-edit")[
            "decision"
        ]
        == "ask"
    )
    assert (
        evaluate_tool_permission("a2a_send_task", {"peer": "agent-b"}, permission_mode="full-auto")[
            "decision"
        ]
        == "allow"
    )
    deny_engine = PermissionRuleEngine(deny=["A2A(agent-b)"])
    assert (
        evaluate_tool_permission(
            "a2a_send_task",
            {"peer": "agent-b"},
            permission_mode="full-auto",
            rule_engine=deny_engine,
        )["decision"]
        == "deny"
    )
    assert suggest_session_grant_pattern("a2a_send_task", {"peer": "agent-b"}) == "A2A(agent-b)"

    async with app_b.router.lifespan_context(app_b):
        transport_b = httpx.ASGITransport(app=app_b)
        mgr = A2AClientManager(peers_doc, transport=transport_b)
        try:
            # 4a) Direct A2AClientManager -> Agent B discovery, send_message, and get_task
            disc = await mgr.discover_agent_card("agent-b")
            assert disc["ok"] is True
            assert disc["card"]["name"] == "Agent Systems Lab"
            assert disc["card"]["url"] == "http://127.0.0.1:8788/a2a"
            assert disc["card"]["capabilities"]["streaming"] is True

            sent = await mgr.send_message("agent-b", "Direct ping from Agent A", blocking=True)
            assert sent["ok"] is True
            assert sent["state"] == "completed"
            assert sent["task_id"]
            assert "[Agent B Result #1] Direct ping from Agent A" in sent["answer"]

            fetched = await mgr.get_task("agent-b", sent["task_id"])
            assert fetched["ok"] is True
            assert fetched["task_id"] == sent["task_id"]
            assert fetched["state"] == "completed"
            assert "[Agent B Result #1] Direct ping from Agent A" in fetched["answer"]

            # 4b) AgentRuntime A -> Agent B with ask mode -> native a2a_call proposal -> session grant -> auto-allow
            observed_tool_results: list[dict[str, Any]] = []

            class AgentAOrchestratorProvider:
                def __init__(self) -> None:
                    self.step = 0

                async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
                    self.step += 1
                    for m in messages:
                        if m.get("role") == "user" and isinstance(m.get("content"), list):
                            for blk in m["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                    try:
                                        parsed = json.loads(str(blk.get("content", "{}")))
                                        if isinstance(parsed, dict) and parsed not in observed_tool_results:
                                            observed_tool_results.append(parsed)
                                    except ValueError:
                                        pass
                    if self.step == 1:
                        return _resp(
                            [
                                {"type": "tool_use", "id": "c_list", "name": "a2a_list_agents", "input": {}},
                                {
                                    "type": "tool_use",
                                    "id": "c_disc",
                                    "name": "a2a_discover_agent",
                                    "input": {"peer": "agent-b"},
                                },
                            ]
                        )
                    if self.step == 2:
                        return _resp(
                            [
                                {
                                    "type": "tool_use",
                                    "id": "c_send_1",
                                    "name": "a2a_send_task",
                                    "input": {"peer": "agent-b", "prompt": "Delegated review #1"},
                                }
                            ]
                        )
                    if self.step == 3:
                        # Immediately after approval + session_grant A2A(agent-b), issue a 2nd send_task
                        return _resp(
                            [
                                {
                                    "type": "tool_use",
                                    "id": "c_send_2",
                                    "name": "a2a_send_task",
                                    "input": {"peer": "agent-b", "prompt": "Delegated review #2"},
                                }
                            ]
                        )
                    return _resp([{"type": "text", "text": "Agent A completed both delegated tasks."}])

            _, storage_a, runtime_a = _make_runtime(
                tmp_path / "agent_a",
                AgentAOrchestratorProvider(),
                execution_profile="host",
                permission_mode="ask",
                a2a_manager=mgr,
            )
            run_a = runtime_a.create("Coordinate with Agent B")
            try:
                paused_run = await runtime_a.execute(run_a["id"])
                assert paused_run["status"] == "waiting_approval"

                # Verify readonly tools executed without approval in ask mode
                assert any(
                    "peers" in r and any(p["name"] == "agent-b" for p in r["peers"])
                    for r in observed_tool_results
                )
                assert any("card" in r and r.get("peer") == "agent-b" for r in observed_tool_results)

                # Verify native kind="a2a_call" proposal was created with A2A(agent-b) grant suggestion
                props = storage_a.proposals(run_a["id"])
                assert len(props) == 1
                prop = props[0]
                assert prop["kind"] == "a2a_call"
                assert prop["tool"] == "a2a_send_task"
                assert prop["status"] == "pending"
                persisted_tool_results: list[dict[str, Any]] = []
                for m in paused_run.get("messages") or []:
                    if m.get("role") == "user" and isinstance(m.get("content"), list):
                        for blk in m["content"]:
                            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                try:
                                    parsed = json.loads(str(blk.get("content", "{}")))
                                    if isinstance(parsed, dict):
                                        persisted_tool_results.append(parsed)
                                except ValueError:
                                    pass
                assert any(
                    r.get("requires_approval")
                    and r.get("kind") == "a2a_call"
                    and r.get("suggested_grant_pattern") == "A2A(agent-b)"
                    for r in persisted_tool_results
                )

                # Approve with session_grant A2A(agent-b) -> task #1 runs AND task #2 auto-allows!
                final_a = await runtime_a.approve(
                    run_a["id"],
                    prop["id"],
                    approved=True,
                    grant_pattern="A2A(agent-b)",
                )
                assert final_a["status"] == "completed"
                assert "A2A(agent-b)" in (final_a.get("metadata") or {}).get("session_grants", [])
                assert len(storage_a.proposals(run_a["id"])) == 1
                assert "Delegated review #1" in agent_b_received
                assert "Delegated review #2" in agent_b_received
                events_a = storage_a.events(run_a["id"])
                delegated_events = [e for e in events_a if e["kind"] == "a2a_task_delegated"]
                assert len(delegated_events) == 2
                assert all(
                    e["payload"]["peer"] == "agent-b" and e["payload"]["ok"] is True
                    for e in delegated_events
                )
            finally:
                await runtime_a.aclose()

            # 4c) Explicit deny rule overrides full-auto!
            before_deny_calls = len(agent_b_received)
            deny_results: list[dict[str, Any]] = []

            class DeniedSendProvider:
                def __init__(self) -> None:
                    self.called = False

                async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
                    if not self.called:
                        self.called = True
                        return _resp(
                            [
                                {
                                    "type": "tool_use",
                                    "id": "c_deny_1",
                                    "name": "a2a_send_task",
                                    "input": {"peer": "agent-b", "prompt": "Should never reach Agent B"},
                                }
                            ]
                        )
                    for m in messages:
                        if m.get("role") == "user" and isinstance(m.get("content"), list):
                            for blk in m["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                    deny_results.append(json.loads(str(blk["content"])))
                    return _resp([{"type": "text", "text": "Delegation blocked by deny policy."}])

            _, _, runtime_denied = _make_runtime(
                tmp_path / "agent_a_denied",
                DeniedSendProvider(),
                execution_profile="host",
                permission_mode="full-auto",
                a2a_manager=mgr,
                rule_engine=PermissionRuleEngine(deny=["A2A(agent-b)"]),
            )
            run_denied = runtime_denied.create("Attempt denied delegation")
            try:
                settled_denied = await runtime_denied.execute(run_denied["id"])
            finally:
                await runtime_denied.aclose()
            assert settled_denied["status"] == "completed"
            assert any(r.get("ok") is False and r.get("code") == "permission_denied" for r in deny_results)
            assert len(agent_b_received) == before_deny_calls
        finally:
            await mgr.aclose()


async def test_zero_a2a_tools_and_zero_pinned_bytes_when_no_peers_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    class CaptureProvider:
        def __init__(self) -> None:
            self.captured: list[tuple[str, str, str]] = []

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            prefix_msgs = [
                m
                for m in messages
                if not (
                    m.get("role") == "user"
                    and isinstance(m.get("content"), str)
                    and m["content"].startswith("HARNESS_STATE\n")
                )
            ]
            self.captured.append((system, canonical_json(tools), canonical_json(prefix_msgs)))
            return _resp([{"type": "text", "text": "ok"}])

    shared_ws = tmp_path / "shared_ws"
    shared_ws.mkdir()
    (shared_ws / "hello.py").write_text("x = 1\n", encoding="utf-8")

    for profile in ("sandbox", "host"):
        prov_default = CaptureProvider()
        prov_empty_mgr = CaptureProvider()

        _, _, rt_default = _make_runtime(
            tmp_path / f"{profile}_default",
            prov_default,
            execution_profile=profile,
            permission_mode="ask",
            a2a_manager=None,
            ws=shared_ws,
        )
        _, _, rt_empty_mgr = _make_runtime(
            tmp_path / f"{profile}_empty_mgr",
            prov_empty_mgr,
            execution_profile=profile,
            permission_mode="ask",
            a2a_manager=A2AClientManager(),
            ws=shared_ws,
        )

        assert rt_default.a2a_manager.enabled is False
        assert rt_empty_mgr.a2a_manager.enabled is False

        r1 = rt_default.create("Simple inspection prompt")
        r2 = rt_empty_mgr.create("Simple inspection prompt")
        assert not any(
            t["name"].startswith("a2a_") for t in rt_default._all_tools(r1, rt_default._registry(r1))
        )
        assert "pinned_a2a_directory" not in (r1.get("metadata") or {})
        pinned_1 = rt_default._assemble_pinned_for_worker(r1, "agent", allow_extras=True)
        pinned_2 = rt_empty_mgr._assemble_pinned_for_worker(r2, "agent", allow_extras=True)
        assert pinned_1 == pinned_2
        assert "[A2A_PEER_DIRECTORY]" not in canonical_json(pinned_1)

        try:
            await rt_default.execute(r1["id"])
            await rt_empty_mgr.execute(r2["id"])
        finally:
            await rt_default.aclose()
            await rt_empty_mgr.aclose()

        assert len(prov_default.captured) == 1
        assert len(prov_empty_mgr.captured) == 1
        sys_1, tools_1, msgs_1 = prov_default.captured[0]
        sys_2, tools_2, msgs_2 = prov_empty_mgr.captured[0]

        assert "a2a_" not in tools_1
        assert "[A2A_PEER_DIRECTORY]" not in msgs_1
        assert "pinned_a2a_directory" not in (rt_default.storage.get_run(r1["id"]) or {}).get("metadata", {})
        # Byte-for-byte identical KV-cache prefix (system, tools, messages)
        assert sys_1 == sys_2
        assert tools_1 == tools_2
        assert msgs_1 == msgs_2


# ---------------------------------------------------------------------------
# 5. CLI, REPL & Web UI A2A Checks
# ---------------------------------------------------------------------------


def test_cli_a2a_init_list_and_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENT_LAB_NO_DOTENV", "1")
    state_dir = tmp_path / "cli_state"
    state_dir.mkdir()
    a2a_path = state_dir / "a2a.json"

    # 1) `agent-lab a2a init` creates 0600 file matching default_a2a_template()
    monkeypatch.setattr(sys, "argv", ["agent-lab", "--state", str(state_dir), "a2a", "init"])
    cli_mod.main()
    out_init = capsys.readouterr().out
    assert "0600" in out_init
    assert a2a_path.is_file()
    assert stat.S_IMODE(a2a_path.stat().st_mode) == 0o600
    assert json.loads(a2a_path.read_text(encoding="utf-8")) == default_a2a_template()

    # 2) Without --force, refuses to overwrite existing a2a.json
    custom_doc = default_a2a_template()
    custom_doc["peers"]["local-reviewer"]["description"] = "KEEP_CUSTOM_PEER"
    custom_doc["peers"]["local-reviewer"]["api_key"] = "sk-cli-inline-secret-555"
    save_a2a_config(a2a_path, custom_doc)

    monkeypatch.setattr(sys, "argv", ["agent-lab", "--state", str(state_dir), "a2a", "init"])
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()
    assert exc_info.value.code == 1
    assert "--force" in capsys.readouterr().err
    assert load_a2a_config(explicit_path=a2a_path)["peers"]["local-reviewer"]["description"] == (
        "KEEP_CUSTOM_PEER"
    )

    # 3) `agent-lab a2a list --json` prints structured redacted JSON (zero secrets)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-lab", "--state", str(state_dir), "a2a", "list", "--json"],
    )
    cli_mod.main()
    list_raw = capsys.readouterr().out
    assert "sk-cli-inline-secret-555" not in list_raw
    list_json = json.loads(list_raw)
    assert list_json["ok"] is True
    assert list_json["peer_count"] == 2
    assert list_json["enabled_count"] == 1
    assert list_json["peers"]["local-reviewer"]["api_key"] == A2A_REDACTED_MARKER
    assert "credential_status" in list_json["peers"]["local-reviewer"]

    # 4) `agent-lab a2a init --force` overwrites cleanly with 0600
    monkeypatch.setattr(sys, "argv", ["agent-lab", "--state", str(state_dir), "a2a", "init", "--force"])
    cli_mod.main()
    capsys.readouterr()
    assert stat.S_IMODE(a2a_path.stat().st_mode) == 0o600
    assert json.loads(a2a_path.read_text(encoding="utf-8")) == default_a2a_template()

    # 5) `agent-lab a2a card` outputs valid self AgentCard JSON (and extended metadata with --extended)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-lab", "--state", str(state_dir), "a2a", "card", "--base-url", "http://127.0.0.1:9000"],
    )
    cli_mod.main()
    card_json = json.loads(capsys.readouterr().out)
    assert card_json["name"] == "Agent Systems Lab"
    assert card_json["url"] == "http://127.0.0.1:9000/a2a"
    assert card_json["protocolVersion"] == A2A_PROTOCOL_VERSION
    assert card_json["capabilities"]["streaming"] is True
    assert len(card_json["skills"]) >= 2
    assert card_json["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"

    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-lab", "--state", str(state_dir), "a2a", "card", "--extended"],
    )
    cli_mod.main()
    ext_card_json = json.loads(capsys.readouterr().out)
    assert "metadata" in ext_card_json
    assert "SendMessage" in ext_card_json["metadata"]["supportedRpcMethods"]


async def test_repl_a2a_list_and_discover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    app_b = create_app(state_dir=tmp_path / "repl_agent_b_state", provider="demo")

    repl_state_dir = tmp_path / "repl_agent_a"
    repl_state_dir.mkdir()
    peers_doc = {
        "schema_version": 1,
        "peers": {
            "agent-b": {
                "name": "agent-b",
                "url": "http://127.0.0.1:8788",
                "api_key": "sk-repl-inline-secret-321",
                "allow_private_network": True,
                "enabled": True,
                "description": "REPL peer specialist",
            }
        },
    }
    a2a_cfg_file = repl_state_dir / "a2a.json"
    save_a2a_config(a2a_cfg_file, peers_doc)

    class FailIfCalledProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            raise AssertionError("Slash /a2a commands must not invoke the model provider")

    async with app_b.router.lifespan_context(app_b):
        mgr = A2AClientManager(peers_doc, transport=httpx.ASGITransport(app=app_b))
        _, _, runtime = _make_runtime(
            repl_state_dir,
            FailIfCalledProvider(),
            execution_profile="host",
            permission_mode="auto-edit",
            a2a_manager=mgr,
        )
        runtime.a2a_config_path = a2a_cfg_file

        scripted = iter(["/a2a list", "/a2a discover agent-b", "/exit"])
        outputs: list[str] = []
        try:
            await run_interactive_repl(
                runtime,
                input_fn=lambda _prompt: next(scripted),
                output_fn=outputs.append,
            )
        finally:
            await runtime.aclose()
            await mgr.aclose()

    joined = "\n".join(outputs)
    assert "sk-repl-inline-secret-321" not in joined
    assert "agent-b" in joined
    assert "http://127.0.0.1:8788" in joined
    assert A2A_REDACTED_MARKER in joined
    assert "[A2A AgentCard]" in joined
    assert "Agent Systems Lab" in joined
    assert "streaming=True" in joined
    assert "code-engineering" in joined


def test_web_ui_a2a_elements_and_a2a_call_handling() -> None:
    html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (REPO_ROOT / "web" / "app.js").read_text(encoding="utf-8")

    for elem_id in (
        "a2a-card",
        "a2a-self-card-link",
        "a2a-peer-select",
        "a2a-discover-btn",
        "a2a-json-editor",
        "a2a-save-btn",
    ):
        assert f'id="{elem_id}"' in html, f"Missing #{elem_id} in web/index.html"

    assert 'href="/.well-known/agent-card.json"' in html

    for js_token in (
        '"a2a-card"',
        '"a2a-peer-select"',
        '"a2a-discover-btn"',
        '"a2a-json-editor"',
        '"a2a-save-btn"',
        "/api/a2a/peers",
        "/api/a2a/discover",
        'kind === "a2a_call"',
    ):
        assert js_token in js, f"Missing {js_token} in web/app.js"

"""Tests for RunStreamBroker, provider streaming, OpenAI reasoning/truncation, pricing/budgets, cancellation, and SSE/cookie auth APIs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_lab.api import SESSION_COOKIE_NAME, create_app
from agent_lab.providers import (
    DemoProvider,
    OpenAICompatibleProvider,
    call_provider_complete,
    compute_cost_usd,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage
from agent_lab.streaming import RunStreamBroker


def _make_runtime_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Storage]:
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / "state.db")
    return ws, kb, sk, storage


def _parse_sse_frames(raw_text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in raw_text.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        frame: dict[str, Any] = {}
        data_lines: list[str] = []
        for ln in lines:
            if ln.startswith("id:"):
                frame["id"] = ln[3:].strip()
            elif ln.startswith("event:"):
                frame["event"] = ln[6:].strip()
            elif ln.startswith("data:"):
                data_lines.append(ln[5:].strip())
        if data_lines:
            joined = "\n".join(data_lines)
            try:
                frame["data"] = json.loads(joined)
            except json.JSONDecodeError:
                frame["data"] = joined
        frames.append(frame)
    return frames


async def test_run_stream_broker_threadsafe_publish_replay_ring_buffer_and_close() -> None:
    broker = RunStreamBroker(max_buffer=4, max_runs=8)
    run_id = "run_stream_01"

    # Publish 6 events synchronously before subscribing to verify bounded ring buffer (maxlen=4)
    published: list[dict[str, Any]] = []
    for i in range(6):
        published.append(broker.publish(run_id, "text_delta", {"delta": f"pre-{i}"}))

    assert [ev["seq"] for ev in published] == [1, 2, 3, 4, 5, 6]
    buffered = broker.recent_events(run_id)
    assert [ev["seq"] for ev in buffered] == [3, 4, 5, 6]
    assert [ev["seq"] for ev in broker.recent_events(run_id, after_seq=4)] == [5, 6]

    # Subscribe from after_seq=4 while a background thread publishes live events and closes the run
    received_a: list[dict[str, Any]] = []
    received_b: list[dict[str, Any]] = []
    sub_ready = asyncio.Event()

    async def _consume(target: list[dict[str, Any]], after_seq: int, notify_ready: bool = False) -> None:
        agen = broker.subscribe(run_id, after_seq=after_seq)
        first = True
        async for item in agen:
            target.append(item)
            if first and notify_ready:
                first = False
                sub_ready.set()

    task_a = asyncio.create_task(_consume(received_a, after_seq=4, notify_ready=True))
    task_b = asyncio.create_task(_consume(received_b, after_seq=5))
    await asyncio.wait_for(sub_ready.wait(), timeout=2.0)

    # Wait until both subscribers are registered before publishing from a worker thread
    for _ in range(50):
        if broker.subscriber_count(run_id) == 2:
            break
        await asyncio.sleep(0.01)
    assert broker.subscriber_count(run_id) == 2

    def _worker_thread() -> None:
        broker.publish(run_id, "thinking_delta", {"delta": "step-think"})
        broker.publish(run_id, "bash_output_delta", {"delta": "line-1\n", "stream": "stdout"})
        broker.close_run(run_id)

    t = threading.Thread(target=_worker_thread, daemon=True)
    t.start()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)
    t.join(timeout=1.0)

    assert [ev["seq"] for ev in received_a] == [5, 6, 7, 8]
    assert [ev["seq"] for ev in received_b] == [6, 7, 8]
    assert received_a[2]["type"] == "thinking_delta"
    assert received_a[3]["type"] == "bash_output_delta"
    assert broker.subscriber_count(run_id) == 0

    # Publishing again after close_run automatically reopens the channel with monotonic seq
    reopened = broker.publish(run_id, "run_status", {"status": "running"})
    assert reopened["seq"] == 9


async def test_provider_streaming_demo_and_mock_via_call_provider_complete() -> None:
    # 1. DemoProvider.stream() yields tool_use_start + input_json_delta for tool turns and text_delta for text turns
    demo = DemoProvider()
    tool_events: list[dict[str, Any]] = []
    async for ev in demo.stream("system", [{"role": "user", "content": "请修复 pricing.py 的折扣计算 bug"}], []):
        tool_events.append(ev)

    tool_types = [ev["type"] for ev in tool_events]
    assert "tool_use_start" in tool_types
    assert "input_json_delta" in tool_types
    assert tool_types[-1] == "message_stop"

    text_events: list[dict[str, Any]] = []
    async for ev in demo.stream("system", [{"role": "user", "content": "Describe system boundaries"}], []):
        text_events.append(ev)

    text_types = [ev["type"] for ev in text_events]
    assert "text_delta" in text_types
    assert text_types[-1] == "message_stop"

    # 2. Mock streaming provider accepting on_event directly through call_provider_complete
    class StreamingMockProvider:
        async def complete(
            self,
            system: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            max_tokens: int = 2048,
            *,
            on_event: Any = None,
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            if on_event is not None:
                await on_event({"type": "thinking_delta", "thinking": "plan ", "delta": "plan ", "index": 0})
                await on_event({"type": "text_delta", "text": "reading file", "delta": "reading file", "index": 1})
                await on_event({"type": "tool_use_start", "id": "call_1", "name": "fs_read", "index": 2})
                await on_event(
                    {
                        "type": "input_json_delta",
                        "partial_json": '{"path":"a.py"}',
                        "delta": '{"path":"a.py"}',
                        "id": "call_1",
                        "name": "fs_read",
                        "index": 2,
                    }
                )
            return {
                "content": [
                    {"type": "thinking", "thinking": "plan "},
                    {"type": "text", "text": "reading file"},
                    {"type": "tool_use", "id": "call_1", "name": "fs_read", "input": {"path": "a.py"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }

    stream_events: list[dict[str, Any]] = []

    async def _collect(ev: dict[str, Any]) -> None:
        stream_events.append(ev)

    resp = await call_provider_complete(
        StreamingMockProvider(),
        "sys",
        [{"role": "user", "content": "hi"}],
        [],
        on_event=_collect,
    )
    assert resp["stop_reason"] == "tool_use"
    assert [e["type"] for e in stream_events] == [
        "thinking_delta",
        "text_delta",
        "tool_use_start",
        "input_json_delta",
    ]

    # 3. Legacy mock provider without on_event still receives synthesized stream events
    class LegacyMockProvider:
        async def complete(
            self,
            system: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            max_tokens: int = 2048,
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            return {
                "content": [
                    {"type": "thinking", "thinking": "check inputs"},
                    {"type": "text", "text": "calling tool"},
                    {"type": "tool_use", "id": "call_legacy", "name": "repo_read", "input": {"path": "x.py"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 5},
            }

    legacy_events: list[dict[str, Any]] = []
    await call_provider_complete(
        LegacyMockProvider(),
        "sys",
        [{"role": "user", "content": "hi"}],
        [],
        on_event=legacy_events.append,
    )
    assert [e["type"] for e in legacy_events] == [
        "thinking_delta",
        "text_delta",
        "tool_use_start",
        "input_json_delta",
        "message_stop",
    ]


async def test_openai_compatible_reasoning_content_tokens_and_truncated_tool_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-offline-test")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-reasoner")
    monkeypatch.delenv("OPENAI_STREAM", raising=False)

    tools = [
        {
            "name": "fs_write",
            "description": "write file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        }
    ]

    # Case 1: Streaming SSE with reasoning_content, text, tool_calls, and reasoning_tokens
    sse_chunks = [
        {
            "choices": [
                {"index": 0, "delta": {"reasoning_content": "First analyze "}, "finish_reason": None}
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"reasoning_content": "the file."}, "finish_reason": None}
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"content": "Applying fix."}, "finish_reason": None}
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_ok_1",
                                "type": "function",
                                "function": {"name": "fs_write", "arguments": '{"path":"a.py",'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"content":"print(1)\\n"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 180,
                "prompt_tokens_details": {"cached_tokens": 300},
                "completion_tokens_details": {"reasoning_tokens": 120},
            },
        },
    ]
    sse_body = (
        "".join(f"data: {json.dumps(c)}\n\n" for c in sse_chunks) + "data: [DONE]\n\n"
    ).encode("utf-8")

    # Case 2: Non-streaming JSON with truncated tool-call arguments (finish_reason="length")
    truncated_payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "reasoning_content": "Writing large file...",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_trunc_1",
                            "type": "function",
                            "function": {
                                "name": "fs_write",
                                "arguments": '{"path": "big.py", "content": "unterminated',
                            },
                        },
                        {
                            "id": "call_trunc_empty",
                            "type": "function",
                            "function": {
                                "name": "fs_write",
                                "arguments": "",
                            },
                        },
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 64,
            "completion_tokens_details": {"reasoning_tokens": 40},
        },
    }

    call_idx = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=truncated_payload,
        )

    provider = OpenAICompatibleProvider(max_retries=0)
    await provider.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://mock-openai.local/v1",
        transport=httpx.MockTransport(_handler),
    )
    try:
        # Streaming call
        emitted: list[dict[str, Any]] = []
        res_stream = await provider.complete(
            "system",
            [{"role": "user", "content": "fix a.py"}],
            tools,
            on_event=emitted.append,
        )
        assert res_stream["content"][0] == {"type": "thinking", "thinking": "First analyze the file."}
        assert res_stream["content"][1] == {"type": "text", "text": "Applying fix."}
        assert res_stream["content"][2]["type"] == "tool_use"
        assert res_stream["content"][2]["input"] == {"path": "a.py", "content": "print(1)\n"}
        assert res_stream["usage"]["reasoning_tokens"] == 120
        assert res_stream["usage"]["cache_read_input_tokens"] == 300
        assert any(e["type"] == "thinking_delta" for e in emitted)
        assert any(e["type"] == "text_delta" for e in emitted)
        assert any(e["type"] == "tool_use_start" for e in emitted)
        assert any(e["type"] == "input_json_delta" for e in emitted)

        # Truncated tool-call JSON detection (must NOT silently produce {})
        res_trunc = await provider.complete(
            "system",
            [{"role": "user", "content": "write big.py"}],
            tools,
        )
        assert res_trunc["truncated"] is True
        assert res_trunc["usage"]["reasoning_tokens"] == 40
        assert res_trunc["content"][0] == {"type": "thinking", "thinking": "Writing large file..."}
        tool_blocks = [b for b in res_trunc["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 2
        for tb in tool_blocks:
            assert tb["input"] != {}
            assert "__truncated_or_invalid_json__" in tb["input"]
        assert tool_blocks[0]["input"]["__tool_input_error__"] == "truncated_json"
        assert "__json_error__" in tool_blocks[0]["input"]
        assert tool_blocks[1]["input"]["__tool_input_error__"] == "truncated_at_max_tokens"
    finally:
        await provider.aclose()


def test_model_aware_usd_pricing_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_LAB_MODEL_PRICING_JSON", raising=False)

    # Anthropic model with provider prefix & context suffix normalization (additive cache)
    claude_cost = compute_cost_usd(
        "us.anthropic.claude-sonnet-4-6[1m]",
        {
            "input_tokens": 100_000,
            "cache_creation_input_tokens": 200_000,
            "cache_read_input_tokens": 1_000_000,
            "output_tokens": 50_000,
        },
    )
    assert claude_cost["pricing_known"] is True
    assert claude_cost["input_cost_usd"] == pytest.approx(0.30)
    assert claude_cost["cache_write_cost_usd"] == pytest.approx(0.75)
    assert claude_cost["cache_read_cost_usd"] == pytest.approx(0.30)
    assert claude_cost["output_cost_usd"] == pytest.approx(0.75)
    assert claude_cost["total_cost_usd"] == pytest.approx(2.10)

    # OpenAI model (non-additive cache: input_tokens already includes cached_tokens)
    gpt_cost = compute_cost_usd(
        "gpt-4o",
        {"input_tokens": 1_000_000, "cache_read_input_tokens": 800_000, "output_tokens": 100_000},
    )
    assert gpt_cost["pricing_known"] is True
    # 200k uncached * $2.50/M = $0.50; 800k cached * $1.25/M = $1.00; 100k out * $10/M = $1.00
    assert gpt_cost["input_cost_usd"] == pytest.approx(0.50)
    assert gpt_cost["cache_read_cost_usd"] == pytest.approx(1.00)
    assert gpt_cost["output_cost_usd"] == pytest.approx(1.00)
    assert gpt_cost["total_cost_usd"] == pytest.approx(2.50)

    # Unknown model reports pricing_known=False
    unknown = compute_cost_usd("my-private-unlisted-model", {"input_tokens": 500_000, "output_tokens": 1000})
    assert unknown["pricing_known"] is False
    assert unknown["total_cost_usd"] == 0.0

    # AGENT_LAB_MODEL_PRICING_JSON override adds custom private model and overrides existing pricing
    monkeypatch.setenv(
        "AGENT_LAB_MODEL_PRICING_JSON",
        json.dumps(
            {
                "my-private-unlisted-model": {
                    "input": 4.0,
                    "output": 16.0,
                    "cache_write": 0.0,
                    "cache_read": 0.5,
                },
                "gpt-4o": {"input": 1.0, "output": 2.0, "cache_read": 0.1},
            }
        ),
    )
    custom = compute_cost_usd(
        "my-private-unlisted-model",
        {"input_tokens": 500_000, "cache_read_input_tokens": 250_000, "output_tokens": 100_000},
    )
    assert custom["pricing_known"] is True
    # 250k * $4/M = $1.00; 250k * $0.5/M = $0.125; 100k * $16/M = $1.60 -> $2.725
    assert custom["total_cost_usd"] == pytest.approx(2.725)

    overridden_gpt = compute_cost_usd("gpt-4o", {"input_tokens": 1_000_000, "output_tokens": 500_000})
    assert overridden_gpt["total_cost_usd"] == pytest.approx(2.0)


async def test_kv_cache_billed_token_budget_and_max_cost_usd_hard_cap(tmp_path: Path) -> None:
    ws, kb, sk, storage = _make_runtime_dirs(tmp_path)
    (ws / "note.txt").write_text("hello world\n", encoding="utf-8")

    # 1. KV-cache billed token budget: raw input tokens far exceed max_tokens,
    #    but billed tokens (after cache-read discount) stay below max_tokens -> completes cleanly!
    class CachedMultiStepProvider:
        cache_usage_additive = False

        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.calls += 1
            # 10,000 raw input (9,600 cached + 400 uncached) -> billed_input = 400 + 2400 = 2800
            usage = {
                "input_tokens": 10_000,
                "cache_read_input_tokens": 9_600,
                "output_tokens": 100,
            }
            if self.calls < 4:
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"cache_call_{self.calls}",
                            "name": "repo_read",
                            "input": {"path": "note.txt"},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": usage,
                }
            return {
                "content": [{"type": "text", "text": "All cached turns completed."}],
                "stop_reason": "end_turn",
                "usage": usage,
            }

    cached_provider = CachedMultiStepProvider()
    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=cached_provider,
        max_steps=8,
        max_tokens=15_000,
        context_chars=24_000,
        auto_dream=False,
    )
    try:
        res = await rt.start("Read note.txt across cached turns")
        assert res["status"] == "completed"
        assert cached_provider.calls == 4
        # Raw tokens = 40,400 >> 15,000, while billed tokens = 4 * 2,900 = 11,600 <= 15,000
        assert res["input_tokens"] + res["output_tokens"] == 40_400
        assert res["metadata"]["billed_tokens"] == 11_600
        assert res["metadata"]["billed_tokens"] <= 15_000
    finally:
        await rt.aclose()

    # 2. max_cost_usd hard cap stops execution as soon as USD cost exceeds cap
    class ExpensiveProvider:
        cache_usage_additive = True
        model = "claude-sonnet-4-6"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.calls += 1
            # 1,000 input ($0.003) + 1,000 output ($0.015) = $0.018 > $0.005 cap
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"exp_call_{self.calls}",
                        "name": "repo_read",
                        "input": {"path": "note.txt"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1_000, "output_tokens": 1_000},
            }

    exp_provider = ExpensiveProvider()
    rt_cost = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=exp_provider,
        max_steps=8,
        max_tokens=100_000,
        model="claude-sonnet-4-6",
        auto_dream=False,
    )
    try:
        run = rt_cost.create(
            "Trigger max_cost_usd hard cap",
            extra_metadata={"max_cost_usd": 0.005, "model": "claude-sonnet-4-6"},
        )
        stopped = await rt_cost.execute(run["id"])
        assert stopped["status"] == "budget_exceeded"
        assert "max_cost_usd" in str(stopped.get("error") or "")
        assert exp_provider.calls == 1
        assert stopped["metadata"]["cost_usd"] == pytest.approx(0.018)
    finally:
        await rt_cost.aclose()


async def test_instant_cooperative_cancellation_interrupts_long_bash_exec(tmp_path: Path) -> None:
    ws, kb, sk, storage = _make_runtime_dirs(tmp_path)
    broker = RunStreamBroker()

    class SleepBashProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
        ) -> dict[str, Any]:
            del system, messages, tools, max_tokens
            self.calls += 1
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_sleep_30",
                        "name": "bash_exec",
                        "input": {"command": "echo READY_TO_CANCEL; sleep 30", "timeout_seconds": 30},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }

    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=SleepBashProvider(),
        execution_profile="host",
        permission_mode="full-auto",
        stream_broker=broker,
        auto_dream=False,
    )
    try:
        run = rt.create("Run a long sleep command and cancel it immediately")
        run_id = run["id"]

        ready_event = asyncio.Event()

        async def _watch_for_ready() -> None:
            async for ev in broker.subscribe(run_id, after_seq=0):
                if ev.get("type") == "bash_output_delta" and "READY_TO_CANCEL" in str(
                    ev.get("payload", {}).get("delta", "")
                ):
                    ready_event.set()
                    return

        watcher = asyncio.create_task(_watch_for_ready())
        exec_task = asyncio.create_task(rt.execute(run_id))

        await asyncio.wait_for(ready_event.wait(), timeout=3.0)
        t_cancel = time.monotonic()
        rt.cancel_active_run(run_id, actor_id="tester", reason="instant_cancel_test")

        cancelled_run = await asyncio.wait_for(exec_task, timeout=2.5)
        elapsed = time.monotonic() - t_cancel
        await asyncio.wait_for(watcher, timeout=1.0)

        assert elapsed < 2.0, f"Cooperative cancellation took too long: {elapsed:.2f}s"
        assert cancelled_run["status"] == "cancelled"
        assert storage.is_cancel_requested(run_id) is True
    finally:
        await rt.aclose()


async def test_api_sse_stream_httponly_cookie_auth_and_audit_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = "tok-admin-secret-001"
    operator_token = "tok-operator-secret-002"
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.setenv(
        "AGENT_LAB_TOKENS_JSON",
        json.dumps(
            {
                admin_token: {
                    "tenant_id": "acme",
                    "actor_id": "alice-admin",
                    "roles": ["admin", "operator"],
                    "global_admin": True,
                },
                operator_token: {
                    "tenant_id": "acme",
                    "actor_id": "bob-op",
                    "roles": ["operator"],
                },
            }
        ),
    )

    state_dir = tmp_path / "api_state"
    app = create_app(state_dir=state_dir, provider="demo")

    with TestClient(app) as client:
        # 1. Unauthenticated requests are rejected with 401
        assert client.get("/api/runs").status_code == 401

        # 2. Invalid token cannot establish cookie session
        bad_auth = client.post("/api/auth/session", json={"token": "wrong-token"})
        assert bad_auth.status_code == 401

        # 3. Valid token exchanges for HttpOnly + SameSite=Strict session cookie
        login = client.post("/api/auth/session", json={"token": admin_token})
        assert login.status_code == 200
        login_data = login.json()
        assert login_data["authenticated"] is True
        assert login_data["actor_id"] == "alice-admin"
        set_cookie_hdr = login.headers.get("set-cookie", "")
        assert f"{SESSION_COOKIE_NAME}=" in set_cookie_hdr
        assert "httponly" in set_cookie_hdr.lower()
        assert "samesite=strict" in set_cookie_hdr.lower()

        # 4. Subsequent authenticated calls work using ONLY the HttpOnly cookie
        sess = client.get("/api/auth/session")
        assert sess.status_code == 200
        assert sess.json()["actor_id"] == "alice-admin"

        # Create a run directly in storage so we deterministically verify broker SSE delivery
        run_doc = app.state.storage.create_run("SSE cookie auth probe", mode="agent", provider="demo")
        run_doc["tenant_id"] = "acme"
        run_doc.setdefault("metadata", {})["tenant_id"] = "acme"
        run_doc["status"] = "completed"
        app.state.storage.save_run(run_doc)
        run_id = run_doc["id"]

        ev1 = app.state.stream_broker.publish(run_id, "thinking_delta", {"delta": "analyzing"})
        ev2 = app.state.stream_broker.publish(run_id, "text_delta", {"delta": "hello sse"})
        assert ev1["seq"] == 1
        assert ev2["seq"] == 2

        # Full buffered SSE replay via cookie auth
        sse_resp = client.get(f"/api/runs/{run_id}/stream")
        assert sse_resp.status_code == 200
        assert "text/event-stream" in sse_resp.headers.get("content-type", "")
        frames = _parse_sse_frames(sse_resp.text)
        assert [f["event"] for f in frames] == ["thinking_delta", "text_delta", "done"]
        assert frames[0]["id"] == "1"
        assert frames[0]["data"]["payload"]["delta"] == "analyzing"
        assert frames[1]["id"] == "2"
        assert frames[1]["data"]["payload"]["delta"] == "hello sse"

        # Incremental SSE replay using Last-Event-ID header skips already-seen seq=1
        sse_resume = client.get(f"/api/runs/{run_id}/stream", headers={"Last-Event-ID": "1"})
        assert sse_resume.status_code == 200
        resume_frames = _parse_sse_frames(sse_resume.text)
        assert [f["event"] for f in resume_frames] == ["text_delta", "done"]
        assert resume_frames[0]["id"] == "2"

        # 5. Live async SSE delivery while run is actively running
        live_run = app.state.storage.create_run("Live SSE push", mode="agent", provider="demo")
        live_run["tenant_id"] = "acme"
        live_run.setdefault("metadata", {})["tenant_id"] = "acme"
        live_run["status"] = "running"
        app.state.storage.save_run(live_run)
        live_id = live_run["id"]

        cookie_val = client.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_val

        async def _emit_live_frames() -> None:
            for _ in range(50):
                if app.state.stream_broker.subscriber_count(live_id) >= 1:
                    break
                await asyncio.sleep(0.01)
            app.state.stream_broker.publish(live_id, "text_delta", {"delta": "chunk-1"})
            app.state.stream_broker.publish(live_id, "text_delta", {"delta": "chunk-2"})
            app.state.stream_broker.close_run(live_id)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={SESSION_COOKIE_NAME: cookie_val},
        ) as async_client:
            emitter = asyncio.create_task(_emit_live_frames())
            live_resp = await async_client.get(f"/api/runs/{live_id}/stream")
            await emitter
            assert live_resp.status_code == 200
            live_frames = _parse_sse_frames(live_resp.text)
            assert [f["event"] for f in live_frames] == ["text_delta", "text_delta", "done"]
            assert [f["data"]["payload"]["delta"] for f in live_frames[:2]] == ["chunk-1", "chunk-2"]

        # 6. Audit logs accessible by admin cookie, forbidden (403) for non-admin operator
        audit_resp = client.get("/api/audit-logs?action=auth_session_created")
        assert audit_resp.status_code == 200
        audit_data = audit_resp.json()
        assert audit_data["ok"] is True
        assert audit_data["count"] >= 1
        assert audit_data["logs"][0]["actor"] == "alice-admin"
        assert audit_data["logs"][0]["action"] == "auth_session_created"

        op_forbidden = client.get(
            "/api/audit-logs",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert op_forbidden.status_code == 403

        # 7. Deleting session revokes cookie immediately
        logout = client.delete("/api/auth/session")
        assert logout.status_code == 200
        client.cookies.set(SESSION_COOKIE_NAME, cookie_val)
        assert client.get("/api/runs").status_code == 401

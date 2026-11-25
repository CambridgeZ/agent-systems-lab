"""Offline deterministic tests for multi-protocol providers and declarative gateways."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any
import urllib.parse

import httpx
import pytest

from agent_lab.gateways import (
    REDACTED_SECRET_MARKER,
    build_provider_from_profile,
    default_gateways_template,
    interpolate_gateway_headers,
    load_gateways_config,
    normalize_protocol,
    redact_gateways_document,
    resolve_provider_or_gateway,
    save_gateways_config,
    set_active_gateway_profile,
    validate_gateway_url,
    validate_gateways_document,
)
from agent_lab.providers import (
    AnthropicProvider,
    DemoProvider,
    GeminiNativeProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    _sanitize_gemini_schema,
    resolve_model_for_tier,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sse_payload(events: list[dict[str, Any]], *, include_done: bool = False) -> bytes:
    lines: list[str] = []
    for item in events:
        lines.append(f"data: {json.dumps(item, ensure_ascii=False)}\n\n")
    if include_done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. OpenAIResponsesProvider (openai_responses)
# ---------------------------------------------------------------------------


async def test_openai_responses_sse_streaming_events_usage_and_cost() -> None:
    captured_requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    completed_response: dict[str, Any] = {
        "id": "resp_stream_01",
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Inspect pricing logic first."}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Reading pricing.py now."}],
            },
            {
                "type": "function_call",
                "id": "fc_item_1",
                "call_id": "call_read_1",
                "name": "repo_read",
                "arguments": '{"path": "pricing.py"}',
            },
        ],
        "usage": {
            "input_tokens": 1_000_000,
            "output_tokens": 200_000,
            "input_tokens_details": {"cached_tokens": 600_000},
            "output_tokens_details": {"reasoning_tokens": 50_000},
        },
    }

    sse_events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "Inspect pricing "},
        {"type": "response.reasoning_summary_text.delta", "delta": "logic first."},
        {"type": "response.output_text.delta", "delta": "Reading "},
        {"type": "response.output_text.delta", "delta": "pricing.py now."},
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_item_1",
                "call_id": "call_read_1",
                "name": "repo_read",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_item_1",
            "delta": '{"path":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_item_1",
            "delta": ' "pricing.py"}',
        },
        {"type": "response.completed", "response": completed_response},
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append((request, json.loads(request.content.decode("utf-8"))))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=_sse_payload(sse_events),
        )

    provider = OpenAIResponsesProvider(
        api_key="sk-resp-offline",
        base_url="https://mock.openai.local/v1",
        allow_custom_base_url=True,
        model="gpt-5",
        fast_model="gpt-5-mini",
        reasoning_effort="medium",
        extra_headers={"X-Custom-Trace": "resp-sse"},
        use_stream=True,
        transport=httpx.MockTransport(_handler),
    )

    tools = [
        {
            "name": "repo_read",
            "description": "Read a workspace file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    streamed_events: list[dict[str, Any]] = []

    async def _on_event(ev: dict[str, Any]) -> None:
        streamed_events.append(ev)

    result = await provider.complete(
        "You are a coding assistant.",
        [{"role": "user", "content": "Inspect pricing.py"}],
        tools,
        max_tokens=1024,
        prompt_cache_key="cache-key-01",
        on_event=_on_event,
    )

    assert len(captured_requests) == 1
    req, body = captured_requests[0]
    assert str(req.url) == "https://mock.openai.local/v1/responses"
    assert req.headers["authorization"] == "Bearer sk-resp-offline"
    assert req.headers["x-custom-trace"] == "resp-sse"
    assert body["model"] == "gpt-5"
    assert body["instructions"] == "You are a coding assistant."
    assert body["stream"] is True
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "medium"}
    assert body["prompt_cache_key"] == "cache-key-01"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == "repo_read"

    event_types = [ev["type"] for ev in streamed_events]
    assert event_types == [
        "thinking_delta",
        "thinking_delta",
        "text_delta",
        "text_delta",
        "tool_use_start",
        "input_json_delta",
        "input_json_delta",
        "message_stop",
    ]
    assert "".join(ev["delta"] for ev in streamed_events if ev["type"] == "thinking_delta") == (
        "Inspect pricing logic first."
    )
    assert "".join(ev["delta"] for ev in streamed_events if ev["type"] == "text_delta") == (
        "Reading pricing.py now."
    )
    tool_start = next(ev for ev in streamed_events if ev["type"] == "tool_use_start")
    assert tool_start["name"] == "repo_read"
    assert tool_start["call_id"] == "call_read_1"
    assert "".join(ev["delta"] for ev in streamed_events if ev["type"] == "input_json_delta") == (
        '{"path": "pricing.py"}'
    )
    assert streamed_events[-1]["stop_reason"] == "tool_use"

    assert result["stop_reason"] == "tool_use"
    assert result["truncated"] is False
    assert result["content"] == [
        {"type": "thinking", "thinking": "Inspect pricing logic first."},
        {"type": "text", "text": "Reading pricing.py now."},
        {
            "type": "tool_use",
            "id": "call_read_1",
            "name": "repo_read",
            "input": {"path": "pricing.py"},
        },
    ]

    # gpt-5 rates: input=1.25, output=10.0, cache_read=0.125 USD/MTok (non-additive)
    # uncached=400k -> 0.50, cache_read=600k -> 0.075, output=200k -> 2.00 => total=2.575
    usage = result["usage"]
    assert usage["input_tokens"] == 1_000_000
    assert usage["output_tokens"] == 200_000
    assert usage["cache_read_input_tokens"] == 600_000
    assert usage["reasoning_tokens"] == 50_000
    assert usage["cost_breakdown"]["pricing_known"] is True
    assert usage["cost_usd"] == pytest.approx(2.575)


async def test_openai_responses_non_streaming_multiturn_function_call_output_translation() -> None:
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_bodies.append(body)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "resp_json_02",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Reviewed file contents."}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "The discount logic is verified."}],
                    },
                ],
                "usage": {
                    "input_tokens": 500_000,
                    "output_tokens": 100_000,
                    "input_tokens_details": {"cached_tokens": 200_000},
                    "output_tokens_details": {"reasoning_tokens": 10_000},
                },
            },
        )

    provider = OpenAIResponsesProvider(
        api_key="sk-resp-nonstream",
        base_url="https://mock.openai.local/v1",
        allow_custom_base_url=True,
        model="gpt-5",
        use_stream=False,
        transport=httpx.MockTransport(_handler),
    )

    tools = [
        {
            "name": "repo_read",
            "description": "Read file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    multiturn_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Check discounts.py"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking discounts.py"},
                {
                    "type": "tool_use",
                    "id": "call_lookup_42",
                    "name": "repo_read",
                    "input": {"path": "discounts.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_lookup_42",
                    "content": '{"path": "discounts.py", "ok": true}',
                },
                {"type": "text", "text": "Summarize findings."},
            ],
        },
    ]

    resp = await provider.complete("System prompt", multiturn_messages, tools, max_tokens=512)
    assert len(captured_bodies) == 1
    assert "stream" not in captured_bodies[0]
    assert resp["stop_reason"] == "end_turn"
    assert resp["content"] == [
        {"type": "thinking", "thinking": "Reviewed file contents."},
        {"type": "text", "text": "The discount logic is verified."},
    ]
    assert resp["usage"]["cache_read_input_tokens"] == 200_000
    assert resp["usage"]["reasoning_tokens"] == 10_000
    # uncached=300k*1.25/1M=0.375, cached=200k*0.125/1M=0.025, output=100k*10/1M=1.0 -> 1.40
    assert resp["usage"]["cost_usd"] == pytest.approx(1.40)

    sent_input = captured_bodies[0]["input"]
    fc_items = [item for item in sent_input if item.get("type") == "function_call"]
    fco_items = [item for item in sent_input if item.get("type") == "function_call_output"]
    assert fc_items == [
        {
            "type": "function_call",
            "call_id": "call_lookup_42",
            "name": "repo_read",
            "arguments": '{"path": "discounts.py"}',
        }
    ]
    assert fco_items == [
        {
            "type": "function_call_output",
            "call_id": "call_lookup_42",
            "output": '{"path": "discounts.py", "ok": true}',
        }
    ]
    assert sent_input[-1] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Summarize findings."}],
    }


async def test_openai_responses_non_streaming_function_call_response() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "resp_json_fc",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_item_2",
                        "call_id": "call_lookup_42",
                        "name": "repo_read",
                        "arguments": '{"path": "discounts.py"}',
                    }
                ],
                "usage": {
                    "input_tokens": 500_000,
                    "output_tokens": 100_000,
                    "input_tokens_details": {"cached_tokens": 200_000},
                },
            },
        )

    provider = OpenAIResponsesProvider(
        api_key="sk-resp-nonstream-fc",
        base_url="https://mock.openai.local/v1",
        allow_custom_base_url=True,
        model="gpt-5",
        use_stream=False,
        transport=httpx.MockTransport(_handler),
    )

    result = await provider.complete(
        "System prompt",
        [{"role": "user", "content": "Check discounts.py"}],
        [],
        max_tokens=512,
    )
    assert result["stop_reason"] == "tool_use"
    assert result["content"] == [
        {
            "type": "tool_use",
            "id": "call_lookup_42",
            "name": "repo_read",
            "input": {"path": "discounts.py"},
        }
    ]
    assert result["usage"]["cache_read_input_tokens"] == 200_000
    assert result["usage"]["cost_usd"] == pytest.approx(1.40)


# ---------------------------------------------------------------------------
# 2. GeminiNativeProvider (gemini_generate_content)
# ---------------------------------------------------------------------------


def _assert_no_key_recursive(obj: Any, forbidden_keys: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in forbidden_keys, f"Unexpected forbidden key {k!r} in sanitized Gemini schema"
            _assert_no_key_recursive(v, forbidden_keys)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_key_recursive(item, forbidden_keys)


async def test_gemini_native_sse_streaming_headers_schema_roundtrip_usage_and_cost() -> None:
    secret_key = "gem-secret-key-never-in-url"
    captured_requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    sse_chunks = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"thought": True, "text": "Check the listed files first."}],
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Opening pricing.py for inspection."}],
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "gem_call_7",
                                    "name": "repo_read",
                                    "args": {"path": "pricing.py"},
                                }
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 1_000_000,
                "candidatesTokenCount": 100_000,
                "cachedContentTokenCount": 400_000,
                "thoughtsTokenCount": 25_000,
            },
        },
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append((request, json.loads(request.content.decode("utf-8"))))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=_sse_payload(sse_chunks),
        )

    provider = GeminiNativeProvider(
        api_key=secret_key,
        base_url="https://mock.gemini.local/v1beta",
        allow_custom_base_url=True,
        model="gemini-2.5-pro",
        fast_model="gemini-2.5-flash",
        use_stream=True,
        transport=httpx.MockTransport(_handler),
    )

    tools = [
        {
            "name": "repo_read",
            "description": "Read a file from the repo",
            "input_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "RepoReadInput",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "default": "pricing.py"},
                    "encoding": {"type": ["string", "null"]},
                    "options": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"max_lines": {"type": "integer", "default": 200}},
                    },
                },
                "required": ["path"],
            },
        }
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "List files then inspect pricing.py"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "prev_call_99",
                    "name": "repo_list",
                    "input": {"dir": "."},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "prev_call_99",
                    "content": '{"files": ["pricing.py", "discounts.py"]}',
                }
            ],
        },
    ]

    events: list[dict[str, Any]] = []
    result = await provider.complete(
        "Follow engineering standards.",
        messages,
        tools,
        max_tokens=1024,
        on_event=events.append,
    )

    assert len(captured_requests) == 1
    req, body = captured_requests[0]

    # Security invariant: API key MUST travel via x-goog-api-key header, NEVER in URL query string
    assert req.headers["x-goog-api-key"] == secret_key
    parsed_url = urllib.parse.urlsplit(str(req.url))
    query_params = urllib.parse.parse_qs(parsed_url.query)
    assert parsed_url.path == "/v1beta/models/gemini-2.5-pro:streamGenerateContent"
    assert query_params == {"alt": ["sse"]}
    assert "key" not in query_params
    assert secret_key not in str(req.url)

    # Schema sanitization: additionalProperties / $schema / default / title stripped recursively
    decl_params = body["tools"][0]["functionDeclarations"][0]["parameters"]
    _assert_no_key_recursive(decl_params, {"additionalProperties", "$schema", "default", "title"})
    assert decl_params["properties"]["encoding"] == {"type": "string", "nullable": True}

    # Multi-turn functionResponse roundtrip maps tool_use_id -> original tool name
    contents = body["contents"]
    assert contents[1] == {
        "role": "model",
        "parts": [{"functionCall": {"name": "repo_list", "args": {"dir": "."}}}],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "repo_list",
                    "response": {"files": ["pricing.py", "discounts.py"]},
                }
            }
        ],
    }

    # Streaming events
    assert [ev["type"] for ev in events] == [
        "thinking_delta",
        "text_delta",
        "tool_use_start",
        "message_stop",
    ]
    assert events[0]["delta"] == "Check the listed files first."
    assert events[1]["delta"] == "Opening pricing.py for inspection."
    assert events[2]["name"] == "repo_read"
    assert events[2]["call_id"] == "gem_call_7"

    # Normalized response content
    assert result["stop_reason"] == "tool_use"
    assert result["truncated"] is False
    assert result["content"] == [
        {"type": "thinking", "thinking": "Check the listed files first."},
        {"type": "text", "text": "Opening pricing.py for inspection."},
        {
            "type": "tool_use",
            "id": "gem_call_7",
            "name": "repo_read",
            "input": {"path": "pricing.py"},
        },
    ]

    # gemini-2.5-pro rates: input=1.25, output=10.0, cache_read=0.3125 USD/MTok (non-additive)
    # uncached=600k -> 0.75, cache_read=400k -> 0.125, output=100k -> 1.00 => total=1.875
    usage = result["usage"]
    assert usage["input_tokens"] == 1_000_000
    assert usage["output_tokens"] == 100_000
    assert usage["cache_read_input_tokens"] == 400_000
    assert usage["reasoning_tokens"] == 25_000
    assert usage["cost_breakdown"]["pricing_known"] is True
    assert usage["cost_usd"] == pytest.approx(1.875)


async def test_gemini_native_non_streaming_generate_content_and_schema_sanitizer() -> None:
    # Direct unit test of _sanitize_gemini_schema
    raw_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "nested": {
                "type": "object",
                "additionalProperties": False,
                "default": {},
            },
            "tags": {
                "type": "array",
                "items": {"type": ["string", "null"], "examples": ["a"]},
            },
        },
    }
    sanitized = _sanitize_gemini_schema(raw_schema)
    _assert_no_key_recursive(
        sanitized, {"$schema", "additionalProperties", "default", "examples", "title"}
    )
    assert sanitized["properties"]["nested"] == {"type": "object", "properties": {}}
    assert sanitized["properties"]["tags"]["items"] == {"type": "string", "nullable": True}

    secret_key = "gem-nonstream-secret"
    captured: list[tuple[httpx.Request, dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append((request, json.loads(request.content.decode("utf-8"))))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"thought": True, "text": "Double-checking calculation."},
                                {"text": "Discount formula is fixed."},
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 500_000,
                    "candidatesTokenCount": 50_000,
                    "cachedContentTokenCount": 200_000,
                    "thoughtsTokenCount": 5_000,
                },
            },
        )

    provider = GeminiNativeProvider(
        api_key=secret_key,
        base_url="https://mock.gemini.local/v1beta",
        allow_custom_base_url=True,
        model="gemini-2.5-pro",
        use_stream=False,
        transport=httpx.MockTransport(_handler),
    )

    result = await provider.complete(
        "Be concise.",
        [
            {"role": "user", "content": "Verify fix"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call_verify_1", "name": "verify_python", "input": {"path": "pricing.py"}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_verify_1", "content": '{"syntax_ok": true}'}
                ],
            },
        ],
        [
            {
                "name": "verify_python",
                "description": "Verify python syntax",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        max_tokens=512,
    )

    assert len(captured) == 1
    req, body = captured[0]
    assert req.headers["x-goog-api-key"] == secret_key
    assert secret_key not in str(req.url)
    assert str(req.url) == "https://mock.gemini.local/v1beta/models/gemini-2.5-pro:generateContent"
    assert body["systemInstruction"] == {"parts": [{"text": "Be concise."}]}
    _assert_no_key_recursive(body["tools"], {"additionalProperties"})
    assert body["contents"][2]["parts"][0] == {
        "functionResponse": {"name": "verify_python", "response": {"syntax_ok": True}}
    }

    assert result["stop_reason"] == "end_turn"
    assert result["truncated"] is False
    assert result["content"] == [
        {"type": "thinking", "thinking": "Double-checking calculation."},
        {"type": "text", "text": "Discount formula is fixed."},
    ]
    # uncached=300k*1.25/1M=0.375, cached=200k*0.3125/1M=0.0625, output=50k*10/1M=0.50 -> 0.9375
    assert result["usage"]["cache_read_input_tokens"] == 200_000
    assert result["usage"]["cost_usd"] == pytest.approx(0.9375)


# ---------------------------------------------------------------------------
# 3. Explicit Config Parameterization
# ---------------------------------------------------------------------------


async def test_explicit_config_parameterization_no_env_mutation_and_fast_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-ignored")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-model")
    monkeypatch.setenv("ANTHROPIC_FAST_MODEL", "claude-env-fast")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env-anthropic.example.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-env-ignored")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-env-model")
    monkeypatch.setenv("OPENAI_FAST_MODEL", "gpt-env-fast")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env-openai.example.invalid/v1")
    monkeypatch.setenv("AGENT_LAB_FAST_MODEL", "global-env-fast")

    env_snapshot = dict(os.environ)

    ant_requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    def _ant_handler(request: httpx.Request) -> httpx.Response:
        ant_requests.append((request, json.loads(request.content.decode("utf-8"))))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "msg_explicit_01",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ant-pong"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )

    ant_http_client = httpx.AsyncClient(transport=httpx.MockTransport(_ant_handler))
    ant_provider = AnthropicProvider(
        api_key="sk-ant-explicit-key",
        model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
        reasoning_model="claude-opus-4-6",
        base_url="https://custom-anthropic.example.local",
        extra_headers={"X-Ant-Tenant": "tenant-a"},
        allow_custom_base_url=True,
        http_client=ant_http_client,
    )

    oai_requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    def _oai_handler(request: httpx.Request) -> httpx.Response:
        oai_requests.append((request, json.loads(request.content.decode("utf-8"))))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    oai_provider = OpenAICompatibleProvider(
        api_key="sk-oai-explicit-key",
        model="gpt-4.1",
        fast_model="gpt-4.1-mini",
        reasoning_model="o4-mini",
        base_url="https://custom-openai.example.local/v1",
        extra_headers={"X-Oai-Tenant": "tenant-b"},
        allow_custom_base_url=True,
        stream=False,
        transport=httpx.MockTransport(_oai_handler),
    )

    try:
        # Zero mutation to os.environ
        assert dict(os.environ) == env_snapshot

        # Explicit fields override env vars
        assert ant_provider.model == "claude-sonnet-4-6"
        assert ant_provider.fast_model == "claude-haiku-4-5"
        assert ant_provider.base_url == "https://custom-anthropic.example.local"
        assert ant_provider.extra_headers == {"X-Ant-Tenant": "tenant-a"}
        assert str(ant_provider.client.base_url).rstrip("/") == "https://custom-anthropic.example.local"
        assert ant_provider.client.default_headers.get("X-Ant-Tenant") == "tenant-a"

        assert oai_provider.model == "gpt-4.1"
        assert oai_provider.fast_model == "gpt-4.1-mini"
        assert oai_provider.base_url == "https://custom-openai.example.local/v1"
        assert oai_provider.extra_headers == {"X-Oai-Tenant": "tenant-b"}

        # resolve_model_for_tier prefers explicit provider.fast_model over env overrides
        assert resolve_model_for_tier(ant_provider, "fast") == "claude-haiku-4-5"
        assert resolve_model_for_tier(ant_provider, "reasoning") == "claude-opus-4-6"
        assert resolve_model_for_tier(ant_provider, "inherit") == "claude-sonnet-4-6"

        assert resolve_model_for_tier(oai_provider, "fast") == "gpt-4.1-mini"
        assert resolve_model_for_tier(oai_provider, "reasoning") == "o4-mini"
        assert resolve_model_for_tier(oai_provider, "inherit") == "gpt-4.1"

        # Wire verification for AnthropicProvider explicit config
        ant_resp = await ant_provider.complete("sys", [{"role": "user", "content": "ping"}], [], max_tokens=32)
        assert ant_resp["content"] == [{"type": "text", "text": "ant-pong"}]
        assert len(ant_requests) == 1
        ant_req, ant_body = ant_requests[0]
        assert str(ant_req.url) == "https://custom-anthropic.example.local/v1/messages"
        assert ant_req.headers["x-api-key"] == "sk-ant-explicit-key"
        assert ant_req.headers["x-ant-tenant"] == "tenant-a"
        assert ant_body["model"] == "claude-sonnet-4-6"

        # Wire verification for OpenAICompatibleProvider explicit config
        resp = await oai_provider.complete("sys", [{"role": "user", "content": "ping"}], [], max_tokens=32)
        assert resp["content"] == [{"type": "text", "text": "pong"}]
        assert len(oai_requests) == 1
        req, body = oai_requests[0]
        assert str(req.url) == "https://custom-openai.example.local/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer sk-oai-explicit-key"
        assert req.headers["x-oai-tenant"] == "tenant-b"
        assert body["model"] == "gpt-4.1"
        assert dict(os.environ) == env_snapshot
    finally:
        await ant_provider.aclose()
        await ant_http_client.aclose()
        await oai_provider.aclose()


# ---------------------------------------------------------------------------
# 4. Declarative gateways.py & gateways.example.json
# ---------------------------------------------------------------------------


def test_gateways_example_json_parses_and_validates() -> None:
    example_path = REPO_ROOT / "gateways.example.json"
    assert example_path.is_file()
    raw_doc = json.loads(example_path.read_text(encoding="utf-8"))
    validated = validate_gateways_document(raw_doc)

    assert validated["schema_version"] == 1
    assert validated["active_profile"] == "demo"
    assert set(validated["profiles"]) == {
        "claude-anthropic",
        "openai-chat",
        "openai-responses",
        "gemini-native",
        "ollama-local",
        "demo",
    }
    assert validated == default_gateways_template()


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("anthropic", "anthropic_messages"),
        ("claude", "anthropic_messages"),
        ("anthropic-messages", "anthropic_messages"),
        ("openai", "openai_chat"),
        ("openai_compat", "openai_chat"),
        ("chat_completions", "openai_chat"),
        ("responses", "openai_responses"),
        ("openai-responses", "openai_responses"),
        ("gemini", "gemini_generate_content"),
        ("google_gemini", "gemini_generate_content"),
        ("gemini_native", "gemini_generate_content"),
        ("demo", "demo"),
        ("offline_demo", "demo"),
    ],
)
def test_protocol_alias_normalization(alias: str, expected: str) -> None:
    assert normalize_protocol(alias) == expected


def test_protocol_aliases_normalized_inside_gateways_document() -> None:
    doc = {
        "schema_version": 1,
        "active_profile": "p-resp",
        "profiles": {
            "p-ant": {"protocol": "anthropic", "model": "claude-sonnet-4-6"},
            "p-oai": {"protocol": "openai", "model": "gpt-4.1"},
            "p-resp": {"protocol": "responses", "model": "gpt-5"},
            "p-gem": {"protocol": "gemini", "model": "gemini-2.5-pro"},
        },
    }
    validated = validate_gateways_document(doc)
    assert validated["profiles"]["p-ant"]["protocol"] == "anthropic_messages"
    assert validated["profiles"]["p-oai"]["protocol"] == "openai_chat"
    assert validated["profiles"]["p-resp"]["protocol"] == "openai_responses"
    assert validated["profiles"]["p-gem"]["protocol"] == "gemini_generate_content"

    with pytest.raises(ValueError):
        normalize_protocol("unknown_protocol_xyz")


def test_gateways_security_url_credentials_imds_and_crlf_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_ALLOW_CUSTOM_BASE_URL", raising=False)

    # Embedded URL credentials rejected even with allow_custom_base_url=True
    for bad_cred_url in (
        "https://u:p@host",
        "https://u:p@api.openai.com/v1",
        "https://token@api.anthropic.com",
    ):
        with pytest.raises(ValueError):
            validate_gateway_url(bad_cred_url, protocol="openai_chat", allow_custom_base_url=True)

    # Cloud metadata IMDS & obfuscated IPs rejected unconditionally
    for imds_url in (
        "http://169.254.169.254/latest/meta-data",
        "https://169.254.169.254/v1",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://[fd00:ec2::254]/latest/meta-data",
        "http://100.100.100.200/latest/meta-data",
        "http://2852039166/latest/meta-data",
        "http://[::ffff:169.254.169.254]/v1",
    ):
        with pytest.raises(ValueError):
            validate_gateway_url(imds_url, protocol="openai_chat", allow_custom_base_url=True)

    # Custom endpoint rejected unless allow_custom_base_url=True
    with pytest.raises(ValueError):
        validate_gateway_url(
            "https://untrusted-proxy.example.com/v1",
            protocol="openai_chat",
            allow_custom_base_url=False,
        )

    # CRLF header injection rejected during document validation
    with pytest.raises(ValueError):
        validate_gateways_document(
            {
                "schema_version": 1,
                "active_profile": "bad-hdr",
                "profiles": {
                    "bad-hdr": {
                        "protocol": "openai_chat",
                        "model": "gpt-4.1",
                        "headers": {"X-Custom": "ok\r\nX-Injected: evil"},
                    }
                },
            }
        )

    with pytest.raises(ValueError):
        validate_gateways_document(
            {
                "schema_version": 1,
                "active_profile": "bad-hdr-name",
                "profiles": {
                    "bad-hdr-name": {
                        "protocol": "openai_chat",
                        "model": "gpt-4.1",
                        "headers": {"X-Bad\r\nName": "value"},
                    }
                },
            }
        )

    # CRLF injection via env interpolation also rejected
    with pytest.raises(ValueError):
        interpolate_gateway_headers(
            {"Authorization": "Bearer ${MALICIOUS_TOKEN}"},
            environ={"MALICIOUS_TOKEN": "abc\r\nX-Evil: 1"},
            profile_name="test-profile",
        )


def test_gateways_security_symlink_hardlink_and_0600_permissions(tmp_path: Path) -> None:
    doc = default_gateways_template()
    cfg_path = tmp_path / "gateways.json"

    saved = save_gateways_config(cfg_path, doc)
    assert Path(saved["config_path"]) == cfg_path.resolve()
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

    # Symlink file rejected on read and save
    symlink_path = tmp_path / "gateways-link.json"
    symlink_path.symlink_to(cfg_path)
    with pytest.raises(ValueError):
        load_gateways_config(explicit_path=symlink_path)
    with pytest.raises(ValueError):
        save_gateways_config(symlink_path, doc)

    # Symlink parent directory rejected on read and save
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    save_gateways_config(real_dir / "gateways.json", doc)
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError):
        load_gateways_config(explicit_path=link_dir / "gateways.json")
    with pytest.raises(ValueError):
        save_gateways_config(link_dir / "gateways.json", doc)

    # Hardlink (st_nlink > 1) rejected on read and save
    hard_source = tmp_path / "hard_source.json"
    save_gateways_config(hard_source, doc)
    hard_alias = tmp_path / "hard_alias.json"
    os.link(hard_source, hard_alias)
    with pytest.raises(ValueError):
        load_gateways_config(explicit_path=hard_alias)
    with pytest.raises(ValueError):
        save_gateways_config(hard_alias, doc)
    with pytest.raises(ValueError):
        load_gateways_config(explicit_path=hard_source)
    with pytest.raises(ValueError):
        save_gateways_config(hard_source, doc)


def test_redact_gateways_document_masks_inline_api_key_and_secret_headers() -> None:
    doc = {
        "schema_version": 1,
        "active_profile": "secret-profile",
        "profiles": {
            "secret-profile": {
                "name": "secret-profile",
                "protocol": "openai_responses",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "",
                "api_key": "sk-inline-super-secret-value",
                "model": "gpt-5",
                "headers": {
                    "Authorization": "Bearer auth-secret-111",
                    "x-api-key": "ant-secret-222",
                    "x-goog-api-key": "gem-secret-333",
                    "X-Gateway-Token": "tok-secret-444",
                    "X-Debug-Note": "Bearer embedded-bearer-555",
                    "X-Client-App": "agent-systems-lab",
                },
            },
            "unconfigured-cloud": {
                "name": "unconfigured-cloud",
                "protocol": "anthropic_messages",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "MISSING_KEY_ENV",
                "model": "claude-sonnet-4-6",
                "headers": {},
            },
        },
    }

    redacted = redact_gateways_document(doc, environ={})

    # Original document must remain untouched
    assert doc["profiles"]["secret-profile"]["api_key"] == "sk-inline-super-secret-value"

    sp = redacted["profiles"]["secret-profile"]
    assert sp["api_key"] == REDACTED_SECRET_MARKER
    assert sp["headers"]["Authorization"] == REDACTED_SECRET_MARKER
    assert sp["headers"]["x-api-key"] == REDACTED_SECRET_MARKER
    assert sp["headers"]["x-goog-api-key"] == REDACTED_SECRET_MARKER
    assert sp["headers"]["X-Gateway-Token"] == REDACTED_SECRET_MARKER
    assert sp["headers"]["X-Debug-Note"] == f"Bearer {REDACTED_SECRET_MARKER}"
    assert sp["headers"]["X-Client-App"] == "agent-systems-lab"
    assert sp["credential_configured"] is True

    assert redacted["profiles"]["unconfigured-cloud"]["credential_configured"] is False

    serialized = json.dumps(redacted)
    for leaked in (
        "sk-inline-super-secret-value",
        "auth-secret-111",
        "ant-secret-222",
        "gem-secret-333",
        "tok-secret-444",
        "embedded-bearer-555",
    ):
        assert leaked not in serialized


async def test_build_provider_from_profile_constructs_all_five_protocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-factory")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test-factory")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key-test-factory")
    monkeypatch.setenv("CUSTOM_HDR_ENV", "header-from-env")

    template = default_gateways_template()
    profiles = template["profiles"]

    expected_types: list[tuple[str, type[Any]]] = [
        ("claude-anthropic", AnthropicProvider),
        ("openai-chat", OpenAICompatibleProvider),
        ("openai-responses", OpenAIResponsesProvider),
        ("gemini-native", GeminiNativeProvider),
        ("demo", DemoProvider),
    ]

    built: list[Any] = []
    try:
        for prof_name, expected_cls in expected_types:
            prof = dict(profiles[prof_name])
            if prof_name != "demo":
                prof["headers"] = {"X-Env-Tag": "${CUSTOM_HDR_ENV}"}
            provider = build_provider_from_profile(prof_name, prof)
            built.append(provider)
            assert type(provider) is expected_cls
            assert getattr(provider, "gateway_profile") == prof_name
            assert getattr(provider, "gateway_protocol") == prof["protocol"]
            if prof_name != "demo":
                assert getattr(provider, "model") == prof["model"]
                assert getattr(provider, "fast_model") == prof["fast_model"]
                assert getattr(provider, "extra_headers") == {"X-Env-Tag": "header-from-env"}
    finally:
        for provider in built:
            if hasattr(provider, "aclose"):
                await provider.aclose()


async def test_set_active_gateway_profile_and_resolve_provider_or_gateway_switching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_GATEWAY_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_LAB_GATEWAYS_FILE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-switch")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-switch")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-switch")

    cfg_path = tmp_path / "gateways.json"
    save_gateways_config(cfg_path, default_gateways_template())

    transitions: list[tuple[str, type[Any], str, str]] = [
        ("demo", DemoProvider, "demo", "demo"),
        ("claude-anthropic", AnthropicProvider, "anthropic_messages", "claude-sonnet-4-6"),
        ("openai-chat", OpenAICompatibleProvider, "openai_chat", "gpt-4.1"),
        ("openai-responses", OpenAIResponsesProvider, "openai_responses", "gpt-5"),
        ("gemini-native", GeminiNativeProvider, "gemini_generate_content", "gemini-2.5-pro"),
        ("ollama-local", OpenAICompatibleProvider, "openai_chat", "qwen2.5-coder:14b"),
    ]

    for prof_name, expected_cls, expected_proto, expected_model in transitions:
        updated = set_active_gateway_profile(cfg_path, prof_name)
        assert updated["active_profile"] == prof_name
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

        provider, info = resolve_provider_or_gateway(None, explicit_config_path=cfg_path)
        try:
            assert type(provider) is expected_cls
            assert info["profile"] == prof_name
            assert info["protocol"] == expected_proto
            assert info["model"] == expected_model
            assert info["source"] == "active_profile"
            assert "sk-ant-switch" not in json.dumps(info)
            assert "sk-oai-switch" not in json.dumps(info)
            assert "gem-switch" not in json.dumps(info)
        finally:
            if hasattr(provider, "aclose"):
                await provider.aclose()

    # Explicit gateway:<name> spec overrides active_profile without mutating the file
    override_provider, override_info = resolve_provider_or_gateway(
        "gateway:openai-responses",
        explicit_config_path=cfg_path,
    )
    assert type(override_provider) is OpenAIResponsesProvider
    assert override_info["profile"] == "openai-responses"
    assert override_info["protocol"] == "openai_responses"
    assert override_info["source"] == "gateway_explicit"

    # Attempting to activate a nonexistent profile raises ValueError
    with pytest.raises(ValueError):
        set_active_gateway_profile(cfg_path, "does-not-exist")


def test_model_aware_context_window_scaling_for_1m_200k_128k_and_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify real 1M / 200k / 128k models auto-scale ContextManager while DemoProvider stays compact."""
    from agent_lab.providers import resolve_model_context_window
    from agent_lab.runtime import AgentRuntime
    from agent_lab.storage import Storage

    monkeypatch.delenv("AGENT_LAB_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("AGENT_LAB_CONTEXT_CHARS", raising=False)

    assert resolve_model_context_window("gemini-4-neon[1m]") == 1_000_000
    assert resolve_model_context_window("my-claude-gemini-4-neon[1m]") == 1_000_000
    assert resolve_model_context_window("gemini-2.5-pro") == 1_000_000
    assert resolve_model_context_window("gpt-5.3-codex[400k]") == 400_000
    assert resolve_model_context_window("claude-sonnet-5") == 200_000
    assert resolve_model_context_window("gpt-4o") == 128_000
    assert resolve_model_context_window("demo") is None

    # Explicit gateways.json context_window_tokens overrides family default
    assert resolve_model_context_window("claude-sonnet-5", explicit_window=1_000_000) == 1_000_000

    # AgentRuntime auto-scales for 1M model (e.g. gemini-4-neon[1m] via AnthropicProvider)
    ws = tmp_path / "ws"
    ws.mkdir()
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    sdir = tmp_path / "skills"
    sdir.mkdir()
    storage = Storage(tmp_path / "state.db")

    prov_1m = AnthropicProvider(
        api_key="sk-ant-test-1m",
        model="gemini-4-neon[1m]",
        base_url="http://127.0.0.1:4141",
        allow_custom_base_url=True,
    )
    rt_1m = AgentRuntime(storage, ws, kdir, sdir, provider=prov_1m, execution_profile="host")
    assert rt_1m.context_window_tokens == 1_000_000
    assert rt_1m.context.max_tokens is not None and rt_1m.context.max_tokens >= 950_000
    assert rt_1m.context.max_chars >= 3_500_000
    assert rt_1m.max_tokens >= 2_000_000
    assert rt_1m.max_output_tokens == 16_384

    # DemoProvider preserves compact baseline for deterministic unit tests
    rt_demo = AgentRuntime(storage, ws, kdir, sdir, provider="demo", execution_profile="sandbox")
    assert rt_demo.context_window_tokens is None
    assert rt_demo.context.max_chars == 24_000
    assert rt_demo.max_tokens == 30_000

    # AGENT_LAB_CONTEXT_TOKENS env override works cleanly
    monkeypatch.setenv("AGENT_LAB_CONTEXT_TOKENS", "500000")
    assert resolve_model_context_window("claude-sonnet-5") == 500_000

"""模型适配层：运行时只处理标准化 content / usage。

DemoProvider 是确定性脚本，用来复现状态机和故障，不能代表 LLM 能力。
AnthropicProvider 与 OpenAICompatibleProvider 由真实模型决定下一步工具；
三者共享同一工具执行、审批、修复循环和恢复路径。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Protocol

from .kv_cache import build_anthropic_cached_payload, canonical_json, canonical_tools


class Provider(Protocol):
    async def complete(
        self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict: ...


def _call(name: str, **arguments: object) -> dict:
    return {"type": "tool_use", "id": "call_" + uuid.uuid4().hex[:16], "name": name, "input": arguments}


def _response(content: list[dict]) -> dict:
    return {
        "content": content,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


class DemoProvider:
    """只演示内置单文件 pricing.py 与多文件 checkout/discounts 两个固定场景；其他任务明确说明边界。"""

    async def complete(
        self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict:
        results: list[dict] = []
        for message in messages:
            if not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    try:
                        results.append(json.loads(block["content"]))
                    except (ValueError, TypeError):
                        pass
        original = str(messages[0].get("content", "")) if messages else ""
        lower = original.lower()
        is_multi_file = any(
            term in lower for term in ("checkout", "discounts", "结算", "优惠券", "封顶", "多文件")
        )
        if "ROLE:reviewer" in system or "ROLE:tester" in system:
            if is_multi_file:
                if not results:
                    return _response(
                        [
                            _call("repo_map", max_chars=1800, focus_query="checkout discount coupon"),
                            _call("symbol_search", symbol="compute_discount_cents"),
                        ]
                    )
                advice = (
                    "测试建议：校验无券原价、百分比扣减、max_discount_cents 封顶上限以及零元边界"
                    if "ROLE:tester" in system
                    else "代码分析：discounts.py 漏掉 max_discount_cents 封顶，checkout.py 将减免额误赋给应付总额"
                )
                return _response([{"type": "text", "text": advice}])
            if not results:
                return _response([_call("repo_read", path="pricing.py")])
            role = (
                "测试建议：检查 0%、10%、100% 折扣及非法参数"
                if "ROLE:tester" in system
                else ("代码分析：discount_percent 是百分数，应先除以 100；修改范围限于返回表达式")
            )
            return _response([{"type": "text", "text": role}])

        if is_multi_file:
            return self._complete_multi_file(messages, results)

        if not any(term in lower for term in ("折扣", "discount", "pricing", "百分比")):
            return _response(
                [
                    {
                        "type": "text",
                        "text": "离线 demo 仅支持内置 pricing.py 单文件任务与 checkout/discounts 多文件任务。"
                        "任意开放编码任务需配置真实模型（anthropic 或 openai）。",
                    }
                ]
            )
        # 只识别运行时在后续独立 user 消息中写入的审批标记。原始任务和工具正文
        # 即使包含同样字符串，也不能被 demo 当作审批（真正写入仍由工具策略保护）。
        decisions = [
            m.get("content", "")
            for m in messages[1:]
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        # 旧标记可能被裁掉。执行器从持久化审批对象重建状态，不靠模型猜测摘要。
        harness_decisions = []
        for text in decisions:
            if text.startswith("HARNESS_STATE\n"):
                try:
                    harness_decisions = json.loads(text.split("\n", 1)[1]).get("decisions", [])
                except (ValueError, AttributeError):
                    pass
        latest = harness_decisions[-1].get("decision") if harness_decisions else None
        if latest == "rejected" or any(text.startswith("REVIEW_REJECTED: ") for text in decisions):
            return _response([{"type": "text", "text": "补丁已被用户拒绝，工作区保持原状。本次任务停止。"}])
        if latest == "approved" or any(text.startswith("REVIEW_APPROVED: ") for text in decisions):
            if not any("valid" in result or "syntax_ok" in result for result in results):
                return _response([_call("verify_python", path="pricing.py"), _call("sandbox_test")])
            sandbox = next(
                (
                    r
                    for r in results
                    if r.get("code") in {"passed", "tests_failed"} or r.get("code", "").startswith("sandbox_")
                ),
                {},
            )
            citations = [
                m.get("chunk_id", m.get("id"))
                for r in results
                for m in r.get("matches", [])
                if m.get("chunk_id") or m.get("id")
            ]
            evidence = f" 知识参考 [{citations[0]}]。" if citations else ""
            return _response(
                [
                    {
                        "type": "text",
                        "text": "已修复 pricing.py：百分比先除以 100，再计算折扣。"
                        "补丁经人工审批后应用；AST 语法检查通过。容器测试状态："
                        + sandbox.get("code", "未返回结果")
                        + "。"
                        "若状态不是 passed，不能宣称单元测试通过。" + evidence,
                    }
                ]
            )
        if not any("content" in result and result.get("path") == "pricing.py" for result in results):
            return _response(
                [
                    _call("repo_list"),
                    _call("repo_read", path="pricing.py"),
                    _call("knowledge_search", query="coding agent discount verification 安全 审批"),
                ]
            )
        read = next(
            result for result in results if result.get("path") == "pricing.py" and "content" in result
        )
        if "return subtotal * (1 - discount_percent)" not in read["content"]:
            return _response([{"type": "text", "text": "示例中的原始错误表达式已不存在，无需重复应用补丁。"}])
        if not any(result.get("requires_approval") for result in results):
            return _response(
                [
                    _call(
                        "propose_patch",
                        path="pricing.py",
                        old_text="return subtotal * (1 - discount_percent)",
                        new_text="return subtotal * (1 - discount_percent / 100)",
                    )
                ]
            )
        return _response([{"type": "text", "text": "等待你审批补丁。"}])

    def _complete_multi_file(self, messages: list[dict], results: list[dict]) -> dict:
        decisions = [
            m.get("content", "")
            for m in messages[1:]
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        harness_decisions = []
        for text in decisions:
            if text.startswith("HARNESS_STATE\n"):
                try:
                    harness_decisions = json.loads(text.split("\n", 1)[1]).get("decisions", [])
                except (ValueError, AttributeError):
                    pass
        latest = harness_decisions[-1].get("decision") if harness_decisions else None
        if latest == "rejected" or any(text.startswith("REVIEW_REJECTED: ") for text in decisions):
            return _response(
                [{"type": "text", "text": "多文件变更集已被用户拒绝，Git worktree 保持基线原状。"}]
            )
        if latest == "approved" or any(text.startswith("REVIEW_APPROVED: ") for text in decisions):
            sandbox = next(
                (
                    r
                    for r in results
                    if r.get("code") in {"passed", "tests_failed"}
                    or str(r.get("code", "")).startswith("sandbox_")
                ),
                None,
            )
            if sandbox is None:
                return _response(
                    [
                        _call("working_memory_read"),
                        _call("verify_python", path="discounts.py"),
                        _call("verify_python", path="checkout.py"),
                        _call("sandbox_test"),
                    ]
                )
            wm_audit = next((r for r in reversed(results) if "stale_count" in r), {})
            commit_note = (
                f" 已自动创建受控 Git 提交 {sandbox['git_commit'][:12]}。"
                if sandbox.get("git_commit")
                else ""
            )
            stale_note = (
                f" 工作记忆自动将 {wm_audit['stale_count']} 条修改前源码事实降级为 stale。"
                if wm_audit.get("stale_count")
                else ""
            )
            return _response(
                [
                    {
                        "type": "text",
                        "text": "已完成跨文件修复：在 discounts.py 补齐 max_discount_cents 封顶，"
                        "并在 checkout.py 修正折后总额为 subtotal - discount。"
                        f"容器验收测试状态：{sandbox.get('code', 'unknown')}。{commit_note}{stale_note}",
                    }
                ]
            )

        if not any("map" in r and "indexed_files" in r for r in results):
            return _response(
                [
                    _call("repo_map", max_chars=2000, focus_query="checkout discount coupon"),
                    _call("symbol_search", symbol="compute_discount_cents"),
                    _call("repo_git_status"),
                ]
            )
        discounts_read = next(
            (r for r in results if r.get("path") == "discounts.py" and "content" in r), None
        )
        checkout_read = next(
            (r for r in results if r.get("path") == "checkout.py" and "content" in r), None
        )
        if not discounts_read or not checkout_read:
            return _response(
                [
                    _call("repo_read", path="discounts.py"),
                    _call("repo_read", path="checkout.py"),
                    _call("knowledge_search", query="patch verification 审批 测试"),
                ]
            )
        if "return raw_discount" not in discounts_read["content"] or (
            "total = discount if coupon is not None else subtotal" not in checkout_read["content"]
        ):
            return _response([{"type": "text", "text": "目标跨文件缺陷已被修复，无需重复提交变更集。"}])
        if not any(r.get("requires_approval") for r in results):
            return _response(
                [
                    _call(
                        "working_memory_write",
                        goal="协调修复 discounts.py 封顶限制与 checkout.py 折后应付总额计算",
                        constraints=[
                            "保持 models.py 与受保护的 tests/test_checkout.py 不变",
                            "通过原子多文件变更集同时提交两个模块，避免中间态不一致",
                        ],
                        todos=[
                            {"id": "cap", "task": "在 discounts.py 应用 max_discount_cents 上限", "status": "in_progress"},
                            {"id": "total", "task": "在 checkout.py 改为 subtotal - discount", "status": "in_progress"},
                        ],
                        facts=[
                            {
                                "id": "fact_discounts",
                                "claim": "修改前 compute_discount_cents 直接返回 raw_discount，未截断 max_discount_cents",
                                "source_type": "file",
                                "source_ref": "discounts.py",
                                "source_sha256": discounts_read["sha256"],
                            },
                            {
                                "id": "fact_checkout",
                                "claim": "修改前 build_receipt 将返回的减免分值直接赋给了 total",
                                "source_type": "file",
                                "source_ref": "checkout.py",
                                "source_sha256": checkout_read["sha256"],
                            },
                        ],
                    ),
                    _call(
                        "propose_changeset",
                        summary="修复优惠封顶与结算应付总额跨文件契约缺陷",
                        edits=[
                            {
                                "path": "discounts.py",
                                "old_text": "    # 缺陷 1：忽略了 coupon.max_discount_cents 封顶限制\n    return raw_discount",
                                "new_text": "    if coupon.max_discount_cents is not None:\n        return min(raw_discount, coupon.max_discount_cents)\n    return raw_discount",
                            },
                            {
                                "path": "checkout.py",
                                "old_text": "    # 缺陷 2：把返回的减免金额误当成折后总额，导致 discount_cents 与 total_cents 颠倒\n    total = discount if coupon is not None else subtotal\n    applied_discount = subtotal - total",
                                "new_text": "    applied_discount = min(subtotal, discount)\n    total = subtotal - applied_discount",
                            },
                        ],
                    ),
                ]
            )
        return _response([{"type": "text", "text": "等待你审批多文件原子变更集。"}])


class AnthropicProvider:
    """SDK 做有限网络重试和请求超时；凭证不进入 prompt / SQLite / 事件。"""

    supports_cache_hints = True

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic

        model = os.environ.get("ANTHROPIC_MODEL", "").strip()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        if (not api_key and not auth_token) or not model:
            raise ValueError("真实模式需要 ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN）与 ANTHROPIC_MODEL")
        self.model = model
        base_url = "https://api.anthropic.com"
        if os.environ.get("AGENT_ALLOW_CUSTOM_BASE_URL") == "1":
            custom_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
            if custom_url:
                base_url = custom_url
        kwargs: dict[str, Any] = {"max_retries": 2, "timeout": 45.0, "base_url": base_url}
        if api_key:
            kwargs["api_key"] = api_key
        else:
            kwargs["auth_token"] = auth_token
        self.client = AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self.client.close()

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        tail_state_count: int = 0,
    ) -> dict:
        del cache_key  # Anthropic 通过显式 cache_control 断点命中前缀缓存
        cached_req = build_anthropic_cached_payload(
            system, messages, tools, max_tokens=max_tokens, tail_state_count=tail_state_count
        )
        response = await self.client.messages.create(model=self.model, **cached_req)
        content = [block.model_dump(exclude_none=True) for block in response.content]
        if any(block["type"] not in {"text", "tool_use"} for block in content):
            raise ValueError("模型返回当前适配器不支持的内容块")
        usage: dict[str, int] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        cache_read = getattr(response.usage, "cache_read_input_tokens", None)
        cache_write = getattr(response.usage, "cache_creation_input_tokens", None)
        if cache_read is not None:
            usage["cache_read_input_tokens"] = int(cache_read)
        if cache_write is not None:
            usage["cache_creation_input_tokens"] = int(cache_write)
        return {
            "content": content,
            "stop_reason": response.stop_reason,
            "usage": usage,
        }


class OpenAICompatibleProvider:
    """兼容 OpenAI Chat Completions 工具调用协议的真实模型适配器。"""

    supports_cache_hints = True

    def __init__(self) -> None:
        import httpx

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("OPENAI_MODEL", "").strip()
        if not api_key or not model:
            raise ValueError("OpenAI 兼容模式需要 OPENAI_API_KEY 与 OPENAI_MODEL")
        base_url = "https://api.openai.com/v1"
        if os.environ.get("AGENT_ALLOW_CUSTOM_BASE_URL") == "1":
            custom_url = os.environ.get("OPENAI_BASE_URL", "").strip()
            if custom_url:
                base_url = custom_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=45.0,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg["role"]
            content = msg.get("content")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant" and isinstance(content, list):
                texts = [b["text"] for b in content if b.get("type") == "text"]
                calls = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": canonical_json(b["input"]),
                        },
                    }
                    for b in content
                    if b.get("type") == "tool_use"
                ]
                entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
            elif role == "user" and isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": str(block.get("content", "")),
                            }
                        )
                    elif block.get("type") == "text":
                        out.append({"role": "user", "content": block["text"]})
        return out

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        tail_state_count: int = 0,
    ) -> dict:
        del tail_state_count  # OpenAI / vLLM 基于前缀块自动匹配，尾部瞬态布局已由 ContextManager 保证
        norm_tools = canonical_tools(tools)
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for spec in norm_tools
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if openai_tools:
            payload["tools"] = openai_tools
        if cache_key:
            payload["prompt_cache_key"] = cache_key
        resp = await self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        content: list[dict] = []
        if message.get("content"):
            content.append({"type": "text", "text": str(message["content"])})
        for tc in message.get("tool_calls") or []:
            raw_args = tc.get("function", {}).get("arguments") or "{}"
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": parsed_args,
                }
            )
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        if choice.get("finish_reason") == "length" and stop_reason == "end_turn":
            stop_reason = "max_tokens"
        usage = data.get("usage") or {}
        parsed_usage: dict[str, int] = {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
        }
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            parsed_usage["cache_read_input_tokens"] = int(details["cached_tokens"])
        return {
            "content": content,
            "stop_reason": stop_reason,
            "usage": parsed_usage,
        }


def get_provider(name: str) -> Provider:
    if name == "demo":
        return DemoProvider()
    if name == "anthropic":
        return AnthropicProvider()
    if name in {"openai", "openai_compat"}:
        return OpenAICompatibleProvider()
    raise ValueError(f"未知 provider: {name}")

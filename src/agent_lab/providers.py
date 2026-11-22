"""模型适配层：运行时只处理标准化 content / usage。

DemoProvider 是确定性脚本，用来复现状态机和故障，不能代表 LLM 能力。
AnthropicProvider 与 OpenAICompatibleProvider 由真实模型决定下一步工具；
三者共享同一工具执行、审批、修复循环和恢复路径。
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import email.utils
import json
import os
import random
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .context import estimate_bpe_tokens
from .kv_cache import build_anthropic_cached_payload, canonical_json, canonical_tools

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_STRICT_NO_CACHE_KEY_HOSTS = (
    "mistral.ai",
    "groq.com",
    "together.xyz",
    "fireworks.ai",
    "cohere.com",
    "perplexity.ai",
)


def _parse_positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    val = int(raw)
    return val if val > 0 else None


class ProviderRateLimiter:
    """线程安全与异步安全的 RPM / TPM 滑动窗口准入限流器。

    当 AGENT_LAB_RPM_LIMIT 与 AGENT_LAB_TPM_LIMIT 均未配置时零休眠直通；
    请求发出前通过 reserve(estimated_tokens) 预留配额，响应返回后通过
    record_actual(actual_tokens, reserved_tokens) 回填实际消耗差额。
    """

    def __init__(
        self,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.rpm_limit = (
            _parse_positive_int_env("AGENT_LAB_RPM_LIMIT")
            if rpm_limit is None
            else (int(rpm_limit) if int(rpm_limit) > 0 else None)
        )
        self.tpm_limit = (
            _parse_positive_int_env("AGENT_LAB_TPM_LIMIT")
            if tpm_limit is None
            else (int(tpm_limit) if int(tpm_limit) > 0 else None)
        )
        self.window_seconds = float(window_seconds)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._lock = threading.Lock()
        self._request_timestamps: deque[float] = deque()
        self._token_ledger: deque[list[float | int]] = deque()

    @property
    def enabled(self) -> bool:
        return self.rpm_limit is not None or self.tpm_limit is not None

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._request_timestamps and self._request_timestamps[0] <= cutoff:
            self._request_timestamps.popleft()
        while self._token_ledger and float(self._token_ledger[0][0]) <= cutoff:
            self._token_ledger.popleft()

    def _try_acquire(self, estimated: int) -> float:
        with self._lock:
            now = self._clock()
            self._prune(now)
            wait_rpm = 0.0
            if self.rpm_limit is not None and len(self._request_timestamps) >= self.rpm_limit:
                wait_rpm = max(1e-4, (self._request_timestamps[0] + self.window_seconds) - now)

            wait_tpm = 0.0
            if self.tpm_limit is not None and estimated > 0:
                used_tokens = sum(int(item[1]) for item in self._token_ledger)
                if used_tokens > 0 and used_tokens + estimated > self.tpm_limit:
                    needed_to_free = (used_tokens + estimated) - self.tpm_limit
                    freed = 0
                    wait_until = now
                    for ts, tok in self._token_ledger:
                        freed += int(tok)
                        wait_until = float(ts) + self.window_seconds
                        if freed >= needed_to_free:
                            break
                    wait_tpm = max(1e-4, wait_until - now)

            wait_sec = max(wait_rpm, wait_tpm)
            if wait_sec > 0.0:
                return wait_sec

            if self.rpm_limit is not None:
                self._request_timestamps.append(now)
            if self.tpm_limit is not None and estimated > 0:
                self._token_ledger.append([now, estimated])
            return 0.0

    async def reserve(self, estimated_tokens: int = 0) -> int:
        estimated = max(0, int(estimated_tokens or 0))
        if not self.enabled:
            return estimated
        while True:
            wait_sec = self._try_acquire(estimated)
            if wait_sec <= 0.0:
                return estimated
            await self._sleep(wait_sec)

    def reserve_sync(self, estimated_tokens: int = 0) -> int:
        estimated = max(0, int(estimated_tokens or 0))
        if not self.enabled:
            return estimated
        while True:
            wait_sec = self._try_acquire(estimated)
            if wait_sec <= 0.0:
                return estimated
            time.sleep(wait_sec)

    def record_actual(self, actual_tokens: int, reserved_tokens: int = 0) -> None:
        if self.tpm_limit is None:
            return
        actual = max(0, int(actual_tokens or 0))
        reserved = max(0, int(reserved_tokens or 0))
        delta = actual - reserved
        if delta == 0:
            return
        with self._lock:
            now = self._clock()
            self._prune(now)
            if delta > 0:
                self._token_ledger.append([now, delta])
                return
            refund = -delta
            for entry in reversed(self._token_ledger):
                if int(entry[1]) == reserved and refund <= reserved:
                    entry[1] = int(entry[1]) - refund
                    if int(entry[1]) <= 0:
                        self._token_ledger.remove(entry)
                    return
            for idx in range(len(self._token_ledger) - 1, -1, -1):
                if refund <= 0:
                    break
                avail = int(self._token_ledger[idx][1])
                take = min(avail, refund)
                self._token_ledger[idx][1] = avail - take
                refund -= take
            self._token_ledger = deque(item for item in self._token_ledger if int(item[1]) > 0)


def compute_usage_totals(usage: dict[str, Any] | None, *, additive_cache: bool = False) -> dict[str, int]:
    """统一折算总输入 token、加权计费输入 token 与有效预算消耗 token。

    - Anthropic（additive_cache=True 或含 cache_creation_input_tokens）：
      input_tokens 仅统计未命中缓存尾部，需与 cache_creation_input_tokens、cache_read_input_tokens 相加。
    - OpenAI（additive_cache=False）：
      input_tokens（即 prompt_tokens）已包含 cache_read_input_tokens（即 cached_tokens）。
    """
    data = usage or {}
    out = max(0, int(data.get("output_tokens", 0) or 0))
    if additive_cache or "cache_creation_input_tokens" in data:
        uncached_input = max(0, int(data.get("input_tokens", 0) or 0))
        cache_write = max(0, int(data.get("cache_creation_input_tokens", 0) or 0))
        cache_read = max(0, int(data.get("cache_read_input_tokens", 0) or 0))
        total_input = uncached_input + cache_write + cache_read
        billed_input = uncached_input + int(round(1.25 * cache_write)) + int(round(0.10 * cache_read))
    else:
        total_input = max(0, int(data.get("input_tokens", 0) or 0))
        cache_read = min(total_input, max(0, int(data.get("cache_read_input_tokens", 0) or 0)))
        uncached_input = max(0, total_input - cache_read)
        cache_write = 0
        billed_input = uncached_input + int(round(0.25 * cache_read))
    return {
        "total_input_tokens": total_input,
        "billed_input_tokens": billed_input,
        "output_tokens": out,
        "effective_budget_tokens": max(total_input, billed_input) + out,
    }


def _collect_historical_tool_ids(messages: list[dict]) -> set[str]:
    ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                call_id = block.get("id")
                if isinstance(call_id, str) and call_id:
                    ids.add(call_id)
    return ids


def _normalize_tool_call_id(raw_id: Any, unavailable_ids: set[str]) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw_id or ""))[:96]
    if not cleaned:
        cleaned = f"call_{uuid.uuid4().hex[:12]}"
    candidate = cleaned
    suffix_idx = 2
    while candidate in unavailable_ids:
        suffix = f"_{suffix_idx}"
        candidate = f"{cleaned[: max(1, 96 - len(suffix))]}{suffix}"
        suffix_idx += 1
    unavailable_ids.add(candidate)
    return candidate


def _parse_retry_after(header_value: str | None, *, max_delay: float = 5.0) -> float | None:
    if not header_value:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        return min(max_delay, max(0.0, seconds))
    except ValueError:
        pass
    try:
        parsed_dt = email.utils.parsedate_to_datetime(raw)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        delta = (parsed_dt - datetime.now(timezone.utc)).total_seconds()
        return min(max_delay, max(0.0, delta))
    except (TypeError, ValueError, OverflowError):
        return None


def _estimate_request_tokens(system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> int:
    return (
        estimate_bpe_tokens(system)
        + estimate_bpe_tokens(tools)
        + estimate_bpe_tokens(messages)
        + min(max(0, int(max_tokens)), 1024)
    )


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

    cache_usage_additive = False

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
    accepts_historical_tool_ids = True
    cache_usage_additive = True
    supports_vision = True
    supports_dreaming = True

    def __init__(self, *, thinking: dict[str, Any] | None = None) -> None:
        from anthropic import AsyncAnthropic

        model = os.environ.get("ANTHROPIC_MODEL", "").strip()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        if (not api_key and not auth_token) or not model:
            raise ValueError("真实模式需要 ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN）与 ANTHROPIC_MODEL")
        self.model = model
        if thinking is not None:
            self.thinking: dict[str, Any] | None = deepcopy(thinking)
        else:
            budget_raw = os.environ.get("ANTHROPIC_THINKING_BUDGET", "").strip()
            self.thinking = (
                {"type": "enabled", "budget_tokens": int(budget_raw)}
                if budget_raw and int(budget_raw) >= 1024
                else None
            )
        self.rate_limiter = ProviderRateLimiter()
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
        thinking: dict[str, Any] | None = None,
        historical_tool_ids: set[str] | None = None,
        vision_attachments: list[dict[str, Any]] | None = None,
    ) -> dict:
        del cache_key  # Anthropic 通过显式 cache_control 断点命中前缀缓存
        effective_thinking = thinking if thinking is not None else getattr(self, "thinking", None)
        if isinstance(effective_thinking, dict) and effective_thinking.get("type") == "enabled":
            budget = int(effective_thinking.get("budget_tokens", 1024))
            if max_tokens <= 1024 and thinking is None:
                effective_thinking = None
            elif budget >= max_tokens and max_tokens > 1024:
                effective_thinking = {**effective_thinking, "budget_tokens": max_tokens - 1}

        cached_req = build_anthropic_cached_payload(
            system,
            messages,
            tools,
            max_tokens=max_tokens,
            tail_state_count=tail_state_count,
            thinking=effective_thinking,
        )
        if vision_attachments:
            img_blocks: list[dict[str, Any]] = []
            for att in vision_attachments:
                b64 = att.get("base64_data")
                if not isinstance(b64, str) or not b64:
                    continue
                img_blocks.append(
                    {
                        "type": "text",
                        "text": f"[最新桌面截图 {att.get('artifact_name', 'screen.png')} ({att.get('width')}x{att.get('height')})]",
                    }
                )
                img_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": str(att.get("media_type", "image/png")),
                            "data": b64,
                        },
                    }
                )
            if img_blocks:
                if cached_req["messages"] and cached_req["messages"][-1]["role"] == "user":
                    cached_req["messages"][-1]["content"].extend(img_blocks)
                else:
                    cached_req["messages"].append({"role": "user", "content": img_blocks})
        limiter = getattr(self, "rate_limiter", None)
        reserved_tokens = 0
        if limiter is not None and limiter.enabled:
            reserved_tokens = await limiter.reserve(
                _estimate_request_tokens(system, messages, tools, max_tokens)
            )
        try:
            response = await self.client.messages.create(model=self.model, **cached_req)
        except Exception:
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise

        raw_blocks = getattr(response, "content", None)
        if raw_blocks is None or not isinstance(raw_blocks, (list, tuple)):
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise ValueError("模型返回损坏的空响应（缺少 content 列表）")

        is_truncated = getattr(response, "stop_reason", None) == "max_tokens"
        unavailable_ids = _collect_historical_tool_ids(messages)
        if historical_tool_ids:
            unavailable_ids.update(historical_tool_ids)
        content: list[dict[str, Any]] = []

        for idx, blk in enumerate(raw_blocks):
            if hasattr(blk, "model_dump"):
                dumped = blk.model_dump(exclude_none=True)
            elif isinstance(blk, dict):
                dumped = dict(blk)
            else:
                raise ValueError("模型返回无法解析的内容块")
            btype = dumped.get("type")
            if btype == "thinking":
                t_block: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": str(dumped.get("thinking", "")),
                }
                if dumped.get("signature") is not None:
                    t_block["signature"] = str(dumped["signature"])
                content.append(t_block)
            elif btype == "redacted_thinking":
                if dumped.get("data") is not None:
                    content.append({"type": "redacted_thinking", "data": str(dumped["data"])})
            elif btype == "text":
                text_val = dumped.get("text")
                if isinstance(text_val, str) and text_val:
                    content.append({"type": "text", "text": text_val})
            elif btype == "tool_use":
                raw_name = dumped.get("name")
                if not isinstance(raw_name, str) or not (1 <= len(raw_name.strip()) <= 64):
                    if is_truncated:
                        continue
                    raise ValueError("模型返回的 tool_use 缺少合法工具名")
                norm_id = _normalize_tool_call_id(dumped.get("id"), unavailable_ids)
                raw_input = dumped.get("input")
                is_last_block = idx == len(raw_blocks) - 1
                if isinstance(raw_input, str):
                    try:
                        parsed_input = json.loads(raw_input)
                    except (json.JSONDecodeError, TypeError):
                        parsed_input = {"__truncated_or_invalid_json__": raw_input[:240]}
                    if not isinstance(parsed_input, dict):
                        parsed_input = {"__truncated_or_invalid_json__": raw_input[:240]}
                elif isinstance(raw_input, dict):
                    if is_truncated and is_last_block and not raw_input:
                        parsed_input = {"__truncated_or_invalid_json__": "<truncated_at_max_tokens>"}
                    else:
                        parsed_input = raw_input
                else:
                    parsed_input = {
                        "__truncated_or_invalid_json__": str(
                            raw_input if raw_input is not None else "<truncated_at_max_tokens>"
                        )[:240]
                    }
                content.append(
                    {
                        "type": "tool_use",
                        "id": norm_id,
                        "name": raw_name.strip(),
                        "input": parsed_input,
                    }
                )
            else:
                # 忽略良性服务端元数据块（如 citations / server_tool_use），不中断主流程
                continue

        has_tool_use = any(b["type"] == "tool_use" for b in content)
        has_text = any(b["type"] == "text" and b.get("text") for b in content)
        if is_truncated:
            if has_tool_use:
                stop_reason = "tool_use"
            else:
                if not has_text:
                    content.append(
                        {"type": "text", "text": "[输出因达到 max_tokens 上限被截断，请分步继续]"}
                    )
                stop_reason = "max_tokens"
        else:
            if not content or (not has_tool_use and not has_text):
                if limiter is not None and limiter.enabled:
                    limiter.record_actual(0, reserved_tokens)
                raise ValueError("模型返回空内容或不包含有效文本/工具调用")
            stop_reason = "tool_use" if has_tool_use else (response.stop_reason or "end_turn")

        usage: dict[str, int] = {
            "input_tokens": int(getattr(response.usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response.usage, "output_tokens", 0) or 0),
        }
        cache_read = getattr(response.usage, "cache_read_input_tokens", None)
        cache_write = getattr(response.usage, "cache_creation_input_tokens", None)
        if cache_read is not None:
            usage["cache_read_input_tokens"] = int(cache_read)
        if cache_write is not None:
            usage["cache_creation_input_tokens"] = int(cache_write)

        if limiter is not None and limiter.enabled:
            totals = compute_usage_totals(usage, additive_cache=True)
            limiter.record_actual(totals["total_input_tokens"] + totals["output_tokens"], reserved_tokens)

        result: dict[str, Any] = {
            "content": content,
            "stop_reason": stop_reason,
            "usage": usage,
        }
        if is_truncated:
            result["truncated"] = True
        return result


class OpenAICompatibleProvider:
    """兼容 OpenAI Chat Completions 工具调用协议的真实模型适配器。"""

    supports_cache_hints = True
    accepts_historical_tool_ids = True
    cache_usage_additive = False
    supports_vision = True
    supports_dreaming = True

    def __init__(
        self,
        *,
        max_retries: int = 2,
        stream: bool | None = None,
        fallback_base_urls: list[str] | None = None,
        base_retry_delay: float = 0.25,
        max_retry_delay: float = 5.0,
    ) -> None:
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
        raw_fallbacks = os.environ.get("OPENAI_FALLBACK_BASE_URLS", "").strip()
        self.fallback_base_urls: list[str] = (
            [u.strip().rstrip("/") for u in fallback_base_urls if u and u.strip()]
            if fallback_base_urls is not None
            else [u.strip().rstrip("/") for u in raw_fallbacks.split(",") if u.strip()]
        )
        self.max_retries = max(0, int(max_retries))
        self.stream = bool(stream) if stream is not None else (os.environ.get("OPENAI_STREAM") == "1")
        self.base_retry_delay = max(0.0, float(base_retry_delay))
        self.max_retry_delay = max(0.05, float(max_retry_delay))
        self._supports_prompt_cache_key: bool | None = None
        self.rate_limiter = ProviderRateLimiter()
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=45.0,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _should_send_prompt_cache_key(self, target_base_url: str) -> bool:
        if getattr(self, "_supports_prompt_cache_key", None) is False:
            return False
        if os.environ.get("OPENAI_SEND_PROMPT_CACHE_KEY") == "0":
            return False
        lowered = target_base_url.lower()
        return not any(host in lowered for host in _STRICT_NO_CACHE_KEY_HOSTS)

    @staticmethod
    def _append_user_text(out: list[dict[str, Any]], text: str) -> None:
        if out and out[-1].get("role") == "user" and isinstance(out[-1].get("content"), str):
            prev = out[-1]["content"]
            if text:
                out[-1]["content"] = f"{prev}\n\n{text}" if prev else text
        else:
            out.append({"role": "user", "content": text})

    @classmethod
    def _to_openai_messages(cls, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg["role"]
            content = msg.get("content")
            if isinstance(content, str):
                if role == "user":
                    cls._append_user_text(out, content)
                else:
                    out.append({"role": role, "content": content})
                continue
            if role == "assistant" and isinstance(content, list):
                # 跳过 thinking / redacted_thinking 块，避免非标准字段触发兼容网关 400
                texts = [
                    str(b["text"])
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
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
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                if not texts and not calls:
                    continue
                entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
            elif role == "user" and isinstance(content, list):
                # 先输出紧邻 assistant.tool_calls 的 tool 结果，再合并普通 user 文本块
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": str(block.get("content", "")),
                            }
                        )
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        cls._append_user_text(out, str(block.get("text", "")))
        return out

    def _compute_backoff(self, attempt: int, retry_after_header: str | None) -> float:
        max_delay = getattr(self, "max_retry_delay", 5.0)
        parsed_ra = _parse_retry_after(retry_after_header, max_delay=max_delay)
        if parsed_ra is not None:
            return parsed_ra
        base_delay = getattr(self, "base_retry_delay", 0.25)
        jitter = random.uniform(0.75, 1.25)
        return min(max_delay, base_delay * (2**attempt) * jitter)

    async def _send_sse_and_assemble(self, endpoint_url: str, req_payload: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
        text_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_data: dict[str, Any] = {}
        has_any_choice = False

        async with self.client.stream("POST", endpoint_url, json=req_payload) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                return resp, None
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                chunk = json.loads(data_str)
                if not isinstance(chunk, dict):
                    continue
                chunk_usage = chunk.get("usage")
                if isinstance(chunk_usage, dict) and chunk_usage:
                    usage_data = chunk_usage
                chunk_choices = chunk.get("choices") or []
                if not chunk_choices:
                    continue
                has_any_choice = True
                c0 = chunk_choices[0] if isinstance(chunk_choices[0], dict) else {}
                if c0.get("finish_reason"):
                    finish_reason = str(c0["finish_reason"])
                delta = c0.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                delta_text = delta.get("content")
                if isinstance(delta_text, str) and delta_text:
                    text_parts.append(delta_text)
                for tc_delta in delta.get("tool_calls") or []:
                    if not isinstance(tc_delta, dict):
                        continue
                    idx = int(tc_delta.get("index", 0) or 0)
                    slot = tool_calls_by_index.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc_delta.get("id") and not slot["id"]:
                        slot["id"] = str(tc_delta["id"])
                    fn_delta = tc_delta.get("function") or {}
                    if isinstance(fn_delta, dict):
                        if fn_delta.get("name"):
                            slot["function"]["name"] += str(fn_delta["name"])
                        if isinstance(fn_delta.get("arguments"), str):
                            slot["function"]["arguments"] += fn_delta["arguments"]

            assembled_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
            assembled_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts) if text_parts else None,
            }
            if assembled_calls:
                assembled_msg["tool_calls"] = assembled_calls
            assembled_data: dict[str, Any] = {
                "choices": (
                    [
                        {
                            "finish_reason": finish_reason
                            or ("tool_calls" if assembled_calls else "stop"),
                            "message": assembled_msg,
                        }
                    ]
                    if (has_any_choice or text_parts or assembled_calls)
                    else []
                ),
                "usage": usage_data,
            }
            return resp, assembled_data

    async def _post_with_resilience(
        self, base_payload: dict[str, Any], *, cache_key: str | None
    ) -> dict[str, Any]:
        import httpx

        primary_base = str(self.client.base_url).rstrip("/")
        fallbacks = list(getattr(self, "fallback_base_urls", None) or [])
        env_fallbacks = [
            u.strip().rstrip("/")
            for u in os.environ.get("OPENAI_FALLBACK_BASE_URLS", "").split(",")
            if u.strip()
        ]
        for fb in env_fallbacks:
            if fb not in fallbacks:
                fallbacks.append(fb)

        endpoints: list[tuple[str, str]] = [("/chat/completions", primary_base)] + [
            (f"{fb}/chat/completions", fb) for fb in fallbacks if fb != primary_base
        ]
        max_retries = max(0, int(getattr(self, "max_retries", 2)))
        use_stream = bool(getattr(self, "stream", False)) or os.environ.get("OPENAI_STREAM") == "1"
        last_exc: Exception | None = None

        for ep_idx, (ep_url, ep_base) in enumerate(endpoints):
            has_next_endpoint = ep_idx + 1 < len(endpoints)
            req_payload = dict(base_payload)
            if cache_key and self._should_send_prompt_cache_key(ep_base):
                req_payload["prompt_cache_key"] = cache_key
            if use_stream:
                req_payload["stream"] = True
                req_payload["stream_options"] = {"include_usage": True}

            attempt = 0
            while attempt <= max_retries:
                try:
                    if use_stream:
                        resp, stream_data = await self._send_sse_and_assemble(ep_url, req_payload)
                    else:
                        resp = await self.client.post(ep_url, json=req_payload)
                        stream_data = None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        await asyncio.sleep(self._compute_backoff(attempt, None))
                        attempt += 1
                        continue
                    if has_next_endpoint:
                        break
                    raise

                if resp.status_code in {400, 422} and "prompt_cache_key" in req_payload:
                    self._supports_prompt_cache_key = False
                    req_payload = {k: v for k, v in req_payload.items() if k != "prompt_cache_key"}
                    continue

                if use_stream and resp.status_code in {400, 422} and "stream_options" in req_payload:
                    req_payload = {k: v for k, v in req_payload.items() if k != "stream_options"}
                    continue

                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                        await asyncio.sleep(self._compute_backoff(attempt, retry_after))
                        attempt += 1
                        continue
                    if has_next_endpoint:
                        break
                    resp.raise_for_status()

                resp.raise_for_status()
                return stream_data if stream_data is not None else resp.json()

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("所有配置的 OpenAI 兼容网关均不可用")

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        tail_state_count: int = 0,
        historical_tool_ids: set[str] | None = None,
        vision_attachments: list[dict[str, Any]] | None = None,
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
        openai_messages = self._to_openai_messages(system, messages)
        if vision_attachments:
            parts: list[dict[str, Any]] = []
            for att in vision_attachments:
                b64 = att.get("base64_data")
                if not isinstance(b64, str) or not b64:
                    continue
                mime = str(att.get("media_type", "image/png"))
                parts.append(
                    {
                        "type": "text",
                        "text": f"[最新桌面截图 {att.get('artifact_name', 'screen.png')} ({att.get('width')}x{att.get('height')})]",
                    }
                )
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    }
                )
            if parts:
                if openai_messages and openai_messages[-1].get("role") == "user":
                    prev_content = openai_messages[-1].get("content")
                    if isinstance(prev_content, str) and prev_content:
                        openai_messages[-1]["content"] = [{"type": "text", "text": prev_content}, *parts]
                    elif isinstance(prev_content, list):
                        prev_content.extend(parts)
                    else:
                        openai_messages[-1]["content"] = parts
                else:
                    openai_messages.append({"role": "user", "content": parts})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
        }
        if openai_tools:
            payload["tools"] = openai_tools

        limiter = getattr(self, "rate_limiter", None)
        reserved_tokens = 0
        if limiter is not None and limiter.enabled:
            reserved_tokens = await limiter.reserve(
                _estimate_request_tokens(system, messages, tools, max_tokens)
            )
        try:
            data = await self._post_with_resilience(payload, cache_key=cache_key)
        except Exception:
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise

        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise ValueError("OpenAI 兼容网关返回空 choices")

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        is_length_truncated = finish_reason == "length"
        message = choice.get("message") or {}
        unavailable_ids = _collect_historical_tool_ids(messages)
        if historical_tool_ids:
            unavailable_ids.update(historical_tool_ids)

        content: list[dict[str, Any]] = []
        if message.get("content"):
            content.append({"type": "text", "text": str(message["content"])})
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            fn_name = str(fn.get("name") or "").strip()
            if not fn_name:
                if is_length_truncated:
                    continue
                fn_name = "unknown_tool"
            norm_id = _normalize_tool_call_id(tc.get("id"), unavailable_ids)
            raw_args = fn.get("arguments")
            if raw_args is None or raw_args == "":
                if is_length_truncated:
                    parsed_args: Any = {"__truncated_or_invalid_json__": "<truncated_empty_arguments>"}
                else:
                    parsed_args = {}
            elif isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    parsed_args = {"__truncated_or_invalid_json__": raw_args[:240]}
            else:
                parsed_args = raw_args
            if not isinstance(parsed_args, dict):
                parsed_args = {"__truncated_or_invalid_json__": str(raw_args)[:240]}
            content.append(
                {
                    "type": "tool_use",
                    "id": norm_id,
                    "name": fn_name[:64],
                    "input": parsed_args,
                }
            )

        has_calls = any(b["type"] == "tool_use" for b in content)
        stop_reason = "tool_use" if has_calls else "end_turn"
        if is_length_truncated and not has_calls:
            stop_reason = "max_tokens"
            if not content:
                content.append({"type": "text", "text": "[输出因达到 max_tokens 上限被截断]"})

        usage = data.get("usage") or {}
        parsed_usage: dict[str, int] = {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        }
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            parsed_usage["cache_read_input_tokens"] = int(details["cached_tokens"])

        if limiter is not None and limiter.enabled:
            totals = compute_usage_totals(parsed_usage, additive_cache=False)
            limiter.record_actual(totals["total_input_tokens"] + totals["output_tokens"], reserved_tokens)

        return {
            "content": content,
            "stop_reason": stop_reason,
            "usage": parsed_usage,
            "truncated": is_length_truncated,
        }


def get_provider(name: str) -> Provider:
    if name == "demo":
        return DemoProvider()
    if name == "anthropic":
        return AnthropicProvider()
    if name in {"openai", "openai_compat"}:
        return OpenAICompatibleProvider()
    raise ValueError(f"未知 provider: {name}")

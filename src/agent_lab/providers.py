"""模型适配层：屏蔽多厂商协议差异，使 AgentRuntime 只处理标准化的 content / stop_reason / usage。

核心工程不变量与设计取舍：
1. 确定性脚本与真实生成模型的能力边界隔离（supports_dreaming / supports_vision）：
   - DemoProvider 仅用于离线复现工具闭环、审批门控、多文件原子变更与崩溃恢复，故意不声明
     supports_dreaming = True；否则会话结束后的自动 Dreaming 若向脚本化/测试 Mock Provider 发送
     开放式 Wiki 提炼请求，不仅无法产出合法 JSON，还会错位消耗单测预设响应队列破坏确定性断言。
   - AnthropicProvider 与 OpenAICompatibleProvider 显式开启 supports_vision 与 supports_dreaming，
     驱动多模态桌面 GUI 视觉闭环与离线睡眠记忆合成。
2. 瞬态视觉附件（vision_attachments）为什么必须挂载在尾部第 3 缓存断点之后：
   - 若把动辄数 MB 的桌面截图 Base64 持久写入 SQLite messages，几轮点击就会撑爆上下文水位线，
     迫使历史消息频繁裁剪并导致前缀 KV Cache 全量失效；
   - 本层仅在当轮请求发出前，将最新截图作为瞬态块追加在合并后的最末条 user 消息末尾（位于
     build_anthropic_cached_payload 第 3 处 cache_control 断点、即稳定历史与瞬态 HARNESS_STATE 分界点之后）。
     这既守住了 Anthropic 严格的 user/assistant 角色交替约束，又保证下一轮截图更新时前序历史前缀
     100% 字节级命中缓存。
3. 截断与畸形工具调用的协议韧性：
   - 当输出触及 max_tokens 导致半截 JSON 参数时，适配器将其封装为 {"__truncated_or_invalid_json__": ...}
     而非直接抛异常中断任务，交由下游 JSON Schema 校验器转化为结构化错误回执供模型下一轮自愈重试。
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import email.utils
import hashlib
import inspect
import json
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from .context import estimate_bpe_tokens
from .kv_cache import (
    build_anthropic_cached_payload,
    build_anthropic_context_management,
    canonical_json,
    canonical_tools,
)

SUPPORTED_GATEWAY_PROTOCOLS = (
    "anthropic_messages",
    "openai_chat",
    "openai_responses",
    "gemini_generate_content",
    "demo",
)

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
    """线程安全与异步安全的 RPM / TPM 滑动窗口准入限流器（预留—结算双阶段模型）。

    为什么不能只在响应返回后才统计限流：
    在 parallel worker 或 subagent_run 并发派发最多 4 个子代理时，多个请求会在同一毫秒同时发出；
    若不做前置预留（reserve），所有并发请求都会观察到“当前已用额度 = 0”而瞬间穿透网关触发 429 雪崩。
    因此请求发出前先按估算值原子占坑，响应返回后通过 record_actual 多退少补对齐真实账单。
    当 AGENT_LAB_RPM_LIMIT 与 AGENT_LAB_TPM_LIMIT 均未配置时零休眠直通。
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


StreamEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

# 主流模型每百万 token（MTok）标准美元单价表（USD / 1M tokens）。
# 可通过环境变量 AGENT_LAB_MODEL_PRICING_JSON 动态覆盖或新增私有网关模型费率。
DEFAULT_MODEL_PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    # Claude Fable / Mythos / Opus / Sonnet / Haiku
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 0.25},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.50, "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-3-7-sonnet": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.0, "cache_write": 1.00, "cache_read": 0.08},
    # OpenAI GPT-4o / GPT-4.1 / o3 / o4 / GPT-5
    "gpt-5-pro": {"input": 15.0, "output": 120.0, "cache_write": 0.0, "cache_read": 1.50},
    "gpt-5-mini": {"input": 0.25, "output": 2.0, "cache_write": 0.0, "cache_read": 0.025},
    "gpt-5-nano": {"input": 0.05, "output": 0.40, "cache_write": 0.0, "cache_read": 0.005},
    "gpt-5": {"input": 1.25, "output": 10.0, "cache_write": 0.0, "cache_read": 0.125},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40, "cache_write": 0.0, "cache_read": 0.025},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60, "cache_write": 0.0, "cache_read": 0.10},
    "gpt-4.1": {"input": 2.0, "output": 8.0, "cache_write": 0.0, "cache_read": 0.50},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_write": 0.0, "cache_read": 0.075},
    "gpt-4o": {"input": 2.50, "output": 10.0, "cache_write": 0.0, "cache_read": 1.25},
    "o4-mini": {"input": 1.10, "output": 4.40, "cache_write": 0.0, "cache_read": 0.275},
    "o3-pro": {"input": 20.0, "output": 80.0, "cache_write": 0.0, "cache_read": 5.0},
    "o3-mini": {"input": 1.10, "output": 4.40, "cache_write": 0.0, "cache_read": 0.55},
    "o3": {"input": 2.0, "output": 8.0, "cache_write": 0.0, "cache_read": 0.50},
    # Google Gemini 2.5 / 3 / 4
    "gemini-4-pro": {"input": 2.50, "output": 15.0, "cache_write": 0.0, "cache_read": 0.25},
    "gemini-4-flash": {"input": 0.30, "output": 2.50, "cache_write": 0.0, "cache_read": 0.03},
    "gemini-4": {"input": 2.50, "output": 15.0, "cache_write": 0.0, "cache_read": 0.25},
    "gemini-3-pro": {"input": 2.0, "output": 12.0, "cache_write": 0.0, "cache_read": 0.20},
    "gemini-3-flash": {"input": 0.25, "output": 1.50, "cache_write": 0.0, "cache_read": 0.025},
    "gemini-3": {"input": 2.0, "output": 12.0, "cache_write": 0.0, "cache_read": 0.20},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cache_write": 0.0, "cache_read": 0.3125},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_write": 0.0, "cache_read": 0.025},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_write": 0.0, "cache_read": 0.075},
    # DeepSeek
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "cache_write": 0.0, "cache_read": 0.14},
    "deepseek-r1": {"input": 0.55, "output": 2.19, "cache_write": 0.0, "cache_read": 0.14},
    "deepseek-chat": {"input": 0.27, "output": 1.10, "cache_write": 0.0, "cache_read": 0.07},
    "deepseek-v3": {"input": 0.27, "output": 1.10, "cache_write": 0.0, "cache_read": 0.07},
}

_PROVIDER_PREFIX_RE = re.compile(
    r"^(?:(?:us|eu|ap|global)\.)?(?:anthropic|openai|google|deepseek)[/.]"
)
_CONTEXT_SUFFIX_RE = re.compile(r"\[\d+[km]\]$")
_CONTEXT_WINDOW_TAG_RE = re.compile(r"\[(\d+)([km])\]$", re.IGNORECASE)

# 常见模型家族默认上下文窗口上限（单位：tokens）。
# 可通过 gateways.json 的 `context_window_tokens` 或环境变量 `AGENT_LAB_CONTEXT_TOKENS` 显式覆盖；
# 若模型 ID 自带 `[1m]` / `[400k]` / `[256k]` 等容量后缀，则直接按后缀精确解析。
DEFAULT_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gemini-4", 1_000_000),
    ("gemini-3", 1_000_000),
    ("gemini-2.5", 1_000_000),
    ("gemini-2.0", 1_000_000),
    ("gemini-1.5", 1_000_000),
    ("gpt-6", 1_000_000),
    ("gpt-5.6", 1_000_000),
    ("gpt-4.1", 1_000_000),
    ("mai-code-2", 1_000_000),
    ("grok-4", 500_000),
    ("gpt-5", 400_000),
    ("claude-fable", 200_000),
    ("claude-mythos", 200_000),
    ("claude-opus", 200_000),
    ("claude-sonnet", 200_000),
    ("claude-haiku", 200_000),
    ("claude-3", 200_000),
    ("o4", 200_000),
    ("o3", 200_000),
    ("gpt-4o", 128_000),
    ("deepseek", 128_000),
    ("qwen", 128_000),
)


def resolve_model_context_window(
    model: str | None,
    *,
    explicit_window: int | None = None,
) -> int | None:
    """解析目标模型的原生上下文窗口大小（tokens）。

    优先级：显式参数（如 gateways.json `context_window_tokens`）> `AGENT_LAB_CONTEXT_TOKENS` 环境变量 >
    模型名容量后缀（如 `[1m]` / `[400k]`）> 常见模型家族映射表；未识别模型或离线 `demo` 返回 `None`。
    """
    if (
        explicit_window is not None
        and not isinstance(explicit_window, bool)
        and isinstance(explicit_window, int)
        and explicit_window >= 4096
    ):
        return min(10_000_000, explicit_window)

    env_tokens = _parse_positive_int_env("AGENT_LAB_CONTEXT_TOKENS")
    if env_tokens is not None and env_tokens >= 4096:
        return min(10_000_000, env_tokens)

    if not isinstance(model, str) or not model.strip():
        return None
    raw = model.strip().lower()
    if raw in {"demo", "custom", "mock"}:
        return None

    # 1. 显式容量标签（如 `gemini-4-neon[1m]`、`gpt-5.3-codex[400k]`、`claude-opus-5[1m]`）
    tag_match = _CONTEXT_WINDOW_TAG_RE.search(raw)
    if tag_match:
        qty = int(tag_match.group(1))
        unit = tag_match.group(2).lower()
        parsed = qty * (1_000_000 if unit == "m" else 1_000)
        if parsed >= 4096:
            return min(10_000_000, parsed)

    # 2. 剥离网关前缀（如 `my-claude-` / `anthropic/` / `google/`）后按模型家族前缀匹配
    norm = _PROVIDER_PREFIX_RE.sub("", raw)
    if norm.startswith("my-claude-"):
        norm = norm[len("my-claude-") :]
    for prefix, win in DEFAULT_MODEL_CONTEXT_WINDOWS:
        if norm.startswith(prefix) or f"/{prefix}" in norm:
            return win
    return None


def _normalize_model_id_for_pricing(model: str) -> str:
    raw = str(model or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("models/"):
        raw = raw[len("models/") :]
    raw = _CONTEXT_SUFFIX_RE.sub("", raw)
    raw = _PROVIDER_PREFIX_RE.sub("", raw)
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw


def _load_model_pricing_table() -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {k: dict(v) for k, v in DEFAULT_MODEL_PRICING_USD_PER_MTOK.items()}
    raw_override = os.environ.get("AGENT_LAB_MODEL_PRICING_JSON", "").strip()
    if not raw_override:
        return table
    try:
        parsed = json.loads(raw_override)
    except (ValueError, TypeError):
        return table
    if not isinstance(parsed, dict):
        return table
    for raw_name, raw_rates in parsed.items():
        if not isinstance(raw_name, str) or not isinstance(raw_rates, dict):
            continue
        key = raw_name.strip().lower()
        if not key:
            continue
        try:
            inp = float(raw_rates.get("input", 0.0))
            out = float(raw_rates.get("output", 0.0))
        except (TypeError, ValueError):
            continue
        if inp < 0.0 or out < 0.0:
            continue
        default_write = inp * 1.25 if key.startswith("claude-") else 0.0
        default_read = inp * 0.10 if key.startswith("claude-") else inp * 0.25
        try:
            c_write = float(raw_rates.get("cache_write", default_write))
            c_read = float(raw_rates.get("cache_read", default_read))
        except (TypeError, ValueError):
            c_write, c_read = default_write, default_read
        table[key] = {
            "input": max(0.0, inp),
            "output": max(0.0, out),
            "cache_write": max(0.0, c_write),
            "cache_read": max(0.0, c_read),
        }
    return table


def _lookup_model_pricing(model: str) -> dict[str, float] | None:
    norm = _normalize_model_id_for_pricing(model)
    if not norm:
        return None
    table = _load_model_pricing_table()
    if norm in table:
        return table[norm]
    # 按最长键优先匹配，且要求在边界分隔符（- / . / _ / :）处断开，避免测试模型名（如 claude-sonnet-test / gpt-test）误匹配
    for key in sorted(table, key=len, reverse=True):
        if norm.startswith(key) and len(norm) > len(key) and norm[len(key)] in {"-", ".", "_", ":"}:
            return table[key]
    return None


def compute_cost_usd(
    model: str,
    usage: dict[str, Any] | None,
    *,
    additive_cache: bool | None = None,
) -> dict[str, Any]:
    """按模型单价表核算美元成本账本，区分未命中输入、缓存写、缓存读与输出 token。"""
    model_str = str(model or "").strip()
    rates = _lookup_model_pricing(model_str)
    if rates is None:
        return {
            "input_cost_usd": 0.0,
            "cache_write_cost_usd": 0.0,
            "cache_read_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "total_cost_usd": 0.0,
            "model": model_str,
            "pricing_known": False,
        }
    data = usage or {}
    norm = _normalize_model_id_for_pricing(model_str)
    is_additive = (
        bool(additive_cache)
        if additive_cache is not None
        else ("cache_creation_input_tokens" in data or norm.startswith("claude-"))
    )
    out_tokens = max(0, int(data.get("output_tokens", 0) or 0))
    if is_additive:
        uncached_input = max(0, int(data.get("input_tokens", 0) or 0))
        cache_write = max(0, int(data.get("cache_creation_input_tokens", 0) or 0))
        cache_read = max(0, int(data.get("cache_read_input_tokens", 0) or 0))
    else:
        total_input = max(0, int(data.get("input_tokens", 0) or 0))
        cache_read = min(total_input, max(0, int(data.get("cache_read_input_tokens", 0) or 0)))
        uncached_input = max(0, total_input - cache_read)
        cache_write = max(0, int(data.get("cache_creation_input_tokens", 0) or 0))

    input_cost = round((uncached_input / 1_000_000.0) * rates["input"], 8)
    cache_write_cost = round((cache_write / 1_000_000.0) * rates["cache_write"], 8)
    cache_read_cost = round((cache_read / 1_000_000.0) * rates["cache_read"], 8)
    output_cost = round((out_tokens / 1_000_000.0) * rates["output"], 8)
    total_cost = round(input_cost + cache_write_cost + cache_read_cost + output_cost, 8)
    return {
        "input_cost_usd": input_cost,
        "cache_write_cost_usd": cache_write_cost,
        "cache_read_cost_usd": cache_read_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": total_cost,
        "model": model_str,
        "pricing_known": True,
    }


def compute_usage_totals(
    usage: dict[str, Any] | None,
    *,
    additive_cache: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    """统一折算总输入 token、加权计费输入 token、有效预算消耗 token 与可选美元成本账本。

    - Anthropic（additive_cache=True 或含 cache_creation_input_tokens）：
      input_tokens 仅统计未命中缓存尾部，需与 cache_creation_input_tokens、cache_read_input_tokens 相加。
    - OpenAI（additive_cache=False）：
      input_tokens（即 prompt_tokens）已包含 cache_read_input_tokens（即 cached_tokens）。
    """
    data = usage or {}
    out = max(0, int(data.get("output_tokens", 0) or 0))
    is_additive = additive_cache or "cache_creation_input_tokens" in data
    if is_additive:
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
    totals: dict[str, Any] = {
        "total_input_tokens": total_input,
        "billed_input_tokens": billed_input,
        "output_tokens": out,
        "effective_budget_tokens": max(total_input, billed_input) + out,
    }
    if model is not None:
        breakdown = compute_cost_usd(model, data, additive_cache=is_additive)
        totals["cost_usd"] = float(breakdown["total_cost_usd"])
        totals["cost_breakdown"] = breakdown
    return totals


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
    """清洗并去重模型生成的工具调用 ID，严守内部命名空间边界与跨轮唯一性。

    1. 字符集清洗为 [A-Za-z0-9_-]，剥离冒号 ':'（Runtime 保留 ':' 作为子 Agent 调用账本命名空间分隔符）；
    2. 对照 unavailable_ids（含已被上下文窗口裁剪掉的历史轮次 ID）自动追加递增后缀，防止开源模型或兼容网关
       在长会话中重复输出 'call_0' / 'call_1' 撞上 SQLite tool_calls(run_id, call_id) 主键幂等校验。
    """
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


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def _emit_stream_event(on_event: StreamEventCallback | None, event: dict[str, Any]) -> None:
    if on_event is None:
        return
    maybe_awaitable = on_event(event)
    if inspect.isawaitable(maybe_awaitable):
        await maybe_awaitable


async def _emit_events_from_response(
    on_event: StreamEventCallback | None, response: dict[str, Any]
) -> None:
    if on_event is None:
        return
    for idx, block in enumerate(response.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            thought = str(block.get("thinking") or "")
            if thought:
                await _emit_stream_event(
                    on_event,
                    {"type": "thinking_delta", "thinking": thought, "delta": thought, "index": idx},
                )
        elif btype == "text":
            text = str(block.get("text") or "")
            if text:
                await _emit_stream_event(
                    on_event,
                    {"type": "text_delta", "text": text, "delta": text, "index": idx},
                )
        elif btype == "tool_use":
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            await _emit_stream_event(
                on_event,
                {"type": "tool_use_start", "id": call_id, "name": name, "index": idx},
            )
            raw_json = canonical_json(block.get("input", {}))
            if raw_json:
                await _emit_stream_event(
                    on_event,
                    {
                        "type": "input_json_delta",
                        "partial_json": raw_json,
                        "delta": raw_json,
                        "index": idx,
                        "id": call_id,
                        "name": name,
                    },
                )
    await _emit_stream_event(
        on_event,
        {
            "type": "message_stop",
            "stop_reason": response.get("stop_reason"),
            "usage": response.get("usage", {}),
            "response": response,
        },
    )


async def _anext_with_timeout_and_cancel(
    aiter: AsyncIterator[Any],
    *,
    timeout: float | None,
    cancel_event: asyncio.Event | None,
) -> Any:
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError("Provider stream cancelled")
    if cancel_event is None:
        if timeout is not None and timeout > 0:
            return await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        return await aiter.__anext__()

    next_task = asyncio.create_task(aiter.__anext__())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, pending = await asyncio.wait(
            {next_task, cancel_task},
            timeout=timeout if (timeout is not None and timeout > 0) else None,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise TimeoutError(f"Provider stream idle timeout after {timeout}s")
        if cancel_task in done:
            if not next_task.done():
                next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)
            raise asyncio.CancelledError("Provider stream cancelled")
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        return next_task.result()
    except BaseException:
        for t in (next_task, cancel_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(next_task, cancel_task, return_exceptions=True)
        raise


async def _await_with_cancel(coro: Awaitable[Any], cancel_event: asyncio.Event | None) -> Any:
    if cancel_event is None:
        return await coro
    if cancel_event.is_set():
        if inspect.iscoroutine(coro):
            coro.close()
        raise asyncio.CancelledError("Provider request cancelled")
    req_task = asyncio.ensure_future(coro)
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({req_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done:
            if not req_task.done():
                req_task.cancel()
            await asyncio.gather(req_task, return_exceptions=True)
            raise asyncio.CancelledError("Provider request cancelled")
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        return req_task.result()
    except BaseException:
        for t in (req_task, cancel_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(req_task, cancel_task, return_exceptions=True)
        raise


async def call_provider_complete(
    provider: Any,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 2048,
    *,
    on_event: StreamEventCallback | None = None,
    model: str | None = None,
    cancel_event: asyncio.Event | None = None,
    **kwargs: Any,
) -> dict:
    """按底层 provider.complete 的真实签名自适应透传可选关键字参数，确保旧测试 Mock 100% 兼容。"""
    complete_fn = provider.complete
    sig = inspect.signature(complete_fn)
    params = sig.parameters
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    candidate_kwargs: dict[str, Any] = dict(kwargs)
    if on_event is not None:
        candidate_kwargs["on_event"] = on_event
    if model is not None:
        candidate_kwargs["model"] = model
    if cancel_event is not None:
        candidate_kwargs["cancel_event"] = cancel_event

    accepted_kwargs: dict[str, Any] = {}
    for key, val in candidate_kwargs.items():
        if has_var_kw:
            accepted_kwargs[key] = val
        elif key in params and params[key].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            accepted_kwargs[key] = val

    provider_handles_events = "on_event" in accepted_kwargs
    if "max_tokens" in params and params["max_tokens"].kind == inspect.Parameter.KEYWORD_ONLY:
        accepted_kwargs["max_tokens"] = max_tokens
        response = await complete_fn(system, messages, tools, **accepted_kwargs)
    else:
        response = await complete_fn(system, messages, tools, max_tokens, **accepted_kwargs)

    if on_event is not None and not provider_handles_events:
        await _emit_events_from_response(on_event, response)
    return response


async def _stream_via_complete(
    provider: Any,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 2048,
    **kwargs: Any,
) -> AsyncIterator[dict[str, Any]]:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    emitted_stop = False

    async def _on_event(ev: dict[str, Any]) -> None:
        nonlocal emitted_stop
        if ev.get("type") == "message_stop":
            emitted_stop = True
        await queue.put(("event", ev))

    async def _runner() -> None:
        try:
            response = await call_provider_complete(
                provider,
                system,
                messages,
                tools,
                max_tokens=max_tokens,
                on_event=_on_event,
                **kwargs,
            )
            if not emitted_stop:
                await queue.put(
                    (
                        "event",
                        {
                            "type": "message_stop",
                            "stop_reason": response.get("stop_reason"),
                            "usage": response.get("usage", {}),
                            "response": response,
                        },
                    )
                )
        except BaseException as exc:
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(_runner())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "event":
                yield payload
            elif kind == "error":
                raise payload
            elif kind == "done":
                break
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def resolve_model_for_tier(provider: Any, tier: str) -> str | None:
    """按推理档位（fast / reasoning / inherit）解析目标模型名称。"""
    norm_tier = str(tier or "inherit").strip().lower()
    raw_base = getattr(provider, "model", None)
    base_model = raw_base.strip() if isinstance(raw_base, str) and raw_base.strip() else None

    if norm_tier == "inherit":
        return base_model

    cls_name = type(provider).__name__.lower()
    protocol = str(getattr(provider, "protocol", "") or "").strip().lower()
    if protocol == "anthropic_messages":
        is_anthropic, is_openai, is_gemini = True, False, False
    elif protocol in {"openai_chat", "openai_responses"}:
        is_anthropic, is_openai, is_gemini = False, True, False
    elif protocol == "gemini_generate_content":
        is_anthropic, is_openai, is_gemini = False, False, True
    else:
        is_anthropic = (
            isinstance(provider, AnthropicProvider)
            or "anthropic" in cls_name
            or (base_model is not None and "claude" in base_model.lower())
        )
        is_openai = (
            isinstance(provider, (OpenAICompatibleProvider, OpenAIResponsesProvider))
            or "openai" in cls_name
        )
        is_gemini = (
            isinstance(provider, GeminiNativeProvider)
            or "gemini" in cls_name
            or (base_model is not None and "gemini" in base_model.lower())
        )

    # 具备显式网关协议或 profile_name 的配置化 Provider 实例（零全局 env 变更场景），
    # 其 fast_model / reasoning_model 属性优先于全局环境变量；无 protocol/profile_name 的轻量测试桩仍允许 env 覆盖。
    prefer_provider_attr = (
        isinstance(
            provider,
            (
                AnthropicProvider,
                OpenAICompatibleProvider,
                OpenAIResponsesProvider,
                GeminiNativeProvider,
            ),
        )
        or bool(protocol)
        or bool(getattr(provider, "profile_name", None))
        or bool(getattr(provider, "gateway_profile", None))
    )

    if norm_tier == "fast":
        provider_fast = getattr(provider, "fast_model", None)
        provider_fast_str = (
            provider_fast.strip() if isinstance(provider_fast, str) and provider_fast.strip() else None
        )
        if prefer_provider_attr and provider_fast_str:
            return provider_fast_str
        env_candidates: list[str] = []
        if is_anthropic:
            env_candidates.append("ANTHROPIC_FAST_MODEL")
        elif is_openai:
            env_candidates.append("OPENAI_FAST_MODEL")
        elif is_gemini:
            env_candidates.append("GEMINI_FAST_MODEL")
        env_candidates.append("AGENT_LAB_FAST_MODEL")
        for env_key in env_candidates:
            val = os.environ.get(env_key, "").strip()
            if val:
                return val
        if provider_fast_str:
            return provider_fast_str
        return base_model

    if norm_tier == "reasoning":
        provider_reasoning = getattr(provider, "reasoning_model", None)
        provider_reasoning_str = (
            provider_reasoning.strip()
            if isinstance(provider_reasoning, str) and provider_reasoning.strip()
            else None
        )
        if prefer_provider_attr and provider_reasoning_str:
            return provider_reasoning_str
        env_candidates = []
        if is_anthropic:
            env_candidates.append("ANTHROPIC_REASONING_MODEL")
        elif is_openai:
            env_candidates.append("OPENAI_REASONING_MODEL")
        elif is_gemini:
            env_candidates.append("GEMINI_REASONING_MODEL")
        env_candidates.append("AGENT_LAB_REASONING_MODEL")
        for env_key in env_candidates:
            val = os.environ.get(env_key, "").strip()
            if val:
                return val
        if provider_reasoning_str:
            return provider_reasoning_str
        return base_model

    raise ValueError(f"未知模型档位: {tier!r}（仅支持 'fast'、'reasoning'、'inherit'）")


class Provider(Protocol):
    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        on_event: StreamEventCallback | None = None,
        model: str | None = None,
    ) -> dict: ...

    def stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]: ...


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

    protocol = "demo"
    cache_usage_additive = False

    async def stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in _stream_via_complete(
            self, system, messages, tools, max_tokens=max_tokens, **kwargs
        ):
            yield event

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        on_event: StreamEventCallback | None = None,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> dict:
        del model
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("DemoProvider request cancelled")
        response = self._complete_sync(system, messages, tools, max_tokens=max_tokens)
        if on_event is not None:
            await _emit_events_from_response(on_event, response)
        return response

    def _complete_sync(
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


def _is_loopback_base_url(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def _resolve_base_url(
    default_url: str,
    *,
    explicit_base_url: str | None,
    env_vars: tuple[str, ...],
    allow_custom_base_url: bool | None,
) -> str:
    """解析目标网关地址，兼顾显式配置透传与环境变量污染防护。"""
    cleaned_default = default_url.rstrip("/")
    explicit = (
        explicit_base_url.strip().rstrip("/")
        if isinstance(explicit_base_url, str) and explicit_base_url.strip()
        else ""
    )
    if explicit:
        if allow_custom_base_url is False:
            try:
                default_parsed = urllib.parse.urlsplit(cleaned_default)
                explicit_parsed = urllib.parse.urlsplit(explicit)
                if (
                    (explicit_parsed.scheme or "").lower() == "https"
                    and (explicit_parsed.hostname or "").lower()
                    == (default_parsed.hostname or "").lower()
                ):
                    return explicit
            except ValueError:
                pass
            return cleaned_default
        return explicit
    env_allowed = allow_custom_base_url is True or (
        allow_custom_base_url is None and os.environ.get("AGENT_ALLOW_CUSTOM_BASE_URL") == "1"
    )
    if env_allowed:
        for env_key in env_vars:
            custom = os.environ.get(env_key, "").strip().rstrip("/")
            if custom:
                return custom
    return cleaned_default


def _normalize_extra_headers(extra_headers: dict[str, str] | None) -> dict[str, str]:
    if not extra_headers:
        return {}
    if not isinstance(extra_headers, dict):
        raise ValueError("extra_headers 必须是 dict[str, str]")
    cleaned: dict[str, str] = {}
    for raw_k, raw_v in extra_headers.items():
        if raw_v is None:
            continue
        k = str(raw_k).strip()
        v = str(raw_v)
        if not k:
            continue
        if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
            raise ValueError(f"extra_headers 包含非法换行字符: {k!r}")
        cleaned[k] = v
    return cleaned


def _merge_headers(
    base_headers: dict[str, str], extra_headers: dict[str, str] | None
) -> dict[str, str]:
    merged = dict(base_headers)
    if not extra_headers:
        return merged
    lower_to_key = {k.lower(): k for k in merged}
    for k, v in extra_headers.items():
        existing = lower_to_key.get(k.lower())
        if existing is not None and existing != k:
            del merged[existing]
        merged[k] = v
        lower_to_key[k.lower()] = k
    return merged


def _has_header(headers: dict[str, str], name: str) -> bool:
    target = name.lower()
    return any(k.lower() == target and bool(str(v).strip()) for k, v in headers.items())


def _missing_client_headers(client: Any, desired_headers: dict[str, str]) -> dict[str, str] | None:
    if not desired_headers:
        return None
    existing_raw = getattr(client, "headers", None)
    existing_keys = (
        {str(k).lower() for k in existing_raw.keys()}
        if hasattr(existing_raw, "keys")
        else set()
    )
    missing = {k: v for k, v in desired_headers.items() if k.lower() not in existing_keys}
    return missing or None


async def _sleep_with_cancel(delay: float, cancel_event: asyncio.Event | None) -> None:
    if delay <= 0:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("Provider request cancelled")
        return
    if cancel_event is None:
        await asyncio.sleep(delay)
        return
    if cancel_event.is_set():
        raise asyncio.CancelledError("Provider request cancelled")
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return
    raise asyncio.CancelledError("Provider request cancelled during retry backoff")


def _compute_backoff_delay(
    attempt: int,
    retry_after_header: str | None,
    *,
    base_retry_delay: float = 0.25,
    max_retry_delay: float = 5.0,
) -> float:
    parsed_ra = _parse_retry_after(retry_after_header, max_delay=max_retry_delay)
    if parsed_ra is not None:
        return parsed_ra
    jitter = random.uniform(0.75, 1.25)
    return min(max_retry_delay, base_retry_delay * (2**attempt) * jitter)


def _parse_data_uri(uri: str) -> tuple[str, str] | None:
    if not isinstance(uri, str) or not uri.startswith("data:"):
        return None
    header_and_data = uri[5:]
    if ";base64," not in header_and_data:
        return None
    mime, b64 = header_and_data.split(";base64,", 1)
    mime = mime.strip() or "image/png"
    b64 = b64.strip()
    if not b64:
        return None
    return mime, b64


def _stringify_tool_result_content(raw_content: Any) -> str:
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content
    try:
        return json.dumps(raw_content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw_content)


def _callable_accepts_kwarg(fn: Any, kwarg: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    p = params.get(kwarg)
    return p is not None and p.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


class AnthropicProvider:
    """Anthropic Messages API 真实模型适配器：管理显式三级缓存断点、Extended Thinking、原生上下文管理与瞬态多模态截图。

    安全与配额边界：SDK 仅做有限网络重试和请求超时；API Key / Auth Token 绝不进入 prompt / SQLite / 审计事件，
    且自定义网关地址必须显式开启 AGENT_ALLOW_CUSTOM_BASE_URL=1 或由调用方显式传入（allow_custom_base_url!=False）才生效，
    防止环境变量污染将密钥发往不可信端点。
    """

    protocol = "anthropic_messages"
    supports_cache_hints = True
    accepts_historical_tool_ids = True
    cache_usage_additive = True
    supports_vision = True
    # 声明具备开放式 JSON 提炼能力：允许 DreamEngine 在无锁离线阶段调用本适配器合成跨会话 Wiki 知识页
    supports_dreaming = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        extra_headers: dict[str, str] | None = None,
        thinking: dict[str, Any] | None = None,
        timeout: float | Any | None = None,
        allow_custom_base_url: bool | None = None,
        profile_name: str | None = None,
        stream_idle_timeout: float | None = None,
        enable_context_editing: bool | None = None,
        enable_thinking_clearing: bool | None = None,
        enable_server_compaction: bool | None = None,
        server_compact_trigger_tokens: int | None = None,
        keep_recent_tool_uses: int = 4,
        enable_ptc: bool | None = None,
        ptc_allowed_callers: list[str] | None = None,
        http_client: Any | None = None,
    ) -> None:
        from anthropic import AsyncAnthropic, Omit, Timeout

        resolved_model = (
            model.strip()
            if isinstance(model, str) and model.strip()
            else os.environ.get("ANTHROPIC_MODEL", "").strip()
        )
        cleaned_extra_headers = _normalize_extra_headers(extra_headers)
        has_header_x_api_key = _has_header(cleaned_extra_headers, "x-api-key")
        has_header_authorization = _has_header(cleaned_extra_headers, "authorization")
        has_header_auth = has_header_x_api_key or has_header_authorization
        resolved_base_url = _resolve_base_url(
            "https://api.anthropic.com",
            explicit_base_url=base_url,
            env_vars=("ANTHROPIC_BASE_URL",),
            allow_custom_base_url=allow_custom_base_url,
        )
        explicit_api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else ""
        explicit_auth_token = (
            auth_token.strip() if isinstance(auth_token, str) and auth_token.strip() else ""
        )
        allow_keyless_loopback = (
            bool(base_url) and _is_loopback_base_url(resolved_base_url) and api_key is not None
        )
        if explicit_api_key or explicit_auth_token or has_header_auth or allow_keyless_loopback:
            resolved_api_key = explicit_api_key
            resolved_auth_token = explicit_auth_token
        else:
            resolved_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            resolved_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
            if not resolved_api_key and not resolved_auth_token and bool(base_url) and _is_loopback_base_url(resolved_base_url):
                allow_keyless_loopback = True

        has_auth = (
            bool(resolved_api_key)
            or bool(resolved_auth_token)
            or has_header_auth
            or allow_keyless_loopback
            or http_client is not None
        )
        if not has_auth or not resolved_model:
            raise ValueError("真实模式需要 ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN）与 ANTHROPIC_MODEL")
        self.model = resolved_model
        self.fast_model: str | None = (
            fast_model.strip() if isinstance(fast_model, str) and fast_model.strip() else None
        )
        self.reasoning_model: str | None = (
            reasoning_model.strip()
            if isinstance(reasoning_model, str) and reasoning_model.strip()
            else None
        )
        self.profile_name: str | None = (
            profile_name.strip() if isinstance(profile_name, str) and profile_name.strip() else None
        )
        self.extra_headers: dict[str, str] = cleaned_extra_headers
        if thinking is not None:
            self.thinking: dict[str, Any] | None = deepcopy(thinking)
        else:
            budget_raw = os.environ.get("ANTHROPIC_THINKING_BUDGET", "").strip()
            self.thinking = (
                {"type": "enabled", "budget_tokens": int(budget_raw)}
                if budget_raw and int(budget_raw) >= 1024
                else None
            )
        self.enable_context_editing: bool = (
            bool(enable_context_editing)
            if enable_context_editing is not None
            else (os.environ.get("AGENT_LAB_ANTHROPIC_CONTEXT_EDITING", "").strip() == "1")
        )
        self.enable_thinking_clearing: bool | None = (
            bool(enable_thinking_clearing) if enable_thinking_clearing is not None else None
        )
        self.enable_server_compaction: bool = (
            bool(enable_server_compaction)
            if enable_server_compaction is not None
            else (os.environ.get("AGENT_LAB_ANTHROPIC_SERVER_COMPACT", "").strip() == "1")
        )
        self.server_compact_trigger_tokens: int = (
            max(1, int(server_compact_trigger_tokens))
            if server_compact_trigger_tokens is not None
            else (_parse_positive_int_env("AGENT_LAB_ANTHROPIC_SERVER_COMPACT_TRIGGER_TOKENS") or 150_000)
        )
        self.keep_recent_tool_uses: int = max(0, int(keep_recent_tool_uses))
        self.enable_ptc: bool = (
            bool(enable_ptc)
            if enable_ptc is not None
            else (os.environ.get("AGENT_LAB_ANTHROPIC_PTC", "").strip() == "1")
        )
        self.ptc_allowed_callers: list[str] | None = (
            [str(c).strip() for c in ptc_allowed_callers if isinstance(c, str) and c.strip()]
            if ptc_allowed_callers is not None
            else None
        )
        self.rate_limiter = ProviderRateLimiter()
        self.base_url = resolved_base_url

        if timeout is not None:
            client_timeout = timeout
        else:
            env_timeout = os.environ.get("ANTHROPIC_TIMEOUT", "").strip()
            if env_timeout:
                parsed_t = float(env_timeout)
                client_timeout = Timeout(parsed_t, connect=min(15.0, parsed_t), read=parsed_t, write=30.0)
            else:
                client_timeout = Timeout(120.0, connect=15.0, read=120.0, write=30.0)
        self.timeout = client_timeout
        if stream_idle_timeout is not None:
            self.stream_idle_timeout = float(stream_idle_timeout)
        else:
            env_idle = os.environ.get("ANTHROPIC_STREAM_IDLE_TIMEOUT", "").strip()
            if env_idle:
                self.stream_idle_timeout = float(env_idle)
            elif isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                self.stream_idle_timeout = float(timeout)
            else:
                self.stream_idle_timeout = 120.0

        kwargs: dict[str, Any] = {
            "max_retries": 2,
            "timeout": client_timeout,
            "base_url": resolved_base_url,
        }
        sdk_default_headers: dict[str, Any] = dict(cleaned_extra_headers)
        if resolved_api_key:
            kwargs["api_key"] = resolved_api_key
        elif resolved_auth_token:
            kwargs["auth_token"] = resolved_auth_token
        elif has_header_auth:
            # 显式传入 api_key="" 阻断 AsyncAnthropic 读取宿主机 ANTHROPIC_API_KEY，
            # 同时将自定义鉴权头规范化为 SDK _validate_headers 可识别的大小写键名，避免请求阶段误报 TypeError。
            kwargs["api_key"] = ""
            if has_header_x_api_key:
                key_val = next(v for k, v in cleaned_extra_headers.items() if k.lower() == "x-api-key")
                sdk_default_headers = {
                    k: v for k, v in sdk_default_headers.items() if k.lower() != "x-api-key"
                }
                sdk_default_headers["X-Api-Key"] = key_val
            else:
                auth_val = next(
                    v for k, v in cleaned_extra_headers.items() if k.lower() == "authorization"
                )
                sdk_default_headers = {
                    k: v for k, v in sdk_default_headers.items() if k.lower() != "authorization"
                }
                sdk_default_headers["X-Api-Key"] = Omit()
                sdk_default_headers["Authorization"] = auth_val
        elif allow_keyless_loopback or http_client is not None:
            # 本地回环或注入测试客户端且未传密钥时使用占位密钥阻断宿主机 env 泄露并通过 SDK 鉴权校验
            kwargs["api_key"] = "local-loopback"
        if sdk_default_headers:
            kwargs["default_headers"] = sdk_default_headers
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self.client.close()

    async def stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in _stream_via_complete(
            self, system, messages, tools, max_tokens=max_tokens, **kwargs
        ):
            yield event

    async def _stream_anthropic_message(
        self,
        effective_model: str,
        cached_req: dict[str, Any],
        *,
        on_event: StreamEventCallback,
        unavailable_ids: set[str],
        cancel_event: asyncio.Event | None,
        idle_timeout: float,
        messages_api: Any | None = None,
    ) -> tuple[Any, dict[int, str]]:
        stream_unavailable_ids = set(unavailable_ids)
        active_tools: dict[int, tuple[str, str]] = {}
        stream_tool_ids: dict[int, str] = {}
        target_api = messages_api if messages_api is not None else self.client.messages

        async with target_api.stream(model=effective_model, **cached_req) as stream_ctx:
            event_iter = stream_ctx.__aiter__()
            while True:
                try:
                    event = await _anext_with_timeout_and_cancel(
                        event_iter, timeout=idle_timeout, cancel_event=cancel_event
                    )
                except StopAsyncIteration:
                    break
                ev_type = _get_field(event, "type")
                if ev_type == "content_block_start":
                    idx = int(_get_field(event, "index", 0) or 0)
                    cb = _get_field(event, "content_block")
                    if _get_field(cb, "type") == "tool_use":
                        raw_cid = _get_field(cb, "id", "")
                        norm_cid = _normalize_tool_call_id(raw_cid, stream_unavailable_ids)
                        tname = str(_get_field(cb, "name", "") or "").strip()
                        active_tools[idx] = (norm_cid, tname)
                        stream_tool_ids[idx] = norm_cid
                        await _emit_stream_event(
                            on_event,
                            {"type": "tool_use_start", "id": norm_cid, "name": tname, "index": idx},
                        )
                elif ev_type == "content_block_delta":
                    idx = int(_get_field(event, "index", 0) or 0)
                    delta = _get_field(event, "delta")
                    dtype = _get_field(delta, "type")
                    if dtype == "thinking_delta":
                        chunk = _get_field(delta, "thinking", "")
                        if isinstance(chunk, str) and chunk:
                            await _emit_stream_event(
                                on_event,
                                {"type": "thinking_delta", "thinking": chunk, "delta": chunk, "index": idx},
                            )
                    elif dtype == "text_delta":
                        chunk = _get_field(delta, "text", "")
                        if isinstance(chunk, str) and chunk:
                            await _emit_stream_event(
                                on_event,
                                {"type": "text_delta", "text": chunk, "delta": chunk, "index": idx},
                            )
                    elif dtype == "compaction_delta":
                        chunk = _get_field(delta, "content", "")
                        if isinstance(chunk, str) and chunk:
                            await _emit_stream_event(
                                on_event,
                                {"type": "compaction_delta", "content": chunk, "delta": chunk, "index": idx},
                            )
                    elif dtype == "input_json_delta":
                        chunk = _get_field(delta, "partial_json", "")
                        if isinstance(chunk, str) and chunk:
                            cid, cname = active_tools.get(idx, ("", ""))
                            await _emit_stream_event(
                                on_event,
                                {
                                    "type": "input_json_delta",
                                    "partial_json": chunk,
                                    "delta": chunk,
                                    "index": idx,
                                    "id": cid,
                                    "name": cname,
                                },
                            )
            final_maybe = stream_ctx.get_final_message()
            response = await final_maybe if inspect.isawaitable(final_maybe) else final_maybe
            return response, stream_tool_ids

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
        on_event: StreamEventCallback | None = None,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
        stream_idle_timeout: float | None = None,
    ) -> dict:
        del cache_key  # Anthropic 通过显式 cache_control 断点命中前缀缓存
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("Anthropic request cancelled before start")

        effective_model = (model.strip() if isinstance(model, str) and model.strip() else None) or self.model
        effective_thinking = thinking if thinking is not None else getattr(self, "thinking", None)
        # Anthropic 协议硬约束：thinking.budget_tokens 必须严格小于 max_tokens 且 >= 1024；
        # 当任务剩余 token 预算收缩导致 output_cap <= 1024 时自动临时降级关闭 thinking，避免触发服务端 400。
        if isinstance(effective_thinking, dict) and effective_thinking.get("type") == "enabled":
            budget = int(effective_thinking.get("budget_tokens", 1024))
            if max_tokens <= 1024 and thinking is None:
                effective_thinking = None
            elif budget >= max_tokens and max_tokens > 1024:
                effective_thinking = {**effective_thinking, "budget_tokens": max_tokens - 1}

        # 可选 Programmatic Tool Calling（PTC）元数据透传：仅当显式启用 enable_ptc=True 时保留或附着 allowed_callers
        enable_ptc = bool(getattr(self, "enable_ptc", False))
        ptc_default_callers = getattr(self, "ptc_allowed_callers", None)
        prepared_tools: list[dict[str, Any]] = []
        for spec in tools:
            if not isinstance(spec, dict):
                continue
            item = deepcopy(spec)
            if enable_ptc:
                raw_callers = item.get("allowed_callers")
                if isinstance(raw_callers, (list, tuple)):
                    cleaned_callers = [
                        str(c).strip() for c in raw_callers if isinstance(c, str) and c.strip()
                    ]
                    if cleaned_callers:
                        item["allowed_callers"] = cleaned_callers
                    else:
                        item.pop("allowed_callers", None)
                elif ptc_default_callers and item.get("input_schema") is not None:
                    item["allowed_callers"] = list(ptc_default_callers)
            else:
                item.pop("allowed_callers", None)
            prepared_tools.append(item)

        # 先构造打好 3 处显式 cache_control 断点的标准请求负载：
        # 断点 1 = tools[-1]，断点 2 = system[0]，断点 3 = 尾部 tail_state 之前的最后一块稳定历史消息。
        cached_req = build_anthropic_cached_payload(
            system,
            messages,
            prepared_tools,
            max_tokens=max_tokens,
            tail_state_count=tail_state_count,
            thinking=effective_thinking,
        )
        # 在第 3 处缓存断点构建完毕后，才将当轮瞬态桌面截图块追加到最末尾的 user 消息上：
        # 1. 截图位于断点 3 之后的未缓存尾部，下一轮画面变化或不再带图时，前序缓存哈希分毫不差；
        # 2. 直接合并入末尾已有 user 消息（若存在），不破坏 Anthropic 强制的 user/assistant 严格交替结构。
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

        # 2026 原生上下文管理（Context Editing / Server-Side Compaction）：默认全关，零改动既有请求
        ctx_editing = bool(getattr(self, "enable_context_editing", False))
        ctx_thinking_attr = getattr(self, "enable_thinking_clearing", None)
        ctx_thinking_clear = (
            bool(ctx_thinking_attr)
            if ctx_thinking_attr is not None
            else (ctx_editing and effective_thinking is not None)
        )
        ctx_compact = bool(getattr(self, "enable_server_compaction", False))
        ctx_trigger = int(getattr(self, "server_compact_trigger_tokens", 150_000) or 150_000)
        ctx_keep_tools = int(getattr(self, "keep_recent_tool_uses", 4) or 4)
        context_mgmt, beta_headers = build_anthropic_context_management(
            enable_tool_clearing=ctx_editing,
            enable_thinking_clearing=ctx_thinking_clear,
            enable_server_compaction=ctx_compact,
            trigger_tokens=ctx_trigger,
            keep_recent_tool_uses=ctx_keep_tools,
        )

        beta_ns = getattr(self.client, "beta", None)
        beta_messages_api = getattr(beta_ns, "messages", None) if beta_ns is not None else None
        use_beta_api = context_mgmt is not None and beta_messages_api is not None
        target_messages_api = beta_messages_api if use_beta_api else self.client.messages

        req_kwargs: dict[str, Any] = dict(cached_req)
        if context_mgmt is not None:
            merged_betas: list[str] = []
            for hk, hv in (getattr(self, "extra_headers", None) or {}).items():
                if hk.lower() == "anthropic-beta" and hv:
                    for token in str(hv).split(","):
                        clean_tok = token.strip()
                        if clean_tok and clean_tok not in merged_betas:
                            merged_betas.append(clean_tok)
            for b in beta_headers:
                if b not in merged_betas:
                    merged_betas.append(b)
            if merged_betas:
                req_kwargs["extra_headers"] = {"anthropic-beta": ",".join(merged_betas)}
            will_stream = on_event is not None and hasattr(target_messages_api, "stream")
            target_fn = target_messages_api.stream if will_stream else target_messages_api.create
            if use_beta_api or _callable_accepts_kwarg(target_fn, "context_management"):
                req_kwargs["context_management"] = context_mgmt
            else:
                req_kwargs["extra_body"] = {"context_management": context_mgmt}

        unavailable_ids = _collect_historical_tool_ids(messages)
        if historical_tool_ids:
            unavailable_ids.update(historical_tool_ids)

        limiter = getattr(self, "rate_limiter", None)
        reserved_tokens = 0
        if limiter is not None and limiter.enabled:
            reserved_tokens = await limiter.reserve(
                _estimate_request_tokens(system, messages, prepared_tools, max_tokens)
            )
        streamed_live = False
        stream_tool_ids: dict[int, str] = {}
        try:
            if on_event is not None and hasattr(target_messages_api, "stream"):
                idle_timeout = (
                    float(stream_idle_timeout)
                    if stream_idle_timeout is not None
                    else float(getattr(self, "stream_idle_timeout", 120.0))
                )
                response, stream_tool_ids = await self._stream_anthropic_message(
                    effective_model,
                    req_kwargs,
                    on_event=on_event,
                    unavailable_ids=unavailable_ids,
                    cancel_event=cancel_event,
                    idle_timeout=idle_timeout,
                    messages_api=target_messages_api,
                )
                streamed_live = True
            else:
                response = await _await_with_cancel(
                    target_messages_api.create(model=effective_model, **req_kwargs),
                    cancel_event,
                )
        except BaseException:
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise

        raw_blocks = getattr(response, "content", None)
        if raw_blocks is None or not isinstance(raw_blocks, (list, tuple)):
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise ValueError("模型返回损坏的空响应（缺少 content 列表）")

        is_truncated = getattr(response, "stop_reason", None) == "max_tokens"
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
            elif btype == "compaction":
                comp_block: dict[str, Any] = {"type": "compaction"}
                raw_comp_content = dumped.get("content")
                if isinstance(raw_comp_content, str):
                    comp_block["content"] = raw_comp_content if raw_comp_content != "" else None
                elif raw_comp_content is None:
                    comp_block["content"] = None
                else:
                    comp_block["content"] = str(raw_comp_content)
                if dumped.get("encrypted_content") is not None:
                    comp_block["encrypted_content"] = str(dumped["encrypted_content"])
                content.append(comp_block)
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
                if idx in stream_tool_ids:
                    norm_id = stream_tool_ids[idx]
                    unavailable_ids.add(norm_id)
                else:
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
        has_compaction = any(
            b["type"] == "compaction" and (b.get("content") or b.get("encrypted_content"))
            for b in content
        )
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
            if not content or (not has_tool_use and not has_text and not has_compaction):
                if limiter is not None and limiter.enabled:
                    limiter.record_actual(0, reserved_tokens)
                raise ValueError("模型返回空内容或不包含有效文本/工具调用")
            stop_reason = "tool_use" if has_tool_use else (response.stop_reason or "end_turn")

        usage: dict[str, Any] = {
            "input_tokens": int(getattr(response.usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response.usage, "output_tokens", 0) or 0),
        }
        cache_read = getattr(response.usage, "cache_read_input_tokens", None)
        cache_write = getattr(response.usage, "cache_creation_input_tokens", None)
        if cache_read is not None:
            usage["cache_read_input_tokens"] = int(cache_read)
        if cache_write is not None:
            usage["cache_creation_input_tokens"] = int(cache_write)

        cost_info = compute_cost_usd(effective_model, usage, additive_cache=True)
        if cost_info["pricing_known"]:
            usage["cost_usd"] = float(cost_info["total_cost_usd"])
            usage["cost_breakdown"] = cost_info

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

        if on_event is not None:
            if streamed_live:
                await _emit_stream_event(
                    on_event,
                    {
                        "type": "message_stop",
                        "stop_reason": result["stop_reason"],
                        "usage": result["usage"],
                        "response": result,
                    },
                )
            else:
                await _emit_events_from_response(on_event, result)
        return result


_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _prefers_max_completion_tokens(model: str) -> bool:
    norm = _normalize_model_id_for_pricing(model)
    for prefix in _MAX_COMPLETION_TOKEN_MODEL_PREFIXES:
        if norm == prefix or (
            norm.startswith(prefix) and len(norm) > len(prefix) and norm[len(prefix)] in {"-", ".", "_"}
        ):
            return True
    return False


def _invalid_tool_args_payload(
    raw_snippet: str,
    *,
    reason: str,
    parse_error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "__truncated_or_invalid_json__": raw_snippet[:240],
        "__tool_input_error__": reason,
    }
    if parse_error:
        payload["__json_error__"] = parse_error[:160]
    return payload


def _parse_tool_arguments(
    raw_args: Any,
    *,
    required_params: list[str],
    is_truncated: bool,
) -> dict[str, Any]:
    """安全解析工具参数 JSON；截断或畸形时封装为结构化诊断字典而不静默降级为空对象。"""
    if raw_args is None or raw_args == "":
        if is_truncated:
            return _invalid_tool_args_payload(
                "<truncated_empty_arguments>", reason="truncated_at_max_tokens"
            )
        if required_params:
            return _invalid_tool_args_payload(
                "<empty_arguments_missing_required>",
                reason="empty_arguments_for_required_params",
            )
        return {}
    if isinstance(raw_args, str):
        if not raw_args.strip():
            return _invalid_tool_args_payload(
                raw_args,
                reason="truncated_json" if is_truncated else "invalid_json",
            )
        try:
            parsed_args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError) as exc:
            return _invalid_tool_args_payload(
                raw_args,
                reason="truncated_json" if is_truncated else "invalid_json",
                parse_error=str(exc),
            )
        if not isinstance(parsed_args, dict):
            return _invalid_tool_args_payload(raw_args, reason="non_object_json")
        if is_truncated and not parsed_args and required_params:
            return _invalid_tool_args_payload(
                "<truncated_empty_object>", reason="truncated_at_max_tokens"
            )
        return parsed_args
    if isinstance(raw_args, dict):
        if is_truncated and not raw_args and required_params:
            return _invalid_tool_args_payload(
                "<truncated_empty_object>", reason="truncated_at_max_tokens"
            )
        return raw_args
    return _invalid_tool_args_payload(str(raw_args), reason="non_object_arguments")


class OpenAICompatibleProvider:
    """兼容 OpenAI Chat Completions 协议的真实模型适配器（覆盖 OpenAI / vLLM / SGLang / DeepSeek 等网关）。

    支持多网关故障切换（fallback_base_urls）、SSE 流式分片重组与实时事件回调、reasoning_content 思维链保留、
    prompt_cache_key / reasoning_effort / max_completion_tokens 自适应降级、尾部瞬态多模态 Data URI 注入
    以及离线 Dreaming 知识合成。
    """

    protocol = "openai_chat"
    supports_cache_hints = True
    accepts_historical_tool_ids = True
    cache_usage_additive = False
    supports_vision = True
    # 声明支持离线 Dreaming：供 DreamEngine 在无锁阶段提炼结构化跨会话记忆页面
    supports_dreaming = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        fallback_base_urls: list[str] | None = None,
        model: str | None = None,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | Any | None = None,
        allow_custom_base_url: bool | None = None,
        reasoning_effort: str | None = None,
        profile_name: str | None = None,
        max_retries: int = 2,
        stream: bool | None = None,
        base_retry_delay: float = 0.25,
        max_retry_delay: float = 5.0,
        use_max_completion_tokens: bool | None = None,
        stream_idle_timeout: float | None = None,
        http_client: Any | None = None,
        transport: Any | None = None,
    ) -> None:
        import httpx

        cleaned_extra_headers = _normalize_extra_headers(extra_headers)
        has_header_auth = (
            _has_header(cleaned_extra_headers, "authorization")
            or _has_header(cleaned_extra_headers, "api-key")
            or _has_header(cleaned_extra_headers, "x-api-key")
        )
        resolved_base_url = _resolve_base_url(
            "https://api.openai.com/v1",
            explicit_base_url=base_url,
            env_vars=("OPENAI_BASE_URL",),
            allow_custom_base_url=allow_custom_base_url,
        )
        explicit_api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else ""
        allow_keyless_loopback = (
            bool(base_url) and _is_loopback_base_url(resolved_base_url) and api_key is not None
        )
        if explicit_api_key or has_header_auth or allow_keyless_loopback:
            resolved_api_key = explicit_api_key
        else:
            resolved_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not resolved_api_key and bool(base_url) and _is_loopback_base_url(resolved_base_url):
                allow_keyless_loopback = True
        resolved_model = (
            model.strip()
            if isinstance(model, str) and model.strip()
            else os.environ.get("OPENAI_MODEL", "").strip()
        )
        has_auth = (
            bool(resolved_api_key)
            or has_header_auth
            or allow_keyless_loopback
            or http_client is not None
            or transport is not None
        )
        if not has_auth or not resolved_model:
            raise ValueError("OpenAI 兼容模式需要 OPENAI_API_KEY 与 OPENAI_MODEL")

        self.base_url = resolved_base_url
        self._explicit_fallbacks = fallback_base_urls is not None
        raw_fallbacks = os.environ.get("OPENAI_FALLBACK_BASE_URLS", "").strip()
        self.fallback_base_urls: list[str] = (
            [u.strip().rstrip("/") for u in fallback_base_urls if u and u.strip()]
            if fallback_base_urls is not None
            else [u.strip().rstrip("/") for u in raw_fallbacks.split(",") if u.strip()]
        )
        self.max_retries = max(0, int(max_retries))
        self.use_stream = bool(stream) if stream is not None else (os.environ.get("OPENAI_STREAM") == "1")
        self.base_retry_delay = max(0.0, float(base_retry_delay))
        self.max_retry_delay = max(0.05, float(max_retry_delay))
        self.reasoning_effort: str | None = (
            reasoning_effort.strip() or None
            if isinstance(reasoning_effort, str)
            else (
                reasoning_effort
                if reasoning_effort is not None
                else (os.environ.get("OPENAI_REASONING_EFFORT", "").strip() or None)
            )
        )
        self.use_max_completion_tokens: bool | None = use_max_completion_tokens
        self._supports_prompt_cache_key: bool | None = None
        self.rate_limiter = ProviderRateLimiter()
        self.model = resolved_model
        self.fast_model: str | None = (
            fast_model.strip() if isinstance(fast_model, str) and fast_model.strip() else None
        )
        self.reasoning_model: str | None = (
            reasoning_model.strip()
            if isinstance(reasoning_model, str) and reasoning_model.strip()
            else None
        )
        self.profile_name: str | None = (
            profile_name.strip() if isinstance(profile_name, str) and profile_name.strip() else None
        )
        self.extra_headers: dict[str, str] = cleaned_extra_headers

        if timeout is not None:
            client_timeout = timeout
        else:
            env_timeout = os.environ.get("OPENAI_TIMEOUT", "").strip()
            if env_timeout:
                parsed_t = float(env_timeout)
                client_timeout = httpx.Timeout(parsed_t, connect=min(15.0, parsed_t), read=parsed_t, write=30.0)
            else:
                client_timeout = httpx.Timeout(120.0, connect=15.0, read=120.0, write=30.0)
        self.timeout = client_timeout
        if stream_idle_timeout is not None:
            self.stream_idle_timeout = float(stream_idle_timeout)
        else:
            env_idle = os.environ.get("OPENAI_STREAM_IDLE_TIMEOUT", "").strip()
            if env_idle:
                self.stream_idle_timeout = float(env_idle)
            elif isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                self.stream_idle_timeout = float(timeout)
            else:
                self.stream_idle_timeout = 120.0

        base_headers: dict[str, str] = {"Content-Type": "application/json"}
        if resolved_api_key:
            base_headers["Authorization"] = f"Bearer {resolved_api_key}"
        self._default_headers: dict[str, str] = _merge_headers(base_headers, cleaned_extra_headers)
        if http_client is not None:
            self.client = http_client
            self._injected_http_client = True
        else:
            self._injected_http_client = False
            client_kwargs: dict[str, Any] = {
                "base_url": resolved_base_url,
                "headers": self._default_headers,
                "timeout": client_timeout,
            }
            if transport is not None:
                client_kwargs["transport"] = transport
            self.client = httpx.AsyncClient(**client_kwargs)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in _stream_via_complete(
            self, system, messages, tools, max_tokens=max_tokens, **kwargs
        ):
            yield event

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
                # 协议顺序硬约束：OpenAI / vLLM 要求 role="tool" 消息必须紧邻上一条 assistant.tool_calls，
                # 绝不能被普通 role="user" 文本插入隔断（否则报 400），故先输出 tool 结果再合并 user 文本。
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
        return _compute_backoff_delay(
            attempt,
            retry_after_header,
            base_retry_delay=getattr(self, "base_retry_delay", 0.25),
            max_retry_delay=getattr(self, "max_retry_delay", 5.0),
        )

    async def _send_sse_and_assemble(
        self,
        endpoint_url: str,
        req_payload: dict[str, Any],
        *,
        on_event: StreamEventCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        stream_idle_timeout: float | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        reasoning_parts: list[str] = []
        text_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        emitted_tool_starts: set[int] = set()
        finish_reason: str | None = None
        usage_data: dict[str, Any] = {}
        has_any_choice = False
        saw_done = False
        stream_truncated = False
        idle_timeout = (
            float(stream_idle_timeout)
            if stream_idle_timeout is not None
            else float(getattr(self, "stream_idle_timeout", 120.0))
        )

        stream_kwargs: dict[str, Any] = {"json": req_payload}
        if getattr(self, "_injected_http_client", False):
            missing_hdrs = _missing_client_headers(
                self.client, getattr(self, "_default_headers", {})
            )
            if missing_hdrs:
                stream_kwargs["headers"] = missing_hdrs

        async with self.client.stream("POST", endpoint_url, **stream_kwargs) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                return resp, None
            content_type = str(resp.headers.get("content-type", "")).lower()
            if content_type.startswith("application/json"):
                await resp.aread()
                parsed_json = resp.json()
                if isinstance(parsed_json, dict):
                    parsed_json = {**parsed_json, "__not_streamed_live__": True}
                return resp, parsed_json

            line_iter = resp.aiter_lines().__aiter__()
            while True:
                try:
                    raw_line = await _anext_with_timeout_and_cancel(
                        line_iter, timeout=idle_timeout, cancel_event=cancel_event
                    )
                except StopAsyncIteration:
                    break
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    if has_any_choice or reasoning_parts or text_parts or tool_calls_by_index:
                        stream_truncated = True
                        break
                    raise
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

                delta_reasoning = delta.get("reasoning_content")
                if not isinstance(delta_reasoning, str) or not delta_reasoning:
                    delta_reasoning = delta.get("reasoning")
                if isinstance(delta_reasoning, str) and delta_reasoning:
                    reasoning_parts.append(delta_reasoning)
                    await _emit_stream_event(
                        on_event,
                        {
                            "type": "thinking_delta",
                            "thinking": delta_reasoning,
                            "delta": delta_reasoning,
                            "index": 0,
                        },
                    )

                delta_text = delta.get("content")
                if isinstance(delta_text, str) and delta_text:
                    text_parts.append(delta_text)
                    await _emit_stream_event(
                        on_event,
                        {
                            "type": "text_delta",
                            "text": delta_text,
                            "delta": delta_text,
                            "index": 1 if reasoning_parts else 0,
                        },
                    )

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
                        if slot["function"]["name"] and idx not in emitted_tool_starts:
                            emitted_tool_starts.add(idx)
                            await _emit_stream_event(
                                on_event,
                                {
                                    "type": "tool_use_start",
                                    "id": slot["id"],
                                    "name": slot["function"]["name"],
                                    "index": idx,
                                },
                            )
                        arg_chunk = fn_delta.get("arguments")
                        if isinstance(arg_chunk, str):
                            slot["function"]["arguments"] += arg_chunk
                            if arg_chunk:
                                if idx not in emitted_tool_starts:
                                    emitted_tool_starts.add(idx)
                                    await _emit_stream_event(
                                        on_event,
                                        {
                                            "type": "tool_use_start",
                                            "id": slot["id"],
                                            "name": slot["function"]["name"],
                                            "index": idx,
                                        },
                                    )
                                await _emit_stream_event(
                                    on_event,
                                    {
                                        "type": "input_json_delta",
                                        "partial_json": arg_chunk,
                                        "delta": arg_chunk,
                                        "index": idx,
                                        "id": slot["id"],
                                        "name": slot["function"]["name"],
                                    },
                                )

            assembled_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
            if finish_reason is None and not saw_done and (
                has_any_choice or reasoning_parts or text_parts or assembled_calls
            ):
                stream_truncated = True
            assembled_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts) if text_parts else None,
            }
            if reasoning_parts:
                assembled_msg["reasoning_content"] = "".join(reasoning_parts)
            if assembled_calls:
                assembled_msg["tool_calls"] = assembled_calls
            effective_finish = finish_reason or (
                "length" if stream_truncated else ("tool_calls" if assembled_calls else "stop")
            )
            assembled_data: dict[str, Any] = {
                "choices": (
                    [
                        {
                            "finish_reason": effective_finish,
                            "message": assembled_msg,
                        }
                    ]
                    if (has_any_choice or reasoning_parts or text_parts or assembled_calls)
                    else []
                ),
                "usage": usage_data,
            }
            if stream_truncated:
                assembled_data["__stream_truncated__"] = True
            return resp, assembled_data

    async def _post_with_resilience(
        self,
        base_payload: dict[str, Any],
        *,
        cache_key: str | None,
        on_event: StreamEventCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        stream_idle_timeout: float | None = None,
    ) -> dict[str, Any]:
        import httpx

        primary_base = str(self.client.base_url).rstrip("/")
        effective_primary_base = primary_base or str(getattr(self, "base_url", "") or "").rstrip("/")
        fallbacks = list(getattr(self, "fallback_base_urls", None) or [])
        if not getattr(self, "_explicit_fallbacks", False):
            env_fallbacks = [
                u.strip().rstrip("/")
                for u in os.environ.get("OPENAI_FALLBACK_BASE_URLS", "").split(",")
                if u.strip()
            ]
            for fb in env_fallbacks:
                if fb not in fallbacks:
                    fallbacks.append(fb)

        primary_ep_url = (
            "/chat/completions"
            if primary_base
            else f"{effective_primary_base}/chat/completions"
        )
        endpoints: list[tuple[str, str]] = [(primary_ep_url, effective_primary_base)] + [
            (f"{fb}/chat/completions", fb) for fb in fallbacks if fb != effective_primary_base
        ]
        max_retries = max(0, int(getattr(self, "max_retries", 2)))
        stream_attr = getattr(self, "use_stream", None)
        if stream_attr is None:
            raw_stream = self.__dict__.get("stream")
            stream_attr = bool(raw_stream) if isinstance(raw_stream, bool) else False
        use_stream = bool(stream_attr) or (on_event is not None) or (os.environ.get("OPENAI_STREAM") == "1")
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
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError("OpenAI request cancelled")
                try:
                    if use_stream:
                        resp, stream_data = await self._send_sse_and_assemble(
                            ep_url,
                            req_payload,
                            on_event=on_event,
                            cancel_event=cancel_event,
                            stream_idle_timeout=stream_idle_timeout,
                        )
                    else:
                        post_kwargs: dict[str, Any] = {"json": req_payload}
                        if getattr(self, "_injected_http_client", False):
                            missing_hdrs = _missing_client_headers(
                                self.client, getattr(self, "_default_headers", {})
                            )
                            if missing_hdrs:
                                post_kwargs["headers"] = missing_hdrs
                        resp = await _await_with_cancel(
                            self.client.post(ep_url, **post_kwargs),
                            cancel_event,
                        )
                        stream_data = None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        await _sleep_with_cancel(self._compute_backoff(attempt, None), cancel_event)
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

                if resp.status_code in {400, 422} and "reasoning_effort" in req_payload:
                    err_text = str(getattr(resp, "text", "") or "").lower()
                    if "reasoning_effort" in err_text:
                        req_payload = {k: v for k, v in req_payload.items() if k != "reasoning_effort"}
                        continue

                if resp.status_code in {400, 422} and "max_completion_tokens" in req_payload:
                    err_text = str(getattr(resp, "text", "") or "").lower()
                    if "max_completion_tokens" in err_text:
                        limit_val = req_payload.pop("max_completion_tokens")
                        req_payload["max_tokens"] = limit_val
                        continue

                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                        await _sleep_with_cancel(self._compute_backoff(attempt, retry_after), cancel_event)
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
        on_event: StreamEventCallback | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_completion_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
        stream_idle_timeout: float | None = None,
    ) -> dict:
        del tail_state_count  # OpenAI / vLLM 基于前缀块自动匹配，尾部瞬态布局已由 ContextManager 保证
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("OpenAI request cancelled before start")

        effective_model = (model.strip() if isinstance(model, str) and model.strip() else None) or self.model
        effective_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else getattr(self, "reasoning_effort", None)
        )
        norm_tools = canonical_tools(tools)
        required_by_tool: dict[str, list[str]] = {
            str(spec["name"]): list(spec.get("input_schema", {}).get("required") or [])
            for spec in norm_tools
            if isinstance(spec, dict) and spec.get("name")
        }
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
        # 尾部瞬态视觉注入：在稳定历史前缀与 HARNESS_STATE 全部序列化完成后，将当轮截图作为 image_url
        # 追加到最末条 user 消息（若原 content 为纯字符串则无损升维成多模态 content 数组）。
        # 这样 OpenAI Prompt Caching 与 vLLM Automatic Prefix Caching 对前序所有物理页仍保持 100% 命中。
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
            "model": effective_model,
            "messages": openai_messages,
        }
        use_mct = getattr(self, "use_max_completion_tokens", None)
        if max_completion_tokens is not None:
            payload["max_completion_tokens"] = int(max_completion_tokens)
        elif use_mct is True or (
            use_mct is None
            and (
                os.environ.get("OPENAI_USE_MAX_COMPLETION_TOKENS") == "1"
                or _prefers_max_completion_tokens(effective_model)
            )
        ):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens

        if effective_effort:
            payload["reasoning_effort"] = str(effective_effort)
        if openai_tools:
            payload["tools"] = openai_tools

        limiter = getattr(self, "rate_limiter", None)
        reserved_tokens = 0
        if limiter is not None and limiter.enabled:
            reserved_tokens = await limiter.reserve(
                _estimate_request_tokens(system, messages, tools, max_tokens)
            )
        try:
            data = await self._post_with_resilience(
                payload,
                cache_key=cache_key,
                on_event=on_event,
                cancel_event=cancel_event,
                stream_idle_timeout=stream_idle_timeout,
            )
        except BaseException:
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise

        not_streamed_live = bool(data.pop("__not_streamed_live__", False)) if isinstance(data, dict) else False
        stream_truncated = bool(data.pop("__stream_truncated__", False)) if isinstance(data, dict) else False

        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            raise ValueError("OpenAI 兼容网关返回空 choices")

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        is_length_truncated = finish_reason == "length" or stream_truncated
        message = choice.get("message") or {}
        unavailable_ids = _collect_historical_tool_ids(messages)
        if historical_tool_ids:
            unavailable_ids.update(historical_tool_ids)

        content: list[dict[str, Any]] = []
        raw_reasoning = message.get("reasoning_content")
        if not isinstance(raw_reasoning, str) or not raw_reasoning:
            raw_reasoning = message.get("reasoning")
        if isinstance(raw_reasoning, str) and raw_reasoning:
            content.append({"type": "thinking", "thinking": raw_reasoning})
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
            required_params = required_by_tool.get(fn_name, [])
            parsed_args = _parse_tool_arguments(
                raw_args,
                required_params=required_params,
                is_truncated=is_length_truncated,
            )
            content.append(
                {
                    "type": "tool_use",
                    "id": norm_id,
                    "name": fn_name[:64],
                    "input": parsed_args,
                }
            )

        has_calls = any(b["type"] == "tool_use" for b in content)
        has_text = any(b["type"] == "text" and b.get("text") for b in content)
        stop_reason = "tool_use" if has_calls else "end_turn"
        if is_length_truncated and not has_calls:
            stop_reason = "max_tokens"
            if not has_text:
                content.append({"type": "text", "text": "[输出因达到 max_tokens 上限被截断]"})

        usage = data.get("usage") or {}
        parsed_usage: dict[str, Any] = {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        }
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            parsed_usage["cache_read_input_tokens"] = int(details["cached_tokens"])
        comp_details = usage.get("completion_tokens_details") or {}
        if isinstance(comp_details, dict) and comp_details.get("reasoning_tokens") is not None:
            parsed_usage["reasoning_tokens"] = int(comp_details["reasoning_tokens"])

        cost_info = compute_cost_usd(effective_model, parsed_usage, additive_cache=False)
        if cost_info["pricing_known"]:
            parsed_usage["cost_usd"] = float(cost_info["total_cost_usd"])
            parsed_usage["cost_breakdown"] = cost_info

        if limiter is not None and limiter.enabled:
            totals = compute_usage_totals(parsed_usage, additive_cache=False)
            limiter.record_actual(totals["total_input_tokens"] + totals["output_tokens"], reserved_tokens)

        result: dict[str, Any] = {
            "content": content,
            "stop_reason": stop_reason,
            "usage": parsed_usage,
            "truncated": is_length_truncated,
        }
        if on_event is not None:
            if not_streamed_live:
                await _emit_events_from_response(on_event, result)
            else:
                await _emit_stream_event(
                    on_event,
                    {
                        "type": "message_stop",
                        "stop_reason": result["stop_reason"],
                        "usage": result["usage"],
                        "response": result,
                    },
                )
        return result


def _sanitize_gemini_schema(schema: Any) -> Any:
    """递归清理 Gemini REST FunctionDeclaration 不接受的 JSON Schema 扩展键（如 additionalProperties/$schema/default）。"""
    if isinstance(schema, list):
        return [_sanitize_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    unsupported = {"$schema", "additionalProperties", "default", "$id", "$defs", "definitions", "title", "examples"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in unsupported:
            continue
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in v:
                out["nullable"] = True
            continue
        if k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _sanitize_gemini_schema(pv) for pk, pv in v.items()}
            continue
        out[k] = _sanitize_gemini_schema(v)
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


class OpenAIResponsesProvider:
    """OpenAI Responses API (`/v1/responses`) 适配器（适配 Codex / GPT-5 / o3 / o4 与兼容网关）。"""

    protocol = "openai_responses"
    supports_cache_hints = True
    accepts_historical_tool_ids = True
    cache_usage_additive = False
    supports_vision = True
    supports_dreaming = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        fallback_base_urls: list[str] | None = None,
        model: str | None = None,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        reasoning_effort: str | None = None,
        preserve_encrypted_reasoning: bool | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_custom_base_url: bool | None = None,
        profile_name: str | None = None,
        use_stream: bool = True,
        max_retries: int = 2,
        http_client: Any = None,
        transport: Any = None,
        rate_limiter: ProviderRateLimiter | None = None,
    ):
        self.base_url = _resolve_base_url(
            "https://api.openai.com/v1",
            explicit_base_url=base_url,
            env_vars=("OPENAI_BASE_URL",),
            allow_custom_base_url=allow_custom_base_url,
        )
        self.fallback_base_urls: list[str] = [
            u.strip().rstrip("/") for u in (fallback_base_urls or []) if isinstance(u, str) and u.strip()
        ]
        self.extra_headers = _normalize_extra_headers(extra_headers)
        raw_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.api_key = raw_key.strip()
        if not self.api_key and not _has_header(self.extra_headers, "authorization") and not _is_loopback_base_url(self.base_url):
            raise RuntimeError("请先配置 OPENAI_API_KEY（或在 gateways.json 中配置凭证）以启用 OpenAI Responses 协议。")
        self.model = (model or os.environ.get("OPENAI_MODEL", "gpt-5")).strip()
        self.fast_model = fast_model.strip() if isinstance(fast_model, str) and fast_model.strip() else None
        self.reasoning_model = (
            reasoning_model.strip() if isinstance(reasoning_model, str) and reasoning_model.strip() else None
        )
        self.reasoning_effort = (
            reasoning_effort.strip().lower()
            if isinstance(reasoning_effort, str) and reasoning_effort.strip()
            else os.environ.get("OPENAI_REASONING_EFFORT", "").strip().lower() or None
        )
        if preserve_encrypted_reasoning is not None:
            self.preserve_encrypted_reasoning: bool = bool(preserve_encrypted_reasoning)
        else:
            env_enc = os.environ.get("AGENT_LAB_OPENAI_ENCRYPTED_REASONING", "").strip()
            if env_enc == "0":
                self.preserve_encrypted_reasoning = False
            else:
                self.preserve_encrypted_reasoning = bool(self.reasoning_effort) or (env_enc == "1")
        self._supports_encrypted_reasoning_include: bool | None = None
        self.profile_name = profile_name
        self.gateway_profile = profile_name
        self.gateway_protocol = self.protocol
        self.use_stream = use_stream
        self.max_retries = max(0, int(max_retries))
        self.timeout = float(timeout) if timeout is not None else 120.0
        self.rate_limiter = rate_limiter
        self._http_client = http_client
        self._transport = transport

    def _build_headers(self) -> dict[str, str]:
        base: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key and not _has_header(self.extra_headers, "authorization"):
            base["Authorization"] = f"Bearer {self.api_key}"
        return _merge_headers(base, self.extra_headers)

    def _to_responses_input(
        self, messages: list[dict], vision_attachments: list[dict] | None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            top_type = msg.get("type")
            if top_type == "compaction" and isinstance(msg.get("encrypted_content"), str) and msg["encrypted_content"]:
                comp_top: dict[str, Any] = {
                    "type": "compaction",
                    "encrypted_content": msg["encrypted_content"],
                }
                if msg.get("id"):
                    comp_top["id"] = str(msg["id"])
                items.append(comp_top)
                continue
            if top_type == "reasoning" and isinstance(msg.get("encrypted_content"), str) and msg["encrypted_content"]:
                r_top: dict[str, Any] = {
                    "type": "reasoning",
                    "summary": list(msg.get("summary") or []),
                    "encrypted_content": msg["encrypted_content"],
                }
                if msg.get("id"):
                    r_top["id"] = str(msg["id"])
                items.append(r_top)
                continue
            if top_type in {"function_call", "function_call_output"}:
                items.append(deepcopy(msg))
                continue

            role = msg.get("role", "user")
            content = msg.get("content")
            is_last_user = role == "user" and idx == len(messages) - 1
            if role == "user":
                if isinstance(content, str):
                    parts: list[dict[str, Any]] = [{"type": "input_text", "text": content}]
                    if is_last_user and vision_attachments:
                        for att in vision_attachments:
                            data_url = att.get("data_url") or (
                                f"data:{att.get('media_type', 'image/png')};base64,{att['base64_data']}"
                                if att.get("base64_data")
                                else None
                            )
                            if data_url:
                                parts.append({"type": "input_image", "image_url": data_url})
                    items.append({"type": "message", "role": "user", "content": parts})
                elif isinstance(content, list):
                    text_parts: list[dict[str, Any]] = []
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        btype = blk.get("type")
                        if btype == "tool_result":
                            if text_parts:
                                items.append({"type": "message", "role": "user", "content": text_parts})
                                text_parts = []
                            items.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": str(blk.get("tool_use_id") or ""),
                                    "output": _stringify_tool_result_content(blk.get("content")),
                                }
                            )
                        elif btype == "compaction":
                            raw_enc = blk.get("encrypted_content")
                            if isinstance(raw_enc, str) and raw_enc:
                                if text_parts:
                                    items.append({"type": "message", "role": "user", "content": text_parts})
                                    text_parts = []
                                c_item: dict[str, Any] = {
                                    "type": "compaction",
                                    "encrypted_content": raw_enc,
                                }
                                if blk.get("id"):
                                    c_item["id"] = str(blk["id"])
                                items.append(c_item)
                            elif isinstance(blk.get("content"), str) and blk["content"]:
                                text_parts.append({"type": "input_text", "text": blk["content"]})
                        elif btype in {"text", "input_text"} and blk.get("text"):
                            text_parts.append({"type": "input_text", "text": str(blk["text"])})
                    if is_last_user and vision_attachments:
                        for att in vision_attachments:
                            data_url = att.get("data_url") or (
                                f"data:{att.get('media_type', 'image/png')};base64,{att['base64_data']}"
                                if att.get("base64_data")
                                else None
                            )
                            if data_url:
                                text_parts.append({"type": "input_image", "image_url": data_url})
                    if text_parts:
                        items.append({"type": "message", "role": "user", "content": text_parts})
            elif role == "assistant":
                if isinstance(content, str):
                    if content:
                        items.append(
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": content}],
                            }
                        )
                elif isinstance(content, list):
                    out_parts: list[dict[str, Any]] = []
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        btype = blk.get("type")
                        if btype == "thinking":
                            raw_enc = blk.get("encrypted_content")
                            if isinstance(raw_enc, str) and raw_enc:
                                if out_parts:
                                    items.append({"type": "message", "role": "assistant", "content": out_parts})
                                    out_parts = []
                                thought_txt = str(blk.get("thinking") or "").strip()
                                r_item: dict[str, Any] = {
                                    "type": "reasoning",
                                    "summary": (
                                        [{"type": "summary_text", "text": thought_txt}]
                                        if thought_txt
                                        else []
                                    ),
                                    "encrypted_content": raw_enc,
                                }
                                if blk.get("reasoning_id"):
                                    r_item["id"] = str(blk["reasoning_id"])
                                items.append(r_item)
                        elif btype == "compaction":
                            raw_enc = blk.get("encrypted_content")
                            if isinstance(raw_enc, str) and raw_enc:
                                if out_parts:
                                    items.append({"type": "message", "role": "assistant", "content": out_parts})
                                    out_parts = []
                                c_item = {
                                    "type": "compaction",
                                    "encrypted_content": raw_enc,
                                }
                                if blk.get("id"):
                                    c_item["id"] = str(blk["id"])
                                items.append(c_item)
                            elif isinstance(blk.get("content"), str) and blk["content"]:
                                out_parts.append({"type": "output_text", "text": blk["content"]})
                        elif btype in {"text", "output_text"} and blk.get("text"):
                            out_parts.append({"type": "output_text", "text": str(blk["text"])})
                        elif btype == "tool_use":
                            if out_parts:
                                items.append({"type": "message", "role": "assistant", "content": out_parts})
                                out_parts = []
                            items.append(
                                {
                                    "type": "function_call",
                                    "call_id": str(blk.get("id") or ""),
                                    "name": str(blk.get("name") or ""),
                                    "arguments": json.dumps(blk.get("input") or {}, ensure_ascii=False),
                                }
                            )
                    if out_parts:
                        items.append({"type": "message", "role": "assistant", "content": out_parts})
        return items

    def _to_responses_tools(self, tools: list[dict]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in tools:
            out.append(
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                }
            )
        return out

    @staticmethod
    def _normalize_usage(raw_usage: Any, *, effective_model: str) -> dict[str, Any]:
        usage_dict = raw_usage if isinstance(raw_usage, dict) else {}
        in_tok = int(usage_dict.get("input_tokens") or 0)
        out_tok = int(usage_dict.get("output_tokens") or 0)
        cached_tok = int((usage_dict.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        reason_tok = int((usage_dict.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
        usage: dict[str, Any] = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_input_tokens": cached_tok,
            "cache_creation_input_tokens": 0,
            "reasoning_tokens": reason_tok,
        }
        cost_info = compute_cost_usd(effective_model, usage, additive_cache=False)
        usage["cost_usd"] = cost_info["total_cost_usd"]
        usage["cost_breakdown"] = cost_info
        return usage

    def _normalize_response_obj(
        self,
        resp_obj: dict[str, Any],
        *,
        effective_model: str,
        used_ids: set[str],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        has_tool_use = False
        for item in resp_obj.get("output") or []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "reasoning":
                summary_parts = [
                    str(s.get("text") or "")
                    for s in (item.get("summary") or [])
                    if isinstance(s, dict) and s.get("text")
                ]
                raw_text = "\n".join(summary_parts).strip() or str(item.get("text") or "").strip()
                raw_enc = item.get("encrypted_content")
                enc_content = raw_enc if isinstance(raw_enc, str) and raw_enc else None
                raw_rid = item.get("id")
                reasoning_id = str(raw_rid) if isinstance(raw_rid, str) and raw_rid else None
                if raw_text or enc_content is not None:
                    t_block: dict[str, Any] = {"type": "thinking", "thinking": raw_text}
                    if enc_content is not None:
                        t_block["encrypted_content"] = enc_content
                    if reasoning_id is not None:
                        t_block["reasoning_id"] = reasoning_id
                    content.append(t_block)
            elif itype == "compaction":
                comp_blk: dict[str, Any] = {"type": "compaction"}
                raw_enc = item.get("encrypted_content")
                if isinstance(raw_enc, str) and raw_enc:
                    comp_blk["encrypted_content"] = raw_enc
                if isinstance(item.get("id"), str) and item["id"]:
                    comp_blk["id"] = item["id"]
                if isinstance(item.get("content"), str) and item["content"]:
                    comp_blk["content"] = item["content"]
                if "encrypted_content" in comp_blk or "content" in comp_blk:
                    content.append(comp_blk)
            elif itype == "message":
                for blk in item.get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") in {"output_text", "text"} and blk.get("text"):
                        content.append({"type": "text", "text": str(blk["text"])})
            elif itype == "function_call":
                has_tool_use = True
                cid = _normalize_tool_call_id(item.get("call_id") or item.get("id"), used_ids)
                used_ids.add(cid)
                parsed_args = _parse_tool_arguments(
                    item.get("arguments"),
                    required_params=[],
                    is_truncated=False,
                )
                content.append(
                    {
                        "type": "tool_use",
                        "id": cid,
                        "name": str(item.get("name") or ""),
                        "input": parsed_args,
                    }
                )
        if not content and isinstance(resp_obj.get("output_text"), str) and resp_obj["output_text"]:
            content.append({"type": "text", "text": resp_obj["output_text"]})

        usage = self._normalize_usage(resp_obj.get("usage"), effective_model=effective_model)

        status_str = str(resp_obj.get("status") or "completed")
        incomplete_reason = str((resp_obj.get("incomplete_details") or {}).get("reason") or "")
        truncated = status_str == "incomplete" and incomplete_reason == "max_output_tokens"
        if has_tool_use:
            stop_reason = "tool_use"
        elif truncated:
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        return {
            "content": content,
            "stop_reason": stop_reason,
            "usage": usage,
            "truncated": truncated,
        }

    def _normalize_compacted_output(
        self, raw_output: list[Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将 /responses/compact 返回的 output 项规范化为 (items, messages) 双视图。"""
        normalized_items: list[dict[str, Any]] = []
        normalized_messages: list[dict[str, Any]] = []
        for item in raw_output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "compaction":
                comp_item: dict[str, Any] = {"type": "compaction"}
                if isinstance(item.get("encrypted_content"), str) and item["encrypted_content"]:
                    comp_item["encrypted_content"] = item["encrypted_content"]
                if isinstance(item.get("id"), str) and item["id"]:
                    comp_item["id"] = item["id"]
                if isinstance(item.get("content"), str) and item["content"]:
                    comp_item["content"] = item["content"]
                if "encrypted_content" in comp_item or "content" in comp_item:
                    normalized_items.append(comp_item)
                    normalized_messages.append({"role": "assistant", "content": [deepcopy(comp_item)]})
            elif itype == "reasoning":
                summary_parts = [
                    str(s.get("text") or "")
                    for s in (item.get("summary") or [])
                    if isinstance(s, dict) and s.get("text")
                ]
                raw_text = "\n".join(summary_parts).strip() or str(item.get("text") or "").strip()
                raw_enc = item.get("encrypted_content")
                enc_content = raw_enc if isinstance(raw_enc, str) and raw_enc else None
                raw_rid = item.get("id")
                reasoning_id = str(raw_rid) if isinstance(raw_rid, str) and raw_rid else None
                if raw_text or enc_content is not None:
                    normalized_items.append(deepcopy(item))
                    t_blk: dict[str, Any] = {"type": "thinking", "thinking": raw_text}
                    if enc_content is not None:
                        t_blk["encrypted_content"] = enc_content
                    if reasoning_id is not None:
                        t_blk["reasoning_id"] = reasoning_id
                    normalized_messages.append({"role": "assistant", "content": [t_blk]})
            elif itype == "message":
                normalized_items.append(deepcopy(item))
                role = "assistant" if item.get("role") == "assistant" else "user"
                raw_content = item.get("content")
                if isinstance(raw_content, str):
                    if raw_content:
                        normalized_messages.append({"role": role, "content": raw_content})
                elif isinstance(raw_content, list):
                    texts = [
                        str(p.get("text") or "")
                        for p in raw_content
                        if isinstance(p, dict)
                        and p.get("type") in {"input_text", "output_text", "text"}
                        and p.get("text")
                    ]
                    if texts:
                        normalized_messages.append({"role": role, "content": "\n".join(texts)})
            elif itype == "function_call":
                normalized_items.append(deepcopy(item))
                parsed_args = _parse_tool_arguments(
                    item.get("arguments"), required_params=[], is_truncated=False
                )
                normalized_messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": str(item.get("call_id") or item.get("id") or ""),
                                "name": str(item.get("name") or ""),
                                "input": parsed_args,
                            }
                        ],
                    }
                )
            elif itype == "function_call_output":
                normalized_items.append(deepcopy(item))
                normalized_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": str(item.get("call_id") or ""),
                                "content": _stringify_tool_result_content(item.get("output")),
                            }
                        ],
                    }
                )
            else:
                normalized_items.append(deepcopy(item))
        return normalized_items, normalized_messages

    async def compact_remote(
        self,
        system: str,
        messages: list[dict],
        *,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """调用 OpenAI Responses 原生无状态压缩端点 `POST <base_url>/responses/compact`（ZDR 兼容）。

        若目标网关不支持该端点（如 400/404/405/501）或发生不可恢复网络错误，不中断主流程，
        而是返回 `{"ok": False, "fallback": True, "messages": <原消息副本>, ...}` 供调用方无缝回退本地压缩。
        """
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("OpenAI Responses compact_remote cancelled before start")

        effective_model = (model or self.model).strip()
        payload: dict[str, Any] = {
            "model": effective_model,
            "input": self._to_responses_input(messages),
        }
        if system:
            payload["instructions"] = system

        limiter = self.rate_limiter
        reserved_tokens = 0
        if limiter is not None and limiter.enabled:
            est = _estimate_request_tokens(system, messages, [], 0)
            reserved_tokens = await limiter.reserve(est)

        urls = [self.base_url, *self.fallback_base_urls]
        headers = self._build_headers()
        own_client = self._http_client is None
        client = (
            self._http_client
            if not own_client
            else httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                transport=self._transport,
            )
        )
        last_error: str = "OpenAI Responses compact_remote failed"
        last_status_code: int | None = None
        try:
            for base in urls:
                endpoint = f"{base.rstrip('/')}/responses/compact"
                for attempt in range(self.max_retries + 1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError("OpenAI Responses compact_remote cancelled")
                    try:
                        resp = await _await_with_cancel(
                            client.post(endpoint, headers=headers, json=payload), cancel_event
                        )
                        if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                            delay = _compute_backoff_delay(attempt, resp.headers.get("retry-after"))
                            await _sleep_with_cancel(delay, cancel_event)
                            continue
                        if resp.status_code >= 400:
                            last_status_code = resp.status_code
                            snippet = (resp.text or "").strip()[:240]
                            last_error = f"HTTP {resp.status_code}: {snippet}" if snippet else f"HTTP {resp.status_code}"
                            break
                        data = resp.json()
                        if not isinstance(data, dict) or not isinstance(data.get("output"), list):
                            last_error = "Invalid /responses/compact payload: missing output list"
                            break
                        items, compacted_messages = self._normalize_compacted_output(data["output"])
                        usage = self._normalize_usage(data.get("usage"), effective_model=effective_model)
                        if limiter is not None and limiter.enabled:
                            totals = compute_usage_totals(usage, additive_cache=False)
                            limiter.record_actual(
                                totals["total_input_tokens"] + totals["output_tokens"], reserved_tokens
                            )
                        return {
                            "ok": True,
                            "fallback": False,
                            "id": data.get("id"),
                            "items": items,
                            "messages": compacted_messages,
                            "usage": usage,
                        }
                    except asyncio.CancelledError:
                        if limiter is not None and limiter.enabled:
                            limiter.record_actual(0, reserved_tokens)
                        raise
                    except (httpx.TransportError, TimeoutError) as exc:
                        last_error = str(exc)
                        if attempt < self.max_retries:
                            await _sleep_with_cancel(_compute_backoff_delay(attempt, None), cancel_event)
                            continue
                        break
                    except (ValueError, json.JSONDecodeError) as exc:
                        last_error = str(exc)
                        break

            if limiter is not None and limiter.enabled:
                limiter.record_actual(0, reserved_tokens)
            return {
                "ok": False,
                "fallback": True,
                "error": last_error,
                "status_code": last_status_code,
                "items": [],
                "messages": deepcopy(messages),
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_usd": 0.0,
                },
            }
        finally:
            if own_client:
                await client.aclose()

    async def stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        prompt_cache_key: str | None = None,
        tail_state_count: int = 0,
        historical_tool_ids: set[str] | None = None,
        vision_attachments: list[dict] | None = None,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for ev in _stream_via_complete(
            self,
            system,
            messages,
            tools,
            max_tokens=max_tokens,
            cache_key=cache_key,
            prompt_cache_key=prompt_cache_key,
            tail_state_count=tail_state_count,
            historical_tool_ids=historical_tool_ids,
            vision_attachments=vision_attachments,
            model=model,
            cancel_event=cancel_event,
        ):
            yield ev

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        prompt_cache_key: str | None = None,
        tail_state_count: int = 0,
        historical_tool_ids: set[str] | None = None,
        vision_attachments: list[dict] | None = None,
        on_event: StreamEventCallback | None = None,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        del tail_state_count
        effective_model = (model or self.model).strip()
        effective_cache_key = prompt_cache_key or cache_key
        payload: dict[str, Any] = {
            "model": effective_model,
            "instructions": system,
            "input": self._to_responses_input(messages, vision_attachments),
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if tools:
            payload["tools"] = self._to_responses_tools(tools)
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if (
            getattr(self, "preserve_encrypted_reasoning", False)
            and getattr(self, "_supports_encrypted_reasoning_include", None) is not False
        ):
            payload["include"] = ["reasoning.encrypted_content"]
        if effective_cache_key:
            payload["prompt_cache_key"] = effective_cache_key
        should_stream = bool(self.use_stream or on_event is not None)
        if should_stream:
            payload["stream"] = True

        limiter = self.rate_limiter
        reserved_tokens = 0
        if limiter is not None:
            est = _estimate_request_tokens(system, messages, tools, max_tokens)
            reserved_tokens = await limiter.reserve(est)

        used_ids = _collect_historical_tool_ids(messages)
        if historical_tool_ids:
            used_ids.update(historical_tool_ids)
        urls = [self.base_url, *self.fallback_base_urls]
        headers = self._build_headers()
        own_client = self._http_client is None
        client = (
            self._http_client
            if not own_client
            else httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                transport=self._transport,
            )
        )
        try:
            last_exc: Exception | None = None
            for base in urls:
                endpoint = f"{base.rstrip('/')}/responses"
                req_payload = dict(payload)
                attempt = 0
                while attempt <= self.max_retries:
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError("Provider request cancelled")
                    try:
                        if should_stream:
                            res = await self._send_sse(
                                client,
                                endpoint,
                                headers,
                                req_payload,
                                effective_model=effective_model,
                                used_ids=used_ids,
                                on_event=on_event,
                                cancel_event=cancel_event,
                            )
                        else:
                            resp = await _await_with_cancel(
                                client.post(endpoint, headers=headers, json=req_payload), cancel_event
                            )
                            if (
                                resp.status_code in {400, 422}
                                and "include" in req_payload
                                and any(
                                    tok in (resp.text or "").lower()
                                    for tok in ("include", "encrypted_content")
                                )
                            ):
                                self._supports_encrypted_reasoning_include = False
                                req_payload = {k: v for k, v in req_payload.items() if k != "include"}
                                continue
                            if resp.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                                delay = _compute_backoff_delay(attempt, resp.headers.get("retry-after"))
                                await _sleep_with_cancel(delay, cancel_event)
                                attempt += 1
                                continue
                            resp.raise_for_status()
                            res = self._normalize_response_obj(
                                resp.json(), effective_model=effective_model, used_ids=used_ids
                            )
                            if on_event is not None:
                                await _emit_events_from_response(on_event, res)
                        if limiter is not None:
                            totals = compute_usage_totals(res["usage"], additive_cache=False)
                            limiter.record_actual(
                                totals["total_input_tokens"] + totals["output_tokens"], reserved_tokens
                            )
                        return res
                    except httpx.HTTPStatusError as exc:
                        last_exc = exc
                        status_code = exc.response.status_code if exc.response is not None else 0
                        err_body = (exc.response.text or "").lower() if exc.response is not None else ""
                        if (
                            status_code in {400, 422}
                            and "include" in req_payload
                            and any(tok in err_body for tok in ("include", "encrypted_content"))
                        ):
                            self._supports_encrypted_reasoning_include = False
                            req_payload = {k: v for k, v in req_payload.items() if k != "include"}
                            continue
                        if status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                            ra = exc.response.headers.get("retry-after") if exc.response is not None else None
                            await _sleep_with_cancel(_compute_backoff_delay(attempt, ra), cancel_event)
                            attempt += 1
                            continue
                        break
                    except (httpx.TransportError, TimeoutError) as exc:
                        last_exc = exc
                        if attempt < self.max_retries:
                            await _sleep_with_cancel(_compute_backoff_delay(attempt, None), cancel_event)
                            attempt += 1
                            continue
                        break
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("OpenAI Responses 请求失败")
        finally:
            if own_client:
                await client.aclose()

    async def _send_sse(
        self,
        client: Any,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        effective_model: str,
        used_ids: set[str],
        on_event: StreamEventCallback | None,
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        initial_used_ids = set(used_ids)
        text_chunks: list[str] = []
        thinking_chunks: list[str] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        tool_order: list[str] = []
        done_reasoning_blocks: list[dict[str, Any]] = []
        done_compaction_blocks: list[dict[str, Any]] = []
        completed_obj: dict[str, Any] | None = None

        async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" not in ctype:
                raw = await _await_with_cancel(resp.aread(), cancel_event)
                res = self._normalize_response_obj(
                    json.loads(raw.decode("utf-8")), effective_model=effective_model, used_ids=used_ids
                )
                if on_event is not None:
                    await _emit_events_from_response(on_event, res)
                return res

            line_iter = resp.aiter_lines()
            while True:
                try:
                    line = await _anext_with_timeout_and_cancel(
                        line_iter, timeout=self.timeout, cancel_event=cancel_event
                    )
                except StopAsyncIteration:
                    break
                if line is None:
                    break
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    ev_obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                ev_type = str(ev_obj.get("type") or "")
                if ev_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                    delta = str(ev_obj.get("delta") or "")
                    if delta:
                        thinking_chunks.append(delta)
                        await _emit_stream_event(on_event, {"type": "thinking_delta", "delta": delta})
                elif ev_type == "response.output_text.delta":
                    delta = str(ev_obj.get("delta") or "")
                    if delta:
                        text_chunks.append(delta)
                        await _emit_stream_event(on_event, {"type": "text_delta", "delta": delta})
                elif ev_type == "response.output_item.added":
                    item = ev_obj.get("item") or {}
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        raw_id = str(item.get("call_id") or item.get("id") or f"fc_{len(tool_order)}")
                        norm_id = _normalize_tool_call_id(raw_id, used_ids)
                        used_ids.add(norm_id)
                        item_key = str(item.get("id") or raw_id)
                        tool_calls[item_key] = {
                            "id": norm_id,
                            "name": str(item.get("name") or ""),
                            "args_parts": [],
                        }
                        tool_order.append(item_key)
                        await _emit_stream_event(
                            on_event,
                            {"type": "tool_use_start", "call_id": norm_id, "name": str(item.get("name") or "")},
                        )
                elif ev_type == "response.output_item.done":
                    item = ev_obj.get("item") or {}
                    if isinstance(item, dict):
                        itype = item.get("type")
                        if itype == "reasoning":
                            summary_parts = [
                                str(s.get("text") or "")
                                for s in (item.get("summary") or [])
                                if isinstance(s, dict) and s.get("text")
                            ]
                            raw_text = "\n".join(summary_parts).strip() or str(item.get("text") or "").strip()
                            raw_enc = item.get("encrypted_content")
                            enc_content = raw_enc if isinstance(raw_enc, str) and raw_enc else None
                            raw_rid = item.get("id")
                            reasoning_id = str(raw_rid) if isinstance(raw_rid, str) and raw_rid else None
                            if raw_text or enc_content is not None:
                                r_blk: dict[str, Any] = {"type": "thinking", "thinking": raw_text}
                                if enc_content is not None:
                                    r_blk["encrypted_content"] = enc_content
                                if reasoning_id is not None:
                                    r_blk["reasoning_id"] = reasoning_id
                                done_reasoning_blocks.append(r_blk)
                        elif itype == "compaction":
                            c_blk: dict[str, Any] = {"type": "compaction"}
                            if isinstance(item.get("encrypted_content"), str) and item["encrypted_content"]:
                                c_blk["encrypted_content"] = item["encrypted_content"]
                            if isinstance(item.get("id"), str) and item["id"]:
                                c_blk["id"] = item["id"]
                            if isinstance(item.get("content"), str) and item["content"]:
                                c_blk["content"] = item["content"]
                            if "encrypted_content" in c_blk or "content" in c_blk:
                                done_compaction_blocks.append(c_blk)
                elif ev_type == "response.function_call_arguments.delta":
                    item_key = str(ev_obj.get("item_id") or ev_obj.get("call_id") or "")
                    delta = str(ev_obj.get("delta") or "")
                    if item_key in tool_calls and delta:
                        tool_calls[item_key]["args_parts"].append(delta)
                        await _emit_stream_event(
                            on_event,
                            {
                                "type": "input_json_delta",
                                "call_id": tool_calls[item_key]["id"],
                                "name": tool_calls[item_key]["name"],
                                "delta": delta,
                            },
                        )
                elif ev_type in {"response.completed", "response.incomplete"}:
                    if isinstance(ev_obj.get("response"), dict):
                        completed_obj = ev_obj["response"]

        if completed_obj is not None:
            result = self._normalize_response_obj(
                completed_obj, effective_model=effective_model, used_ids=set(initial_used_ids)
            )
            # 若 completed 对象里缺少增量阶段捕获的 thinking 文本，优先补全到已有 thinking 块以免丢失 encrypted_content
            if thinking_chunks:
                existing_thinking = next((b for b in result["content"] if b.get("type") == "thinking"), None)
                if existing_thinking is None:
                    result["content"].insert(0, {"type": "thinking", "thinking": "".join(thinking_chunks)})
                elif not existing_thinking.get("thinking"):
                    existing_thinking["thinking"] = "".join(thinking_chunks)
        else:
            content: list[dict[str, Any]] = []
            content.extend(done_compaction_blocks)
            if done_reasoning_blocks:
                if thinking_chunks and not done_reasoning_blocks[0].get("thinking"):
                    done_reasoning_blocks[0]["thinking"] = "".join(thinking_chunks)
                content.extend(done_reasoning_blocks)
            elif thinking_chunks:
                content.append({"type": "thinking", "thinking": "".join(thinking_chunks)})
            if text_chunks:
                content.append({"type": "text", "text": "".join(text_chunks)})
            for k in tool_order:
                tc = tool_calls[k]
                raw_args = "".join(tc["args_parts"])
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": _parse_tool_arguments(
                            raw_args, required_params=[], is_truncated=False
                        ),
                    }
                )
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cost_usd": 0.0,
            }
            result = {
                "content": content,
                "stop_reason": "tool_use" if tool_order else "end_turn",
                "usage": usage,
                "truncated": False,
            }
        await _emit_stream_event(
            on_event,
            {
                "type": "message_stop",
                "stop_reason": result["stop_reason"],
                "usage": result["usage"],
                "response": result,
            },
        )
        return result


class GeminiExplicitCacheManager:
    """Gemini 显式 Context Caching 管理器（`/v1beta/cachedContents` 创建、TTL 追踪、PATCH 续期与透明降级）。

    核心机制：
    - 仅当 `systemInstruction + tools` 估算前缀 token 数 >= `min_cache_tokens`（默认 32768）时创建显式缓存；
    - 以 `SHA-256(model, system, canonical_tools)` 为确定性键缓存返回的 `cachedContents/...` 资源名；
    - 追踪本地单调时钟 TTL（默认 3600s），在临近过期窗口内通过 `PATCH <base_url>/<cache_name>?updateMask=ttl` 续期；
    - 当端点返回 400 / 404 / 不支持或发生异常时无缝降级返回 `None`，绝不阻塞正常 `generateContent` 请求。
    """

    def __init__(
        self,
        *,
        min_cache_tokens: int = 32_768,
        ttl_seconds: int = 3600,
        renew_before_expiry_seconds: float | None = None,
        max_entries: int = 128,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.min_cache_tokens = max(1, int(min_cache_tokens))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.renew_before_expiry_seconds = (
            max(0.0, float(renew_before_expiry_seconds))
            if renew_before_expiry_seconds is not None
            else min(300.0, self.ttl_seconds * 0.2)
        )
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._unsupported_base_urls: set[str] = set()

    @staticmethod
    def _normalize_model_resource(model: str) -> str:
        clean = str(model or "").strip()
        return clean if clean.startswith("models/") else f"models/{clean}"

    @classmethod
    def compute_cache_key(cls, model: str, system: str, tools: list[dict]) -> str:
        model_resource = cls._normalize_model_resource(model)
        canon_tools = canonical_tools(tools) if tools else []
        payload = canonical_json(
            {
                "model": model_resource,
                "system": system or "",
                "tools": canon_tools,
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def estimate_prefix_tokens(system: str, tools: list[dict]) -> int:
        canon_tools = canonical_tools(tools) if tools else []
        return estimate_bpe_tokens(system or "") + estimate_bpe_tokens(canon_tools)

    def get_entry(self, cache_hash: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(cache_hash)
            if entry is None:
                return None
            if self._clock() >= float(entry["expires_at"]):
                self._entries.pop(cache_hash, None)
                return None
            return dict(entry)

    def invalidate(
        self, cache_hash: str | None = None, *, cache_name: str | None = None
    ) -> None:
        with self._lock:
            if cache_hash is not None:
                self._entries.pop(cache_hash, None)
            if cache_name is not None:
                to_drop = [k for k, v in self._entries.items() if v.get("name") == cache_name]
                for k in to_drop:
                    self._entries.pop(k, None)

    def _store_entry(self, cache_hash: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.pop(cache_hash, None)
            self._entries[cache_hash] = entry
            while len(self._entries) > self.max_entries:
                oldest_key = next(iter(self._entries))
                self._entries.pop(oldest_key, None)

    async def renew_ttl(
        self,
        client: Any,
        base_url: str,
        headers: dict[str, str],
        cache_name: str,
        *,
        ttl_seconds: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """通过 `PATCH <base_url>/<cache_name>?updateMask=ttl` 延长已有显式缓存的 TTL。"""
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("Gemini explicit cache TTL renewal cancelled")

        ttl = max(60, int(ttl_seconds if ttl_seconds is not None else self.ttl_seconds))
        resource_path = cache_name.lstrip("/")
        if not resource_path.startswith("cachedContents/"):
            resource_path = f"cachedContents/{resource_path}"
        endpoint = f"{base_url.rstrip('/')}/{resource_path}?updateMask=ttl"
        try:
            resp = await _await_with_cancel(
                client.patch(endpoint, headers=headers, json={"ttl": f"{ttl}s"}),
                cancel_event,
            )
            if 200 <= resp.status_code < 300:
                now = self._clock()
                expire_time = None
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        expire_time = body.get("expireTime")
                except (ValueError, json.JSONDecodeError):
                    pass
                with self._lock:
                    for entry in self._entries.values():
                        if entry.get("name") == cache_name:
                            entry["expires_at"] = now + ttl
                            entry["ttl_seconds"] = ttl
                            if expire_time is not None:
                                entry["expire_time"] = expire_time
                return True
            if resp.status_code in {404, 410}:
                self.invalidate(cache_name=cache_name)
                return False
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def get_or_create(
        self,
        client: Any,
        *,
        base_url: str,
        headers: dict[str, str],
        model: str,
        system: str,
        tools: list[dict],
        gemini_tools: list[dict[str, Any]],
        display_name: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        """查找、续期或创建 Gemini 显式缓存资源名；不支持或失败时透明返回 None。"""
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("Gemini explicit cache lookup cancelled")

        clean_base = base_url.rstrip("/")
        with self._lock:
            if clean_base in self._unsupported_base_urls:
                return None

        if not system and not gemini_tools:
            return None

        est_tokens = self.estimate_prefix_tokens(system, tools)
        if est_tokens < self.min_cache_tokens:
            return None

        cache_hash = self.compute_cache_key(model, system, tools)
        now = self._clock()
        existing: dict[str, Any] | None = None
        with self._lock:
            cand = self._entries.get(cache_hash)
            if cand is not None:
                if now >= float(cand["expires_at"]):
                    self._entries.pop(cache_hash, None)
                elif cand.get("base_url") == clean_base:
                    existing = dict(cand)

        if existing is not None:
            remaining = float(existing["expires_at"]) - now
            if remaining <= self.renew_before_expiry_seconds:
                renewed = await self.renew_ttl(
                    client,
                    clean_base,
                    headers,
                    str(existing["name"]),
                    cancel_event=cancel_event,
                )
                if renewed:
                    return str(existing["name"])
                # 若续期因 404/410 淘汰了旧缓存则继续向下重建；若只是临时网络抖动且仍未过期则继续复用
                if self.get_entry(cache_hash) is not None:
                    return str(existing["name"])
            else:
                return str(existing["name"])

        model_resource = self._normalize_model_resource(model)
        create_body: dict[str, Any] = {
            "model": model_resource,
            "ttl": f"{self.ttl_seconds}s",
        }
        if isinstance(display_name, str) and display_name.strip():
            create_body["displayName"] = display_name.strip()[:128]
        if system:
            create_body["systemInstruction"] = {"parts": [{"text": system}]}
        if gemini_tools:
            create_body["tools"] = gemini_tools

        endpoint = f"{clean_base}/cachedContents"
        try:
            resp = await _await_with_cancel(
                client.post(endpoint, headers=headers, json=create_body),
                cancel_event,
            )
            if resp.status_code in {404, 405, 501}:
                with self._lock:
                    self._unsupported_base_urls.add(clean_base)
                return None
            if resp.status_code >= 400:
                # 400/403/422/429/5xx 等场景透明降级为标准非显式缓存请求
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            cache_name = str(data.get("name") or "").strip()
            if not cache_name:
                return None
            created_now = self._clock()
            self._store_entry(
                cache_hash,
                {
                    "name": cache_name,
                    "base_url": clean_base,
                    "model": model_resource,
                    "created_at": created_now,
                    "expires_at": created_now + self.ttl_seconds,
                    "ttl_seconds": self.ttl_seconds,
                    "expire_time": data.get("expireTime"),
                },
            )
            return cache_name
        except asyncio.CancelledError:
            raise
        except Exception:
            return None


class GeminiNativeProvider:
    """Google Gemini 原生 REST 协议适配器（`/v1beta/models/{model}:streamGenerateContent?alt=sse`）。"""

    protocol = "gemini_generate_content"
    supports_cache_hints = True
    accepts_historical_tool_ids = True
    cache_usage_additive = False
    supports_vision = True
    supports_dreaming = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        fallback_base_urls: list[str] | None = None,
        model: str | None = None,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_custom_base_url: bool | None = None,
        profile_name: str | None = None,
        use_stream: bool = True,
        max_retries: int = 2,
        thinking_budget: int | None = None,
        enable_explicit_cache: bool | None = None,
        min_cache_tokens: int | None = None,
        explicit_cache_ttl_seconds: int | None = None,
        explicit_cache_manager: GeminiExplicitCacheManager | None = None,
        http_client: Any = None,
        transport: Any = None,
        rate_limiter: ProviderRateLimiter | None = None,
    ):
        self.base_url = _resolve_base_url(
            "https://generativelanguage.googleapis.com/v1beta",
            explicit_base_url=base_url,
            env_vars=("GEMINI_BASE_URL",),
            allow_custom_base_url=allow_custom_base_url,
        )
        self.fallback_base_urls: list[str] = [
            u.strip().rstrip("/") for u in (fallback_base_urls or []) if isinstance(u, str) and u.strip()
        ]
        self.extra_headers = _normalize_extra_headers(extra_headers)
        raw_key = (
            api_key
            if api_key is not None
            else (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "")
        )
        self.api_key = raw_key.strip()
        if (
            not self.api_key
            and not _has_header(self.extra_headers, "x-goog-api-key")
            and not _has_header(self.extra_headers, "authorization")
            and not _is_loopback_base_url(self.base_url)
        ):
            raise RuntimeError("请先配置 GEMINI_API_KEY（或在 gateways.json 中配置凭证）以启用 Gemini 原生协议。")
        self.model = (model or os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")).strip()
        self.fast_model = fast_model.strip() if isinstance(fast_model, str) and fast_model.strip() else None
        self.reasoning_model = (
            reasoning_model.strip() if isinstance(reasoning_model, str) and reasoning_model.strip() else None
        )
        self.profile_name = profile_name
        self.gateway_profile = profile_name
        self.gateway_protocol = self.protocol
        self.use_stream = use_stream
        self.max_retries = max(0, int(max_retries))
        self.timeout = float(timeout) if timeout is not None else 120.0
        self.thinking_budget = thinking_budget
        self.enable_explicit_cache: bool = (
            bool(enable_explicit_cache)
            if enable_explicit_cache is not None
            else (os.environ.get("AGENT_LAB_GEMINI_EXPLICIT_CACHE", "").strip() == "1")
        )
        resolved_min_cache_tokens = (
            max(1, int(min_cache_tokens))
            if min_cache_tokens is not None
            else (_parse_positive_int_env("AGENT_LAB_GEMINI_EXPLICIT_CACHE_MIN_TOKENS") or 32_768)
        )
        resolved_ttl = (
            max(60, int(explicit_cache_ttl_seconds))
            if explicit_cache_ttl_seconds is not None
            else (_parse_positive_int_env("AGENT_LAB_GEMINI_EXPLICIT_CACHE_TTL") or 3600)
        )
        self.explicit_cache_manager: GeminiExplicitCacheManager = (
            explicit_cache_manager
            if explicit_cache_manager is not None
            else GeminiExplicitCacheManager(
                min_cache_tokens=resolved_min_cache_tokens,
                ttl_seconds=resolved_ttl,
            )
        )
        self.rate_limiter = rate_limiter
        self._http_client = http_client
        self._transport = transport

    def _build_headers(self) -> dict[str, str]:
        # 严格通过请求头传递 x-goog-api-key，绝不将密钥拼入 URL query string 防止访问日志泄露！
        base: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key and not _has_header(self.extra_headers, "x-goog-api-key"):
            base["x-goog-api-key"] = self.api_key
        return _merge_headers(base, self.extra_headers)

    def _to_gemini_contents(
        self, messages: list[dict], vision_attachments: list[dict] | None = None
    ) -> list[dict[str, Any]]:
        id_to_name: dict[str, str] = {}
        contents: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            role = "model" if msg.get("role") == "assistant" else "user"
            content = msg.get("content")
            is_last_user = role == "user" and idx == len(messages) - 1
            parts: list[dict[str, Any]] = []
            if isinstance(content, str):
                if content:
                    parts.append({"text": content})
            elif isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text" and blk.get("text"):
                        parts.append({"text": str(blk["text"])})
                    elif btype == "compaction" and isinstance(blk.get("content"), str) and blk["content"]:
                        parts.append({"text": blk["content"]})
                    elif btype == "tool_use":
                        tid = str(blk.get("id") or "")
                        tname = str(blk.get("name") or "")
                        if tid and tname:
                            id_to_name[tid] = tname
                        parts.append({"functionCall": {"name": tname, "args": blk.get("input") or {}}})
                    elif btype == "tool_result":
                        tid = str(blk.get("tool_use_id") or "")
                        tname = id_to_name.get(tid, "tool")
                        raw_out = blk.get("content")
                        if isinstance(raw_out, str):
                            try:
                                parsed_out = json.loads(raw_out)
                                resp_obj = parsed_out if isinstance(parsed_out, dict) else {"result": parsed_out}
                            except json.JSONDecodeError:
                                resp_obj = {"result": raw_out}
                        elif isinstance(raw_out, dict):
                            resp_obj = raw_out
                        else:
                            resp_obj = {"result": _stringify_tool_result_content(raw_out)}
                        parts.append({"functionResponse": {"name": tname, "response": resp_obj}})
            if is_last_user and vision_attachments:
                for att in vision_attachments:
                    if att.get("base64_data"):
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": att.get("media_type", "image/png"),
                                    "data": att["base64_data"],
                                }
                            }
                        )
                    elif att.get("data_url"):
                        parsed_uri = _parse_data_uri(att["data_url"])
                        if parsed_uri is not None:
                            parts.append({"inlineData": {"mimeType": parsed_uri[0], "data": parsed_uri[1]}})
            if not parts:
                continue
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        return contents

    def _to_gemini_tools(self, tools: list[dict]) -> list[dict[str, Any]]:
        if not tools:
            return []
        norm_tools = canonical_tools(tools)
        decls = []
        for t in norm_tools:
            decls.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": _sanitize_gemini_schema(
                        t.get("input_schema", {"type": "object", "properties": {}})
                    ),
                }
            )
        return [{"functionDeclarations": decls}]

    def _parse_gemini_chunk(
        self,
        chunk: dict[str, Any],
        *,
        text_parts: list[str],
        thinking_parts: list[str],
        tool_blocks: list[dict[str, Any]],
        used_ids: set[str],
        usage_acc: dict[str, int],
        finish_reasons: list[str],
    ) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        meta = chunk.get("usageMetadata") or {}
        if isinstance(meta, dict):
            if meta.get("promptTokenCount") is not None:
                usage_acc["input_tokens"] = int(meta["promptTokenCount"])
            if meta.get("candidatesTokenCount") is not None:
                usage_acc["output_tokens"] = int(meta["candidatesTokenCount"])
            if meta.get("cachedContentTokenCount") is not None:
                usage_acc["cache_read_input_tokens"] = int(meta["cachedContentTokenCount"])
            if meta.get("thoughtsTokenCount") is not None:
                usage_acc["reasoning_tokens"] = int(meta["thoughtsTokenCount"])

        for cand in chunk.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            if cand.get("finishReason"):
                finish_reasons.append(str(cand["finishReason"]))
            parts = ((cand.get("content") or {}).get("parts")) or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("thought") is True and part.get("text"):
                    t_txt = str(part["text"])
                    thinking_parts.append(t_txt)
                    emitted.append({"type": "thinking_delta", "delta": t_txt})
                elif "text" in part and part["text"]:
                    txt = str(part["text"])
                    text_parts.append(txt)
                    emitted.append({"type": "text_delta", "delta": txt})
                elif isinstance(part.get("functionCall"), dict):
                    fc = part["functionCall"]
                    tname = str(fc.get("name") or "")
                    targs = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                    cid = _normalize_tool_call_id(fc.get("id") or f"gemini_fc_{len(tool_blocks)}", used_ids)
                    used_ids.add(cid)
                    tool_blocks.append({"type": "tool_use", "id": cid, "name": tname, "input": targs})
                    emitted.append({"type": "tool_use_start", "call_id": cid, "name": tname})
        return emitted

    async def stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        prompt_cache_key: str | None = None,
        tail_state_count: int = 0,
        historical_tool_ids: set[str] | None = None,
        vision_attachments: list[dict] | None = None,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
        enable_explicit_cache: bool | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for ev in _stream_via_complete(
            self,
            system,
            messages,
            tools,
            max_tokens=max_tokens,
            cache_key=cache_key,
            prompt_cache_key=prompt_cache_key,
            tail_state_count=tail_state_count,
            historical_tool_ids=historical_tool_ids,
            vision_attachments=vision_attachments,
            model=model,
            cancel_event=cancel_event,
            enable_explicit_cache=enable_explicit_cache,
        ):
            yield ev

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        *,
        cache_key: str | None = None,
        prompt_cache_key: str | None = None,
        tail_state_count: int = 0,
        historical_tool_ids: set[str] | None = None,
        vision_attachments: list[dict] | None = None,
        on_event: StreamEventCallback | None = None,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
        enable_explicit_cache: bool | None = None,
    ) -> dict[str, Any]:
        del tail_state_count
        effective_model = (model or self.model).strip()
        effective_cache_key = prompt_cache_key or cache_key
        effective_explicit_cache = (
            bool(enable_explicit_cache)
            if enable_explicit_cache is not None
            else bool(getattr(self, "enable_explicit_cache", False))
        )
        gen_cfg: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if isinstance(self.thinking_budget, int) and self.thinking_budget > 0:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": self.thinking_budget}
        gemini_contents = self._to_gemini_contents(messages, vision_attachments)
        gemini_tools = self._to_gemini_tools(tools)

        def _build_generate_payload(cached_content_name: str | None) -> dict[str, Any]:
            body: dict[str, Any] = {
                "contents": gemini_contents,
                "generationConfig": gen_cfg,
            }
            if cached_content_name:
                # Gemini API 规范：携带 cachedContent 时不得重复携带 systemInstruction 或 tools
                body["cachedContent"] = cached_content_name
            else:
                if system:
                    body["systemInstruction"] = {"parts": [{"text": system}]}
                if gemini_tools:
                    body["tools"] = gemini_tools
            return body

        limiter = self.rate_limiter
        reserved_tokens = 0
        if limiter is not None:
            est = _estimate_request_tokens(system, messages, tools, max_tokens)
            reserved_tokens = await limiter.reserve(est)

        should_stream = bool(self.use_stream or on_event is not None)
        used_ids = _collect_historical_tool_ids(messages)
        if historical_tool_ids:
            used_ids.update(historical_tool_ids)
        urls = [self.base_url, *self.fallback_base_urls]
        headers = self._build_headers()
        own_client = self._http_client is None
        client = (
            self._http_client
            if not own_client
            else httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                transport=self._transport,
            )
        )
        encoded_model = urllib.parse.quote(effective_model, safe="-_.:")
        cache_mgr = getattr(self, "explicit_cache_manager", None)
        try:
            last_exc: Exception | None = None
            for base in urls:
                suffix = (
                    f"/models/{encoded_model}:streamGenerateContent?alt=sse"
                    if should_stream
                    else f"/models/{encoded_model}:generateContent"
                )
                endpoint = f"{base.rstrip('/')}{suffix}"
                active_cached_name: str | None = None
                if effective_explicit_cache and cache_mgr is not None:
                    active_cached_name = await cache_mgr.get_or_create(
                        client,
                        base_url=base,
                        headers=headers,
                        model=effective_model,
                        system=system,
                        tools=tools,
                        gemini_tools=gemini_tools,
                        display_name=effective_cache_key,
                        cancel_event=cancel_event,
                    )
                retried_uncached = False
                attempt = 0
                while attempt <= self.max_retries:
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError("Provider request cancelled")
                    payload = _build_generate_payload(active_cached_name)
                    text_parts: list[str] = []
                    thinking_parts: list[str] = []
                    tool_blocks: list[dict[str, Any]] = []
                    finish_reasons: list[str] = []
                    usage_acc = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "reasoning_tokens": 0,
                    }
                    try:
                        if should_stream:
                            async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                                if (
                                    resp.status_code in {400, 404}
                                    and active_cached_name is not None
                                    and not retried_uncached
                                ):
                                    await resp.aread()
                                    if cache_mgr is not None:
                                        cache_mgr.invalidate(cache_name=active_cached_name)
                                    active_cached_name = None
                                    retried_uncached = True
                                    continue
                                if resp.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                                    await resp.aread()
                                    await _sleep_with_cancel(
                                        _compute_backoff_delay(attempt, resp.headers.get("retry-after")),
                                        cancel_event,
                                    )
                                    attempt += 1
                                    continue
                                if resp.status_code >= 400:
                                    await resp.aread()
                                    resp.raise_for_status()
                                ctype = (resp.headers.get("content-type") or "").lower()
                                if "text/event-stream" in ctype:
                                    line_iter = resp.aiter_lines()
                                    while True:
                                        try:
                                            line = await _anext_with_timeout_and_cancel(
                                                line_iter,
                                                timeout=self.timeout,
                                                cancel_event=cancel_event,
                                            )
                                        except StopAsyncIteration:
                                            break
                                        if line is None:
                                            break
                                        if not line.startswith("data:"):
                                            continue
                                        data_str = line[5:].strip()
                                        if not data_str or data_str == "[DONE]":
                                            continue
                                        try:
                                            chunk_obj = json.loads(data_str)
                                        except json.JSONDecodeError:
                                            continue
                                        evs = self._parse_gemini_chunk(
                                            chunk_obj,
                                            text_parts=text_parts,
                                            thinking_parts=thinking_parts,
                                            tool_blocks=tool_blocks,
                                            used_ids=used_ids,
                                            usage_acc=usage_acc,
                                            finish_reasons=finish_reasons,
                                        )
                                        for ev in evs:
                                            await _emit_stream_event(on_event, ev)
                                else:
                                    raw = await _await_with_cancel(resp.aread(), cancel_event)
                                    parsed_body = json.loads(raw.decode("utf-8"))
                                    chunk_list = parsed_body if isinstance(parsed_body, list) else [parsed_body]
                                    for ch in chunk_list:
                                        if isinstance(ch, dict):
                                            evs = self._parse_gemini_chunk(
                                                ch,
                                                text_parts=text_parts,
                                                thinking_parts=thinking_parts,
                                                tool_blocks=tool_blocks,
                                                used_ids=used_ids,
                                                usage_acc=usage_acc,
                                                finish_reasons=finish_reasons,
                                            )
                                            for ev in evs:
                                                await _emit_stream_event(on_event, ev)
                        else:
                            resp = await _await_with_cancel(
                                client.post(endpoint, headers=headers, json=payload), cancel_event
                            )
                            if (
                                resp.status_code in {400, 404}
                                and active_cached_name is not None
                                and not retried_uncached
                            ):
                                if cache_mgr is not None:
                                    cache_mgr.invalidate(cache_name=active_cached_name)
                                active_cached_name = None
                                retried_uncached = True
                                continue
                            if resp.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                                await _sleep_with_cancel(
                                    _compute_backoff_delay(attempt, resp.headers.get("retry-after")),
                                    cancel_event,
                                )
                                attempt += 1
                                continue
                            resp.raise_for_status()
                            self._parse_gemini_chunk(
                                resp.json(),
                                text_parts=text_parts,
                                thinking_parts=thinking_parts,
                                tool_blocks=tool_blocks,
                                used_ids=used_ids,
                                usage_acc=usage_acc,
                                finish_reasons=finish_reasons,
                            )

                        content: list[dict[str, Any]] = []
                        if thinking_parts:
                            content.append({"type": "thinking", "thinking": "".join(thinking_parts)})
                        if text_parts:
                            content.append({"type": "text", "text": "".join(text_parts)})
                        content.extend(tool_blocks)

                        cost_info = compute_cost_usd(effective_model, usage_acc, additive_cache=False)
                        usage_out: dict[str, Any] = {
                            **usage_acc,
                            "cost_usd": cost_info["total_cost_usd"],
                            "cost_breakdown": cost_info,
                        }
                        if limiter is not None:
                            totals = compute_usage_totals(usage_out, additive_cache=False)
                            limiter.record_actual(
                                totals["total_input_tokens"] + totals["output_tokens"], reserved_tokens
                            )
                        truncated = any(fr == "MAX_TOKENS" for fr in finish_reasons)
                        stop_reason = (
                            "tool_use" if tool_blocks else ("max_tokens" if truncated else "end_turn")
                        )
                        result: dict[str, Any] = {
                            "content": content,
                            "stop_reason": stop_reason,
                            "usage": usage_out,
                            "truncated": truncated,
                        }
                        if on_event is not None:
                            if not should_stream:
                                await _emit_events_from_response(on_event, result)
                            else:
                                await _emit_stream_event(
                                    on_event,
                                    {
                                        "type": "message_stop",
                                        "stop_reason": result["stop_reason"],
                                        "usage": result["usage"],
                                        "response": result,
                                    },
                                )
                        return result
                    except httpx.HTTPStatusError as exc:
                        last_exc = exc
                        status_code = exc.response.status_code if exc.response is not None else 0
                        if status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                            ra = exc.response.headers.get("retry-after") if exc.response is not None else None
                            await _sleep_with_cancel(_compute_backoff_delay(attempt, ra), cancel_event)
                            attempt += 1
                            continue
                        break
                    except (httpx.TransportError, TimeoutError) as exc:
                        last_exc = exc
                        if attempt < self.max_retries:
                            await _sleep_with_cancel(_compute_backoff_delay(attempt, None), cancel_event)
                            attempt += 1
                            continue
                        break
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("Gemini 原生协议请求失败")
        finally:
            if own_client:
                await client.aclose()


def get_provider(
    name: str,
    *,
    state_dir: str | Path | None = None,
    workspace: str | Path | None = None,
    gateway_config: str | Path | None = None,
    trust_workspace: bool = False,
) -> Provider:
    """按协议名或 `gateways.json` 配置档名解析并返回对应的模型 Provider 适配器。"""
    clean = (name or "").strip()
    low = clean.lower()
    if low == "demo":
        return DemoProvider()
    if low in {"anthropic", "anthropic_messages"}:
        return AnthropicProvider()
    if low in {"openai", "openai_compat", "openai_chat", "openai_chat_completions"}:
        return OpenAICompatibleProvider()
    if low in {"openai_responses", "responses"}:
        return OpenAIResponsesProvider()
    if low in {"gemini", "google_gemini", "gemini_generate_content"}:
        return GeminiNativeProvider()
    if low.startswith("gateway:") or gateway_config is not None or state_dir is not None:
        from .gateways import resolve_provider_or_gateway

        prov, _ = resolve_provider_or_gateway(
            clean,
            state_dir=state_dir,
            workspace=workspace,
            explicit_config_path=gateway_config,
            trust_workspace=trust_workspace,
        )
        return prov
    raise ValueError(f"未知 provider 或网关配置档: {name}")

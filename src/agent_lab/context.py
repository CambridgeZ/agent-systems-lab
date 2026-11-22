"""按协议完整性裁剪对话，保留显式工作状态，并计入请求的固定开销。"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re
from typing import Any

_MESSAGE_BPE_OVERHEAD = 4
_CONVERSATION_BPE_OVERHEAD = 2

_BPE_PIECE_RE = re.compile(
    r"(?P<esc>\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[\"\\/bfnrt0']))"
    r"|(?P<nl>\r\n|[\r\n\t])"
    r"|(?P<cjk>[㐀-鿿豈-﫿]+)"
    r"|(?P<word> ?[A-Za-z]+)"
    r"|(?P<num>[0-9]+)"
    r"|(?P<sp> +)"
    r"|(?P<pair>\{\"|\":|\"\}|,\"|\[\{|\}\]|\[\]|\{\}|==|!=|<=|>=|->|\*\*|//|\+=|-=|\(\))"
    r"|(?P<ascii_punct>[\x00-\x7f])"
    r"|(?P<mb>[^\x00-\x7f㐀-鿿豈-﫿]+)"
)


def _count_str_bpe_tokens(text: str) -> int:
    if not text:
        return 0
    total = 0.0
    for match in _BPE_PIECE_RE.finditer(text):
        kind = match.lastgroup
        piece = match.group()
        if kind == "word":
            letters = len(piece) - (1 if piece[0] == " " else 0)
            total += (letters + 3) // 4
        elif kind == "num":
            total += (len(piece) + 2) // 3
        elif kind == "cjk":
            total += len(piece) * 1.5
        elif kind == "esc":
            total += 2.0 if len(piece) > 2 else 1.0
        elif kind == "nl":
            total += 1.0
        elif kind == "sp":
            total += (len(piece) + 3) // 4
        elif kind in {"pair", "ascii_punct"}:
            total += 1.0
        elif kind == "mb":
            total += len(piece.encode("utf-8")) * 0.5
    return math.ceil(total)


def _is_message_dict(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("role"), str) and "content" in value


def _message_bpe_tokens(message: dict[str, Any], *, _str_counter=_count_str_bpe_tokens) -> int:
    tokens = _MESSAGE_BPE_OVERHEAD + _str_counter(str(message.get("role", "")))
    content = message.get("content")
    if isinstance(content, str):
        tokens += _str_counter(content)
    elif isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and len(block) == 2
            ):
                tokens += 2 + _str_counter(block["text"])
            else:
                tokens += 3 + _str_counter(
                    json.dumps(block, ensure_ascii=False, separators=(",", ":"))
                )
    elif content is not None:
        tokens += _str_counter(json.dumps(content, ensure_ascii=False, separators=(",", ":")))
    for key, val in message.items():
        if key not in {"role", "content"}:
            tokens += _str_counter(str(key)) + _str_counter(
                json.dumps(val, ensure_ascii=False, separators=(",", ":"))
            )
    return tokens


def estimate_bpe_tokens(value: Any, *, use_tiktoken: bool | None = None) -> int:
    """确定性、零外部依赖的校准 BPE token 估算器（显式开启且本地可用时支持 tiktoken cl100k_base）。"""
    opt_in = (
        os.environ.get("AGENT_LAB_USE_TIKTOKEN") == "1"
        if use_tiktoken is None
        else bool(use_tiktoken)
    )
    str_counter = _count_str_bpe_tokens
    if opt_in:
        try:
            import tiktoken  # type: ignore[import-not-found]

            enc = tiktoken.get_encoding("cl100k_base")

            def _tiktoken_count(text: str) -> int:
                return len(enc.encode(text)) if text else 0

            str_counter = _tiktoken_count
        except Exception:
            str_counter = _count_str_bpe_tokens

    if value is None:
        return 0
    if isinstance(value, str):
        return str_counter(value)
    if isinstance(value, dict):
        if _is_message_dict(value):
            return _message_bpe_tokens(value, _str_counter=str_counter)
        return str_counter(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    if isinstance(value, list):
        if not value:
            return 0
        if all(_is_message_dict(item) for item in value):
            return _CONVERSATION_BPE_OVERHEAD + sum(
                _message_bpe_tokens(item, _str_counter=str_counter) for item in value
            )
        return str_counter(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return str_counter(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _blocks(message: dict, kind: str) -> list[dict]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == kind]


def _item_size(message: dict) -> int:
    # 字符预算可复演，但不是 tokenizer：中文、英文和 schema 的 token 比例不同。
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))


def _size(messages: list[dict]) -> int:
    # 单独序列化每条消息，避免为很长的原始日志再分配一个同样大的 JSON 字符串。
    # JSON 数组长度 = 两侧括号 + 每项长度 + 项间逗号。
    return 2 + sum(_item_size(message) for message in messages) + max(0, len(messages) - 1)


class ContextOverflow(ValueError):
    """原任务、固定工作状态或最新完整工具交换无法装入剩余字符预算。"""


class ContextManager:
    """保留最初任务、显式 pinned 工作状态与最近完整工具交换。

    一条 assistant 消息可能同时请求多个工具；紧随其后的 user 消息必须含有所有
    对应 tool_result。它们组成一个原子组，绝不只留下结果或只留下调用。

    pinned 是运行时从持久状态重建的普通 user 消息，例如当前审批对象和已加载技能；
    它不会升级成 system 权限。裁剪只移除旧历史，不让这些显式工作状态随历史消失。
    reserved_chars 则由调用方提供，用于预留系统指令、工具目录等非 messages 开销。
    这是序列化字符预算，既不包含服务商隐藏包装，也不保证等于真实 token 上限。
    """

    def __init__(
        self,
        max_chars: int = 24_000,
        *,
        low_watermark_ratio: float = 1.0,
        max_tokens: int | None = None,
    ):
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            raise ValueError("max_chars 必须是正整数")
        if (
            isinstance(low_watermark_ratio, bool)
            or not isinstance(low_watermark_ratio, (int, float))
            or not 0.2 <= float(low_watermark_ratio) <= 1.0
        ):
            raise ValueError("low_watermark_ratio 必须在 0.2 到 1.0 之间")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        ):
            raise ValueError("max_tokens 必须是正整数")
        self.max_chars = max_chars
        self.low_watermark_ratio = float(low_watermark_ratio)
        self.max_tokens = max_tokens
        self._stats: dict = {}

    def _atomic_groups(self, messages: list[dict]) -> list[list[dict]]:
        groups: list[list[dict]] = []
        used_ids: set[str] = set()
        index = 0
        while index < len(messages):
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                raise ValueError("上下文消息只能使用 user/assistant 角色")
            if _blocks(message, "tool_result"):
                raise ValueError("发现孤立 tool_result：缺少紧邻的 assistant tool_use")
            calls = _blocks(message, "tool_use")
            if not calls:
                groups.append([message])
                index += 1
                continue
            if message["role"] != "assistant":
                raise ValueError("tool_use 必须属于 assistant 消息")
            ids = [call.get("id") for call in calls]
            if (
                any(not isinstance(call_id, str) or not call_id for call_id in ids)
                or len(set(ids)) != len(ids)
                or used_ids.intersection(ids)
            ):
                raise ValueError("tool_use id 必须非空且在对话中唯一")
            used_ids.update(ids)
            if index + 1 >= len(messages):
                raise ValueError("tool_use 尚无结果，不能构建下一轮模型上下文")
            following = messages[index + 1]
            if not isinstance(following, dict) or following.get("role") != "user":
                raise ValueError("tool_use 后必须紧跟 user tool_result 消息")
            results = _blocks(following, "tool_result")
            result_ids = [result.get("tool_use_id") for result in results]
            if (
                any(not isinstance(result_id, str) or not result_id for result_id in result_ids)
                or len(result_ids) != len(ids)
                or set(result_ids) != set(ids)
            ):
                raise ValueError("tool_result 必须与本轮所有 tool_use 一一对应")
            if _blocks(following, "tool_use"):
                raise ValueError("user 消息不能包含 tool_use")
            groups.append([message, following])
            index += 2
        return groups

    @staticmethod
    def _with_notice(first: dict, dropped: int) -> dict:
        if not dropped:
            return deepcopy(first)
        first = deepcopy(first)
        notice = (
            f"\n\n[上下文管理：已有 {dropped} 条较早消息按完整工具往返移除。"
            "持久化对话与调用账本仍保留记录；不能假定被移除的细节，必要时重新读取证据。]"
        )
        if isinstance(first.get("content"), str):
            first["content"] += notice
        elif isinstance(first.get("content"), list):
            first["content"].append({"type": "text", "text": notice})
        else:
            raise ValueError("消息 content 必须是字符串或内容块列表")
        return first

    @staticmethod
    def _validate_pinned(pinned: list[dict]) -> None:
        # pinned 位于历史工具交换之前，不能带孤立结果或伪造调用；只接受低权限文本。
        for message in pinned:
            if not isinstance(message, dict) or message.get("role") != "user":
                raise ValueError("pinned 必须是普通 user 文本消息，不能使用 system/assistant 角色")
            content = message.get("content")
            if isinstance(content, str):
                continue
            if (
                not isinstance(content, list)
                or not content
                or any(
                    not isinstance(block, dict)
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                    for block in content
                )
            ):
                raise ValueError("pinned 只接受字符串或 text 块，不能包含工具调用/结果")

    def build(
        self,
        messages: list[dict],
        *,
        pinned: list[dict] | None = None,
        tail_state: list[dict] | None = None,
        reserved_chars: int = 0,
        reserved_tokens: int = 0,
        min_dropped_groups: int = 0,
    ) -> list[dict]:
        """构造预算内的消息副本；原任务与必需内容装不下时显式失败。

        不截断 JSON、源码或最新工具结果。大结果应由运行时先保存，再投影为包含
        取回引用的有界结果；本层只负责保留协议与删除完整旧组，不假装生成语义摘要。
        为提高 KV Cache 前缀命中率，支持两项机制：
        1. tail_state 将每步高频变化的 HARNESS_STATE / WORKING_MEMORY 追加在末尾，不污染历史前缀；
        2. min_dropped_groups + low_watermark_ratio 实现阶梯式水位压缩，避免满预算后每步滑动左移。
        """
        if isinstance(reserved_chars, bool) or not isinstance(reserved_chars, int) or reserved_chars < 0:
            raise ValueError("reserved_chars 必须是非负整数")
        if isinstance(reserved_tokens, bool) or not isinstance(reserved_tokens, int) or reserved_tokens < 0:
            raise ValueError("reserved_tokens 必须是非负整数")
        if isinstance(min_dropped_groups, bool) or not isinstance(min_dropped_groups, int) or min_dropped_groups < 0:
            raise ValueError("min_dropped_groups 必须是非负整数")
        if not isinstance(messages, list):
            raise ValueError("messages 必须是消息列表")
        if pinned is None:
            pinned = []
        if not isinstance(pinned, list):
            raise ValueError("pinned 必须是消息列表")
        if tail_state is None:
            tail_state = []
        if not isinstance(tail_state, list):
            raise ValueError("tail_state 必须是消息列表")
        self._validate_pinned(pinned)
        self._validate_pinned(tail_state)
        if not messages and (pinned or tail_state):
            raise ValueError("提供 pinned 时仍需要最初的 user 任务消息")

        # 只扫描原始引用；验证与裁剪不修改它们。最终仅复制保留部分，避免复制整份长日志。
        if messages and (not isinstance(messages[0], dict) or messages[0].get("role") != "user"):
            raise ValueError("上下文必须从 user 任务消息开始")
        groups = self._atomic_groups(messages)
        if groups and len(groups[0]) != 1:
            raise ValueError("首条消息必须是普通 user 任务，不能是工具结果")
        original_size = _size(messages)
        original_tokens = estimate_bpe_tokens(messages)
        effective_budget = self.max_chars - reserved_chars
        effective_token_budget = (
            self.max_tokens - reserved_tokens if self.max_tokens is not None else None
        )
        pinned_cost = sum(_item_size(message) + 1 for message in pinned)
        pinned_size = _size(pinned) if pinned else 0
        pinned_token_cost = sum(_message_bpe_tokens(message) for message in pinned)
        tail_state_cost = sum(_item_size(message) + 1 for message in tail_state)
        tail_state_size = _size(tail_state) if tail_state else 0
        tail_state_token_cost = sum(_message_bpe_tokens(message) for message in tail_state)
        tail = groups[1:]
        # 每条消息多计一个分隔逗号；存在首条任务时，它恰好等于追加各组的字符增量。
        group_costs = [sum(_item_size(message) + 1 for message in group) for group in tail]
        group_token_costs = [sum(_message_bpe_tokens(message) for message in group) for group in tail]
        start_cursor = min(min_dropped_groups, max(0, len(tail) - 1)) if tail else 0
        dropped_groups = start_cursor
        removed = sum(len(group) for group in tail[:dropped_groups])
        remaining_cost = sum(group_costs[dropped_groups:])
        remaining_token_cost = sum(group_token_costs[dropped_groups:])
        overflow_triggered = False
        while True:
            first = self._with_notice(messages[0], removed) if messages else None
            fixed_overhead = (2 + _item_size(first) + pinned_cost + tail_state_cost) if first else 2
            candidate_size = fixed_overhead + remaining_cost if first else 2
            fixed_token_overhead = (
                (
                    _CONVERSATION_BPE_OVERHEAD
                    + _message_bpe_tokens(first)
                    + pinned_token_cost
                    + tail_state_token_cost
                )
                if first
                else 0
            )
            candidate_tokens = fixed_token_overhead + remaining_token_cost if first else 0
            fits_chars = candidate_size <= effective_budget
            fits_tokens = (
                effective_token_budget is None or candidate_tokens <= effective_token_budget
            )
            fits_budget = fits_chars and fits_tokens
            if fits_budget and overflow_triggered and self.low_watermark_ratio < 1.0:
                history_capacity = max(0, effective_budget - fixed_overhead)
                low_target = int(history_capacity * self.low_watermark_ratio)
                low_token_target = (
                    int(max(0, effective_token_budget - fixed_token_overhead) * self.low_watermark_ratio)
                    if effective_token_budget is not None
                    else None
                )
                compacted_further = False
                while len(tail) - dropped_groups > 1 and (
                    remaining_cost > low_target
                    or (low_token_target is not None and remaining_token_cost > low_token_target)
                ):
                    removed += len(tail[dropped_groups])
                    remaining_cost -= group_costs[dropped_groups]
                    remaining_token_cost -= group_token_costs[dropped_groups]
                    dropped_groups += 1
                    compacted_further = True
                if compacted_further:
                    first = self._with_notice(messages[0], removed) if messages else None
                    fixed_overhead = (
                        (2 + _item_size(first) + pinned_cost + tail_state_cost) if first else 2
                    )
                    candidate_size = fixed_overhead + remaining_cost if first else 2
                    fixed_token_overhead = (
                        (
                            _CONVERSATION_BPE_OVERHEAD
                            + _message_bpe_tokens(first)
                            + pinned_token_cost
                            + tail_state_token_cost
                        )
                        if first
                        else 0
                    )
                    candidate_tokens = fixed_token_overhead + remaining_token_cost if first else 0
                    fits_chars = candidate_size <= effective_budget
                    fits_tokens = (
                        effective_token_budget is None or candidate_tokens <= effective_token_budget
                    )
                    fits_budget = fits_chars and fits_tokens
            self._stats = {
                "original_messages": len(messages),
                "kept_messages": len(messages) - removed + len(pinned) + len(tail_state),
                "kept_history_messages": len(messages) - removed,
                "dropped_messages": removed,
                "dropped_groups": dropped_groups,
                "original_chars": original_size,
                "kept_chars": candidate_size,
                "pinned_messages": len(pinned),
                "pinned_chars": pinned_size,
                "tail_state_messages": len(tail_state),
                "tail_state_chars": tail_state_size,
                "compaction_cursor": dropped_groups,
                "compaction_triggered": dropped_groups > start_cursor,
                "low_watermark_ratio": self.low_watermark_ratio,
                "reserved_chars": reserved_chars,
                "effective_message_budget": effective_budget,
                "request_chars": candidate_size + reserved_chars,
                "max_chars": self.max_chars,
                "original_tokens": original_tokens,
                "estimated_tokens": candidate_tokens,
                "reserved_tokens": reserved_tokens,
                "effective_token_budget": effective_token_budget,
                "request_tokens": candidate_tokens + reserved_tokens,
                "max_tokens": self.max_tokens,
                "budget_kind": (
                    "serialized_characters_not_tokens"
                    if self.max_tokens is None
                    else "chars_and_bpe_tokens"
                ),
                "overflow": not fits_budget,
            }
            if fits_budget:
                candidate = (
                    ([first] + pinned if first else [])
                    + [message for group in tail[dropped_groups:] for message in group]
                    + tail_state
                )
                return deepcopy(candidate)
            # 最新一组不能删除；只剩下原任务、固定工作状态时也不能继续缩小。
            if len(tail) - dropped_groups <= 1:
                if not fits_chars:
                    raise ContextOverflow("原任务、固定工作状态与最新完整消息组超过上下文字符预算")
                raise ContextOverflow("原任务、固定工作状态与最新完整消息组超过上下文 BPE token 预算")
            overflow_triggered = True
            removed += len(tail[dropped_groups])
            remaining_cost -= group_costs[dropped_groups]
            remaining_token_cost -= group_token_costs[dropped_groups]
            dropped_groups += 1

    def stats(self) -> dict:
        return dict(self._stats)

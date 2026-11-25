"""Harness 级 KV Cache 前缀对齐、链式块哈希追踪与离线消融基准。

核心原理：
1. 自回归 Transformer 的 KV Cache 严格依赖前缀序列与位置编码（RoPE）：前部哪怕改动 1 个字符，
   后续所有 PagedAttention / RadixAttention 物理块哈希都会级联失效。
2. 本模块提供：
   - 确定性 JSON 序列化与工具目录规范化，消除字典键序和动态时间戳噪声；
   - 模拟 vLLM Automatic Prefix Caching 的链式块哈希（H_k = SHA256(H_{k-1} || block_k)）追踪器；
   - Anthropic 三段式显式 cache_control 断点组装（同时合并相邻 user 消息满足严格角色交替）；
   - 可复现实验 simulate_kv_cache_ablation()，对比“前置动态状态 + 逐轮滑动裁剪”与
     “尾部瞬态状态 + 高低水位阶梯压缩 + 共享工具前缀”的真实块命中率差异。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

DEFAULT_BLOCK_CHARS = 64  # 约对应 16—20 个源码/JSON token，贴近 vLLM 默认 16-token 物理块粒度
_MAX_TRACKED_BLOCKS = 2048
_PINNED_ROOT_BLOCKS = 128
_NON_CACHEABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def canonical_json(value: Any) -> str:
    """确定性紧凑 JSON：固定字典键顺序与分隔符，避免同义对象产生不同字节前缀。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_tools(tools: list[dict]) -> list[dict]:
    """保持工具列表顺序不变，并将每个 schema 内部的键顺序规范化。"""
    return json.loads(canonical_json(tools))


def split_system_role(system: str) -> tuple[str, str | None]:
    """拆分共享基础系统指令与子 Agent 尾部角色后缀，使主从 Agent 共享基础前缀块。"""
    marker = "\nROLE:"
    if marker in system:
        base, suffix = system.split(marker, 1)
        return base, "ROLE:" + suffix
    return system, None


def chained_block_hashes(stream: str, block_chars: int = DEFAULT_BLOCK_CHARS) -> list[str]:
    """计算链式前缀块哈希：H_i = SHA256(H_{i-1} || block_i)。

    只对写满 block_chars 的完整块生成哈希（与 vLLM 仅缓存完整物理页一致）；
    前序任意 1 个字节变化都会令该块及后续所有块的哈希全部改变。
    """
    if block_chars < 16:
        raise ValueError("block_chars 至少为 16")
    full_count = len(stream) // block_chars
    parent = "kv_root"
    hashes: list[str] = []
    for idx in range(full_count):
        chunk = stream[idx * block_chars : (idx + 1) * block_chars]
        parent = hashlib.sha256((parent + "\0" + chunk).encode("utf-8")).hexdigest()[:24]
        hashes.append(parent)
    return hashes


def serialize_kv_stream(system: str, tools: list[dict], messages: list[dict]) -> str:
    """按推理服务端常见的前缀层级（Tools -> Base System -> Role -> Messages）生成规范化字符流。"""
    base_system, role_suffix = split_system_role(system)
    parts = [
        "<TOOLS>\n" + canonical_json(tools),
        "<SYSTEM_BASE>\n" + base_system,
    ]
    if role_suffix:
        parts.append("<SYSTEM_ROLE>\n" + role_suffix)
    for message in messages:
        parts.append("<MSG>\n" + canonical_json(message))
    return "\n".join(parts)


def record_kv_step(
    kv_state: dict[str, Any] | None,
    *,
    system: str,
    tools: list[dict],
    messages: list[dict],
    tail_state_count: int = 0,
    worker: str = "coordinator",
    block_chars: int = DEFAULT_BLOCK_CHARS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """记录单次模型请求的链式前缀块命中情况，返回更新后的持久状态与单步指标。"""
    state = deepcopy(kv_state) if isinstance(kv_state, dict) else {}
    block_chars = int(state.get("block_chars", block_chars))
    lru: dict[str, None] = dict.fromkeys(state.get("known_blocks", []))

    stream = serialize_kv_stream(system, tools, messages)
    hashes = chained_block_hashes(stream, block_chars=block_chars)
    hit_blocks = 0
    for digest_hex in hashes:
        if digest_hex in lru:
            hit_blocks += 1
        else:
            break

    # 命中的旧块与新增块一并移至 MRU 尾部；驱逐时始终固定前 min(128, len(hashes)) 个根前缀块（<TOOLS>+<SYSTEM_BASE>）
    for digest_hex in hashes:
        lru.pop(digest_hex, None)
        lru[digest_hex] = None

    if len(lru) > _MAX_TRACKED_BLOCKS:
        pinned_roots = hashes[: min(_PINNED_ROOT_BLOCKS, len(hashes))]
        pinned_set = set(pinned_roots)
        tail_budget = max(0, _MAX_TRACKED_BLOCKS - len(pinned_set))
        non_pinned = [b for b in lru if b not in pinned_set]
        kept_tail = non_pinned[-tail_budget:] if tail_budget > 0 else []
        known_list = list(dict.fromkeys([*pinned_roots, *kept_tail]))[:_MAX_TRACKED_BLOCKS]
    else:
        known_list = list(lru)

    total_chars = len(stream)
    cached_chars = min(total_chars, hit_blocks * block_chars)
    uncached_chars = total_chars - cached_chars
    step_hit_rate = round(cached_chars / total_chars, 4) if total_chars else 0.0

    steps_recorded = int(state.get("steps_recorded", 0)) + 1
    cum_total_chars = int(state.get("total_chars", 0)) + total_chars
    cum_cached_chars = int(state.get("cached_chars", 0)) + cached_chars
    cum_uncached_chars = int(state.get("uncached_chars", 0)) + uncached_chars
    cum_total_blocks = int(state.get("total_blocks", 0)) + len(hashes)
    cum_hit_blocks = int(state.get("hit_blocks", 0)) + hit_blocks
    cumulative_hit_rate = round(cum_cached_chars / cum_total_chars, 4) if cum_total_chars else 0.0

    step_report = {
        "step_index": steps_recorded,
        "worker": worker,
        "total_chars": total_chars,
        "cached_chars": cached_chars,
        "uncached_chars": uncached_chars,
        "total_blocks": len(hashes),
        "hit_blocks": hit_blocks,
        "miss_blocks": len(hashes) - hit_blocks,
        "tail_state_messages": tail_state_count,
        "step_hit_rate": step_hit_rate,
        "cumulative_hit_rate": cumulative_hit_rate,
    }
    history = list(state.get("history", []))[-23:] + [step_report]
    updated = {
        "block_chars": block_chars,
        "known_blocks": known_list,
        "steps_recorded": steps_recorded,
        "total_chars": cum_total_chars,
        "cached_chars": cum_cached_chars,
        "uncached_chars": cum_uncached_chars,
        "total_blocks": cum_total_blocks,
        "hit_blocks": cum_hit_blocks,
        "last_step_hit_rate": step_hit_rate,
        "cumulative_hit_rate": cumulative_hit_rate,
        "provider_cache_read_input_tokens": int(state.get("provider_cache_read_input_tokens", 0)),
        "provider_cache_creation_input_tokens": int(state.get("provider_cache_creation_input_tokens", 0)),
        "history": history,
    }
    return updated, step_report


def record_provider_cache_usage(kv_state: dict[str, Any] | None, usage: dict[str, Any]) -> dict[str, Any]:
    """累加真实模型服务商返回的缓存命中与缓存写入 token 数。"""
    state = deepcopy(kv_state) if isinstance(kv_state, dict) else {}
    read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
    write_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
    state["provider_cache_read_input_tokens"] = int(state.get("provider_cache_read_input_tokens", 0)) + read_tokens
    state["provider_cache_creation_input_tokens"] = (
        int(state.get("provider_cache_creation_input_tokens", 0)) + write_tokens
    )
    return state


def summarize_kv_cache(kv_state: dict[str, Any] | None) -> dict[str, Any]:
    """供 API 与前端展示的轻量摘要，不暴露内部几千个块哈希列表。"""
    if not isinstance(kv_state, dict):
        return {
            "steps_recorded": 0,
            "total_chars": 0,
            "cached_chars": 0,
            "uncached_chars": 0,
            "last_step_hit_rate": 0.0,
            "cumulative_hit_rate": 0.0,
            "provider_cache_read_input_tokens": 0,
            "provider_cache_creation_input_tokens": 0,
        }
    return {
        "block_chars": kv_state.get("block_chars", DEFAULT_BLOCK_CHARS),
        "steps_recorded": kv_state.get("steps_recorded", 0),
        "total_chars": kv_state.get("total_chars", 0),
        "cached_chars": kv_state.get("cached_chars", 0),
        "uncached_chars": kv_state.get("uncached_chars", 0),
        "total_blocks": kv_state.get("total_blocks", 0),
        "hit_blocks": kv_state.get("hit_blocks", 0),
        "last_step_hit_rate": kv_state.get("last_step_hit_rate", 0.0),
        "cumulative_hit_rate": kv_state.get("cumulative_hit_rate", 0.0),
        "provider_cache_read_input_tokens": kv_state.get("provider_cache_read_input_tokens", 0),
        "provider_cache_creation_input_tokens": kv_state.get("provider_cache_creation_input_tokens", 0),
    }


def _to_content_blocks(content: str | list[dict]) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    copied = deepcopy(content)
    sanitized: list[dict] = []
    for block in copied:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in _NON_CACHEABLE_BLOCK_TYPES:
            block.pop("cache_control", None)
            sanitized.append(block)
        elif btype == "compaction":
            comp_blk: dict[str, Any] = {"type": "compaction"}
            raw_content = block.get("content")
            if isinstance(raw_content, str):
                comp_blk["content"] = raw_content if raw_content != "" else None
            elif raw_content is None and "content" in block:
                comp_blk["content"] = None
            elif raw_content is not None:
                comp_blk["content"] = str(raw_content)
            if block.get("encrypted_content") is not None:
                comp_blk["encrypted_content"] = str(block["encrypted_content"])
            if block.get("cache_control") is not None:
                comp_blk["cache_control"] = block["cache_control"]
            sanitized.append(comp_blk)
        else:
            sanitized.append(block)
    return sanitized


def _is_cacheable_block(block: dict[str, Any]) -> bool:
    btype = block.get("type")
    if btype in _NON_CACHEABLE_BLOCK_TYPES:
        return False
    if btype == "compaction" and not block.get("content") and not block.get("encrypted_content"):
        return False
    return True


def build_anthropic_context_management(
    *,
    enable_tool_clearing: bool = False,
    enable_thinking_clearing: bool = False,
    enable_server_compaction: bool = False,
    trigger_tokens: int = 150_000,
    keep_recent_tool_uses: int = 4,
    keep_recent_thinking_turns: int = 1,
    clear_tool_inputs: bool = False,
    exclude_tools: list[str] | None = None,
    compaction_instructions: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """构造 Anthropic 原生上下文工程配置（context_management.edits）及对应 Beta 请求头列表。

    规则与顺序约束：
    - 默认全部开关为 False 时返回 `(None, [])`，对既有请求 100% 零改动；
    - 策略顺序遵循 Anthropic 服务端要求：`clear_thinking_20251015` 必须排在 `clear_tool_uses_20250919` 之前，
      `compact_20260112` 位于末尾作为上下文超限时的服务端压缩兜底；
    - 自动去重并按稳定顺序返回必需的 `anthropic-beta` 标识（`context-management-2025-06-27` / `compact-2026-01-12`）。
    """
    edits: list[dict[str, Any]] = []
    betas: list[str] = []
    safe_trigger = max(1, int(trigger_tokens))

    if enable_thinking_clearing:
        edits.append(
            {
                "type": "clear_thinking_20251015",
                "keep": {
                    "type": "thinking_turns",
                    "value": max(1, int(keep_recent_thinking_turns)),
                },
            }
        )
        if "context-management-2025-06-27" not in betas:
            betas.append("context-management-2025-06-27")

    if enable_tool_clearing:
        tool_edit: dict[str, Any] = {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": safe_trigger},
            "keep": {"type": "tool_uses", "value": max(0, int(keep_recent_tool_uses))},
        }
        if clear_tool_inputs:
            tool_edit["clear_tool_inputs"] = True
        if exclude_tools:
            cleaned_excludes = [str(t).strip() for t in exclude_tools if isinstance(t, str) and t.strip()]
            if cleaned_excludes:
                tool_edit["exclude_tools"] = cleaned_excludes
        edits.append(tool_edit)
        if "context-management-2025-06-27" not in betas:
            betas.append("context-management-2025-06-27")

    if enable_server_compaction:
        compact_edit: dict[str, Any] = {
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": safe_trigger},
        }
        if isinstance(compaction_instructions, str) and compaction_instructions.strip():
            compact_edit["instructions"] = compaction_instructions.strip()
        edits.append(compact_edit)
        if "compact-2026-01-12" not in betas:
            betas.append("compact-2026-01-12")

    if not edits:
        return None, []
    return {"edits": edits}, betas


def build_anthropic_cached_payload(
    system: str,
    messages: list[dict],
    tools: list[dict],
    *,
    max_tokens: int = 2048,
    tail_state_count: int = 0,
    thinking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造带 3 处显式 cache_control 断点且严格满足角色交替的 Anthropic 请求负载。

    断点布局（Anthropic 限制单请求最多 4 个断点）：
    1. tools[-1]：缓存整套规范化工具 JSON Schema；
    2. system[0]（共享基础系统提示）：主协调者与只读子 Agent 共享同一缓存前缀；
    3. 尾部瞬态状态之前的最后一个稳定历史内容块：使每步新增工具往返命中滚动历史缓存。
       若目标块为 thinking / redacted_thinking，则向前回退至最近的 text / tool_use / tool_result / compaction 块。
    """
    cached_tools = canonical_tools(tools)
    if cached_tools:
        cached_tools[-1]["cache_control"] = {"type": "ephemeral"}

    base_system, role_suffix = split_system_role(system)
    system_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": base_system, "cache_control": {"type": "ephemeral"}}
    ]
    if role_suffix:
        system_blocks.append({"type": "text", "text": role_suffix})

    stable_cutoff = max(0, len(messages) - max(0, tail_state_count))
    coalesced: list[dict[str, Any]] = []
    last_stable_coord: tuple[int, int] | None = None

    for idx, msg in enumerate(messages):
        role = msg["role"]
        blocks = _to_content_blocks(msg["content"])
        if not blocks:
            continue
        if coalesced and coalesced[-1]["role"] == role:
            coalesced[-1]["content"].extend(blocks)
        else:
            coalesced.append({"role": role, "content": blocks})
        if idx < stable_cutoff:
            last_stable_coord = (len(coalesced) - 1, len(coalesced[-1]["content"]) - 1)

    if last_stable_coord is not None:
        msg_idx, blk_idx = last_stable_coord
        placed = False
        for b_idx in range(blk_idx, -1, -1):
            candidate = coalesced[msg_idx]["content"][b_idx]
            if _is_cacheable_block(candidate):
                candidate["cache_control"] = {"type": "ephemeral"}
                placed = True
                break
        if not placed:
            for m_idx in range(msg_idx - 1, -1, -1):
                msg_blocks = coalesced[m_idx]["content"]
                for b_idx in range(len(msg_blocks) - 1, -1, -1):
                    if _is_cacheable_block(msg_blocks[b_idx]):
                        msg_blocks[b_idx]["cache_control"] = {"type": "ephemeral"}
                        placed = True
                        break
                if placed:
                    break

    payload: dict[str, Any] = {
        "system": system_blocks,
        "messages": coalesced,
        "tools": cached_tools,
        "max_tokens": max_tokens,
    }
    if thinking is not None:
        payload["thinking"] = deepcopy(thinking)
    return payload


def simulate_kv_cache_ablation(
    turns: int = 16,
    max_chars: int = 6800,
    block_chars: int = DEFAULT_BLOCK_CHARS,
) -> dict[str, Any]:
    """在完全相同的多子代理 + 工具轨迹上对比优化前后的链式前缀块命中率。

    - legacy：工具子集过滤 + HARNESS_STATE/WORKING_MEMORY 前置在历史之前 + 满预算后每步滑动删 1 组
    - optimized：全量规范工具共享 + 动态状态移至尾部瞬态消息 + 62% 低水位阶梯式压缩
    """
    from .context import ContextManager

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 2400:
        raise ValueError("max_chars 至少需要 2400")
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
        raise ValueError("turns 至少为 1")

    if max_chars < 5200:
        scale = max_chars / 6800.0
        sys_repeats = max(4, int(18 * scale))
        tool_count = max(4, min(10, int(round(10 * scale))))
        short_desc = max_chars < 3800
        evidence_repeats = 1 if max_chars < 3000 else max(2, int(6 * scale))
    else:
        sys_repeats = 18
        tool_count = 10
        short_desc = False
        evidence_repeats = 6

    base_system = ("你是谨慎的 Python 代码开发 Agent。\n" * sys_repeats).strip()
    full_tools = [
        {
            "name": f"tool_{idx}",
            "description": (
                f"受控工具 {idx}"
                if short_desc
                else (
                    f"受控编码工具 {idx}，读取或验证代码。"
                    if max_chars < 5200
                    else f"受控编码工具 {idx}，用于读取代码、检索符号或运行受限容器验证。"
                )
            ),
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "query": {"type": "string"}},
                "required": ["path"],
            },
        }
        for idx in range(tool_count)
    ]
    readonly_tools = full_tools[: max(2, tool_count // 2)]
    initial_task = {"role": "user", "content": "请定位并修复跨模块结算与重试退避缺陷，并在容器中验证。"}

    def make_exchange(step: int) -> list[dict]:
        evidence = f"step={step}: " + (
            "def compute_settlement(item_cents, cap): return item_cents\n" * evidence_repeats
        )
        return [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"call_{step}",
                        "name": "tool_0",
                        "input": {"path": f"module_{step % 4}.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"call_{step}", "content": evidence}],
            },
        ]

    # 1. Legacy 路径模拟
    legacy_mgr = ContextManager(max_chars=max_chars, low_watermark_ratio=1.0)
    legacy_kv: dict[str, Any] | None = None
    legacy_compactions = 0
    prev_dropped = 0
    for role in ("reviewer", "tester"):
        legacy_kv, _ = record_kv_step(
            legacy_kv,
            system=base_system + f"\nROLE:{role}\n只读分析",
            tools=readonly_tools,
            messages=[initial_task],
            worker=role,
            block_chars=block_chars,
        )
    legacy_history = [deepcopy(initial_task)]
    legacy_reserved = len(canonical_json({"system": base_system, "tools": full_tools, "messages": []})) - 2
    for step in range(1, turns + 1):
        volatile_state = [
            {
                "role": "user",
                "content": "HARNESS_STATE\n"
                + canonical_json({"step": step, "recent_calls": [f"call_{i}" for i in range(max(1, step - 5), step + 1)]}),
            },
            {
                "role": "user",
                "content": "WORKING_MEMORY\n"
                + canonical_json({"goal": "fix", "recorded_at": f"2026-11-12T00:00:{step:02d}Z"}),
            },
        ]
        built_legacy = legacy_mgr.build(legacy_history, pinned=volatile_state, reserved_chars=legacy_reserved)
        if legacy_mgr.stats()["dropped_groups"] != prev_dropped:
            legacy_compactions += 1
            prev_dropped = legacy_mgr.stats()["dropped_groups"]
        legacy_kv, _ = record_kv_step(
            legacy_kv,
            system=base_system,
            tools=full_tools,
            messages=built_legacy,
            block_chars=block_chars,
        )
        legacy_history.extend(make_exchange(step))

    # 2. Optimized 路径模拟
    opt_mgr = ContextManager(max_chars=max_chars, low_watermark_ratio=0.62)
    opt_kv: dict[str, Any] | None = None
    opt_compactions = 0
    cursor = 0
    canon_tools = canonical_tools(full_tools)
    for role in ("reviewer", "tester"):
        opt_kv, _ = record_kv_step(
            opt_kv,
            system=base_system + f"\nROLE:{role}\n只读分析",
            tools=canon_tools,
            messages=[initial_task],
            worker=role,
            block_chars=block_chars,
        )
    opt_history = [deepcopy(initial_task)]
    opt_reserved = len(canonical_json({"system": base_system, "tools": canon_tools, "messages": []})) - 2
    for step in range(1, turns + 1):
        tail_state = [
            {
                "role": "user",
                "content": "HARNESS_STATE\n"
                + canonical_json({"step": step, "recent_calls": [f"call_{i}" for i in range(max(1, step - 5), step + 1)]}),
            },
            {
                "role": "user",
                "content": "WORKING_MEMORY\n" + canonical_json({"goal": "fix"}),
            },
        ]
        built_opt = opt_mgr.build(
            opt_history,
            tail_state=tail_state,
            reserved_chars=opt_reserved,
            min_dropped_groups=cursor,
        )
        if opt_mgr.stats()["compaction_triggered"]:
            opt_compactions += 1
        cursor = opt_mgr.stats()["compaction_cursor"]
        opt_kv, _ = record_kv_step(
            opt_kv,
            system=base_system,
            tools=canon_tools,
            messages=built_opt,
            tail_state_count=len(tail_state),
            block_chars=block_chars,
        )
        opt_history.extend(make_exchange(step))

    legacy_summary = summarize_kv_cache(legacy_kv)
    opt_summary = summarize_kv_cache(opt_kv)
    # 按主流商业 API 命中缓存输入单价 = 0.1x 未命中单价（90% 折扣）估算等效输入计费体积
    legacy_effective_cost = legacy_summary["uncached_chars"] + 0.1 * legacy_summary["cached_chars"]
    opt_effective_cost = opt_summary["uncached_chars"] + 0.1 * opt_summary["cached_chars"]
    cost_saving_pct = (
        round((1.0 - opt_effective_cost / legacy_effective_cost) * 100, 2) if legacy_effective_cost else 0.0
    )
    return {
        "config": {"turns": turns, "max_chars": max_chars, "block_chars": block_chars, "low_watermark_ratio": 0.62},
        "legacy": {**legacy_summary, "compaction_events": legacy_compactions},
        "optimized": {**opt_summary, "compaction_events": opt_compactions},
        "improvement": {
            "hit_rate_delta": round(opt_summary["cumulative_hit_rate"] - legacy_summary["cumulative_hit_rate"], 4),
            "uncached_prefill_reduction_pct": (
                round((1.0 - opt_summary["uncached_chars"] / legacy_summary["uncached_chars"]) * 100, 2)
                if legacy_summary["uncached_chars"]
                else 0.0
            ),
            "estimated_effective_input_cost_saving_pct": cost_saving_pct,
        },
    }


if __name__ == "__main__":
    print(json.dumps(simulate_kv_cache_ablation(), ensure_ascii=False, indent=2))

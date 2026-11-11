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
_MAX_TRACKED_BLOCKS = 8192


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
    known_list: list[str] = list(state.get("known_blocks", []))
    known_set = set(known_list)

    stream = serialize_kv_stream(system, tools, messages)
    hashes = chained_block_hashes(stream, block_chars=block_chars)
    hit_blocks = 0
    for digest_hex in hashes:
        if digest_hex in known_set:
            hit_blocks += 1
        else:
            break

    for digest_hex in hashes:
        if digest_hex not in known_set:
            known_set.add(digest_hex)
            known_list.append(digest_hex)
    if len(known_list) > _MAX_TRACKED_BLOCKS:
        known_list = known_list[-_MAX_TRACKED_BLOCKS:]

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
    return deepcopy(content)


def build_anthropic_cached_payload(
    system: str,
    messages: list[dict],
    tools: list[dict],
    *,
    max_tokens: int = 2048,
    tail_state_count: int = 0,
) -> dict[str, Any]:
    """构造带 3 处显式 cache_control 断点且严格满足角色交替的 Anthropic 请求负载。

    断点布局（Anthropic 限制单请求最多 4 个断点）：
    1. tools[-1]：缓存整套规范化工具 JSON Schema；
    2. system[0]（共享基础系统提示）：主协调者与只读子 Agent 共享同一缓存前缀；
    3. 尾部瞬态状态之前的最后一个稳定历史内容块：使每步新增工具往返命中滚动历史缓存。
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
        coalesced[msg_idx]["content"][blk_idx]["cache_control"] = {"type": "ephemeral"}

    return {
        "system": system_blocks,
        "messages": coalesced,
        "tools": cached_tools,
        "max_tokens": max_tokens,
    }


def simulate_kv_cache_ablation(
    turns: int = 16,
    max_chars: int = 6800,
    block_chars: int = DEFAULT_BLOCK_CHARS,
) -> dict[str, Any]:
    """在完全相同的多子代理 + 14 步工具轨迹上对比优化前后的链式前缀块命中率。

    - legacy：工具子集过滤 + HARNESS_STATE/WORKING_MEMORY 前置在历史之前 + 满预算后每步滑动删 1 组
    - optimized：全量规范工具共享 + 动态状态移至尾部瞬态消息 + 62% 低水位阶梯式压缩
    """
    from .context import ContextManager

    base_system = ("你是谨慎的 Python 代码开发 Agent。\n" * 18).strip()
    full_tools = [
        {
            "name": f"tool_{idx}",
            "description": f"受控编码工具 {idx}，用于读取代码、检索符号或运行受限容器验证。",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "query": {"type": "string"}},
                "required": ["path"],
            },
        }
        for idx in range(10)
    ]
    readonly_tools = full_tools[:5]
    initial_task = {"role": "user", "content": "请定位并修复跨模块结算与重试退避缺陷，并在容器中验证。"}

    def make_exchange(step: int) -> list[dict]:
        evidence = f"step={step}: " + ("def compute_settlement(item_cents, cap): return item_cents\n" * 6)
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
            "uncached_prefill_reduction_pct": round(
                (1.0 - opt_summary["uncached_chars"] / legacy_summary["uncached_chars"]) * 100, 2
            ),
            "estimated_effective_input_cost_saving_pct": cost_saving_pct,
        },
    }


if __name__ == "__main__":
    print(json.dumps(simulate_kv_cache_ablation(), ensure_ascii=False, indent=2))

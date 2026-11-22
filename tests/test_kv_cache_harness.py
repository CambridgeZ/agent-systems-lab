"""Harness 级 KV Cache 命中率优化回归测试。

覆盖 6 项核心机制：
1. 多步工具循环的前缀字节级不变性（HARNESS_STATE / WORKING_MEMORY 移至瞬态尾部 + 剥离墙钟时间戳）
2. 跨任务 System + Tools 前缀字节对齐（技能目录按名称稳定排序 + 确定性紧凑 JSON）
3. 高低水位滞回阶梯压缩（Hysteresis Step Compaction）避免逐轮滑动重算全部前缀块
4. Parallel 多子代理（reviewer / tester / coordinator）共享 Tools + Base System 前缀块，同时保持只读拦截
5. Anthropic 三断点 cache_control 布局 + 相邻 user 消息合并交替，以及 OpenAI prompt_cache_key 与缓存 token 统计
6. 链式块哈希级联失效特性、离线消融实验 simulate_kv_cache_ablation() 与 API / CLI 契约
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_lab.api import ROOT, create_app
from agent_lab.context import ContextManager
from agent_lab.kv_cache import (
    _MAX_TRACKED_BLOCKS,
    build_anthropic_cached_payload,
    canonical_json,
    chained_block_hashes,
    record_kv_step,
    serialize_kv_stream,
    simulate_kv_cache_ablation,
)
from agent_lab.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderRateLimiter,
    compute_usage_totals,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    return ws


class _CacheHintCapturingProvider:
    """捕获每一步传给底层模型的完整负载，并模拟服务端返回缓存命中 token。"""

    supports_cache_hints = True

    def __init__(self, scripted_steps: list[list[dict]]):
        self.scripted_steps = list(scripted_steps)
        self.requests: list[dict] = []

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
        idx = len(self.requests)
        self.requests.append(
            {
                "system": deepcopy(system),
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "max_tokens": max_tokens,
                "cache_key": cache_key,
                "tail_state_count": tail_state_count,
            }
        )
        content = self.scripted_steps[idx]
        has_tool = any(b.get("type") == "tool_use" for b in content)
        return {
            "content": content,
            "stop_reason": "tool_use" if has_tool else "end_turn",
            "usage": {
                "input_tokens": 300 + idx * 80,
                "output_tokens": 40,
                "cache_read_input_tokens": 0 if idx == 0 else 240 + (idx - 1) * 70,
                "cache_creation_input_tokens": 240 if idx == 0 else 70,
            },
        }


def test_chained_block_hashes_cascade_on_single_prefix_byte_mutation():
    base = "A" * 64 + "B" * 64 + "C" * 64 + "D" * 64
    mutated_front = "X" + base[1:]
    mutated_tail = base[:-1] + "Z"

    h_base = chained_block_hashes(base, block_chars=64)
    h_front = chained_block_hashes(mutated_front, block_chars=64)
    h_tail = chained_block_hashes(mutated_tail, block_chars=64)

    assert len(h_base) == 4
    # 前缀第 0 块改动 1 个字节 -> 全部 4 个链式块哈希全部失效
    assert all(a != b for a, b in zip(h_base, h_front, strict=True))
    # 尾部最后 1 块改动 -> 前 3 个块 100% 保持不变，仅最后 1 块更新
    assert h_base[:3] == h_tail[:3]
    assert h_base[3] != h_tail[3]


async def test_multistep_agent_prefix_remains_byte_identical_across_turns(tmp_path):
    ws = _make_workspace(tmp_path)
    storage = Storage(tmp_path / "state.db")
    provider = _CacheHintCapturingProvider(
        [
            [{"type": "tool_use", "id": "c1", "name": "repo_read", "input": {"path": "pricing.py"}}],
            [
                {
                    "type": "tool_use",
                    "id": "c2",
                    "name": "working_memory_write",
                    "input": {
                        "goal": "定位折扣计算错误",
                        "todos": ["核对百分比除数"],
                        "add_facts": [
                            {
                                "claim": "pricing.py 存在折扣计算逻辑",
                                "source_type": "file",
                                "source_ref": "pricing.py",
                            }
                        ],
                    },
                }
            ],
            [{"type": "tool_use", "id": "c3", "name": "repo_list", "input": {}}],
            [{"type": "text", "text": "已完成只读核查与证据记录。"}],
        ]
    )
    runtime = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills", provider=provider)
    created = runtime.create("检查 pricing.py 折扣逻辑并记录工作记忆")
    completed = await runtime.execute(created["id"])

    assert completed["status"] == "completed"
    assert len(provider.requests) == 4

    for step_idx, req in enumerate(provider.requests):
        assert req["cache_key"] == created["id"]
        tail_n = req["tail_state_count"]
        assert tail_n >= 1
        stable_msgs = req["messages"][:-tail_n]
        tail_msgs = req["messages"][-tail_n:]
        # 动态状态只允许出现在尾部瞬态区，绝不污染稳定前缀
        assert all("HARNESS_STATE" not in str(m["content"]) for m in stable_msgs)
        assert all("WORKING_MEMORY" not in str(m["content"]) for m in stable_msgs)
        assert "HARNESS_STATE" in tail_msgs[0]["content"]
        # 模型可见的 WORKING_MEMORY 已剥离墙钟时间戳 recorded_at
        for tm in tail_msgs:
            assert "recorded_at" not in tm["content"]
        if step_idx > 0:
            prev_req = provider.requests[step_idx - 1]
            prev_stable = prev_req["messages"][: -prev_req["tail_state_count"]]
            assert req["system"] == prev_req["system"]
            assert req["tools"] == prev_req["tools"]
            # 稳定历史前缀 100% 字节级一致（只追加，不改写）
            assert canonical_json(stable_msgs[: len(prev_stable)]) == canonical_json(prev_stable)

    kv = completed["metadata"]["kv_cache"]
    assert kv["steps_recorded"] == 4
    assert kv["last_step_hit_rate"] > 0.72
    assert kv["provider_cache_read_input_tokens"] == 240 + 310 + 380
    assert kv["provider_cache_creation_input_tokens"] == 240 + 70 + 70 + 70


def test_cross_run_system_and_tools_prefix_is_byte_identical_despite_different_prompts(tmp_path):
    ws = _make_workspace(tmp_path)
    storage = Storage(tmp_path / "state.db")
    runtime = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills", provider="demo")

    run_review = runtime.create("请执行严格 code review 审查边界条件")
    run_test = runtime.create("请编写 pytest 单元测试验证折扣")

    # 尽管两次查询词面命中的技能排序不同，注入 System 的目录按名称稳定排序后 100% 字节一致
    sys_a = runtime._system(run_review)
    sys_b = runtime._system(run_test)
    assert sys_a == sys_b

    stream_a = serialize_kv_stream(sys_a, [], [])
    stream_b = serialize_kv_stream(sys_b, [], [])
    assert chained_block_hashes(stream_a) == chained_block_hashes(stream_b)


def test_hysteresis_watermark_compacts_in_steps_instead_of_every_turn():
    initial = [{"role": "user", "content": "修复多模块结算与重试缺陷"}]

    def exchange(idx: int) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"c_{idx}", "name": "repo_read", "input": {"i": idx}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"c_{idx}", "content": "X" * 260}],
            },
        ]

    sliding = ContextManager(max_chars=3200, low_watermark_ratio=1.0)
    stepped = ContextManager(max_chars=3200, low_watermark_ratio=0.55)

    history_s = deepcopy(initial)
    history_h = deepcopy(initial)
    sliding_shifts = 0
    stepped_shifts = 0
    prev_sliding_dropped = 0
    cursor = 0
    prev_built_after_compaction: list[dict] | None = None
    verified_post_compaction_prefix_reuse = False

    for step in range(1, 15):
        history_s.extend(exchange(step))
        history_h.extend(exchange(step))

        sliding.build(history_s)
        if sliding.stats()["dropped_groups"] != prev_sliding_dropped:
            sliding_shifts += 1
            prev_sliding_dropped = sliding.stats()["dropped_groups"]

        built_h = stepped.build(history_h, min_dropped_groups=cursor)
        if prev_built_after_compaction is not None:
            # 刚完成水位压缩后的下一轮：不再触发滑动删除，且前序历史前缀 100% 字节复用
            assert stepped.stats()["compaction_triggered"] is False
            assert built_h[: len(prev_built_after_compaction)] == prev_built_after_compaction
            verified_post_compaction_prefix_reuse = True
            prev_built_after_compaction = None
        if stepped.stats()["compaction_triggered"]:
            stepped_shifts += 1
            prev_built_after_compaction = built_h
        cursor = stepped.stats()["compaction_cursor"]

    # 传统逐组滑动在满预算后几乎每步都改变前缀起点；阶梯水位压缩将重排次数显著降低一半以上
    assert sliding_shifts >= 7
    assert 1 <= stepped_shifts <= 3
    assert stepped_shifts <= sliding_shifts // 2
    assert verified_post_compaction_prefix_reuse is True


async def test_parallel_workers_share_root_prefix_blocks_with_coordinator_and_enforce_readonly(tmp_path):
    class _ParallelProbeProvider:
        def __init__(self):
            self.calls_by_worker: list[tuple[str, str, list[dict]]] = []
            self._reviewer_done = False

        async def complete(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048):
            del max_tokens
            if "ROLE:reviewer" in system:
                self.calls_by_worker.append(("reviewer", system, deepcopy(tools)))
                if not self._reviewer_done:
                    self._reviewer_done = True
                    # 故意尝试调用写提案工具，验证即使 schema 共享，执行层依然严格拦截
                    return {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "illegitimate_write",
                                "name": "propose_patch",
                                "input": {
                                    "path": "pricing.py",
                                    "old_string": "a",
                                    "new_string": "b",
                                    "description": "越权尝试",
                                },
                            }
                        ],
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    }
                assert "permission_denied" in json.dumps(messages[-1], ensure_ascii=False)
                return {
                    "content": [{"type": "text", "text": "只读审查报告：越权已被安全拦截。"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 120, "output_tokens": 20},
                }
            if "ROLE:tester" in system:
                self.calls_by_worker.append(("tester", system, deepcopy(tools)))
                return {
                    "content": [{"type": "text", "text": "只读测试报告：建议覆盖边界测试。"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            self.calls_by_worker.append(("coordinator", system, deepcopy(tools)))
            return {
                "content": [{"type": "text", "text": "汇总完成。"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 160, "output_tokens": 30},
            }

    ws = _make_workspace(tmp_path)
    storage = Storage(tmp_path / "state.db")
    probe = _ParallelProbeProvider()
    runtime = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills", provider=probe)
    created = runtime.create("并行分析 pricing.py", mode="parallel")
    completed = await runtime.execute(created["id"])

    assert completed["status"] == "completed"
    by_role = {role: (sys_text, tls) for role, sys_text, tls in probe.calls_by_worker}
    assert {"reviewer", "tester", "coordinator"} <= set(by_role)

    # 三个角色的工具 Schema 列表 100% 字节一致
    assert canonical_json(by_role["reviewer"][1]) == canonical_json(by_role["coordinator"][1])
    assert canonical_json(by_role["tester"][1]) == canonical_json(by_role["coordinator"][1])

    # Tools + Base System 对应的所有前缀块哈希完全一致，子代理直接命中主代理根前缀缓存
    coord_prefix_hashes = chained_block_hashes(
        serialize_kv_stream(by_role["coordinator"][0], by_role["coordinator"][1], [])
    )
    reviewer_hashes = chained_block_hashes(
        serialize_kv_stream(by_role["reviewer"][0], by_role["reviewer"][1], [])
    )
    assert len(coord_prefix_hashes) >= 8
    assert reviewer_hashes[: len(coord_prefix_hashes)] == coord_prefix_hashes
    assert storage.proposals(created["id"]) == []


def test_anthropic_cached_payload_breakpoints_and_role_coalescing():
    tools = [
        {"name": "repo_read", "description": "读文件", "input_schema": {"type": "object"}},
        {"name": "verify_python", "description": "跑测试", "input_schema": {"type": "object"}},
    ]
    messages = [
        {"role": "user", "content": "修复 bug"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "repo_read", "input": {"path": "a.py"}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "print(1)"}],
        },
        {"role": "user", "content": 'HARNESS_STATE\n{"step":2}'},
        {"role": "user", "content": 'WORKING_MEMORY\n{"goal":"fix"}'},
    ]
    payload = build_anthropic_cached_payload(
        "基础系统提示\nROLE:reviewer\n只读角色",
        messages,
        tools,
        max_tokens=1024,
        tail_state_count=2,
    )

    # 1. 相邻 user 消息被合并，严格满足 user -> assistant -> user 角色交替
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert len(payload["messages"][-1]["content"]) == 3

    # 2. 统计整份请求中的 cache_control 断点数：严格 == 3（不超过 Anthropic 4 断点上限）
    breakpoints = 0
    if "cache_control" in payload["tools"][-1]:
        breakpoints += 1
    breakpoints += sum(1 for blk in payload["system"] if "cache_control" in blk)
    for msg in payload["messages"]:
        breakpoints += sum(1 for blk in msg["content"] if "cache_control" in blk)
    assert breakpoints == 3

    # 3. 历史断点精确打在最后一个稳定块（tool_result），而非尾部每步变化的 HARNESS_STATE / WORKING_MEMORY
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["system"][1]
    last_user_blocks = payload["messages"][-1]["content"]
    assert last_user_blocks[0]["type"] == "tool_result"
    assert last_user_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in last_user_blocks[1]
    assert "cache_control" not in last_user_blocks[2]


async def test_anthropic_and_openai_provider_cache_usage_accounting(monkeypatch):
    # 1. Anthropic 适配器缓存指标回传
    captured_anth: dict = {}

    async def fake_anth_create(**kwargs):
        captured_anth.update(kwargs)
        block = SimpleNamespace(model_dump=lambda exclude_none=True: {"type": "text", "text": "ok"})
        usage = SimpleNamespace(
            input_tokens=50,
            output_tokens=12,
            cache_read_input_tokens=420,
            cache_creation_input_tokens=80,
        )
        return SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage)

    anth = AnthropicProvider.__new__(AnthropicProvider)
    anth.model = "claude-sonnet-test"
    anth.client = SimpleNamespace(messages=SimpleNamespace(create=fake_anth_create))
    res_anth = await anth.complete(
        "系统规则",
        [{"role": "user", "content": "你好"}, {"role": "user", "content": "HARNESS_STATE\n{}"}],
        [{"name": "t1", "description": "d", "input_schema": {"type": "object"}}],
        cache_key="run_abc",
        tail_state_count=1,
    )
    assert res_anth["usage"] == {
        "input_tokens": 50,
        "output_tokens": 12,
        "cache_read_input_tokens": 420,
        "cache_creation_input_tokens": 80,
    }
    assert len(captured_anth["messages"]) == 1  # 相邻 user 已安全合并

    # 2. OpenAI 兼容适配器 prompt_cache_key 与 cached_tokens 提取
    captured_oai: dict = {}

    def oai_handler(request: httpx.Request) -> httpx.Response:
        captured_oai.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 600,
                    "completion_tokens": 25,
                    "prompt_tokens_details": {"cached_tokens": 512},
                },
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    oai = OpenAICompatibleProvider()
    await oai.client.aclose()
    oai.client = httpx.AsyncClient(base_url="https://api.example.test/v1", transport=httpx.MockTransport(oai_handler))
    try:
        res_oai = await oai.complete(
            "系统规则",
            [{"role": "user", "content": "检查代码"}],
            [{"name": "t1", "description": "d", "input_schema": {"b": 1, "a": 2}}],
            cache_key="run_xyz",
            tail_state_count=1,
        )
    finally:
        await oai.aclose()

    assert captured_oai["prompt_cache_key"] == "run_xyz"
    assert list(captured_oai["tools"][0]["function"]["parameters"].keys()) == ["a", "b"]
    assert res_oai["usage"] == {
        "input_tokens": 600,
        "output_tokens": 25,
        "cache_read_input_tokens": 512,
    }


def test_ablation_benchmark_and_api_endpoints(tmp_path):
    report = simulate_kv_cache_ablation()
    assert report["optimized"]["cumulative_hit_rate"] > report["legacy"]["cumulative_hit_rate"] + 0.35
    assert report["improvement"]["uncached_prefill_reduction_pct"] > 60.0
    assert report["optimized"]["compaction_events"] < report["legacy"]["compaction_events"]

    app = create_app(state_dir=tmp_path / "api_state", provider="demo")
    with TestClient(app) as client:
        bench = client.get("/api/kv-benchmark?turns=10&max_chars=8000")
        assert bench.status_code == 200
        data = bench.json()
        assert data["optimized"]["cumulative_hit_rate"] > data["legacy"]["cumulative_hit_rate"]

        created = client.post(
            "/api/runs",
            json={"prompt": "修复 pricing.py 折扣 bug", "mode": "agent", "template": "sample"},
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        detail = client.get(f"/api/runs/{run_id}").json()
        assert "kv_cache" in detail
        assert detail["kv_cache"]["steps_recorded"] >= 1

    out_report = tmp_path / "kv-bench.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_lab.cli",
            "--state",
            str(tmp_path / "cli_state"),
            "kv-benchmark",
            "--turns",
            "12",
            "--report",
            str(out_report),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out_report.is_file()
    parsed = json.loads(proc.stdout)
    assert parsed["optimized"]["cumulative_hit_rate"] > parsed["legacy"]["cumulative_hit_rate"]


def test_root_pinned_lru_tracker_and_tight_ablation_budgets(tmp_path):
    assert _MAX_TRACKED_BLOCKS == 2048

    tools = [
        {
            "name": f"tool_{idx}",
            "description": f"受控工具说明 {idx} " + ("S" * 80),
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
        for idx in range(8)
    ]
    base_system = "共享根系统指令 " * 30
    root_hashes = chained_block_hashes(serialize_kv_stream(base_system, tools, []))
    assert 16 <= len(root_hashes) <= 128

    kv_state = None
    # 1) 多轮高流失消息写入超过 _MAX_TRACKED_BLOCKS 个块，根前缀块不得被 FIFO 淘汰
    for step in range(28):
        big_msg = [{"role": "user", "content": f"turn={step}:" + (f"{step:02d}_payload_" * 600)}]
        kv_state, report = record_kv_step(kv_state, system=base_system, tools=tools, messages=big_msg)
        assert len(kv_state["known_blocks"]) <= _MAX_TRACKED_BLOCKS
        if step > 0:
            assert report["hit_blocks"] >= len(root_hashes)

    # 2) 单步超长请求（> 2048 块）后，下一步子代理仍 100% 命中根前缀块
    huge_single = [{"role": "user", "content": "H" * (64 * 2400)}]
    kv_state, _ = record_kv_step(kv_state, system=base_system, tools=tools, messages=huge_single)
    assert len(kv_state["known_blocks"]) == _MAX_TRACKED_BLOCKS

    kv_state, subagent_step = record_kv_step(
        kv_state,
        system=base_system + "\nROLE:reviewer\n只读审查",
        tools=tools,
        messages=[{"role": "user", "content": "新子任务"}],
        worker="reviewer",
    )
    assert subagent_step["hit_blocks"] >= len(root_hashes)

    # 3) 紧凑预算 max_chars=2400 / 3200 消融实验与 API 查询均不触发 ContextOverflow
    with pytest.raises(ValueError, match="max_chars 至少需要 2400"):
        simulate_kv_cache_ablation(turns=8, max_chars=2000)

    for budget in (2400, 3200, 4800):
        tight_report = simulate_kv_cache_ablation(turns=16, max_chars=budget)
        assert tight_report["optimized"]["cumulative_hit_rate"] > tight_report["legacy"]["cumulative_hit_rate"]

    app = create_app(state_dir=tmp_path / "tight_api_state", provider="demo")
    with TestClient(app) as client:
        resp = client.get("/api/kv-benchmark?max_chars=3200")
        assert resp.status_code == 200
        body = resp.json()
        assert body["optimized"]["cumulative_hit_rate"] > body["legacy"]["cumulative_hit_rate"]


async def test_anthropic_thinking_blocks_roundtrip_and_usage_totals():
    tools = [{"name": "repo_read", "description": "读文件", "input_schema": {"type": "object"}}]
    messages = [
        {"role": "user", "content": "分析根因"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "先检查入口"},
                {"type": "thinking", "thinking": "内部推导步骤", "signature": "sig_123"},
                {"type": "redacted_thinking", "data": "enc_blob"},
            ],
        },
        {"role": "user", "content": 'HARNESS_STATE\n{"step":2}'},
    ]
    payload = build_anthropic_cached_payload(
        "系统提示",
        messages,
        tools,
        max_tokens=4096,
        tail_state_count=1,
        thinking={"type": "enabled", "budget_tokens": 1536},
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1536}
    assistant_blocks = payload["messages"][1]["content"]
    # 第 3 个断点向前回退到非 thinking 的 text 块，绝不打在 thinking / redacted_thinking 上
    assert assistant_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in assistant_blocks[1]
    assert "cache_control" not in assistant_blocks[2]

    payload_no_thinking = build_anthropic_cached_payload("系统提示", messages[:1], tools)
    assert "thinking" not in payload_no_thinking

    # AnthropicProvider 保留 thinking / redacted_thinking、忽略良性元数据块，并正确处理 max_tokens 截断
    assert AnthropicProvider.cache_usage_additive is True

    async def fake_create_thinking(**kwargs):
        del kwargs
        blocks = [
            SimpleNamespace(
                model_dump=lambda exclude_none=True: {
                    "type": "thinking",
                    "thinking": "推理链...",
                    "signature": "sig_abc",
                }
            ),
            SimpleNamespace(
                model_dump=lambda exclude_none=True: {
                    "type": "redacted_thinking",
                    "data": "opaque_cipher",
                }
            ),
            SimpleNamespace(
                model_dump=lambda exclude_none=True: {
                    "type": "server_tool_use",
                    "id": "srv_1",
                }
            ),
            SimpleNamespace(
                model_dump=lambda exclude_none=True: {
                    "type": "tool_use",
                    "id": "call:raw/1",
                    "name": "repo_read",
                    "input": {"path": "pricing.py"},
                }
            ),
        ]
        usage = SimpleNamespace(
            input_tokens=50,
            output_tokens=30,
            cache_read_input_tokens=400,
            cache_creation_input_tokens=100,
        )
        return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=usage)

    anth = AnthropicProvider.__new__(AnthropicProvider)
    anth.model = "claude-sonnet-test"
    anth.client = SimpleNamespace(messages=SimpleNamespace(create=fake_create_thinking))
    res = await anth.complete("系统提示", [{"role": "user", "content": "检查"}], tools)
    assert res["stop_reason"] == "tool_use"
    assert res["content"] == [
        {"type": "thinking", "thinking": "推理链...", "signature": "sig_abc"},
        {"type": "redacted_thinking", "data": "opaque_cipher"},
        {"type": "tool_use", "id": "call_raw_1", "name": "repo_read", "input": {"path": "pricing.py"}},
    ]

    # max_tokens 截断半截 tool_use 时转为可恢复响应，不抛异常中断任务
    async def fake_create_cutoff(**kwargs):
        del kwargs
        blocks = [
            {"type": "thinking", "thinking": "长思考...", "signature": "sig_cut"},
            {"type": "tool_use", "id": "cut_1", "name": "propose_patch", "input": {}},
        ]
        return SimpleNamespace(
            content=blocks,
            stop_reason="max_tokens",
            usage=SimpleNamespace(input_tokens=20, output_tokens=2048),
        )

    anth.client = SimpleNamespace(messages=SimpleNamespace(create=fake_create_cutoff))
    cutoff_res = await anth.complete("系统提示", [{"role": "user", "content": "修改"}], tools)
    assert cutoff_res["truncated"] is True
    assert cutoff_res["stop_reason"] == "tool_use"
    assert "__truncated_or_invalid_json__" in cutoff_res["content"][-1]["input"]

    # compute_usage_totals：区分 Anthropic 累加型与 OpenAI 包含型缓存计费
    anth_totals = compute_usage_totals(res["usage"], additive_cache=True)
    assert anth_totals == {
        "total_input_tokens": 550,
        "billed_input_tokens": 50 + int(round(1.25 * 100)) + int(round(0.10 * 400)),
        "output_tokens": 30,
        "effective_budget_tokens": 580,
    }
    oai_totals = compute_usage_totals(
        {"input_tokens": 600, "output_tokens": 25, "cache_read_input_tokens": 500},
        additive_cache=False,
    )
    assert oai_totals == {
        "total_input_tokens": 600,
        "billed_input_tokens": 100 + int(round(0.25 * 500)),
        "output_tokens": 25,
        "effective_budget_tokens": 625,
    }


async def test_openai_resilience_coalescing_sanitization_and_sse_streaming(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    # 1. 相邻 user 消息合并（\n\n 连接）且跳过 assistant thinking / redacted_thinking 块
    translated = OpenAICompatibleProvider._to_openai_messages(
        "sys",
        [
            {"role": "user", "content": "初始任务"},
            {"role": "user", "content": "SKILL_SNAPSHOT\n{}"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret", "signature": "s"},
                    {"type": "redacted_thinking", "data": "enc"},
                    {"type": "tool_use", "id": "call_1", "name": "repo_list", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": '{"ok":true}'}],
            },
            {"role": "user", "content": "HARNESS_STATE\n{}"},
            {"role": "user", "content": "WORKING_MEMORY\n{}"},
        ],
    )
    roles = [m["role"] for m in translated]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert translated[1]["content"] == "初始任务\n\nSKILL_SNAPSHOT\n{}"
    assert translated[-1]["content"] == "HARNESS_STATE\n{}\n\nWORKING_MEMORY\n{}"
    assert translated[2]["content"] is None
    assert len(translated[2]["tool_calls"]) == 1

    # 2. 429 Retry-After 退避恢复 + 422 prompt_cache_key 自动降级 + ID 消毒去重 + 截断 JSON 容错
    seen_payloads: list[dict] = []
    call_counter = {"n": 0}

    def resilient_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            assert "prompt_cache_key" in body
            return httpx.Response(422, json={"error": "extra_forbidden: prompt_cache_key"})
        if call_counter["n"] == 2:
            assert "prompt_cache_key" not in body
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate_limited"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_hist",  # 与历史 ID 冲突 -> 自动加后缀
                                    "type": "function",
                                    "function": {"name": "repo_read", "arguments": '{"path":"pricing.py"}'},
                                },
                                {
                                    "id": "bad:id/with spaces!",  # 含非法字符 + 截断 JSON
                                    "type": "function",
                                    "function": {"name": "propose_patch", "arguments": '{"path":"pricing.py","old'},
                                },
                                {
                                    "id": "bad:id/with spaces!",  # 同轮重复 ID
                                    "type": "function",
                                    "function": {"name": "repo_list", "arguments": ""},
                                },
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 64},
            },
        )

    oai = OpenAICompatibleProvider(base_retry_delay=0.01, max_retry_delay=0.05)
    await oai.client.aclose()
    oai.client = httpx.AsyncClient(
        base_url="https://api.example.test/v1", transport=httpx.MockTransport(resilient_handler)
    )
    history_with_id = [
        {"role": "user", "content": "开始"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_hist", "name": "repo_list", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_hist", "content": "{}"}],
        },
    ]
    try:
        res_recovered = await oai.complete("sys", history_with_id, [], cache_key="run_1")
        assert oai._supports_prompt_cache_key is False
        assert call_counter["n"] == 3
        assert res_recovered["stop_reason"] == "tool_use"
        assert res_recovered["truncated"] is True
        ids = [b["id"] for b in res_recovered["content"]]
        assert len(set(ids)) == 3
        assert "call_hist" not in ids
        assert all(re.fullmatch(r"[A-Za-z0-9_-]+", cid) for cid in ids)
        assert res_recovered["content"][0]["input"] == {"path": "pricing.py"}
        assert "__truncated_or_invalid_json__" in res_recovered["content"][1]["input"]
        assert "__truncated_or_invalid_json__" in res_recovered["content"][2]["input"]
    finally:
        await oai.aclose()

    # 3. 已知严格第三方网关默认不发 prompt_cache_key + 备用网关自动故障转移 + 空 choices 拦截
    failover_urls: list[str] = []

    def failover_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        failover_urls.append(url)
        body = json.loads(request.content.decode("utf-8"))
        if "groq.com" in url:
            assert "prompt_cache_key" not in body
            return httpx.Response(503, json={"error": "upstream_down"})
        return httpx.Response(200, json={"choices": [], "usage": {}})

    oai_fb = OpenAICompatibleProvider(
        max_retries=1,
        fallback_base_urls=["https://backup.example.test/v1"],
        base_retry_delay=0.005,
        max_retry_delay=0.02,
    )
    await oai_fb.client.aclose()
    oai_fb.client = httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(failover_handler)
    )
    try:
        with pytest.raises(ValueError, match="OpenAI 兼容网关返回空 choices"):
            await oai_fb.complete("sys", [{"role": "user", "content": "hi"}], [], cache_key="run_2")
        assert sum("groq.com" in u for u in failover_urls) == 2
        assert any("backup.example.test" in u for u in failover_urls)
    finally:
        await oai_fb.aclose()

    # 4. SSE 流式增量拼接（文本 delta + 多工具分片 + finish_reason + include_usage）
    sse_seen_req: dict = {}
    sse_lines = [
        ": keep-alive",
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"正在"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"读取代码"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_s1","type":"function","function":{"name":"repo_read","arguments":"{\\"pa"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_s1","function":{"arguments":"th\\":\\"pricing.py\\"}"}}]},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":320,"completion_tokens":18,"prompt_tokens_details":{"cached_tokens":256}}}',
        "data: [DONE]",
    ]
    sse_body = ("\n\n".join(sse_lines) + "\n\n").encode("utf-8")

    def sse_handler(request: httpx.Request) -> httpx.Response:
        sse_seen_req.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_body,
        )

    monkeypatch.setenv("OPENAI_STREAM", "1")
    oai_stream = OpenAICompatibleProvider()
    await oai_stream.client.aclose()
    oai_stream.client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1", transport=httpx.MockTransport(sse_handler)
    )
    try:
        stream_res = await oai_stream.complete(
            "sys", [{"role": "user", "content": "检查"}], [], cache_key="stream_run"
        )
    finally:
        await oai_stream.aclose()

    assert sse_seen_req["stream"] is True
    assert sse_seen_req["stream_options"] == {"include_usage": True}
    assert stream_res["stop_reason"] == "tool_use"
    assert stream_res["truncated"] is False
    assert stream_res["content"] == [
        {"type": "text", "text": "正在读取代码"},
        {"type": "tool_use", "id": "call_s1", "name": "repo_read", "input": {"path": "pricing.py"}},
    ]
    assert stream_res["usage"] == {
        "input_tokens": 320,
        "output_tokens": 18,
        "cache_read_input_tokens": 256,
    }


async def test_provider_rate_limiter_rpm_tpm_and_zero_sleep_default(monkeypatch):
    monkeypatch.delenv("AGENT_LAB_RPM_LIMIT", raising=False)
    monkeypatch.delenv("AGENT_LAB_TPM_LIMIT", raising=False)

    slept: list[float] = []
    now_box = [100.0]

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)
        now_box[0] += sec

    # 默认未设环境变量时零休眠直通
    disabled = ProviderRateLimiter(clock=lambda: now_box[0], sleep=fake_sleep)
    assert disabled.enabled is False
    for _ in range(10):
        await disabled.reserve(50_000)
        disabled.record_actual(60_000, 50_000)
    assert slept == []

    # 配置 RPM=2, TPM=1000 时严格滑窗准入，且 record_actual 退还高估预留额
    monkeypatch.setenv("AGENT_LAB_RPM_LIMIT", "2")
    monkeypatch.setenv("AGENT_LAB_TPM_LIMIT", "1000")
    limiter = ProviderRateLimiter(window_seconds=60.0, clock=lambda: now_box[0], sleep=fake_sleep)
    assert limiter.enabled is True

    r1 = await limiter.reserve(800)
    assert slept == []
    # 实际仅消耗 300 token -> 退还 500 token，使下一笔 600 token 请求无需等待 TPM
    limiter.record_actual(300, r1)

    r2 = await limiter.reserve(600)
    assert slept == []
    limiter.record_actual(600, r2)

    # 第 3 次请求触发 RPM=2 限制，等待滑窗释放
    await limiter.reserve(100)
    assert len(slept) == 1 and slept[0] >= 59.9

    # 跨线程并发调用 reserve_sync / record_actual 保持线程安全
    sync_limiter = ProviderRateLimiter(rpm_limit=100, tpm_limit=100_000)
    errors: list[Exception] = []

    def worker_fn() -> None:
        try:
            res_tok = sync_limiter.reserve_sync(200)
            sync_limiter.record_actual(150, res_tok)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker_fn) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


async def test_openai_provider_deduplicates_constant_call_id_across_context_compaction(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    (ws / "large_module.py").write_text("PAYLOAD = '" + ("M" * 1500) + "'\n", encoding="utf-8")
    storage = Storage(tmp_path / "state.db")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    turn_counter = {"n": 0}
    seen_request_msg_counts: list[int] = []

    def constant_call_id_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_request_msg_counts.append(len(body["messages"]))
        idx = turn_counter["n"]
        turn_counter["n"] += 1
        if idx < 6:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_0",
                                        "type": "function",
                                        "function": {
                                            "name": "repo_read",
                                            "arguments": '{"path":"large_module.py"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 20},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "已完成全部 6 轮代码核查。"},
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 15},
            },
        )

    oai = OpenAICompatibleProvider()
    await oai.client.aclose()
    oai.client = httpx.AsyncClient(
        base_url="https://api.example.test/v1",
        transport=httpx.MockTransport(constant_call_id_handler),
    )
    try:
        probe = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills", provider=oai)
        probe_run = probe.create("测量固定开销")
        probe_tools = probe._registry(probe_run).definitions()
        from agent_lab.runtime import EXTRA_TOOLS

        fixed_chars = len(
            json.dumps(
                {"system": probe._system(probe_run), "tools": probe_tools + EXTRA_TOOLS, "messages": []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        runtime = AgentRuntime(
            storage,
            ws,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=oai,
            max_steps=10,
            context_chars=fixed_chars + 4800,
        )
        created = runtime.create("连续 6 轮读取 large_module.py 触发上下文裁剪")
        completed = await runtime.execute(created["id"])
    finally:
        await oai.aclose()

    assert completed["status"] == "completed", completed
    assert turn_counter["n"] == 7
    assert completed["metadata"]["context_stats"]["dropped_messages"] >= 2
    calls = storage.list_calls(created["id"])
    assert [c["call_id"] for c in calls] == [
        "call_0",
        "call_0_2",
        "call_0_3",
        "call_0_4",
        "call_0_5",
        "call_0_6",
    ]



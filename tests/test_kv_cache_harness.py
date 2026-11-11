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
import shutil
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx

from agent_lab.api import ROOT, create_app
from agent_lab.context import ContextManager
from agent_lab.kv_cache import (
    build_anthropic_cached_payload,
    canonical_json,
    chained_block_hashes,
    serialize_kv_stream,
    simulate_kv_cache_ablation,
)
from agent_lab.providers import AnthropicProvider, OpenAICompatibleProvider
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

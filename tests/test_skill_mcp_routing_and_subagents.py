"""多通道技能路由、阈值门控延迟 MCP ToolSearch 与多智能体上下文隔离架构测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from agent_lab.api import ROOT
from agent_lab.kv_cache import (
    DEFAULT_BLOCK_CHARS,
    canonical_json,
    chained_block_hashes,
    serialize_kv_stream,
    split_system_role,
)
from agent_lab.mcp_client import McpClientManager, make_mcp_tool_name, search_mcp_tools, should_defer_mcp_tools
from agent_lab.permissions import evaluate_tool_permission
from agent_lab.runtime import AgentRuntime
from agent_lab.skills import SkillCatalog
from agent_lab.storage import Storage


def _call(name: str, call_id: str | None = None, **kwargs: object) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id or f"tc_{uuid.uuid4().hex[:10]}", "name": name, "input": kwargs}


def _resp(blocks: list[dict[str, Any]], *, input_tokens: int = 100, output_tokens: int = 30) -> dict[str, Any]:
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    return {"content": blocks, "stop_reason": stop, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def _write_skill(skills_root: Path, name: str, frontmatter_extra: str, body: str) -> None:
    folder = skills_root / name
    folder.mkdir(parents=True, exist_ok=True)
    extra = f"\n{frontmatter_extra.strip()}" if frontmatter_extra.strip() else ""
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 领域规范指南{extra}\n---\n\n{body.strip()}\n", encoding="utf-8"
    )


def _build_16_mock_mcp_routes() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    specs_by_server: dict[str, list[tuple[str, bool, str, dict[str, Any]]]] = {
        "github": [
            ("get_pull_request", True, "查询 PR 详情与 diff 状态", {"pull_number": {"type": "integer"}}),
            ("list_issues", True, "按标签检索仓库议题列表", {"label": {"type": "string"}}),
            ("get_commit_status", True, "读取提交关联的 CI 流水线状态", {"sha": {"type": "string"}}),
            ("search_code", True, "跨仓库检索符号与代码片段", {"q": {"type": "string"}}),
            ("merge_pull_request", False, "合并目标 Pull Request 到主干分支", {"pull_number": {"type": "integer"}}),
            ("create_release", False, "打标签并发布正式版本 Release", {"tag_name": {"type": "string"}}),
        ],
        "postgres": [
            ("query_readonly", True, "执行只读 SELECT 分析查询", {"sql": {"type": "string"}}),
            ("list_tables", True, "列出当前 schema 下全部数据表", {"schema": {"type": "string"}}),
            ("describe_indexes", True, "查看指定表的 B-Tree 与 GIN 索引定义", {"table": {"type": "string"}}),
            ("explain_plan", True, "生成无副作用查询执行计划", {"sql": {"type": "string"}}),
            ("execute_ddl", False, "执行建表或字段变更 DDL 迁移语句", {"ddl": {"type": "string"}}),
        ],
        "playwright": [
            ("snapshot_accessibility", True, "抓取当前页面无障碍 DOM 快照树", {"selector": {"type": "string"}}),
            ("list_console_logs", True, "读取浏览器控制台错误与告警日志", {"level": {"type": "string"}}),
            ("get_network_requests", True, "列出最近发起的 XHR/Fetch 请求摘要", {"filter": {"type": "string"}}),
            ("click_element", False, "点击页面目标交互元素触发跳转或提交", {"ref": {"type": "string"}}),
            ("fill_form_input", False, "向表单输入框填入文本", {"ref": {"type": "string"}, "value": {"type": "string"}}),
        ],
    }
    routes: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for srv, items in specs_by_server.items():
        for remote_name, ro, desc, props in items:
            ns = make_mcp_tool_name(srv, remote_name)
            schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
            spec = {"name": ns, "description": f"[MCP:{srv}] {desc}", "input_schema": schema}
            routes[ns] = {"server": srv, "remote_name": remote_name, "read_only": ro, "defer_loading": False, "spec": spec}
        ro_cnt = sum(1 for _, ro, _, _ in items if ro)
        summaries[srv] = {"server": srv, "tool_count": len(items), "read_only_count": ro_cnt, "instructions": f"{srv} ready", "defer_loading": False}
    return routes, summaries


def test_multi_channel_skill_router_and_step0_explicit_preload(tmp_path: Path) -> None:
    """验证多通道技能路由：既有 skills 元数据零漂移、显式 /skill & $skill Step-0 预载、disable-model-invocation 门控与 Glob+RRF 排序。"""
    repo_skills = SkillCatalog(ROOT / "skills").catalog()
    assert repo_skills and all(set(item.keys()) == {"name", "description"} for item in repo_skills)

    skills_dir = tmp_path / "skills"
    (skills_dir / "code-review").mkdir(parents=True)
    (skills_dir / "code-review" / "SKILL.md").write_text(
        (ROOT / "skills" / "code-review" / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _write_skill(
        skills_dir,
        "deploy-prod",
        "disable-model-invocation: true\ntriggers: [production, release, rollout]",
        "# 生产环境发布 SOP\n严禁模型在未获显式命令时自主触发生产发版。",
    )
    _write_skill(
        skills_dir,
        "sql-migration",
        "globs: [migrations/**/*.sql, *.sql]\ntriggers: [alembic, ddl_index, concurrent_index]",
        "# 数据库迁移规范\n大表新增索引必须使用 CONCURRENTLY。",
    )
    _write_skill(
        skills_dir,
        "retry-policy",
        "paths: src/net/**/*.py\ntriggers: [exponential_backoff, jitter_retry]",
        "# 网络重试退避规范\n使用带随机抖动的指数退避。",
    )

    cat = SkillCatalog(skills_dir)
    legacy_meta = next(item for item in cat.catalog() if item["name"] == "code-review")
    assert set(legacy_meta.keys()) == {"name", "description"}

    # disable-model-invocation: true 在常规查询与普通文件路径下均被屏蔽，唯有 /deploy-prod 或 $deploy-prod 解锁
    assert "deploy-prod" not in [s["name"] for s in cat.select("deploy-prod production release rollout")]
    assert "deploy-prod" not in [s["name"] for s in cat.select("请检查 src/deploy-prod/runner.py 的日志")]
    assert cat.select("执行 /deploy-prod 检查清单")[0]["name"] == "deploy-prod"
    assert cat.select("执行 $deploy-prod 检查清单")[0]["name"] == "deploy-prod"

    # paths/globs 工作区上下文激活 + BM25/TF-IDF 子词余弦/RRF 混合触发词召回
    ws_ranked = cat.select("审查初始化脚本与索引变更", workspace_files=["migrations/001_init.sql", "README.md"])
    assert ws_ranked[0]["name"] == "sql-migration"
    assert cat.select("超时请求实现 exponentialBackoff 与 jitterRetry")[0]["name"] == "retry-policy"

    # Step-0 显式 /skill 与 $skill 自动预载入 active_skills 且挂载到固定前缀区
    ws, kb = tmp_path / "ws", tmp_path / "kb"
    ws.mkdir(), kb.mkdir()
    (ws / "app.py").write_text("OK = True\n", encoding="utf-8")
    rt = AgentRuntime(Storage(tmp_path / ".agent-lab" / "state.db"), ws, kb, skills_dir, execution_profile="host")
    run = rt.create("请按 /deploy-prod 与 $retry-policy 核对发版与重试策略")
    active = run["metadata"]["active_skills"]
    assert set(active.keys()) == {"deploy-prod", "retry-policy"}
    assert all(not snap.get("mid_run_dynamic") for snap in active.values())
    pinned_texts = [m["content"] for m in rt._assemble_pinned_for_worker(run, "coordinator", allow_extras=True)]
    assert sum("SKILL_SNAPSHOT" in txt for txt in pinned_texts) == 2


async def test_threshold_gated_deferred_mcp_tool_search_and_permissions(tmp_path: Path) -> None:
    """验证 >12 个冻结 MCP 工具切换粘性延迟检索、<TOOLS> 跨轮字节级恒定、BM25/select 检索及 Schema+三级权限门控。"""
    routes, summaries = _build_16_mock_mcp_routes()
    assert len(routes) == 16 and should_defer_mcp_tools(routes, mode="auto") is True
    assert should_defer_mcp_tools(dict(list(routes.items())[:4]), mode="auto") is False

    bm25_res = search_mcp_tools(routes, "pull_number PR diff", limit=3)
    assert bm25_res["matches"][0]["name"] == "mcp__github__get_pull_request"
    sel_res = search_mcp_tools(routes, "select:mcp__postgres__query_readonly,mcp__playwright__click_element")
    assert [m["name"] for m in sel_res["matches"]] == [
        "mcp__postgres__query_readonly",
        "mcp__playwright__click_element",
    ]

    for mode in ("ask", "auto-edit"):
        ro_call = {"tool_name": "mcp__postgres__query_readonly", "arguments": {"sql": "SELECT 1"}}
        mut_call = {"tool_name": "mcp__github__merge_pull_request", "arguments": {"pull_number": 7}}
        assert evaluate_tool_permission("mcp_call_tool", ro_call, permission_mode=mode, mcp_routes=routes)["decision"] == "allow"
        assert evaluate_tool_permission("mcp_call_tool", mut_call, permission_mode=mode, mcp_routes=routes)["decision"] == "ask"
    assert evaluate_tool_permission("mcp_call_tool", mut_call, permission_mode="full-auto", mcp_routes=routes)["decision"] == "allow"

    class StubMcpManager(McpClientManager):
        def __init__(self) -> None:
            super().__init__({"github": {"transport": "stdio", "command": "stub"}})
            self._routes, self._connected, self.called = routes, True, []

        async def connect_all(self) -> list[dict[str, Any]]:
            return [e["spec"] for _, e in sorted(self._routes.items())]

        def server_summaries(self) -> dict[str, dict[str, Any]]:
            return summaries

        async def call_tool(self, namespaced_name: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
            self.called.append((namespaced_name, arguments))
            return {"ok": True, "mcp_server": routes[namespaced_name]["server"], "rows": [{"ok": 1}]}

    class DeferredFlowProvider:
        def __init__(self) -> None:
            self.turn, self.seen_tools_json, self.seen_messages = 0, [], []

        async def complete(self, system: str, messages: list[dict], tools: list[dict], **_: Any) -> dict:
            self.turn += 1
            self.seen_tools_json.append(canonical_json(tools))
            self.seen_messages.append(messages)
            if self.turn == 1:
                return _resp([_call("mcp_tool_search", "s1", query="postgres SELECT readonly")])
            if self.turn == 2:
                return _resp([
                    _call("mcp_tool_search", "s2", query="select:mcp__github__merge_pull_request"),
                    _call("mcp_call_tool", "c_bad", tool_name="mcp__postgres__query_readonly", arguments={"sql": 123}),
                    _call("mcp_call_tool", "c_ro", tool_name="mcp__postgres__query_readonly", arguments={"sql": "SELECT 1"}),
                ])
            return _resp([_call("mcp_call_tool", "c_mut", tool_name="mcp__github__merge_pull_request", arguments={"pull_number": 42})])

    ws, kb, skills_dir = tmp_path / "ws", tmp_path / "kb", tmp_path / "skills"
    ws.mkdir(), kb.mkdir(), skills_dir.mkdir()
    (ws / "main.py").write_text("X = 1\n", encoding="utf-8")
    provider, stub_mgr, storage = DeferredFlowProvider(), StubMcpManager(), Storage(tmp_path / ".agent-lab" / "state.db")
    rt = AgentRuntime(
        storage, ws, kb, skills_dir, provider=provider, execution_profile="host", permission_mode="auto-edit", mcp_manager=stub_mgr
    )
    finished = await rt.start("检索只读 SQL 工具查询后再尝试合并 PR #42")
    assert finished["status"] == "waiting_approval"
    assert stub_mgr.called == [("mcp__postgres__query_readonly", {"sql": "SELECT 1"})]
    assert storage.get_call(finished["id"], "c_bad")["result"]["code"] == "invalid_arguments"
    assert storage.get_call(finished["id"], "c_mut")["result"]["requires_approval"] is True

    exposed_names = {t["name"] for t in json.loads(provider.seen_tools_json[0])}
    assert {"mcp_tool_search", "mcp_call_tool"} <= exposed_names
    assert not any(n.startswith("mcp__") for n in exposed_names)
    assert len(set(provider.seen_tools_json)) == 1
    assert any(isinstance(m.get("content"), str) and m["content"].startswith("MCP_SERVER_DIRECTORY") for m in provider.seen_messages[0][:3])


async def test_subagent_context_isolation_shared_kv_prefix_and_structured_handoff(tmp_path: Path) -> None:
    """验证 subagent_run 隔离简报 PARENT_CONTEXT_BRIEF（含 WorkingMemory 事实与兄弟分工、零主会话工具噪声泄露）、100% 根前缀块共享及结构化返回。"""
    ws, kb, skills_dir = tmp_path / "ws", tmp_path / "kb", tmp_path / "skills"
    (ws / "src").mkdir(parents=True), (ws / "tests").mkdir(parents=True), kb.mkdir(), skills_dir.mkdir()
    orders_src = "MAX_DISCOUNT = 0.30  # RAW_LEAD_TOOL_NOISE_99182\n"
    (ws / "src" / "orders.py").write_text(orders_src, encoding="utf-8")
    (ws / "tests" / "test_orders.py").write_text("def test_cap(): assert True\n", encoding="utf-8")
    orders_sha = hashlib.sha256(orders_src.encode("utf-8")).hexdigest()

    class MultiAgentProvider:
        def __init__(self) -> None:
            self.lead_turn, self.sub_turns, self.requests = 0, {}, []

        async def complete(self, system: str, messages: list[dict], tools: list[dict], **_: Any) -> dict:
            _, role_suffix = split_system_role(system)
            worker = role_suffix.splitlines()[0].removeprefix("ROLE:") if role_suffix else "coordinator"
            self.requests.append((worker, system, tools, messages))
            if worker == "coordinator":
                self.lead_turn += 1
                if self.lead_turn == 1:
                    fact = {"id": "f1", "claim": "订单折扣硬上限为 30%", "source_type": "file", "source_ref": "src/orders.py", "source_sha256": orders_sha}
                    return _resp([
                        _call("fs_read", "lead_read_1", path="src/orders.py"),
                        _call("working_memory_write", "lead_wm_1", goal="审计折扣与单测", constraints=["严禁修改公开函数签名"], facts=[fact]),
                    ])
                if self.lead_turn == 2:
                    return _resp([_call("subagent_run", "sub_batch_1", tasks=[
                        {"role": "arch-explorer", "prompt": "核查常量", "scope_boundaries": "仅检视 src/ 目录", "expected_output": "常量名与 file:line"},
                        {"role": "test-auditor", "prompt": "核查断言", "scope_boundaries": "仅检视 tests/ 目录", "expected_output": "测试名与 file:line"},
                    ])])
                return _resp([{"type": "text", "text": "已完成并行子 Agent 汇总分析。"}])
            step = self.sub_turns[worker] = self.sub_turns.get(worker, 0) + 1
            target_path = "src/orders.py" if "arch-explorer" in worker else "tests/test_orders.py"
            if step == 1:
                return _resp([_call("fs_read", f"{worker[:6]}_r1", path=target_path)])
            return _resp([{"type": "text", "text": f"结论引自 {target_path}:1。"}])

    provider, storage = MultiAgentProvider(), Storage(tmp_path / ".agent-lab" / "state.db")
    rt = AgentRuntime(storage, ws, kb, skills_dir, provider=provider, execution_profile="host", permission_mode="auto-edit")
    done = await rt.start("协同调研 src/orders.py 与 tests/test_orders.py 的折扣边界")
    assert done["status"] == "completed"

    lead_reqs = [r for r in provider.requests if r[0] == "coordinator"]
    sub_first_reqs = [r for r in provider.requests if r[0] != "coordinator" and len(r[3]) == 1]
    assert len(sub_first_reqs) == 2
    for worker_id, _, _, msgs in sub_first_reqs:
        brief = msgs[0]["content"]
        assert brief.startswith("PARENT_CONTEXT_BRIEF") and "RAW_LEAD_TOOL_NOISE_99182" not in brief
        assert "订单折扣硬上限为 30%" in brief and "严禁修改公开函数签名" in brief
        expected_scope, peer_tag = ("仅检视 src/ 目录", "test-auditor(") if "arch-explorer" in worker_id else ("仅检视 tests/ 目录", "arch-explorer(")
        assert expected_scope in brief and peer_tag in brief

    _, lead_sys, lead_tools, _ = lead_reqs[0]
    lead_base_sys, _ = split_system_role(lead_sys)
    root_blocks = chained_block_hashes(f"<TOOLS>\n{canonical_json(lead_tools)}\n<SYSTEM_BASE>\n{lead_base_sys}", DEFAULT_BLOCK_CHARS)
    assert len(root_blocks) >= 20
    for _, sub_sys, sub_tools, sub_msgs in [r for r in provider.requests if r[0] != "coordinator"]:
        assert chained_block_hashes(serialize_kv_stream(sub_sys, sub_tools, sub_msgs), DEFAULT_BLOCK_CHARS)[: len(root_blocks)] == root_blocks

    sub_res = storage.get_call(done["id"], "sub_batch_1")["result"]
    by_role = {rep["role"]: rep for rep in sub_res["reports"]}
    assert by_role["arch-explorer"]["tools_used"] == ["fs_read"] and by_role["arch-explorer"]["files_inspected"] == ["src/orders.py"]
    assert by_role["test-auditor"]["tools_used"] == ["fs_read"] and by_role["test-auditor"]["files_inspected"] == ["tests/test_orders.py"]
    assert "src/orders.py:1" in by_role["arch-explorer"]["answer"] and "tests/test_orders.py:1" in by_role["test-auditor"]["answer"]

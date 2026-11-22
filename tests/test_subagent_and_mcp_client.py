"""动态子 Agent 派发（subagent_run）与外部 MCP Client（McpClientManager）端到端测试。"""

from __future__ import annotations

import json
import shutil
import sys
import uuid

import pytest

from agent_lab.api import ROOT
from agent_lab.mcp_client import McpClientManager
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _call(name: str, **kwargs):
    return {"type": "tool_use", "id": "sm_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict]):
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


async def test_dynamic_subagent_run_parallel_research_recursion_guard_and_kv_alignment(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    storage = Storage(tmp_path / "state.db")

    seen_tools_by_role: dict[str, str] = {}

    class SubagentTestProvider:
        supports_cache_hints = True

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            role_marker = "coordinator"
            if "\nROLE:" in system:
                role_marker = system.split("\nROLE:", 1)[1].splitlines()[0]
            seen_tools_by_role[role_marker] = json.dumps(tools, sort_keys=True)

            if role_marker == "coordinator":
                has_sub_res = any(
                    isinstance(m.get("content"), list)
                    and any(b.get("type") == "tool_result" for b in m["content"])
                    for m in messages
                )
                if not has_sub_res:
                    return _resp(
                        [
                            _call(
                                "subagent_run",
                                tasks=[
                                    {"role": "explorer", "prompt": "用 fs_glob 列出文件并尝试违规递归调用"},
                                    {"role": "reviewer", "prompt": "用 fs_read 读取 checkout.py"},
                                ],
                            )
                        ]
                    )
                serialized = json.dumps(messages, ensure_ascii=False)
                assert "EXPLORER_REPORT_OK" in serialized
                assert "REVIEWER_REPORT_OK" in serialized
                return _resp([{"type": "text", "text": "汇总两个动态子 Agent 结论完成。"}])

            # 子 Agent 分支
            tool_results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            if "explorer" in role_marker:
                if not tool_results:
                    # 同时调用合法只读 fs_glob + 非法递归 subagent_run + 非法写 fs_write
                    return _resp(
                        [
                            _call("fs_glob", pattern="*.py"),
                            _call("subagent_run", tasks=[{"role": "nested", "prompt": "boom"}]),
                            _call("fs_write", path="hack.py", content="x=1"),
                        ]
                    )
                assert tool_results[0]["ok"] is True
                assert tool_results[1]["code"] == "permission_denied"
                assert tool_results[2]["code"] == "permission_denied"
                assert not (ws / "hack.py").exists()
                return _resp([{"type": "text", "text": "EXPLORER_REPORT_OK"}])

            if not tool_results:
                return _resp([_call("fs_read", path="checkout.py", offset_line=1, limit_lines=20)])
            assert tool_results[0]["ok"] is True
            return _resp([{"type": "text", "text": "REVIEWER_REPORT_OK"}])

    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=SubagentTestProvider(),
        execution_profile="host",
        permission_mode="full-auto",
    )
    created = runtime.create("动态派发两个子 Agent 调研仓库")
    done = await runtime.execute(created["id"])
    await runtime.aclose()

    assert done["status"] == "completed", done
    # 主协调者与所有动态子 Agent 的规范化 tools JSON Schema 必须 100% 字节一致以共享 KV Cache 根前缀
    unique_tool_schemas = set(seen_tools_by_role.values())
    assert len(unique_tool_schemas) == 1
    assert len(seen_tools_by_role) == 3


async def test_external_mcp_client_connects_to_real_stdio_server_and_executes_tools(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    ws = tmp_path / "local_ws"
    ws.mkdir()
    (ws / "note.txt").write_text("local workspace\n", encoding="utf-8")

    # 配置真实子进程 agent_lab.mcp_server 作为外部 MCP Server
    mcp_cfg_file = tmp_path / "mcp.json"
    mcp_cfg_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sample_repo_mcp": {
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "agent_lab.mcp_server",
                            "--workspace",
                            str(ROOT / "data/sample_repo"),
                            "--knowledge",
                            str(ROOT / "data/knowledge"),
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-MUST-NOT-LEAK-TO-MCP")

    class McpCallerProvider:
        def __init__(self):
            self.step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.step += 1
            tool_names = {t["name"] for t in tools}
            assert "mcp__sample_repo_mcp__repo_list" in tool_names
            assert "mcp__sample_repo_mcp__repo_read" in tool_names
            if self.step == 1:
                return _resp([_call("mcp__sample_repo_mcp__repo_read", path="pricing.py")])
            serialized = json.dumps(messages, ensure_ascii=False)
            assert "discount_percent" in serialized
            return _resp([{"type": "text", "text": "已通过外部 MCP Server 读取 pricing.py。"}])

    storage = Storage(tmp_path / "state.db")

    # 1. auto-edit 下未标注 readOnlyHint 的外部 MCP 工具先进入审批，批准后执行真实 stdio MCP 调用
    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=McpCallerProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        mcp_config=mcp_cfg_file,
    )
    try:
        created = runtime.create("通过外部 MCP 服务读取 sample_repo 的 pricing.py")
        paused = await runtime.execute(created["id"])
        assert paused["status"] == "waiting_approval"
        assert "mcp_tools" in storage.get_run(created["id"])["metadata"]

        pid = paused["metadata"]["waiting_proposal_ids"][0]
        prop = storage.get_proposal(pid)
        assert prop["kind"] == "mcp_call"
        assert prop["tool"] == "mcp__sample_repo_mcp__repo_read"
    finally:
        await runtime.aclose()

    # 模拟跨请求重建全新 AgentRuntime（不显式传 mcp_config，仅靠 run.metadata.mcp_config 惰性恢复）完成审批
    fresh_runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=McpCallerProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
    )
    # 让 step 从第 2 步继续返回总结文本
    fresh_runtime.provider.step = 1
    try:
        done = await fresh_runtime.approve(created["id"], pid, True)
        assert done["status"] == "completed", done
        assert "外部 MCP Server" in done["answer"]
    finally:
        await fresh_runtime.aclose()

    # 2. 直接验证 McpClientManager 独立调用与跨 Task 关闭安全性（无 AnyIO cancel-scope 报错）
    import asyncio

    mgr = McpClientManager.from_config_path(mcp_cfg_file)
    specs = await mgr.connect_all()
    assert len(specs) == 4
    list_res = await mgr.call_tool("mcp__sample_repo_mcp__repo_list", {})
    assert list_res["ok"] is True
    assert "pricing.py" in list_res["files"]
    await asyncio.create_task(mgr.aclose())

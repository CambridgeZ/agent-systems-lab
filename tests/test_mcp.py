"""通过真正的 stdio JSON-RPC 会话验证 MCP 接口，不直接调用 Python 工具函数。"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import shutil
import sys

import pytest

pytest.importorskip("mcp", reason="需要安装可选 mcp extra 才运行协议集成测试")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _payload(result) -> dict:
    # SDK 可以同时提供文本与 structuredContent。解析实际线路上传回的文本，
    # 确保非 SDK 专属的 MCP 客户端也能读取结果。
    text_blocks = [block.text for block in result.content if block.type == "text"]
    assert text_blocks, result
    return json.loads(text_blocks[0])


async def test_stdio_server_readonly_tools_and_path_boundary(tmp_path):
    workspace = tmp_path / "sample repo with spaces"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("private-test-marker", encoding="utf-8")
    before = (workspace / "pricing.py").read_bytes()
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_lab.mcp_server",
            "--workspace",
            str(workspace),
            "--knowledge",
            str(ROOT / "data/knowledge"),
        ],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "agent-systems-lab-readonly"
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"repo_list", "repo_read", "repo_search", "knowledge_search"}
            assert not names.intersection(
                {"propose_patch", "apply_proposal", "approve_proposal", "sandbox_test"}
            )

            listing = _payload(await session.call_tool("repo_list", {}))
            assert listing["ok"] and "pricing.py" in listing["files"]
            read_result = _payload(await session.call_tool("repo_read", {"path": "pricing.py"}))
            assert read_result["ok"] and "discounted_total" in read_result["content"]
            searched = _payload(await session.call_tool("repo_search", {"query": "discount_percent"}))
            assert searched["ok"] and any(row["path"] == "pricing.py" for row in searched["matches"])
            assert all(row["line"] > 0 for row in searched["matches"])
            knowledge = _payload(await session.call_tool("knowledge_search", {"query": "审批 补丁"}))
            assert knowledge["ok"] and knowledge["matches"]
            assert all(row["id"] and row["source"] for row in knowledge["matches"])

            denied = _payload(await session.call_tool("repo_read", {"path": "../outside.txt"}))
            assert denied["ok"] is False and denied["code"] == "unsafe_path"
            assert "private-test-marker" not in str(denied)
            absent = await session.call_tool("propose_patch", {})
            assert absent.isError is True
    assert (workspace / "pricing.py").read_bytes() == before

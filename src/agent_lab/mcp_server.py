"""使用官方 MCP Python SDK v1 暴露只读代码检索工具。

运行：uv run --extra mcp python -m agent_lab.mcp_server --workspace data/sample_repo
MCP 是工具连接协议，不能替代鉴权或审批。此 server 刻意不暴露补丁审批与
任意 shell；写入必须走带人类审查的主应用。stdio 的 stdout 由 SDK 专用，
请勿在这里 print 日志，否则会损坏 JSON-RPC 帧。

参考：https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
"""

from __future__ import annotations

import argparse
import inspect
import os
import tempfile
from pathlib import Path
from typing import Any

from agent_lab.retrieval import KnowledgeBase
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


def _readonly_tool_decorator(mcp: Any):
    """构造附带显式只读 ToolAnnotations 的 FastMCP 工具装饰器；低版本 SDK 优雅回退。"""
    annotations = None
    try:
        from mcp.types import ToolAnnotations

        annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        )
    except Exception:
        annotations = None

    if annotations is not None:
        try:
            sig = inspect.signature(mcp.tool)
            if "annotations" in sig.parameters:
                return lambda: mcp.tool(annotations=annotations)
        except (TypeError, ValueError):
            pass
    return lambda: mcp.tool()


def create_server(
    workspace: Path,
    knowledge_dir: Path,
    *,
    include_extended_tools: bool = True,
):
    """延迟导入：不安装可选 mcp extra 也能运行主应用与离线评测。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("请先执行 uv sync --extra mcp；此服务使用 MCP SDK v1。") from exc

    mcp = FastMCP("agent-systems-lab-readonly")
    # 数据库只记本次 MCP 会话审计；临时目录生命周期跟随 server 对象。
    scratch = tempfile.TemporaryDirectory(prefix="agent-lab-mcp-")
    storage = Storage(Path(scratch.name) / "mcp.sqlite")
    run = storage.create_run("MCP 只读检索会话", provider="mcp")
    registry = ToolRegistry(storage, workspace, KnowledgeBase(knowledge_dir), run["id"])
    readonly_tool = _readonly_tool_decorator(mcp)

    @readonly_tool()
    def repo_list() -> dict:
        """列出受限仓库文本文件，不访问隐藏文件或符号链接。"""
        return registry.execute("repo_list", {})

    @readonly_tool()
    def repo_read(path: str) -> dict:
        """读取相对路径 UTF-8 文件；路径不能越过工作区。"""
        return registry.execute("repo_read", {"path": path})

    @readonly_tool()
    def repo_search(query: str) -> dict:
        """字面量检索仓库并返回文件名、行号与短片段。"""
        return registry.execute("repo_search", {"query": query})

    @readonly_tool()
    def knowledge_search(query: str) -> dict:
        """检索工程笔记；引用使用返回的 chunk id。"""
        return registry.execute("knowledge_search", {"query": query})

    if include_extended_tools:

        @readonly_tool()
        def repo_map(max_chars: int = 2000, focus_query: str = "") -> dict:
            """生成有字符预算的 Python AST 代码地图（模块导入、类、函数签名与行号）。"""
            return registry.execute("repo_map", {"max_chars": max_chars, "focus_query": focus_query})

        @readonly_tool()
        def symbol_search(symbol: str) -> dict:
            """基于 AST 跨文件定位函数、类或方法的定义签名、行号与引用位置。"""
            return registry.execute("symbol_search", {"symbol": symbol})

    # 保持 TemporaryDirectory 活着，不向客户端暴露数据库路径。
    mcp._agent_lab_scratch = scratch
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Lab 只读 MCP stdio server")
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--workspace", type=Path, default=root / "data" / "sample_repo")
    parser.add_argument("--knowledge", type=Path, default=root / "data" / "knowledge")
    parser.add_argument(
        "--extended",
        action="store_true",
        default=False,
        help="额外暴露只读 AST 代码地图工具（repo_map、symbol_search）。也可通过 AGENT_LAB_MCP_EXTENDED=1 开启。",
    )
    args = parser.parse_args()
    env_extended = os.environ.get("AGENT_LAB_MCP_EXTENDED", "").strip().lower() in {"1", "true", "yes", "on"}
    create_server(
        args.workspace,
        args.knowledge,
        include_extended_tools=bool(args.extended or env_extended),
    ).run(transport="stdio")


if __name__ == "__main__":
    main()

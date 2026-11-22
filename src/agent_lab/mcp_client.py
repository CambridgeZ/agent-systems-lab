"""外部 MCP Client 连接池与工具桥接器（支持 stdio 与 streamable-http 传输）。

安全、并发与缓存不变量：
1. 子进程 stdio MCP Server 默认使用 build_scrubbed_env() 剥离本服务的 ANTHROPIC_API_KEY /
   OPENAI_API_KEY / AGENT_LAB_TOKEN 等凭证，仅合并用户为该 MCP Server 显式声明的 env。
2. 所有 AnyIO stdio_client / streamablehttp_client 与 ClientSession 上下文统一在单个后台宿主 Task 内进入与退出，
   避免跨协程/跨任务关闭触发 AnyIO cancel-scope 任务亲和性异常。
3. 外部工具名统一映射为 `mcp__<server>__<tool>`（满足 `^[A-Za-z0-9_-]{1,64}$`），并在任务首次准备时
   将规范化 JSON Schema 快照冻结进 `run["metadata"]["mcp_tools"]`，确保多轮与恢复期间 `<TOOLS>` KV Cache 前缀稳定。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .observability import redact_secrets
from .permissions import build_scrubbed_env


def _sanitize_ident(raw: str, max_len: int = 24) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw).strip()).strip("_")
    return (cleaned or "srv")[:max_len]


def make_mcp_tool_name(server_name: str, tool_name: str) -> str:
    s_clean = _sanitize_ident(server_name, 20)
    t_clean = _sanitize_ident(tool_name, 36)
    candidate = f"mcp__{s_clean}__{t_clean}"
    if len(candidate) <= 64:
        return candidate
    suffix = hashlib.sha256(f"{server_name}:{tool_name}".encode("utf-8")).hexdigest()[:8]
    return f"mcp__{s_clean[:16]}__{t_clean[:32]}_{suffix}"[:64]


def resolve_mcp_config_path(
    explicit_path: str | Path | None = None,
    *,
    state_dir: Path | None = None,
) -> Path | None:
    """按 显式参数 -> AGENT_LAB_MCP_CONFIG 环境变量 -> <state_dir>/mcp.json 顺序解析 MCP 配置文件。"""
    if explicit_path is not None and str(explicit_path).strip():
        p = Path(explicit_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"指定的 MCP 配置文件不存在：{p}")
        return p
    env_cfg = os.environ.get("AGENT_LAB_MCP_CONFIG", "").strip()
    if env_cfg:
        p = Path(env_cfg).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"AGENT_LAB_MCP_CONFIG 指向的文件不存在：{p}")
        return p
    if state_dir is not None:
        default_p = Path(state_dir).resolve() / "mcp.json"
        if default_p.is_file() and not default_p.is_symlink():
            return default_p
    return None


def parse_mcp_servers_config(config_path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("MCP 配置文件根节点必须是 JSON 对象")
    servers = raw.get("mcpServers") or raw.get("servers") or {}
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 字段必须是对象映射")
    validated: dict[str, dict[str, Any]] = {}
    for srv_name, spec in servers.items():
        if not isinstance(srv_name, str) or not srv_name.strip() or not isinstance(spec, dict):
            continue
        if spec.get("disabled") is True:
            continue
        if "command" in spec:
            cmd = str(spec["command"]).strip()
            args = [str(a) for a in (spec.get("args") or [])]
            env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
            cwd = str(spec["cwd"]).strip() if spec.get("cwd") else None
            validated[srv_name.strip()] = {
                "transport": "stdio",
                "command": cmd,
                "args": args,
                "env": env,
                "cwd": cwd,
            }
        elif "url" in spec:
            url = str(spec["url"]).strip()
            headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
            validated[srv_name.strip()] = {
                "transport": "streamable_http",
                "url": url,
                "headers": headers,
            }
    return validated


class McpClientManager:
    """管理外部 MCP Server 会话生命周期与工具路由（单任务持有 AnyIO 作用域，支持跨协程安全调用与关闭）。"""

    def __init__(self, servers: dict[str, dict[str, Any]] | None = None) -> None:
        self.servers = dict(servers or {})
        self._lock = asyncio.Lock()
        self._owner_task: asyncio.Task[None] | None = None
        self._close_event: asyncio.Event | None = None
        self._sessions: dict[str, Any] = {}
        self._routes: dict[str, dict[str, Any]] = {}
        self._connected = False

    @classmethod
    def from_config_path(cls, config_path: Path | None) -> McpClientManager:
        if config_path is None:
            return cls({})
        return cls(parse_mcp_servers_config(config_path))

    @property
    def enabled(self) -> bool:
        return bool(self.servers)

    async def _session_owner_loop(
        self, ready_fut: asyncio.Future[None], close_ev: asyncio.Event
    ) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            if not ready_fut.done():
                ready_fut.set_exception(
                    RuntimeError("连接外部 MCP Server 需要安装可选依赖：uv sync --extra mcp")
                )
            raise RuntimeError("连接外部 MCP Server 需要安装可选依赖：uv sync --extra mcp") from exc

        try:
            async with contextlib.AsyncExitStack() as stack:
                sessions: dict[str, Any] = {}
                routes: dict[str, dict[str, Any]] = {}
                for srv_name, cfg in sorted(self.servers.items()):
                    transport = cfg["transport"]
                    if transport == "stdio":
                        params = StdioServerParameters(
                            command=cfg["command"],
                            args=cfg.get("args") or [],
                            env=build_scrubbed_env(cfg.get("env")),
                            cwd=cfg.get("cwd"),
                        )
                        streams = await stack.enter_async_context(stdio_client(params))
                        read_stream, write_stream = streams[0], streams[1]
                    elif transport == "streamable_http":
                        from mcp.client.streamable_http import streamablehttp_client

                        streams = await stack.enter_async_context(
                            streamablehttp_client(cfg["url"], headers=cfg.get("headers") or None)
                        )
                        read_stream, write_stream = streams[0], streams[1]
                    else:
                        raise ValueError(f"不支持的 MCP 传输类型：{transport}")

                    session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                    await session.initialize()
                    sessions[srv_name] = session

                    listed = await session.list_tools()
                    for t_obj in getattr(listed, "tools", []) or []:
                        remote_name = str(getattr(t_obj, "name", "")).strip()
                        if not remote_name:
                            continue
                        ns_name = make_mcp_tool_name(srv_name, remote_name)
                        raw_schema = getattr(t_obj, "inputSchema", None) or {
                            "type": "object",
                            "properties": {},
                        }
                        if not isinstance(raw_schema, dict):
                            raw_schema = {"type": "object", "properties": {}}
                        schema_copy = dict(raw_schema)
                        schema_copy.setdefault("type", "object")
                        schema_copy.setdefault("properties", {})

                        annotations = getattr(t_obj, "annotations", None)
                        read_only = bool(
                            getattr(annotations, "readOnlyHint", False)
                            if annotations is not None
                            else False
                        )
                        desc = str(
                            getattr(t_obj, "description", "") or f"外部 MCP 工具 {srv_name}.{remote_name}"
                        )
                        spec = {
                            "name": ns_name,
                            "description": f"[MCP:{srv_name}] {desc}"[:500],
                            "input_schema": schema_copy,
                        }
                        routes[ns_name] = {
                            "server": srv_name,
                            "remote_name": remote_name,
                            "read_only": read_only,
                            "spec": spec,
                        }

                self._sessions = sessions
                self._routes = routes
                self._connected = True
                if not ready_fut.done():
                    ready_fut.set_result(None)
                await close_ev.wait()
        except BaseException as exc:
            if not ready_fut.done():
                ready_fut.set_exception(exc)
        finally:
            self._sessions.clear()
            self._connected = False

    async def connect_all(self) -> list[dict[str, Any]]:
        """连接全部配置的外部 MCP Server，返回可供模型使用的规范化工具定义列表。"""
        if not self.servers:
            return []
        async with self._lock:
            if self._connected and self._owner_task is not None and not self._owner_task.done():
                return [entry["spec"] for _, entry in sorted(self._routes.items())]
            loop = asyncio.get_running_loop()
            ready_fut: asyncio.Future[None] = loop.create_future()
            close_ev = asyncio.Event()
            owner = asyncio.create_task(self._session_owner_loop(ready_fut, close_ev))
            self._owner_task = owner
            self._close_event = close_ev
            try:
                await ready_fut
            except BaseException:
                close_ev.set()
                with contextlib.suppress(BaseException):
                    await owner
                self._owner_task = None
                self._close_event = None
                raise
            return [entry["spec"] for _, entry in sorted(self._routes.items())]

    def route_metadata(self) -> dict[str, dict[str, Any]]:
        """导出可持久化到 run.metadata.mcp_tools 的工具快照与路由表。"""
        return {
            ns_name: {
                "server": info["server"],
                "remote_name": info["remote_name"],
                "read_only": info["read_only"],
                "spec": info["spec"],
            }
            for ns_name, info in sorted(self._routes.items())
        }

    def is_read_only_tool(self, namespaced_name: str, frozen_routes: dict[str, Any] | None = None) -> bool:
        if frozen_routes and namespaced_name in frozen_routes:
            return bool(frozen_routes[namespaced_name].get("read_only", False))
        info = self._routes.get(namespaced_name)
        return bool(info and info.get("read_only", False))

    async def call_tool(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
        *,
        frozen_routes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._connected:
            await self.connect_all()
        route = self._routes.get(namespaced_name) or (
            frozen_routes.get(namespaced_name) if frozen_routes else None
        )
        if not route:
            return {"ok": False, "code": "unknown_mcp_tool", "error": f"未找到外部 MCP 工具：{namespaced_name}"}
        srv_name = route["server"]
        remote_name = route["remote_name"]
        session = self._sessions.get(srv_name)
        if session is None:
            return {"ok": False, "code": "mcp_server_disconnected", "error": f"MCP 服务未连接：{srv_name}"}

        call_res = await session.call_tool(remote_name, arguments or {})
        is_error = bool(getattr(call_res, "isError", False))
        texts: list[str] = []
        for blk in getattr(call_res, "content", []) or []:
            txt = getattr(blk, "text", None)
            if isinstance(txt, str):
                texts.append(redact_secrets(txt))
            else:
                texts.append(redact_secrets(str(blk)))
        combined = "\n".join(texts)
        parsed_json: Any = None
        if len(texts) == 1:
            with contextlib.suppress(ValueError, TypeError):
                parsed_json = redact_secrets(json.loads(texts[0]))

        if isinstance(parsed_json, dict) and "ok" in parsed_json:
            return {
                "mcp_server": srv_name,
                "mcp_tool": remote_name,
                **parsed_json,
                "ok": bool(parsed_json["ok"]) and not is_error,
            }
        return {
            "ok": not is_error,
            "code": "mcp_error" if is_error else "ok",
            "mcp_server": srv_name,
            "mcp_tool": remote_name,
            "content": parsed_json if parsed_json is not None else combined,
        }

    async def aclose(self) -> None:
        async with self._lock:
            owner = self._owner_task
            close_ev = self._close_event
            self._owner_task = None
            self._close_event = None
            self._sessions.clear()
            self._connected = False
            if close_ev is not None:
                close_ev.set()
            if owner is not None:
                with contextlib.suppress(BaseException):
                    await owner

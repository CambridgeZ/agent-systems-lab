"""轻量级异步 Stdio JSON-RPC 2.0 LSP 客户端管理器（LspClientManager）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   为 Coding Agent 提供超越纯正则/文本检索（grep）的编译器级语义代码智能：跳转定义
   （`goToDefinition`）、全局引用追踪（`findReferences`）、类型悬停文档（`hover`）、
   文档/工作区符号大纲（`documentSymbol` / `workspaceSymbol`）以及编辑后即时静态诊断
   （`get_file_diagnostics` / `diagnostics`）。

2. 协议与可靠性设计（Protocol & Reliability Invariants）
   - 严格遵循 LSP 3.17 Stdio `Content-Length` 字节分帧（注意按 UTF-8 编码字节数而非字符数
     计算长度，防止含中文注释/字符串时帧头错位断流）。
   - 自动探测本地已安装的语言服务器（`basedpyright-langserver`、`pyright-langserver`、
     `typescript-language-server`、`gopls`、`rust-analyzer`），未安装时零报错优雅降级返回
     `{"available": False}` 或 `[]`。
   - 凭证隔离与进程组清场：派生语言服务器前经 `build_scrubbed_env()` 剥离宿主密钥，后台任务
     持续抽干 `stderr` 防止管道写满死锁，`close_all()` 先发 `shutdown`/`exit` 再对整个进程组
     执行 `SIGTERM -> SIGKILL` 回收，绝不遗留孤儿语言服务器守护进程。
   - 路径边界安全：所有文件同步与查询强制通过 `resolve_host_workspace_path` 验界，拒绝通过
     LSP 间接读取工作区外文件或 `.env` 等敏感凭证。
================================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
from typing import Any
from urllib.parse import unquote, urlparse

from .host_tools import HostToolError, resolve_host_workspace_path
from .permissions import build_scrubbed_env

LSP_OPERATIONS = frozenset(
    {
        "goToDefinition",
        "findReferences",
        "hover",
        "documentSymbol",
        "workspaceSymbol",
        "diagnostics",
    }
)

_MAX_LSP_FRAME_BYTES = 16 * 1024 * 1024  # 单帧 JSON-RPC 载荷上限 16 MiB
_MAX_RESULT_ITEMS = 100

# 文件扩展名 -> (语言族, LSP languageId)
_EXT_TO_LANGUAGE: dict[str, tuple[str, str]] = {
    ".py": ("python", "python"),
    ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"),
    ".tsx": ("typescript", "typescriptreact"),
    ".js": ("typescript", "javascript"),
    ".jsx": ("typescript", "javascriptreact"),
    ".mjs": ("typescript", "javascript"),
    ".cjs": ("typescript", "javascript"),
    ".go": ("go", "go"),
    ".rs": ("rust", "rust"),
}

# 语言族 -> 按优先级排列的候选语言服务器启动命令（binary, args...）
_LANGUAGE_SERVER_CANDIDATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "python": (
        ("basedpyright-langserver", "--stdio"),
        ("pyright-langserver", "--stdio"),
    ),
    "typescript": (("typescript-language-server", "--stdio"),),
    "go": (("gopls",),),
    "rust": (("rust-analyzer",),),
}

_SEVERITY_NAMES: dict[int, str] = {
    1: "error",
    2: "warning",
    3: "information",
    4: "hint",
}

_SYMBOL_KIND_NAMES: dict[int, str] = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}


def _uri_to_rel_path(uri: str, workspace: Path) -> str:
    """将 LSP `file://` URI 尽量还原为工作区相对 POSIX 路径。"""
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return str(uri or "")
    parsed = urlparse(uri)
    raw_fs_path = unquote(parsed.path)
    try:
        resolved = Path(raw_fs_path).resolve()
        ws_root = workspace.resolve()
        if resolved == ws_root or resolved.is_relative_to(ws_root):
            return resolved.relative_to(ws_root).as_posix()
        return str(resolved)
    except OSError:
        return raw_fs_path


def _normalize_uri(uri: str) -> str:
    """将 `file://` URI 物理规范化，消除 macOS `/var` 与 `/private/var` 映射不一致。"""
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return str(uri or "")
    parsed = urlparse(uri)
    raw_path = unquote(parsed.path)
    with contextlib.suppress(OSError):
        return Path(raw_path).resolve().as_uri()
    return uri


def _normalize_range(lsp_range: Any) -> dict[str, int]:
    """将 LSP 0-based Range 归一化为 1-based 行列坐标字典。"""
    if not isinstance(lsp_range, dict):
        return {"line": 1, "character": 1, "end_line": 1, "end_character": 1}
    start = lsp_range.get("start") if isinstance(lsp_range.get("start"), dict) else {}
    end = lsp_range.get("end") if isinstance(lsp_range.get("end"), dict) else start
    return {
        "line": int(start.get("line", 0)) + 1,
        "character": int(start.get("character", 0)) + 1,
        "end_line": int(end.get("line", start.get("line", 0))) + 1,
        "end_character": int(end.get("character", start.get("character", 0))) + 1,
    }


def _normalize_locations(raw: Any, workspace: Path) -> list[dict[str, Any]]:
    """归一化 LSP `Location | Location[] | LocationLink[] | None` 为统一位置列表。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "targetUri" in item:
            uri = str(item.get("targetUri") or "")
            rng = item.get("targetSelectionRange") or item.get("targetRange")
        else:
            uri = str(item.get("uri") or "")
            rng = item.get("range")
        if not uri:
            continue
        coords = _normalize_range(rng)
        out.append({"path": _uri_to_rel_path(uri, workspace), "uri": uri, **coords})
        if len(out) >= _MAX_RESULT_ITEMS:
            break
    return out


def _normalize_hover(raw: Any) -> dict[str, Any] | None:
    """将 LSP Hover 响应（MarkupContent / MarkedString / list）提取为纯可读 Markdown 文本。"""
    if not isinstance(raw, dict):
        return None
    contents = raw.get("contents")
    parts: list[str] = []

    def _collect(node: Any) -> None:
        if isinstance(node, str):
            if node.strip():
                parts.append(node.strip())
        elif isinstance(node, dict):
            val = node.get("value")
            if isinstance(val, str) and val.strip():
                lang = node.get("language")
                if isinstance(lang, str) and lang.strip():
                    parts.append(f"```{lang.strip()}\n{val.strip()}\n```")
                else:
                    parts.append(val.strip())
        elif isinstance(node, list):
            for sub in node:
                _collect(sub)

    _collect(contents)
    result: dict[str, Any] = {"contents": "\n\n".join(parts)}
    if isinstance(raw.get("range"), dict):
        result["range"] = _normalize_range(raw["range"])
    return result


def _normalize_symbols(raw: Any, workspace: Path, *, default_path: str = "") -> list[dict[str, Any]]:
    """归一化 `DocumentSymbol[]`（树形）与 `SymbolInformation[]`（扁平）为紧凑列表。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []

    def _visit(sym: Any, container: str = "") -> None:
        if len(out) >= _MAX_RESULT_ITEMS or not isinstance(sym, dict):
            return
        name = str(sym.get("name") or "").strip()
        kind_num = int(sym.get("kind", 0)) if isinstance(sym.get("kind"), int) else 0
        kind_name = _SYMBOL_KIND_NAMES.get(kind_num, str(kind_num))
        if "location" in sym and isinstance(sym["location"], dict):
            loc = sym["location"]
            uri = str(loc.get("uri") or "")
            coords = _normalize_range(loc.get("range"))
            entry: dict[str, Any] = {
                "name": name,
                "kind": kind_name,
                "path": _uri_to_rel_path(uri, workspace) if uri else default_path,
                **coords,
            }
            if sym.get("containerName"):
                entry["container"] = str(sym["containerName"])
            out.append(entry)
            return

        coords = _normalize_range(sym.get("selectionRange") or sym.get("range"))
        entry = {
            "name": name,
            "kind": kind_name,
            "path": default_path,
            **coords,
        }
        if sym.get("detail"):
            entry["detail"] = str(sym["detail"])
        if container:
            entry["container"] = container
        out.append(entry)
        children = sym.get("children")
        if isinstance(children, list):
            for child in children:
                _visit(child, container=name)

    for item in raw:
        _visit(item)
        if len(out) >= _MAX_RESULT_ITEMS:
            break
    return out


def _normalize_diagnostics(raw_diags: Any, uri: str, workspace: Path) -> list[dict[str, Any]]:
    """归一化 LSP `publishDiagnostics` 诊断条目。"""
    if not isinstance(raw_diags, list):
        return []
    rel_path = _uri_to_rel_path(uri, workspace)
    out: list[dict[str, Any]] = []
    for item in raw_diags:
        if not isinstance(item, dict):
            continue
        coords = _normalize_range(item.get("range"))
        sev_num = int(item.get("severity", 1)) if isinstance(item.get("severity"), int) else 1
        out.append(
            {
                "path": rel_path,
                "severity": _SEVERITY_NAMES.get(sev_num, "error"),
                "severity_code": sev_num,
                "code": item.get("code"),
                "source": item.get("source"),
                "message": str(item.get("message") or "").strip(),
                **coords,
            }
        )
        if len(out) >= _MAX_RESULT_ITEMS:
            break
    return out


class _LspServerSession:
    """单个 Stdio JSON-RPC 2.0 语言服务器子进程会话。"""

    def __init__(self, *, workspace: Path, language_family: str, argv: list[str]) -> None:
        self.workspace = workspace.resolve()
        self.language_family = language_family
        self.argv = list(argv)
        self.server_name = Path(argv[0]).name if argv else language_family

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._opened_docs: dict[str, tuple[int, str]] = {}  # norm_uri -> (version, text)
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_seq: dict[str, int] = {}
        self._diag_events: dict[str, asyncio.Event] = {}
        self._initialized = False
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return (
            not self._closed
            and self._initialized
            and self._proc is not None
            and self._proc.returncode is None
        )

    async def ensure_started(self, *, init_timeout: float = 10.0) -> None:
        async with self._start_lock:
            if self.is_alive:
                return
            await self._cleanup_process()
            self._closed = False
            self._initialized = False
            self._opened_docs.clear()

            env = build_scrubbed_env()
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

            root_uri = self.workspace.as_uri()
            init_params = {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "rootPath": str(self.workspace),
                "workspaceFolders": [{"uri": root_uri, "name": self.workspace.name}],
                "capabilities": {
                    "textDocument": {
                        "synchronization": {
                            "dynamicRegistration": False,
                            "didSave": True,
                        },
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {
                        "symbol": {},
                        "workspaceFolders": True,
                    },
                },
            }
            try:
                await self.request("initialize", init_params, timeout=init_timeout)
                await self.notify("initialized", {})
                self._initialized = True
            except Exception:
                await self._cleanup_process()
                raise

    async def _send_message(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            raise ConnectionError(f"LSP 进程 ({self.server_name}) 未运行或已退出。")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        async with self._write_lock:
            self._proc.stdin.write(frame)
            await self._proc.stdin.drain()

    async def request(self, method: str, params: Any, *, timeout: float = 5.0) -> Any:
        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send_message({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: Any) -> None:
        await self._send_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    break
        except (asyncio.CancelledError, OSError):
            pass

    async def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        reader = self._proc.stdout
        try:
            while True:
                content_length: int | None = None
                while True:
                    header_line = await reader.readline()
                    if not header_line:
                        return
                    if header_line in (b"\r\n", b"\n"):
                        break
                    decoded = header_line.decode("ascii", errors="ignore").strip()
                    if ":" in decoded:
                        k, v = decoded.split(":", 1)
                        if k.strip().lower() == "content-length":
                            with contextlib.suppress(ValueError):
                                content_length = int(v.strip())

                if content_length is None or content_length <= 0 or content_length > _MAX_LSP_FRAME_BYTES:
                    continue
                body = await reader.readexactly(content_length)
                try:
                    msg = json.loads(body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._handle_incoming(msg)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self._fail_all_pending(ConnectionError(f"LSP 连接已断开 ({self.server_name})"))

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _handle_incoming(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg["id"] is not None
        has_method = "method" in msg and isinstance(msg["method"], str)

        # 1. Response to our request
        if has_id and not has_method:
            req_id = msg["id"]
            fut = self._pending.get(req_id)
            if fut is not None and not fut.done():
                if "error" in msg and msg["error"] is not None:
                    err_obj = msg["error"]
                    err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                    fut.set_exception(RuntimeError(f"LSP error: {err_msg}"))
                else:
                    fut.set_result(msg.get("result"))
            return

        # 2. Server-to-client Request（必须回包否则部分语言服务器会挂起等待）
        if has_id and has_method:
            method = msg["method"]
            result_val: Any = None
            if method == "workspace/configuration":
                items = (msg.get("params") or {}).get("items") if isinstance(msg.get("params"), dict) else []
                result_val = [{} for _ in (items if isinstance(items, list) else [])]
            with contextlib.suppress(Exception):
                await self._send_message({"jsonrpc": "2.0", "id": msg["id"], "result": result_val})
            return

        # 3. Server Notification
        if has_method and msg["method"] == "textDocument/publishDiagnostics":
            params = msg.get("params")
            if isinstance(params, dict) and isinstance(params.get("uri"), str):
                norm_uri = _normalize_uri(params["uri"])
                diags = _normalize_diagnostics(params.get("diagnostics", []), norm_uri, self.workspace)
                self._diagnostics[norm_uri] = diags
                self._diag_seq[norm_uri] = self._diag_seq.get(norm_uri, 0) + 1
                ev = self._diag_events.get(norm_uri)
                if ev is not None:
                    ev.set()

    def diagnostics_seq(self, norm_uri: str) -> int:
        return self._diag_seq.get(norm_uri, 0)

    def current_diagnostics(self, norm_uri: str) -> list[dict[str, Any]]:
        return list(self._diagnostics.get(norm_uri, ()))

    async def sync_document(self, target_path: Path, language_id: str) -> tuple[str, bool]:
        """同步文件内容到语言服务器（首次 didOpen，内容变化时 didChange）。返回 `(norm_uri, changed)`。"""
        resolved = target_path.resolve()
        norm_uri = resolved.as_uri()
        text = resolved.read_text(encoding="utf-8")
        prev = self._opened_docs.get(norm_uri)
        if prev is None:
            self._opened_docs[norm_uri] = (1, text)
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": norm_uri,
                        "languageId": language_id,
                        "version": 1,
                        "text": text,
                    }
                },
            )
            return norm_uri, True

        prev_ver, prev_text = prev
        if prev_text == text:
            return norm_uri, False

        next_ver = prev_ver + 1
        self._opened_docs[norm_uri] = (next_ver, text)
        await self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": norm_uri, "version": next_ver},
                "contentChanges": [{"text": text}],
            },
        )
        return norm_uri, True

    async def wait_for_diagnostics(self, norm_uri: str, prev_seq: int, timeout: float) -> list[dict[str, Any]]:
        """等待自 `prev_seq` 之后新推送的 `publishDiagnostics` 通知。"""
        if self._diag_seq.get(norm_uri, 0) > prev_seq:
            return self.current_diagnostics(norm_uri)
        ev = self._diag_events.get(norm_uri)
        if ev is None:
            ev = asyncio.Event()
            self._diag_events[norm_uri] = ev
        ev.clear()
        if self._diag_seq.get(norm_uri, 0) > prev_seq:
            return self.current_diagnostics(norm_uri)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout=max(0.05, float(timeout)))
        return self.current_diagnostics(norm_uri)

    async def _cleanup_process(self) -> None:
        self._initialized = False
        self._fail_all_pending(ConnectionError("LSP session closed"))

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
        self._stderr_task = None

        proc = self._proc
        self._proc = None
        if proc is None:
            return

        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()

        pgid = proc.pid
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGTERM)
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=0.6)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._initialized and self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                await self.request("shutdown", None, timeout=1.0)
            with contextlib.suppress(Exception):
                await self.notify("exit", None)
        await self._cleanup_process()


class LspClientManager:
    """按工作区与语言族按需惰性启动并复用 LSP 语言服务器会话的管理器。"""

    def __init__(
        self,
        workspace: Path | str | None = None,
        *,
        server_overrides: dict[str, list[str]] | None = None,
        init_timeout: float = 10.0,
    ) -> None:
        self.workspace: Path | None = Path(workspace).resolve() if workspace is not None else None
        self.server_overrides: dict[str, list[str]] = (
            {str(k).strip().lower(): list(v) for k, v in server_overrides.items() if v}
            if server_overrides
            else {}
        )
        self.init_timeout = max(1.0, float(init_timeout))
        self._sessions: dict[tuple[str, str], _LspServerSession] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> LspClientManager:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close_all()

    def _resolve_workspace(self, workspace: Path | str | None = None) -> Path:
        ws = workspace if workspace is not None else self.workspace
        if ws is None:
            raise ValueError("LspClientManager 需要提供有效的 workspace 路径。")
        return Path(ws).resolve()

    def detect_server_command(self, language_family: str) -> list[str] | None:
        """探测指定语言族可用的本地 LSP 可执行命令；未安装时返回 None。"""
        fam = language_family.strip().lower()
        if fam in self.server_overrides:
            return list(self.server_overrides[fam])
        for candidate in _LANGUAGE_SERVER_CANDIDATES.get(fam, ()):
            binary = shutil.which(candidate[0])
            if binary:
                return [binary, *candidate[1:]]
        return None

    def detect_available_servers(self) -> dict[str, list[str]]:
        """返回本机已探测到的所有受支持语言服务器映射（language_family -> argv）。"""
        detected: dict[str, list[str]] = {}
        for fam in _LANGUAGE_SERVER_CANDIDATES:
            cmd = self.detect_server_command(fam)
            if cmd:
                detected[fam] = cmd
        for fam, cmd in self.server_overrides.items():
            if fam not in detected:
                detected[fam] = list(cmd)
        return detected

    def _detect_language_for_path(self, path: Path) -> tuple[str | None, str]:
        ext = path.suffix.lower()
        if ext in _EXT_TO_LANGUAGE:
            return _EXT_TO_LANGUAGE[ext]
        if ext in self.server_overrides:
            return ext, ext.lstrip(".") or "plaintext"
        return None, "plaintext"

    async def _get_or_start_session(
        self,
        workspace: Path,
        language_family: str,
    ) -> _LspServerSession | None:
        argv = self.detect_server_command(language_family)
        if not argv:
            return None
        key = (str(workspace.resolve()), language_family)
        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = _LspServerSession(
                    workspace=workspace,
                    language_family=language_family,
                    argv=argv,
                )
                self._sessions[key] = session
        try:
            await session.ensure_started(init_timeout=self.init_timeout)
        except Exception:
            async with self._lock:
                self._sessions.pop(key, None)
            await session.close()
            return None
        return session

    async def get_file_diagnostics(
        self,
        file_path: str | Path,
        timeout: float = 2.0,
        *,
        workspace: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        """同步文件内容（didOpen / didChange）并等待语言服务器推送的最新诊断列表。

        若对应语言未安装任何 LSP 二进制，或超时未收到诊断，均优雅返回 `[]` 而绝不中断主流程。
        """
        ws = self._resolve_workspace(workspace)
        try:
            _, resolved = resolve_host_workspace_path(ws, str(file_path), allow_missing=False)
        except HostToolError:
            return []

        lang_family, language_id = self._detect_language_for_path(resolved)
        if not lang_family:
            return []
        session = await self._get_or_start_session(ws, lang_family)
        if session is None:
            return []

        norm_uri = resolved.resolve().as_uri()
        prev_seq = session.diagnostics_seq(norm_uri)
        try:
            _, changed = await session.sync_document(resolved, language_id)
        except (OSError, UnicodeError, ConnectionError):
            return []

        if changed or prev_seq == 0:
            return await session.wait_for_diagnostics(norm_uri, prev_seq, timeout=timeout)
        return session.current_diagnostics(norm_uri)

    async def query(
        self,
        operation: str,
        file_path: str | Path,
        line: int = 1,
        character: int = 1,
        query_text: str | None = None,
        *,
        timeout: float = 5.0,
        workspace: Path | str | None = None,
        one_based: bool = True,
    ) -> dict[str, Any]:
        """执行 LSP 语义查询（支持 goToDefinition / findReferences / hover / documentSymbol / workspaceSymbol / diagnostics）。"""
        if operation not in LSP_OPERATIONS:
            raise ValueError(f"不支持的 LSP 操作 '{operation}'，必须是 {sorted(LSP_OPERATIONS)} 之一。")

        ws = self._resolve_workspace(workspace)
        allow_dir = operation == "workspaceSymbol"
        rel_path, resolved = resolve_host_workspace_path(
            ws,
            str(file_path or "."),
            allow_missing=False,
            allow_directory=allow_dir,
        )

        lang_family: str | None
        language_id: str
        if resolved.is_dir():
            # workspaceSymbol 传入目录时，自动选取已存在会话或本机首个可用语言服务器
            avail = self.detect_available_servers()
            lang_family = next(iter(avail.keys()), None)
            language_id = lang_family or "plaintext"
        else:
            lang_family, language_id = self._detect_language_for_path(resolved)

        if not lang_family:
            return {
                "ok": True,
                "available": False,
                "operation": operation,
                "path": rel_path,
                "server": None,
                "result": None,
                "diagnostics": [],
                "message": f"未识别文件 {rel_path} 对应的受支持 LSP 语言。",
            }

        session = await self._get_or_start_session(ws, lang_family)
        if session is None:
            return {
                "ok": True,
                "available": False,
                "operation": operation,
                "path": rel_path,
                "server": None,
                "result": None,
                "diagnostics": [],
                "message": f"未检测到适用于 {lang_family} 的本地 LSP 服务器二进制文件。",
            }

        if operation == "diagnostics":
            diags = await self.get_file_diagnostics(rel_path, timeout=min(timeout, 2.0), workspace=ws)
            return {
                "ok": True,
                "available": True,
                "operation": operation,
                "path": rel_path,
                "server": session.server_name,
                "diagnostics": diags,
                "result": diags,
            }

        norm_uri: str | None = None
        if resolved.is_file():
            norm_uri, _ = await session.sync_document(resolved, language_id)

        lsp_line = max(0, int(line) - 1) if one_based else max(0, int(line))
        lsp_char = max(0, int(character) - 1) if one_based else max(0, int(character))

        try:
            if operation == "goToDefinition":
                assert norm_uri is not None
                raw = await session.request(
                    "textDocument/definition",
                    {"textDocument": {"uri": norm_uri}, "position": {"line": lsp_line, "character": lsp_char}},
                    timeout=timeout,
                )
                normalized: Any = _normalize_locations(raw, ws)
            elif operation == "findReferences":
                assert norm_uri is not None
                raw = await session.request(
                    "textDocument/references",
                    {
                        "textDocument": {"uri": norm_uri},
                        "position": {"line": lsp_line, "character": lsp_char},
                        "context": {"includeDeclaration": True},
                    },
                    timeout=timeout,
                )
                normalized = _normalize_locations(raw, ws)
            elif operation == "hover":
                assert norm_uri is not None
                raw = await session.request(
                    "textDocument/hover",
                    {"textDocument": {"uri": norm_uri}, "position": {"line": lsp_line, "character": lsp_char}},
                    timeout=timeout,
                )
                normalized = _normalize_hover(raw)
            elif operation == "documentSymbol":
                assert norm_uri is not None
                raw = await session.request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": norm_uri}},
                    timeout=timeout,
                )
                normalized = _normalize_symbols(raw, ws, default_path=rel_path)
            else:  # workspaceSymbol
                raw = await session.request(
                    "workspace/symbol",
                    {"query": str(query_text or "")},
                    timeout=timeout,
                )
                normalized = _normalize_symbols(raw, ws)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "available": True,
                "code": "lsp_timeout",
                "operation": operation,
                "path": rel_path,
                "server": session.server_name,
                "error": f"LSP 查询 {operation} 超时（{timeout}s）。",
            }
        except Exception as exc:
            return {
                "ok": False,
                "available": True,
                "code": "lsp_error",
                "operation": operation,
                "path": rel_path,
                "server": session.server_name,
                "error": f"LSP 查询 {operation} 失败：{exc}",
            }

        return {
            "ok": True,
            "available": True,
            "operation": operation,
            "path": rel_path,
            "server": session.server_name,
            "result": normalized,
        }

    async def close_all(self) -> None:
        """优雅关闭并回收所有活跃 LSP 语言服务器进程组（幂等）。"""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

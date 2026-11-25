"""本地程序化工具编排引擎（PTC, Programmatic Tool Calling / Code-Execution-as-Orchestrator）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块实现受 Anthropic Programmatic Tool Calling 与 Code Execution with MCP 启发的
   本地只读代码编排执行器：允许模型在单次 LLM 回合内提交一段短小 Python 3 编排脚本，在隔离无网
   子进程中通过同步代理 `call_tool(name, **kwargs)` 或 `tools.<name>(**kwargs)` 批量循环调用
   只读工具（如 `fs_glob` -> `fs_grep` -> `fs_read`、`repo_search`、`symbol_search`、`lsp_query`、
   `memory_search` 等），在脚本内完成过滤、去重、关联聚合与统计，并仅通过 `print()` 输出最终紧凑
   摘要进入模型上下文窗口，大幅降低多跳工具调用的 Token 膨胀与往返延迟。

2. 协议与架构设计（Protocol & Architecture）
   - 隔离引导子进程（Sandboxed Subprocess）：使用 `sys.executable` 运行内联引导程序
     `_PTC_BOOTSTRAP_CODE`，当本机支持 OS 内核沙箱时通过 `HostKernelSandbox` /
     `build_sandboxed_shell_argv(..., allow_network=False)` 包装（macOS Seatbelt SBPL 禁出站
     网络与凭据读取 / Linux `bwrap` 隔离），叠加 `build_scrubbed_env()` 剥离宿主密钥环境变量，
     并以 `start_new_session=True` 建立独立进程组，确保超时或取消时可整树回收所有子孙进程。
   - 双通道隔离的换行 JSON-RPC 桥接（Newline-Delimited JSON-RPC Bridge）：
     * 引导程序启动后立刻复制原始管道 FD（`os.dup(0)` / `os.dup(1)`）作为专属 RPC 通道，将
       FD 0/1 重定向至 `/dev/null`，并把用户脚本的 `sys.stdout` 替换为有界内存 `_BoundedStringIO`。
       由此彻底保证用户代码中的 `print()`、第三方输出甚至底层 `os.write(1, ...)` 均绝不会污染
       或伪造 JSON-RPC 帧流；
     * 握手帧（Host -> Child）：`{"type": "exec", "code": ..., "max_output_chars": ...}`；
     * 工具调用帧（Child -> Host）：`{"type": "call", "id": ..., "name": ..., "arguments": ...}`；
     * 工具结果帧（Host -> Child）：`{"type": "result", "id": ..., "result": ...}`；
     * 完成帧（Child -> Host）：`{"type": "done", "ok": bool, "stdout": ..., "error": ...}`。

3. 核心安全与工程不变量（Core Security & Engineering Invariants）
   - 严格只读白名单不变量（Fail-Closed Allowlist）：`PTC_DEFAULT_ALLOWED_TOOLS` 严格取自
     `READONLY_HOST_TOOLS` 的纯只读子集，且永远剔除递归入口 `ptc_exec`；宿主在调度前对每次子调用
     执行白名单核验，任何未在白名单内的写入/变更工具（如 `fs_write`、`fs_edit`、`bash_exec`）
     绝不触发 `dispatch_fn`，直接返回 `{"ok": False, "code": "ptc_tool_not_allowed"}`。
   - 调用配额、输出体积与防泛洪不变量：有效子调用次数硬性封顶 `<= PTC_MAX_SUBCALLS`（24 次），
     总输出经 `redact_secrets` 脱敏并硬截断至 `<= PTC_MAX_OUTPUT_CHARS`（16000 字符），且连续
     拒绝调用超过阈值时主动中止脚本防止 RPC 管道泛洪。
   - 墙钟超时、协作取消与进程组零孤儿不变量：所有 I/O 等待与子工具调度均受统一墙钟截止时间
     （`<= PTC_MAX_TIMEOUT_SECONDS`）与 `cancel_event`（兼容 `threading.Event` / `asyncio.Event`）
     双重约束；`finally` 块始终对独立进程组执行 `SIGTERM -> 存活探测 -> SIGKILL` 两阶段清场，
     且独立后台协程持续抽干 `stderr` 至 EOF，杜绝 64KB 管道死锁与孤儿进程残留。
================================================================================
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Coroutine, Iterable
import contextlib
import inspect
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any

from .host_tools import HostKernelSandbox, HostToolError, build_sandboxed_shell_argv
from .observability import redact_secrets
from .permissions import READONLY_HOST_TOOLS, build_scrubbed_env

# ---------------------------------------------------------------------------
# 1. 核心安全配额常量与只读工具白名单
# ---------------------------------------------------------------------------

# 默认允许在 PTC 编排脚本内调用的纯只读工具白名单（严格限定为 READONLY_HOST_TOOLS 的无副作用子集，
# 排除 memory_write / subagent_run / ask_user_question / ptc_exec 等状态变更、交互或递归入口）。
PTC_DEFAULT_ALLOWED_TOOLS: frozenset[str] = (
    frozenset(
        {
            "fs_read",
            "fs_grep",
            "fs_glob",
            "repo_list",
            "repo_read",
            "repo_search",
            "repo_map",
            "symbol_search",
            "knowledge_search",
            "memory_read",
            "memory_search",
            "skill_search",
            "skill_inspect",
            "lsp_query",
            "mcp_tool_search",
            "mcp_list_resources",
            "mcp_read_resource",
            "a2a_list_agents",
            "a2a_discover_agent",
            "a2a_get_task",
        }
    )
    & READONLY_HOST_TOOLS
)

PTC_MAX_SUBCALLS = 24
PTC_MAX_TIMEOUT_SECONDS = 45
PTC_MAX_OUTPUT_CHARS = 16000
PTC_DEFAULT_TIMEOUT_SECONDS = 20
PTC_DEFAULT_MAX_CALLS = 16
PTC_MAX_CODE_CHARS = 32000

_PTC_MAX_RPC_LINE_BYTES = 2 * 1024 * 1024  # 单帧最大 2 MiB，防止恶意长行撑爆宿主内存
_PTC_MAX_STDERR_BYTES = 8 * 1024  # 最多保留 8 KiB stderr 用于诊断，超出部分继续抽干丢弃
_PTC_MAX_ERROR_CHARS = 4000
_PTC_MAX_REJECTED_CALLS = 64
_PTC_TRUNCATION_SUFFIX = "\n... [output truncated]"


# ---------------------------------------------------------------------------
# 2. JSON Schema Draft 2020-12 工具契约（PTC_TOOL_SPECS）
# ---------------------------------------------------------------------------

PTC_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "ptc_exec",
        "description": (
            "在隔离无网 Python 3 沙箱中执行一次性程序化工具编排脚本（Programmatic Tool Calling）："
            "在单次模型回合内通过同步函数 `call_tool(name, **kwargs)` 或动态代理 `tools.<name>(**kwargs)` "
            "循环批量调用只读检索工具（如 fs_glob/fs_grep/fs_read/repo_search/symbol_search/lsp_query/"
            "memory_search/mcp_list_resources 等），在脚本内部完成过滤、去重、统计与聚合后，仅通过 "
            "`print(...)` 输出最终紧凑结果返回上下文。仅允许调用白名单内的只读工具，任何变更型工具调用"
            "均会被宿主拒绝（ptc_tool_not_allowed）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": PTC_MAX_CODE_CHARS,
                    "description": (
                        "待执行的 Python 3 编排脚本。可直接同步调用 `call_tool(name, **kwargs)` 或 "
                        "`tools.<name>(**kwargs)` 获取只读工具返回字典，并在过滤聚合后仅 `print()` 最终精简摘要。"
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": PTC_MAX_TIMEOUT_SECONDS,
                    "default": PTC_DEFAULT_TIMEOUT_SECONDS,
                    "description": (
                        f"脚本总墙钟超时秒数（默认 {PTC_DEFAULT_TIMEOUT_SECONDS}，"
                        f"上限 {PTC_MAX_TIMEOUT_SECONDS}）。"
                    ),
                },
                "max_calls": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": PTC_MAX_SUBCALLS,
                    "default": PTC_DEFAULT_MAX_CALLS,
                    "description": (
                        f"单次脚本内允许执行的只读工具子调用次数上限"
                        f"（默认 {PTC_DEFAULT_MAX_CALLS}，上限 {PTC_MAX_SUBCALLS}）。"
                    ),
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    }
]


# ---------------------------------------------------------------------------
# 3. 隔离引导子进程内联脚本（Newline JSON-RPC + StringIO 通道隔离）
# ---------------------------------------------------------------------------

# 说明：
# - 第一行优先剔除 sys.path 中的 "" 与 "."，防止工作区内的 json.py / io.py / os.py 等同名文件
#   劫持标准库导入；
# - 通过 os.dup(0/1) 独占 JSON-RPC 协议通道并将原始 FD 0/1 重定向至 /dev/null，同时把 sys.stdout
#   替换为有界内存 _BoundedStringIO，保证任何 print() 或底层 FD 1 写入都无法破坏帧通道，且防止
#   死循环打印导致子进程 OOM；
# - 暴露 call_tool(name, **kwargs)（同时兼容 positional dict）与动态 tools.<name>(**kwargs) 代理，
#   严格按 id 匹配同步阻塞等待宿主返回 result 帧。
_PTC_BOOTSTRAP_CODE = r'''
import sys
sys.path = [p for p in sys.path if p not in ("", ".")]
import builtins
import io
import json
import os
import traceback


class _BoundedStringIO(io.StringIO):
    encoding = "utf-8"

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = max(0, int(limit))
        self._written = 0
        self.truncated = False

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        n = len(s)
        if n == 0:
            return 0
        rem = self._limit - self._written
        if rem <= 0:
            self.truncated = True
            return n
        if n > rem:
            super().write(s[:rem])
            self._written += rem
            self.truncated = True
            return n
        super().write(s)
        self._written += n
        return n


def _ptc_bootstrap_main() -> None:
    rpc_in_fd = os.dup(0)
    rpc_out_fd = os.dup(1)
    try:
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(devnull_fd, 0)
            os.dup2(devnull_fd, 1)
        finally:
            os.close(devnull_fd)
    except OSError:
        pass

    rpc_in = os.fdopen(rpc_in_fd, "r", encoding="utf-8", errors="replace", newline="\n")
    rpc_out = os.fdopen(rpc_out_fd, "w", encoding="utf-8", buffering=1)

    def _send(frame: dict) -> None:
        rpc_out.write(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
        rpc_out.flush()

    try:
        init_line = rpc_in.readline()
        if not init_line:
            return
        init_msg = json.loads(init_line)
        if not isinstance(init_msg, dict) or init_msg.get("type") != "exec":
            _send({"type": "done", "ok": False, "stdout": "", "error": "invalid ptc exec handshake"})
            return

        code = init_msg.get("code", "")
        max_output_chars = int(init_msg.get("max_output_chars", 16256))
        captured = _BoundedStringIO(max_output_chars)

        sys.stdin = io.StringIO("")
        sys.stdout = captured
        sys.__stdin__ = sys.stdin
        sys.__stdout__ = captured

        call_seq = 0

        def call_tool(name, _arguments=None, /, **kwargs):
            nonlocal call_seq
            if not isinstance(name, str) or not name.strip():
                raise ValueError("call_tool: tool name must be a non-empty string")
            if _arguments is not None:
                if not isinstance(_arguments, dict):
                    raise TypeError("call_tool: positional arguments must be a dict")
                arguments = dict(_arguments)
                arguments.update(kwargs)
            else:
                arguments = dict(kwargs)
            call_seq += 1
            req_id = f"c{call_seq}"
            _send(
                {
                    "type": "call",
                    "id": req_id,
                    "name": name.strip(),
                    "arguments": arguments,
                }
            )
            while True:
                raw_resp = rpc_in.readline()
                if not raw_resp:
                    raise RuntimeError("PTC host bridge closed stdin before returning tool result")
                resp = json.loads(raw_resp)
                if isinstance(resp, dict) and resp.get("type") == "result" and resp.get("id") == req_id:
                    return resp.get("result")

        class _ToolsProxy:
            def __getattr__(self, tool_name: str):
                if tool_name.startswith("__") and tool_name.endswith("__"):
                    raise AttributeError(tool_name)

                def _invoke(_arguments=None, /, **kwargs):
                    return call_tool(tool_name, _arguments, **kwargs)

                _invoke.__name__ = tool_name
                return _invoke

            def __getitem__(self, tool_name: str):
                return self.__getattr__(tool_name)

            def __repr__(self) -> str:
                return "<PTCToolsProxy: use tools.<tool_name>(**kwargs) or call_tool(name, **kwargs)>"

        user_ns = {
            "__name__": "__main__",
            "__builtins__": builtins,
            "call_tool": call_tool,
            "tools": _ToolsProxy(),
        }

        ok = True
        error_text = None
        try:
            compiled = compile(code, "<ptc_script>", "exec")
            exec(compiled, user_ns, user_ns)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                ok = False
                error_text = f"SystemExit: {exc.code}"
        except BaseException:
            ok = False
            error_text = traceback.format_exc()

        _send(
            {
                "type": "done",
                "ok": ok,
                "stdout": captured.getvalue(),
                "stdout_truncated": captured.truncated,
                "error": error_text,
            }
        )
    finally:
        try:
            rpc_out.close()
        except OSError:
            pass
        try:
            rpc_in.close()
        except OSError:
            pass


_ptc_bootstrap_main()
'''


# ---------------------------------------------------------------------------
# 4. 辅助函数：白名单构建、沙箱启动参数、输出截断与进程组回收
# ---------------------------------------------------------------------------


class _RpcProtocolError(ValueError):
    """子进程 JSON-RPC 帧协议解析错误或单帧超限。"""


def filter_ptc_allowed_tools(
    candidates: Iterable[str] | None = None,
    *,
    extra_readonly_tools: Iterable[str] = (),
) -> frozenset[str]:
    """基于 `READONLY_HOST_TOOLS` 构建安全的 PTC 只读白名单，始终排除递归调用 `ptc_exec`。"""
    if candidates is None:
        base = PTC_DEFAULT_ALLOWED_TOOLS
    else:
        base = frozenset(str(name).strip() for name in candidates if isinstance(name, str) and name.strip())
        base = base & READONLY_HOST_TOOLS
    extras = frozenset(
        str(name).strip() for name in extra_readonly_tools if isinstance(name, str) and name.strip()
    )
    return (base | extras) - {"ptc_exec"}


def build_ptc_subprocess_argv(
    workspace: Path,
    *,
    sandbox_mode: str | None = None,
) -> list[str]:
    """构造受 OS 内核沙箱（默认禁出站网络）包装的 PTC Python 子进程启动 argv。"""
    eff_mode = HostKernelSandbox.normalize_mode(sandbox_mode)
    if eff_mode == "off":
        return [sys.executable, "-c", _PTC_BOOTSTRAP_CODE]
    return build_sandboxed_shell_argv(
        sys.executable,
        _PTC_BOOTSTRAP_CODE,
        workspace,
        allow_network=False,
        mode=eff_mode,
    )


def _is_cancelled(cancel_event: threading.Event | asyncio.Event | None) -> bool:
    return cancel_event is not None and bool(cancel_event.is_set())


def _is_async_callable(fn: Any) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    call_attr = getattr(fn, "__call__", None)
    return call_attr is not None and inspect.iscoroutinefunction(call_attr)


def _cap_output(
    text: str,
    *,
    max_chars: int = PTC_MAX_OUTPUT_CHARS,
    force_truncated: bool = False,
) -> str:
    """对脚本 stdout 执行密文脱敏并严格截断至 `<= max_chars`。"""
    redacted = redact_secrets(text)
    if not isinstance(redacted, str):
        redacted = str(redacted)
    if max_chars <= 0:
        return ""
    if len(redacted) <= max_chars and not force_truncated:
        return redacted
    if max_chars <= len(_PTC_TRUNCATION_SUFFIX):
        return redacted[:max_chars]
    keep = max_chars - len(_PTC_TRUNCATION_SUFFIX)
    return redacted[:keep] + _PTC_TRUNCATION_SUFFIX


def _cap_error(error: Any, *, max_chars: int = _PTC_MAX_ERROR_CHARS) -> str | None:
    if error is None:
        return None
    text = redact_secrets(str(error))
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    suffix = "\n... [error truncated]"
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)] + suffix


async def _read_rpc_line(stream: asyncio.StreamReader, max_bytes: int) -> bytes:
    """从子进程 RPC stdout 读取单行 JSON 帧，限制单帧体积防止 OOM。"""
    try:
        line = await stream.readline()
    except ValueError as exc:
        raise _RpcProtocolError(f"PTC JSON-RPC 单帧超出上限 ({max_bytes} bytes)") from exc
    if len(line) > max_bytes:
        raise _RpcProtocolError(f"PTC JSON-RPC 单帧超出上限 ({max_bytes} bytes)")
    return line


async def _drain_stderr(
    stream: asyncio.StreamReader | None,
    *,
    max_bytes: int = _PTC_MAX_STDERR_BYTES,
) -> str:
    """持续抽干子进程 stderr 至 EOF（防 64KB 管道死锁），仅保留前缀用于异常诊断。"""
    if stream is None:
        return ""
    buf = bytearray()
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            if len(buf) < max_bytes:
                take = min(len(chunk), max_bytes - len(buf))
                buf.extend(chunk[:take])
    except OSError:
        pass
    redacted = redact_secrets(buf.decode("utf-8", errors="replace"))
    return redacted if isinstance(redacted, str) else str(redacted)


async def _await_with_deadline_and_cancel(
    coro: Coroutine[Any, Any, Any],
    *,
    deadline: float,
    cancel_event: threading.Event | asyncio.Event | None,
    poll_interval: float = 0.05,
) -> tuple[str, Any]:
    """在统一墙钟截止时间与协作取消信号约束下等待单个协程，不反复取消底层流读取器。"""
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    try:
        while True:
            if _is_cancelled(cancel_event):
                return "cancelled", None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            step = min(poll_interval, remaining) if cancel_event is not None else remaining
            done, _ = await asyncio.wait({task}, timeout=step)
            if task in done:
                return "ok", task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        elif not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()


async def _invoke_dispatch_safe(
    dispatch_fn: Callable[[str, dict[str, Any]], Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, Any]:
    """兼容同步与异步 `dispatch_fn`，将异常收敛为结构化错误字典回传给脚本。"""
    try:
        if _is_async_callable(dispatch_fn):
            result = await dispatch_fn(tool_name, arguments)
        else:
            result = await asyncio.to_thread(dispatch_fn, tool_name, arguments)
            if inspect.isawaitable(result):
                result = await result
    except Exception as exc:
        err_code = str(getattr(exc, "code", "") or "ptc_subcall_error")
        err_msg = redact_secrets(str(exc))
        return (
            False,
            {
                "ok": False,
                "code": err_code,
                "tool": tool_name,
                "error": err_msg if isinstance(err_msg, str) else str(err_msg),
            },
        )
    if isinstance(result, dict):
        ok_flag = bool(result.get("ok", True))
    else:
        ok_flag = True
    return ok_flag, result


async def _send_rpc_frame(
    stdin: asyncio.StreamWriter,
    frame: dict[str, Any],
) -> None:
    payload = (json.dumps(frame, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    stdin.write(payload)
    await stdin.drain()


async def _reap_process_group(
    proc: asyncio.subprocess.Process,
    pgid: int,
    *,
    grace_seconds: float = 0.35,
) -> None:
    """对子进程组执行两阶段清场（SIGTERM -> 存活探测 -> SIGKILL），彻底防止孤儿进程残留。"""
    if proc.stdin is not None and not proc.stdin.is_closing():
        with contextlib.suppress(OSError):
            proc.stdin.close()

    group_alive = proc.returncode is None
    if not group_alive:
        try:
            os.killpg(pgid, 0)
            group_alive = True
        except ProcessLookupError:
            group_alive = False
        except (PermissionError, OSError):
            group_alive = False

    if not group_alive:
        return

    # 第 1 阶段：向整个进程组发送 SIGTERM
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)

    deadline = time.monotonic() + grace_seconds
    if proc.returncode is None:
        wait_budget = max(0.05, deadline - time.monotonic())
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), timeout=wait_budget)

    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
            await asyncio.sleep(0.03)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            break

    # 第 2 阶段：强制 SIGKILL 清场整个进程组并回收僵尸主进程
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)


# ---------------------------------------------------------------------------
# 5. 核心异步桥接入口（execute_ptc_script）
# ---------------------------------------------------------------------------


async def execute_ptc_script(
    code: str,
    *,
    workspace: str | Path,
    dispatch_fn: Callable[[str, dict[str, Any]], Any],
    allowed_tools: Collection[str] | None = PTC_DEFAULT_ALLOWED_TOOLS,
    timeout_seconds: float = 20.0,
    max_calls: int = 16,
    cancel_event: threading.Event | asyncio.Event | None = None,
    sandbox_mode: str | None = None,
) -> dict[str, Any]:
    """在沙箱隔离的 Python 子进程中执行 PTC 编排脚本，并通过换行 JSON-RPC 桥接只读工具调用。

    参数：
    - `code`: 待执行的 Python 3 编排脚本，可使用 `call_tool(name, **kwargs)` 或 `tools.<name>(**kwargs)`。
    - `workspace`: 工作区根目录路径（作为子进程 `cwd` 及 OS 内核沙箱边界）。
    - `dispatch_fn`: 工具调度回调 `dispatch_fn(tool_name, arguments) -> dict`，兼容同步函数与异步协程函数。
    - `allowed_tools`: 允许调用的工具白名单集合（默认 `PTC_DEFAULT_ALLOWED_TOOLS`，始终剔除 `ptc_exec`）。
    - `timeout_seconds`: 总墙钟超时秒数（自动夹紧至 `<= PTC_MAX_TIMEOUT_SECONDS`）。
    - `max_calls`: 最大允许工具子调用次数（自动夹紧至 `<= PTC_MAX_SUBCALLS`）。
    - `cancel_event`: 可选协作取消事件（支持 `threading.Event` 与 `asyncio.Event`）。
    - `sandbox_mode`: 可选 OS 沙箱模式覆盖（`"auto"` / `"strict"` / `"off"`，默认读取环境变量）。

    返回结构：
    `{"ok": bool, "stdout": str, "subcalls_count": int, "subcalls": [{"name", "ok", "duration_ms"}],
      "duration_ms": int, "error": str | None}`
    """
    t0 = time.monotonic()
    subcalls: list[dict[str, Any]] = []

    def _result(
        *,
        ok: bool,
        stdout: str = "",
        stdout_truncated: bool = False,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": bool(ok),
            "stdout": _cap_output(stdout, max_chars=PTC_MAX_OUTPUT_CHARS, force_truncated=stdout_truncated),
            "subcalls_count": len(subcalls),
            "subcalls": list(subcalls),
            "duration_ms": int(round((time.monotonic() - t0) * 1000.0)),
            "error": _cap_error(error),
        }

    # 基础参数校验（Fail-Closed）
    if not isinstance(code, str) or not code.strip():
        return _result(ok=False, error="PTC 脚本代码不能为空。")
    if "\x00" in code:
        return _result(ok=False, error="PTC 脚本代码包含非法空字节。")
    if len(code) > PTC_MAX_CODE_CHARS:
        return _result(ok=False, error=f"PTC 脚本长度超过上限 ({PTC_MAX_CODE_CHARS} chars)。")
    if not callable(dispatch_fn):
        return _result(ok=False, error="PTC dispatch_fn 必须是可调用对象。")
    if _is_cancelled(cancel_event):
        return _result(ok=False, error="PTC 脚本执行已取消。")

    try:
        ws_path = Path(workspace).expanduser().resolve()
    except OSError as exc:
        return _result(ok=False, error=f"无效的 PTC 工作区路径：{exc}")
    if not ws_path.exists() or not ws_path.is_dir():
        return _result(ok=False, error=f"PTC 工作区目录不存在：{ws_path}")

    if isinstance(timeout_seconds, bool):
        effective_timeout = float(PTC_DEFAULT_TIMEOUT_SECONDS)
    else:
        try:
            effective_timeout = max(0.1, min(float(PTC_MAX_TIMEOUT_SECONDS), float(timeout_seconds)))
        except (TypeError, ValueError):
            effective_timeout = float(PTC_DEFAULT_TIMEOUT_SECONDS)

    if isinstance(max_calls, bool):
        effective_max_calls = PTC_DEFAULT_MAX_CALLS
    else:
        try:
            effective_max_calls = max(0, min(PTC_MAX_SUBCALLS, int(max_calls)))
        except (TypeError, ValueError):
            effective_max_calls = PTC_DEFAULT_MAX_CALLS

    if allowed_tools is None:
        effective_allowed = PTC_DEFAULT_ALLOWED_TOOLS
    else:
        effective_allowed = (
            frozenset(str(name).strip() for name in allowed_tools if isinstance(name, str) and name.strip())
            - {"ptc_exec"}
        )

    try:
        argv = build_ptc_subprocess_argv(ws_path, sandbox_mode=sandbox_mode)
    except HostToolError as exc:
        return _result(ok=False, error=str(exc))

    env = build_scrubbed_env(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(ws_path),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_PTC_MAX_RPC_LINE_BYTES,
        )
    except OSError as exc:
        return _result(ok=False, error=f"无法启动 PTC 沙箱子进程：{exc}")

    # start_new_session=True 保证子进程作为新进程组 Leader，故 pgid 恒等于 proc.pid
    pgid = proc.pid
    stderr_task: asyncio.Task[str] | None = asyncio.create_task(_drain_stderr(proc.stderr))
    deadline = t0 + effective_timeout

    done_received = False
    done_ok = False
    done_stdout = ""
    done_stdout_truncated = False
    done_error: str | None = None
    timed_out = False
    cancelled = False
    rejected_calls = 0

    try:
        assert proc.stdin is not None and proc.stdout is not None

        # 发送初始化握手帧，传递用户脚本与子进程内存输出缓冲上限
        init_frame = {
            "type": "exec",
            "code": code,
            "max_output_chars": PTC_MAX_OUTPUT_CHARS + 256,
        }
        try:
            init_status, _ = await _await_with_deadline_and_cancel(
                _send_rpc_frame(proc.stdin, init_frame),
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            init_status = "error"
            done_error = f"向 PTC 子进程写入脚本握手失败：{exc}"

        if init_status == "timeout":
            timed_out = True
        elif init_status == "cancelled":
            cancelled = True
        elif init_status == "ok":
            while True:
                try:
                    read_status, raw_line = await _await_with_deadline_and_cancel(
                        _read_rpc_line(proc.stdout, _PTC_MAX_RPC_LINE_BYTES),
                        deadline=deadline,
                        cancel_event=cancel_event,
                    )
                except _RpcProtocolError as exc:
                    done_error = str(exc)
                    break

                if read_status == "timeout":
                    timed_out = True
                    break
                if read_status == "cancelled":
                    cancelled = True
                    break

                if not raw_line:
                    # 子进程在发送 done 帧前关闭了 stdout（例如异常崩溃或 os._exit）
                    break

                try:
                    frame = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    done_error = f"PTC JSON-RPC 帧解析失败：{exc}"
                    break

                if not isinstance(frame, dict):
                    done_error = "PTC JSON-RPC 帧必须是 JSON 对象。"
                    break

                msg_type = frame.get("type")
                if msg_type == "done":
                    done_received = True
                    done_ok = bool(frame.get("ok", False))
                    raw_out = frame.get("stdout", "")
                    done_stdout = raw_out if isinstance(raw_out, str) else str(raw_out)
                    done_stdout_truncated = bool(frame.get("stdout_truncated", False))
                    raw_err = frame.get("error")
                    done_error = None if raw_err is None else str(raw_err)
                    break

                if msg_type != "call":
                    done_error = f"PTC 收到未知 JSON-RPC 帧类型：{msg_type!r}"
                    break

                req_id = frame.get("id")
                raw_name = frame.get("name")
                raw_args = frame.get("arguments")
                tool_name = raw_name.strip() if isinstance(raw_name, str) else ""

                if not tool_name:
                    rejected_calls += 1
                    reply_payload: Any = {
                        "ok": False,
                        "code": "ptc_tool_not_allowed",
                        "error": "工具名称不能为空。",
                    }
                elif tool_name not in effective_allowed:
                    rejected_calls += 1
                    reply_payload = {
                        "ok": False,
                        "code": "ptc_tool_not_allowed",
                        "tool": tool_name,
                        "error": f"工具 '{tool_name}' 不在 PTC 允许的只读工具白名单内，已被拒绝。",
                    }
                elif len(subcalls) >= effective_max_calls:
                    rejected_calls += 1
                    reply_payload = {
                        "ok": False,
                        "code": "ptc_max_calls_exceeded",
                        "tool": tool_name,
                        "max_calls": effective_max_calls,
                        "error": f"已达到单次 PTC 脚本最大工具调用次数上限 ({effective_max_calls})。",
                    }
                elif raw_args is not None and not isinstance(raw_args, dict):
                    rejected_calls += 1
                    reply_payload = {
                        "ok": False,
                        "code": "ptc_invalid_arguments",
                        "tool": tool_name,
                        "error": "工具调用参数 arguments 必须是 JSON 对象。",
                    }
                else:
                    tool_args = dict(raw_args) if isinstance(raw_args, dict) else {}
                    call_t0 = time.monotonic()
                    call_status, call_val = await _await_with_deadline_and_cancel(
                        _invoke_dispatch_safe(dispatch_fn, tool_name, tool_args),
                        deadline=deadline,
                        cancel_event=cancel_event,
                    )
                    if call_status == "timeout":
                        timed_out = True
                        break
                    if call_status == "cancelled":
                        cancelled = True
                        break
                    subcall_ok, reply_payload = call_val
                    subcall_dur_ms = int(round((time.monotonic() - call_t0) * 1000.0))
                    subcalls.append(
                        {
                            "name": tool_name,
                            "ok": bool(subcall_ok),
                            "duration_ms": subcall_dur_ms,
                        }
                    )

                if rejected_calls > _PTC_MAX_REJECTED_CALLS:
                    done_error = (
                        f"PTC 脚本触发过多被拒绝的工具调用（超过 {_PTC_MAX_REJECTED_CALLS} 次），已中止执行。"
                    )
                    break

                reply_frame = {
                    "type": "result",
                    "id": req_id,
                    "result": reply_payload,
                }
                try:
                    send_status, _ = await _await_with_deadline_and_cancel(
                        _send_rpc_frame(proc.stdin, reply_frame),
                        deadline=deadline,
                        cancel_event=cancel_event,
                    )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    done_error = "PTC 子进程在接收工具返回结果前已关闭连接。"
                    break

                if send_status == "timeout":
                    timed_out = True
                    break
                if send_status == "cancelled":
                    cancelled = True
                    break

        # 正常收到 done 帧后关闭 stdin 并短暂等待子进程优雅退出，再进入 finally 兜底清场
        if done_received and not timed_out and not cancelled:
            if proc.stdin is not None and not proc.stdin.is_closing():
                with contextlib.suppress(OSError):
                    proc.stdin.close()
            if proc.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError, OSError):
                    await asyncio.wait_for(proc.wait(), timeout=0.3)
    finally:
        # 无论正常退出、超时、取消或任务被外部 CancelledError 中断，均强制回收整个进程组与 stderr 排水协程
        try:
            await _reap_process_group(proc, pgid)
        except BaseException:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            raise

    stderr_text = ""
    if stderr_task is not None:
        try:
            stderr_text = await asyncio.wait_for(stderr_task, timeout=0.5)
        except asyncio.TimeoutError:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
        except Exception:
            stderr_text = ""

    if cancelled:
        return _result(ok=False, stdout=done_stdout, error="PTC 脚本执行已取消。")
    if timed_out:
        return _result(
            ok=False,
            stdout=done_stdout,
            error=f"PTC 脚本执行超时（超过 {effective_timeout:g}s 墙钟上限），已强制回收进程组。",
        )
    if done_received:
        return _result(
            ok=done_ok,
            stdout=done_stdout,
            stdout_truncated=done_stdout_truncated,
            error=done_error,
        )

    rc = proc.returncode
    detail = done_error or f"PTC 子进程在完成协议握手前异常退出（exit_code={rc}）"
    stderr_clean = stderr_text.strip()
    if stderr_clean:
        detail = f"{detail} | stderr: {stderr_clean[:1000]}"
    return _result(ok=False, stdout=done_stdout, error=detail)


__all__ = [
    "PTC_DEFAULT_ALLOWED_TOOLS",
    "PTC_DEFAULT_MAX_CALLS",
    "PTC_DEFAULT_TIMEOUT_SECONDS",
    "PTC_MAX_CODE_CHARS",
    "PTC_MAX_OUTPUT_CHARS",
    "PTC_MAX_SUBCALLS",
    "PTC_MAX_TIMEOUT_SECONDS",
    "PTC_TOOL_SPECS",
    "build_ptc_subprocess_argv",
    "execute_ptc_script",
    "filter_ptc_allowed_tools",
]

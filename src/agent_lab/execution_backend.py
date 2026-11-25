"""Anthropic Managed Agents 风格 Brain/Hands 解耦执行后端抽象：本地 OS 沙箱、持久化 Docker 会话容器与远端 MicroVM JSON-RPC 驱动。

================================================================================
模块导读（Module Guide）
================================================================================
1. 架构角色（Brain / Hands Decoupling）
   受 Anthropic Managed Agents 架构启发，本模块将 Agent 控制面（Brain：提示词编排、上下文管理、
   权限与 DLP 裁决、LLM 凭据托管）与工作区执行面（Hands：文件读写编辑与终端命令执行）彻底解耦
   在统一的异步协议 `WorkspaceExecutionBackend` 之后，支持按安全隔离级别透明切换三类执行后端：
   - `LocalHostExecutionBackend`（`isolation_level="host_kernel"`）：
     复用 `host_tools.py` 的五阶路径防御（`resolve_host_workspace_path`）、四级局部编辑匹配
     （`match_and_replace_tiered`）、POSIX `mkstemp + fsync + os.replace` 原子写盘，以及基于
     `HostKernelSandbox`（macOS `sandbox-exec` / Linux `bwrap`）与 `build_scrubbed_env()` 脱敏环境的
     命令执行；
   - `DockerSessionBackend`（`isolation_level="container"`）：
     按会话惰性拉起持久化隔离容器（`docker run -d --network=none --cap-drop=ALL
     --security-opt=no-new-privileges --read-only --tmpfs /tmp:rw,size=64m
     -v <ws>:/workspace:rw`），后续全部命令通过 `docker exec` 在容器内执行，天然与宿主机 LLM
     凭据进程空间彻底隔离，并在 `aclose()` 中自动强制销毁容器；
   - `RemoteMicroVMBackend`（`isolation_level="microvm"`）：
     面向 Firecracker / Kata Containers / E2B / Daytona 等远端微虚拟机执行节点的标准 JSON-RPC 2.0
     HTTP 异步驱动，内置严格 SSRF / 云元数据 IMDS 校验、Bearer 令牌鉴权、W3C `traceparent` 链路透传，
     并支持 `httpx.AsyncClient(transport=...)` 注入以实现确定性离线测试。

2. 核心安全不变量（Core Security Invariants）
   - 凭据单向隔离不变量：宿主 LLM API 密钥与服务端令牌绝不注入子进程、Docker 容器或被回显在
     `snapshot_info()` / `__repr__()` / 错误消息中；
   - 路径防逃逸与密钥禁区不变量：本地、容器与远端路径均先经词法/五阶安全校验，阻断 `..` 穿越、
     符号链接、多重硬链接、`.git` 目录及 `.env*` / 私钥等敏感文件；
   - 远端端点 SSRF 零绕过不变量：`RemoteMicroVMBackend` 仅允许 `http(s)` 协议，硬拒绝 URL 内嵌
     凭据、HTTP 重定向、链路本地地址（含 `169.254.169.254`、`::ffff:169.254.169.254`、`fd00:ec2::254`
     及云元数据主机名）与未经显式授权的私网/回环目标。
================================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import ipaddress
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import time
from typing import Any, Literal, Protocol, runtime_checkable
import urllib.parse
import uuid

from .host_tools import (
    _HOST_MAX_FILE_BYTES,
    HostKernelSandbox,
    HostToolError,
    _atomic_write_host_file,
    is_host_secret_segment,
    match_and_replace_tiered,
    resolve_host_workspace_path,
    run_host_bash_command,
)
from .observability import format_traceparent, get_trace_id, redact_secrets
from .permissions import build_scrubbed_env

IsolationLevel = Literal["host_kernel", "container", "microvm"]

_MAX_STREAM_BYTES = 32 * 1024
_DEFAULT_EXEC_TIMEOUT = 30.0
_MAX_EXEC_TIMEOUT = 300.0

_FORBIDDEN_DOT_DIRS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".kube", ".agent-lab"})
_DOCKER_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
_DOCKER_MEMORY_RE = re.compile(r"^[1-9][0-9]*[bBkKmMgG]?$")
_DOCKER_CPUS_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_ALIBABA_IMDS_NET = ipaddress.ip_network("100.100.100.200/32")
_AWS_IPV6_IMDS_NET = ipaddress.ip_network("fd00:ec2::254/128")
_NAT64_WELL_KNOWN_NET = ipaddress.ip_network("64:ff9b::/96")
_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "metadata.azure.internal",
        "metadata.aws.internal",
        "169.254.169.254",
        "fd00:ec2::254",
    }
)


class ExecutionBackendError(ValueError):
    """执行后端结构化错误：继承 `ValueError` 并携带机器可读错误码 `code`。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@runtime_checkable
class WorkspaceExecutionBackend(Protocol):
    """Brain / Hands 解耦工作区执行后端异步协议。"""

    backend_kind: str
    isolation_level: IsolationLevel

    async def read_file(self, rel_path: str) -> dict[str, Any]:
        """读取工作区内 UTF-8 文本文件内容与校验摘要。"""
        ...

    async def write_file(self, rel_path: str, content: str) -> dict[str, Any]:
        """创建或原子覆写工作区内 UTF-8 文本文件。"""
        ...

    async def edit_file(self, rel_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        """对工作区内文件执行确定性局部编辑并返回差异摘要。"""
        ...

    async def exec_command(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """在受隔离的执行环境中运行终端命令并返回结构化输出与退出码。"""
        ...

    async def snapshot_info(self) -> dict[str, Any]:
        """返回当前执行后端的隔离级别、资源配置与运行时状态快照（不含任何凭据）。"""
        ...

    async def aclose(self) -> None:
        """幂等释放后端持有的容器、连接池或临时资源。"""
        ...


# ---------------------------------------------------------------------------
# 共享本地工作区文件操作辅助函数（复用 host_tools 五阶路径校验与原子写盘）
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_timeout_seconds(
    value: float,
    *,
    min_value: float,
    max_value: float,
) -> float:
    if isinstance(value, bool):
        raise ExecutionBackendError("invalid_timeout", "timeout_seconds 必须为有限正数。")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionBackendError("invalid_timeout", "timeout_seconds 必须为有限正数。") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ExecutionBackendError("invalid_timeout", "timeout_seconds 必须为有限正数。")
    return max(min_value, min(max_value, parsed))


def _read_workspace_file_sync(
    workspace: Path,
    rel_path: str,
    *,
    backend_kind: str,
    isolation_level: IsolationLevel,
) -> dict[str, Any]:
    rel, target = resolve_host_workspace_path(workspace, rel_path, allow_missing=False)
    st = target.stat()
    if st.st_size > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc
    return {
        "ok": True,
        "path": rel,
        "content": content,
        "sha256": _sha256_text(content),
        "size_bytes": len(content.encode("utf-8")),
        "total_lines": len(content.splitlines()),
        "backend": backend_kind,
        "isolation_level": isolation_level,
    }


def _write_workspace_file_sync(
    workspace: Path,
    rel_path: str,
    content: str,
    *,
    backend_kind: str,
    isolation_level: IsolationLevel,
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise HostToolError("invalid_content", "写入内容必须是 UTF-8 字符串。")
    encoded = content.encode("utf-8")
    if len(encoded) > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    rel, target = resolve_host_workspace_path(workspace, rel_path, allow_missing=True)
    existed = target.exists()
    _atomic_write_host_file(target, content)
    return {
        "ok": True,
        "path": rel,
        "action": "modify" if existed else "create",
        "sha256": _sha256_text(content),
        "bytes_written": len(encoded),
        "backend": backend_kind,
        "isolation_level": isolation_level,
    }


def _edit_workspace_file_sync(
    workspace: Path,
    rel_path: str,
    old_text: str,
    new_text: str,
    *,
    backend_kind: str,
    isolation_level: IsolationLevel,
) -> dict[str, Any]:
    rel, target = resolve_host_workspace_path(workspace, rel_path, allow_missing=False)
    if old_text == new_text:
        return {
            "ok": False,
            "code": "empty_patch",
            "path": rel,
            "match_tier": "none",
            "replacements": 0,
            "error": "old_text 与 new_text 完全相同，未产生修改。",
            "backend": backend_kind,
            "isolation_level": isolation_level,
        }
    if target.stat().st_size > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"目标文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc

    updated, match_tier, replacements, err_code = match_and_replace_tiered(original, old_text, new_text)
    if updated is None or err_code is not None:
        if err_code == "ambiguous_match":
            err_msg = f"old_text 在文件 {rel} 中出现了 {replacements} 次，请补充更多上下文使其唯一。"
        else:
            err_msg = f"old_text 在文件 {rel} 中未找到，请先读取最新内容核对。"
        return {
            "ok": False,
            "code": err_code or "old_text_not_found",
            "path": rel,
            "match_tier": match_tier,
            "replacements": replacements,
            "error": err_msg,
            "backend": backend_kind,
            "isolation_level": isolation_level,
        }

    encoded_new = updated.encode("utf-8")
    if len(encoded_new) > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"编辑后文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")

    _atomic_write_host_file(target, updated)
    diff_text = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    return {
        "ok": True,
        "path": rel,
        "match_tier": match_tier,
        "replacements": replacements,
        "sha256": _sha256_text(updated),
        "diff": diff_text[:4000],
        "backend": backend_kind,
        "isolation_level": isolation_level,
    }


def _validate_remote_rel_path(raw_path: str, *, allow_root: bool = False) -> str:
    """对发往远端 MicroVM 的相对路径执行纯词法纵深审计，阻断目录穿越、绝对路径与密钥文件访问。"""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path or "\\" in raw_path:
        raise ExecutionBackendError("unsafe_path", "路径必须是非空的 POSIX 相对路径。")
    cleaned = raw_path.strip()
    if cleaned in {".", "./"}:
        if not allow_root:
            raise ExecutionBackendError("unsafe_path", "目标需要是具体文件路径而非工作区根目录。")
        return "."
    if ":" in cleaned or cleaned.startswith("/"):
        raise ExecutionBackendError("unsafe_path", "远端执行后端仅允许工作区相对 POSIX 路径。")
    pure = PurePosixPath(cleaned)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ExecutionBackendError("unsafe_path", "拒绝上级目录跳转或非法相对路径。")
    if any(part in _FORBIDDEN_DOT_DIRS for part in pure.parts):
        raise ExecutionBackendError("unsafe_path", "禁止读写 .git 或系统密钥隐藏目录。")
    if any(is_host_secret_segment(part) for part in pure.parts):
        raise ExecutionBackendError("secret_access_forbidden", "禁止读取或修改敏感凭证与密钥文件。")
    return pure.as_posix()


# ---------------------------------------------------------------------------
# 1. LocalHostExecutionBackend（OS 内核沙箱 + 脱敏子进程环境）
# ---------------------------------------------------------------------------


class LocalHostExecutionBackend:
    """本地宿主工作区执行后端：基于 `host_tools` 的 OS 内核沙箱与五阶路径保护。"""

    backend_kind: str = "local"
    isolation_level: IsolationLevel = "host_kernel"

    def __init__(
        self,
        workspace: Path | str,
        *,
        sandbox_mode: str | None = None,
        allow_network: bool = False,
    ) -> None:
        ws = Path(workspace).resolve()
        if not ws.is_dir():
            raise ExecutionBackendError("invalid_workspace", f"工作区目录不存在：{ws}")
        if sandbox_mode is not None:
            HostKernelSandbox.normalize_mode(sandbox_mode)
        self.workspace: Path = ws
        self._sandbox_mode: str | None = sandbox_mode
        self._allow_network: bool = bool(allow_network)
        self._closed: bool = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ExecutionBackendError("backend_closed", "LocalHostExecutionBackend 已关闭。")

    async def __aenter__(self) -> LocalHostExecutionBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def read_file(self, rel_path: str) -> dict[str, Any]:
        self._ensure_open()
        return await asyncio.to_thread(
            _read_workspace_file_sync,
            self.workspace,
            rel_path,
            backend_kind=self.backend_kind,
            isolation_level=self.isolation_level,
        )

    async def write_file(self, rel_path: str, content: str) -> dict[str, Any]:
        self._ensure_open()
        return await asyncio.to_thread(
            _write_workspace_file_sync,
            self.workspace,
            rel_path,
            content,
            backend_kind=self.backend_kind,
            isolation_level=self.isolation_level,
        )

    async def edit_file(self, rel_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        self._ensure_open()
        return await asyncio.to_thread(
            _edit_workspace_file_sync,
            self.workspace,
            rel_path,
            old_text,
            new_text,
            backend_kind=self.backend_kind,
            isolation_level=self.isolation_level,
        )

    async def exec_command(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: float = _DEFAULT_EXEC_TIMEOUT,
    ) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(command, str) or not command.strip():
            raise ExecutionBackendError("invalid_command", "执行命令不能为空。")
        eff_timeout = int(
            math.ceil(_normalize_timeout_seconds(timeout_seconds, min_value=1.0, max_value=120.0))
        )
        res = await asyncio.to_thread(
            run_host_bash_command,
            self.workspace,
            command,
            cwd=cwd,
            timeout_seconds=eff_timeout,
            allow_network=self._allow_network,
            sandbox_mode=self._sandbox_mode,
        )
        res["backend"] = self.backend_kind
        res["isolation_level"] = self.isolation_level
        return res

    async def snapshot_info(self) -> dict[str, Any]:
        kernel_backend = await asyncio.to_thread(HostKernelSandbox.detect_backend)
        return {
            "ok": True,
            "backend_kind": self.backend_kind,
            "isolation_level": self.isolation_level,
            "workspace": str(self.workspace),
            "workspace_exists": self.workspace.is_dir(),
            "kernel_sandbox_backend": kernel_backend,
            "kernel_sandbox_mode": HostKernelSandbox.normalize_mode(self._sandbox_mode),
            "allow_network": self._allow_network,
            "closed": self._closed,
        }

    async def aclose(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# 2. DockerSessionBackend（按会话持久化容器 + 零宿主凭据 docker exec）
# ---------------------------------------------------------------------------


async def _drain_async_stream(
    stream: asyncio.StreamReader | None,
    *,
    max_bytes: int = _MAX_STREAM_BYTES,
) -> tuple[bytes, bool]:
    """持续抽干异步子进程管道直至 EOF，超过字节预算后截断保留并继续消费以防 OS 管道写满死锁。"""
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        remaining = max_bytes - len(buf)
        if remaining > 0:
            buf.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        else:
            truncated = True
    return bytes(buf), truncated


class DockerSessionBackend:
    """持久化会话级 Docker 容器执行后端（按需惰性启动，`docker exec` 执行，`aclose()` 清理）。"""

    backend_kind: str = "docker"
    isolation_level: IsolationLevel = "container"

    def __init__(
        self,
        workspace: Path | str,
        *,
        image: str = "python:3.12-slim",
        memory_limit: str = "512m",
        cpus: str = "1.0",
        pids_limit: int = 128,
        docker_bin: str | None = None,
        session_id: str | None = None,
    ) -> None:
        ws = Path(workspace).resolve()
        if not ws.is_dir():
            raise ExecutionBackendError("invalid_workspace", f"工作区目录不存在：{ws}")
        ws_str = str(ws)
        if any(ch in ws_str for ch in (":", "\x00", "\n", "\r")):
            raise ExecutionBackendError(
                "unsafe_workspace_path",
                "Docker 挂载路径禁止包含冒号或控制字符。",
            )
        clean_image = str(image or "").strip()
        if not _DOCKER_IMAGE_RE.fullmatch(clean_image):
            raise ExecutionBackendError("invalid_docker_image", f"非法 Docker 镜像名：{image!r}")
        clean_mem = str(memory_limit or "").strip()
        if not _DOCKER_MEMORY_RE.fullmatch(clean_mem):
            raise ExecutionBackendError("invalid_memory_limit", f"非法内存配额：{memory_limit!r}")
        clean_cpus = str(cpus or "").strip()
        if not _DOCKER_CPUS_RE.fullmatch(clean_cpus) or not (0.0 < float(clean_cpus) <= 128.0):
            raise ExecutionBackendError("invalid_cpus", f"非法 CPU 配额：{cpus!r}")
        if isinstance(pids_limit, bool) or not (16 <= int(pids_limit) <= 4096):
            raise ExecutionBackendError("invalid_pids_limit", f"pids_limit 必须在 16~4096 之间：{pids_limit!r}")

        raw_sid = session_id if session_id is not None else f"agentlab-{uuid.uuid4().hex[:12]}"
        clean_sid = str(raw_sid).strip()
        if not _SAFE_SESSION_ID_RE.fullmatch(clean_sid):
            raise ExecutionBackendError("invalid_session_id", f"非法 session_id：{session_id!r}")

        self.workspace: Path = ws
        self.image: str = clean_image
        self.memory_limit: str = clean_mem
        self.cpus: str = clean_cpus
        self.pids_limit: int = int(pids_limit)
        self.session_id: str = clean_sid
        self._docker_bin_override: str | None = docker_bin
        self._container_name: str = f"agent-lab-session-{clean_sid[:40]}"
        self._container_id: str | None = None
        self._lock = asyncio.Lock()
        self._closed: bool = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ExecutionBackendError("backend_closed", "DockerSessionBackend 已关闭。")

    def _resolve_docker_bin(self) -> str:
        resolved = self._docker_bin_override or shutil.which("docker")
        if not resolved:
            raise ExecutionBackendError("docker_unavailable", "未找到 docker 可执行文件，无法启动容器后端。")
        return resolved

    def build_run_argv(self, docker_bin: str = "docker") -> list[str]:
        """构建高加固 `docker run -d` 参数列表（无网、降全权、只读根文件系统、受限 tmpfs 与配额）。"""
        return [
            docker_bin,
            "run",
            "-d",
            "--init",
            "--name",
            self._container_name,
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpus}",
            f"--pids-limit={self.pids_limit}",
            "--label",
            "agent_lab.managed=1",
            "--label",
            f"agent_lab.session={self.session_id}",
            "-v",
            f"{self.workspace}:/workspace:rw",
            "-w",
            "/workspace",
            self.image,
            "sh",
            "-c",
            "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done",
        ]

    def build_exec_argv(
        self,
        container_id: str,
        command: str,
        *,
        rel_cwd: str = ".",
        docker_bin: str = "docker",
    ) -> list[str]:
        """构建 `docker exec` 参数列表（不注入任何宿主机 LLM/服务凭据环境变量）。"""
        container_cwd = "/workspace" if rel_cwd in {"", "."} else f"/workspace/{rel_cwd}"
        return [
            docker_bin,
            "exec",
            "-w",
            container_cwd,
            "-e",
            "GIT_TERMINAL_PROMPT=0",
            "-e",
            "PAGER=cat",
            "-e",
            "GIT_PAGER=cat",
            "-e",
            "PYTHONUNBUFFERED=1",
            container_id,
            "/bin/sh",
            "-c",
            command,
        ]

    async def _ensure_container(self) -> str:
        async with self._lock:
            self._ensure_open()
            if self._container_id is not None:
                return self._container_id
            docker_bin = self._resolve_docker_bin()
            argv = self.build_run_argv(docker_bin)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=build_scrubbed_env(),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            except asyncio.TimeoutError as exc:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(OSError):
                    await proc.wait()
                raise ExecutionBackendError("docker_start_timeout", "启动会话 Docker 容器超时。") from exc
            except BaseException:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(OSError):
                    await proc.wait()
                raise

            if proc.returncode != 0:
                err_msg = redact_secrets(stderr_b.decode("utf-8", errors="replace").strip())
                raise ExecutionBackendError(
                    "docker_start_failed",
                    err_msg or f"docker run 失败（exit_code={proc.returncode}）。",
                )
            lines = [ln.strip() for ln in stdout_b.decode("utf-8", errors="replace").splitlines() if ln.strip()]
            if not lines or lines[0].startswith("-"):
                raise ExecutionBackendError("docker_start_failed", "docker run 未返回有效容器 ID。")
            self._container_id = lines[0]
            return self._container_id

    async def __aenter__(self) -> DockerSessionBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def read_file(self, rel_path: str) -> dict[str, Any]:
        self._ensure_open()
        return await asyncio.to_thread(
            _read_workspace_file_sync,
            self.workspace,
            rel_path,
            backend_kind=self.backend_kind,
            isolation_level=self.isolation_level,
        )

    async def write_file(self, rel_path: str, content: str) -> dict[str, Any]:
        self._ensure_open()
        return await asyncio.to_thread(
            _write_workspace_file_sync,
            self.workspace,
            rel_path,
            content,
            backend_kind=self.backend_kind,
            isolation_level=self.isolation_level,
        )

    async def edit_file(self, rel_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        self._ensure_open()
        return await asyncio.to_thread(
            _edit_workspace_file_sync,
            self.workspace,
            rel_path,
            old_text,
            new_text,
            backend_kind=self.backend_kind,
            isolation_level=self.isolation_level,
        )

    async def exec_command(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: float = _DEFAULT_EXEC_TIMEOUT,
    ) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(command, str) or not command.strip():
            raise ExecutionBackendError("invalid_command", "执行命令不能为空。")
        rel_cwd, resolved_cwd = await asyncio.to_thread(
            resolve_host_workspace_path, self.workspace, cwd or ".", allow_directory=True
        )
        if not resolved_cwd.is_dir():
            raise HostToolError("unsafe_path", f"命令工作目录不是有效目录：{rel_cwd}")

        eff_timeout = _normalize_timeout_seconds(
            timeout_seconds, min_value=0.5, max_value=_MAX_EXEC_TIMEOUT
        )
        container_id = await self._ensure_container()
        docker_bin = self._resolve_docker_bin()
        argv = self.build_exec_argv(container_id, command, rel_cwd=rel_cwd, docker_bin=docker_bin)

        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_scrubbed_env(),
        )
        out_task = asyncio.create_task(_drain_async_stream(proc.stdout, max_bytes=_MAX_STREAM_BYTES))
        err_task = asyncio.create_task(_drain_async_stream(proc.stderr, max_bytes=_MAX_STREAM_BYTES))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=eff_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(OSError):
                await proc.wait()
        except BaseException:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(OSError):
                await proc.wait()
            out_task.cancel()
            err_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(out_task, err_task, return_exceptions=True)
            raise

        if timed_out:
            try:
                (stdout_b, out_trunc), (stderr_b, err_trunc) = await asyncio.wait_for(
                    asyncio.gather(out_task, err_task), timeout=2.0
                )
            except asyncio.TimeoutError:
                out_task.cancel()
                err_task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.gather(out_task, err_task, return_exceptions=True)
                stdout_b, out_trunc = b"", True
                stderr_b, err_trunc = b"", True
        else:
            stdout_b, out_trunc = await out_task
            stderr_b, err_trunc = await err_task
        duration_ms = round((time.monotonic() - t0) * 1000.0, 2)
        exit_code = proc.returncode if proc.returncode is not None else -9
        stdout_text = redact_secrets(stdout_b.decode("utf-8", errors="replace"))
        stderr_text = redact_secrets(stderr_b.decode("utf-8", errors="replace"))

        if timed_out:
            ok = False
            code = "timeout"
        elif exit_code == 0:
            ok = True
            code = "ok"
        else:
            ok = False
            code = "nonzero_exit"

        return {
            "ok": ok,
            "code": code,
            "command": command,
            "cwd": rel_cwd,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "cancelled": False,
            "duration_ms": duration_ms,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": out_trunc,
            "stderr_truncated": err_trunc,
            "container_id": container_id[:12],
            "image": self.image,
            "backend": self.backend_kind,
            "isolation_level": self.isolation_level,
        }

    async def snapshot_info(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend_kind": self.backend_kind,
            "isolation_level": self.isolation_level,
            "workspace": str(self.workspace),
            "image": self.image,
            "memory_limit": self.memory_limit,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "session_id": self.session_id,
            "container_name": self._container_name,
            "container_id": self._container_id,
            "container_started": self._container_id is not None,
            "network_mode": "none",
            "read_only_rootfs": True,
            "closed": self._closed,
        }

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            cid = self._container_id
            self._container_id = None
        if not cid:
            return
        docker_bin = self._docker_bin_override or shutil.which("docker")
        if not docker_bin:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                docker_bin,
                "rm",
                "-f",
                cid,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=build_scrubbed_env(),
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(OSError):
                    await proc.wait()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3. RemoteMicroVMBackend（Firecracker / Kata / E2B / Daytona JSON-RPC 驱动）
# ---------------------------------------------------------------------------


def _is_blocked_microvm_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private_network: bool,
) -> bool:
    """校验解析后的目标 IP：无条件阻断链路本地/IMDS/未指定/多播/保留地址，默认阻断私网与回环。"""
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _is_blocked_microvm_ip(ip_obj.ipv4_mapped, allow_private_network=allow_private_network)
        if ip_obj.sixtofour is not None:
            return _is_blocked_microvm_ip(ip_obj.sixtofour, allow_private_network=allow_private_network)
        if ip_obj.teredo is not None:
            return _is_blocked_microvm_ip(
                ip_obj.teredo[0], allow_private_network=allow_private_network
            ) or _is_blocked_microvm_ip(ip_obj.teredo[1], allow_private_network=allow_private_network)
        if ip_obj in _NAT64_WELL_KNOWN_NET:
            embedded_v4 = ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF)
            return _is_blocked_microvm_ip(embedded_v4, allow_private_network=allow_private_network)
        if ip_obj in _AWS_IPV6_IMDS_NET:
            return True
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _ALIBABA_IMDS_NET:
        return True
    # 无论是否放行私网，永远阻断链路本地（含 AWS/GCP/Azure 169.254.169.254 IMDS）、未指定、多播与保留网段
    if ip_obj.is_link_local or ip_obj.is_unspecified or ip_obj.is_multicast or ip_obj.is_reserved:
        return True
    if not allow_private_network:
        if ip_obj.is_loopback or ip_obj.is_private:
            return True
        if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NET:
            return True
    return False


def validate_microvm_endpoint_url(
    endpoint_url: str,
    *,
    allow_private_network: bool = False,
) -> str:
    """严格校验远端 MicroVM JSON-RPC 端点 URL（阻断非 http(s)、内嵌凭据、IMDS 元数据与未授权私网 SSRF）。"""
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise ExecutionBackendError("invalid_url", "远端 MicroVM endpoint_url 不能为空。")
    cleaned = endpoint_url.strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
        raise ExecutionBackendError("invalid_url", "endpoint_url 包含非法控制字符。")

    parsed = urllib.parse.urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ExecutionBackendError(
            "blocked_scheme",
            f"远端 MicroVM 仅支持 http/https 协议，已拒绝：{scheme!r}",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ExecutionBackendError("credentials_in_url", "endpoint_url 禁止内嵌用户名或密码凭据。")
    if parsed.fragment:
        raise ExecutionBackendError("invalid_url", "endpoint_url 不允许包含 URL 片段 (#fragment)。")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ExecutionBackendError("invalid_url", "endpoint_url 包含非法端口号。") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ExecutionBackendError("invalid_url", f"endpoint_url 端口越界：{port}")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ExecutionBackendError("invalid_url", "endpoint_url 缺少有效主机名。")
    if host in _BLOCKED_METADATA_HOSTS or (host.startswith("metadata.") and host.endswith(".internal")):
        raise ExecutionBackendError("ssrf_blocked", f"安全防护拒绝访问云元数据主机：{host}")

    is_localhost_name = host == "localhost" or host.endswith(".localhost")
    if is_localhost_name and not allow_private_network:
        raise ExecutionBackendError(
            "ssrf_blocked",
            "安全防护默认禁止访问回环地址（如需连接本机测试节点请显式传 allow_private_network=True）。",
        )

    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        # 拦截十进制/十六进制/八进制或简写 IPv4 混淆（如 2852039166 / 0xa9fea9fe / 127.1）绕过 IMDS/SSRF 校验
        try:
            ip_obj = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            ip_obj = None
    if ip_obj is not None and _is_blocked_microvm_ip(ip_obj, allow_private_network=allow_private_network):
        raise ExecutionBackendError("ssrf_blocked", f"安全防护拒绝受限 IP 地址：{host}")

    return cleaned


async def _validate_microvm_dns(endpoint_url: str, *, allow_private_network: bool) -> None:
    """在真实网络请求前解析域名全部 A/AAAA 记录并逐一核验，缓解 DNS Rebinding SSRF 攻击。"""
    parsed = urllib.parse.urlsplit(endpoint_url)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ExecutionBackendError("invalid_url", "endpoint_url 缺少有效主机名。")
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        if _is_blocked_microvm_ip(ip_obj, allow_private_network=allow_private_network):
            raise ExecutionBackendError("ssrf_blocked", f"安全防护拒绝受限 IP 地址：{host}")
        return

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ExecutionBackendError("dns_resolution_failed", f"无法解析远端 MicroVM 主机名：{host}") from exc
    if not infos:
        raise ExecutionBackendError("dns_resolution_failed", f"远端 MicroVM 主机名未返回任何 IP：{host}")
    for info in infos:
        raw_ip = str(info[4][0])
        try:
            resolved_ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise ExecutionBackendError(
                "ssrf_blocked",
                f"安全防护拦截：域名 {host} 返回了无法校验的地址记录。",
            ) from exc
        if _is_blocked_microvm_ip(resolved_ip, allow_private_network=allow_private_network):
            raise ExecutionBackendError(
                "ssrf_blocked",
                f"安全防护拦截：域名 {host} 解析至受限地址 {raw_ip}",
            )


class RemoteMicroVMBackend:
    """Firecracker / Kata / E2B / Daytona 兼容远端 MicroVM JSON-RPC 2.0 异步执行后端。"""

    backend_kind: str = "microvm"
    isolation_level: IsolationLevel = "microvm"

    def __init__(
        self,
        endpoint_url: str,
        *,
        api_token: str | None = None,
        allow_private_network: bool = False,
        transport: Any = None,
        timeout: float = 60.0,
        session_id: str | None = None,
    ) -> None:
        validated_url = validate_microvm_endpoint_url(
            endpoint_url,
            allow_private_network=bool(allow_private_network),
        )
        eff_client_timeout = _normalize_timeout_seconds(timeout, min_value=0.1, max_value=3600.0)
        raw_sid = session_id if session_id is not None else f"vm-{uuid.uuid4().hex[:12]}"
        clean_sid = str(raw_sid).strip()
        if not _SAFE_SESSION_ID_RE.fullmatch(clean_sid):
            raise ExecutionBackendError("invalid_session_id", f"非法 session_id：{session_id!r}")

        self.endpoint_url: str = validated_url
        token_val = api_token if api_token is not None else os.environ.get("AGENT_LAB_MICROVM_TOKEN")
        self._api_token: str | None = str(token_val).strip() if token_val and str(token_val).strip() else None
        self._allow_private_network: bool = bool(allow_private_network)
        self._transport: Any = transport
        self._timeout: float = eff_client_timeout
        self.session_id: str = clean_sid
        self._client: Any = None
        self._rpc_seq: int = 0
        self._closed: bool = False

    def __repr__(self) -> str:
        return (
            f"RemoteMicroVMBackend(endpoint_url={self.endpoint_url!r}, "
            f"session_id={self.session_id!r}, auth_configured={bool(self._api_token)})"
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise ExecutionBackendError("backend_closed", "RemoteMicroVMBackend 已关闭。")

    def _get_client(self) -> Any:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    async def _rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        import httpx

        self._ensure_open()
        # 仅对真实网络传输执行实时 DNS 复核，避免拦截基于 MockTransport 的离线确定性测试
        if self._transport is None:
            await _validate_microvm_dns(
                self.endpoint_url,
                allow_private_network=self._allow_private_network,
            )

        self._rpc_seq += 1
        req_id = f"{self.session_id}-{self._rpc_seq}"
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "traceparent": format_traceparent(get_trace_id()),
            "X-Session-Id": self.session_id,
        }
        if self._api_token is not None:
            headers["Authorization"] = f"Bearer {self._api_token}"

        client = self._get_client()
        eff_timeout = self._timeout if timeout is None else max(1.0, float(timeout))
        try:
            response = await client.post(
                self.endpoint_url,
                json=payload,
                headers=headers,
                timeout=eff_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ExecutionBackendError(
                "remote_timeout",
                f"远端 MicroVM RPC 调用 {method} 超时（{eff_timeout}s）。",
            ) from exc
        except httpx.HTTPError as exc:
            raise ExecutionBackendError(
                "remote_transport_error",
                f"远端 MicroVM RPC 传输失败：{type(exc).__name__}",
            ) from exc

        if 300 <= response.status_code < 400:
            raise ExecutionBackendError(
                "remote_redirect_blocked",
                f"安全防护拒绝远端 MicroVM HTTP 重定向（status={response.status_code}）。",
            )
        if response.status_code >= 400:
            raise ExecutionBackendError(
                f"remote_http_{response.status_code}",
                f"远端 MicroVM RPC 返回错误状态码 HTTP {response.status_code}。",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ExecutionBackendError("remote_invalid_json", "远端 MicroVM 响应不是合法 JSON。") from exc
        if not isinstance(body, dict):
            raise ExecutionBackendError("remote_invalid_payload", "远端 MicroVM JSON-RPC 响应必须是对象。")

        rpc_error = body.get("error")
        if rpc_error is not None:
            if isinstance(rpc_error, dict):
                err_code = rpc_error.get("code", "rpc_error")
                err_msg = redact_secrets(str(rpc_error.get("message") or "remote rpc error"))
            else:
                err_code = "rpc_error"
                err_msg = redact_secrets(str(rpc_error))
            raise ExecutionBackendError("remote_rpc_error", f"[{err_code}] {err_msg}")

        result = body.get("result")
        if not isinstance(result, dict):
            raise ExecutionBackendError("remote_invalid_result", "远端 MicroVM JSON-RPC result 必须是字典对象。")

        normalized = dict(result)
        normalized.setdefault("ok", True)
        normalized.setdefault("backend", self.backend_kind)
        normalized.setdefault("isolation_level", self.isolation_level)
        return normalized

    async def __aenter__(self) -> RemoteMicroVMBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def read_file(self, rel_path: str) -> dict[str, Any]:
        norm_path = _validate_remote_rel_path(rel_path, allow_root=False)
        return await self._rpc_call("workspace.read_file", {"path": norm_path})

    async def write_file(self, rel_path: str, content: str) -> dict[str, Any]:
        norm_path = _validate_remote_rel_path(rel_path, allow_root=False)
        if not isinstance(content, str):
            raise ExecutionBackendError("invalid_content", "写入内容必须是 UTF-8 字符串。")
        if len(content.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
            raise ExecutionBackendError(
                "file_too_large",
                f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。",
            )
        return await self._rpc_call("workspace.write_file", {"path": norm_path, "content": content})

    async def edit_file(self, rel_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        norm_path = _validate_remote_rel_path(rel_path, allow_root=False)
        return await self._rpc_call(
            "workspace.edit_file",
            {"path": norm_path, "old_text": old_text, "new_text": new_text},
        )

    async def exec_command(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: float = _DEFAULT_EXEC_TIMEOUT,
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ExecutionBackendError("invalid_command", "执行命令不能为空。")
        norm_cwd = _validate_remote_rel_path(cwd or ".", allow_root=True)
        eff_timeout = _normalize_timeout_seconds(
            timeout_seconds, min_value=0.5, max_value=_MAX_EXEC_TIMEOUT
        )
        return await self._rpc_call(
            "workspace.exec_command",
            {
                "command": command,
                "cwd": norm_cwd,
                "timeout_seconds": eff_timeout,
            },
            timeout=max(self._timeout, eff_timeout + 5.0),
        )

    async def snapshot_info(self) -> dict[str, Any]:
        remote_info = await self._rpc_call("workspace.snapshot_info", {})
        remote_info.setdefault("backend_kind", self.backend_kind)
        remote_info.setdefault("isolation_level", self.isolation_level)
        remote_info.setdefault("endpoint_url", self.endpoint_url)
        remote_info.setdefault("session_id", self.session_id)
        remote_info.setdefault("auth_configured", bool(self._api_token))
        return remote_info

    async def aclose(self) -> None:
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()


# ---------------------------------------------------------------------------
# 4. 统一工厂入口（build_execution_backend）
# ---------------------------------------------------------------------------


def build_execution_backend(
    kind: str | None = None,
    workspace: Path | str | None = None,
    **kwargs: Any,
) -> WorkspaceExecutionBackend:
    """根据显式 `kind` 或环境变量 `AGENT_LAB_EXECUTION_BACKEND`（默认 `local`）构建执行后端。"""
    raw_kind = os.environ.get("AGENT_LAB_EXECUTION_BACKEND", "local") if kind is None else kind
    norm = str(raw_kind or "local").strip().lower()

    if norm in {"local", "host", "host_kernel"}:
        if workspace is None:
            raise ExecutionBackendError("missing_workspace", "local 执行后端必须提供 workspace 目录。")
        return LocalHostExecutionBackend(Path(workspace), **kwargs)

    if norm in {"docker", "container", "docker_session"}:
        if workspace is None:
            raise ExecutionBackendError("missing_workspace", "docker 执行后端必须提供 workspace 目录。")
        return DockerSessionBackend(Path(workspace), **kwargs)

    if norm in {"microvm", "remote", "remote_microvm", "firecracker", "kata", "e2b", "daytona"}:
        endpoint_url = kwargs.pop("endpoint_url", None) or os.environ.get("AGENT_LAB_MICROVM_ENDPOINT", "")
        if not endpoint_url:
            raise ExecutionBackendError(
                "missing_endpoint_url",
                "microvm 执行后端必须通过 endpoint_url 参数或 AGENT_LAB_MICROVM_ENDPOINT 提供端点地址。",
            )
        return RemoteMicroVMBackend(str(endpoint_url), **kwargs)

    raise ExecutionBackendError(
        "invalid_backend_kind",
        f"不支持的执行后端类型：{norm!r}（可选：local / docker / microvm）。",
    )


__all__ = [
    "DockerSessionBackend",
    "ExecutionBackendError",
    "IsolationLevel",
    "LocalHostExecutionBackend",
    "RemoteMicroVMBackend",
    "WorkspaceExecutionBackend",
    "build_execution_backend",
    "validate_microvm_endpoint_url",
]

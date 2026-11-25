"""Cursor 风格影子工作区管理器（ShadowWorkspaceManager）：写前推测性隔离验证引擎。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块实现受 Cursor Shadow Workspace 架构启发的写前推测性验证层（Speculative
   Pre-Mutation Verification）：在 Agent 正式改动用户真实工作区（`live_workspace`）的
   任何一个字节之前，将代码仓安全克隆至隔离的临时影子工作区（`shadow_dir`），依次应用
   候选编辑集合（`write` / `edit` / `sketch_edit` / `delete`），并执行三层自动检验：
   - Python AST 语法解析校验（`ast.parse`，精确汇报 `file` / `line` / `col` / `message`）；
   - 可选的影子 LSP 静态语义诊断（`LspClientManager(shadow_dir).get_file_diagnostics`）；
   - 可选的只读验证命令执行（经 `classify_bash_command` 准入后在 `HostKernelSandbox`
     无网沙箱 + `build_scrubbed_env()` 脱敏环境下针对 `shadow_dir` 运行）。
   全部校验完成后在 `finally` 中无条件销毁影子工作区与影子 LSP 进程，并通过前后
   SHA-256 指纹严格核验 `live_workspace` 毫发无损（`live_workspace_untouched=True`）。

2. 核心安全与工程不变量（Core Security & Engineering Invariants）
   - 零密钥克隆不变量（Zero-Secret Shadow Cloning）：`_clone_workspace_to_shadow` 遍历
     工作区时原地剪枝 `.git` / `.venv` / `venv` / `node_modules` / `__pycache__` /
     `dist` / `build` / `.agent-lab` 等目录，并严格过滤 `.env*`、`*.pem`、`*.key`、
     `credentials*`、`secrets*`、`id_rsa*` 等一切敏感凭证文件，绝不将任何密钥复制入影子区。
   - 软硬链接与特殊文件防逃逸不变量：克隆阶段对每个目录项与文件先执行 `os.lstat`，拒绝
     符号链接（`S_ISLNK`）、非常规文件（FIFO/Socket/设备节点）及多重硬链接（`st_nlink != 1`）；
     应用编辑阶段复用 `resolve_host_workspace_path` 五阶路径校验防线。
   - APFS Copy-on-Write 高速克隆与透明降级：在 macOS（Darwin）上优先调用原生 `/bin/cp -c -p`
     按目录批量执行 APFS 写时复制克隆（零数据块拷贝、独立 inode），当跨卷或非 APFS 文件
     系统不支持时自动无缝回退至 `shutil.copy2`。
   - 真实工作区零污染 SHA-256 守恒不变量：验证前后对 `live_workspace` 计算 SHA-256 基准
     快照并逐项比对，确保无论编辑成功、语法报错、LSP 异常还是验证命令超时，用户真实工作树
     均 100% 保持原样。
   - 资源零泄漏不变量：`shadow_dir` 目录与影子 `LspClientManager` 进程组、沙箱校验命令进程组
     全部受 `try / finally` 与 `os.killpg` 保护，异常与取消路径均保证彻底清场。
================================================================================
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Sequence
import contextlib
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

from .host_tools import (
    _HOST_MAX_FILE_BYTES,
    HostKernelSandbox,
    HostToolError,
    _atomic_write_host_file,
    apply_sketch_edit,
    is_host_secret_segment,
    match_and_replace_tiered,
    resolve_host_workspace_path,
)
from .lsp_client import LspClientManager
from .observability import redact_secrets
from .permissions import build_scrubbed_env, classify_bash_command

# 影子克隆阶段始终剪枝跳过的目录：版本控制、虚拟环境、依赖包、构建产物、缓存与敏感隐藏目录
_SHADOW_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".agent-lab",
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
    }
)

# 在 host_tools.is_host_secret_segment 基础上补充拦截常见凭证配置文件与证书库
_EXTRA_SECRET_FILENAMES = frozenset({".netrc", ".pypirc", ".npmrc"})
_EXTRA_SECRET_PREFIXES = ("id_ecdsa", "id_dsa")
_EXTRA_SECRET_SUFFIXES = (".jks", ".keystore")

_SUPPORTED_EDIT_ACTIONS = frozenset({"write", "edit", "sketch_edit", "delete"})
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})

_SHADOW_MAX_EDITS = 64
_SHADOW_MAX_CLONE_FILES = 20_000
_SHADOW_MAX_CLONE_FILE_BYTES = 16 * 1024 * 1024  # 单文件 16 MiB 上限，避免大二进制产物拖慢克隆
_SHADOW_CP_BATCH_SIZE = 128
_SHADOW_MAX_STREAM_BYTES = 32 * 1024  # 32 KiB 校验命令单流捕获上限
_SHADOW_MAX_DIFF_CHARS = 32_768
_SHADOW_HASH_BUDGET_FILES = 2000
_SHADOW_HASH_BUDGET_BYTES = 32 * 1024 * 1024


def _is_shadow_secret_name(name: str) -> bool:
    """判定单个路径段（目录名或文件名）是否属于敏感凭证，严禁进入影子克隆。"""
    if is_host_secret_segment(name):
        return True
    lower = name.casefold()
    if lower in _EXTRA_SECRET_FILENAMES:
        return True
    if lower.startswith(_EXTRA_SECRET_PREFIXES):
        return True
    return any(lower.endswith(sfx) for sfx in _EXTRA_SECRET_SUFFIXES)


def _is_shadow_secret_rel_path(rel_path: str) -> bool:
    return any(_is_shadow_secret_name(part) for part in PurePosixPath(rel_path).parts)


def _safe_rmtree(path: Path) -> None:
    """带只读权限自愈的递归目录清理器，兼容 Python 3.11+。"""
    if not path.exists() and not path.is_symlink():
        return

    def _fix_and_retry(func: Any, target_path: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            parent = os.path.dirname(target_path)
            if parent:
                os.chmod(parent, 0o700)
            if not os.path.islink(target_path):
                os.chmod(target_path, 0o700)
        with contextlib.suppress(OSError):
            func(target_path)

    if sys.version_info >= (3, 12):
        with contextlib.suppress(OSError):
            shutil.rmtree(path, onexc=_fix_and_retry)
    else:
        with contextlib.suppress(OSError):
            shutil.rmtree(path, onerror=_fix_and_retry)


def _sha256_file(path: Path) -> str:
    """以流式分块方式计算常规文件的 SHA-256 十六进制摘要。"""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _snapshot_live_workspace_hashes(
    live_root: Path,
    target_rel_paths: Sequence[str],
) -> dict[str, str | None]:
    """采集真实工作区的文件 SHA-256 指纹快照，用于事后零污染守恒核验。

    所有目标编辑路径无论仓库规模大小均强制计算精确内容 SHA-256（缺失文件记录为 None）；
    其余常规文件在安全预算内计算内容 SHA-256（超出预算时退化为 size+mtime_ns 元数据指纹），
    确保连同工作区内的敏感文件与兄弟文件在内的任何意外改动都能被确定性检出。
    """
    snapshot: dict[str, str | None] = {}
    hashed_files = 0
    hashed_bytes = 0

    for rel in target_rel_paths:
        target = live_root / rel
        try:
            st = os.lstat(target)
        except OSError:
            snapshot[rel] = None
            continue
        if stat.S_ISREG(st.st_mode):
            snapshot[rel] = _sha256_file(target)
        else:
            snapshot[rel] = f"special:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}"

    for dirpath, dirnames, filenames in os.walk(live_root, topdown=True, followlinks=False):
        cur_dir = Path(dirpath)
        dirnames.sort()
        kept_dirs: list[str] = []
        for dname in dirnames:
            if dname in _SHADOW_SKIP_DIRS:
                continue
            cand = cur_dir / dname
            try:
                st = os.lstat(cand)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                continue
            kept_dirs.append(dname)
        dirnames[:] = kept_dirs

        rel_dir = cur_dir.relative_to(live_root)
        filenames.sort()
        for fname in filenames:
            rel_posix = (rel_dir / fname).as_posix() if rel_dir != Path(".") else fname
            if rel_posix in snapshot:
                continue
            src = cur_dir / fname
            try:
                st = os.lstat(src)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            if (
                hashed_files < _SHADOW_HASH_BUDGET_FILES
                and hashed_bytes + st.st_size <= _SHADOW_HASH_BUDGET_BYTES
            ):
                snapshot[rel_posix] = _sha256_file(src)
                hashed_files += 1
                hashed_bytes += st.st_size
            else:
                snapshot[rel_posix] = f"stat:{st.st_size}:{st.st_mtime_ns}"

    return snapshot


def _copy_regular_file_fallback(src_file: Path, dst_file: Path) -> None:
    """安全回退复制单个常规文件（复制前再次 lstat 校验非软链、常规文件且单硬链）。"""
    try:
        st = os.lstat(src_file)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        return
    try:
        shutil.copy2(src_file, dst_file, follow_symlinks=False)
    except FileNotFoundError:
        return
    if dst_file.is_symlink():
        with contextlib.suppress(OSError):
            dst_file.unlink()


def _copy_file_batch(
    batch: list[tuple[Path, Path]],
    dst_dir: Path,
    *,
    cp_bin: str | None,
    use_apfs_cow: bool,
) -> bool:
    """将单个目录内的安全常规文件批次克隆到 dst_dir，返回后续是否继续尝试 APFS CoW。"""
    if not batch:
        return use_apfs_cow

    if use_apfs_cow and cp_bin:
        if len(batch) == 1:
            src_one, dst_one = batch[0]
            cmd = [cp_bin, "-c", "-p", "--", str(src_one), str(dst_one)]
        else:
            cmd = [cp_bin, "-c", "-p", "--", *[str(src) for src, _ in batch], str(dst_dir)]
        try:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30.0,
                check=False,
            )
            if res.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass

        # APFS CoW 失败（如跨卷 EXDEV 或非 APFS 分区）：回退至 shutil.copy2 补齐未复制文件
        use_apfs_cow = False
        for src_file, dst_file in batch:
            if not dst_file.exists() and not dst_file.is_symlink():
                _copy_regular_file_fallback(src_file, dst_file)
        return use_apfs_cow

    for src_file, dst_file in batch:
        _copy_regular_file_fallback(src_file, dst_file)
    return False


def _sandbox_backend_label(argv: list[str]) -> str:
    if not argv:
        return "off"
    exe = Path(argv[0]).name
    if exe == "sandbox-exec":
        return "macos-sandbox-exec"
    if exe == "bwrap":
        return "linux-bwrap"
    return "off"


async def _drain_async_stream(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> tuple[bytes, bool]:
    """持续抽干异步子进程输出管道直至 EOF（超出预算截断内存缓冲但永不阻塞写端）。"""
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            if len(buf) < limit:
                take = min(len(chunk), limit - len(buf))
                buf.extend(chunk[:take])
                if take < len(chunk):
                    truncated = True
            else:
                truncated = True
    except (asyncio.CancelledError, OSError):
        pass
    return bytes(buf), truncated


async def _terminate_async_process_group(proc: asyncio.subprocess.Process) -> None:
    """两阶段终止子进程组（SIGTERM -> 短暂宽限 -> SIGKILL），防止孤儿进程占住管道。"""
    pgid = proc.pid
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(proc.wait(), timeout=0.4)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.0)


def _check_python_syntax(rel_path: str, file_path: Path) -> dict[str, Any] | None:
    """使用 `ast.parse` 校验单个 Python 文件的语法，报错时返回含精确行号的诊断条目。"""
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            "file": rel_path,
            "line": 1,
            "col": 1,
            "message": f"无法以 UTF-8 读取 Python 文件：{exc}",
        }
    try:
        ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        return {
            "file": rel_path,
            "line": int(exc.lineno) if exc.lineno is not None else 1,
            "col": int(exc.offset) if exc.offset is not None else 1,
            "message": str(exc.msg or exc),
            "text": (exc.text or "").rstrip("\r\n"),
        }
    except ValueError as exc:
        return {
            "file": rel_path,
            "line": 1,
            "col": 1,
            "message": str(exc),
        }
    return None


def _build_unified_diff(
    rel_path: str,
    before_text: str,
    after_text: str,
    *,
    existed_before: bool,
    exists_after: bool,
) -> str:
    if not existed_before and not exists_after:
        return ""
    fromfile = f"a/{rel_path}" if existed_before else "/dev/null"
    tofile = f"b/{rel_path}" if exists_after else "/dev/null"
    diff_str = "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )
    if len(diff_str) > _SHADOW_MAX_DIFF_CHARS:
        return diff_str[:_SHADOW_MAX_DIFF_CHARS] + "\n... [diff truncated]\n"
    return diff_str


class ShadowWorkspaceManager:
    """Cursor 风格影子工作区管理器：在不触碰真实工作区的前提下验证候选代码变更。"""

    def __init__(
        self,
        *,
        shadow_base_dir: Path | str | None = None,
        sandbox_mode: str | None = None,
        lsp_server_overrides: dict[str, list[str]] | None = None,
    ) -> None:
        self.shadow_base_dir: Path | None = (
            Path(shadow_base_dir).expanduser().resolve() if shadow_base_dir is not None else None
        )
        self.sandbox_mode = sandbox_mode
        self.lsp_server_overrides = (
            {str(k): list(v) for k, v in lsp_server_overrides.items()}
            if lsp_server_overrides
            else None
        )

    def _resolve_shadow_root(self) -> Path:
        if self.shadow_base_dir is not None:
            return self.shadow_base_dir
        return (Path(tempfile.gettempdir()) / "agent-lab-shadow").resolve()

    @staticmethod
    def _clone_workspace_to_shadow(live_workspace: Path, shadow_root: Path) -> str:
        """将 live_workspace 安全克隆到 shadow_root 下的隔离临时目录并返回目录路径字符串。

        行为与安全保证：
        - 跳过 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`、`dist`、`build`、
          `.agent-lab` 等非源码或敏感隐藏目录；
        - 严格跳过 `.env*`、`*.pem`、`*.key`、`credentials*`、`secrets*` 等所有密钥凭证文件；
        - 逐项 `os.lstat` 拒绝符号链接（`S_ISLNK`）、非常规文件及多重硬链接（`st_nlink != 1`）；
        - 在 Darwin（macOS）上优先使用 APFS Copy-on-Write (`cp -c -p`) 复制常规文件，
          不支持时透明降级为 `shutil.copy2`。
        """
        live_path = Path(live_workspace)
        if live_path.is_symlink():
            raise HostToolError("unsafe_path", "拒绝将符号链接目录作为真实工作区克隆。")
        if not live_path.exists() or not live_path.is_dir():
            raise HostToolError("not_found", f"真实工作区目录不存在：{live_workspace}")
        live_root = live_path.resolve()

        shadow_root_path = Path(shadow_root)
        if shadow_root_path.is_symlink():
            raise HostToolError("unsafe_path", "shadow_root 不能是符号链接。")
        shadow_root_path.mkdir(parents=True, exist_ok=True)
        shadow_root_resolved = shadow_root_path.resolve()

        shadow_dir = Path(tempfile.mkdtemp(prefix="shadow-", dir=str(shadow_root_resolved))).resolve()
        with contextlib.suppress(OSError):
            os.chmod(shadow_dir, 0o700)

        is_darwin = sys.platform == "darwin"
        cp_bin: str | None = None
        if is_darwin:
            cp_bin = "/bin/cp" if os.path.exists("/bin/cp") else shutil.which("cp")
        use_apfs_cow = bool(is_darwin and cp_bin)

        cloned_files = 0
        try:
            for dirpath, dirnames, filenames in os.walk(live_root, topdown=True, followlinks=False):
                cur_dir = Path(dirpath)
                rel_dir = cur_dir.relative_to(live_root)
                dst_dir = shadow_dir if rel_dir == Path(".") else (shadow_dir / rel_dir)
                dst_dir.mkdir(parents=True, exist_ok=True)

                # 原地剪枝：跳过构建/依赖/版本控制目录、密钥目录、软链接目录以及 shadow_root 自身防递归
                dirnames.sort()
                kept_dirs: list[str] = []
                for dname in dirnames:
                    if dname in _SHADOW_SKIP_DIRS or _is_shadow_secret_name(dname):
                        continue
                    cand_dir = cur_dir / dname
                    try:
                        st = os.lstat(cand_dir)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                        continue
                    with contextlib.suppress(OSError):
                        cand_resolved = cand_dir.resolve()
                        if cand_resolved in (shadow_root_resolved, shadow_dir):
                            continue
                    kept_dirs.append(dname)
                dirnames[:] = kept_dirs

                # 过滤当前目录常规文件：拒密钥、拒软链、拒非常规文件、拒多重硬链接
                filenames.sort()
                dir_batch: list[tuple[Path, Path]] = []
                for fname in filenames:
                    if cloned_files >= _SHADOW_MAX_CLONE_FILES:
                        break
                    if _is_shadow_secret_name(fname):
                        continue
                    src_file = cur_dir / fname
                    try:
                        st = os.lstat(src_file)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                        continue
                    if st.st_nlink != 1:
                        continue
                    if st.st_size > _SHADOW_MAX_CLONE_FILE_BYTES:
                        continue
                    dir_batch.append((src_file, dst_dir / fname))
                    cloned_files += 1
                    if len(dir_batch) >= _SHADOW_CP_BATCH_SIZE:
                        use_apfs_cow = _copy_file_batch(
                            dir_batch,
                            dst_dir,
                            cp_bin=cp_bin,
                            use_apfs_cow=use_apfs_cow,
                        )
                        dir_batch = []

                if dir_batch:
                    use_apfs_cow = _copy_file_batch(
                        dir_batch,
                        dst_dir,
                        cp_bin=cp_bin,
                        use_apfs_cow=use_apfs_cow,
                    )

                if cloned_files >= _SHADOW_MAX_CLONE_FILES:
                    dirnames[:] = []
                    break
        except BaseException:
            _safe_rmtree(shadow_dir)
            raise

        return str(shadow_dir)

    @staticmethod
    def _validate_edits(
        live_root: Path,
        edits: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """校验候选编辑条目结构与路径安全，返回 `(validated_edits, validation_errors)`。"""
        validated: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        if not isinstance(edits, (list, tuple)) or len(edits) == 0:
            errors.append(
                {
                    "index": -1,
                    "path": "",
                    "action": "",
                    "code": "invalid_edits",
                    "error": "edits 必须是非空的编辑动作列表。",
                }
            )
            return validated, errors

        if len(edits) > _SHADOW_MAX_EDITS:
            errors.append(
                {
                    "index": -1,
                    "path": "",
                    "action": "",
                    "code": "too_many_edits",
                    "error": f"单次影子校验最多支持 {_SHADOW_MAX_EDITS} 条编辑动作。",
                }
            )
            return validated, errors

        for idx, raw in enumerate(edits):
            if not isinstance(raw, dict):
                errors.append(
                    {
                        "index": idx,
                        "path": "",
                        "action": "",
                        "code": "invalid_edit_entry",
                        "error": "每条编辑项必须是字典对象。",
                    }
                )
                continue

            action = raw.get("action")
            raw_path = raw.get("path")
            if not isinstance(action, str) or action not in _SUPPORTED_EDIT_ACTIONS:
                errors.append(
                    {
                        "index": idx,
                        "path": str(raw_path or ""),
                        "action": str(action or ""),
                        "code": "invalid_action",
                        "error": f"action 必须是 {sorted(_SUPPORTED_EDIT_ACTIONS)} 之一，收到：{action!r}",
                    }
                )
                continue

            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(
                    {
                        "index": idx,
                        "path": "",
                        "action": action,
                        "code": "invalid_path",
                        "error": "每条编辑项必须提供非空字符串 path。",
                    }
                )
                continue

            try:
                rel_path, _ = resolve_host_workspace_path(live_root, raw_path, allow_missing=True)
                if _is_shadow_secret_rel_path(rel_path):
                    raise HostToolError(
                        "secret_access_forbidden",
                        "禁止在影子工作区中操作敏感凭证与密钥文件。",
                    )
            except HostToolError as exc:
                errors.append(
                    {
                        "index": idx,
                        "path": raw_path,
                        "action": action,
                        "code": exc.code,
                        "error": str(exc),
                    }
                )
                continue

            if action == "write":
                content = raw.get("content")
                if not isinstance(content, str):
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "missing_content",
                            "error": "action='write' 必须提供字符串字段 content。",
                        }
                    )
                    continue
                if len(content.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "file_too_large",
                            "error": f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。",
                        }
                    )
                    continue
                validated.append({"index": idx, "action": action, "path": rel_path, "content": content})

            elif action == "edit":
                old_text = raw.get("old_text")
                new_text = raw.get("new_text")
                replace_all = raw.get("replace_all", False)
                if not isinstance(old_text, str) or not old_text:
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "missing_old_text",
                            "error": "action='edit' 必须提供非空字符串 old_text。",
                        }
                    )
                    continue
                if not isinstance(new_text, str):
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "missing_new_text",
                            "error": "action='edit' 必须提供字符串 new_text。",
                        }
                    )
                    continue
                if not isinstance(replace_all, bool):
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "invalid_replace_all",
                            "error": "replace_all 必须是布尔值。",
                        }
                    )
                    continue
                if old_text == new_text:
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "empty_patch",
                            "error": "old_text 与 new_text 完全相同，未产生修改。",
                        }
                    )
                    continue
                validated.append(
                    {
                        "index": idx,
                        "action": action,
                        "path": rel_path,
                        "old_text": old_text,
                        "new_text": new_text,
                        "replace_all": replace_all,
                    }
                )

            elif action == "sketch_edit":
                sketch = raw.get("sketch")
                if not isinstance(sketch, str) or not sketch:
                    errors.append(
                        {
                            "index": idx,
                            "path": rel_path,
                            "action": action,
                            "code": "missing_sketch",
                            "error": "action='sketch_edit' 必须提供非空字符串 sketch。",
                        }
                    )
                    continue
                validated.append({"index": idx, "action": action, "path": rel_path, "sketch": sketch})

            else:  # delete
                validated.append({"index": idx, "action": "delete", "path": rel_path})

        return validated, errors

    @staticmethod
    def _apply_edits_in_shadow(
        shadow_dir: Path,
        validated_edits: Sequence[dict[str, Any]],
    ) -> tuple[list[str], set[str], dict[str, str], list[dict[str, Any]]]:
        """在影子工作区中顺序应用全部编辑，返回 `(touched_files, deleted_files, diffs, apply_errors)`。"""
        original_snapshots: dict[str, tuple[bool, str]] = {}
        touched_files: list[str] = []
        deleted_files: set[str] = set()
        apply_errors: list[dict[str, Any]] = []

        def _mark_touched(rel_p: str) -> None:
            if rel_p not in touched_files:
                touched_files.append(rel_p)

        def _read_shadow_utf8(target: Path) -> str:
            if target.stat().st_size > _HOST_MAX_FILE_BYTES:
                raise HostToolError(
                    "file_too_large",
                    f"目标文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。",
                )
            raw_bytes = target.read_bytes()
            try:
                return raw_bytes.decode("utf-8")
            except UnicodeError as exc:
                raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc

        for edit in validated_edits:
            idx = int(edit["index"])
            action = str(edit["action"])
            rel_path = str(edit["path"])
            try:
                if action == "write":
                    rel_p, target = resolve_host_workspace_path(shadow_dir, rel_path, allow_missing=True)
                    if rel_p not in original_snapshots:
                        if target.exists():
                            original_snapshots[rel_p] = (True, _read_shadow_utf8(target))
                        else:
                            original_snapshots[rel_p] = (False, "")
                    _atomic_write_host_file(target, str(edit["content"]))
                    deleted_files.discard(rel_p)
                    _mark_touched(rel_p)

                elif action == "edit":
                    rel_p, target = resolve_host_workspace_path(shadow_dir, rel_path, allow_missing=False)
                    current_text = _read_shadow_utf8(target)
                    if rel_p not in original_snapshots:
                        original_snapshots[rel_p] = (True, current_text)
                    new_text, _match_tier, cnt, err_code = match_and_replace_tiered(
                        current_text,
                        str(edit["old_text"]),
                        str(edit["new_text"]),
                        replace_all=bool(edit.get("replace_all", False)),
                    )
                    if err_code == "ambiguous_match":
                        raise HostToolError(
                            "ambiguous_match",
                            f"old_text 在文件 {rel_p} 中出现了 {cnt} 次；请补充更多上下文使其唯一，或显式传 replace_all=true。",
                        )
                    if err_code == "old_text_not_found" or new_text is None:
                        raise HostToolError(
                            "old_text_not_found",
                            f"old_text 在文件 {rel_p} 中未找到。",
                        )
                    if len(new_text.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
                        raise HostToolError(
                            "file_too_large",
                            f"编辑后文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。",
                        )
                    _atomic_write_host_file(target, new_text)
                    deleted_files.discard(rel_p)
                    _mark_touched(rel_p)

                elif action == "sketch_edit":
                    rel_p, target = resolve_host_workspace_path(shadow_dir, rel_path, allow_missing=False)
                    current_text = _read_shadow_utf8(target)
                    if rel_p not in original_snapshots:
                        original_snapshots[rel_p] = (True, current_text)
                    new_text, _merge_meta = apply_sketch_edit(current_text, str(edit["sketch"]))
                    if new_text == current_text:
                        raise HostToolError("empty_patch", "草图合并结果与原文件完全一致，未产生修改。")
                    if len(new_text.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
                        raise HostToolError(
                            "file_too_large",
                            f"编辑后文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。",
                        )
                    _atomic_write_host_file(target, new_text)
                    deleted_files.discard(rel_p)
                    _mark_touched(rel_p)

                else:  # delete
                    rel_p, target = resolve_host_workspace_path(shadow_dir, rel_path, allow_missing=False)
                    if rel_p not in original_snapshots:
                        try:
                            before_txt = _read_shadow_utf8(target)
                        except HostToolError:
                            before_txt = ""
                        original_snapshots[rel_p] = (True, before_txt)
                    target.unlink()
                    deleted_files.add(rel_p)
                    _mark_touched(rel_p)

            except HostToolError as exc:
                apply_errors.append(
                    {
                        "index": idx,
                        "path": rel_path,
                        "action": action,
                        "code": exc.code,
                        "error": str(exc),
                    }
                )
            except OSError as exc:
                apply_errors.append(
                    {
                        "index": idx,
                        "path": rel_path,
                        "action": action,
                        "code": "io_error",
                        "error": str(exc),
                    }
                )

        diffs: dict[str, str] = {}
        for rel_p in touched_files:
            existed_before, before_text = original_snapshots.get(rel_p, (False, ""))
            target = shadow_dir / rel_p
            exists_after = target.exists() and not target.is_symlink() and target.is_file()
            after_text = ""
            if exists_after:
                with contextlib.suppress(OSError, UnicodeError):
                    after_text = target.read_text(encoding="utf-8")
            diffs[rel_p] = _build_unified_diff(
                rel_p,
                before_text,
                after_text,
                existed_before=existed_before,
                exists_after=exists_after,
            )

        return touched_files, deleted_files, diffs, apply_errors

    async def _run_shadow_check_command(
        self,
        shadow_dir: Path,
        check_command: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """在 HostKernelSandbox（脱敏环境变量 + 禁用网络）中对影子工作区运行只读验证命令。"""
        cmd_str = str(check_command).strip()
        verdict = classify_bash_command(cmd_str)
        if verdict["classification"] != "safe_readonly":
            return {
                "ok": False,
                "code": "unsafe_check_command",
                "classification": verdict["classification"],
                "command": cmd_str,
                "exit_code": None,
                "timed_out": False,
                "duration_ms": 0.0,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "sandbox": "blocked",
                "error": f"影子校验仅允许只读验证命令（safe_readonly），已拒绝：{verdict['reason']}",
            }

        shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        try:
            argv = HostKernelSandbox.build_sandboxed_shell_argv(
                shell_bin,
                cmd_str,
                shadow_dir,
                allow_network=False,
                mode=self.sandbox_mode,
            )
        except HostToolError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "classification": "safe_readonly",
                "command": cmd_str,
                "exit_code": None,
                "timed_out": False,
                "duration_ms": 0.0,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "sandbox": "unavailable",
                "error": str(exc),
            }

        sandbox_backend = _sandbox_backend_label(argv)
        env = build_scrubbed_env()
        eff_timeout = max(0.5, min(60.0, float(timeout_seconds)))
        t0 = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(shadow_dir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out_task = asyncio.create_task(_drain_async_stream(proc.stdout, _SHADOW_MAX_STREAM_BYTES))
        err_task = asyncio.create_task(_drain_async_stream(proc.stderr, _SHADOW_MAX_STREAM_BYTES))

        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=eff_timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await _terminate_async_process_group(proc)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            out_task.cancel()
            err_task.cancel()
            raise
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)

        out_bytes, out_trunc = await out_task
        err_bytes, err_trunc = await err_task

        duration_ms = round((time.monotonic() - t0) * 1000.0, 2)
        exit_code = proc.returncode if proc.returncode is not None else -9
        stdout_text = redact_secrets(out_bytes.decode("utf-8", errors="replace"))
        stderr_text = redact_secrets(err_bytes.decode("utf-8", errors="replace"))

        if timed_out:
            code = "timeout"
            ok = False
        elif exit_code == 0:
            code = "ok"
            ok = True
        else:
            code = "nonzero_exit"
            ok = False

        return {
            "ok": ok,
            "code": code,
            "classification": "safe_readonly",
            "command": cmd_str,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": out_trunc,
            "stderr_truncated": err_trunc,
            "sandbox": sandbox_backend,
        }

    async def verify_speculative_edits(
        self,
        live_workspace: Path,
        edits: list[dict[str, Any]],
        *,
        run_lsp: bool = True,
        run_syntax_check: bool = True,
        check_command: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        """在隔离影子工作区中推测性应用并校验候选编辑集合，绝不修改真实工作区。

        执行流水线：
        1. 校验 `live_workspace` 与全部 `edits`（`write`/`edit`/`sketch_edit`/`delete`）的路径与字段；
        2. 采集 `live_workspace` 的 SHA-256 基准快照；
        3. 安全克隆至隔离临时影子工作区（绝不复制 `.env`/`*.pem` 等密钥与 `.git`/依赖目录）；
        4. 在影子工作区中按序应用全部编辑并生成每文件 Unified Diff；
        5. 对变更后的 `.py`/`.pyi` 文件执行 Python `ast.parse` 语法校验；
        6. 可选启动影子 `LspClientManager(shadow_dir)` 收集静态语义诊断并在 `finally` 中关闭；
        7. 可选在无网 `HostKernelSandbox` 中执行只读 `check_command`；
        8. 在 `finally` 中销毁影子目录，重新计算并比对 `live_workspace` SHA-256 快照。
        """
        eff_timeout = max(1.0, min(60.0, float(timeout_seconds)))
        deadline = time.monotonic() + eff_timeout

        live_path = Path(live_workspace)
        if live_path.is_symlink() or not live_path.exists() or not live_path.is_dir():
            return {
                "ok": False,
                "clean": False,
                "live_workspace_untouched": True,
                "files_checked": [],
                "apply_errors": [
                    {
                        "index": -1,
                        "path": str(live_workspace),
                        "action": "",
                        "code": "invalid_workspace",
                        "error": f"真实工作区路径无效或不是普通目录：{live_workspace}",
                    }
                ],
                "syntax_errors": [],
                "lsp_diagnostics": [],
                "check_result": None,
                "diffs": {},
            }

        live_root = live_path.resolve()
        validated_edits, validation_errors = self._validate_edits(live_root, edits)
        if validation_errors:
            return {
                "ok": False,
                "clean": False,
                "live_workspace_untouched": True,
                "files_checked": [],
                "apply_errors": validation_errors,
                "syntax_errors": [],
                "lsp_diagnostics": [],
                "check_result": None,
                "diffs": {},
            }

        target_rel_paths = list(dict.fromkeys(str(e["path"]) for e in validated_edits))
        before_hashes = await asyncio.to_thread(_snapshot_live_workspace_hashes, live_root, target_rel_paths)

        shadow_root = self._resolve_shadow_root()
        shadow_dir_str = await asyncio.to_thread(self._clone_workspace_to_shadow, live_root, shadow_root)
        shadow_dir = Path(shadow_dir_str)

        files_checked: list[str] = []
        apply_errors: list[dict[str, Any]] = []
        syntax_errors: list[dict[str, Any]] = []
        lsp_diagnostics: list[dict[str, Any]] = []
        check_result: dict[str, Any] | None = None
        diffs: dict[str, str] = {}

        try:
            touched_files, deleted_files, diffs, apply_errors = await asyncio.to_thread(
                self._apply_edits_in_shadow,
                shadow_dir,
                validated_edits,
            )
            files_checked = list(touched_files)
            existing_modified = [rel for rel in touched_files if rel not in deleted_files]

            # 1. Python AST 语法校验
            if run_syntax_check:
                for rel_p in existing_modified:
                    target = shadow_dir / rel_p
                    if target.suffix.lower() in _PYTHON_SUFFIXES and target.is_file():
                        err = _check_python_syntax(rel_p, target)
                        if err is not None:
                            syntax_errors.append(err)

            # 2. 可选影子 LSP 语义诊断（无本地语言服务器时零报错优雅降级）
            if run_lsp and existing_modified:
                remaining_for_lsp = deadline - time.monotonic()
                if remaining_for_lsp > 0.2:
                    lsp_manager = LspClientManager(
                        shadow_dir,
                        server_overrides=self.lsp_server_overrides,
                        init_timeout=min(10.0, max(1.0, remaining_for_lsp)),
                    )
                    try:
                        per_file_timeout = min(2.5, max(0.3, remaining_for_lsp / max(1, len(existing_modified))))
                        for rel_p in existing_modified:
                            rem = deadline - time.monotonic()
                            if rem <= 0.15:
                                break
                            try:
                                diags = await lsp_manager.get_file_diagnostics(
                                    rel_p,
                                    timeout=min(per_file_timeout, rem),
                                )
                                if diags:
                                    lsp_diagnostics.extend(diags)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                continue
                    finally:
                        await lsp_manager.close_all()

            # 3. 可选无网 OS 沙箱只读检查命令
            if check_command is not None and str(check_command).strip():
                rem_cmd = max(1.0, deadline - time.monotonic())
                check_result = await self._run_shadow_check_command(
                    shadow_dir,
                    str(check_command),
                    timeout_seconds=rem_cmd,
                )
        finally:
            await asyncio.to_thread(_safe_rmtree, shadow_dir)

        after_hashes = await asyncio.to_thread(_snapshot_live_workspace_hashes, live_root, target_rel_paths)
        live_workspace_untouched = before_hashes == after_hashes

        has_lsp_errors = any(
            d.get("severity") == "error" or d.get("severity_code") == 1 for d in lsp_diagnostics
        )
        check_ok = check_result is None or bool(check_result.get("ok"))
        ok = live_workspace_untouched and len(apply_errors) == 0
        clean = (
            ok
            and len(syntax_errors) == 0
            and not has_lsp_errors
            and check_ok
        )

        return {
            "ok": ok,
            "clean": clean,
            "live_workspace_untouched": live_workspace_untouched,
            "files_checked": files_checked,
            "apply_errors": apply_errors,
            "syntax_errors": syntax_errors,
            "lsp_diagnostics": lsp_diagnostics,
            "check_result": check_result,
            "diffs": diffs,
        }


DEFAULT_SHADOW_WORKSPACE_MANAGER = ShadowWorkspaceManager()


async def execute_shadow_verify_edits(
    live_workspace: Path,
    *,
    edits: list[dict[str, Any]],
    run_lsp: bool = True,
    run_syntax_check: bool = True,
    check_command: str | None = None,
    timeout_seconds: float = 15.0,
    manager: ShadowWorkspaceManager | None = None,
) -> dict[str, Any]:
    """工具调用入口：在隔离影子工作区中推测性验证一组候选代码编辑。"""
    mgr = manager or DEFAULT_SHADOW_WORKSPACE_MANAGER
    return await mgr.verify_speculative_edits(
        live_workspace,
        edits,
        run_lsp=run_lsp,
        run_syntax_check=run_syntax_check,
        check_command=check_command,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# 影子工作区工具规范（SHADOW_TOOL_SPECS）
# ---------------------------------------------------------------------------

SHADOW_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "shadow_verify_edits",
        "description": (
            "在隔离的写时复制（Copy-on-Write）影子工作区中推测性试应用一组候选文件变更"
            "（支持 write / edit / sketch_edit / delete），自动执行 Python AST 语法检查、"
            "可选 LSP 静态诊断与无网沙箱只读验证命令，并返回 Unified Diff 与诊断报告；"
            "全程绝不修改真实工作区的任何字节，适合在高风险重构落盘前预先验证。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "description": "按顺序在隔离影子工作区中试应用的候选文件变更列表。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["write", "edit", "sketch_edit", "delete"],
                                "description": (
                                    "编辑动作：write（全量写）、edit（四级锚点局部替换）、"
                                    "sketch_edit（草图省略拼接）或 delete（删除文件）。"
                                ),
                            },
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 320,
                                "description": "工作区相对路径。",
                            },
                            "content": {
                                "type": "string",
                                "maxLength": 524288,
                                "description": "action='write' 时的完整 UTF-8 文件内容。",
                            },
                            "old_text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 65536,
                                "description": "action='edit' 时待替换的原文片段。",
                            },
                            "new_text": {
                                "type": "string",
                                "maxLength": 65536,
                                "description": "action='edit' 时替换后的新文本。",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "action='edit' 时是否替换全部匹配项（默认 false）。",
                            },
                            "sketch": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 262144,
                                "description": "action='sketch_edit' 时含省略标记与前后锚点的编辑草图。",
                            },
                        },
                        "required": ["action", "path"],
                        "additionalProperties": False,
                    },
                },
                "run_lsp": {
                    "type": "boolean",
                    "description": "是否对影子工作区中变更文件执行 LSP 诊断（默认 true；未安装语言服务器时优雅降级）。",
                },
                "run_syntax_check": {
                    "type": "boolean",
                    "description": "是否对变更后的 Python 文件执行 ast.parse 语法检查（默认 true）。",
                },
                "check_command": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "可选：在影子工作区无网 OS 沙箱中执行的只读验证命令"
                        "（例如 `pytest -q`、`ruff check .`、`python -m py_compile ...`）。"
                    ),
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "影子校验总超时秒数（默认 15，最大 60）。",
                },
            },
            "required": ["edits"],
            "additionalProperties": False,
        },
    }
]

__all__ = [
    "DEFAULT_SHADOW_WORKSPACE_MANAGER",
    "SHADOW_TOOL_SPECS",
    "ShadowWorkspaceManager",
    "execute_shadow_verify_edits",
]

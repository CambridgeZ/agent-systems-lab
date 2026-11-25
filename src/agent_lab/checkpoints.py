"""轮次检查点与文件/对话双向时间旅行回退管理器（CheckpointManager）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   为 `host` / `computer` 直连工作区提供类似 Claude Code `/rewind` 的细粒度回滚能力：
   - 内容寻址（Content-Addressed）文件快照仓库：在 `<state_dir>/checkpoints/<run_id>/blobs/<sha256>`
     下按 SHA-256 去重原子落盘每次修改前的原始字节；
   - 轮次首改记录（First-Touch Snapshot）：每轮对同一路径的多次编辑仅保留该轮首次修改前的原始状态
     （或新建标记 `existed=False`），保证回退到轮次起点时 100% 精准还原；
   - 三模式回退（`scope="both" | "files" | "messages"`）：支持仅回退工作区文件、仅截断对话历史或
     文件与对话同步回退，并自动闭合悬空 `tool_use` 块与重置上下文压缩游标。

2. 安全与原子性不变量（Security & Atomicity Invariants）
   - 符号链接零容忍：对 `<state_dir>/checkpoints/...` 与目标工作区目录树的每一级路径段均逐层拦截
     符号链接与 `..` 目录穿越，并在读/验阶段配合 `O_NOFOLLOW` 防止 TOCTOU 替换攻击；
   - 原子落盘与完整性校验：所有 Blob 写入与文件恢复均走同目录 `mkstemp + fsync + os.replace`，
     读取 Blob 恢复前强制校验实际字节 SHA-256 与索引摘要一致；
   - 多轮逆序回放幂等性：恢复文件时从目标检查点起按时间逆序逐轮回放 pre-edit 状态（新建文件删除、
     已存在文件还原），确保跨多轮创建再修改的文件准确回到目标轮次前的真实状态。
================================================================================
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
import threading
from typing import Any

from .host_tools import (
    _FORBIDDEN_DOT_PARTS,
    HostToolError,
    is_host_secret_segment,
    resolve_host_workspace_path,
)
from .storage import Storage

VALID_REWIND_SCOPES = frozenset({"both", "files", "messages"})
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BLOB_BYTES = 16 * 1024 * 1024  # 单文件检查点快照上限 16 MiB


class CheckpointError(ValueError):
    """检查点记录与回退过程中的结构化业务或安全异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validate_run_id(run_id: str) -> str:
    clean = str(run_id or "").strip()
    if not _SAFE_RUN_ID_RE.fullmatch(clean):
        raise CheckpointError("invalid_run_id", f"非法的 run_id：{run_id!r}")
    return clean


def _ensure_safe_dir_chain(root: Path, *segments: str) -> Path:
    """在已解析的 root 目录下逐段安全建目录，拒绝任意层级出现符号链接或非目录节点。"""
    if root.is_symlink():
        raise CheckpointError("unsafe_path", "检查点根目录不能是符号链接。")
    root_resolved = root.resolve()
    if not root_resolved.exists():
        root_resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not root_resolved.is_dir():
        raise CheckpointError("unsafe_path", "检查点根目录不是有效目录。")
    cur = root_resolved
    for seg in segments:
        if not seg or seg in {".", ".."} or "/" in seg or "\\" in seg or "\x00" in seg:
            raise CheckpointError("unsafe_path", f"检查点路径包含非法目录段：{seg!r}")
        cur = cur / seg
        if cur.is_symlink():
            raise CheckpointError("unsafe_path", f"检查点存储目录包含符号链接：{seg}")
        if cur.exists() and not cur.is_dir():
            raise CheckpointError("unsafe_path", f"检查点存储路径不是目录：{seg}")
        cur.mkdir(mode=0o700, exist_ok=True)
        if cur.is_symlink() or not cur.resolve().is_relative_to(root_resolved):
            raise CheckpointError("unsafe_path", f"检查点存储目录越界：{seg}")
    return cur


def _validate_workspace_relpath_for_restore(workspace: Path, rel_path: str) -> tuple[str, Path]:
    """校验待恢复/删除的工作区相对路径，逐段阻断符号链接与路径穿越。"""
    if not isinstance(rel_path, str) or not rel_path.strip() or "\x00" in rel_path or "\\" in rel_path:
        raise CheckpointError("unsafe_path", f"检查点文件清单包含非法路径：{rel_path!r}")
    cleaned = rel_path.strip()
    if cleaned.startswith("/") or ":" in cleaned:
        raise CheckpointError("unsafe_path", f"检查点文件清单仅允许工作区相对路径：{rel_path!r}")
    pure = PurePosixPath(cleaned)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise CheckpointError("unsafe_path", f"检查点文件清单包含越界路径段：{rel_path!r}")
    if any(part in _FORBIDDEN_DOT_PARTS for part in pure.parts):
        raise CheckpointError("unsafe_path", f"禁止回退敏感隐藏目录路径：{rel_path!r}")
    if any(is_host_secret_segment(part) for part in pure.parts):
        raise CheckpointError("unsafe_path", f"禁止回退敏感凭证文件路径：{rel_path!r}")

    ws_raw = Path(workspace).absolute()
    if ws_raw.is_symlink() or not ws_raw.is_dir():
        raise CheckpointError("unsafe_path", "工作区目录无效或是符号链接。")
    ws_root = ws_raw.resolve()

    cur = ws_root
    for idx, part in enumerate(pure.parts):
        cur = cur / part
        if cur.is_symlink():
            raise CheckpointError("unsafe_path", f"工作区目标路径存在符号链接：{rel_path}")
        if idx < len(pure.parts) - 1 and cur.exists() and not cur.is_dir():
            raise CheckpointError("unsafe_path", f"工作区目标父节点不是目录：{rel_path}")
    resolved = cur.resolve()
    if resolved == ws_root or not resolved.is_relative_to(ws_root):
        raise CheckpointError("unsafe_path", f"回退目标越出工作区边界：{rel_path}")
    return pure.as_posix(), cur


def _open_safe_parent_dir_fd(
    workspace: Path,
    rel_posix: str,
    *,
    create_missing: bool,
) -> tuple[int, str] | None:
    """使用 `O_DIRECTORY | O_NOFOLLOW` 逐级安全打开目标父目录 FD，全程拒绝跟随任何符号链接。"""
    ws_raw = Path(workspace).absolute()
    if ws_raw.is_symlink() or not ws_raw.is_dir():
        raise CheckpointError("unsafe_path", "工作区目录无效或是符号链接。")
    ws_root = ws_raw.resolve()
    pure = PurePosixPath(rel_posix)
    if not pure.parts:
        raise CheckpointError("unsafe_path", f"检查点路径为空：{rel_posix!r}")

    dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        cur_fd = os.open(ws_root, dir_flags)
    except OSError as exc:
        raise CheckpointError("unsafe_path", f"无法安全打开工作区根目录：{exc}") from exc

    try:
        root_st = os.fstat(cur_fd)
        if not stat.S_ISDIR(root_st.st_mode):
            raise CheckpointError("unsafe_path", "工作区根目录不是有效目录。")
        for part in pure.parts[:-1]:
            try:
                next_fd = os.open(part, dir_flags, dir_fd=cur_fd)
            except FileNotFoundError:
                if not create_missing:
                    os.close(cur_fd)
                    return None
                try:
                    os.mkdir(part, mode=0o755, dir_fd=cur_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise CheckpointError("unsafe_path", f"无法安全创建工作区父目录：{rel_posix} ({exc})") from exc
                try:
                    next_fd = os.open(part, dir_flags, dir_fd=cur_fd)
                except OSError as exc:
                    raise CheckpointError("unsafe_path", f"工作区父目录存在符号链接或非目录节点：{rel_posix}") from exc
            except OSError as exc:
                raise CheckpointError("unsafe_path", f"工作区父目录存在符号链接或非目录节点：{rel_posix}") from exc
            os.close(cur_fd)
            cur_fd = next_fd
        return cur_fd, pure.parts[-1]
    except Exception:
        with contextlib.suppress(OSError):
            os.close(cur_fd)
        raise


def _safe_unlink_in_dirfd(parent_fd: int, leaf_name: str, norm_rel: str) -> bool:
    """在已验证非符号链接的父目录 FD 内安全删除常规单硬链接文件。"""
    try:
        st = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CheckpointError("unsafe_path", f"无法安全校验待删除文件 {norm_rel}：{exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise CheckpointError("unsafe_path", f"拒绝删除符号链接：{norm_rel}")
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise CheckpointError("unsafe_path", f"拒绝删除非常规或硬链接文件：{norm_rel}")
    try:
        os.unlink(leaf_name, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CheckpointError("unsafe_path", f"无法安全删除文件 {norm_rel}：{exc}") from exc
    return True


def _atomic_write_bytes_in_dirfd(
    parent_fd: int,
    leaf_name: str,
    data: bytes,
    *,
    mode: int | None = None,
    norm_rel: str = "",
) -> None:
    """在已验证非符号链接的父目录 FD 内执行 `O_NOFOLLOW` 校验 + 临时文件 + `fsync` + `fchmod` + `os.replace` 原子恢复。"""
    label = norm_rel or leaf_name
    existing_mode: int | None = None
    try:
        pre_st = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pre_st = None
    except OSError as exc:
        raise CheckpointError("unsafe_path", f"无法安全校验回退目标文件 {label}：{exc}") from exc

    if pre_st is not None:
        if stat.S_ISLNK(pre_st.st_mode):
            raise CheckpointError("unsafe_path", f"拒绝覆写符号链接目标：{label}")
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            check_fd = os.open(leaf_name, open_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise CheckpointError("unsafe_path", f"无法安全校验回退目标文件 {label}：{exc}") from exc
        try:
            st = os.fstat(check_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise CheckpointError("unsafe_path", f"回退目标必须是无硬链接的常规文件：{label}")
            existing_mode = st.st_mode & 0o777
        finally:
            os.close(check_fd)

    eff_mode = (
        (mode & 0o777)
        if mode is not None
        else (existing_mode if existing_mode is not None else (0o755 if leaf_name.endswith(".sh") else 0o644))
    )

    tmp_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    tmp_name = ""
    fd = -1
    for _ in range(16):
        cand_name = f".agent-rewind-{secrets.token_hex(8)}"
        try:
            fd = os.open(cand_name, tmp_flags, 0o600, dir_fd=parent_fd)
            tmp_name = cand_name
            break
        except FileExistsError:
            continue
    if fd < 0 or not tmp_name:
        raise CheckpointError("rewind_failed", f"无法在目标目录安全创建临时文件：{label}")

    replaced = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), eff_mode)
        # replace 前再次校验目标未被并发替换为符号链接
        with contextlib.suppress(FileNotFoundError):
            post_st = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(post_st.st_mode):
                raise CheckpointError("unsafe_path", f"拒绝覆写符号链接目标：{label}")
            if not stat.S_ISREG(post_st.st_mode) or post_st.st_nlink != 1:
                raise CheckpointError("unsafe_path", f"回退目标必须是无硬链接的常规文件：{label}")
        os.replace(tmp_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replaced = True
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not replaced and tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=parent_fd)


def _atomic_write_bytes(target: Path, data: bytes, *, mode: int = 0o644) -> None:
    """使用 mkstemp + fsync + chmod + os.replace 原子写入二进制文件（带 O_NOFOLLOW 安全校验）。"""
    if target.is_symlink():
        raise CheckpointError("unsafe_path", f"拒绝覆写符号链接目标：{target.name}")
    if target.exists():
        # 通过 O_NOFOLLOW 打开验证目标确为普通文件且无多重硬链接
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            check_fd = os.open(target, open_flags)
        except OSError as exc:
            raise CheckpointError("unsafe_path", f"无法安全校验回退目标文件：{exc}") from exc
        try:
            st = os.fstat(check_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise CheckpointError("unsafe_path", "回退目标必须是无硬链接的常规文件。")
            mode = st.st_mode & 0o777
        finally:
            os.close(check_fd)

    fd, tmp_name = tempfile.mkstemp(prefix=".agent-rewind-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def _heal_dangling_tool_uses(messages: list[dict[str, Any]]) -> bool:
    """若截断后的对话末尾悬空未闭合的 assistant.tool_use，自动补齐 1:1 合成 tool_result 闭合块。"""
    if not messages:
        return False
    healed = False
    # 1. 若末尾残留孤立 user.tool_result（缺少紧邻的 assistant.tool_use），将其安全弹出
    while messages:
        last = messages[-1]
        if (
            isinstance(last, dict)
            and last.get("role") == "user"
            and isinstance(last.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"])
        ):
            prev = messages[-2] if len(messages) >= 2 else None
            prev_uses = (
                {
                    b.get("id")
                    for b in (prev.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str)
                }
                if isinstance(prev, dict)
                and prev.get("role") == "assistant"
                and isinstance(prev.get("content"), list)
                else set()
            )
            res_ids = {
                b.get("tool_use_id")
                for b in last["content"]
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)
            }
            if not prev_uses or res_ids != prev_uses:
                messages.pop()
                healed = True
                continue
        break

    # 2. 若末尾是含 tool_use 的 assistant 消息，补齐 1:1 合成 tool_result 闭合协议
    if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
        content = messages[-1].get("content")
        if isinstance(content, list):
            orphan_uses = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str) and b.get("id")
            ]
            if orphan_uses:
                synth_blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": json.dumps(
                            {
                                "ok": False,
                                "code": "rewound_checkpoint",
                                "error": "会话已回退至检查点，未完成工具调用已自动闭合。",
                            },
                            ensure_ascii=False,
                        ),
                        "is_error": True,
                    }
                    for tu in orphan_uses
                ]
                messages.append({"role": "user", "content": synth_blocks})
                healed = True
    return healed


class CheckpointManager:
    """管理内容寻址的文件写前快照与轮次检查点回退（支持 both / files / messages）。"""

    def __init__(self, storage: Storage, state_dir: str | Path) -> None:
        self.storage = storage
        raw_state = Path(state_dir)
        if raw_state.is_symlink():
            raise CheckpointError("unsafe_path", "state_dir 不能是符号链接。")
        self.state_dir = raw_state.resolve()
        self._lock = threading.RLock()

    def _blobs_dir(self, run_id: str) -> Path:
        safe_run_id = _validate_run_id(run_id)
        return _ensure_safe_dir_chain(self.state_dir, "checkpoints", safe_run_id, "blobs")

    def _store_blob(self, run_id: str, data: bytes) -> str:
        if len(data) > _MAX_BLOB_BYTES:
            raise CheckpointError("blob_too_large", f"快照文件超过 {_MAX_BLOB_BYTES} 字节上限。")
        blobs_dir = self._blobs_dir(run_id)
        sha = hashlib.sha256(data).hexdigest()
        blob_path = blobs_dir / sha
        if blob_path.is_symlink():
            raise CheckpointError("unsafe_path", "检查点 Blob 路径不能是符号链接。")
        if blob_path.exists():
            st = os.lstat(blob_path)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise CheckpointError("unsafe_path", "检查点 Blob 文件类型或链接数异常。")
            return sha
        fd, tmp_name = tempfile.mkstemp(prefix=".blob-", dir=blobs_dir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, blob_path)
        finally:
            if os.path.exists(tmp_name):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
        return sha

    def _load_blob(self, run_id: str, sha256: str) -> bytes:
        clean_sha = str(sha256 or "").strip().lower()
        if not _SHA256_HEX_RE.fullmatch(clean_sha):
            raise CheckpointError("invalid_blob_sha", f"非法的 Blob SHA-256：{sha256!r}")
        blobs_dir = self._blobs_dir(run_id)
        blob_path = blobs_dir / clean_sha
        if blob_path.is_symlink():
            raise CheckpointError("unsafe_path", "检查点 Blob 路径不能是符号链接。")
        if not blob_path.resolve().is_relative_to(blobs_dir):
            raise CheckpointError("unsafe_path", "检查点 Blob 路径越界。")
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(blob_path, open_flags)
        except FileNotFoundError as exc:
            raise CheckpointError("blob_not_found", f"检查点快照 Blob 缺失：{clean_sha[:12]}") from exc
        except OSError as exc:
            raise CheckpointError("blob_read_failed", f"无法安全读取检查点 Blob：{exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise CheckpointError("unsafe_path", "检查点 Blob 必须是无硬链接的常规文件。")
            if st.st_size > _MAX_BLOB_BYTES:
                raise CheckpointError("blob_too_large", "检查点 Blob 大小超过安全上限。")
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                raw = fh.read(_MAX_BLOB_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > _MAX_BLOB_BYTES:
            raise CheckpointError("blob_too_large", "检查点 Blob 大小超过安全上限。")
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != clean_sha:
            raise CheckpointError("blob_corrupted", f"检查点 Blob 校验和不匹配：{clean_sha[:12]}")
        return raw

    def begin_turn_checkpoint(self, run: dict[str, Any], label: str = "") -> dict[str, Any]:
        """在执行用户轮次前创建新检查点，记录当前轮次编号、消息下标与空文件写前清单。"""
        run_id = _validate_run_id(str(run.get("id") or ""))
        meta = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        tenant_id = str(run.get("tenant_id") or meta.get("tenant_id") or "local").strip() or "local"
        messages = run.get("messages")
        message_idx = len(messages) if isinstance(messages, list) else 0
        with self._lock:
            existing = self.storage.list_checkpoints(run_id, tenant_id=tenant_id)
            turn_index = (int(existing[-1]["turn_index"]) + 1) if existing else 0
            return self.storage.save_checkpoint(
                run_id,
                turn_index=turn_index,
                message_idx=message_idx,
                tenant_id=tenant_id,
                label=str(label or "")[:200],
                file_manifest={},
            )

    def record_file_before_mutation(
        self,
        run_id: str,
        checkpoint_id: str,
        workspace: str | Path,
        path: str | Path,
        old_bytes: bytes | None,
        existed: bool,
    ) -> dict[str, Any]:
        """在文件被修改/新建前一瞬记录该轮首次写前快照（同路径本轮二次修改时保持首份不变）。"""
        safe_run_id = _validate_run_id(run_id)
        ws_raw = Path(workspace).absolute()
        if ws_raw.is_symlink() or not ws_raw.is_dir():
            raise CheckpointError("unsafe_path", "工作区目录无效或是符号链接。")
        try:
            rel_posix, target = resolve_host_workspace_path(
                ws_raw, str(path), allow_missing=True
            )
        except HostToolError as exc:
            raise CheckpointError(exc.code, str(exc)) from exc

        with self._lock:
            cp = self.storage.get_checkpoint(safe_run_id, checkpoint_id)
            if cp is None:
                raise CheckpointError("checkpoint_not_found", f"检查点不存在：{checkpoint_id}")
            manifest = dict(cp.get("file_manifest") or {})
            # 同一轮次内同一文件被连续编辑多次时，严格保留首次修改前的原始状态
            if rel_posix in manifest:
                return dict(manifest[rel_posix])

            if existed:
                raw_bytes = b"" if old_bytes is None else bytes(old_bytes)
                blob_sha = self._store_blob(safe_run_id, raw_bytes)
                mode = 0o644
                with contextlib.suppress(OSError):
                    if target.exists() and not target.is_symlink():
                        mode = target.stat().st_mode & 0o777
                entry: dict[str, Any] = {
                    "existed": True,
                    "sha256": blob_sha,
                    "size": len(raw_bytes),
                    "mode": mode,
                }
            else:
                entry = {
                    "existed": False,
                    "sha256": None,
                    "size": 0,
                    "mode": None,
                }

            manifest[rel_posix] = entry
            self.storage.save_checkpoint(
                safe_run_id,
                turn_index=int(cp["turn_index"]),
                message_idx=int(cp["message_idx"]),
                checkpoint_id=str(cp["checkpoint_id"]),
                tenant_id=str(cp.get("tenant_id") or "local"),
                label=str(cp.get("label") or ""),
                file_manifest=manifest,
                git_tree=cp.get("git_tree"),
                created_at=str(cp.get("created_at") or ""),
            )
            return dict(entry)

    def rewind(
        self,
        run: dict[str, Any],
        checkpoint_id: str,
        workspace: str | Path,
        *,
        scope: str = "both",
    ) -> dict[str, Any]:
        """按指定范围（both / files / messages）将工作区文件与/或会话消息回退到目标检查点。"""
        clean_scope = str(scope or "both").strip().lower()
        if clean_scope not in VALID_REWIND_SCOPES:
            raise ValueError(f"scope 必须是 {sorted(VALID_REWIND_SCOPES)} 之一")
        run_id = _validate_run_id(str(run.get("id") or ""))
        meta = run.setdefault("metadata", {})
        tenant_id = str(run.get("tenant_id") or meta.get("tenant_id") or "local").strip() or "local"

        with self._lock:
            checkpoints = self.storage.list_checkpoints(run_id, tenant_id=tenant_id)
            target_idx = next(
                (i for i, item in enumerate(checkpoints) if item["checkpoint_id"] == checkpoint_id),
                None,
            )
            if target_idx is None:
                raise CheckpointError("checkpoint_not_found", f"检查点不存在：{checkpoint_id}")
            target_cp = checkpoints[target_idx]
            affected = checkpoints[target_idx:]

            final_file_actions: dict[str, str] = {}
            if clean_scope in {"both", "files"}:
                # 严禁在此处提前调用 .resolve()：必须保留未解析的原始 Path(workspace)，
                # 确保 _validate_workspace_relpath_for_restore 与 _open_safe_parent_dir_fd 真正拦截符号链接工作区！
                ws_input = Path(workspace)
                ws_raw = ws_input.absolute()
                if ws_raw.is_symlink() or not ws_raw.is_dir():
                    raise CheckpointError("unsafe_path", "工作区目录无效或是符号链接。")

                # 阶段 1（零磁盘变更预校验）：预校验全部受影响检查点的路径合法性并加载/校验全部 Blob 完整性
                blob_cache: dict[str, bytes] = {}
                planned_ops: dict[str, tuple[str, bytes | None, int | None]] = {}
                # 按时间严格逆序折叠检查点：先撤消后几轮修改，最终收束为目标轮次的 pre-edit 原始状态
                for cp in reversed(affected):
                    manifest = cp.get("file_manifest")
                    if not isinstance(manifest, dict):
                        continue
                    for rel_path, entry in sorted(manifest.items()):
                        if not isinstance(entry, dict):
                            continue
                        norm_rel, target = _validate_workspace_relpath_for_restore(ws_input, rel_path)
                        if not entry.get("existed", False):
                            if target.is_symlink():
                                raise CheckpointError("unsafe_path", f"拒绝删除符号链接：{norm_rel}")
                            if target.exists():
                                st = os.lstat(target)
                                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                                    raise CheckpointError("unsafe_path", f"拒绝删除非常规或硬链接文件：{norm_rel}")
                            planned_ops[norm_rel] = ("delete", None, None)
                        else:
                            blob_sha = entry.get("sha256")
                            if not isinstance(blob_sha, str) or not blob_sha:
                                raise CheckpointError("invalid_blob_sha", f"检查点缺少文件 {norm_rel} 的 Blob 摘要。")
                            blob_bytes = blob_cache.get(blob_sha)
                            if blob_bytes is None:
                                blob_bytes = self._load_blob(run_id, blob_sha)
                                blob_cache[blob_sha] = blob_bytes
                            if target.is_symlink():
                                raise CheckpointError("unsafe_path", f"拒绝覆写符号链接目标：{norm_rel}")
                            if target.exists():
                                open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                                try:
                                    check_fd = os.open(target, open_flags)
                                except OSError as exc:
                                    raise CheckpointError("unsafe_path", f"无法安全校验回退目标文件：{exc}") from exc
                                try:
                                    st = os.fstat(check_fd)
                                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                                        raise CheckpointError("unsafe_path", f"回退目标必须是无硬链接的常规文件：{norm_rel}")
                                finally:
                                    os.close(check_fd)
                            raw_mode = entry.get("mode")
                            file_mode = (
                                int(raw_mode) & 0o777
                                if isinstance(raw_mode, int) and not isinstance(raw_mode, bool)
                                else None
                            )
                            planned_ops[norm_rel] = ("restore", blob_bytes, file_mode)

                # 阶段 2（基于 dir_fd + O_DIRECTORY | O_NOFOLLOW 安全落盘/删除，全程不跟随符号链接）
                for norm_rel, (op_kind, blob_bytes, file_mode) in sorted(planned_ops.items()):
                    if op_kind == "delete":
                        opened = _open_safe_parent_dir_fd(ws_input, norm_rel, create_missing=False)
                        if opened is not None:
                            parent_fd, leaf_name = opened
                            try:
                                _safe_unlink_in_dirfd(parent_fd, leaf_name, norm_rel)
                            finally:
                                os.close(parent_fd)
                        final_file_actions[norm_rel] = "deleted"
                    else:
                        opened = _open_safe_parent_dir_fd(ws_input, norm_rel, create_missing=True)
                        if opened is None:
                            raise CheckpointError("unsafe_path", f"无法安全打开工作区目标父目录：{norm_rel}")
                        parent_fd, leaf_name = opened
                        try:
                            _atomic_write_bytes_in_dirfd(
                                parent_fd,
                                leaf_name,
                                blob_bytes or b"",
                                mode=file_mode,
                                norm_rel=norm_rel,
                            )
                        finally:
                            os.close(parent_fd)
                        final_file_actions[norm_rel] = "restored"

            messages_before = len(run.get("messages") or [])
            healed_dangling = False
            deleted_checkpoints = 0
            if clean_scope in {"both", "messages"}:
                target_msg_idx = min(max(0, int(target_cp["message_idx"])), messages_before)
                truncated_msgs = list((run.get("messages") or [])[:target_msg_idx])
                healed_dangling = _heal_dangling_tool_uses(truncated_msgs)
                run["messages"] = truncated_msgs
                meta["compaction_cursors"] = {}
                meta["compaction_summaries"] = {}
                meta.pop("compaction_summary", None)
                meta["micro_compaction_cursors"] = {}
                for transient_key in (
                    "pending_calls",
                    "pending_results",
                    "waiting_proposal_ids",
                    "executing_proposal_id",
                    "active_checkpoint_id",
                    "turn_skills_snapshot",
                    "stop_veto_count",
                    "truncation_continuations",
                ):
                    meta.pop(transient_key, None)
                deleted_checkpoints = self.storage.delete_checkpoints_after(
                    run_id, int(target_cp["turn_index"]), tenant_id=tenant_id
                )

            restored_files = sorted(p for p, act in final_file_actions.items() if act == "restored")
            deleted_files = sorted(p for p, act in final_file_actions.items() if act == "deleted")
            return {
                "ok": True,
                "run_id": run_id,
                "checkpoint_id": checkpoint_id,
                "turn_index": int(target_cp["turn_index"]),
                "message_idx": int(target_cp["message_idx"]),
                "scope": clean_scope,
                "restored_files": restored_files,
                "deleted_files": deleted_files,
                "messages_before": messages_before,
                "messages_after": len(run.get("messages") or []),
                "healed_dangling_tool_use": healed_dangling,
                "deleted_checkpoints": deleted_checkpoints,
            }

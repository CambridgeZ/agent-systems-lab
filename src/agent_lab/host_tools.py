"""本地直连工作区工具集（Claude Code / Codex 风格）：全功能文件读写编辑、Glob/Grep 检索与终端命令执行。"""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any

from .observability import redact_secrets
from .permissions import build_scrubbed_env, evaluate_tool_permission
from .repomap import _load_gitignore_patterns, _matches_gitignore
from .storage import Storage, digest

_HOST_MAX_FILE_BYTES = 512 * 1024  # 512 KiB，覆盖绝大多数源码、配置与脚本文件
_HOST_MAX_GLOB_RESULTS = 200
_HOST_MAX_GREP_MATCHES = 60
_HOST_MAX_STREAM_BYTES = 32 * 1024  # 32 KiB 原始输出上限（超出交由 Runtime 分页投影）

_ALWAYS_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".agent-lab"}
)
_FORBIDDEN_DOT_PARTS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".kube", ".agent-lab"})
_SENSITIVE_STEMS = frozenset(
    {"credentials", "secrets", "secret", "private_key", "tokens", "api_key", "apikey"}
)
_SECRET_NAMES = frozenset({"credentials.py", "secrets.py", "secret.py", "private_key.py", "tokens.py"})
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".env"})


class HostToolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_host_secret_segment(name: str) -> bool:
    lower = name.casefold()
    stem = Path(name).stem.casefold()
    first_stem = lower.split(".", 1)[0] if not lower.startswith(".") else lower[1:].split(".", 1)[0]
    if (
        lower in _SECRET_NAMES
        or stem in _SENSITIVE_STEMS
        or first_stem in _SENSITIVE_STEMS
        or lower.startswith((".env", "id_rsa", "id_ed25519", ".agent-apply-", ".agent-patch-"))
        or any(lower.endswith(sfx) for sfx in _SECRET_SUFFIXES)
    ):
        return True
    return False


def resolve_host_workspace_path(
    workspace: Path,
    raw_path: str,
    *,
    allow_missing: bool = False,
    allow_directory: bool = False,
) -> tuple[str, Path]:
    """将相对路径（或位于工作区内的绝对路径）解析为安全的 (rel_posix, resolved_path)。"""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path or "\\" in raw_path:
        raise HostToolError("unsafe_path", "路径必须是非空的 POSIX 工作区路径。")
    ws_root = Path(workspace).resolve()
    cleaned = raw_path.strip()

    if cleaned in {".", "./"}:
        if not allow_directory:
            raise HostToolError("unsafe_path", "目标需要是具体文件路径而非工作区根目录。")
        return ".", ws_root

    raw_p = Path(cleaned)
    if raw_p.is_absolute():
        # 严禁在逐段校验前对工作区内部调用 resolve()（否则会跟随软链接绕过 .env 与 symlink 拦截）！
        raw_pure = PurePosixPath(cleaned)
        if any(part in ("", ".", "..") for part in raw_pure.parts[1:]):
            raise HostToolError("unsafe_path", "绝对路径中禁止包含 '..' 或 '.' 跳转段。")
        ws_lex = PurePosixPath(ws_root.as_posix())
        ws_raw_lex = PurePosixPath(Path(workspace).expanduser().absolute().as_posix())
        rel_pure: PurePosixPath | None = None
        for prefix in (ws_lex, ws_raw_lex):
            try:
                rel_pure = raw_pure.relative_to(prefix)
                break
            except ValueError:
                continue
        if rel_pure is None:
            raise HostToolError("unsafe_path", "绝对路径越过了当前工作区边界。")
        if str(rel_pure) == ".":
            if not allow_directory:
                raise HostToolError("unsafe_path", "目标需要是具体文件路径而非工作区根目录。")
            return ".", ws_root
        pure = rel_pure
    else:
        if ":" in cleaned:
            raise HostToolError("unsafe_path", "路径不能包含冒号或 URL 协议。")
        pure = PurePosixPath(cleaned)

    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise HostToolError("unsafe_path", "拒绝上级目录跳转或非法相对路径。")
    if any(part in _FORBIDDEN_DOT_PARTS for part in pure.parts):
        raise HostToolError("unsafe_path", "禁止读写 .git 或系统密钥隐藏目录。")
    if any(is_host_secret_segment(part) for part in pure.parts):
        raise HostToolError("secret_access_forbidden", "禁止读取或修改敏感凭证与密钥文件。")

    candidate = ws_root
    for idx, part in enumerate(pure.parts):
        candidate = candidate / part
        if candidate.is_symlink():
            raise HostToolError("unsafe_path", "工作区内拒绝操作符号链接路径。")
        is_last = idx == len(pure.parts) - 1
        if not is_last and candidate.exists() and not candidate.is_dir():
            raise HostToolError("unsafe_path", "父级路径段不是有效目录。")

    try:
        resolved = candidate.resolve()
    except RuntimeError as exc:
        raise HostToolError("unsafe_path", "检测到符号链接循环。") from exc
    if resolved != ws_root and not resolved.is_relative_to(ws_root):
        raise HostToolError("unsafe_path", "路径解析越过了工作区边界。")

    if candidate.exists():
        st = candidate.stat()
        if candidate.is_dir():
            if not allow_directory:
                raise HostToolError("unsafe_path", "目标路径是目录而非普通文件。")
        elif candidate.is_file():
            if st.st_nlink != 1:
                raise HostToolError("unsafe_path", "拒绝操作多重硬链接文件。")
        else:
            raise HostToolError("unsafe_path", "拒绝操作非常规特殊文件。")
    elif not allow_missing:
        raise HostToolError("not_found", f"路径不存在：{pure.as_posix()}")

    return pure.as_posix(), candidate


HOST_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "fs_read",
        "description": "读取工作区内任意代码、配置、脚本或测试文件（最大 512 KiB），支持按行号分页读取并带行号返回。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 320, "description": "工作区相对路径。"},
                "offset_line": {"type": "integer", "minimum": 1, "description": "起始行号（从 1 开始，默认 1）。"},
                "limit_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "最大读取行数（默认 200）。",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_write",
        "description": "创建或完整覆写工作区内的文本、代码、脚本或测试文件（最大 512 KiB）；ask 模式下生成待审批提案。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 320, "description": "工作区相对路径。"},
                "content": {"type": "string", "maxLength": 524288, "description": "完整 UTF-8 文件内容。"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_edit",
        "description": "精确替换工作区文件中的文本片段（默认要求唯一匹配，支持 replace_all）；ask 模式下生成待审批提案。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 320, "description": "工作区相对路径。"},
                "old_text": {"type": "string", "minLength": 1, "maxLength": 65536, "description": "待替换原文片段。"},
                "new_text": {"type": "string", "maxLength": 65536, "description": "替换后的新文本。"},
                "replace_all": {"type": "boolean", "description": "是否替换全部出现位置（默认 false）。"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_glob",
        "description": "按通配符模式（如 **/*.py、src/**/*.ts）快速匹配工作区文件列表，自动遵循 .gitignore 并过滤敏感文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Glob 匹配模式。"},
                "path": {"type": "string", "maxLength": 240, "description": "可选子目录起点，默认 '.'。"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_grep",
        "description": "在工作区文本文件中使用正则表达式搜索匹配行，返回文件路径、行号与代码片段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 240, "description": "正则表达式。"},
                "path": {"type": "string", "maxLength": 240, "description": "可选子目录或单文件路径，默认 '.'。"},
                "glob": {"type": "string", "maxLength": 120, "description": "可选文件名过滤（如 *.py）。"},
                "case_sensitive": {"type": "boolean", "description": "是否区分大小写（默认 true）。"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash_exec",
        "description": "在本机工作区目录执行终端命令或运行脚本（自动剥离服务端密钥环境变量，带超时与进程组回收）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 8000, "description": "要执行的终端命令。"},
                "cwd": {"type": "string", "maxLength": 240, "description": "工作区内的相对子目录，默认 '.'。"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": "命令超时秒数（默认 30，最大 120）。",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]


def _walk_workspace_files(workspace: Path, start_dir: Path) -> list[tuple[str, Path]]:
    ws_root = Path(workspace).resolve()
    patterns = _load_gitignore_patterns(ws_root)
    results: list[tuple[str, Path]] = []
    for root_str, subdirs, names in os.walk(start_dir, followlinks=False):
        cur_dir = Path(root_str)
        kept_dirs: list[str] = []
        for d in sorted(subdirs):
            if d in _ALWAYS_SKIP_DIRS or d in _FORBIDDEN_DOT_PARTS or is_host_secret_segment(d):
                continue
            sub_p = cur_dir / d
            if sub_p.is_symlink():
                continue
            sub_rel = sub_p.relative_to(ws_root).as_posix()
            if _matches_gitignore(sub_rel, is_dir=True, patterns=patterns):
                continue
            kept_dirs.append(d)
        subdirs[:] = kept_dirs

        for fname in sorted(names):
            if is_host_secret_segment(fname):
                continue
            fpath = cur_dir / fname
            if fpath.is_symlink():
                continue
            rel = fpath.relative_to(ws_root).as_posix()
            if _matches_gitignore(rel, is_dir=False, patterns=patterns):
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            if st.st_nlink == 1 and st.st_size <= _HOST_MAX_FILE_BYTES and fpath.is_file():
                results.append((rel, fpath))
    return results


def execute_fs_read(
    workspace: Path,
    *,
    path: str,
    offset_line: int = 1,
    limit_lines: int = 200,
) -> dict[str, Any]:
    rel, target = resolve_host_workspace_path(workspace, path)
    st = target.stat()
    if st.st_size > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    try:
        raw_text = target.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc

    all_lines = raw_text.splitlines()
    total_lines = len(all_lines)
    start_idx = max(0, int(offset_line) - 1)
    end_idx = min(total_lines, start_idx + max(1, min(500, int(limit_lines))))
    selected = all_lines[start_idx:end_idx]
    numbered = "\n".join(f"{start_idx + i + 1}\t{line}" for i, line in enumerate(selected))
    return {
        "ok": True,
        "path": rel,
        "sha256": digest(raw_text),
        "total_lines": total_lines,
        "start_line": start_idx + 1 if total_lines else 0,
        "end_line": end_idx,
        "truncated": end_idx < total_lines or start_idx > 0,
        "content": numbered,
    }


def _atomic_write_host_file(target: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = (target.stat().st_mode & 0o777) if target.exists() else (
        0o755 if target.suffix == ".sh" else 0o644
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".agent-patch-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, existing_mode)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def execute_fs_write(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    path: str,
    content: str,
    permission_mode: str,
    cross_lock_cm: Any,
    call_id: str | None = None,
    expected_epoch: int | None = None,
) -> dict[str, Any]:
    if len(content.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")

    perm = evaluate_tool_permission("fs_write", {"path": path}, permission_mode=permission_mode)
    if perm["decision"] == "ask":
        rel, target = resolve_host_workspace_path(workspace, path, allow_missing=True)
        existed = target.exists()
        old_content = ""
        if existed:
            if target.stat().st_size > _HOST_MAX_FILE_BYTES:
                raise HostToolError("file_too_large", "原文件超过 512 KiB，拒绝直接覆盖。")
            try:
                old_content = target.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise HostToolError("binary_file", "拒绝覆盖非 UTF-8 二进制文件。") from exc
        action = "modify" if existed else "create"
        diff_text = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}" if existed else "/dev/null",
                tofile=f"b/{rel}",
            )
        )
        proposal = storage.create_changeset_proposal(
            run_id,
            [{"path": rel, "action": action, "old_content": old_content, "new_content": content}],
            summary=f"fs_write ({action}) {rel}",
            operation_id=call_id,
            expected_epoch=expected_epoch,
            tool_name="fs_write",
        )
        storage.add_event(
            run_id,
            "patch_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": proposal["kind"],
                "path": rel,
                "paths": [rel],
                "diff": diff_text[:6000],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "path": rel,
            "action": action,
            "diff": diff_text[:4000],
        }

    with cross_lock_cm:
        storage.verify_epoch(run_id, expected_epoch)
        rel, target = resolve_host_workspace_path(workspace, path, allow_missing=True)
        existed = target.exists()
        old_content = ""
        if existed:
            if target.stat().st_size > _HOST_MAX_FILE_BYTES:
                raise HostToolError("file_too_large", "原文件超过 512 KiB，拒绝直接覆盖。")
            try:
                old_content = target.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise HostToolError("binary_file", "拒绝覆盖非 UTF-8 二进制文件。") from exc
        action = "modify" if existed else "create"
        diff_text = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}" if existed else "/dev/null",
                tofile=f"b/{rel}",
            )
        )
        _atomic_write_host_file(target, content)
    new_sha = digest(content)
    storage.add_event(
        run_id,
        "file_written",
        {"tool": "fs_write", "path": rel, "action": action, "sha256": new_sha, "bytes": len(content.encode("utf-8"))},
    )
    return {
        "ok": True,
        "path": rel,
        "action": action,
        "sha256": new_sha,
        "bytes": len(content.encode("utf-8")),
        "diff": diff_text[:3000],
    }


def execute_fs_edit(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    permission_mode: str,
    cross_lock_cm: Any,
    call_id: str | None = None,
    expected_epoch: int | None = None,
) -> dict[str, Any]:
    if old_text == new_text:
        raise HostToolError("empty_patch", "old_text 与 new_text 完全相同，未产生修改。")

    def _compute_edit() -> tuple[str, Path, str, str, int, str]:
        rel_p, tgt = resolve_host_workspace_path(workspace, path, allow_missing=False)
        if tgt.stat().st_size > _HOST_MAX_FILE_BYTES:
            raise HostToolError("file_too_large", "目标文件超过 512 KiB 上限。")
        try:
            old_c = tgt.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc
        cnt = old_c.count(old_text)
        if cnt == 0:
            raise HostToolError("old_text_not_found", "old_text 在目标文件中未找到，请先用 fs_read 核对最新内容。")
        if cnt > 1 and not replace_all:
            raise HostToolError(
                "ambiguous_match",
                f"old_text 在文件中出现了 {cnt} 次；请提供更多上下文使其唯一，或显式传 replace_all=true。",
            )
        new_c = old_c.replace(old_text, new_text) if replace_all else old_c.replace(old_text, new_text, 1)
        if len(new_c.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
            raise HostToolError("file_too_large", "编辑后文件超过 512 KiB 上限。")
        diff_t = "".join(
            difflib.unified_diff(
                old_c.splitlines(keepends=True),
                new_c.splitlines(keepends=True),
                fromfile=f"a/{rel_p}",
                tofile=f"b/{rel_p}",
            )
        )
        return rel_p, tgt, old_c, new_c, (cnt if replace_all else 1), diff_t

    perm = evaluate_tool_permission("fs_edit", {"path": path}, permission_mode=permission_mode)
    if perm["decision"] == "ask":
        rel, _, old_content, new_content, rep_count, diff_text = _compute_edit()
        proposal = storage.create_changeset_proposal(
            run_id,
            [{"path": rel, "action": "modify", "old_content": old_content, "new_content": new_content}],
            summary=f"fs_edit {rel} ({rep_count} replacement)",
            operation_id=call_id,
            expected_epoch=expected_epoch,
            tool_name="fs_edit",
        )
        storage.add_event(
            run_id,
            "patch_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": proposal["kind"],
                "path": rel,
                "paths": [rel],
                "diff": diff_text[:6000],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "path": rel,
            "replacements": rep_count,
            "diff": diff_text[:4000],
        }

    with cross_lock_cm:
        storage.verify_epoch(run_id, expected_epoch)
        rel, target, _, new_content, rep_count, diff_text = _compute_edit()
        _atomic_write_host_file(target, new_content)
    new_sha = digest(new_content)
    storage.add_event(
        run_id,
        "file_written",
        {"tool": "fs_edit", "path": rel, "action": "modify", "sha256": new_sha, "replacements": rep_count},
    )
    return {
        "ok": True,
        "path": rel,
        "replacements": rep_count,
        "sha256": new_sha,
        "diff": diff_text[:3000],
    }


def _glob_matches(rel_posix: str, pattern: str) -> bool:
    norm_pat = pattern.strip().lstrip("./")
    if fnmatch.fnmatchcase(rel_posix, norm_pat):
        return True
    if norm_pat.startswith("**/") and fnmatch.fnmatchcase(rel_posix, norm_pat[3:]):
        return True
    if "/" not in norm_pat and fnmatch.fnmatchcase(PurePosixPath(rel_posix).name, norm_pat):
        return True
    # 支持 src/**/foo.py 在零层子目录 src/foo.py 的匹配
    if "/**/" in norm_pat:
        collapsed = norm_pat.replace("/**/", "/")
        if fnmatch.fnmatchcase(rel_posix, collapsed):
            return True
    return False


def execute_fs_glob(workspace: Path, *, pattern: str, path: str = ".") -> dict[str, Any]:
    rel_base, start_dir = resolve_host_workspace_path(workspace, path or ".", allow_directory=True)
    if not start_dir.is_dir():
        raise HostToolError("unsafe_path", "fs_glob 的起点必须是目录。")
    candidates = _walk_workspace_files(workspace, start_dir)
    matched: list[str] = []
    for rel, _ in candidates:
        sub_rel = (
            PurePosixPath(rel).relative_to(PurePosixPath(rel_base)).as_posix()
            if rel_base != "."
            else rel
        )
        if _glob_matches(sub_rel, pattern) or _glob_matches(rel, pattern):
            matched.append(rel)
    matched.sort()
    truncated = len(matched) > _HOST_MAX_GLOB_RESULTS
    return {
        "ok": True,
        "pattern": pattern,
        "base": rel_base,
        "total_matches": len(matched),
        "truncated": truncated,
        "files": matched[:_HOST_MAX_GLOB_RESULTS],
    }


def execute_fs_grep(
    workspace: Path,
    *,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = True,
) -> dict[str, Any]:
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise HostToolError("invalid_regex", f"无效的正则表达式：{exc}") from exc

    rel_base, target = resolve_host_workspace_path(workspace, path or ".", allow_directory=True)
    if target.is_file():
        files_to_scan = [(rel_base, target)]
    else:
        files_to_scan = _walk_workspace_files(workspace, target)

    matches: list[dict[str, Any]] = []
    truncated = False
    for rel, fpath in files_to_scan:
        if glob and not _glob_matches(rel, glob):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {
                        "path": rel,
                        "line": lineno,
                        "text": line[:400],
                    }
                )
                if len(matches) >= _HOST_MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "ok": True,
        "pattern": pattern,
        "matches_count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


def _drain_stream(stream: Any, bucket: bytearray, limit: int, overflow_flag: list[bool]) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if len(bucket) < limit:
                take = min(len(chunk), limit - len(bucket))
                bucket.extend(chunk[:take])
                if take < len(chunk):
                    overflow_flag[0] = True
            else:
                overflow_flag[0] = True
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _terminate_process_group(proc: subprocess.Popen[bytes], pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=0.4)
    # 即使主 shell 已退出，若进程组内仍有后台孙进程存活，也必须等待短暂宽限后 SIGKILL 清场
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
            time.sleep(0.03)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            break
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=1.0)


def run_host_bash_command(
    workspace: Path,
    command: str,
    *,
    cwd: str = ".",
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """在脱敏环境变量与独立进程组中执行终端命令，超时或异常时干净回收全部子孙进程。"""
    rel_cwd, resolved_cwd = resolve_host_workspace_path(workspace, cwd or ".", allow_directory=True)
    if not resolved_cwd.is_dir():
        raise HostToolError("unsafe_path", f"命令工作目录不是有效目录：{rel_cwd}")

    timeout = max(1, min(120, int(timeout_seconds)))
    shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    env = build_scrubbed_env()
    t0 = time.monotonic()

    proc = subprocess.Popen(
        [shell_bin, "-c", command],
        cwd=str(resolved_cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = proc.pid
    out_buf = bytearray()
    err_buf = bytearray()
    out_trunc = [False]
    err_trunc = [False]

    t_out = threading.Thread(
        target=_drain_stream, args=(proc.stdout, out_buf, _HOST_MAX_STREAM_BYTES, out_trunc), daemon=True
    )
    t_err = threading.Thread(
        target=_drain_stream, args=(proc.stderr, err_buf, _HOST_MAX_STREAM_BYTES, err_trunc), daemon=True
    )
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc, pgid)
    except BaseException:
        _terminate_process_group(proc, pgid)
        raise
    finally:
        # 清理可能残留并占用管道 FD 的后台子孙进程，防止 t_out.join 阻塞
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
        t_out.join(timeout=1.5)
        t_err.join(timeout=1.5)

    duration_ms = round((time.monotonic() - t0) * 1000.0, 2)
    exit_code = proc.returncode if proc.returncode is not None else -9
    stdout_text = redact_secrets(out_buf.decode("utf-8", errors="replace"))
    stderr_text = redact_secrets(err_buf.decode("utf-8", errors="replace"))

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
        "command": command,
        "cwd": rel_cwd,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": out_trunc[0],
        "stderr_truncated": err_trunc[0],
    }


def execute_bash_tool(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    command: str,
    cwd: str = ".",
    timeout_seconds: int = 30,
    permission_mode: str,
    call_id: str | None = None,
    expected_epoch: int | None = None,
) -> dict[str, Any]:
    rel_cwd, resolved_cwd = resolve_host_workspace_path(workspace, cwd or ".", allow_directory=True)
    if not resolved_cwd.is_dir():
        raise HostToolError("unsafe_path", f"命令工作目录不是有效目录：{rel_cwd}")

    perm = evaluate_tool_permission(
        "bash_exec",
        {"command": command, "cwd": rel_cwd, "timeout_seconds": timeout_seconds},
        permission_mode=permission_mode,
    )
    if perm["decision"] == "deny":
        return {
            "ok": False,
            "code": "command_blocked",
            "command": command,
            "error": f"安全策略拒绝执行该高危命令：{perm['reason']}",
        }
    if perm["decision"] == "ask":
        args_payload = {"command": command, "cwd": rel_cwd, "timeout_seconds": int(timeout_seconds)}
        proposal = storage.create_action_proposal(
            run_id,
            kind="command",
            tool_name="bash_exec",
            arguments=args_payload,
            summary=f"[{perm['risk']}] ({rel_cwd}) $ {command[:200]}",
            risk_level=perm["risk"],
            operation_id=call_id,
            expected_epoch=expected_epoch,
        )
        storage.add_event(
            run_id,
            "action_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": "command",
                "tool": "bash_exec",
                "command": command[:500],
                "cwd": rel_cwd,
                "risk_level": perm["risk"],
                "reason": perm["reason"],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "kind": "command",
            "command": command,
            "cwd": rel_cwd,
            "risk_level": perm["risk"],
            "reason": perm["reason"],
        }

    storage.verify_epoch(run_id, expected_epoch)
    result = run_host_bash_command(
        workspace,
        command,
        cwd=rel_cwd,
        timeout_seconds=timeout_seconds,
    )
    storage.add_event(
        run_id,
        "command_executed",
        {
            "command": command[:300],
            "cwd": rel_cwd,
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "duration_ms": result["duration_ms"],
        },
    )
    return result

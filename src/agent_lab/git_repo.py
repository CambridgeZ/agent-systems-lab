"""真实 Git 仓库与独立 Worktree 管理：固定基线 commit、多文件 diff、高可用冷启动、归档传输与受控提交。

设计边界：
1. 每个任务运行在基于固定 base_commit 创建的 detached git worktree 中，互不污染，且绝不改写源仓库 .git/config。
2. 只操作本地显式指定的真实目录，不自动联网 clone 任意远端 URL；git 子进程启用独立进程组超时强杀防泄漏。
3. 冷启动基线仓库采用 flock 独占锁 + 同目录临时构建 + 原子 rename，支持多线程/多进程并发安全初始化。
4. 只有补丁经过审批且容器验收测试明确返回 passed 后，才允许生成 Git 提交；递归拦截全部测试与密钥路径篡改。
5. 支持跨节点工作区内存级 tar.gz 打包与解压，自动将 worktree gitdir 物化为独立 Git 仓库并防御路径穿越与压缩炸弹。
"""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import gzip
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import uuid
import zlib

from .host_tools import is_host_secret_segment

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Agent Systems Lab",
    "GIT_AUTHOR_EMAIL": "agent-lab@local.invalid",
    "GIT_COMMITTER_NAME": "Agent Systems Lab",
    "GIT_COMMITTER_EMAIL": "agent-lab@local.invalid",
    "LC_ALL": "C",
}
_DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".css",
    ".scss",
    ".html",
    ".sql",
    ".sh",
)
_FORBIDDEN_HIDDEN_DIRS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".kube", ".agent-lab"})
_MAX_UNTRACKED_DIFF_FILES = 32
_MAX_UNTRACKED_FILE_BYTES = 64 * 1024
_BINARY_SNIFF_BYTES = 8192
_SENSITIVE_STEMS = frozenset(
    {"credentials", "secrets", "secret", "private_key", "tokens", "api_key", "apikey"}
)
_SECRET_FILENAMES = frozenset(
    {
        "credentials.py",
        "secrets.py",
        "secret.py",
        "private_key.py",
        "tokens.py",
    }
)
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".env"})
_DEFAULT_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 10_000


class GitRepoError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _rmtree_force(target: Path) -> None:
    def _handle_readonly(func: Any, path: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRWXU)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_handle_readonly)
    else:
        shutil.rmtree(target, onerror=_handle_readonly)


def _git(args: list[str], cwd: Path, timeout: float = 15.0) -> str:
    git_bin = shutil.which("git")
    if not git_bin:
        raise GitRepoError("git_unavailable", "系统未安装 git 命令。")
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"}
    }
    env.update(_GIT_ENV)
    resolved_cwd = Path(cwd).resolve()
    if resolved_cwd.parent != resolved_cwd and os.pathsep not in str(resolved_cwd.parent):
        env["GIT_CEILING_DIRECTORIES"] = str(resolved_cwd.parent)
    proc = subprocess.Popen(
        [git_bin, *args],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()
        with contextlib.suppress(Exception):
            proc.communicate()
        raise GitRepoError("git_timeout", "git 操作超时。") from exc
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()
        with contextlib.suppress(Exception):
            proc.communicate()
        raise
    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()[:300]
        raise GitRepoError("git_command_failed", f"git {' '.join(args[:2])} 失败: {detail}")
    return stdout


def _validate_local_dir(path: Path, label: str) -> Path:
    candidate = Path(path)
    if "://" in str(candidate) or ".." in candidate.parts:
        raise GitRepoError("unsafe_repo_path", f"{label} 不能是 URL 或包含 ..")
    if candidate.exists() and candidate.is_symlink():
        raise GitRepoError("unsafe_repo_path", f"{label} 不能是符号链接")
    return candidate.parent.resolve() / candidate.name


def _has_own_git_marker(repo: Path) -> bool:
    marker = repo / ".git"
    if marker.is_symlink():
        return False
    if marker.is_dir():
        return (marker / "HEAD").is_file() and (marker / "objects").is_dir()
    if marker.is_file():
        return True
    return (repo / "HEAD").is_file() and (repo / "objects").is_dir()


def init_git_repo_from_dir(source_dir: Path, repo_dir: Path) -> str:
    """从静态模板目录创建带初始基线提交的本地 Git 仓库，返回 40 位 commit SHA。"""
    source = Path(source_dir).resolve()
    if not source.is_dir():
        raise GitRepoError("template_not_found", "模板目录不存在。")
    target = _validate_local_dir(repo_dir, "目标仓库目录")
    if target.exists():
        raise GitRepoError("repo_exists", "目标仓库目录已存在。")
    shutil.copytree(
        source,
        target,
        symlinks=False,
        ignore=shutil.ignore_patterns(".*", "__pycache__", "*.pyc"),
    )
    _git(["init", "-q", "--template="], cwd=target)
    _git(["add", "-A"], cwd=target)
    _git(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "chore: initial baseline"], cwd=target)
    return resolve_commit(target, "HEAD")


def ensure_git_origin_atomic(source_dir: Path, origin_dir: Path) -> str:
    """并发安全地确保本地基线仓库就绪：独占 flock + 同级临时目录初始化 + 原子替换。"""
    source = Path(source_dir).resolve()
    if not source.is_dir():
        raise GitRepoError("template_not_found", "模板目录不存在。")
    origin = _validate_local_dir(origin_dir, "目标仓库目录")
    origin.parent.mkdir(parents=True, exist_ok=True)
    lock_path = origin.parent / f".{origin.name}.init.lock"
    if lock_path.is_symlink():
        raise GitRepoError("unsafe_repo_path", "初始化锁文件不能是符号链接。")
    with open(lock_path, "a+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            if origin.is_symlink():
                raise GitRepoError("unsafe_repo_path", "目标仓库目录不能是符号链接")
            if origin.exists():
                if origin.is_dir() and _has_own_git_marker(origin):
                    try:
                        return resolve_commit(origin, "HEAD")
                    except GitRepoError:
                        pass
                if origin.is_dir():
                    _rmtree_force(origin)
                else:
                    origin.unlink()
            tmp_dir = origin.parent / f".tmp-{origin.name}-{uuid.uuid4().hex}"
            try:
                init_git_repo_from_dir(source, tmp_dir)
                os.replace(tmp_dir, origin)
            finally:
                if tmp_dir.exists():
                    _rmtree_force(tmp_dir)
            return resolve_commit(origin, "HEAD")
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def resolve_commit(repo_dir: Path, ref: str = "HEAD") -> str:
    """解析本地 Git 仓库的指定引用为不可变 40 位 commit SHA。"""
    repo = _validate_local_dir(repo_dir, "源仓库")
    if not repo.is_dir():
        raise GitRepoError("repo_not_found", "本地 Git 仓库不存在。")
    if not ref or ref.startswith("-") or len(ref) > 128 or any(ch.isspace() for ch in ref):
        raise GitRepoError("invalid_git_ref", "Git 基线引用格式不合法。")
    sha = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo).strip()
    if not _COMMIT_RE.fullmatch(sha):
        raise GitRepoError("invalid_commit", "无法解析为 40 位 commit SHA。")
    return sha


def create_task_worktree(repo_dir: Path, worktree_dir: Path, ref: str = "HEAD") -> dict[str, Any]:
    """在指定基线 commit 上创建独立 detached worktree，绝不改写源仓库共享 .git/config。"""
    repo = _validate_local_dir(repo_dir, "源仓库")
    worktree = _validate_local_dir(worktree_dir, "任务 worktree")
    if worktree.exists():
        raise GitRepoError("worktree_exists", "任务 worktree 路径已存在。")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    base_commit = resolve_commit(repo, ref)
    _git(["worktree", "add", "--detach", "-q", str(worktree), base_commit], cwd=repo)
    return {
        "enabled": True,
        "repo_fingerprint": base_commit[:12],
        "base_commit": base_commit,
        "head_commit": base_commit,
        "committed_sha": None,
    }


def cleanup_task_worktree(worktree_dir: Path, origin_repo_dir: Path | None = None) -> bool:
    """安全移除任务 worktree 目录，并在提供 origin_repo_dir 时执行 git worktree prune。"""
    worktree = _validate_local_dir(worktree_dir, "任务 worktree")
    if len(worktree.parts) <= 2:
        raise GitRepoError("unsafe_repo_path", "拒绝清理顶层系统目录。")
    origin: Path | None = None
    if origin_repo_dir is not None:
        origin = _validate_local_dir(origin_repo_dir, "源仓库")
        if worktree == origin or origin.is_relative_to(worktree):
            raise GitRepoError("unsafe_repo_path", "任务 worktree 不能等于或包含源仓库目录。")
    removed = False
    if worktree.exists():
        if not worktree.is_dir():
            raise GitRepoError("unsafe_repo_path", "任务 worktree 路径不是目录。")
        _rmtree_force(worktree)
        removed = True
    if origin is not None and origin.is_dir() and _has_own_git_marker(origin):
        _git(["worktree", "prune"], cwd=origin)
    return removed


def is_git_worktree(workspace: Path) -> bool:
    marker = Path(workspace) / ".git"
    return not marker.is_symlink() and (marker.is_file() or marker.is_dir())


def _is_binary_bytes(raw: bytes) -> bool:
    return b"\x00" in raw[:_BINARY_SNIFF_BYTES]


def _build_untracked_synthetic_diffs(
    root: Path,
    untracked_paths: list[str],
    budget_chars: int,
) -> tuple[str, bool]:
    """为未追踪新建文件生成有界合成 unified diff（对比 /dev/null），过滤敏感文件、符号链接与二进制文件。"""
    if not untracked_paths or budget_chars <= 0:
        return "", bool(untracked_paths)
    chunks: list[str] = []
    used = 0
    truncated = len(untracked_paths) > _MAX_UNTRACKED_DIFF_FILES
    for raw_rel in untracked_paths[:_MAX_UNTRACKED_DIFF_FILES]:
        if used >= budget_chars:
            truncated = True
            break
        try:
            norm = _normalize_repo_relpath(raw_rel)
        except GitRepoError:
            continue
        pure = PurePosixPath(norm)
        if any(
            part in _FORBIDDEN_HIDDEN_DIRS or is_host_secret_segment(part)
            for part in pure.parts
        ) or _is_secret_relpath(norm):
            continue
        cursor = root
        is_sym = False
        for part in pure.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                is_sym = True
                break
        if is_sym:
            continue
        try:
            st = os.lstat(cursor)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            continue

        header = f"diff --git a/{norm} b/{norm}\nnew file mode 100644\n"
        if st.st_size > _MAX_UNTRACKED_FILE_BYTES:
            entry_text = (
                f"{header}--- /dev/null\n+++ b/{norm}\n"
                f"@@ -0,0 +0,0 @@\n"
                f"# [omitted: untracked file exceeds {_MAX_UNTRACKED_FILE_BYTES} bytes ({st.st_size} bytes)]\n"
            )
        else:
            try:
                with open(cursor, "rb") as fp:
                    raw_bytes = fp.read(_MAX_UNTRACKED_FILE_BYTES + 1)
            except OSError:
                continue
            if len(raw_bytes) > _MAX_UNTRACKED_FILE_BYTES:
                entry_text = (
                    f"{header}--- /dev/null\n+++ b/{norm}\n"
                    f"@@ -0,0 +0,0 @@\n"
                    f"# [omitted: untracked file exceeds {_MAX_UNTRACKED_FILE_BYTES} bytes]\n"
                )
            elif _is_binary_bytes(raw_bytes):
                entry_text = f"{header}Binary files /dev/null and b/{norm} differ\n"
            else:
                try:
                    text = raw_bytes.decode("utf-8")
                except UnicodeError:
                    entry_text = f"{header}Binary files /dev/null and b/{norm} differ\n"
                else:
                    body = "".join(
                        difflib.unified_diff(
                            [],
                            text.splitlines(keepends=True),
                            fromfile="/dev/null",
                            tofile=f"b/{norm}",
                        )
                    )
                    if not body:
                        body = f"--- /dev/null\n+++ b/{norm}\n"
                    entry_text = header + body
                    if not entry_text.endswith("\n"):
                        entry_text += "\n"

        remaining = budget_chars - used
        if len(entry_text) > remaining:
            chunks.append(entry_text[:remaining])
            used = budget_chars
            truncated = True
            break
        chunks.append(entry_text)
        used += len(entry_text)
    return "".join(chunks), truncated


def worktree_status(workspace: Path, base_commit: str | None = None, max_diff_chars: int = 8000) -> dict[str, Any]:
    """只读查看当前工作区的 Git 基线、修改文件列表和有界 unified diff（含未追踪新建文件合成 diff）。"""
    root = _validate_local_dir(workspace, "工作区")
    if not is_git_worktree(root):
        return {"ok": True, "is_git": False, "note": "当前工作区是静态目录副本，未启用 Git worktree。"}
    head = resolve_commit(root, "HEAD")
    anchor = base_commit if (base_commit and _COMMIT_RE.fullmatch(base_commit)) else head
    porcelain = _git(["status", "--porcelain", "--untracked-files=normal"], cwd=root)
    changed = []
    for line in porcelain.splitlines():
        if len(line) >= 4:
            changed.append({"status": line[:2].strip() or "M", "path": line[3:]})
    porcelain_all_z = _git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=root)
    _, untracked_all = _parse_porcelain_z(porcelain_all_z)

    raw_diff = _git(["diff", "--no-ext-diff", anchor, "--"], cwd=root)
    truncated = len(raw_diff) > max_diff_chars
    combined_diff = raw_diff[:max_diff_chars]

    if untracked_all:
        remaining = max_diff_chars - len(combined_diff)
        sep = "\n" if (combined_diff and not combined_diff.endswith("\n")) else ""
        if remaining > len(sep):
            untracked_diff, untracked_truncated = _build_untracked_synthetic_diffs(
                root, sorted(untracked_all), remaining - len(sep)
            )
            if untracked_diff:
                combined_diff = combined_diff + sep + untracked_diff
            if untracked_truncated:
                truncated = True
        else:
            truncated = True

    return {
        "ok": True,
        "is_git": True,
        "base_commit": anchor,
        "head_commit": head,
        "baseline_matches_head": head == anchor,
        "dirty": bool(changed),
        "changed_files": changed[:64],
        "diff": combined_diff[:max_diff_chars],
        "diff_truncated": truncated,
    }


def verify_worktree_baseline(workspace: Path, expected_base_commit: str) -> None:
    """审批应用前核验 worktree HEAD 仍停留在提案创建时的基线 commit。"""
    if not _COMMIT_RE.fullmatch(expected_base_commit):
        raise GitRepoError("invalid_commit", "基线 commit 格式无效。")
    root = _validate_local_dir(workspace, "工作区")
    if not is_git_worktree(root):
        raise GitRepoError("not_git_worktree", "工作区不是 Git worktree。")
    head = resolve_commit(root, "HEAD")
    if head != expected_base_commit:
        raise GitRepoError("baseline_changed", "Git 基线 commit 已变化，旧审批失效，拒绝应用补丁。")


def _normalize_repo_relpath(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path or "\x00" in raw_path or raw_path.startswith("/"):
        raise GitRepoError("edit_forbidden", f"非法的仓库相对路径: {raw_path!r}")
    stripped = raw_path.rstrip("/")
    if not stripped:
        raise GitRepoError("edit_forbidden", f"非法的仓库相对路径: {raw_path!r}")
    pure = PurePosixPath(stripped)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise GitRepoError("edit_forbidden", f"拒绝越界或上级路径: {raw_path!r}")
    if pure.parts[0] == ".git":
        raise GitRepoError("edit_forbidden", "拒绝直接操作 .git 内部路径。")
    return pure.as_posix()


def _is_test_path(rel_path: str) -> bool:
    cleaned = rel_path.strip("/")
    if not cleaned:
        return False
    pure = PurePosixPath(cleaned)
    dir_parts = pure.parts[:-1] if not rel_path.endswith("/") else pure.parts
    if any(part.casefold() in {"tests", "test"} for part in dir_parts):
        return True
    name_lower = pure.name.casefold()
    return (
        name_lower in {"tests", "test", "conftest.py"}
        or name_lower.startswith("test_")
        or name_lower.endswith("_test.py")
    )


def _is_secret_relpath(rel_path: str) -> bool:
    cleaned = rel_path.strip("/")
    if not cleaned:
        return False
    pure = PurePosixPath(cleaned)
    for part in pure.parts:
        lower = part.casefold()
        if (
            part in _FORBIDDEN_HIDDEN_DIRS
            or is_host_secret_segment(part)
            or lower in _SECRET_FILENAMES
            or Path(part).stem.casefold() in _SENSITIVE_STEMS
            or (not lower.startswith(".") and lower.split(".", 1)[0] in _SENSITIVE_STEMS)
            or lower.startswith((".env", "id_rsa", "id_ed25519"))
            or any(lower.endswith(suffix) for suffix in _SECRET_SUFFIXES)
        ):
            return True
    return False


_is_secret_path = _is_secret_relpath


def _parse_porcelain_z(raw_porcelain: str) -> tuple[set[str], set[str]]:
    tracked: set[str] = set()
    untracked: set[str] = set()
    tokens = raw_porcelain.split("\x00")
    idx = 0
    while idx < len(tokens):
        entry = tokens[idx]
        idx += 1
        if len(entry) < 4:
            continue
        xy = entry[:2]
        path = entry[3:]
        if "R" in xy or "C" in xy:
            orig_path = tokens[idx] if idx < len(tokens) else ""
            idx += 1
            if path:
                tracked.add(path)
            if orig_path:
                tracked.add(orig_path)
        elif xy == "??":
            if path:
                untracked.add(path)
        else:
            if path:
                tracked.add(path)
    return tracked, untracked


def _validate_stageable_path(
    root: Path,
    rel_path: str,
    allowed_exts: set[str],
    *,
    approved_set: set[str] | None = None,
) -> None:
    norm = _normalize_repo_relpath(rel_path)
    if _is_test_path(norm):
        raise GitRepoError("test_oracle_modified", f"检测到受保护测试路径存在改动，拒绝提交: {norm}")
    if _is_secret_relpath(norm):
        raise GitRepoError("edit_forbidden", f"检测到敏感凭证或密钥文件，拒绝提交: {norm}")
    pure = PurePosixPath(norm)
    if any(part.startswith(".") or part in _FORBIDDEN_HIDDEN_DIRS for part in pure.parts):
        raise GitRepoError("edit_forbidden", f"拒绝提交隐藏路径文件: {norm}")
    if len(pure.parts) == 1 and pure.name.casefold() == "readme.md" and norm not in (approved_set or set()):
        raise GitRepoError("edit_forbidden", f"修改根目录文档需显式列入 approved_paths: {norm}")
    suffix = pure.suffix.casefold()
    if not suffix or suffix not in allowed_exts:
        raise GitRepoError("edit_forbidden", f"不允许提交受限文件类型: {norm}")
    cursor = root
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GitRepoError("edit_forbidden", f"拒绝提交符号链接路径: {norm}")
    if cursor.exists() and cursor.is_file():
        st = cursor.stat()
        if st.st_nlink != 1:
            raise GitRepoError("edit_forbidden", f"拒绝提交多重硬链接文件: {norm}")
        try:
            with open(cursor, "rb") as fp:
                head = fp.read(_BINARY_SNIFF_BYTES)
        except OSError as exc:
            raise GitRepoError("edit_forbidden", f"无法校验待提交文件: {norm}") from exc
        if _is_binary_bytes(head):
            raise GitRepoError("edit_forbidden", f"拒绝提交二进制文件: {norm}")


def commit_verified_worktree(
    workspace: Path,
    base_commit: str,
    verification: dict[str, Any],
    message: str = "fix: apply approved and verified agent changeset",
    allowed_non_py_extensions: tuple[str, ...] = _DEFAULT_ALLOWED_EXTENSIONS,
    allow_untracked: bool = False,
    approved_paths: list[str] | None = None,
) -> dict[str, Any]:
    """仅在容器测试真实通过后提交变更；递归守护测试真值与敏感文件，绝不污染源仓库配置。"""
    if verification.get("status") != "passed" or verification.get("tests_executed") is not True:
        raise GitRepoError(
            "verification_required",
            "测试未通过或验证状态未知，禁止将未验证变更提交到 Git worktree。",
        )
    if not _COMMIT_RE.fullmatch(base_commit):
        raise GitRepoError("invalid_commit", "基线 commit 格式无效。")
    root = _validate_local_dir(workspace, "工作区")
    if not is_git_worktree(root):
        raise GitRepoError("not_git_worktree", "工作区不是 Git worktree。")

    allowed_exts = {
        ".py",
        *(
            ext.casefold() if ext.startswith(".") else f".{ext.casefold()}"
            for ext in allowed_non_py_extensions
            if ext
        ),
    }
    approved_set: set[str] = set()
    for raw_ap in approved_paths or []:
        norm_ap = _normalize_repo_relpath(raw_ap)
        _validate_stageable_path(root, norm_ap, allowed_exts, approved_set={norm_ap})
        approved_set.add(norm_ap)

    head = resolve_commit(root, "HEAD")
    porcelain_normal = _git(["status", "--porcelain", "-z", "--untracked-files=normal"], cwd=root)
    porcelain_all = _git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=root)
    tracked_normal, untracked_normal = _parse_porcelain_z(porcelain_normal)
    tracked_all, untracked_all = _parse_porcelain_z(porcelain_all)
    tracked_vs_head = tracked_normal | tracked_all
    diff_vs_base = {
        p for p in _git(["diff", "--name-only", "-z", base_commit, "--"], cwd=root).split("\x00") if p
    }

    # 递归检查所有已追踪变更、未追踪文件与目录以及相对 base_commit 的全部差异：
    # 任何触碰测试文件或敏感密钥路径的行为一律拦截。
    all_observed = tracked_vs_head | untracked_normal | untracked_all | diff_vs_base
    for observed in sorted(all_observed):
        if _is_test_path(observed):
            raise GitRepoError("test_oracle_modified", f"检测到受保护测试路径存在改动，拒绝提交: {observed}")
        if _is_secret_relpath(observed):
            raise GitRepoError("edit_forbidden", f"检测到敏感凭证或密钥文件改动，拒绝提交: {observed}")

    norm_untracked = {_normalize_repo_relpath(p) for p in untracked_all}
    if allow_untracked:
        untracked_to_stage = norm_untracked
    elif approved_set:
        untracked_to_stage = norm_untracked & approved_set
    else:
        untracked_to_stage = set()

    if head != base_commit and not tracked_vs_head and not untracked_to_stage:
        # 幂等重入：同一 worktree 已经完成提交且工作区干净，直接返回既有 commit。
        files = sorted(
            p for p in _git(["diff", "--name-only", "-z", base_commit, head], cwd=root).split("\x00") if p
        )
        return {
            "ok": True,
            "base_commit": base_commit,
            "commit_sha": head,
            "already_committed": True,
            "files": files,
        }
    if head != base_commit:
        raise GitRepoError("baseline_changed", "worktree HEAD 与记录的基线不一致，拒绝提交。")

    files_to_stage = sorted(
        {_normalize_repo_relpath(p) for p in (tracked_vs_head | diff_vs_base)} | untracked_to_stage
    )
    if not files_to_stage:
        return {
            "ok": True,
            "base_commit": base_commit,
            "commit_sha": head,
            "already_committed": False,
            "no_changes": True,
            "files": [],
        }

    for rel_path in files_to_stage:
        _validate_stageable_path(root, rel_path, allowed_exts, approved_set=approved_set)

    _git(["add", "--", *files_to_stage], cwd=root)
    staged_now = [
        p for p in _git(["diff", "--cached", "--name-only", "-z"], cwd=root).split("\x00") if p
    ]
    if not staged_now:
        return {
            "ok": True,
            "base_commit": base_commit,
            "commit_sha": head,
            "already_committed": False,
            "no_changes": True,
            "files": [],
        }

    clean_msg = " ".join(message.strip().splitlines())[:160] or "fix: apply verified changeset"
    _git(["-c", "commit.gpgsign=false", "commit", "-q", "-m", clean_msg], cwd=root)
    committed_sha = resolve_commit(root, "HEAD")
    committed_files = sorted(
        p for p in _git(["diff", "--name-only", "-z", base_commit, committed_sha], cwd=root).split("\x00") if p
    )
    return {
        "ok": True,
        "base_commit": base_commit,
        "commit_sha": committed_sha,
        "already_committed": False,
        "files": committed_files,
    }


def _materialize_worktree_gitdir(workspace: Path, tmp_parent: Path) -> Path:
    """将 linked worktree 的 .git 指针文件物化为不依赖原宿主路径的独立 .git 目录。"""
    standalone = tmp_parent / "standalone_repo"
    standalone.mkdir(parents=True, exist_ok=True)
    try:
        head_sha = resolve_commit(workspace, "HEAD")
        bundle_path = tmp_parent / "head.bundle"
        _git(["bundle", "create", str(bundle_path), "HEAD"], cwd=workspace)
        _git(["init", "-q", "--template="], cwd=standalone)
        _git(["bundle", "unbundle", str(bundle_path)], cwd=standalone)
        _git(["update-ref", "--no-deref", "HEAD", head_sha], cwd=standalone)
        _git(["read-tree", "HEAD"], cwd=standalone)
        return standalone / ".git"
    except GitRepoError:
        # 若源仓库已被移除导致 worktree gitdir 悬空，回退为基于当前工作区文件的独立快照仓库。
        if standalone.exists():
            _rmtree_force(standalone)
        standalone.mkdir(parents=True, exist_ok=True)
        for item in workspace.iterdir():
            if item.name == ".git":
                continue
            dest = standalone / item.name
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=False)
            elif item.is_file():
                shutil.copy2(item, dest, follow_symlinks=False)
        _git(["init", "-q", "--template="], cwd=standalone)
        _git(["add", "-A"], cwd=standalone)
        _git(
            [
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "chore: materialized workspace snapshot",
            ],
            cwd=standalone,
        )
        return standalone / ".git"


def _add_tree_to_tar(
    tar: tarfile.TarFile,
    base_dir: Path,
    arc_prefix: str,
    *,
    skip_root_git: bool,
    max_bytes: int,
    counters: dict[str, int],
) -> None:
    for current_dir, subdirs, filenames in os.walk(base_dir, topdown=True, followlinks=False):
        cur_path = Path(current_dir)
        rel_dir = cur_path.relative_to(base_dir)
        subdirs.sort()
        filenames.sort()

        if rel_dir == Path(".") and skip_root_git:
            if ".git" in subdirs:
                subdirs.remove(".git")
            if ".git" in filenames:
                filenames.remove(".git")

        # 先用 lstat 检查所有子目录（os.walk 在 followlinks=False 时会把目录符号链接放入 subdirs）
        for subdir in list(subdirs):
            sub_full = cur_path / subdir
            sub_rel = (
                f"{arc_prefix}/{(rel_dir / subdir).as_posix()}"
                if arc_prefix
                else (rel_dir / subdir).as_posix()
            ).strip("/")
            st = os.lstat(sub_full)
            if stat.S_ISLNK(st.st_mode):
                raise GitRepoError("unsafe_archive_entry", f"拒绝打包符号链接目录: {sub_rel}")
            if not stat.S_ISDIR(st.st_mode):
                raise GitRepoError("unsafe_archive_entry", f"拒绝打包特殊目录条目: {sub_rel}")
            counters["entries"] += 1
            if counters["entries"] > _MAX_ARCHIVE_ENTRIES:
                raise GitRepoError("archive_too_large", "工作区文件条目数超过归档安全上限。")
            dir_info = tarfile.TarInfo(name=sub_rel)
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            dir_info.size = 0
            dir_info.mtime = int(st.st_mtime)
            dir_info.uid = 0
            dir_info.gid = 0
            dir_info.uname = ""
            dir_info.gname = ""
            tar.addfile(dir_info)

        for fname in filenames:
            file_full = cur_path / fname
            file_rel = (
                f"{arc_prefix}/{(rel_dir / fname).as_posix()}"
                if arc_prefix
                else (rel_dir / fname).as_posix()
            ).strip("/")
            st = os.lstat(file_full)
            if stat.S_ISLNK(st.st_mode):
                raise GitRepoError("unsafe_archive_entry", f"拒绝打包符号链接文件: {file_rel}")
            if not stat.S_ISREG(st.st_mode):
                raise GitRepoError("unsafe_archive_entry", f"拒绝打包特殊设备或管道文件: {file_rel}")
            counters["entries"] += 1
            if counters["entries"] > _MAX_ARCHIVE_ENTRIES:
                raise GitRepoError("archive_too_large", "工作区文件条目数超过归档安全上限。")
            if counters["bytes"] + st.st_size > max_bytes:
                raise GitRepoError("archive_too_large", "工作区未压缩大小超过 max_bytes 上限。")
            data = file_full.read_bytes()
            counters["bytes"] += len(data)
            if counters["bytes"] > max_bytes:
                raise GitRepoError("archive_too_large", "工作区未压缩大小超过 max_bytes 上限。")
            file_info = tarfile.TarInfo(name=file_rel)
            file_info.type = tarfile.REGTYPE
            file_info.mode = 0o755 if (st.st_mode & 0o111) else 0o644
            file_info.size = len(data)
            file_info.mtime = int(st.st_mtime)
            file_info.uid = 0
            file_info.gid = 0
            file_info.uname = ""
            file_info.gname = ""
            tar.addfile(file_info, io.BytesIO(data))


def pack_workspace_archive(workspace: Path, max_bytes: int = _DEFAULT_ARCHIVE_MAX_BYTES) -> bytes:
    """将工作区打包为内存级可移植 tar.gz；自动物化 worktree .git 指针并严拒符号链接与超限文件。"""
    if max_bytes <= 0:
        raise GitRepoError("invalid_archive_limit", "max_bytes 必须为正整数。")
    root = _validate_local_dir(workspace, "工作区")
    if not root.is_dir():
        raise GitRepoError("workspace_not_found", "工作区目录不存在。")

    git_marker = root / ".git"
    has_git_marker = False
    git_is_worktree_file = False
    try:
        git_st = os.lstat(git_marker)
        has_git_marker = True
        if stat.S_ISLNK(git_st.st_mode):
            raise GitRepoError("unsafe_archive_entry", "拒绝打包符号链接 .git。")
        if stat.S_ISREG(git_st.st_mode):
            git_is_worktree_file = True
        elif not stat.S_ISDIR(git_st.st_mode):
            raise GitRepoError("unsafe_archive_entry", "拒绝打包非法的特殊类型 .git。")
    except FileNotFoundError:
        has_git_marker = False

    buf = io.BytesIO()
    counters = {"bytes": 0, "entries": 0}
    with tempfile.TemporaryDirectory(prefix="agent-lab-pack-") as tmp_str:
        tmp_parent = Path(tmp_str)
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz_fp:
            with tarfile.open(fileobj=gz_fp, mode="w:") as tar:
                _add_tree_to_tar(
                    tar,
                    root,
                    "",
                    skip_root_git=git_is_worktree_file,
                    max_bytes=max_bytes,
                    counters=counters,
                )
                if has_git_marker and git_is_worktree_file:
                    standalone_git = _materialize_worktree_gitdir(root, tmp_parent)
                    counters["entries"] += 1
                    if counters["entries"] > _MAX_ARCHIVE_ENTRIES:
                        raise GitRepoError("archive_too_large", "工作区文件条目数超过归档安全上限。")
                    git_dir_info = tarfile.TarInfo(name=".git")
                    git_dir_info.type = tarfile.DIRTYPE
                    git_dir_info.mode = 0o755
                    git_dir_info.size = 0
                    tar.addfile(git_dir_info)
                    _add_tree_to_tar(
                        tar,
                        standalone_git,
                        ".git",
                        skip_root_git=False,
                        max_bytes=max_bytes,
                        counters=counters,
                    )
                if buf.tell() > max_bytes:
                    raise GitRepoError("archive_too_large", "压缩归档大小超过 max_bytes 上限。")

    archive_bytes = buf.getvalue()
    if len(archive_bytes) > max_bytes:
        raise GitRepoError("archive_too_large", "压缩归档大小超过 max_bytes 上限。")
    return archive_bytes


def _validate_archive_member_path(name: str) -> list[str]:
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or _WIN_DRIVE_RE.match(name):
        raise GitRepoError("unsafe_archive_entry", f"归档包含不安全路径: {name!r}")
    raw_parts = name.split("/")
    if any(part == ".." for part in raw_parts):
        raise GitRepoError("unsafe_archive_entry", f"归档包含上级目录穿越路径: {name!r}")
    parts = [part for part in raw_parts if part not in {"", "."}]
    if not parts:
        raise GitRepoError("unsafe_archive_entry", f"归档包含非法相对路径段: {name!r}")
    return parts


class _BoundedGzipReader(io.RawIOBase):
    """流式限制 gzip 解压字节上限，兼顾防压缩炸弹、低内存峰值与尾部 CRC 校验。"""

    def __init__(self, archive_bytes: bytes, max_stream_bytes: int) -> None:
        try:
            self._gz = gzip.GzipFile(fileobj=io.BytesIO(archive_bytes), mode="rb")
        except (OSError, EOFError, zlib.error) as exc:
            raise GitRepoError("invalid_archive", f"无效的 gzip 归档流: {exc}") from exc
        self._max_stream_bytes = max_stream_bytes
        self._total_read = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        remaining = self._max_stream_bytes + 1 - self._total_read
        to_read = max(1, remaining) if (size is None or size < 0) else min(size, max(1, remaining))
        try:
            chunk = self._gz.read(to_read)
        except (OSError, EOFError, zlib.error) as exc:
            raise GitRepoError("invalid_archive", f"无效的 gzip 归档流: {exc}") from exc
        self._total_read += len(chunk)
        if self._total_read > self._max_stream_bytes:
            raise GitRepoError("archive_too_large", "归档解压流超过 max_bytes 上限（拦截压缩炸弹）。")
        return chunk

    def drain_to_eof(self) -> None:
        while self.read(65536):
            pass

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._gz.close()
        super().close()


def unpack_workspace_archive(
    archive_bytes: bytes,
    destination: Path,
    max_bytes: int = _DEFAULT_ARCHIVE_MAX_BYTES,
) -> None:
    """将内存级 tar.gz 安全原子解压到目标目录，严防符号链接、路径穿越、特殊文件与压缩炸弹。"""
    if max_bytes <= 0:
        raise GitRepoError("invalid_archive_limit", "max_bytes 必须为正整数。")
    if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
        raise GitRepoError("invalid_archive", "归档数据必须是字节序列。")
    if len(archive_bytes) == 0:
        raise GitRepoError("invalid_archive", "归档数据不能为空。")
    if len(archive_bytes) > max_bytes:
        raise GitRepoError("archive_too_large", "压缩归档大小超过 max_bytes 上限。")

    dest = _validate_local_dir(destination, "解压目标目录")
    if dest.exists():
        if not dest.is_dir():
            raise GitRepoError("unsafe_repo_path", "解压目标路径已存在且不是目录。")
        if any(dest.iterdir()):
            raise GitRepoError("destination_not_empty", "解压目标目录非空，拒绝覆盖。")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.parent / f".tmp-unpack-{dest.name}-{uuid.uuid4().hex}"
    tmp_dest.mkdir(parents=True, exist_ok=False)
    tmp_root_resolved = tmp_dest.resolve()
    total_extracted = 0
    entry_count = 0
    max_stream_bytes = max_bytes * 2 + 65536

    try:
        try:
            with contextlib.closing(_BoundedGzipReader(bytes(archive_bytes), max_stream_bytes)) as gz_reader:
                with tarfile.open(fileobj=gz_reader, mode="r|") as tar:
                    for member in tar:
                        entry_count += 1
                        if entry_count > _MAX_ARCHIVE_ENTRIES:
                            raise GitRepoError("archive_too_large", "归档条目数量超过安全上限。")
                        if member.issym() or member.islnk():
                            raise GitRepoError("unsafe_archive_entry", f"归档包含符号链接或硬链接: {member.name!r}")
                        if member.type not in {tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.AREGTYPE}:
                            raise GitRepoError("unsafe_archive_entry", f"归档包含禁止的特殊条目类型: {member.name!r}")
                        if member.name.rstrip("/") == "." and member.isdir():
                            continue
                        parts = _validate_archive_member_path(member.name)
                        if member.size < 0 or member.size > max_bytes:
                            raise GitRepoError("archive_too_large", f"归档单文件大小超限: {member.name!r}")
                        if total_extracted + member.size > max_bytes:
                            raise GitRepoError("archive_too_large", "归档累计提取字节超过 max_bytes 上限。")

                        cursor = tmp_dest
                        for part in parts[:-1]:
                            cursor = cursor / part
                            if cursor.is_symlink():
                                raise GitRepoError("unsafe_archive_entry", "解压路径中间节点存在符号链接。")
                        target_path = cursor / parts[-1]
                        if target_path.is_symlink():
                            raise GitRepoError("unsafe_archive_entry", "解压目标路径存在符号链接。")
                        if not target_path.resolve().is_relative_to(tmp_root_resolved):
                            raise GitRepoError("unsafe_archive_entry", f"解压路径越出目标目录边界: {member.name!r}")

                        if member.isdir():
                            if target_path.exists() and not target_path.is_dir():
                                raise GitRepoError("unsafe_archive_entry", f"归档目录与同名文件冲突: {member.name!r}")
                            target_path.mkdir(parents=True, exist_ok=True)
                            os.chmod(target_path, 0o755)
                        else:
                            if target_path.exists():
                                raise GitRepoError("unsafe_archive_entry", f"归档包含重复覆盖条目: {member.name!r}")
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            src_fp = tar.extractfile(member)
                            if src_fp is None:
                                raise GitRepoError("invalid_archive", f"无法读取归档文件内容: {member.name!r}")
                            written_for_file = 0
                            with src_fp, open(target_path, "wb") as out_fp:
                                while True:
                                    chunk = src_fp.read(65536)
                                    if not chunk:
                                        break
                                    written_for_file += len(chunk)
                                    total_extracted += len(chunk)
                                    if total_extracted > max_bytes:
                                        raise GitRepoError("archive_too_large", "归档提取内容超过 max_bytes 上限。")
                                    out_fp.write(chunk)
                            if written_for_file != member.size:
                                raise GitRepoError("invalid_archive", f"归档文件长度与头部声明不一致: {member.name!r}")
                            os.chmod(target_path, 0o755 if (member.mode & 0o111) else 0o644)
                gz_reader.drain_to_eof()
        except GitRepoError:
            raise
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise GitRepoError("invalid_archive", f"无效或损坏的 tar 归档: {exc}") from exc

        if dest.exists():
            dest.rmdir()
        os.replace(tmp_dest, dest)
    finally:
        if tmp_dest.exists():
            _rmtree_force(tmp_dest)

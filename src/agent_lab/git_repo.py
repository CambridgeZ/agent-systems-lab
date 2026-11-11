"""真实 Git 仓库与独立 Worktree 管理：固定基线 commit、多文件 diff 与受控提交。

设计边界：
1. 每个任务运行在基于固定 base_commit 创建的 detached git worktree 中，互不污染。
2. 只操作本地显式指定的真实目录，不自动联网 clone 任意远端 URL。
3. 只有补丁经过审批且容器验收测试明确返回 passed 后，才允许生成 Git 提交。
4. 提交前额外校验受保护的 tests/ 目录相对基线零改动，防止篡改验收条件。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Agent Systems Lab",
    "GIT_AUTHOR_EMAIL": "agent-lab@local.invalid",
    "GIT_COMMITTER_NAME": "Agent Systems Lab",
    "GIT_COMMITTER_EMAIL": "agent-lab@local.invalid",
    "LC_ALL": "C",
}


class GitRepoError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
    try:
        completed = subprocess.run(
            [git_bin, *args],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitRepoError("git_timeout", "git 操作超时。") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        raise GitRepoError("git_command_failed", f"git {' '.join(args[:2])} 失败: {detail}")
    return completed.stdout


def _validate_local_dir(path: Path, label: str) -> Path:
    candidate = Path(path)
    if "://" in str(candidate) or ".." in candidate.parts:
        raise GitRepoError("unsafe_repo_path", f"{label} 不能是 URL 或包含 ..")
    if candidate.exists() and candidate.is_symlink():
        raise GitRepoError("unsafe_repo_path", f"{label} 不能是符号链接")
    return candidate.parent.resolve() / candidate.name


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
    _git(["init", "-q"], cwd=target)
    _git(["config", "commit.gpgsign", "false"], cwd=target)
    _git(["add", "-A"], cwd=target)
    _git(["commit", "-q", "-m", "chore: initial baseline"], cwd=target)
    return resolve_commit(target, "HEAD")


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
    """在指定基线 commit 上创建独立 detached worktree，避免多任务共享工作目录。"""
    repo = _validate_local_dir(repo_dir, "源仓库")
    worktree = _validate_local_dir(worktree_dir, "任务 worktree")
    if worktree.exists():
        raise GitRepoError("worktree_exists", "任务 worktree 路径已存在。")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    base_commit = resolve_commit(repo, ref)
    _git(["worktree", "add", "--detach", "-q", str(worktree), base_commit], cwd=repo)
    _git(["config", "commit.gpgsign", "false"], cwd=worktree)
    return {
        "enabled": True,
        "repo_fingerprint": base_commit[:12],
        "base_commit": base_commit,
        "head_commit": base_commit,
        "committed_sha": None,
    }


def is_git_worktree(workspace: Path) -> bool:
    marker = Path(workspace) / ".git"
    return marker.is_file() or marker.is_dir()


def worktree_status(workspace: Path, base_commit: str | None = None, max_diff_chars: int = 8000) -> dict[str, Any]:
    """只读查看当前工作区的 Git 基线、修改文件列表和有界 unified diff。"""
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
    raw_diff = _git(["diff", "--no-ext-diff", anchor, "--"], cwd=root)
    truncated = len(raw_diff) > max_diff_chars
    return {
        "ok": True,
        "is_git": True,
        "base_commit": anchor,
        "head_commit": head,
        "baseline_matches_head": head == anchor,
        "dirty": bool(changed),
        "changed_files": changed[:64],
        "diff": raw_diff[:max_diff_chars],
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


def commit_verified_worktree(
    workspace: Path,
    base_commit: str,
    verification: dict[str, Any],
    message: str = "fix: apply approved and verified agent changeset",
) -> dict[str, Any]:
    """仅在容器测试真实通过后提交变更；验证失败、未知或未执行一律拒绝提交。"""
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
    head = resolve_commit(root, "HEAD")
    porcelain = _git(["status", "--porcelain", "--untracked-files=no"], cwd=root).strip()
    if head != base_commit and not porcelain:
        # 幂等重入：同一 worktree 已经完成提交且工作区干净，直接返回既有 commit。
        files = [
            line.strip()
            for line in _git(["diff", "--name-only", base_commit, head], cwd=root).splitlines()
            if line.strip()
        ]
        return {
            "ok": True,
            "base_commit": base_commit,
            "commit_sha": head,
            "already_committed": True,
            "files": files,
        }
    if head != base_commit:
        raise GitRepoError("baseline_changed", "worktree HEAD 与记录的基线不一致，拒绝提交。")
    test_diff = _git(["diff", "--name-only", base_commit, "--", "tests"], cwd=root).strip()
    if test_diff:
        raise GitRepoError("test_oracle_modified", "检测到受保护 tests 目录存在差异，拒绝提交。")
    tracked_changes = [
        line[3:].strip()
        for line in _git(["status", "--porcelain", "--untracked-files=no"], cwd=root).splitlines()
        if len(line) >= 4
    ]
    if not tracked_changes:
        return {
            "ok": True,
            "base_commit": base_commit,
            "commit_sha": head,
            "already_committed": False,
            "no_changes": True,
            "files": [],
        }
    if any(not path.endswith(".py") or path.startswith("tests/") for path in tracked_changes):
        raise GitRepoError("edit_forbidden", "只允许提交非测试目录的已追踪 Python 文件。")
    clean_msg = " ".join(message.strip().splitlines())[:160] or "fix: apply verified changeset"
    _git(["add", "--update", "--", *tracked_changes], cwd=root)
    _git(["commit", "-q", "-m", clean_msg], cwd=root)
    committed_sha = resolve_commit(root, "HEAD")
    return {
        "ok": True,
        "base_commit": base_commit,
        "commit_sha": committed_sha,
        "already_committed": False,
        "files": tracked_changes,
    }

"""Git 高可用原语测试：进程组超时回收、零配置污染、并发原子冷启动、递归测试/密钥防护、Worktree GC 与跨节点安全归档。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import threading
import time

import pytest

from agent_lab.api import ROOT
from agent_lab.git_repo import (
    GitRepoError,
    _git,
    cleanup_task_worktree,
    commit_verified_worktree,
    create_task_worktree,
    ensure_git_origin_atomic,
    init_git_repo_from_dir,
    pack_workspace_archive,
    resolve_commit,
    unpack_workspace_archive,
    worktree_status,
)


def test_git_timeout_kills_entire_process_group_without_leaking_grandchild(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo_from_dir(ROOT / "data/sample_repo", repo)
    pid_file = tmp_path / "grandchild.pid"
    helper = tmp_path / "hang_helper.py"
    # 通过 git alias 派生后台孙进程并保持标准流管道打开；若不按进程组强杀会导致 communicate 死锁且孙进程残留。
    helper.write_text(
        "import pathlib, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    hang_cmd = f"!{shlex.quote(sys.executable)} {shlex.quote(str(helper))}"
    started = time.monotonic()
    with pytest.raises(GitRepoError) as exc_info:
        _git(["-c", f"alias.hang={hang_cmd}", "hang"], cwd=repo, timeout=0.6)
    elapsed = time.monotonic() - started
    assert exc_info.value.code == "git_timeout"
    assert elapsed < 5.0

    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(30):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_host_git_config_untouched_across_init_worktree_and_commit(tmp_path: Path) -> None:
    host_repo = tmp_path / "developer_repo"
    base_commit = init_git_repo_from_dir(ROOT / "data/multi_file_repo", host_repo)
    git_config = host_repo / ".git" / "config"
    assert "gpgsign" not in git_config.read_text(encoding="utf-8")

    # 模拟真实开发者仓库开启了 GPG 签名且指向不可用的 gpg 程序，验证命令行临时 -c 生效且绝不改写 .git/config。
    custom_marker = (
        "\n[commit]\n\tgpgsign = true\n"
        "[gpg]\n\tprogram = /nonexistent/gpg-binary-should-never-be-called\n"
        "[agentlab]\n\tsentinel = keep-exact-bytes\n"
    )
    git_config.write_text(git_config.read_text(encoding="utf-8") + custom_marker, encoding="utf-8")
    before_bytes = git_config.read_bytes()

    worktree = tmp_path / "wt_clean_cfg"
    meta = create_task_worktree(host_repo, worktree, ref=base_commit)
    assert meta["base_commit"] == base_commit
    assert git_config.read_bytes() == before_bytes

    target_py = worktree / "discounts.py"
    target_py.write_text(target_py.read_text(encoding="utf-8") + "\n# safe verified fix\n", encoding="utf-8")
    committed = commit_verified_worktree(
        worktree,
        base_commit,
        {"status": "passed", "tests_executed": True},
        message="fix: commit without mutating host git config",
    )
    assert committed["ok"] is True
    assert committed["commit_sha"] != base_commit
    assert git_config.read_bytes() == before_bytes


def test_concurrent_multithread_ensure_git_origin_atomic_and_broken_recovery(tmp_path: Path) -> None:
    origin = tmp_path / "shared_origins" / "multi_file"
    worker_count = 12
    barrier = threading.Barrier(worker_count)

    def _init_worker() -> str:
        barrier.wait(timeout=5.0)
        return ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_init_worker) for _ in range(worker_count)]
        commits = [f.result(timeout=15.0) for f in futures]

    assert len(set(commits)) == 1
    expected_sha = commits[0]
    assert resolve_commit(origin, "HEAD") == expected_sha
    leftover_tmps = [p.name for p in origin.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftover_tmps == []

    # 模拟在外层 Git 仓库内部遗留无有效 HEAD 的半成品目录，绝不能误取外层仓库 HEAD，必须清理并独立重建。
    outer_repo = tmp_path / "outer_git_repo"
    outer_sha = init_git_repo_from_dir(ROOT / "data/multi_file_repo", outer_repo)
    broken = outer_repo / "nested_state" / "broken_origin"
    (broken / ".git").mkdir(parents=True)
    (broken / "partial.txt").write_text("corrupted cold start", encoding="utf-8")
    recovered_sha = ensure_git_origin_atomic(ROOT / "data/sample_repo", broken)
    assert len(recovered_sha) == 40
    assert recovered_sha != outer_sha
    assert resolve_commit(broken, "HEAD") == recovered_sha
    assert not (broken / "partial.txt").exists()


def test_commit_rejects_nested_tests_secrets_and_root_docs(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    base_commit = ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin)
    ver_ok = {"status": "passed", "tests_executed": True}

    # 1. 递归测试路径拦截（嵌套 subpkg/tests/test_x.py、conftest.py、*_test.py、test_*.py）
    for idx, rel_tamper in enumerate(
        (
            "subpkg/tests/test_x.py",
            "deep/pkg/test/helper.py",
            "subpkg/conftest.py",
            "subpkg/checkout_test.py",
            "subpkg/test_inline.py",
        )
    ):
        wt = tmp_path / f"wt_test_tamper_{idx}"
        create_task_worktree(origin, wt, ref=base_commit)
        (wt / "discounts.py").write_text(
            (wt / "discounts.py").read_text(encoding="utf-8") + "\n# code change\n",
            encoding="utf-8",
        )
        tamper_file = wt / rel_tamper
        tamper_file.parent.mkdir(parents=True, exist_ok=True)
        tamper_file.write_text("def test_bypass(): assert True\n", encoding="utf-8")
        with pytest.raises(GitRepoError) as exc_info:
            commit_verified_worktree(wt, base_commit, ver_ok)
        assert exc_info.value.code == "test_oracle_modified"
        assert resolve_commit(wt, "HEAD") == base_commit

    # 2. 敏感凭证与私钥文件拦截（含跨扩展名 stem 匹配：.json/.yaml/.toml/.cfg/.ini/.txt/.md/.py）
    for idx, secret_rel in enumerate(
        (
            "secrets.py",
            "secrets.json",
            "subpkg/credentials.py",
            "config/credentials.yaml",
            ".env",
            "config/.env.production",
            "certs/server.pem",
            "keys/private.key",
            "certs/bundle.p12",
            "tokens.py",
            "tokens.toml",
            "private_key.cfg",
            "api_key.ini",
        )
    ):
        wt = tmp_path / f"wt_secret_{idx}"
        create_task_worktree(origin, wt, ref=base_commit)
        (wt / "discounts.py").write_text(
            (wt / "discounts.py").read_text(encoding="utf-8") + "\n# code change\n",
            encoding="utf-8",
        )
        sec_file = wt / secret_rel
        sec_file.parent.mkdir(parents=True, exist_ok=True)
        sec_file.write_text("DUMMY='not-allowed'\n", encoding="utf-8")
        with pytest.raises(GitRepoError) as exc_info:
            commit_verified_worktree(wt, base_commit, ver_ok)
        assert exc_info.value.code == "edit_forbidden"
        assert resolve_commit(wt, "HEAD") == base_commit

    # 3. 根目录文档 README.md 修改仍被拦截为 edit_forbidden
    wt_doc = tmp_path / "wt_readme"
    create_task_worktree(origin, wt_doc, ref=base_commit)
    (wt_doc / "README.md").write_text("# unauthorized doc edit\n", encoding="utf-8")
    with pytest.raises(GitRepoError) as exc_info:
        commit_verified_worktree(wt_doc, base_commit, ver_ok)
    assert exc_info.value.code == "edit_forbidden"


def test_approved_paths_new_files_config_files_and_deletions_commit_cleanly(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    base_commit = ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin)
    wt = tmp_path / "wt_rich_commit"
    create_task_worktree(origin, wt, ref=base_commit)

    # 修改已追踪 Python 文件 + 删除已追踪 models.py + 新增受批模块与配置文件 + 放置未授权杂项文件
    (wt / "discounts.py").write_text(
        (wt / "discounts.py").read_text(encoding="utf-8") + "\n# updated logic\n",
        encoding="utf-8",
    )
    (wt / "models.py").unlink()
    (wt / "subpkg").mkdir(parents=True, exist_ok=True)
    (wt / "subpkg" / "rules.py").write_text("MAX_RATE = 50\n", encoding="utf-8")
    (wt / "pyproject.toml").write_text("[tool.demo]\nstrict = true\n", encoding="utf-8")
    (wt / "config").mkdir(parents=True, exist_ok=True)
    (wt / "config" / "limits.yaml").write_text("cap: 100\n", encoding="utf-8")
    # 未列入 approved_paths 的随机未追踪文件不应被意外提交
    (wt / "unapproved_scratch.py").write_text("TEMP = 1\n", encoding="utf-8")
    (wt / "random_notes.txt").write_text("local scratch\n", encoding="utf-8")

    ver_ok = {"status": "passed", "tests_executed": True}
    committed = commit_verified_worktree(
        wt,
        base_commit,
        ver_ok,
        message="feat: add rules module and config",
        approved_paths=["subpkg/rules.py", "pyproject.toml", "config/limits.yaml"],
    )
    assert committed["ok"] is True
    assert committed["already_committed"] is False
    assert committed["commit_sha"] != base_commit
    assert committed["files"] == [
        "config/limits.yaml",
        "discounts.py",
        "models.py",
        "pyproject.toml",
        "subpkg/rules.py",
    ]

    # 幂等重入校验（未授权未追踪文件仍留在工作区但不触发二次提交）
    reentered = commit_verified_worktree(
        wt,
        base_commit,
        ver_ok,
        approved_paths=["subpkg/rules.py", "pyproject.toml", "config/limits.yaml"],
    )
    assert reentered["ok"] is True
    assert reentered["already_committed"] is True
    assert reentered["commit_sha"] == committed["commit_sha"]
    assert reentered["files"] == committed["files"]

    # allow_untracked=True 时自动暂存新建合法文件与配置，无需逐一列入 approved_paths
    wt_auto = tmp_path / "wt_allow_untracked"
    create_task_worktree(origin, wt_auto, ref=base_commit)
    (wt_auto / "new_util.py").write_text("FLAG = True\n", encoding="utf-8")
    (wt_auto / "settings.json").write_text('{"strict": true}\n', encoding="utf-8")
    auto_committed = commit_verified_worktree(
        wt_auto,
        base_commit,
        ver_ok,
        allow_untracked=True,
    )
    assert auto_committed["ok"] is True
    assert auto_committed["files"] == ["new_util.py", "settings.json"]


def test_cleanup_task_worktree_removes_directory_and_prunes_origin(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    base_commit = ensure_git_origin_atomic(ROOT / "data/sample_repo", origin)
    wt = tmp_path / "worktrees" / "task_01"
    create_task_worktree(origin, wt, ref=base_commit)

    listed_before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(wt.resolve()) in listed_before

    assert cleanup_task_worktree(wt, origin) is True
    assert not wt.exists()

    listed_after = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(wt.resolve()) not in listed_after
    worktrees_admin = origin / ".git" / "worktrees"
    assert not worktrees_admin.exists() or list(worktrees_admin.iterdir()) == []

    # 重复清理幂等返回 False
    assert cleanup_task_worktree(wt, origin) is False


def test_pack_and_unpack_workspace_archive_portable_cross_node_roundtrip(tmp_path: Path) -> None:
    node_a = tmp_path / "node_a"
    origin_a = node_a / "origin"
    wt_a = node_a / "wt_task"
    base_commit = ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin_a)
    create_task_worktree(origin_a, wt_a, ref=base_commit)
    assert (wt_a / ".git").is_file()

    # 在节点 A 的 worktree 中产生未提交修改与新文件后打包
    (wt_a / "discounts.py").write_text(
        (wt_a / "discounts.py").read_text(encoding="utf-8") + "\n# edited on node A\n",
        encoding="utf-8",
    )
    (wt_a / "portable_helper.py").write_text("PORTABLE = True\n", encoding="utf-8")

    archive_to_worker = pack_workspace_archive(wt_a)
    assert isinstance(archive_to_worker, bytes) and len(archive_to_worker) > 0

    # 彻底删除节点 A 的源仓库与 worktree，模拟跨机器无共享文件系统的远端节点 B
    cleanup_task_worktree(wt_a, origin_a)
    subprocess.run(["rm", "-rf", str(node_a)], check=True)
    assert not origin_a.exists()

    node_b_ws = tmp_path / "node_b" / "isolated_workspace"
    unpack_workspace_archive(archive_to_worker, node_b_ws)
    assert (node_b_ws / ".git").is_dir()
    assert resolve_commit(node_b_ws, "HEAD") == base_commit

    status_b = worktree_status(node_b_ws, base_commit=base_commit)
    assert status_b["is_git"] is True
    assert status_b["dirty"] is True
    assert "# edited on node A" in status_b["diff"]

    committed_b = commit_verified_worktree(
        node_b_ws,
        base_commit,
        {"status": "passed", "tests_executed": True},
        message="fix: verified on remote worker node B",
        approved_paths=["portable_helper.py"],
    )
    assert committed_b["ok"] is True
    assert committed_b["files"] == ["discounts.py", "portable_helper.py"]

    # 从节点 B 重新打包传回节点 C 解压，验证独立 .git 提交历史与文件完整保留
    archive_back = pack_workspace_archive(node_b_ws)
    node_c_ws = tmp_path / "node_c" / "returned_workspace"
    unpack_workspace_archive(archive_back, node_c_ws)
    assert resolve_commit(node_c_ws, "HEAD") == committed_b["commit_sha"]
    assert (node_c_ws / "portable_helper.py").read_text(encoding="utf-8") == "PORTABLE = True\n"
    status_c = worktree_status(node_c_ws, base_commit=base_commit)
    assert status_c["dirty"] is False
    assert status_c["baseline_matches_head"] is False
    assert "# edited on node A" in status_c["diff"]

    # 小配额 max_bytes 下可正常完成打包与流式解压往返
    small_origin = tmp_path / "small_origin"
    small_sha = ensure_git_origin_atomic(ROOT / "data/sample_repo", small_origin)
    small_blob = pack_workspace_archive(small_origin, max_bytes=16 * 1024)
    small_dest = tmp_path / "small_unpacked"
    unpack_workspace_archive(small_blob, small_dest, max_bytes=16 * 1024)
    assert resolve_commit(small_dest, "HEAD") == small_sha


def _build_custom_tar_gz(mutator) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz_fp:
        with tarfile.open(fileobj=gz_fp, mode="w:") as tar:
            mutator(tar)
    return buf.getvalue()


def test_archive_rejects_symlinks_traversal_special_files_and_zip_bombs(tmp_path: Path) -> None:
    # 1. 打包阶段拒绝工作区符号链接
    ws_with_symlink = tmp_path / "ws_symlink"
    ws_with_symlink.mkdir()
    (ws_with_symlink / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (ws_with_symlink / "leak.py").symlink_to("/etc/hosts")
    with pytest.raises(GitRepoError) as exc_info:
        pack_workspace_archive(ws_with_symlink)
    assert exc_info.value.code == "unsafe_archive_entry"

    # 2. 解压阶段拒绝上级目录穿越 ..、绝对路径、符号链接、硬链接、FIFO 特殊文件，且不残留半成品目录
    def _add_traversal(tar: tarfile.TarFile) -> None:
        ok = tarfile.TarInfo("good.py")
        ok.size = 6
        tar.addfile(ok, io.BytesIO(b"x = 1\n"))
        bad = tarfile.TarInfo("../escape.py")
        bad.size = 6
        tar.addfile(bad, io.BytesIO(b"x = 2\n"))

    def _add_abs(tar: tarfile.TarFile) -> None:
        bad = tarfile.TarInfo("/etc/evil.py")
        bad.size = 6
        tar.addfile(bad, io.BytesIO(b"x = 2\n"))

    def _add_symlink(tar: tarfile.TarFile) -> None:
        sym = tarfile.TarInfo("evil_link")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tar.addfile(sym)

    def _add_hardlink(tar: tarfile.TarFile) -> None:
        hl = tarfile.TarInfo("evil_hardlink")
        hl.type = tarfile.LNKTYPE
        hl.linkname = "/etc/passwd"
        tar.addfile(hl)

    def _add_fifo(tar: tarfile.TarFile) -> None:
        fifo = tarfile.TarInfo("evil_pipe")
        fifo.type = tarfile.FIFOTYPE
        tar.addfile(fifo)

    for idx, builder in enumerate((_add_traversal, _add_abs, _add_symlink, _add_hardlink, _add_fifo)):
        malicious_blob = _build_custom_tar_gz(builder)
        dest = tmp_path / f"reject_dest_{idx}"
        with pytest.raises(GitRepoError) as exc_info:
            unpack_workspace_archive(malicious_blob, dest)
        assert exc_info.value.code == "unsafe_archive_entry"
        assert not dest.exists()
        assert not (tmp_path / "escape.py").exists()

    # 3. 拦截高压缩比 gzip/tar 炸弹（压缩后仅几百字节，解压后超过 max_bytes）
    def _add_bomb(tar: tarfile.TarFile) -> None:
        payload = b"0" * (256 * 1024)
        info = tarfile.TarInfo("bomb.py")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    bomb_blob = _build_custom_tar_gz(_add_bomb)
    assert len(bomb_blob) < 4096
    bomb_dest = tmp_path / "bomb_dest"
    with pytest.raises(GitRepoError) as exc_info:
        unpack_workspace_archive(bomb_blob, bomb_dest, max_bytes=32 * 1024)
    assert exc_info.value.code == "archive_too_large"
    assert not bomb_dest.exists()

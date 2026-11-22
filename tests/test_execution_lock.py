"""本机 flock 的可观察行为：独立进程互斥、立即拒绝、退出释放及路径防护。"""

from contextlib import contextmanager
import hashlib
import os
import select
import subprocess
import sys
import time

import pytest

from agent_lab.execution_lock import RunBusy, cleanup_stale_locks, exclusive_run


_HOLDER = """
from pathlib import Path
import sys
from agent_lab.execution_lock import exclusive_run
with exclusive_run(Path(sys.argv[1]), sys.argv[2]):
    print("locked", flush=True)
    sys.stdin.buffer.read(1)
"""


@contextmanager
def child_holder(database, run_id):
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(database), run_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "子进程没有在时限内报告已持锁"
        assert process.stdout.readline().strip() == "locked"
        yield process
    finally:
        if process.poll() is None:
            try:
                process.communicate("x", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        else:
            process.communicate(timeout=10)


def lock_file(database, run_id):
    name = hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".lock"
    return database.parent / "locks" / name


def test_same_run_is_nonblocking_even_with_separate_file_descriptors(tmp_path):
    database = tmp_path / "state.db"
    with exclusive_run(database, "run-1"):
        with pytest.raises(RunBusy):
            with exclusive_run(database, "run-1"):
                pytest.fail("不能同时进入同一 run")
    with exclusive_run(database, "run-1"):
        assert lock_file(database, "run-1").is_file()
    # 保留同一 inode，避免删除锁文件后两个进程各自锁住不同对象。
    assert lock_file(database, "run-1").is_file()


def test_different_runs_can_enter_concurrently(tmp_path):
    database = tmp_path / "state.db"
    with exclusive_run(database, "run-1"):
        with exclusive_run(database, "run-2"):
            assert lock_file(database, "run-1") != lock_file(database, "run-2")


def test_exception_releases_lock_without_removing_lock_file(tmp_path):
    database = tmp_path / "state.db"
    with pytest.raises(RuntimeError, match="test failure"):
        with exclusive_run(database, "run-1"):
            raise RuntimeError("test failure")
    with exclusive_run(database, "run-1"):
        assert lock_file(database, "run-1").is_file()


def test_same_run_is_exclusive_across_independent_processes(tmp_path):
    database = tmp_path / "state.db"
    with child_holder(database, "run-1") as process:
        with pytest.raises(RunBusy):
            with exclusive_run(database, "run-1"):
                pytest.fail("独立进程持锁时不能进入")
        # 同一进程正在执行 run-1，不妨碍另一个 run 的实际执行入口。
        with exclusive_run(database, "run-2"):
            assert process.poll() is None
    with exclusive_run(database, "run-1"):
        pass


def test_kernel_releases_lock_after_holder_process_is_killed(tmp_path):
    database = tmp_path / "state.db"
    with child_holder(database, "run-1") as process:
        with pytest.raises(RunBusy):
            with exclusive_run(database, "run-1"):
                pass
        process.kill()
        process.wait(timeout=10)
        # 没有 Python finally 执行机会，仍由内核在关闭进程描述符后释放。
        with exclusive_run(database, "run-1"):
            assert process.returncode != 0


@pytest.mark.parametrize("run_id", ["", "../run", "/etc/passwd", "x/y", "x" * 129, None, 1, "任务"])
def test_run_id_cannot_be_used_as_arbitrary_file_path(tmp_path, run_id):
    with pytest.raises(ValueError, match="run_id"):
        with exclusive_run(tmp_path / "state.db", run_id):
            pass
    assert not (tmp_path / "locks").exists()


def test_symlink_in_database_parent_path_is_rejected(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        with exclusive_run(link / "state.db", "run-1"):
            pass
    assert not (target / "locks").exists()


def test_symlink_database_file_is_rejected(tmp_path):
    actual = tmp_path / "actual.db"
    actual.write_text("database")
    database = tmp_path / "state.db"
    database.symlink_to(actual)
    with pytest.raises(ValueError, match="符号链接"):
        with exclusive_run(database, "run-1"):
            pass


def test_symlink_locks_directory_is_rejected(tmp_path):
    actual = tmp_path / "external"
    actual.mkdir()
    (tmp_path / "locks").symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        with exclusive_run(tmp_path / "state.db", "run-1"):
            pass
    assert list(actual.iterdir()) == []


def test_symlink_lock_file_is_rejected_without_touching_target(tmp_path):
    database = tmp_path / "state.db"
    (tmp_path / "locks").mkdir()
    actual = tmp_path / "external.txt"
    actual.write_text("unchanged")
    lock_file(database, "run-1").symlink_to(actual)
    with pytest.raises(ValueError, match="符号链接"):
        with exclusive_run(database, "run-1"):
            pass
    assert actual.read_text() == "unchanged"


def test_hardlinked_lock_file_is_rejected(tmp_path):
    database = tmp_path / "state.db"
    (tmp_path / "locks").mkdir()
    actual = tmp_path / "external.txt"
    actual.write_text("unchanged")
    os.link(actual, lock_file(database, "run-1"))
    with pytest.raises(ValueError, match="硬链接"):
        with exclusive_run(database, "run-1"):
            pass


def test_directory_in_place_of_lock_file_is_rejected(tmp_path):
    database = tmp_path / "state.db"
    lock_file(database, "run-1").mkdir(parents=True)
    with pytest.raises(ValueError, match="普通文件"):
        with exclusive_run(database, "run-1"):
            pass


def test_parent_traversal_is_rejected_instead_of_silently_normalized(tmp_path):
    with pytest.raises(ValueError, match="不能包含"):
        with exclusive_run(tmp_path / "anything/../state.db", "run-1"):
            pass


def test_database_parent_must_exist_but_database_file_may_be_new(tmp_path):
    with exclusive_run(tmp_path / "new.db", "run-1"):
        assert not (tmp_path / "new.db").exists()
    with pytest.raises(FileNotFoundError):
        with exclusive_run(tmp_path / "missing/state.db", "run-1"):
            pass


@pytest.mark.parametrize("disable_o_path", [False, True])
@pytest.mark.parametrize("ancestor_mode", [0o111, 0o711])
def test_execute_only_ancestor_allows_exclusive_run_and_rejects_symlinks(
    tmp_path, monkeypatch, disable_o_path, ancestor_mode
):
    if disable_o_path:
        monkeypatch.delattr(os, "O_PATH", raising=False)
    ancestor = tmp_path / "restricted_ancestor"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    real_target = tmp_path / "real_target"
    (real_target / "nested").mkdir(parents=True)
    direct_link = ancestor / "alias"
    direct_link.symlink_to(real_target, target_is_directory=True)
    (ancestor / "plain_file").write_text("not a dir")

    ancestor.chmod(ancestor_mode)
    try:
        if ancestor_mode == 0o111 and os.geteuid() != 0:
            with pytest.raises(PermissionError):
                os.listdir(ancestor)

        database = workspace / "state.db"
        with child_holder(database, "run-1"):
            with pytest.raises(RunBusy):
                with exclusive_run(database, "run-1"):
                    pass
        with exclusive_run(database, "run-1"):
            assert lock_file(database, "run-1").is_file()

        with pytest.raises(ValueError, match="符号链接"):
            with exclusive_run(direct_link / "state.db", "run-1"):
                pass
        with pytest.raises(ValueError, match="符号链接"):
            with exclusive_run(direct_link / "nested" / "state.db", "run-1"):
                pass
        assert not (real_target / "locks").exists()
        assert not (real_target / "nested" / "locks").exists()

        with pytest.raises(ValueError, match="非目录"):
            with exclusive_run(ancestor / "plain_file" / "state.db", "run-1"):
                pass
        with pytest.raises(FileNotFoundError):
            with exclusive_run(ancestor / "missing" / "state.db", "run-1"):
                pass
    finally:
        ancestor.chmod(0o700)


def test_o_path_is_used_for_intermediate_ancestors_and_rdonly_for_final_parent(tmp_path, monkeypatch):
    real_open = os.open
    synthetic_o_path = getattr(os, "O_PATH", 0x200000)
    has_native_o_path = hasattr(os, "O_PATH")
    if not has_native_o_path:
        monkeypatch.setattr(os, "O_PATH", synthetic_o_path, raising=False)

    opened = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        opened.append((os.fspath(path), flags, dir_fd))
        effective_flags = flags if has_native_o_path else (flags & ~synthetic_o_path)
        if dir_fd is None:
            return real_open(path, effective_flags, mode)
        return real_open(path, effective_flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    nested_dir = tmp_path / "level1" / "final_parent"
    nested_dir.mkdir(parents=True)
    with exclusive_run(nested_dir / "state.db", "run-1"):
        pass

    level1_flags = [flags for name, flags, _ in opened if name == "level1"]
    final_flags = [flags for name, flags, _ in opened if name == "final_parent"]
    assert level1_flags and (level1_flags[0] & synthetic_o_path) == synthetic_o_path
    assert final_flags and (final_flags[0] & synthetic_o_path) == 0


def test_cleanup_stale_locks_removes_aged_unlocked_locks_only(tmp_path):
    database = tmp_path / "state.db"
    for rid in ("stale-1", "stale-2", "fresh-1"):
        with exclusive_run(database, rid):
            pass

    old_ts = time.time() - 7200.0
    os.utime(lock_file(database, "stale-1"), (old_ts, old_ts))
    os.utime(lock_file(database, "stale-2"), (old_ts, old_ts))

    removed = cleanup_stale_locks(database, max_age_seconds=3600.0)
    assert removed == 2
    assert not lock_file(database, "stale-1").exists()
    assert not lock_file(database, "stale-2").exists()
    assert lock_file(database, "fresh-1").is_file()

    with exclusive_run(database, "stale-1"):
        assert lock_file(database, "stale-1").is_file()


def test_cleanup_stale_locks_never_removes_held_flocks_or_active_run_ids(tmp_path):
    database = tmp_path / "state.db"
    for rid in ("active-id", "stale-unlocked"):
        with exclusive_run(database, rid):
            pass

    old_ts = time.time() - 7200.0
    with child_holder(database, "held-child"):
        with exclusive_run(database, "held-inproc"):
            child_ino = lock_file(database, "held-child").stat().st_ino
            inproc_ino = lock_file(database, "held-inproc").stat().st_ino
            for rid in ("held-child", "held-inproc", "active-id", "stale-unlocked"):
                os.utime(lock_file(database, rid), (old_ts, old_ts))

            removed = cleanup_stale_locks(
                database,
                active_run_ids={"active-id"},
                max_age_seconds=3600.0,
            )
            assert removed == 1
            assert not lock_file(database, "stale-unlocked").exists()
            assert lock_file(database, "active-id").is_file()
            assert lock_file(database, "held-child").stat().st_ino == child_ino
            assert lock_file(database, "held-inproc").stat().st_ino == inproc_ino
            with pytest.raises(RunBusy):
                with exclusive_run(database, "held-child"):
                    pass


def test_cleanup_stale_locks_handles_missing_dir_and_untrusted_entries(tmp_path):
    database = tmp_path / "state.db"
    assert cleanup_stale_locks(database) == 0
    with pytest.raises(FileNotFoundError):
        cleanup_stale_locks(tmp_path / "missing" / "state.db")

    with exclusive_run(database, "stale-valid"):
        pass
    locks_dir = tmp_path / "locks"
    external = tmp_path / "external.txt"
    external.write_text("keep")
    notes = locks_dir / "notes.txt"
    notes.write_text("ignore")
    sym_lock = locks_dir / "sym.lock"
    sym_lock.symlink_to(external)
    hard_lock = locks_dir / "hard.lock"
    os.link(external, hard_lock)
    dir_lock = locks_dir / "dir.lock"
    dir_lock.mkdir()

    old_ts = time.time() - 7200.0
    for path in (lock_file(database, "stale-valid"), external, notes):
        os.utime(path, (old_ts, old_ts))

    assert cleanup_stale_locks(database, max_age_seconds=3600.0) == 1
    assert not lock_file(database, "stale-valid").exists()
    assert external.read_text() == "keep"
    assert notes.is_file()
    assert sym_lock.is_symlink()
    assert hard_lock.is_file()
    assert dir_lock.is_dir()


def test_cleanup_stale_locks_rejects_symlink_locks_directory(tmp_path):
    actual = tmp_path / "external_locks"
    actual.mkdir()
    (tmp_path / "locks").symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        cleanup_stale_locks(tmp_path / "state.db")


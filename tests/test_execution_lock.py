"""本机 flock 的可观察行为：独立进程互斥、立即拒绝、退出释放及路径防护。"""

from contextlib import contextmanager
import hashlib
import os
import select
import subprocess
import sys

import pytest

from agent_lab.execution_lock import RunBusy, exclusive_run


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

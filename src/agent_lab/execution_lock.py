"""本机任务互斥：非阻塞 flock，进程退出后由内核释放持有的锁。

只适用于同一台 POSIX 主机上的本地文件系统。它不是分布式租约，也不提供
fencing token；多个主机、网络文件系统或外部系统副作用仍需要独立的协调协议。
锁文件必须保留：删除后重建会产生另一个 inode，可能让两个执行者分别持锁。
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Iterator


class RunBusy(RuntimeError):
    """同一 run 已被本机另一个执行者持有；调用方应返回冲突，不排队等待。"""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory(name: str, parent_fd: int | None = None) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("执行锁目录不允许符号链接或非目录路径") from exc
        raise


@contextmanager
def _database_parent(storage_path: Path) -> Iterator[int]:
    # 不使用 resolve() 静默穿过 symlink：从根目录逐层 O_NOFOLLOW 打开。
    # 拒绝 ..，避免先规范化路径后丢失中间符号链接的安全信息。
    path = Path(storage_path)
    if ".." in path.parts:
        raise ValueError("执行锁数据库路径不能包含 ..")
    path = path.absolute()
    if not path.name:
        raise ValueError("执行锁需要数据库文件路径")
    directory_fd = _open_directory(path.anchor)
    try:
        for component in path.parent.parts[1:]:
            next_fd = _open_directory(component, directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            database_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            # 测试或首次启动可以先加锁；父目录必须已经存在。
            pass
        else:
            if not stat.S_ISREG(database_stat.st_mode):
                raise ValueError("执行锁数据库路径必须是普通文件，不能是符号链接")
        yield directory_fd
    finally:
        os.close(directory_fd)


@contextmanager
def exclusive_run(storage_path: Path, run_id: str) -> Iterator[None]:
    """尝试独占一个 run，失败立即抛 RunBusy；异常或进程终止会释放锁。

    锁的范围是数据库父目录下的 run ID。不同 run 可并行；同 run 的所有
    CLI/API 执行入口必须使用这个上下文，否则账本事务不能防止重复执行。
    不依赖 Python 线程内状态，因此独立进程也会竞争同一个内核锁。
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("run_id 必须是 1 到 128 位字母、数字、短横线或下划线")
    filename = hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".lock"
    with _database_parent(storage_path) as database_fd:
        try:
            os.mkdir("locks", mode=0o700, dir_fd=database_fd)
        except FileExistsError:
            pass
        locks_fd = _open_directory("locks", database_fd)
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
            try:
                lock_fd = os.open(filename, flags, 0o600, dir_fd=locks_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EISDIR}:
                    raise ValueError("执行锁文件必须是普通文件，不能是符号链接") from exc
                raise
            try:
                info = os.fstat(lock_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("执行锁文件必须是没有其他硬链接的普通文件")
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise RunBusy("同一任务正在本机执行，请等待当前执行完成。") from exc
                    raise
                try:
                    yield
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        finally:
            os.close(locks_fd)

"""本机任务互斥：非阻塞 flock，进程退出后由内核释放持有的锁。

只适用于同一台 POSIX 主机上的本地文件系统。它不是分布式租约，也不提供
fencing token；多个主机、网络文件系统或外部系统副作用仍需要独立的协调协议。
锁文件在常规释放时保留，仅在通过非阻塞独占锁校验且过期后才允许安全回收。
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
import time
from typing import Iterator


class RunBusy(RuntimeError):
    """同一 run 已被本机另一个执行者持有；调用方应返回冲突，不排队等待。"""


_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC


def _traversal_flags() -> int:
    o_path = getattr(os, "O_PATH", None)
    if o_path is not None:
        return o_path | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
    return _DIRECTORY_FLAGS


def _open_directory(name: str, parent_fd: int | None = None, *, flags: int = _DIRECTORY_FLAGS) -> int:
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("执行锁目录不允许符号链接或非目录路径") from exc
        raise
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValueError("执行锁目录不允许符号链接或非目录路径")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _verify_parent_prefixes_and_open(parent: Path) -> int:
    prefix = Path(parent.anchor)
    prefixes = [prefix]
    for component in parent.parts[1:]:
        prefix = prefix / component
        prefixes.append(prefix)
    for current in prefixes:
        try:
            st = os.lstat(current)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("执行锁目录不允许符号链接或非目录路径") from exc
            raise
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError("执行锁目录不允许符号链接或非目录路径")
    return _open_directory(os.fspath(parent), flags=_DIRECTORY_FLAGS)


def _open_database_parent(parent: Path) -> int:
    parts = parent.parts
    if len(parts) <= 1:
        return _open_directory(parent.anchor, flags=_DIRECTORY_FLAGS)
    traversal_flags = _traversal_flags()
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory(parent.anchor, flags=traversal_flags)
        for component in parts[1:-1]:
            next_fd = _open_directory(component, directory_fd, flags=traversal_flags)
            os.close(directory_fd)
            directory_fd = next_fd
    except OSError as exc:
        if directory_fd is not None:
            os.close(directory_fd)
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return _verify_parent_prefixes_and_open(parent)
        raise
    except BaseException:
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    try:
        return _open_directory(parts[-1], directory_fd, flags=_DIRECTORY_FLAGS)
    finally:
        os.close(directory_fd)


@contextmanager
def _database_parent(storage_path: Path) -> Iterator[int]:
    # 不使用 resolve() 静默穿过 symlink：从根目录逐层 O_NOFOLLOW 校验并打开。
    # 拒绝 ..，避免先规范化路径后丢失中间符号链接的安全信息。
    path = Path(storage_path)
    if ".." in path.parts:
        raise ValueError("执行锁数据库路径不能包含 ..")
    path = path.absolute()
    if not path.name:
        raise ValueError("执行锁需要数据库文件路径")
    directory_fd = _open_database_parent(path.parent)
    try:
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


def _same_dir_entry(fd_info: os.stat_result, name: str, dir_fd: int) -> bool:
    try:
        entry_info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(entry_info.st_mode)
        and entry_info.st_dev == fd_info.st_dev
        and entry_info.st_ino == fd_info.st_ino
    )


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
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
            lock_fd: int | None = None
            for _ in range(4):
                try:
                    candidate_fd = os.open(filename, flags, 0o600, dir_fd=locks_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.EISDIR}:
                        raise ValueError("执行锁文件必须是普通文件，不能是符号链接") from exc
                    raise
                try:
                    info = os.fstat(candidate_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                        raise ValueError("执行锁文件必须是没有其他硬链接的普通文件")
                    if info.st_nlink == 0:
                        os.close(candidate_fd)
                        continue
                    try:
                        fcntl.flock(candidate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        if exc.errno in {errno.EACCES, errno.EAGAIN}:
                            if os.fstat(candidate_fd).st_nlink == 0:
                                os.close(candidate_fd)
                                continue
                            raise RunBusy("同一任务正在本机执行，请等待当前执行完成。") from exc
                        raise
                    locked_info = os.fstat(candidate_fd)
                    if locked_info.st_nlink > 1:
                        fcntl.flock(candidate_fd, fcntl.LOCK_UN)
                        raise ValueError("执行锁文件必须是没有其他硬链接的普通文件")
                    if locked_info.st_nlink != 1 or not _same_dir_entry(locked_info, filename, locks_fd):
                        fcntl.flock(candidate_fd, fcntl.LOCK_UN)
                        os.close(candidate_fd)
                        continue
                    os.utime(candidate_fd, None)
                    lock_fd = candidate_fd
                    break
                except BaseException:
                    os.close(candidate_fd)
                    raise
            if lock_fd is None:
                raise RunBusy("同一任务正在本机执行，请等待当前执行完成。")
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            os.close(locks_fd)


def cleanup_stale_locks(
    storage_path: Path,
    active_run_ids: set[str] | None = None,
    max_age_seconds: float = 3600.0,
) -> int:
    """安全扫描并删除超过 max_age_seconds 且未被持有或标记活跃的过期锁文件。"""
    active_filenames = {
        hashlib.sha256(rid.encode("utf-8")).hexdigest() + ".lock"
        for rid in (active_run_ids or set())
    }
    removed = 0
    open_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
    with _database_parent(storage_path) as database_fd:
        try:
            locks_fd = _open_directory("locks", database_fd)
        except FileNotFoundError:
            return 0
        try:
            for name in os.listdir(locks_fd):
                if not name.endswith(".lock") or name in active_filenames:
                    continue
                try:
                    lock_fd = os.open(name, open_flags, dir_fd=locks_fd)
                except OSError as exc:
                    if exc.errno in {
                        errno.ENOENT,
                        errno.ELOOP,
                        errno.EISDIR,
                        errno.ENOTDIR,
                        errno.EACCES,
                        errno.EPERM,
                        errno.ENXIO,
                    }:
                        continue
                    raise
                try:
                    info = os.fstat(lock_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        continue
                    if info.st_mtime > time.time() - max_age_seconds:
                        continue
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        if exc.errno in {errno.EACCES, errno.EAGAIN}:
                            continue
                        raise
                    try:
                        locked_info = os.fstat(lock_fd)
                        if (
                            stat.S_ISREG(locked_info.st_mode)
                            and locked_info.st_nlink == 1
                            and locked_info.st_mtime <= time.time() - max_age_seconds
                            and _same_dir_entry(locked_info, name, locks_fd)
                        ):
                            try:
                                os.unlink(name, dir_fd=locks_fd)
                            except FileNotFoundError:
                                pass
                            else:
                                removed += 1
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            os.close(locks_fd)
    return removed

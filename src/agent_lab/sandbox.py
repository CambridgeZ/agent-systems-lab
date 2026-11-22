"""Docker 测试边界：受限快照、固定镜像、双进程防伪回执、资源限制和有界日志。

容器与双进程 HMAC 回执显著收紧待测代码的伪造面：外层 supervisor 持有随机密钥与回执前缀，
绝不导入 /workspace，并过滤子进程标准输出中的伪造前缀；子进程通过审计钩子阻断栈帧与 ctypes 探测。
此模块仍依赖可信 Docker 守护进程与宿主机内核；不支持把 Docker socket 交给待执行代码。
"""

from __future__ import annotations

import ast
import base64
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any


SANDBOX_FAILURE_CODES = frozenset(
    {
        "tests_failed",
        "no_tests_ran",
        "test_count_mismatch",
        "timeout",
        "sandbox_timeout",
        "sandbox_output_limit",
        "sandbox_incomplete",
        "sandbox_oom",
        "sandbox_snapshot_limit",
        "unsafe_workspace",
        "test_oracle_modified",
    }
)
SANDBOX_INFRA_CODES = frozenset(
    {
        "sandbox_unavailable",
        "image_missing",
        "daemon_error",
        "sandbox_error",
        "sandbox_cleanup_failed",
    }
)

_CHILD_PREAMBLE = """
import hashlib, hmac, json, os, sys, sysconfig, threading, unittest

def _entry():
    globals().pop("_entry", None)
    w_fd = int(sys.argv[1])
    ws = sys.argv[2]
    extra_argv = list(sys.argv[3:])
    sys.argv[:] = [sys.argv[0]]
    _key = sys.stdin.buffer.read(32)
    sys.stdin.close()
    if len(_key) != 32:
        os._exit(2)
    _hmac_new, _sha256, _dumps = hmac.new, hashlib.sha256, json.dumps
    _write, _close = os.write, os.close
    def _bind_sender(secret):
        sent = False
        def _send(receipt):
            nonlocal sent
            if sent:
                return
            sent = True
            body = _dumps(receipt, sort_keys=True).encode("utf-8")
            _write(w_fd, _hmac_new(secret, body, _sha256).digest() + body)
            _close(w_fd)
        return _send
    _send = _bind_sender(_key)
    del _key, _bind_sender
    os.chdir(ws)
    src_dir = os.path.join(ws, "src")
    if os.path.isdir(src_dir):
        sys.path.insert(0, src_dir)
    sys.path.insert(0, ws)
    _stdlib = sysconfig.get_path("stdlib") or ""
    _in_hook = False
    def _audit(event, args):
        nonlocal _in_hook
        if _in_hook:
            return
        if event.startswith("ctypes."):
            raise PermissionError("ctypes blocked in sandbox")
        if event == "import" and args and str(args[0]).split(".")[0] in ("ctypes", "_ctypes"):
            raise PermissionError("ctypes import blocked in sandbox")
        if event in (
            "gc.get_objects",
            "gc.get_referrers",
            "gc.get_referents",
            "sys._current_frames",
            "sys._current_exceptions",
        ):
            raise PermissionError("gc/frame inspection blocked in sandbox")
        if event == "sys._getframe":
            if args and getattr(getattr(args[0], "f_code", None), "co_filename", "") == "<string>":
                raise PermissionError("driver frame blocked in sandbox")
            _in_hook = True
            try:
                caller_file = sys._getframe(1).f_code.co_filename
            finally:
                _in_hook = False
            if not _stdlib or not caller_file.startswith(_stdlib) or caller_file.endswith(("/inspect.py", "\\\\inspect.py")):
                raise PermissionError("sys._getframe blocked in sandbox")
    sys.addaudithook(_audit)
    worker_out = []
    t = threading.Thread(target=_run_tests_in_worker, args=(ws, extra_argv, worker_out), daemon=True)
    t.start()
    t.join()
    if len(worker_out) != 1 or not isinstance(worker_out[0], dict):
        os._exit(1)
    receipt = worker_out[0]
    expected = receipt["expected"]
    _send(receipt)
    sys.exit(0 if receipt["tests_run"] == expected and not receipt["skipped"] and receipt["successful"] else 1)
""".strip()

_SINGLE_CHILD_TAIL = """
def _run_tests_in_worker(ws, _extra_argv, worker_out):
    _clear, _append = list.clear, list.append
    try:
        expected = 4
        suite = unittest.defaultTestLoader.discover(os.path.join(ws, "tests"), pattern="test_pricing.py")
        if suite.countTestCases() != expected:
            receipt = {"tests_run": 0, "expected": expected, "failures": 0, "errors": 0, "skipped": 0, "successful": False}
        else:
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            receipt = {"tests_run": result.testsRun, "expected": expected, "failures": len(result.failures),
                       "errors": len(result.errors), "skipped": len(result.skipped), "successful": result.wasSuccessful()}
        _clear(worker_out)
        _append(worker_out, receipt)
    except BaseException:
        _clear(worker_out)
        raise

_entry()
""".strip("\n")

_SUITE_CHILD_TAIL = r"""
def _run_tests_in_worker(ws, extra_argv, worker_out):
    import asyncio, fnmatch, importlib.util, inspect, re, traceback, types
    _clear, _append = list.clear, list.append
    try:
        pattern = extra_argv[0]
        expected = int(extra_argv[1])
        class _SuiteLoader(unittest.TestLoader):
            def loadTestsFromModule(self, module, *args, **kwargs):
                suite = super().loadTestsFromModule(module, *args, **kwargs)
                for name, obj in list(module.__dict__.items()):
                    if getattr(obj, "__module__", None) != module.__name__:
                        continue
                    if name.startswith("test_") and isinstance(obj, types.FunctionType):
                        if inspect.iscoroutinefunction(obj):
                            def _coro(fn=obj):
                                asyncio.run(fn())
                            _coro.__name__ = name
                            suite.addTest(unittest.FunctionTestCase(_coro))
                        else:
                            suite.addTest(unittest.FunctionTestCase(obj))
                    elif isinstance(obj, type) and not issubclass(obj, unittest.TestCase) and name.startswith("Test"):
                        for attr_name in dir(obj):
                            attr = getattr(obj, attr_name, None) if attr_name.startswith("test") else None
                            if isinstance(attr, types.FunctionType):
                                def _meth(cls=obj, m=attr_name):
                                    res = getattr(cls(), m)()
                                    if inspect.iscoroutine(res):
                                        asyncio.run(res)
                                _meth.__name__ = f"{name}.{attr_name}"
                                suite.addTest(unittest.FunctionTestCase(_meth))
                return suite
        tests_root = os.path.join(ws, "tests")
        if tests_root not in sys.path:
            sys.path.append(tests_root)
        discovered_files = []
        for dirpath, dirnames, filenames in os.walk(tests_root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for fname in filenames:
                if not fname.startswith(".") and fnmatch.fnmatch(fname, pattern):
                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, tests_root).replace(os.sep, "/")
                    discovered_files.append((rel_path, full_path, fname))
        discovered_files.sort(key=lambda item: item[0])
        loader = _SuiteLoader()
        suite = unittest.TestSuite()
        load_failed = False
        for idx, (_rel_path, full_path, fname) in enumerate(discovered_files):
            stem = re.sub(r"\W+", "_", os.path.splitext(fname)[0]) or "mod"
            mod_name = f"_agent_lab_test_{idx}_{stem}"
            spec = importlib.util.spec_from_file_location(mod_name, full_path)
            if spec is None or spec.loader is None:
                load_failed = True
                break
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                traceback.print_exc()
                load_failed = True
                break
            suite.addTests(loader.loadTestsFromModule(mod))
        if load_failed or suite.countTestCases() != expected:
            receipt = {"tests_run": 0, "expected": expected, "failures": 0, "errors": 0, "skipped": 0, "successful": False}
        else:
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            receipt = {"tests_run": result.testsRun, "expected": expected, "failures": len(result.failures),
                       "errors": len(result.errors), "skipped": len(result.skipped), "successful": result.wasSuccessful()}
        _clear(worker_out)
        _append(worker_out, receipt)
    except BaseException:
        _clear(worker_out)
        raise

_entry()
""".strip("\n")

def _build_supervisor_driver(child_code: str, *, suite_mode: bool = False) -> str:
    extra_args = ", __TEST_PATTERN__, str(__EXPECTED_TESTS__)" if suite_mode else ""
    return f"""
import hashlib, hmac, json, os, selectors, signal, subprocess, sys, time
marker = __RECEIPT_MARKER__
inner_timeout = float(__INNER_TIMEOUT__)
ws_dir = __WORKSPACE_DIR__
r_fd, w_fd = os.pipe()
key = os.urandom(32)
child = subprocess.Popen(
    [sys.executable, "-I", "-B", "-c", {child_code!r}, str(w_fd), ws_dir{extra_args}],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    bufsize=0, pass_fds=(w_fd,), start_new_session=True,
)
os.close(w_fd)
try:
    child.stdin.write(key)
    child.stdin.close()
except OSError:
    try:
        child.stdin.close()
    except OSError:
        pass
_needle = b"AGENT_LAB_" + b"RECEIPT_"
_repl = b"[SPOOF_BLOCKED_RECEIPT_]"
pending = b""
packet = bytearray()
timed_out = False
deadline = time.monotonic() + inner_timeout
sel = selectors.DefaultSelector()
try:
    sel.register(child.stdout, selectors.EVENT_READ)
    sel.register(r_fd, selectors.EVENT_READ)
    while sel.get_map():
        rem = deadline - time.monotonic()
        if rem <= 0:
            timed_out = True
            break
        events = sel.select(rem)
        if not events:
            timed_out = True
            break
        for sk, _ in events:
            if sk.fileobj is child.stdout:
                chunk = os.read(child.stdout.fileno(), 4096)
                if not chunk:
                    sel.unregister(child.stdout)
                    if pending:
                        os.write(1, pending.replace(_needle, _repl))
                        pending = b""
                else:
                    data = (pending + chunk).replace(_needle, _repl)
                    keep = 0
                    for k in range(min(len(_needle) - 1, len(data)), 0, -1):
                        if data.endswith(_needle[:k]):
                            keep = k
                            break
                    emit, pending = (data[:-keep], data[-keep:]) if keep else (data, b"")
                    if emit:
                        os.write(1, emit)
            else:
                chunk = os.read(r_fd, 4096)
                if not chunk or len(packet) > 8192:
                    sel.unregister(r_fd)
                else:
                    packet.extend(chunk)
    if not timed_out:
        try:
            child.wait(timeout=max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
finally:
    sel.close()
    os.close(r_fd)
    if timed_out or child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=2)
    child.stdout.close()
if timed_out:
    sys.exit(124)
if len(packet) > 32 and hmac.compare_digest(bytes(packet[:32]), hmac.new(key, bytes(packet[32:]), hashlib.sha256).digest()):
    receipt = json.loads(bytes(packet[32:]).decode("utf-8"))
    sys.stdout.write("\\n" + marker + json.dumps(receipt, sort_keys=True) + "\\n")
    sys.stdout.flush()
    ok = (
        receipt.get("tests_run") == receipt.get("expected")
        and not receipt.get("skipped")
        and receipt.get("successful") is True
        and child.returncode == 0
    )
    sys.exit(0 if ok else 1)
if child.returncode is not None and child.returncode < 0:
    sys.exit(128 + (-child.returncode))
sys.exit(child.returncode if child.returncode not in (None, 0) else 1)
""".strip()


_DRIVER = _build_supervisor_driver(_CHILD_PREAMBLE + "\n" + _SINGLE_CHILD_TAIL, suite_mode=False)
_SUITE_DRIVER = _build_supervisor_driver(_CHILD_PREAMBLE + "\n" + _SUITE_CHILD_TAIL, suite_mode=True)

# 这些只是已知凭证命名的额外排除，不能检测业务 .py 中任意硬编码的秘密。
_SENSITIVE_NAMES = frozenset({"credentials.py", "secrets.py", "secret.py", "private_key.py", "tokens.py"})
_SENSITIVE_STEMS = frozenset(
    {"credentials", "secrets", "secret", "private_key", "tokens", "api_key", "apikey"}
)
_SUITE_FIXTURE_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".txt", ".csv"})


def _init_concurrency_limit() -> int:
    try:
        return max(1, int(os.environ.get("AGENT_LAB_SANDBOX_CONCURRENCY", "4")))
    except ValueError:
        return 4


_SANDBOX_SEMAPHORE = threading.BoundedSemaphore(_init_concurrency_limit())
_LIMIT_FLAGS = (
    "--pull=never",
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=64",
    "--memory=128m",
    "--memory-swap=128m",
    "--cpus=1",
    "--user=65534:65534",
    "--ulimit=nofile=128:128",
    "--ulimit=core=0:0",
    "--log-driver=none",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_test_class_ast(node: ast.ClassDef) -> bool:
    if node.name.startswith("Test"):
        return True
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id.endswith("TestCase"):
            return True
        if isinstance(base, ast.Attribute) and base.attr.endswith("TestCase"):
            return True
    return False


def discover_test_oracles(workspace: Path) -> tuple[dict[str, str], int]:
    """静态解析受保护的 tests 目录，提取每个测试文件的 SHA-256 与预期测试数。

    同时计数 TestCase/Test* 类中的 test* 方法与模块顶层 def test_* / async def test_* 函数。
    在宿主端仅做 AST 分析，绝不 import 或执行待测仓库代码。
    """
    root = Path(workspace).resolve()
    tests_dir = root / "tests"
    if not tests_dir.is_dir() or tests_dir.is_symlink():
        raise ValueError("仓库缺少真实 tests 目录")
    oracles: dict[str, str] = {}
    expected_count = 0
    for path in sorted(tests_dir.rglob("*.py")):
        if path.is_symlink() or path.name.startswith("."):
            continue
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        oracles[rel] = _sha(raw)
        if path.name.startswith("test") and path.name.endswith(".py"):
            tree = ast.parse(raw.decode("utf-8"), filename=rel)
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and _is_test_class_ast(node):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
                            "test"
                        ):
                            expected_count += 1
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                    "test_"
                ):
                    expected_count += 1
    if not oracles or expected_count < 1:
        raise ValueError("未在 tests/ 中发现可静态计数的测试方法或函数")
    return oracles, expected_count


@dataclass(frozen=True)
class SandboxPolicy:
    """调用方可以收紧上限，不能通过配置解除默认安全限制。"""

    image: str = "python:3.12-slim"
    timeout_seconds: float = 30
    output_limit_bytes: int = 16_384
    max_files: int = 128
    max_file_bytes: int = 65_536
    max_total_bytes: int = 2_097_152
    max_entries: int = 4096
    max_depth: int = 16

    def __post_init__(self):
        if self.image != "python:3.12-slim":
            raise ValueError("sandbox_policy_invalid: 镜像引用由执行器固定")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 30
        ):
            raise ValueError("sandbox_policy_invalid: timeout_seconds 必须在 (0,30] 内")
        bounds = {
            "output_limit_bytes": 16_384,
            "max_files": 128,
            "max_file_bytes": 65_536,
            "max_total_bytes": 2_097_152,
            "max_entries": 4096,
            "max_depth": 16,
        }
        if any(
            type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= cap
            for key, cap in bounds.items()
        ):
            raise ValueError("sandbox_policy_invalid: 快照与日志上限只能收紧")

    def audit(self) -> dict:
        return {
            **asdict(self),
            "version": 1,
            "docker_flags": list(_LIMIT_FLAGS),
            "tmpfs": "/tmp:rw,noexec,nosuid,size=16m",
            "python_argv": ["python", "-I", "-B", "-c", "controlled_receipt_driver"],
            "driver_sha256": _sha(_DRIVER.encode()),
            "sensitive_names_excluded": sorted(_SENSITIVE_NAMES),
            "expected_tests": 4,
        }


class _SandboxError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class _CommandResult:
    returncode: int | None
    output: bytes
    timed_out: bool = False
    output_limited: bool = False
    error: str | None = None


class SandboxRunner:
    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        remote_url: str | None = None,
        *,
        remote_backend: RemoteHTTPSandboxBackend | None = None,
    ):
        self.policy = policy or SandboxPolicy()
        resolved_url = remote_url if remote_url is not None else os.environ.get("AGENT_LAB_SANDBOX_URL")
        self.remote_backend = remote_backend or (
            RemoteHTTPSandboxBackend(resolved_url) if resolved_url and resolved_url.strip() else None
        )

    @staticmethod
    def _open_directory(
        name: str, parent_fd: int | None = None, *, traversal_only: bool = False
    ) -> int:
        cloexec = getattr(os, "O_CLOEXEC", 0)
        o_path = getattr(os, "O_PATH", None) if traversal_only else None
        base_flag = o_path if o_path is not None else os.O_RDONLY
        flags = base_flag | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _SandboxError("unsafe_workspace", "工作区不允许符号链接或非目录路径") from exc
            raise
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise _SandboxError("unsafe_workspace", "工作区不允许符号链接或非目录路径")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _verify_prefixes_and_open_root(self, path: Path) -> int:
        prefix = Path(path.anchor)
        prefixes = [prefix]
        for component in path.parts[1:]:
            prefix = prefix / component
            prefixes.append(prefix)
        last_stat = None
        for current in prefixes:
            try:
                st = os.lstat(current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _SandboxError("unsafe_workspace", "工作区不允许符号链接或非目录路径") from exc
                raise
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise _SandboxError("unsafe_workspace", "工作区不允许符号链接或非目录路径")
            last_stat = st
        fd = self._open_directory(os.fspath(path), traversal_only=False)
        try:
            opened = os.fstat(fd)
            if last_stat is not None and (opened.st_dev, opened.st_ino) != (
                last_stat.st_dev,
                last_stat.st_ino,
            ):
                raise _SandboxError("unsafe_workspace", "工作区根目录在打开期间发生变化")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_root(self, workspace: Path) -> int:
        path = Path(workspace)
        if ".." in path.parts:
            raise _SandboxError("unsafe_workspace", "工作区路径不能包含 ..")
        path = path.absolute()
        parts = path.parts
        if len(parts) <= 1:
            return self._open_directory(path.anchor, traversal_only=False)
        descriptor: int | None = None
        try:
            descriptor = self._open_directory(path.anchor, traversal_only=True)
            for component in parts[1:-1]:
                next_fd = self._open_directory(component, descriptor, traversal_only=True)
                os.close(descriptor)
                descriptor = next_fd
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EPERM}:
                return self._verify_prefixes_and_open_root(path)
            raise
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        try:
            return self._open_directory(parts[-1], descriptor, traversal_only=False)
        finally:
            os.close(descriptor)

    def _snapshot(self, workspace: Path, destination: Path, oracle_sha256: str) -> dict:
        if not isinstance(oracle_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", oracle_sha256):
            raise _SandboxError("test_oracle_modified", "验收测试摘要无效")
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        manifest, total, entries = [], 0, 0

        def visit(directory_fd: int, relative: Path, depth: int):
            nonlocal total, entries
            if depth > self.policy.max_depth:
                raise _SandboxError("sandbox_snapshot_limit", "工作区目录深度超过限制")
            # 流式遍历，不先把整个潜在巨大目录排序到内存。
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > self.policy.max_entries:
                        raise _SandboxError("sandbox_snapshot_limit", "工作区目录项超过限制")
                    if entry.name.startswith("."):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    child = relative / entry.name
                    if stat.S_ISLNK(info.st_mode):
                        raise _SandboxError("unsafe_workspace", "工作区不允许符号链接")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = self._open_directory(entry.name, directory_fd)
                        try:
                            target = destination / child
                            target.mkdir(mode=0o755)
                            target.chmod(0o755)
                            visit(child_fd, child, depth + 1)
                        finally:
                            os.close(child_fd)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise _SandboxError("unsafe_workspace", "工作区不允许硬链接、FIFO 或其他特殊文件")
                    if not entry.name.endswith(".py") or entry.name.casefold() in _SENSITIVE_NAMES:
                        continue
                    if len(manifest) >= self.policy.max_files or info.st_size > self.policy.max_file_bytes:
                        raise _SandboxError("sandbox_snapshot_limit", "Python 文件数量或单文件大小超过限制")
                    # 即使攻击者在 lstat 后换成 FIFO，也不能让 open 阻塞宿主线程。
                    descriptor = os.open(
                        entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                    )
                    try:
                        before = os.fstat(descriptor)
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise _SandboxError("unsafe_workspace", "复制时源文件已变为链接或特殊文件")
                        content = bytearray()
                        while len(content) <= self.policy.max_file_bytes:
                            part = os.read(
                                descriptor, min(65_536, self.policy.max_file_bytes + 1 - len(content))
                            )
                            if not part:
                                break
                            content.extend(part)
                        after = os.fstat(descriptor)

                        def signature(value):
                            return (
                                value.st_dev,
                                value.st_ino,
                                value.st_size,
                                value.st_mtime_ns,
                                value.st_ctime_ns,
                                value.st_nlink,
                            )

                        if signature(before) != signature(after):
                            raise _SandboxError("unsafe_workspace", "源文件在快照复制期间发生变化")
                    finally:
                        os.close(descriptor)
                    total += len(content)
                    if len(content) > self.policy.max_file_bytes or total > self.policy.max_total_bytes:
                        raise _SandboxError("sandbox_snapshot_limit", "Python 快照总大小超过限制")
                    target = destination / child
                    target.write_bytes(content)
                    target.chmod(0o444)
                    manifest.append(
                        {"path": child.as_posix(), "bytes": len(content), "sha256": _sha(content)}
                    )

        root_fd = self._open_root(workspace)
        try:
            visit(root_fd, Path(), 0)
        finally:
            os.close(root_fd)
        # 必须校验复制后的验收文件；只校验源文件会留下 copy 之间的竞争窗口。
        oracle = destination / "tests/test_pricing.py"
        if not oracle.is_file() or _sha(oracle.read_bytes()) != oracle_sha256:
            raise _SandboxError("test_oracle_modified", "快照中的验收测试与内置版本不一致")
        manifest.sort(key=lambda item: item["path"])
        return {
            "snapshot_sha256": _sha(_json(manifest).encode()),
            "snapshot_file_count": len(manifest),
            "snapshot_total_bytes": total,
        }

    def _snapshot_suite(
        self, workspace: Path, destination: Path, test_oracles: dict[str, str]
    ) -> dict:
        if not test_oracles or any(
            not rel.startswith("tests/")
            or not rel.endswith(".py")
            or ".." in Path(rel).parts
            or not isinstance(digest_hex, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest_hex)
            for rel, digest_hex in test_oracles.items()
        ):
            raise _SandboxError("test_oracle_modified", "测试套件摘要配置无效")
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        manifest, total, entries = [], 0, 0

        def visit(directory_fd: int, relative: Path, depth: int):
            nonlocal total, entries
            if depth > self.policy.max_depth:
                raise _SandboxError("sandbox_snapshot_limit", "工作区目录深度超过限制")
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > self.policy.max_entries:
                        raise _SandboxError("sandbox_snapshot_limit", "工作区目录项超过限制")
                    if entry.name.startswith("."):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    child = relative / entry.name
                    if stat.S_ISLNK(info.st_mode):
                        raise _SandboxError("unsafe_workspace", "工作区不允许符号链接")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = self._open_directory(entry.name, directory_fd)
                        try:
                            target = destination / child
                            target.mkdir(mode=0o755)
                            target.chmod(0o755)
                            visit(child_fd, child, depth + 1)
                        finally:
                            os.close(child_fd)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise _SandboxError("unsafe_workspace", "工作区不允许硬链接、FIFO 或其他特殊文件")
                    lower_name = entry.name.casefold()
                    stem = Path(entry.name).stem.casefold()
                    _, suffix = os.path.splitext(lower_name)
                    is_py = suffix == ".py"
                    is_test_fixture = (
                        bool(child.parts)
                        and child.parts[0] == "tests"
                        and suffix in _SUITE_FIXTURE_SUFFIXES
                        and stem not in _SENSITIVE_STEMS
                    )
                    if (
                        (not is_py and not is_test_fixture)
                        or lower_name in _SENSITIVE_NAMES
                        or stem in _SENSITIVE_STEMS
                    ):
                        continue
                    if len(manifest) >= self.policy.max_files or info.st_size > self.policy.max_file_bytes:
                        raise _SandboxError("sandbox_snapshot_limit", "快照文件数量或单文件大小超过限制")
                    descriptor = os.open(
                        entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                    )
                    try:
                        before = os.fstat(descriptor)
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise _SandboxError("unsafe_workspace", "复制时源文件已变为链接或特殊文件")
                        content = bytearray()
                        while len(content) <= self.policy.max_file_bytes:
                            part = os.read(
                                descriptor, min(65_536, self.policy.max_file_bytes + 1 - len(content))
                            )
                            if not part:
                                break
                            content.extend(part)
                        after = os.fstat(descriptor)
                        sig_before = (
                            before.st_dev,
                            before.st_ino,
                            before.st_size,
                            before.st_mtime_ns,
                            before.st_ctime_ns,
                            before.st_nlink,
                        )
                        sig_after = (
                            after.st_dev,
                            after.st_ino,
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                            after.st_nlink,
                        )
                        if sig_before != sig_after:
                            raise _SandboxError("unsafe_workspace", "源文件在快照复制期间发生变化")
                    finally:
                        os.close(descriptor)
                    total += len(content)
                    if len(content) > self.policy.max_file_bytes or total > self.policy.max_total_bytes:
                        raise _SandboxError("sandbox_snapshot_limit", "Python 快照总大小超过限制")
                    target = destination / child
                    target.write_bytes(content)
                    target.chmod(0o444)
                    manifest.append(
                        {"path": child.as_posix(), "bytes": len(content), "sha256": _sha(content)}
                    )

        root_fd = self._open_root(workspace)
        try:
            visit(root_fd, Path(), 0)
        finally:
            os.close(root_fd)
        copied_tests = {
            item["path"]: item["sha256"]
            for item in manifest
            if item["path"].startswith("tests/") and item["path"].endswith(".py")
        }
        if copied_tests != test_oracles:
            raise _SandboxError("test_oracle_modified", "快照中的受保护测试文件集与基线摘要不一致")
        manifest.sort(key=lambda item: item["path"])
        return {
            "snapshot_sha256": _sha(_json(manifest).encode()),
            "snapshot_file_count": len(manifest),
            "snapshot_total_bytes": total,
        }

    @staticmethod
    def _capture(command: list[str], timeout: float, limit: int) -> _CommandResult:
        """最多实际读取 limit+1 字节；超限立即杀 CLI，不把无限输出写到磁盘。"""
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            close_fds=True,
            shell=False,
        )
        output = bytearray()
        timed_out = output_limited = False
        error = None
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        try:
            if process.stdout is None:
                raise OSError("Docker CLI 没有输出管道")
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready = selector.select(remaining)
                if not ready:
                    timed_out = True
                    break
                part = os.read(process.stdout.fileno(), min(4096, limit + 1 - len(output)))
                if not part:
                    try:
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
                    break
                output.extend(part)
                if len(output) > limit:
                    output_limited = True
                    break
        except OSError as exc:
            error = type(exc).__name__
        finally:
            selector.close()
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    error = type(exc).__name__
            if process.stdout is not None:
                process.stdout.close()
        return _CommandResult(process.returncode, bytes(output[:limit]), timed_out, output_limited, error)

    @staticmethod
    def _receipt(
        output: str, marker: str, returncode: int | None, expected_tests: int = 4
    ) -> dict | None:
        lines = [line[len(marker) :] for line in output.splitlines() if line.startswith(marker)]
        if len(lines) != 1:
            return None
        try:
            receipt = json.loads(lines[0])
        except (ValueError, TypeError):
            return None
        if not isinstance(receipt, dict):
            return None
        counters = ("tests_run", "expected", "failures", "errors", "skipped")
        if any(type(receipt.get(key)) is not int or receipt[key] < 0 for key in counters):
            return None
        if type(receipt.get("successful")) is not bool:
            return None
        if (
            receipt["tests_run"] != expected_tests
            or receipt["expected"] != expected_tests
            or receipt["skipped"] != 0
        ):
            return None
        if receipt["failures"] + receipt["errors"] > receipt["tests_run"]:
            return None
        passed = receipt["failures"] == receipt["errors"] == 0
        if receipt["successful"] != passed or returncode != (0 if passed else 1):
            return None
        return receipt

    @staticmethod
    def _collect_snapshot_b64(snapshot: Path) -> dict[str, str]:
        files_b64: dict[str, str] = {}
        for item in sorted(snapshot.rglob("*")):
            if item.is_file() and not item.is_symlink():
                rel = item.relative_to(snapshot).as_posix()
                files_b64[rel] = base64.b64encode(item.read_bytes()).decode("ascii")
        return files_b64

    @staticmethod
    def _record_rmtree_failure(result: dict, rm_exc: OSError) -> None:
        rm_err = f"rmtree_{errno.errorcode.get(rm_exc.errno, type(rm_exc).__name__)}"
        result["cleanup_verified"] = False
        if not result.get("cleanup_error"):
            result["cleanup_error"] = rm_err
        if result.get("ok") or result.get("code") == "passed":
            result.update(execution_code="passed", code="sandbox_cleanup_failed")
        result["ok"] = False

    def run_suite(
        self,
        workspace: Path,
        test_oracles: dict[str, str],
        expected_tests: int,
        pattern: str = "test*.py",
    ) -> dict:
        """运行多文件或通用测试套件：校验全部受保护测试文件摘要与预期测试数回执。"""
        if type(expected_tests) is not int or not 1 <= expected_tests <= 200:
            raise ValueError("expected_tests 必须是 1—200 之间的整数")
        if not re.fullmatch(r"[A-Za-z0-9_.*-]+", pattern):
            raise ValueError("测试文件匹配模式不合法")
        oracle_bundle_sha = _sha(_json(test_oracles).encode())
        audit = {
            **self.policy.audit(),
            "expected_tests": expected_tests,
            "test_pattern": pattern,
            "driver_sha256": _sha(_SUITE_DRIVER.encode()),
        }
        policy_hash = _sha(_json(audit).encode())
        result = {
            "ok": False,
            "code": "sandbox_unavailable",
            "tests_executed": False,
            "output": "",
            "image_id": None,
            "image_sha256": None,
            "snapshot_sha256": None,
            "policy_sha256": policy_hash,
            "oracle_sha256": oracle_bundle_sha,
            "cleanup_attempted": False,
            "cleanup_verified": True,
            "cleanup_error": None,
            "isolation": "docker_readonly_snapshot_network_none",
        }
        temporary: str | None = None
        try:
            temporary = tempfile.mkdtemp(prefix="agent-lab-sandbox-")
            snapshot = Path(temporary).resolve() / "repo"
            result.update(self._snapshot_suite(Path(workspace), snapshot, test_oracles))
            if self.remote_backend is not None:
                remote_res = self.remote_backend.execute(
                    {
                        "mode": "suite",
                        "files_b64": self._collect_snapshot_b64(snapshot),
                        "test_oracles": dict(test_oracles),
                        "expected_tests": expected_tests,
                        "pattern": pattern,
                        "snapshot_sha256": result["snapshot_sha256"],
                        "policy": asdict(self.policy),
                    },
                    timeout=float(self.policy.timeout_seconds) + self.remote_backend.timeout_buffer_seconds,
                )
                result.update(remote_res)
                return result
            docker = shutil.which("docker")
            if not docker:
                result["error"] = "未安装 Docker，未执行运行时测试。"
                return result
            inspected = self._capture(
                [docker, "image", "inspect", "--format", "{{.Id}}", self.policy.image], 5, 256
            )
            if inspected.returncode != 0 and not (
                inspected.timed_out or inspected.output_limited or inspected.error
            ):
                inspected = self._capture(
                    [docker, "image", "ls", "--format", "{{.ID}}", "--no-trunc", self.policy.image],
                    5,
                    256,
                )
            image_id = inspected.output.decode("ascii", errors="replace").strip()
            if (
                inspected.returncode != 0
                or inspected.timed_out
                or inspected.output_limited
                or inspected.error
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            ):
                result["error"] = "Docker 不可用或本地镜像 ID 无法验证；不会自动拉取镜像。"
                return result
            result.update(image_id=image_id, image_sha256=image_id.removeprefix("sha256:"))
            if "," in str(snapshot):
                raise _SandboxError("unsafe_workspace", "临时快照路径不能包含 Docker mount 分隔符")
            name = "agent-lab-" + uuid.uuid4().hex
            marker = "AGENT_LAB_RECEIPT_" + uuid.uuid4().hex + ":"
            inner_timeout = round(float(self.policy.timeout_seconds) + 2.0, 3)
            expires_at = int(time.time() + self.policy.timeout_seconds + 30)
            driver = (
                _SUITE_DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
                .replace("__INNER_TIMEOUT__", repr(inner_timeout))
                .replace("__WORKSPACE_DIR__", repr("/workspace"))
                .replace("__TEST_PATTERN__", repr(pattern))
                .replace("__EXPECTED_TESTS__", str(expected_tests))
            )
            command = [
                docker,
                "run",
                "--name",
                name,
                "--label",
                "agent-lab.managed=1",
                "--label",
                f"agent-lab.expires-at={expires_at}",
                *_LIMIT_FLAGS,
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--mount",
                f"type=bind,source={snapshot},target=/workspace,readonly",
                "--workdir",
                "/workspace",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONNOUSERSITE=1",
                image_id,
                "python",
                "-I",
                "-B",
                "-c",
                driver,
            ]
            try:
                result["tests_executed"] = None
                with _SANDBOX_SEMAPHORE:
                    execution = self._capture(
                        command, self.policy.timeout_seconds, self.policy.output_limit_bytes
                    )
                display = execution.output.decode("utf-8", errors="replace").encode("utf-8")
                result.update(
                    output=display[: self.policy.output_limit_bytes].decode("utf-8", errors="ignore"),
                    output_truncated=execution.output_limited,
                    exit_code=execution.returncode,
                )
                if execution.timed_out:
                    result.update(code="sandbox_timeout", error="容器测试超过时间上限，已请求终止。")
                elif execution.output_limited:
                    result.update(code="sandbox_output_limit", error="容器输出超过硬上限，已请求终止。")
                elif execution.error:
                    result.update(code="sandbox_error", error=execution.error)
                else:
                    receipt = self._receipt(
                        result["output"], marker, execution.returncode, expected_tests=expected_tests
                    )
                    if receipt is None:
                        result.update(
                            code="sandbox_incomplete",
                            error=f"缺少完整且一致的 {expected_tests} 项测试回执，不能判定通过。",
                        )
                        if execution.returncode == 137:
                            result.update(oom_killed=True, failure_reason="oom_killed")
                    else:
                        passed = receipt["successful"]
                        result.update(
                            ok=passed,
                            code="passed" if passed else "tests_failed",
                            tests_executed=True,
                            test_summary=receipt,
                        )
            except OSError as exc:
                result.update(code="sandbox_error", error=type(exc).__name__, tests_executed=None)
            finally:
                result["cleanup_attempted"] = True
                try:
                    cleanup = self._capture([docker, "rm", "-f", name], 5, 2048)
                    result["cleanup_verified"] = cleanup.returncode == 0 and not (
                        cleanup.timed_out or cleanup.output_limited or cleanup.error
                    )
                    if not result["cleanup_verified"]:
                        result["cleanup_error"] = cleanup.error or (
                            "cleanup_timeout"
                            if cleanup.timed_out
                            else "cleanup_output_limit"
                            if cleanup.output_limited
                            else f"docker_rm_exit_{cleanup.returncode}"
                        )
                except OSError as exc:
                    result.update(cleanup_verified=False, cleanup_error=type(exc).__name__)
                if not result["cleanup_verified"]:
                    if result["ok"]:
                        result.update(execution_code=result["code"], code="sandbox_cleanup_failed")
                    result["ok"] = False
            return result
        except _SandboxError as exc:
            result.update(ok=False, code=exc.code, error=str(exc))
        except (OSError, UnicodeError) as exc:
            result.update(ok=False, code="sandbox_unavailable", error=type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    shutil.rmtree(temporary)
                except OSError as rm_exc:
                    self._record_rmtree_failure(result, rm_exc)
        return result

    def run(self, workspace: Path, oracle_sha256: str) -> dict:
        policy_hash = _sha(_json(self.policy.audit()).encode())
        result = {
            "ok": False,
            "code": "sandbox_unavailable",
            "tests_executed": False,
            "output": "",
            "image_id": None,
            "image_sha256": None,
            "snapshot_sha256": None,
            "policy_sha256": policy_hash,
            "oracle_sha256": oracle_sha256,
            "cleanup_attempted": False,
            "cleanup_verified": True,
            "cleanup_error": None,
            "isolation": "docker_readonly_snapshot_network_none",
        }
        temporary: str | None = None
        try:
            # 父目录保持 0700；容器只看到 bind mount 后的 repo 根，repo 为 0755。
            # 不挂载原始 workspace，也不把主机环境变量传为 container --env。
            temporary = tempfile.mkdtemp(prefix="agent-lab-sandbox-")
            snapshot = Path(temporary).resolve() / "repo"
            result.update(self._snapshot(Path(workspace), snapshot, oracle_sha256))
            if self.remote_backend is not None:
                remote_res = self.remote_backend.execute(
                    {
                        "mode": "single",
                        "files_b64": self._collect_snapshot_b64(snapshot),
                        "oracle_sha256": oracle_sha256,
                        "snapshot_sha256": result["snapshot_sha256"],
                        "policy": asdict(self.policy),
                    },
                    timeout=float(self.policy.timeout_seconds) + self.remote_backend.timeout_buffer_seconds,
                )
                result.update(remote_res)
                return result
            docker = shutil.which("docker")
            if not docker:
                result["error"] = "未安装 Docker，未执行运行时测试。"
                return result
            inspected = self._capture(
                [docker, "image", "inspect", "--format", "{{.Id}}", self.policy.image], 5, 256
            )
            if inspected.returncode != 0 and not (
                inspected.timed_out or inspected.output_limited or inspected.error
            ):
                inspected = self._capture(
                    [docker, "image", "ls", "--format", "{{.ID}}", "--no-trunc", self.policy.image],
                    5,
                    256,
                )
            image_id = inspected.output.decode("ascii", errors="replace").strip()
            if (
                inspected.returncode != 0
                or inspected.timed_out
                or inspected.output_limited
                or inspected.error
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            ):
                result["error"] = "Docker 不可用或本地镜像 ID 无法验证；不会自动拉取镜像。"
                return result
            result.update(image_id=image_id, image_sha256=image_id.removeprefix("sha256:"))
            if "," in str(snapshot):
                raise _SandboxError("unsafe_workspace", "临时快照路径不能包含 Docker mount 分隔符")
            name = "agent-lab-" + uuid.uuid4().hex
            marker = "AGENT_LAB_RECEIPT_" + uuid.uuid4().hex + ":"
            inner_timeout = round(float(self.policy.timeout_seconds) + 2.0, 3)
            expires_at = int(time.time() + self.policy.timeout_seconds + 30)
            driver = (
                _DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
                .replace("__INNER_TIMEOUT__", repr(inner_timeout))
                .replace("__WORKSPACE_DIR__", repr("/workspace"))
            )
            command = [
                docker,
                "run",
                "--name",
                name,
                "--label",
                "agent-lab.managed=1",
                "--label",
                f"agent-lab.expires-at={expires_at}",
                *_LIMIT_FLAGS,
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--mount",
                f"type=bind,source={snapshot},target=/workspace,readonly",
                "--workdir",
                "/workspace",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONNOUSERSITE=1",
                image_id,
                "python",
                "-I",
                "-B",
                "-c",
                driver,
            ]
            try:
                # 不用 --rm 自动删除，统一以 finally 的 rm -f 成功回执确认清理。
                result["tests_executed"] = None
                with _SANDBOX_SEMAPHORE:
                    execution = self._capture(
                        command, self.policy.timeout_seconds, self.policy.output_limit_bytes
                    )
                # 损坏 UTF-8 的替换字符可能扩成三个字节，显示结果也保持同一字节硬上限。
                display = execution.output.decode("utf-8", errors="replace").encode("utf-8")
                result.update(
                    output=display[: self.policy.output_limit_bytes].decode("utf-8", errors="ignore"),
                    output_truncated=execution.output_limited,
                    exit_code=execution.returncode,
                )
                if execution.timed_out:
                    result.update(code="sandbox_timeout", error="容器测试超过时间上限，已请求终止。")
                elif execution.output_limited:
                    result.update(code="sandbox_output_limit", error="容器输出超过硬上限，已请求终止。")
                elif execution.error:
                    result.update(code="sandbox_error", error=execution.error)
                else:
                    receipt = self._receipt(result["output"], marker, execution.returncode)
                    if receipt is None:
                        result.update(
                            code="sandbox_incomplete",
                            error="缺少完整且一致的四项测试回执，不能判定通过。",
                        )
                        if execution.returncode == 137:
                            result.update(oom_killed=True, failure_reason="oom_killed")
                    else:
                        passed = receipt["successful"]
                        result.update(
                            ok=passed,
                            code="passed" if passed else "tests_failed",
                            tests_executed=True,
                            test_summary=receipt,
                        )
            except OSError as exc:
                result.update(code="sandbox_error", error=type(exc).__name__, tests_executed=None)
            finally:
                result["cleanup_attempted"] = True
                try:
                    cleanup = self._capture([docker, "rm", "-f", name], 5, 2048)
                    result["cleanup_verified"] = cleanup.returncode == 0 and not (
                        cleanup.timed_out or cleanup.output_limited or cleanup.error
                    )
                    if not result["cleanup_verified"]:
                        result["cleanup_error"] = cleanup.error or (
                            "cleanup_timeout"
                            if cleanup.timed_out
                            else "cleanup_output_limit"
                            if cleanup.output_limited
                            else f"docker_rm_exit_{cleanup.returncode}"
                        )
                except OSError as exc:
                    result.update(cleanup_verified=False, cleanup_error=type(exc).__name__)
                if not result["cleanup_verified"]:
                    if result["ok"]:
                        result.update(execution_code=result["code"], code="sandbox_cleanup_failed")
                    result["ok"] = False
            return result
        except _SandboxError as exc:
            result.update(ok=False, code=exc.code, error=str(exc))
        except (OSError, UnicodeError) as exc:
            result.update(ok=False, code="sandbox_unavailable", error=type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    shutil.rmtree(temporary)
                except OSError as rm_exc:
                    self._record_rmtree_failure(result, rm_exc)
        return result


def reap_orphan_containers(now_ts: float | None = None) -> dict[str, Any]:
    """扫描带 agent-lab.managed=1 标签的容器并强制回收已超过 expires-at 的孤儿容器。"""
    docker = shutil.which("docker")
    if not docker:
        return {"ok": False, "code": "sandbox_unavailable", "scanned": 0, "reaped": [], "errors": []}
    now = float(time.time() if now_ts is None else now_ts)
    ps_res = SandboxRunner._capture(
        [docker, "ps", "-aq", "--filter", "label=agent-lab.managed=1"], 5, 16_384
    )
    if ps_res.returncode != 0 or ps_res.timed_out or ps_res.output_limited or ps_res.error:
        return {
            "ok": False,
            "code": "daemon_error",
            "scanned": 0,
            "reaped": [],
            "errors": [ps_res.error or f"docker_ps_exit_{ps_res.returncode}"],
        }
    ids = [
        line.strip()
        for line in ps_res.output.decode("utf-8", errors="replace").splitlines()
        if line.strip() and re.fullmatch(r"[0-9a-fA-F]{12,64}", line.strip())
    ]
    if not ids:
        return {"ok": True, "code": "ok", "scanned": 0, "reaped": [], "errors": []}
    inspected = SandboxRunner._capture(
        [docker, "inspect", "--format", '{{.Id}}\t{{index .Config.Labels "agent-lab.expires-at"}}', *ids],
        5,
        65_536,
    )
    if inspected.returncode != 0 or inspected.timed_out or inspected.output_limited or inspected.error:
        return {
            "ok": False,
            "code": "daemon_error",
            "scanned": len(ids),
            "reaped": [],
            "errors": [inspected.error or f"docker_inspect_exit_{inspected.returncode}"],
        }
    text = inspected.output.decode("utf-8", errors="replace").strip()
    expirations: list[tuple[str, float]] = []
    if text.startswith("["):
        for item in json.loads(text):
            cid = str(item.get("Id", "")).strip()
            labels = (item.get("Config") or {}).get("Labels") or item.get("Labels") or {}
            raw_exp = labels.get("agent-lab.expires-at")
            if cid and raw_exp is not None:
                try:
                    expirations.append((cid, float(raw_exp)))
                except (TypeError, ValueError):
                    continue
    else:
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    expirations.append((parts[0], float(parts[1])))
                except ValueError:
                    continue
    reaped: list[str] = []
    errors: list[str] = []
    for cid, expires_at in expirations:
        if expires_at <= now:
            rm_res = SandboxRunner._capture([docker, "rm", "-f", cid], 5, 2048)
            if rm_res.returncode == 0 and not (rm_res.timed_out or rm_res.output_limited or rm_res.error):
                reaped.append(cid)
            else:
                errors.append(f"{cid}:{rm_res.error or f'docker_rm_exit_{rm_res.returncode}'}")
    return {
        "ok": len(errors) == 0,
        "code": "ok" if not errors else "sandbox_cleanup_failed",
        "scanned": len(ids),
        "reaped": reaped,
        "errors": errors,
    }


class RemoteHTTPSandboxBackend:
    """将已完成本地安全校验与快照脱敏的文件集通过 HTTP 发送至远程受管沙箱工作节点。"""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport: Any | None = None,
        timeout_buffer_seconds: float = 10.0,
    ):
        cleaned = base_url.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("sandbox_remote_url_invalid: 远程沙箱 URL 必须以 http:// 或 https:// 开头")
        self.base_url = cleaned
        self.endpoint = f"{cleaned}/api/internal/sandbox/execute"
        self.api_key = api_key if api_key is not None else os.environ.get("AGENT_LAB_SANDBOX_TOKEN")
        self.transport = transport
        self.timeout_buffer_seconds = timeout_buffer_seconds

    def execute(self, payload: dict[str, Any], *, timeout: float = 40.0) -> dict[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with httpx.Client(
                timeout=timeout, transport=self.transport, follow_redirects=False
            ) as client:
                response = client.post(self.endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "code": "sandbox_unavailable",
                "tests_executed": False,
                "error": f"remote_http_error:{type(exc).__name__}",
                "isolation": "remote_http_sandbox",
            }
        if response.status_code != 200:
            return {
                "ok": False,
                "code": "sandbox_unavailable",
                "tests_executed": False,
                "error": f"remote_http_status_{response.status_code}",
                "isolation": "remote_http_sandbox",
            }
        try:
            data = response.json()
        except ValueError:
            return {
                "ok": False,
                "code": "sandbox_error",
                "tests_executed": None,
                "error": "remote_invalid_json",
                "isolation": "remote_http_sandbox",
            }
        if not isinstance(data, dict):
            return {
                "ok": False,
                "code": "sandbox_error",
                "tests_executed": None,
                "error": "remote_invalid_payload",
                "isolation": "remote_http_sandbox",
            }
        valid_codes = {"passed"} | SANDBOX_FAILURE_CODES | SANDBOX_INFRA_CODES
        code = data.get("code")
        if code not in valid_codes or type(data.get("ok")) is not bool:
            return {
                "ok": False,
                "code": "sandbox_error",
                "tests_executed": None,
                "error": "remote_invalid_result_schema",
                "isolation": "remote_http_sandbox",
            }
        if data["ok"] and (code != "passed" or data.get("tests_executed") is not True):
            return {
                "ok": False,
                "code": "sandbox_incomplete",
                "tests_executed": None,
                "error": "remote_contradictory_receipt",
                "isolation": "remote_http_sandbox",
            }
        sanitized = dict(data)
        for local_only in ("snapshot_sha256", "snapshot_file_count", "snapshot_total_bytes", "oracle_sha256"):
            sanitized.pop(local_only, None)
        sanitized.setdefault("isolation", "remote_http_sandbox")
        return sanitized


def execute_remote_sandbox_payload(
    payload: dict[str, Any], policy: SandboxPolicy | None = None
) -> dict[str, Any]:
    """远程沙箱服务端入口：还原受限快照载荷并在本地 Docker 沙箱内执行验证。"""
    if not isinstance(payload, dict):
        return {"ok": False, "code": "sandbox_error", "tests_executed": False, "error": "invalid_payload"}
    try:
        effective_policy = policy or (
            SandboxPolicy(**payload["policy"]) if isinstance(payload.get("policy"), dict) else SandboxPolicy()
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "code": "sandbox_error", "tests_executed": False, "error": str(exc)}
    raw_b64 = payload.get("files_b64")
    raw_text = payload.get("files")
    if not isinstance(raw_b64, dict) and not isinstance(raw_text, dict):
        return {"ok": False, "code": "unsafe_workspace", "tests_executed": False, "error": "missing_files"}
    entries = raw_b64 if isinstance(raw_b64, dict) else raw_text
    assert isinstance(entries, dict)
    if len(entries) > effective_policy.max_files:
        return {"ok": False, "code": "sandbox_snapshot_limit", "tests_executed": False, "error": "too_many_files"}
    temp_ws = tempfile.mkdtemp(prefix="agent-lab-remote-ws-")
    try:
        ws_root = Path(temp_ws).resolve()
        total = 0
        for rel, encoded in entries.items():
            if not isinstance(rel, str) or not rel or rel.startswith("/"):
                return {"ok": False, "code": "unsafe_workspace", "tests_executed": False, "error": "bad_path"}
            rel_path = Path(rel)
            if (
                not rel_path.parts
                or ".." in rel_path.parts
                or any(part.startswith(".") for part in rel_path.parts)
            ):
                return {"ok": False, "code": "unsafe_workspace", "tests_executed": False, "error": "unsafe_path"}
            if len(rel_path.parts) > effective_policy.max_depth:
                return {"ok": False, "code": "sandbox_snapshot_limit", "tests_executed": False, "error": "max_depth"}
            try:
                content = (
                    base64.b64decode(encoded, validate=True)
                    if isinstance(raw_b64, dict)
                    else str(encoded).encode("utf-8")
                )
            except (ValueError, TypeError):
                return {"ok": False, "code": "unsafe_workspace", "tests_executed": False, "error": "bad_b64"}
            total += len(content)
            if len(content) > effective_policy.max_file_bytes or total > effective_policy.max_total_bytes:
                return {"ok": False, "code": "sandbox_snapshot_limit", "tests_executed": False, "error": "size_cap"}
            target = ws_root / rel_path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except OSError as exc:
                return {
                    "ok": False,
                    "code": "unsafe_workspace",
                    "tests_executed": False,
                    "error": f"colliding_or_invalid_path:{type(exc).__name__}",
                }
        runner = SandboxRunner(policy=effective_policy, remote_url="")
        mode = payload.get("mode", "single")
        if mode == "single":
            res = runner.run(ws_root, str(payload.get("oracle_sha256", "")))
        elif mode == "suite":
            res = runner.run_suite(
                ws_root,
                payload.get("test_oracles") or {},
                int(payload.get("expected_tests", 0)),
                pattern=str(payload.get("pattern", "test*.py")),
            )
        else:
            return {"ok": False, "code": "sandbox_error", "tests_executed": False, "error": "unknown_mode"}
        expected_snap = payload.get("snapshot_sha256")
        if expected_snap and res.get("snapshot_sha256") and res["snapshot_sha256"] != expected_snap:
            return {"ok": False, "code": "test_oracle_modified", "tests_executed": False, "error": "snapshot_mismatch"}
        return res
    except (TypeError, ValueError, OSError) as exc:
        return {"ok": False, "code": "sandbox_error", "tests_executed": False, "error": str(exc)}
    finally:
        shutil.rmtree(temp_ws, ignore_errors=True)

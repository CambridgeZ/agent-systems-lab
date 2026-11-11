"""Docker 测试边界：受限快照、固定镜像、资源限制和有界日志。

容器与回执不是对任意敌对代码的完备隔离证明。此模块仍依赖可信 Docker
守护进程、内核和本地文件系统；不支持把 Docker socket 交给待执行代码。
回执可发现提前退出/未运行测试，但不能抵御同进程代码伪造结果或 monkeypatch。
"""

from __future__ import annotations

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
import time
import uuid
from typing import Any


# -I 让可信标准库先于 /workspace 加载，避免 json.py/unittest.py 遮蔽 driver。
# 加入工作区之后，业务代码仍能影响同一解释器，不能把这个回执当作恶意代码证明。
_DRIVER = r"""
import json, sys, unittest
sys.path.insert(0, "/workspace")
suite = unittest.defaultTestLoader.discover("/workspace/tests", pattern="test_pricing.py")
expected = 4
if suite.countTestCases() != expected:
    receipt = {"tests_run": 0, "expected": expected, "failures": 0, "errors": 0,
               "skipped": 0, "successful": False}
else:
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt = {"tests_run": result.testsRun, "expected": expected,
               "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "successful": result.wasSuccessful()}
print("\n" + __RECEIPT_MARKER__ + json.dumps(receipt, sort_keys=True), flush=True)
sys.exit(0 if receipt["tests_run"] == expected and not receipt["skipped"]
         and receipt["successful"] else 1)
""".strip()

# 这些只是已知凭证命名的额外排除，不能检测业务 .py 中任意硬编码的秘密。
_SENSITIVE_NAMES = frozenset({"credentials.py", "secrets.py", "secret.py", "private_key.py", "tokens.py"})
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
    def __init__(self, policy: SandboxPolicy | None = None):
        self.policy = policy or SandboxPolicy()

    @staticmethod
    def _open_directory(name: str, parent_fd: int | None = None) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _SandboxError("unsafe_workspace", "工作区不允许符号链接或非目录路径") from exc
            raise

    def _open_root(self, workspace: Path) -> int:
        path = Path(workspace)
        if ".." in path.parts:
            raise _SandboxError("unsafe_workspace", "工作区路径不能包含 ..")
        path = path.absolute()
        descriptor = self._open_directory(path.anchor)
        try:
            for component in path.parts[1:]:
                next_fd = self._open_directory(component, descriptor)
                os.close(descriptor)
                descriptor = next_fd
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

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
    def _receipt(output: str, marker: str, returncode: int | None) -> dict | None:
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
        if receipt["tests_run"] != 4 or receipt["expected"] != 4 or receipt["skipped"] != 0:
            return None
        if receipt["failures"] + receipt["errors"] > receipt["tests_run"]:
            return None
        passed = receipt["failures"] == receipt["errors"] == 0
        if receipt["successful"] != passed or returncode != (0 if passed else 1):
            return None
        return receipt

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
        try:
            # 父目录保持 0700；容器只看到 bind mount 后的 repo 根，repo 为 0755。
            # 不挂载原始 workspace，也不把主机环境变量传为 container --env。
            with tempfile.TemporaryDirectory(prefix="agent-lab-sandbox-") as temporary:
                snapshot = Path(temporary) / "repo"
                result.update(self._snapshot(Path(workspace), snapshot, oracle_sha256))
                docker = shutil.which("docker")
                if not docker:
                    result["error"] = "未安装 Docker，未执行运行时测试。"
                    return result
                inspected = self._capture(
                    [docker, "image", "inspect", "--format", "{{.Id}}", self.policy.image], 5, 256
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
                driver = _DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
                command = [
                    docker,
                    "run",
                    "--name",
                    name,
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
        return result

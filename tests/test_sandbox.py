"""用 fake Popen 与真实 OS pipe 验证边界；测试不启动 Docker 或生成代码。"""

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import httpx
import pytest

from agent_lab.sandbox import (
    SANDBOX_FAILURE_CODES,
    SANDBOX_INFRA_CODES,
    RemoteHTTPSandboxBackend,
    SandboxPolicy,
    SandboxRunner,
    discover_test_oracles,
    execute_remote_sandbox_payload,
    reap_orphan_containers,
)
import agent_lab.sandbox as sandbox


ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = "sha256:" + "a" * 64


class PipeProcess:
    def __init__(self, content=b"", returncode=0, busy=False):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.writer = write_fd if busy else None
        self.returncode = None if busy else returncode
        self.killed = False
        if content:
            # 所有 fake fixture 小于最小常见 pipe 容量，不制造宿主线程/磁盘洪泛。
            assert len(content) <= 4096
            os.write(write_fd, content)
        if not busy:
            os.close(write_fd)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-docker", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None


class FakeDocker:
    def __init__(self, mode="passed", inspect_output=IMAGE_ID, cleanup="ok", on_run=None):
        self.mode = mode
        self.inspect_output = inspect_output
        self.cleanup = cleanup
        self.on_run = on_run
        self.calls = []
        self.run_process = None
        self.current = None

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.STDOUT
        if command[1:3] == ["image", "inspect"]:
            process = PipeProcess((self.inspect_output + "\n").encode())
        elif command[1] == "rm":
            if self.cleanup == "oserror":
                raise OSError("fake cleanup failure")
            process = PipeProcess(b"removed\n", returncode=0 if self.cleanup == "ok" else 1)
        else:
            assert command[1] == "run"
            if self.on_run:
                self.on_run(command)
            if self.mode == "oserror":
                raise OSError("fake CLI unavailable")
            if self.mode == "timeout":
                process = PipeProcess(b"", busy=True)
            elif self.mode == "output_limit":
                process = PipeProcess(b"x" * 1000, busy=True)
            elif self.mode == "invalid_utf8":
                process = PipeProcess(b"\xff" * 1000, busy=True)
            elif self.mode == "missing_receipt":
                process = PipeProcess(b"exited before running any test\n")
            elif self.mode == "oom_137":
                process = PipeProcess(b"Killed\n", returncode=137)
            else:
                marker = re.search(r"AGENT_LAB_RECEIPT_[0-9a-f]{32}:", command[-1]).group()
                receipt = {
                    "tests_run": 4,
                    "expected": 4,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                    "successful": True,
                }
                returncode = 0
                if self.mode == "tests_failed":
                    receipt.update(failures=1, successful=False)
                    returncode = 1
                elif self.mode == "contradictory":
                    receipt.update(failures=1)
                elif self.mode == "impossible_counts":
                    receipt.update(failures=100, successful=False)
                    returncode = 1
                elif self.mode == "skipped":
                    receipt.update(skipped=1)
                log = "PROBE_EXECUTED\n" + marker + json.dumps(receipt) + "\n"
                if self.mode == "duplicate":
                    log += marker + json.dumps(receipt) + "\n"
                process = PipeProcess(log.encode(), returncode=returncode)
            self.run_process = process
        self.current = (command[1], process)
        return process


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", root)
    oracle = hashlib.sha256((root / "tests/test_pricing.py").read_bytes()).hexdigest()
    return root, oracle


@pytest.fixture
def fake_docker(monkeypatch):
    def install(**kwargs):
        fake = FakeDocker(**kwargs)
        monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/fake/docker")
        monkeypatch.setattr(sandbox.subprocess, "Popen", fake)
        return fake

    return install


def test_uses_immutable_image_fixed_restrictions_and_consistent_test_receipt(
    workspace, fake_docker, monkeypatch
):
    root, oracle = workspace
    monkeypatch.setenv("SANDBOX_HOST_SECRET", "do-not-forward")
    fake = fake_docker()
    result = SandboxRunner().run(root, oracle)
    assert result["ok"] and result["code"] == "passed" and result["tests_executed"] is True
    assert result["cleanup_verified"] and result["cleanup_error"] is None
    assert result["test_summary"]["tests_run"] == 4
    assert "PROBE_EXECUTED" in result["output"]
    assert result["image_id"] == IMAGE_ID and result["image_sha256"] == "a" * 64
    for key in ("snapshot_sha256", "policy_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", result[key])
    inspect_command, run_command, cleanup_command = [command for command, _ in fake.calls]
    assert inspect_command == ["/fake/docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-slim"]
    assert "python:3.12-slim" not in run_command
    assert run_command[run_command.index(IMAGE_ID) + 1 :][:4] == ["python", "-I", "-B", "-c"]
    for flag in (
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
    ):
        assert flag in run_command
    assert "/tmp:rw,noexec,nosuid,size=16m" in run_command
    assert "--rm" not in run_command
    assert not any(
        "SANDBOX_HOST_SECRET" in argument or "do-not-forward" in argument for argument in run_command
    )
    assert cleanup_command == ["/fake/docker", "rm", "-f", run_command[run_command.index("--name") + 1]]


def test_snapshot_contains_only_allowed_python_and_is_removed_after_run(workspace, fake_docker):
    root, oracle = workspace
    (root / ".env").write_text("KEY=private")
    (root / ".git").mkdir()
    (root / ".git/config.py").write_text("SECRET=True")
    (root / "credentials.py").write_text("TOKEN='private'")
    (root / "nested").mkdir()
    (root / "nested/helper.py").write_text("VALUE = 1")
    original = (root / "pricing.py").read_bytes()
    mounted = []

    def inspect_snapshot(command):
        mount = command[command.index("--mount") + 1]
        snapshot = Path(re.search(r"source=(.+),target=/workspace,readonly$", mount).group(1))
        mounted.append(snapshot)
        assert snapshot != root and not snapshot.is_relative_to(root)
        assert not (snapshot / ".env").exists() and not (snapshot / ".git").exists()
        assert not (snapshot / "credentials.py").exists() and not (snapshot / "README.md").exists()
        assert (snapshot / "nested/helper.py").read_text() == "VALUE = 1"
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o755
        assert stat.S_IMODE((snapshot / "pricing.py").stat().st_mode) == 0o444
        assert (snapshot / "pricing.py").read_bytes() == original

    fake_docker(on_run=inspect_snapshot)
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "passed" and result["snapshot_file_count"] == 3
    assert mounted and not mounted[0].exists()
    assert (root / "pricing.py").read_bytes() == original


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory_symlink"])
def test_snapshot_rejects_links_and_special_files_without_starting_cli(workspace, fake_docker, kind):
    root, oracle = workspace
    if kind == "symlink":
        (root / "alias.py").symlink_to(root / "pricing.py")
    elif kind == "hardlink":
        os.link(root / "pricing.py", root / "alias.py")
    elif kind == "fifo":
        os.mkfifo(root / "pipe")
    else:
        (root / "alias").symlink_to(root / "tests", target_is_directory=True)
    fake = fake_docker()
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "unsafe_workspace" and result["tests_executed"] is False
    assert fake.calls == []


@pytest.mark.parametrize(
    "policy", [SandboxPolicy(max_files=1), SandboxPolicy(max_total_bytes=20), SandboxPolicy(max_entries=1)]
)
def test_snapshot_budgets_fail_closed(workspace, fake_docker, policy):
    root, oracle = workspace
    fake = fake_docker()
    result = SandboxRunner(policy).run(root, oracle)
    assert result["code"] == "sandbox_snapshot_limit" and result["tests_executed"] is False
    assert fake.calls == []


@pytest.mark.parametrize("size,expected", [(65_536, "passed"), (65_537, "sandbox_snapshot_limit")])
def test_single_python_file_size_boundary(workspace, fake_docker, size, expected):
    root, oracle = workspace
    (root / "large.py").write_bytes(b"#" * size)
    fake_docker()
    assert SandboxRunner().run(root, oracle)["code"] == expected


def test_oracle_is_revalidated_after_copy(workspace, fake_docker, monkeypatch):
    root, oracle = workspace
    original_write = Path.write_bytes

    def corrupted_copy(self, content):
        return (
            original_write(self, content + b"\n# tampered")
            if self.name == "test_pricing.py"
            else original_write(self, content)
        )

    monkeypatch.setattr(Path, "write_bytes", corrupted_copy)
    fake = fake_docker()
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "test_oracle_modified" and not fake.calls


@pytest.mark.parametrize("image_output", ["python:3.12-slim", "sha256:" + "a" * 63, IMAGE_ID + "\nmalformed"])
def test_invalid_or_ambiguous_image_id_cannot_start_container(workspace, fake_docker, image_output):
    root, oracle = workspace
    fake = fake_docker(inspect_output=image_output)
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "sandbox_unavailable" and result["tests_executed"] is False
    assert len(fake.calls) == 1 and not result["cleanup_attempted"]


@pytest.mark.parametrize("mode", ["missing_receipt", "contradictory", "impossible_counts", "skipped", "duplicate"])
def test_zero_exit_is_not_passed_without_complete_consistent_receipt(workspace, fake_docker, mode):
    root, oracle = workspace
    fake_docker(mode=mode)
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "sandbox_incomplete" and not result["ok"]
    assert result["tests_executed"] is None and result["cleanup_verified"]


def test_nonzero_with_complete_receipt_is_known_test_failure(workspace, fake_docker):
    root, oracle = workspace
    fake_docker(mode="tests_failed")
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "tests_failed" and result["tests_executed"] is True
    assert result["test_summary"]["failures"] == 1 and not result["ok"]


def test_output_limit_reads_exactly_cap_plus_one_then_kills_cli_and_cleans_container(
    workspace, fake_docker, monkeypatch
):
    root, oracle = workspace
    fake = fake_docker(mode="output_limit")
    original_read = os.read
    read_bytes = []

    def observed_read(descriptor, size):
        part = original_read(descriptor, size)
        if fake.current and fake.current[0] == "run" and descriptor == fake.current[1].stdout.fileno():
            read_bytes.append(len(part))
        return part

    monkeypatch.setattr(sandbox.os, "read", observed_read)
    result = SandboxRunner(SandboxPolicy(output_limit_bytes=64)).run(root, oracle)
    assert result["code"] == "sandbox_output_limit" and result["tests_executed"] is None
    assert len(result["output"].encode()) == 64 and sum(read_bytes) == 65
    assert result["output_truncated"] and fake.run_process.killed
    assert result["cleanup_verified"] and fake.calls[-1][0][1] == "rm"


def test_invalid_utf8_display_still_obeys_byte_cap(workspace, fake_docker):
    root, oracle = workspace
    fake_docker(mode="invalid_utf8")
    result = SandboxRunner(SandboxPolicy(output_limit_bytes=64)).run(root, oracle)
    assert result["code"] == "sandbox_output_limit" and len(result["output"].encode()) <= 64


def test_timeout_kills_cli_and_returns_unknown_execution_with_explicit_cleanup(workspace, fake_docker):
    root, oracle = workspace
    fake = fake_docker(mode="timeout")
    result = SandboxRunner(SandboxPolicy(timeout_seconds=0.01)).run(root, oracle)
    assert result["code"] == "sandbox_timeout" and result["tests_executed"] is None
    assert fake.run_process.killed and result["cleanup_verified"]


@pytest.mark.parametrize("cleanup,error", [("failed", "docker_rm_exit_1"), ("oserror", "OSError")])
def test_cleanup_failure_cannot_be_reported_as_success(workspace, fake_docker, cleanup, error):
    root, oracle = workspace
    fake_docker(cleanup=cleanup)
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "sandbox_cleanup_failed" and result["execution_code"] == "passed"
    assert result["tests_executed"] is True and not result["ok"]
    assert result["cleanup_attempted"] and not result["cleanup_verified"] and result["cleanup_error"] == error


def test_spawn_oserror_fails_closed_and_still_attempts_cleanup(workspace, fake_docker):
    root, oracle = workspace
    fake = fake_docker(mode="oserror")
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "sandbox_error" and result["tests_executed"] is None
    assert result["cleanup_verified"] and fake.calls[-1][0][1] == "rm"


def test_missing_docker_does_not_execute_tests(workspace, monkeypatch):
    root, oracle = workspace
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "sandbox_unavailable" and result["tests_executed"] is False
    assert result["cleanup_verified"] and not result["cleanup_attempted"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 31},
        {"timeout_seconds": 0},
        {"output_limit_bytes": 16_385},
        {"max_files": 129},
        {"max_file_bytes": 65_537},
        {"max_total_bytes": 2_097_153},
        {"max_files": True},
        {"image": "other:latest"},
    ],
)
def test_policy_cannot_relax_fixed_security_upper_limits(kwargs):
    with pytest.raises(ValueError, match="sandbox_policy_invalid"):
        SandboxPolicy(**kwargs)


def test_snapshot_hash_changes_only_with_copied_content_and_policy_hash_tracks_policy(workspace, fake_docker):
    root, oracle = workspace
    fake_docker()
    first = SandboxRunner().run(root, oracle)
    (root / ".env").write_text("not copied")
    excluded_change = SandboxRunner().run(root, oracle)
    assert first["snapshot_sha256"] == excluded_change["snapshot_sha256"]
    (root / "pricing.py").write_text("VALUE = 42\n")
    copied_change = SandboxRunner(SandboxPolicy(timeout_seconds=2)).run(root, oracle)
    assert first["snapshot_sha256"] != copied_change["snapshot_sha256"]
    assert first["policy_sha256"] != copied_change["policy_sha256"]


def test_canonical_error_codes_and_managed_labels(workspace, fake_docker):
    assert SANDBOX_FAILURE_CODES.isdisjoint(SANDBOX_INFRA_CODES)
    assert {"tests_failed", "sandbox_timeout", "sandbox_oom", "sandbox_incomplete"} <= SANDBOX_FAILURE_CODES
    assert {"sandbox_unavailable", "daemon_error", "sandbox_cleanup_failed"} <= SANDBOX_INFRA_CODES
    root, oracle = workspace
    fake = fake_docker()
    SandboxRunner().run(root, oracle)
    run_command = fake.calls[1][0]
    assert "agent-lab.managed=1" in run_command
    assert any(arg.startswith("agent-lab.expires-at=") for arg in run_command)


def test_two_process_driver_blocks_stdout_receipt_spoofing_and_frame_introspection(workspace):
    root, _ = workspace
    marker = "AGENT_LAB_RECEIPT_" + "b" * 32 + ":"
    forged = '{"tests_run": 4, "expected": 4, "failures": 0, "errors": 0, "skipped": 0, "successful": true}'
    (root / "pricing.py").write_text(
        f"""
import os, sys
blocked = []
try:
    sys._getframe(0)
except PermissionError:
    blocked.append("getframe")
try:
    import ctypes
except PermissionError:
    blocked.append("ctypes")
print("AUDIT_BLOCKED=" + ",".join(blocked), flush=True)
print({marker!r} + {forged!r}, flush=True)
os.write(1, b"AGENT_LAB_")
os.write(1, (b"RECEIPT_" + b"b" * 32 + b":" + {forged.encode()!r} + b"\\n"))
os._exit(0)
""",
        encoding="utf-8",
    )
    driver = (
        sandbox._DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
        .replace("__INNER_TIMEOUT__", "5.0")
        .replace("__WORKSPACE_DIR__", repr(str(root)))
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", driver],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "AUDIT_BLOCKED=getframe,ctypes" in proc.stdout
    assert proc.stdout.count("[SPOOF_BLOCKED_RECEIPT_]") == 2
    assert marker not in proc.stdout
    assert SandboxRunner._receipt(proc.stdout, marker, proc.returncode) is None


def test_suite_driver_executes_pytest_functions_src_layout_and_json_fixtures(tmp_path):
    repo = tmp_path / "src_repo"
    (repo / "src/demo_pkg").mkdir(parents=True)
    (repo / "src/demo_pkg/__init__.py").write_text("", encoding="utf-8")
    (repo / "src/demo_pkg/calc.py").write_text(
        "def scale_and_add(x: int, factor: int, offset: int) -> int:\n    return x * factor + offset\n",
        encoding="utf-8",
    )
    (repo / "root_ignored.json").write_text('{"ignored": true}', encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/data.json").write_text('{"factor": 3, "offset": 4}', encoding="utf-8")
    (repo / "tests/secrets.json").write_text('{"api_key": "nope"}', encoding="utf-8")
    (repo / "tests/test_calc.py").write_text(
        """
import json
from pathlib import Path
from demo_pkg.calc import scale_and_add

def test_sync_from_fixture():
    cfg = json.loads((Path(__file__).parent / "data.json").read_text(encoding="utf-8"))
    assert scale_and_add(5, cfg["factor"], cfg["offset"]) == 19

async def test_async_from_src():
    assert scale_and_add(2, 10, 1) == 21
""",
        encoding="utf-8",
    )
    oracles, expected = discover_test_oracles(repo)
    assert expected == 2 and list(oracles) == ["tests/test_calc.py"]

    snap = tmp_path / "snap"
    meta = SandboxRunner()._snapshot_suite(repo, snap, oracles)
    assert meta["snapshot_file_count"] == 4
    assert (snap / "tests/data.json").is_file()
    assert not (snap / "tests/secrets.json").exists()
    assert not (snap / "root_ignored.json").exists()

    marker = "AGENT_LAB_RECEIPT_" + "c" * 32 + ":"
    suite_driver = (
        sandbox._SUITE_DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
        .replace("__INNER_TIMEOUT__", "5.0")
        .replace("__WORKSPACE_DIR__", repr(str(snap)))
        .replace("__TEST_PATTERN__", repr("test*.py"))
        .replace("__EXPECTED_TESTS__", str(expected))
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", suite_driver],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stdout
    receipt = SandboxRunner._receipt(proc.stdout, marker, proc.returncode, expected_tests=2)
    assert receipt == {
        "tests_run": 2,
        "expected": 2,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "successful": True,
    }


def test_rmtree_ebusy_preserves_sandbox_timeout_and_flips_passed_only(workspace, fake_docker, monkeypatch):
    root, oracle = workspace
    real_rmtree = shutil.rmtree

    def busy_rmtree(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(sandbox.shutil, "rmtree", busy_rmtree)
    fake_docker(mode="timeout")
    timed_out = SandboxRunner(SandboxPolicy(timeout_seconds=0.01)).run(root, oracle)
    assert timed_out["code"] == "sandbox_timeout" and not timed_out["ok"]
    assert not timed_out["cleanup_verified"] and timed_out["cleanup_error"] == "rmtree_EBUSY"

    fake_docker(mode="passed")
    passed_but_busy = SandboxRunner().run(root, oracle)
    assert passed_but_busy["code"] == "sandbox_cleanup_failed"
    assert passed_but_busy["execution_code"] == "passed" and not passed_but_busy["ok"]
    assert not passed_but_busy["cleanup_verified"] and passed_but_busy["cleanup_error"] == "rmtree_EBUSY"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses 0111 read restrictions")
def test_non_readable_0711_ancestor_directory_supported(tmp_path, fake_docker):
    restricted = tmp_path / "search_only_ancestor"
    restricted.mkdir(mode=0o755)
    repo = restricted / "repo"
    shutil.copytree(ROOT / "data/sample_repo", repo)
    oracle = hashlib.sha256((repo / "tests/test_pricing.py").read_bytes()).hexdigest()
    restricted.chmod(0o111)
    try:
        fake_docker()
        res = SandboxRunner().run(repo, oracle)
        assert res["ok"] and res["code"] == "passed"
    finally:
        restricted.chmod(0o755)


def test_exit_137_marks_oom_killed_while_keeping_sandbox_incomplete(workspace, fake_docker):
    root, oracle = workspace
    fake_docker(mode="oom_137")
    result = SandboxRunner().run(root, oracle)
    assert result["code"] == "sandbox_incomplete" and not result["ok"]
    assert result["oom_killed"] is True and result["failure_reason"] == "oom_killed"
    assert result["exit_code"] == 137 and result["cleanup_verified"]


def test_reap_orphan_containers_removes_only_expired_managed_containers(monkeypatch):
    expired_id = "111111111111aaaa"
    active_id = "222222222222bbbb"
    seen = []

    def fake_popen(command, **kwargs):
        seen.append(command)
        if command[1] == "ps":
            return PipeProcess(f"{expired_id}\n{active_id}\n".encode())
        if command[1] == "inspect":
            return PipeProcess(f"{expired_id}\t1000\n{active_id}\t2000\n".encode())
        assert command == ["/fake/docker", "rm", "-f", expired_id]
        return PipeProcess(b"removed\n")

    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/fake/docker")
    monkeypatch.setattr(sandbox.subprocess, "Popen", fake_popen)
    report = reap_orphan_containers(now_ts=1500.0)
    assert report == {"ok": True, "code": "ok", "scanned": 2, "reaped": [expired_id], "errors": []}
    assert len(seen) == 3


def test_remote_http_sandbox_backend_validates_locally_and_executes_payload(
    workspace, fake_docker, monkeypatch
):
    root, oracle = workspace
    (root / ".env").write_text("SECRET=never-ship", encoding="utf-8")
    fake = fake_docker()
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert str(request.url) == "https://sandbox.internal/api/internal/sandbox/execute"
        assert request.headers.get("Authorization") == "Bearer test-token"
        body = json.loads(request.content.decode("utf-8"))
        assert ".env" not in body["files_b64"]
        return httpx.Response(200, json=execute_remote_sandbox_payload(body))

    backend = RemoteHTTPSandboxBackend(
        "https://sandbox.internal/",
        api_key="test-token",
        transport=httpx.MockTransport(handler),
    )
    res = SandboxRunner(remote_backend=backend).run(root, oracle)
    assert res["ok"] and res["code"] == "passed" and res["test_summary"]["tests_run"] == 4
    assert len(requests_seen) == 1 and len(fake.calls) == 3

    # Symlink in local workspace must fail closed locally BEFORE any HTTP call.
    (root / "bad_link.py").symlink_to(root / "pricing.py")
    unsafe_res = SandboxRunner(remote_backend=backend).run(root, oracle)
    assert unsafe_res["code"] == "unsafe_workspace" and len(requests_seen) == 1
    (root / "bad_link.py").unlink()

    # Non-200 remote HTTP status fails closed as sandbox_unavailable.
    down_backend = RemoteHTTPSandboxBackend(
        "https://sandbox.internal",
        transport=httpx.MockTransport(lambda req: httpx.Response(503, text="unavailable")),
    )
    monkeypatch.setenv("AGENT_LAB_SANDBOX_URL", "https://sandbox.internal")
    down_res = SandboxRunner(remote_backend=down_backend).run(root, oracle)
    assert down_res["code"] == "sandbox_unavailable" and not down_res["ok"]


def test_worker_thread_blocks_tb_frame_walking_receipt_spoof(workspace):
    root, _ = workspace
    marker = "AGENT_LAB_RECEIPT_" + "d" * 32 + ":"
    (root / "pricing.py").write_text(
        """
import sys

leaked = []
cf_blocked = False
try:
    sys._current_frames()
except PermissionError:
    cf_blocked = True

forged = {"tests_run": 4, "expected": 4, "failures": 0, "errors": 0, "skipped": 0, "successful": True}
try:
    1 / 0
except ZeroDivisionError as exc:
    fr = exc.__traceback__.tb_frame
    frames_seen = []
    while fr is not None:
        frames_seen.append(fr.f_code.co_name)
        for name, val in list(fr.f_locals.items()):
            if name in ("_send", "_key", "key", "secret", "w_fd"):
                leaked.append(name)
            if callable(val) and getattr(val, "__name__", "") == "_send":
                val(forged)
                leaked.append("called_send")
            if name == "worker_out" and isinstance(val, list):
                val.append(forged)
        fr = fr.f_back
print(f"WALK_RESULT leaked={leaked} cf_blocked={cf_blocked} root={frames_seen[-1]}", flush=True)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    driver = (
        sandbox._DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
        .replace("__INNER_TIMEOUT__", "5.0")
        .replace("__WORKSPACE_DIR__", repr(str(root)))
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", driver],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "WALK_RESULT leaked=[] cf_blocked=True root=_bootstrap" in proc.stdout
    assert '"successful": true' not in proc.stdout
    assert SandboxRunner._receipt(proc.stdout, marker, proc.returncode) is None


def test_suite_driver_discovers_nested_subdirs_without_init_py(tmp_path):
    repo = tmp_path / "nested_repo"
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "src/pkg/__init__.py").write_text("", encoding="utf-8")
    (repo / "src/pkg/math_ops.py").write_text("def double(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
    (repo / "tests/sub_no_init").mkdir(parents=True)
    (repo / "tests/other_no_init").mkdir(parents=True)
    (repo / "tests/api_key.json").write_text('{"token": "redact"}', encoding="utf-8")
    (repo / "tests/test_top.py").write_text(
        "from pkg.math_ops import double\n\ndef test_top():\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    (repo / "tests/sub_no_init/test_nested.py").write_text(
        """
import unittest
from pkg.math_ops import double

def test_nested_fn():
    assert double(4) == 8

async def test_nested_async():
    assert double(5) == 10

class TestPlainNested:
    def test_plain_method(self):
        assert double(6) == 12

class NestedUnitCase(unittest.TestCase):
    def test_unit_method(self):
        self.assertEqual(double(7), 14)
""",
        encoding="utf-8",
    )
    (repo / "tests/other_no_init/test_nested.py").write_text(
        "from pkg.math_ops import double\n\ndef test_duplicate_basename():\n    assert double(8) == 16\n",
        encoding="utf-8",
    )
    oracles, expected = discover_test_oracles(repo)
    assert expected == 6 and len(oracles) == 3

    snap = tmp_path / "snap_nested"
    SandboxRunner()._snapshot_suite(repo, snap, oracles)
    assert not (snap / "tests/api_key.json").exists()

    marker = "AGENT_LAB_RECEIPT_" + "e" * 32 + ":"
    suite_driver = (
        sandbox._SUITE_DRIVER.replace("__RECEIPT_MARKER__", repr(marker))
        .replace("__INNER_TIMEOUT__", "5.0")
        .replace("__WORKSPACE_DIR__", repr(str(snap)))
        .replace("__TEST_PATTERN__", repr("test*.py"))
        .replace("__EXPECTED_TESTS__", str(expected))
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", suite_driver],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stdout
    receipt = SandboxRunner._receipt(proc.stdout, marker, proc.returncode, expected_tests=expected)
    assert receipt == {
        "tests_run": 6,
        "expected": 6,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "successful": True,
    }


@pytest.mark.parametrize(
    "files_b64",
    [
        {"a": "eA==", "a/b.py": "eA=="},
        {"a/b.py": "eA==", "a": "eA=="},
    ],
)
def test_execute_remote_sandbox_payload_rejects_colliding_file_and_dir_paths(files_b64):
    res = execute_remote_sandbox_payload({"mode": "single", "files_b64": files_b64, "oracle_sha256": "0" * 64})
    assert res["ok"] is False
    assert res["code"] == "unsafe_workspace"
    assert res["tests_executed"] is False


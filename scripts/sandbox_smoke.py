"""在已准备 Docker 镜像的 CI 上验证真实代码执行路径。

先证明受保护测试能捕获原始 bug，再经真实审批路径应用补丁。额外样本在容器内
验证实际隔离状态、超时、输出限额与清理结果；不能只检查 docker 参数是否存在。
宿主进程只把探针作为文本写入样本，不导入或执行业务文件。需先准备镜像。
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path

from agent_lab.api import ROOT
from agent_lab.runtime import AgentRuntime
from agent_lab.sandbox import SandboxPolicy, SandboxRunner
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


HOST_ENV_NAME = "AGENT_LAB_SANDBOX_HOST_SENTINEL"
PROBE_MARKER = "AGENT_LAB_ISOLATION_PROBE_OK"
REPORT_PATH = ROOT / "reports/sandbox-smoke.json"

# 这段字符串只在容器导入 pricing.py 时执行。通过受保护测试且出现 marker，才说明
# 探针真的运行过；宿主端不会 import、exec 或 subprocess 执行这个业务文件。
ISOLATION_PROBE = r"""
def _agent_lab_isolation_probe():
    import errno
    import os
    from pathlib import Path
    import socket

    def require(condition, message):
        if not condition:
            raise RuntimeError("sandbox isolation probe: " + message)

    def deny_write(path):
        try:
            with path.open("w", encoding="utf-8") as handle:
                handle.write("must not be writable")
        except OSError as exc:
            require(exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}, "unexpected write error")
        else:
            raise RuntimeError("sandbox isolation probe: write unexpectedly succeeded")

    def deny_read(path):
        try:
            path.read_bytes()
        except OSError as exc:
            require(exc.errno in {errno.ENOENT, errno.EACCES, errno.EPERM}, "unexpected read error")
        else:
            raise RuntimeError("sandbox isolation probe: host sentinel was readable")

    require(os.getuid() != 0 and os.geteuid() != 0, "container runs as root")
    workspace = Path(__file__).resolve().parent
    # 不能把非 root 导致的 EACCES 误认成根文件系统只读；同时核验挂载标志与写入。
    require(os.statvfs("/").f_flag & os.ST_RDONLY, "root filesystem is writable")
    require(os.statvfs(workspace).f_flag & os.ST_RDONLY, "workspace mount is writable")
    deny_write(Path("/agent-lab-root-write-probe"))
    deny_write(workspace / "agent_lab_workspace_write_probe.py")

    interfaces = {
        line.partition(":")[0].strip()
        for line in Path("/proc/net/dev").read_text().splitlines()
        if ":" in line
    }
    require(interfaces == {"lo"}, "network namespace has an external interface")
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(1)
    try:
        connection.connect(("1.1.1.1", 443))
    except OSError:
        pass
    else:
        raise RuntimeError("sandbox isolation probe: external socket connected")
    finally:
        connection.close()

    require("AGENT_LAB_SANDBOX_HOST_SENTINEL" not in os.environ, "host environment leaked")
    for socket_path in ("/var/run/docker.sock", "/run/docker.sock"):
        require(not Path(socket_path).exists(), "Docker socket was exposed")
    status = {
        key: value.strip()
        for line in Path("/proc/self/status").read_text().splitlines()
        for key, separator, value in [line.partition(":")]
        if separator
    }
    require(int(status["CapEff"], 16) == 0, "effective capabilities are nonzero")
    require(status["NoNewPrivs"] == "1", "no-new-privileges is disabled")
    # Docker daemon 若禁用了 seccomp，这项真实探针失败，CI 环境应被视为不合格。
    require(status["Seccomp"] == "2", "seccomp filter is disabled")

    deny_read(workspace / ".env")
    deny_read(Path(__HOST_ENV_PATH__))
    deny_read(Path(__OUTSIDE_SENTINEL_PATH__))
    print("AGENT_LAB_ISOLATION_PROBE_OK", flush=True)

_agent_lab_isolation_probe()
"""


def fresh_workspace(folder: Path, name: str) -> Path:
    workspace = folder / name
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    return workspace


def fixed_source(workspace: Path) -> str:
    original = (workspace / "pricing.py").read_text(encoding="utf-8")
    old = "return subtotal * (1 - discount_percent)"
    assert original.count(old) == 1, "样本源代码发生变化，必须更新 smoke 的已知最小修复"
    return original.replace(old, "return subtotal * (1 - discount_percent / 100)")


def oracle_digest(workspace: Path) -> str:
    return hashlib.sha256((workspace / "tests/test_pricing.py").read_bytes()).hexdigest()


def require_cleaned(result: dict) -> None:
    assert result.get("cleanup_verified") is True, result
    assert not result.get("cleanup_error"), result


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(REPORT_PATH)


def observe(report: dict, case: str, expected_code: str, result: dict) -> None:
    """先保存已观察事实，再做断言；失败日志不包含临时路径、hostenv 或原始输出。"""
    output = result.get("output", "").encode("utf-8")
    observation = {
        "case": case,
        "expected_code": expected_code,
        **{
            key: result.get(key)
            for key in (
                "code",
                "ok",
                "tests_executed",
                "image_id",
                "snapshot_sha256",
                "policy_sha256",
                "cleanup_verified",
            )
        },
        "cleanup_error_present": bool(result.get("cleanup_error")),
        "captured_output_bytes": len(output),
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    report["cases"].append(observation)
    write_report(report)


async def isolation_smoke(folder: Path, report: dict) -> None:
    workspace = fresh_workspace(folder, "isolation-repo")
    oracle = oracle_digest(workspace)
    outside = folder / "outside-host-sentinel.txt"
    outside.write_text("fake host-only sentinel; never a real credential\n", encoding="utf-8")
    host_env_file = workspace / ".env"
    host_env_file.write_text("FAKE_SECRET=not-a-real-credential\n", encoding="utf-8")
    probe = ISOLATION_PROBE.replace("__HOST_ENV_PATH__", repr(str(host_env_file))).replace(
        "__OUTSIDE_SENTINEL_PATH__", repr(str(outside))
    )
    (workspace / "pricing.py").write_text(probe + "\n" + fixed_source(workspace), encoding="utf-8")
    previous = os.environ.get(HOST_ENV_NAME)
    os.environ[HOST_ENV_NAME] = "must-not-enter-container"
    report["current_case"] = "isolation"
    try:
        result = await asyncio.to_thread(SandboxRunner().run, workspace, oracle)
    finally:
        if previous is None:
            os.environ.pop(HOST_ENV_NAME, None)
        else:
            os.environ[HOST_ENV_NAME] = previous
    observe(report, "isolation", "passed", result)
    assert result["code"] == "passed" and result["tests_executed"] is True, result
    assert PROBE_MARKER in result["output"], result
    require_cleaned(result)
    assert oracle_digest(workspace) == oracle, "受保护验收测试被改变"
    assert host_env_file.read_text() == "FAKE_SECRET=not-a-real-credential\n"
    assert outside.read_text() == "fake host-only sentinel; never a real credential\n"


async def resource_limit_smoke(folder: Path, report: dict) -> None:
    samples = [
        (
            "timeout-repo",
            "import time\nprint('AGENT_LAB_SLEEP_ENTERED', flush=True)\ntime.sleep(3600)\n",
            SandboxPolicy(timeout_seconds=5),
            "sandbox_timeout",
        ),
        (
            "output-repo",
            "import os\nwhile True:\n    os.write(1, b'X' * 4096)\n",
            SandboxPolicy(timeout_seconds=5, output_limit_bytes=2048),
            "sandbox_output_limit",
        ),
    ]
    for name, prefix, policy, expected in samples:
        report["current_case"] = name
        workspace = fresh_workspace(folder, name)
        oracle = oracle_digest(workspace)
        (workspace / "pricing.py").write_text(prefix + "\n" + fixed_source(workspace), encoding="utf-8")
        result = await asyncio.to_thread(SandboxRunner(policy=policy).run, workspace, oracle)
        observe(report, name, expected, result)
        assert result["code"] == expected, result
        assert result["tests_executed"] is None, result
        require_cleaned(result)
        assert oracle_digest(workspace) == oracle, "受保护验收测试被改变"
        if expected == "sandbox_output_limit":
            assert len(result["output"].encode("utf-8")) <= policy.output_limit_bytes, result
        else:
            assert "AGENT_LAB_SLEEP_ENTERED" in result["output"], result


async def incomplete_run_smoke(folder: Path, report: dict) -> None:
    report["current_case"] = "early_exit"
    workspace = fresh_workspace(folder, "early-exit-repo")
    oracle = oracle_digest(workspace)
    payload = "import os\nos._exit(0)\n\n" + fixed_source(workspace)
    (workspace / "pricing.py").write_text(payload, encoding="utf-8")
    result = await asyncio.to_thread(SandboxRunner().run, workspace, oracle)
    observe(report, "early_exit", "sandbox_incomplete", result)
    assert result["code"] == "sandbox_incomplete", result
    assert result["tests_executed"] is not True, result
    require_cleaned(result)
    assert oracle_digest(workspace) == oracle, "受保护验收测试被改变"


async def main():
    report = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "status": "running",
        "cases": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="agent-lab-sandbox-") as temporary:
            folder = Path(temporary)
            workspace = fresh_workspace(folder, "repo")
            storage = Storage(folder / "state.db")
            engine = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills")
            run = engine.create("修复 pricing.py 百分比折扣 bug")
            registry = ToolRegistry(storage, workspace, engine.knowledge, run["id"])
            report["current_case"] = "baseline_bug"
            baseline = await asyncio.to_thread(registry.sandbox_test)
            observe(report, "baseline_bug", "tests_failed", baseline)
            assert baseline["code"] == "tests_failed" and baseline["tests_executed"], baseline
            require_cleaned(baseline)
            report["current_case"] = "approval_workflow"
            pending = await engine.execute(run["id"])
            assert pending["status"] == "waiting_approval", pending
            proposal = storage.proposals(run["id"])[0]
            final = await engine.approve(run["id"], proposal["id"], True)
            results = [
                event["payload"]["result"]
                for event in storage.events(run["id"])
                if event["kind"] == "tool_result" and event["payload"]["name"] == "sandbox_test"
            ]
            for result in results:
                observe(report, "approved_fix", "passed", result)
            assert final["status"] == "completed", final
            assert len(results) == 1 and results[0]["code"] == "passed", results
            require_cleaned(results[0])
            await isolation_smoke(folder, report)
            await resource_limit_smoke(folder, report)
            await incomplete_run_smoke(folder, report)
            report["status"] = "passed"
            report.pop("current_case", None)
            print("Docker 验证通过：业务回归与审批、实际隔离探针、执行回执、超时和输出限额、容器清理。")
    except BaseException as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_report(report)


if __name__ == "__main__":
    asyncio.run(main())

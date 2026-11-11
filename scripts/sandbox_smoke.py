"""在已准备 Docker 镜像的 CI 上验证真实代码执行路径。

先证明受保护测试能捕获原始 bug，再经真实审批路径应用补丁，并检查测试通过。
本脚本不会把生成代码导入宿主进程。运行前需 docker pull python:3.12-slim。
"""

import asyncio
import shutil
import tempfile
from pathlib import Path

from agent_lab.api import ROOT
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


async def main():
    with tempfile.TemporaryDirectory(prefix="agent-lab-sandbox-") as temporary:
        folder = Path(temporary)
        workspace = folder / "repo"
        shutil.copytree(ROOT / "data/sample_repo", workspace)
        storage = Storage(folder / "state.db")
        engine = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills")
        run = engine.create("修复 pricing.py 百分比折扣 bug")
        registry = ToolRegistry(storage, workspace, engine.knowledge, run["id"])
        baseline = await asyncio.to_thread(registry.sandbox_test)
        assert baseline["code"] == "tests_failed" and baseline["tests_executed"], baseline
        pending = await engine.execute(run["id"])
        assert pending["status"] == "waiting_approval", pending
        proposal = storage.proposals(run["id"])[0]
        final = await engine.approve(run["id"], proposal["id"], True)
        assert final["status"] == "completed", final
        results = [
            event["payload"]["result"]
            for event in storage.events(run["id"])
            if event["kind"] == "tool_result" and event["payload"]["name"] == "sandbox_test"
        ]
        assert len(results) == 1 and results[0]["code"] == "passed", results
        print("Docker 验证通过：原始业务测试失败，审批后的修复通过受保护测试。")


if __name__ == "__main__":
    asyncio.run(main())

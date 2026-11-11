"""执行锁覆盖模型请求和同步线程：取消协程后也不能提前允许恢复。"""

import asyncio
from contextlib import suppress
import shutil
import threading

import pytest

from agent_lab.api import ROOT
from agent_lab.execution_lock import RunBusy
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


CALL_ID = "read-once"


def tool_response():
    return {
        "content": [
            {"type": "tool_use", "id": CALL_ID, "name": "repo_read", "input": {"path": "pricing.py"}}
        ],
        "stop_reason": "tool_use",
        "usage": {},
    }


def final_response():
    return {"content": [{"type": "text", "text": "已读取代码。"}], "stop_reason": "end_turn", "usage": {}}


class ReadProvider:
    def __init__(self):
        self.calls = 0

    async def complete(self, system, messages, tools, max_tokens=2048):
        self.calls += 1
        return tool_response() if self.calls == 1 else final_response()


class GatedProvider(ReadProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, system, messages, tools, max_tokens=2048):
        self.entered.set()
        await self.release.wait()
        return await super().complete(system, messages, tools, max_tokens)


class FinishingProvider:
    def __init__(self):
        self.calls = 0
        self.messages = None

    async def complete(self, system, messages, tools, max_tokens=2048):
        self.calls += 1
        self.messages = messages
        return final_response()


@pytest.fixture
def runtime_factory(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")

    def build(provider):
        return AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider=provider)

    return storage, build


async def test_two_runtimes_share_exclusive_run_without_repeating_model_or_tool(runtime_factory, monkeypatch):
    storage, build = runtime_factory
    provider, competing_provider = GatedProvider(), FinishingProvider()
    runtime, competing = build(provider), build(competing_provider)
    run = runtime.create("读取 pricing.py，并报告发现。")
    original_read = ToolRegistry.repo_read
    tool_invocations = []

    def observed_read(self, path):
        tool_invocations.append((self.run_id, path))
        return original_read(self, path)

    monkeypatch.setattr(ToolRegistry, "repo_read", observed_read)
    owner = asyncio.create_task(runtime.execute(run["id"]))
    try:
        await asyncio.wait_for(provider.entered.wait(), timeout=5)
        # 三个公开执行入口都应先拿同一把锁，不能先调用模型或更改审批状态。
        with pytest.raises(RunBusy):
            await competing.execute(run["id"])
        with pytest.raises(RunBusy):
            await competing.resume(run["id"])
        with pytest.raises(RunBusy):
            await competing.approve(run["id"], "not-a-real-proposal", True)
        assert competing_provider.calls == 0
        assert tool_invocations == []
        assert storage.get_run(run["id"])["status"] == "running"
        provider.release.set()
        completed = await asyncio.wait_for(owner, timeout=5)
    finally:
        provider.release.set()
        if not owner.done():
            owner.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(owner, timeout=5)
    assert completed["status"] == "completed"
    assert provider.calls == 2 and competing_provider.calls == 0
    assert tool_invocations == [(run["id"], "pricing.py")]
    assert storage.get_call(run["id"], CALL_ID)["attempts"] == 1
    assert len([event for event in storage.events(run["id"]) if event["kind"] == "model_start"]) == 2
    # 完成后可再次进入恢复入口，已完成任务不会多调用一次模型。
    assert (await competing.resume(run["id"]))["status"] == "completed"
    assert competing_provider.calls == 0


async def test_cancel_drains_tool_thread_before_unlock_and_resume_reuses_persisted_result(
    runtime_factory, monkeypatch
):
    storage, build = runtime_factory
    provider, recovery_provider = ReadProvider(), FinishingProvider()
    runtime, recovery = build(provider), build(recovery_provider)
    run = runtime.create("读取 pricing.py，并报告发现。")
    entered, release = threading.Event(), threading.Event()
    original_read = ToolRegistry.repo_read
    tool_invocations = []

    def blocked_read(self, path):
        tool_invocations.append((self.run_id, path))
        entered.set()
        assert release.wait(timeout=10), "测试必须显式释放工具线程"
        return original_read(self, path)

    monkeypatch.setattr(ToolRegistry, "repo_read", blocked_read)
    owner = asyncio.create_task(runtime.execute(run["id"]))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
        assert storage.get_call(run["id"], CALL_ID)["status"] == "running"
        owner.cancel()
        # cancel 已先入事件循环就绪队列；后排的 callback 作为无 sleep 的调度屏障。
        cancellation_processed = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_processed.set)
        await asyncio.wait_for(cancellation_processed.wait(), timeout=5)
        assert not owner.done(), "工具线程未返回时，取消不能结束持锁的执行协程"
        with pytest.raises(RunBusy):
            await recovery.resume(run["id"])
        assert recovery_provider.calls == 0
        assert tool_invocations == [(run["id"], "pricing.py")]

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=5)
        # CancelledError 返回之前，工具完整结果必须已保存，而协议投影可能尚未保存。
        record = storage.get_call(run["id"], CALL_ID)
        assert record["status"] == "succeeded" and record["attempts"] == 1
        assert record["result_sha256"] is not None
        assert record["result"]["ok"] and "return subtotal" in record["result"]["content"]
        assert storage.get_run(run["id"])["metadata"]["pending_calls"][0]["id"] == CALL_ID

        completed = await asyncio.wait_for(recovery.resume(run["id"]), timeout=5)
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(owner, timeout=5)
    assert completed["status"] == "completed"
    assert tool_invocations == [(run["id"], "pricing.py")]
    assert storage.get_call(run["id"], CALL_ID)["attempts"] == 1
    assert provider.calls == recovery_provider.calls == 1
    results = [
        block
        for message in recovery_provider.messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result" and block["tool_use_id"] == CALL_ID
    ]
    assert len(results) == 1 and not results[0]["is_error"]

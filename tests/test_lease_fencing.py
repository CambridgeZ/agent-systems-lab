"""持久队列、租约续期、超时接管与单调递增世代（epoch fencing）测试。"""

import asyncio
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from agent_lab.api import ROOT, create_app
from agent_lab.lease import LeaseWorker, build_worker_runtime_factory
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import StaleEpochError, Storage
from agent_lab.tools import ToolRegistry


async def test_stale_worker_is_fenced_out_after_lease_expiry_and_takeover(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    original_pricing = (workspace / "pricing.py").read_text(encoding="utf-8")

    runtime_init = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    created = runtime_init.create("修复 pricing.py 折扣 bug")
    run_id = created["id"]
    storage.enqueue_run(run_id, action="execute")

    # 1. Worker A 领取任务，获得 epoch = 1，模拟其卡死直至租约过期（ttl = 0.5s）
    t0 = time.time()
    claim_a = storage.claim_next_run("worker-A", ttl_seconds=0.5, now_ts=t0)
    assert claim_a is not None
    assert claim_a["epoch"] == 1 and claim_a["takeover"] is False

    # 2. 时间推进超过 TTL，Worker B 接管同一任务，获得递增世代 epoch = 2
    claim_b = storage.claim_next_run("worker-B", ttl_seconds=30.0, now_ts=t0 + 1.0)
    assert claim_b is not None
    assert claim_b["run_id"] == run_id
    assert claim_b["epoch"] == 2 and claim_b["takeover"] is True and claim_b["previous_owner"] == "worker-A"

    # Worker B 正常推进任务至 waiting_approval 并释放租约
    runtime_b = AgentRuntime(
        storage,
        workspace,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider="demo",
        expected_epoch=2,
    )
    res_b = await runtime_b._execute_unlocked(run_id)
    assert res_b["status"] == "waiting_approval"
    storage.release_lease(run_id, "worker-B", epoch=2, final_state="waiting_approval")
    proposals_from_b = storage.proposals(run_id)
    assert len(proposals_from_b) == 1

    # 3. 卡死的 Worker A 苏醒，仍携带过期的 epoch = 1：
    #    无论是续租、写状态、登记工具调用、创建提案还是应用文件写入，全部必须触发 StaleEpochError！
    with pytest.raises(StaleEpochError):
        storage.heartbeat_lease(run_id, "worker-A", epoch=1)

    stale_copy = storage.get_run(run_id)
    stale_copy["status"] = "failed"
    with pytest.raises(StaleEpochError):
        storage.save_run(stale_copy, expected_epoch=1)

    with pytest.raises(StaleEpochError):
        storage.schedule_call(run_id, "stale-call", "repo_list", {}, expected_epoch=1)

    stale_registry = ToolRegistry(storage, workspace, None, run_id, expected_epoch=1)
    with pytest.raises(StaleEpochError):
        stale_registry.propose_patch("pricing.py", "subtotal", "0")

    # 即使提案已被人类批准，携带旧 epoch=1 的 Worker A 也绝不能把文件写入磁盘！
    storage.approve_proposal(proposals_from_b[0]["id"], True)
    with pytest.raises(StaleEpochError):
        stale_registry.apply_proposal(proposals_from_b[0]["id"])
    assert (workspace / "pricing.py").read_text(encoding="utf-8") == original_pricing

    # 4. 合法的新世代 Worker C（epoch = 3）消费审批恢复队列并成功落盘完成
    storage.enqueue_run(run_id, action="resume")
    worker_c = LeaseWorker(
        storage=storage,
        runtime_factory=build_worker_runtime_factory(ROOT, storage),
        worker_id="worker-C",
        ttl_seconds=10.0,
    )
    outcome_c = await worker_c.process_one()
    assert outcome_c is not None and outcome_c["ok"] is True
    assert outcome_c["claim"]["epoch"] == 3
    assert outcome_c["run"]["status"] == "completed"
    assert "discount_percent / 100" in (workspace / "pricing.py").read_text(encoding="utf-8")


async def test_live_heartbeat_cancels_preempted_worker_task_cleanly(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")

    entered_model = asyncio.Event()
    allow_exit = asyncio.Event()

    class SlowProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            entered_model.set()
            await allow_exit.wait()
            return {"content": [{"type": "text", "text": "late response"}], "stop_reason": "end_turn", "usage": {}}

    init_rt = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider=SlowProvider())
    run = init_rt.create("慢请求并发接管测试")
    storage.enqueue_run(run["id"], action="execute")

    def slow_factory(run_doc, epoch):
        return AgentRuntime(
            storage,
            workspace,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=SlowProvider(),
            expected_epoch=epoch,
        )

    worker_a = LeaseWorker(
        storage=storage, runtime_factory=slow_factory, worker_id="worker-slow", ttl_seconds=0.3, heartbeat_interval=0.05
    )
    task_a = asyncio.create_task(worker_a.process_one())
    await asyncio.wait_for(entered_model.wait(), timeout=2.0)

    # 模拟网络分区导致租约超时并被外部接管推进 epoch
    future_ts = time.time() + 5.0
    claim_b = storage.claim_next_run("worker-fast", ttl_seconds=15.0, now_ts=future_ts)
    assert claim_b["epoch"] == 2

    # Worker A 的心跳协程应在 50ms 内发现 StaleEpochError 并取消卡住的任务返回 fenced_out
    res_a = await asyncio.wait_for(task_a, timeout=2.0)
    assert res_a["ok"] is False and res_a["code"] == "fenced_out"
    events = storage.events(run["id"])
    assert any(e["kind"] == "worker_fenced" and e["payload"]["stale_epoch"] == 1 for e in events)


async def test_api_queue_dispatch_mode_with_standalone_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LAB_DISPATCH", "queue")
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    state_dir = tmp_path / "queue_state"
    app = create_app(state_dir, provider="demo")
    storage = app.state.storage
    worker = LeaseWorker(
        storage=storage,
        runtime_factory=build_worker_runtime_factory(ROOT, storage),
        worker_id="api-worker-1",
        ttl_seconds=10.0,
    )
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert cfg["dispatch_mode"] == "queue" and cfg["single_process"] is False
        created = client.post("/api/runs", json={"prompt": "修复折扣 bug", "mode": "agent"}).json()
        run_id = created["id"]
        # Worker 尚未运行前，任务保持 queued
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"

        step1 = await worker.process_one()
        assert step1["run"]["status"] == "waiting_approval"
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["lease"]["epoch"] == 1
        pid = detail["proposals"][0]["id"]

        # API 批准提案只入队，不直接在 Web 进程内执行
        approve_resp = client.post(f"/api/runs/{run_id}/proposals/{pid}", json={"approved": True}).json()
        assert approve_resp["queued"] is True

        step2 = await worker.process_one()
        assert step2["run"]["status"] == "completed"
        assert step2["claim"]["epoch"] == 2
        workers_resp = client.get("/api/workers").json()
        assert any(w["worker_id"] == "api-worker-1" for w in workers_resp["workers"])

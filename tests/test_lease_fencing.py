"""持久队列、租约续期、超时接管与单调递增世代（epoch fencing）测试。"""

import asyncio
import json
from pathlib import Path
import shutil
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent_lab.api import ROOT, create_app
from agent_lab.git_repo import create_task_worktree, init_git_repo_from_dir, resolve_commit
from agent_lab.lease import LeaseWorker, build_worker_runtime_factory
from agent_lab.runtime import AgentRuntime
from agent_lab.sandbox import SandboxRunner, discover_test_oracles
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


async def test_external_cancelled_error_requeues_lease_and_propagates(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    entered = asyncio.Event()

    class BlockingProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            entered.set()
            await asyncio.Event().wait()

    init_rt = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    created = init_rt.create("修复 pricing.py 百分比折扣 bug")
    run_id = created["id"]
    storage.enqueue_run(run_id, action="execute")

    worker_a = LeaseWorker(
        storage=storage,
        runtime_factory=lambda run_doc, ep: AgentRuntime(
            storage,
            Path(run_doc["metadata"]["workspace"]),
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=BlockingProvider(),
            expected_epoch=ep,
        ),
        worker_id="worker-shutting-down",
        ttl_seconds=30.0,
    )
    task_a = asyncio.create_task(worker_a.process_one())
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    # 外部关机取消任务（非租约抢占）：应主动释放租约回 queued 并继续向上传播 CancelledError
    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task_a, timeout=2.0)

    lease_after = storage.get_lease(run_id)
    assert lease_after["queue_state"] == "queued"
    assert storage.get_run(run_id)["status"] == "interrupted"
    kinds = [e["kind"] for e in storage.events(run_id)]
    assert "worker_shutdown_requeued" in kinds
    assert "worker_fenced" not in kinds

    # 健康 Worker 无需等待 30s TTL 即可立即领取并继续推进，且不累计超时接管次数
    worker_b = LeaseWorker(
        storage=storage,
        runtime_factory=build_worker_runtime_factory(ROOT, storage),
        worker_id="worker-healthy",
        ttl_seconds=10.0,
    )
    res_b = await worker_b.process_one()
    assert res_b["ok"] is True
    assert res_b["claim"]["takeover"] is False and res_b["claim"]["takeover_count"] == 0
    assert res_b["run"]["status"] == "waiting_approval"


def test_max_takeovers_dead_letters_poison_pill_and_claims_healthy_run(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    rt = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider="demo")

    poison = rt.create("反复导致进程崩溃的毒丸任务")
    healthy = rt.create("排在后面的健康任务")
    storage.enqueue_run(poison["id"], action="execute")

    t0 = time.time()
    c0 = storage.claim_next_run("w-0", ttl_seconds=1.0, now_ts=t0, max_takeovers=2)
    assert c0["run_id"] == poison["id"] and c0["takeover_count"] == 0

    c1 = storage.claim_next_run("w-1", ttl_seconds=1.0, now_ts=t0 + 2.0, max_takeovers=2)
    assert c1["run_id"] == poison["id"] and c1["takeover_count"] == 1

    c2 = storage.claim_next_run("w-2", ttl_seconds=1.0, now_ts=t0 + 4.0, max_takeovers=2)
    assert c2["run_id"] == poison["id"] and c2["takeover_count"] == 2

    time.sleep(0.005)
    storage.enqueue_run(healthy["id"], action="execute")

    # 再次超时已达 max_takeovers=2：自动隔离毒丸至 dead_letter，并直接领取后面的健康任务
    c3 = storage.claim_next_run("w-3", ttl_seconds=10.0, now_ts=t0 + 6.0, max_takeovers=2)
    assert c3 is not None and c3["run_id"] == healthy["id"]
    assert storage.get_lease(poison["id"])["queue_state"] == "dead_letter"
    poison_doc = storage.get_run(poison["id"])
    assert poison_doc["status"] == "failed" and poison_doc["error"] == "DeadLetterExceeded"
    assert any(e["kind"] == "run_dead_lettered" for e in storage.events(poison["id"]))


async def test_transient_operational_error_does_not_kill_heartbeat_loop(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    recovered_hb = asyncio.Event()
    orig_hb = storage.heartbeat_lease
    hb_attempts = 0

    def flaky_heartbeat(run_id, owner_id, epoch, ttl_seconds=15.0, now_ts=None):
        nonlocal hb_attempts
        hb_attempts += 1
        if hb_attempts <= 2:
            raise sqlite3.OperationalError("database is locked")
        res = orig_hb(run_id, owner_id, epoch, ttl_seconds=ttl_seconds, now_ts=now_ts)
        recovered_hb.set()
        return res

    monkeypatch.setattr(storage, "heartbeat_lease", flaky_heartbeat)

    class WaitForHeartbeatRecoveryProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            await asyncio.wait_for(recovered_hb.wait(), timeout=2.0)
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}

    init_rt = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    created = init_rt.create("瞬态锁冲突心跳恢复测试")
    storage.enqueue_run(created["id"], action="execute")

    worker = LeaseWorker(
        storage=storage,
        runtime_factory=lambda run_doc, ep: AgentRuntime(
            storage,
            Path(run_doc["metadata"]["workspace"]),
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=WaitForHeartbeatRecoveryProvider(),
            expected_epoch=ep,
        ),
        worker_id="worker-resilient",
        ttl_seconds=2.0,
        heartbeat_interval=0.03,
    )
    outcome = await worker.process_one()
    assert outcome["ok"] is True and outcome["run"]["status"] == "completed"
    assert hb_attempts >= 3


async def test_zombie_worker_blocked_before_commit_verified_worktree(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin)
    git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")

    init_rt = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider="demo",
        commit_on_pass=True,
        auto_approve_proposals=True,
    )
    created = init_rt.create(
        "僵尸 Worker 提交前拦截测试",
        extra_metadata={"git": git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    run_id = created["id"]
    storage.enqueue_run(run_id, action="execute")

    def preempt_during_sandbox(self, ws, orc, expected_tests, pattern="test*.py"):
        # 容器测试运行期间租约过期，新 Worker B 接管推进 epoch=2；旧 Worker 返回后绝不能生成 Git commit
        storage.claim_next_run("worker-B", ttl_seconds=30.0, now_ts=time.time() + 100.0)
        return {"ok": True, "code": "passed", "tests_executed": True, "output": "OK"}

    monkeypatch.setattr(SandboxRunner, "run_suite", preempt_during_sandbox)

    class PatchThenSandboxProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            if not results:
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "p1",
                            "name": "propose_patch",
                            "input": {
                                "path": "pricing.py",
                                "old_text": "return subtotal * (1 - discount_percent)",
                                "new_text": "return subtotal * (1 - discount_percent / 100)",
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {},
                }
            return {
                "content": [{"type": "tool_use", "id": "s1", "name": "sandbox_test", "input": {}}],
                "stop_reason": "tool_use",
                "usage": {},
            }

    zombie_worker = LeaseWorker(
        storage=storage,
        runtime_factory=lambda run_doc, ep: AgentRuntime(
            storage,
            worktree,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=PatchThenSandboxProvider(),
            expected_epoch=ep,
            commit_on_pass=True,
            auto_approve_proposals=True,
        ),
        worker_id="worker-zombie",
        ttl_seconds=10.0,
        heartbeat_interval=5.0,
    )
    res = await zombie_worker.process_one()
    assert res["ok"] is False and res["code"] == "fenced_out"
    assert resolve_commit(worktree, "HEAD") == base_commit
    assert any(e["kind"] == "worker_fenced" for e in storage.events(run_id))
    assert not any(e["kind"] == "git_committed" for e in storage.events(run_id))


async def test_cross_node_workspace_archive_failover_materializes_and_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LAB_SYNC_WORKSPACE_ARCHIVE", "1")
    node_a_root = tmp_path / "node_a"
    origin_a = node_a_root / "origin"
    ws_a = node_a_root / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin_a)
    git_meta = create_task_worktree(origin_a, ws_a, ref=base_commit)
    storage = Storage(tmp_path / "shared" / "state.db")

    rt_a = AgentRuntime(storage, ws_a, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    created = rt_a.create("修复 pricing.py 百分比折扣 bug", extra_metadata={"git": git_meta})
    run_id = created["id"]
    assert storage.get_workspace_archive(run_id) is not None
    storage.enqueue_run(run_id, action="execute")

    worker_a = LeaseWorker(
        storage=storage,
        runtime_factory=build_worker_runtime_factory(ROOT, storage),
        worker_id="node-a-worker",
        ttl_seconds=10.0,
    )
    step1 = await worker_a.process_one()
    assert step1["ok"] is True and step1["run"]["status"] == "waiting_approval"
    pid = storage.proposals(run_id)[0]["id"]
    storage.approve_and_enqueue(run_id, pid, True)

    # 模拟 Node A 整机磁盘丢失，Node B 从持久化归档恢复工作区并完成补丁落盘与验证
    shutil.rmtree(node_a_root)
    assert not ws_a.exists()

    node_b_ws_root = tmp_path / "node_b_workspaces"
    worker_b = LeaseWorker(
        storage=storage,
        runtime_factory=build_worker_runtime_factory(ROOT, storage),
        worker_id="node-b-worker",
        ttl_seconds=10.0,
        workspaces_dir=node_b_ws_root,
    )
    step2 = await worker_b.process_one()
    assert step2["ok"] is True and step2["run"]["status"] == "completed"
    materialized_ws = Path(step2["run"]["metadata"]["workspace"])
    assert materialized_ws.is_relative_to(node_b_ws_root.resolve())
    assert "discount_percent / 100" in (materialized_ws / "pricing.py").read_text(encoding="utf-8")
    assert any(e["kind"] == "workspace_materialized" for e in storage.events(run_id))
    assert storage.get_workspace_archive(run_id)["epoch"] == step2["claim"]["epoch"]


async def test_inline_bump_blocks_on_claimed_lease_and_fences_expired_worker(tmp_path):
    from agent_lab.execution_lock import RunBusy

    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    inline_rt = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    created = inline_rt.create("修复 pricing.py 百分比折扣 bug")
    run_id = created["id"]
    storage.enqueue_run(run_id, action="execute")

    # 队列 Worker 持有有效租约期间，内联 execute/resume/approve 立即抛出 RunBusy
    claim = storage.claim_next_run("queue-worker", ttl_seconds=30.0)
    assert claim["epoch"] == 1
    with pytest.raises(RunBusy):
        await inline_rt.execute(run_id)
    with pytest.raises(RunBusy):
        await inline_rt.resume(run_id)

    # 模拟租约已过期：内联执行器通过 bump_inline_epoch 推进世代至 epoch=2，旧 Worker 被隔离出局
    with storage._connect() as con:
        con.execute("UPDATE run_leases SET lease_until=? WHERE run_id=?", (time.time() - 5.0, run_id))
    inline_res = await inline_rt.execute(run_id)
    assert inline_res["status"] == "waiting_approval"
    assert storage.get_lease(run_id)["epoch"] == 2
    with pytest.raises(StaleEpochError):
        storage.save_run(inline_res, expected_epoch=1)


async def test_cooperative_cancel_dynamic_skill_kv_emergency_projection_and_fast_fail_workers(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    skills_dir = tmp_path / "skills"
    (skills_dir / "guide").mkdir(parents=True)
    (skills_dir / "guide" / "SKILL.md").write_text(
        "---\nname: guide\ndescription: 动态指南\n---\n# Guide\nMID_RUN_DYNAMIC_SKILL_BODY\n",
        encoding="utf-8",
    )
    storage = Storage(tmp_path / "state.db")

    # 1. 并行 Worker 首异常快速取消慢兄弟任务
    sibling_cancelled = asyncio.Event()

    class FastFailParallelProvider:
        async def complete(self, system, messages, tools, max_tokens=4096):
            if "ROLE:fast_err" in system:
                raise TimeoutError("fast worker failed")
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
            return {"content": [{"type": "text", "text": "slow"}], "stop_reason": "end_turn", "usage": {}}

    parallel_rt = AgentRuntime(
        storage, workspace, ROOT / "data/knowledge", skills_dir, provider=FastFailParallelProvider()
    )
    p_run = parallel_rt.create(
        "检查仓库", mode="parallel", extra_metadata={"worker_roles": ["fast_err", "slow_peer"]}
    )
    p_res = await asyncio.wait_for(parallel_rt.execute(p_run["id"]), timeout=2.0)
    assert p_res["status"] == "failed" and p_res["error"] == "TimeoutError"
    assert sibling_cancelled.is_set()
    assert p_res["metadata"]["error_detail"]["error_type"] == "TimeoutError"

    # 2. 中途 skill_load 保前缀、超大 tool_use.input 紧急投影、thinking + max_tokens 续写与协作取消
    seen_messages: list[list[dict]] = []

    class MultiEdgeProvider:
        cache_usage_additive = True

        def __init__(self):
            self.turn = 0

        async def complete(self, system, messages, tools, max_tokens=4096):
            self.turn += 1
            seen_messages.append(messages)
            if self.turn == 1:
                return {
                    "content": [
                        {"type": "thinking", "thinking": "加载指南并读取超大参数", "signature": "sig1"},
                        {"type": "tool_use", "id": "load_dyn", "name": "skill_load", "input": {"name": "guide"}},
                        {
                            "type": "tool_use",
                            "id": "big_in",
                            "name": "repo_search",
                            "input": {"query": "subtotal " + ("x" * 3200)},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 20, "output_tokens": 10, "cache_creation_input_tokens": 50},
                }
            if self.turn == 2:
                return {
                    "content": [{"type": "text", "text": "半截输出尚未讲完"}],
                    "stop_reason": "max_tokens",
                    "usage": {"input_tokens": 15, "output_tokens": 25, "cache_read_input_tokens": 50},
                }
            storage.request_cancel_run(edge_run["id"], reason="stop_after_continuation")
            return {
                "content": [{"type": "tool_use", "id": "list_after", "name": "repo_list", "input": {}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    edge_rt = AgentRuntime(
        storage,
        workspace,
        ROOT / "data/knowledge",
        skills_dir,
        provider=MultiEdgeProvider(),
        context_chars=14000,
    )
    edge_run = edge_rt.create("验证中途技能加载与溢出降级")
    edge_done = await edge_rt.execute(edge_run["id"])
    assert edge_done["status"] == "cancelled" and edge_done["error"] == "Cancelled"
    # 第 2 轮中动态加载的 SKILL_SNAPSHOT 位于对话后半部尾部区，未插入 messages[1] 破坏首轮前缀
    turn2_msgs = seen_messages[1]
    assert "SKILL_SNAPSHOT" not in str(turn2_msgs[1])
    assert any("MID_RUN_DYNAMIC_SKILL_BODY" in str(m) and "mid_run_dynamic" not in str(m) for m in turn2_msgs[-3:])
    # 第 3 轮收到 max_tokens 续写引导，且非最新轮次的超大 tool_use.input 被投影为 _omitted_large_input
    assert "_omitted_large_input" in json.dumps(seen_messages[2], ensure_ascii=False)
    assert edge_done["input_tokens"] == (20 + 50) + (15 + 50) + 10

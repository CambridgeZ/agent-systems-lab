"""生产级 API 安全边界、多租户 RBAC、队列竞态修复、优雅关机与远程沙箱池端到端测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_lab.api import ROOT, create_app
from agent_lab.cli import _serve_worker_gracefully
from agent_lab.lease import LeaseWorker, build_worker_runtime_factory
from agent_lab.observability import METRICS
from agent_lab.runtime import AgentRuntime
from agent_lab.sandbox import RemoteHTTPSandboxBackend, SandboxRunner
import agent_lab.sandbox as sandbox_mod
from agent_lab.storage import Storage


def test_require_auth_startup_guard_and_custom_allowed_hosts(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_LAB_TOKENS_JSON", raising=False)
    monkeypatch.setenv("AGENT_LAB_REQUIRE_AUTH", "1")
    with pytest.raises(RuntimeError, match="生产模式要求配置 AGENT_LAB_TOKEN 或 AGENT_LAB_TOKENS_JSON"):
        create_app(tmp_path / "unauth_state", provider="demo")

    monkeypatch.setenv("AGENT_LAB_TOKEN", "ingress-admin-token")
    monkeypatch.setenv("AGENT_LAB_ALLOWED_HOSTS", "agent.internal.example.com, localhost")
    app = create_app(tmp_path / "ingress_state", provider="demo")
    with TestClient(app, base_url="http://agent.internal.example.com") as client:
        # /healthz 免鉴权且接受自定义 Ingress Host
        hz = client.get("/healthz", headers={"X-Trace-Id": "custom-trace-001"})
        assert hz.status_code == 200
        assert hz.json() == {"ok": True, "status": "alive"}
        assert hz.headers["x-trace-id"] == "custom-trace-001"

        # 未授权 Host 被 TrustedHostMiddleware 拦截为 400
        bad_host = client.get("/healthz", headers={"Host": "evil.attacker.invalid"})
        assert bad_host.status_code == 400


def test_multitenant_rbac_memory_isolation_and_distinct_approver(tmp_path, monkeypatch):
    tokens = {
        "tok-a-op": {"tenant_id": "tenant-a", "actor_id": "alice", "roles": ["operator"]},
        "tok-a-creator": {"tenant_id": "tenant-a", "actor_id": "bob", "roles": ["operator", "approver"]},
        "tok-a-reviewer": {"tenant_id": "tenant-a", "actor_id": "carol", "roles": ["approver"]},
        "tok-b-admin": {
            "tenant_id": "tenant-b",
            "actor_id": "dave",
            "roles": ["operator", "approver", "admin"],
        },
        "tok-global-sre": {
            "tenant_id": "ops",
            "actor_id": "sre-bot",
            "roles": ["admin"],
            "global": True,
        },
    }
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens))
    monkeypatch.setenv("AGENT_LAB_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENT_LAB_REQUIRE_DISTINCT_APPROVER", "1")

    app = create_app(tmp_path / "mt_state", provider="demo")
    with TestClient(app) as client:
        assert client.get("/api/runs").status_code == 401

        h_bob = {"Authorization": "Bearer tok-a-creator"}
        h_alice = {"Authorization": "Bearer tok-a-op"}
        h_carol = {"Authorization": "Bearer tok-a-reviewer"}
        h_dave = {"Authorization": "Bearer tok-b-admin"}
        h_sre = {"Authorization": "Bearer tok-global-sre"}

        # 1) 跨租户 Memory 严格隔离（杜绝跨租户记忆提示注入）
        assert client.post("/api/memory", headers=h_bob, json={"key": "secret_note", "value": "only-a"}).status_code == 200
        assert client.post("/api/memory", headers=h_dave, json={"key": "secret_note", "value": "only-b"}).status_code == 200
        assert client.get("/api/memory", headers=h_bob).json() == {"secret_note": "only-a"}
        assert client.get("/api/memory", headers=h_dave).json() == {"secret_note": "only-b"}
        # 仅 approver 角色不能写 memory 或创建任务
        assert client.post("/api/memory", headers=h_carol, json={"key": "k", "value": "v"}).status_code == 403
        assert client.post("/api/runs", headers=h_carol, json={"prompt": "fix"}).status_code == 403

        # 2) Tenant A 的 bob 创建任务并等待审批
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        created = client.post(
            "/api/runs",
            headers={**h_bob, "traceparent": traceparent},
            json={"prompt": "修复 pricing.py 折扣 bug", "mode": "agent"},
        )
        assert created.status_code == 202
        assert created.headers["x-trace-id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        run_id = created.json()["id"]

        for _ in range(100):
            detail = client.get(f"/api/runs/{run_id}", headers=h_bob).json()
            if detail["status"] == "waiting_approval":
                break
            time.sleep(0.02)
        assert detail["status"] == "waiting_approval"
        pid = detail["proposals"][0]["id"]

        stored_run = app.state.storage.get_run(run_id)
        assert stored_run["metadata"]["tenant_id"] == "tenant-a"
        assert stored_run["metadata"]["created_by"] == "bob"
        assert stored_run["metadata"]["memory_scope"] == "tenant:tenant-a:user"
        assert stored_run["metadata"]["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")

        # 3) Tenant B（哪怕拥有本租户 admin 角色）无法读取或枚举 Tenant A 的任务
        assert client.get(f"/api/runs/{run_id}", headers=h_dave).status_code == 404
        assert client.get(f"/api/runs/{run_id}/events", headers=h_dave).status_code == 404
        assert client.get("/api/runs", headers=h_dave).json() == []
        # 全局 admin 可跨租户审计
        assert client.get(f"/api/runs/{run_id}", headers=h_sre).status_code == 200
        assert len(client.get("/api/runs?all_tenants=true", headers=h_sre).json()) == 1

        # 4) RBAC 与双人复核：operator 403；创建者 bob 自批 403；独立审批人 carol 202
        assert client.post(f"/api/runs/{run_id}/proposals/{pid}", headers=h_alice, json={"approved": True}).status_code == 403
        self_approve = client.post(f"/api/runs/{run_id}/proposals/{pid}", headers=h_bob, json={"approved": True})
        assert self_approve.status_code == 403

        ok_approve = client.post(f"/api/runs/{run_id}/proposals/{pid}", headers=h_carol, json={"approved": True})
        assert ok_approve.status_code == 202
        for _ in range(100):
            if client.get(f"/api/runs/{run_id}", headers=h_bob).json()["status"] == "completed":
                break
            time.sleep(0.02)
        assert client.get(f"/api/runs/{run_id}", headers=h_bob).json()["status"] == "completed"
        assert app.state.storage.get_proposal(pid)["decided_by"] == "carol"


async def test_queue_atomic_approve_while_claimed_and_resume_409_conflict(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_LAB_TOKENS_JSON", raising=False)
    monkeypatch.setenv("AGENT_LAB_DISPATCH", "queue")
    app = create_app(tmp_path / "queue_race_state", provider="demo")
    storage: Storage = app.state.storage
    factory = build_worker_runtime_factory(ROOT, storage)

    with TestClient(app) as client:
        created = client.post("/api/runs", json={"prompt": "修复 pricing.py 折扣 bug"}).json()
        run_id = created["id"]

        # 模拟 Worker-1 持有活跃租约并刚保存 waiting_approval，尚未执行 release_lease 的时间窗
        claim = storage.claim_next_run("worker-race-1", ttl_seconds=30.0)
        assert claim is not None and claim["epoch"] == 1
        rt_1 = factory(storage.get_run(run_id), claim["epoch"])
        res_1 = await rt_1._execute_unlocked(run_id)
        await rt_1.aclose()
        assert res_1["status"] == "waiting_approval"
        pid = storage.proposals(run_id)[0]["id"]

        # 此时 run_leases 仍为 claimed：调用 /resume 必须返回 HTTP 409 而非 500
        resume_conflict = client.post(f"/api/runs/{run_id}/resume")
        assert resume_conflict.status_code == 409

        # 在 release_lease 前并发到达的 /proposals/{pid} 审批请求必须原子返回 202（不抛 500）
        approve_res = client.post(f"/api/runs/{run_id}/proposals/{pid}", json={"approved": True})
        assert approve_res.status_code == 202
        assert approve_res.json() == {"id": run_id, "decision": True, "queued": True}

        # Worker-1 随后执行 release_lease("waiting_approval") 幂等返回且不覆盖已入队的 queued 状态
        storage.release_lease(run_id, "worker-race-1", epoch=1, final_state="waiting_approval")
        lease_after = storage.get_lease(run_id)
        assert lease_after["queue_state"] == "queued" and lease_after["action"] == "resume"

        # 下一名 Worker-2 领取 epoch=2 并完成任务
        worker_2 = LeaseWorker(storage=storage, runtime_factory=factory, worker_id="worker-race-2")
        done = await worker_2.process_one()
        assert done is not None and done["ok"] is True
        assert done["claim"]["epoch"] == 2
        assert done["run"]["status"] == "completed"


def test_cancel_inline_and_queue_plus_lifespan_shutdown_never_leaves_zombie_running(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_LAB_TOKENS_JSON", raising=False)

    # 1) Queue 模式取消：立即置为 cancelled，Worker 不再消费
    monkeypatch.setenv("AGENT_LAB_DISPATCH", "queue")
    q_app = create_app(tmp_path / "q_cancel_state", provider="demo")
    with TestClient(q_app) as q_client:
        q_run = q_client.post("/api/runs", json={"prompt": "修复折扣 bug"}).json()
        cancelled_q = q_client.post(f"/api/runs/{q_run['id']}/cancel")
        assert cancelled_q.status_code == 200
        assert cancelled_q.json()["status"] == "cancelled"
        assert q_app.state.storage.get_lease(q_run["id"])["queue_state"] == "cancelled"

    # 2) Inline 模式主动取消与 Lifespan 关机中断清理
    monkeypatch.setenv("AGENT_LAB_DISPATCH", "inline")

    class _SlowProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            await asyncio.sleep(30.0)
            return {"content": [{"type": "text", "text": "never"}], "stop_reason": "end_turn", "usage": {}}

    monkeypatch.setattr("agent_lab.runtime.get_provider", lambda _name: _SlowProvider())
    in_app = create_app(tmp_path / "in_cancel_state", provider="demo")
    with TestClient(in_app) as in_client:
        r1 = in_client.post("/api/runs", json={"prompt": "慢任务主动取消"}).json()["id"]
        r2 = in_client.post("/api/runs", json={"prompt": "慢任务随关机中断"}).json()["id"]
        for _ in range(80):
            s1 = in_app.state.storage.get_run(r1)["status"]
            s2 = in_app.state.storage.get_run(r2)["status"]
            if s1 == "running" and s2 == "running":
                break
            time.sleep(0.01)
        assert in_app.state.storage.get_run(r1)["status"] == "running"
        assert in_app.state.storage.get_run(r2)["status"] == "running"

        c_resp = in_client.post(f"/api/runs/{r1}/cancel")
        assert c_resp.status_code == 200
        assert c_resp.json()["status"] == "cancelled"

    # 退出 TestClient 上下文触发 lifespan 关机取消：r2 绝不能残留为 running 僵尸行
    after_r1 = in_app.state.storage.get_run(r1)
    after_r2 = in_app.state.storage.get_run(r2)
    assert after_r1["status"] == "cancelled"
    assert after_r2["status"] == "interrupted"
    assert any(e["kind"] == "interrupted" for e in in_app.state.storage.events(r2))


def test_body_limit_413_queue_depth_429_rate_limit_probes_metrics_kv_and_gc(tmp_path, monkeypatch):
    METRICS.reset()
    tokens = {
        "op-tok": {"tenant_id": "t1", "actor_id": "op1", "roles": ["operator"]},
        "adm-tok": {"tenant_id": "t1", "actor_id": "adm1", "roles": ["operator", "approver", "admin"]},
    }
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens))
    monkeypatch.setenv("AGENT_LAB_DISPATCH", "queue")
    monkeypatch.setenv("AGENT_LAB_MAX_BODY_BYTES", "256")
    monkeypatch.setenv("AGENT_LAB_MAX_QUEUE_DEPTH", "2")
    monkeypatch.setenv("AGENT_LAB_RATE_LIMIT_RPM", "3")

    state_dir = tmp_path / "limits_state"
    app = create_app(state_dir, provider="demo")
    h_op = {"Authorization": "Bearer op-tok"}
    h_adm = {"Authorization": "Bearer adm-tok"}

    with TestClient(app) as client:
        # 1) 请求体超限 -> HTTP 413（带 Content-Length 及流式分块均拦截）
        oversized = client.post("/api/runs", headers=h_op, json={"prompt": "X" * 400})
        assert oversized.status_code == 413
        assert oversized.headers.get("x-trace-id")

        def _chunked_oversized():
            for _ in range(6):
                yield b"A" * 64

        streamed_413 = client.post(
            "/api/runs",
            headers={**h_op, "Content-Type": "application/json"},
            content=_chunked_oversized(),
        )
        assert streamed_413.status_code == 413

        # 2) 队列深度上限 2：前 2 个 202，第 3 个因 queue_depth 返回 429
        r1 = client.post("/api/runs", headers=h_op, json={"prompt": "task 1"})
        r2 = client.post("/api/runs", headers=h_op, json={"prompt": "task 2"})
        assert r1.status_code == 202 and r2.status_code == 202
        q_full = client.post("/api/runs", headers=h_op, json={"prompt": "task 3"})
        assert q_full.status_code == 429

        # 取消 r1/r2 释放队列深度后，第 4 次创建触发租户 RPM=3 滑动窗口限流 -> 429
        assert client.post(f"/api/runs/{r1.json()['id']}/cancel", headers=h_op).status_code == 200
        assert client.post(f"/api/runs/{r2.json()['id']}/cancel", headers=h_op).status_code == 200
        rate_limited = client.post("/api/runs", headers=h_op, json={"prompt": "task 4"})
        assert rate_limited.status_code == 429

        # 3) /readyz 就绪探针（200 与目录只读故障时 503）
        ready_ok = client.get("/readyz")
        assert ready_ok.status_code == 200 and ready_ok.json()["ok"] is True
        ws_dir = state_dir / "workspaces"
        ws_dir.chmod(0o500)
        try:
            ready_fail = client.get("/readyz")
            assert ready_fail.status_code == 503 and ready_fail.json()["ok"] is False
        finally:
            ws_dir.chmod(0o755)

        # 4) /metrics Prometheus 导出鉴权与内容校验 + /api/kv-benchmark?max_chars=3200
        assert client.get("/metrics").status_code == 401
        prom = client.get("/metrics", headers=h_adm)
        assert prom.status_code == 200
        assert "agent_lab_http_requests_total" in prom.text
        assert "agent_lab_http_request_duration_ms_bucket" in prom.text
        snap = client.get("/api/metrics", headers=h_adm).json()
        assert any(c["name"] == "agent_lab_http_requests_total" for c in snap["counters"])

        kv_resp = client.get("/api/kv-benchmark?max_chars=3200", headers=h_op)
        assert kv_resp.status_code == 200
        assert kv_resp.json()["optimized"]["cumulative_hit_rate"] > kv_resp.json()["legacy"]["cumulative_hit_rate"]

        # 校验 app.state 索引缓存与 /repomap LRU 复用
        assert app.state.knowledge is not None and app.state.skills is not None
        r1_id = r1.json()["id"]
        rmap_1 = client.get(f"/api/runs/{r1_id}/repomap", headers=h_op)
        rmap_2 = client.get(f"/api/runs/{r1_id}/repomap?focus=discount", headers=h_op)
        assert rmap_1.status_code == 200 and rmap_2.status_code == 200
        assert len(app.state.repomap_cache) == 1

        # 5) /api/admin/gc：operator 403，admin 200 回收已取消终态工作区并同步清理 repomap 缓存
        assert client.post("/api/admin/gc?retention_seconds=0", headers=h_op).status_code == 403
        gc_resp = client.post("/api/admin/gc?retention_seconds=0", headers=h_adm)
        assert gc_resp.status_code == 200
        assert gc_resp.json()["storage_gc"]["removed_workspaces"] >= 2
        assert len(app.state.repomap_cache) == 0


def test_end_to_end_remote_http_sandbox_backend_via_internal_execute_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LAB_TOKEN", "api-primary-token")
    monkeypatch.setenv("AGENT_LAB_SANDBOX_TOKEN", "sbx-pool-secret-77")

    # 模拟沙箱池节点内部 Docker 执行回执（同时验证本地快照哈希一致性校验通过）
    def _fake_local_docker_run(self, ws_root: Path, oracle_sha256: str) -> dict:
        tmp_snap = Path(__import__("tempfile").mkdtemp(prefix="test-snap-")).resolve()
        try:
            snap_info = self._snapshot(ws_root, tmp_snap / "repo", oracle_sha256)
        finally:
            for p in tmp_snap.rglob("*"):
                with __import__("contextlib").suppress(OSError):
                    p.chmod(0o700)
            shutil.rmtree(tmp_snap, ignore_errors=True)
        return {
            "ok": True,
            "code": "passed",
            "tests_executed": True,
            "output": "Ran 4 tests in 0.01s\nOK",
            "test_summary": {
                "tests_run": 4,
                "expected": 4,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "successful": True,
            },
            **snap_info,
        }

    orig_run = SandboxRunner.run

    def _patched_run(self, workspace: Path, oracle_sha256: str) -> dict:
        if self.remote_backend is None:
            return _fake_local_docker_run(self, workspace, oracle_sha256)
        return orig_run(self, workspace, oracle_sha256)

    monkeypatch.setattr(sandbox_mod.SandboxRunner, "run", _patched_run)

    pool_app = create_app(tmp_path / "sandbox_pool_state", provider="demo")
    ws = tmp_path / "client_ws"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    oracle_sha = hashlib.sha256((ws / "tests/test_pricing.py").read_bytes()).hexdigest()

    with TestClient(pool_app) as pool_client:
        def _asgi_bridge(req: httpx.Request) -> httpx.Response:
            forwarded_headers = {
                k: v for k, v in req.headers.items() if k.lower() not in {"host", "content-length"}
            }
            res = pool_client.request(req.method, req.url.raw_path.decode("ascii"), content=req.content, headers=forwarded_headers)
            return httpx.Response(res.status_code, headers=dict(res.headers), content=res.content)

        mock_transport = httpx.MockTransport(_asgi_bridge)

        # 错误沙箱令牌 -> 401 -> RemoteHTTPSandboxBackend 降级为 sandbox_unavailable
        bad_backend = RemoteHTTPSandboxBackend(
            "http://sandbox-pool.local", api_key="wrong-token", transport=mock_transport
        )
        bad_res = SandboxRunner(remote_backend=bad_backend).run(ws, oracle_sha)
        assert bad_res["ok"] is False and bad_res["code"] == "sandbox_unavailable"
        assert "401" in bad_res["error"]

        # 携带正确 AGENT_LAB_SANDBOX_TOKEN -> 200 passed 端到端通过
        good_backend = RemoteHTTPSandboxBackend(
            "http://sandbox-pool.local", api_key="sbx-pool-secret-77", transport=mock_transport
        )
        good_res = SandboxRunner(remote_backend=good_backend).run(ws, oracle_sha)
        assert good_res["ok"] is True
        assert good_res["code"] == "passed"
        assert good_res["tests_executed"] is True
        assert good_res["isolation"] == "remote_http_sandbox"


async def test_cli_cancel_gc_serve_guard_and_worker_sigterm_requeue(tmp_path):
    cli_state = tmp_path / "cli_ha_state"
    storage = Storage(cli_state / "state.db")
    ws = cli_state / "workspaces" / "w1"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    rt = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    created = rt.create("测试 CLI 取消与 GC")
    run_id = created["id"]
    storage.enqueue_run(run_id, action="execute")

    # 1) CLI serve 非回环地址未配置 Token 时拒绝启动
    clean_env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k not in {"AGENT_LAB_TOKEN", "AGENT_LAB_TOKENS_JSON", "AGENT_LAB_ALLOW_INSECURE_LOCAL"}
    }
    serve_fail = subprocess.run(
        [sys.executable, "-m", "agent_lab.cli", "--state", str(cli_state), "serve", "--host", "0.0.0.0"],
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert serve_fail.returncode != 0

    # 2) CLI cancel 与 gc 子命令
    cancel_proc = subprocess.run(
        [sys.executable, "-m", "agent_lab.cli", "--state", str(cli_state), "cancel", run_id],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(cancel_proc.stdout)["status"] == "cancelled"

    gc_proc = subprocess.run(
        [sys.executable, "-m", "agent_lab.cli", "--state", str(cli_state), "gc", "--retention-seconds", "0"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(gc_proc.stdout)["storage_gc"]["removed_workspaces"] >= 1

    # 3) Worker SIGTERM 信号优雅退出并释放正在执行的租约回 queued
    ws2 = cli_state / "workspaces" / "w2"
    shutil.copytree(ROOT / "data/sample_repo", ws2)
    entered = asyncio.Event()

    class _BlockingProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            entered.set()
            await asyncio.Event().wait()

    run2 = AgentRuntime(storage, ws2, ROOT / "data/knowledge", ROOT / "skills", provider="demo").create("SIGTERM 测试")
    storage.enqueue_run(run2["id"], action="execute")

    daemon = LeaseWorker(
        storage=storage,
        runtime_factory=lambda doc, ep: AgentRuntime(
            storage,
            Path(doc["metadata"]["workspace"]),
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=_BlockingProvider(),
            expected_epoch=ep,
        ),
        worker_id="sigterm-worker",
        ttl_seconds=30.0,
    )
    worker_task = asyncio.create_task(_serve_worker_gracefully(daemon, poll_interval=0.1))
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    __import__("os").kill(__import__("os").getpid(), signal.SIGTERM)
    await asyncio.wait_for(worker_task, timeout=2.0)

    assert storage.get_lease(run2["id"])["queue_state"] == "queued"
    assert storage.get_run(run2["id"])["status"] == "interrupted"
    assert any(w["worker_id"] == "sigterm-worker" and w["status"] == "stopped" for w in storage.list_workers())


def test_failed_run_exposes_sanitized_error_detail_on_list_and_detail_without_leaking_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_LAB_TOKENS_JSON", raising=False)
    monkeypatch.setenv("AGENT_LAB_DISPATCH", "inline")

    class _SecretLeakingTimeoutProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            raise TimeoutError("Bearer sk-secret-provider-999 postgres://u:topsecret@db/app")

    monkeypatch.setattr("agent_lab.runtime.get_provider", lambda _name: _SecretLeakingTimeoutProvider())
    app = create_app(tmp_path / "err_detail_state", provider="demo")

    with TestClient(app) as client:
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        created = client.post(
            "/api/runs",
            headers={"traceparent": traceparent},
            json={"prompt": "触发超时异常并校验脱敏 error_detail"},
        )
        assert created.status_code == 202
        run_id = created.json()["id"]

        for _ in range(100):
            detail_resp = client.get(f"/api/runs/{run_id}")
            if detail_resp.json()["status"] == "failed":
                break
            time.sleep(0.01)

        detail_body = client.get(f"/api/runs/{run_id}").json()
        list_body = client.get("/api/runs").json()
        events_body = client.get(f"/api/runs/{run_id}/events").json()

        expected_detail = {
            "error_type": "TimeoutError",
            "error_code": "timeouterror",
            "retryable": True,
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        }
        assert detail_body["status"] == "failed"
        assert detail_body["error"] == "TimeoutError"
        assert detail_body["error_detail"] == expected_detail
        listed = next(item for item in list_body if item["id"] == run_id)
        assert listed["error"] == "TimeoutError"
        assert listed["error_detail"] == expected_detail

        serialized = json.dumps([detail_body, list_body, events_body], ensure_ascii=False)
        assert "sk-secret-provider-999" not in serialized
        assert "topsecret" not in serialized

        # 覆盖 dispatch() 外层兜底异常分支：同样写入结构化脱敏 error_detail 与 failure 事件
        class _BoundaryCrashError(RuntimeError):
            code = "boundary_crash"

        async def _boom_resume(self, _rid: str):
            raise _BoundaryCrashError("api_key=DISPATCH_RAW_SECRET_888")

        monkeypatch.setattr(AgentRuntime, "resume", _boom_resume)
        resumed = client.post(f"/api/runs/{run_id}/resume", headers={"X-Trace-Id": "resume-trace-77"})
        assert resumed.status_code == 202

        for _ in range(100):
            after_detail = client.get(f"/api/runs/{run_id}").json()
            if (after_detail.get("error_detail") or {}).get("error_code") == "boundary_crash":
                break
            time.sleep(0.01)

        expected_boundary = {
            "error_type": "_BoundaryCrashError",
            "error_code": "boundary_crash",
            "retryable": False,
            "trace_id": "resume-trace-77",
        }
        assert after_detail["error"] == "_BoundaryCrashError"
        assert after_detail["error_detail"] == expected_boundary
        assert client.get("/api/runs").json()[0]["error_detail"] == expected_boundary
        after_events = client.get(f"/api/runs/{run_id}/events").json()
        assert after_events[-1]["kind"] == "failure"
        assert after_events[-1]["payload"] == expected_boundary
        assert "DISPATCH_RAW_SECRET_888" not in json.dumps(
            [after_detail, after_events], ensure_ascii=False
        )

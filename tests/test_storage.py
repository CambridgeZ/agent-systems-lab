"""检查状态跨进程连接持久化、审批不可翻转、审计游标、记忆隔离、乐观并发、死信隔离与可观测性。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

import pytest

from agent_lab.execution_lock import exclusive_run
from agent_lab.git_repo import create_task_worktree, init_git_repo_from_dir
from agent_lab.observability import (
    METRICS,
    MetricsRegistry,
    bind_trace_context,
    format_traceparent,
    get_trace_id,
    log_structured,
    sanitize_error_detail,
)
from agent_lab.storage import (
    PostgresBackendConfig,
    StaleEpochError,
    Storage,
    StorageConfigError,
)


@pytest.fixture
def store(tmp_path):
    return Storage(tmp_path / "state.sqlite")


def test_run_survives_reopen_with_extra_checkpoint_fields(tmp_path):
    file = tmp_path / "state.sqlite"
    first = Storage(file)
    run = first.create_run("修复折扣", mode="parallel", provider="anthropic")
    run.update(
        status="waiting_approval",
        messages=[{"role": "user", "content": "修复"}],
        pending_tool_ids=["t1"],
        steps=3,
    )
    first.save_run(run)
    loaded = Storage(file).get_run(run["id"])
    assert loaded["messages"] == run["messages"]
    assert loaded["pending_tool_ids"] == ["t1"]
    assert loaded["steps"] == 3
    assert loaded["status"] == "waiting_approval"


def test_memory_scope_and_correction(store):
    store.remember("workspace-a", "preference", {"language": "中文"})
    store.remember("workspace-b", "preference", {"language": "English"})
    store.remember("workspace-a", "preference", {"language": "双语"})
    assert store.memories("workspace-a") == {"preference": {"language": "双语"}}
    assert store.memories("workspace-b")["preference"]["language"] == "English"
    assert store.memories("unknown") == {}
    with pytest.raises(ValueError):
        store.remember("a", "x", "x" * 17000)


def test_events_support_incremental_cursor_and_isolate_runs(store):
    a = store.create_run("a")
    b = store.create_run("b")
    one = store.add_event(a["id"], "step", {"n": 1})
    store.add_event(b["id"], "step", {"n": 9})
    two = store.add_event(a["id"], "step", {"n": 2})
    assert [row["id"] for row in store.events(a["id"])] == [one["id"], two["id"]]
    assert store.events(a["id"], after=one["id"]) == [two]
    with pytest.raises(sqlite3.IntegrityError):
        store.add_event("nonexistent", "step", {})


def test_approval_is_immutable_and_idempotent(store):
    run = store.create_run("a")
    proposal = store.create_proposal(run["id"], "pricing.py", "a", "b")
    with pytest.raises(ValueError, match="approval_required"):
        store.mark_proposal_applied(proposal["id"])
    assert store.approve_proposal(proposal["id"], True)["status"] == "approved"
    assert store.approve_proposal(proposal["id"], True)["status"] == "approved"
    with pytest.raises(ValueError, match="already_decided"):
        store.approve_proposal(proposal["id"], False)
    assert store.mark_proposal_applied(proposal["id"])["applied"] is True
    assert store.proposals(run["id"])[0]["new_content"] == "b"
    assert store.get_proposal("missing") is None


def test_rejected_proposal_requires_new_proposal(store):
    run = store.create_run("a")
    proposal = store.create_proposal(run["id"], "pricing.py", "a", "b")
    store.approve_proposal(proposal["id"], False)
    with pytest.raises(ValueError):
        store.approve_proposal(proposal["id"], True)
    with pytest.raises(ValueError):
        store.approve_proposal(proposal["id"], 1)


def test_missing_run_and_memory_database_rejected(store):
    assert store.get_run("missing") is None
    with pytest.raises(KeyError):
        store.save_run({"id": "missing"})
    with pytest.raises(ValueError):
        Storage(":memory:")


def test_pragma_failure_closes_connection_without_handle_leak(tmp_path, monkeypatch):
    store = Storage(tmp_path / "pragma.sqlite", synchronous="FULL")
    assert store.synchronous == "FULL"
    with pytest.raises(ValueError, match="sqlite_synchronous_invalid"):
        Storage(tmp_path / "bad_sync.sqlite", synchronous="INVALID")

    closed_flags: list[bool] = []
    real_connect = sqlite3.connect

    class FailingPragmaConn:
        def __init__(self, raw):
            self._raw = raw
            self.row_factory = None

        def execute(self, sql, *args):
            if "PRAGMA synchronous" in sql:
                raise sqlite3.OperationalError("database is locked during pragma")
            return self._raw.execute(sql, *args)

        def close(self):
            closed_flags.append(True)
            self._raw.close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: FailingPragmaConn(real_connect(*a, **k)))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with store._connect():
            pass
    assert closed_flags == [True]


def test_init_retries_busy_and_protocol_errors(tmp_path, monkeypatch):
    db_file = tmp_path / "retry_boot.sqlite"
    attempts = {"count": 0}
    orig_init = Storage._initialize_schema

    def flaky_init(self):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is busy")
        if attempts["count"] == 2:
            raise sqlite3.OperationalError("locking protocol")
        return orig_init(self)

    monkeypatch.setattr(Storage, "_initialize_schema", flaky_init)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    booted = Storage(db_file)
    assert attempts["count"] == 3
    assert booted.create_run("ok")["status"] == "queued"


async def test_dsn_factory_postgres_config_and_run_async(tmp_path, monkeypatch):
    dsn_file = tmp_path / "from_dsn.sqlite"
    store = Storage.from_dsn(f"sqlite:///{dsn_file}")
    created = await store.run_async(store.create_run, "async prompt", tenant_id="team-a")
    fetched = await store.run_async("get_run", created["id"])
    assert fetched["id"] == created["id"]

    with pytest.raises(ValueError):
        Storage.from_dsn("sqlite:///:memory:")
    with pytest.raises(StorageConfigError, match="unsupported_storage_dsn_scheme"):
        Storage.from_dsn("mysql://localhost/db")

    pg_cfg = PostgresBackendConfig(dsn="postgresql://user:pass@localhost:5432/agent_lab")
    assert pg_cfg.translate_sql("BEGIN IMMEDIATE") == "BEGIN"
    assert pg_cfg.translate_sql("SELECT * FROM runs WHERE id=? AND tenant_id='a?b'") == (
        "SELECT * FROM runs WHERE id=%s AND tenant_id='a?b'"
    )
    assert "FOR UPDATE SKIP LOCKED" in pg_cfg.build_claim_sql(specific_run=False)

    import importlib

    real_import = importlib.import_module

    def missing_psycopg(name, *args, **kwargs):
        if name.startswith("psycopg"):
            raise ImportError("No module named 'psycopg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", missing_psycopg)
    with pytest.raises(StorageConfigError, match="postgresql_driver_missing"):
        Storage.from_dsn("postgresql://localhost:5432/agent_lab")


def test_legacy_run_leases_check_constraint_migrates_cleanly(tmp_path):
    db_file = tmp_path / "legacy_leases.sqlite"
    with sqlite3.connect(db_file) as con:
        con.executescript(
            """
            CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE run_leases (
                run_id TEXT PRIMARY KEY REFERENCES runs(id),
                queue_state TEXT NOT NULL CHECK(queue_state IN (
                    'idle', 'queued', 'claimed', 'waiting_approval', 'completed', 'failed', 'budget_exceeded'
                )),
                action TEXT NOT NULL DEFAULT 'execute',
                owner_id TEXT,
                epoch INTEGER NOT NULL DEFAULT 3,
                lease_until REAL,
                heartbeat_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO runs VALUES ('r1', '2026-01-01', '{"id":"r1","status":"queued"}');
            INSERT INTO run_leases VALUES ('r1', 'queued', 'execute', NULL, 3, NULL, NULL, '2026-01-01');
            """
        )
    migrated = Storage(db_file)
    lease = migrated.get_lease("r1")
    assert lease["epoch"] == 3 and lease["takeover_count"] == 0
    cancelled = migrated.request_cancel_run("r1", actor_id="ops")
    assert cancelled["status"] == "cancelled"
    assert migrated.get_lease("r1")["queue_state"] == "cancelled"


def test_lightweight_list_runs_and_optimistic_state_version_cas(store):
    r_alpha = store.create_run("alpha task", tenant_id="alpha")
    r_beta = store.create_run("beta task", tenant_id="beta")
    assert r_alpha["state_version"] == 1

    r_alpha["messages"] = [{"role": "user", "content": "huge" * 2000}]
    r_alpha["metadata"] = {
        "template": "multi_file",
        "verification": {"status": "passed"},
        "tenant_id": "alpha",
        "actor_id": "u-1",
        "kv_cache": {"hits": 4, "known_blocks": ["block-hash"] * 500},
    }
    store.save_run(r_alpha, expected_version=1)
    assert r_alpha["state_version"] == 2

    with pytest.raises(StaleEpochError, match="state_version_conflict"):
        store.save_run(r_alpha, expected_version=1)
    assert store.get_run(r_alpha["id"])["state_version"] == 2

    alpha_list = store.list_runs(tenant_id="alpha")
    assert [item["id"] for item in alpha_list] == [r_alpha["id"]]
    summary = alpha_list[0]
    assert "messages" not in summary
    assert "known_blocks" not in summary["metadata"]["kv_cache"]
    assert summary["metadata"]["template"] == "multi_file"
    assert summary["metadata"]["verification"] == {"status": "passed"}
    assert [item["id"] for item in store.list_runs(tenant_id="beta")] == [r_beta["id"]]


def test_unified_inline_and_worker_mutual_fencing(store):
    run = store.create_run("fencing test")
    rid = run["id"]
    store.enqueue_run(rid, action="execute")

    t0 = time.time()
    claim = store.claim_next_run("worker-1", ttl_seconds=30.0, now_ts=t0)
    assert claim["epoch"] == 1

    # Worker 持有有效租约期间，内联路径（expected_epoch=None）写状态、校验世代与 bump_inline_epoch 均被拦截
    inline_copy = store.get_run(rid)
    inline_copy["answer"] = "inline hijack"
    with pytest.raises(StaleEpochError, match="fencing_inline_blocked_by_claimed_lease"):
        store.save_run(inline_copy, expected_epoch=None)
    with pytest.raises(StaleEpochError, match="fencing_inline_blocked_by_claimed_lease"):
        store.verify_epoch(rid, None)
    with pytest.raises(StaleEpochError, match="run_currently_claimed"):
        store.bump_inline_epoch(rid, now_ts=t0 + 1.0)

    # 租约过期后，内联执行器调用 bump_inline_epoch 推进世代至 2（保持 idle），旧 Worker 立即被隔离
    bumped = store.bump_inline_epoch(rid, now_ts=t0 + 60.0)
    assert bumped == 2
    lease = store.get_lease(rid)
    assert lease["queue_state"] == "idle" and lease["owner_id"] is None and lease["epoch"] == 2

    with pytest.raises(StaleEpochError):
        store.save_run(inline_copy, expected_epoch=1)
    with pytest.raises(StaleEpochError):
        store.heartbeat_lease(rid, "worker-1", epoch=1)
    store.save_run(inline_copy, expected_epoch=None)
    assert store.get_run(rid)["answer"] == "inline hijack"


def test_max_takeovers_isolates_dead_letter_poison_pill_and_claims_next(store):
    poison = store.create_run("poison pill")
    healthy = store.create_run("healthy task")
    store.enqueue_run(poison["id"], action="execute")

    t0 = 1_700_000_000.0
    first = store.claim_next_run("w-1", ttl_seconds=1.0, now_ts=t0, max_takeovers=2)
    assert first["run_id"] == poison["id"] and first["takeover"] is False and first["takeover_count"] == 0

    take1 = store.claim_next_run("w-2", ttl_seconds=1.0, now_ts=t0 + 2.0, max_takeovers=2)
    assert take1["run_id"] == poison["id"] and take1["takeover"] is True and take1["takeover_count"] == 1

    take2 = store.claim_next_run("w-3", ttl_seconds=1.0, now_ts=t0 + 4.0, max_takeovers=2)
    assert take2["run_id"] == poison["id"] and take2["takeover"] is True and take2["takeover_count"] == 2

    time.sleep(0.005)
    store.enqueue_run(healthy["id"], action="execute")

    # 第 3 次超时接管达到 max_takeovers=2：排在队首的 poison 自动打入 dead_letter，同次调用直接跳过并领取 healthy
    next_claim = store.claim_next_run("w-4", ttl_seconds=10.0, now_ts=t0 + 6.0, max_takeovers=2)
    assert next_claim is not None and next_claim["run_id"] == healthy["id"]

    poison_lease = store.get_lease(poison["id"])
    assert poison_lease["queue_state"] == "dead_letter"
    assert poison_lease["owner_id"] is None and poison_lease["lease_until"] is None
    poison_doc = store.get_run(poison["id"])
    assert poison_doc["status"] == "failed" and poison_doc["error"] == "DeadLetterExceeded"
    poison_summary = next(r for r in store.list_runs() if r["id"] == poison["id"])
    assert poison_summary["error"] == "DeadLetterExceeded"
    events = store.events(poison["id"])
    assert any(e["kind"] == "run_dead_lettered" and e["payload"]["takeover_count"] == 2 for e in events)


def test_atomic_approve_and_enqueue_races_safely_before_release_lease(store):
    run = store.create_run("approval race")
    rid = run["id"]
    other_run = store.create_run("other")
    other_prop = store.create_proposal(other_run["id"], "x.py", "1", "2")

    store.enqueue_run(rid, action="execute")
    claim = store.claim_next_run("worker-a", ttl_seconds=30.0)
    epoch = claim["epoch"]
    prop = store.create_proposal(rid, "pricing.py", "old", "new", expected_epoch=epoch)

    with pytest.raises(KeyError, match="proposal_not_found"):
        store.approve_and_enqueue(rid, other_prop["id"], True, actor_id="reviewer-1")

    # Worker 尚未调用 release_lease(..., 'waiting_approval')，此时 queue_state 仍为 'claimed'
    res = store.approve_and_enqueue(rid, prop["id"], True, actor_id="reviewer-1")
    assert res["status"] == "approved" and res["queued"] is True
    assert res["proposal"]["decided_by"] == "reviewer-1"
    assert res["lease"]["queue_state"] == "queued" and res["lease"]["action"] == "resume"

    # 幂等重入同方向审批允许，反方向翻转被拒绝
    assert store.approve_and_enqueue(rid, prop["id"], True, actor_id="reviewer-1")["status"] == "approved"
    with pytest.raises(ValueError, match="approval_already_decided"):
        store.approve_and_enqueue(rid, prop["id"], False, actor_id="reviewer-2")

    # Worker 随后退出 waiting_approval 调用 release_lease：不抛 StaleEpochError，也不把 queued 覆盖回 waiting_approval
    store.release_lease(rid, "worker-a", epoch=epoch, final_state="waiting_approval")
    assert store.get_lease(rid)["queue_state"] == "queued"

    # 但真正过期的旧世代 epoch - 1 依然严格抛 StaleEpochError
    with pytest.raises(StaleEpochError):
        store.release_lease(rid, "worker-a", epoch=epoch - 1, final_state="waiting_approval")

    # 新 Worker 接管（epoch + 1）后，旧世代 epoch 再调用 release_lease 也必须抛 StaleEpochError
    claim_next = store.claim_next_run("worker-b", ttl_seconds=30.0)
    assert claim_next["epoch"] == epoch + 1
    with pytest.raises(StaleEpochError):
        store.release_lease(rid, "worker-a", epoch=epoch, final_state="waiting_approval")


def test_workspace_archive_round_trip_and_monotonic_epoch_guard(store):
    run = store.create_run("archive task")
    rid = run["id"]
    assert store.get_workspace_archive(rid) is None

    saved = store.save_workspace_archive(rid, epoch=2, archive_bytes=b"\x1f\x8b-tar-v2", git_head="a" * 40)
    assert saved["size_bytes"] == len(b"\x1f\x8b-tar-v2")
    loaded = store.get_workspace_archive(rid)
    assert loaded is not None
    assert loaded["epoch"] == 2
    assert loaded["git_head"] == "a" * 40
    assert loaded["archive"] == b"\x1f\x8b-tar-v2"

    with pytest.raises(StaleEpochError, match="stale_archive_epoch"):
        store.save_workspace_archive(rid, epoch=1, archive_bytes=b"stale")

    store.save_workspace_archive(rid, epoch=3, archive_bytes=b"newer", git_head="b" * 40)
    assert store.get_workspace_archive(rid)["archive"] == b"newer"


def test_request_cancel_run_and_gc_expired_resources(tmp_path):
    store = Storage(tmp_path / "gc.sqlite")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    origins = tmp_path / "git_origins"
    origins.mkdir()

    src_template = tmp_path / "src_tpl"
    src_template.mkdir()
    (src_template / "app.py").write_text("x = 1\n", encoding="utf-8")
    origin_repo = origins / "tpl_origin"
    init_git_repo_from_dir(src_template, origin_repo)

    # 1. 排队未领取任务：立即取消为 cancelled
    queued_run = store.create_run("cancel immediately")
    store.enqueue_run(queued_run["id"])
    cancelled = store.request_cancel_run(queued_run["id"], actor_id="alice", reason="no_longer_needed")
    assert cancelled["status"] == "cancelled"
    assert store.is_cancel_requested(queued_run["id"]) is True
    assert store.get_lease(queued_run["id"])["queue_state"] == "cancelled"
    assert any(e["kind"] == "cancelled" for e in store.events(queued_run["id"]))

    # 2. 正在被 Worker 持有租约的任务：打标协作退出，不强行改写状态为 cancelled
    claimed_run = store.create_run("cancel cooperative")
    claimed_ws = workspaces / claimed_run["id"]
    create_task_worktree(origin_repo, claimed_ws)
    claimed_run["status"] = "running"
    claimed_run["metadata"]["workspace"] = str(claimed_ws)
    store.save_run(claimed_run)
    store.enqueue_run(claimed_run["id"])
    store.claim_next_run("worker-live", ttl_seconds=60.0)
    coop = store.request_cancel_run(claimed_run["id"], actor_id="bob", reason="stop_soon")
    assert coop["status"] == "running"
    assert store.is_cancel_requested(claimed_run["id"]) is True
    assert any(e["kind"] == "cancel_requested" for e in store.events(claimed_run["id"]))

    # 3. 已完成且超过保留期的旧任务：工作区 worktree、归档与过期锁被 GC 清理，活跃任务不受影响
    done_run = store.create_run("old completed")
    done_ws = workspaces / done_run["id"]
    create_task_worktree(origin_repo, done_ws)
    done_run["status"] = "completed"
    done_run["metadata"]["workspace"] = str(done_ws)
    store.save_run(done_run)
    store.save_workspace_archive(done_run["id"], epoch=1, archive_bytes=b"old-archive")
    with exclusive_run(store.path, done_run["id"]):
        pass

    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    with store._connect() as con:
        con.execute(
            "UPDATE runs SET updated_at_col=?, created_at=? WHERE id=?",
            (old_iso, old_iso, done_run["id"]),
        )
        con.execute(
            "UPDATE workspace_archives SET updated_at=? WHERE run_id=?",
            (old_iso, done_run["id"]),
        )

    gc_stats = store.gc_expired_resources(
        workspaces_dir=workspaces,
        git_origins_dir=origins,
        retention_seconds=0.0,
    )
    assert gc_stats["removed_workspaces"] >= 1
    assert gc_stats["removed_archives"] == 1
    assert gc_stats["removed_locks"] >= 1
    assert gc_stats["pruned_origins"] == 1
    assert not done_ws.exists()
    assert store.get_workspace_archive(done_run["id"]) is None
    assert claimed_ws.exists()


def test_observability_trace_context_secret_redaction_and_prometheus_metrics(caplog):
    w3c = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    outer_tid = get_trace_id()
    assert len(outer_tid) == 32

    with bind_trace_context(w3c) as bound_tid:
        assert bound_tid == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert get_trace_id() == bound_tid
        tp = format_traceparent(bound_tid)
        assert tp.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-") and tp.endswith("-01")

        with caplog.at_level("INFO", logger="agent_lab"):
            rec = log_structured(
                "info",
                "provider_call",
                api_key="sk-secret-123",
                bearer_token="tok-xyz",
                nested={"client_secret": "s1", "password": "p1", "safe": "visible"},
                dsn="postgresql://admin:supersecret@db.internal:5432/prod",
                header_dump="Authorization: Bearer sk-live-999",
            )
        assert rec["trace_id"] == bound_tid
        assert rec["api_key"] == "[REDACTED]"
        assert rec["bearer_token"] == "[REDACTED]"
        assert rec["nested"] == {"client_secret": "[REDACTED]", "password": "[REDACTED]", "safe": "visible"}
        assert "supersecret" not in json.dumps(rec)
        assert "sk-live-999" not in json.dumps(rec)
        assert "sk-secret-123" not in caplog.text

        class SdkAuthError(RuntimeError):
            code = "rate_limit"
            retryable = True

        err_detail = sanitize_error_detail(SdkAuthError("raw message leaking sk-ant-secret-key"))
        assert err_detail == {
            "error_type": "SdkAuthError",
            "error_code": "rate_limit",
            "retryable": True,
            "trace_id": bound_tid,
        }
        assert "sk-ant-secret-key" not in json.dumps(err_detail)
        assert sanitize_error_detail(TimeoutError("upstream timed out"))["retryable"] is True

    assert get_trace_id() == outer_tid

    reg = MetricsRegistry()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: reg.inc("agent_lab_requests_total", {"route": "/runs"}), range(20)))
    reg.set_gauge("agent_lab_active_workers", 3, {"pool": "default"})
    reg.observe_ms("agent_lab_step_duration_ms", 12.5, {"provider": "demo"})
    reg.observe_ms("agent_lab_step_duration_ms", 240.0, {"provider": "demo"})

    snap = reg.snapshot()
    assert snap["counters"][0]["value"] == 20.0
    assert snap["gauges"][0]["value"] == 3.0
    assert snap["histograms"][0]["count"] == 2

    prom = reg.render_prometheus()
    assert "# TYPE agent_lab_requests_total counter" in prom
    assert 'agent_lab_requests_total{route="/runs"} 20' in prom
    assert "# TYPE agent_lab_active_workers gauge" in prom
    assert 'agent_lab_active_workers{pool="default"} 3' in prom
    assert "# TYPE agent_lab_step_duration_ms histogram" in prom
    assert 'agent_lab_step_duration_ms_bucket{provider="demo",le="25"} 1' in prom
    assert 'agent_lab_step_duration_ms_bucket{provider="demo",le="+Inf"} 2' in prom
    assert 'agent_lab_step_duration_ms_count{provider="demo"} 2' in prom

    METRICS.inc("agent_lab_global_smoke_total")
    assert "agent_lab_global_smoke_total" in METRICS.render_prometheus()

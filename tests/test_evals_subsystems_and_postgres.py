"""离线评测套件、Model Evals / SWE-bench、Storage 迁移与 SQL 翻译、可观测性追踪及条件化 PostgreSQL 集成测试。"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

import httpx
import pytest

from agent_lab.evals import (
    SUBSYSTEM_EVAL_SPECS,
    run_all_eval_suites,
    run_subsystem_evals,
)
from agent_lab.lease import WorkerMetricsServer
from agent_lab.model_evals import (
    LLMJudge,
    build_swebench_task_prompt,
    estimate_pass_at_k,
    grade_swebench_patch,
    grade_with_llm_judge,
    load_swebench_instances,
)
from agent_lab.observability import (
    METRICS,
    MetricsRegistry,
    bind_trace_context,
    current_parent_span_id,
    current_span_id,
    current_trace_id,
    format_traceparent,
    start_span,
)
from agent_lab.providers import DemoProvider
from agent_lab.storage import Storage, _translate_qmark_to_pyformat

# ---------------------------------------------------------------------------
# 1. evals.py：run_subsystem_evals (7/7) 与 run_all_eval_suites(suite="all") (17/17)
# ---------------------------------------------------------------------------


async def test_evals_subsystem_suite_passes_7_of_7(tmp_path: Path):
    report_file = tmp_path / "subsystems.json"
    report = await run_subsystem_evals(report_path=report_file)

    assert report["total"] == 7
    assert report["passed"] == 7, json.dumps(report["results"], ensure_ascii=False)
    assert report["failed"] == 0
    assert report["summary"]["pass_rate"] == pytest.approx(1.0)
    assert report["environment"]["network_required"] is False

    expected_ids = {name for name, _ in SUBSYSTEM_EVAL_SPECS}
    actual_ids = {item["subsystem"] for item in report["results"]}
    assert actual_ids == expected_ids
    assert all(item["passed"] is True and item["checks"] for item in report["results"])

    disk_doc = json.loads(report_file.read_text(encoding="utf-8"))
    assert disk_doc["summary"] == report["summary"]


async def test_evals_all_suites_pass_17_of_17(tmp_path: Path):
    report_file = tmp_path / "all_evals.json"
    combined = await run_all_eval_suites(suite="all", report_path=report_file)

    assert combined["suite"] == "all"
    assert combined["total"] == 17
    assert combined["passed"] == 17, json.dumps(combined["summary"], ensure_ascii=False)
    assert combined["failed"] == 0
    assert combined["summary"]["core_total"] == 10
    assert combined["summary"]["core_passed"] == 10
    assert combined["summary"]["subsystems_total"] == 7
    assert combined["summary"]["subsystems_passed"] == 7
    assert json.loads(report_file.read_text(encoding="utf-8"))["summary"] == combined["summary"]

    with pytest.raises(ValueError):
        await run_all_eval_suites(suite="nonexistent_suite")


# ---------------------------------------------------------------------------
# 2. model_evals.py：estimate_pass_at_k、grade_with_llm_judge、SWE-bench 全流程
# ---------------------------------------------------------------------------


def test_model_evals_estimate_pass_at_k_edge_cases():
    # c = 0
    assert estimate_pass_at_k(5, 0, 1) == 0.0
    assert estimate_pass_at_k(5, 0, 5) == 0.0
    # c = n
    assert estimate_pass_at_k(5, 5, 1) == 1.0
    assert estimate_pass_at_k(5, 5, 5) == 1.0
    # k = 1 -> c / n
    assert estimate_pass_at_k(10, 3, 1) == pytest.approx(0.3)
    assert estimate_pass_at_k(1, 1, 1) == 1.0
    assert estimate_pass_at_k(1, 0, 1) == 0.0
    # k = n -> 1.0 for any c >= 1, 0.0 for c == 0
    assert estimate_pass_at_k(7, 1, 7) == 1.0
    assert estimate_pass_at_k(7, 0, 7) == 0.0
    # n - c < k -> 1.0
    assert estimate_pass_at_k(5, 3, 3) == 1.0
    # 精确不放回组合数
    assert estimate_pass_at_k(10, 2, 3) == pytest.approx(1.0 - math.comb(8, 3) / math.comb(10, 3))

    for bad_args in (
        (0, 0, 1),
        (5, -1, 1),
        (5, 6, 1),
        (5, 1, 0),
        (5, 1, 6),
        (True, 1, 1),
        (5, False, 1),
        (5, 1, True),
    ):
        with pytest.raises(ValueError):
            estimate_pass_at_k(*bad_args)


async def test_model_evals_grade_with_llm_judge_mock_and_fallbacks():
    class ValidJudgeProvider:
        async def complete(self, system, messages, tools, max_tokens=1024):
            del system, messages, tools, max_tokens
            body = (
                "```json\n"
                '{"correctness": 0.95, "completeness": 0.9, "safety": 1.0, '
                '"overall": 0.95, "rationale": "Minimal root-cause fix with untampered tests."}\n'
                "```"
            )
            return {
                "content": [{"type": "text", "text": body}],
                "usage": {"input_tokens": 120, "output_tokens": 40},
            }

    class TenScaleNoOverallProvider:
        async def complete(self, system, messages, tools, max_tokens=1024):
            del system, messages, tools, max_tokens
            return {
                "content": [
                    {
                        "type": "text",
                        "text": '{"correctness": 8, "completeness": 9, "safety": 10, "rationale": "Good."}',
                    }
                ]
            }

    class BrokenJsonProvider:
        async def complete(self, system, messages, tools, max_tokens=1024):
            del system, messages, tools, max_tokens
            return {"content": [{"type": "text", "text": "not valid json output"}]}

    class IncompleteFieldsProvider:
        async def complete(self, system, messages, tools, max_tokens=1024):
            del system, messages, tools, max_tokens
            return {"content": [{"type": "text", "text": '{"correctness": 0.8}'}]}

    class RaisingProvider:
        async def complete(self, system, messages, tools, max_tokens=1024):
            del system, messages, tools, max_tokens
            raise ConnectionError("network down")

    res_ok = await grade_with_llm_judge(
        ValidJudgeProvider(),
        task_prompt="Fix discount bug",
        agent_answer="Fixed percentage divisor.",
        diff_text="-return a\n+return b\n",
        rubric=["Check edge cases", "Ensure tests untouched"],
    )
    assert res_ok["ok"] is True
    assert res_ok["used_llm"] is True
    assert res_ok["fallback"] is False
    assert res_ok["correctness"] == pytest.approx(0.95)
    assert res_ok["completeness"] == pytest.approx(0.9)
    assert res_ok["safety"] == pytest.approx(1.0)
    assert res_ok["overall"] == pytest.approx(0.95)
    assert res_ok["usage"] == {"input_tokens": 120, "output_tokens": 40}

    judge = LLMJudge(TenScaleNoOverallProvider(), default_rubric="Standard rubric")
    res_scaled = await judge.grade(task_prompt="Fix bug", agent_answer="Done")
    assert res_scaled["ok"] is True
    assert res_scaled["correctness"] == pytest.approx(0.8)
    assert res_scaled["completeness"] == pytest.approx(0.9)
    assert res_scaled["safety"] == pytest.approx(1.0)
    assert res_scaled["overall"] == pytest.approx(0.9)

    fb_none = await grade_with_llm_judge(None, task_prompt="t", agent_answer="a")
    assert fb_none["ok"] is False and fb_none["fallback"] is True and fb_none["fallback_reason"] == "no_provider"

    fb_stub_str = await grade_with_llm_judge("demo", task_prompt="t", agent_answer="a")
    assert fb_stub_str["fallback"] is True and fb_stub_str["fallback_reason"] == "offline_stub_provider"

    fb_stub_obj = await grade_with_llm_judge(DemoProvider(), task_prompt="t", agent_answer="a")
    assert fb_stub_obj["fallback"] is True and fb_stub_obj["fallback_reason"] == "offline_stub_provider"

    fb_json = await grade_with_llm_judge(BrokenJsonProvider(), task_prompt="t", agent_answer="a")
    assert fb_json["fallback"] is True and fb_json["fallback_reason"] == "invalid_json_response"

    fb_inc = await grade_with_llm_judge(IncompleteFieldsProvider(), task_prompt="t", agent_answer="a")
    assert fb_inc["fallback"] is True and fb_inc["fallback_reason"] == "incomplete_score_fields"

    fb_err = await grade_with_llm_judge(RaisingProvider(), task_prompt="t", agent_answer="a")
    assert fb_err["fallback"] is True and fb_err["fallback_reason"] == "provider_error"


def test_model_evals_swebench_loader_no_leak_prompt_and_grader(tmp_path: Path):
    test_patch = (
        "diff --git a/tests/test_bugfix.py b/tests/test_bugfix.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_bugfix.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+from calc import add\n"
        "+\n"
        "+def test_add_secret_oracle():\n"
        "+    # SECRET_TEST_PATCH_MARKER_9123\n"
        "+    assert add(2, 3) == 5\n"
    )
    gold_patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b  # SECRET_GOLD_PATCH_MARKER\n"
    )
    jsonl_path = tmp_path / "swebench_lite.jsonl"
    rec1 = {
        "instance_id": "acme__calc-101",
        "repo": "acme/calc",
        "base_commit": "a" * 40,
        "problem_statement": "add(2, 3) returns -1 instead of 5.",
        "FAIL_TO_PASS": json.dumps(["tests/test_bugfix.py::test_add_secret_oracle"]),
        "PASS_TO_PASS": json.dumps(["tests/test_existing.py::test_baseline"]),
        "test_patch": test_patch,
        "patch": gold_patch,
        "hints_text": "SECRET_HINT_MARKER_4455",
    }
    rec2 = {
        "instance_id": "acme__calc-102",
        "repo": "acme/calc",
        "base_commit": "b" * 40,
        "problem_statement": "Second task.",
        "FAIL_TO_PASS": ["tests/test_other.py::test_other"],
        "PASS_TO_PASS": [],
        "test_patch": "",
    }
    jsonl_path.write_text(
        json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n",
        encoding="utf-8",
    )

    all_inst = load_swebench_instances(jsonl_path)
    assert len(all_inst) == 2
    assert all_inst[0]["FAIL_TO_PASS"] == ["tests/test_bugfix.py::test_add_secret_oracle"]
    assert all_inst[0]["PASS_TO_PASS"] == ["tests/test_existing.py::test_baseline"]
    assert len(load_swebench_instances(jsonl_path, limit=1)) == 1
    filtered = load_swebench_instances(jsonl_path, instance_ids=["acme__calc-102"])
    assert [i["instance_id"] for i in filtered] == ["acme__calc-102"]

    # build_swebench_task_prompt 严禁泄露 test_patch / gold patch / FAIL_TO_PASS / PASS_TO_PASS
    inst = all_inst[0]
    prompt_no_hints = build_swebench_task_prompt(inst, include_hints=False)
    assert "acme__calc-101" in prompt_no_hints
    assert "add(2, 3) returns -1 instead of 5." in prompt_no_hints
    for forbidden in (
        "SECRET_TEST_PATCH_MARKER_9123",
        "SECRET_GOLD_PATCH_MARKER",
        "test_add_secret_oracle",
        "test_baseline",
        "SECRET_HINT_MARKER_4455",
    ):
        assert forbidden not in prompt_no_hints

    prompt_with_hints = build_swebench_task_prompt(inst, include_hints=True)
    assert "SECRET_HINT_MARKER_4455" in prompt_with_hints
    assert "SECRET_TEST_PATCH_MARKER_9123" not in prompt_with_hints
    assert "SECRET_GOLD_PATCH_MARKER" not in prompt_with_hints

    # grade_swebench_patch：在独立临时副本施加 test_patch，修复前 failed、修复后 resolved，且绝不污染原工作区
    ws = tmp_path / "agent_ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "tests" / "test_existing.py").write_text("def test_baseline():\n    assert True\n", encoding="utf-8")

    before_grade = grade_swebench_patch(ws, inst)
    assert before_grade["test_patch_applied"] is True
    assert before_grade["resolved"] is False
    assert before_grade["fail_to_pass"]["total"] == 1
    assert before_grade["fail_to_pass"]["passed"] == 0
    assert before_grade["pass_to_pass"]["total"] == 1
    assert before_grade["pass_to_pass"]["passed"] == 1
    assert not (ws / "tests" / "test_bugfix.py").exists()

    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    after_grade = grade_swebench_patch(ws, inst)
    assert after_grade["test_patch_applied"] is True
    assert after_grade["resolved"] is True
    assert after_grade["passed"] is True
    assert after_grade["fail_to_pass"]["passed"] == 1
    assert after_grade["pass_to_pass"]["passed"] == 1
    assert not (ws / "tests" / "test_bugfix.py").exists()


# ---------------------------------------------------------------------------
# 3. storage.py、observability.py、lease.py：迁移幂等、FIFO seq、SQL 翻译、Span 追踪、Metrics 服务
# ---------------------------------------------------------------------------


def test_storage_schema_migrations_idempotent_and_proposals_fifo_seq_ordering(tmp_path: Path):
    db_path = tmp_path / "state.db"
    s1 = Storage(db_path)
    migs1 = s1.applied_migrations()
    versions = [m["version"] for m in migs1]
    assert versions == sorted(versions)
    assert {1, 2, 3, 4, 5, 6, 7}.issubset(set(versions))
    assert migs1[0]["name"] == "baseline_core_schema"
    assert migs1[1]["name"] == "proposals_fifo_seq"
    assert migs1[4]["name"] == "events_trace_span_columns"

    # 重复打开与显式重跑 _initialize_schema 必须 100% 幂等，不修改历史 applied_at 或产生重复版本
    s2 = Storage(db_path)
    s2._initialize_schema()
    migs2 = s2.applied_migrations()
    assert migs2 == migs1

    # proposals() 必须严格按插入顺序 FIFO seq 返回，而非按随机 UUID id 字典序
    run = s1.create_run("fifo proposals check")
    created_ids: list[str] = []
    for i in range(6):
        if i % 2 == 0:
            prop = s1.create_proposal(
                run["id"],
                path=f"file_{i}.py",
                old_content=f"v={i}\n",
                new_content=f"v={i + 1}\n",
            )
        else:
            prop = s1.create_action_proposal(
                run["id"],
                kind="command",
                tool_name="bash_exec",
                arguments={"command": f"echo {i}"},
                summary=f"cmd {i}",
            )
        created_ids.append(prop["id"])

    listed = s1.proposals(run["id"])
    assert [p["id"] for p in listed] == created_ids

    with sqlite3.connect(db_path) as con:
        seqs = [
            row[0]
            for row in con.execute(
                "SELECT seq FROM proposals WHERE run_id=? ORDER BY seq ASC", (run["id"],)
            ).fetchall()
        ]
    assert seqs == [1, 2, 3, 4, 5, 6]


def test_storage_translate_qmark_to_pyformat_hardened_sql_constructs():
    assert _translate_qmark_to_pyformat("BEGIN IMMEDIATE") == "BEGIN"
    assert _translate_qmark_to_pyformat("  begin immediate  ") == "BEGIN"

    # 单行注释中的 ? 原样保留，真实占位符转 %s
    line_comment_sql = "SELECT * FROM runs -- filter by id?\nWHERE id = ? AND status_col = ?"
    assert _translate_qmark_to_pyformat(line_comment_sql) == (
        "SELECT * FROM runs -- filter by id?\nWHERE id = %s AND status_col = %s"
    )

    # PostgreSQL 嵌套块注释中的 ? 原样保留
    nested_block_sql = "SELECT /* outer ? /* inner ? */ still ? */ id FROM runs WHERE id = ?"
    assert _translate_qmark_to_pyformat(nested_block_sql) == (
        "SELECT /* outer ? /* inner ? */ still ? */ id FROM runs WHERE id = %s"
    )

    # 单引号字面量（含 '' 转义）与 E'...' 反斜杠转义字符串
    quoted_sql = "SELECT 'what''s up?', E'can\\'t? stop?' FROM runs WHERE id = ?"
    assert _translate_qmark_to_pyformat(quoted_sql) == (
        "SELECT 'what''s up?', E'can\\'t? stop?' FROM runs WHERE id = %s"
    )

    # 双引号标识符（含 "" 转义与 ?）
    ident_sql = 'SELECT "col""?name" FROM runs WHERE "weird?" = ? AND id = ?'
    assert _translate_qmark_to_pyformat(ident_sql) == (
        'SELECT "col""?name" FROM runs WHERE "weird?" = %s AND id = %s'
    )

    # PostgreSQL dollar-quoted 字符串 $$...$$ 与 $tag$...$tag$
    dollar_sql = "SELECT $$a?b?c$$, $fn$SELECT ? FROM t$fn$ FROM runs WHERE id = ? AND tenant_id = ?"
    assert _translate_qmark_to_pyformat(dollar_sql) == (
        "SELECT $$a?b?c$$, $fn$SELECT ? FROM t$fn$ FROM runs WHERE id = %s AND tenant_id = %s"
    )


def test_observability_hierarchical_spans_traceparent_and_event_columns(tmp_path: Path):
    storage = Storage(tmp_path / "trace.db")
    run = storage.create_run("hierarchical span tracing test")

    upstream_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    upstream_parent = "00f067aa0ba902b7"
    incoming_tp = f"00-{upstream_trace}-{upstream_parent}-01"

    with bind_trace_context(incoming_tp) as bound_trace:
        assert bound_trace == upstream_trace
        assert current_trace_id() == upstream_trace
        assert current_parent_span_id() == upstream_parent
        assert current_span_id() is None

        with start_span("worker.run", {"run_id": run["id"]}) as outer_span:
            assert outer_span.trace_id == upstream_trace
            assert outer_span.parent_span_id == upstream_parent
            assert len(outer_span.span_id) == 16
            assert current_span_id() == outer_span.span_id
            assert format_traceparent() == f"00-{upstream_trace}-{outer_span.span_id}-01"

            ev_outer = storage.add_event(run["id"], "outer_step", {"phase": "outer"})
            assert ev_outer["trace_id"] == upstream_trace
            assert ev_outer["span_id"] == outer_span.span_id

            with start_span("tool.exec", {"tool": "repo_read"}) as inner_span:
                assert inner_span.trace_id == upstream_trace
                assert inner_span.parent_span_id == outer_span.span_id
                assert inner_span.span_id != outer_span.span_id
                assert format_traceparent() == f"00-{upstream_trace}-{inner_span.span_id}-01"
                storage.add_event(run["id"], "inner_step", {"phase": "inner"})

            # 退出子 Span 后恢复外层 Span 上下文
            assert current_span_id() == outer_span.span_id
            assert inner_span.status == "ok"
            assert inner_span.duration_ms >= 0.0

            # 异常退出 Span 记录 status="error" 与 error.type
            with pytest.raises(ValueError):
                with start_span("failing.child") as err_span:
                    raise ValueError("boom")
            assert err_span.status == "error"
            assert err_span.attributes["error.type"] == "ValueError"

        assert current_span_id() is None

    persisted_events = storage.events(run["id"])
    by_kind = {ev["kind"]: ev for ev in persisted_events}
    assert by_kind["outer_step"]["trace_id"] == upstream_trace
    assert by_kind["outer_step"]["span_id"] == outer_span.span_id
    assert by_kind["inner_step"]["trace_id"] == upstream_trace
    assert by_kind["inner_step"]["span_id"] == inner_span.span_id


def test_metrics_record_tool_metric_and_worker_metrics_server_endpoints(monkeypatch: pytest.MonkeyPatch):
    # 1) 验证全局 METRICS.record_tool_metric 上报统一与分类指标
    unique_tool = f"obs_probe_{uuid.uuid4().hex[:8]}"
    METRICS.record_tool_metric(unique_tool, 12.5, True, category="mcp")
    METRICS.record_tool_metric(unique_tool, 48.0, False, category="mcp")
    global_prom = METRICS.render_prometheus()
    assert f'agent_lab_tool_calls_total{{category="mcp",status="ok",tool="{unique_tool}"}} 1' in global_prom
    assert f'agent_lab_tool_calls_total{{category="mcp",status="error",tool="{unique_tool}"}} 1' in global_prom
    assert f'agent_lab_tool_errors_total{{category="mcp",tool="{unique_tool}"}} 1' in global_prom
    assert f'agent_lab_mcp_calls_total{{status="ok",tool="{unique_tool}"}} 1' in global_prom
    assert f'agent_lab_mcp_duration_ms_count{{tool="{unique_tool}"}} 2' in global_prom

    # 2) 使用隔离 MetricsRegistry 验证 WorkerMetricsServer /healthz、/metrics、404 与可选 Bearer 鉴权
    isolated = MetricsRegistry()
    isolated.record_tool_metric("fs_read", 7.25, True, category="tool")
    isolated.record_tool_metric("subagent_run", 120.0, True, category="subagent")

    monkeypatch.delenv("AGENT_LAB_WORKER_METRICS_TOKEN", raising=False)
    with WorkerMetricsServer(
        port=0,
        host="127.0.0.1",
        metrics=isolated,
        worker_id="worker-metrics-test",
    ) as srv:
        assert srv.is_running is True
        assert srv.port > 0

        health = httpx.get(f"{srv.url}/healthz", timeout=3.0)
        assert health.status_code == 200
        assert health.json() == {"ok": True, "status": "alive", "worker_id": "worker-metrics-test"}

        metrics_resp = httpx.get(f"{srv.url}/metrics", timeout=3.0)
        assert metrics_resp.status_code == 200
        assert "text/plain" in metrics_resp.headers.get("content-type", "")
        assert 'agent_lab_tool_calls_total{category="tool",status="ok",tool="fs_read"} 1' in metrics_resp.text
        assert 'agent_lab_subagent_calls_total{status="ok",tool="subagent_run"} 1' in metrics_resp.text

        missing = httpx.get(f"{srv.url}/not-a-route", timeout=3.0)
        assert missing.status_code == 404

        # 启用 Bearer Token 鉴权保护
        monkeypatch.setenv("AGENT_LAB_WORKER_METRICS_TOKEN", "worker-secret-token")
        unauth = httpx.get(f"{srv.url}/metrics", timeout=3.0)
        assert unauth.status_code == 401
        auth_ok = httpx.get(
            f"{srv.url}/metrics",
            headers={"Authorization": "Bearer worker-secret-token"},
            timeout=3.0,
        )
        assert auth_ok.status_code == 200
        assert "agent_lab_tool_calls_total" in auth_ok.text


# ---------------------------------------------------------------------------
# 4. 条件化实时 PostgreSQL 集成测试（未配置 AGENT_LAB_TEST_POSTGRES_DSN 时干净跳过）
# ---------------------------------------------------------------------------


def test_postgres_live_storage_integration():
    dsn = os.environ.get("AGENT_LAB_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AGENT_LAB_TEST_POSTGRES_DSN 未设置，跳过实时 PostgreSQL 集成测试")

    storage = Storage.from_dsn(dsn)
    created_run_ids: list[str] = []
    tenant = f"ci-pg-{uuid.uuid4().hex[:10]}"
    try:
        assert storage.backend == "postgresql"
        migs = storage.applied_migrations()
        assert [m["version"] for m in migs][:6] == [1, 2, 3, 4, 5, 6]

        # 二次实例化验证迁移幂等
        second = Storage.from_dsn(dsn)
        try:
            assert second.applied_migrations() == migs
        finally:
            second.close()

        run = storage.create_run("live postgres integration run", tenant_id=tenant)
        created_run_ids.append(run["id"])
        fetched = storage.get_run(run["id"])
        assert fetched is not None and fetched["id"] == run["id"]

        # 验证 trace_id / span_id 落盘
        trace_hex = uuid.uuid4().hex
        with bind_trace_context(trace_hex):
            with start_span("pg.live.event") as span:
                ev = storage.add_event(run["id"], "pg_trace_event", {"ok": True})
                assert ev["trace_id"] == trace_hex
                assert ev["span_id"] == span.span_id
        events = storage.events(run["id"])
        assert any(
            e["kind"] == "pg_trace_event" and e["trace_id"] == trace_hex and e["span_id"] == span.span_id
            for e in events
        )

        # 验证 proposals FIFO seq 顺序
        prop_ids = [
            storage.create_proposal(
                run["id"], path=f"f{i}.py", old_content=f"a={i}\n", new_content=f"a={i + 1}\n"
            )["id"]
            for i in range(3)
        ]
        assert [p["id"] for p in storage.proposals(run["id"])] == prop_ids

        # 验证队列租约、心跳、转向收件箱与释放
        storage.enqueue_run(run["id"])
        worker_id = f"pg-worker-{uuid.uuid4().hex[:8]}"
        claim = storage.claim_next_run(worker_id, ttl_seconds=15.0, run_id=run["id"])
        assert claim is not None and claim["run_id"] == run["id"]
        hb = storage.heartbeat_lease(run["id"], worker_id, claim["epoch"], ttl_seconds=15.0)
        assert hb["epoch"] == claim["epoch"]

        storage.enqueue_steering_message(run["id"], "pg steering directive", actor_id="ci")
        drained = storage.drain_steering_messages(run["id"], expected_epoch=claim["epoch"])
        assert len(drained) == 1 and drained[0]["prompt"] == "pg steering directive"
        storage.release_lease(run["id"], worker_id, claim["epoch"], final_state="completed")

        # 验证会话管理：rename / archive / fork
        renamed = storage.rename_run(run["id"], "PG Renamed Title", tenant_id=tenant)
        assert renamed["title"] == "PG Renamed Title"
        archived = storage.archive_run(run["id"], True, tenant_id=tenant)
        assert archived["archived"] is True
        assert all(r["id"] != run["id"] for r in storage.list_runs(tenant_id=tenant, include_archived=False))
        assert any(r["id"] == run["id"] for r in storage.list_runs(tenant_id=tenant, include_archived=True))

        forked = storage.fork_run(run["id"], tenant_id=tenant, title="PG Fork")
        created_run_ids.append(forked["id"])
        assert forked["metadata"]["forked_from_run_id"] == run["id"]
    finally:
        for rid in created_run_ids:
            try:
                storage.delete_run(rid, tenant_id=tenant)
            except Exception:
                pass
        storage.close()

"""真实持久化边界的故障注入，不以回答文本声称“恢复成功”作为依据。"""

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from agent_lab.api import ROOT, create_app
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


class Crash(BaseException):
    """模拟进程突然消失，绕过普通运行时异常处理与末尾 checkpoint。"""


def response(*calls):
    return {
        "content": list(calls) or [{"type": "text", "text": "done"}],
        "stop_reason": "tool_use" if calls else "end_turn",
        "usage": {},
    }


def call(name, arguments=None, call_id="read_1"):
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments or {}}


@pytest.fixture
def harness(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")

    def build(**kwargs):
        return AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", **kwargs)

    return storage, workspace, build


async def test_committed_result_survives_missing_run_checkpoint(harness, monkeypatch):
    storage, _, build = harness

    class Provider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            if not any(m["role"] == "assistant" for m in messages):
                return response(call("repo_read", {"path": "pricing.py"}))
            return response()

    runtime = build(provider=Provider())
    original_event = runtime._event

    def interrupt(run, kind, **payload):
        if kind == "tool_result":
            raise Crash()
        original_event(run, kind, **payload)

    monkeypatch.setattr(runtime, "_event", interrupt)
    with pytest.raises(Crash):
        await runtime.start("read")
    stored = storage.list_runs()[0]
    assert stored["metadata"]["pending_results"] == {}
    assert storage.get_call(stored["id"], "read_1")["status"] == "succeeded"

    def do_not_execute(*args, **kwargs):
        pytest.fail("已提交结果不能重新执行工具")

    monkeypatch.setattr(ToolRegistry, "execute", do_not_execute)
    done = await build(provider=Provider()).resume(stored["id"])
    assert done["status"] == "completed"
    assert storage.get_call(done["id"], "read_1")["attempts"] == 1
    result = json.loads(done["messages"][-2]["content"][0]["content"])
    assert "return subtotal" in result["content"]


@pytest.mark.parametrize("name,expected_attempts", [("repo_read", 2), ("sandbox_test", 1)])
async def test_interrupted_tool_recovery_policy(harness, monkeypatch, name, expected_attempts):
    storage, _, build = harness

    class FinalProvider:
        async def complete(self, *args, **kwargs):
            return response()

    runtime = build(provider=FinalProvider())
    run = runtime.create("recover")
    pending = call(name, {"path": "pricing.py"} if name == "repo_read" else {})
    run["messages"].append({"role": "assistant", "content": [pending]})
    run["metadata"].update(prepared=True, pending_calls=[pending], pending_results={})
    storage.save_run(run)
    storage.schedule_call(run["id"], pending["id"], name, pending["input"])
    storage.start_call(run["id"], pending["id"])
    if name == "sandbox_test":
        monkeypatch.setattr(ToolRegistry, "execute", lambda *a, **k: pytest.fail("未知执行不能自动重跑"))
    done = await build(provider=FinalProvider()).resume(run["id"])
    assert done["status"] == "completed"
    record = storage.get_call(run["id"], pending["id"])
    assert record["attempts"] == expected_attempts
    if name == "sandbox_test":
        assert record["status"] == "unknown"
        assert record["result"]["code"] == "execution_unknown"
        assert record["result"]["tests_executed"] is None
    else:
        assert record["status"] == "succeeded"
        assert record["attempt_history"][0]["status"] == "unknown"


async def test_proposal_created_before_crash_is_not_duplicated(harness, monkeypatch):
    storage, workspace, build = harness
    original_finish = storage.finish_call

    def crash_before_finish(run_id, call_id, result, status=None):
        if result.get("requires_approval"):
            raise Crash()
        return original_finish(run_id, call_id, result, status)

    monkeypatch.setattr(storage, "finish_call", crash_before_finish)
    with pytest.raises(Crash):
        await build().start("修复折扣 bug")
    stored = storage.list_runs()[0]
    proposal = storage.proposals(stored["id"])[0]
    (workspace / "pricing.py").write_text((workspace / "pricing.py").read_text() + "\n# external edit\n")
    monkeypatch.setattr(storage, "finish_call", original_finish)
    recovered = await build().resume(stored["id"])
    assert recovered["status"] == "waiting_approval"
    assert len(storage.proposals(stored["id"])) == 1
    assert recovered["metadata"]["waiting_proposal_ids"] == [proposal["id"]]
    # 审批仍需 CAS；恢复成原提案不意味着可以覆盖期间发生的外部编辑。
    rejected = await build().approve(stored["id"], proposal["id"], True)
    assert rejected["status"] == "failed"
    assert "# external edit" in (workspace / "pricing.py").read_text()


async def test_deferred_tools_have_a_durable_receipt(harness):
    storage, _, build = harness

    class Provider:
        async def complete(self, *args, **kwargs):
            return response(
                call(
                    "propose_patch",
                    {
                        "path": "pricing.py",
                        "old_text": "return subtotal * (1 - discount_percent)",
                        "new_text": "return subtotal * (1 - discount_percent / 100)",
                    },
                    "patch",
                ),
                call("verify_python", {"path": "pricing.py"}, "after_patch"),
            )

    run = await build(provider=Provider()).start("fix")
    assert run["status"] == "waiting_approval"
    deferred = storage.get_call(run["id"], "after_patch")
    assert deferred["attempts"] == 0
    assert deferred["status"] == "failed"
    assert deferred["result"]["code"] == "deferred_until_approval"
    assert len(run["messages"][-1]["content"]) == 2


def test_api_calls_and_paged_result_are_scoped_and_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    app = create_app(tmp_path / "state", provider="demo")
    storage = app.state.storage
    first, second = storage.create_run("one"), storage.create_run("two")
    storage.schedule_call(first["id"], "original", "repo_read", {"path": "sample.py"})
    storage.finish_call(first["id"], "original", {"ok": True, "content": "private marker" * 1000})
    with TestClient(app) as client:
        calls = client.get(f"/api/runs/{first['id']}/calls").json()
        assert len(calls) == 1 and calls[0]["status"] == "succeeded"
        assert "private marker" not in str(calls)  # 摘要接口不反序列化/返回完整大结果。
        page = client.get(f"/api/runs/{first['id']}/tool-result", params={"call_id": "original", "limit": 17})
        assert page.status_code == 200 and len(page.json()["content"]) == 17
        assert page.json()["next_offset"] == 17
        assert (
            client.get(f"/api/runs/{second['id']}/tool-result", params={"call_id": "original"}).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/runs/{first['id']}/tool-result", params={"call_id": "original", "limit": 8001}
            ).status_code
            == 422
        )
        assert "context" in client.get(f"/api/runs/{first['id']}").json()


async def test_context_overflow_stops_before_provider_request(harness):
    storage, _, build = harness

    class NeverProvider:
        async def complete(self, *args, **kwargs):
            pytest.fail("超预算不应发起模型请求")

    result = await build(provider=NeverProvider(), context_chars=10).start("read")
    assert result["status"] == "failed"
    assert result["error"] == "ContextOverflow"
    assert result["steps"] == 0
    assert result["metadata"]["context_stats"]["overflow"] is True
    assert any(e["kind"] == "context_overflow" for e in storage.events(result["id"]))


def test_skill_sections_ignore_fenced_code(tmp_path):
    from agent_lab.skills import SkillCatalog

    directory = tmp_path / "review"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: review\ndescription: review code\n---\n"
        "# Real\nUse this\n```python\n# Fake\npass\n```\n## Child\nMore\n# Next\nEnd\n"
    )
    catalog = SkillCatalog(tmp_path)
    assert catalog.inspect("review")["sections"] == ["Real", "Child", "Next"]
    snapshot = catalog.snapshot("review", "Real")
    assert "# Fake" in snapshot["content"] and "# Next" not in snapshot["content"]
    with pytest.raises(ValueError):
        catalog.snapshot("review", "Fake")


def test_concurrent_startup_migrates_legacy_database_once(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as con:
        con.executescript("""
            CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE proposals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL);
            INSERT INTO runs VALUES ('preserved', 'date', '{"id":"preserved"}');
        """)
    barrier = threading.Barrier(4)

    def initialize(_):
        barrier.wait(timeout=5)
        return Storage(database).get_run("preserved")

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(initialize, range(4))) == [{"id": "preserved"}] * 4
    with sqlite3.connect(database) as con:
        assert [row[1] for row in con.execute("PRAGMA table_info(proposals)")].count("operation_id") == 1

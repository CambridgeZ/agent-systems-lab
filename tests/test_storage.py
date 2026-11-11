"""检查状态跨进程连接持久化、审批不可翻转、审计游标与记忆隔离。"""

import sqlite3

import pytest

from agent_lab.storage import Storage


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

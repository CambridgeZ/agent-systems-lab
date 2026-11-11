"""调用日志与恢复策略分开：数据库保存已知事实，不猜测未完成副作用是否发生。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from agent_lab.storage import Storage, canonical_json, digest
from agent_lab.tools import ToolRegistry


ROOT = Path(__file__).resolve().parents[1]
OLD = "return subtotal * (1 - discount_percent)"
NEW = "return subtotal * (1 - discount_percent / 100)"


@pytest.fixture
def journal(tmp_path):
    storage = Storage(tmp_path / "state.db")
    run = storage.create_run("code task")
    return storage, run["id"]


def test_old_database_migrates_additively_and_preserves_approval(tmp_path):
    database = tmp_path / "old.db"
    legacy_run = {"id": "legacy", "prompt": "kept"}
    legacy_proposal = {"id": "old-proposal", "run_id": "legacy", "status": "rejected"}
    with sqlite3.connect(database) as con:
        con.executescript("""
            CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE proposals (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), body TEXT NOT NULL
            );
        """)
        con.execute("INSERT INTO runs VALUES (?,?,?)", ("legacy", "2026-01-01", json.dumps(legacy_run)))
        con.execute(
            "INSERT INTO proposals VALUES (?,?,?)", ("old-proposal", "legacy", json.dumps(legacy_proposal))
        )
    storage = Storage(database)
    assert storage.get_run("legacy") == legacy_run
    assert storage.get_proposal("old-proposal") == legacy_proposal
    assert storage.list_calls("legacy") == []
    proposal = storage.create_proposal("legacy", "pricing.py", "a", "b", "new-call")
    assert storage.get_proposal_by_operation("legacy", "new-call")["id"] == proposal["id"]
    assert Storage(database).get_proposal("old-proposal") == legacy_proposal


def test_call_identity_uses_sorted_nested_arguments_and_survives_reopen(journal):
    storage, run_id = journal
    first = storage.schedule_call(
        run_id, "call-1", "repo_search", {"query": "折扣", "options": {"b": 2, "a": 1}}
    )
    same = storage.schedule_call(
        run_id, "call-1", "repo_search", {"options": {"a": 1, "b": 2}, "query": "折扣"}
    )
    assert same == first
    assert first["arguments_sha256"] == digest(canonical_json(first["arguments"]))
    assert Storage(storage.path).get_call(run_id, "call-1") == first
    with pytest.raises(ValueError, match="identity_conflict"):
        storage.schedule_call(run_id, "call-1", "repo_read", first["arguments"])
    with pytest.raises(ValueError, match="identity_conflict"):
        storage.schedule_call(run_id, "call-1", "repo_search", {"query": "another"})
    assert len(storage.list_calls(run_id)) == 1


def test_same_call_id_is_scoped_to_run_and_cannot_fetch_other_result(journal):
    storage, one = journal
    two = storage.create_run("another")["id"]
    storage.schedule_call(one, "shared-id", "repo_list", {})
    storage.finish_call(one, "shared-id", {"ok": True, "secret_marker": "only-run-one"})
    storage.schedule_call(two, "shared-id", "repo_read", {"path": "pricing.py"})
    assert storage.get_call(two, "shared-id")["result_sha256"] is None
    with pytest.raises(ValueError, match="not_available"):
        storage.get_call_result(two, "shared-id")
    assert storage.list_calls("unknown") == []
    with pytest.raises(KeyError, match="not_found"):
        storage.get_call_result("unknown", "shared-id")
    with pytest.raises(sqlite3.IntegrityError):
        storage.schedule_call("unknown", "call", "repo_list", {})


@pytest.mark.parametrize("result", [{}, [], False, 0, "", None])
def test_empty_results_are_durable_values_not_cache_misses(journal, result):
    storage, run_id = journal
    storage.schedule_call(run_id, "empty", "example", {})
    storage.start_call(run_id, "empty")
    finished = storage.finish_call(run_id, "empty", result)
    assert finished["status"] == "succeeded"
    assert finished["result_sha256"] is not None
    restored = Storage(storage.path)
    assert restored.get_call(run_id, "empty")["result"] == result
    page = restored.get_call_result(run_id, "empty")
    assert json.loads(page["content"]) == result


def test_explicit_retry_preserves_prior_failure_and_terminal_result_is_immutable(journal):
    storage, run_id = journal
    storage.schedule_call(run_id, "retry", "read", {})
    assert storage.start_call(run_id, "retry")["attempts"] == 1
    failure = storage.finish_call(run_id, "retry", {"ok": False, "code": "temporary_failure"})
    assert failure["status"] == "failed"
    retry = storage.start_call(run_id, "retry")
    assert retry["attempts"] == 2
    assert retry["attempt_history"][0]["result"] == failure["result"]
    done = storage.finish_call(run_id, "retry", {"ok": True, "content": "fresh evidence"})
    assert done["status"] == "succeeded" and done["started_at"] and done["finished_at"]
    assert storage.finish_call(run_id, "retry", done["result"]) == done
    with pytest.raises(ValueError, match="already_finished"):
        storage.start_call(run_id, "retry")
    with pytest.raises(ValueError, match="already_finished"):
        storage.finish_call(run_id, "retry", {"ok": True, "content": "different"})


def test_waiting_approval_and_unknown_are_explicit_persisted_states(journal):
    storage, run_id = journal
    storage.schedule_call(run_id, "patch", "propose_patch", {})
    storage.start_call(run_id, "patch")
    awaiting = storage.finish_call(
        run_id, "patch", {"ok": True, "requires_approval": True, "proposal_id": "p"}
    )
    assert awaiting["status"] == "waiting_approval"
    with pytest.raises(ValueError, match="already_finished"):
        storage.start_call(run_id, "patch")
    storage.schedule_call(run_id, "external", "sandbox_test", {})
    storage.start_call(run_id, "external")
    unknown = storage.finish_call(run_id, "external", {"ok": False, "code": "interrupted"}, status="unknown")
    restored = Storage(storage.path).get_call(run_id, "external")
    assert restored == unknown
    assert restored["status"] == "unknown" and restored["attempts"] == 1
    # 重新打开数据库不会自行开始下一次外部执行；是否重试由执行器策略决定。
    assert [row["status"] for row in storage.list_calls(run_id)] == ["waiting_approval", "unknown"]


def test_restarting_a_running_read_marks_prior_attempt_unknown(journal):
    storage, run_id = journal
    storage.schedule_call(run_id, "read", "repo_read", {"path": "pricing.py"})
    storage.start_call(run_id, "read")
    restored = Storage(storage.path)
    restarted = restored.start_call(run_id, "read")
    assert restarted["attempts"] == 2
    assert restarted["attempt_history"][0]["status"] == "unknown"
    assert restarted["attempt_history"][0]["result"] is None


def test_full_result_can_be_reassembled_from_bounded_unicode_pages(journal):
    storage, run_id = journal
    storage.schedule_call(run_id, "large", "repo_read", {"path": "pricing.py"})
    result = {"ok": True, "content": "中文源码\n" * 500, "metadata": {"b": 2, "a": 1}}
    finished = storage.finish_call(run_id, "large", result)
    parts, offset = [], 0
    while True:
        page = storage.get_call_result(run_id, "large", offset, limit=73)
        assert len(page["content"]) <= 73
        assert page["run_id"] == run_id and page["call_id"] == "large"
        assert page["name"] == "repo_read"
        assert page["arguments_sha256"] == finished["arguments_sha256"]
        assert page["offset"] == offset
        assert page["sha256"] == finished["result_sha256"]
        assert page["total_chars"] == len(canonical_json(result))
        parts.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    complete = "".join(parts)
    assert json.loads(complete) == result
    assert digest(complete) == finished["result_sha256"]
    tail = storage.get_call_result(run_id, "large", len(complete) + 10)
    assert tail["content"] == "" and tail["next_offset"] is None


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 8001), (True, 1), (0, False), (1.5, 10)])
def test_result_page_bounds_are_validated(journal, offset, limit):
    storage, run_id = journal
    with pytest.raises(ValueError, match="page_invalid"):
        storage.get_call_result(run_id, "not-created", offset, limit)


@pytest.mark.parametrize(
    "call_id,name,args", [("", "read", {}), ("a", "", {}), ("a", "read", []), ("a" * 129, "read", {})]
)
def test_invalid_call_identity_is_rejected(journal, call_id, name, args):
    storage, run_id = journal
    with pytest.raises(ValueError):
        storage.schedule_call(run_id, call_id, name, args)
    assert not storage.list_calls(run_id)


def test_concurrent_schedule_deduplicates_same_identity(journal):
    storage, run_id = journal
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(
            pool.map(lambda _: storage.schedule_call(run_id, "one", "read", {"query": "x"}), range(4))
        )
    assert all(record == records[0] for record in records)
    assert len(storage.list_calls(run_id)) == 1


def test_proposal_operation_is_transactionally_deduplicated_and_bound_to_content(journal):
    storage, run_id = journal
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(
            pool.map(lambda _: storage.create_proposal(run_id, "pricing.py", "a", "b", "patch"), range(3))
        )
    assert len({record["id"] for record in records}) == 1
    assert len(storage.proposals(run_id)) == 1
    with pytest.raises(ValueError, match="operation_conflict"):
        storage.create_proposal(run_id, "pricing.py", "a", "c", "patch")
    with pytest.raises(ValueError, match="operation_conflict"):
        storage.create_proposal(run_id, "other.py", "a", "b", "patch")
    other = storage.create_run("other")["id"]
    assert storage.create_proposal(other, "pricing.py", "a", "b", "patch")["id"] != records[0]["id"]


def test_proposal_recovers_crash_before_result_checkpoint_without_second_proposal(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    run_id = storage.create_run("fix")["id"]
    registry = ToolRegistry(storage, workspace, None, run_id)
    args = {"path": "pricing.py", "old_text": OLD, "new_text": NEW}
    storage.schedule_call(run_id, "patch-call", "propose_patch", args)
    storage.start_call(run_id, "patch-call")
    first = registry.execute("propose_patch", args, call_id="patch-call")
    # 模拟创建提案后突然退出，还没 finish_call。期间人类在文件末尾加了一条注释。
    changed = (workspace / "pricing.py").read_text() + "\n# external edit\n"
    (workspace / "pricing.py").write_text(changed)
    restored = Storage(storage.path)
    another_registry = ToolRegistry(restored, workspace, None, run_id)
    replay = another_registry.execute("propose_patch", args, call_id="patch-call")
    assert replay["ok"] and replay["replayed"]
    assert replay["proposal_id"] == first["proposal_id"]
    assert len(restored.proposals(run_id)) == 1
    assert (workspace / "pricing.py").read_text() == changed
    finished = restored.finish_call(run_id, "patch-call", replay)
    assert finished["status"] == "waiting_approval"
    assert restored.get_call_result(run_id, "patch-call")["sha256"] == finished["result_sha256"]
    assert len([event for event in restored.events(run_id) if event["kind"] == "patch_proposed"]) == 1


def test_internal_operation_id_is_not_a_model_parameter_and_replay_cannot_change_patch(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", workspace)
    storage = Storage(tmp_path / "state.db")
    run_id = storage.create_run("fix")["id"]
    registry = ToolRegistry(storage, workspace, None, run_id)
    args = {"path": "pricing.py", "old_text": OLD, "new_text": NEW}
    assert (
        registry.execute("propose_patch", {**args, "operation_id": "forged"})["code"] == "invalid_arguments"
    )
    first = registry.execute("propose_patch", args, call_id="trusted-call")
    invalid = registry.execute("propose_patch", {**args, "new_text": "return 0"}, call_id="trusted-call")
    assert invalid["code"] == "proposal_operation_conflict"
    storage.approve_proposal(first["proposal_id"], True)
    assert registry.apply_proposal(first["proposal_id"])["ok"]
    done = registry.execute("propose_patch", args, call_id="trusted-call")
    assert done["replayed"] and done["applied"] and not done["requires_approval"]
    assert done["proposal_id"] == first["proposal_id"]
    assert NEW in (workspace / "pricing.py").read_text()

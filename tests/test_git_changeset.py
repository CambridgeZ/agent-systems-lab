"""真实 Git Worktree、多文件原子变更集、基线漂移拦截与受控提交测试。"""

import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from agent_lab.api import ROOT, create_app
from agent_lab.git_repo import (
    GitRepoError,
    commit_verified_worktree,
    create_task_worktree,
    init_git_repo_from_dir,
    resolve_commit,
    worktree_status,
)
from agent_lab.runtime import AgentRuntime
from agent_lab.sandbox import SandboxRunner, discover_test_oracles
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


@pytest.fixture
def git_worktree_env(tmp_path):
    origin = tmp_path / "origin_repo"
    worktree = tmp_path / "task_wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/multi_file_repo", origin)
    git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")
    return origin, worktree, base_commit, git_meta, oracles, expected_tests, storage


def test_atomic_changeset_rejects_partial_invalid_edits_without_side_effects(git_worktree_env):
    _, worktree, base_commit, _, oracles, expected_tests, storage = git_worktree_env
    run = storage.create_run("fix multi-file bug")
    registry = ToolRegistry(
        storage,
        worktree,
        None,
        run["id"],
        base_commit=base_commit,
        test_oracles=oracles,
        expected_tests=expected_tests,
    )
    # 第二个文件包含语法错误：整组变更集必须被拒绝，不能产生半成品提案。
    bad_syntax = registry.execute(
        "propose_changeset",
        {
            "summary": "bad syntax in second file",
            "edits": [
                {
                    "path": "discounts.py",
                    "old_text": "    return raw_discount",
                    "new_text": "    return min(raw_discount, 100)",
                },
                {
                    "path": "checkout.py",
                    "old_text": "    applied_discount = subtotal - total",
                    "new_text": "    def broken(:",
                },
            ],
        },
    )
    assert not bad_syntax["ok"] and bad_syntax["code"] == "syntax_error"
    assert storage.proposals(run["id"]) == []

    # 试图在多文件变更集中夹带修改受保护 tests/：整组拒绝。
    touch_test = registry.execute(
        "propose_changeset",
        {
            "edits": [
                {
                    "path": "discounts.py",
                    "old_text": "    return raw_discount",
                    "new_text": "    return 0",
                },
                {
                    "path": "tests/test_checkout.py",
                    "old_text": "5500",
                    "new_text": "0",
                },
            ]
        },
    )
    assert not touch_test["ok"] and touch_test["code"] == "edit_forbidden"
    assert storage.proposals(run["id"]) == []


def test_changeset_rejects_git_baseline_drift_and_gates_git_commit(git_worktree_env):
    _, worktree, base_commit, _, oracles, expected_tests, storage = git_worktree_env
    run = storage.create_run("fix checkout")
    registry = ToolRegistry(
        storage,
        worktree,
        None,
        run["id"],
        base_commit=base_commit,
        test_oracles=oracles,
        expected_tests=expected_tests,
    )
    proposed = registry.execute(
        "propose_changeset",
        {
            "summary": "atomic fix",
            "edits": [
                {
                    "path": "discounts.py",
                    "old_text": "    return raw_discount",
                    "new_text": "    return min(raw_discount, coupon.max_discount_cents) if coupon.max_discount_cents is not None else raw_discount",
                },
                {
                    "path": "checkout.py",
                    "old_text": "    total = discount if coupon is not None else subtotal\n    applied_discount = subtotal - total",
                    "new_text": "    applied_discount = min(subtotal, discount)\n    total = subtotal - applied_discount",
                },
            ],
        },
        call_id="cs_1",
    )
    assert proposed["ok"] and proposed["kind"] == "changeset"
    assert proposed["paths"] == ["discounts.py", "checkout.py"]
    assert proposed["base_commit"] == base_commit
    # 同 call_id 重放必须幂等返回原提案
    replayed = registry.execute(
        "propose_changeset",
        {
            "summary": "atomic fix",
            "edits": [
                {
                    "path": "discounts.py",
                    "old_text": "    return raw_discount",
                    "new_text": "    return min(raw_discount, coupon.max_discount_cents) if coupon.max_discount_cents is not None else raw_discount",
                },
                {
                    "path": "checkout.py",
                    "old_text": "    total = discount if coupon is not None else subtotal\n    applied_discount = subtotal - total",
                    "new_text": "    applied_discount = min(subtotal, discount)\n    total = subtotal - applied_discount",
                },
            ],
        },
        call_id="cs_1",
    )
    assert replayed["replayed"] is True and replayed["proposal_id"] == proposed["proposal_id"]

    storage.approve_proposal(proposed["proposal_id"], True)
    applied = registry.apply_proposal(proposed["proposal_id"])
    assert applied["ok"] and applied["paths"] == ["discounts.py", "checkout.py"]

    # 验证未通过（如 syntax_only / failed / unavailable）时严禁创建 Git commit
    for non_passing in (
        {"status": "syntax_only", "tests_executed": False},
        {"status": "failed", "tests_executed": True},
        {"status": "unavailable", "tests_executed": False},
        {"status": "passed", "tests_executed": False},
    ):
        with pytest.raises(GitRepoError) as exc_info:
            commit_verified_worktree(worktree, base_commit, non_passing)
        assert exc_info.value.code == "verification_required"
    assert resolve_commit(worktree, "HEAD") == base_commit

    # 真实测试通过后才允许提交，产生新的 commit SHA
    committed = commit_verified_worktree(
        worktree, base_commit, {"status": "passed", "tests_executed": True}, message="fix: verified"
    )
    assert committed["ok"] and committed["commit_sha"] != base_commit
    assert committed["files"] == ["checkout.py", "discounts.py"]
    status_after = worktree_status(worktree, base_commit=base_commit)
    assert not status_after["dirty"] and not status_after["baseline_matches_head"]

    # 若另一份基于旧 base_commit 的提案在 HEAD 漂移后尝试应用，必须因 baseline_changed 被拒绝
    stale_proposal = storage.create_proposal(
        run["id"], "models.py", "sku: str", "sku: str  # note", base_commit=base_commit
    )
    storage.approve_proposal(stale_proposal["id"], True)
    rejected = registry.apply_proposal(stale_proposal["id"])
    assert not rejected["ok"] and rejected["code"] == "baseline_changed"


def test_commit_blocks_tampered_test_directory(git_worktree_env):
    _, worktree, base_commit, _, _, _, _ = git_worktree_env
    (worktree / "discounts.py").write_text((worktree / "discounts.py").read_text() + "\n# edit\n")
    (worktree / "tests/test_checkout.py").write_text("# tampered\n")
    with pytest.raises(GitRepoError) as exc_info:
        commit_verified_worktree(worktree, base_commit, {"status": "passed", "tests_executed": True})
    assert exc_info.value.code == "test_oracle_modified"
    # 恢复后确认 HEAD 未推进
    subprocess.run(["git", "checkout", "--", "."], cwd=worktree, check=True)
    assert resolve_commit(worktree, "HEAD") == base_commit


async def test_multi_file_demo_end_to_end_and_api_template(git_worktree_env, tmp_path, monkeypatch):
    _, worktree, _, git_meta, oracles, expected_tests, storage = git_worktree_env
    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": True,
            "code": "passed",
            "tests_executed": True,
            "test_summary": {"tests_run": expected_tests, "expected": expected_tests, "failures": 0, "errors": 0, "skipped": 0, "successful": True},
        },
    )
    runtime = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider="demo",
        commit_on_pass=True,
    )
    created = runtime.create(
        "请检查 checkout.py 与 discounts.py 的多文件结算逻辑并修复验证。",
        mode="agent",
        extra_metadata={"git": git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    waiting = await runtime.execute(created["id"])
    assert waiting["status"] == "waiting_approval"
    proposals = storage.proposals(waiting["id"])
    assert len(proposals) == 1 and proposals[0]["kind"] == "changeset"
    assert proposals[0]["paths"] == ["discounts.py", "checkout.py"]

    done = await runtime.approve(waiting["id"], proposals[0]["id"], True)
    assert done["status"] == "completed"
    ver = done["metadata"]["verification"]
    assert ver["status"] == "passed" and ver["tests_executed"] is True
    assert ver["committed_sha"] and ver["committed_sha"] != git_meta["base_commit"]

    # 验证 FastAPI multi_file 模板与 repomap 端点
    monkeypatch.delenv("AGENT_LAB_TOKEN", raising=False)
    app = create_app(tmp_path / "api_state", provider="demo")
    with TestClient(app) as client:
        resp = client.post(
            "/api/runs",
            json={
                "prompt": "修复 checkout.py 与 discounts.py 多文件结算封顶 bug",
                "mode": "agent",
                "template": "multi_file",
            },
        )
        assert resp.status_code == 202
        rid = resp.json()["id"]
        for _ in range(120):
            detail = client.get(f"/api/runs/{rid}").json()
            if detail["status"] == "waiting_approval":
                break
            time.sleep(0.02)
        assert detail["status"] == "waiting_approval"
        assert detail["git"]["base_commit"]
        assert detail["working_memory"]["valid_facts"]
        rmap = client.get(f"/api/runs/{rid}/repomap").json()
        assert rmap["ok"] and "compute_discount_cents" in rmap["map"]
        pid = detail["proposals"][0]["id"]
        assert client.post(f"/api/runs/{rid}/proposals/{pid}", json={"approved": True}).status_code == 202
        for _ in range(120):
            detail = client.get(f"/api/runs/{rid}").json()
            if detail["status"] == "completed":
                break
            time.sleep(0.02)
        assert detail["status"] == "completed"
        assert detail["verification"]["status"] == "passed"
        assert detail["verification"]["committed_sha"]
        # 补丁落盘修改源码后，先前绑定的 2 条文件哈希事实自动变为 stale
        assert detail["working_memory"]["stale_count"] == 2

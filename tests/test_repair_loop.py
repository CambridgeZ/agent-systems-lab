"""测试驱动自动修复循环与验证状态机分离测试。"""

import json
import uuid

from agent_lab.api import ROOT
from agent_lab.git_repo import create_task_worktree, init_git_repo_from_dir, resolve_commit
from agent_lab.runtime import AgentRuntime
from agent_lab.sandbox import SandboxRunner, discover_test_oracles
from agent_lab.storage import Storage


def _call(name: str, **kwargs):
    return {"type": "tool_use", "id": "rep_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict]):
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }


async def test_repair_loop_recovers_after_failed_test_and_commits_verified_patch(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin)
    git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")

    def fake_run_suite(self, ws, orc, expected_tests, pattern="test*.py"):
        code_text = (ws / "pricing.py").read_text(encoding="utf-8")
        passed = "discount_percent / 100" in code_text
        return {
            "ok": passed,
            "code": "passed" if passed else "tests_failed",
            "tests_executed": True,
            "output": "OK" if passed else "AssertionError: 180.0 != -1800",
        }

    monkeypatch.setattr(SandboxRunner, "run_suite", fake_run_suite)

    class TwoTryProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            proposals = [r for r in results if r.get("proposal_id")]
            tests = [r for r in results if r.get("code") in {"passed", "tests_failed"}]
            if len(proposals) > len(tests):
                return _resp([_call("sandbox_test")])
            if tests and tests[-1]["code"] == "passed":
                return _resp([{"type": "text", "text": "经过 1 轮测试修复后通过。"}])
            if not proposals:
                # 第 0 轮故意写出错误除数 / 10，触发测试失败
                return _resp(
                    [
                        _call(
                            "propose_patch",
                            path="pricing.py",
                            old_text="return subtotal * (1 - discount_percent)",
                            new_text="return subtotal * (1 - discount_percent / 10)",
                        )
                    ]
                )
            # 第 1 轮根据失败回执修正为 / 100
            assert tests[-1]["repair_budget"]["remaining_rounds"] == 2
            return _resp(
                [
                    _call(
                        "propose_patch",
                        path="pricing.py",
                        old_text="return subtotal * (1 - discount_percent / 10)",
                        new_text="return subtotal * (1 - discount_percent / 100)",
                    )
                ]
            )

    runtime = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=TwoTryProvider(),
        max_repair_rounds=2,
        commit_on_pass=True,
        auto_approve_proposals=True,
    )
    created = runtime.create(
        "修复 pricing.py",
        extra_metadata={"git": git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    done = await runtime.execute(created["id"])
    assert done["status"] == "completed"
    ver = done["metadata"]["verification"]
    assert ver["status"] == "passed" and ver["tests_executed"] is True
    assert ver["repair_rounds_used"] == 1
    assert ver["committed_sha"] and resolve_commit(worktree, "HEAD") == ver["committed_sha"]


async def test_repair_loop_enforces_max_rounds_and_keeps_completed_distinct_from_passed(
    tmp_path, monkeypatch
):
    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin)
    git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")

    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": False,
            "code": "tests_failed",
            "tests_executed": True,
            "output": "FAIL: still wrong",
        },
    )

    class StubbornBuggyProvider:
        def __init__(self):
            self.patch_idx = 0

        async def complete(self, system, messages, tools, max_tokens=2048):
            results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            if any(r.get("code") == "repair_rounds_exhausted" for r in results):
                return _resp([{"type": "text", "text": "修复轮次已耗尽，测试仍未通过。"}])
            proposals = [r for r in results if r.get("proposal_id")]
            tests = [r for r in results if r.get("code") == "tests_failed"]
            if len(proposals) > len(tests):
                return _resp([_call("sandbox_test")])
            self.patch_idx += 1
            old_expr = (
                "return subtotal * (1 - discount_percent)"
                if self.patch_idx == 1
                else f"return subtotal * (1 - discount_percent / {self.patch_idx - 1})"
            )
            return _resp(
                [
                    _call(
                        "propose_patch",
                        path="pricing.py",
                        old_text=old_expr,
                        new_text=f"return subtotal * (1 - discount_percent / {self.patch_idx})",
                    )
                ]
            )

    runtime = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=StubbornBuggyProvider(),
        max_steps=16,
        max_repair_rounds=2,
        commit_on_pass=True,
        auto_approve_proposals=True,
    )
    created = runtime.create(
        "修复 pricing.py",
        extra_metadata={"git": git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    done = await runtime.execute(created["id"])
    # 控制流正常收敛为 completed，但验证结论严格保持 failed，且绝未生成 Git commit！
    assert done["status"] == "completed"
    ver = done["metadata"]["verification"]
    assert ver["status"] == "failed"
    assert ver["repair_rounds_used"] == 2
    assert ver["exhausted"] is True
    assert ver["committed_sha"] is None
    assert resolve_commit(worktree, "HEAD") == base_commit


async def test_incomplete_receipt_or_unavailable_docker_never_counts_as_passed(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin)
    git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")

    # 模拟代码调用 os._exit(0) 导致容器 0 退出但无完整回执（sandbox_incomplete）
    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": False,
            "code": "sandbox_incomplete",
            "tests_executed": None,
            "output": "",
        },
    )

    class ExitZeroProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            if not results:
                return _resp([_call("verify_python", path="pricing.py"), _call("sandbox_test")])
            return _resp([{"type": "text", "text": "结束。"}])

    runtime = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=ExitZeroProvider(),
        commit_on_pass=True,
    )
    created = runtime.create(
        "检测提前退出",
        extra_metadata={"git": git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    done = await runtime.execute(created["id"])
    assert done["status"] == "completed"
    assert done["metadata"]["verification"]["status"] == "failed"
    assert done["metadata"]["verification"]["last_code"] == "sandbox_incomplete"
    assert done["metadata"]["verification"]["committed_sha"] is None
    assert resolve_commit(worktree, "HEAD") == base_commit


async def test_sandbox_infra_unavailable_maps_to_unavailable_status(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin)
    git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")

    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": False,
            "code": "sandbox_unavailable",
            "tests_executed": False,
            "output": "docker daemon unreachable",
        },
    )

    class InfraDownProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            if not results:
                return _resp([_call("sandbox_test")])
            return _resp([{"type": "text", "text": "Docker 不可用，已如实报告。"}])

    runtime = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=InfraDownProvider(),
        commit_on_pass=True,
    )
    created = runtime.create(
        "基础设施不可用检测",
        extra_metadata={"git": git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    done = await runtime.execute(created["id"])
    assert done["status"] == "completed"
    ver = done["metadata"]["verification"]
    assert ver["status"] == "unavailable"
    assert ver["last_code"] == "sandbox_unavailable"
    assert ver["committed_sha"] is None
    assert resolve_commit(worktree, "HEAD") == base_commit


async def test_second_patch_after_auto_commit_advances_base_commit_cleanly(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(ROOT / "data/sample_repo", origin)
    caller_git_meta = create_task_worktree(origin, worktree, ref=base_commit)
    oracles, expected_tests = discover_test_oracles(worktree)
    storage = Storage(tmp_path / "state.db")

    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": True,
            "code": "passed",
            "tests_executed": True,
            "output": "OK",
        },
    )

    class TwoSequentialCommitsProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            results = [
                json.loads(b["content"])
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            proposals = [r for r in results if r.get("proposal_id")]
            tests = [r for r in results if r.get("code") == "passed"]
            if len(proposals) == 0:
                return _resp(
                    [
                        _call(
                            "propose_patch",
                            path="pricing.py",
                            old_text="return subtotal * (1 - discount_percent)",
                            new_text="return subtotal * (1 - discount_percent / 100)",
                        )
                    ]
                )
            if len(proposals) == 1 and len(tests) == 0:
                return _resp([_call("sandbox_test")])
            if len(proposals) == 1 and len(tests) == 1:
                return _resp(
                    [
                        _call(
                            "propose_patch",
                            path="pricing.py",
                            old_text="return subtotal * (1 - discount_percent / 100)",
                            new_text="return round(subtotal * (1 - discount_percent / 100), 6)",
                        )
                    ]
                )
            if len(proposals) == 2 and len(tests) == 1:
                return _resp([_call("sandbox_test")])
            return _resp([{"type": "text", "text": "两阶段提交均验证通过。"}])

    runtime = AgentRuntime(
        storage,
        worktree,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=TwoSequentialCommitsProvider(),
        commit_on_pass=True,
        auto_approve_proposals=True,
    )
    created = runtime.create(
        "连续两次自动提交测试",
        extra_metadata={"git": caller_git_meta, "test_oracles": oracles, "expected_tests": expected_tests},
    )
    done = await runtime.execute(created["id"])
    assert done["status"] == "completed"
    # 调用方传入的字典未被原地污染
    assert caller_git_meta["base_commit"] == base_commit
    git_state = done["metadata"]["git"]
    assert git_state["initial_base_commit"] == base_commit
    commit_events = [e for e in storage.events(done["id"]) if e["kind"] == "git_committed"]
    assert len(commit_events) == 2
    first_sha = commit_events[0]["payload"]["commit_sha"]
    second_sha = commit_events[1]["payload"]["commit_sha"]
    assert first_sha != base_commit and second_sha != first_sha
    assert git_state["base_commit"] == second_sha
    assert resolve_commit(worktree, "HEAD") == second_sha

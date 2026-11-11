"""独立集成审查：预算、历史身份、分页和崩溃重放共同成立。"""

import json

import pytest

from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


class CapturingProvider:
    def __init__(self, content=None, stop_reason="end_turn"):
        self.content = content or [{"type": "text", "text": "已检查。"}]
        self.stop_reason = stop_reason
        self.requests = []

    async def complete(self, system, messages, tools, max_tokens=2048):
        self.requests.append({"system": system, "messages": messages, "tools": tools})
        return {"content": self.content, "stop_reason": self.stop_reason, "usage": {}}


def build_runtime(tmp_path, provider=None, context_chars=24000):
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    (workspace / "sample.py").write_text("value = 1\n")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return AgentRuntime(
        Storage(tmp_path / "state.db"),
        workspace,
        knowledge,
        skills,
        provider=provider or CapturingProvider(),
        context_chars=context_chars,
    )


def pair(call_id, content):
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": "repo_list", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content}],
        },
    ]


async def test_context_stats_equal_full_serialized_prompt_with_escaped_pinned_state(tmp_path):
    provider = CapturingProvider()
    runtime = build_runtime(tmp_path, provider, context_chars=1800)
    run = runtime.create("检查 sample.py")
    run["messages"].extend(pair("old_read", "x" * 4000))
    run["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "继续检查。"}]})
    run["metadata"]["active_skills"] = {
        "review": {"name": "review", "content": '\\"\n' * 10, "section": None, "sha256": "a" * 64}
    }
    await runtime._complete(run, run["messages"], [], 'system "\\\n')
    encoded = json.dumps(provider.requests[0], ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= runtime.context.max_chars
    assert runtime.context.stats()["request_chars"] == len(encoded)
    assert runtime.context.stats()["dropped_messages"] == 2
    assert "SKILL_SNAPSHOT" in str(provider.requests[0]["messages"])


def test_escaped_result_page_is_not_replaced_by_another_reference(tmp_path):
    runtime = build_runtime(tmp_path)
    run = runtime.create("按页阅读")
    runtime.storage.schedule_call(run["id"], "original", "repo_read", {"path": "sample.py"})
    original = {"ok": True, "content": "\\" * 6000}
    runtime.storage.finish_call(run["id"], "original", original)
    page = runtime.storage.get_call_result(run["id"], "original", offset=100, limit=1200)
    # JSON 字符转义可使一页的线长超过常规投影阈值；分页结果不能再次变成引用。
    assert len(json.dumps(page, ensure_ascii=False)) > 2600
    view = runtime._project_result(run["id"], "page_call", "tool_result_get", page)
    assert view["content"] == page["content"]
    assert view["call_id"] == "original"
    assert not view.get("content_omitted")


async def test_reused_id_from_pruned_history_is_rejected_before_journal_dispatch(tmp_path):
    reused = {"type": "tool_use", "id": "old_read", "name": "repo_list", "input": {}}
    provider = CapturingProvider([reused], stop_reason="tool_use")
    runtime = build_runtime(tmp_path, provider, context_chars=1200)
    run = runtime.create("检查代码")
    run["messages"].extend(pair("old_read", "x" * 4000))
    run["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "继续。"}]})
    with pytest.raises(ValueError, match="重用|重复"):
        await runtime._complete(run, run["messages"], [], "system")
    assert runtime.context.stats()["dropped_messages"] == 2


def test_skill_snapshot_does_not_change_when_same_call_recovers_after_save_crash(tmp_path, monkeypatch):
    runtime = build_runtime(tmp_path)
    path = runtime.skills.directory / "review" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: review\ndescription: 代码审查\n---\n# Review\n版本 A\n")
    run = runtime.create("加载审查技能")
    registry = ToolRegistry(runtime.storage, runtime.workspace, runtime.knowledge, run["id"])
    call = {"type": "tool_use", "id": "load_once", "name": "skill_load", "input": {"name": "review"}}
    finish = runtime.storage.finish_call

    def crash_before_result_commit(*args, **kwargs):
        raise RuntimeError("模拟 snapshot 已保存、journal 未提交的进程退出")

    monkeypatch.setattr(runtime.storage, "finish_call", crash_before_result_commit)
    with pytest.raises(RuntimeError):
        runtime._execute_call(run, registry, call)
    saved = runtime.storage.get_run(run["id"])
    version_a = saved["metadata"]["active_skills"]["review"]
    assert "版本 A" in version_a["content"]
    path.write_text(path.read_text().replace("版本 A", "版本 B"))
    monkeypatch.setattr(runtime.storage, "finish_call", finish)
    runtime._execute_call(saved, registry, call)
    # 同一持久化调用的恢复不等于新的显式 load；活动版本仍须是已保存的版本。
    assert saved["metadata"]["active_skills"]["review"] == version_a


@pytest.mark.parametrize("provider_id", ["r" * 128, "report"])
async def test_parallel_worker_can_journal_every_accepted_provider_id(tmp_path, provider_id):
    class LongIdProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            has_results = any(
                isinstance(message.get("content"), list)
                and any(block.get("type") == "tool_result" for block in message["content"])
                for message in messages
            )
            if "ROLE:" in system and not has_results:
                return {
                    "content": [{"type": "tool_use", "id": provider_id, "name": "repo_list", "input": {}}],
                    "stop_reason": "tool_use",
                    "usage": {},
                }
            return {
                "content": [{"type": "text", "text": "已读目录。"}],
                "stop_reason": "end_turn",
                "usage": {},
            }

    runtime = build_runtime(tmp_path, LongIdProvider())
    run = await runtime.start("检查目录", mode="parallel")
    assert run["status"] == "completed", run
    calls = runtime.storage.list_calls(run["id"])
    assert sum(call["name"] == "repo_list" for call in calls) == 2
    assert all(call["result_sha256"] is not None for call in calls)


async def test_worker_finished_result_recovers_without_reexecuting_after_checkpoint_gap(
    tmp_path, monkeypatch
):
    class ReadingWorkers:
        async def complete(self, system, messages, tools, max_tokens=2048):
            has_results = any(
                isinstance(message.get("content"), list)
                and any(block.get("type") == "tool_result" for block in message["content"])
                for message in messages
            )
            if "ROLE:" in system and not has_results:
                return {
                    "content": [{"type": "tool_use", "id": "read_once", "name": "repo_list", "input": {}}],
                    "stop_reason": "tool_use",
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "已审查。"}], "stop_reason": "end_turn", "usage": {}}

    runtime = build_runtime(tmp_path, ReadingWorkers())
    finish = runtime.storage.finish_call
    execute = ToolRegistry.execute
    observed = []
    crashed = False

    def observed_execute(self, name, arguments, call_id=None):
        if name == "repo_list":
            observed.append(call_id)
        return execute(self, name, arguments, call_id=call_id)

    def commit_then_crash(run_id, call_id, result, status=None):
        nonlocal crashed
        record = finish(run_id, call_id, result, status=status)
        if not crashed and call_id.startswith("reviewer:") and record["name"] == "repo_list":
            crashed = True
            raise RuntimeError("模拟 journal 已提交、worker pending_results 尚未 checkpoint 的退出")
        return record

    monkeypatch.setattr(ToolRegistry, "execute", observed_execute)
    monkeypatch.setattr(runtime.storage, "finish_call", commit_then_crash)
    interrupted = await runtime.start("读取目录", mode="parallel")
    assert interrupted["status"] == "failed"
    reviewer = interrupted["metadata"]["workers"]["reviewer"]
    assert reviewer["pending_calls"] and not reviewer["pending_results"]
    committed = [c for c in runtime.storage.list_calls(interrupted["id"]) if c["name"] == "repo_list"]
    assert len(committed) == 2 and all(c["result_sha256"] for c in committed)
    # 新 Runtime/Storage 模拟恢复，观测真实工具执行次数而非最终回答。
    restored = AgentRuntime(
        Storage(runtime.storage.path),
        runtime.workspace,
        tmp_path / "knowledge",
        tmp_path / "skills",
        provider=ReadingWorkers(),
    )
    completed = await restored.resume(interrupted["id"])
    assert completed["status"] == "completed", completed
    assert len(observed) == 2 and len(set(observed)) == 2
    assert all(
        c["attempts"] == 1 for c in restored.storage.list_calls(completed["id"]) if c["name"] == "repo_list"
    )


async def test_active_skill_and_approval_decision_survive_pruning_without_body_duplication(tmp_path):
    provider = CapturingProvider()
    runtime = build_runtime(tmp_path, provider, context_chars=2000)
    path = runtime.skills.directory / "review" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: review\ndescription: 代码审查\n---\n# Review\nPINNED_BODY_ONCE_42\n")
    run = runtime.create("检查代码")
    registry = ToolRegistry(runtime.storage, runtime.workspace, runtime.knowledge, run["id"])
    load = {"type": "tool_use", "id": "load", "name": "skill_load", "input": {"name": "review"}}
    result = runtime._execute_call(run, registry, load)
    run["messages"].extend(
        [
            {"role": "assistant", "content": [load]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "load", "content": json.dumps(result)}],
            },
        ]
    )
    run["messages"].extend(pair("old_evidence", "x" * 5000))
    run["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "继续验证。"}]})
    run["metadata"]["decisions"] = [{"proposal_id": "approval_must_survive", "approved": True}]
    await runtime._complete(run, run["messages"], [], "system")
    prompt = json.dumps(provider.requests[0], ensure_ascii=False)
    assert runtime.context.stats()["dropped_messages"] >= 4
    assert "approval_must_survive" in prompt
    assert prompt.count("PINNED_BODY_ONCE_42") == 1
    assert prompt.count("SKILL_SNAPSHOT") == 1
    assert "x" * 100 not in prompt


async def test_worker_can_fetch_large_result_using_exposed_internal_call_id(tmp_path):
    observed_references = []
    observed_pages = []

    class PagingWorkers:
        async def complete(self, system, messages, tools, max_tokens=2048):
            if "ROLE:" not in system:
                return {
                    "content": [{"type": "text", "text": "汇总完成。"}],
                    "stop_reason": "end_turn",
                    "usage": {},
                }
            results = [
                json.loads(block["content"])
                for message in messages
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            ]
            if not results:
                call = {
                    "type": "tool_use",
                    "id": "read_large",
                    "name": "repo_read",
                    "input": {"path": "large.py"},
                }
            elif results[-1].get("content_omitted"):
                view = results[-1]
                reference = view["retrieve"]
                observed_references.append(reference["call_id"])
                assert reference["call_id"] != "read_large"
                call = {
                    "type": "tool_use",
                    "id": "fetch_page",
                    "name": reference["tool"],
                    "input": {key: value for key, value in reference.items() if key != "tool"},
                }
            else:
                page = results[-1]
                assert page["content_format"] == "json_text_page"
                assert "EVIDENCE_FROM_FULL_RESULT" in page["content"]
                observed_pages.append(page)
                return {
                    "content": [{"type": "text", "text": "已按页读取证据。"}],
                    "stop_reason": "end_turn",
                    "usage": {},
                }
            return {"content": [call], "stop_reason": "tool_use", "usage": {}}

    runtime = build_runtime(tmp_path, PagingWorkers())
    (runtime.workspace / "large.py").write_text("# EVIDENCE_FROM_FULL_RESULT\n" + "# payload\n" * 1000)
    completed = await runtime.start("读取大文件", mode="parallel")
    assert completed["status"] == "completed", completed
    assert len(observed_references) == len(set(observed_references)) == 2
    assert len(observed_pages) == 2
    for page in observed_pages:
        record = runtime.storage.get_call(completed["id"], page["call_id"])
        assert page["sha256"] == record["result_sha256"]
        assert "EVIDENCE_FROM_FULL_RESULT" in record["result"]["content"]


def test_repeated_explicit_skill_load_is_deduplicated_and_unload_persists(tmp_path):
    runtime = build_runtime(tmp_path)
    path = runtime.skills.directory / "review" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: review\ndescription: 代码审查\n---\n# Review\n不重复注入的正文。\n")
    run = runtime.create("加载与卸载技能")
    registry = ToolRegistry(runtime.storage, runtime.workspace, runtime.knowledge, run["id"])

    def call(identifier, name):
        return {"type": "tool_use", "id": identifier, "name": name, "input": {"name": "review"}}

    first = runtime._execute_call(run, registry, call("load_first", "skill_load"))
    second_call = call("load_again", "skill_load")
    second = runtime._execute_call(run, registry, second_call)
    assert first["already_loaded"] is False and second["already_loaded"] is True
    assert first["sha256"] == second["sha256"]
    assert len(run["metadata"]["active_skills"]) == 1
    assert sum("SKILL_SNAPSHOT" in message["content"] for message in runtime._pinned(run)) == 1
    unloaded = runtime._execute_call(run, registry, call("unload", "skill_unload"))
    assert unloaded["unloaded"] is True
    restored = runtime.storage.get_run(run["id"])
    assert restored["metadata"]["active_skills"] == {}
    assert not any("SKILL_SNAPSHOT" in message["content"] for message in runtime._pinned(restored))
    # 回放已完成 load 只返回历史回执，不能撤销后来显式的 unload。
    assert runtime._execute_call(restored, registry, second_call) == second
    assert restored["metadata"]["active_skills"] == {}


async def test_worker_report_commit_gap_recovers_without_second_final_model_request(tmp_path, monkeypatch):
    class ReportingProvider:
        def __init__(self, report):
            self.report = report
            self.worker_requests = []

        async def complete(self, system, messages, tools, max_tokens=2048):
            role = next((role for role in ("reviewer", "tester") if "ROLE:" + role in system), None)
            if role:
                self.worker_requests.append(role)
            return {
                "content": [{"type": "text", "text": self.report if role else "汇总完成。"}],
                "stop_reason": "end_turn",
                "usage": {},
            }

    first_provider = ReportingProvider("已提交的报告版本 A")
    runtime = build_runtime(tmp_path, first_provider)
    finish = runtime.storage.finish_call
    crashed = False

    def commit_report_then_crash(run_id, call_id, result, status=None):
        nonlocal crashed
        record = finish(run_id, call_id, result, status=status)
        if not crashed and call_id == "reviewer:report":
            crashed = True
            raise RuntimeError("模拟 report 已提交、state.answer 尚未 checkpoint 的退出")
        return record

    monkeypatch.setattr(runtime.storage, "finish_call", commit_report_then_crash)
    interrupted = await runtime.start("独立审查并总结", mode="parallel")
    assert interrupted["status"] == "failed"
    assert "answer" not in interrupted["metadata"]["workers"]["reviewer"]
    committed = runtime.storage.get_call(interrupted["id"], "reviewer:report")
    assert committed["result"]["content"] == "已提交的报告版本 A"
    # 新模型若被错误地再次调用，会给不同报告并与既有 report ID 的已提交内容冲突。
    second_provider = ReportingProvider("不能重新生成的报告版本 B")
    restored = AgentRuntime(
        Storage(runtime.storage.path),
        runtime.workspace,
        tmp_path / "knowledge",
        tmp_path / "skills",
        provider=second_provider,
    )
    completed = await restored.resume(interrupted["id"])
    assert completed["status"] == "completed", completed
    assert second_provider.worker_requests == []
    assert completed["metadata"]["workers"]["reviewer"]["answer"] == "已提交的报告版本 A"
    assert restored.storage.get_call(completed["id"], "reviewer:report") == committed

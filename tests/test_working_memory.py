"""带证据来源与哈希校验的结构化工作记忆（Working Memory）测试。"""

import json
import shutil

import pytest

from agent_lab.api import ROOT
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage, digest
from agent_lab.tools import ToolRegistry
from agent_lab.working_memory import (
    WorkingMemoryError,
    audit_working_memory,
    update_working_memory,
)


def test_working_memory_verifies_sources_and_marks_stale_on_file_mutation(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    storage = Storage(tmp_path / "state.db")
    run = storage.create_run("working memory test")
    run_id = run["id"]

    # 1. 绑定不存在的文件或未完成的工具调用必须立即拒绝，防止凭空编造事实
    with pytest.raises(WorkingMemoryError) as exc1:
        update_working_memory(
            ws,
            storage,
            run_id,
            None,
            {
                "facts": [
                    {
                        "id": "bad1",
                        "claim": "不存在的文件",
                        "source_type": "file",
                        "source_ref": "nonexistent.py",
                    }
                ]
            },
        )
    assert exc1.value.code == "unverified_source"

    with pytest.raises(WorkingMemoryError) as exc2:
        update_working_memory(
            ws,
            storage,
            run_id,
            None,
            {
                "facts": [
                    {
                        "id": "bad2",
                        "claim": "错误的哈希",
                        "source_type": "file",
                        "source_ref": "pricing.py",
                        "source_sha256": "0" * 64,
                    }
                ]
            },
        )
    assert exc2.value.code == "source_hash_mismatch"

    # 2. 绑定真实文件哈希与已提交工具调用结果哈希
    file_sha = digest((ws / "pricing.py").read_text(encoding="utf-8"))
    storage.schedule_call(run_id, "c1", "repo_list", {})
    finished_call = storage.finish_call(run_id, "c1", {"ok": True, "files": ["pricing.py"]})

    wm = update_working_memory(
        ws,
        storage,
        run_id,
        None,
        {
            "goal": "修复百分比折扣计算",
            "constraints": ["不得修改 tests/test_pricing.py"],
            "todos": [{"id": "t1", "task": "将百分比除以 100", "status": "in_progress"}],
            "facts": [
                {
                    "id": "f_file",
                    "claim": "pricing.py 当前直接用 1 - discount_percent",
                    "source_type": "file",
                    "source_ref": "pricing.py",
                    "source_sha256": file_sha,
                },
                {
                    "id": "f_call",
                    "claim": "仓库包含 pricing.py",
                    "source_type": "tool_call",
                    "source_ref": "c1",
                    "source_sha256": finished_call["result_sha256"],
                },
            ],
        },
    )
    audited_before = audit_working_memory(ws, storage, run_id, wm)
    assert len(audited_before["valid_facts"]) == 2
    assert audited_before["stale_count"] == 0

    # 3. 底层源码被修改后，绑定 pricing.py 的事实自动降级为 stale，而绑定工具调用的事实仍为 valid
    (ws / "pricing.py").write_text(
        (ws / "pricing.py").read_text(encoding="utf-8").replace(
            "1 - discount_percent", "1 - discount_percent / 100"
        ),
        encoding="utf-8",
    )
    audited_after = audit_working_memory(ws, storage, run_id, wm)
    assert audited_after["stale_count"] == 1
    assert audited_after["stale_facts"][0]["id"] == "f_file"
    assert audited_after["stale_facts"][0]["stale_reason"] == "file_hash_changed"
    assert [f["id"] for f in audited_after["valid_facts"]] == ["f_call"]


async def test_working_memory_survives_context_compaction_without_overriding_approvals(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    storage = Storage(tmp_path / "state.db")
    captured = []

    class InspectingProvider:
        async def complete(self, system, messages, tools, max_tokens=2048):
            captured.append(messages)
            return {"content": [{"type": "text", "text": "完成"}], "stop_reason": "end_turn", "usage": {}}

    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=InspectingProvider(),
        context_chars=2800,
    )
    run = runtime.create("长对话裁剪与工作记忆保留测试")
    registry = ToolRegistry(storage, ws, runtime.knowledge, run["id"])
    write_call = {
        "type": "tool_use",
        "id": "wm_1",
        "name": "working_memory_write",
        "input": {
            "goal": "PERSISTED_GOAL_ACROSS_PRUNING",
            "facts": [
                {
                    "id": "f1",
                    "claim": "PERSISTED_FACT_CLAIM",
                    "source_type": "file",
                    "source_ref": "pricing.py",
                }
            ],
        },
    }
    res = runtime._execute_call(run, registry, write_call)
    assert res["ok"] is True and res["valid_facts_count"] == 1

    # 塞入超长早期历史触发裁剪，同时写入真实审批状态
    for idx in range(6):
        cid = f"old_{idx}"
        run["messages"].extend(
            [
                {"role": "assistant", "content": [{"type": "tool_use", "id": cid, "name": "repo_list", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": "x" * 1200}]},
            ]
        )
    run["metadata"]["decisions"] = [{"proposal_id": "REAL_APPROVAL_99", "decision": "rejected"}]
    # 修改源码让事实在注入上下文时实时呈现为 stale
    (ws / "pricing.py").write_text("# mutated\n", encoding="utf-8")

    await runtime._complete(run, run["messages"], [], "system")
    assert runtime.context.stats()["dropped_messages"] >= 8
    serialized = json.dumps(captured[0], ensure_ascii=False)
    assert "PERSISTED_GOAL_ACROSS_PRUNING" in serialized
    assert "file_hash_changed" in serialized
    assert "REAL_APPROVAL_99" in serialized

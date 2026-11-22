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
    prompt_view_working_memory,
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


def test_working_memory_rejects_failed_tool_calls_and_secret_files(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    storage = Storage(tmp_path / "state.db")
    run_id = storage.create_run("reject failed calls")["id"]

    # 未执行完（scheduled）、失败（failed）、待审批（waiting_approval）以及 ok=False 的调用全部拒绝
    storage.schedule_call(run_id, "c_scheduled", "repo_read", {"path": "pricing.py"})
    storage.schedule_call(run_id, "c_failed", "repo_read", {"path": "missing.py"})
    storage.finish_call(run_id, "c_failed", {"ok": False, "code": "not_found", "error": "missing"})
    storage.schedule_call(run_id, "c_waiting", "propose_patch", {"path": "pricing.py"})
    storage.finish_call(
        run_id,
        "c_waiting",
        {"ok": True, "requires_approval": True, "proposal_id": "p1"},
    )
    storage.schedule_call(run_id, "c_forced_bad", "repo_read", {"path": "pricing.py"})
    storage.finish_call(
        run_id,
        "c_forced_bad",
        {"ok": False, "error": "forced"},
        status="succeeded",
    )

    for bad_call_id in ("c_scheduled", "c_failed", "c_waiting", "c_forced_bad"):
        with pytest.raises(WorkingMemoryError) as exc:
            update_working_memory(
                ws,
                storage,
                run_id,
                None,
                {
                    "facts": [
                        {
                            "id": f"f_{bad_call_id}",
                            "claim": "试图把未成功工具调用伪装成已验证事实",
                            "source_type": "tool_call",
                            "source_ref": bad_call_id,
                        }
                    ]
                },
            )
        assert exc.value.code == "unverified_source"
        assert "不能绑定失败或未成功的工具调用" in str(exc.value)

    # 敏感凭证文件名一律拒绝作为工作记忆来源
    for secret_name in (
        "credentials.py",
        "secrets.py",
        "secret.py",
        "private_key.py",
        "tokens.py",
        ".env",
        ".env.production",
    ):
        (ws / secret_name).write_text("API_KEY = 'sensitive'\n", encoding="utf-8")
        with pytest.raises(WorkingMemoryError) as sec_exc:
            update_working_memory(
                ws,
                storage,
                run_id,
                None,
                {
                    "facts": [
                        {
                            "id": "f_sec",
                            "claim": "不应读取或绑定敏感文件",
                            "source_type": "file",
                            "source_ref": secret_name,
                        }
                    ]
                },
            )
        assert sec_exc.value.code == "unverified_source"


def test_tool_call_facts_track_bound_files_and_turn_stale_on_disk_change(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    storage = Storage(tmp_path / "state.db")
    run_id = storage.create_run("bound files staleness")["id"]
    registry = ToolRegistry(storage, ws, None, run_id)

    read_res = registry.execute("repo_read", {"path": "pricing.py"}, call_id="c_read")
    storage.schedule_call(run_id, "c_read", "repo_read", {"path": "pricing.py"})
    storage.finish_call(run_id, "c_read", read_res)

    sym_res = registry.execute("symbol_search", {"symbol": "discount"}, call_id="c_sym")
    storage.schedule_call(run_id, "c_sym", "symbol_search", {"symbol": "discount"})
    storage.finish_call(run_id, "c_sym", sym_res)

    search_res = registry.execute("repo_search", {"query": "discount"}, call_id="c_search")
    storage.schedule_call(run_id, "c_search", "repo_search", {"query": "discount"})
    storage.finish_call(run_id, "c_search", search_res)

    list_res = registry.execute("repo_list", {}, call_id="c_list")
    storage.schedule_call(run_id, "c_list", "repo_list", {})
    storage.finish_call(run_id, "c_list", list_res)

    wm = update_working_memory(
        ws,
        storage,
        run_id,
        None,
        {
            "facts": [
                {
                    "id": "f_read",
                    "claim": "repo_read 确认 pricing.py 实现",
                    "source_type": "tool_call",
                    "source_ref": "c_read",
                },
                {
                    "id": "f_sym",
                    "claim": "symbol_search 定位 discount 相关符号行号",
                    "source_type": "tool_call",
                    "source_ref": "c_sym",
                },
                {
                    "id": "f_search",
                    "claim": "repo_search 检索到 discount 代码行",
                    "source_type": "tool_call",
                    "source_ref": "c_search",
                },
                {
                    "id": "f_list",
                    "claim": "repo_list 显示目录包含 pricing.py",
                    "source_type": "tool_call",
                    "source_ref": "c_list",
                },
            ]
        },
    )

    by_id = {f["id"]: f for f in wm["facts"]}
    expected_sha = digest((ws / "pricing.py").read_text(encoding="utf-8"))
    assert by_id["f_read"]["bound_files"]["pricing.py"] == expected_sha
    assert by_id["f_sym"]["bound_files"]["pricing.py"] == expected_sha
    assert by_id["f_search"]["bound_files"]["pricing.py"] == expected_sha
    assert "bound_files" not in by_id["f_list"]

    audited_initial = audit_working_memory(ws, storage, run_id, wm)
    assert audited_initial["stale_count"] == 0
    view = prompt_view_working_memory(audited_initial)
    for vf in view["valid_facts"]:
        assert "recorded_at" not in vf
        assert "bound_files" not in vf

    # 修改磁盘上的 pricing.py 后，所有读取/检索该文件内容的 tool_call 事实均降级为 bound_file_hash_changed
    (ws / "pricing.py").write_text("# changed implementation\n", encoding="utf-8")
    audited_mutated = audit_working_memory(ws, storage, run_id, wm)
    stale_by_id = {f["id"]: f["stale_reason"] for f in audited_mutated["stale_facts"]}
    assert stale_by_id == {
        "f_read": "bound_file_hash_changed",
        "f_sym": "bound_file_hash_changed",
        "f_search": "bound_file_hash_changed",
    }
    assert [f["id"] for f in audited_mutated["valid_facts"]] == ["f_list"]


def test_working_memory_deletion_and_stale_eviction_prevent_cap_deadlocks(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    (ws / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    storage = Storage(tmp_path / "state.db")
    run_id = storage.create_run("cap management")["id"]

    # 1. 10 个待办上限：支持 remove_todo_ids、status=="deleted" 与 prune_done_todos 解锁容量
    wm = update_working_memory(
        ws,
        storage,
        run_id,
        None,
        {
            "todos": [
                {
                    "id": f"t{i}",
                    "task": f"步骤 {i}",
                    "status": "done" if i <= 2 else "pending",
                }
                for i in range(1, 11)
            ]
        },
    )
    assert len(wm["todos"]) == 10
    with pytest.raises(WorkingMemoryError) as todo_cap_exc:
        update_working_memory(
            ws,
            storage,
            run_id,
            wm,
            {"todos": [{"id": "t11", "task": "溢出待办", "status": "pending"}]},
        )
    assert todo_cap_exc.value.code == "working_memory_budget_exceeded"

    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "remove_todo_ids": ["t1"],
            "todos": [{"id": "t11", "task": "通过 remove_todo_ids 腾出槽位", "status": "pending"}],
        },
    )
    assert {t["id"] for t in wm["todos"]} == {f"t{i}" for i in range(2, 12)}

    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "todos": [
                {"id": "t3", "status": "deleted"},
                {"id": "t12", "task": "通过 status=deleted 替换旧项", "status": "in_progress"},
            ]
        },
    )
    assert "t3" not in {t["id"] for t in wm["todos"]}
    assert "t12" in {t["id"] for t in wm["todos"]}

    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "prune_done_todos": True,
            "todos": [{"id": "t13", "task": "清理已完成待办后新增", "status": "pending"}],
        },
    )
    assert "t2" not in {t["id"] for t in wm["todos"]}
    assert len(wm["todos"]) == 10

    # 2. 12 条事实上限：13 条有效事实必须报错；存在未触碰的 stale 事实时自动按最旧顺序淘汰
    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "facts": [
                {
                    "id": "f1",
                    "claim": "旧 helper 事实 1",
                    "source_type": "file",
                    "source_ref": "helper.py",
                }
            ]
        },
    )
    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "facts": [
                {
                    "id": "f2",
                    "claim": "旧 helper 事实 2",
                    "source_type": "file",
                    "source_ref": "helper.py",
                },
                *[
                    {
                        "id": f"f{i}",
                        "claim": f"pricing 事实 {i}",
                        "source_type": "file",
                        "source_ref": "pricing.py",
                    }
                    for i in range(3, 13)
                ],
            ]
        },
    )
    assert len(wm["facts"]) == 12

    # 显式一次写入 13 条全部有效的事实时依然严格拒绝
    with pytest.raises(WorkingMemoryError) as fact_cap_exc:
        update_working_memory(
            ws,
            storage,
            run_id,
            None,
            {
                "facts": [
                    {
                        "id": f"valid_{i}",
                        "claim": f"有效事实 {i}",
                        "source_type": "file",
                        "source_ref": "pricing.py",
                    }
                    for i in range(13)
                ]
            },
        )
    assert fact_cap_exc.value.code == "working_memory_budget_exceeded"

    # 支持显式 remove_fact_ids 删除旧事实并写入新事实
    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "remove_fact_ids": ["f12"],
            "facts": [
                {
                    "id": "f13",
                    "claim": "显式移除 f12 后新增 f13",
                    "source_type": "file",
                    "source_ref": "pricing.py",
                }
            ],
        },
    )
    assert len(wm["facts"]) == 12
    assert "f12" not in {f["id"] for f in wm["facts"]}

    # 修改 helper.py 使 f1、f2 变为 stale；新增 f14 时自动优先淘汰最旧的 stale 事实 f1
    (ws / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(WorkingMemoryError):
        update_working_memory(
            ws,
            storage,
            run_id,
            wm,
            {
                "auto_evict_stale": False,
                "facts": [
                    {
                        "id": "f14",
                        "claim": "禁用自动淘汰时超限报错",
                        "source_type": "file",
                        "source_ref": "pricing.py",
                    }
                ],
            },
        )

    wm = update_working_memory(
        ws,
        storage,
        run_id,
        wm,
        {
            "facts": [
                {
                    "id": "f14",
                    "claim": "自动淘汰最旧过期事实 f1 后成功写入",
                    "source_type": "file",
                    "source_ref": "pricing.py",
                }
            ]
        },
    )
    fact_ids = [f["id"] for f in wm["facts"]]
    assert len(fact_ids) == 12
    assert "f1" not in fact_ids
    assert "f2" in fact_ids
    assert "f14" in fact_ids

    # prune_stale_facts=True 可主动清空剩余过期事实
    wm = update_working_memory(ws, storage, run_id, wm, {"prune_stale_facts": True})
    assert "f2" not in {f["id"] for f in wm["facts"]}
    assert len(wm["facts"]) == 11

    # 3. 即使条目数 <= 12，超出字符预算时同样优先自动淘汰未触碰的 stale 事实
    wm_chars = update_working_memory(
        ws,
        storage,
        run_id,
        None,
        {
            "facts": [
                {
                    "id": f"long_stale_{i}",
                    "claim": f"旧长事实-{i}-" + ("甲" * 220),
                    "source_type": "file",
                    "source_ref": "helper.py",
                }
                for i in range(3)
            ]
            + [
                {
                    "id": f"long_valid_{i}",
                    "claim": f"有效长事实-{i}-" + ("乙" * 220),
                    "source_type": "file",
                    "source_ref": "pricing.py",
                }
                for i in range(4)
            ]
        },
    )
    (ws / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
    wm_chars = update_working_memory(
        ws,
        storage,
        run_id,
        wm_chars,
        {
            "facts": [
                {
                    "id": "long_new_1",
                    "claim": "新增大事实-" + ("丙" * 230),
                    "source_type": "file",
                    "source_ref": "pricing.py",
                },
                {
                    "id": "long_new_2",
                    "claim": "新增大事实2-" + ("丁" * 230),
                    "source_type": "file",
                    "source_ref": "pricing.py",
                },
            ]
        },
    )
    remaining_ids = [f["id"] for f in wm_chars["facts"]]
    assert "long_stale_0" not in remaining_ids
    assert "long_new_1" in remaining_ids and "long_new_2" in remaining_ids


def test_runtime_invoke_status_deleted_and_complete_bound_files_tracking(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    storage = Storage(tmp_path / "state.db")
    runtime = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills", provider="demo")
    run = runtime.create("测试 working_memory_write 删除 schema 与全量文件绑定")
    run_id = run["id"]
    registry = ToolRegistry(storage, ws, runtime.knowledge, run_id)

    # 1. AgentRuntime._invoke 通过 JSON Schema 校验支持 status="deleted"（无需传 task / claim / source_*）
    seed_res = runtime._invoke(
        registry,
        "working_memory_write",
        {
            "todos": [
                {"id": "t1", "task": "待删除待办", "status": "pending"},
                {"id": "t2", "task": "保留待办", "status": "in_progress"},
            ],
            "facts": [
                {
                    "id": "f1",
                    "claim": "待删除事实",
                    "source_type": "file",
                    "source_ref": "pricing.py",
                },
                {
                    "id": "f2",
                    "claim": "保留事实",
                    "source_type": "file",
                    "source_ref": "pricing.py",
                },
            ],
        },
        run,
        "wm_seed",
    )
    assert seed_res["ok"] is True
    assert seed_res["todos_count"] == 2
    assert seed_res["valid_facts_count"] == 2

    del_res = runtime._invoke(
        registry,
        "working_memory_write",
        {
            "facts": [{"id": "f1", "status": "deleted"}],
            "todos": [{"id": "t1", "status": "deleted"}],
        },
        run,
        "wm_delete",
    )
    assert del_res["ok"] is True, del_res
    assert del_res["todos_count"] == 1
    assert del_res["valid_facts_count"] == 1
    assert [t["id"] for t in run["metadata"]["working_memory"]["todos"]] == ["t2"]
    assert [f["id"] for f in run["metadata"]["working_memory"]["facts"]] == ["f2"]

    # 2. 基于 stem 拦截跨扩展名敏感文件（secrets.json / credentials.yaml / tokens.toml / api_key.json）
    for sec_file in ("secrets.json", "credentials.yaml", "tokens.toml", "api_key.json", "apikey.toml"):
        (ws / sec_file).write_text('{"token": "secret"}\n', encoding="utf-8")
        with pytest.raises(WorkingMemoryError) as sec_exc:
            update_working_memory(
                ws,
                storage,
                run_id,
                None,
                {
                    "facts": [
                        {
                            "id": "f_sec_stem",
                            "claim": "不应绑定非 .py 扩展名的凭证文件",
                            "source_type": "file",
                            "source_ref": sec_file,
                        }
                    ]
                },
            )
        assert sec_exc.value.code == "unverified_source"

    # 3. repo_search：文件 A 前 12 行匹配、文件 B 在第 13 条匹配 -> 修改文件 B 必须将事实标为 stale
    (ws / "aa_many_matches.py").write_text(
        "".join(f"LINE_{i} = 'UNIQUE_SEARCH_NEEDLE'\n" for i in range(12)),
        encoding="utf-8",
    )
    (ws / "zz_thirteenth_match.py").write_text(
        "FINAL_HIT = 'UNIQUE_SEARCH_NEEDLE'\n",
        encoding="utf-8",
    )
    search_res = registry.execute("repo_search", {"query": "UNIQUE_SEARCH_NEEDLE"}, call_id="c_search_13")
    assert len(search_res["matches"]) == 13
    assert search_res["matches"][12]["path"] == "zz_thirteenth_match.py"
    storage.schedule_call(run_id, "c_search_13", "repo_search", {"query": "UNIQUE_SEARCH_NEEDLE"})
    storage.finish_call(run_id, "c_search_13", search_res)

    wm_search = update_working_memory(
        ws,
        storage,
        run_id,
        None,
        {
            "facts": [
                {
                    "id": "f_search_13",
                    "claim": "UNIQUE_SEARCH_NEEDLE 分布在 aa_many_matches.py 与 zz_thirteenth_match.py",
                    "source_type": "tool_call",
                    "source_ref": "c_search_13",
                }
            ]
        },
    )
    assert "aa_many_matches.py" in wm_search["facts"][0]["bound_files"]
    assert "zz_thirteenth_match.py" in wm_search["facts"][0]["bound_files"]
    assert audit_working_memory(ws, storage, run_id, wm_search)["stale_count"] == 0

    (ws / "zz_thirteenth_match.py").write_text("FINAL_HIT = 'CHANGED'\n", encoding="utf-8")
    audited_search = audit_working_memory(ws, storage, run_id, wm_search)
    assert audited_search["stale_count"] == 1
    assert audited_search["stale_facts"][0]["stale_reason"] == "bound_file_hash_changed"

    # 4. symbol_search：仅出现在 references 中的调用方文件被修改后，事实同样必须标为 stale
    (ws / "sym_provider.py").write_text(
        "def compute_cross_module_metric(val: int) -> int:\n    return val * 2\n",
        encoding="utf-8",
    )
    (ws / "sym_consumer.py").write_text(
        "from sym_provider import compute_cross_module_metric\n\n"
        "def run_consumer() -> int:\n    return compute_cross_module_metric(21)\n",
        encoding="utf-8",
    )
    sym_res = registry.execute(
        "symbol_search", {"symbol": "compute_cross_module_metric"}, call_id="c_sym_refs"
    )
    def_paths = {d["path"] for d in sym_res["definitions"]}
    ref_paths = {r["path"] for r in sym_res["references"]}
    assert "sym_provider.py" in def_paths
    assert "sym_consumer.py" in ref_paths and "sym_consumer.py" not in def_paths
    storage.schedule_call(
        run_id, "c_sym_refs", "symbol_search", {"symbol": "compute_cross_module_metric"}
    )
    storage.finish_call(run_id, "c_sym_refs", sym_res)

    wm_sym = update_working_memory(
        ws,
        storage,
        run_id,
        None,
        {
            "facts": [
                {
                    "id": "f_sym_refs",
                    "claim": "compute_cross_module_metric 仅在 sym_consumer.py 中被调用",
                    "source_type": "tool_call",
                    "source_ref": "c_sym_refs",
                }
            ]
        },
    )
    assert "sym_provider.py" in wm_sym["facts"][0]["bound_files"]
    assert "sym_consumer.py" in wm_sym["facts"][0]["bound_files"]
    assert audit_working_memory(ws, storage, run_id, wm_sym)["stale_count"] == 0

    (ws / "sym_consumer.py").write_text(
        "def run_consumer() -> int:\n    return 0\n",
        encoding="utf-8",
    )
    audited_sym = audit_working_memory(ws, storage, run_id, wm_sym)
    assert audited_sym["stale_count"] == 1
    assert audited_sym["stale_facts"][0]["stale_reason"] == "bound_file_hash_changed"


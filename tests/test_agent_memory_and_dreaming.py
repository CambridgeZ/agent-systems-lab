"""全生命周期 Agent 记忆系统与四阶段离线 Dreaming 测试（对应 Anthropic《The components of agent memory》）。"""

from __future__ import annotations

import json
import shutil
import time
import uuid

from fastapi.testclient import TestClient

from agent_lab.api import ROOT, create_app
from agent_lab.memory import (
    MAX_INDEX_CHARS,
    MAX_INDEX_LINES,
    DreamEngine,
    MemoryStore,
)
from agent_lab.repl import run_interactive_repl
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _call(name: str, **kwargs):
    return {"type": "tool_use", "id": "mem_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict]):
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 12},
    }


async def test_auto_post_session_dreaming_link_repair_and_preemptive_memory_injection(tmp_path):
    """验证会话内写笔记 + 悬空 [[链接]] -> 会话结束自动触发 Dreaming 补全断链 -> 下一新会话预加载 MEMORY.md！"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ws = tmp_path / "checkout_proj"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    storage = Storage(state_dir / "state.db")

    class SessionOneProvider:
        def __init__(self):
            self.step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.step += 1
            if self.step == 1:
                return _resp(
                    [
                        _call(
                            "memory_write",
                            layer="project",
                            path="rules/discount-cap.md",
                            title="优惠券封顶校验规则",
                            content="结算计算减免额时必须遵守 `max_discount_cents` 上限，详见 [[coupon-rounding]]。",
                            status="confirmed",
                            sources=["file:discounts.py"],
                            index_summary="优惠券百分比折扣必须执行 max_discount_cents 封顶",
                        ),
                        _call(
                            "memory_tag",
                            label="coupon-rounding",
                            note="分（cents）整数取整需防止浮点误差导致应付金额为负",
                            kind="trap",
                        ),
                    ]
                )
            return _resp([{"type": "text", "text": "已完成首轮结算规则梳理与显著性标记。"}])

    rt1 = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=SessionOneProvider(),
        execution_profile="host",
    )
    run1 = rt1.create("梳理结算与优惠封顶规则")
    done1 = await rt1.execute(run1["id"])
    await rt1.aclose()
    assert done1["status"] == "completed"

    # 验证会话结束默认自动触发了 Dreaming，且把 [[coupon-rounding]] 关联修复到了 traps/coupon-rounding！
    store = rt1._memory_store(done1)
    dreams = storage.list_dream_runs("local", project_slug=store.project_slug)
    assert len(dreams) >= 1
    assert dreams[0]["trigger"] == "auto_post_session"

    graph = store.build_link_graph("project")
    assert graph["stats"]["broken_links"] == 0
    assert graph["stats"]["edges"] >= 1

    rule_page = store.read_page("project", "rules/discount-cap.md")
    assert "traps/coupon-rounding" in rule_page["links"]
    trap_page = store.read_page("project", "traps/coupon-rounding.md")
    assert "rules/discount-cap" in trap_page["backlinks"]

    # 会话 2（全新会话）：验证新会话启动时自动把有界 MEMORY.md 预加载到 pinned 上下文，并支持混合检索历史 Transcript + Wiki
    seen_session2_messages: list[list[dict]] = []

    class SessionTwoProvider:
        def __init__(self):
            self.step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.step += 1
            seen_session2_messages.append(messages)
            if self.step == 1:
                return _resp([_call("memory_search", query="max_discount_cents 封顶 浮点误差", scope="all")])
            serialized = json.dumps(messages, ensure_ascii=False)
            assert "MEMORY_HIERARCHY" in serialized
            assert "rules/discount-cap" in serialized
            return _resp([{"type": "text", "text": "新会话直接复用了跨会话 MEMORY.md 与混合检索证据。"}])

    rt2 = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=SessionTwoProvider(),
        execution_profile="host",
    )
    run2 = rt2.create("在新会话里检查结算封顶注意事项")
    done2 = await rt2.execute(run2["id"])
    await rt2.aclose()
    assert done2["status"] == "completed"
    # 验证第 1 步和第 2 步的 MEMORY_HIERARCHY 预加载块保持 100% 字节稳定，不破坏 KV Cache 前缀
    assert seen_session2_messages[0][1] == seen_session2_messages[1][1]
    assert seen_session2_messages[0][1]["content"].startswith("MEMORY_HIERARCHY")


async def test_dreaming_corrects_wrong_notes_against_transcripts_and_suppresses_contradicted(tmp_path):
    """复现文章 Mt. Moon 纠错场景：对照真实 Transcript 推翻失败调用绑定的错误断言，并用 mistake 标记自动重写错误笔记。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ws = tmp_path / "game_ws"
    ws.mkdir()
    (ws / "map_router.py").write_text("ENTRANCE = 'west'\n", encoding="utf-8")
    storage = Storage(state_dir / "state.db")

    store = MemoryStore(state_dir, storage, project_slug="cwd-mt-moon", workspace=ws)

    # 1. 构造一条绑定了失败工具调用的错误断言笔记 -> Dreaming 必须将其标为 contradicted 并从检索与 MEMORY.md 中剔除
    r_bad = storage.create_run("错误猜测会话")
    storage.schedule_call(r_bad["id"], "bad_call_1", "bash_exec", {"command": "false"})
    storage.finish_call(r_bad["id"], "bad_call_1", {"ok": False, "code": "nonzero_exit"})
    store.write_page(
        layer="project",
        path="wiki/bad-guess.md",
        title="未验证的错误猜测",
        content="WRONG_HALLUCINATED_CLAIM_XYZ",
        status="hypothesis",
        sources=[f"call:{r_bad['id'][:8]}:bad_call_1"],
        index_summary="错误的猜测索引",
    )

    # 2. 构造一条方向写反的笔记（PokéCenter 误记在 east），随后在新会话中打上 mistake 重放纠错标记
    store.write_page(
        layer="project",
        path="traps/mt-moon-landmark.md",
        title="Mt Moon Landmark",
        content="PokéCenter 位于山洞东侧（east side），走出即为通关。",
        status="confirmed",
        index_summary="Mt Moon 地标方位",
    )
    r_fix = storage.create_run("重走 Mt Moon 核实真实方位")
    r_fix["status"] = "completed"
    store.tag_salient(
        r_fix,
        label="mt-moon-landmark",
        note="实测纠错：PokéCenter 位于山洞西侧入口（west side），东侧是单向跳台陷阱，绝不能误判已出洞！",
        kind="mistake",
    )
    storage.save_run(r_fix)

    # 3. 运行 Dreaming
    engine = DreamEngine(store)
    report = await engine.dream(trigger="manual")
    assert report["corrections_count"] >= 2

    # 验证绑定失败调用的猜测已降级为 contradicted，且不再出现在 memory_search 或 MEMORY.md 顶层索引中！
    bad_page = store.read_page("project", "wiki/bad-guess.md")
    assert bad_page["status"] == "contradicted"
    search_bad = store.search("WRONG_HALLUCINATED_CLAIM_XYZ", scope="wiki")
    assert search_bad["matches"] == []
    assert "bad-guess" not in store.read_index_text("project")

    # 验证 Mt. Moon 笔记已被对照 Transcript 纠错重写为西侧入口（west side）！
    fixed_landmark = store.read_page("project", "traps/mt-moon-landmark.md")
    assert fixed_landmark["status"] == "confirmed"
    assert "西侧入口（west side）" in fixed_landmark["content"]


async def test_llm_assisted_goal_focused_dreaming_and_200_line_index_cap(tmp_path):
    """验证支持 Dreaming 的 LLM 目标聚焦合成、独立页面防覆盖，以及 MEMORY.md 严格 <= 200 行预算。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ws = tmp_path / "contract_ws"
    ws.mkdir()
    storage = Storage(state_dir / "state.db")
    store = MemoryStore(state_dir, storage, project_slug="cwd-contracts", workspace=ws)

    # 写入大量规则验证顶层 MEMORY.md 严格受限于 200 行与 4000 字符上限
    for idx in range(55):
        store.write_page(
            layer="project",
            path=f"rules/rule-{idx:02d}.md",
            title=f"条款校验规则 {idx}",
            content=f"条款 {idx} 原文核对细则与验证命令说明。",
            status="confirmed",
            index_summary=f"核对第 {idx} 号合同交割条件与赔偿上限条款",
        )

    r1 = storage.create_run("审查并购合同 A 与合同 B 的赔偿上限条款")
    r1["status"] = "completed"
    r1["answer"] = "合同 A 赔偿上限为交易额 15%，合同 B 包含知识产权特别豁免。"
    storage.save_run(r1)

    class MockDreamLLM:
        supports_dreaming = True

        async def complete(self, system, messages, tools, max_tokens=1800):
            assert "合同关键条款逐页精确引用" in messages[0]["content"]
            payload = {
                "pages": [
                    {
                        "category": "wiki",
                        "slug": "contract-a-indemnity",
                        "title": "合同 A 赔偿条款原文",
                        "status": "confirmed",
                        "summary": "合同 A 赔偿上限 15%，关联 [[contract-b-ip]]",
                        "content": "## 合同 A\n上限 15%，参见关联页 [[contract-b-ip]] 与缺失页 [[escrow-schedule]]。",
                        "run_id": r1["id"],
                    },
                    {
                        "category": "wiki",
                        "slug": "contract-b-ip",
                        "title": "合同 B 知识产权豁免",
                        "status": "confirmed",
                        "summary": "合同 B IP 条款豁免上限",
                        "content": "## 合同 B\n知识产权侵权索赔不受一般赔偿上限约束。",
                        "run_id": r1["id"],
                    },
                ]
            }
            return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    engine = DreamEngine(store, llm_provider=MockDreamLLM())
    rep = await engine.dream(focus_goal="合同关键条款逐页精确引用", trigger="manual")
    assert rep["llm_assisted"] is True
    assert rep["pages_written_count"] >= 2
    assert rep["links_repaired_count"] >= 1

    # 验证悬空链接 [[escrow-schedule]] 被自动补建，[[contract-b-ip]] 被规范化为 [[wiki/contract-b-ip]]
    page_a = store.read_page("project", "wiki/contract-a-indemnity.md")
    assert "wiki/contract-b-ip" in page_a["links"]
    assert store.read_page("project", "wiki/escrow-schedule.md")["ok"] is True

    # 验证预加载索引严守 200 行与 4000 字符硬上限
    pinned_text = store.render_pinned_index()
    assert len(pinned_text.splitlines()) <= MAX_INDEX_LINES + 2
    assert len(pinned_text) <= MAX_INDEX_CHARS + 120


async def test_repl_dream_commands_and_web_memory_console_multitenant_isolation(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    storage = Storage(state_dir / "state.db")

    # 1. 验证 REPL 的 /dream 与 /memory 斜杠命令
    rt = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider="demo",
        execution_profile="host",
    )
    inputs = iter(["/dream 梳理版本发布检查项", "/memory", "/exit"])
    outputs: list[str] = []
    await run_interactive_repl(
        rt,
        input_fn=lambda _p: next(inputs),
        output_fn=outputs.append,
    )
    await rt.aclose()
    joined = "\n".join(outputs)
    assert "[Dreaming 整理完成]" in joined
    assert "记忆库概览" in joined

    # 2. 验证 Web API 新路由与多租户隔离（同时保持原有 GET/POST /api/memory 100% 兼容）
    tokens_cfg = {
        "tok-a": {"tenant_id": "tenant_a", "actor_id": "alice", "roles": ["operator", "approver", "admin"]},
        "tok-b": {"tenant_id": "tenant_b", "actor_id": "bob", "roles": ["operator", "approver", "admin"]},
    }
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens_cfg))
    app = create_app(state_dir=state_dir, provider="demo")

    with TestClient(app) as client:
        # tenant_a 写入 Wiki 页
        put_res = client.put(
            "/api/memory/page",
            headers={"Authorization": "Bearer tok-a"},
            json={
                "layer": "global",
                "path": "rules/deploy-check.md",
                "title": "部署前必跑检查",
                "content": "部署前必须执行 `ruff check` 与全量 `pytest`，参见 [[rollback-plan]]。",
                "status": "confirmed",
                "index_summary": "部署前静态检查与测试门控",
            },
        )
        assert put_res.status_code == 200, put_res.text

        # tenant_b 无法看到或搜到 tenant_a 的 Wiki 笔记
        ov_b = client.get("/api/memory/overview", headers={"Authorization": "Bearer tok-b"}).json()
        assert ov_b["global"]["pages"] == []
        search_b = client.get(
            "/api/memory/search?q=deploy-check", headers={"Authorization": "Bearer tok-b"}
        ).json()
        assert search_b["matches"] == []

        # tenant_a 运行 /api/memory/dream 自动补建悬空链接 [[rollback-plan]]
        dream_a = client.post(
            "/api/memory/dream",
            headers={"Authorization": "Bearer tok-a"},
            json={"focus_goal": "发布稳定性"},
        )
        assert dream_a.status_code == 200
        assert dream_a.json()["links_repaired_count"] >= 1

        ov_a = client.get("/api/memory/overview", headers={"Authorization": "Bearer tok-a"}).json()
        assert len(ov_a["global"]["pages"]) == 2
        assert ov_a["graph"]["stats"]["broken_links"] == 0

        # 验证任务在 Web 端完成后后台自动异步触发 Dreaming（不阻塞 4 任务并发槽位）
        run_resp = client.post(
            "/api/runs",
            headers={"Authorization": "Bearer tok-a"},
            json={"prompt": "未知测试问题快速完成触发后台 Dreaming"},
        )
        assert run_resp.status_code == 202
        rid = run_resp.json()["id"]
        for _ in range(30):
            r_det = client.get(f"/api/runs/{rid}", headers={"Authorization": "Bearer tok-a"}).json()
            if r_det["status"] == "completed":
                break
            time.sleep(0.05)
        time.sleep(0.15)
        ov_after = client.get(
            f"/api/memory/overview?run_id={rid}",
            headers={"Authorization": "Bearer tok-a"},
        ).json()
        assert any(d["trigger"] == "auto_post_session" for d in ov_after["recent_dreams"])


async def test_memory_store_tenant_collision_dot_traversal_symlink_and_inline_code_guards(tmp_path):
    """验证租户名防折叠碰撞、'.'/'..' 防逃逸、中间子目录软链接拦截、跨项目隔离与反引号内链接忽略。"""
    import pytest
    from agent_lab.memory import MemoryError

    state_dir = tmp_path / "state"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "outside_doc.md").write_text("OUTSIDE_DATA\n", encoding="utf-8")
    storage = Storage(state_dir / "state.db")

    # 1. acme:prod 与 acme_prod 以及 '..' / '.' 绝不共享目录或逃逸出 agent_memory
    s_colon = MemoryStore(state_dir, storage, tenant_id="acme:prod", project_slug="proj-1")
    s_under = MemoryStore(state_dir, storage, tenant_id="acme_prod", project_slug="proj-1")
    s_dots = MemoryStore(state_dir, storage, tenant_id="..", project_slug="..")
    assert s_colon.tenant_root != s_under.tenant_root
    assert s_dots.tenant_root.resolve().is_relative_to((state_dir / "agent_memory").resolve())
    assert s_dots.project_dir.resolve().is_relative_to((s_dots.tenant_root / "projects").resolve())

    s_colon.write_page(
        layer="global",
        path="rules/only-colon.md",
        title="Only Colon",
        content="行内代码 `[[not-a-real-link]]` 不应被解析为悬空链接。",
        sources=["note:line1\n---\ninjected: bad"],
    )
    assert s_under.list_pages("global") == []
    read_colon = s_colon.read_page("global", "rules/only-colon.md")
    assert read_colon["links"] == []
    assert all("\n" not in src for src in read_colon["sources"])

    # 2. 中间分类目录若被替换为指向外部的符号链接，读写删均必须抛出 unsafe_memory_path
    s_colon.project_dir.mkdir(parents=True, exist_ok=True)
    (s_colon.project_dir / "wiki").symlink_to(outside_dir)
    with pytest.raises(MemoryError) as exc_w:
        s_colon.write_page(layer="project", path="wiki/escape.md", content="bad")
    assert exc_w.value.code == "unsafe_memory_path"
    with pytest.raises(MemoryError) as exc_r:
        s_colon.read_page("project", "wiki/outside_doc.md")
    assert exc_r.value.code == "unsafe_memory_path"


"""多轮连续对话（send_message）、CLI 交互式工作台（agent-lab code REPL）与 Web API 本地操控测试。"""

from __future__ import annotations

import json
import time
import uuid

from fastapi.testclient import TestClient

from agent_lab.api import ROOT, create_app
from agent_lab.computer import make_synthetic_png
from agent_lab.repl import run_interactive_repl
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _call(name: str, **kwargs):
    return {"type": "tool_use", "id": "mt_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict]):
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 15, "output_tokens": 15},
    }


async def test_multiturn_send_message_and_interactive_cli_repl(tmp_path):
    ws = tmp_path / "my_app"
    ws.mkdir()
    (ws / "hello.py").write_text("GREETING = 'hi'\n", encoding="utf-8")
    storage = Storage(tmp_path / "state.db")

    class MultiTurnProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.calls += 1
            user_texts = [
                m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)
            ]
            # Turn 1: 在 ask 模式下执行命令 -> 触发内联审批，用户输入 'f' 切为 full-auto 并批准
            if self.calls == 1:
                return _resp([_call("bash_exec", command="python3 -c \"import hello; print(hello.GREETING)\"")])
            if self.calls == 2:
                return _resp([{"type": "text", "text": "第一轮输出确认：GREETING=hi"}])
            # Turn 2: 已处于 full-auto 模式，直接修改文件并跑脚本验证，无需任何审批停顿
            if self.calls == 3:
                assert any("把问候语改成 world" in t for t in user_texts)
                return _resp(
                    [
                        _call(
                            "fs_edit",
                            path="hello.py",
                            old_text="GREETING = 'hi'",
                            new_text="GREETING = 'world'",
                        )
                    ]
                )
            if self.calls == 4:
                return _resp([_call("bash_exec", command="python3 -c \"import hello; print(hello.GREETING)\"")])
            serialized = json.dumps(messages, ensure_ascii=False)
            assert "world" in serialized
            return _resp([{"type": "text", "text": "第二轮已修改并验证：GREETING=world"}])

    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=MultiTurnProvider(),
        execution_profile="host",
        permission_mode="ask",
        max_steps=3,
    )

    scripted_inputs = iter(
        [
            "f",  # 在第一轮审批提示时输入 f：切为 full-auto 并批准执行
            "/status",
            "把问候语改成 world 并运行确认",
            "/mode ask",
            "/exit",
        ]
    )
    printed: list[str] = []

    final_run = await run_interactive_repl(
        runtime,
        initial_prompt="运行脚本看看 hello.py 输出什么",
        extra_metadata={"direct_workspace": True, "template": "local_cwd"},
        input_fn=lambda _prompt: next(scripted_inputs),
        output_fn=printed.append,
    )
    await runtime.aclose()

    assert final_run is not None
    assert final_run["status"] == "completed"
    assert "GREETING = 'world'" in (ws / "hello.py").read_text(encoding="utf-8")
    joined_out = "\n".join(printed)
    assert "[权限已切换为 full-auto]" in joined_out
    assert "第二轮已修改并验证：GREETING=world" in joined_out
    assert runtime.permission_mode == "ask"


def test_web_api_local_cwd_rbac_followup_artifacts_and_gc_safety(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    project_dir = tmp_path / "real_user_project"
    project_dir.mkdir()
    important_file = project_dir / "keep_me.py"
    important_file.write_text("IMPORTANT = 42\n", encoding="utf-8")

    tokens_cfg = {
        "tok-local-admin": {"tenant_id": "local", "actor_id": "owner", "roles": ["operator", "approver", "admin"]},
        "tok-tenant-b": {"tenant_id": "tenant_b", "actor_id": "outsider", "roles": ["operator", "approver", "admin"]},
    }
    monkeypatch.setenv("AGENT_LAB_TOKENS_JSON", json.dumps(tokens_cfg))
    app = create_app(state_dir=state_dir, provider="demo")

    with TestClient(app) as client:
        # 1. /api/config 暴露 host_capabilities
        cfg = client.get("/api/config", headers={"Authorization": "Bearer tok-local-admin"}).json()
        assert cfg["host_capabilities"]["direct_workspace_enabled"] is True

        # 2. 非本机/非全局管理员租户禁止挂载服务器本地目录（防止跨租户主机逃逸）
        forbidden = client.post(
            "/api/runs",
            headers={"Authorization": "Bearer tok-tenant-b"},
            json={
                "prompt": "查看文件",
                "template": "local_cwd",
                "workspace_path": str(project_dir),
            },
        )
        assert forbidden.status_code == 403

        # 3. 本机管理员成功创建 local_cwd 任务（准备 pricing.py 供内置 DemoProvider 跑通并停在审批）
        (project_dir / "pricing.py").write_text(
            (ROOT / "data/sample_repo/pricing.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created = client.post(
            "/api/runs",
            headers={"Authorization": "Bearer tok-local-admin"},
            json={
                "prompt": "修复 pricing.py 的百分比折扣 bug",
                "template": "local_cwd",
                "workspace_path": str(project_dir),
                "permission_mode": "auto-edit",
                "enable_gui": True,
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]

        # 轮询直到进入 waiting_approval
        detail = None
        for _ in range(30):
            detail = client.get(
                f"/api/runs/{run_id}", headers={"Authorization": "Bearer tok-local-admin"}
            ).json()
            if detail["status"] in {"waiting_approval", "completed", "failed"}:
                break
            time.sleep(0.05)
        assert detail is not None and detail["status"] == "waiting_approval"
        assert detail["direct_workspace"] is True
        assert detail["execution_profile"] == "computer"
        assert detail["permission_mode"] == "auto-edit"

        # 4. 动态切换权限模式 API
        patched_perm = client.patch(
            f"/api/runs/{run_id}/permissions",
            headers={"Authorization": "Bearer tok-local-admin"},
            json={"permission_mode": "full-auto"},
        )
        assert patched_perm.status_code == 200
        assert patched_perm.json()["permission_mode"] == "full-auto"

        # 5. 批准提案等待任务完成
        prop_id = detail["proposals"][0]["id"]
        app_res = client.post(
            f"/api/runs/{run_id}/proposals/{prop_id}",
            headers={"Authorization": "Bearer tok-local-admin"},
            json={"approved": True},
        )
        assert app_res.status_code == 202
        for _ in range(30):
            detail = client.get(
                f"/api/runs/{run_id}", headers={"Authorization": "Bearer tok-local-admin"}
            ).json()
            if detail["status"] == "completed":
                break
            time.sleep(0.05)
        assert detail["status"] == "completed"

        # 6. 写入一张模拟截图产物，验证同租户可读取、跨租户 404、路径穿越 400
        art_dir = state_dir / "artifacts" / run_id
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "screen-step1.png").write_bytes(make_synthetic_png(64, 48))

        art_ok = client.get(
            f"/api/runs/{run_id}/artifacts/screen-step1.png",
            headers={"Authorization": "Bearer tok-local-admin"},
        )
        assert art_ok.status_code == 200
        assert art_ok.headers["content-type"].startswith("image/png")

        art_cross_tenant = client.get(
            f"/api/runs/{run_id}/artifacts/screen-step1.png",
            headers={"Authorization": "Bearer tok-tenant-b"},
        )
        assert art_cross_tenant.status_code == 404

        art_bad_name = client.get(
            f"/api/runs/{run_id}/artifacts/..%2Fstate.db",
            headers={"Authorization": "Bearer tok-local-admin"},
        )
        assert art_bad_name.status_code in {400, 404}

        # 7. 多轮追问 API：在已完成任务上发送下一条指令
        follow = client.post(
            f"/api/runs/{run_id}/messages",
            headers={"Authorization": "Bearer tok-local-admin"},
            json={"prompt": "继续检查修改后的 pricing.py"},
        )
        assert follow.status_code == 202
        for _ in range(30):
            detail = client.get(
                f"/api/runs/{run_id}", headers={"Authorization": "Bearer tok-local-admin"}
            ).json()
            if detail["status"] in {"completed", "waiting_approval"}:
                break
            time.sleep(0.05)
        assert len(detail["conversation_turns"]) == 1

        # 若停在审批则拒绝以进入终态，测试 GC 绝不误删用户真实目录！
        if detail["status"] == "waiting_approval":
            pending_p = [p for p in detail["proposals"] if p["status"] == "pending"][0]
            client.post(
                f"/api/runs/{run_id}/proposals/{pending_p['id']}",
                headers={"Authorization": "Bearer tok-local-admin"},
                json={"approved": False},
            )
            for _ in range(30):
                detail = client.get(
                    f"/api/runs/{run_id}", headers={"Authorization": "Bearer tok-local-admin"}
                ).json()
                if detail["status"] == "completed":
                    break
                time.sleep(0.05)

        gc_res = client.post(
            "/api/admin/gc?retention_seconds=0",
            headers={"Authorization": "Bearer tok-local-admin"},
        )
        assert gc_res.status_code == 200
        # 产物目录已清理，但用户真实本地目录与文件 100% 完好无损！
        assert not art_dir.exists()
        assert project_dir.is_dir()
        assert important_file.read_text(encoding="utf-8") == "IMPORTANT = 42\n"


async def test_send_message_repairs_dangling_tool_use_after_mid_batch_abort(tmp_path):
    ws = tmp_path / "abort_ws"
    ws.mkdir()
    storage = Storage(tmp_path / "state.db")

    class FollowupRecoveryProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            serialized = json.dumps(messages, ensure_ascii=False)
            assert "aborted_before_followup" in serialized
            return _resp([{"type": "text", "text": "中断后续聊恢复正常。"}])

    rt = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=FollowupRecoveryProvider(),
        execution_profile="host",
    )
    run = rt.create("首轮任务")
    # 模拟在 assistant 发出 tool_use 后尚未写入 user tool_result 即因取消/异常进入终态
    run["status"] = "cancelled"
    run["messages"].append(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "dangling_1", "name": "fs_glob", "input": {"pattern": "*"}}],
        }
    )
    storage.save_run(run)

    recovered = await rt.send_message(run["id"], "请忽略上一轮中断并直接回复")
    await rt.aclose()
    assert recovered["status"] == "completed"
    assert "恢复正常" in recovered["answer"]


def test_slow_command_approval_transitions_running_immediately_and_debounces_double_click(
    tmp_path, monkeypatch
):
    """回归验证：批准耗时命令后，任务状态立即从 waiting_approval 切为 running（不再残留待审批），且并发重复点击幂等返回 202。"""
    state_dir = tmp_path / "state"
    ws_dir = tmp_path / "slow_cmd_ws"
    ws_dir.mkdir()

    class SlowCmdProvider:
        def __init__(self):
            self.step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.step += 1
            has_approved = any(
                isinstance(m.get("content"), str) and m["content"].startswith("REVIEW_APPROVED:")
                for m in messages
            )
            if not has_approved:
                return _resp(
                    [_call("bash_exec", command="python3 -c 'import time; time.sleep(0.35); print(\"SLOW_OK\")'")]
                )
            return _resp([{"type": "text", "text": "### 结论\n\n- **状态**: `SLOW_OK` 已执行完毕。"}])

    monkeypatch.setattr("agent_lab.runtime.get_provider", lambda _name: SlowCmdProvider())
    app = create_app(state_dir=state_dir, provider="demo")

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={
                "prompt": "运行耗时检查命令",
                "template": "local_cwd",
                "workspace_path": str(ws_dir),
                "permission_mode": "auto-edit",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        detail = None
        for _ in range(30):
            detail = client.get(f"/api/runs/{run_id}").json()
            if detail["status"] == "waiting_approval":
                break
            time.sleep(0.03)
        assert detail is not None and detail["status"] == "waiting_approval"
        pid = detail["proposals"][0]["id"]

        # 批准瞬间返回 202，紧接着即使命令仍在 sleep(0.35)，重复点击幂等返回 202（不报 409），且轮询可见 running/job_active
        r1 = client.post(f"/api/runs/{run_id}/proposals/{pid}", json={"approved": True})
        assert r1.status_code == 202
        r2 = client.post(f"/api/runs/{run_id}/proposals/{pid}", json={"approved": True})
        assert r2.status_code == 202
        mid = client.get(f"/api/runs/{run_id}").json()
        assert mid["status"] in {"running", "completed"} or mid["job_active"] is True
        assert mid["proposals"][0]["status"] == "approved"

        for _ in range(40):
            done = client.get(f"/api/runs/{run_id}").json()
            if done["status"] == "completed":
                break
            time.sleep(0.05)
        assert done["status"] == "completed"
        assert "SLOW_OK" in done["answer"]


async def test_multiple_concurrent_local_agents_and_operator_subagents_run_safely(tmp_path, monkeypatch):
    """验证多个本地操控 Agent 同时并发运行（含跨进程 GUI 席位锁、独立 Git Worktree 隔离与执行型 operator 子 Agent）。"""
    import asyncio
    from agent_lab.computer import MockComputerBackend
    from agent_lab.git_repo import init_git_repo_from_dir

    state_dir = tmp_path / "state"
    origin_dir = tmp_path / "origin_repo"
    init_git_repo_from_dir(ROOT / "data/sample_repo", origin_dir)
    storage = Storage(state_dir / "state.db")

    # 1. 验证单个 Agent 内通过 subagent_run(mode="operator") 并发派生两个本地写文件 + 跑命令的子 Agent
    class OperatorSubagentProvider:
        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            role = system.split("\nROLE:", 1)[1].splitlines()[0] if "\nROLE:" in system else "coordinator"
            has_tool_res = any(
                isinstance(m.get("content"), list)
                and any(b.get("type") == "tool_result" for b in m["content"])
                for m in messages
            )
            if role == "coordinator":
                if not has_tool_res:
                    return _resp(
                        [
                            _call(
                                "subagent_run",
                                tasks=[
                                    {"role": "worker-a", "prompt": "写入 mod_a.py 并运行", "mode": "operator"},
                                    {"role": "worker-b", "prompt": "写入 mod_b.py 并运行", "mode": "operator"},
                                ],
                            )
                        ]
                    )
                return _resp([{"type": "text", "text": "两个执行型子 Agent 均已完成本地写入与脚本运行。"}])
            target_file = "mod_a.py" if "worker-a" in role else "mod_b.py"
            if not has_tool_res:
                return _resp(
                    [
                        _call("fs_write", path=target_file, content=f"NAME = '{target_file}'\n"),
                        _call("bash_exec", command=f"python3 -c \"print('{target_file}_OK')\""),
                    ]
                )
            return _resp([{"type": "text", "text": f"{target_file} done"}])

    shared_ws = tmp_path / "shared_local_ws"
    shared_ws.mkdir()
    rt_op = AgentRuntime(
        storage,
        shared_ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=OperatorSubagentProvider(),
        execution_profile="computer",
        permission_mode="full-auto",
        computer_backend=MockComputerBackend(),
    )
    run_op = rt_op.create("并发执行型子 Agent 测试")
    done_op = await rt_op.execute(run_op["id"])
    await rt_op.aclose()
    assert done_op["status"] == "completed"
    assert (shared_ws / "mod_a.py").is_file() and (shared_ws / "mod_b.py").is_file()

    # 2. 验证 3 个独立顶层本地 Agent 会话同时并发操作同一电脑（共享 GUI 席位锁 + 各自写文件/跑命令互不干扰）
    gui_backend = MockComputerBackend()

    class ConcurrentDesktopProvider:
        def __init__(self, idx: int):
            self.idx = idx
            self.step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.step += 1
            if self.step == 1:
                return _resp(
                    [
                        _call("fs_write", path=f"agent_{self.idx}.txt", content=f"hello from {self.idx}\n"),
                        _call("computer_mouse", action="click", x=100 + self.idx * 20, y=200),
                        _call("bash_exec", command=f"python3 -c 'print(\"AGENT_{self.idx}_DONE\")'"),
                    ]
                )
            return _resp([{"type": "text", "text": f"Agent {self.idx} 完成"}])

    runtimes = [
        AgentRuntime(
            storage,
            shared_ws,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=ConcurrentDesktopProvider(i),
            execution_profile="computer",
            permission_mode="full-auto",
            computer_backend=gui_backend,
        )
        for i in range(3)
    ]
    created_runs = [rt.create(f"并行本地任务 {i}") for i, rt in enumerate(runtimes)]
    results = await asyncio.gather(*(rt.execute(r["id"]) for rt, r in zip(runtimes, created_runs)))
    for rt in runtimes:
        await rt.aclose()

    assert all(r["status"] == "completed" for r in results)
    for i in range(3):
        assert (shared_ws / f"agent_{i}.txt").read_text(encoding="utf-8") == f"hello from {i}\n"
    assert len([h for h in gui_backend.history if h["type"] == "mouse"]) == 3

    # 3. 验证 Web API 同时启动多个 isolate_worktree=True 的本地 Git 仓库 Agent 互不污染源码
    monkeypatch.setattr(
        "agent_lab.runtime.get_provider",
        lambda _name: ConcurrentDesktopProvider(9),
    )
    app = create_app(state_dir=state_dir, provider="demo")
    with TestClient(app) as client:
        wt_r1 = client.post(
            "/api/runs",
            json={
                "prompt": "Worktree Agent 1",
                "template": "local_cwd",
                "workspace_path": str(origin_dir),
                "permission_mode": "full-auto",
                "isolate_worktree": True,
            },
        )
        wt_r2 = client.post(
            "/api/runs",
            json={
                "prompt": "Worktree Agent 2",
                "template": "local_cwd",
                "workspace_path": str(origin_dir),
                "permission_mode": "full-auto",
                "isolate_worktree": True,
            },
        )
        assert wt_r1.status_code == 202 and wt_r2.status_code == 202
        assert wt_r1.json()["id"] != wt_r2.json()["id"]
        # 原始仓库目录未被直接塞入 agent_9.txt，两个并发 Agent 均在各自独立 worktree 中运行
        assert not (origin_dir / "agent_9.txt").exists()



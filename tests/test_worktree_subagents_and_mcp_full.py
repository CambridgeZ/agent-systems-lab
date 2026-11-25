"""离线集成测试：Worktree 隔离子 Agent、模型档位路由、后台任务、MCP 全协议与故障隔离、AX 树与多语言 Git 提交。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from agent_lab.computer import MockComputerBackend, execute_computer_tool
from agent_lab.git_repo import (
    GitRepoError,
    commit_verified_worktree,
    create_task_worktree,
    init_git_repo_from_dir,
    worktree_status,
)
from agent_lab.mcp_client import (
    McpClientManager,
    interpolate_env_vars,
    parse_mcp_servers_config,
)
from agent_lab.providers import resolve_model_for_tier
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


def _call(name: str, call_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": call_id or ("wt_" + uuid.uuid4().hex[:12]),
        "name": name,
        "input": kwargs,
    }


def _resp(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 16, "output_tokens": 12},
    }


def _extract_role(system: str) -> str:
    if "\nROLE:" not in system:
        return "coordinator"
    return system.split("\nROLE:", 1)[1].splitlines()[0]


def _extract_mcp_text_json(result: Any) -> dict[str, Any]:
    """兼容 FastMCP.call_tool 返回 list[TextContent] 或 (list[TextContent], structured) 的版本差异。"""
    blocks = result[0] if isinstance(result, tuple) else result
    texts = [getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text"]
    assert texts, result
    return json.loads(texts[0])


# ---------------------------------------------------------------------------
# 1. subagent_run 升级：Worktree 并发隔离、model_tier="fast"、后台执行+状态查询、单子故障隔离
# ---------------------------------------------------------------------------


async def test_subagent_operator_worktree_isolation_parallel_edits_without_clobbering_parent(tmp_path: Path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "shared.py").write_text("VALUE = 'ORIGINAL'\n", encoding="utf-8")
    (seed_dir / "other.py").write_text("OTHER = 0\n", encoding="utf-8")

    repo = tmp_path / "repo"
    init_git_repo_from_dir(seed_dir, repo)
    kb = tmp_path / "kb"
    sk = tmp_path / "sk"
    kb.mkdir()
    sk.mkdir()

    class WorktreeOperatorProvider:
        def __init__(self) -> None:
            self.lead_step = 0
            self.sub_steps: dict[str, int] = {}

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            del tools, max_tokens, kwargs
            role = _extract_role(system)
            if role == "coordinator":
                self.lead_step += 1
                if self.lead_step == 1:
                    return _resp(
                        [
                            _call(
                                "subagent_run",
                                "wt_batch_1",
                                tasks=[
                                    {
                                        "role": "editor-a",
                                        "mode": "operator",
                                        "isolation": "worktree",
                                        "prompt": "在独立 worktree 修改 shared.py 并新建 file_a.py",
                                    },
                                    {
                                        "role": "editor-b",
                                        "mode": "operator",
                                        "isolation": "worktree",
                                        "prompt": "在独立 worktree 修改 shared.py 与 other.py",
                                    },
                                ],
                            )
                        ]
                    )
                return _resp([{"type": "text", "text": "两个 worktree 子 Agent 并发修改已隔离完成。"}])

            step = self.sub_steps[role] = self.sub_steps.get(role, 0) + 1
            if "editor-a" in role:
                if step == 1:
                    return _resp(
                        [
                            _call("fs_write", path="shared.py", content="VALUE = 'FROM_EDITOR_A'\n"),
                            _call("fs_write", path="file_a.py", content="CREATED_BY_A = True\n"),
                        ]
                    )
                return _resp([{"type": "text", "text": "editor-a 修改完成。"}])

            if step == 1:
                return _resp(
                    [
                        _call("fs_write", path="shared.py", content="VALUE = 'FROM_EDITOR_B'\n"),
                        _call("fs_write", path="other.py", content="OTHER = 2\n"),
                    ]
                )
            return _resp([{"type": "text", "text": "editor-b 修改完成。"}])

    storage = Storage(tmp_path / "state.db")
    rt = AgentRuntime(
        storage,
        repo,
        kb,
        sk,
        provider=WorktreeOperatorProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    try:
        done = await rt.start("并发在独立 worktree 中执行两个 operator 修改任务")
    finally:
        await rt.aclose()

    assert done["status"] == "completed"
    # 父工作区绝不被子 Agent 修改污染
    assert (repo / "shared.py").read_text(encoding="utf-8") == "VALUE = 'ORIGINAL'\n"
    assert (repo / "other.py").read_text(encoding="utf-8") == "OTHER = 0\n"
    assert not (repo / "file_a.py").exists()

    batch_res = storage.get_call(done["id"], "wt_batch_1")["result"]
    assert batch_res["ok"] is True
    by_role = {rep["role"]: rep for rep in batch_res["reports"]}
    assert set(by_role.keys()) == {"editor-a", "editor-b"}

    rep_a = by_role["editor-a"]
    rep_b = by_role["editor-b"]
    for rep in (rep_a, rep_b):
        assert rep["ok"] is True
        assert rep["mode"] == "operator"
        assert rep["isolation"] == "worktree"
        assert isinstance(rep["branch"], str) and rep["branch"].startswith("agent-sub-")
        assert isinstance(rep["diff_summary"], dict)
        assert rep["diff_summary"]["dirty"] is True
        assert rep["diff_summary"]["changed_files"]
        assert rep["diff_summary"]["diff"]

    assert rep_a["branch"] != rep_b["branch"]
    assert rep_a["worktree_path"] != rep_b["worktree_path"]
    assert "FROM_EDITOR_A" in rep_a["diff_summary"]["diff"]
    assert "FROM_EDITOR_B" in rep_b["diff_summary"]["diff"]
    assert (Path(rep_a["worktree_path"]) / "shared.py").read_text(encoding="utf-8") == "VALUE = 'FROM_EDITOR_A'\n"
    assert (Path(rep_b["worktree_path"]) / "shared.py").read_text(encoding="utf-8") == "VALUE = 'FROM_EDITOR_B'\n"


async def test_subagent_model_tier_fast_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir()
    (ws / "app.py").write_text("X = 1\n", encoding="utf-8")

    # 校验 resolve_model_for_tier 在 provider 属性与环境变量下的行为
    stub_prov = SimpleNamespace(model="base-model-v1", fast_model="attr-fast-v1", reasoning_model="attr-reason-v1")
    monkeypatch.delenv("AGENT_LAB_FAST_MODEL", raising=False)
    monkeypatch.delenv("AGENT_LAB_REASONING_MODEL", raising=False)
    assert resolve_model_for_tier(stub_prov, "fast") == "attr-fast-v1"
    assert resolve_model_for_tier(stub_prov, "reasoning") == "attr-reason-v1"
    assert resolve_model_for_tier(stub_prov, "inherit") == "base-model-v1"

    monkeypatch.setenv("AGENT_LAB_FAST_MODEL", "configured-fast-haiku-test")
    assert resolve_model_for_tier(stub_prov, "fast") == "configured-fast-haiku-test"

    seen_models: list[tuple[str, str | None]] = []

    class TierInspectProvider:
        model = "primary-sonnet-test"

        def __init__(self) -> None:
            self.lead_step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, *, model=None, **kwargs):
            del messages, tools, max_tokens, kwargs
            role = _extract_role(system)
            seen_models.append((role, model))
            if role == "coordinator":
                self.lead_step += 1
                if self.lead_step == 1:
                    return _resp(
                        [
                            _call(
                                "subagent_run",
                                "tier_batch_1",
                                tasks=[
                                    {"role": "fast-scout", "model_tier": "fast", "prompt": "快速扫描仓库"},
                                    {"role": "base-scout", "model_tier": "inherit", "prompt": "常规扫描仓库"},
                                ],
                            )
                        ]
                    )
                return _resp([{"type": "text", "text": "档位路由测试完成。"}])
            return _resp([{"type": "text", "text": f"{role} 完成"}])

    storage = Storage(tmp_path / "state.db")
    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=TierInspectProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    try:
        done = await rt.start("派发 fast 与 inherit 子 Agent")
    finally:
        await rt.aclose()

    assert done["status"] == "completed"
    fast_calls = [m for role, m in seen_models if "fast-scout" in role]
    base_calls = [m for role, m in seen_models if "base-scout" in role]
    assert fast_calls == ["configured-fast-haiku-test"]
    assert base_calls == [None]

    res = storage.get_call(done["id"], "tier_batch_1")["result"]
    by_role = {rep["role"]: rep for rep in res["reports"]}
    assert by_role["fast-scout"]["model_tier"] == "fast"
    assert by_role["fast-scout"]["model"] == "configured-fast-haiku-test"
    assert by_role["base-scout"]["model_tier"] == "inherit"
    assert by_role["base-scout"]["model"] == "primary-sonnet-test"


async def test_subagent_run_in_background_and_subagent_status(tmp_path: Path):
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir()
    (ws / "mod.py").write_text("FLAG = True\n", encoding="utf-8")

    release_bg = asyncio.Event()
    bg_started = asyncio.Event()
    bg_finished = asyncio.Event()
    captured: dict[str, Any] = {}

    class BackgroundSubagentProvider:
        def __init__(self) -> None:
            self.lead_step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            del tools, max_tokens, kwargs
            role = _extract_role(system)
            if role == "coordinator":
                self.lead_step += 1
                if self.lead_step == 1:
                    return _resp(
                        [
                            _call(
                                "subagent_run",
                                "bg_launch_1",
                                run_in_background=True,
                                tasks=[{"role": "bg-analyst", "prompt": "后台分析 mod.py"}],
                            )
                        ]
                    )
                # 解析上一步返回结果
                tool_results = [
                    json.loads(b["content"])
                    for m in messages
                    if isinstance(m.get("content"), list)
                    for b in m["content"]
                    if b.get("type") == "tool_result"
                ]
                if self.lead_step == 2:
                    launch_res = tool_results[-1]
                    captured["launch"] = launch_res
                    job_id = launch_res["job_id"]
                    captured["job_id"] = job_id
                    # 等待后台子 Agent 进入执行态后再查询状态 -> 必为 running
                    await asyncio.wait_for(bg_started.wait(), timeout=3.0)
                    return _resp([_call("subagent_status", "bg_poll_running", job_id=job_id)])
                if self.lead_step == 3:
                    captured["running_status"] = tool_results[-1]
                    # 放行后台子 Agent 并等待其结束，再查一次 subagent_status -> completed
                    release_bg.set()
                    await asyncio.wait_for(bg_finished.wait(), timeout=3.0)
                    await asyncio.sleep(0.02)
                    return _resp([_call("subagent_status", "bg_poll_done", job_id=captured["job_id"])])
                captured["done_status"] = tool_results[-1]
                return _resp([{"type": "text", "text": "后台子 Agent 结果已取回。"}])

            bg_started.set()
            await release_bg.wait()
            bg_finished.set()
            return _resp([{"type": "text", "text": "BG_ANALYST_RESULT_READY"}])

    storage = Storage(tmp_path / "state.db")
    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=BackgroundSubagentProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    try:
        done = await rt.start("后台启动子 Agent 并通过 subagent_status 轮询结果")
        # 顺带验证无 job_id 列表视图与未知 job_id 错误码
        listed = rt.subagent_status(run_id=done["id"])
        missing = rt.subagent_status("subjob_nonexistent", run_id=done["id"])
    finally:
        await rt.aclose()

    assert done["status"] == "completed"
    assert captured["launch"]["background"] is True
    assert captured["launch"]["status"] == "running"
    assert captured["running_status"]["ok"] is True
    assert captured["running_status"]["status"] == "running"
    assert captured["done_status"]["ok"] is True
    assert captured["done_status"]["status"] == "completed"
    assert captured["done_status"]["reports"][0]["answer"] == "BG_ANALYST_RESULT_READY"
    assert listed["ok"] is True and listed["count"] >= 1
    assert missing["ok"] is False and missing["code"] == "job_not_found"


async def test_subagent_fault_isolation_raising_and_timeout_do_not_abort_healthy_sibling(tmp_path: Path):
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir()
    (ws / "service.py").write_text("OK = True\n", encoding="utf-8")

    class FaultySiblingProvider:
        def __init__(self) -> None:
            self.lead_step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            del messages, tools, max_tokens, kwargs
            role = _extract_role(system)
            if role == "coordinator":
                self.lead_step += 1
                if self.lead_step == 1:
                    return _resp(
                        [
                            _call(
                                "subagent_run",
                                "fault_iso_batch",
                                tasks=[
                                    {"role": "healthy-peer", "prompt": "健康兄弟正常返回"},
                                    {"role": "raising-peer", "prompt": "异常兄弟抛出错误"},
                                    {"role": "slow-peer", "prompt": "超时兄弟挂起", "timeout_seconds": 1},
                                ],
                            )
                        ]
                    )
                return _resp([{"type": "text", "text": "已接收部分成功的子 Agent 报告。"}])
            if "raising-peer" in role:
                raise RuntimeError("simulated subagent failure")
            if "slow-peer" in role:
                await asyncio.sleep(5.0)
                return _resp([{"type": "text", "text": "should never arrive"}])
            return _resp([{"type": "text", "text": "HEALTHY_SIBLING_SURVIVED"}])

    storage = Storage(tmp_path / "state.db")
    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=FaultySiblingProvider(),
        execution_profile="host",
        permission_mode="auto-edit",
        auto_dream=False,
    )
    try:
        done = await rt.start("派发包含故障与超时兄弟的并发子 Agent 批次")
    finally:
        await rt.aclose()

    assert done["status"] == "completed"
    batch_res = storage.get_call(done["id"], "fault_iso_batch")["result"]
    assert batch_res["ok"] is True
    assert batch_res["partial_failure"] is True
    assert batch_res["failed_count"] == 2
    by_role = {rep["role"]: rep for rep in batch_res["reports"]}
    assert by_role["healthy-peer"]["ok"] is True
    assert by_role["healthy-peer"]["answer"] == "HEALTHY_SIBLING_SURVIVED"
    assert by_role["raising-peer"]["ok"] is False
    assert by_role["raising-peer"]["code"] == "subagent_error"
    assert by_role["slow-peer"]["ok"] is False
    assert by_role["slow-peer"]["code"] == "subagent_timeout"


# ---------------------------------------------------------------------------
# 2. McpClientManager 与 mcp_server.py：故障隔离、${ENV_VAR} 插值、Resources/Prompts、扩展工具
# ---------------------------------------------------------------------------


async def test_mcp_client_per_server_fault_isolation():
    pytest.importorskip("mcp")
    mgr = McpClientManager(
        {
            "broken_srv": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", "import sys; sys.exit(1)"],
                "connect_timeout": 3.0,
            },
            "healthy_srv": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [
                    "-m",
                    "agent_lab.mcp_server",
                    "--workspace",
                    str(ROOT / "data/sample_repo"),
                    "--knowledge",
                    str(ROOT / "data/knowledge"),
                    "--extended",
                ],
                "cwd": str(ROOT),
                "trust_readonly_annotations": True,
                "connect_timeout": 10.0,
            },
        }
    )
    try:
        specs = await mgr.connect_all()
        summaries = mgr.server_summaries()
        assert summaries["broken_srv"]["connected"] is False
        assert summaries["broken_srv"]["error"]
        assert summaries["healthy_srv"]["connected"] is True
        assert summaries["healthy_srv"]["error"] is None
        assert summaries["healthy_srv"]["tool_count"] == 6

        spec_names = {s["name"] for s in specs}
        assert "mcp__healthy_srv__repo_list" in spec_names
        assert "mcp__healthy_srv__repo_map" in spec_names
        assert "mcp__healthy_srv__symbol_search" in spec_names

        listed = await mgr.call_tool("mcp__healthy_srv__repo_list", {})
        assert listed["ok"] is True and "pricing.py" in listed["files"]
        mapped = await mgr.call_tool("mcp__healthy_srv__repo_map", {"max_chars": 1500})
        assert mapped["ok"] is True and "discounted_total" in mapped["map"]
    finally:
        await mgr.aclose()


def test_mcp_config_env_var_interpolation_and_strict_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", "tok-abc-123")
    monkeypatch.setenv("TEST_MCP_ENV_VAL", "env-val-99")
    monkeypatch.delenv("TEST_MCP_MISSING_OPT", raising=False)
    monkeypatch.delenv("TEST_MCP_MISSING_STRICT", raising=False)
    monkeypatch.setenv("TEST_MCP_CRLF", "good-val\r\nX-Injected: evil")

    cfg_path = tmp_path / "mcp.json"
    cfg_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote_api": {
                        "url": "https://mcp.example.test/sse",
                        "headers": {
                            "Authorization": "Bearer ${TEST_MCP_TOKEN}",
                            "X-Region": "${TEST_MCP_MISSING_OPT:-us-east-1}",
                            "X-Optional": "${TEST_MCP_MISSING_OPT}",
                            "X-Clean": "${TEST_MCP_CRLF}",
                        },
                    },
                    "strict_srv": {
                        "url": "https://strict.example.test/mcp",
                        "strict_env": True,
                        "headers": {
                            "Authorization": "Bearer ${TEST_MCP_MISSING_STRICT}",
                        },
                    },
                    "local_stdio": {
                        "command": "echo",
                        "args": ["ok"],
                        "env": {
                            "SERVICE_KEY": "${TEST_MCP_ENV_VAL}",
                            "FALLBACK_MODE": "${TEST_MCP_MISSING_OPT:-offline}",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_mcp_servers_config(cfg_path)
    remote = parsed["remote_api"]
    assert remote["headers"]["Authorization"] == "Bearer tok-abc-123"
    assert remote["headers"]["X-Region"] == "us-east-1"
    assert "X-Optional" not in remote["headers"]
    assert "\r" not in remote["headers"]["X-Clean"] and "\n" not in remote["headers"]["X-Clean"]
    assert remote["missing_env_vars"] == ["TEST_MCP_MISSING_OPT"]
    assert "config_error" not in remote

    strict = parsed["strict_srv"]
    assert strict["missing_env_vars"] == ["TEST_MCP_MISSING_STRICT"]
    assert "TEST_MCP_MISSING_STRICT" in strict["config_error"]

    stdio = parsed["local_stdio"]
    assert stdio["env"]["SERVICE_KEY"] == "env-val-99"
    assert stdio["env"]["FALLBACK_MODE"] == "offline"

    # 直接校验 interpolate_env_vars 工具函数
    resolved, missing = interpolate_env_vars(
        {"A": "${FOO:-def}", "B": "${BAR}"}, environ={}, sanitize_http_headers=True
    )
    assert resolved == {"A": "def"}
    assert missing == ["BAR"]


async def test_mcp_resources_and_prompts_helpers_and_runtime_tools(tmp_path: Path):
    class FakeMcpSession:
        async def list_resources(self):
            return SimpleNamespace(
                resources=[
                    SimpleNamespace(
                        uri="docs://architecture/spec",
                        name="Architecture Spec",
                        description="Specs using Bearer sk-secret-desc-1",
                        mimeType="text/markdown",
                    )
                ]
            )

        async def read_resource(self, uri: str):
            return SimpleNamespace(
                contents=[
                    SimpleNamespace(
                        uri=uri,
                        mimeType="text/markdown",
                        text="Service guide with Bearer sk-secret-body-2 and clean text.",
                    )
                ]
            )

        async def list_prompts(self):
            return SimpleNamespace(
                prompts=[
                    SimpleNamespace(
                        name="triage_bug",
                        description="Triage a bug report",
                        arguments=[
                            SimpleNamespace(name="module", description="Target module", required=True)
                        ],
                    )
                ]
            )

        async def get_prompt(self, name: str, arguments=None):
            return SimpleNamespace(
                description=f"Prompt {name}",
                messages=[
                    SimpleNamespace(
                        role="user",
                        content=SimpleNamespace(
                            text=f"Inspect {(arguments or {}).get('module', 'all')} with Bearer sk-secret-prompt-3"
                        ),
                    )
                ],
            )

    class StubResourceMcpManager(McpClientManager):
        def __init__(self) -> None:
            super().__init__({"docs": {"transport": "stdio", "command": "stub"}})
            self._sessions["docs"] = FakeMcpSession()
            self._connected = True

        async def connect_all(self) -> list[dict[str, Any]]:
            return []

    mgr = StubResourceMcpManager()

    res_list = await mgr.list_resources()
    assert res_list["ok"] is True and res_list["count"] == 1
    assert res_list["resources"][0]["uri"] == "docs://architecture/spec"
    assert "sk-secret-desc-1" not in res_list["resources"][0]["description"]
    assert "[REDACTED]" in res_list["resources"][0]["description"]

    res_read = await mgr.read_resource("docs", "docs://architecture/spec")
    assert res_read["ok"] is True
    assert "sk-secret-body-2" not in res_read["contents"][0]["text"]
    assert "[REDACTED]" in res_read["contents"][0]["text"]

    disconn = await mgr.read_resource("missing_srv", "docs://x")
    assert disconn["ok"] is False and disconn["code"] == "mcp_server_disconnected"

    prompts_list = await mgr.list_prompts()
    assert prompts_list["ok"] is True and prompts_list["count"] == 1
    assert prompts_list["prompts"][0]["slash_command"] == "/mcp__docs__triage_bug"
    assert prompts_list["prompts"][0]["arguments"][0]["required"] is True

    prompt_val = await mgr.get_prompt("docs", "triage_bug", {"module": "billing"})
    assert prompt_val["ok"] is True
    assert "billing" in prompt_val["messages"][0]["content"]
    assert "sk-secret-prompt-3" not in prompt_val["messages"][0]["content"]
    assert "[REDACTED]" in prompt_val["messages"][0]["content"]

    # 端到端通过 AgentRuntime 暴露并调用 mcp_list_resources 与 mcp_read_resource
    ws, kb, sk = tmp_path / "ws", tmp_path / "kb", tmp_path / "sk"
    for d in (ws, kb, sk):
        d.mkdir()
    (ws / "main.py").write_text("X = 1\n", encoding="utf-8")

    class ResourceCallerProvider:
        def __init__(self) -> None:
            self.step = 0
            self.seen_tool_names: set[str] = set()

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            del system, messages, max_tokens, kwargs
            self.seen_tool_names = {t["name"] for t in tools}
            self.step += 1
            if self.step == 1:
                return _resp([_call("mcp_list_resources", "res_l1")])
            if self.step == 2:
                return _resp(
                    [
                        _call(
                            "mcp_read_resource",
                            "res_r1",
                            server="docs",
                            uri="docs://architecture/spec",
                        )
                    ]
                )
            return _resp([{"type": "text", "text": "MCP 资源列表与内容读取完成。"}])

    storage = Storage(tmp_path / "state.db")
    prov = ResourceCallerProvider()
    rt = AgentRuntime(
        storage,
        ws,
        kb,
        sk,
        provider=prov,
        execution_profile="host",
        permission_mode="auto-edit",
        mcp_manager=mgr,
        auto_dream=False,
    )
    try:
        done = await rt.start("列出并读取外部 MCP 文档资源")
    finally:
        await rt.aclose()

    assert done["status"] == "completed"
    assert {"mcp_list_resources", "mcp_read_resource"} <= prov.seen_tool_names
    assert storage.get_call(done["id"], "res_l1")["result"]["count"] == 1
    assert storage.get_call(done["id"], "res_r1")["result"]["ok"] is True


async def test_mcp_server_create_server_exposes_repo_map_and_symbol_search():
    pytest.importorskip("mcp")
    from agent_lab.mcp_server import create_server

    srv_ext = create_server(ROOT / "data/sample_repo", ROOT / "data/knowledge", include_extended_tools=True)
    ext_tools = await srv_ext.list_tools()
    ext_names = {t.name for t in ext_tools}
    assert {"repo_list", "repo_read", "repo_search", "knowledge_search", "repo_map", "symbol_search"} == ext_names
    for t in ext_tools:
        if t.annotations is not None:
            assert t.annotations.readOnlyHint is True
            assert t.annotations.destructiveHint is False

    map_payload = _extract_mcp_text_json(await srv_ext.call_tool("repo_map", {"max_chars": 1500}))
    assert map_payload["ok"] is True
    assert "discounted_total" in map_payload["map"]

    sym_payload = _extract_mcp_text_json(
        await srv_ext.call_tool("symbol_search", {"symbol": "discounted_total"})
    )
    assert sym_payload["ok"] is True
    assert any(d["path"] == "pricing.py" and d["name"] == "discounted_total" for d in sym_payload["definitions"])

    srv_base = create_server(ROOT / "data/sample_repo", ROOT / "data/knowledge", include_extended_tools=False)
    base_names = {t.name for t in await srv_base.list_tools()}
    assert base_names == {"repo_list", "repo_read", "repo_search", "knowledge_search"}


# ---------------------------------------------------------------------------
# 3. computer.py 与 git_repo.py：MockComputerBackend ax_tree、未追踪合成 diff 与多语言暂存提交
# ---------------------------------------------------------------------------


def test_mock_computer_backend_ax_tree_bounds_centers_and_tool_dispatch(tmp_path: Path):
    backend = MockComputerBackend(width=1440, height=900)
    default_tree = backend.ax_tree(max_nodes=50, max_depth=5)
    assert default_tree["backend"] == "mock"
    assert default_tree["node_count"] >= 3
    for node in default_tree["nodes"]:
        assert {"role", "title", "x", "y", "width", "height", "center_x", "center_y"} <= set(node.keys())
        assert node["center_x"] == pytest.approx(node["x"] + node["width"] / 2.0, abs=0.2)
        assert node["center_y"] == pytest.approx(node["y"] + node["height"] / 2.0, abs=0.2)

    # 通过 execute_computer_tool 在最严 ask 档位下直接只读调用 ax_tree（无需人工审批）
    storage = Storage(tmp_path / "gui.db")
    run = storage.create_run("ax tree inspect")
    tool_res = execute_computer_tool(
        backend,
        storage,
        run["id"],
        name="computer_screen",
        arguments={"action": "ax_tree", "max_nodes": 20, "max_depth": 4},
        artifacts_dir=tmp_path / "artifacts",
        permission_mode="ask",
        call_id="ax_call_1",
    )
    assert tool_res["ok"] is True
    assert tool_res["action"] == "ax_tree"
    assert not tool_res.get("requires_approval")
    assert storage.proposals(run["id"]) == []
    run_btn = next(n for n in tool_res["nodes"] if n["role"] == "AXButton" and n["title"] == "Run")
    assert (run_btn["center_x"], run_btn["center_y"]) == (504.0, 96.0)

    # 自定义 ax_nodes 与 max_nodes 有界截断
    custom = MockComputerBackend(
        ax_nodes=[
            {"role": "AXButton", "title": "First", "x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0, "center_x": 60.0, "center_y": 40.0},
            {"role": "AXButton", "title": "Second", "x": 120.0, "y": 20.0, "width": 100.0, "height": 40.0, "center_x": 170.0, "center_y": 40.0},
        ]
    )
    truncated = custom.ax_tree(max_nodes=1)
    assert truncated["node_count"] == 1
    assert truncated["truncated"] is True
    assert truncated["nodes"][0]["title"] == "First"


def test_git_worktree_status_untracked_synthetic_diff_and_multilang_staging(tmp_path: Path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "app.py").write_text("X = 1\n", encoding="utf-8")

    origin = tmp_path / "origin"
    worktree = tmp_path / "wt"
    base_commit = init_git_repo_from_dir(seed, origin)
    create_task_worktree(origin, worktree, ref=base_commit)

    # 创建未追踪的 .ts / .go / .md 文件与敏感 .env 文件
    (worktree / "src").mkdir()
    (worktree / "pkg").mkdir()
    (worktree / "docs").mkdir()
    (worktree / "src" / "widget.ts").write_text("export const version = 2;\n", encoding="utf-8")
    (worktree / "pkg" / "handler.go").write_text("package pkg\n\nfunc Handle() int { return 1 }\n", encoding="utf-8")
    (worktree / "docs" / "notes.md").write_text("# Notes\nMulti-language support.\n", encoding="utf-8")
    (worktree / ".env").write_text("SECRET_TOKEN=must_not_appear_in_diff\n", encoding="utf-8")

    st = worktree_status(worktree, base_commit=base_commit, max_diff_chars=8000)
    assert st["ok"] is True
    assert st["is_git"] is True
    assert st["dirty"] is True
    diff = st["diff"]
    for rel, needle in (
        ("src/widget.ts", "+export const version = 2;"),
        ("pkg/handler.go", "+func Handle() int { return 1 }"),
        ("docs/notes.md", "+Multi-language support."),
    ):
        assert f"diff --git a/{rel} b/{rel}" in diff
        assert "new file mode 100644" in diff
        assert "--- /dev/null" in diff
        assert f"+++ b/{rel}" in diff
        assert needle in diff
    assert "must_not_appear_in_diff" not in diff

    # 删除敏感 .env 后，commit_verified_worktree 允许暂存并提交 .ts / .go / .md
    (worktree / ".env").unlink()
    committed = commit_verified_worktree(
        worktree,
        base_commit,
        {"status": "passed", "tests_executed": True},
        message="feat: add ts, go, and md files",
        allow_untracked=True,
    )
    assert committed["ok"] is True
    assert committed["commit_sha"] != base_commit
    assert set(committed["files"]) == {"docs/notes.md", "pkg/handler.go", "src/widget.ts"}

    # 受保护测试路径依然被严格拒绝
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_tamper.py").write_text("def test_x(): pass\n", encoding="utf-8")
    with pytest.raises(GitRepoError) as exc_info:
        commit_verified_worktree(
            worktree,
            committed["commit_sha"],
            {"status": "passed", "tests_executed": True},
            allow_untracked=True,
        )
    assert exc_info.value.code == "test_oracle_modified"

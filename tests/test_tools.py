"""工具边界是可执行约束：测试恶意参数，而不是仅检查提示词文本。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil

import pytest

from agent_lab.storage import Storage, digest
from agent_lab.tools import ToolRegistry


ROOT = Path(__file__).resolve().parents[1]
OLD = "return subtotal * (1 - discount_percent)"
NEW = "return subtotal * (1 - discount_percent / 100)"


class FakeKnowledge:
    def search(self, query, limit=5):
        return [{"id": "note-1", "text": "补丁后验证", "source": "original.md", "score": 1.0}]


@pytest.fixture
def registry(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(ROOT / "data" / "sample_repo", workspace)
    storage = Storage(tmp_path / "state.sqlite")
    run = storage.create_run("修复折扣")
    return ToolRegistry(storage, workspace, FakeKnowledge(), run["id"])


def propose(registry):
    result = registry.execute("propose_patch", {"path": "pricing.py", "old_text": OLD, "new_text": NEW})
    assert result["ok"]
    return result["proposal_id"]


def test_schema_forbids_unknown_tools_extra_args_and_wrong_types(registry):
    for name, args in [
        ("shell", {"command": "id"}),
        ("repo_list", {"extra": 1}),
        ("repo_read", {"path": 1}),
        ("repo_read", {}),
        ("repo_search", {"query": ""}),
    ]:
        assert registry.execute(name, args)["ok"] is False
    assert registry.execute("repo_read", {"path": "pricing.py"})["ok"] is True
    assert registry.storage.events(registry.run_id)[-1]["kind"] == "tool_audit"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/etc/passwd",
        ".env",
        "https://example.com/a.py",
        "tests/../../oops.py",
        "C:\\secret.py",
    ],
)
def test_paths_cannot_escape_repository(registry, path):
    assert registry.execute("repo_read", {"path": path})["code"] == "unsafe_path"


def test_symlinks_and_hardlinks_are_rejected(registry, tmp_path):
    outside = tmp_path / "private.py"
    outside.write_text("secret = 42")
    (registry.workspace / "link.py").symlink_to(outside)
    (registry.workspace / "hard.py").hardlink_to(outside)
    for path in ("link.py", "hard.py"):
        assert registry.execute("repo_read", {"path": path})["code"] == "unsafe_path"
    assert "secret" not in str(registry.execute("repo_search", {"query": "secret"}))


def test_patch_waits_for_human_approval_then_idempotently_applies(registry):
    proposal_id = propose(registry)
    file = registry.workspace / "pricing.py"
    assert OLD in file.read_text()
    assert registry.apply_proposal(proposal_id)["code"] == "approval_required"
    registry.storage.approve_proposal(proposal_id, True)
    assert registry.apply_proposal(proposal_id)["already_applied"] is False
    assert NEW in file.read_text()
    assert registry.apply_proposal(proposal_id)["already_applied"] is True
    assert registry.storage.get_proposal(proposal_id)["applied"] is True


def test_concurrent_apply_same_proposal_is_serialized(registry):
    proposal_id = propose(registry)
    registry.storage.approve_proposal(proposal_id, True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(registry.apply_proposal, [proposal_id, proposal_id]))
    assert all(result["ok"] for result in results)
    assert sorted(result["already_applied"] for result in results) == [False, True]


def test_version_conflict_does_not_overwrite_other_changes(registry):
    proposal_id = propose(registry)
    registry.storage.approve_proposal(proposal_id, True)
    file = registry.workspace / "pricing.py"
    changed = file.read_text() + "\n# another developer change\n"
    file.write_text(changed)
    assert registry.apply_proposal(proposal_id)["code"] == "version_conflict"
    assert file.read_text() == changed


def test_crash_after_file_write_recovers_by_new_hash(registry):
    proposal_id = propose(registry)
    registry.storage.approve_proposal(proposal_id, True)
    proposal = registry.storage.get_proposal(proposal_id)
    (registry.workspace / "pricing.py").write_text(proposal["new_content"])
    assert registry.apply_proposal(proposal_id)["already_applied"] is True
    assert registry.storage.get_proposal(proposal_id)["applied"] is True


def test_proposal_cannot_be_applied_by_another_run(registry):
    proposal_id = propose(registry)
    registry.storage.approve_proposal(proposal_id, True)
    other = registry.storage.create_run("other")
    another_registry = ToolRegistry(registry.storage, registry.workspace, FakeKnowledge(), other["id"])
    assert another_registry.apply_proposal(proposal_id)["code"] == "proposal_not_found"


def test_reject_ambiguous_patch_syntax_error_and_oracle_edit(registry):
    assert (
        registry.execute("propose_patch", {"path": "pricing.py", "old_text": "not present", "new_text": "a"})[
            "code"
        ]
        == "ambiguous_patch"
    )
    assert (
        registry.execute("propose_patch", {"path": "pricing.py", "old_text": OLD, "new_text": "return ("})[
            "code"
        ]
        == "syntax_error"
    )
    assert (
        registry.execute(
            "propose_patch", {"path": "tests/test_pricing.py", "old_text": "180", "new_text": "-1800"}
        )["code"]
        == "edit_forbidden"
    )
    assert (
        registry.execute("propose_patch", {"path": "README.md", "old_text": "Sample Shop", "new_text": "x"})[
            "code"
        ]
        == "edit_forbidden"
    )


def test_verify_is_only_syntax_and_does_not_execute_code(registry):
    file = registry.workspace / "no_execute.py"
    file.write_text('raise RuntimeError("MUST NOT EXECUTE")\n')
    result = registry.execute("verify_python", {"path": "no_execute.py"})
    assert result["ok"] is True
    assert result["verification"] == "syntax_only"
    assert result["tests_executed"] is False
    file.write_text("return (")
    assert registry.execute("verify_python", {"path": "no_execute.py"})["code"] == "syntax_error"


def test_network_urls_are_never_fetched(registry):
    assert registry.execute("knowledge_search", {"query": "https://example.com"})["code"] == "invalid_query"
    matches = registry.execute("knowledge_search", {"query": "验证"})
    assert matches["trust"] == "untrusted_reference_material"


def test_sandbox_fails_closed_when_docker_is_absent(registry, monkeypatch):
    monkeypatch.setattr("agent_lab.sandbox.shutil.which", lambda _: None)
    result = registry.execute("sandbox_test", {})
    assert result["code"] == "sandbox_unavailable"
    assert result["tests_executed"] is False


def test_modified_test_oracle_refuses_verification(registry, monkeypatch):
    (registry.workspace / "tests" / "test_pricing.py").write_text("# fake passing tests\n")
    monkeypatch.setattr("agent_lab.sandbox.shutil.which", lambda _: None)
    assert registry.execute("sandbox_test", {})["code"] == "test_oracle_modified"


def test_sandbox_delegates_only_workspace_and_protected_oracle(registry, monkeypatch):
    seen = []
    sentinel = {
        "ok": False,
        "code": "sandbox_timeout",
        "tests_executed": None,
        "cleanup_verified": True,
        "cleanup_error": None,
    }

    def run(self, workspace, oracle_sha256):
        seen.append((workspace, oracle_sha256))
        return sentinel

    monkeypatch.setattr("agent_lab.sandbox.SandboxRunner.run", run)
    result = registry.execute("sandbox_test", {})
    assert result == sentinel
    assert seen == [(registry.workspace, digest((registry.workspace / "tests/test_pricing.py").read_text()))]
    # 模型不能靠额外 schema 参数改镜像或选择任意命令。
    assert registry.execute("sandbox_test", {"command": "id"})["code"] == "invalid_arguments"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_real_mcp_stdio_roundtrip_has_only_readonly_tools():
    """连接真实 SDK server，验证 initialize/list_tools/call_tool，而非自造协议。"""
    pytest.importorskip("mcp")
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_lab.mcp_server",
            "--workspace",
            str(ROOT / "data/sample_repo"),
            "--knowledge",
            str(ROOT / "data/knowledge"),
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "repo_list",
                "repo_read",
                "repo_search",
                "knowledge_search",
            }
            listed = await session.call_tool("repo_list", {})
            assert not listed.isError
            assert "pricing.py" in listed.content[0].text
            rejected = await session.call_tool("repo_read", {"path": "../outside.py"})
            assert "unsafe_path" in str(rejected.content)

"""工具边界是可执行约束：测试恶意参数，而不是仅检查提示词文本。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from agent_lab.sandbox import discover_test_oracles
from agent_lab.storage import Storage, digest
from agent_lab.tools import _LOCKS, ToolRegistry, prune_workspace_lock


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


def test_apply_proposal_rolls_forward_after_mid_changeset_sigkill(registry):
    shipping = registry.workspace / "shipping.py"
    shipping.write_text("def fee(cents: int) -> int:\n    return cents + 500\n", encoding="utf-8")
    proposed = registry.execute(
        "propose_changeset",
        {
            "summary": "fix pricing and shipping atomically",
            "edits": [
                {"path": "pricing.py", "old_text": OLD, "new_text": NEW},
                {"path": "shipping.py", "old_text": "return cents + 500", "new_text": "return max(0, cents) + 500"},
            ],
        },
    )
    assert proposed["ok"]
    proposal_id = proposed["proposal_id"]
    registry.storage.approve_proposal(proposal_id, True)
    proposal = registry.storage.get_proposal(proposal_id)

    # 模拟第 1 个文件已替换落盘、写前日志残留、第 2 个文件尚未替换时遭遇 SIGKILL（applied=False）。
    journal_path = registry.workspace / f".agent-apply-{proposal_id}.json"
    journal_path.write_text(json.dumps({"proposal_id": proposal_id, "files": proposal["files"]}), encoding="utf-8")
    (registry.workspace / "pricing.py").write_text(proposal["files"][0]["new_content"], encoding="utf-8")
    assert "max(0, cents)" not in shipping.read_text(encoding="utf-8")
    assert registry.storage.get_proposal(proposal_id)["applied"] is False

    recovered = registry.apply_proposal(proposal_id)
    assert recovered["ok"] is True
    assert recovered["already_applied"] is False
    assert recovered["recovered_partial_apply"] is True
    assert NEW in (registry.workspace / "pricing.py").read_text(encoding="utf-8")
    assert "max(0, cents)" in shipping.read_text(encoding="utf-8")
    assert not journal_path.exists()
    assert registry.storage.get_proposal(proposal_id)["applied"] is True
    assert registry.apply_proposal(proposal_id)["already_applied"] is True


def test_mid_changeset_third_party_drift_still_returns_version_conflict(registry):
    shipping = registry.workspace / "shipping.py"
    shipping.write_text("def fee(cents: int) -> int:\n    return cents + 500\n", encoding="utf-8")
    proposed = registry.execute(
        "propose_changeset",
        {
            "edits": [
                {"path": "pricing.py", "old_text": OLD, "new_text": NEW},
                {"path": "shipping.py", "old_text": "return cents + 500", "new_text": "return cents + 300"},
            ]
        },
    )
    proposal_id = proposed["proposal_id"]
    registry.storage.approve_proposal(proposal_id, True)
    proposal = registry.storage.get_proposal(proposal_id)

    # 第 1 个文件处于 post-state，但第 2 个文件被第三方改成既非 pre 也非 post 的内容 -> 必须报 version_conflict。
    (registry.workspace / "pricing.py").write_text(proposal["files"][0]["new_content"], encoding="utf-8")
    third_party = "def fee(cents: int) -> int:\n    return 9999\n"
    shipping.write_text(third_party, encoding="utf-8")
    conflict = registry.apply_proposal(proposal_id)
    assert conflict["ok"] is False and conflict["code"] == "version_conflict"
    assert shipping.read_text(encoding="utf-8") == third_party
    assert registry.storage.get_proposal(proposal_id)["applied"] is False


def test_secret_files_blocked_across_read_list_search_and_edit_tools(registry):
    secret_marker = "TOP_SECRET_CREDENTIAL_MARKER_98765"
    for name in ("credentials.py", "secrets.py", "tokens.py", "cert.pem", "id_rsa"):
        (registry.workspace / name).write_text(f'TOKEN = "{secret_marker}"\n', encoding="utf-8")

    listed = registry.execute("repo_list", {})
    assert listed["ok"] is True
    for name in ("credentials.py", "secrets.py", "tokens.py", "cert.pem", "id_rsa"):
        assert name not in listed["files"]

    searched = registry.execute("repo_search", {"query": secret_marker})
    assert searched["ok"] is True and searched["matches"] == []

    for name in ("credentials.py", "secrets.py", "tokens.py", "cert.pem"):
        assert registry.execute("repo_read", {"path": name})["code"] == "secret_access_forbidden"
        assert (
            registry.execute("propose_patch", {"path": name, "old_text": secret_marker, "new_text": "REDACTED"})[
                "code"
            ]
            == "secret_access_forbidden"
        )
        assert (
            registry.execute(
                "propose_changeset",
                {"edits": [{"path": name, "action": "delete"}]},
            )["code"]
            == "secret_access_forbidden"
        )
    assert (
        registry.execute(
            "propose_changeset",
            {"edits": [{"path": "private_key.py", "action": "create", "new_text": "KEY = 1\n"}]},
        )["code"]
        == "secret_access_forbidden"
    )
    assert registry.execute("repo_read", {"path": ".env"})["code"] == "unsafe_path"


def test_workspace_lock_lru_cap_and_prune(tmp_path):
    storage = Storage(tmp_path / "locks_cap.sqlite")
    run = storage.create_run("lock lru")
    first_ws = tmp_path / "ws_0"
    first_ws.mkdir()
    reg0 = ToolRegistry(storage, first_ws, FakeKnowledge(), run["id"])
    assert str(reg0.workspace) in _LOCKS
    prune_workspace_lock(first_ws)
    assert str(reg0.workspace) not in _LOCKS

    for idx in range(1, 270):
        ws = tmp_path / f"ws_{idx}"
        ws.mkdir()
        ToolRegistry(storage, ws, FakeKnowledge(), run["id"])
    assert len(_LOCKS) <= 256


def test_crlf_test_oracle_passes_sandbox_test_baseline_check(registry, tmp_path, monkeypatch):
    oracle_path = registry.workspace / "tests" / "test_pricing.py"
    lf_bytes = oracle_path.read_bytes()
    crlf_bytes = lf_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    oracle_path.write_bytes(crlf_bytes)

    seen_builtin = []
    monkeypatch.setattr(
        "agent_lab.sandbox.SandboxRunner.run",
        lambda self, ws, sha: seen_builtin.append(sha) or {"ok": True, "code": "passed", "tests_executed": True},
    )
    res_builtin = registry.execute("sandbox_test", {})
    assert res_builtin["ok"] is True and res_builtin["code"] == "passed"
    assert seen_builtin == [hashlib.sha256(crlf_bytes).hexdigest()]

    # 基于 discover_test_oracles 的自定义套件（CRLF 测试文件）同样不会误报 test_oracle_modified。
    oracles, expected_tests = discover_test_oracles(registry.workspace)
    suite_run = registry.storage.create_run("crlf suite")
    suite_reg = ToolRegistry(
        registry.storage,
        registry.workspace,
        FakeKnowledge(),
        suite_run["id"],
        test_oracles=oracles,
        expected_tests=expected_tests,
    )
    monkeypatch.setattr(
        "agent_lab.sandbox.SandboxRunner.run_suite",
        lambda self, ws, orc, expected_tests: {"ok": True, "code": "passed", "tests_executed": True},
    )
    res_suite = suite_reg.execute("sandbox_test", {})
    assert res_suite["ok"] is True and res_suite["code"] == "passed"


def test_repo_read_line_ranges_and_compact_medium_file(registry):
    # 小文件（<= 900 chars）保留 numbered 字段以兼容既有行为。
    small = registry.execute("repo_read", {"path": "pricing.py"})
    assert small["ok"] is True and "numbered" in small and small["total_lines"] >= 1

    # 70 行中等文件（> 900 chars 且单份正文 < 2600 chars）：省略重复 numbered，避免触发 content_omitted。
    lines = [f"def f_{i}(x): return x + {i}" for i in range(1, 71)]
    medium_content = "\n".join(lines) + "\n"
    assert 900 < len(medium_content) < 2300
    (registry.workspace / "medium_mod.py").write_text(medium_content, encoding="utf-8")

    whole = registry.execute("repo_read", {"path": "medium_mod.py"})
    assert whole["ok"] is True
    assert whole["content"] == medium_content
    assert whole["sha256"] == digest(medium_content)
    assert whole["total_lines"] == 70
    assert "numbered" not in whole
    assert len(json.dumps(whole, ensure_ascii=False, separators=(",", ":"))) < 2600

    sliced = registry.execute("repo_read", {"path": "medium_mod.py", "start_line": 10, "end_line": 14})
    assert sliced["ok"] is True and sliced["partial"] is True
    assert sliced["start_line"] == 10 and sliced["end_line"] == 14 and sliced["total_lines"] == 70
    assert sliced["sha256"] == digest(medium_content)
    assert sliced["content"].splitlines() == lines[9:14]
    assert sliced["numbered"].splitlines()[0].startswith("10: def f_10")
    assert registry.execute("repo_read", {"path": "medium_mod.py", "start_line": 20, "end_line": 5})["code"] == (
        "invalid_arguments"
    )


def test_propose_changeset_create_py_modify_configs_and_delete_obsolete_file(registry):
    (registry.workspace / "pyproject.toml").write_text('[project]\nname = "shop"\nversion = "0.1.0"\n', encoding="utf-8")
    (registry.workspace / "config.json").write_text('{"currency": "USD", "max_items": 10}\n', encoding="utf-8")
    (registry.workspace / "obsolete.py").write_text("LEGACY = True\n", encoding="utf-8")

    # 非法 TOML / JSON 语法与受限 .md 均在提案阶段整组拒绝；propose_patch 仍严格限制 .py。
    assert (
        registry.execute(
            "propose_patch",
            {"path": "pyproject.toml", "old_text": 'version = "0.1.0"', "new_text": 'version = "0.2.0"'},
        )["code"]
        == "edit_forbidden"
    )
    assert (
        registry.execute(
            "propose_changeset",
            {"edits": [{"path": "README.md", "old_text": "Sample Shop", "new_text": "Updated"}]},
        )["code"]
        == "edit_forbidden"
    )
    assert (
        registry.execute(
            "propose_changeset",
            {"edits": [{"path": "config.json", "old_text": '"max_items": 10', "new_text": '"max_items": '}]},
        )["code"]
        == "syntax_error"
    )
    assert (
        registry.execute(
            "propose_changeset",
            {"edits": [{"path": "pyproject.toml", "old_text": 'version = "0.1.0"', "new_text": "version = ["}]},
        )["code"]
        == "syntax_error"
    )

    edits = [
        {"path": "helpers/math_utils.py", "action": "create", "new_text": "def clamp_nonneg(x: int) -> int:\n    return max(0, x)\n"},
        {"path": "pyproject.toml", "action": "modify", "old_text": 'version = "0.1.0"', "new_text": 'version = "0.2.0"'},
        {"path": "config.json", "old_text": '"max_items": 10', "new_text": '"max_items": 25'},
        {"path": "obsolete.py", "action": "delete"},
    ]
    proposed = registry.execute("propose_changeset", {"summary": "create/modify/delete", "edits": edits}, call_id="cs_crud")
    assert proposed["ok"] is True
    replayed = registry.execute("propose_changeset", {"summary": "create/modify/delete", "edits": edits}, call_id="cs_crud")
    assert replayed["replayed"] is True and replayed["proposal_id"] == proposed["proposal_id"]

    stored = registry.storage.get_proposal(proposed["proposal_id"])
    assert [f["action"] for f in stored["files"]] == ["create", "modify", "modify", "delete"]

    registry.storage.approve_proposal(proposed["proposal_id"], True)
    applied = registry.apply_proposal(proposed["proposal_id"])
    assert applied["ok"] is True and applied["already_applied"] is False
    assert "def clamp_nonneg" in (registry.workspace / "helpers/math_utils.py").read_text(encoding="utf-8")
    assert 'version = "0.2.0"' in (registry.workspace / "pyproject.toml").read_text(encoding="utf-8")
    assert json.loads((registry.workspace / "config.json").read_text(encoding="utf-8"))["max_items"] == 25
    assert not (registry.workspace / "obsolete.py").exists()
    assert registry.apply_proposal(proposed["proposal_id"])["already_applied"] is True


def test_stem_based_secrets_blocked_across_all_extensions(registry):
    marker = "STEM_SECRET_LEAK_GUARD_778899"
    secret_files = (
        "secrets.json",
        "credentials.yaml",
        "tokens.toml",
        "private_key.cfg",
        "api_key.ini",
        "apikey.txt",
        "secret.md",
    )
    for rel in secret_files:
        (registry.workspace / rel).write_text(f'secret = "{marker}"\n', encoding="utf-8")

    listed = registry.execute("repo_list", {})
    assert listed["ok"] is True
    for rel in secret_files:
        assert rel not in listed["files"]

    searched = registry.execute("repo_search", {"query": marker})
    assert searched["ok"] is True and searched["matches"] == []

    for rel in ("secrets.json", "credentials.yaml", "tokens.toml", "private_key.cfg", "api_key.ini"):
        assert registry.execute("repo_read", {"path": rel})["code"] == "secret_access_forbidden"
        assert (
            registry.execute("propose_patch", {"path": rel, "old_text": marker, "new_text": "REDACTED"})["code"]
            == "secret_access_forbidden"
        )
        assert (
            registry.execute(
                "propose_changeset",
                {"edits": [{"path": rel, "old_text": marker, "new_text": "REDACTED"}]},
            )["code"]
            == "secret_access_forbidden"
        )
        assert (
            registry.execute("propose_changeset", {"edits": [{"path": rel, "action": "delete"}]})["code"]
            == "secret_access_forbidden"
        )


def test_crlf_multifile_changeset_partial_crash_roll_forward_and_idempotent_retry(registry):
    pricing = registry.workspace / "pricing.py"
    cfg = registry.workspace / "settings.json"
    pricing_crlf = pricing.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    pricing.write_bytes(pricing_crlf)
    cfg.write_bytes(b'{\r\n  "discount_cap": 20,\r\n  "strict": true\r\n}\r\n')

    proposed = registry.execute(
        "propose_changeset",
        {
            "summary": "fix pricing and cap under CRLF",
            "edits": [
                {"path": "pricing.py", "old_text": OLD, "new_text": NEW},
                {"path": "settings.json", "old_text": '"discount_cap": 20', "new_text": '"discount_cap": 50'},
            ],
        },
    )
    assert proposed["ok"] is True
    pid = proposed["proposal_id"]
    registry.storage.approve_proposal(pid, True)
    stored = registry.storage.get_proposal(pid)

    # 模拟第 1 个 CRLF 文件已用原始字节写入落盘、日志已建、第 2 个 CRLF 文件未写入时崩溃
    journal_path = registry.workspace / f".agent-apply-{pid}.json"
    journal_path.write_text(json.dumps({"proposal_id": pid, "files": stored["files"]}), encoding="utf-8")
    pricing.write_bytes(stored["files"][0]["new_content"].encode("utf-8"))
    assert b"\r\n" in pricing.read_bytes()

    rolled = registry.apply_proposal(pid)
    assert rolled["ok"] is True
    assert rolled["already_applied"] is False
    assert rolled["recovered_partial_apply"] is True
    assert b"\r\n" in pricing.read_bytes() and NEW.encode("utf-8") in pricing.read_bytes()
    assert b'"discount_cap": 50,\r\n' in cfg.read_bytes()
    assert not journal_path.exists()

    retried = registry.apply_proposal(pid)
    assert retried["ok"] is True and retried["already_applied"] is True


def test_single_config_propose_changeset_crash_before_finish_call_safe_replay_and_apply(tmp_path):
    from agent_lab.runtime import AgentRuntime

    workspace = tmp_path / "ws_cfg_replay"
    shutil.copytree(ROOT / "data" / "sample_repo", workspace)
    (workspace / "config.json").write_text('{"timeout_ms": 1000}\n', encoding="utf-8")

    storage = Storage(tmp_path / "replay_cfg.sqlite")
    run = storage.create_run("update config timeout")
    runtime = AgentRuntime(
        storage=storage,
        workspace=workspace,
        knowledge_dir=ROOT / "data" / "knowledge",
        skills_dir=ROOT / "skills",
    )
    reg = ToolRegistry(storage, workspace, FakeKnowledge(), run["id"])

    call = {
        "id": "call_cfg_1",
        "name": "propose_changeset",
        "input": {
            "summary": "bump timeout in single json file",
            "edits": [{"path": "config.json", "old_text": '"timeout_ms": 1000', "new_text": '"timeout_ms": 2500'}],
        },
    }
    # 模拟 schedule_call + start_call + create_changeset_proposal 落盘后、finish_call 前进程崩溃
    storage.schedule_call(run["id"], call["id"], call["name"], call["input"])
    storage.start_call(run["id"], call["id"])
    first_res = reg.execute(call["name"], call["input"], call_id=call["id"])
    assert first_res["ok"] is True
    assert storage.get_call(run["id"], call["id"])["status"] == "running"

    persisted = storage.get_proposal(first_res["proposal_id"])
    assert persisted["tool"] == "propose_changeset"
    assert persisted["kind"] == "changeset"
    assert persisted["files"][0]["action"] == "modify"

    # 通过 Runtime SAFE_REPLAY 恢复未结束调用，不得丢失 tool="propose_changeset" 且随后 apply_proposal 正常生效
    replayed = runtime._execute_call(run, reg, call)
    assert replayed["ok"] is True
    assert replayed["replayed"] is True
    assert replayed["proposal_id"] == first_res["proposal_id"]
    assert storage.get_call(run["id"], call["id"])["status"] == "waiting_approval"

    storage.approve_proposal(replayed["proposal_id"], True)
    applied = reg.apply_proposal(replayed["proposal_id"])
    assert applied["ok"] is True and applied["already_applied"] is False
    assert json.loads((workspace / "config.json").read_text(encoding="utf-8"))["timeout_ms"] == 2500


def test_gc_prunes_workspace_lock_summary_error_detail_and_apply_lock_isolation(tmp_path):
    from agent_lab.execution_lock import cleanup_stale_locks
    from agent_lab.tools import _cross_process_apply_lock

    storage = Storage(tmp_path / "gc_locks.sqlite")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    run = storage.create_run("gc lock & summary error_detail")
    ws = workspaces / run["id"]
    shutil.copytree(ROOT / "data" / "sample_repo", ws)
    reg = ToolRegistry(storage, ws, FakeKnowledge(), run["id"])
    assert str(reg.workspace) in _LOCKS

    run["status"] = "failed"
    run["metadata"]["workspace"] = str(ws)
    run["metadata"]["error_detail"] = {"error_type": "TimeoutError", "retryable": True}
    storage.save_run(run)

    listed = storage.list_runs()
    assert listed[0]["error_detail"] == {"error_type": "TimeoutError", "retryable": True}
    assert listed[0]["metadata"]["error_detail"] == {"error_type": "TimeoutError", "retryable": True}

    # _cross_process_apply_lock 使用独立 apply_locks 目录并校验 st_nlink/inode，不受 cleanup_stale_locks 影响
    with _cross_process_apply_lock(storage.path, reg.workspace):
        cleanup_stale_locks(storage.path, active_run_ids=set(), max_age_seconds=0.0)
        apply_locks = list((storage.path.parent / "apply_locks").glob("apply-*.lock"))
        assert len(apply_locks) == 1 and apply_locks[0].stat().st_nlink == 1

    stats = storage.gc_expired_resources(workspaces_dir=workspaces, retention_seconds=0.0)
    assert stats["removed_workspaces"] == 1
    assert str(reg.workspace) not in _LOCKS

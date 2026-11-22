"""本地文件系统工具（fs_*）、终端执行（bash_exec）与三级权限策略（ask / auto-edit / full-auto）测试。"""

from __future__ import annotations

import json
import shutil
import uuid

from agent_lab.api import ROOT
from agent_lab.permissions import classify_bash_command, evaluate_tool_permission
from agent_lab.runtime import EXTRA_TOOLS, AgentRuntime
from agent_lab.storage import Storage
from agent_lab.tools import ToolRegistry


def _call(name: str, **kwargs):
    return {"type": "tool_use", "id": "ht_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict]):
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 12},
    }


def test_default_sandbox_profile_keeps_exact_legacy_tool_surface_and_overhead(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/sample_repo", ws)
    storage = Storage(tmp_path / "state.db")
    rt = AgentRuntime(storage, ws, ROOT / "data/knowledge", ROOT / "skills")
    run = rt.create("默认沙盒兼容性核验")
    reg = rt._registry(run)

    tool_names = [t["name"] for t in reg.definitions()]
    assert len(tool_names) == 11
    assert "bash_exec" not in tool_names
    assert "fs_write" not in tool_names
    assert reg.execute("shell", {"command": "id"})["code"] == "unknown_tool"

    reserved = (
        len(
            json.dumps(
                {"system": rt._system(run), "tools": reg.definitions() + EXTRA_TOOLS, "messages": []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        - 2
    )
    assert reserved < 10_000


def test_bash_classifier_and_three_tier_permission_matrix():
    assert classify_bash_command("rm -rf /")["classification"] == "blocked"
    assert classify_bash_command("rm -r -f -- /")["classification"] == "blocked"
    assert classify_bash_command("diskutil eraseDisk APFS Test /dev/disk9")["classification"] == "blocked"
    assert classify_bash_command("sudo rm -rf ~")["classification"] == "blocked"
    assert classify_bash_command("dd if=/dev/zero of=/dev/disk0")["classification"] == "blocked"
    assert classify_bash_command("git status")["classification"] == "safe_readonly"
    assert classify_bash_command("git branch --show-current")["classification"] == "safe_readonly"
    assert classify_bash_command("git branch -D main")["classification"] == "mutating"
    assert classify_bash_command("git diff --no-index .env /dev/null")["classification"] == "mutating"
    assert classify_bash_command("GIT_EXTERNAL_DIFF=./run.sh git diff")["classification"] == "mutating"
    assert classify_bash_command("./ls")["classification"] == "mutating"
    assert classify_bash_command("ruff check --fix-only src")["classification"] == "mutating"
    assert classify_bash_command("pytest --basetemp=.")["classification"] == "mutating"
    assert classify_bash_command("pytest -q tests/test_pricing.py")["classification"] == "safe_readonly"
    assert classify_bash_command("uv run --no-sync ruff check src")["classification"] == "safe_readonly"
    assert classify_bash_command("ls -la && rm foo.txt")["classification"] == "mutating"
    assert classify_bash_command("python3 -c 'print(1)'")["classification"] == "mutating"

    # ask 模式：连只读命令和 fs_write 也需要人工确认
    assert evaluate_tool_permission("fs_write", {"path": "a.py"}, permission_mode="ask")["decision"] == "ask"
    assert evaluate_tool_permission("bash_exec", {"command": "ls"}, permission_mode="ask")["decision"] == "ask"

    # auto-edit 模式：fs_write 与安全只读命令自动放行，修改型命令需确认
    assert (
        evaluate_tool_permission("fs_write", {"path": "a.py"}, permission_mode="auto-edit")["decision"]
        == "allow"
    )
    assert (
        evaluate_tool_permission("bash_exec", {"command": "git diff"}, permission_mode="auto-edit")["decision"]
        == "allow"
    )
    assert (
        evaluate_tool_permission(
            "bash_exec", {"command": "python3 build.py"}, permission_mode="auto-edit"
        )["decision"]
        == "ask"
    )

    # full-auto 模式：普通修改命令放行，但毁机命令依然硬拦截
    assert (
        evaluate_tool_permission(
            "bash_exec", {"command": "python3 build.py"}, permission_mode="full-auto"
        )["decision"]
        == "allow"
    )
    assert (
        evaluate_tool_permission("bash_exec", {"command": "rm -rf /"}, permission_mode="full-auto")["decision"]
        == "deny"
    )


def test_host_filesystem_crud_glob_grep_and_secret_protection(tmp_path):
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / ".gitignore").write_text("ignored_dir/\n*.log\n", encoding="utf-8")
    (ws / "ignored_dir").mkdir()
    (ws / "ignored_dir/skip.py").write_text("SECRET_SKIP = 1\n", encoding="utf-8")
    (ws / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")

    storage = Storage(tmp_path / "state.db")
    run = storage.create_run("host fs test")
    reg = ToolRegistry(
        storage,
        ws,
        None,
        run["id"],
        execution_profile="host",
        permission_mode="auto-edit",
    )

    # 1. 允许写入 tests/ 目录、.sh 脚本与大于 64KiB 的源码文件（上限 512KiB）
    w_sh = reg.execute(
        "fs_write",
        {"path": "scripts/hello.sh", "content": "#!/usr/bin/env bash\necho HELLO_FROM_SCRIPT\n"},
        call_id="c1",
    )
    assert w_sh["ok"] is True
    assert (ws / "scripts/hello.sh").stat().st_mode & 0o111

    big_text = "\n".join(f"LINE_{i} = {i}" for i in range(6000)) + "\n"
    assert len(big_text.encode("utf-8")) > 64 * 1024
    w_big = reg.execute("fs_write", {"path": "tests/test_large.py", "content": big_text}, call_id="c2")
    assert w_big["ok"] is True

    # 2. fs_read 分页读取
    r_page = reg.execute("fs_read", {"path": "tests/test_large.py", "offset_line": 100, "limit_lines": 5})
    assert r_page["ok"] is True
    assert r_page["total_lines"] == 6000
    assert r_page["start_line"] == 100 and r_page["end_line"] == 104
    assert "100\tLINE_99 = 99" in r_page["content"]

    # 3. fs_edit 唯一匹配保护与 replace_all
    reg.execute("fs_write", {"path": "app.ts", "content": "const x = 1;\nconst y = 1;\n"}, call_id="c3")
    amb = reg.execute("fs_edit", {"path": "app.ts", "old_text": "= 1;", "new_text": "= 2;"})
    assert amb["ok"] is False and amb["code"] == "ambiguous_match"
    ed_all = reg.execute(
        "fs_edit", {"path": "app.ts", "old_text": "= 1;", "new_text": "= 2;", "replace_all": True}
    )
    assert ed_all["ok"] is True and ed_all["replacements"] == 2
    assert "const y = 2;" in (ws / "app.ts").read_text(encoding="utf-8")

    # 4. fs_glob 与 fs_grep 自动跳过 .gitignore 与 .env 敏感文件
    glob_res = reg.execute("fs_glob", {"pattern": "**/*"})
    assert glob_res["ok"] is True
    assert "app.ts" in glob_res["files"]
    assert ".gitignore" in glob_res["files"]
    assert ".env" not in glob_res["files"]
    assert "ignored_dir/skip.py" not in glob_res["files"]

    grep_res = reg.execute("fs_grep", {"pattern": r"const\s+[xy]\s*=\s*2", "glob": "*.ts"})
    assert grep_res["ok"] is True and grep_res["matches_count"] == 2

    # 5. 拒绝读取 .env、.git、绝对路径软链接绕过与路径穿越
    (ws / "local.cfg").write_text("PLAIN_CFG=1\n", encoding="utf-8")
    (ws / ".env.link").symlink_to(ws / "local.cfg")
    assert reg.execute("fs_read", {"path": ".env"})["code"] == "secret_access_forbidden"
    assert reg.execute("fs_read", {"path": str(ws / ".env.link")})["code"] == "secret_access_forbidden"
    (ws / "sub_link").symlink_to(ws / "scripts")
    assert reg.execute("fs_read", {"path": str(ws / "sub_link/hello.sh")})["code"] == "unsafe_path"
    assert reg.execute("fs_write", {"path": ".git/config", "content": "x"})["code"] == "unsafe_path"
    assert reg.execute("fs_read", {"path": "../outside.txt"})["code"] == "unsafe_path"


def test_bash_exec_scrubs_secrets_and_reaps_timed_out_process_group(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TOPSECRET-999")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-TOPSECRET-888")
    monkeypatch.setenv("AGENT_LAB_TOKEN", "admin-token-777")
    monkeypatch.setenv("MY_CUSTOM_SECRET_KEY", "hidden-123")
    monkeypatch.setenv("NORMAL_DEV_FLAG", "visible-456")

    storage = Storage(tmp_path / "state.db")
    run = storage.create_run("bash test")
    reg = ToolRegistry(
        storage,
        ws,
        None,
        run["id"],
        execution_profile="host",
        permission_mode="full-auto",
    )

    env_res = reg.execute(
        "bash_exec",
        {
            "command": (
                "python3 -c \"import os, json; print(json.dumps({"
                "'ant': os.environ.get('ANTHROPIC_API_KEY'),"
                "'oai': os.environ.get('OPENAI_API_KEY'),"
                "'tok': os.environ.get('AGENT_LAB_TOKEN'),"
                "'sec': os.environ.get('MY_CUSTOM_SECRET_KEY'),"
                "'dev': os.environ.get('NORMAL_DEV_FLAG')"
                "}))\""
            )
        },
    )
    assert env_res["ok"] is True, env_res
    parsed = json.loads(env_res["stdout"].strip())
    assert parsed == {"ant": None, "oai": None, "tok": None, "sec": None, "dev": "visible-456"}

    # 子进程组超时终止测试：后台派生子进程 sleep，超时后立即干净回收不挂起
    pid_file = ws / "child.pid"
    timeout_res = reg.execute(
        "bash_exec",
        {
            "command": "python3 -c 'import os, time; open(\"child.pid\", \"w\").write(str(os.getpid())); time.sleep(30)'",
            "timeout_seconds": 1,
        },
    )
    assert timeout_res["ok"] is False
    assert timeout_res["code"] == "timeout"
    assert timeout_res["timed_out"] is True
    if pid_file.exists():
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        import os
        import pytest

        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


async def test_auto_edit_command_approval_delivers_nonzero_exit_output_for_self_repair(tmp_path):
    """验证 auto-edit 模式下：写文件自动放行，运行脚本暂停等待确认；即使脚本首次退出码非 0，模型也能拿到报错输出继续修好！"""
    ws = tmp_path / "app_repo"
    ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    class ScriptedHostAgent:
        def __init__(self):
            self.step = 0

        async def complete(self, system, messages, tools, max_tokens=2048, **kwargs):
            self.step += 1
            if self.step == 1:
                # 写一个复现/验证脚本（auto-edit 自动放行）
                return _resp(
                    [
                        _call(
                            "fs_write",
                            path="check_calc.py",
                            content="from calc import add\nassert add(2, 3) == 5, f'got {add(2, 3)}'\nprint('ALL_OK')\n",
                        )
                    ]
                )
            if self.step == 2:
                # 执行任意脚本在 auto-edit 下触发 command 提案审批
                return _resp([_call("bash_exec", command="python3 check_calc.py")])
            if self.step == 3:
                # 人工批准后，check_calc.py 退出码为 1（AssertionError: got -1），模型收到 REVIEW_APPROVED 输出后用 fs_edit 修复
                serialized = json.dumps(messages, ensure_ascii=False)
                assert "AssertionError: got -1" in serialized
                return _resp(
                    [
                        _call(
                            "fs_edit",
                            path="calc.py",
                            old_text="return a - b",
                            new_text="return a + b",
                        )
                    ]
                )
            if self.step == 4:
                return _resp([_call("bash_exec", command="python3 check_calc.py")])
            serialized = json.dumps(messages, ensure_ascii=False)
            assert "ALL_OK" in serialized
            return _resp([{"type": "text", "text": "已通过复现脚本定位并修复 calc.py。"}])

    storage = Storage(tmp_path / "state.db")
    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=ScriptedHostAgent(),
        execution_profile="host",
        permission_mode="auto-edit",
    )
    created = runtime.create("编写复现脚本运行并修复 calc.py")
    paused_1 = await runtime.execute(created["id"])
    assert paused_1["status"] == "waiting_approval"
    assert (ws / "check_calc.py").is_file()

    pid_1 = paused_1["metadata"]["waiting_proposal_ids"][0]
    prop_1 = storage.get_proposal(pid_1)
    assert prop_1["kind"] == "command"

    # 第一次批准：check_calc.py 失败（exit_code=1），但任务不崩溃，而是自动修复并来到第二次验证审批
    paused_2 = await runtime.approve(created["id"], pid_1, True)
    assert paused_2["status"] == "waiting_approval"
    assert "return a + b" in (ws / "calc.py").read_text(encoding="utf-8")

    pid_2 = paused_2["metadata"]["waiting_proposal_ids"][0]
    done = await runtime.approve(created["id"], pid_2, True)
    assert done["status"] == "completed"
    assert "修复 calc.py" in done["answer"]

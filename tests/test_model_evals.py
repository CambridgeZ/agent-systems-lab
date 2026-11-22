"""5 任务编码基准评测（失败分类、容器真值与 OpenAI 兼容适配器契约）测试。"""

import json

import httpx

from agent_lab.model_evals import BENCHMARK_DIR, _oracles_intact, run_model_evals
from agent_lab.providers import OpenAICompatibleProvider
from agent_lab.sandbox import SandboxRunner, discover_test_oracles


def test_all_five_benchmark_repos_have_discoverable_protected_oracles():
    tasks = json.loads((BENCHMARK_DIR / "tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 5
    total_tests = 0
    for task in tasks:
        repo_dir = BENCHMARK_DIR / task["repo_dir"]
        oracles, count = discover_test_oracles(repo_dir)
        assert oracles and count >= 4
        total_tests += count
    assert total_tests == 21


async def test_model_evals_separates_infrastructure_failure_from_model_failure(tmp_path, monkeypatch):
    # 1. 缺少真实模型凭证时：归类为 infrastructure_failure (missing_credentials)，而非 model_failure
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    missing_cred_report = await run_model_evals(
        provider="openai",
        task_ids=["cursor_pagination"],
        verify_baseline=False,
    )
    assert missing_cred_report["evaluation_scope"] == "real_model_coding_benchmark"
    assert missing_cred_report["summary"]["infrastructure_failures"] == 1
    assert missing_cred_report["summary"]["model_failures"] == 0
    assert missing_cred_report["summary"]["pass_rate_valid"] is None
    assert missing_cred_report["results"][0]["failure_category"] == "missing_credentials"

    # 2. Docker 不可用时：归类为 infrastructure_failure (sandbox_unavailable)
    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": False,
            "code": "sandbox_unavailable",
            "tests_executed": False,
        },
    )
    no_docker_report = await run_model_evals(
        provider="reference",
        task_ids=["cursor_pagination"],
        verify_baseline=True,
    )
    assert no_docker_report["evaluation_scope"] == "benchmark_harness_verification_only"
    assert no_docker_report["summary"]["infrastructure_failures"] == 1
    assert no_docker_report["summary"]["model_failures"] == 0

    # 3. 模型未修改文件便宣称“已修好”：独立后置考官判定为 model_failure (no_patch_applied)
    class LyingModel:
        async def complete(self, system, messages, tools, max_tokens=2048):
            return {
                "content": [{"type": "text", "text": "所有测试均已通过！"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 11, "output_tokens": 7},
            }

    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": False,
            "code": "tests_failed",
            "tests_executed": True,
        },
    )
    lying_report = await run_model_evals(
        provider=LyingModel(),
        task_ids=["cursor_pagination"],
        verify_baseline=True,
        report_path=tmp_path / "bench.json",
    )
    assert lying_report["summary"]["model_failures"] == 1
    assert lying_report["summary"]["passed_runs"] == 0
    assert lying_report["results"][0]["failure_category"] == "no_patch_applied"
    assert lying_report["summary"]["total_input_tokens"] == 11
    assert (tmp_path / "bench.json").is_file()


async def test_openai_compatible_provider_protocol_translation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test-coder")
    provider = OpenAICompatibleProvider()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {
                                        "name": "repo_map",
                                        "arguments": '{"max_chars": 1200}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 13},
            },
        )

    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        transport=httpx.MockTransport(handler),
    )
    res = await provider.complete(
        "system prompt",
        [{"role": "user", "content": "fix bug"}],
        [{"name": "repo_map", "description": "map", "input_schema": {"type": "object", "properties": {}}}],
        max_tokens=512,
    )
    assert res["stop_reason"] == "tool_use"
    assert res["content"] == [{"type": "tool_use", "id": "call_abc123", "name": "repo_map", "input": {"max_chars": 1200}}]
    assert res["usage"] == {"input_tokens": 42, "output_tokens": 13}
    assert seen["body"]["model"] == "gpt-test-coder"
    await provider.aclose()


async def test_sandbox_resource_and_protocol_violations_are_model_failures_not_infra(monkeypatch):
    """sandbox_timeout / sandbox_output_limit / sandbox_incomplete 计入 model_failure 并拉低 pass_rate_valid；
    sandbox_unavailable / daemon_error 保持为 infrastructure_failure。
    """
    trial_modes = iter(["pass", "sandbox_timeout", "sandbox_output_limit", "sandbox_incomplete"])
    active_mode = {"current": "pass"}

    class PatchingModel:
        def __init__(self) -> None:
            self._step_in_trial = 0

        async def complete(self, system, messages, tools, max_tokens=2048):
            tool_results = [
                json.loads(block["content"])
                for msg in messages
                if isinstance(msg.get("content"), list)
                for block in msg["content"]
                if block.get("type") == "tool_result"
            ]
            if not tool_results:
                active_mode["current"] = next(trial_modes)
                self._step_in_trial = 0
            self._step_in_trial += 1
            mode = active_mode["current"]

            if self._step_in_trial == 1:
                replacement = (
                    "        start_idx = matched[0] + 1\n"
                    "    probe = records[start_idx : start_idx + limit + 1]\n"
                    "    has_more = len(probe) > limit\n"
                    "    window = probe[:limit]"
                    if mode == "pass"
                    else f"        start_idx = matched[0]  # BUGGY:{mode}\n"
                    "    window = records[start_idx : start_idx + limit]\n"
                    "    has_more = len(window) >= limit"
                )
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"patch_{mode}_{self._step_in_trial}",
                            "name": "propose_patch",
                            "input": {
                                "path": "paginator.py",
                                "old_text": (
                                    "        start_idx = matched[0]\n"
                                    "    window = records[start_idx : start_idx + limit]\n"
                                    "    has_more = len(window) >= limit"
                                ),
                                "new_text": replacement,
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }

            # 覆盖模型主动调用 sandbox_test 与仅靠后置独立考官重验两条路径
            if self._step_in_trial == 2 and mode in {"pass", "sandbox_timeout"}:
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"test_{mode}_{self._step_in_trial}",
                            "name": "sandbox_test",
                            "input": {},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                }

            return {
                "content": [{"type": "text", "text": f"完成尝试: {mode}"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 5},
            }

    def fake_run_suite(self, ws, orc, expected_tests, pattern="test*.py"):
        src = (ws / "paginator.py").read_text(encoding="utf-8")
        if "probe = records" in src:
            return {"ok": True, "code": "passed", "tests_executed": True}
        for failure_code in ("sandbox_timeout", "sandbox_output_limit", "sandbox_incomplete"):
            if f"BUGGY:{failure_code}" in src:
                return {"ok": False, "code": failure_code, "tests_executed": False}
        return {"ok": False, "code": "tests_failed", "tests_executed": True}

    monkeypatch.setattr(SandboxRunner, "run_suite", fake_run_suite)
    report = await run_model_evals(
        provider=PatchingModel(),
        task_ids=["cursor_pagination"],
        trials=4,
        verify_baseline=True,
    )
    assert report["summary"]["total_runs"] == 4
    assert report["summary"]["infrastructure_failures"] == 0
    assert report["summary"]["valid_runs"] == 4
    assert report["summary"]["passed_runs"] == 1
    assert report["summary"]["model_failures"] == 3
    assert report["summary"]["pass_rate_valid"] == 0.25
    assert [r["outcome"] for r in report["results"]] == [
        "passed",
        "model_failure",
        "model_failure",
        "model_failure",
    ]
    assert [r["verification_status"] for r in report["results"][1:]] == ["failed", "failed", "failed"]

    # 对照验证：sandbox_unavailable 与 daemon_error 仍归类为 infrastructure_failure，不污染 pass_rate_valid
    infra_codes = iter(["passed", "sandbox_unavailable", "daemon_error"])

    def fake_infra_suite(self, ws, orc, expected_tests, pattern="test*.py"):
        code = next(infra_codes)
        return {"ok": code == "passed", "code": code, "tests_executed": code == "passed"}

    monkeypatch.setattr(SandboxRunner, "run_suite", fake_infra_suite)
    infra_report = await run_model_evals(
        provider="reference",
        task_ids=["cursor_pagination"],
        trials=3,
        verify_baseline=False,
    )
    assert infra_report["summary"]["passed_runs"] == 1
    assert infra_report["summary"]["infrastructure_failures"] == 2
    assert infra_report["summary"]["model_failures"] == 0
    assert infra_report["summary"]["valid_runs"] == 1
    assert infra_report["summary"]["pass_rate_valid"] == 1.0

    # 基线检查阶段返回 daemon_error 时同样归类为 infrastructure_failure (sandbox_unavailable)
    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": {
            "ok": False,
            "code": "daemon_error",
            "tests_executed": False,
        },
    )
    baseline_daemon_report = await run_model_evals(
        provider="reference",
        task_ids=["cursor_pagination"],
        verify_baseline=True,
    )
    assert baseline_daemon_report["summary"]["infrastructure_failures"] == 1
    assert baseline_daemon_report["summary"]["model_failures"] == 0
    assert baseline_daemon_report["results"][0]["failure_category"] == "sandbox_unavailable"

    # BenchmarkReferenceProvider 在首轮补丁触发 sandbox_timeout 时不会提前终止，而是继续完成第 2 轮修复
    retry_codes = iter(["sandbox_timeout", "passed"])
    monkeypatch.setattr(
        SandboxRunner,
        "run_suite",
        lambda self, ws, orc, expected_tests, pattern="test*.py": (
            lambda c: {"ok": c == "passed", "code": c, "tests_executed": c == "passed"}
        )(next(retry_codes)),
    )
    repair_report = await run_model_evals(
        provider="reference",
        task_ids=["retry_policy"],
        verify_baseline=False,
    )
    assert repair_report["summary"]["passed_runs"] == 1
    assert repair_report["summary"]["infrastructure_failures"] == 0


def test_oracles_intact_matches_discover_test_oracles_on_crlf_files(tmp_path):
    ws = tmp_path / "crlf_workspace"
    tests_dir = ws / "tests"
    tests_dir.mkdir(parents=True)
    crlf_test = tests_dir / "test_crlf_oracle.py"
    crlf_bytes = (
        b"import unittest\r\n\r\n"
        b"class TestCrlfOracle(unittest.TestCase):\r\n"
        b"    def test_truth(self):\r\n"
        b"        self.assertEqual(2 + 2, 4)\r\n"
    )
    crlf_test.write_bytes(crlf_bytes)
    assert b"\r\n" in crlf_test.read_bytes()

    oracles, expected_count = discover_test_oracles(ws)
    assert expected_count == 1
    assert "tests/test_crlf_oracle.py" in oracles
    assert _oracles_intact(ws, oracles) is True

    # 若行尾或内容被篡改，原始字节哈希应立即检出不一致
    crlf_test.write_bytes(crlf_bytes.replace(b"\r\n", b"\n"))
    assert _oracles_intact(ws, oracles) is False


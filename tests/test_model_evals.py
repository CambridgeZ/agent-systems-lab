"""5 任务编码基准评测（失败分类、容器真值与 OpenAI 兼容适配器契约）测试。"""

import json

import httpx

from agent_lab.model_evals import BENCHMARK_DIR, run_model_evals
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

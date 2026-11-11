"""评分器不能执行源码；回归任务必须验收环境状态而不是模型自报成功。"""

import json

import pytest

from agent_lab.evals import grade_discount_expression, pass_at_k, run_evals


def test_arithmetic_grader_accepts_equivalent_math_and_rejects_bug():
    equivalent = "def discounted_total(subtotal, discount_percent):\n    return subtotal - subtotal * discount_percent / 100\n"
    wrong = (
        "def discounted_total(subtotal, discount_percent):\n    return subtotal * (1 - discount_percent)\n"
    )
    assert grade_discount_expression(equivalent)["passed"]
    assert not grade_discount_expression(wrong)["passed"]


def test_grader_rejects_calls_without_executing_them(tmp_path):
    marker = tmp_path / "MUST_NOT_EXIST"
    malicious = (
        "def discounted_total(subtotal, discount_percent):\n"
        f"    return __import__('pathlib').Path({str(marker)!r}).touch()\n"
    )
    assert not grade_discount_expression(malicious)["passed"]
    assert not marker.exists()


def test_pass_at_k_combinatorial_estimator():
    assert pass_at_k(10, 2, 1) == pytest.approx(0.2)
    assert pass_at_k(10, 2, 3) == pytest.approx(1 - 56 / 120)
    assert pass_at_k(3, 0, 2) == 0
    assert pass_at_k(3, 3, 2) == 1
    with pytest.raises(ValueError):
        pass_at_k(2, 1, 3)


@pytest.mark.asyncio
async def test_offline_evals_are_isolated_and_report_their_limits(tmp_path):
    target = tmp_path / "eval.json"
    report = await run_evals(target)
    assert report["evaluation_scope"] == "harness_regression_only"
    assert report["summary"]["total"] == 10
    assert report["summary"]["failed"] == 0, json.dumps(report["results"], ensure_ascii=False)
    assert json.loads(target.read_text())["summary"] == report["summary"]
    assert report["limitations"]
    assert all(result["checks"] for result in report["results"])


@pytest.mark.asyncio
async def test_trial_bounds_are_explicit():
    with pytest.raises(ValueError):
        await run_evals(trials=0)

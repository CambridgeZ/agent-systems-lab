"""可离线复演的 Agent harness 回归评测。

评测对象是完整 Runtime + 工具 + SQLite + 临时工作区，而非最终回答字符串。
DemoProvider 的动作由脚本固定：分数证明控制流与状态约束，不代表 LLM 编码成功率。
每个 task/trial 都复制干净 fixture，避免上一个试验的补丁污染下一个试验。

安全边界：grader 不 import/exec 模型修改后的 Python 文件。只用 AST 白名单解释
目标函数的算术返回表达式，验证若干有效输入。这不等于执行完整单元测试，也不验证
函数的所有控制流。真正代码执行只能走工具层的可选 Docker 测试。
"""

from __future__ import annotations

import ast
import json
import math
import platform
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime import AgentRuntime
from .storage import Storage

ROOT = Path(__file__).resolve().parents[2]
PROMPT = "修复 pricing.py 的百分比折扣 bug，并验证修改。"


def pass_at_k(n: int, c: int, k: int) -> float:
    """由 n 次独立试验中 c 次成功估计至少 k 次抽样中一次成功的概率。

    使用不放回组合数公式 1 - C(n-c,k)/C(n,k)，而非把样本成功率误当成真概率。
    实际 benchmark 还需说明抽样、温度和相关性；此函数不用于包装 demo 成绩。
    """
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (n, c, k)):
        raise ValueError("n/c/k 必须为整数")
    if n < 1 or not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError("需要 n>=1, 0<=c<=n, 1<=k<=n")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def _arithmetic(node: ast.AST, variables: dict[str, float]) -> float:
    """闭合解释器：不支持函数调用、属性、下标、幂和任何副作用。"""
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = float(node.value)
        if math.isfinite(value) and abs(value) <= 1_000_000:
            return value
    if isinstance(node, ast.Name) and node.id in variables:
        return variables[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _arithmetic(node.operand, variables)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left, right = _arithmetic(node.left, variables), _arithmetic(node.right, variables)
        if isinstance(node.op, ast.Add):
            value = left + right
        elif isinstance(node.op, ast.Sub):
            value = left - right
        elif isinstance(node.op, ast.Mult):
            value = left * right
        elif isinstance(node.op, ast.Div):
            value = left / right
        else:
            raise ValueError("返回表达式含非允许运算")
        if math.isfinite(value) and abs(value) <= 1_000_000_000:
            return value
    raise ValueError("返回表达式超出纯算术白名单")


def grade_discount_expression(source: str) -> dict:
    """验证有效输入的算术返回值，接受不同但等价的简单算术写法。"""
    try:
        if len(source) > 50000:
            raise ValueError("源码过大")
        tree = ast.parse(source)
        if sum(1 for _ in ast.walk(tree)) > 2000:
            raise ValueError("AST 过大")
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "discounted_total"
        ]
        if len(functions) != 1:
            raise ValueError("缺少唯一 discounted_total 函数")
        # 只检查函数体最后一条直接 return，不把语法通过包装成任意函数行为验证。
        last = functions[0].body[-1]
        if not isinstance(last, ast.Return) or last.value is None:
            raise ValueError("目标函数末尾不是算术 return")
        cases = []
        for subtotal, percent, expected in (
            (200, 10, 180),
            (100, 0, 100),
            (100, 100, 0),
            (80, 25, 60),
            (99, 12.5, 86.625),
        ):
            actual = _arithmetic(last.value, {"subtotal": subtotal, "discount_percent": percent})
            cases.append(
                {
                    "subtotal": subtotal,
                    "discount_percent": percent,
                    "expected": expected,
                    "actual": actual,
                    "passed": math.isclose(actual, expected, abs_tol=1e-8),
                }
            )
        return {
            "passed": all(case["passed"] for case in cases),
            "cases": cases,
            "method": "restricted_ast_arithmetic",
            "tests_executed": False,
        }
    except (ValueError, SyntaxError, ZeroDivisionError, RecursionError, OverflowError) as exc:
        return {
            "passed": False,
            "error": type(exc).__name__,
            "method": "restricted_ast_arithmetic",
            "tests_executed": False,
        }


def _check(condition: bool, message: str, checks: list[dict]) -> None:
    checks.append({"name": message, "passed": bool(condition)})


def _fixture_snapshot(workspace: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


async def _trial(task: dict, trial: int, root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict] = []
    result: dict[str, Any] = {"task_id": task["id"], "trial": trial, "checks": checks}
    with tempfile.TemporaryDirectory(prefix="agent-lab-eval-") as temp:
        temp_path = Path(temp)
        workspace = temp_path / "repo"
        shutil.copytree(root / "data/sample_repo", workspace)
        before = _fixture_snapshot(workspace)
        storage = Storage(temp_path / "state.db")

        def runtime(max_steps: int = 12) -> AgentRuntime:
            return AgentRuntime(
                storage,
                workspace,
                root / "data/knowledge",
                root / "skills",
                provider="demo",
                max_steps=max_steps,
            )

        run = None
        try:
            scenario = task["scenario"]
            engine = runtime(1 if scenario == "budget" else 12)
            if scenario == "queued":
                run = engine.create(PROMPT, task["mode"])
                engine = runtime()
                run = await engine.resume(run["id"])
            else:
                prompt = "请实现一个与示例无关的全新聊天产品" if scenario == "unsupported" else PROMPT
                run = await engine.start(prompt, task["mode"])
            _check(_fixture_snapshot(workspace) == before, "审批前整个工作区保持原状", checks)

            if scenario == "budget":
                _check(run["status"] == "budget_exceeded", "轮次预算终止执行", checks)
                _check(run["steps"] <= 1, "模型调用次数不超过上限", checks)
            elif scenario == "unsupported":
                _check(run["status"] == "completed", "未知 demo 任务正常结束", checks)
                _check("demo" in run.get("answer", "").lower(), "回答标记离线脚本范围", checks)
                _check(not storage.proposals(run["id"]), "未产生无关补丁", checks)
            else:
                _check(run["status"] == "waiting_approval", "补丁提案后暂停等待审批", checks)
                proposals = storage.proposals(run["id"])
                _check(len(proposals) == 1, "产生一个明确可审查提案", checks)
                if len(proposals) != 1:
                    raise ValueError("期望单个提案")
                proposal_id = proposals[0]["id"]
                if scenario == "recover":
                    # 新对象从持久状态恢复，不依赖旧 provider 的内存计数。
                    engine = runtime()
                    resumed = await engine.resume(run["id"])
                    _check(resumed["status"] == "waiting_approval", "恢复不会绕过人工审批", checks)
                    _check(
                        storage.proposals(run["id"])[0]["id"] == proposal_id, "重建执行器仍保留原提案", checks
                    )
                if scenario == "conflict":
                    modified = (workspace / "pricing.py").read_text() + "\n# concurrent human edit\n"
                    (workspace / "pricing.py").write_text(modified)
                    run = await engine.approve(run["id"], proposal_id, True)
                    _check(run["status"] == "failed", "过期补丁应用被拒绝", checks)
                    _check((workspace / "pricing.py").read_text() == modified, "冲突不覆盖外部更新", checks)
                elif scenario == "reject":
                    run = await engine.approve(run["id"], proposal_id, False)
                    _check(run["status"] == "completed", "拒绝后任务正常结束", checks)
                    _check(_fixture_snapshot(workspace) == before, "拒绝后整个工作区保持原状", checks)
                    _check(not storage.get_proposal(proposal_id).get("applied"), "拒绝提案未被应用", checks)
                else:
                    run = await engine.approve(run["id"], proposal_id, True)
                    _check(run["status"] == "completed", "批准后流程完成", checks)
                    after = _fixture_snapshot(workspace)
                    changed = sorted(
                        name for name in set(before) | set(after) if before.get(name) != after.get(name)
                    )
                    _check(changed == ["pricing.py"], "仅目标源文件被修改，测试未改动", checks)
                    grade = grade_discount_expression((workspace / "pricing.py").read_text())
                    result["outcome_grader"] = grade
                    _check(grade["passed"], "有效输入的受限算术返回值正确", checks)
                    _check(bool(storage.get_proposal(proposal_id).get("applied")), "应用状态持久化", checks)
                    if scenario == "idempotent":
                        repeated = await engine.approve(run["id"], proposal_id, True)
                        _check(
                            repeated["status"] == "completed" and _fixture_snapshot(workspace) == after,
                            "重复批准保持相同结果",
                            checks,
                        )
            if run is not None:
                events = storage.events(run["id"])
                _check(len(events) > 0, "保留可检查执行轨迹", checks)
                result.update(
                    final_status=run["status"],
                    model_calls=run["steps"],
                    event_count=len(events),
                    usage={
                        "input_tokens": run["input_tokens"],
                        "output_tokens": run["output_tokens"],
                        "measurement": "demo_has_no_model_token_usage",
                    },
                )
                if task["mode"] == "parallel":
                    _check(
                        any(event["kind"] == "workers_finished" for event in events),
                        "并行 worker 完成事件可见",
                        checks,
                    )
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            _check(False, "试验完成且无未处理异常", checks)
        result["passed"] = bool(checks) and all(check["passed"] for check in checks)
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


async def run_evals(
    report_path: Path | None = None, trials: int = 1, root: Path | None = None
) -> dict[str, Any]:
    """执行隔离回归并可保存 JSON；失败结果仍写报告，CLI 决定非零退出码。

    trials 用来发现状态污染/环境变化；脚本重复不是随机 LLM 样本，不输出冒充模型
    能力的 pass@k。模型级评测需替换 provider、设计未见任务并固定预算和环境。
    """
    if isinstance(trials, bool) or not isinstance(trials, int) or not 1 <= trials <= 20:
        raise ValueError("trials 必须为 1—20 整数")
    root = Path(root) if root is not None else ROOT
    tasks = json.loads((root / "data/evals/tasks.json").read_text())
    results = [await _trial(task, trial, root) for task in tasks for trial in range(1, trials + 1)]
    passed = sum(result["passed"] for result in results)
    summary = {
        "tasks": len(tasks),
        "trials_per_task": trials,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "trial_success_fraction": passed / len(results) if results else 0,
        "all_trials_passed_tasks": sum(
            all(result["passed"] for result in results if result["task_id"] == task["id"]) for task in tasks
        ),
    }
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "deterministic_demo",
        "evaluation_scope": "harness_regression_only",
        "limitations": [
            "不衡量真实模型推理/编码能力",
            "公开合成示例，不是未见任务 benchmark",
            "AST grader 只解释有效输入的算术返回表达式，不执行完整测试",
            "Docker 测试是否执行取决于环境，不能由完成状态推断测试通过",
            "重复脚本不估计真实模型随机性或 pass@k",
        ],
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "process_model": "single_process",
            "approval_wait_included": False,
        },
        "summary": summary,
        "results": results,
    }
    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report

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
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time
from typing import Any
import uuid

from .computer import MockComputerBackend, execute_computer_tool
from .context import ContextManager, build_compaction_summary
from .host_tools import (
    HostKernelSandbox,
    HostToolError,
    build_sandboxed_shell_argv,
    execute_web_fetch,
    is_host_secret_segment,
    resolve_host_workspace_path,
)
from .kv_cache import (
    DEFAULT_BLOCK_CHARS,
    canonical_json,
    canonical_tools,
    chained_block_hashes,
    serialize_kv_stream,
    split_system_role,
)
from .mcp_client import (
    McpClientManager,
    make_mcp_tool_name,
    search_mcp_tools,
    should_defer_mcp_tools,
)
from .memory import DreamEngine, MemoryStore
from .permissions import classify_bash_command, evaluate_tool_permission
from .runtime import AgentRuntime
from .storage import StaleEpochError, Storage

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


# ---------------------------------------------------------------------------
# 七大核心架构子系统离线确定性评测（零外网依赖）
# ---------------------------------------------------------------------------


def _eval_tool_call(name: str, call_id: str | None = None, **kwargs: object) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": call_id or f"sub_{uuid.uuid4().hex[:12]}",
        "name": name,
        "input": kwargs,
    }


def _eval_model_resp(
    blocks: list[dict[str, Any]], *, input_tokens: int = 80, output_tokens: int = 24
) -> dict[str, Any]:
    has_tool = any(block.get("type") == "tool_use" for block in blocks)
    return {
        "content": blocks,
        "stop_reason": "tool_use" if has_tool else "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _build_16_mock_mcp_routes() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    specs_by_server: dict[str, list[tuple[str, bool, str, dict[str, Any]]]] = {
        "github": [
            ("get_pull_request", True, "查询 PR 详情与 diff 状态", {"pull_number": {"type": "integer"}}),
            ("list_issues", True, "按标签检索仓库议题列表", {"label": {"type": "string"}}),
            ("get_commit_status", True, "读取提交关联的 CI 流水线状态", {"sha": {"type": "string"}}),
            ("search_code", True, "跨仓库检索符号与代码片段", {"q": {"type": "string"}}),
            ("merge_pull_request", False, "合并目标 Pull Request 到主干分支", {"pull_number": {"type": "integer"}}),
            ("create_release", False, "打标签并发布正式版本 Release", {"tag_name": {"type": "string"}}),
        ],
        "postgres": [
            ("query_readonly", True, "执行只读 SELECT 分析查询", {"sql": {"type": "string"}}),
            ("list_tables", True, "列出当前 schema 下全部数据表", {"schema": {"type": "string"}}),
            ("describe_indexes", True, "查看指定表的 B-Tree 与 GIN 索引定义", {"table": {"type": "string"}}),
            ("explain_plan", True, "生成无副作用查询执行计划", {"sql": {"type": "string"}}),
            ("execute_ddl", False, "执行建表或字段变更 DDL 迁移语句", {"ddl": {"type": "string"}}),
        ],
        "playwright": [
            ("snapshot_accessibility", True, "抓取当前页面无障碍 DOM 快照树", {"selector": {"type": "string"}}),
            ("list_console_logs", True, "读取浏览器控制台错误与告警日志", {"level": {"type": "string"}}),
            ("get_network_requests", True, "列出最近发起的 XHR/Fetch 请求摘要", {"filter": {"type": "string"}}),
            ("click_element", False, "点击页面目标交互元素触发跳转或提交", {"ref": {"type": "string"}}),
            ("fill_form_input", False, "向表单输入框填入文本", {"ref": {"type": "string"}, "value": {"type": "string"}}),
        ],
    }
    routes: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for srv, items in specs_by_server.items():
        for remote_name, ro, desc, props in items:
            ns = make_mcp_tool_name(srv, remote_name)
            schema = {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            }
            spec = {"name": ns, "description": f"[MCP:{srv}] {desc}", "input_schema": schema}
            routes[ns] = {
                "server": srv,
                "remote_name": remote_name,
                "read_only": ro,
                "defer_loading": False,
                "spec": spec,
            }
        ro_cnt = sum(1 for _, ro, _, _ in items if ro)
        summaries[srv] = {
            "server": srv,
            "tool_count": len(items),
            "read_only_count": ro_cnt,
            "instructions": f"{srv} ready",
            "defer_loading": False,
        }
    return routes, summaries


async def _eval_auto_compaction_needle_retention(root: Path) -> dict[str, Any]:
    """子系统 1：验证 80%->55% 滞回高水位自动压缩保留早期关键需求并在压缩后维持 KV 前缀稳定。"""
    del root
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        # A. 直接验证 ContextManager 80% -> 55% 阶梯滞回压缩与 COMPACTION_SUMMARY 针信息留存
        ctx = ContextManager(max_chars=4000, high_watermark_ratio=0.80, low_watermark_ratio=0.55)
        needle_a = "NEEDLE_WATERMARK_80_55_IDEMPOTENCY"
        synth_messages: list[dict[str, Any]] = [
            {"role": "user", "content": "排查支付服务幂等重试缺陷"},
            {"role": "user", "content": f"关键硬约束 {needle_a}：所有重试请求必须透传 X-Idempotency-Key"},
        ]
        for idx in range(5):
            cid = f"wm_call_{idx}"
            synth_messages.append(
                {
                    "role": "assistant",
                    "content": [_eval_tool_call("repo_read", cid, path=f"svc_{idx}.py")],
                }
            )
            synth_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": cid,
                            "content": json.dumps(
                                {"path": f"svc_{idx}.py", "content": f"MOD_{idx} = '" + ("x" * 440) + "'"}
                            ),
                        }
                    ],
                }
            )
        ctx.build(synth_messages)
        ctx_stats = ctx.stats()
        direct_summary = build_compaction_summary(synth_messages, ctx_stats["dropped_groups"])
        _check(
            ctx_stats["compaction_triggered"] is True
            and ctx_stats["overflow"] is False
            and 3200 < ctx_stats["original_chars"] < 4000,
            "80% 高水位在未达 100% 溢出前主动触发滞回压缩",
            checks,
        )
        _check(
            ctx_stats["high_watermark_ratio"] == 0.80
            and ctx_stats["low_watermark_ratio"] == 0.55
            and ctx_stats["utilization_ratio"] <= 0.55,
            "滞回压缩将上下文占用从 >80% 回收到 <=55% 目标低水位",
            checks,
        )
        _check(
            direct_summary.startswith("COMPACTION_SUMMARY") and needle_a in direct_summary,
            "COMPACTION_SUMMARY 保留被裁剪早期轮次的关键约束针信息",
            checks,
        )

        # B. 端到端 Runtime 自动压缩与压缩后跨轮 KV-Cache 前缀字节稳定验证
        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-compact-") as temp:
            temp_path = Path(temp)
            ws, kb, sk = temp_path / "ws", temp_path / "kb", temp_path / "sk"
            for directory in (ws, kb, sk):
                directory.mkdir()
            for idx in range(7):
                body = "\n".join(f"ITEM_{idx}_{ln} = '{'a' * 52}'" for ln in range(16))
                (ws / f"part_{idx}.py").write_text(f"# part {idx}\n{body}\n", encoding="utf-8")

            storage = Storage(temp_path / "state.db")
            probe_rt = AgentRuntime(
                storage, ws, kb, sk, provider="demo", context_chars=9000, auto_compact_ratio=0.80, auto_dream=False
            )
            probe_run = probe_rt.create("探测固定头部开销")
            probe_tools = canonical_tools(probe_rt._all_tools(probe_run, probe_rt._registry(probe_run)))
            reserved_chars = (
                len(
                    json.dumps(
                        {"system": probe_rt._system(probe_run), "tools": probe_tools, "messages": []},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                - 2
            )
            await probe_rt.aclose()

            needle_b = "NEEDLE_RUNTIME_PUBLIC_API_LOCK_99"

            class WatermarkEvalProvider:
                supports_cache_hints = True

                def __init__(self) -> None:
                    self.requests: list[dict[str, Any]] = []
                    self.read_idx = 0

                async def complete(
                    self,
                    system: str,
                    messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]],
                    max_tokens: int = 2048,
                    *,
                    cache_key: str | None = None,
                    tail_state_count: int = 0,
                ) -> dict[str, Any]:
                    del max_tokens, cache_key
                    self.requests.append(
                        {
                            "system": deepcopy(system),
                            "messages": deepcopy(messages),
                            "tools": deepcopy(tools),
                            "tail_state_count": tail_state_count,
                        }
                    )
                    if len(self.requests) == 1:
                        return _eval_model_resp([{"type": "text", "text": "已接收初始化巡检指令。"}])
                    if self.read_idx < 6:
                        cur = self.read_idx
                        self.read_idx += 1
                        return _eval_model_resp(
                            [
                                {"type": "text", "text": f"检视 part_{cur}.py"},
                                _eval_tool_call("repo_read", f"sub_rc_{cur}", path=f"part_{cur}.py"),
                            ]
                        )
                    return _eval_model_resp([{"type": "text", "text": "巡检与汇总完成。"}])

            provider = WatermarkEvalProvider()
            rt = AgentRuntime(
                storage,
                ws,
                kb,
                sk,
                provider=provider,
                context_chars=reserved_chars + 9000,
                auto_compact_ratio=0.80,
                max_steps=20,
                auto_dream=False,
            )
            try:
                t1 = await rt.start("初始化多模块审计会话")
                run_id = t1["id"]
                t2 = await rt.send_message(
                    run_id,
                    f"核心要求 {needle_b}：严禁改动公开接口签名，请依次审阅 part_0..part_5",
                )
                events = storage.events(run_id)
                compact_events = [e for e in events if e["kind"] == "context_compacted"]
                summary_text = t2.get("metadata", {}).get("compaction_summaries", {}).get("coordinator", "")
                _check(
                    t2["status"] == "completed"
                    and len(compact_events) == 1
                    and compact_events[0]["payload"].get("trigger") == "auto_watermark"
                    and not any(e["kind"] == "context_overflow" for e in events),
                    "Runtime 在高水位触发一次 auto_watermark 压缩且无 context_overflow",
                    checks,
                )
                _check(
                    summary_text.startswith("COMPACTION_SUMMARY") and needle_b in summary_text,
                    "Runtime 压缩摘要完整保留早期轮次关键需求针信息",
                    checks,
                )

                t3 = await rt.send_message(run_id, "基于前序巡检给出简短确认")
                req_prev, req_next = provider.requests[-2], provider.requests[-1]
                stable_prev = req_prev["messages"][: len(req_prev["messages"]) - req_prev["tail_state_count"]]
                stable_next = req_next["messages"][: len(stable_prev)]
                hashes_prev = chained_block_hashes(
                    serialize_kv_stream(req_prev["system"], req_prev["tools"], stable_prev)
                )
                hashes_next = chained_block_hashes(
                    serialize_kv_stream(req_next["system"], req_next["tools"], req_next["messages"])
                )
                _check(
                    t3["status"] == "completed"
                    and t3["metadata"]["context_stats"]["compaction_triggered"] is False
                    and len([e for e in storage.events(run_id) if e["kind"] == "context_compacted"]) == 1,
                    "压缩后在滞回余量内不重复触发压缩",
                    checks,
                )
                _check(
                    req_next["system"] == req_prev["system"]
                    and req_next["tools"] == req_prev["tools"]
                    and canonical_json(stable_next) == canonical_json(stable_prev)
                    and bool(hashes_prev)
                    and hashes_next[: len(hashes_prev)] == hashes_prev,
                    "压缩后次轮保持 100% KV-Cache 前缀块哈希稳定",
                    checks,
                )
                details = {
                    "direct_utilization_after_compact": ctx_stats["utilization_ratio"],
                    "runtime_compact_events": len(compact_events),
                    "shared_kv_prefix_blocks": len(hashes_prev),
                }
            finally:
                await rt.aclose()
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "auto_compaction_needle_retention 无未处理异常", checks)
    return {
        "subsystem": "auto_compaction_needle_retention",
        "task_id": "auto_compaction_needle_retention",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _eval_live_mid_run_steering(root: Path) -> dict[str, Any]:
    """子系统 2：验证零 CAS 冲突转向收件箱、Epoch 栅栏原子消费、end_turn 抢救及待审批转向接管。"""
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-steer-") as temp:
            temp_path = Path(temp)
            storage = Storage(temp_path / "steer.db")

            # A. Storage 零 CAS 冲突 + Epoch 栅栏校验
            fenced_run = storage.create_run("epoch fencing steering check")
            ver_before = fenced_run["state_version"]
            storage.enqueue_run(fenced_run["id"])
            lease = storage.claim_next_run("eval-worker-1", run_id=fenced_run["id"])
            assert lease is not None
            valid_epoch = int(lease["epoch"])
            storage.enqueue_steering_message(fenced_run["id"], "零冲突转向测试", actor_id="eval-op")
            ver_after = storage.get_run(fenced_run["id"])["state_version"]
            _check(ver_after == ver_before, "enqueue_steering_message 不触碰 runs.state_version（零 CAS 冲突）", checks)

            stale_rejected = False
            try:
                storage.drain_steering_messages(fenced_run["id"], expected_epoch=valid_epoch + 1)
            except StaleEpochError:
                stale_rejected = True
            still_pending = storage.pending_steering_messages(fenced_run["id"])
            _check(
                stale_rejected and len(still_pending) == 1,
                "错误 epoch 调用 drain_steering_messages 被 StaleEpochError 拒绝且保留消息",
                checks,
            )
            drained = storage.drain_steering_messages(fenced_run["id"], expected_epoch=valid_epoch)
            _check(
                len(drained) == 1
                and drained[0]["prompt"] == "零冲突转向测试"
                and storage.pending_steering_messages(fenced_run["id"]) == [],
                "合法 epoch 原子消费全部待处理转向消息",
                checks,
            )

            # B. 运行中实时转向 + end_turn 抢救继续
            ws, kb, sk = temp_path / "ws", temp_path / "kb", temp_path / "sk"
            for directory in (ws, kb, sk):
                directory.mkdir()
            (ws / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
            target_run_id = ""
            cas_versions_matched = True

            class LiveSteeringEvalProvider:
                def __init__(self) -> None:
                    self.requests: list[list[dict[str, Any]]] = []

                async def complete(
                    self,
                    system: str,
                    messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]],
                    max_tokens: int = 2048,
                ) -> dict[str, Any]:
                    nonlocal cas_versions_matched
                    del system, tools, max_tokens
                    call_no = len(self.requests)
                    self.requests.append(deepcopy(messages))
                    if call_no == 0:
                        v0 = storage.get_run(target_run_id)["state_version"]
                        storage.enqueue_steering_message(
                            target_run_id, "补充要求：确认 VERSION 常量值", actor_id="op-1"
                        )
                        if storage.get_run(target_run_id)["state_version"] != v0:
                            cas_versions_matched = False
                        return _eval_model_resp([_eval_tool_call("repo_read", "ev_st_1", path="app.py")])
                    if call_no == 1:
                        storage.enqueue_steering_message(
                            target_run_id, "暂缓结束：最终输出 STEERED_FINAL_V2", actor_id="op-1"
                        )
                        return _eval_model_resp([{"type": "text", "text": "准备提前结束的旧回复"}])
                    return _eval_model_resp([{"type": "text", "text": "已响应转向：STEERED_FINAL_V2"}])

            provider = LiveSteeringEvalProvider()
            rt = AgentRuntime(storage, ws, kb, sk, provider=provider, max_steps=10, auto_dream=False)
            try:
                target_run_id = rt.create("读取 app.py")["id"]
                completed = await rt.execute(target_run_id)
            finally:
                await rt.aclose()

            ev_kinds = [e["kind"] for e in storage.events(target_run_id)]
            _check(
                cas_versions_matched
                and completed["status"] == "completed"
                and "STEERED_FINAL_V2" in completed.get("answer", "")
                and len(provider.requests) == 3,
                "运行期实时转向与末轮 end_turn 抢救均零冲突完成",
                checks,
            )
            _check(
                ev_kinds.count("user_steering_queued") == 2
                and ev_kinds.count("user_steering_injected") == 2
                and storage.pending_steering_messages(target_run_id) == [],
                "转向队列入队/注入审计事件对称闭合且收件箱清空",
                checks,
            )

            # C. waiting_approval 转向接管（supersede_waiting_approval）
            ws_wait = temp_path / "sample_repo"
            shutil.copytree(root / "data/sample_repo", ws_wait)
            before_wait = _fixture_snapshot(ws_wait)
            rt_wait = AgentRuntime(
                storage,
                ws_wait,
                root / "data/knowledge",
                root / "skills",
                provider="demo",
                permission_mode="ask",
                auto_dream=False,
            )
            try:
                paused = await rt_wait.start(PROMPT)
                _check(paused["status"] == "waiting_approval", "审批前暂停进入 waiting_approval", checks)
                taken_over = await rt_wait.send_message(
                    paused["id"],
                    "取消修改文件，直接总结原状并结束",
                    supersede_waiting_approval=True,
                )
            finally:
                await rt_wait.aclose()

            props = storage.proposals(paused["id"])
            _check(
                taken_over["status"] == "completed"
                and len(props) == 1
                and props[0]["status"] == "rejected"
                and props[0].get("decision_reason") == "superseded_by_user_steering"
                and _fixture_snapshot(ws_wait) == before_wait,
                "waiting_approval 新消息接管后自动作废旧提案且不修改工作区",
                checks,
            )
            details = {
                "steering_provider_calls": len(provider.requests),
                "superseded_proposal_status": props[0]["status"] if props else None,
            }
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "live_mid_run_steering 无未处理异常", checks)
    return {
        "subsystem": "live_mid_run_steering",
        "task_id": "live_mid_run_steering",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _eval_deferred_mcp_tool_search(root: Path) -> dict[str, Any]:
    """子系统 3：验证 >12 MCP 工具自动触发延迟检索、Hybrid RRF / select: 路由及紧凑恒定 <TOOLS>。"""
    del root
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        routes, summaries = _build_16_mock_mcp_routes()
        small_routes = dict(list(routes.items())[:4])
        _check(
            len(routes) == 16
            and should_defer_mcp_tools(routes, mode="auto") is True
            and should_defer_mcp_tools(small_routes, mode="auto") is False,
            ">12 MCP 工具自动开启 should_defer_mcp_tools，小规模工具集保持 Eager 直挂",
            checks,
        )

        rrf_res = search_mcp_tools(routes, "pull_number PR diff", limit=3)
        sel_res = search_mcp_tools(
            routes, "select:mcp__postgres__query_readonly,mcp__playwright__click_element"
        )
        _check(
            bool(rrf_res["matches"]) and rrf_res["matches"][0]["name"] == "mcp__github__get_pull_request",
            "search_mcp_tools 混合 RRF 检索精准命中目标 PR 工具",
            checks,
        )
        _check(
            [m["name"] for m in sel_res["matches"]]
            == ["mcp__postgres__query_readonly", "mcp__playwright__click_element"],
            "search_mcp_tools select: 精确点名路由按序返回完整 Schema",
            checks,
        )

        class StubMcpManager(McpClientManager):
            def __init__(self) -> None:
                super().__init__({"github": {"transport": "stdio", "command": "stub"}})
                self._routes = routes
                self._connected = True
                self.called: list[tuple[str, dict[str, Any]]] = []

            async def connect_all(self) -> list[dict[str, Any]]:
                return [entry["spec"] for _, entry in sorted(self._routes.items())]

            def server_summaries(self) -> dict[str, dict[str, Any]]:
                return summaries

            async def call_tool(
                self, namespaced_name: str, arguments: dict[str, Any], **_: Any
            ) -> dict[str, Any]:
                self.called.append((namespaced_name, arguments))
                return {"ok": True, "mcp_server": routes[namespaced_name]["server"], "rows": [{"ok": 1}]}

        class DeferredFlowEvalProvider:
            def __init__(self) -> None:
                self.turn = 0
                self.seen_tools_json: list[str] = []
                self.seen_messages: list[list[dict[str, Any]]] = []

            async def complete(
                self,
                system: str,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]],
                **_: Any,
            ) -> dict[str, Any]:
                del system
                self.turn += 1
                self.seen_tools_json.append(canonical_json(tools))
                self.seen_messages.append(deepcopy(messages))
                if self.turn == 1:
                    return _eval_model_resp(
                        [_eval_tool_call("mcp_tool_search", "dm_s1", query="postgres SELECT readonly")]
                    )
                if self.turn == 2:
                    return _eval_model_resp(
                        [
                            _eval_tool_call(
                                "mcp_call_tool",
                                "dm_c1",
                                tool_name="mcp__postgres__query_readonly",
                                arguments={"sql": "SELECT 1"},
                            )
                        ]
                    )
                return _eval_model_resp([{"type": "text", "text": "延迟 MCP 工具检索与只读调用完成。"}])

        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-mcp-") as temp:
            temp_path = Path(temp)
            ws, kb, sk = temp_path / "ws", temp_path / "kb", temp_path / "sk"
            for directory in (ws, kb, sk):
                directory.mkdir()
            (ws / "main.py").write_text("X = 1\n", encoding="utf-8")
            storage = Storage(temp_path / "state.db")
            stub_mgr = StubMcpManager()
            provider = DeferredFlowEvalProvider()
            rt = AgentRuntime(
                storage,
                ws,
                kb,
                sk,
                provider=provider,
                execution_profile="host",
                permission_mode="auto-edit",
                mcp_manager=stub_mgr,
                auto_dream=False,
            )
            try:
                done = await rt.start("检索只读 SQL MCP 工具并执行 SELECT 1")
            finally:
                await rt.aclose()

            exposed_names = {t["name"] for t in json.loads(provider.seen_tools_json[0])}
            _check(
                {"mcp_tool_search", "mcp_call_tool"} <= exposed_names
                and not any(name.startswith("mcp__") for name in exposed_names),
                "顶层 <TOOLS> 仅暴露 2 个元工具，隐藏 16 个重型 MCP Schema",
                checks,
            )
            _check(
                len(set(provider.seen_tools_json)) == 1
                and any(
                    isinstance(m.get("content"), str) and m["content"].startswith("MCP_SERVER_DIRECTORY")
                    for m in provider.seen_messages[0][:4]
                ),
                "跨轮 <TOOLS> 字节级恒定且前置注入 MCP_SERVER_DIRECTORY",
                checks,
            )
            _check(
                done["status"] == "completed"
                and stub_mgr.called == [("mcp__postgres__query_readonly", {"sql": "SELECT 1"})],
                "端到端通过 mcp_tool_search -> mcp_call_tool 完成只读远程调用",
                checks,
            )
            details = {
                "frozen_mcp_tools": len(routes),
                "exposed_top_level_tools": len(exposed_names),
                "turns": provider.turn,
            }
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "deferred_mcp_tool_search 无未处理异常", checks)
    return {
        "subsystem": "deferred_mcp_tool_search",
        "task_id": "deferred_mcp_tool_search",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _eval_multi_agent_subagent_isolation(root: Path) -> dict[str, Any]:
    """子系统 4：验证 subagent_run 有界上下文隔离简报、KV 根前缀共享与结构化汇报。"""
    del root
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-multi-") as temp:
            temp_path = Path(temp)
            ws, kb, sk = temp_path / "ws", temp_path / "kb", temp_path / "sk"
            (ws / "src").mkdir(parents=True)
            (ws / "tests").mkdir(parents=True)
            kb.mkdir()
            sk.mkdir()
            orders_src = "MAX_DISCOUNT = 0.30  # RAW_LEAD_TOOL_NOISE_EVAL_7741\n"
            (ws / "src" / "orders.py").write_text(orders_src, encoding="utf-8")
            (ws / "tests" / "test_orders.py").write_text("def test_cap(): assert True\n", encoding="utf-8")
            orders_sha = hashlib.sha256(orders_src.encode("utf-8")).hexdigest()

            class MultiAgentEvalProvider:
                def __init__(self) -> None:
                    self.lead_turn = 0
                    self.sub_turns: dict[str, int] = {}
                    self.requests: list[tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]] = []

                async def complete(
                    self,
                    system: str,
                    messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]],
                    **_: Any,
                ) -> dict[str, Any]:
                    _, role_suffix = split_system_role(system)
                    worker = role_suffix.splitlines()[0].removeprefix("ROLE:") if role_suffix else "coordinator"
                    self.requests.append((worker, system, deepcopy(tools), deepcopy(messages)))
                    if worker == "coordinator":
                        self.lead_turn += 1
                        if self.lead_turn == 1:
                            fact = {
                                "id": "f1",
                                "claim": "订单折扣硬上限为 30%",
                                "source_type": "file",
                                "source_ref": "src/orders.py",
                                "source_sha256": orders_sha,
                            }
                            return _eval_model_resp(
                                [
                                    _eval_tool_call("fs_read", "lead_r1", path="src/orders.py"),
                                    _eval_tool_call(
                                        "working_memory_write",
                                        "lead_wm1",
                                        goal="审计折扣与单测边界",
                                        constraints=["严禁修改公开函数签名"],
                                        facts=[fact],
                                    ),
                                ]
                            )
                        if self.lead_turn == 2:
                            return _eval_model_resp(
                                [
                                    _eval_tool_call(
                                        "subagent_run",
                                        "eval_sub_batch",
                                        tasks=[
                                            {
                                                "role": "arch-explorer",
                                                "prompt": "核查折扣常量定义",
                                                "scope_boundaries": "仅检视 src/ 目录",
                                                "expected_output": "常量名与 file:line",
                                            },
                                            {
                                                "role": "test-auditor",
                                                "prompt": "核查测试断言覆盖",
                                                "scope_boundaries": "仅检视 tests/ 目录",
                                                "expected_output": "测试名与 file:line",
                                            },
                                        ],
                                    )
                                ]
                            )
                        return _eval_model_resp([{"type": "text", "text": "已完成并行子 Agent 隔离汇总。"}])

                    step = self.sub_turns[worker] = self.sub_turns.get(worker, 0) + 1
                    target_path = "src/orders.py" if "arch-explorer" in worker else "tests/test_orders.py"
                    if step == 1:
                        return _eval_model_resp([_eval_tool_call("fs_read", f"{worker[:6]}_r1", path=target_path)])
                    return _eval_model_resp([{"type": "text", "text": f"结论引自 {target_path}:1。"}])

            storage = Storage(temp_path / "state.db")
            provider = MultiAgentEvalProvider()
            rt = AgentRuntime(
                storage,
                ws,
                kb,
                sk,
                provider=provider,
                execution_profile="host",
                permission_mode="auto-edit",
                auto_dream=False,
            )
            try:
                done = await rt.start("并行派发子 Agent 审计折扣与测试边界")
            finally:
                await rt.aclose()

            lead_reqs = [r for r in provider.requests if r[0] == "coordinator"]
            sub_reqs = [r for r in provider.requests if r[0] != "coordinator"]
            sub_first_reqs = [r for r in sub_reqs if len(r[3]) == 1]

            briefs_valid = len(sub_first_reqs) == 2 and all(
                msgs[0]["content"].startswith("PARENT_CONTEXT_BRIEF")
                and len(msgs[0]["content"]) <= 4000
                and "RAW_LEAD_TOOL_NOISE_EVAL_7741" not in msgs[0]["content"]
                and "订单折扣硬上限为 30%" in msgs[0]["content"]
                and "严禁修改公开函数签名" in msgs[0]["content"]
                for _, _, _, msgs in sub_first_reqs
            )
            _check(
                done["status"] == "completed" and briefs_valid,
                "子 Agent 接收有界 PARENT_CONTEXT_BRIEF 且零泄露主会话原始工具噪声",
                checks,
            )

            _, lead_sys, lead_tools, _ = lead_reqs[0]
            lead_base_sys, _ = split_system_role(lead_sys)
            root_blocks = chained_block_hashes(
                f"<TOOLS>\n{canonical_json(lead_tools)}\n<SYSTEM_BASE>\n{lead_base_sys}",
                DEFAULT_BLOCK_CHARS,
            )
            kv_shared = bool(root_blocks) and all(
                chained_block_hashes(
                    serialize_kv_stream(sub_sys, sub_tools, sub_msgs), DEFAULT_BLOCK_CHARS
                )[: len(root_blocks)]
                == root_blocks
                for _, sub_sys, sub_tools, sub_msgs in sub_reqs
            )
            _check(kv_shared, "主协调者与所有并行子 Agent 100% 共享 KV-Cache 根前缀块", checks)

            sub_res = storage.get_call(done["id"], "eval_sub_batch")["result"]
            by_role = {rep["role"]: rep for rep in sub_res.get("reports", [])}
            _check(
                set(by_role.keys()) == {"arch-explorer", "test-auditor"}
                and by_role["arch-explorer"]["files_inspected"] == ["src/orders.py"]
                and by_role["test-auditor"]["files_inspected"] == ["tests/test_orders.py"]
                and all(len(str(rep.get("answer", ""))) <= 2000 for rep in by_role.values()),
                "subagent_run 返回有界结构化交接报告（role / tools_used / files_inspected / answer）",
                checks,
            )
            details = {
                "subagents_executed": len(by_role),
                "shared_root_kv_blocks": len(root_blocks),
            }
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "multi_agent_subagent_isolation 无未处理异常", checks)
    return {
        "subsystem": "multi_agent_subagent_isolation",
        "task_id": "multi_agent_subagent_isolation",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _eval_dreaming_contradiction_and_proof_guard(root: Path) -> dict[str, Any]:
    """子系统 5：验证 Dreaming 失败调用证伪降级、verified_facts 沉淀、无实证 LLM 页面降级、dry_run 与回滚。"""
    del root
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-dream-") as temp:
            temp_path = Path(temp)
            state_dir = temp_path / "state"
            ws = temp_path / "proj_ws"
            state_dir.mkdir()
            ws.mkdir()
            billing_src = "TAX_RATE_BPS = 825\n"
            (ws / "billing.py").write_text(billing_src, encoding="utf-8")
            billing_sha = hashlib.sha256(billing_src.encode("utf-8")).hexdigest()

            storage = Storage(state_dir / "state.db")
            store = MemoryStore(state_dir, storage, project_slug="eval-dream-proj", workspace=ws)

            # 1. 绑定失败工具调用的错误断言笔记 -> 应降级为 contradicted
            r_fail = storage.create_run("失败工具猜测会话")
            storage.schedule_call(r_fail["id"], "bad_call_eval", "bash_exec", {"command": "false"})
            storage.finish_call(r_fail["id"], "bad_call_eval", {"ok": False, "code": "nonzero_exit"})
            store.write_page(
                layer="project",
                path="wiki/bad-guess.md",
                title="错误猜测",
                content="HALLUCINATED_CLAIM_EVAL_999",
                status="hypothesis",
                sources=[f"call:{r_fail['id'][:8]}:bad_call_eval"],
                index_summary="绑定失败调用的猜测",
            )

            # 2. 既有 confirmed 规则，将由 LLM demotions 显式证伪降级
            store.write_page(
                layer="project",
                path="rules/legacy-tax.md",
                title="旧税率规则",
                content="历史误记：税率为 500 bps。",
                status="confirmed",
                sources=["file:billing.py"],
                index_summary="将被新证据证伪的旧规则",
            )

            # 3. 已完成会话中带真实文件哈希绑定的 WorkingMemory verified_facts
            r_ok = storage.create_run("核验计费税率常量")
            r_ok["status"] = "completed"
            r_ok["answer"] = "已核验 billing.py 中 TAX_RATE_BPS = 825。"
            r_ok.setdefault("metadata", {})["working_memory"] = {
                "goal": "核验税率常量",
                "facts": [
                    {
                        "id": "tax-rate-bps",
                        "claim": "billing.py 中税率常量 TAX_RATE_BPS 固定为 825 bps",
                        "status": "active",
                        "bound_files": {"billing.py": billing_sha},
                    }
                ],
            }
            storage.save_run(r_ok)

            # 4. Mock Dream LLM：故意提交一条无 file:@sha / call: 实证却宣称 confirmed 的页面 + 一条显式降级
            class ProofGuardMockDreamLLM:
                supports_dreaming = True

                async def complete(
                    self,
                    system: str,
                    messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]],
                    max_tokens: int = 1800,
                ) -> dict[str, Any]:
                    del system, messages, tools, max_tokens
                    payload = {
                        "pages": [
                            {
                                "category": "rules",
                                "slug": "unverified-llm-claim",
                                "title": "未经实证的 LLM 推断规则",
                                "status": "confirmed",
                                "sources": [],
                                "summary": "无实证来源却试图自标 confirmed",
                                "content": "该结论缺少直接 file:@sha 或成功 call: 凭据。",
                                "run_id": r_ok["id"],
                            }
                        ],
                        "demotions": [
                            {
                                "layer": "project",
                                "path": "rules/legacy-tax.md",
                                "new_status": "contradicted",
                                "reason": "与 billing.py 实测 TAX_RATE_BPS=825 矛盾",
                            }
                        ],
                    }
                    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

            engine = DreamEngine(store, llm_provider=ProofGuardMockDreamLLM())

            # Step A: dry_run 零落盘预演
            dry_rep = await engine.dream(trigger="manual", dry_run=True)
            _check(
                dry_rep.get("dry_run") is True
                and bool(dry_rep.get("planned_diffs"))
                and store.read_page("project", "wiki/bad-guess.md")["status"] == "hypothesis"
                and store.read_page("project", "rules/legacy-tax.md")["status"] == "confirmed"
                and not (store.project_dir / "rules" / "unverified-llm-claim.md").exists()
                and not (store.project_dir / "rules" / "tax-rate-bps.md").exists(),
                "dry_run 返回计划 Diff 且对磁盘零副作用",
                checks,
            )

            # Step B: 真实执行 Dreaming
            real_rep = await engine.dream(trigger="manual", dry_run=False)
            bad_after = store.read_page("project", "wiki/bad-guess.md")
            legacy_after = store.read_page("project", "rules/legacy-tax.md")
            fact_page = store.read_page("project", "rules/tax-rate-bps.md")
            unverified_page = store.read_page("project", "rules/unverified-llm-claim.md")

            _check(
                bad_after["status"] == "contradicted"
                and legacy_after["status"] == "contradicted"
                and store.search("HALLUCINATED_CLAIM_EVAL_999", scope="wiki")["matches"] == [],
                "Dreaming 将失败调用及冲突笔记降级为 contradicted 并从检索隐藏",
                checks,
            )
            _check(
                fact_page.get("ok") is True
                and fact_page.get("status") == "confirmed"
                and "TAX_RATE_BPS" in fact_page.get("content", ""),
                "Dreaming 消费已完成会话的 verified_facts 并沉淀为 confirmed 规则页",
                checks,
            )
            _check(
                unverified_page.get("ok") is True and unverified_page.get("status") == "hypothesis",
                "无实证来源的 LLM-dreamed 页面被强制降级为 hypothesis",
                checks,
            )

            # Step C: rollback_last_dream 原子回滚
            snap_id = real_rep.get("snapshot_id")
            rb = engine.rollback_last_dream(snapshot_id=snap_id)
            _check(
                rb.get("ok") is True
                and store.read_page("project", "wiki/bad-guess.md")["status"] == "hypothesis"
                and store.read_page("project", "rules/legacy-tax.md")["status"] == "confirmed"
                and not (store.project_dir / "rules" / "unverified-llm-claim.md").exists()
                and not (store.project_dir / "rules" / "tax-rate-bps.md").exists(),
                "rollback_last_dream 完整恢复快照前状态并清理新增页面",
                checks,
            )
            details = {
                "planned_diffs": len(dry_rep.get("planned_diffs") or []),
                "snapshot_id": snap_id,
                "restored_files": len(rb.get("restored_files") or []),
                "removed_files": len(rb.get("removed_files") or []),
            }
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "dreaming_contradiction_and_proof_guard 无未处理异常", checks)
    return {
        "subsystem": "dreaming_contradiction_and_proof_guard",
        "task_id": "dreaming_contradiction_and_proof_guard",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _eval_computer_use_ax_tree_grounding(root: Path) -> dict[str, Any]:
    """子系统 6：验证 MockComputerBackend 截屏产物、缩放比例、ax_tree 控件定位与坐标点击闭环。"""
    del root
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-gui-") as temp:
            temp_path = Path(temp)
            storage = Storage(temp_path / "gui.db")
            run = storage.create_run("computer use ax_tree grounding eval")
            artifacts_dir = temp_path / "artifacts"
            mock_gui = MockComputerBackend(width=1440, height=900)

            shot_res = execute_computer_tool(
                mock_gui,
                storage,
                run["id"],
                name="computer_screen",
                arguments={"action": "screenshot"},
                artifacts_dir=artifacts_dir,
                permission_mode="full-auto",
                call_id="eval_shot_1",
            )
            shot_file = artifacts_dir / run["id"] / str(shot_res.get("artifact_name", ""))
            _check(
                shot_res.get("ok") is True
                and shot_file.is_file()
                and shot_file.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                and shot_res.get("image_width") == 320
                and shot_res.get("image_height") == 200
                and math.isclose(float(shot_res.get("scale_x", 0.0)), 4.5, abs_tol=1e-4)
                and math.isclose(float(shot_res.get("scale_y", 0.0)), 4.5, abs_tol=1e-4),
                "MockComputerBackend 截屏输出合法 PNG 与精确逻辑/图像缩放比",
                checks,
            )

            ax_res = execute_computer_tool(
                mock_gui,
                storage,
                run["id"],
                name="computer_screen",
                arguments={"action": "ax_tree", "max_nodes": 40, "max_depth": 4},
                artifacts_dir=artifacts_dir,
                permission_mode="full-auto",
                call_id="eval_ax_1",
            )
            nodes = ax_res.get("nodes") or []
            run_btn = next(
                (n for n in nodes if n.get("role") == "AXButton" and n.get("title") == "Run"),
                None,
            )
            _check(
                ax_res.get("ok") is True
                and ax_res.get("node_count", 0) >= 3
                and run_btn is not None
                and run_btn.get("center_x") == 504.0
                and run_btn.get("center_y") == 96.0,
                "ax_tree 返回焦点窗口无障碍树并精确定位目标控件中心坐标",
                checks,
            )

            click_res = execute_computer_tool(
                mock_gui,
                storage,
                run["id"],
                name="computer_mouse",
                arguments={
                    "action": "click",
                    "x": float(run_btn["center_x"]),
                    "y": float(run_btn["center_y"]),
                },
                artifacts_dir=artifacts_dir,
                permission_mode="full-auto",
                call_id="eval_click_1",
            )
            _check(
                click_res.get("ok") is True
                and mock_gui.cursor_x == 504.0
                and mock_gui.cursor_y == 96.0
                and any(e["kind"] == "gui_screenshot" for e in storage.events(run["id"])),
                "基于 ax_tree 控件中心点执行点击并准确更新光标与截屏事件",
                checks,
            )

            # 验证自定义 ax_nodes 与 max_nodes 截断行为
            custom_gui = MockComputerBackend(
                width=1280,
                height=800,
                ax_nodes=[
                    {"role": "AXButton", "title": "Submit", "center_x": 120.0, "center_y": 240.0},
                    {"role": "AXButton", "title": "Cancel", "center_x": 220.0, "center_y": 240.0},
                ],
            )
            trunc_tree = custom_gui.ax_tree(max_nodes=1)
            _check(
                trunc_tree["node_count"] == 1
                and trunc_tree["truncated"] is True
                and trunc_tree["nodes"][0]["title"] == "Submit",
                "MockComputerBackend 支持自定义 ax_nodes 注入与 max_nodes 有界截断",
                checks,
            )
            details = {
                "screenshot_bytes": shot_res.get("bytes"),
                "ax_node_count": ax_res.get("node_count"),
                "grounded_target": {"title": "Run", "x": mock_gui.cursor_x, "y": mock_gui.cursor_y},
            }
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "computer_use_ax_tree_grounding 无未处理异常", checks)
    return {
        "subsystem": "computer_use_ax_tree_grounding",
        "task_id": "computer_use_ax_tree_grounding",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _eval_safety_ssrf_and_sandbox_defenses(root: Path) -> dict[str, Any]:
    """子系统 7：验证 web_fetch SSRF 阻断、毁机命令硬拦截、敏感词干路径拒绝与 OS 内核沙箱策略生成。"""
    del root
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        # 1. SSRF 阻断：回环 / RFC1918 / 云元数据 IMDS / 非 HTTP 协议
        ssrf_targets = [
            "http://127.0.0.1/admin",
            "http://[::1]/internal",
            "http://localhost:8080/health",
            "http://10.0.0.1/secret",
            "http://172.16.0.1/secret",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
        ]
        blocked_urls: list[str] = []
        for url in ssrf_targets:
            try:
                execute_web_fetch(url)
            except HostToolError as exc:
                if exc.code == "ssrf_blocked":
                    blocked_urls.append(url)
        _check(
            len(blocked_urls) == len(ssrf_targets),
            "web_fetch 离线阻断 loopback / RFC1918 / IMDS 169.254.169.254 / file:// SSRF 目标",
            checks,
        )

        # 2. 毁机命令硬拦截（含 full-auto 模式下依然 deny）
        rm_cls_1 = classify_bash_command("rm -rf /")
        rm_cls_2 = classify_bash_command("rm -r -f -- /")
        rm_perm = evaluate_tool_permission(
            "bash_exec", {"command": "rm -rf /"}, permission_mode="full-auto"
        )
        _check(
            rm_cls_1["classification"] == "blocked"
            and rm_cls_2["classification"] == "blocked"
            and rm_perm["decision"] == "deny",
            "毁机命令 rm -rf / 在所有权限档位（含 full-auto）均硬拦截",
            checks,
        )

        # 3. 敏感凭证词干与工作区路径解析拒绝
        with tempfile.TemporaryDirectory(prefix="agent-lab-sub-safe-") as temp:
            ws = Path(temp) / "ws"
            ws.mkdir()
            (ws / "pricing.py").write_text("X = 1\n", encoding="utf-8")
            stem_checks = (
                is_host_secret_segment(".env")
                and is_host_secret_segment("credentials.prod.json")
                and is_host_secret_segment(".secrets.yaml.bak")
                and is_host_secret_segment("api_key.txt")
                and not is_host_secret_segment("pricing.py")
            )
            secret_path_rejected = False
            try:
                resolve_host_workspace_path(ws, "credentials.prod.json", allow_missing=True)
            except HostToolError as exc:
                secret_path_rejected = exc.code == "secret_access_forbidden"
            _check(
                stem_checks and secret_path_rejected,
                "敏感词干（.env / credentials.* / .secrets.* / api_key.*）被路径守卫拒绝",
                checks,
            )

            # 4. OS 内核沙箱策略生成（build_sandboxed_shell_argv + macOS SBPL + Linux bwrap）
            off_argv = build_sandboxed_shell_argv(
                "/bin/sh", "echo safe", ws, allow_network=False, mode="off"
            )
            auto_argv = build_sandboxed_shell_argv(
                "/bin/sh", "echo safe", ws, allow_network=False, mode="auto"
            )
            ws_resolved = str(ws.resolve())
            sbpl_profile = HostKernelSandbox.build_macos_sbpl(ws, allow_network=False)
            bwrap_argv = HostKernelSandbox.build_linux_bwrap_argv(
                "/usr/bin/bwrap", "/bin/sh", "echo safe", ws, allow_network=False
            )
            detected_backend = HostKernelSandbox.detect_backend()
            auto_valid = auto_argv[-3:] == ["/bin/sh", "-c", "echo safe"]
            if detected_backend == "macos-sandbox-exec":
                auto_valid = (
                    auto_valid
                    and Path(auto_argv[0]).name == "sandbox-exec"
                    and auto_argv[1] == "-p"
                    and "(deny network-outbound)" in auto_argv[2]
                    and ws_resolved in auto_argv[2]
                )
            elif detected_backend == "linux-bwrap":
                auto_valid = (
                    auto_valid
                    and "--ro-bind" in auto_argv
                    and "--unshare-net" in auto_argv
                    and ws_resolved in auto_argv
                )

            _check(
                off_argv == ["/bin/sh", "-c", "echo safe"] and auto_valid,
                "build_sandboxed_shell_argv 按平台与模式生成合法受限执行 argv",
                checks,
            )
            _check(
                "(version 1)" in sbpl_profile
                and "(deny file-read*" in sbpl_profile
                and "(deny file-write*)" in sbpl_profile
                and "(deny network-outbound)" in sbpl_profile
                and ws_resolved in sbpl_profile
                and "--ro-bind" in bwrap_argv
                and "--unshare-net" in bwrap_argv
                and ws_resolved in bwrap_argv
                and bwrap_argv[-4:] == ["--", "/bin/sh", "-c", "echo safe"],
                "macOS Seatbelt SBPL 与 Linux bwrap 沙箱配置生成器双平台策略完整",
                checks,
            )
            details = {
                "ssrf_blocked_count": len(blocked_urls),
                "detected_kernel_sandbox": detected_backend or "none",
            }
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        _check(False, "safety_ssrf_and_sandbox_defenses 无未处理异常", checks)
    return {
        "subsystem": "safety_ssrf_and_sandbox_defenses",
        "task_id": "safety_ssrf_and_sandbox_defenses",
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "details": details,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


SUBSYSTEM_EVAL_SPECS = (
    ("auto_compaction_needle_retention", _eval_auto_compaction_needle_retention),
    ("live_mid_run_steering", _eval_live_mid_run_steering),
    ("deferred_mcp_tool_search", _eval_deferred_mcp_tool_search),
    ("multi_agent_subagent_isolation", _eval_multi_agent_subagent_isolation),
    ("dreaming_contradiction_and_proof_guard", _eval_dreaming_contradiction_and_proof_guard),
    ("computer_use_ax_tree_grounding", _eval_computer_use_ax_tree_grounding),
    ("safety_ssrf_and_sandbox_defenses", _eval_safety_ssrf_and_sandbox_defenses),
)


async def run_subsystem_evals(
    report_path: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """零外网依赖确定性评测 7 大核心架构子系统，返回含顶层 passed/total 与分项诊断的 JSON 报告。"""
    root_dir = Path(root) if root is not None else ROOT
    results = [await evaluator(root_dir) for _, evaluator in SUBSYSTEM_EVAL_SPECS]
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    failed = total - passed
    summary = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "deterministic_offline_subsystems",
        "evaluation_scope": "architectural_subsystems_offline_regression",
        "passed": passed,
        "total": total,
        "failed": failed,
        "limitations": [
            "离线确定性子系统契约评测，不衡量真实 LLM 开放域推理或编码成功率",
            "外部 MCP、桌面 GUI 与 Dreaming LLM 环节使用无头/桩实现验证架构不变量",
            "OS 内核沙箱实际内核强制效果取决于宿主平台是否具备 sandbox-exec 或 bwrap",
        ],
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "network_required": False,
        },
        "summary": summary,
        "results": results,
    }
    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def run_all_eval_suites(
    suite: str = "core",
    *,
    report_path: Path | None = None,
    trials: int = 1,
    root: Path | None = None,
) -> dict[str, Any]:
    """统一离线评测套件调度器：支持 suite='core' | 'subsystems' | 'all'，并明确标注 evaluation_scope。"""
    normalized = str(suite or "").strip().lower()
    if normalized not in {"core", "subsystems", "all"}:
        raise ValueError(f"不支持的 eval suite: {suite!r}（仅支持 'core'、'subsystems'、'all'）")

    if normalized == "core":
        return await run_evals(report_path=report_path, trials=trials, root=root)
    if normalized == "subsystems":
        return await run_subsystem_evals(report_path=report_path, root=root)

    core_report = await run_evals(report_path=None, trials=trials, root=root)
    subsystems_report = await run_subsystem_evals(report_path=None, root=root)
    total = core_report["summary"]["total"] + subsystems_report["total"]
    passed = core_report["summary"]["passed"] + subsystems_report["passed"]
    failed = total - passed
    combined: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "all",
        "evaluation_scope": "core_and_subsystems_offline_regression",
        "passed": passed,
        "total": total,
        "failed": failed,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "core_total": core_report["summary"]["total"],
            "core_passed": core_report["summary"]["passed"],
            "subsystems_total": subsystems_report["total"],
            "subsystems_passed": subsystems_report["passed"],
        },
        "limitations": [
            *core_report["limitations"],
            "子系统套件验证离线架构不变量与安全边界，不代表真实 LLM 基准得分",
        ],
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "trials_per_core_task": trials,
            "network_required": False,
        },
        "suites": {
            "core": core_report,
            "subsystems": subsystems_report,
        },
    }
    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return combined


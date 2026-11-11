"""CLI 与网页共享 Runtime，学习时可以先从命令行看完整 JSON 轨迹。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path

from .api import ROOT
from .git_repo import create_task_worktree, init_git_repo_from_dir
from .lease import LeaseWorker, build_worker_runtime_factory
from .runtime import AgentRuntime
from .sandbox import discover_test_oracles
from .storage import Storage


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def with_runtime(runtime, operation):
    try:
        return await operation
    finally:
        await runtime.aclose()


def main():
    parser = argparse.ArgumentParser(description="Agent Systems Lab · 代码 Agent 学习工程")
    parser.add_argument("--state", type=Path, default=ROOT / ".agent-lab")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动本机网页")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--dispatch",
        choices=["inline", "queue"],
        default=None,
        help="任务调度方式：inline 为单进程协程，queue 由独立 agent-lab worker 消费",
    )
    worker = sub.add_parser("worker", help="启动持久队列 Worker（支持租约续期与 epoch fencing）")
    worker.add_argument("--worker-id", default=None)
    worker.add_argument("--lease-ttl", type=float, default=15.0)
    worker.add_argument("--poll-interval", type=float, default=0.8)
    worker.add_argument("--once", action="store_true", help="仅尝试消费队列中的一个任务后退出")
    run = sub.add_parser("run", help="运行任务（默认只提出补丁，等待人工审批）")
    run.add_argument("prompt")
    run.add_argument("--provider", choices=["demo", "anthropic", "openai"], default="demo")
    run.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    run.add_argument("--template", choices=["sample", "multi_file"], default="sample")
    run.add_argument("--repo", type=Path, default=None, help="指定本地真实 Git 仓库，基于其创建独立 worktree")
    run.add_argument("--ref", default="HEAD", help="Git 基线分支或 commit SHA，默认 HEAD")
    run.add_argument("--max-repair-rounds", type=int, default=2)
    demo = sub.add_parser("demo", help="运行内置折扣 bug 场景")
    demo.add_argument("--auto-approve", action="store_true", help="显式授权应用内置示例补丁，仅用于此示例")
    demo.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    demo.add_argument("--template", choices=["sample", "multi_file"], default="sample")
    approve = sub.add_parser("approve", help="审批保存的补丁并恢复任务")
    approve.add_argument("run_id")
    approve.add_argument("proposal_id")
    approve.add_argument("--reject", action="store_true")
    resume = sub.add_parser("resume", help="恢复中断/失败任务，不重置预算")
    resume.add_argument("run_id")
    sub.add_parser("list", help="查看本机任务")
    trace = sub.add_parser("trace", help="查看持久化事件")
    trace.add_argument("run_id")
    calls = sub.add_parser("calls", help="查看持久化工具调用状态摘要")
    calls.add_argument("run_id")
    result = sub.add_parser("result", help="按调用 ID 分页查看完整结果 JSON 文本与哈希")
    result.add_argument("run_id")
    result.add_argument("call_id")
    result.add_argument("--offset", type=int, default=0)
    result.add_argument("--limit", type=int, default=4000)
    evaluate = sub.add_parser("eval", help="运行隔离离线评测")
    evaluate.add_argument("--report", type=Path, default=ROOT / "reports/eval.json")
    evaluate.add_argument("--trials", type=int, default=1)
    model_eval = sub.add_parser("model-eval", help="运行 5 任务真实仓库编码基准评测（Docker 真值 + 失败分类）")
    model_eval.add_argument("--provider", choices=["anthropic", "openai", "reference"], default="anthropic")
    model_eval.add_argument("--report", type=Path, default=ROOT / "reports/model-eval.json")
    model_eval.add_argument("--trials", type=int, default=1)
    model_eval.add_argument("--task", action="append", default=None, help="仅运行指定 task_id，可重复传入")
    model_eval.add_argument("--max-repair-rounds", type=int, default=2)
    model_eval.add_argument("--skip-baseline-check", action="store_true")
    args = parser.parse_args()
    args.state = args.state.resolve()
    args.state.mkdir(parents=True, exist_ok=True)
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("AGENT_LAB_TOKEN"):
            parser.error("监听其他地址必须设置 AGENT_LAB_TOKEN")
        os.environ["AGENT_LAB_STATE"] = str(args.state)
        if args.dispatch:
            os.environ["AGENT_LAB_DISPATCH"] = args.dispatch
        import uvicorn

        uvicorn.run("agent_lab.api:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.command == "eval":
        from .evals import run_evals

        report = asyncio.run(run_evals(args.report, trials=args.trials))
        show(report)
        if report.get("failed", 0) or report.get("summary", {}).get("failed", 0):
            raise SystemExit(1)
        return
    if args.command == "model-eval":
        from .model_evals import run_model_evals

        report = asyncio.run(
            run_model_evals(
                provider=args.provider,
                report_path=args.report,
                trials=args.trials,
                task_ids=args.task,
                max_repair_rounds=args.max_repair_rounds,
                verify_baseline=not args.skip_baseline_check,
            )
        )
        show(report)
        summary = report.get("summary", {})
        if summary.get("model_failures", 0) or summary.get("infrastructure_failures", 0):
            raise SystemExit(1)
        return
    storage = Storage(args.state / "state.db")
    if args.command == "worker":
        daemon = LeaseWorker(
            storage=storage,
            runtime_factory=build_worker_runtime_factory(ROOT, storage),
            worker_id=args.worker_id,
            ttl_seconds=args.lease_ttl,
        )
        if args.once:
            outcome = asyncio.run(daemon.process_one())
            show(outcome or {"ok": True, "idle": True})
        else:
            asyncio.run(daemon.serve_forever(poll_interval=args.poll_interval))
        return
    if args.command == "list":
        show(storage.list_runs())
        return
    if args.command == "trace":
        show(storage.events(args.run_id))
        return
    if args.command == "calls":
        if not storage.get_run(args.run_id):
            parser.error("任务不存在")
        show(storage.call_summaries(args.run_id))
        return
    if args.command == "result":
        try:
            show(storage.get_call_result(args.run_id, args.call_id, args.offset, args.limit))
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
        return
    if args.command in {"run", "demo"}:
        import uuid

        workspace = args.state / "workspaces" / uuid.uuid4().hex
        extra_meta: dict = {"template": args.template}
        commit_on_pass = False
        repo_arg = getattr(args, "repo", None)
        if repo_arg is not None:
            git_meta = create_task_worktree(repo_arg.resolve(), workspace, ref=args.ref)
            oracles, expected_tests = discover_test_oracles(workspace)
            extra_meta.update(template="custom_git", git=git_meta, test_oracles=oracles, expected_tests=expected_tests)
            commit_on_pass = True
        elif args.template == "multi_file":
            origin = args.state / "git_origins" / "multi_file_repo"
            origin.parent.mkdir(parents=True, exist_ok=True)
            if not origin.exists():
                init_git_repo_from_dir(ROOT / "data/multi_file_repo", origin)
            git_meta = create_task_worktree(origin, workspace, ref="HEAD")
            oracles, expected_tests = discover_test_oracles(workspace)
            extra_meta.update(git=git_meta, test_oracles=oracles, expected_tests=expected_tests)
            commit_on_pass = True
        else:
            shutil.copytree(ROOT / "data/sample_repo", workspace)
        provider = args.provider if args.command == "run" else "demo"
        repair_rounds = getattr(args, "max_repair_rounds", 2)
        runtime = AgentRuntime(
            storage,
            workspace,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider,
            max_repair_rounds=repair_rounds,
            commit_on_pass=commit_on_pass,
        )
        if args.command == "run":
            prompt = args.prompt
        elif args.template == "multi_file":
            prompt = "请检查 checkout.py 与 discounts.py 的多文件结算逻辑，修复优惠封顶与应付总额缺陷并在容器中验证。"
        else:
            prompt = "修复 pricing.py 的百分比折扣 bug，并验证修改。"
        created = runtime.create(prompt, args.mode, extra_metadata=extra_meta)
        result = asyncio.run(with_runtime(runtime, runtime.execute(created["id"])))
        proposals = storage.proposals(result["id"])
        if args.command == "demo" and args.auto_approve and proposals:
            result = asyncio.run(
                with_runtime(runtime, runtime.approve(result["id"], proposals[0]["id"], True))
            )
        show({"run": result, "proposals": storage.proposals(result["id"])})
        if result["status"] in {"failed", "budget_exceeded"}:
            raise SystemExit(1)
        return
    run = storage.get_run(args.run_id)
    if not run:
        parser.error("任务不存在")
    config = run["metadata"]
    runtime = AgentRuntime(
        storage,
        Path(config["workspace"]),
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=run["provider"],
        max_steps=config["max_steps"],
        max_tokens=config["max_tokens"],
        context_chars=config["context_chars"],
        max_repair_rounds=config.get("max_repair_rounds", 2),
        commit_on_pass=bool(config.get("commit_on_pass", False)),
    )
    if args.command == "approve":
        result = asyncio.run(
            with_runtime(runtime, runtime.approve(args.run_id, args.proposal_id, not args.reject))
        )
    else:
        result = asyncio.run(with_runtime(runtime, runtime.resume(args.run_id)))
    show(result)
    if result["status"] in {"failed", "budget_exceeded"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

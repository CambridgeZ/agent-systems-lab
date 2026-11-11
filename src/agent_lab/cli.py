"""CLI 与网页共享 Runtime，学习时可以先从命令行看完整 JSON 轨迹。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path

from .api import ROOT
from .runtime import AgentRuntime
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
    run = sub.add_parser("run", help="运行任务（默认只提出补丁，等待人工审批）")
    run.add_argument("prompt")
    run.add_argument("--provider", choices=["demo", "anthropic"], default="demo")
    run.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    demo = sub.add_parser("demo", help="运行内置折扣 bug 场景")
    demo.add_argument("--auto-approve", action="store_true", help="显式授权应用内置示例补丁，仅用于此示例")
    demo.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    approve = sub.add_parser("approve", help="审批保存的补丁并恢复任务")
    approve.add_argument("run_id")
    approve.add_argument("proposal_id")
    approve.add_argument("--reject", action="store_true")
    resume = sub.add_parser("resume", help="恢复中断/失败任务，不重置预算")
    resume.add_argument("run_id")
    sub.add_parser("list", help="查看本机任务")
    trace = sub.add_parser("trace", help="查看持久化事件")
    trace.add_argument("run_id")
    evaluate = sub.add_parser("eval", help="运行隔离离线评测")
    evaluate.add_argument("--report", type=Path, default=ROOT / "reports/eval.json")
    evaluate.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    args.state = args.state.resolve()
    args.state.mkdir(parents=True, exist_ok=True)
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("AGENT_LAB_TOKEN"):
            parser.error("监听其他地址必须设置 AGENT_LAB_TOKEN")
        os.environ["AGENT_LAB_STATE"] = str(args.state)
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
    storage = Storage(args.state / "state.db")
    if args.command == "list":
        show(storage.list_runs())
        return
    if args.command == "trace":
        show(storage.events(args.run_id))
        return
    if args.command in {"run", "demo"}:
        import uuid

        workspace = args.state / "workspaces" / uuid.uuid4().hex
        shutil.copytree(ROOT / "data/sample_repo", workspace)
        provider = args.provider if args.command == "run" else "demo"
        runtime = AgentRuntime(storage, workspace, ROOT / "data/knowledge", ROOT / "skills", provider)
        prompt = args.prompt if args.command == "run" else "修复 pricing.py 的百分比折扣 bug，并验证修改。"
        result = asyncio.run(with_runtime(runtime, runtime.start(prompt, args.mode)))
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

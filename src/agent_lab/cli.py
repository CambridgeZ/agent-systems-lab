"""CLI 与网页共享 Runtime，学习时可以先从命令行看完整 JSON 轨迹。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal

from .api import ROOT
from .git_repo import create_task_worktree, ensure_git_origin_atomic
from .kv_cache import simulate_kv_cache_ablation
from .lease import LeaseWorker, build_worker_runtime_factory
from .permissions import validate_direct_workspace_path
from .repl import drain_approvals_interactively, run_interactive_repl
from .runtime import AgentRuntime
from .sandbox import discover_test_oracles, reap_orphan_containers
from .storage import Storage


def _detect_default_cli_provider() -> str:
    env_p = os.environ.get("AGENT_PROVIDER", "").strip().lower()
    if env_p in {"anthropic", "openai", "openai_compat", "demo"}:
        return env_p
    if (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")) and os.environ.get(
        "ANTHROPIC_MODEL"
    ):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
        return "openai"
    return "demo"


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def with_runtime(runtime, operation):
    try:
        return await operation
    finally:
        await runtime.aclose()


async def _serve_worker_gracefully(daemon: LeaseWorker, poll_interval: float) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    main_task = asyncio.create_task(
        daemon.serve_forever(poll_interval=poll_interval, stop_event=stop_event)
    )
    def _on_signal() -> None:
        stop_event.set()
        if not main_task.done():
            main_task.cancel()

    registered_signals: list[signal.Signals] = []
    previous_handlers: list[tuple[signal.Signals, object]] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
            registered_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            prev = signal.getsignal(sig)
            previous_handlers.append((sig, prev))
            signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_on_signal))
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await main_task
    finally:
        for sig in registered_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        for sig, prev in previous_handlers:
            with contextlib.suppress(Exception):
                signal.signal(sig, prev)


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
    run = sub.add_parser("run", help="运行任务（默认隔离沙盒；传 --cwd 启用本地目录直连操控）")
    run.add_argument("prompt")
    run.add_argument("--provider", choices=["demo", "anthropic", "openai", "openai_compat"], default="demo")
    run.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    run.add_argument("--template", choices=["sample", "multi_file"], default="sample")
    run.add_argument("--repo", type=Path, default=None, help="指定本地真实 Git 仓库，基于其创建独立 worktree")
    run.add_argument("--cwd", type=Path, default=None, help="直接操作指定本地目录（启用终端命令与全功能文件读写）")
    run.add_argument(
        "--profile",
        choices=["sandbox", "host", "computer"],
        default=None,
        help="执行能力档位（--cwd 时默认 host，开启 --gui 时自动升为 computer）",
    )
    run.add_argument(
        "--permission-mode",
        choices=["ask", "auto-edit", "full-auto"],
        default=None,
        help="权限控制策略：ask / auto-edit / full-auto",
    )
    run.add_argument("--gui", action="store_true", help="启用桌面级 GUI 截图与键鼠操控工具")
    run.add_argument("--mcp-config", type=Path, default=None, help="外部 MCP Server 配置文件路径")
    run.add_argument("--no-auto-dream", action="store_true", help="禁用本次任务结束后的默认自动 Dreaming 整理")
    run.add_argument("--ref", default="HEAD", help="Git 基线分支或 commit SHA，默认 HEAD")
    run.add_argument("--max-repair-rounds", type=int, default=2)

    for repl_cmd in ("code", "chat"):
        code_p = sub.add_parser(
            repl_cmd,
            help="启动类似 Claude Code / Codex 的本地目录交互式多轮工作台",
        )
        code_p.add_argument("prompt", nargs="?", default=None, help="可选首轮任务指令")
        code_p.add_argument("--cwd", type=Path, default=Path("."), help="直接操作的本地工作目录（默认当前目录）")
        code_p.add_argument(
            "--provider",
            choices=["demo", "anthropic", "openai", "openai_compat"],
            default=None,
            help="模型提供方（默认自动检测已配置 API Key，否则回退 demo）",
        )
        code_p.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
        code_p.add_argument(
            "--permission-mode",
            choices=["ask", "auto-edit", "full-auto"],
            default="auto-edit",
            help="三级权限档位（默认 auto-edit）",
        )
        code_p.add_argument("--gui", action="store_true", help="同时启用桌面级 GUI 操控（Computer Use）")
        code_p.add_argument("--mcp-config", type=Path, default=None, help="外部 MCP Server 配置文件路径")
        code_p.add_argument("--no-auto-dream", action="store_true", help="禁用每轮会话结束后的默认自动 Dreaming")
        code_p.add_argument("--resume", default=None, help="恢复指定的已有会话 run_id")

    dream_cmd = sub.add_parser(
        "dream",
        help="运行离线 Dreaming 引擎（对照历史会话 Transcript 纠错、合成互链 Wiki、修复断链并整理 MEMORY.md 索引）",
    )
    dream_cmd.add_argument("--cwd", type=Path, default=Path("."), help="目标项目工作目录（默认当前目录）")
    dream_cmd.add_argument("--run-id", default=None, help="可选：针对指定会话 run_id 进行重放整理")
    dream_cmd.add_argument("--goal", default=None, help="可选：指定本次记忆整理的聚焦目标（Focus Goal）")
    dream_cmd.add_argument(
        "--provider",
        choices=["demo", "anthropic", "openai", "openai_compat"],
        default=None,
        help="可选：使用真实模型辅助语义合并（默认自动检测已配置 API Key）",
    )
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
    cancel = sub.add_parser("cancel", help="请求取消排队、等待审批或正在运行的任务")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason", default="cli_cancel", help="取消原因")
    gc_cmd = sub.add_parser("gc", help="回收过期终态工作区、归档快照、陈旧锁与孤儿沙箱容器")
    gc_cmd.add_argument("--retention-seconds", type=float, default=86400.0)
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
    kv_bench = sub.add_parser("kv-benchmark", help="运行 KV Cache 前缀命中率与阶梯压缩离线消融实验")
    kv_bench.add_argument("--turns", type=int, default=16)
    kv_bench.add_argument("--max-chars", type=int, default=6800)
    kv_bench.add_argument("--block-chars", type=int, default=64)
    kv_bench.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    args.state = args.state.resolve()
    args.state.mkdir(parents=True, exist_ok=True)
    if args.command == "serve":
        non_loopback = args.host not in {"127.0.0.1", "localhost", "::1"}
        has_auth = bool(
            os.environ.get("AGENT_LAB_TOKEN", "").strip()
            or os.environ.get("AGENT_LAB_TOKENS_JSON", "").strip()
        )
        allow_insecure = os.environ.get("AGENT_LAB_ALLOW_INSECURE_LOCAL", "").strip() == "1"
        if non_loopback:
            if not has_auth and not allow_insecure:
                parser.error(
                    "监听非回环地址必须设置 AGENT_LAB_TOKEN 或 AGENT_LAB_TOKENS_JSON"
                    "（仅限宿主端 127.0.0.1 端口映射时可显式设置 AGENT_LAB_ALLOW_INSECURE_LOCAL=1）"
                )
            if not allow_insecure:
                os.environ["AGENT_LAB_REQUIRE_AUTH"] = "1"
        os.environ["AGENT_LAB_STATE"] = str(args.state)
        if args.dispatch:
            os.environ["AGENT_LAB_DISPATCH"] = args.dispatch
        import uvicorn

        uvicorn.run("agent_lab.api:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.command == "kv-benchmark":
        report = simulate_kv_cache_ablation(
            turns=args.turns, max_chars=args.max_chars, block_chars=args.block_chars
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        show(report)
        if report["optimized"]["cumulative_hit_rate"] <= report["legacy"]["cumulative_hit_rate"]:
            raise SystemExit(1)
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
            asyncio.run(_serve_worker_gracefully(daemon, poll_interval=args.poll_interval))
        return
    if args.command == "cancel":
        try:
            cancelled = storage.request_cancel_run(args.run_id, actor_id="cli", reason=args.reason)
        except KeyError:
            parser.error("任务不存在")
        show(cancelled)
        return
    if args.command == "gc":
        storage_gc = storage.gc_expired_resources(
            workspaces_dir=args.state / "workspaces",
            git_origins_dir=args.state / "git_origins",
            retention_seconds=args.retention_seconds,
        )
        containers_gc = reap_orphan_containers()
        show({"ok": True, "storage_gc": storage_gc, "containers_gc": containers_gc})
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
    if args.command == "dream":
        target_run = storage.get_run(args.run_id) if args.run_id else None
        if args.run_id and target_run is None:
            parser.error("任务不存在")
        try:
            if target_run is not None:
                ws_raw = (target_run.get("metadata") or {}).get("workspace", "")
                target_ws = Path(ws_raw).resolve() if ws_raw else validate_direct_workspace_path(args.cwd)
                eff_prof = (target_run.get("metadata") or {}).get("execution_profile", "host")
            else:
                target_ws = validate_direct_workspace_path(args.cwd)
                eff_prof = "host"
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        provider = args.provider or _detect_default_cli_provider()
        runtime = AgentRuntime(
            storage,
            target_ws if target_ws.is_dir() else ROOT,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=provider,
            execution_profile=eff_prof,
            enable_agent_memory=True,
        )
        report = asyncio.run(
            with_runtime(runtime, runtime.dream(run_id=args.run_id, focus_goal=args.goal, trigger="cli"))
        )
        show(report)
        return
    if args.command in {"code", "chat"}:
        try:
            direct_ws = validate_direct_workspace_path(args.cwd)
        except ValueError as exc:
            parser.error(str(exc))
        provider = args.provider or _detect_default_cli_provider()
        profile = "computer" if args.gui else "host"
        runtime = AgentRuntime(
            storage,
            direct_ws,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=provider,
            execution_profile=profile,
            permission_mode=args.permission_mode,
            enable_gui=bool(args.gui),
            mcp_config=args.mcp_config,
            auto_dream=not bool(getattr(args, "no_auto_dream", False)),
        )
        extra_meta = {
            "template": "local_cwd",
            "direct_workspace": True,
            "execution_profile": profile,
            "permission_mode": args.permission_mode,
            "enable_gui": bool(args.gui),
        }
        asyncio.run(
            with_runtime(
                runtime,
                run_interactive_repl(
                    runtime,
                    initial_prompt=args.prompt,
                    resume_run_id=args.resume,
                    mode=args.mode,
                    extra_metadata=extra_meta,
                ),
            )
        )
        return
    if args.command in {"run", "demo"}:
        import uuid

        cwd_arg = getattr(args, "cwd", None)
        repo_arg = getattr(args, "repo", None)
        gui_flag = bool(getattr(args, "gui", False))
        explicit_profile = getattr(args, "profile", None)
        perm_arg = getattr(args, "permission_mode", None)
        mcp_cfg_arg = getattr(args, "mcp_config", None)

        extra_meta: dict = {"template": args.template}
        commit_on_pass = False
        if cwd_arg is not None:
            try:
                workspace = validate_direct_workspace_path(cwd_arg)
            except ValueError as exc:
                parser.error(str(exc))
            eff_profile = explicit_profile or ("computer" if gui_flag else "host")
            extra_meta.update(template="local_cwd", direct_workspace=True)
        else:
            workspace = args.state / "workspaces" / uuid.uuid4().hex
            eff_profile = explicit_profile or ("computer" if gui_flag else "sandbox")
            if repo_arg is not None:
                git_meta = create_task_worktree(repo_arg.resolve(), workspace, ref=args.ref)
                oracles, expected_tests = discover_test_oracles(workspace)
                extra_meta.update(
                    template="custom_git", git=git_meta, test_oracles=oracles, expected_tests=expected_tests
                )
                commit_on_pass = True
            elif args.template == "multi_file":
                origin = args.state / "git_origins" / "multi_file_repo"
                ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin)
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
            execution_profile=eff_profile,
            permission_mode=perm_arg,
            enable_gui=gui_flag,
            mcp_config=mcp_cfg_arg,
            auto_dream=not bool(getattr(args, "no_auto_dream", False)),
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
        elif args.command == "run" and result["status"] == "waiting_approval" and os.isatty(0):
            result = asyncio.run(
                with_runtime(runtime, drain_approvals_interactively(runtime, result))
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
        auto_approve_proposals=bool(config.get("auto_approve_proposals", False)),
        execution_profile=config.get("execution_profile", "sandbox"),
        permission_mode=config.get("permission_mode"),
        enable_gui=bool(config.get("enable_gui", False)),
        mcp_config=config.get("mcp_config"),
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

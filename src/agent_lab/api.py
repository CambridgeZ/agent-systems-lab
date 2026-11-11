"""本机学习与工作台 API：支持单进程内联调度（inline）与独立持久队列 Worker 调度（queue）。

每个任务拥有独立目录副本或基于固定 commit 的 Git detached worktree；
SSE 断开不取消任务，重启或 Worker 故障超时后可通过租约世代（epoch）安全恢复进度。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .runtime import AgentRuntime
from .execution_lock import RunBusy
from .git_repo import GitRepoError, create_task_worktree, init_git_repo_from_dir
from .repomap import RepoMapIndex
from .sandbox import discover_test_oracles
from .storage import Storage
from .working_memory import audit_working_memory, has_working_memory_content

ROOT = Path(__file__).resolve().parents[2]


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)
    mode: Literal["agent", "workflow", "parallel"] = "agent"
    template: Literal["sample", "multi_file"] = "sample"


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=r"^[\w\- ]+$")
    value: str = Field(max_length=2000)


def create_app(state_dir: Path | None = None, provider: str | None = None) -> FastAPI:
    state = Path(state_dir or os.environ.get("AGENT_LAB_STATE", ROOT / ".agent-lab")).resolve()
    state.mkdir(parents=True, exist_ok=True)
    workspaces = state / "workspaces"
    workspaces.mkdir(exist_ok=True)
    git_origins = state / "git_origins"
    git_origins.mkdir(exist_ok=True)
    storage = Storage(state / "state.db")
    provider_name = provider or os.environ.get("AGENT_PROVIDER", "demo")
    dispatch_mode = os.environ.get("AGENT_LAB_DISPATCH", "inline").strip().lower()
    if dispatch_mode not in {"inline", "queue"}:
        dispatch_mode = "inline"
    token = os.environ.get("AGENT_LAB_TOKEN", "")
    jobs: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for job in list(jobs.values()):
            job.cancel()
        for job in list(jobs.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await job

    app = FastAPI(title="Agent Systems Lab", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.state.storage = storage

    @app.middleware("http")
    async def authorization(request: Request, call_next):
        if request.url.path.startswith("/api"):
            # 防止恶意网页向本地服务发起跨源写入。合法 CLI 无 Origin 头。
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "跨源请求被拒绝"}, status_code=403)
            if token and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "需要 Bearer token"}, status_code=401)
        return await call_next(request)

    def get_run(run_id: str) -> dict:
        run = storage.get_run(run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        return run

    def workspace_for(run: dict) -> Path:
        workspace = Path(run["metadata"]["workspace"]).resolve()
        if not workspace.is_relative_to(workspaces) or workspace == workspaces:
            raise HTTPException(409, "工作区不属于本服务")
        return workspace

    def runtime_for(run: dict) -> AgentRuntime:
        workspace = workspace_for(run)
        config = run["metadata"]
        return AgentRuntime(
            storage,
            workspace,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=run["provider"],
            max_steps=config["max_steps"],
            max_tokens=config["max_tokens"],
            context_chars=config["context_chars"],
            max_repair_rounds=config.get("max_repair_rounds", 2),
            commit_on_pass=bool(config.get("commit_on_pass", False)),
        )

    def dispatch(run_id: str, operation, runtime: AgentRuntime):
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")

        async def work():
            try:
                await operation()
            except RunBusy:
                # 另一个本机 CLI/API 已持锁；不能把它的正在运行状态覆盖为 failed。
                storage.add_event(run_id, "run_busy", {"reason": "another_local_executor"})
            except Exception as exc:
                # 除 Runtime 已捕获的模型异常外，也记录边界错误，禁止返回 SDK 细节。
                current = get_run(run_id)
                current.update(status="failed", error=type(exc).__name__)
                storage.save_run(current)
                storage.add_event(run_id, "failure", {"error_type": type(exc).__name__})
            finally:
                await runtime.aclose()

        task = asyncio.create_task(work())
        jobs[run_id] = task

        def forget_finished(completed):
            # 持久状态在 SQLite；完成任务不应永久占用进程内调度表。
            if jobs.get(run_id) is completed:
                jobs.pop(run_id)

        task.add_done_callback(forget_finished)

    def public_run(run: dict) -> dict:
        # 消息与仓库内容可从审计事件查看；列表只返回轻量摘要，不暴露本机路径。
        meta = run.get("metadata", {})
        ver = meta.get("verification", {})
        return {
            key: run.get(key)
            for key in (
                "id",
                "prompt",
                "mode",
                "provider",
                "status",
                "answer",
                "error",
                "steps",
                "input_tokens",
                "output_tokens",
                "created_at",
                "updated_at",
            )
        } | {
            "template": meta.get("template", "sample"),
            "verification_status": ver.get("status", "not_run"),
        }

    @app.get("/api/config")
    async def config():
        from .retrieval import KnowledgeBase
        from .skills import SkillCatalog

        anthropic_ready = bool(
            (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
            and os.environ.get("ANTHROPIC_MODEL")
        )
        openai_ready = bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"))
        return {
            "provider": provider_name,
            "dispatch_mode": dispatch_mode,
            "knowledge": KnowledgeBase(ROOT / "data/knowledge").index_stats(),
            "skills": SkillCatalog(ROOT / "skills").catalog(),
            "single_process": dispatch_mode != "queue",
            "workers": storage.list_workers(),
            "real_model_configured": anthropic_ready or openai_ready,
        }

    @app.get("/api/workers")
    async def workers():
        return {"dispatch_mode": dispatch_mode, "workers": storage.list_workers()}

    @app.post("/api/runs", status_code=202)
    async def create_run(body: TaskInput):
        if dispatch_mode == "inline" and sum(not job.done() for job in jobs.values()) >= 4:
            raise HTTPException(429, "本机最多同时运行 4 个任务")
        workspace = workspaces / uuid.uuid4().hex
        extra_meta: dict = {"template": body.template}
        commit_on_pass = False
        try:
            if body.template == "multi_file":
                origin = git_origins / "multi_file_repo"
                if not origin.exists():
                    init_git_repo_from_dir(ROOT / "data/multi_file_repo", origin)
                git_meta = create_task_worktree(origin, workspace, ref="HEAD")
                oracles, expected_tests = discover_test_oracles(workspace)
                extra_meta.update(git=git_meta, test_oracles=oracles, expected_tests=expected_tests)
                commit_on_pass = True
            else:
                shutil.copytree(ROOT / "data/sample_repo", workspace)
        except (GitRepoError, ValueError) as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc
        try:
            runtime = AgentRuntime(
                storage,
                workspace,
                ROOT / "data/knowledge",
                ROOT / "skills",
                provider_name,
                commit_on_pass=commit_on_pass,
            )
            run = runtime.create(body.prompt, body.mode, extra_metadata=extra_meta)
        except ValueError as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc
        if dispatch_mode == "queue":
            await runtime.aclose()
            storage.enqueue_run(run["id"], action="execute")
        else:
            dispatch(run["id"], lambda: runtime.execute(run["id"]), runtime)
        return public_run(run)

    @app.get("/api/runs")
    async def list_runs():
        return [public_run(run) for run in storage.list_runs()]

    @app.get("/api/runs/{run_id}")
    async def run_details(run_id: str):
        stored = get_run(run_id)
        run = public_run(stored)
        run["proposals"] = storage.proposals(run_id)
        run["context"] = stored["metadata"].get("context_stats", {})
        run["verification"] = stored["metadata"].get("verification", {})
        run["git"] = stored["metadata"].get("git")
        run["lease"] = storage.get_lease(run_id)
        wm = stored["metadata"].get("working_memory")
        if has_working_memory_content(wm):
            try:
                ws = workspace_for(stored)
                run["working_memory"] = audit_working_memory(ws, storage, run_id, wm)
            except HTTPException:
                run["working_memory"] = None
        else:
            run["working_memory"] = None
        run["active_skills"] = [
            {key: snapshot[key] for key in ("name", "section", "sha256", "content_sha256")}
            | {"content_chars": len(snapshot["content"])}
            for snapshot in stored["metadata"].get("active_skills", {}).values()
        ]
        return run

    @app.get("/api/runs/{run_id}/repomap")
    async def repomap(
        run_id: str,
        max_chars: int = Query(default=1800, ge=200, le=6000),
        focus: str = Query(default="", max_length=200),
    ):
        stored = get_run(run_id)
        ws = workspace_for(stored)
        return RepoMapIndex(ws).build_map(max_chars=max_chars, focus_query=focus)

    @app.get("/api/runs/{run_id}/calls")
    async def calls(run_id: str):
        get_run(run_id)
        return storage.call_summaries(run_id)

    @app.get("/api/runs/{run_id}/tool-result")
    async def tool_result(
        run_id: str,
        call_id: str = Query(min_length=1, max_length=128),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=4000, ge=1, le=8000),
    ):
        get_run(run_id)
        try:
            return storage.get_call_result(run_id, call_id, offset, limit)
        except KeyError as exc:
            raise HTTPException(404, "调用结果不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, "调用尚未产生结果") from exc

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0):
        get_run(run_id)
        return storage.events(run_id, after=after)

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, after: int = 0):
        get_run(run_id)

        async def generate():
            cursor = after
            while not await request.is_disconnected():
                for event in storage.events(run_id, after=cursor):
                    cursor = event["id"]
                    yield "id: " + str(cursor) + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                current_run = get_run(run_id)
                lease = storage.get_lease(run_id)
                lease_active = bool(lease and lease.get("queue_state") in {"queued", "claimed"})
                if current_run["status"] not in {"queued", "running"} and not lease_active:
                    yield "event: done\ndata: {}\n\n"
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str):
        run = get_run(run_id)
        if dispatch_mode == "queue":
            lease = storage.get_lease(run_id)
            if lease and lease.get("queue_state") == "claimed" and (lease.get("lease_until") or 0) > asyncio.get_running_loop().time():
                pass
            storage.enqueue_run(run_id, action="resume")
            return public_run(run)
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        runtime = runtime_for(run)
        dispatch(run_id, lambda: runtime.resume(run_id), runtime)
        return public_run(run)

    @app.post("/api/runs/{run_id}/proposals/{proposal_id}", status_code=202)
    async def approve(run_id: str, proposal_id: str, body: ApprovalInput):
        run = get_run(run_id)
        proposal = storage.get_proposal(proposal_id)
        if not proposal or proposal["run_id"] != run_id:
            raise HTTPException(404, "提案不存在")
        if run["status"] != "waiting_approval" or proposal_id not in run["metadata"].get(
            "waiting_proposal_ids", []
        ):
            raise HTTPException(409, "任务没有等待这个提案的审批")
        if dispatch_mode == "queue":
            storage.approve_proposal(proposal_id, body.approved)
            storage.enqueue_run(run_id, action="resume")
            return {"id": run_id, "decision": body.approved, "queued": True}
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        runtime = runtime_for(run)
        dispatch(run_id, lambda: runtime.approve(run_id, proposal_id, body.approved), runtime)
        return {"id": run_id, "decision": body.approved}

    @app.get("/api/memory")
    async def memory():
        return storage.memories("user")

    @app.post("/api/memory")
    async def remember(body: MemoryInput):
        storage.remember("user", body.key, body.value)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "web/index.html")

    return app

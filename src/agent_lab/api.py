"""本机学习与工作台 API：支持单进程内联调度（inline）与独立持久队列 Worker 调度（queue）。

每个任务拥有独立目录副本或基于固定 commit 的 Git detached worktree；
SSE 断开不取消任务，重启或 Worker 故障超时后可通过租约世代（epoch）安全恢复进度。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Literal
import uuid

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .context import ContextOverflow
from .execution_lock import RunBusy
from .git_repo import GitRepoError, create_task_worktree, ensure_git_origin_atomic
from .kv_cache import simulate_kv_cache_ablation, summarize_kv_cache
from .memory import DreamEngine, MemoryError, MemoryStore, compute_project_slug
from .observability import (
    METRICS,
    bind_trace_context,
    format_traceparent,
    get_trace_id,
    sanitize_error_detail,
)
from .permissions import normalize_permission_mode, validate_direct_workspace_path
from .repomap import RepoMapIndex
from .retrieval import KnowledgeBase
from .runtime import AgentRuntime
from .sandbox import discover_test_oracles, execute_remote_sandbox_payload, reap_orphan_containers
from .skills import SkillCatalog
from .storage import Storage
from .working_memory import audit_working_memory, has_working_memory_content

ROOT = Path(__file__).resolve().parents[2]
VALID_ROLES = frozenset({"operator", "approver", "admin"})
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_MAX_REPOMAP_CACHE = 32


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor_id: str
    roles: frozenset[str]
    global_admin: bool = False

    def memory_scope(self) -> str:
        return "user" if self.tenant_id == "local" else f"tenant:{self.tenant_id}:user"


class _TenantRateLimiter:
    """线程安全的按租户 60 秒滑动窗口限流器。"""

    def __init__(self, limit_rpm: int) -> None:
        self.limit_rpm = int(limit_rpm)
        self._lock = threading.Lock()
        self._windows: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, tenant_id: str) -> bool:
        if self.limit_rpm <= 0:
            return False
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            win = self._windows.get(tenant_id)
            if win is None:
                win = deque()
                self._windows[tenant_id] = win
            else:
                self._windows.move_to_end(tenant_id)
            while win and win[0] <= cutoff:
                win.popleft()
            if len(win) >= self.limit_rpm:
                return False
            win.append(now)
            while len(self._windows) > 1024:
                oldest_key, oldest_win = next(iter(self._windows.items()))
                if oldest_key == tenant_id:
                    break
                while oldest_win and oldest_win[0] <= cutoff:
                    oldest_win.popleft()
                self._windows.pop(oldest_key, None)
            return True


def _parse_tokens_config(single_token: str, raw_tokens_json: str) -> list[tuple[str, Principal]]:
    entries: list[tuple[str, Principal]] = []
    seen_tokens: set[str] = set()
    if raw_tokens_json:
        try:
            parsed = json.loads(raw_tokens_json)
        except ValueError as exc:
            raise RuntimeError("AGENT_LAB_TOKENS_JSON 不是合法 JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeError("AGENT_LAB_TOKENS_JSON 必须是非空对象映射")
        for tok, spec in parsed.items():
            if not isinstance(tok, str) or not tok.strip() or not isinstance(spec, dict):
                raise RuntimeError("AGENT_LAB_TOKENS_JSON 令牌配置项无效")
            tenant_id = str(spec.get("tenant_id", "")).strip()
            actor_id = str(spec.get("actor_id", "")).strip()
            raw_roles = spec.get("roles")
            if not _TENANT_ID_RE.fullmatch(tenant_id) or not _ACTOR_ID_RE.fullmatch(actor_id):
                raise RuntimeError("AGENT_LAB_TOKENS_JSON 中的 tenant_id 或 actor_id 格式非法")
            if not isinstance(raw_roles, list) or not raw_roles:
                raise RuntimeError("AGENT_LAB_TOKENS_JSON 中的 roles 必须是非空列表")
            roles = frozenset(str(r).strip() for r in raw_roles)
            if not roles.issubset(VALID_ROLES):
                raise RuntimeError("AGENT_LAB_TOKENS_JSON 包含未知角色")
            is_global = bool(spec.get("global") or spec.get("global_admin")) and ("admin" in roles)
            entries.append((tok, Principal(tenant_id, actor_id, roles, global_admin=is_global)))
            seen_tokens.add(tok)
    if single_token and single_token not in seen_tokens:
        entries.append(
            (single_token, Principal("local", "token-admin", VALID_ROLES, global_admin=True))
        )
    return entries


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)
    mode: Literal["agent", "workflow", "parallel"] = "agent"
    template: Literal["sample", "multi_file", "local_cwd"] = "sample"
    workspace_path: str | None = Field(default=None, max_length=512)
    execution_profile: Literal["sandbox", "host", "computer"] | None = None
    permission_mode: Literal["ask", "auto-edit", "full-auto"] | None = None
    enable_gui: bool = False


class FollowUpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)


class PermissionModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission_mode: Literal["ask", "auto-edit", "full-auto"]


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool
    permission_mode: Literal["ask", "auto-edit", "full-auto"] | None = None


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=r"^[\w\- ]+$")
    value: str = Field(max_length=2000)


class MemoryPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Literal["global", "project"] = "project"
    path: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=160)
    content: str = Field(min_length=1, max_length=12000)
    status: Literal["confirmed", "hypothesis"] = "confirmed"
    sources: list[str] | None = Field(default=None, max_length=12)
    index_summary: str | None = Field(default=None, max_length=180)
    run_id: str | None = Field(default=None, max_length=64)
    workspace_path: str | None = Field(default=None, max_length=512)


class DreamInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, max_length=64)
    focus_goal: str | None = Field(default=None, max_length=400)
    workspace_path: str | None = Field(default=None, max_length=512)


def create_app(state_dir: Path | None = None, provider: str | None = None) -> FastAPI:
    raw_hosts = os.environ.get("AGENT_LAB_ALLOWED_HOSTS", "")
    allowed_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()] or [
        "localhost",
        "127.0.0.1",
        "[::1]",
        "testserver",
    ]
    token = os.environ.get("AGENT_LAB_TOKEN", "")
    raw_tokens_json = os.environ.get("AGENT_LAB_TOKENS_JSON", "").strip()
    token_entries = _parse_tokens_config(token, raw_tokens_json)
    auth_configured = len(token_entries) > 0
    require_auth = os.environ.get("AGENT_LAB_REQUIRE_AUTH", "").strip().lower() in {"1", "true", "yes"}
    if require_auth and not auth_configured:
        raise RuntimeError("生产模式要求配置 AGENT_LAB_TOKEN 或 AGENT_LAB_TOKENS_JSON")

    explicit_max_body = os.environ.get("AGENT_LAB_MAX_BODY_BYTES")
    max_body_bytes = max(1, int(explicit_max_body)) if explicit_max_body is not None else 256 * 1024
    sandbox_max_body_bytes = (
        max_body_bytes
        if explicit_max_body is not None
        else max(max_body_bytes, int(os.environ.get("AGENT_LAB_SANDBOX_MAX_BODY_BYTES", str(4 * 1024 * 1024))))
    )
    max_queue_depth = max(1, int(os.environ.get("AGENT_LAB_MAX_QUEUE_DEPTH", "64")))
    rate_limiter = _TenantRateLimiter(int(os.environ.get("AGENT_LAB_RATE_LIMIT_RPM", "120")))
    default_principal = Principal("local", "local-dev", VALID_ROLES, global_admin=False)

    state = Path(state_dir or os.environ.get("AGENT_LAB_STATE", ROOT / ".agent-lab")).resolve()
    state.mkdir(parents=True, exist_ok=True)
    workspaces = state / "workspaces"
    workspaces.mkdir(exist_ok=True)
    git_origins = state / "git_origins"
    git_origins.mkdir(exist_ok=True)
    artifacts_root = state / "artifacts"
    artifacts_root.mkdir(exist_ok=True)
    raw_allowed_ws = os.environ.get("AGENT_LAB_ALLOWED_WORKSPACES", "").strip()
    allowed_ws_roots = (
        [Path(p.strip()).expanduser().resolve() for p in raw_allowed_ws.split(",") if p.strip()]
        if raw_allowed_ws
        else None
    )
    dsn = os.environ.get("AGENT_LAB_STORAGE_DSN", "").strip()
    storage = Storage.from_dsn(dsn) if (state_dir is None and dsn) else Storage(state / "state.db")
    provider_name = provider or os.environ.get("AGENT_PROVIDER", "demo")
    dispatch_mode = os.environ.get("AGENT_LAB_DISPATCH", "inline").strip().lower()
    if dispatch_mode not in {"inline", "queue"}:
        dispatch_mode = "inline"
    jobs: dict[str, asyncio.Task] = {}
    dream_jobs: set[asyncio.Task] = set()
    repomap_lock = threading.Lock()

    def _resolve_bearer(auth_header: str) -> Principal | None:
        if not auth_header.startswith("Bearer "):
            return None
        provided = auth_header[7:]
        if not provided:
            return None
        matched: Principal | None = None
        for expected_tok, principal_obj in token_entries:
            if hmac.compare_digest(provided, expected_tok) and matched is None:
                matched = principal_obj
        return matched

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for job in list(jobs.values()):
            job.cancel()
        for d_job in list(dream_jobs):
            d_job.cancel()
        for job in list(jobs.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await job
        for d_job in list(dream_jobs):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await d_job

    app = FastAPI(title="Agent Systems Lab", version="0.1.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.knowledge = KnowledgeBase(ROOT / "data/knowledge")
    app.state.skills = SkillCatalog(ROOT / "skills")
    app.state.repomap_cache = OrderedDict()

    def _get_repomap_index(ws: Path) -> RepoMapIndex:
        key = str(ws.resolve())
        cache: OrderedDict[str, RepoMapIndex] = app.state.repomap_cache
        with repomap_lock:
            existing = cache.get(key)
            if existing is not None:
                cache.move_to_end(key)
                return existing
            created_idx = RepoMapIndex(ws)
            cache[key] = created_idx
            while len(cache) > _MAX_REPOMAP_CACHE:
                cache.popitem(last=False)
            return created_idx

    class _GuardMiddleware:
        def __init__(self, inner_app: Any) -> None:
            self.app = inner_app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            req = Request(scope)
            path = req.url.path
            method = req.method
            incoming_trace = req.headers.get("traceparent") or req.headers.get("x-trace-id")
            t0 = time.perf_counter()
            status_code = 500

            with bind_trace_context(incoming_trace) as trace_id:
                async def _send_early(payload: dict[str, Any], code: int) -> None:
                    nonlocal status_code
                    status_code = code
                    resp = JSONResponse(payload, status_code=code, headers={"X-Trace-Id": trace_id})
                    await resp(scope, receive, send)

                try:
                    body_cap = (
                        sandbox_max_body_bytes
                        if path == "/api/internal/sandbox/execute"
                        else max_body_bytes
                    )
                    cl_header = req.headers.get("content-length")
                    if cl_header is not None:
                        try:
                            cl_val = int(cl_header)
                        except ValueError:
                            await _send_early({"detail": "非法的 Content-Length"}, 400)
                            return
                        if cl_val < 0:
                            await _send_early({"detail": "非法的 Content-Length"}, 400)
                            return
                        if cl_val > body_cap:
                            await _send_early({"detail": "请求体超过大小限制"}, 413)
                            return

                    auth_header = req.headers.get("authorization", "")
                    if path.startswith("/api"):
                        origin = req.headers.get("origin")
                        if origin and origin != str(req.base_url).rstrip("/"):
                            await _send_early({"detail": "跨源请求被拒绝"}, 403)
                            return
                        if path == "/api/internal/sandbox/execute":
                            sbx_tok = os.environ.get("AGENT_LAB_SANDBOX_TOKEN", "")
                            if sbx_tok and hmac.compare_digest(auth_header, "Bearer " + sbx_tok):
                                scope.setdefault("state", {})["principal"] = Principal(
                                    "internal", "sandbox-pool", frozenset({"admin"}), global_admin=False
                                )
                            elif auth_configured:
                                matched = _resolve_bearer(auth_header)
                                if matched is None:
                                    await _send_early({"detail": "需要 Bearer token"}, 401)
                                    return
                                if "admin" not in matched.roles:
                                    await _send_early({"detail": "需要 admin 角色或有效沙箱令牌"}, 403)
                                    return
                                scope.setdefault("state", {})["principal"] = matched
                            elif sbx_tok:
                                await _send_early({"detail": "需要有效沙箱 Bearer token"}, 401)
                                return
                            else:
                                scope.setdefault("state", {})["principal"] = default_principal
                        elif auth_configured:
                            matched = _resolve_bearer(auth_header)
                            if matched is None:
                                await _send_early({"detail": "需要 Bearer token"}, 401)
                                return
                            scope.setdefault("state", {})["principal"] = matched
                        else:
                            scope.setdefault("state", {})["principal"] = default_principal
                    elif path == "/metrics" and auth_configured:
                        if _resolve_bearer(auth_header) is None:
                            await _send_early({"detail": "需要 Bearer token"}, 401)
                            return

                    downstream_receive = receive
                    if method in {"POST", "PUT", "PATCH"}:
                        buffered = bytearray()
                        more_body = True
                        disconnected_msg: dict[str, Any] | None = None
                        while more_body:
                            msg = await receive()
                            msg_type = msg.get("type")
                            if msg_type == "http.request":
                                chunk = msg.get("body", b"") or b""
                                buffered.extend(chunk)
                                if len(buffered) > body_cap:
                                    await _send_early({"detail": "请求体超过大小限制"}, 413)
                                    return
                                more_body = bool(msg.get("more_body", False))
                            elif msg_type == "http.disconnect":
                                disconnected_msg = msg
                                more_body = False
                            else:
                                more_body = False
                        body_bytes = bytes(buffered)
                        replayed = False

                        async def _replay_receive() -> dict[str, Any]:
                            nonlocal replayed
                            if not replayed:
                                replayed = True
                                if disconnected_msg is not None and not body_bytes:
                                    return disconnected_msg
                                return {"type": "http.request", "body": body_bytes, "more_body": False}
                            if disconnected_msg is not None:
                                return disconnected_msg
                            return await receive()

                        downstream_receive = _replay_receive

                    async def wrapped_send(message: dict[str, Any]) -> None:
                        nonlocal status_code
                        if message.get("type") == "http.response.start":
                            status_code = int(message.get("status", 200))
                            raw_headers = list(message.get("headers") or [])
                            if not any(k.lower() == b"x-trace-id" for k, _ in raw_headers):
                                raw_headers.append((b"x-trace-id", trace_id.encode("latin-1")))
                            message = {**message, "headers": raw_headers}
                        await send(message)

                    await self.app(scope, downstream_receive, wrapped_send)
                finally:
                    elapsed_ms = max(0.0, (time.perf_counter() - t0) * 1000.0)
                    route_obj = scope.get("route")
                    route_label = getattr(route_obj, "path", None) or (
                        path if path in {"/healthz", "/readyz", "/metrics", "/"} else "unmatched"
                    )
                    METRICS.inc(
                        "agent_lab_http_requests_total",
                        {"method": method, "path": route_label, "status": str(status_code)},
                    )
                    METRICS.observe_ms(
                        "agent_lab_http_request_duration_ms",
                        elapsed_ms,
                        {"method": method, "path": route_label},
                    )

    app.add_middleware(_GuardMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    def get_principal(request: Request) -> Principal:
        return getattr(request.state, "principal", default_principal)

    def require_roles(principal: Principal, allowed: set[str] | frozenset[str], detail: str = "权限不足") -> None:
        if not (principal.roles & allowed):
            raise HTTPException(403, detail)

    def _can_access_run(run: dict, principal: Principal) -> bool:
        if "admin" in principal.roles and principal.global_admin:
            return True
        stored_tenant = str(
            run.get("tenant_id") or (run.get("metadata") or {}).get("tenant_id") or "local"
        )
        return stored_tenant == principal.tenant_id

    def get_run(run_id: str, principal: Principal | None = None) -> dict:
        run = storage.get_run(run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        if principal is not None and not _can_access_run(run, principal):
            raise HTTPException(404, "任务不存在")
        return run

    async def get_run_async(run_id: str, principal: Principal | None = None) -> dict:
        run = await storage.run_async(storage.get_run, run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        if principal is not None and not _can_access_run(run, principal):
            raise HTTPException(404, "任务不存在")
        return run

    def _can_use_direct_workspace(principal: Principal) -> bool:
        if not auth_configured:
            return True
        if principal.global_admin and "admin" in principal.roles:
            return True
        return principal.tenant_id == "local" and "admin" in principal.roles

    def workspace_for(run: dict) -> Path:
        meta = run.get("metadata") or {}
        if meta.get("direct_workspace"):
            try:
                return validate_direct_workspace_path(meta["workspace"], allowed_roots=allowed_ws_roots)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        workspace = Path(meta["workspace"]).resolve()
        if not workspace.is_relative_to(workspaces) or workspace == workspaces:
            raise HTTPException(409, "工作区不属于本服务")
        return workspace

    def runtime_for(run: dict) -> AgentRuntime:
        workspace = workspace_for(run)
        config = run["metadata"]
        rt = AgentRuntime(
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
            auto_approve_proposals=bool(config.get("auto_approve_proposals", False)),
            execution_profile=config.get("execution_profile", "sandbox"),
            permission_mode=config.get("permission_mode"),
            enable_gui=bool(config.get("enable_gui", False)),
            mcp_config=config.get("mcp_config"),
            artifacts_dir=artifacts_root,
            enable_agent_memory=config.get("enable_agent_memory"),
            auto_dream=bool(config.get("auto_dream", True)),
            defer_auto_dream=True,
        )
        rt.knowledge = app.state.knowledge
        rt.skills = app.state.skills
        return rt

    def _schedule_background_dream(run_id: str) -> None:
        async def _dream_worker() -> None:
            dream_rt: AgentRuntime | None = None
            try:
                stored_r = await storage.run_async(storage.get_run, run_id)
                if not stored_r or stored_r.get("status") != "completed":
                    return
                if not bool((stored_r.get("metadata") or {}).get("auto_dream", True)):
                    return
                dream_rt = runtime_for(stored_r)
                await dream_rt.dream(run_id=run_id, trigger="auto_post_session")
            except Exception:
                pass
            finally:
                if dream_rt is not None:
                    with contextlib.suppress(Exception):
                        await dream_rt.aclose()

        d_task = asyncio.create_task(_dream_worker())
        dream_jobs.add(d_task)
        d_task.add_done_callback(dream_jobs.discard)

    def dispatch(run_id: str, operation, runtime: AgentRuntime):
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        runtime.defer_auto_dream = True

        async def work():
            completed_ok = False
            try:
                res = await operation()
                completed_ok = isinstance(res, dict) and res.get("status") == "completed"
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    current = storage.get_run(run_id)
                    if current and current.get("status") == "running":
                        if storage.is_cancel_requested(run_id):
                            current["status"] = "cancelled"
                            current["error"] = "Cancelled"
                            storage.save_run(current)
                            storage.add_event(
                                run_id,
                                "cancelled",
                                {"reason": (current.get("metadata") or {}).get("cancel_reason", "cancelled_task")},
                            )
                        else:
                            current["status"] = "interrupted"
                            storage.save_run(current)
                            storage.add_event(run_id, "interrupted", {"reason": "server_shutdown"})
                raise
            except RunBusy:
                # 另一个本机 CLI/API 已持锁；不能把它的正在运行状态覆盖为 failed。
                storage.add_event(run_id, "run_busy", {"reason": "another_local_executor"})
            except Exception as exc:
                # 除 Runtime 已捕获的模型异常外，也记录边界错误，禁止返回 SDK 细节。
                detail = sanitize_error_detail(exc)
                current = storage.get_run(run_id)
                if current is not None:
                    current.update(status="failed", error=type(exc).__name__)
                    current.setdefault("metadata", {})["error_detail"] = detail
                    storage.save_run(current)
                    storage.add_event(run_id, "failure", detail)
            finally:
                with contextlib.suppress(Exception):
                    await runtime.aclose()
            if completed_ok:
                _schedule_background_dream(run_id)

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
            "error_detail": meta.get("error_detail"),
            "template": meta.get("template", "sample"),
            "execution_profile": meta.get("execution_profile", "sandbox"),
            "permission_mode": meta.get("permission_mode", "ask"),
            "enable_gui": bool(meta.get("enable_gui", False)),
            "direct_workspace": bool(meta.get("direct_workspace", False)),
            "verification_status": ver.get("status", "not_run"),
        }

    def _count_active_queue_depth() -> int:
        with storage._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS cnt FROM run_leases WHERE queue_state IN ('queued', 'claimed')"
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def _probe_readiness() -> tuple[bool, dict[str, Any]]:
        db_ok = False
        ws_ok = False
        worker_count = 0
        try:
            with storage._connect() as con:
                con.execute("SELECT 1").fetchone()
            db_ok = True
            worker_count = len(storage.list_workers())
        except Exception:
            db_ok = False
        try:
            if workspaces.is_dir():
                with tempfile.NamedTemporaryFile(dir=workspaces, prefix=".readyz-", delete=True) as probe:
                    probe.write(b"ok")
                    probe.flush()
                ws_ok = True
        except OSError:
            ws_ok = False
        remote_sbx = bool(os.environ.get("AGENT_LAB_SANDBOX_URL", "").strip())
        local_docker = bool(shutil.which("docker"))
        sbx_mode = "remote_http" if remote_sbx else ("local_docker" if local_docker else "unavailable")
        sbx_available = remote_sbx or local_docker
        sbx_required = os.environ.get("AGENT_LAB_REQUIRE_SANDBOX") == "1"
        worker_required = os.environ.get("AGENT_LAB_REQUIRE_WORKER") == "1" and dispatch_mode == "queue"
        ready = (
            db_ok
            and ws_ok
            and (sbx_available if sbx_required else True)
            and (worker_count > 0 if worker_required else True)
        )
        return ready, {
            "ok": ready,
            "status": "ready" if ready else "not_ready",
            "checks": {
                "database": db_ok,
                "workspace_writable": ws_ok,
                "sandbox": {"mode": sbx_mode, "available": sbx_available},
                "workers": {"dispatch_mode": dispatch_mode, "registered": worker_count},
            },
        }

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "status": "alive"}

    @app.get("/readyz")
    async def readyz():
        ready, report = await asyncio.to_thread(_probe_readiness)
        return JSONResponse(report, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def prometheus_metrics():
        METRICS.set_gauge("agent_lab_inline_active_jobs", sum(not j.done() for j in jobs.values()))
        return PlainTextResponse(
            METRICS.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/metrics")
    async def api_metrics():
        METRICS.set_gauge("agent_lab_inline_active_jobs", sum(not j.done() for j in jobs.values()))
        return METRICS.snapshot()

    @app.get("/api/config")
    async def config():
        anthropic_ready = bool(
            (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
            and os.environ.get("ANTHROPIC_MODEL")
        )
        openai_ready = bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"))
        skill_catalog, worker_list = await asyncio.gather(
            asyncio.to_thread(app.state.skills.catalog),
            storage.run_async(storage.list_workers),
        )
        return {
            "provider": provider_name,
            "dispatch_mode": dispatch_mode,
            "knowledge": app.state.knowledge.index_stats(),
            "skills": skill_catalog,
            "single_process": dispatch_mode != "queue",
            "workers": worker_list,
            "real_model_configured": anthropic_ready or openai_ready,
            "host_capabilities": {
                "direct_workspace_enabled": True,
                "default_workspace": str(ROOT),
                "platform": platform.system(),
                "permission_modes": ["ask", "auto-edit", "full-auto"],
            },
        }

    @app.get("/api/workers")
    async def workers():
        return {"dispatch_mode": dispatch_mode, "workers": await storage.run_async(storage.list_workers)}

    @app.get("/api/kv-benchmark")
    async def kv_benchmark(
        turns: int = Query(default=16, ge=4, le=40),
        max_chars: int = Query(default=6800, ge=2400, le=64000),
        block_chars: int = Query(default=64, ge=16, le=512),
    ):
        try:
            return await asyncio.to_thread(
                simulate_kv_cache_ablation, turns=turns, max_chars=max_chars, block_chars=block_chars
            )
        except (ValueError, ContextOverflow) as exc:
            raise HTTPException(400, str(exc)) from exc

    def _prepare_workspace_and_run(
        body: TaskInput, principal: Principal, traceparent_str: str
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        is_direct = body.template == "local_cwd" or bool(body.workspace_path)
        if is_direct and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色使用本地目录直连模式")

        extra_meta: dict[str, Any] = {
            "template": "local_cwd" if is_direct else body.template,
            "tenant_id": principal.tenant_id,
            "created_by": principal.actor_id,
            "actor_id": principal.actor_id,
            "memory_scope": principal.memory_scope(),
            "traceparent": traceparent_str,
        }
        commit_on_pass = False
        allocated_temp_ws: Path | None = None
        try:
            if is_direct:
                target_raw = (body.workspace_path or "").strip() or str(ROOT)
                workspace = validate_direct_workspace_path(target_raw, allowed_roots=allowed_ws_roots)
                eff_profile = body.execution_profile or ("computer" if body.enable_gui else "host")
                eff_perm = body.permission_mode or "auto-edit"
                extra_meta["direct_workspace"] = True
            else:
                workspace = workspaces / uuid.uuid4().hex
                allocated_temp_ws = workspace
                eff_profile = body.execution_profile or ("computer" if body.enable_gui else "sandbox")
                eff_perm = body.permission_mode
                if body.template == "multi_file":
                    origin = git_origins / "multi_file_repo"
                    ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin)
                    git_meta = create_task_worktree(origin, workspace, ref="HEAD")
                    oracles, expected_tests = discover_test_oracles(workspace)
                    extra_meta.update(git=git_meta, test_oracles=oracles, expected_tests=expected_tests)
                    commit_on_pass = True
                else:
                    shutil.copytree(ROOT / "data/sample_repo", workspace)
            runtime = AgentRuntime(
                storage,
                workspace,
                ROOT / "data/knowledge",
                ROOT / "skills",
                provider_name,
                commit_on_pass=commit_on_pass,
                execution_profile=eff_profile,
                permission_mode=eff_perm,
                enable_gui=bool(body.enable_gui),
                artifacts_dir=artifacts_root,
            )
            runtime.knowledge = app.state.knowledge
            runtime.skills = app.state.skills
            run = runtime.create(body.prompt, body.mode, extra_metadata=extra_meta)
            return runtime, run
        except (GitRepoError, ValueError) as exc:
            if allocated_temp_ws is not None:
                shutil.rmtree(allocated_temp_ws, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/runs", status_code=202)
    async def create_run(body: TaskInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能创建任务")
        if not rate_limiter.allow(principal.tenant_id):
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "rate_limited"})
            raise HTTPException(429, "当前租户创建任务频率超过限制")
        if dispatch_mode == "inline" and sum(not job.done() for job in jobs.values()) >= 4:
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "inline_capacity"})
            raise HTTPException(429, "本机最多同时运行 4 个任务")
        if dispatch_mode == "queue":
            depth = await asyncio.to_thread(_count_active_queue_depth)
            if depth >= max_queue_depth:
                METRICS.inc("agent_lab_runs_rejected_total", {"reason": "queue_depth"})
                raise HTTPException(429, "持久队列待处理任务已达上限")
        traceparent_str = format_traceparent(get_trace_id())
        runtime, run = await asyncio.to_thread(_prepare_workspace_and_run, body, principal, traceparent_str)
        if dispatch_mode == "queue":
            await runtime.aclose()
            await storage.run_async(storage.enqueue_run, run["id"], action="execute")
        else:
            dispatch(run["id"], lambda: runtime.execute(run["id"]), runtime)
        return public_run(run)

    def _list_runs_hydrated(filter_tenant: str | None) -> list[dict]:
        runs = storage.list_runs(tenant_id=filter_tenant)
        for item in runs:
            meta = item.setdefault("metadata", {})
            if "error_detail" not in meta and item.get("status") in {"failed", "budget_exceeded"}:
                full = storage.get_run(item["id"])
                if full is not None:
                    meta["error_detail"] = (full.get("metadata") or {}).get("error_detail")
        return runs

    @app.get("/api/runs")
    async def list_runs(request: Request, all_tenants: bool = False):
        principal = get_principal(request)
        filter_tenant = (
            None if (all_tenants and "admin" in principal.roles and principal.global_admin) else principal.tenant_id
        )
        runs = await storage.run_async(_list_runs_hydrated, filter_tenant)
        return [public_run(run) for run in runs]

    @app.get("/api/runs/{run_id}")
    async def run_details(run_id: str, request: Request):
        principal = get_principal(request)
        stored = await get_run_async(run_id, principal)
        run = public_run(stored)
        proposals_list, lease_doc = await asyncio.gather(
            storage.run_async(storage.proposals, run_id),
            storage.run_async(storage.get_lease, run_id),
        )
        run["proposals"] = proposals_list
        run["context"] = stored["metadata"].get("context_stats", {})
        run["verification"] = stored["metadata"].get("verification", {})
        run["kv_cache"] = summarize_kv_cache(stored["metadata"].get("kv_cache"))
        run["git"] = stored["metadata"].get("git")
        run["lease"] = lease_doc
        wm = stored["metadata"].get("working_memory")
        if has_working_memory_content(wm):
            try:
                ws = workspace_for(stored)
                run["working_memory"] = await asyncio.to_thread(
                    audit_working_memory, ws, storage, run_id, wm
                )
            except HTTPException:
                run["working_memory"] = None
        else:
            run["working_memory"] = None
        run["active_skills"] = [
            {key: snapshot[key] for key in ("name", "section", "sha256", "content_sha256")}
            | {"content_chars": len(snapshot["content"])}
            for snapshot in stored["metadata"].get("active_skills", {}).values()
        ]
        run["conversation_turns"] = stored["metadata"].get("conversation_turns", [])
        run["waiting_proposal_ids"] = stored["metadata"].get("waiting_proposal_ids", [])
        run["dream_pending"] = any(not t.done() for t in dream_jobs)
        return run

    @app.get("/api/runs/{run_id}/artifacts/{artifact_name}")
    async def get_run_artifact(run_id: str, artifact_name: str, request: Request):
        await get_run_async(run_id, get_principal(request))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(png|jpg|jpeg|webp|txt|json)", artifact_name):
            raise HTTPException(400, "非法的产物文件名")
        run_art_dir = (artifacts_root / run_id).resolve()
        target = (run_art_dir / artifact_name).resolve()
        if (
            not target.is_relative_to(run_art_dir)
            or target == run_art_dir
            or target.is_symlink()
            or not target.is_file()
        ):
            raise HTTPException(404, "产物不存在")
        return FileResponse(
            target,
            headers={
                "Content-Security-Policy": "default-src 'none'; img-src 'self'",
                "Cache-Control": "private, max-age=300",
            },
        )

    @app.patch("/api/runs/{run_id}/permissions")
    async def update_run_permissions(run_id: str, body: PermissionModeInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"approver", "admin"}, "需要 approver 或 admin 角色才能切换权限模式")
        cur_run = await get_run_async(run_id, principal)
        if (run_id in jobs and not jobs[run_id].done()) or cur_run.get("status") == "running":
            raise HTTPException(409, "任务正在运行中，请稍后切换权限档位")
        norm_mode = normalize_permission_mode(body.permission_mode)

        def _persist_perm() -> dict:
            latest = storage.get_run(run_id)
            if not latest:
                raise KeyError("run_not_found")
            if latest.get("status") == "running":
                raise ValueError("run_currently_running")
            latest.setdefault("metadata", {})["permission_mode"] = norm_mode
            storage.save_run(latest)
            storage.add_event(
                run_id,
                "permission_mode_changed",
                {"permission_mode": norm_mode, "actor_id": principal.actor_id},
            )
            return latest

        try:
            updated = await storage.run_async(_persist_perm)
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return public_run(updated)

    @app.post("/api/runs/{run_id}/messages", status_code=202)
    async def append_run_message(run_id: str, body: FollowUpInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能发送后续指令")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")
        run = await get_run_async(run_id, principal)
        run_meta = run.get("metadata") or {}
        step_ext = max(12, int(run_meta.get("base_max_steps") or 24))
        tok_ext = max(30_000, int(run_meta.get("base_max_tokens") or 120_000))
        if dispatch_mode == "queue":
            depth = await asyncio.to_thread(_count_active_queue_depth)
            if depth >= max_queue_depth:
                raise HTTPException(429, "持久队列待处理任务已达上限")
            try:
                updated = await storage.run_async(
                    storage.append_user_turn,
                    run_id,
                    body.prompt,
                    step_extension=step_ext,
                    token_extension=tok_ext,
                    enqueue=True,
                    actor_id=principal.actor_id,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return public_run(updated)

        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        if sum(not job.done() for job in jobs.values()) >= 4:
            raise HTTPException(429, "本机最多同时运行 4 个任务")
        runtime = runtime_for(run)
        try:
            updated = await storage.run_async(
                storage.append_user_turn,
                run_id,
                body.prompt,
                step_extension=step_ext,
                token_extension=tok_ext,
                enqueue=False,
                actor_id=principal.actor_id,
            )
        except ValueError as exc:
            await runtime.aclose()
            raise HTTPException(409, str(exc)) from exc
        runtime.max_steps = int(updated["metadata"].get("max_steps", runtime.max_steps))
        runtime.max_tokens = int(updated["metadata"].get("max_tokens", runtime.max_tokens))
        dispatch(run_id, lambda: runtime.execute(run_id), runtime)
        return public_run(updated)

    @app.get("/api/runs/{run_id}/repomap")
    async def repomap(
        run_id: str,
        request: Request,
        max_chars: int = Query(default=1800, ge=200, le=6000),
        focus: str = Query(default="", max_length=200),
    ):
        stored = await get_run_async(run_id, get_principal(request))
        ws = workspace_for(stored)
        index = _get_repomap_index(ws)
        return await asyncio.to_thread(index.build_map, max_chars=max_chars, focus_query=focus)

    @app.get("/api/runs/{run_id}/calls")
    async def calls(run_id: str, request: Request):
        await get_run_async(run_id, get_principal(request))
        return await storage.run_async(storage.call_summaries, run_id)

    @app.get("/api/runs/{run_id}/tool-result")
    async def tool_result(
        run_id: str,
        request: Request,
        call_id: str = Query(min_length=1, max_length=128),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=4000, ge=1, le=8000),
    ):
        await get_run_async(run_id, get_principal(request))
        try:
            return await storage.run_async(storage.get_call_result, run_id, call_id, offset, limit)
        except KeyError as exc:
            raise HTTPException(404, "调用结果不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, "调用尚未产生结果") from exc

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        await get_run_async(run_id, get_principal(request))
        return await storage.run_async(storage.events, run_id, after=after)

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, after: int = 0):
        principal = get_principal(request)
        await get_run_async(run_id, principal)

        async def generate():
            cursor = after
            while not await request.is_disconnected():
                current_run = await get_run_async(run_id, principal)
                lease = await storage.run_async(storage.get_lease, run_id)
                lease_active = bool(lease and lease.get("queue_state") in {"queued", "claimed"})
                new_events = await storage.run_async(storage.events, run_id, after=cursor)
                for event in new_events:
                    cursor = event["id"]
                    yield "id: " + str(cursor) + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if current_run["status"] not in {"queued", "running"} and not lease_active:
                    yield "event: done\ndata: {}\n\n"
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能恢复任务")
        run = await get_run_async(run_id, principal)
        if dispatch_mode == "queue":
            lease = await storage.run_async(storage.get_lease, run_id)
            if lease and lease.get("queue_state") == "claimed" and (lease.get("lease_until") or 0.0) > time.time():
                raise HTTPException(409, "任务当前由活跃 Worker 持有租约")
            try:
                await storage.run_async(storage.enqueue_run, run_id, action="resume")
            except ValueError as exc:
                if "run_currently_claimed" in str(exc):
                    raise HTTPException(409, "任务当前由活跃 Worker 持有租约") from exc
                raise HTTPException(409, str(exc)) from exc
            return public_run(run)
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        runtime = runtime_for(run)
        dispatch(run_id, lambda: runtime.resume(run_id), runtime)
        return public_run(run)

    @app.post("/api/runs/{run_id}/cancel", status_code=200)
    async def cancel_run(run_id: str, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "approver", "admin"})
        await get_run_async(run_id, principal)
        updated = await storage.run_async(
            storage.request_cancel_run, run_id, actor_id=principal.actor_id, reason="api_cancel"
        )
        running_job = jobs.get(run_id)
        if running_job is not None and not running_job.done():
            running_job.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(running_job, timeout=1.0)
            refreshed = await storage.run_async(storage.get_run, run_id)
            if refreshed is not None:
                updated = refreshed
        return public_run(updated)

    @app.post("/api/runs/{run_id}/proposals/{proposal_id}", status_code=202)
    async def approve(run_id: str, proposal_id: str, body: ApprovalInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"approver", "admin"}, "需要 approver 或 admin 角色才能审批补丁")
        run = await get_run_async(run_id, principal)
        if os.environ.get("AGENT_LAB_REQUIRE_DISTINCT_APPROVER") == "1":
            creator = (run.get("metadata") or {}).get("created_by")
            if creator and principal.actor_id == creator:
                raise HTTPException(403, "双人复核策略禁止任务发起人自行审批补丁")
        proposal = await storage.run_async(storage.get_proposal, proposal_id)
        if not proposal or proposal["run_id"] != run_id:
            raise HTTPException(404, "提案不存在")
        if run["status"] != "waiting_approval" or proposal_id not in run["metadata"].get(
            "waiting_proposal_ids", []
        ):
            raise HTTPException(409, "任务没有等待这个提案的审批")
        if dispatch_mode == "queue":
            try:
                await storage.run_async(
                    storage.approve_and_enqueue,
                    run_id,
                    proposal_id,
                    body.approved,
                    actor_id=principal.actor_id,
                    permission_mode=body.permission_mode,
                )
            except KeyError as exc:
                raise HTTPException(404, "提案不存在") from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {"id": run_id, "decision": body.approved, "queued": True}
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        try:
            if body.permission_mode:
                norm_m = normalize_permission_mode(body.permission_mode)

                def _set_p() -> dict:
                    latest = storage.get_run(run_id)
                    if latest:
                        latest.setdefault("metadata", {})["permission_mode"] = norm_m
                        storage.save_run(latest)
                    return latest or run

                run = await storage.run_async(_set_p)
            await storage.run_async(
                storage.approve_proposal, proposal_id, body.approved, actor_id=principal.actor_id
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime = runtime_for(run)
        dispatch(run_id, lambda: runtime.approve(run_id, proposal_id, body.approved), runtime)
        return {"id": run_id, "decision": body.approved}

    async def _resolve_api_memory_store(
        principal: Principal,
        *,
        run_id: str | None = None,
        workspace_path: str | None = None,
    ) -> tuple[MemoryStore, Path | None]:
        if run_id:
            stored_run = await get_run_async(run_id, principal)
            meta = stored_run.get("metadata") or {}
            r_tenant = str(stored_run.get("tenant_id") or meta.get("tenant_id") or "local").strip() or "local"
            r_scope = str(
                meta.get("memory_scope")
                or ("user" if r_tenant == "local" else f"tenant:{r_tenant}:user")
            )
            ws: Path | None = None
            raw_ws = meta.get("workspace")
            if isinstance(raw_ws, str) and raw_ws.strip():
                cand = Path(raw_ws).resolve()
                worker_ws_root = (state / "worker_workspaces").resolve()
                if meta.get("direct_workspace"):
                    with contextlib.suppress(ValueError):
                        ws = validate_direct_workspace_path(cand, allowed_roots=allowed_ws_roots)
                elif (
                    (cand.is_relative_to(workspaces) and cand != workspaces)
                    or (cand.is_relative_to(worker_ws_root) and cand != worker_ws_root)
                ) and cand.is_dir():
                    ws = cand
            proj_slug = str(
                meta.get("project_slug")
                or compute_project_slug(
                    ws,
                    template=meta.get("template"),
                    git_meta=meta.get("git") if isinstance(meta.get("git"), dict) else None,
                    direct_workspace=bool(meta.get("direct_workspace")),
                    state_dir=state,
                )
            )
            return (
                MemoryStore(
                    state,
                    storage,
                    tenant_id=r_tenant,
                    memory_scope=r_scope,
                    project_slug=proj_slug,
                    workspace=ws,
                ),
                ws,
            )
        if workspace_path and workspace_path.strip():
            if not _can_use_direct_workspace(principal):
                raise HTTPException(403, "仅限本机或管理员角色访问指定本地路径记忆")
            try:
                ws_dir = validate_direct_workspace_path(workspace_path, allowed_roots=allowed_ws_roots)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            proj_slug = compute_project_slug(ws_dir, direct_workspace=True, state_dir=state)
            return (
                MemoryStore(
                    state,
                    storage,
                    tenant_id=principal.tenant_id,
                    memory_scope=principal.memory_scope(),
                    project_slug=proj_slug,
                    workspace=ws_dir,
                ),
                ws_dir,
            )
        if _can_use_direct_workspace(principal):
            candidate_default = allowed_ws_roots[0] if allowed_ws_roots else ROOT
            with contextlib.suppress(ValueError):
                ws_default = validate_direct_workspace_path(
                    candidate_default, allowed_roots=allowed_ws_roots
                )
                proj_slug = compute_project_slug(ws_default, direct_workspace=True, state_dir=state)
                return (
                    MemoryStore(
                        state,
                        storage,
                        tenant_id=principal.tenant_id,
                        memory_scope=principal.memory_scope(),
                        project_slug=proj_slug,
                        workspace=ws_default,
                    ),
                    ws_default,
                )
        return (
            MemoryStore(
                state,
                storage,
                tenant_id=principal.tenant_id,
                memory_scope=principal.memory_scope(),
                project_slug="tpl-sample",
                workspace=None,
            ),
            None,
        )

    @app.get("/api/memory")
    async def memory(request: Request):
        scope = get_principal(request).memory_scope()
        return await storage.run_async(storage.memories, scope)

    @app.post("/api/memory")
    async def remember(body: MemoryInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能写入记忆")
        await storage.run_async(storage.remember, principal.memory_scope(), body.key, body.value)
        return {"ok": True}

    @app.get("/api/memory/overview")
    async def memory_overview(
        request: Request,
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        principal = get_principal(request)
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        return await asyncio.to_thread(store.overview)

    @app.get("/api/memory/page")
    async def get_memory_page(
        request: Request,
        layer: Literal["global", "project"] = Query(default="project"),
        path: str = Query(default="MEMORY.md", max_length=160),
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        principal = get_principal(request)
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        try:
            return await asyncio.to_thread(store.read_page, layer, path)
        except MemoryError as exc:
            status = 404 if exc.code == "memory_not_found" else 400
            raise HTTPException(status, str(exc)) from exc

    @app.put("/api/memory/page")
    async def put_memory_page(body: MemoryPageInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能编辑记忆笔记")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=body.run_id, workspace_path=body.workspace_path
        )
        try:
            return await asyncio.to_thread(
                store.write_page,
                layer=body.layer,
                path=body.path,
                content=body.content,
                title=body.title,
                status=body.status,
                sources=body.sources,
                index_summary=body.index_summary,
                run_id=body.run_id,
            )
        except MemoryError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/memory/page")
    async def delete_memory_page(
        request: Request,
        layer: Literal["global", "project"] = Query(default="project"),
        path: str = Query(min_length=1, max_length=160),
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能删除记忆笔记")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        try:
            return await asyncio.to_thread(store.delete_page, layer, path)
        except MemoryError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/memory/search")
    async def search_memory(
        request: Request,
        q: str = Query(min_length=1, max_length=400),
        scope: Literal["all", "wiki", "transcripts"] = Query(default="all"),
        limit: int = Query(default=6, ge=1, le=15),
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        principal = get_principal(request)
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        return await asyncio.to_thread(store.search, q, scope=scope, limit=limit)

    @app.post("/api/memory/dream")
    async def trigger_dream(body: DreamInput, request: Request):
        from .providers import get_provider

        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能运行 Dreaming")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=body.run_id, workspace_path=body.workspace_path
        )
        llm_prov = get_provider(provider_name)
        try:
            engine = DreamEngine(store, llm_provider=llm_prov)
            report = await engine.dream(
                run_ids=[body.run_id] if body.run_id else None,
                focus_goal=body.focus_goal,
                trigger="api",
            )
            if body.run_id:
                await storage.run_async(
                    storage.add_event,
                    body.run_id,
                    "memory_dreamed",
                    {
                        "dream_id": report["id"],
                        "trigger": "api",
                        "corrections_count": report["corrections_count"],
                        "pages_written_count": report["pages_written_count"],
                        "links_repaired_count": report["links_repaired_count"],
                    },
                )
            return report
        finally:
            close_fn = getattr(llm_prov, "aclose", None)
            if close_fn:
                with contextlib.suppress(Exception):
                    await close_fn()

    @app.post("/api/admin/gc")
    async def admin_gc(request: Request, retention_seconds: float = Query(default=86400.0, ge=0.0)):
        require_roles(get_principal(request), {"admin"}, "需要 admin 角色才能执行资源回收")
        storage_gc, containers_gc = await asyncio.gather(
            storage.run_async(
                storage.gc_expired_resources,
                workspaces_dir=workspaces,
                git_origins_dir=git_origins,
                artifacts_dir=artifacts_root,
                retention_seconds=retention_seconds,
            ),
            asyncio.to_thread(reap_orphan_containers),
        )
        with repomap_lock:
            stale_keys = [k for k in app.state.repomap_cache if not Path(k).exists()]
            for k in stale_keys:
                app.state.repomap_cache.pop(k, None)
        return {"ok": True, "storage_gc": storage_gc, "containers_gc": containers_gc}

    @app.post("/api/internal/sandbox/execute")
    async def internal_sandbox_execute(request: Request):
        require_roles(get_principal(request), {"admin"}, "需要 admin 角色或有效沙箱令牌")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "非法的 JSON 请求体") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        return await asyncio.to_thread(execute_remote_sandbox_payload, payload)

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "web/index.html")

    return app

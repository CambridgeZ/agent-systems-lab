"""本机工作台与多租户控制面 API：统一承载内联协程调度（inline）与持久队列调度（queue）。

核心安全边界、并发隔离与可观测设计：
1. 多租户宿主目录隔离（_can_use_direct_workspace）：
   - 未开启鉴权的单机开发模式允许直接挂载本地工程目录（仍受敏感系统路径黑名单与白名单根约束）；
   - 一旦启用多租户令牌鉴权，仅 global_admin 或本机 local 租户的 admin 角色可使用 local_cwd / workspace_path，
     普通外部租户被严格限制在独立临时沙箱与租户私有记忆空间内，杜绝借直连工作区或记忆 API 越权读写宿主机。
2. 出带睡眠任务池（dream_jobs）与主任务槽位（jobs）解耦：
   - inline 模式下主任务池严格限制最多 4 个并发运行（防本机 CPU/API 配额过载）；
   - 会话完成后的后台自动 Dreaming 放入独立 dream_jobs 而非占用 jobs[run_id]：既不挤占 4 并发槽位误报 429，
     也不会在用户立即发送下一轮追问（POST /api/runs/{id}/messages）时触发“任务正在运行”的 409 假冲突。
3. 多模态产物防 XSS 沙箱响应头（Content-Security-Policy）：
   - 截图与文本产物端点除校验租户归属、扩展名白名单（禁绝 html/svg）与防软链越界外，强制下发
     `Content-Security-Policy: default-src 'none'; img-src 'self'` 与 `Cache-Control: private`，
     彻底封死浏览器同源脚本执行与反向代理缓存串号。
4. 分层长期记忆控制面（/api/memory/*）：
   - 提供双层（global / project）Wiki 概览、互链图谱、混合 RRF 检索、人工修订与按需离线 Dreaming 闭环。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict, deque
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import struct
import tempfile
import threading
import time
from typing import Any, Literal
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .a2a import (
    A2A_REDACTED_MARKER,
    A2A_TASK_NOT_CANCELABLE,
    A2A_TASK_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    A2AClientManager,
    build_agent_card,
    build_artifact_update_event,
    build_status_update_event,
    deliver_a2a_push_notification,
    discover_a2a_config_path,
    extract_a2a_message_parts,
    is_terminal_a2a_state,
    jsonrpc_error,
    jsonrpc_success,
    load_a2a_config,
    normalize_a2a_method,
    redact_a2a_document,
    run_status_to_a2a_state,
    run_to_a2a_task,
    save_a2a_config,
    validate_a2a_document,
    validate_a2a_url,
)
from .checkpoints import CheckpointError
from .context import ContextOverflow
from .eval_flywheel import grade_trajectory, harvest_failure_traces
from .execution_lock import RunBusy
from .gateways import (
    CANONICAL_PROTOCOLS,
    DEFAULT_BASE_URLS,
    REDACTED_SECRET_MARKER,
    build_provider_from_profile,
    discover_gateways_config_path,
    load_gateways_config,
    normalize_protocol,
    redact_gateways_document,
    resolve_provider_or_gateway,
    save_gateways_config,
    set_active_gateway_profile,
    validate_gateways_document,
)
from .git_repo import GitRepoError, create_task_worktree, ensure_git_origin_atomic, is_git_worktree
from .hooks import HOOK_EVENTS
from .horizon import HorizonCampaignManager, MechanicalInvariantChecker
from .host_tools import (
    _ALWAYS_SKIP_DIRS,
    _FORBIDDEN_DOT_PARTS,
    HostToolError,
    is_host_secret_segment,
)
from .kv_cache import canonical_json, simulate_kv_cache_ablation, summarize_kv_cache
from .memory import DreamEngine, MemoryError, MemoryStore, compute_project_slug
from .observability import (
    METRICS,
    bind_trace_context,
    format_traceparent,
    get_trace_id,
    redact_secrets,
    sanitize_error_detail,
)
from .permissions import (
    is_workspace_trusted,
    load_settings_layers,
    normalize_permission_mode,
    trust_workspace,
    untrust_workspace,
    validate_direct_workspace_path,
)
from .providers import DEFAULT_MODEL_PRICING_USD_PER_MTOK, Provider, get_provider
from .repomap import RepoMapIndex
from .retrieval import KnowledgeBase
from .runtime import AgentRuntime
from .sandbox import discover_test_oracles, execute_remote_sandbox_payload, reap_orphan_containers
from .skills import SkillCatalog
from .storage import StaleEpochError, Storage, digest
from .streaming import RunStreamBroker
from .triggers import (
    CronSchedule,
    TriggerScheduler,
    render_webhook_prompt,
    verify_webhook_signature,
)
from .working_memory import audit_working_memory, has_working_memory_content

ROOT = Path(__file__).resolve().parents[2]
VALID_ROLES = frozenset({"operator", "approver", "admin"})
SESSION_COOKIE_NAME = "agent_lab_session"
_SESSION_TTL_SECONDS = 86_400.0
_MAX_COOKIE_SESSIONS = 1024
_MAX_COOKIE_SESSIONS_PER_TENANT = 64
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_TRIGGER_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/@\[\]-]{1,128}$")
_ATTACHMENT_PATH_RE = re.compile(r"^/api/runs/[A-Za-z0-9_-]{1,128}/attachments$")
_WEBHOOK_PATH_RE = re.compile(r"^/api/webhooks/([A-Za-z0-9_-][A-Za-z0-9_.-]{0,127})$")
_SAFE_UPLOAD_ID_RE = re.compile(r"^upl_[a-f0-9]{32}$")
_SAFE_PROC_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_ARTIFACT_SERVE_RE = re.compile(
    r"^[A-Za-z0-9_.-]+\.(png|jpg|jpeg|webp|gif|pdf|txt|md|json)$", re.IGNORECASE
)
_AT_FILE_RE = re.compile(r"(?<![A-Za-z0-9._+-])@([A-Za-z0-9_./-]{1,180})")
_MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
_STAGED_UPLOAD_TTL_SECONDS = 3600.0
_MAX_STAGED_UPLOADS_PER_TENANT = 64
_BROWSE_MAX_DIRS = 120
_MAX_SETTINGS_BYTES = 256 * 1024
_MAX_REPOMAP_CACHE = 32
_WORKSPACE_FILES_SKIP_DIRS = (
    _ALWAYS_SKIP_DIRS
    | _FORBIDDEN_DOT_PARTS
    | frozenset({".tox", "dist", "build", ".next", ".nuxt", "target"})
)
_STEERING_MSG_PREFIX = "[用户运行中实时补充指令]\n"
_INTERNAL_USER_MSG_PREFIXES = (
    "REVIEW_APPROVED:",
    "REVIEW_REJECTED:",
    "[SessionStart Hook 上下文]",
    "[UserPromptSubmit Hook 上下文]",
    "[Stop 钩子否决结束",
    "[用户转向接管 (superseded_by_user_steering)",
    "不可信 worker 建议：",
    "固定检索阶段（不可信数据）：",
    "上一条回复因达到单轮输出长度上限被截断",
    "HARNESS_STATE\n",
    "WORKING_MEMORY",
    "COMPACTION_SUMMARY",
    "MCP_SERVER_DIRECTORY",
    "PINNED_MEMORY",
)
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#111827"/>'
    '<path d="M9 22L16 8l7 14h-3.3l-1.4-3H13.7l-1.4 3H9zm5.8-5.6h2.4L16 13.7l-1.2 2.7z" fill="#60a5fa"/>'
    '<circle cx="24" cy="9" r="2.5" fill="#34d399"/>'
    "</svg>"
)
_ATTACHMENT_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
_ARTIFACT_SERVE_MIME: dict[str, str] = {
    **_ATTACHMENT_EXT_TO_MIME,
    ".json": "application/json",
}
_ATTACHMENT_MIME_TO_EXTS: dict[str, frozenset[str]] = {
    "image/png": frozenset({".png"}),
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/webp": frozenset({".webp"}),
    "image/gif": frozenset({".gif"}),
    "application/pdf": frozenset({".pdf"}),
    "text/plain": frozenset({".txt"}),
    "text/markdown": frozenset({".md"}),
}


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


class AuthSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str | None = Field(default=None, min_length=1, max_length=512)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)
    mode: Literal["agent", "workflow", "parallel"] = "agent"
    template: Literal["sample", "multi_file", "local_cwd"] = "sample"
    workspace_path: str | None = Field(default=None, max_length=512)
    execution_profile: Literal["sandbox", "host", "computer"] | None = None
    permission_mode: Literal["ask", "auto-edit", "full-auto", "plan"] | None = None
    enable_gui: bool = False
    isolate_worktree: bool = False
    model: str | None = Field(default=None, max_length=128)
    gateway_profile: str | None = Field(default=None, max_length=64)
    max_cost_usd: float | None = Field(default=None, gt=0.0, le=1000.0)
    attachments: list[dict[str, Any]] | None = Field(default=None, max_length=8)


class GatewayActivateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str = Field(min_length=1, max_length=64)


class FollowUpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=128)
    max_cost_usd: float | None = Field(default=None, gt=0.0, le=1000.0)
    attachments: list[dict[str, Any]] | None = Field(default=None, max_length=8)


class UpdateRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class ForkRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    up_to_message_idx: int | None = Field(default=None, ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class RewindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkpoint_id: str = Field(min_length=1, max_length=128)
    scope: Literal["both", "files", "messages"] = "both"


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=160)
    media_type: str | None = Field(default=None, max_length=80)
    mime_type: str | None = Field(default=None, max_length=80)
    content_base64: str | None = None
    base64_data: str | None = None
    data: str | None = None
    width: int | None = Field(default=None, ge=1, le=16384)
    height: int | None = Field(default=None, ge=1, le=16384)
    turn_message_idx: int | None = Field(default=None, ge=0)


class PermissionModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission_mode: Literal["ask", "auto-edit", "full-auto", "plan"]


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool
    permission_mode: Literal["ask", "auto-edit", "full-auto", "plan"] | None = None
    grant_pattern: str | None = Field(default=None, max_length=200)
    selected_files: list[str] | None = Field(default=None, max_length=32)
    answer_text: str | None = Field(default=None, max_length=4000)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=r"^[\w\- ]+$")
    value: str = Field(max_length=2000)


class MemoryPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Literal["global", "project"] = "project"
    path: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=160)
    content: str = Field(default="", max_length=12000)
    status: Literal["confirmed", "hypothesis"] = "confirmed"
    sources: list[str] | None = Field(default=None, max_length=12)
    index_summary: str | None = Field(default=None, max_length=180)
    run_id: str | None = Field(default=None, max_length=64)
    workspace_path: str | None = Field(default=None, max_length=512)
    sync_workspace_file: bool = False


class CompactRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_recent_groups: int = Field(default=2, ge=1, le=20)
    focus: str = Field(default="", max_length=240)
    use_llm: bool = False


class DreamInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, max_length=64)
    focus_goal: str | None = Field(default=None, max_length=400)
    workspace_path: str | None = Field(default=None, max_length=512)
    dry_run: bool = False


class DreamRollbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str | None = Field(default=None, max_length=80)
    run_id: str | None = Field(default=None, max_length=64)
    workspace_path: str | None = Field(default=None, max_length=512)


class SettingsPermissionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deny: list[str] = Field(default_factory=list, max_length=200)
    ask: list[str] = Field(default_factory=list, max_length=200)
    allow: list[str] = Field(default_factory=list, max_length=200)


class SettingsRulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["global", "project"] = "global"
    workspace_path: str | None = Field(default=None, max_length=512)
    permissions: SettingsPermissionsInput | None = None
    deny: list[str] | None = Field(default=None, max_length=200)
    ask: list[str] | None = Field(default=None, max_length=200)
    allow: list[str] | None = Field(default=None, max_length=200)
    hooks: dict[str, list[dict[str, Any]]] | None = None
    trust_workspace: bool | None = None


class A2ADiscoverInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peer: str | None = Field(default=None, max_length=128)
    url: str | None = Field(default=None, max_length=1024)
    allow_private_network: bool = False


class A2ASendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peer: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=8000)
    task_id: str | None = Field(default=None, max_length=128)
    context_id: str | None = Field(default=None, max_length=128)
    blocking: bool = True
    data: dict[str, Any] | None = None


class HorizonFeatureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=48)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1200)
    verification_cmd: str | None = Field(default=None, max_length=400)


class HorizonCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=1200)
    features: list[HorizonFeatureInput] = Field(min_length=1, max_length=200)
    smoke_cmd: str | None = Field(default="pytest -q", max_length=400)
    campaign_id: str | None = Field(default=None, max_length=64)
    workspace_path: str | None = Field(default=None, max_length=512)


class HorizonLintInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_path: str | None = Field(default=None, max_length=512)


class TriggerCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["cron", "webhook"]
    cron_expr: str | None = Field(default=None, max_length=128)
    cron: str | None = Field(default=None, max_length=128)
    prompt_template: str | None = Field(default=None, max_length=16000)
    prompt: str | None = Field(default=None, max_length=16000)
    webhook_secret: str | None = Field(default=None, max_length=512)
    workspace_path: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    timezone: str | None = Field(default=None, max_length=64)
    mode: Literal["agent", "workflow", "parallel"] | None = None
    template: Literal["sample", "multi_file", "local_cwd"] | None = None
    metadata: dict[str, Any] | None = None


class EvalHarvestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=200)
    min_friction: int = Field(default=1, ge=0, le=50)


class _TenantScopedStorageProxy:
    """将轨迹采集限定在单个租户边界内的只读 Storage 视图，防止多租户下跨租户扫库或事件越权。"""

    def __init__(self, storage: Storage, tenant_id: str) -> None:
        self._storage = storage
        self._tenant_id = tenant_id
        self._verified_run_ids: set[str] = set()

    def _belongs_to_tenant(self, run: dict[str, Any] | None) -> bool:
        if not isinstance(run, dict):
            return False
        stored_tenant = str(
            run.get("tenant_id") or (run.get("metadata") or {}).get("tenant_id") or "local"
        )
        return stored_tenant == self._tenant_id

    def list_runs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        runs = self._storage.list_runs(tenant_id=self._tenant_id, limit=limit)
        for item in runs:
            rid = str(item.get("id") or "").strip()
            if rid:
                self._verified_run_ids.add(rid)
        return runs

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._storage.get_run(run_id)
        if not self._belongs_to_tenant(run):
            return None
        self._verified_run_ids.add(run_id)
        return run

    def events(self, run_id: str) -> list[dict[str, Any]]:
        if run_id not in self._verified_run_ids and self.get_run(run_id) is None:
            return []
        return self._storage.events(run_id)

    def pending_steering_messages(self, run_id: str) -> list[dict[str, Any]]:
        if run_id not in self._verified_run_ids and self.get_run(run_id) is None:
            return []
        return self._storage.pending_steering_messages(run_id)


def _mask_trigger_public(trigger: dict[str, Any]) -> dict[str, Any]:
    """脱敏触发器记录：若配置了 webhook_secret 则始终掩码为 ***REDACTED***，永不返回明文密钥。"""
    out = dict(trigger)
    raw_secret = out.get("webhook_secret")
    has_secret = bool(raw_secret) or bool(out.get("has_webhook_secret"))
    out["has_webhook_secret"] = has_secret
    out["webhook_secret"] = REDACTED_SECRET_MARKER if has_secret else None
    out.pop("metadata_json", None)
    raw_meta = out.get("metadata")
    if isinstance(raw_meta, dict):
        safe_meta = dict(raw_meta)
        safe_meta.pop("direct_workspace_authorized", None)
        out["metadata"] = safe_meta
    return out


def _scoped_webhook_dedup_key(trigger_id: str, base_dedup_key: str) -> str:
    """将 Webhook 去重键绑定到 trigger_id 维度并控制在 256 字符上限内，防止跨租户/跨触发器碰撞。"""
    candidate = f"{trigger_id}:{base_dedup_key}"
    if len(candidate) <= 256:
        return candidate
    suffix = hashlib.sha256(base_dedup_key.encode("utf-8")).hexdigest()[:16]
    prefix_cap = max(1, 256 - len(trigger_id) - 2 - len(suffix))
    return f"{trigger_id}:{base_dedup_key[:prefix_cap].rstrip(':')}:{suffix}"


def _clean_model_override(raw: str | None) -> str | None:
    if raw is None:
        return None
    clean = raw.strip()
    if not clean:
        return None
    if not _SAFE_MODEL_RE.fullmatch(clean):
        raise ValueError("非法的模型名称格式")
    return clean


def _probe_png_dimensions(raw_bytes: bytes) -> tuple[int | None, int | None]:
    if len(raw_bytes) >= 24 and raw_bytes[:8] == b"\x89PNG\r\n\x1a\n" and raw_bytes[12:16] == b"IHDR":
        width, height = struct.unpack(">II", raw_bytes[16:24])
        if 1 <= width <= 16384 and 1 <= height <= 16384:
            return int(width), int(height)
    return None, None


def _validate_attachment_filename(raw_name: str) -> tuple[str, str]:
    clean = str(raw_name or "").strip()
    if (
        not clean
        or "/" in clean
        or "\\" in clean
        or "\x00" in clean
        or ".." in clean
        or clean.startswith(".")
        or not _SAFE_FILENAME_RE.fullmatch(clean)
    ):
        raise ValueError("非法的附件文件名")
    ext = Path(clean).suffix.lower()
    if ext not in _ATTACHMENT_EXT_TO_MIME:
        raise ValueError("不支持的附件类型，仅允许 png/jpeg/webp/gif/pdf/txt/md")
    return clean, ext


def _validate_attachment_magic(raw_bytes: bytes, media_type: str) -> None:
    """校验解码后的附件魔数/编码安全性，防止伪造 MIME 或非法二进制伪装成合法媒体/文本。"""
    if media_type == "image/png":
        if not raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PNG 附件文件头魔数无效")
    elif media_type == "image/jpeg":
        if not raw_bytes.startswith(b"\xff\xd8\xff"):
            raise ValueError("JPEG 附件文件头魔数无效")
    elif media_type == "image/webp":
        if len(raw_bytes) < 12 or raw_bytes[:4] != b"RIFF" or raw_bytes[8:12] != b"WEBP":
            raise ValueError("WebP 附件文件头魔数无效")
    elif media_type == "image/gif":
        if not raw_bytes.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("GIF 附件文件头魔数无效")
    elif media_type == "application/pdf":
        if not raw_bytes.startswith(b"%PDF-"):
            raise ValueError("PDF 附件文件头魔数无效")
    elif media_type in {"text/plain", "text/markdown"}:
        if b"\x00" in raw_bytes:
            raise ValueError("文本附件不能包含 NUL 空字节")
        try:
            raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("文本附件必须是合法的 UTF-8 编码") from exc
    else:
        raise ValueError(f"不支持的附件 MIME 类型：{media_type!r}")


def _parse_attachment_payload(
    raw_item: dict[str, Any],
    *,
    allow_artifact_ref: bool = True,
    allow_upload_ref: bool = True,
) -> dict[str, Any]:
    if not isinstance(raw_item, dict):
        raise ValueError("附件必须是 JSON 对象")
    raw_b64 = raw_item.get("content_base64") or raw_item.get("base64_data") or raw_item.get("data")
    turn_idx_raw = raw_item.get("turn_message_idx")
    turn_msg_idx: int | None = None
    if turn_idx_raw is not None:
        if isinstance(turn_idx_raw, bool) or not isinstance(turn_idx_raw, int) or turn_idx_raw < 0:
            raise ValueError("turn_message_idx 必须是非负整数")
        turn_msg_idx = turn_idx_raw

    width_raw = raw_item.get("width")
    height_raw = raw_item.get("height")
    width = int(width_raw) if isinstance(width_raw, int) and not isinstance(width_raw, bool) and width_raw > 0 else None
    height = int(height_raw) if isinstance(height_raw, int) and not isinstance(height_raw, bool) and height_raw > 0 else None

    if isinstance(raw_b64, str) and raw_b64.strip():
        filename = str(raw_item.get("filename") or raw_item.get("artifact_name") or "").strip()
        clean_name, ext = _validate_attachment_filename(filename)
        b64_str = raw_b64.strip()
        data_uri_mime: str | None = None
        if b64_str.startswith("data:") and ";base64," in b64_str:
            header_part, b64_str = b64_str.split(";base64,", 1)
            data_uri_mime = header_part[5:].strip().lower() or None
        raw_mime = str(
            raw_item.get("media_type")
            or raw_item.get("mime_type")
            or data_uri_mime
            or _ATTACHMENT_EXT_TO_MIME[ext]
        ).strip().lower()
        if raw_mime == "image/jpg":
            raw_mime = "image/jpeg"
        allowed_exts = _ATTACHMENT_MIME_TO_EXTS.get(raw_mime)
        if allowed_exts is None or ext not in allowed_exts:
            raise ValueError(f"附件 MIME 类型 {raw_mime!r} 与扩展名 {ext!r} 不匹配或不受支持")
        # 粗略预检 base64 长度，避免解码超大无效字符串消耗内存
        compact_b64 = re.sub(r"\s+", "", b64_str)
        if len(compact_b64) > ((_MAX_ATTACHMENT_BYTES * 4) // 3) + 64:
            raise OverflowError("附件大小超过 6 MiB 上限")
        try:
            decoded = base64.b64decode(compact_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("非法的 base64 编码内容") from exc
        if not decoded:
            raise ValueError("附件内容不能为空")
        if len(decoded) > _MAX_ATTACHMENT_BYTES:
            raise OverflowError("附件大小超过 6 MiB 上限")
        _validate_attachment_magic(decoded, raw_mime)
        if raw_mime == "image/png" and (width is None or height is None):
            pw, ph = _probe_png_dimensions(decoded)
            width = width or pw
            height = height or ph
        return {
            "mode": "upload",
            "filename": clean_name,
            "ext": ext,
            "media_type": raw_mime,
            "raw_bytes": decoded,
            "width": width,
            "height": height,
            "turn_message_idx": turn_msg_idx,
        }

    upload_ref = str(raw_item.get("upload_id") or "").strip()
    if allow_upload_ref and upload_ref:
        if not _SAFE_UPLOAD_ID_RE.fullmatch(upload_ref):
            raise ValueError("非法的 upload_id 格式")
        return {
            "mode": "staged",
            "upload_id": upload_ref,
            "filename": str(raw_item.get("filename") or "").strip()[:160] or None,
            "width": width,
            "height": height,
            "turn_message_idx": turn_msg_idx,
        }

    art_ref = str(raw_item.get("artifact_name") or "").strip()
    if allow_artifact_ref and art_ref:
        clean_ref, ext = _validate_attachment_filename(art_ref)
        raw_mime = str(
            raw_item.get("media_type") or raw_item.get("mime_type") or _ATTACHMENT_EXT_TO_MIME[ext]
        ).strip().lower()
        if raw_mime == "image/jpg":
            raw_mime = "image/jpeg"
        allowed_exts = _ATTACHMENT_MIME_TO_EXTS.get(raw_mime)
        if allowed_exts is None or ext not in allowed_exts:
            raise ValueError(f"附件 MIME 类型 {raw_mime!r} 不受支持")
        return {
            "mode": "ref",
            "filename": str(raw_item.get("filename") or clean_ref).strip()[:160],
            "artifact_name": clean_ref,
            "media_type": raw_mime,
            "width": width,
            "height": height,
            "turn_message_idx": turn_msg_idx,
        }

    raise ValueError("附件缺少有效的 base64 内容或 upload_id")


def _write_attachment_artifact(
    artifacts_root: Path,
    run_id: str,
    parsed: dict[str, Any],
    *,
    actor_id: str,
    turn_message_idx: int | None = None,
    steering_id: int | None = None,
) -> dict[str, Any]:
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise ValueError("非法的 run_id")
    if artifacts_root.is_symlink():
        raise ValueError("artifacts 根目录不能是符号链接")
    raw_run_dir = artifacts_root / run_id
    if raw_run_dir.is_symlink():
        raise ValueError("任务产物目录不能是符号链接")
    art_base = artifacts_root.resolve()
    run_art_dir = art_base / run_id
    if run_art_dir.is_symlink():
        raise ValueError("任务产物目录不能是符号链接")
    run_art_dir.mkdir(parents=True, exist_ok=True)
    if raw_run_dir.is_symlink() or run_art_dir.is_symlink():
        raise ValueError("任务产物目录不能是符号链接")
    run_art_resolved = run_art_dir.resolve()
    if run_art_resolved == art_base or not run_art_resolved.is_relative_to(art_base):
        raise ValueError("任务产物目录越界")

    eff_turn_idx = turn_message_idx if turn_message_idx is not None else parsed.get("turn_message_idx")
    now_iso = datetime.now(timezone.utc).isoformat()

    if parsed["mode"] == "ref":
        art_name = parsed["artifact_name"]
        raw_target = run_art_resolved / art_name
        if raw_target.is_symlink():
            raise FileNotFoundError(f"引用的附件产物不存在：{art_name}")
        target = raw_target.resolve()
        if (
            target == art_base
            or target == run_art_resolved
            or not target.is_relative_to(art_base)
            or not target.is_relative_to(run_art_resolved)
        ):
            raise FileNotFoundError(f"引用的附件产物不存在：{art_name}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(raw_target, flags)
        except OSError as exc:
            raise FileNotFoundError(f"引用的附件产物不存在：{art_name}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise FileNotFoundError(f"引用的附件产物不存在：{art_name}")
            if st.st_size > _MAX_ATTACHMENT_BYTES:
                raise OverflowError("附件大小超过 6 MiB 上限")
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                raw_bytes = fh.read(_MAX_ATTACHMENT_BYTES + 1)
            if len(raw_bytes) > _MAX_ATTACHMENT_BYTES:
                raise OverflowError("附件大小超过 6 MiB 上限")
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        rec: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "filename": parsed["filename"],
            "artifact_name": art_name,
            "media_type": parsed["media_type"],
            "size_bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "url": f"/api/runs/{run_id}/artifacts/{art_name}",
            "uploaded_by": actor_id,
            "uploaded_at": now_iso,
        }
    else:
        raw_bytes = parsed["raw_bytes"]
        ext = parsed["ext"]
        stem = Path(parsed["filename"]).stem[:80]
        art_name = f"att-{uuid.uuid4().hex[:10]}-{stem}{ext}"
        raw_target = run_art_resolved / art_name
        if raw_target.is_symlink():
            raise ValueError("附件存储路径不能是符号链接")
        target = raw_target.resolve()
        if (
            target == art_base
            or target == run_art_resolved
            or not target.is_relative_to(art_base)
            or not target.is_relative_to(run_art_resolved)
        ):
            raise ValueError("附件存储路径越界")
        fd, tmp_name = tempfile.mkstemp(prefix=".att-", dir=run_art_resolved)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, raw_target)
        finally:
            if os.path.exists(tmp_name):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
        rec = {
            "id": uuid.uuid4().hex[:12],
            "filename": parsed["filename"],
            "artifact_name": art_name,
            "media_type": parsed["media_type"],
            "size_bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "url": f"/api/runs/{run_id}/artifacts/{art_name}",
            "uploaded_by": actor_id,
            "uploaded_at": now_iso,
        }
    if parsed.get("width") is not None:
        rec["width"] = int(parsed["width"])
    if parsed.get("height") is not None:
        rec["height"] = int(parsed["height"])
    if eff_turn_idx is not None:
        rec["turn_message_idx"] = int(eff_turn_idx)
    if steering_id is not None:
        rec["steering_id"] = int(steering_id)
    return rec


def _cleanup_written_attachments(
    artifacts_root: Path, run_id: str, records: list[dict[str, Any]]
) -> None:
    """在附件写库失败或并发超限时安全清理本次新写入磁盘的孤儿产物文件（严禁跟随软链）。"""
    if not records or not _SAFE_RUN_ID_RE.fullmatch(run_id) or artifacts_root.is_symlink():
        return
    art_base = artifacts_root.resolve()
    raw_run_dir = artifacts_root / run_id
    if raw_run_dir.is_symlink() or not raw_run_dir.is_dir():
        return
    run_art_resolved = raw_run_dir.resolve()
    if run_art_resolved == art_base or not run_art_resolved.is_relative_to(art_base):
        return
    for rec in records:
        art_name = rec.get("artifact_name")
        if (
            not isinstance(art_name, str)
            or not art_name
            or "/" in art_name
            or "\\" in art_name
            or ".." in art_name
        ):
            continue
        raw_target = run_art_resolved / art_name
        if raw_target.is_symlink():
            continue
        target = raw_target.resolve()
        if (
            target != art_base
            and target != run_art_resolved
            and target.is_relative_to(art_base)
            and target.is_relative_to(run_art_resolved)
        ):
            with contextlib.suppress(OSError):
                raw_target.unlink(missing_ok=True)


def _tenant_upload_slug(tenant_id: str) -> str:
    raw = str(tenant_id or "local").strip() or "local"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_") or "tenant"
    tag = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{clean[:40]}-{tag}"


def _resolve_tenant_staged_dir(
    staged_root: Path, tenant_id: str, *, create: bool = False
) -> Path:
    if staged_root.is_symlink():
        raise ValueError("staged_uploads 根目录不能是符号链接")
    if create:
        staged_root.mkdir(parents=True, exist_ok=True)
    if staged_root.is_symlink():
        raise ValueError("staged_uploads 根目录不能是符号链接")
    base = staged_root.resolve()
    slug = _tenant_upload_slug(tenant_id)
    raw_dir = base / slug
    if raw_dir.is_symlink():
        raise ValueError("租户暂存上传目录不能是符号链接")
    if create:
        raw_dir.mkdir(parents=True, exist_ok=True)
        if raw_dir.is_symlink():
            raise ValueError("租户暂存上传目录不能是符号链接")
    resolved = raw_dir.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError("租户暂存上传目录越界")
    return resolved


def _prune_expired_staged_uploads(
    staged_root: Path,
    *,
    max_age_seconds: float = _STAGED_UPLOAD_TTL_SECONDS,
    tenant_dir: Path | None = None,
) -> int:
    """清理过期的暂存上传文件（严禁跟随软链）。"""
    if staged_root.is_symlink() or not staged_root.is_dir():
        return 0
    base = staged_root.resolve()
    cutoff = time.time() - max(0.0, float(max_age_seconds))
    removed = 0
    scan_dirs: list[Path] = []
    if tenant_dir is not None:
        if not tenant_dir.is_symlink() and tenant_dir.is_dir():
            resolved_td = tenant_dir.resolve()
            if resolved_td != base and resolved_td.is_relative_to(base):
                scan_dirs.append(resolved_td)
    else:
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    cand = (base / entry.name).resolve()
                    if cand != base and cand.is_relative_to(base):
                        scan_dirs.append(cand)
        except OSError:
            return 0

    for tdir in scan_dirs:
        try:
            with os.scandir(tdir) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_mtime <= cutoff:
                raw_p = tdir / entry.name
                if not raw_p.is_symlink() and raw_p.resolve().is_relative_to(tdir):
                    with contextlib.suppress(OSError):
                        raw_p.unlink(missing_ok=True)
                        removed += 1
    return removed


def _write_staged_upload(
    staged_root: Path,
    tenant_id: str,
    actor_id: str,
    parsed: dict[str, Any],
) -> dict[str, Any]:
    tenant_dir = _resolve_tenant_staged_dir(staged_root, tenant_id, create=True)
    _prune_expired_staged_uploads(staged_root, tenant_dir=tenant_dir)
    active_count = 0
    with os.scandir(tenant_dir) as it:
        for entry in it:
            if (
                not entry.is_symlink()
                and entry.is_file(follow_symlinks=False)
                and _SAFE_UPLOAD_ID_RE.fullmatch(entry.name)
            ):
                active_count += 1
    if active_count >= _MAX_STAGED_UPLOADS_PER_TENANT:
        raise ValueError("当前租户暂存上传数量已达上限，请先发送或稍后再试")

    upload_id = f"upl_{uuid.uuid4().hex}"
    raw_bytes: bytes = parsed["raw_bytes"]
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    now_iso = datetime.now(timezone.utc).isoformat()
    blob_target = tenant_dir / upload_id
    meta_target = tenant_dir / f"{upload_id}.meta.json"
    if blob_target.is_symlink() or meta_target.is_symlink():
        raise ValueError("暂存上传目标路径不能是符号链接")

    meta_doc: dict[str, Any] = {
        "upload_id": upload_id,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "filename": parsed["filename"],
        "ext": parsed["ext"],
        "media_type": parsed["media_type"],
        "size_bytes": len(raw_bytes),
        "sha256": sha256_hex,
        "created_at": now_iso,
    }
    if parsed.get("width") is not None:
        meta_doc["width"] = int(parsed["width"])
    if parsed.get("height") is not None:
        meta_doc["height"] = int(parsed["height"])

    tmp_blob = ""
    tmp_meta = ""
    try:
        fd_b, tmp_blob = tempfile.mkstemp(prefix=".upl-", dir=tenant_dir)
        with os.fdopen(fd_b, "wb") as fh:
            fh.write(raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_blob, 0o600)

        meta_bytes = (json.dumps(meta_doc, ensure_ascii=False) + "\n").encode("utf-8")
        fd_m, tmp_meta = tempfile.mkstemp(prefix=".upl-meta-", dir=tenant_dir)
        with os.fdopen(fd_m, "wb") as fh:
            fh.write(meta_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_meta, 0o600)

        os.replace(tmp_meta, meta_target)
        tmp_meta = ""
        os.replace(tmp_blob, blob_target)
        tmp_blob = ""
    finally:
        for leftover in (tmp_blob, tmp_meta):
            if leftover and os.path.exists(leftover):
                with contextlib.suppress(OSError):
                    os.unlink(leftover)

    resp: dict[str, Any] = {
        "ok": True,
        "upload_id": upload_id,
        "filename": parsed["filename"],
        "media_type": parsed["media_type"],
        "size_bytes": len(raw_bytes),
        "sha256": sha256_hex,
    }
    if parsed.get("width") is not None:
        resp["width"] = int(parsed["width"])
    if parsed.get("height") is not None:
        resp["height"] = int(parsed["height"])
    return resp


def _read_regular_file_nofollow(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise FileNotFoundError("symlink_forbidden")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FileNotFoundError("open_failed") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise FileNotFoundError("not_unique_regular_file")
        if st.st_size <= 0:
            raise ValueError("暂存文件内容为空")
        if st.st_size > max_bytes:
            raise OverflowError("附件大小超过 6 MiB 上限")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OverflowError("附件大小超过 6 MiB 上限")
        return data
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def _claim_staged_upload(
    staged_root: Path,
    tenant_id: str,
    staged_item: dict[str, Any],
) -> tuple[dict[str, Any], tuple[Path, Path, Path]]:
    """校验租户归属、防软链/硬链逃逸与完整性后原子认领暂存文件，返回 (parsed_upload, (claimed_blob, orig_blob, meta_path))。"""
    upload_id = str(staged_item.get("upload_id") or "").strip()
    if not _SAFE_UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("非法的 upload_id 格式")
    tenant_dir = _resolve_tenant_staged_dir(staged_root, tenant_id, create=False)
    if not tenant_dir.is_dir():
        raise FileNotFoundError(f"暂存上传不存在：{upload_id}")
    blob_path = tenant_dir / upload_id
    meta_path = tenant_dir / f"{upload_id}.meta.json"
    if blob_path.is_symlink() or meta_path.is_symlink():
        raise FileNotFoundError(f"暂存上传不存在：{upload_id}")
    if (
        not blob_path.resolve().is_relative_to(tenant_dir)
        or not meta_path.resolve().is_relative_to(tenant_dir)
    ):
        raise FileNotFoundError(f"暂存上传不存在：{upload_id}")

    try:
        meta_raw = _read_regular_file_nofollow(meta_path, max_bytes=16 * 1024)
        meta_doc = json.loads(meta_raw.decode("utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, ValueError) as exc:
        raise FileNotFoundError(f"暂存上传不存在：{upload_id}") from exc
    if (
        not isinstance(meta_doc, dict)
        or meta_doc.get("tenant_id") != tenant_id
        or meta_doc.get("upload_id") != upload_id
    ):
        raise FileNotFoundError(f"暂存上传不存在：{upload_id}")

    raw_bytes = _read_regular_file_nofollow(blob_path, max_bytes=_MAX_ATTACHMENT_BYTES)
    expected_sha = str(meta_doc.get("sha256") or "")
    if not expected_sha or hashlib.sha256(raw_bytes).hexdigest() != expected_sha:
        raise ValueError(f"暂存上传校验和不一致：{upload_id}")

    clean_name, ext = _validate_attachment_filename(str(meta_doc.get("filename") or ""))
    media_type = str(meta_doc.get("media_type") or _ATTACHMENT_EXT_TO_MIME[ext]).strip().lower()
    allowed_exts = _ATTACHMENT_MIME_TO_EXTS.get(media_type)
    if allowed_exts is None or ext not in allowed_exts:
        raise ValueError(f"暂存上传 MIME 类型非法：{media_type!r}")
    _validate_attachment_magic(raw_bytes, media_type)

    width = staged_item.get("width") or meta_doc.get("width")
    height = staged_item.get("height") or meta_doc.get("height")
    if media_type == "image/png" and (width is None or height is None):
        pw, ph = _probe_png_dimensions(raw_bytes)
        width = width or pw
        height = height or ph

    claimed_path = tenant_dir / f".claimed-{upload_id}-{uuid.uuid4().hex[:8]}"
    try:
        os.replace(blob_path, claimed_path)
    except OSError as exc:
        raise FileNotFoundError(f"暂存上传已被消费或不存在：{upload_id}") from exc

    parsed_upload = {
        "mode": "upload",
        "filename": clean_name,
        "ext": ext,
        "media_type": media_type,
        "raw_bytes": raw_bytes,
        "width": int(width) if isinstance(width, int) and not isinstance(width, bool) and width > 0 else None,
        "height": int(height) if isinstance(height, int) and not isinstance(height, bool) and height > 0 else None,
        "turn_message_idx": staged_item.get("turn_message_idx"),
    }
    return parsed_upload, (claimed_path, blob_path, meta_path)


def _finalize_staged_claims(
    staged_root: Path, claims: list[tuple[Path, Path, Path]]
) -> None:
    if not claims or staged_root.is_symlink():
        return
    base = staged_root.resolve()
    for claimed_path, _orig_blob, meta_path in claims:
        for p in (claimed_path, meta_path):
            if p.is_symlink():
                continue
            with contextlib.suppress(OSError):
                if p.resolve().is_relative_to(base):
                    p.unlink(missing_ok=True)


def _restore_staged_claims(
    staged_root: Path, claims: list[tuple[Path, Path, Path]]
) -> None:
    if not claims or staged_root.is_symlink():
        return
    base = staged_root.resolve()
    for claimed_path, orig_blob, _meta_path in claims:
        if claimed_path.is_symlink() or orig_blob.is_symlink():
            continue
        with contextlib.suppress(OSError):
            if (
                claimed_path.exists()
                and not orig_blob.exists()
                and claimed_path.resolve().is_relative_to(base)
                and orig_blob.parent.resolve().is_relative_to(base)
            ):
                os.replace(claimed_path, orig_blob)


def _parse_attachment_list(
    raw_items: list[dict[str, Any]] | None,
    *,
    allow_artifact_ref: bool,
    allow_upload_ref: bool,
) -> list[dict[str, Any]]:
    if not raw_items:
        return []
    if len(raw_items) > 32:
        raise ValueError("单会话最多保留 32 个用户附件")
    parsed: list[dict[str, Any]] = []
    seen_upload_ids: set[str] = set()
    for raw_att in raw_items:
        item = _parse_attachment_payload(
            raw_att,
            allow_artifact_ref=allow_artifact_ref,
            allow_upload_ref=allow_upload_ref,
        )
        if item.get("mode") == "staged":
            uid = str(item["upload_id"])
            if uid in seen_upload_ids:
                raise ValueError(f"不能重复引用同一个 upload_id：{uid}")
            seen_upload_ids.add(uid)
        parsed.append(item)
    return parsed


def _safe_read_workspace_text(
    workspace: Path, rel_path: str, *, max_bytes: int = 128 * 1024
) -> str | None:
    """安全只读工作区内相对路径普通文本文件（拦截路径穿越、符号链接、硬链接与敏感文件名）。"""
    clean = str(rel_path or "").strip().lstrip("./")
    if not clean or clean.startswith("/") or "\\" in clean or "\x00" in clean:
        return None
    parts = Path(clean).parts
    if (
        not parts
        or ".." in parts
        or any(seg in _FORBIDDEN_DOT_PARTS or is_host_secret_segment(seg) for seg in parts)
    ):
        return None
    try:
        ws_root = workspace.resolve()
        cur = ws_root
        for seg in parts:
            cur = cur / seg
            if cur.is_symlink():
                return None
        resolved = cur.resolve()
        if not resolved.is_relative_to(ws_root) or not resolved.is_file():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(resolved), flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > max_bytes:
                return None
            raw = os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)
        if b"\x00" in raw or len(raw) > max_bytes:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _expand_at_file_mentions(
    user_text: str,
    workspace: Path,
    *,
    max_files: int = 4,
    max_chars_per_file: int = 2400,
    max_total_chars: int = 7800,
) -> str:
    """解析提示词中的 `@relative/path` 文件引用，安全附加受控工作区文件片段（严守总长度上限）。"""
    if "@" not in user_text or len(user_text) >= max_total_chars - 80:
        return user_text
    header = "\n\n[已引用工作区文件上下文]"
    remaining = max_total_chars - len(user_text) - len(header)
    if remaining < 64:
        return user_text

    attached: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _AT_FILE_RE.finditer(user_text):
        candidate = match.group(1).rstrip(".,;:!?)]}\"'")
        if not candidate or candidate in seen:
            continue
        content = _safe_read_workspace_text(workspace, candidate)
        if content is None:
            continue
        seen.add(candidate)
        prefix = f"\n--- @{candidate} ---\n"
        avail = min(max_chars_per_file, remaining - len(prefix) - 48)
        if avail < 32:
            break
        snippet = content[:avail]
        if len(content) > avail:
            snippet += f"\n... [已截断，总长 {len(content)} 字符]"
        attached.append((candidate, snippet))
        remaining -= len(prefix) + len(snippet)
        if len(attached) >= max_files or remaining < 64:
            break
    if not attached:
        return user_text
    blocks = [header]
    for rel, snippet in attached:
        blocks.append(f"--- @{rel} ---\n{snippet}")
    return user_text + "\n".join(blocks)


def _list_workspace_files(
    ws_root: Path,
    *,
    query: str = "",
    limit: int = 80,
    max_scanned: int = 10_000,
) -> dict[str, Any]:
    """裁剪遍历工作区内的常规非敏感非符号链接文件，供 Web `@file` 自动补全与文件浏览使用。"""
    root = ws_root.resolve()
    if ws_root.is_symlink() or not root.is_dir():
        return {"ok": True, "query": query, "count": 0, "truncated": False, "files": []}

    clean_q = query.strip().lstrip("@").lower()
    tokens = [tok for tok in clean_q.split() if tok]
    matches: list[str] = []
    scanned = 0
    truncated = False
    collect_cap = max(limit, 1500) if tokens else limit

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        cur_dir = Path(dirpath)
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            if d in _WORKSPACE_FILES_SKIP_DIRS or is_host_secret_segment(d):
                continue
            cand_dir = cur_dir / d
            if cand_dir.is_symlink():
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            scanned += 1
            if scanned > max_scanned:
                truncated = True
                break
            if is_host_secret_segment(fname):
                continue
            full_p = cur_dir / fname
            if full_p.is_symlink():
                continue
            try:
                st = full_p.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                continue
            try:
                rel_posix = full_p.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(
                seg in _FORBIDDEN_DOT_PARTS or is_host_secret_segment(seg)
                for seg in Path(rel_posix).parts
            ):
                continue
            if tokens:
                low = rel_posix.lower()
                if not all(tok in low for tok in tokens):
                    continue
            matches.append(rel_posix)
            if len(matches) >= collect_cap:
                truncated = True
                break
        if truncated:
            break

    if tokens and matches:
        needle = tokens[0]

        def _rank(rel_path: str) -> tuple[int, int, str]:
            low = rel_path.lower()
            base_low = low.rsplit("/", 1)[-1]
            if base_low == needle:
                tier = 0
            elif base_low.startswith(needle):
                tier = 1
            elif low.startswith(needle):
                tier = 2
            elif needle in base_low:
                tier = 3
            else:
                tier = 4
            return (tier, len(rel_path), low)

        matches.sort(key=_rank)

    if len(matches) > limit:
        matches = matches[:limit]
        truncated = True
    return {
        "ok": True,
        "query": query,
        "count": len(matches),
        "truncated": truncated,
        "files": matches,
    }


def _is_internal_user_message(content: Any) -> bool:
    if not isinstance(content, str):
        return True
    stripped = content.strip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in _INTERNAL_USER_MSG_PREFIXES)


def _sanitize_attachment_public(att: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "id",
        "filename",
        "artifact_name",
        "media_type",
        "mime_type",
        "size_bytes",
        "sha256",
        "url",
        "width",
        "height",
        "uploaded_by",
        "uploaded_at",
        "turn_message_idx",
        "steering_id",
    ):
        if key in att and att[key] is not None:
            out[key] = att[key]
    return out


def _build_conversation_thread(
    stored: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从 run['messages'] 与 meta['conversation_turns'] 确定性重建多轮对话线程与思考过程。

    返回值：(full_thread, followup_turns)
    - full_thread 包含从首轮（turn_index=0）到最新一轮的完整多轮记录（供 Web 全量聊天流渲染）；
    - followup_turns 为 full_thread[1:]（与 meta['conversation_turns'] 对齐，兼容既有长度契约）。
    """
    messages = stored.get("messages") if isinstance(stored.get("messages"), list) else []
    meta = stored.get("metadata") if isinstance(stored.get("metadata"), dict) else {}
    meta_turns = [t for t in (meta.get("conversation_turns") or []) if isinstance(t, dict)]
    raw_atts = [a for a in (meta.get("user_attachments") or []) if isinstance(a, dict)]
    status = str(stored.get("status") or "")
    is_terminal = status in {"completed", "failed", "cancelled", "budget_exceeded"}

    raw_segments: list[dict[str, Any]] = []
    current_seg: dict[str, Any] | None = None
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if _is_internal_user_message(content):
                continue
            assert isinstance(content, str)
            is_steering = content.startswith(_STEERING_MSG_PREFIX)
            clean_text = (
                content[len(_STEERING_MSG_PREFIX) :].strip()
                if is_steering
                else content.strip()
            )
            current_seg = {
                "message_idx": idx,
                "user_prompt": clean_text,
                "steering": is_steering,
                "thinking_parts": [],
                "tool_calls_count": 0,
                "final_text": "",
                "fallback_text": "",
            }
            raw_segments.append(current_seg)
        elif role == "assistant" and current_seg is not None:
            if isinstance(content, str):
                text_val = content.strip()
                if text_val:
                    current_seg["final_text"] = text_val
                    current_seg["fallback_text"] = text_val
            elif isinstance(content, list):
                step_texts: list[str] = []
                step_tool_uses = 0
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "thinking":
                        t_txt = str(blk.get("thinking") or blk.get("text") or "").strip()
                        if t_txt:
                            current_seg["thinking_parts"].append(t_txt)
                    elif btype == "text":
                        txt = str(blk.get("text") or "")
                        if txt.strip():
                            step_texts.append(txt)
                    elif btype == "tool_use":
                        step_tool_uses += 1
                current_seg["tool_calls_count"] += step_tool_uses
                joined_step_text = "\n".join(step_texts).strip()
                if joined_step_text:
                    current_seg["fallback_text"] = joined_step_text
                    if step_tool_uses == 0:
                        current_seg["final_text"] = joined_step_text

    if not raw_segments and stored.get("prompt"):
        raw_segments.append(
            {
                "message_idx": 0,
                "user_prompt": str(stored.get("prompt") or "").strip(),
                "steering": False,
                "thinking_parts": [],
                "tool_calls_count": 0,
                "final_text": str(stored.get("answer") or "").strip(),
                "fallback_text": str(stored.get("answer") or "").strip(),
            }
        )

    full_thread: list[dict[str, Any]] = []
    turn_steering_ids: list[set[Any]] = []
    meta_cursor = 0

    for seg_i, seg in enumerate(raw_segments):
        is_active_turn = (not is_terminal) and (seg_i == len(raw_segments) - 1)
        # 活跃回合执行工具期间不把中间工具前导语当作最终回答塞入 chat-thread，避免与实时流式面板重复闪烁；
        # 回合结束或历史轮次仍保留 fallback_text，确保中途异常中断时内容不丢失。
        answer_str = seg["final_text"] if is_active_turn else (seg["final_text"] or seg["fallback_text"] or "")
        thinking_str = "\n\n".join(seg["thinking_parts"])
        if seg_i == 0:
            started_at = stored.get("created_at")
            completed_at = meta.get("initial_turn_completed_at")
            actor_id = meta.get("created_by") or meta.get("actor_id")
            full_thread.append(
                {
                    "turn_index": 0,
                    "message_idx": seg["message_idx"],
                    "user_prompt": seg["user_prompt"],
                    "prompt": seg["user_prompt"],
                    "assistant_answer": answer_str,
                    "thinking": thinking_str,
                    "tool_calls_count": int(seg["tool_calls_count"]),
                    "attachments": [],
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "created_at": started_at,
                    "actor_id": actor_id,
                    "steering": False,
                }
            )
            turn_steering_ids.append(set())
            continue

        matched_batch: list[dict[str, Any]] = []
        if seg["steering"] and meta_cursor < len(meta_turns):
            # 识别单条或同拍合并排空的连续 steering 项
            if meta_turns[meta_cursor].get("steering"):
                acc_prompts: list[str] = []
                probe = meta_cursor
                while probe < len(meta_turns) and meta_turns[probe].get("steering"):
                    p_str = str(meta_turns[probe].get("prompt") or "").strip()
                    acc_prompts.append(p_str)
                    matched_batch.append(meta_turns[probe])
                    probe += 1
                    if "\n\n".join(acc_prompts) == seg["user_prompt"]:
                        break
                else:
                    matched_batch = [meta_turns[meta_cursor]]
                    probe = meta_cursor + 1
                meta_cursor = probe
        elif meta_cursor < len(meta_turns):
            matched_batch = [meta_turns[meta_cursor]]
            meta_cursor += 1

        if matched_batch and len(matched_batch) > 1:
            for b_idx, m_item in enumerate(matched_batch):
                is_last_in_batch = b_idx == len(matched_batch) - 1
                b_prompt = str(m_item.get("prompt") or "").strip() or seg["user_prompt"]
                b_started = m_item.get("created_at")
                b_completed = m_item.get("completed_at") if is_last_in_batch else None
                full_thread.append(
                    {
                        "turn_index": len(full_thread),
                        "message_idx": seg["message_idx"],
                        "user_prompt": b_prompt,
                        "prompt": b_prompt,
                        "assistant_answer": answer_str if is_last_in_batch else "",
                        "thinking": thinking_str if is_last_in_batch else "",
                        "tool_calls_count": int(seg["tool_calls_count"]) if is_last_in_batch else 0,
                        "attachments": [],
                        "started_at": b_started,
                        "completed_at": b_completed,
                        "created_at": b_started,
                        "actor_id": m_item.get("actor_id"),
                        "steering": bool(m_item.get("steering", seg["steering"])),
                    }
                )
                s_set = {m_item.get("steering_id")} if m_item.get("steering_id") is not None else set()
                turn_steering_ids.append(s_set)
        else:
            m_item = matched_batch[0] if matched_batch else {}
            t_started = m_item.get("created_at")
            t_completed = m_item.get("completed_at")
            full_thread.append(
                {
                    "turn_index": len(full_thread),
                    "message_idx": seg["message_idx"],
                    "user_prompt": seg["user_prompt"],
                    "prompt": seg["user_prompt"],
                    "assistant_answer": answer_str,
                    "thinking": thinking_str,
                    "tool_calls_count": int(seg["tool_calls_count"]),
                    "attachments": [],
                    "started_at": t_started,
                    "completed_at": t_completed,
                    "created_at": t_started,
                    "actor_id": m_item.get("actor_id"),
                    "steering": bool(m_item.get("steering", seg["steering"])),
                }
            )
            s_set = {m_item.get("steering_id")} if m_item.get("steering_id") is not None else set()
            turn_steering_ids.append(s_set)

    # 补齐各轮结束时间戳及末轮兜底 answer
    for i, turn in enumerate(full_thread):
        turn["turn_index"] = i
        if turn["completed_at"] is None:
            if i + 1 < len(full_thread):
                turn["completed_at"] = full_thread[i + 1].get("started_at")
            elif is_terminal:
                turn["completed_at"] = stored.get("updated_at")
    if full_thread and not full_thread[-1]["assistant_answer"] and stored.get("answer"):
        full_thread[-1]["assistant_answer"] = str(stored["answer"])

    # 将 user_attachments 归类到对应轮次
    if full_thread and raw_atts:
        msg_idx_to_turn: dict[int, int] = {}
        steering_id_to_turn: dict[Any, int] = {}
        for i, turn in enumerate(full_thread):
            msg_idx_to_turn[int(turn["message_idx"])] = i
            for sid in turn_steering_ids[i]:
                steering_id_to_turn[sid] = i
        for att in raw_atts:
            pub_att = _sanitize_attachment_public(att)
            target_idx: int | None = None
            sid = att.get("steering_id")
            if sid is not None and sid in steering_id_to_turn:
                target_idx = steering_id_to_turn[sid]
            elif att.get("turn_message_idx") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    m_idx = int(att["turn_message_idx"])
                    if m_idx in msg_idx_to_turn:
                        target_idx = msg_idx_to_turn[m_idx]
            if target_idx is None:
                # 无显式下标时，按上传时间就近归入不晚于该时间的最新轮次，兜底归入末轮/首轮
                uploaded_at = str(att.get("uploaded_at") or "")
                if uploaded_at:
                    best = 0
                    for i, turn in enumerate(full_thread):
                        t_start = str(turn.get("started_at") or "")
                        if t_start and t_start <= uploaded_at:
                            best = i
                    target_idx = best
                else:
                    target_idx = 0
            full_thread[target_idx]["attachments"].append(pub_att)

    return full_thread, full_thread[1:]


def _summarize_checkpoint(cp: dict[str, Any], *, include_manifest: bool = False) -> dict[str, Any]:
    manifest = cp.get("file_manifest") if isinstance(cp.get("file_manifest"), dict) else {}
    summary: dict[str, Any] = {
        "checkpoint_id": str(cp["checkpoint_id"]),
        "run_id": str(cp["run_id"]),
        "turn_index": int(cp["turn_index"]),
        "message_idx": int(cp["message_idx"]),
        "label": str(cp.get("label") or ""),
        "files_count": len(manifest),
        "files": sorted(manifest.keys()),
        "git_tree": cp.get("git_tree"),
        "created_at": str(cp.get("created_at") or ""),
    }
    if include_manifest:
        summary["file_manifest"] = manifest
    return summary


def _read_settings_file_safe(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        return {}
    try:
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            return {}
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _extract_rules_and_hooks(cfg: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    raw_perms = cfg.get("permissions") if isinstance(cfg.get("permissions"), dict) else {}
    perms: dict[str, list[str]] = {}
    for bucket in ("deny", "ask", "allow"):
        items = raw_perms.get(bucket)
        perms[bucket] = (
            [str(r).strip() for r in items if isinstance(r, str) and r.strip()]
            if isinstance(items, list)
            else []
        )
    raw_hooks = cfg.get("hooks") if isinstance(cfg.get("hooks"), dict) else {}
    hooks_out: dict[str, list[dict[str, Any]]] = {}
    for ev, entries in raw_hooks.items():
        if ev in HOOK_EVENTS and isinstance(entries, list):
            hooks_out[ev] = [dict(item) for item in entries if isinstance(item, dict)]
    return perms, hooks_out


def _sanitize_rule_list(rules: list[str]) -> list[str]:
    out: list[str] = []
    for raw in rules:
        if not isinstance(raw, str):
            raise ValueError("权限规则必须是字符串")
        cleaned = raw.strip()
        if not cleaned or len(cleaned) > 256 or any(ch in cleaned for ch in ("\x00", "\n", "\r")):
            raise ValueError(f"非法的权限规则：{raw!r}")
        if cleaned not in out:
            out.append(cleaned)
    return out


def _sanitize_hooks_dict(raw_hooks: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    cleaned: dict[str, list[dict[str, Any]]] = {}
    for ev, entries in raw_hooks.items():
        if ev not in HOOK_EVENTS:
            raise ValueError(f"不支持的钩子事件 '{ev}'，必须是 {sorted(HOOK_EVENTS)} 之一")
        if not isinstance(entries, list) or len(entries) > 64:
            raise ValueError(f"钩子事件 '{ev}' 的配置必须是长度不超过 64 的列表")
        valid_entries: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError(f"钩子事件 '{ev}' 中的配置项必须是对象")
            matcher = str(item.get("matcher") or "*").strip() or "*"
            if len(matcher) > 200 or "\x00" in matcher:
                raise ValueError("钩子 matcher 格式非法")
            on_error = str(item.get("on_error") or "allow").strip().lower()
            if on_error not in {"allow", "deny"}:
                raise ValueError("钩子 on_error 必须是 'allow' 或 'deny'")
            entry_out: dict[str, Any] = {"matcher": matcher, "on_error": on_error}
            if item.get("name") is not None:
                entry_out["name"] = str(item["name"]).strip()[:80]
            if item.get("timeout_seconds") is not None:
                try:
                    t_val = float(item["timeout_seconds"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("钩子 timeout_seconds 必须是数字") from exc
                if not 0.2 <= t_val <= 60.0:
                    raise ValueError("钩子 timeout_seconds 必须在 0.2 到 60 之间")
                entry_out["timeout_seconds"] = t_val
            if isinstance(item.get("command"), str) and item["command"].strip():
                cmd = item["command"].strip()
                if len(cmd) > 2000 or "\x00" in cmd:
                    raise ValueError("钩子 command 超过长度限制或包含非法空字节")
                entry_out["command"] = cmd
            elif isinstance(item.get("hooks"), list) and item["hooks"]:
                sub_list: list[dict[str, Any]] = []
                for sub in item["hooks"]:
                    if not isinstance(sub, dict) or not isinstance(sub.get("command"), str) or not sub["command"].strip():
                        raise ValueError("嵌套钩子项必须包含非空 command")
                    sub_cmd = sub["command"].strip()
                    if len(sub_cmd) > 2000 or "\x00" in sub_cmd:
                        raise ValueError("嵌套钩子 command 非法")
                    sub_entry: dict[str, Any] = {"command": sub_cmd}
                    if sub.get("matcher"):
                        sub_entry["matcher"] = str(sub["matcher"]).strip()[:200]
                    if sub.get("on_error"):
                        sub_oe = str(sub["on_error"]).strip().lower()
                        if sub_oe not in {"allow", "deny"}:
                            raise ValueError("嵌套钩子 on_error 必须是 'allow' 或 'deny'")
                        sub_entry["on_error"] = sub_oe
                    if sub.get("timeout_seconds") is not None:
                        st_val = float(sub["timeout_seconds"])
                        if not 0.2 <= st_val <= 60.0:
                            raise ValueError("嵌套钩子 timeout_seconds 必须在 0.2 到 60 之间")
                        sub_entry["timeout_seconds"] = st_val
                    if sub.get("name"):
                        sub_entry["name"] = str(sub["name"]).strip()[:80]
                    sub_list.append(sub_entry)
                entry_out["hooks"] = sub_list
            else:
                raise ValueError(f"钩子事件 '{ev}' 中的配置项必须包含 command 或 hooks 列表")
            valid_entries.append(entry_out)
        if valid_entries:
            cleaned[ev] = valid_entries
    return cleaned


def _format_broker_sse(envelope: dict[str, Any]) -> str:
    ev_type = str(envelope.get("type") or "event")
    seq = envelope.get("seq")
    data_json = json.dumps(envelope, ensure_ascii=False)
    if seq is not None:
        return f"id: {seq}\nevent: {ev_type}\ndata: {data_json}\n\n"
    return f"event: {ev_type}\ndata: {data_json}\n\n"


def _extract_safe_provider_base_url(provider_obj: Any, protocol: str) -> str:
    """提取 Provider 实例的非敏感 base_url（剥离内嵌凭据、查询参数与尾部斜杠）。"""
    if protocol == "demo":
        return ""
    raw = getattr(provider_obj, "base_url", None)
    if not raw:
        client = getattr(provider_obj, "client", None)
        if client is not None:
            raw = getattr(client, "base_url", None)
    raw_str = str(raw).strip() if raw is not None else ""
    if not raw_str:
        return DEFAULT_BASE_URLS.get(protocol, "")
    try:
        parsed = urlsplit(raw_str)
        if not parsed.scheme or not parsed.hostname:
            return DEFAULT_BASE_URLS.get(protocol, "")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port_part = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{host}{port_part}{path_part}"
    except ValueError:
        return DEFAULT_BASE_URLS.get(protocol, "")


def _build_gateway_info_from_profile(
    profile_name: str,
    prof_data: dict[str, Any],
    provider_obj: Any,
    *,
    source: str,
) -> dict[str, Any]:
    norm_proto = normalize_protocol(prof_data["protocol"])
    resolved_model = (
        str(getattr(provider_obj, "model", "") or "").strip()
        or str(prof_data.get("model") or "").strip()
        or ("demo" if norm_proto == "demo" else "")
    )
    safe_url = str(prof_data.get("base_url") or "") or _extract_safe_provider_base_url(
        provider_obj, norm_proto
    )
    return {
        "profile": profile_name,
        "protocol": norm_proto,
        "model": resolved_model,
        "base_url": safe_url,
        "source": source,
    }


def _merge_preserved_gateway_secrets(
    incoming_doc: dict[str, Any],
    existing_doc: dict[str, Any] | None,
) -> dict[str, Any]:
    """在 PUT /api/gateways 校验落盘前，将客户端回传的 `***REDACTED***` 占位符还原为已有密钥与敏感请求头。"""
    if not isinstance(incoming_doc, dict):
        return incoming_doc
    raw_profiles = incoming_doc.get("profiles")
    if not isinstance(raw_profiles, dict):
        return incoming_doc

    existing_profiles = (
        existing_doc.get("profiles")
        if isinstance(existing_doc, dict) and isinstance(existing_doc.get("profiles"), dict)
        else {}
    )
    existing_redacted_profiles = (
        redact_gateways_document(existing_doc).get("profiles", {})
        if isinstance(existing_doc, dict) and existing_profiles
        else {}
    )

    merged_profiles: dict[str, Any] = {}
    for prof_name, raw_prof in raw_profiles.items():
        if not isinstance(raw_prof, dict):
            merged_profiles[prof_name] = raw_prof
            continue
        prof_copy = dict(raw_prof)
        prof_copy.pop("credential_configured", None)

        prev_prof = existing_profiles.get(prof_name)
        prev_redacted_prof = existing_redacted_profiles.get(prof_name)

        in_key = prof_copy.get("api_key")
        if isinstance(in_key, str) and in_key.strip() == REDACTED_SECRET_MARKER:
            prev_key = prev_prof.get("api_key") if isinstance(prev_prof, dict) else None
            if isinstance(prev_key, str) and prev_key.strip():
                prof_copy["api_key"] = prev_key
            else:
                prof_copy.pop("api_key", None)

        in_headers = prof_copy.get("headers")
        if isinstance(in_headers, dict):
            prev_headers = (
                prev_prof.get("headers")
                if isinstance(prev_prof, dict) and isinstance(prev_prof.get("headers"), dict)
                else {}
            )
            prev_red_headers = (
                prev_redacted_prof.get("headers")
                if isinstance(prev_redacted_prof, dict)
                and isinstance(prev_redacted_prof.get("headers"), dict)
                else {}
            )
            prev_headers_ci = {str(k).lower(): v for k, v in prev_headers.items()}
            prev_red_ci = {str(k).lower(): v for k, v in prev_red_headers.items()}
            restored_headers: dict[str, Any] = {}
            for hk, hv in in_headers.items():
                if isinstance(hv, str) and REDACTED_SECRET_MARKER in hv:
                    hk_str = str(hk)
                    hk_low = hk_str.lower()
                    if hv.strip() == REDACTED_SECRET_MARKER:
                        if hk_str in prev_headers:
                            restored_headers[hk] = prev_headers[hk_str]
                        elif hk_low in prev_headers_ci:
                            restored_headers[hk] = prev_headers_ci[hk_low]
                        else:
                            raise ValueError(
                                f"profile {prof_name!r} 的请求头 {hk_str!r} 包含脱敏占位符但未找到原有配置值"
                            )
                    else:
                        if hk_str in prev_headers and prev_red_headers.get(hk_str) == hv:
                            restored_headers[hk] = prev_headers[hk_str]
                        elif hk_low in prev_headers_ci and prev_red_ci.get(hk_low) == hv:
                            restored_headers[hk] = prev_headers_ci[hk_low]
                        else:
                            raise ValueError(
                                f"profile {prof_name!r} 的请求头 {hk_str!r} 包含脱敏占位符但与原有配置不匹配"
                            )
                else:
                    restored_headers[hk] = hv
            prof_copy["headers"] = restored_headers

        merged_profiles[prof_name] = prof_copy

    merged_doc = dict(incoming_doc)
    merged_doc.pop("config_path", None)
    merged_doc["profiles"] = merged_profiles
    return merged_doc


_MIME_TO_DEFAULT_ATTACHMENT_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
}
_A2A_FALLBACK_FILENAME_RE = re.compile(r"^a2a_part_\d+\.bin$")


def _normalize_a2a_attachments(raw_atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 extract_a2a_message_parts 提取的附件规整为标准上传载荷，交由严格魔数与扩展名白名单校验。"""
    normalized: list[dict[str, Any]] = []
    for raw in raw_atts:
        if not isinstance(raw, dict):
            continue
        fname = str(raw.get("filename") or "").strip()
        mime = str(raw.get("media_type") or "").strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        ext = Path(fname).suffix.lower() if fname else ""
        default_ext = _MIME_TO_DEFAULT_ATTACHMENT_EXT.get(mime)
        if default_ext and (_A2A_FALLBACK_FILENAME_RE.fullmatch(fname) or (fname and not ext)):
            stem = Path(fname).stem or "a2a_attachment"
            fname = f"{stem}{default_ext}"
        normalized.append(
            {
                "filename": fname,
                "media_type": mime,
                "content_base64": raw.get("content_base64"),
            }
        )
    return normalized


def _merge_preserved_a2a_secrets(
    incoming_doc: dict[str, Any],
    existing_doc: dict[str, Any] | None,
) -> dict[str, Any]:
    """在 PUT /api/a2a/peers 校验落盘前，将客户端回传的 `***REDACTED***` 占位符还原为已有密钥与敏感请求头。"""
    if not isinstance(incoming_doc, dict):
        return incoming_doc
    raw_peers = incoming_doc.get("peers")
    if not isinstance(raw_peers, dict):
        return incoming_doc

    existing_peers = (
        existing_doc.get("peers")
        if isinstance(existing_doc, dict) and isinstance(existing_doc.get("peers"), dict)
        else {}
    )
    existing_redacted_peers = (
        redact_a2a_document(existing_doc).get("peers", {})
        if isinstance(existing_doc, dict) and existing_peers
        else {}
    )

    merged_peers: dict[str, Any] = {}
    for peer_name, raw_peer in raw_peers.items():
        if not isinstance(raw_peer, dict):
            merged_peers[peer_name] = raw_peer
            continue
        peer_copy = dict(raw_peer)
        peer_copy.pop("credential_configured", None)

        prev_peer = existing_peers.get(peer_name)
        prev_redacted_peer = existing_redacted_peers.get(peer_name)

        in_key = peer_copy.get("api_key")
        if isinstance(in_key, str) and in_key.strip() == A2A_REDACTED_MARKER:
            prev_key = prev_peer.get("api_key") if isinstance(prev_peer, dict) else None
            if isinstance(prev_key, str) and prev_key.strip():
                peer_copy["api_key"] = prev_key
            else:
                peer_copy.pop("api_key", None)

        in_headers = peer_copy.get("headers")
        if isinstance(in_headers, dict):
            prev_headers = (
                prev_peer.get("headers")
                if isinstance(prev_peer, dict) and isinstance(prev_peer.get("headers"), dict)
                else {}
            )
            prev_red_headers = (
                prev_redacted_peer.get("headers")
                if isinstance(prev_redacted_peer, dict)
                and isinstance(prev_redacted_peer.get("headers"), dict)
                else {}
            )
            prev_headers_ci = {str(k).lower(): v for k, v in prev_headers.items()}
            prev_red_ci = {str(k).lower(): v for k, v in prev_red_headers.items()}
            restored_headers: dict[str, Any] = {}
            for hk, hv in in_headers.items():
                if isinstance(hv, str) and A2A_REDACTED_MARKER in hv:
                    hk_str = str(hk)
                    hk_low = hk_str.lower()
                    if hv.strip() == A2A_REDACTED_MARKER:
                        if hk_str in prev_headers:
                            restored_headers[hk] = prev_headers[hk_str]
                        elif hk_low in prev_headers_ci:
                            restored_headers[hk] = prev_headers_ci[hk_low]
                        else:
                            raise ValueError(
                                f"A2A peer {peer_name!r} 的请求头 {hk_str!r} 包含脱敏占位符但未找到原有配置值"
                            )
                    else:
                        if hk_str in prev_headers and prev_red_headers.get(hk_str) == hv:
                            restored_headers[hk] = prev_headers[hk_str]
                        elif hk_low in prev_headers_ci and prev_red_ci.get(hk_low) == hv:
                            restored_headers[hk] = prev_headers_ci[hk_low]
                        else:
                            raise ValueError(
                                f"A2A peer {peer_name!r} 的请求头 {hk_str!r} 包含脱敏占位符但与原有配置不匹配"
                            )
                else:
                    restored_headers[hk] = hv
            peer_copy["headers"] = restored_headers

        merged_peers[peer_name] = peer_copy

    merged_doc = dict(incoming_doc)
    merged_doc.pop("config_path", None)
    merged_doc["peers"] = merged_peers
    return merged_doc


def _build_available_models(extra_models: list[str] | None = None) -> list[str]:
    ordered: list[str] = []
    for raw_m in extra_models or []:
        val = str(raw_m or "").strip()
        if val and val.lower() != "demo" and val not in ordered:
            ordered.append(val)
    for env_key in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_REASONING_MODEL",
        "ANTHROPIC_FAST_MODEL",
        "OPENAI_MODEL",
        "OPENAI_REASONING_MODEL",
        "OPENAI_FAST_MODEL",
        "GEMINI_MODEL",
        "GEMINI_REASONING_MODEL",
        "GEMINI_FAST_MODEL",
        "AGENT_LAB_REASONING_MODEL",
        "AGENT_LAB_FAST_MODEL",
    ):
        val = os.environ.get(env_key, "").strip()
        if val and val not in ordered:
            ordered.append(val)
    for model_id in DEFAULT_MODEL_PRICING_USD_PER_MTOK:
        if model_id not in ordered:
            ordered.append(model_id)
    return ordered


def create_app(
    state_dir: Path | None = None,
    provider: str | Provider | None = None,
    gateway_profile: str | None = None,
    gateway_config: str | Path | None = None,
) -> FastAPI:
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
    attachment_max_body_bytes = (
        max_body_bytes
        if explicit_max_body is not None
        else max(
            max_body_bytes,
            int(os.environ.get("AGENT_LAB_ATTACHMENT_MAX_BODY_BYTES", str(8 * 1024 * 1024))),
        )
    )
    sse_keepalive_seconds = max(
        0.05, float(os.environ.get("AGENT_LAB_SSE_KEEPALIVE_SECONDS", "15.0"))
    )
    max_queue_depth = max(1, int(os.environ.get("AGENT_LAB_MAX_QUEUE_DEPTH", "64")))
    max_inline_jobs = max(1, int(os.environ.get("AGENT_LAB_MAX_INLINE_JOBS", "16")))
    rate_limiter = _TenantRateLimiter(int(os.environ.get("AGENT_LAB_RATE_LIMIT_RPM", "120")))
    auth_rate_limiter = _TenantRateLimiter(
        int(os.environ.get("AGENT_LAB_AUTH_RATE_LIMIT_RPM", str(rate_limiter.limit_rpm)))
    )
    configured_tenants = frozenset(p.tenant_id for _, p in token_entries)
    has_explicit_global_admin = any(
        p.global_admin and "admin" in p.roles for _, p in token_entries
    )
    default_principal = Principal("local", "local-dev", VALID_ROLES, global_admin=False)

    state = Path(state_dir or os.environ.get("AGENT_LAB_STATE", ROOT / ".agent-lab")).resolve()
    state.mkdir(parents=True, exist_ok=True)
    workspaces = state / "workspaces"
    workspaces.mkdir(exist_ok=True)
    git_origins = state / "git_origins"
    git_origins.mkdir(exist_ok=True)
    artifacts_root = state / "artifacts"
    artifacts_root.mkdir(exist_ok=True)
    staged_uploads_root = state / "staged_uploads"
    staged_uploads_root.mkdir(exist_ok=True)
    staged_upload_lock = threading.Lock()
    raw_allowed_ws = os.environ.get("AGENT_LAB_ALLOWED_WORKSPACES", "").strip()
    allowed_ws_roots = (
        [Path(p.strip()).expanduser().resolve() for p in raw_allowed_ws.split(",") if p.strip()]
        if raw_allowed_ws
        else None
    )
    dsn = os.environ.get("AGENT_LAB_STORAGE_DSN", "").strip()
    storage = Storage.from_dsn(dsn) if (state_dir is None and dsn) else Storage(state / "state.db")
    gateway_lock = threading.Lock()
    a2a_lock = threading.Lock()
    explicit_gateway_config_path: Path | None = (
        Path(gateway_config).expanduser()
        if (
            gateway_config is not None
            and (not isinstance(gateway_config, str) or bool(gateway_config.strip()))
        )
        else None
    )
    default_state_dir = (ROOT / ".agent-lab").resolve()
    gateway_discovery_tenant_id: str | None = (
        "local" if (not auth_configured and state == default_state_dir) else None
    )
    injected_provider_obj: Any = (
        provider if (provider is not None and not isinstance(provider, str)) else None
    )

    def _a2a_config_target_path() -> Path:
        env_cfg = os.environ.get("AGENT_LAB_A2A_CONFIG", "").strip()
        if env_cfg:
            return Path(env_cfg).expanduser()
        return state / "a2a.json"

    def _gateway_config_target_path(*, for_activate: bool = False) -> Path:
        if explicit_gateway_config_path is not None:
            return explicit_gateway_config_path
        env_cfg = os.environ.get("AGENT_LAB_GATEWAYS_FILE", "").strip()
        if env_cfg:
            return Path(env_cfg).expanduser()
        state_file = state / "gateways.json"
        if for_activate and not state_file.is_symlink() and not state_file.exists():
            discovered = discover_gateways_config_path(
                state_dir=state,
                tenant_id=gateway_discovery_tenant_id,
            )
            if discovered is not None:
                return discovered
        return state_file

    def _init_active_gateway_state() -> dict[str, Any]:
        # 1. 显式传入具体 Provider 对象（如测试 Mock 或预构造实例），原样保留
        if injected_provider_obj is not None:
            raw_proto = getattr(injected_provider_obj, "gateway_protocol", None) or getattr(
                injected_provider_obj, "protocol", None
            )
            try:
                norm_proto = (
                    normalize_protocol(raw_proto)
                    if isinstance(raw_proto, str) and raw_proto.strip()
                    else "custom"
                )
            except ValueError:
                norm_proto = "custom"
            raw_prof = getattr(injected_provider_obj, "gateway_profile", None)
            prof_name = (
                raw_prof.strip() if isinstance(raw_prof, str) and raw_prof.strip() else None
            )
            model_name = str(getattr(injected_provider_obj, "model", "") or "").strip() or (
                "demo" if norm_proto == "demo" else ""
            )
            spec = (
                f"gateway:{prof_name}"
                if prof_name
                else (
                    "demo"
                    if norm_proto == "demo" and type(injected_provider_obj).__name__ == "DemoProvider"
                    else "custom"
                )
            )
            return {
                "provider_spec": spec,
                "provider_obj": injected_provider_obj,
                "info": {
                    "profile": prof_name,
                    "protocol": norm_proto,
                    "model": model_name,
                    "base_url": _extract_safe_provider_base_url(injected_provider_obj, norm_proto),
                    "source": "explicit_object",
                },
            }

        clean_gw_profile = (
            gateway_profile.strip()
            if isinstance(gateway_profile, str) and gateway_profile.strip()
            else None
        )
        clean_provider_str = (
            provider.strip() if isinstance(provider, str) and provider.strip() else None
        )

        # 2. 显式传入 provider="demo"（且未同时指定 gateway_profile），保持既有回归测试 100% 行为一致
        if (
            clean_provider_str
            and clean_provider_str.lower() == "demo"
            and clean_gw_profile is None
        ):
            return {
                "provider_spec": "demo",
                "provider_obj": get_provider("demo"),
                "info": {
                    "profile": None,
                    "protocol": "demo",
                    "model": "demo",
                    "base_url": "",
                    "source": "explicit_demo",
                },
            }

        # 3. 显式传入 gateway_profile
        if clean_gw_profile is not None:
            target_spec = (
                clean_gw_profile
                if clean_gw_profile.startswith("gateway:")
                else f"gateway:{clean_gw_profile}"
            )
            prov_obj, gw_info = resolve_provider_or_gateway(
                target_spec,
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
            resolved_prof = gw_info.get("profile") or clean_gw_profile.removeprefix("gateway:")
            return {
                "provider_spec": f"gateway:{resolved_prof}",
                "provider_obj": prov_obj,
                "info": dict(gw_info),
            }

        # 4. 显式传入非 demo/auto 的 provider 字符串（如 openai_responses / gemini / gateway:<name> / profile 名）
        if clean_provider_str is not None and clean_provider_str.lower() != "auto":
            prov_obj, gw_info = resolve_provider_or_gateway(
                clean_provider_str,
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
            spec_out = f"gateway:{gw_info['profile']}" if gw_info.get("profile") else clean_provider_str
            return {
                "provider_spec": spec_out,
                "provider_obj": prov_obj,
                "info": dict(gw_info),
            }

        # 5. 未显式传 provider / gateway_profile：通过 gateways.resolve_provider_or_gateway 解析
        env_gw = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
        strict_startup = bool(env_gw or explicit_gateway_config_path is not None)
        try:
            prov_obj, gw_info = resolve_provider_or_gateway(
                None,
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            err_msg = str(exc)
            is_security_err = any(
                tok in err_msg
                for tok in ("符号链接", "硬链接", "O_NOFOLLOW", "常规文件", "元数据", "IMDS")
            )
            if strict_startup or is_security_err:
                raise
            return {
                "provider_spec": "demo",
                "provider_obj": get_provider("demo"),
                "info": {
                    "profile": None,
                    "protocol": "demo",
                    "model": "demo",
                    "base_url": "",
                    "source": "fallback_demo",
                },
            }
        if gw_info.get("profile"):
            spec_out = f"gateway:{gw_info['profile']}"
        else:
            env_prov = os.environ.get("AGENT_PROVIDER", "").strip()
            if env_prov and env_prov.lower() != "auto" and gw_info.get("source") == "env_provider":
                spec_out = env_prov
            else:
                spec_out = str(gw_info.get("protocol") or "demo")
        return {
            "provider_spec": spec_out,
            "provider_obj": prov_obj,
            "info": dict(gw_info),
        }

    active_gateway_state: dict[str, Any] = _init_active_gateway_state()

    async def _close_provider_safely(prov_obj: Any) -> None:
        if prov_obj is None or prov_obj is injected_provider_obj:
            return
        close_fn = getattr(prov_obj, "aclose", None)
        if callable(close_fn):
            with contextlib.suppress(Exception):
                maybe_coro = close_fn()
                if inspect.isawaitable(maybe_coro):
                    await maybe_coro

    def _resolve_provider_for_new_run(
        requested_profile: str | None,
    ) -> tuple[str, Any, dict[str, Any]]:
        """为新建任务解析 (provider_spec, provider_arg_for_runtime, gateway_info)。"""
        clean_req = (
            requested_profile.strip()
            if isinstance(requested_profile, str) and requested_profile.strip()
            else None
        )
        if clean_req is not None:
            prof_key = (
                clean_req[len("gateway:") :].strip()
                if clean_req.startswith("gateway:")
                else clean_req
            )
            if not prof_key:
                raise ValueError("gateway_profile 不能为空")
            prov_obj, gw_info = resolve_provider_or_gateway(
                f"gateway:{prof_key}",
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
            resolved_prof = gw_info.get("profile") or prof_key
            return f"gateway:{resolved_prof}", prov_obj, dict(gw_info)

        with gateway_lock:
            cur_spec = str(active_gateway_state["provider_spec"])
            cur_obj = active_gateway_state.get("provider_obj")
            cur_info = dict(active_gateway_state["info"])

        if cur_obj is not None and cur_obj is injected_provider_obj:
            return cur_spec, cur_obj, cur_info
        if cur_spec == "demo":
            return "demo", "demo", cur_info
        if cur_spec.startswith("gateway:"):
            prov_obj, gw_info = resolve_provider_or_gateway(
                cur_spec,
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
            return cur_spec, prov_obj, dict(gw_info)
        prov_obj = get_provider(
            cur_spec,
            state_dir=state,
            gateway_config=explicit_gateway_config_path,
        )
        return cur_spec, prov_obj, cur_info

    def _resolve_provider_for_existing_run(run: dict[str, Any]) -> tuple[str, Any]:
        meta = run.get("metadata") or {}
        run_prov = str(run.get("provider") or "demo").strip() or "demo"
        meta_gw_prof = meta.get("gateway_profile")

        with gateway_lock:
            cur_obj = active_gateway_state.get("provider_obj")
            cur_spec = str(active_gateway_state["provider_spec"])

        if run_prov == "custom" and injected_provider_obj is not None:
            return run_prov, injected_provider_obj
        if cur_obj is not None and cur_obj is injected_provider_obj and run_prov == cur_spec:
            return run_prov, cur_obj
        if run_prov == "demo":
            return "demo", "demo"
        if run_prov.startswith("gateway:"):
            prov_obj, _ = resolve_provider_or_gateway(
                run_prov,
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
            return run_prov, prov_obj
        if isinstance(meta_gw_prof, str) and meta_gw_prof.strip():
            prof_spec = f"gateway:{meta_gw_prof.strip()}"
            prov_obj, _ = resolve_provider_or_gateway(
                prof_spec,
                state_dir=state,
                explicit_config_path=explicit_gateway_config_path,
                tenant_id=gateway_discovery_tenant_id,
            )
            return prof_spec, prov_obj
        prov_obj = get_provider(
            run_prov,
            state_dir=state,
            gateway_config=explicit_gateway_config_path,
        )
        return run_prov, prov_obj

    dispatch_mode = os.environ.get("AGENT_LAB_DISPATCH", "inline").strip().lower()
    if dispatch_mode not in {"inline", "queue"}:
        dispatch_mode = "inline"
    stream_broker = RunStreamBroker()
    # jobs 追踪正在执行的编码主任务（受单机 inline 并发上限约束，并按 run_id 互斥）；
    # active_runtimes 追踪进程内正在处理 run_id 的实时 AgentRuntime 实例，供 /cancel 瞬时中断；
    # dream_jobs 独立追踪会话完成后的异步睡眠整理，避免慢速记忆归纳挤占主并发槽位或阻塞同 run_id 追问。
    jobs: dict[str, asyncio.Task] = {}
    active_runtimes: dict[str, AgentRuntime] = {}
    dream_jobs: set[asyncio.Task] = set()
    push_jobs: set[asyncio.Task] = set()
    a2a_push_run_ids: OrderedDict[str, None] = OrderedDict()
    repomap_lock = threading.Lock()
    session_lock = threading.Lock()
    cookie_sessions: OrderedDict[str, tuple[Principal, float]] = OrderedDict()

    def _resolve_token(provided: str) -> Principal | None:
        if not provided:
            return None
        matched: Principal | None = None
        for expected_tok, principal_obj in token_entries:
            if hmac.compare_digest(provided, expected_tok) and matched is None:
                matched = principal_obj
        return matched

    def _resolve_bearer(auth_header: str) -> Principal | None:
        if not auth_header.startswith("Bearer "):
            return None
        return _resolve_token(auth_header[7:])

    def _create_cookie_session(principal: Principal) -> str:
        sid = secrets.token_urlsafe(32)
        now_m = time.monotonic()
        expires_at = now_m + _SESSION_TTL_SECONDS
        with session_lock:
            expired_keys = [k for k, (_, exp) in cookie_sessions.items() if exp <= now_m]
            for k in expired_keys:
                cookie_sessions.pop(k, None)
            # 先执行单租户会话上限淘汰（FIFO），杜绝单一租户刷会话踢出其他租户或管理员会话
            tenant_sids = [
                k
                for k, (p_obj, _) in cookie_sessions.items()
                if p_obj.tenant_id == principal.tenant_id
            ]
            while len(tenant_sids) >= _MAX_COOKIE_SESSIONS_PER_TENANT:
                evict_sid = tenant_sids.pop(0)
                cookie_sessions.pop(evict_sid, None)
            cookie_sessions[sid] = (principal, expires_at)
            while len(cookie_sessions) > _MAX_COOKIE_SESSIONS:
                counts: dict[str, int] = {}
                for _, (p_obj, _) in cookie_sessions.items():
                    counts[p_obj.tenant_id] = counts.get(p_obj.tenant_id, 0) + 1
                victim_tenant = max(counts, key=lambda t: counts[t])
                victim_sid = next(
                    (
                        k
                        for k, (p_obj, _) in cookie_sessions.items()
                        if p_obj.tenant_id == victim_tenant and k != sid
                    ),
                    None,
                )
                if victim_sid is None:
                    cookie_sessions.popitem(last=False)
                else:
                    cookie_sessions.pop(victim_sid, None)
        return sid

    def _resolve_cookie(cookie_val: str) -> Principal | None:
        if not cookie_val:
            return None
        now_m = time.monotonic()
        with session_lock:
            entry = cookie_sessions.get(cookie_val)
            if entry is None:
                return None
            principal_obj, exp = entry
            if exp <= now_m:
                cookie_sessions.pop(cookie_val, None)
                return None
            cookie_sessions.move_to_end(cookie_val)
            return principal_obj

    def _delete_cookie_session(cookie_val: str) -> Principal | None:
        if not cookie_val:
            return None
        with session_lock:
            entry = cookie_sessions.pop(cookie_val, None)
        return entry[0] if entry is not None else None

    def _resolve_request_auth(req: Request, auth_header: str) -> tuple[Principal | None, str]:
        if auth_header:
            return _resolve_bearer(auth_header), ""
        cookie_sid = req.cookies.get(SESSION_COOKIE_NAME, "")
        if cookie_sid:
            matched = _resolve_cookie(cookie_sid)
            return matched, (cookie_sid if matched is not None else "")
        return None, ""

    def _is_https_request(req: Request) -> bool:
        if req.url.scheme == "https":
            return True
        forwarded = req.headers.get("x-forwarded-proto", "")
        return bool(forwarded and forwarded.split(",")[0].strip().lower() == "https")

    def _origin_matches_request(raw_origin: str, req: Request) -> bool:
        clean = raw_origin.strip()
        if not clean or clean.lower() == "null":
            return False
        try:
            orig = urlsplit(clean)
            req_url = urlsplit(str(req.base_url))
        except ValueError:
            return False
        orig_scheme = orig.scheme.lower()
        if orig_scheme not in {"http", "https"} or not orig.hostname:
            return False
        req_scheme = "https" if _is_https_request(req) else req_url.scheme.lower()
        req_host = (req_url.hostname or "").lower()
        orig_host = orig.hostname.lower()
        if orig_host != req_host:
            return False
        default_ports = {"http": 80, "https": 443}
        orig_port = orig.port or default_ports.get(orig_scheme)
        req_port = req_url.port or default_ports.get(req_scheme)
        return orig_scheme == req_scheme and orig_port == req_port

    def _check_cookie_csrf(req: Request) -> bool:
        """校验 Cookie 认证变更请求的同源属性：拒绝跨站 Origin / Sec-Fetch-Site / Referer，
        并在非回环宿主上要求显式同源 Origin 或 Sec-Fetch-Site: same-origin。"""
        origin = req.headers.get("origin", "").strip()
        sec_fetch_site = req.headers.get("sec-fetch-site", "").strip().lower()
        referer = req.headers.get("referer", "").strip()
        if origin and not _origin_matches_request(origin, req):
            return False
        if sec_fetch_site and sec_fetch_site != "same-origin":
            return False
        if not origin and referer and not _origin_matches_request(referer, req):
            return False
        if not origin and not sec_fetch_site:
            req_host = (req.url.hostname or "").strip().lower()
            if req_host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
                return False
        return True

    async def _audit_log(
        *,
        principal: Principal | None = None,
        action: str,
        resource: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        tenant = principal.tenant_id if principal is not None else "local"
        actor = principal.actor_id if principal is not None else "anonymous"
        with contextlib.suppress(Exception):
            await storage.run_async(
                storage.record_audit_log,
                tenant_id=tenant,
                actor=actor,
                action=action,
                resource=resource,
                outcome=outcome,
                detail=detail or {},
                trace_id=trace_id,
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stream_broker.bind_loop()
        trig_sched = getattr(app.state, "trigger_scheduler", None)
        if (
            isinstance(trig_sched, TriggerScheduler)
            and os.environ.get("AGENT_LAB_ENABLE_TRIGGER_SCHEDULER", "").strip().lower()
            in {"1", "true", "yes"}
        ):
            trig_sched.start()
        yield
        if isinstance(trig_sched, TriggerScheduler):
            await trig_sched.stop()
        # 服务关机时统一取消并回收主执行任务与后台 Dreaming 协程，防止残留悬空事件循环任务或未关闭连接池。
        for rid, job in list(jobs.items()):
            job.cancel()
            stream_broker.close_run(rid)
        for d_job in list(dream_jobs):
            d_job.cancel()
        for job in list(jobs.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await job
        for d_job in list(dream_jobs):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await d_job
        if push_jobs:
            pending_push = list(push_jobs)
            with contextlib.suppress(Exception):
                _, still_pending = await asyncio.wait(pending_push, timeout=0.5)
                for p_job in still_pending:
                    p_job.cancel()
                for p_job in still_pending:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await p_job
        with gateway_lock:
            cur_gw_prov = active_gateway_state.get("provider_obj")
            if cur_gw_prov is not injected_provider_obj:
                active_gateway_state["provider_obj"] = None
        await _close_provider_safely(cur_gw_prov)

    app = FastAPI(title="Agent Systems Lab", version="0.1.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.stream_broker = stream_broker
    app.state.active_runtimes = active_runtimes
    app.state.active_gateway_state = active_gateway_state
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
            audit_logged = False

            with bind_trace_context(incoming_trace) as trace_id:
                async def _send_early(payload: dict[str, Any], code: int) -> None:
                    nonlocal status_code, audit_logged
                    status_code = code
                    if code in (401, 403) and not audit_logged:
                        audit_logged = True
                        early_p = scope.get("state", {}).get("principal")
                        await _audit_log(
                            principal=early_p if isinstance(early_p, Principal) else None,
                            action="auth_denied" if code == 401 else "access_denied",
                            resource=f"{method} {path}",
                            outcome=f"denied_{code}",
                            detail={"status": code, "reason": str(payload.get("detail") or "")[:200]},
                            trace_id=trace_id,
                        )
                    resp = JSONResponse(payload, status_code=code, headers={"X-Trace-Id": trace_id})
                    await resp(scope, receive, send)

                try:
                    if path == "/api/internal/sandbox/execute":
                        body_cap = sandbox_max_body_bytes
                    elif _ATTACHMENT_PATH_RE.fullmatch(path) or path in {"/api/uploads", "/a2a"}:
                        body_cap = attachment_max_body_bytes
                    else:
                        body_cap = max_body_bytes
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
                    pre_buffered_body: bytes | None = None
                    pre_disconnected_msg: dict[str, Any] | None = None
                    if path.startswith("/api"):
                        origin = req.headers.get("origin")
                        if origin and not _origin_matches_request(origin, req):
                            await _send_early({"detail": "跨源请求被拒绝"}, 403)
                            return
                        webhook_match = (
                            _WEBHOOK_PATH_RE.fullmatch(path) if method == "POST" else None
                        )
                        if webhook_match is not None and ".." not in webhook_match.group(1):
                            wh_buffered = bytearray()
                            wh_more_body = True
                            while wh_more_body:
                                msg = await receive()
                                msg_type = msg.get("type")
                                if msg_type == "http.request":
                                    chunk = msg.get("body", b"") or b""
                                    wh_buffered.extend(chunk)
                                    if len(wh_buffered) > body_cap:
                                        await _send_early({"detail": "请求体超过大小限制"}, 413)
                                        return
                                    wh_more_body = bool(msg.get("more_body", False))
                                elif msg_type == "http.disconnect":
                                    pre_disconnected_msg = msg
                                    wh_more_body = False
                                else:
                                    wh_more_body = False
                            pre_buffered_body = bytes(wh_buffered)

                            wh_trigger_id = webhook_match.group(1)
                            wh_trigger = await storage.run_async(
                                storage.get_trigger, wh_trigger_id
                            )
                            if wh_trigger is None:
                                await _send_early({"detail": "触发器不存在"}, 404)
                                return
                            if not wh_trigger.get("enabled") or wh_trigger.get("kind") != "webhook":
                                await _send_early(
                                    {"detail": "触发器未启用或不支持 Webhook 投递"},
                                    403,
                                )
                                return

                            sig_header = (
                                (req.headers.get("x-hub-signature-256") or "").strip()
                                or (req.headers.get("x-agent-lab-signature") or "").strip()
                            )
                            if not sig_header:
                                await _send_early(
                                    {
                                        "detail": "缺少 Webhook 签名头（X-Hub-Signature-256 或 X-Agent-Lab-Signature）"
                                    },
                                    401,
                                )
                                return
                            wh_secret = str(wh_trigger.get("webhook_secret") or "")
                            if not wh_secret or not verify_webhook_signature(
                                pre_buffered_body, sig_header, wh_secret
                            ):
                                await _send_early({"detail": "Webhook 签名校验失败"}, 403)
                                return

                            wh_tenant = str(wh_trigger.get("tenant_id") or "local")
                            state_bag = scope.setdefault("state", {})
                            state_bag["principal"] = Principal(
                                wh_tenant,
                                f"webhook:{wh_trigger_id}",
                                frozenset({"operator"}),
                                global_admin=False,
                            )
                            state_bag["webhook_verified"] = True
                            state_bag["webhook_trigger"] = wh_trigger
                            state_bag["webhook_raw_body"] = pre_buffered_body
                        elif path == "/api/internal/sandbox/execute":
                            sbx_tok = os.environ.get("AGENT_LAB_SANDBOX_TOKEN", "")
                            if sbx_tok and hmac.compare_digest(auth_header, "Bearer " + sbx_tok):
                                state_bag = scope.setdefault("state", {})
                                state_bag["principal"] = Principal(
                                    "internal", "sandbox-pool", frozenset({"admin"}), global_admin=False
                                )
                                state_bag["sandbox_token_auth"] = True
                            elif auth_configured:
                                matched = _resolve_bearer(auth_header)
                                if matched is None:
                                    await _send_early({"detail": "需要 Bearer token"}, 401)
                                    return
                                if not (matched.global_admin and "admin" in matched.roles):
                                    scope.setdefault("state", {})["principal"] = matched
                                    await _send_early({"detail": "需要全局 admin 角色或有效沙箱令牌"}, 403)
                                    return
                                scope.setdefault("state", {})["principal"] = matched
                            elif sbx_tok:
                                await _send_early({"detail": "需要有效沙箱 Bearer token"}, 401)
                                return
                            else:
                                scope.setdefault("state", {})["principal"] = default_principal
                        elif path == "/api/auth/session" and method in {"POST", "DELETE"}:
                            if auth_configured:
                                existing_p, existing_sid = _resolve_request_auth(req, auth_header)
                                if existing_p is not None:
                                    state_bag = scope.setdefault("state", {})
                                    state_bag["principal"] = existing_p
                                    if existing_sid:
                                        state_bag["auth_cookie_sid"] = existing_sid
                                if (
                                    method == "DELETE"
                                    and not auth_header
                                    and req.cookies.get(SESSION_COOKIE_NAME, "")
                                    and not _check_cookie_csrf(req)
                                ):
                                    await _send_early({"detail": "Cookie 会话变更请求未通过同源校验"}, 403)
                                    return
                            else:
                                scope.setdefault("state", {})["principal"] = default_principal
                        elif auth_configured:
                            matched, cookie_sid = _resolve_request_auth(req, auth_header)
                            if matched is None:
                                await _send_early({"detail": "需要 Bearer token 或有效会话 Cookie"}, 401)
                                return
                            state_bag = scope.setdefault("state", {})
                            state_bag["principal"] = matched
                            if cookie_sid:
                                state_bag["auth_cookie_sid"] = cookie_sid
                                if method in {"POST", "PUT", "PATCH", "DELETE"} and not _check_cookie_csrf(req):
                                    await _send_early({"detail": "Cookie 会话变更请求未通过同源校验"}, 403)
                                    return
                        else:
                            scope.setdefault("state", {})["principal"] = default_principal
                    elif path in {"/.well-known/agent-card.json", "/.well-known/agent.json"}:
                        # A2A 规范公开 AgentCard 发现端点：无需鉴权，且严禁暴露任何凭据或内部文件路径
                        pass
                    elif path in {"/a2a", "/extendedAgentCard"}:
                        origin = req.headers.get("origin")
                        if origin and not _origin_matches_request(origin, req):
                            await _send_early({"detail": "跨源请求被拒绝"}, 403)
                            return
                        if auth_configured:
                            matched, cookie_sid = _resolve_request_auth(req, auth_header)
                            if matched is None:
                                await _send_early({"detail": "需要 Bearer token 或有效会话 Cookie"}, 401)
                                return
                            state_bag = scope.setdefault("state", {})
                            state_bag["principal"] = matched
                            if cookie_sid:
                                state_bag["auth_cookie_sid"] = cookie_sid
                                if method in {"POST", "PUT", "PATCH", "DELETE"} and not _check_cookie_csrf(req):
                                    await _send_early({"detail": "Cookie 会话变更请求未通过同源校验"}, 403)
                                    return
                        else:
                            scope.setdefault("state", {})["principal"] = default_principal
                    elif path == "/metrics" and auth_configured:
                        matched, cookie_sid = _resolve_request_auth(req, auth_header)
                        if matched is None:
                            await _send_early({"detail": "需要 Bearer token"}, 401)
                            return
                        state_bag = scope.setdefault("state", {})
                        state_bag["principal"] = matched
                        if cookie_sid:
                            state_bag["auth_cookie_sid"] = cookie_sid

                    downstream_receive = receive
                    if pre_buffered_body is not None:
                        body_bytes = pre_buffered_body
                        disconnected_msg = pre_disconnected_msg
                        replayed = False

                        async def _replay_pre_receive() -> dict[str, Any]:
                            nonlocal replayed
                            if not replayed:
                                replayed = True
                                if disconnected_msg is not None and not body_bytes:
                                    return disconnected_msg
                                return {"type": "http.request", "body": body_bytes, "more_body": False}
                            if disconnected_msg is not None:
                                return disconnected_msg
                            return await receive()

                        downstream_receive = _replay_pre_receive
                    elif method in {"POST", "PUT", "PATCH"}:
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
                    if not audit_logged and status_code in (401, 403):
                        audit_logged = True
                        req_p = scope.get("state", {}).get("principal")
                        await _audit_log(
                            principal=req_p if isinstance(req_p, Principal) else None,
                            action="auth_denied" if status_code == 401 else "access_denied",
                            resource=f"{method} {path}",
                            outcome=f"denied_{status_code}",
                            detail={"status": status_code},
                            trace_id=trace_id,
                        )
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
        """多租户宿主边界门卫：判断调用主体是否有权挂载宿主机本地目录（local_cwd / workspace_path）。

        安全原因：在配置了鉴权的多租户部署中，若允许非本机普通租户（如外部 SaaS 租户的 operator）指定
        workspace_path，攻击者即可绕过临时沙箱隔离，直接读写宿主机代码库甚至探测相邻租户目录。
        因此开启鉴权后仅放行全局管理员（global_admin）或 local 租户下的 admin 角色。
        """
        if not auth_configured:
            return True
        if principal.global_admin and "admin" in principal.roles:
            return True
        return principal.tenant_id == "local" and "admin" in principal.roles

    def _require_global_admin(principal: Principal, detail: str) -> None:
        """多租户全局管理边界门卫：开启鉴权后严格要求 global_admin 且具备 admin 角色。"""
        if auth_configured:
            if not (principal.global_admin and "admin" in principal.roles):
                raise HTTPException(403, detail)
        else:
            require_roles(principal, {"admin"}, detail)

    def _can_run_admin_gc(principal: Principal) -> bool:
        """判断调用方是否有权触发全局资源 GC：
        - 未开启鉴权：本地开发模式放行；
        - 已显式配置 global_admin 时：仅放行 global_admin 且具备 admin 角色的主体；
        - 多租户未显式配置 global_admin 时：仅放行宿主机 local 管理员，拒绝所有外部租户 admin；
        - 单租户部署且无独立 global_admin 时：不存在跨租户影响，放行该唯一租户的 admin。
        """
        if not auth_configured:
            return True
        if "admin" not in principal.roles:
            return False
        if principal.global_admin:
            return True
        if has_explicit_global_admin:
            return False
        if len(configured_tenants) > 1:
            return principal.tenant_id == "local"
        return True

    def _is_direct_workspace_run(meta: dict[str, Any] | None) -> bool:
        if not isinstance(meta, dict):
            return False
        return bool(
            meta.get("direct_workspace")
            or meta.get("template") == "local_cwd"
            or meta.get("isolated_local_worktree")
        )

    def _validate_managed_workspace_dir(raw_ws: str, allowed_bases: tuple[Path, ...]) -> Path:
        """在调用 resolve() 之前逐段拦截符号链接与越界段，确保托管工作区严格位于受控沙箱根内。"""
        if not isinstance(raw_ws, str) or not raw_ws.strip() or "\x00" in raw_ws:
            raise ValueError("工作区路径无效")
        cand_abs = Path(raw_ws.strip()).expanduser().absolute()
        if cand_abs.is_symlink():
            raise ValueError("拒绝访问符号链接工作区")
        for base in allowed_bases:
            if base.is_symlink():
                continue
            base_abs = base.absolute()
            try:
                rel = cand_abs.relative_to(base_abs)
            except ValueError:
                continue
            if not rel.parts:
                continue
            cur = base_abs
            for part in rel.parts:
                if part in {".", ".."}:
                    raise ValueError("工作区路径包含非法相对段")
                cur = cur / part
                if cur.is_symlink():
                    raise ValueError("拒绝访问包含符号链接的工作区路径")
            resolved = cand_abs.resolve()
            base_resolved = base.resolve()
            if resolved != base_resolved and resolved.is_relative_to(base_resolved):
                return resolved
        raise ValueError("工作区不属于本服务")

    def workspace_for(run: dict) -> Path:
        meta = run.get("metadata") or {}
        raw_ws = meta.get("workspace")
        if not isinstance(raw_ws, str) or not raw_ws.strip():
            raise HTTPException(409, "工作区不属于本服务")
        if meta.get("direct_workspace"):
            try:
                return validate_direct_workspace_path(raw_ws, allowed_roots=allowed_ws_roots)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        try:
            return _validate_managed_workspace_dir(raw_ws, (workspaces, state / "worker_workspaces"))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def runtime_for(run: dict) -> AgentRuntime:
        workspace = workspace_for(run)
        config = run["metadata"]
        try:
            resolved_prov_name, resolved_prov_arg = _resolve_provider_for_existing_run(run)
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            raise HTTPException(400, f"无法解析任务网关配置：{exc}") from exc
        rt = AgentRuntime(
            storage,
            workspace,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=resolved_prov_arg,
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
            enable_subagents=config.get("enable_subagents"),
            auto_dream=bool(config.get("auto_dream", True)),
            defer_auto_dream=True,
            stream_broker=stream_broker,
            model=config.get("model"),
        )
        rt.provider_name = resolved_prov_name
        rt.knowledge = app.state.knowledge
        rt.skills = app.state.skills
        return rt

    def _schedule_background_dream(run_id: str) -> None:
        """在主任务完全退出执行锁并关闭原 Runtime 后，将离线睡眠整理投递到独立的 dream_jobs 后台集合。

        为什么必须与 jobs[run_id] 彻底解耦：
        1. 不计入 create_run / append_run_message 的内联并发上限，避免连续完成短任务后
           因后台还在做 Wiki 合成而将新用户请求误拒为 429；
        2. 释放 jobs[run_id] 槽位后，用户可在任务完成瞬间无缝发起下一轮追问，不会撞上 409 冲突。
        """
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

    def _track_a2a_push_run_id(run_id: str) -> None:
        a2a_push_run_ids[run_id] = None
        a2a_push_run_ids.move_to_end(run_id)
        while len(a2a_push_run_ids) > 4096:
            a2a_push_run_ids.popitem(last=False)

    def _schedule_a2a_push_notification(run_id: str) -> None:
        """在 A2A 任务进入终态或 input-required 后，以非阻塞尽力投递方式触发已注册的 Webhook 推送通知。"""

        async def _push_worker() -> None:
            try:
                stored_r = await storage.run_async(storage.get_run, run_id)
                if not stored_r:
                    return
                meta = stored_r.get("metadata") if isinstance(stored_r.get("metadata"), dict) else {}
                if not meta.get("a2a_origin") and run_id not in a2a_push_run_ids:
                    return
                a2a_state = run_status_to_a2a_state(stored_r.get("status"))
                if not (is_terminal_a2a_state(a2a_state) or a2a_state == "input-required"):
                    return
                push_cfg = await storage.run_async(storage.get_a2a_push_config, run_id)
                if not push_cfg or not push_cfg.get("url"):
                    return
                allowed_events = push_cfg.get("events") or []
                if allowed_events:
                    normalized_allowed = {
                        str(ev).strip().lower() for ev in allowed_events if str(ev).strip()
                    }
                    raw_status = str(stored_r.get("status") or "").strip().lower()
                    if normalized_allowed and not (
                        normalized_allowed
                        & {"*", "all", "status-update", "task", a2a_state.lower(), raw_status}
                    ):
                        return
                pending_props = await _get_pending_proposals(stored_r)
                task_obj = run_to_a2a_task(stored_r, pending_proposals=pending_props)
                context_id = str(meta.get("a2a_context_id") or run_id)
                status_msg = _a2a_status_message_text(stored_r, a2a_state, pending_props)
                status_event = build_status_update_event(
                    run_id,
                    context_id,
                    a2a_state,
                    message_text=status_msg,
                    final=True,
                )
                payload: dict[str, Any] = {
                    **status_event,
                    "task": task_obj,
                }
                push_transport = getattr(app.state, "a2a_push_transport", None) or getattr(
                    app.state, "a2a_transport", None
                )
                delivery = await deliver_a2a_push_notification(
                    str(push_cfg["url"]),
                    payload,
                    secret=push_cfg.get("secret"),
                    transport=push_transport,
                )
                with contextlib.suppress(Exception):
                    await storage.run_async(
                        storage.add_event,
                        run_id,
                        "a2a_push_delivered" if delivery.get("ok") else "a2a_push_failed",
                        {
                            "url": str(push_cfg["url"]),
                            "state": a2a_state,
                            "ok": bool(delivery.get("ok")),
                            "status_code": delivery.get("status_code"),
                            "error": delivery.get("error"),
                        },
                    )
            except Exception:
                pass

        p_task = asyncio.create_task(_push_worker())
        push_jobs.add(p_task)
        p_task.add_done_callback(push_jobs.discard)

    def dispatch(run_id: str, operation, runtime: AgentRuntime):
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行")
        # 强制推迟内联自动 Dreaming：待主操作退出文件锁并关闭原 Runtime 后再由 _schedule_background_dream 接手
        runtime.defer_auto_dream = True
        # 在返回 202 前显式打开流通道，避免多轮续聊或审批恢复瞬间新连入的 SSE 读到上一轮 closed=True 状态
        stream_broker.reopen_run(run_id)
        active_runtimes[run_id] = runtime

        async def work():
            completed_ok = False
            run_busy_abort = False
            final_run_hint: dict[str, Any] | None = None
            try:
                res = await operation()
                if isinstance(res, dict):
                    final_run_hint = res
                completed_ok = isinstance(res, dict) and res.get("status") == "completed"
            except asyncio.CancelledError:
                cancelled_by_user = False
                with contextlib.suppress(Exception):
                    current = storage.get_run(run_id)
                    if current:
                        final_run_hint = current
                        if current.get("status") == "running":
                            if storage.is_cancel_requested(run_id):
                                current["status"] = "cancelled"
                                current["error"] = "Cancelled"
                                storage.save_run(current)
                                storage.add_event(
                                    run_id,
                                    "cancelled",
                                    {
                                        "reason": (current.get("metadata") or {}).get(
                                            "cancel_reason", "cancelled_task"
                                        )
                                    },
                                )
                                cancelled_by_user = True
                            else:
                                current["status"] = "interrupted"
                                storage.save_run(current)
                                storage.add_event(run_id, "interrupted", {"reason": "server_shutdown"})
                        elif current.get("status") == "cancelled":
                            cancelled_by_user = True
                if cancelled_by_user and (
                    run_id in a2a_push_run_ids
                    or bool(((final_run_hint or {}).get("metadata") or {}).get("a2a_origin"))
                ):
                    _schedule_a2a_push_notification(run_id)
                raise
            except RunBusy:
                run_busy_abort = True
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
                    final_run_hint = current
            finally:
                if active_runtimes.get(run_id) is runtime:
                    active_runtimes.pop(run_id, None)
                with contextlib.suppress(Exception):
                    await runtime.aclose()
                stream_broker.close_run(run_id)
            if completed_ok:
                # 弥合 status="completed" 落盘到 runtime.aclose() 退出极窄窗口内抵达的实时插话消息
                late_steering = await storage.run_async(storage.drain_steering_messages, run_id)
                if late_steering:
                    late_text = "\n\n".join(
                        item["prompt"] for item in late_steering if item.get("prompt")
                    ).strip()
                    if late_text:
                        with contextlib.suppress(Exception):
                            stored_latest = await storage.run_async(storage.get_run, run_id)
                            if stored_latest and stored_latest.get("status") == "completed":
                                l_meta = stored_latest.get("metadata") or {}
                                updated_late = await storage.run_async(
                                    storage.append_user_turn,
                                    run_id,
                                    late_text,
                                    step_extension=max(12, int(l_meta.get("base_max_steps") or 24)),
                                    token_extension=max(30_000, int(l_meta.get("base_max_tokens") or 120_000)),
                                    enqueue=False,
                                    actor_id=late_steering[-1].get("actor_id"),
                                )
                                late_ids = {
                                    item.get("id")
                                    for item in late_steering
                                    if item.get("id") is not None
                                }
                                late_msg_idx = max(0, len(updated_late.get("messages") or []) - 1)
                                late_atts = (updated_late.get("metadata") or {}).get("user_attachments")
                                if late_ids and isinstance(late_atts, list):
                                    changed_atts = False
                                    for att in late_atts:
                                        if (
                                            isinstance(att, dict)
                                            and att.get("steering_id") in late_ids
                                            and att.get("turn_message_idx") is None
                                        ):
                                            att["turn_message_idx"] = late_msg_idx
                                            changed_atts = True
                                    if changed_atts:
                                        with contextlib.suppress(Exception):
                                            await storage.run_async(
                                                storage.save_run,
                                                updated_late,
                                                expected_version=int(
                                                    updated_late.get("state_version") or 1
                                                ),
                                            )
                                next_rt = runtime_for(updated_late)
                                next_rt.max_steps = int(
                                    updated_late["metadata"].get("max_steps", next_rt.max_steps)
                                )
                                next_rt.max_tokens = int(
                                    updated_late["metadata"].get("max_tokens", next_rt.max_tokens)
                                )
                                jobs.pop(run_id, None)
                                dispatch(run_id, lambda: next_rt.execute(run_id), next_rt)
                                return
                _schedule_background_dream(run_id)
            if not run_busy_abort:
                hint_meta = (
                    final_run_hint.get("metadata")
                    if isinstance(final_run_hint, dict)
                    else None
                )
                if run_id in a2a_push_run_ids or (
                    isinstance(hint_meta, dict) and hint_meta.get("a2a_origin")
                ):
                    _schedule_a2a_push_notification(run_id)

        task = asyncio.create_task(work())
        jobs[run_id] = task

        def forget_finished(completed):
            # 持久状态在 SQLite；完成任务不应永久占用进程内调度表。
            if jobs.get(run_id) is completed:
                jobs.pop(run_id)
            if active_runtimes.get(run_id) is runtime:
                active_runtimes.pop(run_id, None)

        task.add_done_callback(forget_finished)

    def public_run(run: dict) -> dict:
        # 消息与仓库内容可从审计事件查看；列表只返回轻量摘要，不暴露本机路径。
        meta = run.get("metadata", {}) or {}
        ver = meta.get("verification", {}) or {}
        cursor_val = (
            Storage.make_run_cursor(run)
            if (run.get("created_at") and run.get("id"))
            else None
        )
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
            "title": run.get("title") or meta.get("title"),
            "archived": bool(run.get("archived", meta.get("archived", False))),
            "model": meta.get("model"),
            "gateway_profile": meta.get("gateway_profile"),
            "gateway_protocol": meta.get("gateway_protocol"),
            "cost_usd": round(float(meta.get("cost_usd", 0.0) or 0.0), 8),
            "max_cost_usd": meta.get("max_cost_usd"),
            "forked_from_run_id": meta.get("forked_from_run_id"),
            "cursor": cursor_val,
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

    @app.get("/api/auth/session")
    async def get_auth_session(request: Request):
        principal = get_principal(request)
        return {
            "ok": True,
            "auth_enabled": auth_configured,
            "authenticated": True,
            "tenant_id": principal.tenant_id,
            "actor_id": principal.actor_id,
            "roles": sorted(principal.roles),
            "global_admin": principal.global_admin,
        }

    @app.post("/api/auth/session")
    async def create_auth_session(
        request: Request,
        body: AuthSessionInput | None = Body(default=None),
    ):
        """使用配置令牌换取 HttpOnly + SameSite=Strict 会话 Cookie，使浏览器 EventSource 支持鉴权流式订阅。"""
        client_ip = request.client.host if (request.client and request.client.host) else "unknown"
        if not auth_rate_limiter.allow(f"ip:{client_ip}"):
            METRICS.inc("agent_lab_auth_rate_limited_total", {"scope": "ip"})
            raise HTTPException(429, "认证请求过于频繁，请稍后重试")
        if not auth_configured:
            return {
                "ok": True,
                "auth_enabled": False,
                "authenticated": True,
                "tenant_id": default_principal.tenant_id,
                "actor_id": default_principal.actor_id,
                "roles": sorted(default_principal.roles),
                "global_admin": default_principal.global_admin,
            }
        candidate = ""
        auth_method = "bearer"
        if body is not None and body.token:
            candidate = body.token
            auth_method = "body"
        else:
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                candidate = auth_header[7:]
        matched = _resolve_token(candidate)
        if matched is None:
            raise HTTPException(401, "无效的认证令牌")
        if not auth_rate_limiter.allow(f"tenant:{matched.tenant_id}"):
            METRICS.inc("agent_lab_auth_rate_limited_total", {"scope": "tenant"})
            raise HTTPException(429, "当前租户认证请求过于频繁，请稍后重试")
        sid = _create_cookie_session(matched)
        is_https = _is_https_request(request)
        resp = JSONResponse(
            {
                "ok": True,
                "auth_enabled": True,
                "authenticated": True,
                "tenant_id": matched.tenant_id,
                "actor_id": matched.actor_id,
                "roles": sorted(matched.roles),
                "global_admin": matched.global_admin,
            }
        )
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=sid,
            max_age=int(_SESSION_TTL_SECONDS),
            httponly=True,
            samesite="strict",
            secure=is_https,
            path="/",
        )
        await _audit_log(
            principal=matched,
            action="auth_session_created",
            resource="/api/auth/session",
            outcome="allowed",
            detail={"method": auth_method, "secure_cookie": is_https},
        )
        return resp

    @app.delete("/api/auth/session")
    async def delete_auth_session(request: Request):
        """注销服务端会话并清除 HttpOnly 会话 Cookie。"""
        cookie_sid = request.cookies.get(SESSION_COOKIE_NAME, "")
        revoked_principal = _delete_cookie_session(cookie_sid)
        existing_principal = revoked_principal or getattr(request.state, "principal", None)
        is_https = _is_https_request(request)
        resp = JSONResponse({"ok": True, "authenticated": False})
        resp.delete_cookie(
            key=SESSION_COOKIE_NAME,
            httponly=True,
            samesite="strict",
            secure=is_https,
            path="/",
        )
        if existing_principal is not None or cookie_sid:
            await _audit_log(
                principal=existing_principal if isinstance(existing_principal, Principal) else None,
                action="auth_session_deleted",
                resource="/api/auth/session",
                outcome="allowed",
                detail={"had_cookie": bool(cookie_sid)},
            )
        return resp

    @app.get("/api/audit-logs")
    async def list_audit_logs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        action: str | None = Query(default=None, max_length=128),
        actor: str | None = Query(default=None, max_length=128),
        tenant_id: str | None = Query(default=None, max_length=64),
    ):
        """按时间倒序分页读取持久化安全审计日志（开启鉴权后仅限 admin 角色访问）。"""
        principal = get_principal(request)
        if auth_configured:
            require_roles(principal, {"admin"}, "需要 admin 角色才能查看安全审计日志")
        if not auth_configured or (principal.global_admin and "admin" in principal.roles):
            filter_tenant = tenant_id.strip() if isinstance(tenant_id, str) and tenant_id.strip() else None
        else:
            filter_tenant = principal.tenant_id
        logs = await storage.run_async(
            storage.list_audit_logs,
            tenant_id=filter_tenant,
            limit=limit,
            action=action,
            actor=actor,
        )
        return {"ok": True, "count": len(logs), "logs": logs}

    @app.get("/api/config")
    async def config(request: Request):
        principal = get_principal(request)
        can_direct = _can_use_direct_workspace(principal)
        is_gw_admin = (
            (principal.global_admin and "admin" in principal.roles)
            if auth_configured
            else ("admin" in principal.roles)
        )
        default_ws_str = (
            str(allowed_ws_roots[0] if allowed_ws_roots else ROOT)
            if can_direct
            else None
        )
        with gateway_lock:
            cur_provider_spec = str(active_gateway_state["provider_spec"])
            cur_gw_info = dict(active_gateway_state["info"])
        cur_gw_profile: str | None = (
            str(cur_gw_info["profile"]).strip()
            if isinstance(cur_gw_info.get("profile"), str) and str(cur_gw_info["profile"]).strip()
            else None
        )
        cur_gw_protocol = str(cur_gw_info.get("protocol") or "demo")

        def _load_gateway_profiles_summary() -> list[dict[str, Any]]:
            try:
                with gateway_lock:
                    raw_doc = load_gateways_config(
                        explicit_path=explicit_gateway_config_path,
                        state_dir=state,
                        tenant_id=gateway_discovery_tenant_id,
                    )
                redacted = redact_gateways_document(raw_doc)
            except ValueError:
                return []
            summaries: list[dict[str, Any]] = []
            for pname, pdata in (redacted.get("profiles") or {}).items():
                if not isinstance(pdata, dict):
                    continue
                summaries.append(
                    {
                        "name": pname,
                        "protocol": pdata.get("protocol"),
                        "model": pdata.get("model") or "",
                        "fast_model": pdata.get("fast_model"),
                        "reasoning_model": pdata.get("reasoning_model"),
                        "base_url": pdata.get("base_url") or "",
                        "credential_configured": bool(pdata.get("credential_configured", False)),
                        "description": pdata.get("description") or "",
                        "active": bool(cur_gw_profile and pname == cur_gw_profile),
                    }
                )
            return summaries

        anthropic_ready = bool(
            (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
            and os.environ.get("ANTHROPIC_MODEL")
        )
        openai_ready = bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"))
        gemini_ready = bool(
            (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
            and os.environ.get("GEMINI_MODEL")
        )
        default_model: str | None = None
        if cur_gw_protocol != "demo" and cur_gw_info.get("model"):
            default_model = str(cur_gw_info["model"]).strip() or None
        elif cur_provider_spec == "anthropic":
            default_model = os.environ.get("ANTHROPIC_MODEL", "").strip() or None
        elif cur_provider_spec in {"openai", "openai_compat", "openai_responses"}:
            default_model = os.environ.get("OPENAI_MODEL", "").strip() or None
        elif cur_provider_spec in {"gemini", "gemini_generate_content"}:
            default_model = os.environ.get("GEMINI_MODEL", "").strip() or None
        def _count_a2a_peers() -> int:
            try:
                with a2a_lock:
                    raw_doc = load_a2a_config(state_dir=state)
                peers = raw_doc.get("peers")
                if not isinstance(peers, dict):
                    return 0
                return sum(
                    1
                    for p in peers.values()
                    if isinstance(p, dict) and bool(p.get("enabled", True))
                )
            except ValueError:
                return 0

        skill_catalog, worker_list, gw_profiles, a2a_peers_count = await asyncio.gather(
            asyncio.to_thread(app.state.skills.catalog),
            storage.run_async(storage.list_workers),
            asyncio.to_thread(_load_gateway_profiles_summary),
            asyncio.to_thread(_count_a2a_peers),
        )
        extra_models: list[str] = []
        if default_model:
            extra_models.append(default_model)
        for item in gw_profiles:
            for m_key in ("model", "fast_model", "reasoning_model"):
                m_val = item.get(m_key)
                if isinstance(m_val, str) and m_val.strip():
                    extra_models.append(m_val.strip())
        gateway_real_ready = (cur_gw_protocol != "demo") or any(
            item.get("protocol") != "demo" and item.get("credential_configured")
            for item in gw_profiles
        )
        capabilities = [
            "sse_streaming",
            "cookie_session_auth",
            "audit_logs",
            "checkpoints_rewind",
            "session_search",
            "session_management",
            "session_fork",
            "attachments",
            "usd_cost_tracking",
            "permission_rules",
            "lifecycle_hooks",
            "session_grants",
            "partial_changeset_approval",
            "auto_compaction",
            "llm_compaction",
            "live_steering",
            "dream_dry_run_rollback",
            "staged_uploads",
            "workspace_browser",
            "file_autocomplete",
            "background_processes",
            "conversation_thread",
            "multi_protocol_gateways",
            "a2a_server",
            "a2a_client",
            "ptc_exec",
            "shadow_workspace",
            "horizon_campaigns",
            "codebase_merkle_index",
            "browser_aria_automation",
            "mcp_elicitation_sampling",
            "a2a_push_notifications",
            "execution_backends",
            "turn_pipeline_middleware",
            "cron_webhook_triggers",
            "eval_flywheel",
            "push_queue_dispatch",
        ]
        return {
            "provider": cur_provider_spec,
            "default_model": default_model,
            "available_models": _build_available_models(extra_models),
            "gateway_profile": cur_gw_profile,
            "gateway_protocol": cur_gw_protocol,
            "active_gateway": cur_gw_info,
            "supported_protocols": list(CANONICAL_PROTOCOLS),
            "gateway_profiles": gw_profiles,
            "a2a_peers_count": a2a_peers_count,
            "a2a_card_url": "/.well-known/agent-card.json",
            "auth_enabled": auth_configured,
            "dispatch_mode": dispatch_mode,
            "knowledge": app.state.knowledge.index_stats(),
            "skills": skill_catalog,
            "single_process": dispatch_mode != "queue",
            "workers": worker_list,
            "real_model_configured": anthropic_ready or openai_ready or gemini_ready or gateway_real_ready,
            "capabilities": capabilities,
            "host_capabilities": {
                "direct_workspace_enabled": can_direct,
                "workspace_browser_enabled": can_direct,
                "default_workspace": default_ws_str,
                "platform": platform.system(),
                "permission_modes": ["ask", "auto-edit", "full-auto", "plan"],
                "checkpoints_rewind": True,
                "attachments_enabled": True,
                "staged_uploads_enabled": True,
                "max_attachment_bytes": _MAX_ATTACHMENT_BYTES,
                "settings_rules_enabled": can_direct and ("admin" in principal.roles),
                "gateway_admin_enabled": is_gw_admin,
                "a2a_admin_enabled": is_gw_admin,
                "horizon_campaigns_enabled": can_direct,
                "sse_streaming": True,
                "cookie_auth": auth_configured,
            },
        }

    @app.get("/api/gateways")
    async def get_gateways(request: Request):
        """返回声明式多协议网关配置脱敏视图与当前活跃网关状态（绝不泄露任何内联密钥或敏感请求头）。"""
        principal = get_principal(request)
        is_gw_admin = (
            (principal.global_admin and "admin" in principal.roles)
            if auth_configured
            else ("admin" in principal.roles)
        )

        def _load_gateways_sync() -> tuple[dict[str, Any], dict[str, Any]]:
            with gateway_lock:
                raw_doc = load_gateways_config(
                    explicit_path=explicit_gateway_config_path,
                    state_dir=state,
                    tenant_id=gateway_discovery_tenant_id,
                )
                redacted = redact_gateways_document(raw_doc)
                cur_info = dict(active_gateway_state["info"])
            return redacted, cur_info

        try:
            redacted_doc, cur_info = await asyncio.to_thread(_load_gateways_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        if is_gw_admin:
            config_path_out = redacted_doc.get("config_path")
        else:
            config_path_out = None
            redacted_doc.pop("config_path", None)

        active_prof_out = cur_info.get("profile") or redacted_doc.get("active_profile")
        return {
            "ok": True,
            "config_path": config_path_out,
            "active_profile": active_prof_out,
            "active_gateway": cur_info,
            "supported_protocols": list(CANONICAL_PROTOCOLS),
            "config": redacted_doc,
        }

    @app.put("/api/gateways")
    async def put_gateways(request: Request):
        """原子校验并保存 gateways.json（开启鉴权后严格限 global_admin，自动保留 ***REDACTED*** 原有密钥并落盘审计）。"""
        principal = get_principal(request)
        _require_global_admin(principal, "需要全局 admin 角色才能修改模型网关配置")
        try:
            raw_payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "非法的 JSON 请求体") from exc
        if not isinstance(raw_payload, dict):
            raise HTTPException(400, "gateways 配置根对象必须为 JSON 对象")
        doc_in = (
            raw_payload["config"]
            if ("profiles" not in raw_payload and isinstance(raw_payload.get("config"), dict))
            else raw_payload
        )

        def _save_gateways_sync() -> tuple[dict[str, Any], dict[str, Any], Any]:
            old_prov_to_close: Any = None
            with gateway_lock:
                target_path = _gateway_config_target_path(for_activate=False)
                existing_doc: dict[str, Any] | None = None
                if target_path.is_symlink() or target_path.exists():
                    try:
                        existing_doc = load_gateways_config(explicit_path=target_path)
                    except ValueError as exc:
                        err_s = str(exc)
                        if any(
                            tok in err_s
                            for tok in ("符号链接", "硬链接", "O_NOFOLLOW", "常规文件")
                        ):
                            raise
                        existing_doc = None
                else:
                    discovered = discover_gateways_config_path(
                        explicit_path=explicit_gateway_config_path,
                        state_dir=state,
                        tenant_id=gateway_discovery_tenant_id,
                    )
                    if discovered is not None:
                        with contextlib.suppress(ValueError):
                            existing_doc = load_gateways_config(explicit_path=discovered)
                merged_doc = _merge_preserved_gateway_secrets(doc_in, existing_doc)
                validated = validate_gateways_document(merged_doc)
                saved_doc = save_gateways_config(target_path, validated)

                # 若当前已处于某个 gateway profile 激活状态，则在编辑后同步热刷新其配置或安全回退
                cur_active_prof = active_gateway_state["info"].get("profile")
                if isinstance(cur_active_prof, str) and cur_active_prof:
                    saved_profiles = saved_doc.get("profiles") or {}
                    saved_active = saved_doc.get("active_profile")
                    candidates: list[str] = []
                    if isinstance(saved_active, str) and saved_active in saved_profiles:
                        candidates.append(saved_active)
                    if cur_active_prof in saved_profiles and cur_active_prof not in candidates:
                        candidates.append(cur_active_prof)

                    refreshed = False
                    for cand_prof in candidates:
                        try:
                            rebuilt = build_provider_from_profile(
                                cand_prof, saved_profiles[cand_prof]
                            )
                        except (ValueError, RuntimeError, NotImplementedError):
                            continue
                        new_info = _build_gateway_info_from_profile(
                            cand_prof,
                            saved_profiles[cand_prof],
                            rebuilt,
                            source="api_put",
                        )
                        old_prov_to_close = active_gateway_state.get("provider_obj")
                        active_gateway_state["provider_spec"] = f"gateway:{cand_prof}"
                        active_gateway_state["provider_obj"] = rebuilt
                        active_gateway_state["info"] = new_info
                        refreshed = True
                        break

                    if not refreshed and cur_active_prof not in saved_profiles:
                        old_prov_to_close = active_gateway_state.get("provider_obj")
                        active_gateway_state["provider_spec"] = "demo"
                        active_gateway_state["provider_obj"] = get_provider("demo")
                        active_gateway_state["info"] = {
                            "profile": None,
                            "protocol": "demo",
                            "model": "demo",
                            "base_url": "",
                            "source": "fallback_demo",
                        }

                redacted = redact_gateways_document(saved_doc)
                cur_info = dict(active_gateway_state["info"])
            return redacted, cur_info, old_prov_to_close

        try:
            redacted_doc, cur_info, old_prov_to_close = await asyncio.to_thread(_save_gateways_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        await _close_provider_safely(old_prov_to_close)
        await _audit_log(
            principal=principal,
            action="gateways_config_update",
            resource="/api/gateways",
            outcome="allowed",
            detail={
                "config_path": redacted_doc.get("config_path"),
                "active_profile": redacted_doc.get("active_profile"),
                "profile_names": sorted((redacted_doc.get("profiles") or {}).keys()),
                "profile_count": len(redacted_doc.get("profiles") or {}),
            },
        )
        return {
            "ok": True,
            "config_path": redacted_doc.get("config_path"),
            "active_profile": cur_info.get("profile") or redacted_doc.get("active_profile"),
            "active_gateway": cur_info,
            "supported_protocols": list(CANONICAL_PROTOCOLS),
            "config": redacted_doc,
        }

    @app.post("/api/gateways/activate")
    async def activate_gateway(body: GatewayActivateInput, request: Request):
        """热切换生效模型网关 Profile：先校验凭证与 URL 并构造 Provider，再原子落盘 active_profile 并热替换运行态。"""
        principal = get_principal(request)
        _require_global_admin(principal, "需要全局 admin 角色才能切换模型网关")
        clean_profile = body.profile.strip()
        if not clean_profile:
            raise HTTPException(400, "profile 不能为空")

        def _activate_gateway_sync() -> tuple[dict[str, Any] | None, Any, Any, Exception | None]:
            with gateway_lock:
                target_path = _gateway_config_target_path(for_activate=True)
                if not target_path.exists() and not target_path.is_symlink():
                    raise FileNotFoundError(
                        "尚未配置 gateways.json，请先通过 PUT /api/gateways 保存网关配置"
                    )
                doc = load_gateways_config(explicit_path=target_path)
                profiles = doc.get("profiles") or {}
                if clean_profile not in profiles:
                    raise ValueError(f"无法激活不存在的 gateway profile: {clean_profile!r}")
                prof_data = profiles[clean_profile]
                built_prov = build_provider_from_profile(clean_profile, prof_data)
                try:
                    updated_doc = set_active_gateway_profile(target_path, clean_profile)
                except Exception as exc:
                    return None, None, built_prov, exc
                new_info = _build_gateway_info_from_profile(
                    clean_profile,
                    updated_doc["profiles"][clean_profile],
                    built_prov,
                    source="api_activate",
                )
                old_prov = active_gateway_state.get("provider_obj")
                active_gateway_state["provider_spec"] = f"gateway:{clean_profile}"
                active_gateway_state["provider_obj"] = built_prov
                active_gateway_state["info"] = new_info
                return new_info, old_prov, None, None

        try:
            new_info, old_prov, failed_built_prov, persist_err = await asyncio.to_thread(
                _activate_gateway_sync
            )
        except (FileNotFoundError, ValueError, RuntimeError, NotImplementedError) as exc:
            raise HTTPException(400, str(exc)) from exc

        if persist_err is not None:
            await _close_provider_safely(failed_built_prov)
            raise HTTPException(400, str(persist_err)) from persist_err

        assert new_info is not None
        await _close_provider_safely(old_prov)
        await _audit_log(
            principal=principal,
            action="gateway_activate",
            resource=f"gateway:{clean_profile}",
            outcome="allowed",
            detail={
                "profile": clean_profile,
                "protocol": new_info.get("protocol"),
                "model": new_info.get("model"),
                "base_url": new_info.get("base_url"),
            },
        )
        return {
            "ok": True,
            "active_profile": clean_profile,
            "active_gateway": new_info,
        }

    @app.post("/api/uploads")
    async def create_staged_upload(body: AttachmentInput, request: Request):
        """安全暂存最大 6 MiB 的图片/PDF/文本上传文件，供后续 POST /api/runs 或 /messages 按 upload_id 引用。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能上传附件")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")
        try:
            parsed = _parse_attachment_payload(
                body.model_dump(exclude_none=True),
                allow_artifact_ref=False,
                allow_upload_ref=False,
            )
        except OverflowError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        def _stage_sync() -> dict[str, Any]:
            with staged_upload_lock:
                return _write_staged_upload(
                    staged_uploads_root,
                    principal.tenant_id,
                    principal.actor_id,
                    parsed,
                )

        try:
            return await asyncio.to_thread(_stage_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/workspaces/browse")
    async def browse_workspaces(
        request: Request,
        path: str | None = Query(default=None, max_length=512),
    ):
        """受控本地目录浏览器：严格受直连工作区权限、敏感目录黑名单、白名单根与零符号链接逃逸约束。"""
        principal = get_principal(request)
        if not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色浏览本地工作区目录")
        raw_target = (
            path.strip()
            if isinstance(path, str) and path.strip()
            else str(allowed_ws_roots[0] if allowed_ws_roots else ROOT)
        )
        try:
            current_dir = validate_direct_workspace_path(raw_target, allowed_roots=allowed_ws_roots)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if any(seg in _FORBIDDEN_DOT_PARTS for seg in current_dir.parts):
            raise HTTPException(400, "禁止浏览敏感隐藏目录")

        filter_tenant = (
            None
            if ("admin" in principal.roles and principal.global_admin)
            else principal.tenant_id
        )

        def _browse_sync() -> dict[str, Any]:
            parent_str: str | None = None
            cand_parent = current_dir.parent
            if cand_parent != current_dir:
                with contextlib.suppress(ValueError):
                    validated_parent = validate_direct_workspace_path(
                        cand_parent, allowed_roots=allowed_ws_roots
                    )
                    parent_str = str(validated_parent)

            directories: list[dict[str, Any]] = []
            try:
                with os.scandir(current_dir) as it:
                    entries = sorted(it, key=lambda e: e.name.casefold())
            except PermissionError as exc:
                raise PermissionError("无权读取目标目录") from exc
            except OSError as exc:
                raise ValueError(f"无法读取目标目录：{exc}") from exc

            for entry in entries:
                if len(directories) >= _BROWSE_MAX_DIRS:
                    break
                name = entry.name
                if (
                    not name
                    or name.startswith(".")
                    or name in _FORBIDDEN_DOT_PARTS
                    or is_host_secret_segment(name)
                ):
                    continue
                if entry.is_symlink():
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child_raw = current_dir / name
                if child_raw.is_symlink():
                    continue
                try:
                    child_resolved = validate_direct_workspace_path(
                        child_raw, allowed_roots=allowed_ws_roots
                    )
                except ValueError:
                    continue
                if child_resolved == current_dir or not child_resolved.is_relative_to(current_dir):
                    continue
                git_marker = child_resolved / ".git"
                is_git = False
                with contextlib.suppress(OSError):
                    is_git = git_marker.exists() and not git_marker.is_symlink()
                directories.append(
                    {
                        "name": name,
                        "path": str(child_resolved),
                        "is_git": is_git,
                    }
                )

            recent_workspaces: list[str] = []
            seen_recent: set[str] = set()
            with contextlib.suppress(Exception):
                recent_runs = storage.list_runs(tenant_id=filter_tenant, limit=40)
                checked = 0
                for item in recent_runs:
                    if len(recent_workspaces) >= 10 or checked >= 20:
                        break
                    meta = item.get("metadata") or {}
                    if not meta.get("direct_workspace"):
                        continue
                    checked += 1
                    full = storage.get_run(item["id"])
                    raw_ws = ((full or {}).get("metadata") or {}).get("workspace")
                    if not isinstance(raw_ws, str) or not raw_ws.strip():
                        continue
                    with contextlib.suppress(ValueError):
                        valid_ws = validate_direct_workspace_path(
                            raw_ws, allowed_roots=allowed_ws_roots
                        )
                        ws_str = str(valid_ws)
                        if ws_str not in seen_recent:
                            seen_recent.add(ws_str)
                            recent_workspaces.append(ws_str)

            roots_out = [str(r) for r in allowed_ws_roots] if allowed_ws_roots else [str(ROOT)]
            return {
                "ok": True,
                "current_path": str(current_dir),
                "parent_path": parent_str,
                "allowed_roots": roots_out,
                "recent_workspaces": recent_workspaces,
                "directories": directories,
            }

        try:
            return await asyncio.to_thread(_browse_sync)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/workspaces/files")
    async def list_direct_workspace_files(
        request: Request,
        workspace_path: str | None = Query(default=None, max_length=512),
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=80, ge=1, le=200),
    ):
        """列出直连本地工作区的受控相对文件路径，供新建任务页 `@file` 自动补全使用。"""
        principal = get_principal(request)
        if not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色列出本地工作区文件")
        raw_ws = (
            workspace_path.strip()
            if isinstance(workspace_path, str) and workspace_path.strip()
            else str(allowed_ws_roots[0] if allowed_ws_roots else ROOT)
        )
        try:
            ws_dir = validate_direct_workspace_path(raw_ws, allowed_roots=allowed_ws_roots)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if any(seg in _FORBIDDEN_DOT_PARTS for seg in ws_dir.parts):
            raise HTTPException(400, "禁止列举敏感隐藏目录文件")
        result = await asyncio.to_thread(_list_workspace_files, ws_dir, query=q, limit=limit)
        return {**result, "workspace_path": str(ws_dir)}

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

    def _authorize_run_creation(body: TaskInput, principal: Principal) -> None:
        is_direct = body.template == "local_cwd" or bool(body.workspace_path)
        needs_host_priv = (
            is_direct
            or body.execution_profile in {"host", "computer"}
            or bool(body.enable_gui)
        )
        if needs_host_priv and not _can_use_direct_workspace(principal):
            raise HTTPException(
                403,
                "仅限本机或管理员角色使用本地目录直连、宿主机执行（host/computer）或桌面 GUI 模式",
            )
        if auth_configured and body.permission_mode == "full-auto":
            require_roles(
                principal,
                {"approver", "admin"},
                "需要 approver 或 admin 角色才能以 full-auto 权限模式创建任务",
            )

    def _claim_attachment_batch(
        parsed_items: list[dict[str, Any]], tenant_id: str
    ) -> tuple[list[dict[str, Any]], list[tuple[Path, Path, Path]]]:
        """原子认领一批附件中的暂存 upload_id（校验租户归属与软/硬链安全），任一失败时自动回滚本批已认领文件。"""
        if not parsed_items or not any(item.get("mode") == "staged" for item in parsed_items):
            return list(parsed_items), []
        resolved_items: list[dict[str, Any]] = []
        claims: list[tuple[Path, Path, Path]] = []
        with staged_upload_lock:
            try:
                for item in parsed_items:
                    if item.get("mode") == "staged":
                        materialized, claim = _claim_staged_upload(
                            staged_uploads_root, tenant_id, item
                        )
                        resolved_items.append(materialized)
                        claims.append(claim)
                    else:
                        resolved_items.append(item)
            except Exception:
                _restore_staged_claims(staged_uploads_root, claims)
                raise
        return resolved_items, claims

    def _prepare_workspace_and_run(
        body: TaskInput,
        principal: Principal,
        traceparent_str: str,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        _authorize_run_creation(body, principal)
        is_direct = body.template == "local_cwd" or bool(body.workspace_path)

        try:
            clean_model = _clean_model_override(body.model)
            parsed_attachments = _parse_attachment_list(
                body.attachments,
                allow_artifact_ref=False,
                allow_upload_ref=True,
            )
            resolved_attachments, staged_claims = _claim_attachment_batch(
                parsed_attachments, principal.tenant_id
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        extra_meta: dict[str, Any] = {
            "template": "local_cwd" if is_direct else body.template,
            "tenant_id": principal.tenant_id,
            "created_by": principal.actor_id,
            "actor_id": principal.actor_id,
            "memory_scope": principal.memory_scope(),
            "traceparent": traceparent_str,
        }
        if clean_model is not None:
            extra_meta["model"] = clean_model
        if body.max_cost_usd is not None:
            extra_meta["max_cost_usd"] = float(body.max_cost_usd)
        if extra_metadata:
            for mk, mv in extra_metadata.items():
                if mk not in {
                    "template",
                    "tenant_id",
                    "created_by",
                    "actor_id",
                    "memory_scope",
                    "traceparent",
                    "workspace",
                    "direct_workspace",
                    "execution_profile",
                    "permission_mode",
                }:
                    extra_meta[mk] = mv
        commit_on_pass = False
        allocated_temp_ws: Path | None = None
        try:
            if is_direct:
                target_raw = (body.workspace_path or "").strip() or str(ROOT)
                base_local_ws = validate_direct_workspace_path(target_raw, allowed_roots=allowed_ws_roots)
                eff_profile = body.execution_profile or ("computer" if body.enable_gui else "host")
                eff_perm = body.permission_mode or "auto-edit"
                # 绑定源项目目录的统一记忆标识，使并发开启的多个本地或独立 Worktree Agent 共享同一项目知识图谱
                extra_meta["project_slug"] = compute_project_slug(
                    base_local_ws, direct_workspace=True, state_dir=state
                )
                if body.isolate_worktree and is_git_worktree(base_local_ws):
                    workspace = workspaces / uuid.uuid4().hex
                    allocated_temp_ws = workspace
                    git_meta = create_task_worktree(base_local_ws, workspace, ref="HEAD")
                    extra_meta.update(git=git_meta, direct_workspace=False, isolated_local_worktree=True)
                else:
                    workspace = base_local_ws
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
            effective_prompt = (
                _expand_at_file_mentions(body.prompt, workspace)
                if eff_profile in {"host", "computer"} and "@" in body.prompt
                else body.prompt
            )
            try:
                run_provider_spec, run_provider_arg, run_gw_info = _resolve_provider_for_new_run(
                    body.gateway_profile
                )
            except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
                raise ValueError(str(exc)) from exc
            if run_gw_info.get("profile"):
                extra_meta["gateway_profile"] = str(run_gw_info["profile"])
            if run_gw_info.get("protocol"):
                extra_meta["gateway_protocol"] = str(run_gw_info["protocol"])
            extra_meta["gateway"] = {
                "profile": run_gw_info.get("profile"),
                "protocol": run_gw_info.get("protocol"),
                "model": run_gw_info.get("model"),
                "base_url": run_gw_info.get("base_url"),
                "source": run_gw_info.get("source"),
            }
            runtime = AgentRuntime(
                storage,
                workspace,
                ROOT / "data/knowledge",
                ROOT / "skills",
                run_provider_arg,
                commit_on_pass=commit_on_pass,
                execution_profile=eff_profile,
                permission_mode=eff_perm,
                enable_gui=bool(body.enable_gui),
                artifacts_dir=artifacts_root,
                stream_broker=stream_broker,
                model=clean_model,
            )
            runtime.provider_name = run_provider_spec
            runtime.knowledge = app.state.knowledge
            runtime.skills = app.state.skills
            run = runtime.create(effective_prompt, body.mode, extra_metadata=extra_meta)
            run_meta_created = run.setdefault("metadata", {})
            needs_a2a_ctx_save = False
            if run_meta_created.get("a2a_origin") and not run_meta_created.get("a2a_context_id"):
                run_meta_created["a2a_context_id"] = run["id"]
                needs_a2a_ctx_save = True
            if resolved_attachments or needs_a2a_ctx_save:
                att_records: list[dict[str, Any]] = []
                try:
                    for item in resolved_attachments:
                        att_records.append(
                            _write_attachment_artifact(
                                artifacts_root,
                                run["id"],
                                item,
                                actor_id=principal.actor_id,
                                turn_message_idx=0,
                            )
                        )
                    expected_ver = int(run.get("state_version") or 1)
                    if att_records:
                        run_meta_created.setdefault("user_attachments", []).extend(att_records)
                    storage.save_run(run, expected_version=expected_ver)
                except Exception:
                    _cleanup_written_attachments(artifacts_root, run["id"], att_records)
                    raise
            if staged_claims:
                with staged_upload_lock:
                    _finalize_staged_claims(staged_uploads_root, staged_claims)
            return runtime, run
        except (GitRepoError, ValueError) as exc:
            if staged_claims:
                with staged_upload_lock:
                    _restore_staged_claims(staged_uploads_root, staged_claims)
            if allocated_temp_ws is not None:
                shutil.rmtree(allocated_temp_ws, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc
        except Exception:
            if staged_claims:
                with staged_upload_lock:
                    _restore_staged_claims(staged_uploads_root, staged_claims)
            if allocated_temp_ws is not None:
                shutil.rmtree(allocated_temp_ws, ignore_errors=True)
            raise

    @app.post("/api/runs", status_code=202)
    async def create_run(body: TaskInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能创建任务")
        _authorize_run_creation(body, principal)
        if not rate_limiter.allow(principal.tenant_id):
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "rate_limited"})
            raise HTTPException(429, "当前租户创建任务频率超过限制")
        if dispatch_mode == "inline" and sum(not job.done() for job in jobs.values()) >= max_inline_jobs:
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "inline_capacity"})
            raise HTTPException(429, f"本机最多同时运行 {max_inline_jobs} 个并发 Agent 任务")
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

    def _list_runs_hydrated(
        filter_tenant: str | None,
        *,
        limit: int = 200,
        query: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
    ) -> list[dict]:
        runs = storage.list_runs(
            tenant_id=filter_tenant,
            limit=limit,
            query=query,
            status=status,
            include_archived=include_archived,
            cursor=cursor,
        )
        for item in runs:
            meta = item.setdefault("metadata", {})
            if "error_detail" not in meta and item.get("status") in {"failed", "budget_exceeded"}:
                full = storage.get_run(item["id"])
                if full is not None:
                    meta["error_detail"] = (full.get("metadata") or {}).get("error_detail")
        return runs

    @app.get("/api/runs")
    async def list_runs(
        request: Request,
        all_tenants: bool = False,
        q: str | None = Query(default=None, max_length=200),
        status: str | None = Query(default=None, max_length=64),
        include_archived: bool = False,
        cursor: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        principal = get_principal(request)
        filter_tenant = (
            None if (all_tenants and "admin" in principal.roles and principal.global_admin) else principal.tenant_id
        )
        runs = await storage.run_async(
            _list_runs_hydrated,
            filter_tenant,
            limit=limit,
            query=q,
            status=status,
            include_archived=include_archived,
            cursor=cursor,
        )
        return [public_run(run) for run in runs]

    @app.get("/api/runs/{run_id}")
    async def run_details(run_id: str, request: Request):
        principal = get_principal(request)
        stored = await get_run_async(run_id, principal)
        meta = stored.get("metadata") or {}
        run_tenant = str(stored.get("tenant_id") or meta.get("tenant_id") or "local").strip() or "local"
        run = public_run(stored)
        proposals_list, lease_doc, pending_steering, checkpoints_list = await asyncio.gather(
            storage.run_async(storage.proposals, run_id),
            storage.run_async(storage.get_lease, run_id),
            storage.run_async(storage.pending_steering_messages, run_id),
            storage.run_async(storage.list_checkpoints, run_id, run_tenant),
        )
        run["proposals"] = proposals_list
        run["pending_steering"] = pending_steering
        ctx_payload = dict(meta.get("context_stats") or {})
        if meta.get("context_window_tokens") is not None:
            ctx_payload.setdefault("context_window_tokens", meta["context_window_tokens"])
        run["context"] = ctx_payload
        run["context_window_tokens"] = meta.get("context_window_tokens")
        run["compaction_summary"] = (meta.get("compaction_summaries") or {}).get(
            "coordinator", ""
        )
        run["verification"] = meta.get("verification", {})
        run["kv_cache"] = summarize_kv_cache(meta.get("kv_cache"))
        run["git"] = meta.get("git")
        run["lease"] = lease_doc
        run["cost_usd"] = round(float(meta.get("cost_usd", 0.0) or 0.0), 8)
        run["cost_breakdown"] = meta.get("cost_breakdown") or {
            "input_cost_usd": 0.0,
            "cache_write_cost_usd": 0.0,
            "cache_read_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "total_cost_usd": 0.0,
            "model": meta.get("model") or "",
            "pricing_known": False,
        }
        run["max_cost_usd"] = meta.get("max_cost_usd")
        run["model"] = meta.get("model")
        run["gateway"] = meta.get("gateway")
        run["session_grants"] = list(meta.get("session_grants") or [])
        run["checkpoints_count"] = len(checkpoints_list)
        run["checkpoints"] = [_summarize_checkpoint(cp) for cp in checkpoints_list]
        run["user_attachments"] = list(meta.get("user_attachments") or [])
        wm = meta.get("working_memory")
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
            for snapshot in meta.get("active_skills", {}).values()
        ]
        full_thread, followup_turns = _build_conversation_thread(stored)
        run["conversation_thread"] = full_thread
        run["initial_turn"] = full_thread[0] if full_thread else None
        run["conversation_turns"] = followup_turns
        run["thinking"] = full_thread[-1]["thinking"] if full_thread else ""
        run["waiting_proposal_ids"] = meta.get("waiting_proposal_ids", [])
        run["job_active"] = bool(run_id in jobs and not jobs[run_id].done())
        run["dream_pending"] = any(not t.done() for t in dream_jobs)
        recent_stream = stream_broker.recent_events(run_id)
        run["stream_latest_seq"] = (
            max(int(ev.get("seq", 0) or 0) for ev in recent_stream) if recent_stream else 0
        )
        return run

    @app.patch("/api/runs/{run_id}")
    async def update_run(run_id: str, body: UpdateRunInput, request: Request):
        """重命名会话标题或归档/取消归档会话。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能更新会话")
        await get_run_async(run_id, principal)
        if body.title is None and body.archived is None:
            raise HTTPException(400, "至少需要提供 title 或 archived 字段")
        eff_tenant = (
            None if (principal.global_admin and "admin" in principal.roles) else principal.tenant_id
        )
        updated: dict[str, Any] | None = None
        try:
            if body.title is not None:
                updated = await storage.run_async(
                    storage.rename_run, run_id, body.title, eff_tenant
                )
            if body.archived is not None:
                updated = await storage.run_async(
                    storage.archive_run, run_id, body.archived, eff_tenant
                )
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if updated is None:
            raise HTTPException(404, "任务不存在")
        return public_run(updated)

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str, request: Request):
        """原子删除会话记录及其专属产物/检查点快照（绝不删除用户直连工作区或 append-only 审计日志）。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能删除会话")
        await get_run_async(run_id, principal)
        if run_id in jobs and not jobs[run_id].done():
            raise HTTPException(409, "任务正在运行中，请先取消后再删除")
        eff_tenant = (
            None if (principal.global_admin and "admin" in principal.roles) else principal.tenant_id
        )
        try:
            deleted = await storage.run_async(storage.delete_run, run_id, eff_tenant)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not deleted:
            raise HTTPException(404, "任务不存在")
        a2a_push_run_ids.pop(run_id, None)
        stream_broker.close_run(run_id)
        if _SAFE_RUN_ID_RE.fullmatch(run_id):
            for base_dir in (artifacts_root, state / "checkpoints"):
                if base_dir.is_symlink() or not base_dir.is_dir():
                    continue
                raw_run_dir = base_dir / run_id
                if raw_run_dir.is_symlink():
                    continue
                base_resolved = base_dir.resolve()
                target_dir = raw_run_dir.resolve()
                if (
                    target_dir != base_resolved
                    and target_dir.is_relative_to(base_resolved)
                    and raw_run_dir.is_dir()
                    and not raw_run_dir.is_symlink()
                ):
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(shutil.rmtree, raw_run_dir)
        return {"ok": True, "id": run_id, "deleted": True}

    @app.post("/api/runs/{run_id}/fork")
    async def fork_run(
        run_id: str,
        request: Request,
        body: ForkRunInput | None = Body(default=None),
    ):
        """从已有会话（可选截断至指定消息下标）分叉创建独立新会话分支。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能分叉会话")
        stored = await get_run_async(run_id, principal)
        if _is_direct_workspace_run(stored.get("metadata")) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色分叉本地直连工作区会话")
        eff_tenant = (
            None if (principal.global_admin and "admin" in principal.roles) else principal.tenant_id
        )
        up_to = body.up_to_message_idx if body is not None else None
        title = body.title if body is not None else None
        try:
            forked = await storage.run_async(
                storage.fork_run,
                run_id,
                up_to_message_idx=up_to,
                tenant_id=eff_tenant,
                title=title,
            )
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        pub = public_run(forked)
        return {
            **pub,
            "forked_from_run_id": run_id,
            "forked_at_message_idx": (forked.get("metadata") or {}).get("forked_at_message_idx"),
            "messages_count": len(forked.get("messages") or []),
        }

    @app.get("/api/runs/{run_id}/checkpoints")
    async def list_run_checkpoints(run_id: str, request: Request):
        """列出指定会话已持久化的轮次检查点清单，校验租户归属与直连工作区权限。"""
        principal = get_principal(request)
        stored = await get_run_async(run_id, principal)
        meta = stored.get("metadata") or {}
        if _is_direct_workspace_run(meta) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色访问本地直连工作区检查点")
        run_tenant = str(stored.get("tenant_id") or meta.get("tenant_id") or "local").strip() or "local"
        cps = await storage.run_async(storage.list_checkpoints, run_id, run_tenant)
        return {
            "ok": True,
            "run_id": run_id,
            "count": len(cps),
            "checkpoints": [_summarize_checkpoint(cp, include_manifest=True) for cp in cps],
        }

    @app.post("/api/runs/{run_id}/rewind")
    async def rewind_run_checkpoint(run_id: str, body: RewindInput, request: Request):
        """将工作区文件与/或对话历史时间旅行回退到指定检查点（both / files / messages）。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能回退检查点")
        cur_run = await get_run_async(run_id, principal)
        if _is_direct_workspace_run(cur_run.get("metadata")) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色回退本地直连工作区检查点")
        if (run_id in jobs and not jobs[run_id].done()) or cur_run.get("status") in {"running", "queued"}:
            raise HTTPException(409, "任务正在运行中，请等待当前轮次结束后再回退检查点")
        lease = await storage.run_async(storage.get_lease, run_id)
        if lease and (
            lease.get("queue_state") == "queued"
            or (
                lease.get("queue_state") == "claimed"
                and (lease.get("lease_until") or 0.0) > time.time()
            )
        ):
            raise HTTPException(409, "任务当前在队列或由活跃 Worker 持有租约")
        runtime = runtime_for(cur_run)
        try:
            report = await asyncio.to_thread(
                runtime.rewind, run_id, body.checkpoint_id, scope=body.scope
            )
        except CheckpointError as exc:
            status_code = 404 if exc.code == "checkpoint_not_found" else 400
            raise HTTPException(status_code, str(exc)) from exc
        except RunBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            err_msg = str(exc)
            raise HTTPException(409 if "正在运行" in err_msg else 400, err_msg) from exc
        finally:
            with contextlib.suppress(Exception):
                await runtime.aclose()
        await _audit_log(
            principal=principal,
            action="run_rewind",
            resource=f"run:{run_id}",
            outcome="allowed",
            detail={
                "checkpoint_id": body.checkpoint_id,
                "scope": body.scope,
                "restored_files": report.get("restored_files", []),
                "deleted_files": report.get("deleted_files", []),
                "messages_before": report.get("messages_before"),
                "messages_after": report.get("messages_after"),
            },
        )
        refreshed = await get_run_async(run_id, principal)
        return {**report, "run": public_run(refreshed)}

    @app.post("/api/runs/{run_id}/attachments")
    async def upload_run_attachment(run_id: str, body: AttachmentInput, request: Request):
        """安全上传并持久化图片或文本附件（上限 6 MiB，允许 png/jpeg/webp/gif/pdf/txt/md），追加到 user_attachments。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能上传附件")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")
        await get_run_async(run_id, principal)
        try:
            parsed = _parse_attachment_payload(
                body.model_dump(exclude_none=True),
                allow_artifact_ref=False,
                allow_upload_ref=False,
            )
        except OverflowError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        def _store_and_attach() -> tuple[dict[str, Any], int]:
            pre_run = storage.get_run(run_id)
            if pre_run is None:
                raise KeyError("run_not_found")
            pre_atts = (pre_run.get("metadata") or {}).get("user_attachments")
            if isinstance(pre_atts, list) and len(pre_atts) >= 32:
                raise ValueError("单会话最多保留 32 个用户附件")

            att_rec = _write_attachment_artifact(
                artifacts_root,
                run_id,
                parsed,
                actor_id=principal.actor_id,
                turn_message_idx=body.turn_message_idx,
            )
            try:
                total_count = 0
                saved = False
                for attempt in range(6):
                    latest = storage.get_run(run_id)
                    if latest is None:
                        raise KeyError("run_not_found")
                    expected_ver = int(latest.get("state_version") or 1)
                    atts = latest.setdefault("metadata", {}).setdefault("user_attachments", [])
                    if not isinstance(atts, list):
                        atts = []
                        latest["metadata"]["user_attachments"] = atts
                    if len(atts) >= 32:
                        raise ValueError("单会话最多保留 32 个用户附件")
                    atts.append(att_rec)
                    try:
                        storage.save_run(latest, expected_version=expected_ver)
                    except StaleEpochError as exc:
                        if "state_version_conflict" in str(exc) and attempt < 5:
                            time.sleep(0.01 * (attempt + 1))
                            continue
                        raise
                    total_count = len(atts)
                    saved = True
                    break
                if not saved:
                    raise StaleEpochError("state_version_conflict")
                storage.add_event(
                    run_id,
                    "attachment_uploaded",
                    {
                        "attachment_id": att_rec["id"],
                        "filename": att_rec["filename"],
                        "artifact_name": att_rec["artifact_name"],
                        "media_type": att_rec["media_type"],
                        "size_bytes": att_rec["size_bytes"],
                    },
                )
                return att_rec, total_count
            except Exception:
                _cleanup_written_attachments(artifacts_root, run_id, [att_rec])
                raise

        try:
            att_rec, total_count = await storage.run_async(_store_and_attach)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except StaleEpochError as exc:
            raise HTTPException(409, "任务状态正在并发更新，请稍后重试") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "run_id": run_id,
            "attachment": att_rec,
            "attachments_count": total_count,
        }

    @app.get("/api/runs/{run_id}/artifacts/{artifact_name}")
    async def get_run_artifact(run_id: str, artifact_name: str, request: Request):
        """安全返回桌面 GUI 截图、用户附件或文本运行产物：租户越权校验 + 扩展名白名单 + 防软链穿越 + CSP 脚本禁闭。

        为什么过滤扩展名（禁止 .html / .svg）之外还必须追加 Content-Security-Policy 头：
        Agent 捕获的文本/JSON 产物可能混有不可信网页或恶意仓库构造的伪造内容；一旦用户在浏览器新标签页
        直接打开产物 URL（与工作台共享同一 Origin），`default-src 'none'; img-src 'self'` 可在浏览器内核层
        彻底禁用一切 JavaScript 执行、内联样式、iframe 嵌套与外发请求，阻断 MIME 嗅探型同源 XSS；
        同时 `Cache-Control: private` 防止反向代理将含敏感桌面信息的截图缓存在共享边缘节点。
        """
        await get_run_async(run_id, get_principal(request))
        if not _SAFE_RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(400, "非法的任务 ID")
        if (
            ".." in artifact_name
            or "/" in artifact_name
            or "\\" in artifact_name
            or not _ARTIFACT_SERVE_RE.fullmatch(artifact_name)
        ):
            raise HTTPException(400, "非法的产物文件名")
        if artifacts_root.is_symlink():
            raise HTTPException(404, "产物不存在")
        raw_run_dir = artifacts_root / run_id
        if raw_run_dir.is_symlink():
            raise HTTPException(404, "产物不存在")
        raw_target = raw_run_dir / artifact_name
        if raw_target.is_symlink():
            raise HTTPException(404, "产物不存在")
        art_base = artifacts_root.resolve()
        run_art_dir = raw_run_dir.resolve()
        if run_art_dir == art_base or not run_art_dir.is_relative_to(art_base):
            raise HTTPException(404, "产物不存在")
        target = raw_target.resolve()
        if (
            target == art_base
            or target == run_art_dir
            or not target.is_relative_to(art_base)
            or not target.is_relative_to(run_art_dir)
        ):
            raise HTTPException(404, "产物不存在")

        def _open_verified_artifact(path: Path) -> tuple[Any, int]:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                raise FileNotFoundError("artifact_open_failed") from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise FileNotFoundError("artifact_not_regular_unique_file")
                fh = os.fdopen(fd, "rb")
            except Exception:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            return fh, int(st.st_size)

        try:
            fh, size_bytes = await asyncio.to_thread(_open_verified_artifact, raw_target)
        except FileNotFoundError as exc:
            raise HTTPException(404, "产物不存在") from exc

        media_type = _ARTIFACT_SERVE_MIME.get(
            raw_target.suffix.lower(), "application/octet-stream"
        )

        async def _stream_artifact():
            try:
                while True:
                    chunk = await asyncio.to_thread(fh.read, 65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                with contextlib.suppress(OSError):
                    fh.close()

        return StreamingResponse(
            _stream_artifact(),
            media_type=media_type,
            headers={
                "Content-Security-Policy": "default-src 'none'; img-src 'self'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=300",
                "Content-Length": str(size_bytes),
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

    @app.post("/api/runs/{run_id}/compact")
    async def compact_run_context(
        run_id: str,
        request: Request,
        body: CompactRunInput | None = Body(default=None),
    ):
        """主动触发会话滚动上下文压缩：归档早期原子消息组并生成确定性 COMPACTION_SUMMARY，保留最近轮次。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能压缩会话上下文")
        cur_run = await get_run_async(run_id, principal)
        if _is_direct_workspace_run(cur_run.get("metadata")) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色压缩本地直连工作区任务上下文")
        if (run_id in jobs and not jobs[run_id].done()) or cur_run.get("status") in {"running", "queued"}:
            raise HTTPException(409, "任务正在运行中，请等待当前轮次结束后再压缩上下文")
        lease = await storage.run_async(storage.get_lease, run_id)
        if lease and (
            lease.get("queue_state") == "queued"
            or (
                lease.get("queue_state") == "claimed"
                and (lease.get("lease_until") or 0.0) > time.time()
            )
        ):
            raise HTTPException(409, "任务当前在队列或由活跃 Worker 持有租约")
        keep_n = body.keep_recent_groups if body is not None else 2
        focus_str = body.focus if body is not None else ""
        use_llm_flag = body.use_llm if body is not None else False
        runtime = runtime_for(cur_run)
        try:
            compaction = await asyncio.to_thread(
                runtime.compact_context,
                run_id,
                keep_recent_groups=keep_n,
                focus=focus_str,
                use_llm=use_llm_flag,
            )
        except (RunBusy, ValueError, ContextOverflow) as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            with contextlib.suppress(Exception):
                await runtime.aclose()
        refreshed = await get_run_async(run_id, principal)
        pub = public_run(refreshed)
        return {**pub, **compaction, "run": pub}

    @app.post("/api/runs/{run_id}/messages", status_code=202)
    async def append_run_message(run_id: str, body: FollowUpInput, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能发送后续指令")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")
        run = await get_run_async(run_id, principal)
        run_meta = run.get("metadata") or {}
        if _is_direct_workspace_run(run_meta) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色向本地直连工作区任务发送指令")

        try:
            clean_model = _clean_model_override(body.model)
            parsed_attachments = _parse_attachment_list(
                body.attachments,
                allow_artifact_ref=True,
                allow_upload_ref=True,
            )
        except OverflowError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        existing_atts = run_meta.get("user_attachments")
        existing_att_cnt = len(existing_atts) if isinstance(existing_atts, list) else 0
        if parsed_attachments and existing_att_cnt + len(parsed_attachments) > 32:
            raise HTTPException(400, "单会话最多保留 32 个用户附件")

        effective_prompt = body.prompt
        run_profile = str(run_meta.get("execution_profile") or "sandbox")
        if run_profile in {"host", "computer"} and "@" in body.prompt:
            with contextlib.suppress(HTTPException):
                ws_dir = workspace_for(run)
                effective_prompt = await asyncio.to_thread(
                    _expand_at_file_mentions, body.prompt, ws_dir
                )

        try:
            resolved_attachments, staged_claims = await asyncio.to_thread(
                _claim_attachment_batch, parsed_attachments, principal.tenant_id
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        claims_finalized = False

        def _finalize_claims_sync() -> None:
            nonlocal claims_finalized
            if staged_claims and not claims_finalized:
                with staged_upload_lock:
                    _finalize_staged_claims(staged_uploads_root, staged_claims)
                claims_finalized = True

        def _restore_claims_sync() -> None:
            if staged_claims and not claims_finalized:
                with staged_upload_lock:
                    _restore_staged_claims(staged_uploads_root, staged_claims)

        def _persist_followup_extras(target_run_id: str, turn_idx: int) -> dict[str, Any]:
            if resolved_attachments:
                pre_run = storage.get_run(target_run_id)
                if pre_run is None:
                    raise KeyError("run_not_found")
                pre_atts = (pre_run.get("metadata") or {}).get("user_attachments")
                cur_cnt = len(pre_atts) if isinstance(pre_atts, list) else 0
                if cur_cnt + len(resolved_attachments) > 32:
                    raise ValueError("单会话最多保留 32 个用户附件")

            att_records: list[dict[str, Any]] = []
            newly_written: list[dict[str, Any]] = []
            try:
                for item in resolved_attachments:
                    rec = _write_attachment_artifact(
                        artifacts_root,
                        target_run_id,
                        item,
                        actor_id=principal.actor_id,
                        turn_message_idx=turn_idx,
                    )
                    att_records.append(rec)
                    if item.get("mode") == "upload":
                        newly_written.append(rec)
                for attempt in range(6):
                    latest = storage.get_run(target_run_id)
                    if latest is None:
                        raise KeyError("run_not_found")
                    expected_ver = int(latest.get("state_version") or 1)
                    l_meta = latest.setdefault("metadata", {})
                    if clean_model is not None:
                        l_meta["model"] = clean_model
                    if body.max_cost_usd is not None:
                        l_meta["max_cost_usd"] = float(body.max_cost_usd)
                    if att_records:
                        atts = l_meta.setdefault("user_attachments", [])
                        if not isinstance(atts, list):
                            atts = []
                            l_meta["user_attachments"] = atts
                        if len(atts) + len(att_records) > 32:
                            raise ValueError("单会话最多保留 32 个用户附件")
                        atts.extend(att_records)
                    try:
                        storage.save_run(latest, expected_version=expected_ver)
                    except StaleEpochError as exc:
                        if "state_version_conflict" in str(exc) and attempt < 5:
                            time.sleep(0.01 * (attempt + 1))
                            continue
                        raise
                    _finalize_claims_sync()
                    return latest
                raise StaleEpochError("state_version_conflict")
            except Exception:
                _cleanup_written_attachments(artifacts_root, target_run_id, newly_written)
                raise

        def _persist_steering_attachments(
            target_run_id: str, steering_id: int
        ) -> list[dict[str, Any]]:
            if not resolved_attachments:
                return []
            pre_run = storage.get_run(target_run_id)
            if pre_run is None:
                raise KeyError("run_not_found")
            pre_atts = (pre_run.get("metadata") or {}).get("user_attachments")
            cur_cnt = len(pre_atts) if isinstance(pre_atts, list) else 0
            if cur_cnt + len(resolved_attachments) > 32:
                raise ValueError("单会话最多保留 32 个用户附件")

            att_records: list[dict[str, Any]] = []
            newly_written: list[dict[str, Any]] = []
            try:
                for item in resolved_attachments:
                    rec = _write_attachment_artifact(
                        artifacts_root,
                        target_run_id,
                        item,
                        actor_id=principal.actor_id,
                        turn_message_idx=None,
                        steering_id=steering_id,
                    )
                    att_records.append(rec)
                    if item.get("mode") == "upload":
                        newly_written.append(rec)

                saved = False
                fenced_by_remote_worker = False
                for attempt in range(8):
                    latest = storage.get_run(target_run_id)
                    if latest is None:
                        raise KeyError("run_not_found")
                    expected_ver = int(latest.get("state_version") or 1)
                    l_meta = latest.setdefault("metadata", {})
                    atts = l_meta.setdefault("user_attachments", [])
                    if not isinstance(atts, list):
                        atts = []
                        l_meta["user_attachments"] = atts
                    if len(atts) + len(att_records) > 32:
                        raise ValueError("单会话最多保留 32 个用户附件")
                    atts.extend(att_records)
                    try:
                        storage.save_run(latest, expected_version=expected_ver)
                    except StaleEpochError as exc:
                        err_s = str(exc)
                        if "state_version_conflict" in err_s and attempt < 7:
                            time.sleep(0.01 * (attempt + 1))
                            continue
                        if "fencing_inline_blocked_by_claimed_lease" in err_s:
                            fenced_by_remote_worker = True
                            break
                        raise
                    saved = True
                    break
                if not saved and not fenced_by_remote_worker:
                    raise StaleEpochError("state_version_conflict")

                for att_rec in att_records:
                    storage.add_event(
                        target_run_id,
                        "attachment_uploaded",
                        {
                            "attachment_id": att_rec["id"],
                            "filename": att_rec["filename"],
                            "artifact_name": att_rec["artifact_name"],
                            "media_type": att_rec["media_type"],
                            "size_bytes": att_rec["size_bytes"],
                            "steering_id": steering_id,
                        },
                    )
                live_rt = active_runtimes.get(target_run_id)
                if live_rt is not None:
                    live_rt.register_external_attachments(target_run_id, att_records)
                _finalize_claims_sync()
                return att_records
            except Exception:
                _cleanup_written_attachments(artifacts_root, target_run_id, newly_written)
                raise

        async def _enqueue_steering_with_attachments(status_hint: str | None) -> dict[str, Any]:
            try:
                rec = await storage.run_async(
                    storage.enqueue_steering_message,
                    run_id,
                    effective_prompt,
                    actor_id=principal.actor_id,
                )
            except KeyError as exc:
                raise HTTPException(404, "任务不存在") from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            att_records: list[dict[str, Any]] = []
            if resolved_attachments:
                try:
                    att_records = await storage.run_async(
                        _persist_steering_attachments, run_id, int(rec["id"])
                    )
                except FileNotFoundError as exc:
                    raise HTTPException(404, str(exc)) from exc
                except StaleEpochError as exc:
                    raise HTTPException(409, "任务状态正在并发更新，请稍后重试") from exc
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            out_steering: dict[str, Any] = {
                "id": run_id,
                "status": status_hint,
                "queued_steering": True,
                "steering": rec,
            }
            if att_records:
                out_steering["attachments"] = att_records
            return out_steering

        try:
            lease = await storage.run_async(storage.get_lease, run_id)
            job_running = bool(run_id in jobs and not jobs[run_id].done())
            lease_claimed = bool(
                lease is not None
                and lease.get("queue_state") == "claimed"
                and (lease.get("lease_until") or 0.0) > time.time()
            )
            lease_queued = bool(lease is not None and lease.get("queue_state") == "queued")

            # 运行期零锁争用实时转向（Mid-Run Live Steering）：写入收件箱表并同步持久化附件，待当前工具步安全边界合并
            if job_running or run.get("status") in {"running", "queued"} or lease_claimed or lease_queued:
                return await _enqueue_steering_with_attachments(run.get("status"))

            is_waiting_approval = run.get("status") == "waiting_approval"
            step_ext = max(12, int(run_meta.get("base_max_steps") or 24))
            tok_ext = max(30_000, int(run_meta.get("base_max_tokens") or 120_000))
            has_extras = bool(
                clean_model is not None or body.max_cost_usd is not None or resolved_attachments
            )
            if dispatch_mode == "queue":
                depth = await asyncio.to_thread(_count_active_queue_depth)
                if depth >= max_queue_depth:
                    raise HTTPException(429, "持久队列待处理任务已达上限")
                try:
                    updated = await storage.run_async(
                        storage.append_user_turn,
                        run_id,
                        effective_prompt,
                        step_extension=step_ext,
                        token_extension=tok_ext,
                        enqueue=True,
                        actor_id=principal.actor_id,
                        supersede_waiting_approval=is_waiting_approval,
                    )
                except ValueError as exc:
                    if str(exc).startswith("run_busy:"):
                        return await _enqueue_steering_with_attachments("running")
                    raise HTTPException(409, str(exc)) from exc
                if has_extras:
                    try:
                        turn_idx = max(0, len(updated.get("messages") or []) - 1)
                        updated = await storage.run_async(_persist_followup_extras, run_id, turn_idx)
                    except FileNotFoundError as exc:
                        raise HTTPException(404, str(exc)) from exc
                    except StaleEpochError as exc:
                        raise HTTPException(409, "任务状态正在并发更新，请稍后重试") from exc
                    except ValueError as exc:
                        raise HTTPException(400, str(exc)) from exc
                payload = public_run(updated)
                if is_waiting_approval:
                    payload["superseded_approval"] = True
                return payload

            if sum(not job.done() for job in jobs.values()) >= max_inline_jobs:
                raise HTTPException(429, f"本机最多同时运行 {max_inline_jobs} 个并发 Agent 任务")
            runtime = runtime_for(run)
            try:
                updated = await storage.run_async(
                    storage.append_user_turn,
                    run_id,
                    effective_prompt,
                    step_extension=step_ext,
                    token_extension=tok_ext,
                    enqueue=False,
                    actor_id=principal.actor_id,
                    supersede_waiting_approval=is_waiting_approval,
                )
            except ValueError as exc:
                await runtime.aclose()
                if str(exc).startswith("run_busy:"):
                    return await _enqueue_steering_with_attachments("running")
                raise HTTPException(409, str(exc)) from exc

            if has_extras:
                try:
                    turn_idx = max(0, len(updated.get("messages") or []) - 1)
                    updated = await storage.run_async(_persist_followup_extras, run_id, turn_idx)
                except FileNotFoundError as exc:
                    await runtime.aclose()
                    raise HTTPException(404, str(exc)) from exc
                except StaleEpochError as exc:
                    await runtime.aclose()
                    raise HTTPException(409, "任务状态正在并发更新，请稍后重试") from exc
                except ValueError as exc:
                    await runtime.aclose()
                    raise HTTPException(400, str(exc)) from exc

            if clean_model is not None:
                runtime.model_override = clean_model
            runtime.max_steps = int(updated["metadata"].get("max_steps", runtime.max_steps))
            runtime.max_tokens = int(updated["metadata"].get("max_tokens", runtime.max_tokens))
            dispatch(run_id, lambda: runtime.execute(run_id), runtime)
            payload = public_run(updated)
            if is_waiting_approval:
                payload["superseded_approval"] = True
            return payload
        except Exception:
            await asyncio.to_thread(_restore_claims_sync)
            raise

    @app.get("/api/runs/{run_id}/files")
    async def list_run_files(
        run_id: str,
        request: Request,
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=80, ge=1, le=200),
    ):
        """列出指定会话工作区的常规非敏感非符号链接相对路径，供 Web `@file` 自动补全与文件探索使用。"""
        principal = get_principal(request)
        stored = await get_run_async(run_id, principal)
        if _is_direct_workspace_run(stored.get("metadata")) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色列出本地直连工作区文件")
        ws = workspace_for(stored)
        result = await asyncio.to_thread(_list_workspace_files, ws, query=q, limit=limit)
        return {**result, "run_id": run_id}

    @app.get("/api/runs/{run_id}/processes")
    async def list_run_processes(run_id: str, request: Request):
        """返回当前会话活跃/近期后台 PTY/Shell 进程及后台子 Agent 任务状态（无存活运行时优雅返回空列表）。"""
        principal = get_principal(request)
        stored = await get_run_async(run_id, principal)
        active_rt = active_runtimes.get(run_id)
        if active_rt is not None:
            processes = await asyncio.to_thread(active_rt.list_background_processes)
            sub_res = await storage.run_async(active_rt.subagent_status, None, run_id=run_id)
            subagents = list(sub_res.get("jobs") or [])
        else:
            processes = []
            bg_map = ((stored.get("metadata") or {}).get("background_subagents")) or {}
            subagents = [
                {"job_id": jid, "run_id": run_id, **entry}
                for jid, entry in bg_map.items()
                if isinstance(entry, dict)
            ]
        return {
            "ok": True,
            "run_id": run_id,
            "live": active_rt is not None,
            "processes": processes,
            "subagents": subagents,
        }

    @app.post("/api/runs/{run_id}/processes/{proc_id}/kill")
    async def kill_run_process(run_id: str, proc_id: str, request: Request):
        """终止指定会话下的后台 PTY/Shell 进程组并写入持久化安全审计日志。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能终止后台进程")
        stored = await get_run_async(run_id, principal)
        if _is_direct_workspace_run(stored.get("metadata")) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色终止本地直连工作区后台进程")
        if not _SAFE_PROC_ID_RE.fullmatch(proc_id):
            raise HTTPException(400, "非法的后台进程 ID")
        active_rt = active_runtimes.get(run_id)
        if active_rt is None:
            raise HTTPException(404, "该任务当前没有活跃的后台进程")
        try:
            result = await asyncio.to_thread(active_rt.kill_background_process, proc_id)
        except KeyError as exc:
            raise HTTPException(404, f"未找到后台进程：{proc_id}") from exc
        except HostToolError as exc:
            status_code = 404 if exc.code == "process_not_found" else 400
            raise HTTPException(status_code, str(exc)) from exc
        with contextlib.suppress(Exception):
            await storage.run_async(
                storage.add_event,
                run_id,
                "background_process_killed",
                {
                    "proc_id": proc_id,
                    "pid": result.get("pid"),
                    "status": result.get("status"),
                    "exit_code": result.get("exit_code"),
                    "actor_id": principal.actor_id,
                },
            )
        await _audit_log(
            principal=principal,
            action="process_kill",
            resource=f"run:{run_id}:proc:{proc_id}",
            outcome="allowed",
            detail={
                "run_id": run_id,
                "proc_id": proc_id,
                "pid": result.get("pid"),
                "status": result.get("status"),
                "exit_code": result.get("exit_code"),
            },
        )
        return {"ok": True, "run_id": run_id, **result}

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

    @app.get("/api/runs/{run_id}/grade")
    async def grade_run_trajectory(run_id: str, request: Request):
        """对租户名下指定运行轨迹执行四维量化评分并提取结构化摩擦信号。"""
        principal = get_principal(request)
        stored = await get_run_async(run_id, principal)
        run_events, pending_steering = await asyncio.gather(
            storage.run_async(storage.events, run_id),
            storage.run_async(storage.pending_steering_messages, run_id),
        )
        hydrated = dict(stored)
        if pending_steering:
            hydrated["pending_steering"] = pending_steering
        grade = await asyncio.to_thread(grade_trajectory, hydrated, run_events)
        return {"ok": True, **grade}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        after_seq: int = Query(default=0, ge=0),
    ):
        principal = get_principal(request)
        auth_cookie_sid = str(getattr(request.state, "auth_cookie_sid", "") or "")
        initial_run = await get_run_async(run_id, principal)
        effective_after_seq = after_seq
        if effective_after_seq == 0 and after == 0:
            raw_last_id = (request.headers.get("last-event-id") or "").strip()
            if raw_last_id.isdigit():
                effective_after_seq = int(raw_last_id)

        def _cookie_session_still_valid() -> bool:
            if not auth_cookie_sid:
                return True
            return _resolve_cookie(auth_cookie_sid) is not None

        async def generate():
            if not _cookie_session_still_valid():
                return
            local_job_active = bool(
                (run_id in jobs and not jobs[run_id].done()) or (run_id in active_runtimes)
            )
            use_queue_fallback = (dispatch_mode == "queue") and not local_job_active

            # 仅在独立进程 queue 模式且本地 broker 无 writer 时优雅回退数据库轮询
            if use_queue_fallback:
                cursor = after
                last_emit_ts = time.monotonic()
                while not await request.is_disconnected():
                    if not _cookie_session_still_valid():
                        return
                    current_run = await get_run_async(run_id, principal)
                    lease = await storage.run_async(storage.get_lease, run_id)
                    lease_active = bool(lease and lease.get("queue_state") in {"queued", "claimed"})
                    new_events = await storage.run_async(storage.events, run_id, after=cursor)
                    for event in new_events:
                        if not _cookie_session_still_valid():
                            return
                        cursor = event["id"]
                        last_emit_ts = time.monotonic()
                        yield (
                            "id: "
                            + str(cursor)
                            + "\nevent: event\ndata: "
                            + json.dumps(event, ensure_ascii=False)
                            + "\n\n"
                        )
                    if current_run["status"] not in {"queued", "running"} and not lease_active:
                        if _cookie_session_still_valid():
                            yield "event: done\ndata: {}\n\n"
                        return
                    now_m = time.monotonic()
                    if now_m - last_emit_ts >= sse_keepalive_seconds:
                        if not _cookie_session_still_valid():
                            return
                        last_emit_ts = now_m
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)
                return

            # 事件驱动 RunStreamBroker 订阅路径：直接转发增量帧，零数据库轮询
            run_is_live = local_job_active or initial_run["status"] in {"queued", "running"}
            if not run_is_live:
                buffered = stream_broker.recent_events(run_id, after_seq=effective_after_seq)
                if after == 0 and buffered:
                    for envelope in buffered:
                        if await request.is_disconnected() or not _cookie_session_still_valid():
                            return
                        yield _format_broker_sse(envelope)
                elif effective_after_seq == 0:
                    persisted = await storage.run_async(storage.events, run_id, after=after)
                    for event in persisted:
                        if await request.is_disconnected() or not _cookie_session_still_valid():
                            return
                        yield (
                            "id: "
                            + str(event["id"])
                            + "\nevent: event\ndata: "
                            + json.dumps(event, ensure_ascii=False)
                            + "\n\n"
                        )
                if not await request.is_disconnected() and _cookie_session_still_valid():
                    yield "event: done\ndata: {}\n\n"
                return

            seen_db_event_ids: set[int] = set()
            if after > 0 and effective_after_seq == 0:
                persisted = await storage.run_async(storage.events, run_id, after=after)
                for event in persisted:
                    if await request.is_disconnected() or not _cookie_session_still_valid():
                        return
                    ev_id = event.get("id")
                    if isinstance(ev_id, int):
                        seen_db_event_ids.add(ev_id)
                    yield (
                        "id: "
                        + str(event["id"])
                        + "\nevent: event\ndata: "
                        + json.dumps(event, ensure_ascii=False)
                        + "\n\n"
                    )

            sub_iter = stream_broker.subscribe(run_id, after_seq=effective_after_seq)
            next_task: asyncio.Task[dict[str, Any]] | None = None
            wait_slice = (
                min(sse_keepalive_seconds, 0.5) if auth_cookie_sid else sse_keepalive_seconds
            )
            last_keepalive_ts = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected() or not _cookie_session_still_valid():
                        return
                    if next_task is None:
                        next_task = asyncio.create_task(sub_iter.__anext__())
                    done, _ = await asyncio.wait({next_task}, timeout=wait_slice)
                    if not done:
                        if await request.is_disconnected() or not _cookie_session_still_valid():
                            return
                        now_m = time.monotonic()
                        if now_m - last_keepalive_ts < sse_keepalive_seconds:
                            continue
                        last_keepalive_ts = now_m
                        if run_id not in jobs and run_id not in active_runtimes:
                            latest_run = await storage.run_async(storage.get_run, run_id)
                            if latest_run is None or latest_run.get("status") not in {"queued", "running"}:
                                break
                        yield ": keepalive\n\n"
                        continue
                    try:
                        envelope = next_task.result()
                    except StopAsyncIteration:
                        next_task = None
                        break
                    next_task = None
                    if not _cookie_session_still_valid():
                        return
                    last_keepalive_ts = time.monotonic()
                    if envelope.get("type") == "event":
                        ev_id = (envelope.get("payload") or {}).get("event_id")
                        if isinstance(ev_id, int):
                            if ev_id in seen_db_event_ids:
                                continue
                            seen_db_event_ids.add(ev_id)
                    yield _format_broker_sse(envelope)
            finally:
                if next_task is not None and not next_task.done():
                    next_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await next_task
                with contextlib.suppress(Exception):
                    await sub_iter.aclose()

            if not await request.is_disconnected() and _cookie_session_still_valid():
                yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, request: Request):
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能恢复任务")
        run = await get_run_async(run_id, principal)
        if _is_direct_workspace_run(run.get("metadata")) and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色恢复本地直连工作区任务")
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
        running_job = jobs.get(run_id)
        active_rt = active_runtimes.get(run_id)
        updated = await storage.run_async(
            storage.request_cancel_run, run_id, actor_id=principal.actor_id, reason="api_cancel"
        )
        if active_rt is not None:
            with contextlib.suppress(Exception):
                updated = await storage.run_async(
                    active_rt.cancel_active_run,
                    run_id,
                    actor_id=principal.actor_id,
                    reason="api_cancel",
                )
        if running_job is not None and not running_job.done():
            running_job.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(running_job, timeout=1.0)
        if running_job is not None or active_rt is not None:
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
        target_decision = "approved" if body.approved else "rejected"
        # 幂等去抖：若同一审批决定刚被提交且后台任务正在执行，直接返回 202 而非向前端报 409 错误
        if (
            proposal.get("status") == target_decision
            and run_id in jobs
            and not jobs[run_id].done()
        ):
            return {"id": run_id, "decision": body.approved, "already_in_progress": True}
        if run["status"] != "waiting_approval" or proposal_id not in run["metadata"].get(
            "waiting_proposal_ids", []
        ):
            raise HTTPException(409, "任务没有等待这个提案的审批")

        clean_grant: str | None = None
        if body.grant_pattern is not None:
            g_str = body.grant_pattern.strip()
            if g_str:
                if any(ch in g_str for ch in ("\x00", "\n", "\r")):
                    raise HTTPException(400, "非法的 grant_pattern 格式")
                clean_grant = g_str[:200]

        clean_answer: str | None = None
        if body.answer_text is not None:
            a_str = body.answer_text.strip()
            if "\x00" in a_str:
                raise HTTPException(400, "非法的 answer_text 格式")
            if a_str:
                clean_answer = a_str[:4000]

        clean_selected: list[str] | None = None
        if body.approved and body.selected_files is not None:
            if proposal.get("kind") in {
                "command",
                "gui_action",
                "mcp_call",
                "a2a_call",
                "user_question",
                "plan_approval",
            }:
                raise HTTPException(400, "非文件补丁提案不支持 selected_files")
            prop_files = proposal.get("files") or (
                [{"path": proposal["path"]}] if proposal.get("path") else []
            )
            valid_paths = [str(f["path"]) for f in prop_files if isinstance(f, dict) and f.get("path")]
            valid_set = set(valid_paths)
            deduped: list[str] = []
            for raw_p in body.selected_files:
                p_str = raw_p.strip()
                if not p_str or p_str not in valid_set:
                    raise HTTPException(400, f"selected_files 包含不在该提案中的文件路径：{raw_p!r}")
                if p_str not in deduped:
                    deduped.append(p_str)
            if not deduped:
                raise HTTPException(400, "selected_files 不能为空")
            clean_selected = deduped

        def _narrow_proposal_files(pid: str, selected: list[str]) -> dict[str, Any]:
            selected_set = set(selected)
            with storage._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT body FROM proposals WHERE id=?", (pid,)).fetchone()
                if row is None:
                    raise KeyError("proposal_not_found")
                doc = json.loads(row["body"])
                if doc.get("status") != "pending" or doc.get("applied"):
                    return doc
                orig_files = doc.get("files") or []
                if not orig_files:
                    return doc
                kept = [f for f in orig_files if f.get("path") in selected_set]
                if not kept:
                    raise ValueError("selected_files_empty")
                if len(kept) == len(orig_files):
                    return doc
                doc.setdefault("original_paths", [f["path"] for f in orig_files])
                doc["selected_files"] = [f["path"] for f in kept]
                doc["skipped_files"] = [f["path"] for f in orig_files if f.get("path") not in selected_set]
                doc["files"] = kept
                doc["paths"] = [f["path"] for f in kept]
                single = len(kept) == 1
                doc["path"] = kept[0]["path"] if single else ", ".join(f["path"] for f in kept)
                doc["old_content"] = (
                    kept[0]["old_content"]
                    if single
                    else "\n".join(f"=== {f['path']} ===\n{f['old_content']}" for f in kept)
                )
                doc["new_content"] = (
                    kept[0]["new_content"]
                    if single
                    else "\n".join(f"=== {f['path']} ===\n{f['new_content']}" for f in kept)
                )
                doc["old_sha256"] = (
                    kept[0]["old_sha256"]
                    if single
                    else digest(
                        canonical_json([{"path": f["path"], "sha256": f["old_sha256"]} for f in kept])
                    )
                )
                doc["new_sha256"] = (
                    kept[0]["new_sha256"]
                    if single
                    else digest(
                        canonical_json([{"path": f["path"], "sha256": f["new_sha256"]} for f in kept])
                    )
                )
                con.execute(
                    "UPDATE proposals SET body=? WHERE id=?",
                    (json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False), pid),
                )
                return doc

        def _persist_session_grant_cas(target_run_id: str, pattern: str) -> None:
            for attempt in range(6):
                latest = storage.get_run(target_run_id)
                if latest is None:
                    raise KeyError("run_not_found")
                expected_ver = int(latest.get("state_version") or 1)
                grants = latest.setdefault("metadata", {}).setdefault("session_grants", [])
                if not isinstance(grants, list):
                    grants = []
                    latest["metadata"]["session_grants"] = grants
                if pattern in grants or len(grants) >= 64:
                    return
                grants.append(pattern)
                try:
                    storage.save_run(latest, expected_version=expected_ver)
                except StaleEpochError as exc:
                    if "state_version_conflict" in str(exc) and attempt < 5:
                        time.sleep(0.01 * (attempt + 1))
                        continue
                    raise
                storage.add_event(
                    target_run_id,
                    "session_grant_added",
                    {"proposal_id": proposal_id, "pattern": pattern, "actor_id": principal.actor_id},
                )
                return
            raise StaleEpochError("state_version_conflict")

        def _persist_proposal_answer(pid: str, answer: str) -> None:
            with storage._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT body FROM proposals WHERE id=?", (pid,)).fetchone()
                if row is None:
                    raise KeyError("proposal_not_found")
                doc = json.loads(row["body"])
                if doc.get("status") != "pending" or doc.get("applied"):
                    return
                doc["answer_text"] = answer
                con.execute(
                    "UPDATE proposals SET body=? WHERE id=?",
                    (json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False), pid),
                )

        if clean_selected is not None:
            try:
                await storage.run_async(_narrow_proposal_files, proposal_id, clean_selected)
            except KeyError as exc:
                raise HTTPException(404, "提案不存在") from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        if dispatch_mode == "queue":
            eff_grant = clean_grant if (body.approved and clean_grant is not None) else None
            ae_sig = inspect.signature(storage.approve_and_enqueue)
            ae_params = ae_sig.parameters
            ae_has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in ae_params.values()
            )
            ae_supports_grant = "grant_pattern" in ae_params or ae_has_var_kw
            ae_supports_answer = "answer_text" in ae_params or ae_has_var_kw
            try:
                if eff_grant is not None and not ae_supports_grant:
                    # 若存储层尚未提供原子 grant_pattern 参数，必须在入队前以 CAS 写入 session_grants，杜绝入队后与 Worker 竞态
                    await storage.run_async(_persist_session_grant_cas, run_id, eff_grant)
                if clean_answer is not None and not ae_supports_answer:
                    await storage.run_async(_persist_proposal_answer, proposal_id, clean_answer)
                ae_kwargs: dict[str, Any] = {
                    "actor_id": principal.actor_id,
                    "permission_mode": body.permission_mode,
                }
                if eff_grant is not None and ae_supports_grant:
                    ae_kwargs["grant_pattern"] = eff_grant
                if clean_answer is not None and ae_supports_answer:
                    ae_kwargs["answer_text"] = clean_answer
                await storage.run_async(
                    storage.approve_and_enqueue,
                    run_id,
                    proposal_id,
                    body.approved,
                    **ae_kwargs,
                )
            except KeyError as exc:
                raise HTTPException(404, "提案不存在") from exc
            except (ValueError, StaleEpochError) as exc:
                raise HTTPException(409, str(exc)) from exc
            q_resp: dict[str, Any] = {"id": run_id, "decision": body.approved, "queued": True}
            if eff_grant is not None:
                q_resp["grant_pattern"] = eff_grant
            if clean_selected is not None:
                q_resp["selected_files"] = clean_selected
            if clean_answer is not None:
                q_resp["answer_text"] = clean_answer
            return q_resp
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
            ap_sig = inspect.signature(storage.approve_proposal)
            ap_params = ap_sig.parameters
            ap_has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in ap_params.values()
            )
            ap_kwargs: dict[str, Any] = {"actor_id": principal.actor_id}
            if clean_answer is not None and (ap_has_var_kw or "answer_text" in ap_params):
                ap_kwargs["answer_text"] = clean_answer
            elif clean_answer is not None:
                await storage.run_async(_persist_proposal_answer, proposal_id, clean_answer)
            await storage.run_async(
                storage.approve_proposal, proposal_id, body.approved, **ap_kwargs
            )
        except KeyError as exc:
            raise HTTPException(404, "提案不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime = runtime_for(run)

        def _invoke_approve():
            sig = inspect.signature(runtime.approve)
            params = sig.parameters
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            approve_kwargs: dict[str, Any] = {}
            if clean_grant is not None and (has_var_kw or "grant_pattern" in params):
                approve_kwargs["grant_pattern"] = clean_grant
            if clean_selected is not None and (has_var_kw or "selected_files" in params):
                approve_kwargs["selected_files"] = clean_selected
            if clean_answer is not None and (has_var_kw or "answer_text" in params):
                approve_kwargs["answer_text"] = clean_answer
            return runtime.approve(run_id, proposal_id, body.approved, **approve_kwargs)

        dispatch(run_id, _invoke_approve, runtime)
        inline_resp: dict[str, Any] = {"id": run_id, "decision": body.approved}
        if body.approved and clean_grant is not None:
            inline_resp["grant_pattern"] = clean_grant
        if clean_selected is not None:
            inline_resp["selected_files"] = clean_selected
        if clean_answer is not None:
            inline_resp["answer_text"] = clean_answer
        return inline_resp

    async def _resolve_api_memory_store(
        principal: Principal,
        *,
        run_id: str | None = None,
        workspace_path: str | None = None,
    ) -> tuple[MemoryStore, Path | None]:
        """按四级优先级与多租户边界解析 MemoryStore（run_id -> workspace_path -> 默认目录 -> 沙箱）。

        安全隔离设计：
        1. 指定 run_id 时先过 get_run_async 租户归属鉴权，防止跨租户枚举 run_id 窃取他人项目记忆；
        2. 指定 workspace_path 时强制校验 _can_use_direct_workspace，拦截非管理员借接口探测宿主机；
        3. 普通受限租户在不传参时回退到无工作区绑定的 tpl-sample，与本机宿主仓库 cwd-* 记忆层物理隔离。
        """
        if run_id:
            stored_run = await get_run_async(run_id, principal)
            meta = stored_run.get("metadata") or {}
            if _is_direct_workspace_run(meta) and not _can_use_direct_workspace(principal):
                raise HTTPException(403, "仅限本机或管理员角色访问本地直连工作区记忆")
            r_tenant = str(stored_run.get("tenant_id") or meta.get("tenant_id") or "local").strip() or "local"
            r_scope = str(
                meta.get("memory_scope")
                or ("user" if r_tenant == "local" else f"tenant:{r_tenant}:user")
            )
            ws: Path | None = None
            raw_ws = meta.get("workspace")
            if isinstance(raw_ws, str) and raw_ws.strip():
                if meta.get("direct_workspace"):
                    if _can_use_direct_workspace(principal):
                        with contextlib.suppress(ValueError):
                            ws = validate_direct_workspace_path(raw_ws, allowed_roots=allowed_ws_roots)
                else:
                    with contextlib.suppress(ValueError):
                        cand_ws = _validate_managed_workspace_dir(
                            raw_ws, (workspaces, state / "worker_workspaces")
                        )
                        if cand_ws.is_dir():
                            ws = cand_ws
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
        """读取当前租户作用域下的轻量键值偏好记忆（SQLite memories 表）。"""
        scope = get_principal(request).memory_scope()
        return await storage.run_async(storage.memories, scope)

    @app.post("/api/memory")
    async def remember(body: MemoryInput, request: Request):
        """显式保存用户跨会话键值偏好；此类笔记作为辅助上下文注入，不具备越权指令效力。"""
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
        """返回分层记忆全景：双层 MEMORY.md 索引、页面清单、[[wiki-links]] 图谱与最近 Dreaming 报告。"""
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
        """按层（global / project）与路径读取单篇记忆页及其验证状态、来源证据和入/出链元数据。"""
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
        """新建或修订独立 Wiki / 规则 / 陷阱页，自动完成凭据脱敏、Frontmatter 校验及索引同步。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能编辑记忆笔记")
        if body.sync_workspace_file and not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色向工作区同步写入记忆文件")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=body.run_id, workspace_path=body.workspace_path
        )
        try:
            res = await asyncio.to_thread(
                store.write_page,
                layer=body.layer,
                path=body.path,
                content=body.content,
                title=body.title,
                status=body.status,
                sources=body.sources,
                index_summary=body.index_summary,
                run_id=body.run_id,
                sync_workspace_file=body.sync_workspace_file,
            )
        except MemoryError as exc:
            raise HTTPException(400, str(exc)) from exc
        await _audit_log(
            principal=principal,
            action="memory_page_write",
            resource=f"memory:{body.layer}:{res.get('path', body.path)}",
            outcome="allowed",
            detail={
                "layer": body.layer,
                "path": res.get("path", body.path),
                "status": body.status,
                "sync_workspace_file": body.sync_workspace_file,
                "run_id": body.run_id,
            },
        )
        return res

    @app.delete("/api/memory/page")
    async def delete_memory_page(
        request: Request,
        layer: Literal["global", "project"] = Query(default="project"),
        path: str = Query(min_length=1, max_length=160),
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """删除指定子页并原子摘除顶层 MEMORY.md 中指向该页的 [[wiki-link]] 索引行，避免残留悬空索引。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能删除记忆笔记")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        try:
            res = await asyncio.to_thread(store.delete_page, layer, path)
        except MemoryError as exc:
            raise HTTPException(400, str(exc)) from exc
        await _audit_log(
            principal=principal,
            action="memory_page_delete",
            resource=f"memory:{layer}:{res.get('path', path)}",
            outcome="allowed",
            detail={
                "layer": layer,
                "path": res.get("path", path),
                "deleted": bool(res.get("deleted", False)),
                "run_id": run_id,
            },
        )
        return res

    @app.get("/api/memory/search")
    async def search_memory(
        request: Request,
        q: str = Query(min_length=1, max_length=400),
        scope: Literal["all", "wiki", "transcripts"] = Query(default="all"),
        limit: int = Query(default=6, ge=1, le=15),
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """跨双层结构化 Wiki 笔记与同租户历史会话 Transcript 执行 BM25 + 向量 + RRF 混合检索。"""
        principal = get_principal(request)
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        return await asyncio.to_thread(store.search, q, scope=scope, limit=limit)

    @app.post("/api/memory/dream")
    async def trigger_dream(
        request: Request,
        body: DreamInput | None = Body(default=None),
    ):
        """按需显式触发四阶段离线 Dreaming 整理（支持 dry_run 零落盘预演，退出时确保释放模型 HTTP 连接池）。"""
        req_body = body if body is not None else DreamInput()
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能运行 Dreaming")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=req_body.run_id, workspace_path=req_body.workspace_path
        )
        try:
            _dream_spec, dream_prov_arg, _dream_info = await asyncio.to_thread(
                _resolve_provider_for_new_run, None
            )
            if isinstance(dream_prov_arg, str):
                llm_prov = get_provider(dream_prov_arg)
                owns_dream_prov = True
            else:
                llm_prov = dream_prov_arg
                owns_dream_prov = llm_prov is not injected_provider_obj
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            raise HTTPException(400, f"无法解析当前模型网关：{exc}") from exc
        try:
            engine = DreamEngine(store, llm_provider=llm_prov)
            report = await engine.dream(
                run_ids=[req_body.run_id] if req_body.run_id else None,
                focus_goal=req_body.focus_goal,
                trigger="api",
                dry_run=req_body.dry_run,
            )
            if req_body.run_id and not req_body.dry_run:
                await storage.run_async(
                    storage.add_event,
                    req_body.run_id,
                    "memory_dreamed",
                    {
                        "dream_id": report.get("id"),
                        "trigger": "api",
                        "corrections_count": report["corrections_count"],
                        "pages_written_count": report["pages_written_count"],
                        "links_repaired_count": report["links_repaired_count"],
                    },
                )
            return report
        finally:
            if owns_dream_prov:
                await _close_provider_safely(llm_prov)

    @app.get("/api/memory/dream/snapshots")
    async def list_dream_snapshots(
        request: Request,
        run_id: str | None = Query(default=None, max_length=64),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """列出当前项目可用的 Dreaming 回滚快照元数据（最新在前）。"""
        principal = get_principal(request)
        store, _ = await _resolve_api_memory_store(
            principal, run_id=run_id, workspace_path=workspace_path
        )
        snapshots = await asyncio.to_thread(store.list_dream_snapshots)
        return {
            "ok": True,
            "tenant_id": store.tenant_id,
            "project_slug": store.project_slug,
            "count": len(snapshots),
            "snapshots": snapshots,
        }

    @app.post("/api/memory/dream/rollback")
    async def rollback_dream_snapshot(
        request: Request,
        body: DreamRollbackInput | None = Body(default=None),
    ):
        """恢复最近一次（或指定 snapshot_id）Dreaming 前的双层记忆快照。"""
        req_body = body if body is not None else DreamRollbackInput()
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能回滚 Dreaming 快照")
        store, _ = await _resolve_api_memory_store(
            principal, run_id=req_body.run_id, workspace_path=req_body.workspace_path
        )
        try:
            res = await asyncio.to_thread(
                store.rollback_last_dream, snapshot_id=req_body.snapshot_id
            )
        except MemoryError as exc:
            status = 404 if exc.code == "dream_snapshot_not_found" else 400
            raise HTTPException(status, str(exc)) from exc
        await _audit_log(
            principal=principal,
            action="memory_dream_rollback",
            resource=f"memory:dream:{res.get('snapshot_id') or 'latest'}",
            outcome="allowed",
            detail={
                "snapshot_id": res.get("snapshot_id"),
                "restored_count": len(res.get("restored_files") or []),
                "removed_count": len(res.get("removed_files") or []),
                "project_slug": store.project_slug,
            },
        )
        return res

    @app.get("/api/settings/rules")
    async def get_settings_rules(
        request: Request,
        scope: Literal["global", "project", "effective"] = Query(default="global"),
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """读取全局或直连工作区 `.agent-lab/settings.json` 中的声明式权限规则与生命周期钩子配置。"""
        principal = get_principal(request)
        if auth_configured:
            require_roles(principal, {"admin"}, "需要 admin 角色才能查看权限规则与钩子配置")
        if not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或全局管理员访问规则与钩子配置")

        ws_dir: Path | None = None
        if scope in {"project", "effective"} or (workspace_path and workspace_path.strip()):
            raw_ws = (
                workspace_path.strip()
                if (workspace_path and workspace_path.strip())
                else str(allowed_ws_roots[0] if allowed_ws_roots else ROOT)
            )
            try:
                ws_dir = validate_direct_workspace_path(raw_ws, allowed_roots=allowed_ws_roots)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        def _load_rules_sync() -> dict[str, Any]:
            global_cfg = _read_settings_file_safe(state / "settings.json")
            global_perms, global_hooks = _extract_rules_and_hooks(global_cfg)
            proj_perms: dict[str, list[str]] | None = None
            proj_hooks: dict[str, list[dict[str, Any]]] | None = None
            trusted: bool | None = None

            if ws_dir is not None:
                trusted = is_workspace_trusted(ws_dir, state_dir=state)
                proj_dir = ws_dir / ".agent-lab"
                proj_cfg = (
                    _read_settings_file_safe(proj_dir / "settings.json")
                    if not proj_dir.is_symlink()
                    else {}
                )
                proj_perms, proj_hooks = _extract_rules_and_hooks(proj_cfg)

            if scope == "project":
                active_perms = proj_perms or {"deny": [], "ask": [], "allow": []}
                active_hooks = proj_hooks or {}
            elif scope == "effective":
                eff_g, eff_p = load_settings_layers(state_dir=state, workspace=ws_dir)
                g_p, g_h = _extract_rules_and_hooks(eff_g)
                p_p, p_h = _extract_rules_and_hooks(eff_p)
                active_perms = {
                    bucket: list(dict.fromkeys([*g_p[bucket], *p_p[bucket]]))
                    for bucket in ("deny", "ask", "allow")
                }
                active_hooks = {}
                for ev in sorted(HOOK_EVENTS):
                    merged_h = [*g_h.get(ev, []), *p_h.get(ev, [])]
                    if merged_h:
                        active_hooks[ev] = merged_h
            else:
                active_perms = global_perms
                active_hooks = global_hooks

            return {
                "ok": True,
                "scope": scope,
                "permissions": active_perms,
                "hooks": active_hooks,
                "trusted_workspace": trusted,
                "global": {"permissions": global_perms, "hooks": global_hooks},
                "project": (
                    {"permissions": proj_perms, "hooks": proj_hooks}
                    if ws_dir is not None
                    else None
                ),
            }

        return await asyncio.to_thread(_load_rules_sync)

    @app.put("/api/settings/rules")
    async def put_settings_rules(body: SettingsRulesInput, request: Request):
        """原子更新全局或直连工作区 `.agent-lab/settings.json` 的权限规则与钩子配置（含严格 RBAC 与审计）。"""
        principal = get_principal(request)
        if body.scope == "global":
            _require_global_admin(principal, "需要全局 admin 角色才能修改全局权限规则与钩子配置")
        else:
            if auth_configured:
                require_roles(principal, {"admin"}, "需要 admin 角色才能修改权限规则与钩子配置")
            if not _can_use_direct_workspace(principal):
                raise HTTPException(403, "仅限本机或全局管理员修改项目规则与钩子配置")

        ws_dir: Path | None = None
        if body.scope == "project":
            raw_ws = (
                body.workspace_path.strip()
                if (body.workspace_path and body.workspace_path.strip())
                else str(allowed_ws_roots[0] if allowed_ws_roots else ROOT)
            )
            try:
                ws_dir = validate_direct_workspace_path(raw_ws, allowed_roots=allowed_ws_roots)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        try:
            validated_perms_update: dict[str, list[str]] | None = None
            if body.permissions is not None:
                validated_perms_update = {
                    "deny": _sanitize_rule_list(body.permissions.deny),
                    "ask": _sanitize_rule_list(body.permissions.ask),
                    "allow": _sanitize_rule_list(body.permissions.allow),
                }
            elif any(x is not None for x in (body.deny, body.ask, body.allow)):
                validated_perms_update = {}
                if body.deny is not None:
                    validated_perms_update["deny"] = _sanitize_rule_list(body.deny)
                if body.ask is not None:
                    validated_perms_update["ask"] = _sanitize_rule_list(body.ask)
                if body.allow is not None:
                    validated_perms_update["allow"] = _sanitize_rule_list(body.allow)
            validated_hooks: dict[str, list[dict[str, Any]]] | None = (
                _sanitize_hooks_dict(body.hooks) if body.hooks is not None else None
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        def _write_rules_sync() -> dict[str, Any]:
            if body.scope == "project":
                assert ws_dir is not None
                if ws_dir.is_symlink():
                    raise ValueError("工作区不能是符号链接")
                target_dir = ws_dir / ".agent-lab"
                if target_dir.is_symlink():
                    raise ValueError("项目 .agent-lab 目录不能是符号链接")
                target_dir.mkdir(exist_ok=True)
                if target_dir.is_symlink() or not target_dir.resolve().is_relative_to(ws_dir):
                    raise ValueError("项目 .agent-lab 目录越界")
                target_file = target_dir / "settings.json"
            else:
                if state.is_symlink():
                    raise ValueError("state_dir 不能是符号链接")
                target_file = state / "settings.json"

            if target_file.is_symlink():
                raise ValueError("settings.json 不能是符号链接")

            existing = _read_settings_file_safe(target_file)
            cur_perms, cur_hooks = _extract_rules_and_hooks(existing)
            if validated_perms_update is not None:
                merged_perms = {**cur_perms, **validated_perms_update}
                existing["permissions"] = merged_perms
            else:
                merged_perms = cur_perms
            if validated_hooks is not None:
                existing["hooks"] = validated_hooks
                merged_hooks = validated_hooks
            else:
                merged_hooks = cur_hooks

            encoded = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
            if len(encoded.encode("utf-8")) > _MAX_SETTINGS_BYTES:
                raise ValueError("settings.json 内容超过 256 KiB 上限")

            fd, tmp_name = tempfile.mkstemp(prefix=".settings-", dir=target_file.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, target_file)
            finally:
                if os.path.exists(tmp_name):
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)

            trusted_flag: bool | None = None
            if body.scope == "project" and ws_dir is not None:
                if body.trust_workspace is True:
                    trust_workspace(ws_dir, state_dir=state)
                elif body.trust_workspace is False:
                    untrust_workspace(ws_dir, state_dir=state)
                trusted_flag = is_workspace_trusted(ws_dir, state_dir=state)

            return {
                "ok": True,
                "scope": body.scope,
                "permissions": merged_perms,
                "hooks": merged_hooks,
                "trusted_workspace": trusted_flag,
            }

        try:
            res = await asyncio.to_thread(_write_rules_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        await _audit_log(
            principal=principal,
            action="settings_rules_update",
            resource=f"settings:{body.scope}",
            outcome="allowed",
            detail={
                "scope": body.scope,
                "deny_count": len(res["permissions"].get("deny") or []),
                "ask_count": len(res["permissions"].get("ask") or []),
                "allow_count": len(res["permissions"].get("allow") or []),
                "hook_events": sorted((res.get("hooks") or {}).keys()),
                "trusted_workspace": res.get("trusted_workspace"),
            },
        )
        return res

    @app.post("/api/admin/gc")
    async def admin_gc(request: Request, retention_seconds: float = Query(default=86400.0, ge=0.0)):
        principal = get_principal(request)
        if not _can_run_admin_gc(principal):
            raise HTTPException(403, "需要全局 admin 角色才能执行资源回收")
        storage_gc, containers_gc, removed_staged = await asyncio.gather(
            storage.run_async(
                storage.gc_expired_resources,
                workspaces_dir=workspaces,
                git_origins_dir=git_origins,
                artifacts_dir=artifacts_root,
                retention_seconds=retention_seconds,
            ),
            asyncio.to_thread(reap_orphan_containers),
            asyncio.to_thread(
                _prune_expired_staged_uploads,
                staged_uploads_root,
                max_age_seconds=retention_seconds,
            ),
        )
        with repomap_lock:
            stale_keys = [k for k in app.state.repomap_cache if not Path(k).exists()]
            for k in stale_keys:
                app.state.repomap_cache.pop(k, None)
        await _audit_log(
            principal=principal,
            action="admin_gc",
            resource="/api/admin/gc",
            outcome="allowed",
            detail={
                "retention_seconds": retention_seconds,
                "storage_gc": storage_gc,
                "containers_gc": containers_gc,
                "removed_staged_uploads": removed_staged,
            },
        )
        return {
            "ok": True,
            "storage_gc": storage_gc,
            "containers_gc": containers_gc,
            "removed_staged_uploads": removed_staged,
        }

    @app.post("/api/internal/sandbox/execute")
    async def internal_sandbox_execute(request: Request):
        if not getattr(request.state, "sandbox_token_auth", False):
            _require_global_admin(get_principal(request), "需要全局 admin 角色或有效沙箱令牌")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "非法的 JSON 请求体") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        return await asyncio.to_thread(execute_remote_sandbox_payload, payload)

    # -----------------------------------------------------------------------
    # A2A（Agent2Agent）协议服务端（AgentCard / JSON-RPC 2.0 / SSE）与对端管理 API
    # -----------------------------------------------------------------------

    async def _build_self_agent_card(request: Request, *, extended: bool) -> dict[str, Any]:
        skills_list = await asyncio.to_thread(app.state.skills.catalog)
        with gateway_lock:
            cur_gw_protocol = str(active_gateway_state["info"].get("protocol") or "demo")
        return build_agent_card(
            base_url=str(request.base_url).rstrip("/"),
            skills=skills_list,
            auth_enabled=auth_configured,
            gateway_protocol=cur_gw_protocol,
            extended=extended,
        )

    @app.get("/.well-known/agent-card.json")
    async def a2a_agent_card(request: Request):
        """A2A 公开 AgentCard 发现端点（v0.3 / v1.0 规范路径，零密钥、零内部路径暴露）。"""
        return await _build_self_agent_card(request, extended=False)

    @app.get("/.well-known/agent.json")
    async def a2a_agent_card_legacy(request: Request):
        """A2A 兼容公开 AgentCard 发现端点（v0.2 规范路径）。"""
        return await _build_self_agent_card(request, extended=False)

    @app.get("/extendedAgentCard")
    async def a2a_extended_agent_card(request: Request):
        """A2A 鉴权扩展 AgentCard 端点：需有效身份凭证，返回当前协议及完整 RPC 方法目录。"""
        get_principal(request)
        return await _build_self_agent_card(request, extended=True)

    async def _get_pending_proposals(run_doc: dict[str, Any]) -> list[dict[str, Any]]:
        if run_doc.get("status") != "waiting_approval":
            return []
        all_props = await storage.run_async(storage.proposals, run_doc["id"])
        waiting_ids = set((run_doc.get("metadata") or {}).get("waiting_proposal_ids") or [])
        pending = [
            p
            for p in all_props
            if isinstance(p, dict) and (p.get("status") == "pending" or p.get("id") in waiting_ids)
        ]
        return pending or [
            p for p in all_props if isinstance(p, dict) and p.get("status") == "pending"
        ]

    def _a2a_status_message_text(
        run_doc: dict[str, Any], state_str: str, pending_props: list[dict[str, Any]]
    ) -> str:
        if state_str == "input-required":
            if pending_props:
                p0 = pending_props[0]
                return (
                    f"需要审批或补充指令：[{p0.get('kind', 'action')}] "
                    f"{p0.get('summary') or p0.get('path') or p0.get('tool') or p0.get('id', '')}"
                )
            return "任务已暂停等待人工审批或补充指令。"
        if state_str == "failed":
            return str(run_doc.get("error") or "任务执行失败")
        if state_str == "canceled":
            return str(run_doc.get("error") or "任务已取消")
        if state_str == "completed":
            return str(run_doc.get("answer") or "任务已完成")
        return ""

    def _persist_a2a_attachments_sync(
        target_run_id: str,
        parsed_atts: list[dict[str, Any]],
        *,
        actor_id: str,
        turn_message_idx: int | None = None,
        steering_id: int | None = None,
    ) -> dict[str, Any]:
        pre_run = storage.get_run(target_run_id)
        if pre_run is None:
            raise KeyError("run_not_found")
        if not parsed_atts:
            return pre_run
        pre_atts = (pre_run.get("metadata") or {}).get("user_attachments")
        cur_cnt = len(pre_atts) if isinstance(pre_atts, list) else 0
        if cur_cnt + len(parsed_atts) > 32:
            raise ValueError("单会话最多保留 32 个用户附件")

        att_records: list[dict[str, Any]] = []
        try:
            for item in parsed_atts:
                rec = _write_attachment_artifact(
                    artifacts_root,
                    target_run_id,
                    item,
                    actor_id=actor_id,
                    turn_message_idx=turn_message_idx,
                    steering_id=steering_id,
                )
                att_records.append(rec)

            saved = False
            fenced_by_remote_worker = False
            latest_saved: dict[str, Any] = pre_run
            for attempt in range(8):
                latest = storage.get_run(target_run_id)
                if latest is None:
                    raise KeyError("run_not_found")
                expected_ver = int(latest.get("state_version") or 1)
                l_meta = latest.setdefault("metadata", {})
                atts = l_meta.setdefault("user_attachments", [])
                if not isinstance(atts, list):
                    atts = []
                    l_meta["user_attachments"] = atts
                if len(atts) + len(att_records) > 32:
                    raise ValueError("单会话最多保留 32 个用户附件")
                atts.extend(att_records)
                try:
                    storage.save_run(latest, expected_version=expected_ver)
                except StaleEpochError as exc:
                    err_s = str(exc)
                    if "state_version_conflict" in err_s and attempt < 7:
                        time.sleep(0.01 * (attempt + 1))
                        continue
                    if steering_id is not None and "fencing_inline_blocked_by_claimed_lease" in err_s:
                        fenced_by_remote_worker = True
                        latest_saved = latest
                        break
                    raise
                latest_saved = latest
                saved = True
                break
            if not saved and not fenced_by_remote_worker:
                raise StaleEpochError("state_version_conflict")

            for att_rec in att_records:
                ev_payload: dict[str, Any] = {
                    "attachment_id": att_rec["id"],
                    "filename": att_rec["filename"],
                    "artifact_name": att_rec["artifact_name"],
                    "media_type": att_rec["media_type"],
                    "size_bytes": att_rec["size_bytes"],
                }
                if steering_id is not None:
                    ev_payload["steering_id"] = steering_id
                storage.add_event(target_run_id, "attachment_uploaded", ev_payload)
            if steering_id is not None:
                live_rt = active_runtimes.get(target_run_id)
                if live_rt is not None:
                    live_rt.register_external_attachments(target_run_id, att_records)
            return latest_saved
        except Exception:
            _cleanup_written_attachments(artifacts_root, target_run_id, att_records)
            raise

    async def _await_a2a_run_completion(
        run_id: str, *, timeout_seconds: float = 120.0
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            cur_job = jobs.get(run_id)
            if cur_job is not None and not cur_job.done():
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(cur_job), timeout=min(remaining, 1.0))
                if jobs.get(run_id) is not None and not jobs[run_id].done():
                    continue
            latest = await storage.run_async(storage.get_run, run_id)
            if latest is None:
                return None
            st = str(latest.get("status") or "")
            a2a_st = run_status_to_a2a_state(st)
            if st == "waiting_approval" or is_terminal_a2a_state(a2a_st):
                if not (run_id in jobs and not jobs[run_id].done()):
                    if dispatch_mode != "queue":
                        return latest
                    lease = await storage.run_async(storage.get_lease, run_id)
                    if not (lease and lease.get("queue_state") in {"queued", "claimed"}):
                        return latest
            elif dispatch_mode == "queue":
                lease = await storage.run_async(storage.get_lease, run_id)
                if (
                    not (lease and lease.get("queue_state") in {"queued", "claimed"})
                    and st not in {"queued", "running"}
                ):
                    return latest
            await asyncio.sleep(min(0.15, max(0.02, deadline - time.monotonic())))
        return await storage.run_async(storage.get_run, run_id)

    def _parse_a2a_push_config_obj(
        cfg_obj: dict[str, Any],
    ) -> tuple[str, str | None, list[str] | None]:
        raw_url = cfg_obj.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError("pushNotificationConfig.url must be a non-empty string")
        clean_url = validate_a2a_url(raw_url)

        raw_secret: Any = None
        for cand_key in ("secret", "token"):
            if cand_key in cfg_obj and cfg_obj.get(cand_key) is not None:
                raw_secret = cfg_obj.get(cand_key)
                break
        if raw_secret is None and isinstance(cfg_obj.get("authentication"), dict):
            auth_creds = cfg_obj["authentication"].get("credentials")
            if auth_creds is not None:
                raw_secret = auth_creds
        if raw_secret is not None and not isinstance(raw_secret, str):
            raise ValueError("pushNotificationConfig secret/token must be a string")
        clean_secret = (
            raw_secret.strip()
            if isinstance(raw_secret, str) and raw_secret.strip()
            else None
        )

        raw_events = cfg_obj.get("events")
        events_list: list[str] | None = None
        if raw_events is not None:
            if not isinstance(raw_events, list) or any(not isinstance(ev, str) for ev in raw_events):
                raise ValueError("pushNotificationConfig.events must be an array of strings")
            events_list = list(raw_events)
        return clean_url, clean_secret, events_list

    def _public_a2a_push_config(cfg: dict[str, Any]) -> dict[str, Any]:
        push_cfg: dict[str, Any] = {
            "url": cfg["url"],
            "events": list(cfg.get("events") or []),
            "hasSecret": bool(cfg.get("has_secret")),
            "createdAt": cfg.get("created_at"),
            "updatedAt": cfg.get("updated_at"),
        }
        if cfg.get("has_secret"):
            push_cfg["token"] = A2A_REDACTED_MARKER
        return {
            "id": cfg["task_id"],
            "taskId": cfg["task_id"],
            "pushNotificationConfig": push_cfg,
        }

    async def _start_or_continue_a2a_run(
        params: dict[str, Any],
        principal: Principal,
        req_id: Any,
        *,
        push_config: tuple[str, str | None, list[str] | None] | None = None,
    ) -> tuple[dict[str, Any] | None, int, dict[str, Any] | None]:
        raw_msg = params.get("message")
        try:
            prompt, extracted_atts, merged_data = extract_a2a_message_parts(raw_msg)
            norm_atts = _normalize_a2a_attachments(extracted_atts)
            parsed_atts = _parse_attachment_list(
                norm_atts,
                allow_artifact_ref=False,
                allow_upload_ref=False,
            )
        except (ValueError, OverflowError) as exc:
            return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))

        raw_tid = (
            params.get("id")
            or params.get("taskId")
            or (raw_msg.get("taskId") if isinstance(raw_msg, dict) else None)
        )
        task_id: str | None = None
        if raw_tid is not None:
            if not isinstance(raw_tid, str) or not raw_tid.strip() or len(raw_tid.strip()) > 128:
                return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Invalid task id")
            task_id = raw_tid.strip()

        raw_cid = (
            (raw_msg.get("contextId") if isinstance(raw_msg, dict) else None)
            or params.get("contextId")
        )
        context_id: str | None = None
        if raw_cid is not None:
            if (
                not isinstance(raw_cid, str)
                or len(raw_cid.strip()) > 128
                or any(ch in raw_cid for ch in ("\x00", "\r", "\n"))
            ):
                return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Invalid contextId")
            context_id = raw_cid.strip() or None

        if task_id is not None:
            existing = await storage.run_async(storage.get_run, task_id)
            if not existing or not _can_access_run(existing, principal):
                return None, 0, jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {task_id}")
            existing_meta = existing.get("metadata") or {}
            if _is_direct_workspace_run(existing_meta) and not _can_use_direct_workspace(principal):
                raise HTTPException(403, "仅限本机或管理员角色向本地直连工作区任务发送指令")

            if push_config is not None:
                existing_tenant = str(
                    existing.get("tenant_id") or existing_meta.get("tenant_id") or "local"
                )
                try:
                    await storage.run_async(
                        storage.set_a2a_push_config,
                        task_id,
                        existing_tenant,
                        push_config[0],
                        secret=push_config[1],
                        events=push_config[2],
                    )
                    _track_a2a_push_run_id(task_id)
                except KeyError:
                    return None, 0, jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {task_id}")
                except ValueError as exc:
                    return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))

            existing_atts = existing_meta.get("user_attachments")
            existing_att_cnt = len(existing_atts) if isinstance(existing_atts, list) else 0
            if parsed_atts and existing_att_cnt + len(parsed_atts) > 32:
                return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "单会话最多保留 32 个用户附件")

            recent_ev = stream_broker.recent_events(task_id)
            stream_start_seq = int(recent_ev[-1]["seq"]) if recent_ev else 0

            async def _enqueue_a2a_steering() -> tuple[dict[str, Any] | None, int, dict[str, Any] | None]:
                try:
                    rec = await storage.run_async(
                        storage.enqueue_steering_message,
                        task_id,
                        prompt,
                        actor_id=principal.actor_id,
                    )
                except KeyError:
                    return None, 0, jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {task_id}")
                except ValueError as exc:
                    return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))
                if parsed_atts:
                    try:
                        await storage.run_async(
                            _persist_a2a_attachments_sync,
                            task_id,
                            parsed_atts,
                            actor_id=principal.actor_id,
                            steering_id=int(rec["id"]),
                        )
                    except KeyError:
                        return None, 0, jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {task_id}")
                    except (ValueError, StaleEpochError) as exc:
                        return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))
                latest_run = await storage.run_async(storage.get_run, task_id)
                return latest_run or existing, stream_start_seq, None

            lease = await storage.run_async(storage.get_lease, task_id)
            job_running = bool(task_id in jobs and not jobs[task_id].done())
            lease_claimed = bool(
                lease is not None
                and lease.get("queue_state") == "claimed"
                and (lease.get("lease_until") or 0.0) > time.time()
            )
            lease_queued = bool(lease is not None and lease.get("queue_state") == "queued")
            if job_running or existing.get("status") in {"running", "queued"} or lease_claimed or lease_queued:
                return await _enqueue_a2a_steering()

            is_waiting_approval = existing.get("status") == "waiting_approval"
            step_ext = max(12, int(existing_meta.get("base_max_steps") or 24))
            tok_ext = max(30_000, int(existing_meta.get("base_max_tokens") or 120_000))

            if dispatch_mode == "queue":
                depth = await asyncio.to_thread(_count_active_queue_depth)
                if depth >= max_queue_depth:
                    raise HTTPException(429, "持久队列待处理任务已达上限")
                try:
                    updated = await storage.run_async(
                        storage.append_user_turn,
                        task_id,
                        prompt,
                        step_extension=step_ext,
                        token_extension=tok_ext,
                        enqueue=True,
                        actor_id=principal.actor_id,
                        supersede_waiting_approval=is_waiting_approval,
                    )
                except ValueError as exc:
                    if str(exc).startswith("run_busy:"):
                        return await _enqueue_a2a_steering()
                    return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))
                if parsed_atts:
                    turn_idx = max(0, len(updated.get("messages") or []) - 1)
                    try:
                        updated = await storage.run_async(
                            _persist_a2a_attachments_sync,
                            task_id,
                            parsed_atts,
                            actor_id=principal.actor_id,
                            turn_message_idx=turn_idx,
                        )
                    except (KeyError, ValueError, StaleEpochError) as exc:
                        return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))
                return updated, stream_start_seq, None

            if sum(not job.done() for job in jobs.values()) >= max_inline_jobs:
                raise HTTPException(429, f"本机最多同时运行 {max_inline_jobs} 个并发 Agent 任务")
            runtime = runtime_for(existing)
            try:
                updated = await storage.run_async(
                    storage.append_user_turn,
                    task_id,
                    prompt,
                    step_extension=step_ext,
                    token_extension=tok_ext,
                    enqueue=False,
                    actor_id=principal.actor_id,
                    supersede_waiting_approval=is_waiting_approval,
                )
            except ValueError as exc:
                await runtime.aclose()
                if str(exc).startswith("run_busy:"):
                    return await _enqueue_a2a_steering()
                return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))

            if parsed_atts:
                turn_idx = max(0, len(updated.get("messages") or []) - 1)
                try:
                    updated = await storage.run_async(
                        _persist_a2a_attachments_sync,
                        task_id,
                        parsed_atts,
                        actor_id=principal.actor_id,
                        turn_message_idx=turn_idx,
                    )
                except (KeyError, ValueError, StaleEpochError) as exc:
                    await runtime.aclose()
                    return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))

            runtime.max_steps = int(updated["metadata"].get("max_steps", runtime.max_steps))
            runtime.max_tokens = int(updated["metadata"].get("max_tokens", runtime.max_tokens))
            dispatch(task_id, lambda: runtime.execute(task_id), runtime)
            return updated, stream_start_seq, None

        # 新建受控沙箱 A2A 任务
        if dispatch_mode == "inline" and sum(not job.done() for job in jobs.values()) >= max_inline_jobs:
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "inline_capacity"})
            raise HTTPException(429, f"本机最多同时运行 {max_inline_jobs} 个并发 Agent 任务")
        if dispatch_mode == "queue":
            depth = await asyncio.to_thread(_count_active_queue_depth)
            if depth >= max_queue_depth:
                METRICS.inc("agent_lab_runs_rejected_total", {"reason": "queue_depth"})
                raise HTTPException(429, "持久队列待处理任务已达上限")

        raw_meta_param = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        req_mode = str(raw_meta_param.get("mode") or "agent").strip()
        eff_mode: Literal["agent", "workflow", "parallel"] = (
            req_mode if req_mode in {"agent", "workflow", "parallel"} else "agent"
        )
        req_tpl = str(raw_meta_param.get("template") or "sample").strip()
        eff_tpl: Literal["sample", "multi_file", "local_cwd"] = (
            req_tpl if req_tpl in {"sample", "multi_file"} else "sample"
        )
        raw_model = raw_meta_param.get("model")
        raw_gw = raw_meta_param.get("gateway_profile")
        try:
            task_body = TaskInput(
                prompt=prompt,
                mode=eff_mode,
                template=eff_tpl,
                execution_profile="sandbox",
                model=str(raw_model).strip() if isinstance(raw_model, str) and raw_model.strip() else None,
                gateway_profile=(
                    str(raw_gw).strip() if isinstance(raw_gw, str) and raw_gw.strip() else None
                ),
                attachments=norm_atts or None,
            )
        except Exception as exc:
            return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))

        a2a_extra_meta: dict[str, Any] = {"a2a_origin": True}
        if context_id is not None:
            a2a_extra_meta["a2a_context_id"] = context_id
        if merged_data:
            a2a_extra_meta["a2a_data"] = merged_data

        traceparent_str = format_traceparent(get_trace_id())
        try:
            runtime, run = await asyncio.to_thread(
                _prepare_workspace_and_run,
                task_body,
                principal,
                traceparent_str,
                extra_metadata=a2a_extra_meta,
            )
        except HTTPException as exc:
            if exc.status_code in {400, 413}:
                return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc.detail))
            raise

        if push_config is not None:
            new_run_tenant = str(
                run.get("tenant_id") or (run.get("metadata") or {}).get("tenant_id") or "local"
            )
            try:
                await storage.run_async(
                    storage.set_a2a_push_config,
                    run["id"],
                    new_run_tenant,
                    push_config[0],
                    secret=push_config[1],
                    events=push_config[2],
                )
                _track_a2a_push_run_id(run["id"])
            except ValueError as exc:
                await runtime.aclose()
                return None, 0, jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))

        if dispatch_mode == "queue":
            await runtime.aclose()
            await storage.run_async(storage.enqueue_run, run["id"], action="execute")
        else:
            dispatch(run["id"], lambda: runtime.execute(run["id"]), runtime)
        return run, 0, None

    def _build_a2a_sse_response(
        req_id: Any,
        target_run: dict[str, Any],
        request: Request,
        *,
        stream_start_seq: int = 0,
        is_resubscribe: bool = False,
    ) -> StreamingResponse:
        run_id = str(target_run["id"])
        meta = target_run.get("metadata") or {}
        context_id = str(meta.get("a2a_context_id") or run_id)
        artifact_id = f"art-{run_id[:8]}-response"
        auth_cookie_sid = str(getattr(request.state, "auth_cookie_sid", "") or "")

        def _cookie_still_valid() -> bool:
            if not auth_cookie_sid:
                return True
            return _resolve_cookie(auth_cookie_sid) is not None

        def _sse_frame(event_obj: dict[str, Any]) -> str:
            return "data: " + json.dumps(jsonrpc_success(req_id, event_obj), ensure_ascii=False) + "\n\n"

        async def _generate():
            if not _cookie_still_valid():
                return

            local_job_active = bool(
                (run_id in jobs and not jobs[run_id].done()) or (run_id in active_runtimes)
            )
            if is_resubscribe and not local_job_active:
                latest_now = await storage.run_async(storage.get_run, run_id) or target_run
                lease_now = await storage.run_async(storage.get_lease, run_id)
                lease_active = bool(
                    lease_now and lease_now.get("queue_state") in {"queued", "claimed"}
                )
                cur_status = str(latest_now.get("status") or "")
                cur_state = run_status_to_a2a_state(cur_status)
                if not lease_active and (
                    cur_status == "waiting_approval" or is_terminal_a2a_state(cur_state)
                ):
                    ans_text = str(latest_now.get("answer") or "").strip()
                    if ans_text:
                        yield _sse_frame(
                            build_artifact_update_event(
                                run_id,
                                context_id,
                                artifact_id,
                                ans_text,
                                append=False,
                                last_chunk=True,
                            )
                        )
                    pending_props = await _get_pending_proposals(latest_now)
                    final_msg = _a2a_status_message_text(latest_now, cur_state, pending_props)
                    yield _sse_frame(
                        build_status_update_event(
                            run_id,
                            context_id,
                            cur_state,
                            message_text=final_msg,
                            final=True,
                        )
                    )
                    return

            yield _sse_frame(
                build_status_update_event(run_id, context_id, "working", final=False)
            )

            use_queue_fallback = (dispatch_mode == "queue") and not local_job_active
            if use_queue_fallback:
                last_emit_ts = time.monotonic()
                while not await request.is_disconnected():
                    if not _cookie_still_valid():
                        return
                    current_run = await storage.run_async(storage.get_run, run_id) or target_run
                    lease = await storage.run_async(storage.get_lease, run_id)
                    lease_active = bool(lease and lease.get("queue_state") in {"queued", "claimed"})
                    cur_status = str(current_run.get("status") or "")
                    cur_state = run_status_to_a2a_state(cur_status)
                    if (
                        cur_status == "waiting_approval" or is_terminal_a2a_state(cur_state)
                    ) and not lease_active:
                        ans_text = str(current_run.get("answer") or "").strip()
                        if ans_text:
                            yield _sse_frame(
                                build_artifact_update_event(
                                    run_id,
                                    context_id,
                                    artifact_id,
                                    ans_text,
                                    append=False,
                                    last_chunk=True,
                                )
                            )
                        pending_props = await _get_pending_proposals(current_run)
                        final_msg = _a2a_status_message_text(current_run, cur_state, pending_props)
                        yield _sse_frame(
                            build_status_update_event(
                                run_id,
                                context_id,
                                cur_state,
                                message_text=final_msg,
                                final=True,
                            )
                        )
                        return
                    now_m = time.monotonic()
                    if now_m - last_emit_ts >= sse_keepalive_seconds:
                        last_emit_ts = now_m
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.25)
                return

            chunk_count = 0
            sub_iter = stream_broker.subscribe(run_id, after_seq=stream_start_seq)
            next_task: asyncio.Task[dict[str, Any]] | None = None
            wait_slice = (
                min(sse_keepalive_seconds, 0.5) if auth_cookie_sid else sse_keepalive_seconds
            )
            last_keepalive_ts = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected() or not _cookie_still_valid():
                        return
                    if next_task is None:
                        next_task = asyncio.create_task(sub_iter.__anext__())
                    done, _ = await asyncio.wait({next_task}, timeout=wait_slice)
                    if not done:
                        if await request.is_disconnected() or not _cookie_still_valid():
                            return
                        now_m = time.monotonic()
                        if now_m - last_keepalive_ts < sse_keepalive_seconds:
                            continue
                        last_keepalive_ts = now_m
                        if run_id not in jobs and run_id not in active_runtimes:
                            latest_probe = await storage.run_async(storage.get_run, run_id)
                            if latest_probe is None or latest_probe.get("status") not in {
                                "queued",
                                "running",
                            }:
                                break
                        yield ": keepalive\n\n"
                        continue
                    try:
                        envelope = next_task.result()
                    except StopAsyncIteration:
                        next_task = None
                        break
                    next_task = None
                    if not _cookie_still_valid():
                        return
                    last_keepalive_ts = time.monotonic()
                    if envelope.get("type") == "text_delta":
                        ev_payload = (
                            envelope.get("payload")
                            if isinstance(envelope.get("payload"), dict)
                            else {}
                        )
                        delta = str(ev_payload.get("delta") or ev_payload.get("text") or "")
                        if delta:
                            yield _sse_frame(
                                build_artifact_update_event(
                                    run_id,
                                    context_id,
                                    artifact_id,
                                    delta,
                                    append=chunk_count > 0,
                                    last_chunk=False,
                                )
                            )
                            chunk_count += 1
            finally:
                if next_task is not None and not next_task.done():
                    next_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await next_task
                with contextlib.suppress(Exception):
                    await sub_iter.aclose()

            if await request.is_disconnected() or not _cookie_still_valid():
                return

            final_run = await storage.run_async(storage.get_run, run_id) or target_run
            if final_run.get("status") in {"queued", "running"}:
                cur_job = jobs.get(run_id)
                if cur_job is not None and not cur_job.done():
                    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        await asyncio.wait_for(asyncio.shield(cur_job), timeout=2.0)
                final_run = await storage.run_async(storage.get_run, run_id) or final_run

            final_state = run_status_to_a2a_state(final_run.get("status"))
            if chunk_count == 0:
                ans_text = str(final_run.get("answer") or "").strip()
                if ans_text:
                    yield _sse_frame(
                        build_artifact_update_event(
                            run_id,
                            context_id,
                            artifact_id,
                            ans_text,
                            append=False,
                            last_chunk=True,
                        )
                    )
            pending_props = await _get_pending_proposals(final_run)
            final_msg = _a2a_status_message_text(final_run, final_state, pending_props)
            yield _sse_frame(
                build_status_update_event(
                    run_id,
                    context_id,
                    final_state,
                    message_text=final_msg,
                    final=True,
                )
            )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/a2a")
    async def a2a_jsonrpc(request: Request):
        """A2A JSON-RPC 2.0 统一服务端端点（双版本兼容 v0.2/v0.3 与 v1.0，支持 SSE 流式输出）。"""
        principal = get_principal(request)
        if not rate_limiter.allow(principal.tenant_id):
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "rate_limited"})
            raise HTTPException(429, "当前租户 A2A 请求频率超过限制")

        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return JSONResponse(jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Parse error: invalid JSON"))

        if not isinstance(payload, dict):
            return JSONResponse(
                jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "Invalid Request: expected JSON object")
            )

        req_id = payload.get("id")
        if req_id is not None and (isinstance(req_id, bool) or not isinstance(req_id, (str, int, float))):
            return JSONResponse(
                jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "Invalid Request: invalid id field")
            )

        method = payload.get("method")
        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method.strip():
            return JSONResponse(
                jsonrpc_error(
                    req_id,
                    JSONRPC_INVALID_REQUEST,
                    "Invalid Request: jsonrpc must be '2.0' and method must be a non-empty string",
                )
            )

        raw_params = payload.get("params")
        if raw_params is None:
            params: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            return JSONResponse(
                jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Invalid params: expected JSON object")
            )

        canonical = normalize_a2a_method(method)
        if canonical is None:
            return JSONResponse(
                jsonrpc_error(req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
            )

        if canonical == "message/send":
            require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能发送 A2A 任务")
            raw_cfg = params.get("configuration")
            if raw_cfg is None:
                configuration: dict[str, Any] = {}
            elif isinstance(raw_cfg, dict):
                configuration = raw_cfg
            else:
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "configuration must be an object")
                )
            blocking = True if configuration.get("blocking") is None else bool(configuration.get("blocking"))
            raw_hl = (
                configuration.get("historyLength")
                if configuration.get("historyLength") is not None
                else params.get("historyLength")
            )
            if raw_hl is not None:
                if isinstance(raw_hl, bool) or not isinstance(raw_hl, int) or raw_hl < 0:
                    return JSONResponse(
                        jsonrpc_error(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "historyLength must be a non-negative integer",
                        )
                    )
            parsed_inline_push: tuple[str, str | None, list[str] | None] | None = None
            raw_inline_push = configuration.get("pushNotificationConfig")
            if raw_inline_push is not None:
                if not isinstance(raw_inline_push, dict):
                    return JSONResponse(
                        jsonrpc_error(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "configuration.pushNotificationConfig must be an object",
                        )
                    )
                try:
                    parsed_inline_push = _parse_a2a_push_config_obj(raw_inline_push)
                except ValueError as exc:
                    return JSONResponse(jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc)))
            initial_run, _start_seq, err_obj = await _start_or_continue_a2a_run(
                params,
                principal,
                req_id,
                push_config=parsed_inline_push,
            )
            if err_obj is not None:
                return JSONResponse(err_obj)
            assert initial_run is not None
            if not blocking:
                pending_props = await _get_pending_proposals(initial_run)
                return JSONResponse(
                    jsonrpc_success(
                        req_id,
                        run_to_a2a_task(
                            initial_run,
                            history_length=raw_hl,
                            pending_proposals=pending_props,
                        ),
                    )
                )
            final_run = await _await_a2a_run_completion(initial_run["id"], timeout_seconds=120.0)
            eff_run = final_run or initial_run
            pending_props = await _get_pending_proposals(eff_run)
            return JSONResponse(
                jsonrpc_success(
                    req_id,
                    run_to_a2a_task(
                        eff_run,
                        history_length=raw_hl,
                        pending_proposals=pending_props,
                    ),
                )
            )

        if canonical == "message/stream":
            require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能流式执行 A2A 任务")
            parsed_stream_push: tuple[str, str | None, list[str] | None] | None = None
            raw_stream_cfg = params.get("configuration")
            if raw_stream_cfg is not None:
                if not isinstance(raw_stream_cfg, dict):
                    return JSONResponse(
                        jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "configuration must be an object")
                    )
                raw_sp = raw_stream_cfg.get("pushNotificationConfig")
                if raw_sp is not None:
                    if not isinstance(raw_sp, dict):
                        return JSONResponse(
                            jsonrpc_error(
                                req_id,
                                JSONRPC_INVALID_PARAMS,
                                "configuration.pushNotificationConfig must be an object",
                            )
                        )
                    try:
                        parsed_stream_push = _parse_a2a_push_config_obj(raw_sp)
                    except ValueError as exc:
                        return JSONResponse(jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc)))
            target_run, start_seq, err_obj = await _start_or_continue_a2a_run(
                params,
                principal,
                req_id,
                push_config=parsed_stream_push,
            )
            if err_obj is not None:
                return JSONResponse(err_obj)
            assert target_run is not None
            return _build_a2a_sse_response(
                req_id,
                target_run,
                request,
                stream_start_seq=start_seq,
                is_resubscribe=False,
            )

        if canonical == "tasks/resubscribe":
            raw_tid = params.get("id") or params.get("taskId")
            if not isinstance(raw_tid, str) or not raw_tid.strip():
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Missing or invalid task id")
                )
            tid = raw_tid.strip()
            existing = await storage.run_async(storage.get_run, tid)
            if not existing or not _can_access_run(existing, principal):
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {tid}"))
            return _build_a2a_sse_response(
                req_id,
                existing,
                request,
                stream_start_seq=0,
                is_resubscribe=True,
            )

        if canonical == "tasks/get":
            raw_tid = params.get("id") or params.get("taskId")
            if not isinstance(raw_tid, str) or not raw_tid.strip():
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Missing or invalid task id")
                )
            raw_hl = params.get("historyLength")
            if raw_hl is not None:
                if isinstance(raw_hl, bool) or not isinstance(raw_hl, int) or raw_hl < 0:
                    return JSONResponse(
                        jsonrpc_error(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "historyLength must be a non-negative integer",
                        )
                    )
            tid = raw_tid.strip()
            run = await storage.run_async(storage.get_run, tid)
            if not run or not _can_access_run(run, principal):
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {tid}"))
            pending_props = await _get_pending_proposals(run)
            return JSONResponse(
                jsonrpc_success(
                    req_id,
                    run_to_a2a_task(
                        run,
                        history_length=raw_hl,
                        pending_proposals=pending_props,
                    ),
                )
            )

        if canonical == "tasks/list":
            raw_limit = (
                params.get("pageSize") if params.get("pageSize") is not None else params.get("limit")
            )
            if raw_limit is None:
                limit = 50
            else:
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
                    return JSONResponse(
                        jsonrpc_error(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "pageSize/limit must be a positive integer",
                        )
                    )
                limit = min(raw_limit, 100)

            raw_ctx = params.get("contextId")
            if raw_ctx is not None and not isinstance(raw_ctx, str):
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "contextId must be a string")
                )
            context_filter = raw_ctx.strip() if isinstance(raw_ctx, str) and raw_ctx.strip() else None

            raw_state = params.get("status") if params.get("status") is not None else params.get("state")
            if raw_state is not None and not isinstance(raw_state, str):
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "status/state must be a string")
                )
            state_filter = (
                raw_state.strip().lower() if isinstance(raw_state, str) and raw_state.strip() else None
            )

            raw_hl = params.get("historyLength")
            if raw_hl is not None:
                if isinstance(raw_hl, bool) or not isinstance(raw_hl, int) or raw_hl < 0:
                    return JSONResponse(
                        jsonrpc_error(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "historyLength must be a non-negative integer",
                        )
                    )

            filter_tenant = (
                None
                if (params.get("allTenants") and "admin" in principal.roles and principal.global_admin)
                else principal.tenant_id
            )

            def _list_a2a_runs_sync() -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
                matched: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
                if context_filter:
                    # 优先走 runs(a2a_context_id, tenant_id, created_at DESC) 索引实现 O(log N) 检索
                    ctx_limit = 500 if state_filter else limit
                    candidates = storage.list_runs_by_a2a_context(
                        context_filter,
                        tenant_id=filter_tenant,
                        limit=ctx_limit,
                    )
                    seen_ids = {str(r.get("id") or "") for r in candidates}
                    if context_filter not in seen_ids:
                        fallback_run = storage.get_run(context_filter)
                        if fallback_run is not None:
                            fb_tenant = str(
                                fallback_run.get("tenant_id")
                                or (fallback_run.get("metadata") or {}).get("tenant_id")
                                or "local"
                            )
                            fb_ctx = str(
                                (fallback_run.get("metadata") or {}).get("a2a_context_id")
                                or fallback_run["id"]
                            )
                            if fb_ctx == context_filter and (
                                filter_tenant is None or fb_tenant == filter_tenant
                            ):
                                candidates.append(fallback_run)
                    for full in candidates:
                        if not _can_access_run(full, principal):
                            continue
                        if state_filter:
                            item_st = str(full.get("status") or "").lower()
                            if state_filter not in {item_st, run_status_to_a2a_state(item_st)}:
                                continue
                        props: list[dict[str, Any]] = []
                        if full.get("status") == "waiting_approval":
                            all_p = storage.proposals(full["id"])
                            w_ids = set((full.get("metadata") or {}).get("waiting_proposal_ids") or [])
                            props = [
                                p
                                for p in all_p
                                if isinstance(p, dict)
                                and (p.get("status") == "pending" or p.get("id") in w_ids)
                            ]
                        matched.append((full, props))
                        if len(matched) >= limit:
                            break
                    return matched

                fetch_limit = 500 if state_filter else limit
                summaries = storage.list_runs(tenant_id=filter_tenant, limit=fetch_limit)
                for item in summaries:
                    if state_filter:
                        item_st = str(item.get("status") or "").lower()
                        if state_filter not in {item_st, run_status_to_a2a_state(item_st)}:
                            continue
                    full = storage.get_run(item["id"])
                    if full is None or not _can_access_run(full, principal):
                        continue
                    props = []
                    if full.get("status") == "waiting_approval":
                        all_p = storage.proposals(full["id"])
                        w_ids = set((full.get("metadata") or {}).get("waiting_proposal_ids") or [])
                        props = [
                            p
                            for p in all_p
                            if isinstance(p, dict) and (p.get("status") == "pending" or p.get("id") in w_ids)
                        ]
                    matched.append((full, props))
                    if len(matched) >= limit:
                        break
                return matched

            matched_runs = await storage.run_async(_list_a2a_runs_sync)
            tasks_out = [
                run_to_a2a_task(
                    r,
                    history_length=raw_hl,
                    include_artifacts=False,
                    pending_proposals=props,
                )
                for r, props in matched_runs
            ]
            return JSONResponse(jsonrpc_success(req_id, {"tasks": tasks_out}))

        if canonical == "tasks/cancel":
            require_roles(
                principal,
                {"operator", "approver", "admin"},
                "需要 operator、approver 或 admin 角色才能取消任务",
            )
            raw_tid = params.get("id") or params.get("taskId")
            if not isinstance(raw_tid, str) or not raw_tid.strip():
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Missing or invalid task id")
                )
            rid = raw_tid.strip()
            run = await storage.run_async(storage.get_run, rid)
            if not run or not _can_access_run(run, principal):
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {rid}"))
            if is_terminal_a2a_state(run_status_to_a2a_state(run.get("status"))):
                return JSONResponse(
                    jsonrpc_error(
                        req_id,
                        A2A_TASK_NOT_CANCELABLE,
                        "Task is already in a terminal state",
                    )
                )
            running_job = jobs.get(rid)
            active_rt = active_runtimes.get(rid)
            was_running_in_dispatch = running_job is not None and not running_job.done()
            updated = await storage.run_async(
                storage.request_cancel_run, rid, actor_id=principal.actor_id, reason="a2a_cancel"
            )
            if active_rt is not None:
                with contextlib.suppress(Exception):
                    updated = await storage.run_async(
                        active_rt.cancel_active_run,
                        rid,
                        actor_id=principal.actor_id,
                        reason="a2a_cancel",
                    )
            if running_job is not None and not running_job.done():
                running_job.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(running_job, timeout=1.0)
            if running_job is not None or active_rt is not None:
                refreshed = await storage.run_async(storage.get_run, rid)
                if refreshed is not None:
                    updated = refreshed
            if not was_running_in_dispatch and is_terminal_a2a_state(
                run_status_to_a2a_state(updated.get("status"))
            ):
                u_meta = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
                if rid in a2a_push_run_ids or u_meta.get("a2a_origin"):
                    _schedule_a2a_push_notification(rid)
            return JSONResponse(jsonrpc_success(req_id, run_to_a2a_task(updated)))

        if canonical == "tasks/pushNotificationConfig/set":
            require_roles(
                principal,
                {"operator", "admin"},
                "需要 operator 或 admin 角色才能配置 A2A 推送通知",
            )
            raw_tid = params.get("id") or params.get("taskId")
            if not isinstance(raw_tid, str) or not raw_tid.strip() or len(raw_tid.strip()) > 128:
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Missing or invalid task id")
                )
            tid = raw_tid.strip()

            if "pushNotificationConfig" in params:
                raw_cfg = params.get("pushNotificationConfig")
                if not isinstance(raw_cfg, dict):
                    return JSONResponse(
                        jsonrpc_error(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "pushNotificationConfig must be an object",
                        )
                    )
                cfg_obj = raw_cfg
            elif "config" in params:
                raw_cfg = params.get("config")
                if not isinstance(raw_cfg, dict):
                    return JSONResponse(
                        jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "config must be an object")
                    )
                cfg_obj = raw_cfg
            else:
                cfg_obj = params

            try:
                clean_url, clean_secret, events_list = _parse_a2a_push_config_obj(cfg_obj)
            except ValueError as exc:
                return JSONResponse(jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc)))

            run = await storage.run_async(storage.get_run, tid)
            if not run or not _can_access_run(run, principal):
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {tid}"))
            run_tenant = str(
                run.get("tenant_id") or (run.get("metadata") or {}).get("tenant_id") or "local"
            )
            try:
                saved_cfg = await storage.run_async(
                    storage.set_a2a_push_config,
                    tid,
                    run_tenant,
                    clean_url,
                    secret=clean_secret,
                    events=events_list,
                )
            except KeyError:
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {tid}"))
            except ValueError as exc:
                return JSONResponse(jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, str(exc)))

            _track_a2a_push_run_id(tid)
            await _audit_log(
                principal=principal,
                action="a2a_push_config_set",
                resource=f"a2a:task:{tid}",
                outcome="allowed",
                detail={
                    "task_id": tid,
                    "url": clean_url,
                    "has_secret": bool(saved_cfg.get("has_secret")),
                    "events": saved_cfg.get("events") or [],
                },
            )
            return JSONResponse(jsonrpc_success(req_id, _public_a2a_push_config(saved_cfg)))

        if canonical == "tasks/pushNotificationConfig/get":
            raw_tid = params.get("id") or params.get("taskId")
            if not isinstance(raw_tid, str) or not raw_tid.strip() or len(raw_tid.strip()) > 128:
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Missing or invalid task id")
                )
            tid = raw_tid.strip()
            run = await storage.run_async(storage.get_run, tid)
            if not run or not _can_access_run(run, principal):
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {tid}"))
            run_tenant = str(
                run.get("tenant_id") or (run.get("metadata") or {}).get("tenant_id") or "local"
            )
            cfg = await storage.run_async(storage.get_a2a_push_config, tid, tenant_id=run_tenant)
            if cfg is None:
                return JSONResponse(
                    jsonrpc_success(
                        req_id,
                        {"id": tid, "taskId": tid, "pushNotificationConfig": None},
                    )
                )
            return JSONResponse(jsonrpc_success(req_id, _public_a2a_push_config(cfg)))

        if canonical == "tasks/pushNotificationConfig/delete":
            require_roles(
                principal,
                {"operator", "admin"},
                "需要 operator 或 admin 角色才能删除 A2A 推送通知配置",
            )
            raw_tid = params.get("id") or params.get("taskId")
            if not isinstance(raw_tid, str) or not raw_tid.strip() or len(raw_tid.strip()) > 128:
                return JSONResponse(
                    jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Missing or invalid task id")
                )
            tid = raw_tid.strip()
            run = await storage.run_async(storage.get_run, tid)
            if not run or not _can_access_run(run, principal):
                return JSONResponse(jsonrpc_error(req_id, A2A_TASK_NOT_FOUND, f"Task not found: {tid}"))
            run_tenant = str(
                run.get("tenant_id") or (run.get("metadata") or {}).get("tenant_id") or "local"
            )
            deleted = await storage.run_async(
                storage.delete_a2a_push_config, tid, tenant_id=run_tenant
            )
            a2a_push_run_ids.pop(tid, None)
            await _audit_log(
                principal=principal,
                action="a2a_push_config_delete",
                resource=f"a2a:task:{tid}",
                outcome="allowed",
                detail={"task_id": tid, "deleted": bool(deleted)},
            )
            return JSONResponse(
                jsonrpc_success(req_id, {"id": tid, "taskId": tid, "deleted": bool(deleted)})
            )

        return JSONResponse(
            jsonrpc_error(req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
        )

    @app.get("/api/a2a/peers")
    async def get_a2a_peers(request: Request):
        """返回声明式 A2A 对端配置脱敏视图（严禁泄露内联 api_key 或敏感请求头）。"""
        principal = get_principal(request)
        is_a2a_admin = (
            (principal.global_admin and "admin" in principal.roles)
            if auth_configured
            else ("admin" in principal.roles)
        )

        def _load_a2a_sync() -> dict[str, Any]:
            with a2a_lock:
                raw_doc = load_a2a_config(state_dir=state)
                return redact_a2a_document(raw_doc)

        try:
            redacted_doc = await asyncio.to_thread(_load_a2a_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        if is_a2a_admin:
            config_path_out = redacted_doc.get("config_path")
        else:
            config_path_out = None
            redacted_doc.pop("config_path", None)

        return {
            "ok": True,
            "config_path": config_path_out,
            "config": redacted_doc,
            "self_card_url": "/.well-known/agent-card.json",
        }

    @app.put("/api/a2a/peers")
    async def put_a2a_peers(request: Request):
        """原子校验并保存 <state_dir>/a2a.json（严格限全局/本机 admin，自动保留脱敏占位符对应密钥并记审计日志）。"""
        principal = get_principal(request)
        _require_global_admin(principal, "需要全局 admin 角色才能修改 A2A 对端配置")
        try:
            raw_payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "非法的 JSON 请求体") from exc
        if not isinstance(raw_payload, dict):
            raise HTTPException(400, "a2a 配置根对象必须为 JSON 对象")
        doc_in = (
            raw_payload["config"]
            if ("peers" not in raw_payload and isinstance(raw_payload.get("config"), dict))
            else raw_payload
        )

        def _save_a2a_sync() -> dict[str, Any]:
            with a2a_lock:
                target_path = _a2a_config_target_path()
                existing_doc: dict[str, Any] | None = None
                if target_path.is_symlink() or target_path.exists():
                    try:
                        existing_doc = load_a2a_config(explicit_path=target_path)
                    except ValueError as exc:
                        err_s = str(exc)
                        if any(
                            tok in err_s
                            for tok in ("符号链接", "硬链接", "O_NOFOLLOW", "常规文件", "不安全")
                        ):
                            raise
                        existing_doc = None
                else:
                    discovered = discover_a2a_config_path(state_dir=state)
                    if discovered is not None:
                        with contextlib.suppress(ValueError):
                            existing_doc = load_a2a_config(explicit_path=discovered)
                merged_doc = _merge_preserved_a2a_secrets(doc_in, existing_doc)
                validated = validate_a2a_document(merged_doc)
                saved_doc = save_a2a_config(target_path, validated)
                return redact_a2a_document(saved_doc)

        try:
            redacted_doc = await asyncio.to_thread(_save_a2a_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        await _audit_log(
            principal=principal,
            action="a2a_peers_config_update",
            resource="/api/a2a/peers",
            outcome="allowed",
            detail={
                "config_path": redacted_doc.get("config_path"),
                "peer_names": sorted((redacted_doc.get("peers") or {}).keys()),
                "peer_count": len(redacted_doc.get("peers") or {}),
            },
        )
        return {
            "ok": True,
            "config_path": redacted_doc.get("config_path"),
            "config": redacted_doc,
            "self_card_url": "/.well-known/agent-card.json",
        }

    @app.post("/api/a2a/discover")
    async def discover_a2a_peer(body: A2ADiscoverInput, request: Request):
        """发现并解析远端 A2A 智能体 AgentCard（受 SSRF 与云元数据防护约束）。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能发现远端 A2A 智能体")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")

        clean_peer = body.peer.strip() if isinstance(body.peer, str) and body.peer.strip() else None
        clean_url = body.url.strip() if isinstance(body.url, str) and body.url.strip() else None
        if not clean_peer and not clean_url:
            raise HTTPException(400, "必须提供 peer 或 url")

        def _load_doc_sync() -> dict[str, Any]:
            with a2a_lock:
                return load_a2a_config(state_dir=state)

        try:
            doc = await asyncio.to_thread(_load_doc_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        if clean_peer is not None:
            raw_peers = doc.get("peers") or {}
            if clean_peer in raw_peers and not bool(raw_peers[clean_peer].get("enabled", True)):
                raise HTTPException(400, f"A2A peer {clean_peer!r} 未启用")

        target = clean_peer if clean_peer is not None else clean_url
        assert target is not None
        mgr = A2AClientManager(doc, transport=getattr(app.state, "a2a_transport", None))
        try:
            return await mgr.discover_agent_card(
                target,
                allow_private_network=bool(body.allow_private_network),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"远端 A2A AgentCard 拉取失败：{type(exc).__name__}") from exc
        finally:
            await mgr.aclose()

    @app.post("/api/a2a/send")
    async def send_a2a_peer_message(body: A2ASendInput, request: Request):
        """按 A2A 协议向已配置的远端协同智能体委托任务并记录持久化审计日志。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能向远端 A2A 智能体发送任务")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")

        peer_name = body.peer.strip()

        def _load_doc_sync() -> dict[str, Any]:
            with a2a_lock:
                return load_a2a_config(state_dir=state)

        try:
            doc = await asyncio.to_thread(_load_doc_sync)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        mgr = A2AClientManager(doc, transport=getattr(app.state, "a2a_transport", None))
        try:
            result = await mgr.send_message(
                peer_name,
                body.prompt,
                task_id=body.task_id,
                context_id=body.context_id,
                blocking=bool(body.blocking),
                data=body.data,
            )
        except ValueError as exc:
            await _audit_log(
                principal=principal,
                action="a2a_send_message",
                resource=f"a2a:{peer_name}",
                outcome="denied",
                detail={"peer": peer_name, "error": str(exc)[:200]},
            )
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            await _audit_log(
                principal=principal,
                action="a2a_send_message",
                resource=f"a2a:{peer_name}",
                outcome="error",
                detail={"peer": peer_name, "error": type(exc).__name__},
            )
            raise HTTPException(502, f"远端 A2A 任务发送失败：{type(exc).__name__}") from exc
        finally:
            await mgr.aclose()

        await _audit_log(
            principal=principal,
            action="a2a_send_message",
            resource=f"a2a:{peer_name}",
            outcome="allowed" if result.get("ok") else "failed",
            detail={
                "peer": peer_name,
                "task_id": result.get("task_id") or body.task_id,
                "context_id": result.get("context_id") or body.context_id,
                "blocking": bool(body.blocking),
                "state": result.get("state"),
                "ok": bool(result.get("ok")),
                "code": result.get("code"),
            },
        )
        return result

    # -----------------------------------------------------------------------
    # Horizon 超长程任务编排与机械化架构边界 Lint API（受直连工作区权限门控）
    # -----------------------------------------------------------------------

    def _resolve_horizon_workspace(
        principal: Principal,
        workspace_path: str | None,
    ) -> Path:
        if not _can_use_direct_workspace(principal):
            raise HTTPException(403, "仅限本机或管理员角色访问 Horizon 工作区编排接口")
        raw_ws = (
            workspace_path.strip()
            if isinstance(workspace_path, str) and workspace_path.strip()
            else str(allowed_ws_roots[0] if allowed_ws_roots else ROOT)
        )
        try:
            ws_dir = validate_direct_workspace_path(raw_ws, allowed_roots=allowed_ws_roots)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if any(seg in _FORBIDDEN_DOT_PARTS for seg in ws_dir.parts):
            raise HTTPException(400, "禁止访问敏感隐藏目录")
        return ws_dir

    @app.get("/api/horizons")
    async def list_horizons(
        request: Request,
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """列出指定直连工作区下的全部 Horizon Campaign 及其通过进度摘要。"""
        principal = get_principal(request)
        ws_dir = _resolve_horizon_workspace(principal, workspace_path)

        def _list_sync() -> list[dict[str, Any]]:
            return HorizonCampaignManager(ws_dir).list_campaigns()

        try:
            campaigns = await asyncio.to_thread(_list_sync)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "workspace_path": str(ws_dir),
            "count": len(campaigns),
            "campaigns": campaigns,
        }

    @app.post("/api/horizons")
    async def create_horizon(
        body: HorizonCreateInput,
        request: Request,
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """在直连工作区初始化新的 Horizon Campaign（生成不可变基线账本、init.sh 与 progress.md）并记审计日志。"""
        principal = get_principal(request)
        raw_ws = (
            body.workspace_path
            if (isinstance(body.workspace_path, str) and body.workspace_path.strip())
            else workspace_path
        )
        ws_dir = _resolve_horizon_workspace(principal, raw_ws)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能创建 Horizon Campaign")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")

        feature_dicts = [feat.model_dump(exclude_none=True) for feat in body.features]
        smoke_cmd = body.smoke_cmd if body.smoke_cmd is not None else "pytest -q"

        def _init_sync() -> dict[str, Any]:
            mgr = HorizonCampaignManager(ws_dir)
            created = mgr.init_campaign(
                body.goal,
                feature_dicts,
                smoke_cmd=smoke_cmd,
                campaign_id=body.campaign_id,
            )
            return mgr.get_campaign(created["campaign_id"])

        try:
            doc = await asyncio.to_thread(_init_sync)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

        await _audit_log(
            principal=principal,
            action="horizon_campaign_create",
            resource=f"horizon:{doc['campaign_id']}",
            outcome="allowed",
            detail={
                "campaign_id": doc["campaign_id"],
                "workspace_path": str(ws_dir),
                "features_count": len(doc.get("features") or []),
                "smoke_cmd": doc.get("smoke_cmd") or "",
            },
        )
        return {
            "ok": True,
            "workspace_path": str(ws_dir),
            **doc,
        }

    @app.post("/api/horizons/lint")
    async def lint_horizon_invariants(
        request: Request,
        workspace_path: str | None = Query(default=None, max_length=512),
        body: HorizonLintInput | None = Body(default=None),
    ):
        """运行确定性机械架构边界检查器，返回结构化违规诊断与模型可执行修复指引（remediation）。"""
        principal = get_principal(request)
        raw_ws = (
            workspace_path
            if (isinstance(workspace_path, str) and workspace_path.strip())
            else (body.workspace_path if body is not None else None)
        )
        ws_dir = _resolve_horizon_workspace(principal, raw_ws)

        def _lint_sync() -> dict[str, Any]:
            checker = MechanicalInvariantChecker.from_workspace(ws_dir)
            return checker.check(ws_dir)

        try:
            report = await asyncio.to_thread(_lint_sync)
        except (ValueError, re.error, OSError) as exc:
            raise HTTPException(400, f"架构边界检查配置无效：{exc}") from exc
        return {
            "workspace_path": str(ws_dir),
            **report,
        }

    @app.get("/api/horizons/{campaign_id}")
    async def get_horizon(
        campaign_id: str,
        request: Request,
        workspace_path: str | None = Query(default=None, max_length=512),
    ):
        """读取指定 Horizon Campaign 的完整特征账本、交接日志与进度摘要（不存在返回 404）。"""
        principal = get_principal(request)
        ws_dir = _resolve_horizon_workspace(principal, workspace_path)

        def _get_sync() -> dict[str, Any]:
            return HorizonCampaignManager(ws_dir).get_campaign(campaign_id)

        try:
            doc = await asyncio.to_thread(_get_sync)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "workspace_path": str(ws_dir),
            **doc,
        }

    # -----------------------------------------------------------------------
    # 自主 Cron/Webhook 触发器与轨迹评测回归飞轮 API
    # -----------------------------------------------------------------------

    def _update_trigger_fire_record(
        fire_id: int,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> None:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if not clauses:
            return
        params.append(fire_id)
        with storage._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                f"UPDATE trigger_fires SET {', '.join(clauses)} WHERE id=?",
                tuple(params),
            )

    def _update_trigger_fire_by_dedup_key(
        dedup_key: str,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> None:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if not clauses:
            return
        params.append(dedup_key)
        with storage._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                f"UPDATE trigger_fires SET {', '.join(clauses)} WHERE dedup_key=?",
                tuple(params),
            )

    async def _create_trigger_run(
        trigger: dict[str, Any],
        prompt_text: str,
        principal: Principal,
        *,
        extra_trigger_meta: dict[str, Any],
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        trig_meta = trigger.get("metadata") if isinstance(trigger.get("metadata"), dict) else {}
        req_mode = str(trig_meta.get("mode") or "agent").strip()
        eff_mode: Literal["agent", "workflow", "parallel"] = (
            req_mode if req_mode in {"agent", "workflow", "parallel"} else "agent"
        )
        raw_ws = trigger.get("workspace_path")
        clean_ws = str(raw_ws).strip() if isinstance(raw_ws, str) and raw_ws.strip() else None
        req_tpl = str(trig_meta.get("template") or ("local_cwd" if clean_ws else "sample")).strip()
        if clean_ws:
            eff_tpl: Literal["sample", "multi_file", "local_cwd"] = "local_cwd"
        elif req_tpl in {"sample", "multi_file"}:
            eff_tpl = req_tpl
        else:
            eff_tpl = "sample"

        task_principal = principal
        if clean_ws is not None:
            can_direct = (
                (not auth_configured)
                or bool(trig_meta.get("direct_workspace_authorized"))
                or principal.tenant_id == "local"
            )
            if not can_direct:
                raise HTTPException(403, "触发器未获授权访问宿主机本地工作区")
            try:
                validate_direct_workspace_path(clean_ws, allowed_roots=allowed_ws_roots)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if auth_configured and not _can_use_direct_workspace(principal):
                task_principal = Principal(
                    principal.tenant_id,
                    principal.actor_id,
                    frozenset({"operator", "admin"}),
                    global_admin=bool(trig_meta.get("direct_workspace_authorized")),
                )

        bounded_prompt = prompt_text[:8000] if len(prompt_text) > 8000 else prompt_text
        task_body = TaskInput(
            prompt=bounded_prompt,
            mode=eff_mode,
            template=eff_tpl,
            workspace_path=clean_ws,
            execution_profile="host" if clean_ws else "sandbox",
        )
        traceparent_str = format_traceparent(get_trace_id())
        return await asyncio.to_thread(
            _prepare_workspace_and_run,
            task_body,
            task_principal,
            traceparent_str,
            extra_metadata=extra_trigger_meta,
        )

    async def _dispatch_cron_trigger(trigger: dict[str, Any], dedup_key: str) -> dict[str, Any]:
        trigger_id = str(trigger.get("id") or "")
        trigger_tenant = str(trigger.get("tenant_id") or "local")
        cron_principal = Principal(
            trigger_tenant,
            f"cron:{trigger_id}",
            frozenset({"operator"}),
            global_admin=False,
        )
        prompt_text = str(trigger.get("prompt_template") or "").strip()
        runtime, run = await _create_trigger_run(
            trigger,
            prompt_text,
            cron_principal,
            extra_trigger_meta={
                "trigger_id": trigger_id,
                "trigger_kind": "cron",
                "trigger_dedup_key": dedup_key,
            },
        )
        with contextlib.suppress(Exception):
            await storage.run_async(
                _update_trigger_fire_by_dedup_key,
                dedup_key,
                run_id=str(run["id"]),
            )
        if dispatch_mode == "queue":
            await runtime.aclose()
            await storage.run_async(storage.enqueue_run, run["id"], action="execute")
        else:
            dispatch(run["id"], lambda: runtime.execute(run["id"]), runtime)
        return {"run_id": run["id"], "status": run.get("status")}

    app.state.trigger_scheduler = TriggerScheduler(storage, _dispatch_cron_trigger)

    @app.get("/api/triggers")
    async def list_triggers(
        request: Request,
        enabled_only: bool = False,
        all_tenants: bool = False,
    ):
        """列出租户作用域内的触发器定义（始终将已配置的 webhook_secret 掩码为 ***REDACTED***）。"""
        principal = get_principal(request)
        filter_tenant = (
            None
            if (all_tenants and "admin" in principal.roles and principal.global_admin)
            else principal.tenant_id
        )
        items = await storage.run_async(
            storage.list_triggers,
            tenant_id=filter_tenant,
            enabled_only=enabled_only,
        )
        masked = [_mask_trigger_public(item) for item in items]
        return {
            "ok": True,
            "count": len(masked),
            "triggers": masked,
        }

    @app.post("/api/triggers")
    async def create_trigger(body: TriggerCreateInput, request: Request):
        """创建租户作用域的 Cron 或 Webhook 触发器（校验 Cron 表达式、写入安全审计日志并返回脱敏视图）。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能创建触发器")
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")

        raw_cron = body.cron_expr if body.cron_expr is not None else body.cron
        clean_cron: str | None = None
        if body.kind == "cron":
            if not isinstance(raw_cron, str) or not raw_cron.strip():
                raise HTTPException(400, "Cron 触发器必须提供有效的 cron_expr")
            try:
                CronSchedule.parse(raw_cron)
            except ValueError as exc:
                raise HTTPException(400, f"非法的 Cron 表达式：{exc}") from exc
            clean_cron = raw_cron.strip()

        raw_prompt = body.prompt_template if body.prompt_template is not None else body.prompt
        if raw_prompt is not None:
            clean_prompt = raw_prompt.strip()
            if not clean_prompt:
                raise HTTPException(400, "prompt_template 不能为空")
        elif body.kind == "webhook":
            clean_prompt = "Handle incoming webhook event '{event}' for {repo}.\n\n{context}"
        else:
            raise HTTPException(400, "Cron 触发器必须提供 prompt_template")

        clean_secret: str | None = None
        if body.webhook_secret is not None:
            stripped_secret = body.webhook_secret.strip()
            if stripped_secret == REDACTED_SECRET_MARKER:
                raise HTTPException(400, "webhook_secret 不能为脱敏占位符")
            clean_secret = stripped_secret or None

        clean_ws: str | None = None
        if body.workspace_path is not None and body.workspace_path.strip():
            if not _can_use_direct_workspace(principal):
                raise HTTPException(403, "仅限本机或管理员角色为触发器绑定本地工作区路径")
            try:
                ws_dir = validate_direct_workspace_path(
                    body.workspace_path.strip(), allowed_roots=allowed_ws_roots
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if any(seg in _FORBIDDEN_DOT_PARTS for seg in ws_dir.parts):
                raise HTTPException(400, "禁止绑定敏感隐藏目录")
            clean_ws = str(ws_dir)

        meta: dict[str, Any] = dict(body.metadata) if isinstance(body.metadata, dict) else {}
        meta.pop("direct_workspace_authorized", None)
        if body.timezone is not None and body.timezone.strip():
            tz_str = body.timezone.strip()
            try:
                ZoneInfo(tz_str)
            except ZoneInfoNotFoundError as exc:
                raise HTTPException(400, f"非法的时区名称：{tz_str}") from exc
            meta["timezone"] = tz_str
        if body.mode is not None:
            meta["mode"] = body.mode
        if body.template is not None:
            if body.template == "local_cwd" and not _can_use_direct_workspace(principal):
                raise HTTPException(403, "仅限本机或管理员角色使用 local_cwd 模板")
            meta["template"] = body.template
        if clean_ws is not None and _can_use_direct_workspace(principal):
            meta["direct_workspace_authorized"] = True
        meta["created_by"] = principal.actor_id

        try:
            created = await storage.run_async(
                storage.create_trigger,
                body.name,
                body.kind,
                clean_prompt,
                tenant_id=principal.tenant_id,
                cron_expr=clean_cron,
                webhook_secret=clean_secret,
                workspace_path=clean_ws,
                enabled=body.enabled,
                metadata=meta,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        masked = _mask_trigger_public(created)
        await _audit_log(
            principal=principal,
            action="trigger_create",
            resource=f"trigger:{created['id']}",
            outcome="allowed",
            detail={
                "trigger_id": created["id"],
                "name": created["name"],
                "kind": created["kind"],
                "cron_expr": created["cron_expr"],
                "has_webhook_secret": bool(created["has_webhook_secret"]),
                "enabled": bool(created["enabled"]),
            },
        )
        return {"ok": True, **masked, "trigger": masked}

    @app.delete("/api/triggers/{trigger_id}")
    async def delete_trigger(trigger_id: str, request: Request):
        """原子删除租户名下触发器及其历史触发记录并记录安全审计日志（不存在返回 404）。"""
        principal = get_principal(request)
        require_roles(principal, {"operator", "admin"}, "需要 operator 或 admin 角色才能删除触发器")
        eff_tenant = (
            None if (principal.global_admin and "admin" in principal.roles) else principal.tenant_id
        )
        deleted = await storage.run_async(
            storage.delete_trigger, trigger_id, tenant_id=eff_tenant
        )
        if not deleted:
            raise HTTPException(404, "触发器不存在")
        await _audit_log(
            principal=principal,
            action="trigger_delete",
            resource=f"trigger:{trigger_id}",
            outcome="allowed",
            detail={"trigger_id": trigger_id, "deleted": True},
        )
        return {"ok": True, "id": trigger_id, "trigger_id": trigger_id, "deleted": True}

    @app.get("/api/triggers/{trigger_id}/fires")
    async def list_trigger_fires(
        trigger_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=1000),
    ):
        """列出租户名下指定触发器的近期触发派发历史记录（跨租户或不存在返回 404）。"""
        principal = get_principal(request)
        eff_tenant = (
            None if (principal.global_admin and "admin" in principal.roles) else principal.tenant_id
        )
        trig = await storage.run_async(storage.get_trigger, trigger_id, tenant_id=eff_tenant)
        if trig is None:
            raise HTTPException(404, "触发器不存在")
        fires = await storage.run_async(storage.list_trigger_fires, trigger_id, limit=limit)
        return {
            "ok": True,
            "trigger_id": trigger_id,
            "count": len(fires),
            "fires": fires,
        }

    @app.post("/api/webhooks/{trigger_id}", status_code=202)
    async def receive_webhook_trigger(trigger_id: str, request: Request):
        """接收经 HMAC-SHA256 常量时间验签的外部 Webhook 回调，执行幂等去重、安全模板渲染与任务派发。"""
        if not getattr(request.state, "webhook_verified", False):
            raise HTTPException(401, "未通过 Webhook 签名校验")
        trigger = getattr(request.state, "webhook_trigger", None)
        if not isinstance(trigger, dict) or str(trigger.get("id") or "") != trigger_id:
            raise HTTPException(404, "触发器不存在")

        principal = get_principal(request)
        if not rate_limiter.allow(principal.tenant_id):
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "rate_limited"})
            raise HTTPException(429, "当前租户请求频率超过限制")

        raw_body = getattr(request.state, "webhook_raw_body", None)
        if not isinstance(raw_body, (bytes, bytearray)):
            raw_body = await request.body()
        if raw_body:
            try:
                payload = json.loads(bytes(raw_body).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(400, "Webhook 请求体必须是合法的 JSON 对象") from exc
            if not isinstance(payload, dict):
                raise HTTPException(400, "Webhook 请求体必须是 JSON 对象")
        else:
            payload = {}

        event_name = (
            (request.headers.get("x-github-event") or "").strip()
            or (request.headers.get("x-agent-lab-event") or "").strip()
            or str(payload.get("event") or "webhook").strip()
            or "webhook"
        )
        rendered_prompt, base_dedup_key = render_webhook_prompt(
            event_name,
            payload,
            prompt_template=str(trigger.get("prompt_template") or ""),
        )
        dedup_key = _scoped_webhook_dedup_key(trigger_id, base_dedup_key)

        fire_rec = await storage.run_async(
            storage.record_trigger_fire,
            trigger_id,
            dedup_key,
            status="dispatched",
        )
        if fire_rec is None:
            METRICS.inc("agent_lab_triggers_fired_total", {"kind": "webhook", "status": "deduplicated"})
            return JSONResponse(
                {
                    "ok": True,
                    "deduplicated": True,
                    "trigger_id": trigger_id,
                    "dedup_key": dedup_key,
                },
                status_code=200,
            )

        if dispatch_mode == "inline" and sum(not job.done() for job in jobs.values()) >= max_inline_jobs:
            await storage.run_async(
                _update_trigger_fire_record, int(fire_rec["id"]), status="rejected_capacity"
            )
            METRICS.inc("agent_lab_runs_rejected_total", {"reason": "inline_capacity"})
            raise HTTPException(429, f"本机最多同时运行 {max_inline_jobs} 个并发 Agent 任务")
        if dispatch_mode == "queue":
            depth = await asyncio.to_thread(_count_active_queue_depth)
            if depth >= max_queue_depth:
                await storage.run_async(
                    _update_trigger_fire_record, int(fire_rec["id"]), status="rejected_capacity"
                )
                METRICS.inc("agent_lab_runs_rejected_total", {"reason": "queue_depth"})
                raise HTTPException(429, "持久队列待处理任务已达上限")

        try:
            runtime, run = await _create_trigger_run(
                trigger,
                rendered_prompt,
                principal,
                extra_trigger_meta={
                    "trigger_id": trigger_id,
                    "trigger_kind": "webhook",
                    "trigger_event": event_name,
                    "trigger_dedup_key": dedup_key,
                },
            )
        except Exception:
            await storage.run_async(
                _update_trigger_fire_record, int(fire_rec["id"]), status="failed"
            )
            METRICS.inc("agent_lab_triggers_fired_total", {"kind": "webhook", "status": "error"})
            raise

        await storage.run_async(
            _update_trigger_fire_record,
            int(fire_rec["id"]),
            run_id=str(run["id"]),
            status="dispatched",
        )
        if dispatch_mode == "queue":
            await runtime.aclose()
            await storage.run_async(storage.enqueue_run, run["id"], action="execute")
        else:
            dispatch(run["id"], lambda: runtime.execute(run["id"]), runtime)

        METRICS.inc("agent_lab_triggers_fired_total", {"kind": "webhook", "status": "ok"})
        await _audit_log(
            principal=principal,
            action="webhook_trigger_fire",
            resource=f"trigger:{trigger_id}",
            outcome="allowed",
            detail={
                "trigger_id": trigger_id,
                "run_id": run["id"],
                "event": event_name,
                "dedup_key": dedup_key,
            },
        )
        return JSONResponse(
            {
                "ok": True,
                "run_id": run["id"],
                "trigger_id": trigger_id,
                "dedup_key": dedup_key,
                "status": run.get("status"),
            },
            status_code=202,
        )

    @app.post("/api/evals/harvest")
    async def harvest_eval_traces(
        request: Request,
        body: EvalHarvestInput | None = Body(default=None),
        limit: int | None = Query(default=None, ge=1, le=200),
        min_friction: int | None = Query(default=None, ge=0, le=50),
    ):
        """采集近期失败/高摩擦轨迹，经双重脱敏匿名化落盘回归集，返回脱敏汇总并写入安全审计日志。"""
        principal = get_principal(request)
        require_roles(
            principal,
            {"operator", "admin"},
            "需要 operator 或 admin 角色才能采集失败轨迹回归用例",
        )
        if not rate_limiter.allow(principal.tenant_id):
            raise HTTPException(429, "当前租户请求频率超过限制")

        eff_limit = (
            body.limit
            if body is not None
            else (limit if limit is not None else 20)
        )
        eff_min_friction = (
            body.min_friction
            if body is not None
            else (min_friction if min_friction is not None else 1)
        )
        is_global_or_local_admin = (not auth_configured) or (
            principal.global_admin and "admin" in principal.roles
        )
        harvest_storage: Any = (
            storage
            if is_global_or_local_admin
            else _TenantScopedStorageProxy(storage, principal.tenant_id)
        )
        target_dir = (
            state / "harvested_evals"
            if (is_global_or_local_admin or principal.tenant_id == "local")
            else (state / "harvested_evals" / _tenant_upload_slug(principal.tenant_id))
        )

        try:
            report = await asyncio.to_thread(
                harvest_failure_traces,
                harvest_storage,
                target_dir,
                limit=eff_limit,
                min_friction=eff_min_friction,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

        safe_cases: list[dict[str, Any]] = []
        for entry in report.get("cases") or []:
            if not isinstance(entry, dict):
                continue
            item: dict[str, Any] = {
                "case_id": entry.get("case_id"),
                "file": entry.get("file"),
                "original_status": entry.get("original_status"),
                "overall_score": entry.get("overall_score"),
                "friction_signals": list(entry.get("friction_signals") or []),
                "baseline_grade": entry.get("baseline_grade"),
            }
            if is_global_or_local_admin and entry.get("path"):
                item["path"] = entry["path"]
            safe_cases.append(redact_secrets(item))

        summary: dict[str, Any] = {
            "ok": True,
            "scanned": int(report.get("scanned") or 0),
            "harvested": int(report.get("harvested") or 0),
            "manifest_path": report.get("manifest_path") if is_global_or_local_admin else None,
            "cases": safe_cases,
        }
        await _audit_log(
            principal=principal,
            action="eval_harvest",
            resource="/api/evals/harvest",
            outcome="allowed",
            detail={
                "limit": eff_limit,
                "min_friction": eff_min_friction,
                "scanned": summary["scanned"],
                "harvested": summary["harvested"],
                "case_ids": [c["case_id"] for c in safe_cases[:50]],
            },
        )
        return summary

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.get("/favicon.ico")
    async def favicon():
        return Response(
            content=_FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/")
    async def index():
        return FileResponse(
            ROOT / "web/index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    return app

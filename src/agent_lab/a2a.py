"""A2A（Agent2Agent）开放协议双向适配层：服务端（AgentCard / JSON-RPC 2.0 / SSE）与客户端（远端发现与委托）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 协议兼容性（Dual-Version Spec Compliance）：
   本模块同时兼容 A2A v0.2/v0.3 与 A2A v1.0（latest）规范：
   - 发现端点：`/.well-known/agent-card.json`、`/.well-known/agent.json`、`/extendedAgentCard`；
   - JSON-RPC 2.0 方法映射：
     * `message/send` / `SendMessage` / `tasks/send`
     * `message/stream` / `SendStreamingMessage` / `tasks/sendSubscribe`
     * `tasks/get` / `GetTask`
     * `tasks/list` / `ListTasks`
     * `tasks/cancel` / `CancelTask`
     * `tasks/resubscribe` / `SubscribeToTask`
   - 标准错误码：`-32700..-32603`（JSON-RPC 2.0）及 `-32001 TaskNotFoundError`、
     `-32002 TaskNotCancelableError`、`-32003 PushNotificationNotSupportedError`、
     `-32004 UnsupportedOperationError`。
2. 安全边界（Security Invariants）：
   - 配置文件 `<state_dir>/a2a.json` 采用 `O_NOFOLLOW`、`st_nlink == 1`、`0600` 原子替换；
   - 严格拦截云元数据 IMDS（`169.254.169.254` / `metadata.google.internal`）、URL 内嵌凭据、CRLF 请求头注入；
   - 默认禁止访问回环/私网地址，仅在 peer 显式声明 `allow_private_network=True` 或
     `AGENT_LAB_A2A_ALLOW_PRIVATE=1` 时放行本地/内网对端 Agent；
   - `redact_a2a_document` 严禁向 API/Web/CLI 暴露内联 `api_key` 或敏感请求头明文。
================================================================================
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
from typing import Any
import urllib.parse
import uuid

import httpx

from .observability import format_traceparent, redact_secrets

A2A_PROTOCOL_VERSION = "0.3.0"
A2A_SCHEMA_VERSION = 1
A2A_REDACTED_MARKER = "***REDACTED***"

# JSON-RPC 2.0 & A2A 标准错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
A2A_TASK_NOT_FOUND = -32001
A2A_TASK_NOT_CANCELABLE = -32002
A2A_PUSH_NOT_SUPPORTED = -32003
A2A_UNSUPPORTED_OPERATION = -32004

_METHOD_ALIASES: dict[str, str] = {
    "message/send": "message/send",
    "sendmessage": "message/send",
    "tasks/send": "message/send",
    "message/stream": "message/stream",
    "sendstreamingmessage": "message/stream",
    "tasks/sendsubscribe": "message/stream",
    "tasks/get": "tasks/get",
    "gettask": "tasks/get",
    "tasks/list": "tasks/list",
    "listtasks": "tasks/list",
    "tasks/cancel": "tasks/cancel",
    "canceltask": "tasks/cancel",
    "tasks/resubscribe": "tasks/resubscribe",
    "subscribetotask": "tasks/resubscribe",
    "tasks/pushnotificationconfig/set": "tasks/pushNotificationConfig/set",
    "settaskpushnotificationconfig": "tasks/pushNotificationConfig/set",
    "tasks/pushnotificationconfig/get": "tasks/pushNotificationConfig/get",
    "gettaskpushnotificationconfig": "tasks/pushNotificationConfig/get",
    "tasks/pushnotificationconfig/delete": "tasks/pushNotificationConfig/delete",
    "deletetaskpushnotificationconfig": "tasks/pushNotificationConfig/delete",
}

_PASCAL_FALLBACK_METHODS: dict[str, str] = {
    "message/send": "SendMessage",
    "message/stream": "SendStreamingMessage",
    "tasks/get": "GetTask",
    "tasks/list": "ListTasks",
    "tasks/cancel": "CancelTask",
    "tasks/resubscribe": "SubscribeToTask",
    "tasks/pushNotificationConfig/set": "SetTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/get": "GetTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/delete": "DeleteTaskPushNotificationConfig",
}

_PEER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_ENV_INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE_HEADER_RE = re.compile(r"(?:key|token|secret|authorization|password|credential)", re.I)
_BEARER_VALUE_RE = re.compile(r"(?i)\b(Bearer\s+)[^\s,;\"']+")
_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "metadata.azure.internal",
        "metadata.aws.internal",
    }
)
_DARWIN_SYSTEM_SYMLINKS = frozenset({"/tmp", "/var", "/etc"})
_MAX_A2A_FILE_BYTES = 256 * 1024
_MAX_A2A_CARD_BYTES = 256 * 1024
_MAX_A2A_ATTACHMENT_BYTES = 6 * 1024 * 1024
_MAX_PEERS = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_a2a_method(method: Any) -> str | None:
    """将 A2A v0.2/v0.3 斜杠方法名与 v1.0 PascalCase 方法名统一归一化为规范内部方法名。"""
    if not isinstance(method, str) or not method.strip():
        return None
    return _METHOD_ALIASES.get(method.strip().lower())


def jsonrpc_success(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def run_status_to_a2a_state(status: str | None) -> str:
    """将内部 `AgentRuntime` 状态映射为标准 A2A TaskState。"""
    s = str(status or "").strip().lower()
    mapping = {
        "queued": "submitted",
        "running": "working",
        "waiting_approval": "input-required",
        "completed": "completed",
        "cancelled": "canceled",
        "interrupted": "canceled",
        "failed": "failed",
        "budget_exceeded": "failed",
        "dead_letter": "failed",
    }
    return mapping.get(s, "unknown")


def is_terminal_a2a_state(state: str | None) -> bool:
    return str(state or "").strip().lower() in {"completed", "canceled", "failed", "rejected"}


def build_agent_card(
    *,
    base_url: str,
    skills: Sequence[Mapping[str, Any]] = (),
    auth_enabled: bool = False,
    gateway_protocol: str = "demo",
    extended: bool = False,
    name: str = "Agent Systems Lab",
    version: str = "0.1.0",
) -> dict[str, Any]:
    """构造同时兼容 A2A v0.2/v0.3 与 v1.0 规范的 AgentCard JSON 文档。"""
    clean_base = base_url.strip().rstrip("/") or "http://127.0.0.1:8787"
    rpc_url = f"{clean_base}/a2a"

    card_skills: list[dict[str, Any]] = [
        {
            "id": "code-engineering",
            "name": "Code Engineering & Debugging",
            "description": "Inspect repositories, analyze bugs, apply verified code patches, and run tests.",
            "tags": ["coding", "debugging", "testing", "git"],
            "examples": ["Fix the discount calculation bug and verify tests pass."],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["text/plain", "text/markdown", "application/json"],
        },
        {
            "id": "architecture-research",
            "name": "Codebase Research & Review",
            "description": "Multi-agent parallel code inspection, AST RepoMap analysis, and security/architecture audits.",
            "tags": ["research", "review", "architecture"],
            "examples": ["Review authentication boundaries and summarize module responsibilities."],
            "inputModes": ["text/plain"],
            "outputModes": ["text/markdown", "text/plain"],
        },
    ]
    seen_ids = {s["id"] for s in card_skills}
    for sk in skills:
        sname = str(sk.get("name") or "").strip()
        if not sname or sname in seen_ids:
            continue
        seen_ids.add(sname)
        card_skills.append(
            {
                "id": sname,
                "name": sname,
                "description": str(sk.get("description") or f"Loaded skill: {sname}")[:240],
                "tags": list(sk.get("triggers") or [])[:6] or ["skill"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/markdown", "text/plain"],
            }
        )

    card: dict[str, Any] = {
        "name": name,
        "description": (
            "Recoverable, KV-cache-aligned coding & research agent supporting MCP, "
            "autonomous subagents, /rewind checkpoints, and bidirectional A2A collaboration."
        ),
        "url": rpc_url,
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "version": version,
        "provider": {
            "organization": "Agent Systems Lab",
            "url": clean_base,
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json", "image/png", "image/jpeg"],
        "defaultOutputModes": ["text/plain", "text/markdown", "application/json"],
        "skills": card_skills,
        "supportedInterfaces": [
            {
                "url": rpc_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "supportsAuthenticatedExtendedCard": True,
    }
    if auth_enabled:
        card["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "AGENT_LAB_TOKEN or tenant bearer token",
            }
        }
        card["security"] = [{"bearerAuth": []}]
    if extended:
        card["metadata"] = {
            "gatewayProtocol": gateway_protocol,
            "supportedRpcMethods": [
                "message/send",
                "SendMessage",
                "message/stream",
                "SendStreamingMessage",
                "tasks/get",
                "GetTask",
                "tasks/list",
                "ListTasks",
                "tasks/cancel",
                "CancelTask",
                "tasks/resubscribe",
                "SubscribeToTask",
                "tasks/pushNotificationConfig/set",
                "SetTaskPushNotificationConfig",
                "tasks/pushNotificationConfig/get",
                "GetTaskPushNotificationConfig",
                "tasks/pushNotificationConfig/delete",
                "DeleteTaskPushNotificationConfig",
            ],
        }
    return card


def extract_a2a_message_parts(message: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """从 A2A Message 中提取文本提示词、安全校验后的附件列表与结构化数据负载。"""
    if not isinstance(message, Mapping):
        raise ValueError("A2A message 必须是对象")
    raw_parts = message.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        # 宽松容忍单文本快捷形式
        bare_text = message.get("text")
        if isinstance(bare_text, str) and bare_text.strip():
            return bare_text.strip()[:8000], [], {}
        raise ValueError("A2A message.parts 必须是非空数组")

    text_chunks: list[str] = []
    attachments: list[dict[str, Any]] = []
    merged_data: dict[str, Any] = {}

    for idx, part in enumerate(raw_parts):
        if not isinstance(part, Mapping):
            continue
        pkind = str(part.get("kind") or part.get("type") or "").strip().lower()
        if pkind == "text" or (not pkind and isinstance(part.get("text"), str)):
            tval = str(part.get("text") or "").strip()
            if tval:
                text_chunks.append(tval)
        elif pkind == "data" or (not pkind and isinstance(part.get("data"), Mapping)):
            dval = part.get("data")
            if isinstance(dval, Mapping):
                merged_data.update(dict(dval))
        elif pkind == "file" or (not pkind and isinstance(part.get("file"), Mapping)):
            fobj = part.get("file") if isinstance(part.get("file"), Mapping) else part
            b64 = fobj.get("bytes") or fobj.get("content_base64")
            if isinstance(b64, str) and b64.strip():
                try:
                    decoded = base64.b64decode(b64.strip(), validate=True)
                except Exception as exc:
                    raise ValueError(f"A2A file part #{idx} base64 非法") from exc
                if len(decoded) > _MAX_A2A_ATTACHMENT_BYTES:
                    raise ValueError(f"A2A file part #{idx} 超过 6 MiB 体积上限")
                mime = str(fobj.get("mimeType") or fobj.get("mediaType") or "application/octet-stream").strip()
                fname = Path(str(fobj.get("name") or f"a2a_part_{idx}.bin")).name
                attachments.append(
                    {
                        "filename": fname[:120],
                        "media_type": mime[:80],
                        "content_base64": b64.strip(),
                    }
                )
        if len(attachments) >= 8:
            break

    if not text_chunks and merged_data:
        text_chunks.append(json.dumps(merged_data, ensure_ascii=False)[:4000])
    prompt = "\n\n".join(text_chunks).strip()
    if not prompt:
        raise ValueError("A2A message 未包含有效的 text 或 data 内容")
    return prompt[:8000], attachments, merged_data


def run_to_a2a_task(
    run: Mapping[str, Any],
    *,
    history_length: int | None = None,
    include_artifacts: bool = True,
    pending_proposals: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """将内部 `run` 字典转换为标准 A2A `Task` 对象。"""
    run_id = str(run.get("id") or "")
    meta = run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
    context_id = str(meta.get("a2a_context_id") or run_id)
    state = run_status_to_a2a_state(str(run.get("status") or ""))
    timestamp = str(run.get("updated_at") or run.get("created_at") or _now_iso())

    status_text = ""
    if state == "input-required":
        props = list(pending_proposals or [])
        if props:
            p0 = props[0]
            status_text = (
                f"需要审批或补充指令：[{p0.get('kind', 'action')}] "
                f"{p0.get('summary') or p0.get('path') or p0.get('tool') or p0.get('id', '')}"
            )
        else:
            status_text = "任务已暂停等待人工审批或补充指令。"
    elif state == "failed":
        status_text = str(run.get("error") or "任务执行失败")
    elif state == "completed":
        status_text = str(run.get("answer") or "任务已完成")
    elif state == "canceled":
        status_text = str(run.get("error") or "任务已取消")

    status_obj: dict[str, Any] = {"state": state, "timestamp": timestamp}
    if status_text:
        status_obj["message"] = {
            "kind": "message",
            "messageId": f"msg-{run_id[:8]}-{state}",
            "role": "agent",
            "taskId": run_id,
            "contextId": context_id,
            "parts": [{"kind": "text", "text": status_text}],
        }

    artifacts: list[dict[str, Any]] = []
    if include_artifacts:
        answer_str = str(run.get("answer") or "").strip()
        if answer_str:
            artifacts.append(
                {
                    "artifactId": f"art-{run_id[:8]}-answer",
                    "name": "answer",
                    "description": "Agent final response",
                    "parts": [{"kind": "text", "text": answer_str}],
                }
            )
        ver = meta.get("verification") if isinstance(meta.get("verification"), Mapping) else None
        if ver or meta.get("cost_usd"):
            artifacts.append(
                {
                    "artifactId": f"art-{run_id[:8]}-telemetry",
                    "name": "telemetry",
                    "description": "Execution verification and token/cost metadata",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "steps": int(run.get("steps") or 0),
                                "input_tokens": int(run.get("input_tokens") or 0),
                                "output_tokens": int(run.get("output_tokens") or 0),
                                "cost_usd": round(float(meta.get("cost_usd") or 0.0), 8),
                                "verification": dict(ver) if ver else {},
                            },
                        }
                    ],
                }
            )

    history: list[dict[str, Any]] = []
    raw_messages = run.get("messages") if isinstance(run.get("messages"), list) else []
    for m_idx, msg in enumerate(raw_messages):
        if not isinstance(msg, Mapping):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str) and content.strip():
            if content.startswith(("[PINNED_", "[HARNESS_STATE]", "[WORKING_MEMORY]")):
                continue
            history.append(
                {
                    "kind": "message",
                    "messageId": f"hist-{run_id[:8]}-{m_idx}",
                    "role": "user",
                    "taskId": run_id,
                    "contextId": context_id,
                    "parts": [{"kind": "text", "text": content.strip()}],
                }
            )
        elif role == "assistant":
            a_texts: list[str] = []
            if isinstance(content, str) and content.strip():
                a_texts.append(content.strip())
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, Mapping) and blk.get("type") == "text" and blk.get("text"):
                        a_texts.append(str(blk["text"]).strip())
            if a_texts:
                history.append(
                    {
                        "kind": "message",
                        "messageId": f"hist-{run_id[:8]}-{m_idx}",
                        "role": "agent",
                        "taskId": run_id,
                        "contextId": context_id,
                        "parts": [{"kind": "text", "text": "\n".join(a_texts)}],
                    }
                )
    if history_length is not None and history_length >= 0:
        history = history[-history_length:] if history_length > 0 else []

    return {
        "kind": "task",
        "id": run_id,
        "contextId": context_id,
        "status": status_obj,
        "artifacts": artifacts,
        "history": history,
        "metadata": {
            "provider": run.get("provider"),
            "mode": run.get("mode"),
            "steps": int(run.get("steps") or 0),
        },
    }


def build_status_update_event(
    task_id: str,
    context_id: str,
    state: str,
    *,
    message_text: str = "",
    final: bool = False,
) -> dict[str, Any]:
    status_obj: dict[str, Any] = {"state": state, "timestamp": _now_iso()}
    if message_text:
        status_obj["message"] = {
            "kind": "message",
            "messageId": f"msg-{uuid.uuid4().hex[:10]}",
            "role": "agent",
            "taskId": task_id,
            "contextId": context_id,
            "parts": [{"kind": "text", "text": message_text}],
        }
    return {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": context_id,
        "status": status_obj,
        "final": bool(final),
    }


def build_artifact_update_event(
    task_id: str,
    context_id: str,
    artifact_id: str,
    text_chunk: str,
    *,
    append: bool = True,
    last_chunk: bool = False,
    name: str = "response",
) -> dict[str, Any]:
    return {
        "kind": "artifact-update",
        "taskId": task_id,
        "contextId": context_id,
        "append": bool(append),
        "lastChunk": bool(last_chunk),
        "artifact": {
            "artifactId": artifact_id,
            "name": name,
            "parts": [{"kind": "text", "text": text_chunk}],
        },
    }


# ---------------------------------------------------------------------------
# 声明式 A2A Peer 注册表（a2a.json）与安全校验
# ---------------------------------------------------------------------------


def _is_blocked_a2a_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _is_blocked_a2a_ip(ip_obj.ipv4_mapped, allow_private=allow_private)
        if ip_obj.sixtofour is not None:
            return _is_blocked_a2a_ip(ip_obj.sixtofour, allow_private=allow_private)
        if ip_obj.teredo is not None:
            return _is_blocked_a2a_ip(ip_obj.teredo[0], allow_private=allow_private) or _is_blocked_a2a_ip(
                ip_obj.teredo[1], allow_private=allow_private
            )
    # 永远阻断链路本地（含 AWS/GCP/Azure 169.254.169.254 IMDS）、未指定、多播与保留网段
    if ip_obj.is_link_local or ip_obj.is_unspecified or ip_obj.is_multicast or ip_obj.is_reserved:
        return True
    if not allow_private and (ip_obj.is_loopback or ip_obj.is_private):
        return True
    return False


def validate_a2a_url(url: str, *, allow_private_network: bool = False) -> str:
    """校验远端 A2A Agent URL，阻断非 http(s)、内嵌凭据、IMDS 元数据地址与未授权私网访问。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("A2A peer URL 不能为空")
    clean = url.strip().rstrip("/")
    if any(ch in clean for ch in ("\r", "\n", "\x00")):
        raise ValueError("A2A peer URL 包含非法控制字符")
    parsed = urllib.parse.urlsplit(clean)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"A2A peer URL 仅支持 http/https 协议: {scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("A2A peer URL 禁止内嵌用户名或密码")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("A2A peer URL 缺少合法主机名")
    if host in _BLOCKED_METADATA_HOSTS:
        raise ValueError(f"安全防护拒绝云元数据主机: {host}")

    eff_private = allow_private_network or (
        os.environ.get("AGENT_LAB_A2A_ALLOW_PRIVATE", "").strip() == "1"
    )
    is_localhost_name = host == "localhost" or host.endswith(".localhost")
    if is_localhost_name and not eff_private:
        raise ValueError(
            "安全防护默认禁止访问回环/私网 A2A Agent（如需连接本地 Agent 请设置 allow_private_network=true）"
        )
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None and _is_blocked_a2a_ip(ip_obj, allow_private=eff_private):
        raise ValueError(f"安全防护拒绝受限 IP 地址: {host}")
    if scheme == "http" and not (is_localhost_name or (ip_obj is not None and eff_private)):
        raise ValueError("公网 A2A peer 必须使用 https://（仅受信任私网/回环允许 http://）")
    return clean


def _validate_resolved_ips(url: str, *, allow_private_network: bool) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("A2A URL 缺少主机名")
    eff_private = allow_private_network or (
        os.environ.get("AGENT_LAB_A2A_ALLOW_PRIVATE", "").strip() == "1"
    )
    try:
        ip_obj = ipaddress.ip_address(host)
        if _is_blocked_a2a_ip(ip_obj, allow_private=eff_private):
            raise ValueError(f"A2A 目标 IP 受安全策略拦截: {host}")
        return
    except ValueError as exc:
        if "受安全策略拦截" in str(exc):
            raise
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError:
        return
    for info in infos:
        addr = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_a2a_ip(ip_obj, allow_private=eff_private):
            raise ValueError(f"A2A 域名 {host} 解析到受限 IP {addr}")


def verify_a2a_push_signature(body_bytes: bytes, signature: str, secret: str) -> bool:
    """常量时间校验 A2A Push Notification 的 HMAC-SHA256 `X-A2A-Signature` 请求头。"""
    if not isinstance(body_bytes, (bytes, bytearray)) or not isinstance(signature, str) or not isinstance(secret, str):
        return False
    clean_secret = secret.strip()
    clean_sig = signature.strip()
    if not clean_secret or not clean_sig:
        return False
    expected = "sha256=" + hmac.new(
        clean_secret.encode("utf-8"), bytes(body_bytes), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(clean_sig, expected)


async def deliver_a2a_push_notification(
    url: str,
    event_payload: Mapping[str, Any],
    *,
    secret: str | None = None,
    allow_private_network: bool = False,
    transport: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """向已注册 Webhook 投递 A2A 异步事件通知（SSRF/IMDS 防护、可选 HMAC-SHA256 签名与 W3C traceparent）。"""
    try:
        clean_url = validate_a2a_url(url, allow_private_network=allow_private_network)
        if transport is None:
            _validate_resolved_ips(clean_url, allow_private_network=allow_private_network)
    except ValueError as exc:
        return {"ok": False, "status_code": None, "error": str(exc)}

    if not isinstance(event_payload, Mapping):
        return {"ok": False, "status_code": None, "error": "event_payload 必须是 JSON 对象"}

    try:
        body_bytes = json.dumps(
            dict(event_payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return {"ok": False, "status_code": None, "error": f"event_payload 序列化失败: {exc}"}

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    tp = format_traceparent()
    if tp:
        headers["traceparent"] = tp
    if isinstance(secret, str) and secret.strip():
        sig_hex = hmac.new(
            secret.strip().encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        headers["X-A2A-Signature"] = f"sha256={sig_hex}"

    eff_timeout = (
        float(timeout)
        if isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(float(timeout))
        and float(timeout) > 0
        else 10.0
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(eff_timeout, connect=min(eff_timeout, 5.0)),
            transport=transport,
            follow_redirects=False,
        ) as client:
            resp = await client.post(clean_url, content=body_bytes, headers=headers)
        ok = 200 <= resp.status_code < 300
        return {
            "ok": ok,
            "status_code": int(resp.status_code),
            "error": None if ok else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "error": str(redact_secrets(str(exc) or type(exc).__name__))[:400],
        }


@dataclass
class A2APeerConfig:
    name: str
    url: str
    card_url: str | None = None
    api_key_env: str = ""
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    allow_private_network: bool = False
    enabled: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "url": self.url,
            "card_url": self.card_url,
            "api_key_env": self.api_key_env,
            "headers": dict(self.headers),
            "timeout_seconds": float(self.timeout_seconds),
            "allow_private_network": bool(self.allow_private_network),
            "enabled": bool(self.enabled),
            "description": self.description,
        }
        if self.api_key:
            payload["api_key"] = self.api_key
        return payload


def _validate_a2a_headers(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("A2A headers 必须是 JSON 对象")
    out: dict[str, str] = {}
    for k, v in raw.items():
        ks, vs = str(k).strip(), str(v)
        if not ks:
            continue
        if any(c in ks or c in vs for c in ("\r", "\n", "\x00")):
            raise ValueError(f"A2A header 包含非法换行或空字节: {ks!r}")
        out[ks] = vs
    return out


def _validate_peer(name: str, raw: Mapping[str, Any]) -> A2APeerConfig:
    clean_name = str(name).strip()
    if not _PEER_NAME_RE.match(clean_name):
        raise ValueError(f"非法 A2A peer 名称: {name!r}")
    allow_private = bool(raw.get("allow_private_network", False))
    url = validate_a2a_url(str(raw.get("url") or ""), allow_private_network=allow_private)
    raw_card = raw.get("card_url")
    card_url = (
        validate_a2a_url(str(raw_card), allow_private_network=allow_private)
        if isinstance(raw_card, str) and raw_card.strip()
        else None
    )
    api_key_env = str(raw.get("api_key_env") or "").strip()
    if api_key_env and not _ENV_VAR_NAME_RE.match(api_key_env):
        raise ValueError(f"A2A peer {clean_name!r} 的 api_key_env 非法: {api_key_env!r}")
    raw_inline_key = raw.get("api_key")
    api_key = str(raw_inline_key).strip() if isinstance(raw_inline_key, str) and raw_inline_key.strip() else None
    headers = _validate_a2a_headers(raw.get("headers"))
    raw_timeout = raw.get("timeout_seconds", 60.0)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise ValueError(f"A2A peer {clean_name!r} 的 timeout_seconds 必须为数字")
    timeout_val = float(raw_timeout)
    if not math.isfinite(timeout_val) or not (1.0 <= timeout_val <= 600.0):
        raise ValueError(f"A2A peer {clean_name!r} 的 timeout_seconds 必须在 [1, 600] 范围内")
    enabled = bool(raw.get("enabled", True))
    description = str(raw.get("description") or "").strip()[:400]
    return A2APeerConfig(
        name=clean_name,
        url=url,
        card_url=card_url,
        api_key_env=api_key_env,
        api_key=api_key,
        headers=headers,
        timeout_seconds=timeout_val,
        allow_private_network=allow_private,
        enabled=enabled,
        description=description,
    )


def default_a2a_template() -> dict[str, Any]:
    return {
        "schema_version": A2A_SCHEMA_VERSION,
        "peers": {
            "local-reviewer": {
                "name": "local-reviewer",
                "url": "http://127.0.0.1:8788",
                "card_url": None,
                "api_key_env": "A2A_LOCAL_REVIEWER_TOKEN",
                "headers": {},
                "timeout_seconds": 60.0,
                "allow_private_network": True,
                "enabled": True,
                "description": "Local peer A2A agent instance for parallel review or delegated execution",
            },
            "remote-researcher": {
                "name": "remote-researcher",
                "url": "https://agent.example.com",
                "card_url": None,
                "api_key_env": "A2A_REMOTE_RESEARCHER_TOKEN",
                "headers": {},
                "timeout_seconds": 90.0,
                "allow_private_network": False,
                "enabled": False,
                "description": "Example remote HTTPS A2A specialist agent (set enabled=true and token env to activate)",
            },
        },
    }


def validate_a2a_document(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, Mapping):
        raise ValueError("a2a.json 顶层必须是 JSON 对象")
    sv = doc.get("schema_version", A2A_SCHEMA_VERSION)
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != A2A_SCHEMA_VERSION:
        raise ValueError(f"不支持的 a2a.json schema_version: {sv!r}")
    raw_peers = doc.get("peers", {})
    if not isinstance(raw_peers, Mapping):
        raise ValueError("a2a.json 的 peers 字段必须是 JSON 对象")
    if len(raw_peers) > _MAX_PEERS:
        raise ValueError(f"A2A peers 数量不能超过 {_MAX_PEERS}")
    peers_out: dict[str, dict[str, Any]] = {}
    for pname, praw in raw_peers.items():
        if not isinstance(praw, Mapping):
            raise ValueError(f"A2A peer {pname!r} 必须是对象")
        validated = _validate_peer(str(pname), praw)
        peers_out[validated.name] = validated.to_dict()
    return {"schema_version": A2A_SCHEMA_VERSION, "peers": peers_out}


def redact_a2a_document(doc: Mapping[str, Any], *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env_src = environ if environ is not None else os.environ
    out: dict[str, Any] = {"schema_version": doc.get("schema_version", A2A_SCHEMA_VERSION), "peers": {}}
    if "config_path" in doc:
        out["config_path"] = doc["config_path"]
    raw_peers = doc.get("peers")
    if isinstance(raw_peers, Mapping):
        for pname, praw in raw_peers.items():
            if not isinstance(praw, Mapping):
                continue
            pcopy = deepcopy(dict(praw))
            key_env = str(pcopy.get("api_key_env") or "").strip()
            has_cred = bool(
                (key_env and env_src.get(key_env, "").strip())
                or (isinstance(pcopy.get("api_key"), str) and pcopy["api_key"].strip())
            )
            if isinstance(pcopy.get("api_key"), str) and pcopy["api_key"].strip():
                pcopy["api_key"] = A2A_REDACTED_MARKER
            raw_h = pcopy.get("headers")
            if isinstance(raw_h, Mapping):
                safe_h: dict[str, str] = {}
                for hk, hv in raw_h.items():
                    hkey = str(hk)
                    if _SENSITIVE_HEADER_RE.search(hkey):
                        safe_h[hkey] = A2A_REDACTED_MARKER
                    else:
                        safe_h[hkey] = _BEARER_VALUE_RE.sub(rf"\1{A2A_REDACTED_MARKER}", str(hv))
                pcopy["headers"] = safe_h
            pcopy["credential_configured"] = has_cred
            out["peers"][str(pname)] = pcopy
    return out


def _assert_no_symlink(target: Path) -> None:
    if target.is_symlink():
        raise ValueError(f"安全防护拒绝符号链接文件: {target}")
    for anc in target.parents:
        if anc == anc.parent:
            break
        if anc.is_symlink() and not (sys.platform == "darwin" and str(anc) in _DARWIN_SYSTEM_SYMLINKS):
            raise ValueError(f"安全防护拒绝符号链接目录: {anc}")


def discover_a2a_config_path(
    *,
    explicit_path: str | Path | None = None,
    state_dir: str | Path | None = None,
    workspace: str | Path | None = None,
    trust_workspace: bool = False,
) -> Path | None:
    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path).expanduser())
    env_p = os.environ.get("AGENT_LAB_A2A_CONFIG", "").strip()
    if env_p:
        candidates.append(Path(env_p).expanduser())
    if trust_workspace and workspace is not None:
        candidates.append(Path(workspace) / ".agent-lab" / "a2a.json")
    if state_dir is not None:
        candidates.append(Path(state_dir) / "a2a.json")
    for cand in candidates:
        try:
            if cand.exists() and not cand.is_symlink() and cand.is_file():
                _assert_no_symlink(cand)
                return cand
        except ValueError:
            continue
    return None


def load_a2a_config(
    *,
    explicit_path: str | Path | None = None,
    state_dir: str | Path | None = None,
    workspace: str | Path | None = None,
    trust_workspace: bool = False,
) -> dict[str, Any]:
    path = discover_a2a_config_path(
        explicit_path=explicit_path,
        state_dir=state_dir,
        workspace=workspace,
        trust_workspace=trust_workspace,
    )
    if path is None:
        return {"schema_version": A2A_SCHEMA_VERSION, "peers": {}, "config_path": None}
    _assert_no_symlink(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"安全防护拒绝打开符号链接: {path}") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _MAX_A2A_FILE_BYTES:
            raise ValueError(f"不安全或超限的 a2a.json 配置文件: {path}")
        raw_bytes = os.read(fd, _MAX_A2A_FILE_BYTES + 1)
    finally:
        os.close(fd)
    parsed = json.loads(raw_bytes.decode("utf-8"))
    validated = validate_a2a_document(parsed)
    validated["config_path"] = str(path)
    return validated


def save_a2a_config(path: str | Path, doc: Mapping[str, Any]) -> dict[str, Any]:
    target = Path(path).expanduser()
    validated = validate_a2a_document(doc)
    _assert_no_symlink(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink(target)
    payload = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(payload)
            tf.flush()
            os.fsync(tf.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    validated["config_path"] = str(target)
    return validated


# ---------------------------------------------------------------------------
# A2A Client Manager & Agent Tool Specifications
# ---------------------------------------------------------------------------


class A2AClientManager:
    """面向远端 A2A 智能体的异步客户端管理器（支持发现 AgentCard、任务委托、状态轮询与取消）。"""

    def __init__(
        self,
        peers_doc: Mapping[str, Any] | None = None,
        *,
        transport: Any = None,
    ) -> None:
        self._peers: dict[str, A2APeerConfig] = {}
        self._card_cache: dict[str, dict[str, Any]] = {}
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        if peers_doc:
            validated = validate_a2a_document(peers_doc)
            for pname, pdata in validated["peers"].items():
                cfg = _validate_peer(pname, pdata)
                if cfg.enabled:
                    self._peers[cfg.name] = cfg

    @classmethod
    def from_config(
        cls,
        *,
        explicit_path: str | Path | None = None,
        state_dir: str | Path | None = None,
        workspace: str | Path | None = None,
        trust_workspace: bool = False,
        transport: Any = None,
    ) -> A2AClientManager:
        doc = load_a2a_config(
            explicit_path=explicit_path,
            state_dir=state_dir,
            workspace=workspace,
            trust_workspace=trust_workspace,
        )
        return cls(doc, transport=transport)

    @property
    def enabled(self) -> bool:
        return len(self._peers) > 0

    def peer_names(self) -> list[str]:
        return sorted(self._peers.keys())

    def get_peer(self, name: str) -> A2APeerConfig | None:
        return self._peers.get(name)

    def list_peers(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for pname in self.peer_names():
            cfg = self._peers[pname]
            card = self._card_cache.get(pname)
            has_cred = bool(
                (cfg.api_key_env and os.environ.get(cfg.api_key_env, "").strip())
                or cfg.api_key
            )
            items.append(
                {
                    "name": cfg.name,
                    "url": cfg.url,
                    "description": (card.get("description") if card else None) or cfg.description,
                    "credential_configured": has_cred,
                    "allow_private_network": cfg.allow_private_network,
                    "card_cached": card is not None,
                    "skills": [
                        {"id": s.get("id"), "name": s.get("name"), "description": s.get("description")}
                        for s in (card.get("skills") or [])[:8]
                        if isinstance(s, Mapping)
                    ]
                    if card
                    else [],
                }
            )
        return items

    def render_peer_directory(self, max_chars: int = 1200) -> str:
        if not self._peers:
            return ""
        lines = ["[A2A_PEER_DIRECTORY] 已配置的远端 A2A 协同智能体（通过 a2a_discover_agent / a2a_send_task 调用）："]
        for item in self.list_peers():
            sk_names = ", ".join(str(s.get("id") or s.get("name") or "") for s in item["skills"][:4])
            sk_part = f" | skills: {sk_names}" if sk_names else ""
            lines.append(f"- {item['name']} ({item['url']}): {item['description'][:120]}{sk_part}")
        return "\n".join(lines)[:max_chars]

    def _get_client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0),
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    def _resolve_peer_headers(self, peer: A2APeerConfig) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        tp = format_traceparent()
        if tp:
            headers["traceparent"] = tp
        for k, v in peer.headers.items():
            interp = _ENV_INTERP_RE.sub(lambda m: os.environ.get(m.group(1), ""), v)
            if any(c in interp for c in ("\r", "\n", "\x00")):
                raise ValueError(f"A2A header {k!r} 插值结果包含非法换行")
            headers[k] = interp
        secret = (
            os.environ.get(peer.api_key_env, "").strip()
            if peer.api_key_env
            else (peer.api_key or "")
        )
        if secret and not any(hk.lower() == "authorization" for hk in headers):
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _rpc_endpoint(self, peer: A2APeerConfig) -> str:
        card = self._card_cache.get(peer.name)
        if card and isinstance(card.get("url"), str) and card["url"].strip():
            try:
                return validate_a2a_url(card["url"], allow_private_network=peer.allow_private_network)
            except ValueError:
                pass
        base = peer.url.rstrip("/")
        return base if base.endswith("/a2a") else f"{base}/a2a"

    async def discover_agent_card(
        self,
        peer_or_url: str,
        *,
        allow_private_network: bool = False,
    ) -> dict[str, Any]:
        """获取并缓存远端 AgentCard（优先 `/.well-known/agent-card.json`，自动回退 `/.well-known/agent.json`）。"""
        peer = self._peers.get(peer_or_url)
        if peer is not None:
            eff_private = peer.allow_private_network
            base = peer.url.rstrip("/")
            if base.endswith("/a2a"):
                base = base[:-4]
            candidate_urls = (
                [peer.card_url]
                if peer.card_url
                else [f"{base}/.well-known/agent-card.json", f"{base}/.well-known/agent.json"]
            )
            headers = self._resolve_peer_headers(peer)
            timeout = peer.timeout_seconds
        else:
            clean_url = validate_a2a_url(peer_or_url, allow_private_network=allow_private_network)
            eff_private = allow_private_network
            if clean_url.endswith(".json"):
                candidate_urls = [clean_url]
            else:
                base = clean_url[:-4] if clean_url.endswith("/a2a") else clean_url
                candidate_urls = [f"{base}/.well-known/agent-card.json", f"{base}/.well-known/agent.json"]
            headers = {"Accept": "application/json"}
            tp = format_traceparent()
            if tp:
                headers["traceparent"] = tp
            timeout = 30.0

        client = self._get_client(timeout=timeout)
        last_err = ""
        for card_url in candidate_urls:
            if self._transport is None:
                _validate_resolved_ips(card_url, allow_private_network=eff_private)
            resp = await client.get(card_url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                last_err = f"404 at {card_url}"
                continue
            resp.raise_for_status()
            if len(resp.content) > _MAX_A2A_CARD_BYTES:
                raise ValueError("远端 AgentCard 超过 256 KiB 上限")
            card = resp.json()
            if not isinstance(card, dict) or not card.get("name"):
                raise ValueError(f"远端 AgentCard 格式无效: {card_url}")
            if peer is not None:
                self._card_cache[peer.name] = card
            return {"ok": True, "peer": peer.name if peer else peer_or_url, "card_url": card_url, "card": card}
        return {"ok": False, "code": "a2a_card_not_found", "error": last_err or "未找到远端 AgentCard"}

    async def _rpc_call(
        self,
        peer: A2APeerConfig,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        endpoint = self._rpc_endpoint(peer)
        if self._transport is None:
            _validate_resolved_ips(endpoint, allow_private_network=peer.allow_private_network)
        headers = self._resolve_peer_headers(peer)
        eff_timeout = float(timeout) if timeout is not None else peer.timeout_seconds
        client = self._get_client(timeout=eff_timeout)

        methods_to_try = [method]
        pascal = _PASCAL_FALLBACK_METHODS.get(method)
        if pascal and pascal not in methods_to_try:
            methods_to_try.append(pascal)

        for idx, m_name in enumerate(methods_to_try):
            body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": m_name, "params": params}
            resp = await client.post(endpoint, headers=headers, json=body, timeout=eff_timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("远端 A2A 响应不是合法 JSON-RPC 对象")
            if "error" in data and isinstance(data["error"], dict):
                code = data["error"].get("code")
                if code == JSONRPC_METHOD_NOT_FOUND and idx + 1 < len(methods_to_try):
                    continue
                return {
                    "ok": False,
                    "code": f"a2a_rpc_{code}",
                    "error": str(data["error"].get("message") or "A2A RPC error"),
                    "rpc_error": data["error"],
                }
            return {"ok": True, "result": data.get("result")}
        return {"ok": False, "code": "a2a_rpc_failed", "error": "A2A RPC 请求失败"}

    @staticmethod
    def _extract_answer_from_rpc_result(res_obj: Any) -> dict[str, Any]:
        if not isinstance(res_obj, Mapping):
            return {"task_id": None, "context_id": None, "state": "unknown", "answer": str(res_obj or ""), "artifacts": []}
        kind = str(res_obj.get("kind") or "").lower()
        if kind == "message" or ("parts" in res_obj and "status" not in res_obj):
            texts = [
                str(p.get("text") or "")
                for p in (res_obj.get("parts") or [])
                if isinstance(p, Mapping) and p.get("text")
            ]
            return {
                "task_id": res_obj.get("taskId"),
                "context_id": res_obj.get("contextId"),
                "state": "completed",
                "answer": "\n".join(texts).strip(),
                "artifacts": [],
            }
        status = res_obj.get("status") if isinstance(res_obj.get("status"), Mapping) else {}
        state = str(status.get("state") or "unknown")
        artifacts = res_obj.get("artifacts") if isinstance(res_obj.get("artifacts"), list) else []
        answer_texts: list[str] = []
        for art in artifacts:
            if not isinstance(art, Mapping):
                continue
            for p in art.get("parts") or []:
                if isinstance(p, Mapping) and p.get("text"):
                    answer_texts.append(str(p["text"]))
        if not answer_texts and isinstance(status.get("message"), Mapping):
            for p in status["message"].get("parts") or []:
                if isinstance(p, Mapping) and p.get("text"):
                    answer_texts.append(str(p["text"]))
        return {
            "task_id": res_obj.get("id"),
            "context_id": res_obj.get("contextId"),
            "state": state,
            "answer": "\n".join(answer_texts).strip(),
            "artifacts": artifacts,
        }

    async def send_message(
        self,
        peer_name: str,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        blocking: bool = True,
        data: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        peer = self._peers.get(peer_name)
        if peer is None:
            return {"ok": False, "code": "a2a_peer_not_found", "error": f"未配置或未启用的 A2A peer: {peer_name}"}
        parts: list[dict[str, Any]] = [{"kind": "text", "text": str(text)}]
        if data:
            parts.append({"kind": "data", "data": dict(data)})
        message: dict[str, Any] = {
            "kind": "message",
            "messageId": f"msg-{uuid.uuid4().hex[:12]}",
            "role": "user",
            "parts": parts,
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        params: dict[str, Any] = {
            "message": message,
            "configuration": {
                "acceptedOutputModes": ["text/plain", "text/markdown", "application/json"],
                "blocking": bool(blocking),
            },
        }
        rpc = await self._rpc_call(peer, "message/send", params, timeout=timeout)
        if not rpc.get("ok"):
            return {**rpc, "peer": peer.name}
        parsed = self._extract_answer_from_rpc_result(rpc.get("result"))
        return {"ok": True, "peer": peer.name, **parsed}

    async def get_task(
        self, peer_name: str, task_id: str, *, history_length: int | None = None
    ) -> dict[str, Any]:
        peer = self._peers.get(peer_name)
        if peer is None:
            return {"ok": False, "code": "a2a_peer_not_found", "error": f"未配置或未启用的 A2A peer: {peer_name}"}
        params: dict[str, Any] = {"id": str(task_id)}
        if history_length is not None:
            params["historyLength"] = max(0, int(history_length))
        rpc = await self._rpc_call(peer, "tasks/get", params)
        if not rpc.get("ok"):
            return {**rpc, "peer": peer.name}
        parsed = self._extract_answer_from_rpc_result(rpc.get("result"))
        return {"ok": True, "peer": peer.name, **parsed}

    async def cancel_task(self, peer_name: str, task_id: str) -> dict[str, Any]:
        peer = self._peers.get(peer_name)
        if peer is None:
            return {"ok": False, "code": "a2a_peer_not_found", "error": f"未配置或未启用的 A2A peer: {peer_name}"}
        rpc = await self._rpc_call(peer, "tasks/cancel", {"id": str(task_id)})
        if not rpc.get("ok"):
            return {**rpc, "peer": peer.name}
        parsed = self._extract_answer_from_rpc_result(rpc.get("result"))
        return {"ok": True, "peer": peer.name, **parsed}

    async def _rpc_stream(
        self,
        peer: A2APeerConfig,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        endpoint = self._rpc_endpoint(peer)
        if self._transport is None:
            _validate_resolved_ips(endpoint, allow_private_network=peer.allow_private_network)
        headers = self._resolve_peer_headers(peer)
        headers["Accept"] = "text/event-stream, application/json"
        eff_timeout = float(timeout) if timeout is not None else peer.timeout_seconds
        client = self._get_client(timeout=eff_timeout)

        methods_to_try = [method]
        pascal = _PASCAL_FALLBACK_METHODS.get(method)
        if pascal and pascal not in methods_to_try:
            methods_to_try.append(pascal)

        for idx, m_name in enumerate(methods_to_try):
            body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": m_name, "params": params}
            yielded_any = False
            retry_fallback = False

            async with client.stream("POST", endpoint, headers=headers, json=body, timeout=eff_timeout) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").lower()
                if "text/event-stream" not in ctype:
                    raw_bytes = await resp.aread()
                    data = json.loads(raw_bytes.decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("远端 A2A 流式响应不是合法 JSON-RPC 对象")
                    if "error" in data and isinstance(data["error"], dict):
                        code = data["error"].get("code")
                        if code == JSONRPC_METHOD_NOT_FOUND and idx + 1 < len(methods_to_try):
                            continue
                        yield {
                            "ok": False,
                            "kind": "error",
                            "peer": peer.name,
                            "code": f"a2a_rpc_{code}",
                            "error": str(data["error"].get("message") or "A2A RPC error"),
                            "rpc_error": data["error"],
                        }
                        return
                    res_obj = data.get("result")
                    if isinstance(res_obj, dict):
                        yield res_obj
                    return

                data_buf: list[str] = []

                def _decode_frame(raw_text: str) -> tuple[str, Any]:
                    cleaned = raw_text.strip()
                    if not cleaned or cleaned == "[DONE]":
                        return ("skip", None)
                    try:
                        frame = json.loads(cleaned)
                    except json.JSONDecodeError:
                        return ("skip", None)
                    if not isinstance(frame, dict):
                        return ("skip", None)
                    if isinstance(frame.get("error"), dict):
                        return ("error", frame["error"])
                    if isinstance(frame.get("result"), dict):
                        return ("event", frame["result"])
                    if isinstance(frame.get("kind"), str):
                        return ("event", frame)
                    return ("skip", None)

                async for raw_line in resp.aiter_lines():
                    line = raw_line.rstrip("\r")
                    if line == "":
                        if not data_buf:
                            continue
                        raw_data = "\n".join(data_buf)
                        data_buf.clear()
                        tag, payload = _decode_frame(raw_data)
                        if tag == "skip":
                            continue
                        if tag == "error":
                            code = payload.get("code")
                            if (
                                code == JSONRPC_METHOD_NOT_FOUND
                                and not yielded_any
                                and idx + 1 < len(methods_to_try)
                            ):
                                retry_fallback = True
                                break
                            yield {
                                "ok": False,
                                "kind": "error",
                                "peer": peer.name,
                                "code": f"a2a_rpc_{code}",
                                "error": str(payload.get("message") or "A2A RPC error"),
                                "rpc_error": payload,
                            }
                            return
                        yielded_any = True
                        yield payload
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        val = line[5:]
                        if val.startswith(" "):
                            val = val[1:]
                        data_buf.append(val)

                if not retry_fallback and data_buf:
                    raw_data = "\n".join(data_buf)
                    data_buf.clear()
                    tag, payload = _decode_frame(raw_data)
                    if tag == "error":
                        code = payload.get("code")
                        if (
                            code == JSONRPC_METHOD_NOT_FOUND
                            and not yielded_any
                            and idx + 1 < len(methods_to_try)
                        ):
                            retry_fallback = True
                        else:
                            yield {
                                "ok": False,
                                "kind": "error",
                                "peer": peer.name,
                                "code": f"a2a_rpc_{code}",
                                "error": str(payload.get("message") or "A2A RPC error"),
                                "rpc_error": payload,
                            }
                            return
                    elif tag == "event":
                        yielded_any = True
                        yield payload

            if retry_fallback:
                continue
            return

    async def send_message_stream(
        self,
        peer_name: str,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过 SSE 流式发送 A2A 消息（优先 `message/stream`，遇 `-32601` 自动回退 `SendStreamingMessage`）。"""
        peer = self._peers.get(peer_name)
        if peer is None:
            yield {
                "ok": False,
                "kind": "error",
                "peer": peer_name,
                "code": "a2a_peer_not_found",
                "error": f"未配置或未启用的 A2A peer: {peer_name}",
            }
            return
        parts: list[dict[str, Any]] = [{"kind": "text", "text": str(text)}]
        if data:
            parts.append({"kind": "data", "data": dict(data)})
        message: dict[str, Any] = {
            "kind": "message",
            "messageId": f"msg-{uuid.uuid4().hex[:12]}",
            "role": "user",
            "parts": parts,
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        params: dict[str, Any] = {
            "message": message,
            "configuration": {
                "acceptedOutputModes": ["text/plain", "text/markdown", "application/json"],
            },
        }
        async for event in self._rpc_stream(peer, "message/stream", params, timeout=timeout):
            yield event

    async def resubscribe_task(
        self,
        peer_name: str,
        task_id: str,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过 SSE 重新订阅远端 A2A 任务事件流（优先 `tasks/resubscribe`，遇 `-32601` 回退 `SubscribeToTask`）。"""
        peer = self._peers.get(peer_name)
        if peer is None:
            yield {
                "ok": False,
                "kind": "error",
                "peer": peer_name,
                "code": "a2a_peer_not_found",
                "error": f"未配置或未启用的 A2A peer: {peer_name}",
            }
            return
        async for event in self._rpc_stream(
            peer, "tasks/resubscribe", {"id": str(task_id)}, timeout=timeout
        ):
            yield event

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


A2A_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "a2a_list_agents",
        "description": "列出当前已配置的远端 A2A 协同智能体（名称、地址、能力摘要与技能列表）。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "a2a_discover_agent",
        "description": "拉取并解析远端 A2A 智能体的 AgentCard（/.well-known/agent-card.json 或 agent.json），了解其技能与输入输出能力。",
        "input_schema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "在 a2a.json 中配置的远端智能体名称"},
            },
            "required": ["peer"],
            "additionalProperties": False,
        },
    },
    {
        "name": "a2a_send_task",
        "description": "按 A2A 协议向远端协同智能体委托任务或继续多轮对话（受权限策略与人工审批门控）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "远端 A2A 智能体名称"},
                "prompt": {"type": "string", "description": "要委托给远端智能体的明确任务描述"},
                "task_id": {"type": "string", "description": "可选：继续已有远端任务时传入 task_id"},
                "context_id": {"type": "string", "description": "可选：跨轮次共享的 A2A contextId"},
                "blocking": {"type": "boolean", "description": "是否等待远端任务完成（默认 true）"},
            },
            "required": ["peer", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "a2a_get_task",
        "description": "查询远端 A2A 任务的最新状态与产出 Artifacts。",
        "input_schema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "远端 A2A 智能体名称"},
                "task_id": {"type": "string", "description": "远端任务 ID"},
                "history_length": {"type": "integer", "minimum": 0, "maximum": 50},
            },
            "required": ["peer", "task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "a2a_cancel_task",
        "description": "请求取消正在运行的远端 A2A 任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "远端 A2A 智能体名称"},
                "task_id": {"type": "string", "description": "远端任务 ID"},
            },
            "required": ["peer", "task_id"],
            "additionalProperties": False,
        },
    },
]

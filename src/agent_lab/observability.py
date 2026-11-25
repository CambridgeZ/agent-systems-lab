"""结构化可观测性：W3C Trace Context 传播、敏感字段脱敏日志、错误安全摘要与 Prometheus 指标注册表。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）：
   本模块是 Agent 全链路分布式追踪、日志脱敏与生产遥测的底层基础设施：
   - 跨进程/跨协程链路追踪：兼容 W3C Trace Context 1.0（`traceparent`）标准，通过 `ContextVar`
     在 FastAPI 中间件、`asyncio.to_thread` 工作线程与持久队列 `LeaseWorker` 间贯通同一 `trace_id`；
   - 零信任凭据脱敏：`redact_secrets` 递归屏蔽键名命中敏感词的字段，并清洗字符串内嵌的 `Bearer`
     令牌与数据库连接串（DSN）明文密码，供结构化日志、MCP 工具回执与长期记忆落盘复用；
   - 安全错误摘要与 Prometheus 指标：`sanitize_error_detail` 剥离可能夹带隐私数据的原始异常文本；
     `MetricsRegistry` 提供零第三方依赖、线程安全的 Counter / Gauge / Histogram 与 0.0.4 文本导出。

2. 核心工程与安全不变量（Core Invariants）：
   - [W3C Trace Context 规范校验] 按标准拒绝版本号 `ff`、全零 32 位十六进制 `trace-id` 与全零 16 位
     `span-id`，对不合法外部输入自动退回新建随机 UUID 或 SHA-256 摘要，杜绝日志注入（Log Injection）。
   - [Python `bool` 子类陷阱防御] 在 Python 类型体系中 `isinstance(True, int)` 为 `True`；指标写入
     与格式化入口显式拦截 `isinstance(value, bool)`，防止误传布尔值污染数值监控曲线。
   - [短临界区快照渲染] `render_prometheus` 仅在 `threading.Lock` 内做浅拷贝快照，将耗时的排序与多行
     文本拼接移出锁外执行，避免 `/metrics` 高频抓取阻塞业务线程埋点。
================================================================================
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import threading
import time
from typing import Any, Iterator
import urllib.error
import urllib.parse
import urllib.request
import uuid

_TRACE_ID_VAR: ContextVar[str | None] = ContextVar("agent_lab_trace_id", default=None)
_SPAN_ID_VAR: ContextVar[str | None] = ContextVar("agent_lab_span_id", default=None)
_PARENT_SPAN_ID_VAR: ContextVar[str | None] = ContextVar("agent_lab_parent_span_id", default=None)

_TRACEPARENT_RE = re.compile(
    r"^\s*([0-9a-fA-F]{2})-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-([0-9a-fA-F]{2})\s*$"
)
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX16_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_SAFE_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SECRET_KEY_RE = re.compile(r"(?:key|token|secret|authorization|password|credential)", re.IGNORECASE)
_BEARER_VALUE_RE = re.compile(r"(?i)\b(Bearer\s+)[^\s,;\"']+")
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)")
_PROM_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")
_PROM_LABEL_RE = re.compile(r"[^a-zA-Z0-9_]")

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_AWS_IPV6_IMDS_NET = ipaddress.ip_network("fd00:ec2::254/128")
_BLOCKED_OTLP_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "metadata.azure.internal",
    }
)

_DEFAULT_MS_BUCKETS: tuple[float, ...] = (
    1.0,
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10000.0,
)

_LOGGER = logging.getLogger("agent_lab")


def _new_span_id() -> str:
    """生成符合 W3C Trace Context 规范的 16 位小写十六进制非全零 span_id。"""
    span_id = os.urandom(8).hex()
    if span_id == "0" * 16:
        span_id = "0" * 15 + "1"
    return span_id


def _normalize_trace_id_hex32(trace_id: str) -> str:
    raw = str(trace_id or "").strip()
    compact = raw.replace("-", "").lower()
    if _HEX32_RE.fullmatch(compact) and compact != "0" * 32:
        return compact
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_trace_and_span(traceparent_or_id: str | None) -> tuple[str, str | None]:
    """解析上游传入的 W3C `traceparent` 或自定义请求 ID，返回 `(trace_id, incoming_parent_span_id)`。"""
    if traceparent_or_id is None:
        return uuid.uuid4().hex, None
    raw = str(traceparent_or_id).strip()
    if not raw:
        return uuid.uuid4().hex, None
    match = _TRACEPARENT_RE.match(raw)
    if match:
        version, trace_id, span_id, _flags = match.groups()
        trace_lower = trace_id.lower()
        span_lower = span_id.lower()
        # W3C Trace Context 规范：version=ff 保留无效，且 trace-id 与 parent-id 均严禁全 0。
        if version.lower() != "ff" and trace_lower != "0" * 32 and span_lower != "0" * 16:
            return trace_lower, span_lower
        return uuid.uuid4().hex, None
    compact = raw.replace("-", "").lower()
    if _HEX32_RE.fullmatch(compact) and compact != "0" * 32:
        return compact, None
    if _SAFE_TRACE_ID_RE.fullmatch(raw):
        return raw, None
    # 含空白、换行或特殊控制字符的不可信输入统一哈希定长化，阻断头部与日志伪造注入。
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32], None


def _parse_trace_input(traceparent_or_id: str | None) -> str:
    """解析上游传入的 W3C `traceparent` 或自定义请求 ID，清洗为安全的追踪标识符。"""
    trace_id, _ = _parse_trace_and_span(traceparent_or_id)
    return trace_id


def current_trace_id() -> str | None:
    """返回当前上下文已绑定的 trace_id；若未绑定则返回 None（不产生惰性副作用）。"""
    return _TRACE_ID_VAR.get()


def current_span_id() -> str | None:
    """返回当前上下文活跃的 16-hex W3C span_id；若未处于 Span 内则返回 None。"""
    return _SPAN_ID_VAR.get()


def current_parent_span_id() -> str | None:
    """返回当前上下文活跃 Span（或上游 W3C traceparent）关联的 parent_span_id。"""
    return _PARENT_SPAN_ID_VAR.get()


def get_trace_id() -> str:
    """获取当前协程/线程上下文绑定的 trace_id；若尚未绑定则惰性生成并绑定。"""
    current = _TRACE_ID_VAR.get()
    if not current:
        current = uuid.uuid4().hex
        _TRACE_ID_VAR.set(current)
    return current


@contextmanager
def bind_trace_context(traceparent_or_id: str | None = None) -> Iterator[str]:
    """解析 W3C traceparent 或自定义 trace_id 并绑定到当前 contextvars 作用域。

    若在同一 trace_id 内重入绑定（例如 LeaseWorker 已开启 Span 后 Runtime 再次绑定同一 traceparent），
    保留当前活跃的 span_id 与 parent_span_id，避免丢失层级调用链；切换至新 trace_id 时则隔离重置 Span 上下文。
    """
    trace_id, incoming_parent_span = _parse_trace_and_span(traceparent_or_id)
    prev_trace = _TRACE_ID_VAR.get()
    trace_token = _TRACE_ID_VAR.set(trace_id)
    span_token = None
    parent_token = None
    if prev_trace == trace_id:
        if (
            incoming_parent_span is not None
            and _SPAN_ID_VAR.get() is None
            and _PARENT_SPAN_ID_VAR.get() is None
        ):
            parent_token = _PARENT_SPAN_ID_VAR.set(incoming_parent_span)
    else:
        span_token = _SPAN_ID_VAR.set(None)
        parent_token = _PARENT_SPAN_ID_VAR.set(incoming_parent_span)
    try:
        yield trace_id
    finally:
        if span_token is not None:
            _SPAN_ID_VAR.reset(span_token)
        if parent_token is not None:
            _PARENT_SPAN_ID_VAR.reset(parent_token)
        _TRACE_ID_VAR.reset(trace_token)


def format_traceparent(trace_id: str | None = None, span_id: str | None = None) -> str:
    """将 trace_id 与活跃（或随机生成的）span_id 格式化为标准 W3C traceparent (00-<32hex>-<16hex>-01)。"""
    raw = get_trace_id() if trace_id is None else str(trace_id or "").strip()
    norm_trace = _normalize_trace_id_hex32(raw)
    candidate_span = span_id if span_id is not None else _SPAN_ID_VAR.get()
    if (
        isinstance(candidate_span, str)
        and _HEX16_RE.fullmatch(candidate_span.strip())
        and candidate_span.strip().lower() != "0" * 16
    ):
        resolved_span = candidate_span.strip().lower()
    else:
        resolved_span = _new_span_id()
    return f"00-{norm_trace}-{resolved_span}-01"


class SpanRecord(dict):
    """兼容字典索引与属性访问的轻量级追踪 Span 记录。"""

    @property
    def name(self) -> str:
        return str(self["name"])

    @property
    def trace_id(self) -> str:
        return str(self["trace_id"])

    @property
    def span_id(self) -> str:
        return str(self["span_id"])

    @property
    def parent_span_id(self) -> str | None:
        return self.get("parent_span_id")

    @property
    def attributes(self) -> dict[str, Any]:
        return self["attributes"]

    @property
    def duration_ms(self) -> float:
        return float(self.get("duration_ms", 0.0))

    @property
    def status(self) -> str:
        return str(self.get("status", "ok"))

    def set_attribute(self, key: str, value: Any) -> None:
        self["attributes"][str(key)] = value


def _is_otlp_ip_allowed(raw_ip: str, *, allow_private: bool = False) -> bool:
    """校验 OTLP 目标解析后的 IP 地址，严格阻断云元数据、链路本地、多播与默认私有网段 SSRF。"""
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or ip in _AWS_IPV6_IMDS_NET
    ):
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return False
    if not allow_private and (ip.is_private or ip.is_loopback or not ip.is_global):
        return False
    return True


def _resolve_otlp_allow_private(allow_private: bool | None) -> bool:
    if allow_private is not None:
        return bool(allow_private)
    return os.environ.get("AGENT_LAB_OTLP_ALLOW_PRIVATE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _parse_otlp_endpoint_fast(
    url: str | None, *, allow_private: bool | None = None
) -> tuple[str, str, int, bool, bool] | None:
    """无阻塞纯语法与字面量 IP 预校验：立即拦截非法协议、凭据、云元数据与字面量内网地址。

    返回 `(cleaned_url, host, port, is_ip_literal, eff_allow_private)`；非法时返回 None。
    """
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip()
    if not cleaned or any(ch in cleaned for ch in ("\r", "\n", "\x00")):
        return None
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _BLOCKED_OTLP_HOSTS or host.endswith(".internal"):
        return None
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if not 1 <= port <= 65535:
        return None
    eff_allow_private = _resolve_otlp_allow_private(allow_private)
    try:
        ipaddress.ip_address(host)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if is_ip_literal and not _is_otlp_ip_allowed(host, allow_private=eff_allow_private):
        return None
    return cleaned, host, port, is_ip_literal, eff_allow_private


def validate_otlp_endpoint(url: str | None, *, allow_private: bool | None = None) -> str | None:
    """完整校验 OTLP/HTTP 导出端点 URL（含 DNS 解析），阻断危险协议、内嵌凭据、云元数据及未授权内网 SSRF。"""
    fast = _parse_otlp_endpoint_fast(url, allow_private=allow_private)
    if fast is None:
        return None
    cleaned, host, port, is_ip_literal, eff_allow_private = fast
    if is_ip_literal:
        return cleaned
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    ips = [str(info[4][0]) for info in infos if info and info[4]]
    if not ips:
        return None
    if not all(_is_otlp_ip_allowed(ip_str, allow_private=eff_allow_private) for ip_str in ips):
        return None
    return cleaned


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用 OTLP HTTP 重定向，阻断通过 302 跳转访问云元数据或内网服务的 SSRF 绕过。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, "otlp_redirects_disabled", headers, fp)


def _to_otlp_attr_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}


def _build_otlp_span_payload(span: dict[str, Any]) -> dict[str, Any]:
    """将脱敏后的 SpanRecord 转换为标准 OTLP/HTTP JSON (`ExportTraceServiceRequest`) 载荷。"""
    redacted_attrs = redact_secrets(dict(span.get("attributes") or {}))
    otlp_attrs = [
        {"key": str(k), "value": _to_otlp_attr_value(v)}
        for k, v in redacted_attrs.items()
    ]
    span_obj: dict[str, Any] = {
        "traceId": _normalize_trace_id_hex32(str(span.get("trace_id") or "")),
        "spanId": str(span.get("span_id") or _new_span_id()),
        "name": str(span.get("name") or "span"),
        "kind": 1,
        "startTimeUnixNano": str(int(span.get("start_time_unix_nano") or 0)),
        "endTimeUnixNano": str(int(span.get("end_time_unix_nano") or 0)),
        "attributes": otlp_attrs,
        "status": {"code": 2 if span.get("status") == "error" else 1},
    }
    parent_id = span.get("parent_span_id")
    if isinstance(parent_id, str) and _HEX16_RE.fullmatch(parent_id) and parent_id != "0" * 16:
        span_obj["parentSpanId"] = parent_id.lower()
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {
                                "stringValue": os.environ.get(
                                    "AGENT_LAB_SERVICE_NAME", "agent-systems-lab"
                                )
                            },
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "agent_lab.observability"},
                        "spans": [span_obj],
                    }
                ],
            }
        ]
    }


class _BackgroundOTLPExporter:
    """有界非阻塞后台 OTLP/HTTP JSON 导出器：未配置端点时零线程开销，队列满或网络故障时静默丢弃。"""

    def __init__(self, maxsize: int = 256, timeout: float = 2.0) -> None:
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=maxsize)
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            worker = threading.Thread(
                target=self._run,
                name="agent-lab-otlp-exporter",
                daemon=True,
            )
            worker.start()
            self._thread = worker

    def _run(self) -> None:
        while True:
            endpoint, body = self._queue.get()
            try:
                # 发送前复核一次 SSRF 规则以缓解 DNS 重绑定窗口。
                validated = validate_otlp_endpoint(endpoint)
                if validated is None:
                    continue
                req = urllib.request.Request(
                    validated,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self._opener.open(req, timeout=self._timeout):
                    pass
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def enqueue(self, endpoint: str, span: dict[str, Any]) -> bool:
        fast = _parse_otlp_endpoint_fast(endpoint)
        if fast is None:
            return False
        cleaned = fast[0]
        payload = _build_otlp_span_payload(span)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._ensure_worker()
        try:
            self._queue.put_nowait((cleaned, body))
            return True
        except queue.Full:
            return False


_OTLP_EXPORTER = _BackgroundOTLPExporter()


@contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[SpanRecord]:
    """开启层级追踪 Span，生成合规 16-hex W3C span_id、关联 parent_span_id、记录耗时并按需异步导出 OTLP。"""
    existing_trace = _TRACE_ID_VAR.get()
    trace_token = None
    if not existing_trace:
        trace_id = uuid.uuid4().hex
        trace_token = _TRACE_ID_VAR.set(trace_id)
    else:
        trace_id = existing_trace

    prev_span = _SPAN_ID_VAR.get()
    prev_parent = _PARENT_SPAN_ID_VAR.get()
    parent_span_id = prev_span if prev_span is not None else prev_parent
    span_id = _new_span_id()

    parent_token = _PARENT_SPAN_ID_VAR.set(parent_span_id)
    span_token = _SPAN_ID_VAR.set(span_id)

    start_ns = time.time_ns()
    t0 = time.perf_counter()
    span = SpanRecord(
        name=str(name),
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=None,
        duration_ms=0.0,
        status="ok",
        attributes=dict(attributes) if attributes else {},
    )
    try:
        yield span
    except BaseException as exc:
        span["status"] = "error"
        span["error_type"] = type(exc).__name__
        span["attributes"].setdefault("error.type", type(exc).__name__)
        raise
    finally:
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        end_ns = max(start_ns, time.time_ns())
        span["end_time_unix_nano"] = end_ns
        span["duration_ms"] = duration_ms
        _SPAN_ID_VAR.reset(span_token)
        _PARENT_SPAN_ID_VAR.reset(parent_token)
        if trace_token is not None:
            _TRACE_ID_VAR.reset(trace_token)
        otlp_endpoint = os.environ.get("AGENT_LAB_OTLP_ENDPOINT", "").strip()
        if otlp_endpoint:
            _OTLP_EXPORTER.enqueue(otlp_endpoint, span)


def _redact_string(text: str) -> str:
    masked = _BEARER_VALUE_RE.sub(r"\1[REDACTED]", text)
    return _URL_CRED_RE.sub(r"\1[REDACTED]\3", masked)


def redact_secrets(value: Any) -> Any:
    """递归脱敏包含密钥语义键名或内嵌 Bearer/DSN 凭据的数据结构。"""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _SECRET_KEY_RE.search(key_str):
                sanitized[key_str] = "[REDACTED]"
            else:
                sanitized[key_str] = redact_secrets(item)
        return sanitized
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def log_structured(level: str, event: str, **fields: Any) -> dict[str, Any]:
    """输出脱敏后的单行结构化 JSON 日志并返回记录字典。"""
    norm_level = str(level or "INFO").upper()
    redacted_fields = redact_secrets(fields)
    trace_id = str(redacted_fields.pop("trace_id", None) or get_trace_id())
    active_span_id = redacted_fields.pop("span_id", None) or current_span_id()
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": norm_level,
        "event": str(event),
        "trace_id": trace_id,
    }
    if active_span_id:
        record["span_id"] = str(active_span_id)
    record.update(redacted_fields)
    numeric_level = getattr(logging, norm_level, logging.INFO)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    _LOGGER.log(numeric_level, json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str))
    return record


def sanitize_error_detail(exc: BaseException) -> dict[str, Any]:
    """提取可安全对外返回与记录的错误分类信息，绝不泄露原始异常消息中的敏感凭据。"""
    raw_code = getattr(exc, "code", None)
    error_code = (
        str(raw_code)
        if isinstance(raw_code, (str, int)) and str(raw_code).strip()
        else type(exc).__name__.lower()
    )
    retryable = bool(
        isinstance(exc, (TimeoutError, ConnectionError)) or getattr(exc, "retryable", False)
    )
    return {
        "error_type": type(exc).__name__,
        "error_code": error_code,
        "retryable": retryable,
        "trace_id": get_trace_id(),
    }


def _sanitize_metric_name(name: str) -> str:
    cleaned = _PROM_NAME_RE.sub("_", str(name).strip())
    if not cleaned:
        raise ValueError("metric_name_invalid")
    if cleaned[0].isdigit():
        cleaned = f"m_{cleaned}"
    return cleaned


def _normalize_labels(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    pairs: list[tuple[str, str]] = []
    for key, val in sorted(labels.items(), key=lambda item: str(item[0])):
        clean_key = _PROM_LABEL_RE.sub("_", str(key).strip())
        if not clean_key:
            continue
        if clean_key[0].isdigit():
            clean_key = f"l_{clean_key}"
        pairs.append((clean_key, str(val)))
    return tuple(pairs)


def _escape_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_label_block(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape_prom_label(v)}"' for k, v in labels)
    return f"{{{inner}}}"


def _format_prom_number(value: int | float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    return f"{as_float:.6g}"


class MetricsRegistry:
    """线程安全的进程内指标注册表，支持 Counter、Gauge、耗时直方图与 Prometheus 文本导出。"""

    def __init__(self, buckets_ms: tuple[float, ...] = _DEFAULT_MS_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets_ms = tuple(sorted(float(b) for b in buckets_ms))
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    def inc(
        self, name: str, labels: dict[str, Any] | None = None, value: int | float = 1
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("counter_increment_must_be_non_negative")
        metric = _sanitize_metric_name(name)
        norm_labels = _normalize_labels(labels)
        key = (metric, norm_labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def set_gauge(
        self, name: str, value: int | float, labels: dict[str, Any] | None = None
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("gauge_value_invalid")
        metric = _sanitize_metric_name(name)
        norm_labels = _normalize_labels(labels)
        key = (metric, norm_labels)
        with self._lock:
            self._gauges[key] = float(value)

    def observe_ms(
        self, name: str, duration_ms: int | float, labels: dict[str, Any] | None = None
    ) -> None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)) or duration_ms < 0:
            raise ValueError("duration_ms_must_be_non_negative")
        metric = _sanitize_metric_name(name)
        norm_labels = _normalize_labels(labels)
        key = (metric, norm_labels)
        val = float(duration_ms)
        with self._lock:
            entry = self._histograms.get(key)
            if entry is None:
                entry = {
                    "count": 0,
                    "sum": 0.0,
                    "min": val,
                    "max": val,
                    "buckets": {b: 0 for b in self._buckets_ms},
                }
                self._histograms[key] = entry
            entry["count"] += 1
            entry["sum"] += val
            if val < entry["min"]:
                entry["min"] = val
            if val > entry["max"]:
                entry["max"] = val
            # Prometheus 直方图采用累积桶（Cumulative Histogram）：凡满足 val <= bound 的上界桶均递增。
            for bound in self._buckets_ms:
                if val <= bound:
                    entry["buckets"][bound] += 1

    def record_tool_metric(
        self,
        tool_name: str,
        duration_ms: int | float,
        ok: bool,
        category: str = "tool",
    ) -> None:
        """统一上报工具、bash_exec、subagent、sandbox 与 MCP 调用的计数器与耗时直方图指标。"""
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)) or duration_ms < 0:
            raise ValueError("duration_ms_must_be_non_negative")
        clean_tool = str(tool_name or "unknown").strip()[:120] or "unknown"
        raw_cat = str(category or "tool").strip().lower()
        category_aliases = {
            "tool": "tool",
            "tools": "tool",
            "bash": "bash_exec",
            "bash_exec": "bash_exec",
            "shell": "bash_exec",
            "subagent": "subagent",
            "subagents": "subagent",
            "sandbox": "sandbox",
            "mcp": "mcp",
        }
        clean_category = category_aliases.get(raw_cat, raw_cat or "tool")
        status = "ok" if bool(ok) else "error"
        self.inc(
            "agent_lab_tool_calls_total",
            {"tool": clean_tool, "category": clean_category, "status": status},
        )
        self.observe_ms(
            "agent_lab_tool_duration_ms",
            float(duration_ms),
            {"tool": clean_tool, "category": clean_category},
        )
        if not ok:
            self.inc(
                "agent_lab_tool_errors_total",
                {"tool": clean_tool, "category": clean_category},
            )
        if clean_category != "tool":
            self.inc(
                f"agent_lab_{clean_category}_calls_total",
                {"tool": clean_tool, "status": status},
            )
            self.observe_ms(
                f"agent_lab_{clean_category}_duration_ms",
                float(duration_ms),
                {"tool": clean_tool},
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters_snap: list[dict[str, Any]] = [
                {"name": name, "labels": dict(lbls), "value": val}
                for (name, lbls), val in sorted(self._counters.items())
            ]
            gauges_snap: list[dict[str, Any]] = [
                {"name": name, "labels": dict(lbls), "value": val}
                for (name, lbls), val in sorted(self._gauges.items())
            ]
            hist_snap: list[dict[str, Any]] = []
            for (name, lbls), entry in sorted(self._histograms.items()):
                cnt = int(entry["count"])
                total = float(entry["sum"])
                hist_snap.append(
                    {
                        "name": name,
                        "labels": dict(lbls),
                        "count": cnt,
                        "sum_ms": total,
                        "min_ms": float(entry["min"]),
                        "max_ms": float(entry["max"]),
                        "avg_ms": (total / cnt) if cnt else 0.0,
                        "buckets": {str(b): int(c) for b, c in entry["buckets"].items()},
                    }
                )
        return {
            "counters": counters_snap,
            "gauges": gauges_snap,
            "histograms": hist_snap,
        }

    def render_prometheus(self) -> str:
        """按标准 Prometheus Text Exposition Format (0.0.4) 渲染全部指标。"""
        with self._lock:
            counters_copy = dict(self._counters)
            gauges_copy = dict(self._gauges)
            hist_copy = {
                key: {
                    "count": val["count"],
                    "sum": val["sum"],
                    "buckets": dict(val["buckets"]),
                }
                for key, val in self._histograms.items()
            }

        lines: list[str] = []
        declared_types: set[str] = set()

        for (name, lbls), val in sorted(counters_copy.items()):
            if name not in declared_types:
                lines.append(f"# TYPE {name} counter")
                declared_types.add(name)
            lines.append(f"{name}{_format_label_block(lbls)} {_format_prom_number(val)}")

        for (name, lbls), val in sorted(gauges_copy.items()):
            if name not in declared_types:
                lines.append(f"# TYPE {name} gauge")
                declared_types.add(name)
            lines.append(f"{name}{_format_label_block(lbls)} {_format_prom_number(val)}")

        for (name, lbls), entry in sorted(hist_copy.items()):
            if name not in declared_types:
                lines.append(f"# TYPE {name} histogram")
                declared_types.add(name)
            for bound in self._buckets_ms:
                b_labels = (*lbls, ("le", _format_prom_number(bound)))
                lines.append(f"{name}_bucket{_format_label_block(b_labels)} {entry['buckets'][bound]}")
            inf_labels = (*lbls, ("le", "+Inf"))
            lines.append(f"{name}_bucket{_format_label_block(inf_labels)} {entry['count']}")
            lines.append(f"{name}_sum{_format_label_block(lbls)} {_format_prom_number(entry['sum'])}")
            lines.append(f"{name}_count{_format_label_block(lbls)} {entry['count']}")

        return ("\n".join(lines) + "\n") if lines else ""


METRICS = MetricsRegistry()

"""结构化可观测性：W3C Trace Context 传播、敏感字段脱敏日志、错误安全摘要与 Prometheus 指标注册表。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import threading
from typing import Any, Iterator
import uuid

_TRACE_ID_VAR: ContextVar[str | None] = ContextVar("agent_lab_trace_id", default=None)

_TRACEPARENT_RE = re.compile(
    r"^\s*([0-9a-fA-F]{2})-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-([0-9a-fA-F]{2})\s*$"
)
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SAFE_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SECRET_KEY_RE = re.compile(r"(?:key|token|secret|authorization|password|credential)", re.IGNORECASE)
_BEARER_VALUE_RE = re.compile(r"(?i)\b(Bearer\s+)[^\s,;\"']+")
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)")
_PROM_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")
_PROM_LABEL_RE = re.compile(r"[^a-zA-Z0-9_]")

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


def _parse_trace_input(traceparent_or_id: str | None) -> str:
    if traceparent_or_id is None:
        return uuid.uuid4().hex
    raw = str(traceparent_or_id).strip()
    if not raw:
        return uuid.uuid4().hex
    match = _TRACEPARENT_RE.match(raw)
    if match:
        version, trace_id, span_id, _flags = match.groups()
        trace_lower = trace_id.lower()
        span_lower = span_id.lower()
        if version.lower() != "ff" and trace_lower != "0" * 32 and span_lower != "0" * 16:
            return trace_lower
        return uuid.uuid4().hex
    compact = raw.replace("-", "").lower()
    if _HEX32_RE.fullmatch(compact) and compact != "0" * 32:
        return compact
    if _SAFE_TRACE_ID_RE.fullmatch(raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_trace_id() -> str:
    """获取当前协程/线程上下文绑定的 trace_id；若尚未绑定则惰性生成并绑定。"""
    current = _TRACE_ID_VAR.get()
    if not current:
        current = uuid.uuid4().hex
        _TRACE_ID_VAR.set(current)
    return current


@contextmanager
def bind_trace_context(traceparent_or_id: str | None = None) -> Iterator[str]:
    """解析 W3C traceparent 或自定义 trace_id 并绑定到当前 contextvars 作用域。"""
    trace_id = _parse_trace_input(traceparent_or_id)
    token = _TRACE_ID_VAR.set(trace_id)
    try:
        yield trace_id
    finally:
        _TRACE_ID_VAR.reset(token)


def format_traceparent(trace_id: str) -> str:
    """将 trace_id 格式化为标准 W3C traceparent (00-<32hex>-<16hex>-01)。"""
    raw = str(trace_id or "").strip()
    compact = raw.replace("-", "").lower()
    if _HEX32_RE.fullmatch(compact) and compact != "0" * 32:
        norm_trace = compact
    else:
        norm_trace = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    span_id = os.urandom(8).hex()
    if span_id == "0" * 16:
        span_id = "0" * 15 + "1"
    return f"00-{norm_trace}-{span_id}-01"


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
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": norm_level,
        "event": str(event),
        "trace_id": trace_id,
        **redacted_fields,
    }
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
            for bound in self._buckets_ms:
                if val <= bound:
                    entry["buckets"][bound] += 1

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

"""运行级轻量异步流式事件代理（RunStreamBroker）与可插拔跨节点传输层（StreamTransport）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块是 AgentRuntime 与前端 SSE / CLI 流式渲染之间的零数据库轮询事件总线：
   - 实时承载模型增量输出（`thinking_delta`、`text_delta`、`tool_use_start`、`input_json_delta`）、
     终端实时日志流（`bash_output_delta`）、子 Agent 进度（`subagent_progress`）、
     持久化时间线事件镜像（`event`）以及任务状态跃迁（`run_status`）；
   - 为每个 `run_id` 维护单调递增 `seq` 与有界环形缓冲（默认 256 条），断线重连客户端可通过
     `subscribe(run_id, after_seq=last_seq)` 无损补齐错过的增量帧；
   - 通过可插拔 `StreamTransport` 协议支持默认零开销进程内模式（`InMemoryStreamTransport`）、
     自定义回调桥接模式（`CallbackStreamTransport`）与多 Pod PostgreSQL `LISTEN/NOTIFY`
     跨节点扇出模式（`PostgresNotifyStreamTransport`，频道 `agent_lab_stream`）。

2. 并发与跨节点不变量（Concurrency & Multi-Node Invariants）
   - 跨线程发布安全：后台工具线程（如 `bash_exec` 的管道排水线程 `_drain_stream`）与异步事件循环
     均可直接调用 `publish()`；对归属其他事件循环的订阅队列自动经 `loop.call_soon_threadsafe` 投递；
   - 锁外外部传输：调用 `transport.publish_external()` 严格位于 `self._lock` 临界区之外且捕获异常，
     绝不因外部传输回调重入或网络波动导致死锁或阻断本地 SSE/CLI 渲染；
   - 防回声闭环（Echo Prevention）：每条出站信封携带来源 `node_id`，`PostgresNotifyStreamTransport`
     与 `RunStreamBroker._on_remote_envelope()` 双向过滤自产 `node_id` 帧，且远端注入只落本地缓冲
     与订阅者而绝不二次回发 `publish_external()`；
   - 慢消费者背压隔离：单订阅者队列与 PG 出站队列满时均淘汰最旧待消费帧，绝不阻塞生产者线程；
   - 有界通道回收：超过 `max_runs` 时仅淘汰无活跃订阅者的最久未活跃通道，防长驻 API 进程内存泄漏。
================================================================================
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import inspect
import json
import os
import re
import threading
import time
from typing import Any, Protocol, runtime_checkable
import uuid

from .observability import log_structured

PG_STREAM_NOTIFY_CHANNEL = "agent_lab_stream"
_PG_NOTIFY_DEFAULT_MAX_BYTES = 7500

STREAM_EVENT_TYPES = frozenset(
    {
        "thinking_delta",
        "text_delta",
        "tool_use_start",
        "input_json_delta",
        "bash_output_delta",
        "subagent_progress",
        "event",
        "run_status",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_node_id() -> str:
    return f"node-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _validate_pg_channel(channel: str) -> str:
    clean = str(channel or "").strip()
    if not clean or len(clean) > 63 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean):
        raise ValueError("pg_notify_channel_invalid")
    return clean


def _resolve_pg_dsn(dsn_or_config: Any) -> tuple[str, int]:
    """兼容原始 DSN 字符串、`PostgresBackendConfig` 或携带 `.pg_config` 的 `Storage` 实例。"""
    if isinstance(dsn_or_config, str):
        clean = dsn_or_config.strip()
        if not clean:
            raise ValueError("postgres_dsn_empty")
        return clean, 10
    pg_cfg = getattr(dsn_or_config, "pg_config", None)
    if pg_cfg is not None:
        dsn_or_config = pg_cfg
    raw_dsn = getattr(dsn_or_config, "dsn", None)
    if isinstance(raw_dsn, str) and raw_dsn.strip():
        timeout = int(getattr(dsn_or_config, "connect_timeout", 10) or 10)
        return raw_dsn.strip(), max(1, timeout)
    raise ValueError("postgres_dsn_invalid")


def _truncate_utf8_str(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "...[truncated]"
    budget = max(0, max_bytes - len(suffix))
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped + suffix


def _encode_pg_stream_payload(
    run_id: str,
    envelope: dict[str, Any],
    *,
    node_id: str,
    max_bytes: int = _PG_NOTIFY_DEFAULT_MAX_BYTES,
) -> str:
    """将流式信封序列化为严格受控在 PostgreSQL `pg_notify` 8KB 上限内的 UTF-8 JSON 文本。"""
    origin = str(envelope.get("node_id") or node_id).strip() or node_id
    wire: dict[str, Any] = {
        "seq": envelope.get("seq"),
        "run_id": run_id,
        "type": str(envelope.get("type") or "event").strip() or "event",
        "timestamp": str(envelope.get("timestamp") or _now_iso()),
        "node_id": origin,
        "payload": dict(envelope["payload"]) if isinstance(envelope.get("payload"), dict) else {},
    }
    raw = json.dumps(wire, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(raw.encode("utf-8")) <= max_bytes:
        return raw

    # 对超长载荷字段按 UTF-8 字节预算优雅截断，确保永远不触碰 PostgreSQL 8000 字节硬上限。
    payload_copy = dict(wire["payload"])
    large_keys: list[str] = []
    for key, val in list(payload_copy.items()):
        if isinstance(val, str):
            if len(val.encode("utf-8")) > 256:
                large_keys.append(key)
        elif isinstance(val, (dict, list)):
            serialized_val = json.dumps(val, ensure_ascii=False, separators=(",", ":"), default=str)
            if len(serialized_val.encode("utf-8")) > 256:
                payload_copy[key] = serialized_val
                large_keys.append(key)

    per_field_budget = max(128, (max_bytes - 512) // max(1, len(large_keys)))
    for key in large_keys:
        if isinstance(payload_copy.get(key), str):
            payload_copy[key] = _truncate_utf8_str(payload_copy[key], per_field_budget)
    payload_copy["truncated"] = True
    wire["payload"] = payload_copy

    trimmed = json.dumps(wire, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(trimmed.encode("utf-8")) <= max_bytes:
        return trimmed

    minimal = {
        "seq": wire["seq"],
        "run_id": run_id[:128],
        "type": wire["type"][:64],
        "timestamp": wire["timestamp"][:64],
        "node_id": origin[:128],
        "payload": {"truncated": True},
    }
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"), default=str)


def _enqueue_nowait_bounded(
    queue: asyncio.Queue[dict[str, Any] | None],
    item: dict[str, Any] | None,
) -> None:
    """在订阅者所属事件循环线程内非阻塞入队；满队列时丢弃最旧数据帧（永不丢失关闭哨兵 None）。"""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(item)


def _enqueue_str_bounded(queue: asyncio.Queue[str], item: str) -> None:
    """非阻塞入队字符串帧；满队列时淘汰最旧帧，防止慢网络反向阻塞生产线程。"""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(item)


@runtime_checkable
class StreamTransport(Protocol):
    """可插拔跨节点流式事件传输协议（支撑单机零开销、回调桥接与多 Pod PG NOTIFY 扇出）。"""

    def publish_external(self, run_id: str, envelope: dict[str, Any]) -> None:
        """将本节点新产生的事件信封非阻塞投递到外部传输总线。"""
        ...

    async def start(self, on_remote_envelope: Callable[[str, dict[str, Any]], None]) -> None:
        """启动远端事件订阅监听，并将远端到达的 `(run_id, envelope)` 回调注入本地代理。"""
        ...

    async def aclose(self) -> None:
        """优雅停止远端监听并释放底层连接资源（幂等）。"""
        ...


class InMemoryStreamTransport:
    """默认零开销进程内空操作传输层：不产生任何序列化、队列或网络开销。"""

    def publish_external(self, run_id: str, envelope: dict[str, Any]) -> None:
        del run_id, envelope
        return None

    async def start(self, on_remote_envelope: Callable[[str, dict[str, Any]], None]) -> None:
        del on_remote_envelope
        return None

    async def aclose(self) -> None:
        return None


class CallbackStreamTransport:
    """基于回调的可插拔流式传输层：便于测试多 Pod 扇出、自定义事件总线或外部 Redis/NATS 桥接。"""

    def __init__(
        self,
        on_publish: Callable[[str, dict[str, Any]], Any] | None = None,
        *,
        node_id: str | None = None,
        on_start: Callable[[Callable[[str, dict[str, Any]], None]], Any] | None = None,
        on_close: Callable[[], Any] | None = None,
    ) -> None:
        self._on_publish = on_publish
        self._on_start = on_start
        self._on_close = on_close
        self._explicit_node_id = node_id is not None and bool(str(node_id).strip())
        self.node_id: str = str(node_id).strip() if self._explicit_node_id else _default_node_id()
        self._on_remote_envelope: Callable[[str, dict[str, Any]], None] | None = None
        self._started = False
        self._lock = threading.RLock()

    def bind_node_id(self, node_id: str) -> None:
        """由所属 RunStreamBroker 同步节点标识（若构造时未显式指定则自动对齐）。"""
        clean = str(node_id or "").strip()
        if clean and not self._explicit_node_id:
            self.node_id = clean

    def publish_external(self, run_id: str, envelope: dict[str, Any]) -> None:
        cb = self._on_publish
        if cb is None:
            return
        wire = dict(envelope)
        wire.setdefault("run_id", run_id)
        wire.setdefault("node_id", self.node_id)
        result = cb(run_id, wire)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                if inspect.iscoroutine(result):
                    result.close()
                return
            loop.create_task(result)

    def deliver_remote(self, run_id: str, envelope: dict[str, Any]) -> bool:
        """将外部总线收到的远端事件投递给本节点的注册回调；自动过滤本节点自产回声帧。"""
        if not isinstance(envelope, dict):
            return False
        origin = envelope.get("node_id")
        if isinstance(origin, str) and origin.strip() == self.node_id:
            return False
        with self._lock:
            cb = self._on_remote_envelope
        if cb is None:
            return False
        clean_run_id = str(run_id or envelope.get("run_id") or "").strip()
        if not clean_run_id:
            return False
        cb(clean_run_id, dict(envelope))
        return True

    async def start(self, on_remote_envelope: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._on_remote_envelope = on_remote_envelope
            if self._started:
                return
            self._started = True
        if self._on_start is not None:
            res = self._on_start(self.deliver_remote)
            if inspect.isawaitable(res):
                await res

    async def aclose(self) -> None:
        with self._lock:
            self._on_remote_envelope = None
            was_started = self._started
            self._started = False
        if was_started and self._on_close is not None:
            res = self._on_close()
            if inspect.isawaitable(res):
                await res


class PostgresNotifyStreamTransport:
    """基于 PostgreSQL `pg_notify('agent_lab_stream', payload)` / `LISTEN` 的跨节点多 Pod SSE 扇出传输层。"""

    def __init__(
        self,
        dsn: str | Any,
        *,
        channel: str = PG_STREAM_NOTIFY_CHANNEL,
        node_id: str | None = None,
        connect_timeout: int | None = None,
        application_name: str = "agent-systems-lab-stream",
        max_outbound_queue: int = 1024,
        max_payload_bytes: int = _PG_NOTIFY_DEFAULT_MAX_BYTES,
        enable_listen: bool = True,
    ) -> None:
        resolved_dsn, default_timeout = _resolve_pg_dsn(dsn)
        if isinstance(max_outbound_queue, bool) or not isinstance(max_outbound_queue, int) or max_outbound_queue < 1:
            raise ValueError("max_outbound_queue 必须是正整数")
        if isinstance(max_payload_bytes, bool) or not isinstance(max_payload_bytes, int) or max_payload_bytes < 256:
            raise ValueError("max_payload_bytes 必须是不小于 256 的整数")
        self._dsn = resolved_dsn
        self._channel = _validate_pg_channel(channel)
        self._connect_timeout = (
            max(1, int(connect_timeout)) if connect_timeout is not None else default_timeout
        )
        self._application_name = str(application_name or "agent-systems-lab-stream").strip()[:64]
        self._max_outbound_queue = max_outbound_queue
        self._max_payload_bytes = min(7900, max_payload_bytes)
        self._enable_listen = bool(enable_listen)
        self._explicit_node_id = node_id is not None and bool(str(node_id).strip())
        self.node_id: str = str(node_id).strip() if self._explicit_node_id else _default_node_id()

        self._state_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbound_queue: asyncio.Queue[str] | None = None
        self._prestart_buffer: deque[str] = deque(maxlen=max_outbound_queue)
        self._on_remote_envelope: Callable[[str, dict[str, Any]], None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._publish_task: asyncio.Task[None] | None = None
        self._listen_active = False
        self._started = False
        self._closed = False

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def is_listening(self) -> bool:
        return self._listen_active

    def bind_node_id(self, node_id: str) -> None:
        """由所属 RunStreamBroker 同步节点标识（若构造时未显式指定则自动对齐）。"""
        clean = str(node_id or "").strip()
        if clean and not self._explicit_node_id:
            self.node_id = clean

    def publish_external(self, run_id: str, envelope: dict[str, Any]) -> None:
        """线程安全、非阻塞地将事件信封排入出站队列，由后台 PG 连接异步调用 `pg_notify`。"""
        if self._closed or not isinstance(envelope, dict):
            return
        clean_run_id = str(run_id or envelope.get("run_id") or "").strip()
        if not clean_run_id:
            return
        payload = _encode_pg_stream_payload(
            clean_run_id,
            envelope,
            node_id=self.node_id,
            max_bytes=self._max_payload_bytes,
        )
        with self._state_lock:
            if self._closed:
                return
            loop = self._loop
            outbound = self._outbound_queue
            if loop is None or loop.is_closed() or outbound is None:
                self._prestart_buffer.append(payload)
                return
        try:
            loop.call_soon_threadsafe(_enqueue_str_bounded, outbound, payload)
        except RuntimeError:
            return

    def _dispatch_remote_dict(self, message: dict[str, Any]) -> bool:
        inner = message.get("envelope") if isinstance(message.get("envelope"), dict) else None
        origin = message.get("node_id")
        if origin is None and inner is not None:
            origin = inner.get("node_id")
        # 严格过滤本节点自产回声，防止多 Pod 广播风暴与本地重复推送。
        if isinstance(origin, str) and origin.strip() == self.node_id:
            return False

        envelope = dict(inner) if inner is not None else dict(message)
        run_id = str(message.get("run_id") or envelope.get("run_id") or "").strip()
        if not run_id:
            return False
        envelope["run_id"] = run_id
        if isinstance(origin, str) and origin.strip():
            envelope.setdefault("node_id", origin.strip())

        with self._state_lock:
            cb = self._on_remote_envelope
        if cb is None:
            return False
        try:
            cb(run_id, envelope)
        except Exception as exc:
            log_structured(
                "WARNING",
                "pg_stream_remote_callback_failed",
                node_id=self.node_id,
                run_id=run_id,
                error_type=type(exc).__name__,
            )
            return False
        return True

    def handle_raw_notification(self, payload: str) -> bool:
        """解析单条 PostgreSQL NOTIFY JSON 文本，过滤自产 `node_id` 回声并回调本地代理。"""
        if not isinstance(payload, str) or not payload.strip():
            return False
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed, dict):
            return False
        return self._dispatch_remote_dict(parsed)

    def _handle_notify_item(self, notify: Any) -> bool:
        raw_payload = getattr(notify, "payload", notify)
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8", errors="replace")
        if isinstance(raw_payload, dict):
            return self._dispatch_remote_dict(raw_payload)
        if isinstance(raw_payload, str):
            return self.handle_raw_notification(raw_payload)
        return False

    async def _pg_publish_loop(self) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError:
            return
        async_conn_cls = getattr(psycopg, "AsyncConnection", None)
        if async_conn_cls is None or not hasattr(async_conn_cls, "connect"):
            return

        stop_event = self._stop_event
        outbound = self._outbound_queue
        if stop_event is None or outbound is None:
            return

        aconn: Any = None
        backoff = 0.5
        try:
            while not stop_event.is_set():
                get_task = asyncio.create_task(outbound.get())
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {get_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    if stop_task in done:
                        break
                    payload = get_task.result()
                except BaseException:
                    for task in (get_task, stop_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(get_task, stop_task, return_exceptions=True)
                    raise

                try:
                    if aconn is None:
                        aconn = await async_conn_cls.connect(
                            self._dsn,
                            autocommit=True,
                            connect_timeout=self._connect_timeout,
                            application_name=f"{self._application_name}-pub",
                        )
                    await aconn.execute(
                        "SELECT pg_notify(%s, %s)",
                        (self._channel, payload),
                    )
                    backoff = 0.5
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_structured(
                        "WARNING",
                        "pg_stream_publish_degraded",
                        node_id=self.node_id,
                        channel=self._channel,
                        error_type=type(exc).__name__,
                    )
                    if aconn is not None:
                        with contextlib.suppress(Exception):
                            await aconn.close()
                        aconn = None
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    backoff = min(10.0, backoff * 2.0)
        finally:
            if aconn is not None:
                with contextlib.suppress(Exception):
                    await aconn.close()

    async def _pg_listen_loop(self) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError:
            return
        async_conn_cls = getattr(psycopg, "AsyncConnection", None)
        if async_conn_cls is None or not hasattr(async_conn_cls, "connect"):
            return

        stop_event = self._stop_event
        if stop_event is None:
            return

        backoff = 0.5
        while not stop_event.is_set():
            aconn: Any = None
            try:
                aconn = await async_conn_cls.connect(
                    self._dsn,
                    autocommit=True,
                    connect_timeout=self._connect_timeout,
                    application_name=f"{self._application_name}-sub",
                )
                if not callable(getattr(aconn, "notifies", None)):
                    return
                await aconn.execute(f"LISTEN {self._channel}")
                self._listen_active = True
                backoff = 0.5
                while not stop_event.is_set():
                    try:
                        notify_iter = aconn.notifies(timeout=1.0)
                        supports_timeout = True
                    except TypeError:
                        notify_iter = aconn.notifies()
                        supports_timeout = False
                    got_any = False
                    t_iter = time.monotonic()
                    async for notify in notify_iter:
                        got_any = True
                        self._handle_notify_item(notify)
                        if stop_event.is_set():
                            break
                    if not supports_timeout:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                        break
                    if not got_any and (time.monotonic() - t_iter) < 0.05:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(stop_event.wait(), timeout=0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_structured(
                    "WARNING",
                    "pg_stream_listen_degraded",
                    node_id=self.node_id,
                    channel=self._channel,
                    error_type=type(exc).__name__,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                backoff = min(10.0, backoff * 2.0)
            finally:
                self._listen_active = False
                if aconn is not None:
                    with contextlib.suppress(Exception):
                        await aconn.close()

    async def start(self, on_remote_envelope: Callable[[str, dict[str, Any]], None]) -> None:
        """启动 PostgreSQL `LISTEN agent_lab_stream` 订阅协程与异步 `pg_notify` 发布协程（幂等）。"""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            self._on_remote_envelope = on_remote_envelope
            if self._started:
                return
            self._closed = False
            self._started = True
            self._loop = loop
            self._stop_event = asyncio.Event()
            outbound: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_outbound_queue)
            while self._prestart_buffer:
                _enqueue_str_bounded(outbound, self._prestart_buffer.popleft())
            self._outbound_queue = outbound
            self._publish_task = asyncio.create_task(self._pg_publish_loop())
            if self._enable_listen:
                self._listen_task = asyncio.create_task(self._pg_listen_loop())

    async def aclose(self) -> None:
        """优雅关闭后台 PG 监听与发布协程并释放资源（幂等）。"""
        with self._state_lock:
            self._closed = True
            self._started = False
            self._on_remote_envelope = None
            stop_event = self._stop_event
            listen_task = self._listen_task
            publish_task = self._publish_task
            self._stop_event = None
            self._listen_task = None
            self._publish_task = None
            self._outbound_queue = None
            self._loop = None
            self._prestart_buffer.clear()

        if stop_event is not None:
            stop_event.set()
        tasks = [t for t in (listen_task, publish_task) if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._listen_active = False


@dataclass
class _RunChannel:
    run_id: str
    buffer: deque[dict[str, Any]]
    seq: int = 0
    closed: bool = False
    updated_at: float = field(default_factory=time.monotonic)
    subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any] | None]]] = field(
        default_factory=set
    )


class RunStreamBroker:
    """轻量级每 Run 异步发布/订阅代理，带单调序号、有界近期事件环形缓冲与可插拔跨节点传输层。"""

    def __init__(
        self,
        *,
        max_buffer: int = 256,
        max_runs: int = 1024,
        max_subscriber_queue: int | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        transport: StreamTransport | None = None,
        node_id: str | None = None,
    ) -> None:
        if isinstance(max_buffer, bool) or not isinstance(max_buffer, int) or max_buffer < 1:
            raise ValueError("max_buffer 必须是正整数")
        if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1:
            raise ValueError("max_runs 必须是正整数")
        if node_id is not None:
            clean_node_id = str(node_id).strip()
            if not clean_node_id:
                raise ValueError("node_id 不能为空")
            resolved_node_id = clean_node_id
        else:
            transport_node = getattr(transport, "node_id", None) if transport is not None else None
            resolved_node_id = (
                transport_node.strip()
                if isinstance(transport_node, str) and transport_node.strip()
                else _default_node_id()
            )
        self.max_buffer = max_buffer
        self.max_runs = max_runs
        self.max_subscriber_queue = (
            max(max_buffer, 512)
            if max_subscriber_queue is None
            else max(1, int(max_subscriber_queue))
        )
        self.node_id: str = resolved_node_id
        if transport is None:
            self.transport: StreamTransport = InMemoryStreamTransport()
            self._transport_is_in_memory = True
        else:
            self.transport = transport
            self._transport_is_in_memory = type(transport) is InMemoryStreamTransport
            if node_id is not None:
                bind_fn = getattr(transport, "bind_node_id", None)
                if callable(bind_fn):
                    bind_fn(self.node_id)
        self._transport_started = False
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._lock = threading.RLock()
        self._channels: OrderedDict[str, _RunChannel] = OrderedDict()

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.AbstractEventLoop | None:
        """可选绑定主事件循环引用（单订阅者仍按各自实际运行的循环投递）。"""
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        if loop is not None:
            self._loop = loop
        return self._loop

    async def start(self) -> None:
        """启动外部传输层订阅（对默认 `InMemoryStreamTransport` 为零开销空操作，幂等）。"""
        self.bind_loop()
        if self._transport_started:
            return
        res = self.transport.start(self._on_remote_envelope)
        if inspect.isawaitable(res):
            await res
        self._transport_started = True

    async def aclose(self) -> None:
        """优雅关闭外部传输层并清理后台协程（幂等）。"""
        self._transport_started = False
        res = self.transport.aclose()
        if inspect.isawaitable(res):
            await res

    def _get_or_create_channel_locked(self, run_id: str) -> _RunChannel:
        clean_id = str(run_id or "").strip()
        if not clean_id:
            raise ValueError("run_id 不能为空")
        channel = self._channels.get(clean_id)
        if channel is not None:
            self._channels.move_to_end(clean_id)
            channel.updated_at = time.monotonic()
            return channel
        channel = _RunChannel(run_id=clean_id, buffer=deque(maxlen=self.max_buffer))
        self._channels[clean_id] = channel
        self._evict_idle_channels_locked(keep_run_id=clean_id)
        return channel

    def _evict_idle_channels_locked(self, *, keep_run_id: str) -> None:
        if len(self._channels) <= self.max_runs:
            return
        for cand_id in list(self._channels.keys()):
            if len(self._channels) <= self.max_runs:
                break
            if cand_id == keep_run_id:
                continue
            cand = self._channels[cand_id]
            if not cand.subscribers:
                self._channels.pop(cand_id, None)

    @staticmethod
    def _current_running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _dispatch_to_subscribers(
        self,
        subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any] | None]]],
        item: dict[str, Any] | None,
    ) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any] | None]]]:
        """在持锁临界区内向全部订阅者统一经 `call_soon_threadsafe` 投递事件或关闭哨兵，确保严格 FIFO 顺序。"""
        if not subscribers:
            return []
        running_loop = self._current_running_loop()
        if running_loop is not None and (self._loop is None or self._loop.is_closed()):
            self._loop = running_loop
        dead: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any] | None]]] = []
        for sub_loop, sub_queue in subscribers:
            if sub_loop.is_closed():
                dead.append((sub_loop, sub_queue))
                continue
            try:
                # 始终统一通过 call_soon_threadsafe 排入 sub_loop 就绪队列：
                # O(1) 非阻塞，且彻底消除同线程直写与跨线程回调混用导致的乱序越位或丢帧。
                sub_loop.call_soon_threadsafe(_enqueue_nowait_bounded, sub_queue, item)
            except RuntimeError:
                dead.append((sub_loop, sub_queue))
        return dead

    def _on_remote_envelope(self, run_id: str, envelope: dict[str, Any]) -> None:
        """接收外部传输层发来的远端节点事件：过滤本节点回声后仅写入本地通道并扇出，绝不二次外发。"""
        if not isinstance(envelope, dict):
            return
        origin = envelope.get("node_id")
        if isinstance(origin, str) and origin.strip() == self.node_id:
            return
        clean_id = str(run_id or envelope.get("run_id") or "").strip()
        if not clean_id:
            return
        ev_type = str(envelope.get("type") or "event").strip() or "event"
        raw_payload = envelope.get("payload")
        if isinstance(raw_payload, dict):
            ev_payload = dict(raw_payload)
        elif raw_payload is None:
            ev_payload = {}
        else:
            ev_payload = {"value": raw_payload}
        timestamp = str(envelope.get("timestamp") or _now_iso())

        with self._lock:
            channel = self._get_or_create_channel_locked(clean_id)
            if channel.closed:
                return
            channel.seq += 1
            local_envelope: dict[str, Any] = {
                "seq": channel.seq,
                "run_id": channel.run_id,
                "type": ev_type,
                "timestamp": timestamp,
                "payload": ev_payload,
            }
            if isinstance(origin, str) and origin.strip():
                local_envelope["node_id"] = origin.strip()
            channel.buffer.append(local_envelope)
            dead = self._dispatch_to_subscribers(list(channel.subscribers), local_envelope)
            for item in dead:
                channel.subscribers.discard(item)

    def publish(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发布一条标准化流式事件（事件循环协程与同步工作线程均可安全调用）。"""
        ev_type = str(event_type or "event").strip() or "event"
        if isinstance(payload, dict):
            ev_payload = dict(payload)
        elif payload is None:
            ev_payload = {}
        else:
            ev_payload = {"value": payload}

        with self._lock:
            channel = self._get_or_create_channel_locked(run_id)
            if channel.closed:
                # 已关闭通道绝不因迟到/出带 publish 隐式重开（防后续订阅者永久挂起）；
                # 仅当调用方显式调用 reopen_run(run_id) 后才恢复缓冲与扇出投递。
                return {
                    "seq": channel.seq + 1,
                    "run_id": channel.run_id,
                    "type": ev_type,
                    "timestamp": _now_iso(),
                    "payload": ev_payload,
                    "ignored": True,
                }
            channel.seq += 1
            envelope: dict[str, Any] = {
                "seq": channel.seq,
                "run_id": channel.run_id,
                "type": ev_type,
                "timestamp": _now_iso(),
                "payload": ev_payload,
            }
            channel.buffer.append(envelope)
            dead = self._dispatch_to_subscribers(list(channel.subscribers), envelope)
            for item in dead:
                channel.subscribers.discard(item)
            clean_run_id = channel.run_id

        if not self._transport_is_in_memory:
            wire_envelope = {**envelope, "node_id": self.node_id}
            try:
                self.transport.publish_external(clean_run_id, wire_envelope)
            except Exception as exc:
                log_structured(
                    "WARNING",
                    "stream_transport_publish_failed",
                    node_id=self.node_id,
                    run_id=clean_run_id,
                    error_type=type(exc).__name__,
                )
        return envelope

    def recent_events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """只读返回环形缓冲中 `seq > after_seq` 的近期事件快照。"""
        with self._lock:
            channel = self._channels.get(str(run_id or "").strip())
            if channel is None:
                return []
            return [dict(ev) for ev in channel.buffer if ev["seq"] > after_seq]

    def subscriber_count(self, run_id: str) -> int:
        with self._lock:
            channel = self._channels.get(str(run_id or "").strip())
            return len(channel.subscribers) if channel is not None else 0

    def reopen_run(self, run_id: str) -> None:
        """显式重新打开指定 run 的流通道（供多轮续聊或审批恢复使用）。"""
        with self._lock:
            channel = self._get_or_create_channel_locked(run_id)
            channel.closed = False

    def close_run(self, run_id: str) -> None:
        """关闭指定 run 的当前流订阅会话，向全部活跃订阅者发送 EOF 哨兵并清理句柄。"""
        clean_id = str(run_id or "").strip()
        if not clean_id:
            return
        with self._lock:
            channel = self._channels.get(clean_id)
            if channel is None:
                return
            channel.closed = True
            channel.updated_at = time.monotonic()
            subs_snapshot = list(channel.subscribers)
            channel.subscribers.clear()
            self._dispatch_to_subscribers(subs_snapshot, None)

    async def subscribe(
        self,
        run_id: str,
        after_seq: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """异步订阅指定 run 的事件流：先无损回放 `seq > after_seq` 的缓冲帧，再实时接收增量帧直至 `close_run`。"""
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError("after_seq 必须是非负整数")
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed():
            self._loop = loop
        sub_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=self.max_subscriber_queue
        )
        handle = (loop, sub_queue)

        with self._lock:
            channel = self._get_or_create_channel_locked(run_id)
            replay = [dict(ev) for ev in channel.buffer if ev["seq"] > after_seq]
            already_closed = channel.closed
            if not already_closed:
                channel.subscribers.add(handle)

        last_seq = after_seq
        try:
            for buffered_ev in replay:
                if buffered_ev["seq"] > last_seq:
                    last_seq = buffered_ev["seq"]
                    yield buffered_ev
            if already_closed:
                return
            while True:
                item = await sub_queue.get()
                if item is None:
                    break
                if item["seq"] > last_seq:
                    last_seq = item["seq"]
                    yield item
        finally:
            with self._lock:
                chan = self._channels.get(channel.run_id)
                if chan is not None:
                    chan.subscribers.discard(handle)

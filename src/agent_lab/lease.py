"""持久队列 Worker 执行器：领取任务、定时心跳续租、世代（epoch）隔离、跨节点工作区物化与出带 Dreaming。

核心分布式与并发不变量（技术面试重点）：
1. 为什么仅有 TTL 租约不够，必须配合单调递增 Fencing Token（epoch）：
   - 朴素做法只检查本地时钟未超时就允许写库/写盘，但进程可能遭遇 GC 停顿、SIGSTOP 或磁盘 I/O 假死；
   - Worker A（epoch = 1）卡死超过 lease_ttl 后，Worker B 接管并原子递增至 epoch = 2；
   - 当 A 恢复执行时，它自以为仍持有任务，但所有 SQLite 写事务（_verify_epoch_tx）和工作区补丁写入入口
     都会校验 expected_epoch == 当前数据库 epoch，立即抛出 StaleEpochError 将僵尸写拦截在门外。
2. 心跳主动熔断与优雅关机区分（fenced_by_heartbeat vs. worker_shutdown_requeued）：
   - 心跳协程一旦捕获 StaleEpochError（租约已被抢占），或遭遇 SQLite 锁繁忙且本地已越过 lease_deadline，
     立即置位 fenced_by_heartbeat 并 cancel 主执行协程，绝不再调用 release_lease 污染新 Owner 状态；
   - 若因进程收到 SIGTERM 被外部取消且未被抢占，则先同步归档快照再将租约归还为 queued，实现零丢失平滑重启。
3. 为什么会话后自动 Dreaming（auto_dream）必须推迟到归档同步、释放租约与退出文件锁之后：
   - 若在 _execute_unlocked 内部（释放租约和文件锁之前）直接运行耗时数秒的 LLM Dreaming：
     (a) 任务状态虽已落盘为 completed，但租约仍处于 claimed 且持有 exclusive_run 文件锁；用户立刻发起
         多轮追问（send_message / append_user_turn）时，要么被 RunBusy / run_currently_claimed 拒绝，
         要么递增 epoch 导致当前 Worker 随后的归档和 release_lease 触发 StaleEpochError 误报故障；
     (b) 若 Dreaming 期间网络抖动抛错，外层异常捕获会把已成功完成的编码任务错误覆写为 failed。
   - 因此 Worker 注入 defer_auto_dream=True，严格按“同步归档 -> release_lease -> 退出 exclusive_run 锁 ->
     后台独立 Runtime 出带执行 dream()”顺序推进；记忆写入并发由 MemoryStore 自身短临界区文件锁保证。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
from typing import Any, Callable
import urllib.parse
import uuid

from .execution_lock import RunBusy, exclusive_run
from .git_repo import (
    _rmtree_force,
    is_git_worktree,
    pack_workspace_archive,
    resolve_commit,
    unpack_workspace_archive,
)
from .observability import (
    METRICS,
    bind_trace_context,
    log_structured,
    sanitize_error_detail,
    start_span,
)
from .storage import PG_QUEUE_NOTIFY_CHANNEL, StaleEpochError, Storage


def _validate_pg_channel(channel: str) -> str:
    clean = str(channel or "").strip()
    if not clean or len(clean) > 63 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean):
        raise ValueError("pg_notify_channel_invalid")
    return clean


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class WorkerMetricsServer:
    """轻量级纯标准库 HTTP 服务，供独立队列 Worker 暴露 Prometheus `/metrics` 与 `/healthz`。"""

    def __init__(
        self,
        port: int = 0,
        host: str = "127.0.0.1",
        *,
        metrics: Any | None = None,
        worker_id: str | None = None,
        health_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("metrics_port_invalid")
        self._host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        self._configured_port = port
        self._metrics = metrics if metrics is not None else METRICS
        self._worker_id = worker_id
        self._health_provider = health_provider
        self._lock = threading.Lock()
        self._httpd: _ReusableThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        with self._lock:
            if self._httpd is not None:
                return int(self._httpd.server_address[1])
            return self._configured_port

    @property
    def address(self) -> tuple[str, int]:
        return (self._host, self.port)

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> int:
        """启动后台 HTTP 守护线程并返回实际监听端口（幂等）。"""
        with self._lock:
            if self._httpd is not None:
                return int(self._httpd.server_address[1])

            metrics_ref = self._metrics
            worker_id = self._worker_id
            health_provider = self._health_provider

            class _MetricsHandler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                    return

                def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self) -> None:  # noqa: N802
                    route = urllib.parse.urlsplit(self.path).path
                    if route == "/healthz":
                        payload = (
                            health_provider()
                            if health_provider is not None
                            else {"ok": True, "status": "alive", "worker_id": worker_id}
                        )
                        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        self._send_bytes(200, "application/json; charset=utf-8", body)
                        return
                    if route == "/metrics":
                        expected_tok = os.environ.get("AGENT_LAB_WORKER_METRICS_TOKEN", "").strip()
                        if expected_tok:
                            auth_header = self.headers.get("Authorization", "")
                            if not hmac.compare_digest(auth_header, f"Bearer {expected_tok}"):
                                err = json.dumps({"ok": False, "error": "unauthorized"}).encode("utf-8")
                                self._send_bytes(401, "application/json; charset=utf-8", err)
                                return
                        text = metrics_ref.render_prometheus()
                        self._send_bytes(
                            200,
                            "text/plain; version=0.0.4; charset=utf-8",
                            text.encode("utf-8"),
                        )
                        return
                    not_found = json.dumps({"ok": False, "error": "not_found"}).encode("utf-8")
                    self._send_bytes(404, "application/json; charset=utf-8", not_found)

            httpd = _ReusableThreadingHTTPServer((self._host, self._configured_port), _MetricsHandler)
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.2},
                name=f"agent-lab-worker-metrics-{worker_id or 'default'}",
                daemon=True,
            )
            thread.start()
            self._httpd = httpd
            self._thread = thread
            return int(httpd.server_address[1])

    def stop(self) -> None:
        """优雅关闭监听套接字并等待服务线程退出（幂等）。"""
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
        if httpd is not None:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> WorkerMetricsServer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.stop()
        return False


class LeaseWorker:
    """无状态持久队列消费者：只依赖 SQLite/PostgreSQL 中的任务配置、调用账本与工作区归档重建执行现场。

    任何进程内状态都不作为真值来源：Worker 随时可被 SIGKILL 终止并由新进程接管恢复。
    """

    def __init__(
        self,
        storage: Storage,
        runtime_factory: Callable[[dict, int], Any],
        worker_id: str | None = None,
        ttl_seconds: float = 15.0,
        heartbeat_interval: float | None = None,
        *,
        workspaces_dir: Path | None = None,
        metrics_port: int | None = None,
        metrics_host: str = "127.0.0.1",
        poll_interval: float = 0.8,
        pg_notify_channel: str = PG_QUEUE_NOTIFY_CHANNEL,
        enable_pg_listen: bool = True,
    ) -> None:
        self.storage = storage
        self.runtime_factory = runtime_factory
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.ttl_seconds = float(ttl_seconds)
        # 默认以 1/3 TTL 作为续租周期：允许单次 SQLite busy 锁抖动重试而不至于立刻被其它节点判定失联接管。
        self.heartbeat_interval = (
            float(heartbeat_interval)
            if heartbeat_interval is not None
            else max(0.1, round(self.ttl_seconds / 3.0, 3))
        )
        self.poll_interval = max(0.01, float(poll_interval))
        self.host = socket.gethostname()
        self.workspaces_dir = Path(workspaces_dir) if workspaces_dir is not None else None
        self._status = "idle"
        self._active_run_id: str | None = None
        self._active_epoch: int | None = None
        self._wakeup_event: asyncio.Event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pg_notify_channel: str = _validate_pg_channel(pg_notify_channel)
        self._enable_pg_listen: bool = bool(enable_pg_listen)
        self._pg_listen_active: bool = False
        self._metrics_server: WorkerMetricsServer | None = (
            WorkerMetricsServer(
                port=metrics_port,
                host=metrics_host,
                worker_id=self.worker_id,
                health_provider=self._health_payload,
            )
            if metrics_port is not None
            else None
        )

    def _health_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": self._status,
            "worker_id": self.worker_id,
            "host": self.host,
            "pid": os.getpid(),
            "active_run_id": self._active_run_id,
            "active_epoch": self._active_epoch,
            "pg_listen_active": self._pg_listen_active,
        }

    def notify_wakeup(self) -> None:
        """线程安全地唤醒处于空闲等待中的队列消费循环（支持本机推送与 PostgreSQL LISTEN/NOTIFY）。"""
        running_loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            running_loop = asyncio.get_running_loop()
        target_loop = (
            self._loop
            if self._loop is not None and not self._loop.is_closed()
            else running_loop
        )
        if target_loop is not None and target_loop is not running_loop and target_loop.is_running():
            with contextlib.suppress(RuntimeError):
                target_loop.call_soon_threadsafe(self._wakeup_event.set)
            return
        self._wakeup_event.set()

    @property
    def metrics_server(self) -> WorkerMetricsServer | None:
        return self._metrics_server

    @property
    def metrics_port(self) -> int | None:
        if self._metrics_server is None:
            return None
        return self._metrics_server.port

    def start_metrics_server(self) -> int | None:
        if self._metrics_server is None:
            return None
        return self._metrics_server.start()

    def stop_metrics_server(self) -> None:
        if self._metrics_server is not None:
            self._metrics_server.stop()

    async def process_one(self, run_id: str | None = None) -> dict[str, Any] | None:
        """尝试原子领取并执行一个待处理或租约过期的队列任务；队列为空时更新节点为 idle 并返回 None。"""
        claim = self.storage.claim_next_run(self.worker_id, ttl_seconds=self.ttl_seconds, run_id=run_id)
        if claim is None:
            self._status = "idle"
            self._active_run_id = None
            self._active_epoch = None
            METRICS.set_gauge("agent_lab_worker_busy", 0, {"worker_id": self.worker_id})
            self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
            return None

        claimed_run_id = claim["run_id"]
        epoch = claim["epoch"]
        action = claim["action"]
        current_lease_deadline = float(claim["lease_until"])
        self._status = "busy"
        self._active_run_id = claimed_run_id
        self._active_epoch = epoch
        METRICS.set_gauge("agent_lab_worker_busy", 1, {"worker_id": self.worker_id})
        METRICS.inc(
            "agent_lab_worker_claims_total",
            {
                "worker_id": self.worker_id,
                "action": action,
                "takeover": str(bool(claim.get("takeover"))).lower(),
            },
        )
        self.storage.update_worker_node(
            self.worker_id,
            os.getpid(),
            self.host,
            status="busy",
            active_run_id=claimed_run_id,
            active_epoch=epoch,
        )
        run = self.storage.get_run(claimed_run_id)
        if run is None:
            with contextlib.suppress(StaleEpochError):
                self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state="failed")
            self._status = "idle"
            self._active_run_id = None
            self._active_epoch = None
            METRICS.set_gauge("agent_lab_worker_busy", 0, {"worker_id": self.worker_id})
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
            return None

        run_meta = run.get("metadata") or {}
        trace_ref = run_meta.get("traceparent") or run_meta.get("trace_id") or claimed_run_id
        try:
            # 同机防重双保险：数据库租约拦截逻辑并发，OS 级 flock 拦截本机 CLI/Worker 重叠退出窗口。
            # 若使用远程 PostgreSQL 且本地锚点路径不支持文件锁，优雅退化为空上下文，由 DB epoch 兜底。
            lock_cm = exclusive_run(self.storage.path, claimed_run_id)
        except Exception:
            lock_cm = contextlib.nullcontext()

        t_run = time.perf_counter()
        with bind_trace_context(trace_ref):
            try:
                with (
                    start_span(
                        "lease_worker.process_run",
                        {
                            "run_id": claimed_run_id,
                            "worker_id": self.worker_id,
                            "epoch": epoch,
                            "action": action,
                            "takeover": bool(claim.get("takeover", False)),
                        },
                    ),
                    lock_cm,
                ):
                    outcome = await self._run_claimed_under_lock(
                        claim=claim,
                        run=run,
                        claimed_run_id=claimed_run_id,
                        epoch=epoch,
                        action=action,
                        initial_deadline=current_lease_deadline,
                    )
            except RunBusy:
                # 另一本机进程（例如前一个被取消但同步子线程仍在收尾的实例）尚未释放 flock：
                # 不能将任务标为 failed，而是把租约退回 queued 等待下一轮轮询安全重试。
                with contextlib.suppress(StaleEpochError):
                    self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state="queued")
                with contextlib.suppress(Exception):
                    self.storage.add_event(
                        claimed_run_id,
                        "worker_busy_requeued",
                        {"worker_id": self.worker_id, "epoch": epoch},
                    )
                self._status = "idle"
                self._active_run_id = None
                self._active_epoch = None
                METRICS.set_gauge("agent_lab_worker_busy", 0, {"worker_id": self.worker_id})
                with contextlib.suppress(Exception):
                    self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
                return {"ok": False, "code": "run_busy", "claim": claim}
            finally:
                self._status = "idle"
                self._active_run_id = None
                self._active_epoch = None
                METRICS.set_gauge("agent_lab_worker_busy", 0, {"worker_id": self.worker_id})

            elapsed_ms = (time.perf_counter() - t_run) * 1000.0
            outcome_status = (
                str(outcome["run"].get("status", "unknown"))
                if isinstance(outcome, dict) and isinstance(outcome.get("run"), dict)
                else str(outcome.get("code", "unknown") if isinstance(outcome, dict) else "unknown")
            )
            METRICS.inc(
                "agent_lab_worker_runs_total",
                {"worker_id": self.worker_id, "status": outcome_status},
            )
            METRICS.observe_ms(
                "agent_lab_worker_run_duration_ms",
                elapsed_ms,
                {"worker_id": self.worker_id, "action": action},
            )

            # 关键时序不变量：此时 _run_claimed_under_lock 已完成 _sync_workspace_archive 与 release_lease，
            # 且已退出上方 with lock_cm（释放了 exclusive_run 内核文件锁）。
            # 在此之后再用新 Runtime 实例出带运行 auto_dream，并保持原 W3C Trace Context 贯通：
            # 1. 即使人类用户在完成瞬间立即发起第 2 轮 send_message 续聊并拿到 epoch+1，
            #    也不会与 Dreaming 互锁或触发 StaleEpochError；
            # 2. Dreaming 采用“无锁 LLM 提炼 + 短时间局部记忆文件锁原子落盘”的三阶段协议，
            #    异常全部静默吞掉，绝不污染已完成的主任务状态。
            finished_run = outcome.get("run") if isinstance(outcome, dict) else None
            if (
                isinstance(finished_run, dict)
                and finished_run.get("status") == "completed"
                and bool((finished_run.get("metadata") or {}).get("auto_dream", True))
            ):
                dream_rt: Any = None
                with (
                    contextlib.suppress(BaseException),
                    start_span(
                        "lease_worker.auto_dream",
                        {"run_id": claimed_run_id, "worker_id": self.worker_id, "epoch": epoch},
                    ),
                ):
                    dream_rt = self.runtime_factory(finished_run, epoch)
                    await dream_rt.dream(run_id=claimed_run_id, trigger="auto_post_session")
                if dream_rt is not None:
                    with contextlib.suppress(BaseException):
                        await dream_rt.aclose()
        return outcome

    async def _materialize_workspace_if_needed(
        self, run: dict[str, Any], claimed_run_id: str, epoch: int
    ) -> bool:
        """跨节点故障接管时，若当前节点本地不存在原工作区目录，则从数据库归档快照解压还原并重绑路径。"""
        meta = run.setdefault("metadata", {})
        ws_raw = meta.get("workspace")
        ws_path = Path(ws_raw) if ws_raw else None
        archive_doc = self.storage.get_workspace_archive(claimed_run_id)
        had_archive = archive_doc is not None
        if ws_path is None or not ws_path.exists():
            if archive_doc is not None:
                base_ws_dir = (
                    self.workspaces_dir
                    if self.workspaces_dir is not None
                    else Path(
                        os.environ.get("AGENT_LAB_WORKER_WORKSPACES_DIR")
                        or (self.storage.path.parent / "worker_workspaces" / self.worker_id)
                    )
                )
                local_ws = base_ws_dir / claimed_run_id
                if local_ws.exists():
                    _rmtree_force(local_ws)
                unpack_workspace_archive(archive_doc["archive_bytes"], local_ws)
                meta["workspace"] = str(local_ws.resolve())
                self.storage.save_run(run, expected_epoch=epoch)
                self.storage.add_event(
                    claimed_run_id,
                    "workspace_materialized",
                    {
                        "worker_id": self.worker_id,
                        "epoch": epoch,
                        "archive_epoch": archive_doc["epoch"],
                    },
                )
        return had_archive

    def _sync_workspace_archive(self, runtime: Any, claimed_run_id: str, epoch: int) -> None:
        """将托管隔离工作区打包写回数据库归档表；本机直连工作区（direct_workspace）严禁归档。

        为什么 direct_workspace 必须跳过归档：
        直连模式直接操作用户真实大型代码库（可能包含海量依赖或未提交大文件），打包进 SQLite/PG BLOB
        不仅会导致数据库体积爆炸，还会在恢复解压时用旧快照倒灌覆盖用户在宿主编辑器中的最新修改。
        """
        if runtime is None or not runtime.workspace.is_dir():
            return
        stored = self.storage.get_run(claimed_run_id)
        if stored and (stored.get("metadata") or {}).get("direct_workspace"):
            return
        git_head = (
            resolve_commit(runtime.workspace, "HEAD")
            if is_git_worktree(runtime.workspace)
            else None
        )
        archive_bytes = pack_workspace_archive(runtime.workspace)
        self.storage.save_workspace_archive(
            claimed_run_id, epoch, archive_bytes, git_head=git_head
        )
    async def _run_claimed_under_lock(
        self,
        *,
        claim: dict[str, Any],
        run: dict[str, Any],
        claimed_run_id: str,
        epoch: int,
        action: str,
        initial_deadline: float,
    ) -> dict[str, Any]:
        """在持有内核锁与有效 epoch 租约的前提下推进任务，同时运行后台心跳协程监控抢占。"""
        stop_heartbeat = asyncio.Event()
        exec_task: asyncio.Task | None = None
        runtime: Any = None
        had_archive = False
        fenced_by_heartbeat = False
        current_lease_deadline = initial_deadline

        async def heartbeat_loop() -> None:
            nonlocal current_lease_deadline, fenced_by_heartbeat
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=self.heartbeat_interval)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    hb = self.storage.heartbeat_lease(
                        claimed_run_id, self.worker_id, epoch, ttl_seconds=self.ttl_seconds
                    )
                    current_lease_deadline = float(hb["lease_until"])
                    with contextlib.suppress(sqlite3.OperationalError):
                        self.storage.update_worker_node(
                            self.worker_id,
                            os.getpid(),
                            self.host,
                            status="busy",
                            active_run_id=claimed_run_id,
                            active_epoch=epoch,
                        )
                except StaleEpochError:
                    # 租约世代已落后（另一节点完成接管）：主动取消本地推理协程止损。
                    fenced_by_heartbeat = True
                    if exec_task is not None and not exec_task.done():
                        exec_task.cancel()
                    return
                except sqlite3.OperationalError as exc:
                    log_structured(
                        "WARNING",
                        "heartbeat_operational_error",
                        worker_id=self.worker_id,
                        run_id=claimed_run_id,
                        epoch=epoch,
                        error_type=type(exc).__name__,
                    )
                    # 若因数据库锁阻塞导致本地时间已超过上次获批的租约截止期，其他 Worker 随时可能接管，
                    # 必须按被抢占处理（保守自杀策略），防止脑裂双写。
                    if time.time() > current_lease_deadline:
                        fenced_by_heartbeat = True
                        if exec_task is not None and not exec_task.done():
                            exec_task.cancel()
                        return

        hb_task = asyncio.create_task(heartbeat_loop())
        try:
            had_archive = await self._materialize_workspace_if_needed(run, claimed_run_id, epoch)
            runtime = self.runtime_factory(run, epoch)
            coro = (
                runtime._resume_unlocked(claimed_run_id)
                if action == "resume"
                else runtime._execute_unlocked(claimed_run_id)
            )
            exec_task = asyncio.create_task(coro)
            result = await exec_task
            # 必须在释放租约前以当前有效 epoch 同步工作区归档；否则新世代一旦开启，旧归档会被拒绝。
            if os.environ.get("AGENT_LAB_SYNC_WORKSPACE_ARCHIVE") == "1" or had_archive:
                self._sync_workspace_archive(runtime, claimed_run_id, epoch)
            final_state = (
                result["status"]
                if result["status"]
                in {
                    "waiting_approval",
                    "completed",
                    "failed",
                    "budget_exceeded",
                    "cancelled",
                    "interrupted",
                }
                else "idle"
            )
            self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state=final_state)
            return {"ok": True, "claim": claim, "run": result}
        except StaleEpochError:
            METRICS.inc("agent_lab_worker_fenced_total", {"worker_id": self.worker_id})
            with contextlib.suppress(Exception):
                self.storage.add_event(
                    claimed_run_id,
                    "worker_fenced",
                    {"worker_id": self.worker_id, "stale_epoch": epoch},
                )
            return {"ok": False, "code": "fenced_out", "claim": claim}
        except asyncio.CancelledError:
            if exec_task is not None and not exec_task.done():
                exec_task.cancel()
                with contextlib.suppress(BaseException):
                    await exec_task
            # 区分“因心跳检测到租约被抢占而主动 cancel”与“进程优雅退出收到外部 CancelledError”：
            # 前者不能重新入队或冒泡异常终止 Worker 主循环；后者需归还租约并继续向上传播取消信号。
            if fenced_by_heartbeat:
                METRICS.inc("agent_lab_worker_fenced_total", {"worker_id": self.worker_id})
                with contextlib.suppress(Exception):
                    self.storage.add_event(
                        claimed_run_id,
                        "worker_fenced",
                        {"worker_id": self.worker_id, "stale_epoch": epoch},
                    )
                return {"ok": False, "code": "fenced_out", "claim": claim}
            if os.environ.get("AGENT_LAB_SYNC_WORKSPACE_ARCHIVE") == "1" or had_archive:
                with contextlib.suppress(Exception):
                    self._sync_workspace_archive(runtime, claimed_run_id, epoch)
            with contextlib.suppress(StaleEpochError):
                self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state="queued")
            with contextlib.suppress(Exception):
                self.storage.add_event(
                    claimed_run_id,
                    "worker_shutdown_requeued",
                    {"worker_id": self.worker_id, "epoch": epoch},
                )
            raise
        except Exception as exc:
            detail = sanitize_error_detail(exc)
            with contextlib.suppress(StaleEpochError, KeyError):
                failed_run = self.storage.get_run(claimed_run_id) or run
                failed_run["status"] = "failed"
                failed_run["error"] = type(exc).__name__
                failed_run.setdefault("metadata", {})["error_detail"] = detail
                self.storage.save_run(failed_run, expected_epoch=epoch)
            with contextlib.suppress(Exception):
                self.storage.add_event(
                    claimed_run_id,
                    "failure",
                    {"worker_id": self.worker_id, "epoch": epoch, **detail},
                )
            with contextlib.suppress(StaleEpochError):
                self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state="failed")
            return {
                "ok": False,
                "code": "worker_bootstrap_failed",
                "error": type(exc).__name__,
                "claim": claim,
            }
        finally:
            stop_heartbeat.set()
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            if runtime is not None:
                with contextlib.suppress(Exception):
                    await runtime.aclose()
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")

    async def _wait_for_wakeup_or_stop(
        self, stopper: asyncio.Event, timeout: float
    ) -> bool:
        """等待推送唤醒、停止事件或超时；返回是否由 `_wakeup_event` 提前中断空闲等待。"""
        running_loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed():
            self._loop = running_loop
        if stopper.is_set():
            return False
        if self._wakeup_event.is_set():
            self._wakeup_event.clear()
            return True
        stop_task = asyncio.create_task(stopper.wait())
        wake_task = asyncio.create_task(self._wakeup_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, wake_task},
                timeout=max(0.0, float(timeout)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            woken = wake_task in done and not stop_task.done()
            if woken:
                self._wakeup_event.clear()
            return woken
        except BaseException:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            raise

    async def _pg_listen_loop(self, stopper: asyncio.Event) -> None:
        """PostgreSQL `LISTEN agent_lab_queue` 后台监听协程：收到通知立刻唤醒主循环，不可用时优雅降级为自适应轮询。"""
        pg_config = getattr(self.storage, "pg_config", None)
        if pg_config is None or not self._enable_pg_listen:
            return
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError:
            return
        async_conn_cls = getattr(psycopg, "AsyncConnection", None)
        if async_conn_cls is None or not hasattr(async_conn_cls, "connect"):
            return

        backoff = 0.5
        while not stopper.is_set():
            aconn: Any = None
            try:
                aconn = await async_conn_cls.connect(
                    pg_config.dsn,
                    autocommit=True,
                    connect_timeout=pg_config.connect_timeout,
                    application_name=f"{pg_config.application_name}-listen",
                )
                if not callable(getattr(aconn, "notifies", None)):
                    return
                await aconn.execute(f"LISTEN {self._pg_notify_channel}")
                self._pg_listen_active = True
                backoff = 0.5
                # 订阅建立后主动触发一次唤醒，弥合建连窗口内已提交入队的通知空洞
                self.notify_wakeup()
                while not stopper.is_set():
                    try:
                        notify_iter = aconn.notifies(timeout=1.0)
                        supports_timeout = True
                    except TypeError:
                        notify_iter = aconn.notifies()
                        supports_timeout = False
                    got_any = False
                    t_iter = time.monotonic()
                    async for _notify in notify_iter:
                        got_any = True
                        self.notify_wakeup()
                        if stopper.is_set():
                            break
                    if not supports_timeout:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(stopper.wait(), timeout=backoff)
                        break
                    if not got_any and (time.monotonic() - t_iter) < 0.05:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(stopper.wait(), timeout=0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_structured(
                    "WARNING",
                    "pg_listen_degraded",
                    worker_id=self.worker_id,
                    channel=self._pg_notify_channel,
                    error_type=type(exc).__name__,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stopper.wait(), timeout=backoff)
                backoff = min(10.0, backoff * 2.0)
            finally:
                self._pg_listen_active = False
                if aconn is not None:
                    with contextlib.suppress(Exception):
                        await aconn.close()

    async def serve_forever(
        self, poll_interval: float | None = None, stop_event: asyncio.Event | None = None
    ) -> None:
        """常驻消费持久队列：有任务零延迟续领，空闲时支持事件推送/PG LISTEN 即时唤醒与自适应回退轮询。"""
        base_interval = max(
            0.01, float(self.poll_interval if poll_interval is None else poll_interval)
        )
        fast_interval = min(base_interval, max(0.05, base_interval * 0.25))
        current_wait = base_interval
        stopper = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        self._loop = loop
        started_metrics_here = False
        if self._metrics_server is not None and not self._metrics_server.is_running:
            self._metrics_server.start()
            started_metrics_here = True
        self._status = "idle"
        METRICS.set_gauge("agent_lab_worker_busy", 0, {"worker_id": self.worker_id})
        self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
        pg_listen_task: asyncio.Task[None] | None = None
        if getattr(self.storage, "pg_config", None) is not None and self._enable_pg_listen:
            pg_listen_task = asyncio.create_task(self._pg_listen_loop(stopper))
        try:
            while not stopper.is_set():
                # 在查库前先清零唤醒标志，杜绝“查库返回空后、进入等待前”的并发唤醒丢失窗口
                self._wakeup_event.clear()
                handled = await self.process_one()
                if handled is not None:
                    current_wait = fast_interval
                    continue
                if stopper.is_set():
                    break
                wait_budget = base_interval if self._pg_listen_active else current_wait
                woken = await self._wait_for_wakeup_or_stop(stopper, wait_budget)
                if woken:
                    current_wait = fast_interval
                elif not self._pg_listen_active:
                    current_wait = min(base_interval, current_wait * 1.5)
        finally:
            if pg_listen_task is not None:
                pg_listen_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pg_listen_task
            self._pg_listen_active = False
            if self._loop is loop:
                self._loop = None
            self._status = "stopped"
            self._active_run_id = None
            self._active_epoch = None
            METRICS.set_gauge("agent_lab_worker_busy", 0, {"worker_id": self.worker_id})
            if started_metrics_here and self._metrics_server is not None:
                self._metrics_server.stop()
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(
                    self.worker_id, os.getpid(), self.host, status="stopped"
                )


def build_worker_runtime_factory(root_dir: Path, storage: Storage) -> Callable[[dict, int], Any]:
    """根据任务持久化的元数据重建与工作区、配额、权限档位和世代绑定的 AgentRuntime。

    注意此处固定传入 defer_auto_dream=True：禁止 AgentRuntime._execute_unlocked 在持锁期内直接跑
    Dreaming，统一交由 LeaseWorker.process_one 在完成归档、释放租约并退出文件锁之后出带调度。
    """
    from .runtime import AgentRuntime

    def factory(run: dict, epoch: int) -> AgentRuntime:
        meta = run.get("metadata", {})
        return AgentRuntime(
            storage=storage,
            workspace=Path(meta["workspace"]),
            knowledge_dir=root_dir / "data/knowledge",
            skills_dir=root_dir / "skills",
            provider=run["provider"],
            max_steps=meta.get("max_steps", 12),
            max_tokens=meta.get("max_tokens", 30000),
            context_chars=meta.get("context_chars", 24000),
            max_output_tokens=meta.get("max_output_tokens", 4096),
            expected_epoch=epoch,
            max_repair_rounds=meta.get("max_repair_rounds", 2),
            commit_on_pass=bool(meta.get("commit_on_pass", False)),
            auto_approve_proposals=bool(meta.get("auto_approve_proposals", False)),
            execution_profile=meta.get("execution_profile", "sandbox"),
            permission_mode=meta.get("permission_mode"),
            enable_gui=bool(meta.get("enable_gui", False)),
            mcp_config=meta.get("mcp_config"),
            enable_agent_memory=meta.get("enable_agent_memory"),
            auto_dream=bool(meta.get("auto_dream", True)),
            defer_auto_dream=True,
        )

    return factory

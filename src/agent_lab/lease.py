"""持久队列 Worker 执行器：领取任务、定时心跳续租、世代（epoch）隔离与故障接管。

设计验证目标（对应多 Worker 故障接管核心不变量）：
1. Worker A 领取任务获得 epoch = 1；若 A 卡死/暂停超过 lease_ttl，Worker B 可接管并获得 epoch = 2。
2. Worker B 推进并提交新状态后，即使 Worker A 从暂停中恢复，A 携带的旧 epoch = 1 会在所有
   SQLite 事务和工作区写入入口触发 StaleEpochError，绝不覆盖 B 的结果。
3. 心跳协程若检测到租约被抢占，会主动取消本地已过期的模型等待任务，尽快释放本地计算资源。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import socket
import sqlite3
import time
import uuid
from typing import Any, Callable

from .execution_lock import RunBusy, exclusive_run
from .git_repo import (
    _rmtree_force,
    is_git_worktree,
    pack_workspace_archive,
    resolve_commit,
    unpack_workspace_archive,
)
from .observability import METRICS, bind_trace_context, log_structured, sanitize_error_detail
from .storage import StaleEpochError, Storage


class LeaseWorker:
    """独立任务队列消费者：只依赖持久化在 SQLite 中的任务配置、账本和工作区元数据。"""

    def __init__(
        self,
        storage: Storage,
        runtime_factory: Callable[[dict, int], Any],
        worker_id: str | None = None,
        ttl_seconds: float = 15.0,
        heartbeat_interval: float | None = None,
        *,
        workspaces_dir: Path | None = None,
    ) -> None:
        self.storage = storage
        self.runtime_factory = runtime_factory
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.ttl_seconds = float(ttl_seconds)
        self.heartbeat_interval = (
            float(heartbeat_interval)
            if heartbeat_interval is not None
            else max(0.1, round(self.ttl_seconds / 3.0, 3))
        )
        self.host = socket.gethostname()
        self.workspaces_dir = Path(workspaces_dir) if workspaces_dir is not None else None

    async def process_one(self, run_id: str | None = None) -> dict[str, Any] | None:
        """尝试领取并执行一个队列任务；无任务可领时返回 None。"""
        claim = self.storage.claim_next_run(self.worker_id, ttl_seconds=self.ttl_seconds, run_id=run_id)
        if claim is None:
            self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
            return None

        claimed_run_id = claim["run_id"]
        epoch = claim["epoch"]
        action = claim["action"]
        current_lease_deadline = float(claim["lease_until"])
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
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
            return None

        trace_ref = (run.get("metadata") or {}).get("traceparent") or claimed_run_id
        try:
            lock_cm = exclusive_run(self.storage.path, claimed_run_id)
        except Exception:
            lock_cm = contextlib.nullcontext()

        try:
            with bind_trace_context(trace_ref), lock_cm:
                outcome = await self._run_claimed_under_lock(
                    claim=claim,
                    run=run,
                    claimed_run_id=claimed_run_id,
                    epoch=epoch,
                    action=action,
                    initial_deadline=current_lease_deadline,
                )
        except RunBusy:
            with contextlib.suppress(StaleEpochError):
                self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state="queued")
            with contextlib.suppress(Exception):
                self.storage.add_event(
                    claimed_run_id,
                    "worker_busy_requeued",
                    {"worker_id": self.worker_id, "epoch": epoch},
                )
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
            return {"ok": False, "code": "run_busy", "claim": claim}

        # 租约释放与文件锁退出后，在后台出带执行已完成会话的自动 Dreaming，绝不阻塞或干扰后续多轮续聊租约
        finished_run = outcome.get("run") if isinstance(outcome, dict) else None
        if (
            isinstance(finished_run, dict)
            and finished_run.get("status") == "completed"
            and bool((finished_run.get("metadata") or {}).get("auto_dream", True))
        ):
            dream_rt: Any = None
            with contextlib.suppress(BaseException):
                dream_rt = self.runtime_factory(finished_run, epoch)
                await dream_rt.dream(run_id=claimed_run_id, trigger="auto_post_session")
            if dream_rt is not None:
                with contextlib.suppress(BaseException):
                    await dream_rt.aclose()
        return outcome

    async def _materialize_workspace_if_needed(
        self, run: dict[str, Any], claimed_run_id: str, epoch: int
    ) -> bool:
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

    async def serve_forever(
        self, poll_interval: float = 0.8, stop_event: asyncio.Event | None = None
    ) -> None:
        """常驻轮询消费持久队列，直到收到停止信号。"""
        stopper = stop_event or asyncio.Event()
        self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
        try:
            while not stopper.is_set():
                handled = await self.process_one()
                if handled is not None:
                    continue
                try:
                    await asyncio.wait_for(stopper.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(
                    self.worker_id, os.getpid(), self.host, status="stopped"
                )


def build_worker_runtime_factory(root_dir: Path, storage: Storage) -> Callable[[dict, int], Any]:
    """根据任务持久化的元数据重建与工作区、配额和世代绑定的 AgentRuntime。"""
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

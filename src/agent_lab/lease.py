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
import uuid
from typing import Any, Callable

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

    async def process_one(self, run_id: str | None = None) -> dict[str, Any] | None:
        """尝试领取并执行一个队列任务；无任务可领时返回 None。"""
        claim = self.storage.claim_next_run(self.worker_id, ttl_seconds=self.ttl_seconds, run_id=run_id)
        if claim is None:
            self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
            return None

        claimed_run_id = claim["run_id"]
        epoch = claim["epoch"]
        action = claim["action"]
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
            return None

        runtime = self.runtime_factory(run, epoch)
        stop_heartbeat = asyncio.Event()
        exec_task: asyncio.Task | None = None

        async def heartbeat_loop() -> None:
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=self.heartbeat_interval)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    self.storage.heartbeat_lease(
                        claimed_run_id, self.worker_id, epoch, ttl_seconds=self.ttl_seconds
                    )
                    self.storage.update_worker_node(
                        self.worker_id,
                        os.getpid(),
                        self.host,
                        status="busy",
                        active_run_id=claimed_run_id,
                        active_epoch=epoch,
                    )
                except StaleEpochError:
                    if exec_task is not None and not exec_task.done():
                        exec_task.cancel()
                    return

        hb_task = asyncio.create_task(heartbeat_loop())
        try:
            coro = (
                runtime._resume_unlocked(claimed_run_id)
                if action == "resume"
                else runtime._execute_unlocked(claimed_run_id)
            )
            exec_task = asyncio.create_task(coro)
            result = await exec_task
            final_state = (
                result["status"]
                if result["status"] in {"waiting_approval", "completed", "failed", "budget_exceeded"}
                else "idle"
            )
            self.storage.release_lease(claimed_run_id, self.worker_id, epoch, final_state=final_state)
            return {"ok": True, "claim": claim, "run": result}
        except (StaleEpochError, asyncio.CancelledError):
            self.storage.add_event(
                claimed_run_id,
                "worker_fenced",
                {"worker_id": self.worker_id, "stale_epoch": epoch},
            )
            return {"ok": False, "code": "fenced_out", "claim": claim}
        finally:
            stop_heartbeat.set()
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            await runtime.aclose()
            with contextlib.suppress(Exception):
                self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")

    async def serve_forever(
        self, poll_interval: float = 0.8, stop_event: asyncio.Event | None = None
    ) -> None:
        """常驻轮询消费持久队列，直到收到停止信号。"""
        stopper = stop_event or asyncio.Event()
        self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="idle")
        while not stopper.is_set():
            handled = await self.process_one()
            if handled is not None:
                continue
            try:
                await asyncio.wait_for(stopper.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue
        self.storage.update_worker_node(self.worker_id, os.getpid(), self.host, status="stopped")


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
            expected_epoch=epoch,
            max_repair_rounds=meta.get("max_repair_rounds", 2),
            commit_on_pass=bool(meta.get("commit_on_pass", False)),
            auto_approve_proposals=bool(meta.get("auto_approve_proposals", False)),
        )

    return factory

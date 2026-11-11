"""SQLite 持久化、任务队列租约与单调递增 Fencing Token。

模型只看到有界上下文；数据库保留完整轨迹、待审批补丁集、调用账本、跨会话笔记
以及本机多 Worker 协调所需的队列租约与递增世代（epoch / fencing token）。
此存储可在同一主机共享 SQLite WAL 的多个 Worker 进程间实现过期接管与过期写拦截；
跨多台物理机器的高可用仍需替换为 PostgreSQL 等网络事务存储。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StaleEpochError(RuntimeError):
    """旧 Worker 持有的 fencing token 已过期，拒绝写入状态、工具账本或补丁产物。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def canonical_json(value: Any) -> str:
    """排序所有对象键后序列化，参数顺序变化不会改变一次调用的身份。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(content: str) -> str:
    """对 UTF-8 原文取摘要；审批绑定此版本，避免批准后覆盖他人的修改。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Storage:
    """一个文件保存状态；写入事务用 WAL 并限制锁等待时间。"""

    def __init__(self, db_path: str | Path):
        # :memory: 每个连接是一份独立数据库，不适合本实现的连接生命周期。
        if str(db_path) == ":memory:":
            raise ValueError("请使用临时目录中的 SQLite 文件，不能使用 :memory:")
        configured = Path(db_path)
        # 配置目录规范化一次，兼容 macOS /var -> /private/var 等系统路径别名。
        # 保留末端文件名，让执行锁仍能拒绝数据库文件本身的 symlink。
        self.path = configured.parent.resolve() / configured.name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 多个线程/进程在 Barrier 同刻首次将 DELETE 日志切换为 WAL 时，SQLite 死锁检测可能
        # 直接返回 database is locked 而不等待 busy_timeout；启动迁移做有界退避重试。
        for attempt in range(8):
            try:
                self._initialize_schema()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _initialize_schema(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS memories (
                    scope TEXT NOT NULL, key TEXT NOT NULL,
                    value TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope, key)
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proposals_run ON proposals(run_id);
                CREATE TABLE IF NOT EXISTS tool_calls (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    call_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'scheduled', 'running', 'succeeded', 'failed', 'waiting_approval', 'unknown'
                    )),
                    created_at TEXT NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY(run_id, call_id)
                );
                CREATE INDEX IF NOT EXISTS tool_calls_run_status ON tool_calls(run_id, status);
                CREATE TABLE IF NOT EXISTS run_leases (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),
                    queue_state TEXT NOT NULL CHECK(queue_state IN (
                        'idle', 'queued', 'claimed', 'waiting_approval', 'completed', 'failed', 'budget_exceeded'
                    )),
                    action TEXT NOT NULL DEFAULT 'execute',
                    owner_id TEXT,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL,
                    heartbeat_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_leases_claimable ON run_leases(queue_state, lease_until);
                CREATE TABLE IF NOT EXISTS worker_nodes (
                    worker_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    host TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active_run_id TEXT,
                    active_epoch INTEGER,
                    status TEXT NOT NULL
                );
                """
            )
            # 给已有 v0.1 数据库做加法迁移，不重建、不删除已有审批对象。
            # 列检查与 ALTER 同事务排他，避免两个启动者都看到“缺列”后重复迁移。
            con.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in con.execute("PRAGMA table_info(proposals)")}
            if "operation_id" not in columns:
                con.execute("ALTER TABLE proposals ADD COLUMN operation_id TEXT")
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS proposals_operation "
                "ON proposals(run_id, operation_id) WHERE operation_id IS NOT NULL"
            )

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        try:
            with con:
                yield con
        finally:
            con.close()

    def create_run(self, prompt: str, mode: str = "agent", provider: str = "demo") -> dict:
        now = _now()
        run = {
            "id": uuid.uuid4().hex,
            "prompt": prompt,
            "mode": mode,
            "provider": provider,
            "status": "queued",
            "answer": "",
            "error": None,
            "messages": [],
            "steps": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "created_at": now,
            "updated_at": now,
            "metadata": {},
        }
        with self._connect() as con:
            con.execute("INSERT INTO runs VALUES (?, ?, ?)", (run["id"], now, _dump(run)))
            con.execute(
                "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, lease_until, heartbeat_at, updated_at) "
                "VALUES (?, 'idle', 'execute', NULL, 0, NULL, NULL, ?)",
                (run["id"], now),
            )
        return run

    @staticmethod
    def _verify_epoch_tx(con: sqlite3.Connection, run_id: str, expected_epoch: int | None) -> None:
        if expected_epoch is None:
            return
        row = con.execute(
            "SELECT epoch, queue_state, owner_id FROM run_leases WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row["epoch"] != expected_epoch or row["queue_state"] != "claimed":
            actual = row["epoch"] if row is not None else None
            state = row["queue_state"] if row is not None else None
            raise StaleEpochError(
                f"fencing_token_rejected: run={run_id} expected_epoch={expected_epoch} actual_epoch={actual} state={state}"
            )

    def verify_epoch(self, run_id: str, expected_epoch: int | None) -> None:
        """在工作区写文件或 Git 提交前校验当前执行世代仍有效。"""
        if expected_epoch is None:
            return
        with self._connect() as con:
            self._verify_epoch_tx(con, run_id, expected_epoch)

    def enqueue_run(self, run_id: str, action: str = "execute") -> dict:
        """将任务放入持久队列等待独立 Worker 领取；若已在排队则保持幂等。"""
        if action not in {"execute", "resume"}:
            raise ValueError("queue_action_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
            row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO run_leases VALUES (?, 'queued', ?, NULL, 0, NULL, NULL, ?)",
                    (run_id, action, now),
                )
            else:
                # 如果另一个存活 Worker 仍在有效租约期内持有任务，拒绝重复入队覆盖。
                if row["queue_state"] == "claimed" and (row["lease_until"] or 0.0) > time.time():
                    raise ValueError("run_currently_claimed")
                con.execute(
                    "UPDATE run_leases SET queue_state='queued', action=?, owner_id=NULL, lease_until=NULL, updated_at=? WHERE run_id=?",
                    (action, now, run_id),
                )
            updated = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
        return dict(updated)

    def claim_next_run(
        self,
        owner_id: str,
        ttl_seconds: float = 15.0,
        run_id: str | None = None,
        now_ts: float | None = None,
    ) -> dict | None:
        """领取待执行或租约已过期的任务，单调递增 epoch 作为 fencing token。"""
        self._identifier(owner_id, "owner_id")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not 0.2 <= ttl_seconds <= 600:
            raise ValueError("lease_ttl_invalid")
        ts = time.time() if now_ts is None else float(now_ts)
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if run_id is not None:
                row = con.execute(
                    "SELECT * FROM run_leases WHERE run_id=? AND "
                    "(queue_state IN ('idle', 'queued') OR (queue_state='claimed' AND lease_until <= ?))",
                    (run_id, ts),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM run_leases WHERE queue_state='queued' "
                    "OR (queue_state='claimed' AND lease_until <= ?) "
                    "ORDER BY updated_at ASC, run_id ASC LIMIT 1",
                    (ts,),
                ).fetchone()
            if row is None:
                return None
            previous_owner = row["owner_id"]
            takeover = row["queue_state"] == "claimed"
            new_epoch = int(row["epoch"]) + 1
            deadline = ts + float(ttl_seconds)
            con.execute(
                "UPDATE run_leases SET queue_state='claimed', owner_id=?, epoch=?, lease_until=?, heartbeat_at=?, updated_at=? "
                "WHERE run_id=?",
                (owner_id, new_epoch, deadline, now, now, row["run_id"]),
            )
            payload = {
                "owner_id": owner_id,
                "epoch": new_epoch,
                "previous_owner": previous_owner,
                "takeover": takeover,
                "action": row["action"],
                "ttl_seconds": float(ttl_seconds),
            }
            con.execute(
                "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, 'lease_claimed', ?, ?)",
                (row["run_id"], now, _dump(payload)),
            )
        return {
            "run_id": row["run_id"],
            "owner_id": owner_id,
            "epoch": new_epoch,
            "action": row["action"],
            "lease_until": deadline,
            "previous_owner": previous_owner,
            "takeover": takeover,
        }

    def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        epoch: int,
        ttl_seconds: float = 15.0,
        now_ts: float | None = None,
    ) -> dict:
        """续租当前任务；若已被新世代 Worker 接管则立即抛 StaleEpochError。"""
        ts = time.time() if now_ts is None else float(now_ts)
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, epoch)
            row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if row["owner_id"] != owner_id:
                raise StaleEpochError("lease_owner_mismatch")
            deadline = ts + float(ttl_seconds)
            con.execute(
                "UPDATE run_leases SET lease_until=?, heartbeat_at=?, updated_at=? WHERE run_id=?",
                (deadline, now, now, run_id),
            )
        return {"run_id": run_id, "owner_id": owner_id, "epoch": epoch, "lease_until": deadline}

    def release_lease(self, run_id: str, owner_id: str, epoch: int, final_state: str) -> None:
        """结束本次租约；同样校验世代，防止过期 Worker 把已完成任务改回旧状态。"""
        if final_state not in {"idle", "queued", "waiting_approval", "completed", "failed", "budget_exceeded"}:
            raise ValueError("lease_final_state_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, epoch)
            row = con.execute("SELECT owner_id FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if row["owner_id"] != owner_id:
                raise StaleEpochError("lease_owner_mismatch")
            con.execute(
                "UPDATE run_leases SET queue_state=?, owner_id=NULL, lease_until=NULL, updated_at=? WHERE run_id=?",
                (final_state, now, run_id),
            )

    def get_lease(self, run_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def update_worker_node(
        self,
        worker_id: str,
        pid: int,
        host: str,
        status: str = "idle",
        active_run_id: str | None = None,
        active_epoch: int | None = None,
    ) -> None:
        self._identifier(worker_id, "worker_id")
        now = _now()
        with self._connect() as con:
            con.execute(
                "INSERT INTO worker_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET "
                "pid=excluded.pid, host=excluded.host, last_seen_at=excluded.last_seen_at, "
                "active_run_id=excluded.active_run_id, active_epoch=excluded.active_epoch, status=excluded.status",
                (worker_id, int(pid), host[:120], now, now, active_run_id, active_epoch, status[:40]),
            )

    def list_workers(self, limit: int = 32) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM worker_nodes ORDER BY last_seen_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def list_runs(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT body FROM runs ORDER BY created_at DESC LIMIT 200").fetchall()
        return [json.loads(row["body"]) for row in rows]

    def save_run(self, run: dict, expected_epoch: int | None = None) -> None:
        # JSON 整体保存，新增字段无需迁移。生产环境通常拆出可检索的状态列。
        run["updated_at"] = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run["id"], expected_epoch)
            changed = con.execute("UPDATE runs SET body=? WHERE id=?", (_dump(run), run["id"]))
            if changed.rowcount != 1:
                raise KeyError("run_not_found")

    def add_event(self, run_id: str, kind: str, payload: dict) -> dict:
        now = _now()
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, ?, ?, ?)",
                (run_id, kind, now, _dump(payload)),
            )
            event_id = cur.lastrowid
        return {"id": event_id, "run_id": run_id, "kind": kind, "created_at": now, "payload": payload}

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 2000",
                (run_id, after),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def remember(self, scope: str, key: str, value: Any) -> None:
        if not scope or not key or len(scope) > 200 or len(key) > 200:
            raise ValueError("memory_scope_or_key_invalid")
        encoded = _dump(value)
        if len(encoded) > 16000:
            raise ValueError("memory_too_large")
        with self._connect() as con:
            con.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?) ON CONFLICT(scope,key) "
                "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (scope, key, encoded, _now()),
            )

    def memories(self, scope: str) -> dict:
        with self._connect() as con:
            rows = con.execute(
                "SELECT key,value FROM memories WHERE scope=? ORDER BY key", (scope,)
            ).fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def create_proposal(
        self,
        run_id: str,
        path: str,
        old_content: str,
        new_content: str,
        operation_id: str | None = None,
        expected_epoch: int | None = None,
        base_commit: str | None = None,
    ) -> dict:
        """同一工具调用只产生一个提案，弥合创建提案后、保存工具结果前的崩溃窗口。

        这只是本地数据库中提案创建的幂等键。文件写入仍需独立审批和 CAS；
        不能把它推广为任意外部系统副作用的 exactly-once 保证。
        """
        return self.create_changeset_proposal(
            run_id=run_id,
            files=[
                {
                    "path": path,
                    "old_content": old_content,
                    "new_content": new_content,
                }
            ],
            summary="",
            base_commit=base_commit,
            operation_id=operation_id,
            expected_epoch=expected_epoch,
        )

    def create_changeset_proposal(
        self,
        run_id: str,
        files: list[dict[str, str]],
        summary: str = "",
        base_commit: str | None = None,
        operation_id: str | None = None,
        expected_epoch: int | None = None,
    ) -> dict:
        """支持单文件或多文件整组原子提案，绑定每个目标文件的原文 SHA-256 与 Git 基线。"""
        if not files or len(files) > 8:
            raise ValueError("changeset_files_count_invalid")
        if operation_id is not None:
            self._identifier(operation_id, "operation_id")
        normalized_files = []
        seen_paths: set[str] = set()
        for entry in files:
            path = entry["path"]
            if path in seen_paths:
                raise ValueError("duplicate_proposal_path")
            seen_paths.add(path)
            old_c = entry["old_content"]
            new_c = entry["new_content"]
            normalized_files.append(
                {
                    "path": path,
                    "old_content": old_c,
                    "new_content": new_c,
                    "old_sha256": digest(old_c),
                    "new_sha256": digest(new_c),
                }
            )
        single = len(normalized_files) == 1
        old_sha = (
            normalized_files[0]["old_sha256"]
            if single
            else digest(
                canonical_json([{"path": f["path"], "sha256": f["old_sha256"]} for f in normalized_files])
            )
        )
        new_sha = (
            normalized_files[0]["new_sha256"]
            if single
            else digest(
                canonical_json([{"path": f["path"], "sha256": f["new_sha256"]} for f in normalized_files])
            )
        )
        proposal = {
            "id": uuid.uuid4().hex,
            "run_id": run_id,
            "kind": "single_file" if single else "changeset",
            "summary": summary[:300],
            "base_commit": base_commit,
            "paths": [f["path"] for f in normalized_files],
            "files": normalized_files,
            "path": normalized_files[0]["path"]
            if single
            else ", ".join(f["path"] for f in normalized_files),
            "old_content": normalized_files[0]["old_content"]
            if single
            else "\n".join(f"=== {f['path']} ===\n{f['old_content']}" for f in normalized_files),
            "new_content": normalized_files[0]["new_content"]
            if single
            else "\n".join(f"=== {f['path']} ===\n{f['new_content']}" for f in normalized_files),
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "status": "pending",
            "applied": False,
            "created_at": _now(),
            "operation_id": operation_id,
        }
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, expected_epoch)
            if operation_id is not None:
                existing = con.execute(
                    "SELECT body FROM proposals WHERE run_id=? AND operation_id=?", (run_id, operation_id)
                ).fetchone()
                if existing:
                    stored = json.loads(existing["body"])
                    if any(stored[key] != proposal[key] for key in ("path", "old_sha256", "new_sha256")):
                        raise ValueError("proposal_operation_conflict")
                    return stored
            con.execute(
                "INSERT INTO proposals(id, run_id, body, operation_id) VALUES (?, ?, ?, ?)",
                (proposal["id"], run_id, _dump(proposal), operation_id),
            )
        return proposal

    def get_proposal_by_operation(self, run_id: str, operation_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT body FROM proposals WHERE run_id=? AND operation_id=?", (run_id, operation_id)
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def get_proposal(self, proposal_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT body FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def proposals(self, run_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT body FROM proposals WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def approve_proposal(self, proposal_id: str, approved: bool) -> dict:
        """审批不可反复翻转；同一决定重试幂等，已拒绝补丁需重新提出。"""
        if not isinstance(approved, bool):
            raise ValueError("approval_must_be_boolean")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT body FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
            target = "approved" if approved else "rejected"
            if proposal["status"] not in ("pending", target):
                raise ValueError("approval_already_decided")
            proposal["status"] = target
            proposal["decided_at"] = _now()
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))
        return proposal

    def mark_proposal_applied(self, proposal_id: str, expected_epoch: int | None = None) -> dict:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT body FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
            self._verify_epoch_tx(con, proposal["run_id"], expected_epoch)
            if proposal["status"] != "approved":
                raise ValueError("approval_required")
            proposal["applied"] = True
            proposal["applied_at"] = _now()
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))
        return proposal

    @staticmethod
    def _identifier(value: str, field: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"{field}_invalid")

    def schedule_call(
        self, run_id: str, call_id: str, name: str, arguments: dict, expected_epoch: int | None = None
    ) -> dict:
        """先记录调用意图。相同 ID 只接受相同工具与规范化参数，绝不靠名称猜测恢复。"""
        self._identifier(call_id, "call_id")
        self._identifier(name, "tool_name")
        if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
            raise ValueError("tool_arguments_must_be_object")
        encoded_arguments = canonical_json(arguments)
        fingerprint = digest(encoded_arguments)
        now = _now()
        record = {
            "run_id": run_id,
            "call_id": call_id,
            "name": name,
            "arguments": json.loads(encoded_arguments),
            "arguments_sha256": fingerprint,
            "status": "scheduled",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "result_sha256": None,
            "attempt_history": [],
        }
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, expected_epoch)
            row = con.execute(
                "SELECT body FROM tool_calls WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
            if row:
                stored = json.loads(row["body"])
                if stored["name"] != name or stored["arguments_sha256"] != fingerprint:
                    raise ValueError("tool_call_identity_conflict")
                return stored
            con.execute(
                "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, call_id, name, fingerprint, "scheduled", now, _dump(record)),
            )
        return record

    def get_call(self, run_id: str, call_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT body FROM tool_calls WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def list_calls(self, run_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT body FROM tool_calls WHERE run_id=? ORDER BY created_at, call_id", (run_id,)
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def call_summaries(self, run_id: str, limit: int = 200) -> list[dict]:
        """轻量状态视图；构建模型上下文时不反复读取全部大结果 JSON。"""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("tool_call_limit_invalid")
        with self._connect() as con:
            rows = con.execute(
                "SELECT call_id,name,status,arguments_sha256,created_at FROM tool_calls "
                "WHERE run_id=? ORDER BY created_at DESC,call_id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _save_call(con: sqlite3.Connection, record: dict) -> None:
        record["updated_at"] = _now()
        con.execute(
            "UPDATE tool_calls SET status=?,body=? WHERE run_id=? AND call_id=?",
            (record["status"], _dump(record), record["run_id"], record["call_id"]),
        )

    @staticmethod
    def _require_call(con: sqlite3.Connection, run_id: str, call_id: str) -> dict:
        row = con.execute(
            "SELECT body FROM tool_calls WHERE run_id=? AND call_id=?", (run_id, call_id)
        ).fetchone()
        if not row:
            raise KeyError("tool_call_not_found")
        return json.loads(row["body"])

    def start_call(self, run_id: str, call_id: str, expected_epoch: int | None = None) -> dict:
        """明确开始一次尝试。是否允许重放由 Runtime 的工具恢复策略决定。

        running 只说明数据库里没有完成记录，不能证明上个进程没产生副作用。
        因而这里不会自行把 running/unknown 当成“可以安全重试”。本项目的
        单进程执行器需先判断只读、幂等提案或未知外部效果，再显式调用本方法。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, expected_epoch)
            record = self._require_call(con, run_id, call_id)
            if record["status"] in {"succeeded", "waiting_approval"}:
                raise ValueError("tool_call_already_finished")
            if record["attempts"]:
                record["attempt_history"].append(
                    {
                        "attempt": record["attempts"],
                        "status": "unknown" if record["status"] == "running" else record["status"],
                        "started_at": record["started_at"],
                        "finished_at": record["finished_at"],
                        "result": record["result"],
                        "result_sha256": record["result_sha256"],
                    }
                )
            record.update(
                status="running",
                attempts=record["attempts"] + 1,
                started_at=_now(),
                finished_at=None,
                result=None,
                result_sha256=None,
            )
            self._save_call(con, record)
        return record

    def finish_call(
        self,
        run_id: str,
        call_id: str,
        result: Any,
        status: str | None = None,
        expected_epoch: int | None = None,
    ) -> dict:
        """完整结果先落盘，再由 Runtime 生成短投影；空字典/列表/False 都是真实结果。"""
        if status is None:
            if isinstance(result, dict) and result.get("requires_approval"):
                status = "waiting_approval"
            elif isinstance(result, dict) and not result.get("ok", True):
                status = "failed"
            else:
                status = "succeeded"
        if status not in {"succeeded", "failed", "waiting_approval", "unknown"}:
            raise ValueError("tool_call_status_invalid")
        fingerprint = digest(canonical_json(result))
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, expected_epoch)
            record = self._require_call(con, run_id, call_id)
            if record["status"] not in {"scheduled", "running"}:
                if record["status"] == status and record["result_sha256"] == fingerprint:
                    return record
                raise ValueError("tool_call_already_finished")
            record.update(status=status, result=result, result_sha256=fingerprint, finished_at=_now())
            self._save_call(con, record)
        return record

    def get_call_result(self, run_id: str, call_id: str, offset: int = 0, limit: int = 4000) -> dict:
        """分页取回完整结果的 JSON 文本，不把一页文本假装成完整的源码或 JSON 对象。"""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
            raise ValueError("tool_result_page_invalid")
        record = self.get_call(run_id, call_id)
        if record is None:
            raise KeyError("tool_call_not_found")
        if record["result_sha256"] is None:
            raise ValueError("tool_result_not_available")
        encoded = canonical_json(record["result"])
        end = min(offset + limit, len(encoded))
        return {
            "ok": True,
            "run_id": run_id,
            "call_id": call_id,
            "name": record["name"],
            "arguments_sha256": record["arguments_sha256"],
            "status": record["status"],
            "sha256": record["result_sha256"],
            "total_chars": len(encoded),
            "offset": offset,
            "next_offset": end if end < len(encoded) else None,
            "content": encoded[offset:end],
            "content_format": "json_text_page",
        }

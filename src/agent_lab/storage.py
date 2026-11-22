"""SQLite 持久化、任务队列租约与单调递增 Fencing Token。

模型只看到有界上下文；数据库保留完整轨迹、待审批补丁集、调用账本、跨会话笔记
以及本机多 Worker 协调所需的队列租约与递增世代（epoch / fencing token）。
此存储可在同一主机共享 SQLite WAL 的多个 Worker 进程间实现过期接管与过期写拦截；
跨多台物理机器的高可用仍需替换为 PostgreSQL 等网络事务存储。
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any
import uuid


class StaleEpochError(RuntimeError):
    """旧 Worker 持有的 fencing token 已过期，拒绝写入状态、工具账本或补丁产物。"""


class StorageConfigError(RuntimeError, ValueError):
    """存储后端 DSN 配置非法或缺少对应数据库驱动。"""


def _translate_qmark_to_pyformat(sql: str) -> str:
    stripped = sql.strip()
    if stripped.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    out: list[str] = []
    in_single = False
    idx = 0
    length = len(sql)
    while idx < length:
        ch = sql[idx]
        if ch == "'":
            out.append(ch)
            if in_single and idx + 1 < length and sql[idx + 1] == "'":
                out.append("'")
                idx += 2
                continue
            in_single = not in_single
        elif ch == "?" and not in_single:
            out.append("%s")
        else:
            out.append(ch)
        idx += 1
    return "".join(out)


@dataclass(frozen=True)
class PostgresBackendConfig:
    """多节点生产环境 PostgreSQL 连接与 FOR UPDATE SKIP LOCKED 队列锁配置。"""

    dsn: str
    connect_timeout: int = 10
    application_name: str = "agent-systems-lab"
    claim_lock_clause: str = "FOR UPDATE SKIP LOCKED"

    def translate_sql(self, sql: str) -> str:
        return _translate_qmark_to_pyformat(sql)

    def build_claim_sql(self, *, specific_run: bool = False) -> str:
        if specific_run:
            return (
                "SELECT * FROM run_leases WHERE run_id=%s AND "
                "(queue_state IN ('idle', 'queued') OR (queue_state='claimed' AND lease_until <= %s)) "
                f"{self.claim_lock_clause}"
            )
        return (
            "SELECT * FROM run_leases WHERE queue_state='queued' "
            "OR (queue_state='claimed' AND lease_until <= %s) "
            f"ORDER BY updated_at ASC, run_id ASC LIMIT 1 {self.claim_lock_clause}"
        )

    def connect(self) -> Any:
        try:
            psycopg = importlib.import_module("psycopg")
            rows_mod = importlib.import_module("psycopg.rows")
        except ImportError as exc:
            raise StorageConfigError(
                "postgresql_driver_missing: install 'psycopg[binary]' to use postgresql:// DSN"
            ) from exc
        raw_con = psycopg.connect(
            self.dsn,
            connect_timeout=self.connect_timeout,
            row_factory=rows_mod.dict_row,
            application_name=self.application_name,
            autocommit=True,
        )
        return _PostgresConnectionAdapter(raw_con, self)


class _PostgresConnectionAdapter:
    """将 psycopg 连接适配为与 sqlite3 连接一致的上下文与占位符接口。"""

    def __init__(self, raw_con: Any, config: PostgresBackendConfig) -> None:
        self._raw = raw_con
        self._config = config
        self._in_tx = False

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        translated = self._config.translate_sql(sql)
        if translated == "BEGIN":
            self._raw.execute("BEGIN")
            self._in_tx = True
            return self._raw
        return self._raw.execute(translated, params) if params else self._raw.execute(translated)

    def __enter__(self) -> _PostgresConnectionAdapter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._in_tx:
            self._in_tx = False
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        return False

    def close(self) -> None:
        self._raw.close()


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
    """一个文件保存状态；写入事务用 WAL 并限制锁等待时间，支持通过 DSN 切换后端。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        synchronous: str | None = None,
        pg_config: PostgresBackendConfig | None = None,
    ):
        raw_sync = synchronous if synchronous is not None else os.environ.get("AGENT_LAB_SQLITE_SYNC", "NORMAL")
        sync_mode = str(raw_sync).strip().upper()
        if sync_mode not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("sqlite_synchronous_invalid")
        self.synchronous = sync_mode
        self.pg_config = pg_config
        self.backend = "postgresql" if pg_config is not None else "sqlite"

        if pg_config is not None:
            try:
                importlib.import_module("psycopg")
            except ImportError as exc:
                raise StorageConfigError(
                    "postgresql_driver_missing: install 'psycopg[binary]' to use postgresql:// DSN"
                ) from exc
            anchor = Path(db_path) if db_path is not None else Path(".agent-lab/pg_state.anchor")
            anchor.parent.mkdir(parents=True, exist_ok=True)
            self.path = anchor.parent.resolve() / anchor.name
            self._initialize_postgres_schema()
            return

        # :memory: 每个连接是一份独立数据库，不适合本实现的连接生命周期。
        if db_path is None or str(db_path) == ":memory:":
            raise ValueError("请使用临时目录中的 SQLite 文件，不能使用 :memory:")
        configured = Path(db_path)
        # 配置目录规范化一次，兼容 macOS /var -> /private/var 等系统路径别名。
        # 保留末端文件名，让执行锁仍能拒绝数据库文件本身的 symlink。
        self.path = configured.parent.resolve() / configured.name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 多个线程/进程在 Barrier 同刻首次切换 WAL 或锁竞争时，SQLite 可能返回 locked/busy/protocol；
        # 启动迁移做有界退避重试。
        for attempt in range(8):
            try:
                self._initialize_schema()
                break
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if not any(tok in err_msg for tok in ("locked", "busy", "protocol")) or attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))

    @classmethod
    def from_dsn(cls, dsn: str, *, synchronous: str | None = None) -> Storage:
        """从 DSN 构建存储实例：原生支持 sqlite:///...，并在安装 psycopg 时支持 postgresql://...。"""
        if not isinstance(dsn, str) or not dsn.strip():
            raise StorageConfigError("storage_dsn_empty")
        cleaned = dsn.strip()
        if cleaned in {":memory:", "sqlite:///:memory:", "sqlite://:memory:"}:
            raise ValueError("请使用临时目录中的 SQLite 文件，不能使用 :memory:")
        if cleaned.startswith("sqlite:///"):
            raw_path = cleaned[len("sqlite:///") :]
            if not raw_path or raw_path == ":memory:":
                raise ValueError("请使用临时目录中的 SQLite 文件，不能使用 :memory:")
            return cls(Path(raw_path), synchronous=synchronous)
        if cleaned.startswith(("postgresql://", "postgres://")):
            return cls(None, synchronous=synchronous, pg_config=PostgresBackendConfig(dsn=cleaned))
        if "://" not in cleaned:
            return cls(Path(cleaned), synchronous=synchronous)
        scheme = cleaned.split("://", 1)[0]
        raise StorageConfigError(f"unsupported_storage_dsn_scheme: {scheme}")

    async def run_async(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """在工作线程池中执行同步存储操作，避免阻塞事件循环并自动传播 contextvars。"""
        target = getattr(self, fn) if isinstance(fn, str) else fn
        return await asyncio.to_thread(target, *args, **kwargs)

    def _initialize_postgres_schema(self) -> None:
        ddl_statements = (
            """CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL,
                state_version INTEGER NOT NULL DEFAULT 1, status_col TEXT,
                updated_at_col TEXT, tenant_id TEXT NOT NULL DEFAULT 'local', summary_json TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS runs_tenant_created ON runs(tenant_id, created_at DESC)",
            """CREATE TABLE IF NOT EXISTS events (
                id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id)",
            """CREATE TABLE IF NOT EXISTS memories (
                scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(scope, key)
            )""",
            """CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                body TEXT NOT NULL, operation_id TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS proposals_run ON proposals(run_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS proposals_operation ON proposals(run_id, operation_id) WHERE operation_id IS NOT NULL",
            """CREATE TABLE IF NOT EXISTS tool_calls (
                run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL,
                name TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'scheduled', 'running', 'succeeded', 'failed', 'waiting_approval', 'unknown'
                )),
                created_at TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id, call_id)
            )""",
            "CREATE INDEX IF NOT EXISTS tool_calls_run_status ON tool_calls(run_id, status)",
            """CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY REFERENCES runs(id),
                queue_state TEXT NOT NULL CHECK(queue_state IN (
                    'idle', 'queued', 'claimed', 'waiting_approval', 'completed',
                    'failed', 'budget_exceeded', 'dead_letter', 'cancelled', 'interrupted'
                )),
                action TEXT NOT NULL DEFAULT 'execute', owner_id TEXT,
                epoch INTEGER NOT NULL DEFAULT 0, takeover_count INTEGER NOT NULL DEFAULT 0,
                lease_until DOUBLE PRECISION, heartbeat_at TEXT, updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS run_leases_claimable ON run_leases(queue_state, lease_until)",
            """CREATE TABLE IF NOT EXISTS worker_nodes (
                worker_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, host TEXT NOT NULL,
                started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                active_run_id TEXT, active_epoch INTEGER, status TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS workspace_archives (
                run_id TEXT PRIMARY KEY REFERENCES runs(id), epoch INTEGER NOT NULL,
                git_head TEXT, archive BYTEA NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS dream_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_slug TEXT NOT NULL,
                created_at TEXT NOT NULL, body TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS dream_runs_scope ON dream_runs(tenant_id, project_slug, created_at DESC)",
        )
        with self._connect() as con:
            con.execute("BEGIN")
            for stmt in ddl_statements:
                con.execute(stmt)

    def _initialize_schema(self) -> None:
        lease_table_ddl = """
            CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY REFERENCES runs(id),
                queue_state TEXT NOT NULL CHECK(queue_state IN (
                    'idle', 'queued', 'claimed', 'waiting_approval', 'completed',
                    'failed', 'budget_exceeded', 'dead_letter', 'cancelled', 'interrupted'
                )),
                action TEXT NOT NULL DEFAULT 'execute',
                owner_id TEXT,
                epoch INTEGER NOT NULL DEFAULT 0,
                takeover_count INTEGER NOT NULL DEFAULT 0,
                lease_until REAL,
                heartbeat_at TEXT,
                updated_at TEXT NOT NULL
            )
        """
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            # 全程在单个排他写事务内完成建表与旧库加法迁移，保护既有数据行与并发启动幂等性。
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    body TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 1,
                    status_col TEXT,
                    updated_at_col TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    summary_json TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id)")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    scope TEXT NOT NULL, key TEXT NOT NULL,
                    value TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope, key)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    body TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS proposals_run ON proposals(run_id)")
            con.execute(
                """
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
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS tool_calls_run_status ON tool_calls(run_id, status)")
            con.execute(lease_table_ddl)
            con.execute("CREATE INDEX IF NOT EXISTS run_leases_claimable ON run_leases(queue_state, lease_until)")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_nodes (
                    worker_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    host TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active_run_id TEXT,
                    active_epoch INTEGER,
                    status TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS dream_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project_slug TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    body TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS dream_runs_scope ON dream_runs(tenant_id, project_slug, created_at DESC)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_archives (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),
                    epoch INTEGER NOT NULL,
                    git_head TEXT,
                    archive BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            run_cols = {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
            if "state_version" not in run_cols:
                con.execute("ALTER TABLE runs ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1")
            if "status_col" not in run_cols:
                con.execute("ALTER TABLE runs ADD COLUMN status_col TEXT")
            if "updated_at_col" not in run_cols:
                con.execute("ALTER TABLE runs ADD COLUMN updated_at_col TEXT")
            if "tenant_id" not in run_cols:
                con.execute("ALTER TABLE runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            if "summary_json" not in run_cols:
                con.execute("ALTER TABLE runs ADD COLUMN summary_json TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS runs_tenant_created ON runs(tenant_id, created_at DESC)")

            proposal_cols = {row["name"] for row in con.execute("PRAGMA table_info(proposals)")}
            if "operation_id" not in proposal_cols:
                con.execute("ALTER TABLE proposals ADD COLUMN operation_id TEXT")
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS proposals_operation "
                "ON proposals(run_id, operation_id) WHERE operation_id IS NOT NULL"
            )

            lease_master = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_leases'"
            ).fetchone()
            lease_sql = (lease_master["sql"] or "") if lease_master is not None else ""
            lease_cols = {row["name"] for row in con.execute("PRAGMA table_info(run_leases)")}
            if any(st not in lease_sql for st in ("dead_letter", "cancelled", "interrupted")):
                con.execute("DROP INDEX IF EXISTS run_leases_claimable")
                con.execute("ALTER TABLE run_leases RENAME TO _run_leases_legacy")
                con.execute(lease_table_ddl)
                takeover_expr = "COALESCE(takeover_count, 0)" if "takeover_count" in lease_cols else "0"
                con.execute(
                    f"INSERT INTO run_leases("
                    f"run_id, queue_state, action, owner_id, epoch, takeover_count, lease_until, heartbeat_at, updated_at"
                    f") SELECT run_id, queue_state, action, owner_id, epoch, {takeover_expr}, lease_until, heartbeat_at, updated_at "
                    f"FROM _run_leases_legacy"
                )
                con.execute("DROP TABLE _run_leases_legacy")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS run_leases_claimable ON run_leases(queue_state, lease_until)"
                )
            elif "takeover_count" not in lease_cols:
                con.execute("ALTER TABLE run_leases ADD COLUMN takeover_count INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self):
        if self.pg_config is not None:
            pg_con = self.pg_config.connect()
            try:
                with pg_con:
                    yield pg_con
            finally:
                pg_con.close()
            return

        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute(f"PRAGMA synchronous={self.synchronous}")
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _build_run_summary(run: dict, tenant_id: str | None = None) -> dict:
        summary: dict[str, Any] = {}
        for key, value in run.items():
            if key in {"messages", "metadata"}:
                continue
            if value is None or isinstance(value, (str, int, float, bool)):
                summary[key] = value
        raw_meta = run.get("metadata")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        meta_summary: dict[str, Any] = {}
        if isinstance(raw_meta, dict):
            for mkey in (
                "template",
                "execution_profile",
                "permission_mode",
                "enable_gui",
                "direct_workspace",
                "verification",
                "error_detail",
                "tenant_id",
                "actor_id",
                "cancel_requested",
                "cancel_reason",
                "pending_results",
                "waiting_proposal_ids",
            ):
                if mkey in raw_meta:
                    meta_summary[mkey] = raw_meta[mkey]
            if isinstance(raw_meta.get("kv_cache"), dict):
                meta_summary["kv_cache"] = {
                    k: v for k, v in raw_meta["kv_cache"].items() if k != "known_blocks"
                }
        if tenant_id and "tenant_id" not in meta_summary:
            meta_summary["tenant_id"] = tenant_id
        summary["error_detail"] = meta.get("error_detail")
        summary["metadata"] = meta_summary
        return summary

    def create_run(
        self, prompt: str, mode: str = "agent", provider: str = "demo", *, tenant_id: str = "local"
    ) -> dict:
        now = _now()
        clean_tenant = str(tenant_id or "local").strip() or "local"
        metadata: dict[str, Any] = {}
        if clean_tenant != "local":
            metadata["tenant_id"] = clean_tenant
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
            "state_version": 1,
            "created_at": now,
            "updated_at": now,
            "metadata": metadata,
        }
        summary = self._build_run_summary(run, tenant_id=clean_tenant)
        with self._connect() as con:
            con.execute(
                "INSERT INTO runs(id, created_at, body, state_version, status_col, updated_at_col, tenant_id, summary_json) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (run["id"], now, _dump(run), run["status"], now, clean_tenant, _dump(summary)),
            )
            con.execute(
                "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, lease_until, heartbeat_at, updated_at) "
                "VALUES (?, 'idle', 'execute', NULL, 0, 0, NULL, NULL, ?)",
                (run["id"], now),
            )
        return run

    @staticmethod
    def _verify_epoch_tx(con: Any, run_id: str, expected_epoch: int | None) -> None:
        row = con.execute(
            "SELECT epoch, queue_state, owner_id, lease_until FROM run_leases WHERE run_id=?", (run_id,)
        ).fetchone()
        if expected_epoch is None:
            if row is not None and row["queue_state"] == "claimed" and (row["lease_until"] or 0.0) > time.time():
                raise StaleEpochError(f"fencing_inline_blocked_by_claimed_lease: run={run_id}")
            return
        if row is None or row["epoch"] != expected_epoch or row["queue_state"] != "claimed":
            actual = row["epoch"] if row is not None else None
            state = row["queue_state"] if row is not None else None
            raise StaleEpochError(
                f"fencing_token_rejected: run={run_id} expected_epoch={expected_epoch} actual_epoch={actual} state={state}"
            )

    def verify_epoch(self, run_id: str, expected_epoch: int | None) -> None:
        """在工作区写文件或 Git 提交前校验当前执行世代仍有效，同时拦截与存活租约冲突的内联写入。"""
        with self._connect() as con:
            self._verify_epoch_tx(con, run_id, expected_epoch)

    def bump_inline_epoch(self, run_id: str, *, now_ts: float | None = None) -> int:
        """内联执行器启动前递增世代号（保持 idle），立即将任何旧世代过期 Worker 隔离出局。"""
        ts = time.time() if now_ts is None else float(now_ts)
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
            row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, lease_until, heartbeat_at, updated_at) "
                    "VALUES (?, 'idle', 'execute', NULL, 1, 0, NULL, NULL, ?)",
                    (run_id, now),
                )
                return 1
            if row["queue_state"] == "claimed" and (row["lease_until"] or 0.0) > ts:
                raise StaleEpochError("run_currently_claimed")
            new_epoch = int(row["epoch"] or 0) + 1
            con.execute(
                "UPDATE run_leases SET queue_state='idle', owner_id=NULL, epoch=?, lease_until=NULL, updated_at=? WHERE run_id=?",
                (new_epoch, now, run_id),
            )
        return new_epoch

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
                    "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, lease_until, heartbeat_at, updated_at) "
                    "VALUES (?, 'queued', ?, NULL, 0, 0, NULL, NULL, ?)",
                    (run_id, action, now),
                )
            else:
                # 如果另一个存活 Worker 仍在有效租约期内持有任务，拒绝重复入队覆盖。
                if row["queue_state"] == "claimed" and (row["lease_until"] or 0.0) > time.time():
                    raise ValueError("run_currently_claimed")
                con.execute(
                    "UPDATE run_leases SET queue_state='queued', action=?, owner_id=NULL, takeover_count=0, lease_until=NULL, updated_at=? WHERE run_id=?",
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
        max_takeovers: int = 3,
    ) -> dict | None:
        """领取待执行或租约已过期的任务；超限毒丸任务自动转入死信队列（dead_letter）并继续领取下一候选。"""
        self._identifier(owner_id, "owner_id")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not 0.2 <= ttl_seconds <= 600
        ):
            raise ValueError("lease_ttl_invalid")
        if isinstance(max_takeovers, bool) or not isinstance(max_takeovers, int) or max_takeovers < 0:
            raise ValueError("max_takeovers_invalid")
        ts = time.time() if now_ts is None else float(now_ts)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            while True:
                if self.pg_config is not None:
                    query = self.pg_config.build_claim_sql(specific_run=run_id is not None)
                    params: tuple[Any, ...] = (run_id, ts) if run_id is not None else (ts,)
                    row = con.execute(query, params).fetchone()
                elif run_id is not None:
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

                now = _now()
                candidate_id = row["run_id"]
                previous_owner = row["owner_id"]
                takeover = row["queue_state"] == "claimed"
                prior_takeovers = int(row["takeover_count"] or 0)

                if takeover and prior_takeovers >= max_takeovers:
                    con.execute(
                        "UPDATE run_leases SET queue_state='dead_letter', owner_id=NULL, lease_until=NULL, updated_at=? "
                        "WHERE run_id=?",
                        (now, candidate_id),
                    )
                    run_row = con.execute(
                        "SELECT body, tenant_id FROM runs WHERE id=?", (candidate_id,)
                    ).fetchone()
                    if run_row is not None:
                        run_doc = json.loads(run_row["body"])
                        run_doc["status"] = "failed"
                        run_doc["error"] = "DeadLetterExceeded"
                        run_doc["updated_at"] = now
                        run_doc["state_version"] = int(run_doc.get("state_version") or 1) + 1
                        tenant = str(run_row["tenant_id"] or "local")
                        summary = self._build_run_summary(run_doc, tenant_id=tenant)
                        con.execute(
                            "UPDATE runs SET body=?, state_version=state_version+1, status_col='failed', "
                            "updated_at_col=?, summary_json=? WHERE id=?",
                            (_dump(run_doc), now, _dump(summary), candidate_id),
                        )
                    dlq_payload = {
                        "previous_owner": previous_owner,
                        "epoch": int(row["epoch"]),
                        "takeover_count": prior_takeovers,
                        "max_takeovers": max_takeovers,
                    }
                    con.execute(
                        "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, 'run_dead_lettered', ?, ?)",
                        (candidate_id, now, _dump(dlq_payload)),
                    )
                    if run_id is not None:
                        return None
                    continue

                new_takeovers = prior_takeovers + 1 if takeover else prior_takeovers
                new_epoch = int(row["epoch"]) + 1
                deadline = ts + float(ttl_seconds)
                con.execute(
                    "UPDATE run_leases SET queue_state='claimed', owner_id=?, epoch=?, takeover_count=?, "
                    "lease_until=?, heartbeat_at=?, updated_at=? WHERE run_id=?",
                    (owner_id, new_epoch, new_takeovers, deadline, now, now, candidate_id),
                )
                payload = {
                    "owner_id": owner_id,
                    "epoch": new_epoch,
                    "previous_owner": previous_owner,
                    "takeover": takeover,
                    "takeover_count": new_takeovers,
                    "action": row["action"],
                    "ttl_seconds": float(ttl_seconds),
                }
                con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, 'lease_claimed', ?, ?)",
                    (candidate_id, now, _dump(payload)),
                )
                return {
                    "run_id": candidate_id,
                    "owner_id": owner_id,
                    "epoch": new_epoch,
                    "action": row["action"],
                    "lease_until": deadline,
                    "previous_owner": previous_owner,
                    "takeover": takeover,
                    "takeover_count": new_takeovers,
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
        """结束本次租约；若审批已抢先原子入队（同世代 queued + waiting_approval），则幂等无操作返回。"""
        if final_state not in {
            "idle",
            "queued",
            "waiting_approval",
            "completed",
            "failed",
            "budget_exceeded",
            "dead_letter",
            "cancelled",
            "interrupted",
        }:
            raise ValueError("lease_final_state_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT epoch, queue_state, owner_id FROM run_leases WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or row["epoch"] != epoch:
                actual = row["epoch"] if row is not None else None
                state = row["queue_state"] if row is not None else None
                raise StaleEpochError(
                    f"fencing_token_rejected: run={run_id} expected_epoch={epoch} actual_epoch={actual} state={state}"
                )
            if row["queue_state"] == "queued" and final_state == "waiting_approval":
                return
            if row["queue_state"] != "claimed":
                raise StaleEpochError(
                    f"fencing_token_rejected: run={run_id} expected_epoch={epoch} actual_epoch={row['epoch']} state={row['queue_state']}"
                )
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
                "INSERT INTO worker_nodes(worker_id, pid, host, started_at, last_seen_at, active_run_id, active_epoch, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
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

    def list_runs(self, *, tenant_id: str | None = None, limit: int = 200) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("run_limit_invalid")
        with self._connect() as con:
            if tenant_id is not None:
                rows = con.execute(
                    "SELECT summary_json, body FROM runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT summary_json, body FROM runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            json.loads(row["summary_json"]) if row["summary_json"] is not None else json.loads(row["body"])
            for row in rows
        ]

    def save_run(
        self,
        run: dict,
        expected_epoch: int | None = None,
        *,
        expected_version: int | None = None,
    ) -> None:
        if expected_version is not None and (
            isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1
        ):
            raise ValueError("expected_version_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run["id"], expected_epoch)
            existing = con.execute(
                "SELECT state_version, tenant_id FROM runs WHERE id=?", (run["id"],)
            ).fetchone()
            if existing is None:
                raise KeyError("run_not_found")
            current_version = int(existing["state_version"] or 1)
            if expected_version is not None and current_version != expected_version:
                raise StaleEpochError("state_version_conflict")
            next_version = current_version + 1
            prev_updated_at = run.get("updated_at")
            prev_version = run.get("state_version")
            run["updated_at"] = now
            run["state_version"] = next_version
            tenant = str(
                run.get("tenant_id")
                or (run.get("metadata") or {}).get("tenant_id")
                or existing["tenant_id"]
                or "local"
            )
            summary = self._build_run_summary(run, tenant_id=tenant)
            where_version = expected_version if expected_version is not None else current_version
            changed = con.execute(
                "UPDATE runs SET body=?, state_version=state_version+1, status_col=?, updated_at_col=?, "
                "tenant_id=?, summary_json=? WHERE id=? AND state_version=?",
                (_dump(run), run.get("status"), now, tenant, _dump(summary), run["id"], where_version),
            )
            if changed.rowcount != 1:
                if prev_updated_at is not None:
                    run["updated_at"] = prev_updated_at
                if prev_version is not None:
                    run["state_version"] = prev_version
                else:
                    run.pop("state_version", None)
                raise StaleEpochError("state_version_conflict")

    def add_event(self, run_id: str, kind: str, payload: dict) -> dict:
        now = _now()
        with self._connect() as con:
            if self.pg_config is not None:
                row = con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, ?, ?, ?) RETURNING id",
                    (run_id, kind, now, _dump(payload)),
                ).fetchone()
                event_id = int(row["id"])
            else:
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
                "INSERT INTO memories(scope, key, value, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(scope,key) "
                "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (scope, key, encoded, _now()),
            )

    def memories(self, scope: str) -> dict:
        with self._connect() as con:
            rows = con.execute(
                "SELECT key,value FROM memories WHERE scope=? ORDER BY key", (scope,)
            ).fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def full_runs_for_tenant(self, tenant_id: str = "local", *, limit: int = 25) -> list[dict]:
        """按时间倒序读取指定租户最近的完整会话记录，供跨会话 Transcript 检索与离线 Dreaming 重放。"""
        clean_tenant = str(tenant_id or "local").strip() or "local"
        cap = max(1, min(100, int(limit)))
        with self._connect() as con:
            rows = con.execute(
                "SELECT body FROM runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (clean_tenant, cap),
            ).fetchall()
        return [json.loads(r["body"]) for r in rows]

    def record_dream_run(
        self, tenant_id: str, project_slug: str, report: dict[str, Any]
    ) -> dict[str, Any]:
        """持久化记录一次离线 Dreaming 整理结果，用于审计与前端可观测展示。"""
        clean_tenant = str(tenant_id or "local").strip() or "local"
        clean_proj = str(project_slug or "default").strip() or "default"
        dream_id = str(report.get("id") or uuid.uuid4().hex)
        now = str(report.get("created_at") or _now())
        stored = {
            **report,
            "id": dream_id,
            "tenant_id": clean_tenant,
            "project_slug": clean_proj,
            "created_at": now,
        }
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO dream_runs(id, tenant_id, project_slug, created_at, body) VALUES (?, ?, ?, ?, ?)",
                (dream_id, clean_tenant, clean_proj, now, _dump(stored)),
            )
        return stored

    def list_dream_runs(
        self,
        tenant_id: str = "local",
        *,
        project_slug: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        clean_tenant = str(tenant_id or "local").strip() or "local"
        cap = max(1, min(50, int(limit)))
        with self._connect() as con:
            if project_slug:
                rows = con.execute(
                    "SELECT body FROM dream_runs WHERE tenant_id=? AND project_slug=? ORDER BY created_at DESC LIMIT ?",
                    (clean_tenant, project_slug, cap),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT body FROM dream_runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                    (clean_tenant, cap),
                ).fetchall()
        return [json.loads(r["body"]) for r in rows]

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
                    "action": "modify",
                    "old_content": old_content,
                    "new_content": new_content,
                }
            ],
            summary="",
            base_commit=base_commit,
            operation_id=operation_id,
            expected_epoch=expected_epoch,
            tool_name="propose_patch",
        )

    def create_changeset_proposal(
        self,
        run_id: str,
        files: list[dict[str, str]],
        summary: str = "",
        base_commit: str | None = None,
        operation_id: str | None = None,
        expected_epoch: int | None = None,
        tool_name: str | None = None,
    ) -> dict:
        """支持单文件或多文件整组原子提案，绑定每个目标文件的原文 SHA-256 与 Git 基线。"""
        if not files or len(files) > 8:
            raise ValueError("changeset_files_count_invalid")
        if operation_id is not None:
            self._identifier(operation_id, "operation_id")
        if tool_name is not None:
            self._identifier(tool_name, "tool_name")
        normalized_files = []
        seen_paths: set[str] = set()
        for entry in files:
            path = entry["path"]
            if path in seen_paths:
                raise ValueError("duplicate_proposal_path")
            seen_paths.add(path)
            action = entry.get("action") or "modify"
            if action not in {"modify", "create", "delete"}:
                raise ValueError("proposal_action_invalid")
            old_c = entry["old_content"]
            new_c = entry["new_content"]
            normalized_files.append(
                {
                    "path": path,
                    "action": action,
                    "old_content": old_c,
                    "new_content": new_c,
                    "old_sha256": digest(old_c),
                    "new_sha256": digest(new_c),
                }
            )
        single = len(normalized_files) == 1
        kind = (
            "changeset"
            if (
                len(normalized_files) > 1
                or tool_name == "propose_changeset"
                or any(f["action"] != "modify" for f in normalized_files)
            )
            else "single_file"
        )
        resolved_tool = tool_name or ("propose_changeset" if kind == "changeset" else "propose_patch")
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
            "kind": kind,
            "tool": resolved_tool,
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
                    stored_actions = [f.get("action", "modify") for f in stored.get("files", [])]
                    proposed_actions = [f["action"] for f in normalized_files]
                    stored_tool = stored.get("tool") or (
                        "propose_changeset" if stored.get("kind") == "changeset" else "propose_patch"
                    )
                    if (
                        any(stored[key] != proposal[key] for key in ("path", "old_sha256", "new_sha256"))
                        or stored.get("paths") != proposal["paths"]
                        or stored_actions != proposed_actions
                        or stored_tool != proposal["tool"]
                    ):
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
            order_clause = "ORDER BY id" if self.pg_config is not None else "ORDER BY rowid"
            rows = con.execute(
                f"SELECT body FROM proposals WHERE run_id=? {order_clause}", (run_id,)
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def approve_proposal(
        self, proposal_id: str, approved: bool, *, actor_id: str | None = None
    ) -> dict:
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
            if actor_id is not None or "decided_by" not in proposal:
                proposal["decided_by"] = actor_id
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))
        return proposal

    def approve_and_enqueue(
        self,
        run_id: str,
        proposal_id: str,
        approved: bool,
        *,
        actor_id: str | None = None,
        permission_mode: str | None = None,
    ) -> dict:
        """单事务原子完成补丁审批决策（及可选权限模式更新）与持久队列恢复入队。"""
        if not isinstance(approved, bool):
            raise ValueError("approval_must_be_boolean")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run_row = con.execute("SELECT body, tenant_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError("run_not_found")
            if permission_mode is not None:
                run_doc = json.loads(run_row["body"])
                run_doc.setdefault("metadata", {})["permission_mode"] = permission_mode
                run_doc["updated_at"] = now
                run_doc["state_version"] = int(run_doc.get("state_version") or 1) + 1
                tenant_val = str(run_row["tenant_id"] or run_doc["metadata"].get("tenant_id") or "local")
                summary_doc = self._build_run_summary(run_doc, tenant_id=tenant_val)
                con.execute(
                    "UPDATE runs SET body=?, state_version=state_version+1, updated_at_col=?, summary_json=? WHERE id=?",
                    (_dump(run_doc), now, _dump(summary_doc), run_id),
                )
            row = con.execute(
                "SELECT run_id, body FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None or row["run_id"] != run_id:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
            if proposal.get("run_id") != run_id:
                raise KeyError("proposal_not_found")
            target = "approved" if approved else "rejected"
            if proposal["status"] not in ("pending", target):
                raise ValueError("approval_already_decided")
            proposal["status"] = target
            proposal["decided_at"] = now
            proposal["decided_by"] = actor_id
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))

            lease_row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if lease_row is None:
                con.execute(
                    "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, "
                    "lease_until, heartbeat_at, updated_at) "
                    "VALUES (?, 'queued', 'resume', NULL, 0, 0, NULL, NULL, ?)",
                    (run_id, now),
                )
            else:
                if lease_row["queue_state"] in {"completed", "cancelled", "dead_letter"}:
                    raise ValueError(f"run_already_terminal:{lease_row['queue_state']}")
                con.execute(
                    "UPDATE run_leases SET queue_state='queued', action='resume', owner_id=NULL, "
                    "takeover_count=0, lease_until=NULL, updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
            updated_lease = dict(
                con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            )
        return {
            "run_id": run_id,
            "proposal": proposal,
            "lease": updated_lease,
            "status": proposal["status"],
            "queued": True,
        }

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

    def create_action_proposal(
        self,
        run_id: str,
        *,
        kind: str,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str = "",
        risk_level: str = "mutating",
        operation_id: str | None = None,
        expected_epoch: int | None = None,
    ) -> dict:
        """创建终端命令、桌面 GUI 动作或外部 MCP 工具调用的待审批提案（与文件补丁共享审批状态机）。"""
        if kind not in {"command", "gui_action", "mcp_call"}:
            raise ValueError("action_proposal_kind_invalid")
        self._identifier(tool_name, "tool_name")
        if operation_id is not None:
            self._identifier(operation_id, "operation_id")
        if not isinstance(arguments, dict):
            raise ValueError("action_proposal_arguments_invalid")
        encoded_args = canonical_json(arguments)
        payload_sha = digest(canonical_json({"kind": kind, "tool": tool_name, "arguments": json.loads(encoded_args)}))
        proposal = {
            "id": uuid.uuid4().hex,
            "run_id": run_id,
            "kind": kind,
            "tool": tool_name,
            "summary": summary[:300],
            "risk_level": risk_level,
            "arguments": json.loads(encoded_args),
            "base_commit": None,
            "paths": [],
            "files": [],
            "path": "",
            "old_content": "",
            "new_content": encoded_args,
            "old_sha256": "",
            "new_sha256": payload_sha,
            "status": "pending",
            "applied": False,
            "execution_result": None,
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
                    if (
                        stored.get("kind") != kind
                        or stored.get("tool") != tool_name
                        or stored.get("new_sha256") != payload_sha
                    ):
                        raise ValueError("proposal_operation_conflict")
                    return stored
            con.execute(
                "INSERT INTO proposals(id, run_id, body, operation_id) VALUES (?, ?, ?, ?)",
                (proposal["id"], run_id, _dump(proposal), operation_id),
            )
        return proposal

    def mark_action_proposal_executed(
        self,
        proposal_id: str,
        execution_result: dict[str, Any],
        expected_epoch: int | None = None,
    ) -> dict:
        """持久化记录非文件动作提案已执行及其输出回执，防止崩溃恢复时重复触发副作用。"""
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
            proposal["execution_result"] = execution_result
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))
        return proposal

    def append_user_turn(
        self,
        run_id: str,
        prompt: str,
        *,
        step_extension: int = 24,
        token_extension: int = 120_000,
        enqueue: bool = False,
        actor_id: str | None = None,
    ) -> dict:
        """在已结束/暂停的任务上追加新一轮用户输入，扩展轮次与 token 配额（可选原子入队）。"""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt or len(clean_prompt) > 8000:
            raise ValueError("prompt_length_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run_row = con.execute(
                "SELECT body, state_version, tenant_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError("run_not_found")
            run = json.loads(run_row["body"])
            cur_status = run.get("status")
            if cur_status in {"running", "queued", "waiting_approval"}:
                raise ValueError(f"run_busy:{cur_status}")
            lease_row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if lease_row is not None and lease_row["queue_state"] == "dead_letter":
                raise ValueError("run_dead_lettered")
            if (
                lease_row is not None
                and lease_row["queue_state"] == "claimed"
                and (lease_row["lease_until"] or 0.0) > time.time()
            ):
                raise ValueError("run_busy:claimed")

            meta = run.setdefault("metadata", {})
            meta.setdefault("initial_prompt", run.get("prompt", ""))
            msgs = run.setdefault("messages", [])
            # 若上一轮因取消/预算超限/异常在 assistant.tool_use 后中断而尚未写入对应 tool_result，
            # 先补齐 1:1 协议配对块，保证 ContextManager._atomic_groups 永远不会破坏。
            if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "assistant":
                last_content = msgs[-1].get("content")
                if isinstance(last_content, list):
                    orphan_uses = [
                        b
                        for b in last_content
                        if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str)
                    ]
                    if orphan_uses:
                        saved_map = meta.get("pending_results") if isinstance(meta.get("pending_results"), dict) else {}
                        synth_blocks = []
                        for tu in orphan_uses:
                            tu_id = tu["id"]
                            fallback_res = saved_map.get(tu_id) or {
                                "ok": False,
                                "code": "aborted_before_followup",
                                "error": "上一轮在该工具完成前已终止，现由新用户指令接续。",
                            }
                            synth_blocks.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tu_id,
                                    "content": json.dumps(fallback_res, ensure_ascii=False),
                                    "is_error": not fallback_res.get("ok", True),
                                }
                            )
                        msgs.append({"role": "user", "content": synth_blocks})

            msgs.append({"role": "user", "content": clean_prompt})
            run["prompt"] = clean_prompt
            run["answer"] = ""
            run["status"] = "queued" if enqueue else "running"
            run["error"] = None
            meta.pop("error_detail", None)
            meta.pop("approval_failure", None)
            meta.pop("pending_calls", None)
            meta.pop("pending_results", None)
            meta.pop("waiting_proposal_ids", None)
            meta.pop("cancel_requested", None)
            meta.pop("cancel_reason", None)
            meta.pop("truncation_continuations", None)
            per_turn_steps = max(1, int(meta.get("base_max_steps") or step_extension))
            per_turn_tokens = max(2048, int(meta.get("base_max_tokens") or token_extension))
            meta["max_steps"] = max(
                int(meta.get("max_steps", 12)),
                int(run.get("steps", 0)) + per_turn_steps,
            )
            used_tokens = max(
                int(run.get("input_tokens", 0)) + int(run.get("output_tokens", 0)),
                int(meta.get("billed_tokens", 0)),
            )
            meta["max_tokens"] = max(
                int(meta.get("max_tokens", 30000)),
                used_tokens + per_turn_tokens,
            )
            turns = meta.setdefault("conversation_turns", [])
            turns.append({"prompt": clean_prompt, "actor_id": actor_id, "created_at": now})

            next_version = int(run_row["state_version"] or 1) + 1
            run["updated_at"] = now
            run["state_version"] = next_version
            tenant = str(run_row["tenant_id"] or meta.get("tenant_id") or "local")
            summary = self._build_run_summary(run, tenant_id=tenant)
            con.execute(
                "UPDATE runs SET body=?, state_version=state_version+1, status_col=?, updated_at_col=?, "
                "summary_json=? WHERE id=?",
                (_dump(run), run["status"], now, _dump(summary), run_id),
            )
            con.execute(
                "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, 'user_followup', ?, ?)",
                (run_id, now, _dump({"prompt": clean_prompt[:500], "actor_id": actor_id})),
            )
            target_q_state = "queued" if enqueue else "idle"
            if lease_row is None:
                con.execute(
                    "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, "
                    "lease_until, heartbeat_at, updated_at) "
                    "VALUES (?, ?, 'execute', NULL, 1, 0, NULL, NULL, ?)",
                    (run_id, target_q_state, now),
                )
            else:
                next_epoch = int(lease_row["epoch"] or 0) + 1
                con.execute(
                    "UPDATE run_leases SET queue_state=?, action='execute', owner_id=NULL, epoch=?, "
                    "takeover_count=0, lease_until=NULL, updated_at=? WHERE run_id=?",
                    (target_q_state, next_epoch, now, run_id),
                )
        return run

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
                "INSERT INTO tool_calls(run_id, call_id, name, arguments_sha256, status, created_at, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
    def _save_call(con: Any, record: dict) -> None:
        record["updated_at"] = _now()
        con.execute(
            "UPDATE tool_calls SET status=?,body=? WHERE run_id=? AND call_id=?",
            (record["status"], _dump(record), record["run_id"], record["call_id"]),
        )

    @staticmethod
    def _require_call(con: Any, run_id: str, call_id: str) -> dict:
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

    def save_workspace_archive(
        self,
        run_id: str,
        epoch: int,
        archive_bytes: bytes,
        git_head: str | None = None,
    ) -> dict:
        """按单调递增世代号保存跨节点可移植工作区归档快照，拦截过期 Worker 的旧归档覆盖。"""
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("epoch_invalid")
        if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
            raise ValueError("archive_bytes_invalid")
        raw_bytes = bytes(archive_bytes)
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
            lease_row = con.execute("SELECT epoch FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            if lease_row is not None and int(lease_row["epoch"] or 0) > epoch:
                raise StaleEpochError(
                    f"stale_archive_epoch: run={run_id} archive_epoch={epoch} lease_epoch={lease_row['epoch']}"
                )
            existing = con.execute(
                "SELECT epoch FROM workspace_archives WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None and int(existing["epoch"]) > epoch:
                raise StaleEpochError(
                    f"stale_archive_epoch: run={run_id} archive_epoch={epoch} stored_epoch={existing['epoch']}"
                )
            con.execute(
                "INSERT INTO workspace_archives(run_id, epoch, git_head, archive, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "epoch=excluded.epoch, git_head=excluded.git_head, "
                "archive=excluded.archive, updated_at=excluded.updated_at",
                (run_id, epoch, git_head, raw_bytes, now),
            )
        return {
            "run_id": run_id,
            "epoch": epoch,
            "git_head": git_head,
            "size_bytes": len(raw_bytes),
            "updated_at": now,
        }

    def get_workspace_archive(self, run_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT run_id, epoch, git_head, archive, updated_at FROM workspace_archives WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        raw_bytes = bytes(row["archive"])
        return {
            "run_id": row["run_id"],
            "epoch": int(row["epoch"]),
            "git_head": row["git_head"],
            "archive": raw_bytes,
            "archive_bytes": raw_bytes,
            "size_bytes": len(raw_bytes),
            "updated_at": row["updated_at"],
        }

    def request_cancel_run(
        self,
        run_id: str,
        *,
        actor_id: str | None = None,
        reason: str = "user_requested",
    ) -> dict:
        """请求取消任务：排队或等待审批且未被活跃持锁时立即终止为 cancelled，运行中则打标协作退出。"""
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run_row = con.execute(
                "SELECT body, state_version, tenant_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError("run_not_found")
            run = json.loads(run_row["body"])
            current_status = run.get("status")
            if current_status in {"completed", "failed", "budget_exceeded", "cancelled"}:
                return run

            meta = run.setdefault("metadata", {})
            meta["cancel_requested"] = True
            meta["cancel_reason"] = reason
            meta["cancel_requested_at"] = now
            if actor_id is not None:
                meta["cancelled_by"] = actor_id

            lease_row = con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            actively_claimed = bool(
                lease_row is not None
                and lease_row["queue_state"] == "claimed"
                and (lease_row["lease_until"] or 0.0) > time.time()
            )
            immediate = current_status in {"queued", "waiting_approval"} and not actively_claimed
            if immediate:
                run["status"] = "cancelled"
                if lease_row is None:
                    con.execute(
                        "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, "
                        "lease_until, heartbeat_at, updated_at) "
                        "VALUES (?, 'cancelled', 'execute', NULL, 1, 0, NULL, NULL, ?)",
                        (run_id, now),
                    )
                else:
                    next_epoch = int(lease_row["epoch"] or 0) + 1
                    con.execute(
                        "UPDATE run_leases SET queue_state='cancelled', owner_id=NULL, epoch=?, "
                        "lease_until=NULL, updated_at=? WHERE run_id=?",
                        (next_epoch, now, run_id),
                    )
                con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, 'cancelled', ?, ?)",
                    (run_id, now, _dump({"actor_id": actor_id, "reason": reason, "immediate": True})),
                )
            else:
                con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload) VALUES (?, 'cancel_requested', ?, ?)",
                    (run_id, now, _dump({"actor_id": actor_id, "reason": reason, "immediate": False})),
                )

            next_version = int(run_row["state_version"] or 1) + 1
            run["updated_at"] = now
            run["state_version"] = next_version
            tenant = str(run_row["tenant_id"] or meta.get("tenant_id") or "local")
            summary = self._build_run_summary(run, tenant_id=tenant)
            con.execute(
                "UPDATE runs SET body=?, state_version=state_version+1, status_col=?, updated_at_col=?, "
                "summary_json=? WHERE id=?",
                (_dump(run), run.get("status"), now, _dump(summary), run_id),
            )
        return run

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT status_col, summary_json, body FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            return False
        if row["status_col"] == "cancelled":
            return True
        doc = json.loads(row["summary_json"]) if row["summary_json"] is not None else json.loads(row["body"])
        if doc.get("status") == "cancelled":
            return True
        return bool((doc.get("metadata") or {}).get("cancel_requested"))

    @staticmethod
    def _parse_iso_timestamp(raw: str | None) -> float | None:
        if not raw or not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def gc_expired_resources(
        self,
        *,
        workspaces_dir: Path | None = None,
        git_origins_dir: Path | None = None,
        artifacts_dir: Path | None = None,
        retention_seconds: float = 86400.0,
    ) -> dict:
        """回收已进入终态且超过保留窗口的工作区/worktree、数据库归档快照与陈旧内核锁文件。"""
        from .execution_lock import cleanup_stale_locks
        from .git_repo import _git, _has_own_git_marker, cleanup_task_worktree
        from .tools import prune_workspace_lock

        if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, (int, float)):
            raise ValueError("retention_seconds_invalid")
        retention = float(retention_seconds)
        cutoff_ts = time.time() - retention
        terminal_statuses = {"completed", "failed", "budget_exceeded", "cancelled", "dead_letter"}
        active_lease_states = {"queued", "claimed", "waiting_approval"}

        with self._connect() as con:
            run_rows = con.execute("SELECT id, created_at, updated_at_col, body FROM runs").fetchall()
            lease_rows = con.execute("SELECT run_id, queue_state FROM run_leases").fetchall()

        lease_map = {r["run_id"]: r["queue_state"] for r in lease_rows}
        active_run_ids: set[str] = set()
        terminal_runs: dict[str, dict] = {}
        expired_runs: dict[str, dict] = {}

        for row in run_rows:
            rid = row["id"]
            doc = json.loads(row["body"])
            status = doc.get("status")
            q_state = lease_map.get(rid)
            if status not in terminal_statuses or q_state in active_lease_states:
                active_run_ids.add(rid)
                continue
            terminal_runs[rid] = doc
            ts_str = row["updated_at_col"] or doc.get("updated_at") or doc.get("created_at") or row["created_at"]
            ts_val = self._parse_iso_timestamp(ts_str)
            if retention <= 0 or (ts_val is not None and ts_val <= cutoff_ts):
                expired_runs[rid] = doc

        removed_workspaces = 0
        for doc in expired_runs.values():
            meta_ws = (doc.get("metadata") or {}).get("workspace")
            if isinstance(meta_ws, str) and meta_ws.strip():
                with contextlib.suppress(Exception):
                    prune_workspace_lock(Path(meta_ws))
        if workspaces_dir is not None:
            ws_root = Path(workspaces_dir).resolve()
            if ws_root.is_dir():
                visited_paths: set[Path] = set()
                for rid, doc in expired_runs.items():
                    doc_meta = doc.get("metadata") or {}
                    if doc_meta.get("direct_workspace"):
                        # 绝不删除用户本地直连的真实目录，仅释放进程内锁对象。
                        continue
                    candidates: list[Path] = []
                    meta_ws = doc_meta.get("workspace")
                    if isinstance(meta_ws, str) and meta_ws.strip():
                        resolved_ws = Path(meta_ws).resolve()
                        if resolved_ws != ws_root and resolved_ws.is_relative_to(ws_root):
                            candidates.append(resolved_ws)
                    by_id = (ws_root / rid).resolve()
                    if by_id != ws_root and by_id.is_relative_to(ws_root):
                        candidates.append(by_id)
                    for target in candidates:
                        if target in visited_paths:
                            continue
                        visited_paths.add(target)
                        prune_workspace_lock(target)
                        if not target.exists():
                            continue
                        with contextlib.suppress(Exception):
                            if cleanup_task_worktree(target):
                                removed_workspaces += 1
                                prune_workspace_lock(target)

        pruned_origins = 0
        if git_origins_dir is not None:
            origins_root = Path(git_origins_dir).resolve()
            if origins_root.is_dir():
                for child in origins_root.iterdir():
                    if child.is_dir() and not child.is_symlink() and _has_own_git_marker(child):
                        with contextlib.suppress(Exception):
                            _git(["worktree", "prune"], cwd=child)
                            pruned_origins += 1

        removed_archives = 0
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            arch_rows = con.execute("SELECT run_id, updated_at FROM workspace_archives").fetchall()
            for arow in arch_rows:
                arid = arow["run_id"]
                arch_ts = self._parse_iso_timestamp(arow["updated_at"])
                should_delete = arid in expired_runs or (
                    arid in terminal_runs and (retention <= 0 or (arch_ts is not None and arch_ts <= cutoff_ts))
                )
                if should_delete:
                    cur = con.execute("DELETE FROM workspace_archives WHERE run_id=?", (arid,))
                    removed_archives += int(cur.rowcount or 0)

        removed_artifacts = 0
        eff_artifacts_dir = artifacts_dir
        if eff_artifacts_dir is None and self.pg_config is None:
            eff_artifacts_dir = self.path.parent / "artifacts"
        if eff_artifacts_dir is not None:
            art_root = Path(eff_artifacts_dir).resolve()
            if art_root.is_dir() and not art_root.is_symlink():
                for rid in expired_runs:
                    target_art = (art_root / rid).resolve()
                    if (
                        target_art != art_root
                        and target_art.is_relative_to(art_root)
                        and target_art.exists()
                        and not target_art.is_symlink()
                    ):
                        with contextlib.suppress(OSError):
                            shutil.rmtree(target_art)
                            removed_artifacts += 1

        removed_locks = 0
        if self.pg_config is None:
            removed_locks = cleanup_stale_locks(
                self.path,
                active_run_ids=active_run_ids,
                max_age_seconds=max(0.0, retention),
            )

        return {
            "removed_workspaces": removed_workspaces,
            "removed_archives": removed_archives,
            "removed_artifacts": removed_artifacts,
            "removed_locks": removed_locks,
            "pruned_origins": pruned_origins,
            "active_runs": len(active_run_ids),
        }

"""SQLite / PostgreSQL 双后端事务存储、队列租约、单调递增 Fencing Token 与全生命周期账本。

核心持久化与分布式协调不变量：
1. 双层乐观并发与脑裂防护（epoch + state_version）：
   - run_leases.epoch 作为 Kleppmann 意义下的单调递增 Fencing Token，拦截租约超时后苏醒的僵尸 Worker；
   - runs.state_version 在单次保存时执行 CAS（WHERE state_version=?），防止同世代内的并发写互相静默覆盖。
2. 统一审批提案状态机（文件补丁集 + 非文件副作用动作）：
   - proposals 表同时承载文件补丁（single_file / changeset）与高风险动作（command / gui_action / mcp_call）；
   - 部分唯一索引 proposals_operation(run_id, operation_id) 弥合“创建提案后、工具账本落盘前”的崩溃窗口，
     而 mark_action_proposal_executed 持久化动作回执则弥合“动作已执行、对话历史未落盘”的重放窗口。
3. 多轮连续会话的协议自愈与线性预算延展（append_user_turn）：
   - 若上一轮因取消或超预算中断在 assistant.tool_use 之后，自动补齐 1:1 合成 tool_result 闭合块，防止后续请求
     触发模型 API 400 协议错误或破坏 ContextManager 原子消息组配对；
   - 基于“当前真实已消耗量 + 单轮基线额度”线性延展预算，既不饿死后续轮次，也不因多轮短对话累积失控配额余量。
4. 离线睡眠记录（dream_runs）与宿主直连目录 GC 绝对豁免（direct_workspace）：
   - dream_runs 按 (tenant_id, project_slug) 独立索引记录离线记忆整理报告，不污染主任务队列；
   - gc_expired_resources 严格检查 direct_workspace，严禁删除用户真实代码库，仅回收锁与过期截图产物。
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import contextmanager
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable
import uuid

from .observability import current_span_id, current_trace_id, redact_secrets

PG_QUEUE_NOTIFY_CHANNEL = "agent_lab_queue"


class StaleEpochError(RuntimeError):
    """旧 Worker 持有的 fencing token 已过期，拒绝写入状态、工具账本或补丁产物。"""


class StorageConfigError(RuntimeError, ValueError):
    """存储后端 DSN 配置非法或缺少对应数据库驱动。"""


def _match_dollar_quote_tag(sql: str, start: int) -> str | None:
    """若 sql[start:] 指向合法 PostgreSQL dollar-quote 分隔符（$$ 或 $tag$），返回完整分隔符。"""
    length = len(sql)
    if start >= length or sql[start] != "$":
        return None
    if start + 1 < length and sql[start + 1] == "$":
        return "$$"
    idx = start + 1
    if idx >= length or not (sql[idx].isalpha() or sql[idx] == "_"):
        return None
    idx += 1
    while idx < length and (sql[idx].isalnum() or sql[idx] == "_"):
        idx += 1
    if idx < length and sql[idx] == "$":
        return sql[start : idx + 1]
    return None


def _translate_qmark_to_pyformat(sql: str) -> str:
    """将 SQLite 风格 `?` 占位符翻译为 psycopg `%s`，并将所有字面 `%` 转义为 `%%`；安全跳过注释、引号字面量、双引号标识符与 PostgreSQL dollar-quote 中的 `?`。"""
    stripped = sql.strip()
    if stripped.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    out: list[str] = []
    idx = 0
    length = len(sql)
    while idx < length:
        ch = sql[idx]
        # 1) SQL 单行注释 -- ... \n
        if ch == "-" and idx + 1 < length and sql[idx + 1] == "-":
            end = sql.find("\n", idx + 2)
            if end == -1:
                out.append(sql[idx:].replace("%", "%%"))
                break
            out.append(sql[idx : end + 1].replace("%", "%%"))
            idx = end + 1
            continue
        # 2) SQL 块注释 /* ... */（支持 PostgreSQL 嵌套块注释）
        if ch == "/" and idx + 1 < length and sql[idx + 1] == "*":
            out.append("/*")
            idx += 2
            depth = 1
            while idx < length and depth > 0:
                if sql[idx] == "/" and idx + 1 < length and sql[idx + 1] == "*":
                    out.append("/*")
                    idx += 2
                    depth += 1
                elif sql[idx] == "*" and idx + 1 < length and sql[idx + 1] == "/":
                    out.append("*/")
                    idx += 2
                    depth -= 1
                else:
                    cur = sql[idx]
                    out.append("%%" if cur == "%" else cur)
                    idx += 1
            continue
        # 3) 单引号字符串字面量 '...'（兼容 '' 转义及 PostgreSQL E'...' 反斜杠转义）
        if ch == "'":
            is_escape_str = (
                idx > 0
                and sql[idx - 1] in {"e", "E"}
                and (idx == 1 or not (sql[idx - 2].isalnum() or sql[idx - 2] == "_"))
            )
            out.append("'")
            idx += 1
            while idx < length:
                cur = sql[idx]
                if is_escape_str and cur == "\\" and idx + 1 < length:
                    out.append(sql[idx : idx + 2].replace("%", "%%"))
                    idx += 2
                    continue
                if cur == "'":
                    out.append("'")
                    if idx + 1 < length and sql[idx + 1] == "'":
                        out.append("'")
                        idx += 2
                        continue
                    idx += 1
                    break
                out.append("%%" if cur == "%" else cur)
                idx += 1
            continue
        # 4) 双引号标识符 "..."（兼容 "" 转义）
        if ch == '"':
            out.append('"')
            idx += 1
            while idx < length:
                cur = sql[idx]
                if cur == '"':
                    out.append('"')
                    if idx + 1 < length and sql[idx + 1] == '"':
                        out.append('"')
                        idx += 2
                        continue
                    idx += 1
                    break
                out.append("%%" if cur == "%" else cur)
                idx += 1
            continue
        # 5) PostgreSQL dollar-quoted 字符串 $$...$$ / $tag$...$tag$
        if ch == "$":
            tag = _match_dollar_quote_tag(sql, idx)
            if tag is not None:
                out.append(tag)
                idx += len(tag)
                close_pos = sql.find(tag, idx)
                if close_pos == -1:
                    out.append(sql[idx:].replace("%", "%%"))
                    break
                out.append(sql[idx : close_pos + len(tag)].replace("%", "%%"))
                idx = close_pos + len(tag)
                continue
        if ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        idx += 1
    return "".join(out)


@dataclass(frozen=True)
class PostgresBackendConfig:
    """多节点生产环境 PostgreSQL 连接、可选连接池与 FOR UPDATE SKIP LOCKED 队列锁配置。"""

    dsn: str
    connect_timeout: int = 10
    application_name: str = "agent-systems-lab"
    claim_lock_clause: str = "FOR UPDATE SKIP LOCKED"
    pool_size: int = 4
    pool_max_size: int | None = None
    pool_timeout: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.pool_size, bool) or not isinstance(self.pool_size, int) or self.pool_size < 0:
            raise StorageConfigError("pg_pool_size_invalid")
        if self.pool_max_size is not None and (
            isinstance(self.pool_max_size, bool)
            or not isinstance(self.pool_max_size, int)
            or self.pool_max_size < max(1, self.pool_size)
        ):
            raise StorageConfigError("pg_pool_max_size_invalid")

    def translate_sql(self, sql: str) -> str:
        return _translate_qmark_to_pyformat(sql)

    def build_claim_sql(self, *, specific_run: bool = False) -> str:
        if specific_run:
            return (
                "SELECT * FROM run_leases WHERE run_id=? AND "
                "(queue_state IN ('idle', 'queued') OR (queue_state='claimed' AND lease_until <= ?)) "
                f"{self.claim_lock_clause}"
            )
        return (
            "SELECT * FROM run_leases WHERE queue_state='queued' "
            "OR (queue_state='claimed' AND lease_until <= ?) "
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

    def __init__(
        self,
        raw_con: Any,
        config: PostgresBackendConfig,
        *,
        close_raw: bool = True,
    ) -> None:
        self._raw = raw_con
        self._config = config
        self._in_tx = False
        self._close_raw = close_raw

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        translated = self._config.translate_sql(sql)
        if translated == "BEGIN":
            self._raw.execute("BEGIN")
            self._in_tx = True
            return self._raw
        if params:
            return self._raw.execute(translated, params)
        if "%" in translated:
            return self._raw.execute(translated, ())
        return self._raw.execute(translated)

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
        if self._close_raw:
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


def _heal_dangling_tool_uses(
    messages: list[dict[str, Any]],
    *,
    reason_code: str = "forked_branch",
    reason_error: str = "会话已在此处分叉，未完成工具调用已自动闭合。",
) -> bool:
    """若截断或分叉后的对话末尾残留孤立 user.tool_result 或悬空 assistant.tool_use，自动修复为合法原子配对。"""
    if not messages:
        return False
    healed = False
    # 1. 若末尾残留孤立或配对不完整的 user.tool_result，安全弹出
    while messages:
        last = messages[-1]
        if (
            isinstance(last, dict)
            and last.get("role") == "user"
            and isinstance(last.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"])
        ):
            prev = messages[-2] if len(messages) >= 2 else None
            prev_use_ids = (
                [
                    b["id"]
                    for b in (prev.get("content") or [])
                    if isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and isinstance(b.get("id"), str)
                    and b.get("id")
                ]
                if isinstance(prev, dict)
                and prev.get("role") == "assistant"
                and isinstance(prev.get("content"), list)
                else []
            )
            total_res_blocks = sum(
                1 for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            )
            res_ids = [
                b["tool_use_id"]
                for b in last["content"]
                if isinstance(b, dict)
                and b.get("type") == "tool_result"
                and isinstance(b.get("tool_use_id"), str)
                and b.get("tool_use_id")
            ]
            if (
                not prev_use_ids
                or len(res_ids) != total_res_blocks
                or len(res_ids) != len(prev_use_ids)
                or set(res_ids) != set(prev_use_ids)
            ):
                messages.pop()
                healed = True
                continue
        break

    # 2. 若末尾是含 tool_use 的 assistant 消息，补齐 1:1 合成 tool_result 闭合块
    if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
        content = messages[-1].get("content")
        if isinstance(content, list):
            orphan_uses = [
                b
                for b in content
                if isinstance(b, dict)
                and b.get("type") == "tool_use"
                and isinstance(b.get("id"), str)
                and b.get("id")
            ]
            if orphan_uses:
                synth_blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": json.dumps(
                            {
                                "ok": False,
                                "code": reason_code,
                                "error": reason_error,
                            },
                            ensure_ascii=False,
                        ),
                        "is_error": True,
                    }
                    for tu in orphan_uses
                ]
                messages.append({"role": "user", "content": synth_blocks})
                healed = True
    return healed


class Storage:
    """一个文件保存状态；写入事务用 WAL 并限制锁等待时间，支持通过 DSN 切换后端与可选 PG 连接池。"""

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
        self._pg_pool: Any = None
        self._pg_pool_lock = threading.Lock()
        self._pg_pool_unavailable = False

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
    def from_dsn(
        cls,
        dsn: str,
        *,
        synchronous: str | None = None,
        pool_size: int | None = None,
    ) -> Storage:
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
            resolved_pool_size = pool_size
            if resolved_pool_size is None:
                raw_env_pool = os.environ.get("AGENT_LAB_PG_POOL_SIZE", "").strip()
                if raw_env_pool:
                    try:
                        resolved_pool_size = int(raw_env_pool)
                    except ValueError as exc:
                        raise StorageConfigError("pg_pool_size_invalid") from exc
                else:
                    resolved_pool_size = 4
            return cls(
                None,
                synchronous=synchronous,
                pg_config=PostgresBackendConfig(dsn=cleaned, pool_size=resolved_pool_size),
            )
        if "://" not in cleaned:
            return cls(Path(cleaned), synchronous=synchronous)
        scheme = cleaned.split("://", 1)[0]
        raise StorageConfigError(f"unsupported_storage_dsn_scheme: {scheme}")

    def _get_or_create_pg_pool(self) -> Any:
        """惰性创建实例级 psycopg_pool.ConnectionPool；缺失 psycopg_pool 或 pool_size<=0 时优雅回退直连。"""
        if self.pg_config is None or self.pg_config.pool_size <= 0 or self._pg_pool_unavailable:
            return None
        if self._pg_pool is not None:
            return self._pg_pool
        with self._pg_pool_lock:
            if self._pg_pool is not None:
                return self._pg_pool
            if self._pg_pool_unavailable:
                return None
            try:
                pool_mod = importlib.import_module("psycopg_pool")
                rows_mod = importlib.import_module("psycopg.rows")
            except ImportError:
                self._pg_pool_unavailable = True
                return None
            min_size = max(1, int(self.pg_config.pool_size))
            max_size = (
                max(min_size, int(self.pg_config.pool_max_size))
                if self.pg_config.pool_max_size is not None
                else max(min_size, min_size * 2)
            )
            pool_cls = getattr(pool_mod, "ConnectionPool")
            pool = pool_cls(
                conninfo=self.pg_config.dsn,
                min_size=min_size,
                max_size=max_size,
                timeout=float(self.pg_config.pool_timeout),
                kwargs={
                    "connect_timeout": self.pg_config.connect_timeout,
                    "row_factory": rows_mod.dict_row,
                    "application_name": self.pg_config.application_name,
                    "autocommit": True,
                },
                open=True,
            )
            self._pg_pool = pool
            return pool

    def close(self) -> None:
        """关闭实例持有的 PostgreSQL 连接池（幂等；SQLite 后端安全空操作）。"""
        with self._pg_pool_lock:
            pool = self._pg_pool
            self._pg_pool = None
        if pool is not None:
            with contextlib.suppress(Exception):
                pool.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False

    async def run_async(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """在工作线程池中执行同步存储操作，避免阻塞事件循环并自动传播 contextvars。"""
        target = getattr(self, fn) if isinstance(fn, str) else fn
        return await asyncio.to_thread(target, *args, **kwargs)

    @staticmethod
    def _sqlite_columns(con: Any, table_name: str) -> set[str]:
        return {row["name"] for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()}

    def _migrations(self) -> tuple[tuple[int, str, Callable[[Any, bool], None]], ...]:
        """返回跨 SQLite 与 PostgreSQL 共享的有序幂等版本化迁移清单。"""

        def _m1_baseline_core_schema(con: Any, is_pg: bool) -> None:
            if is_pg:
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
                    """CREATE TABLE IF NOT EXISTS run_steering_messages (
                        id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                        prompt TEXT NOT NULL, actor_id TEXT, created_at TEXT NOT NULL, consumed_at TEXT
                    )""",
                    "CREATE INDEX IF NOT EXISTS run_steering_messages_pending ON run_steering_messages(run_id, consumed_at, id)",
                )
                for stmt in ddl_statements:
                    con.execute(stmt)
                return

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
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS run_steering_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    prompt TEXT NOT NULL,
                    actor_id TEXT,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS run_steering_messages_pending "
                "ON run_steering_messages(run_id, consumed_at, id)"
            )

            run_cols = self._sqlite_columns(con, "runs")
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

            proposal_cols = self._sqlite_columns(con, "proposals")
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
            lease_cols = self._sqlite_columns(con, "run_leases")
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

        def _m2_proposals_fifo_seq(con: Any, is_pg: bool) -> None:
            if is_pg:
                con.execute("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS seq BIGSERIAL")
            else:
                proposal_cols = self._sqlite_columns(con, "proposals")
                if "seq" not in proposal_cols:
                    con.execute("ALTER TABLE proposals ADD COLUMN seq INTEGER")
                con.execute("UPDATE proposals SET seq = rowid WHERE seq IS NULL")
            con.execute("CREATE INDEX IF NOT EXISTS proposals_run_seq ON proposals(run_id, seq)")

        def _m3_run_checkpoints_table(con: Any, is_pg: bool) -> None:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    turn_index INTEGER NOT NULL DEFAULT 0,
                    message_idx INTEGER NOT NULL DEFAULT 0,
                    label TEXT NOT NULL DEFAULT '',
                    file_manifest_json TEXT NOT NULL DEFAULT '{}',
                    git_tree TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS run_checkpoints_run_turn "
                "ON run_checkpoints(run_id, turn_index, created_at)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS run_checkpoints_tenant ON run_checkpoints(tenant_id, run_id)"
            )

        def _m4_security_audit_logs_table(con: Any, is_pg: bool) -> None:
            id_col = "BIGSERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS security_audit_logs (
                    id {id_col},
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{{}}',
                    trace_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS security_audit_logs_tenant_created "
                "ON security_audit_logs(tenant_id, created_at DESC, id DESC)"
            )

        def _m5_events_trace_span_columns(con: Any, is_pg: bool) -> None:
            if is_pg:
                con.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS trace_id TEXT")
                con.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS span_id TEXT")
            else:
                event_cols = self._sqlite_columns(con, "events")
                if "trace_id" not in event_cols:
                    con.execute("ALTER TABLE events ADD COLUMN trace_id TEXT")
                if "span_id" not in event_cols:
                    con.execute("ALTER TABLE events ADD COLUMN span_id TEXT")
            con.execute(
                "CREATE INDEX IF NOT EXISTS events_trace ON events(trace_id) WHERE trace_id IS NOT NULL"
            )

        def _m6_runs_session_management_columns(con: Any, is_pg: bool) -> None:
            if is_pg:
                con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS title TEXT")
                con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS archived INTEGER NOT NULL DEFAULT 0")
            else:
                run_cols = self._sqlite_columns(con, "runs")
                if "title" not in run_cols:
                    con.execute("ALTER TABLE runs ADD COLUMN title TEXT")
                if "archived" not in run_cols:
                    con.execute("ALTER TABLE runs ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            con.execute(
                "CREATE INDEX IF NOT EXISTS runs_tenant_archived_created "
                "ON runs(tenant_id, archived, created_at DESC, id DESC)"
            )
            null_status_rows = con.execute(
                "SELECT id, body FROM runs WHERE status_col IS NULL"
            ).fetchall()
            for row in null_status_rows:
                try:
                    doc = json.loads(row["body"])
                except Exception:
                    continue
                status_val = doc.get("status")
                if status_val is not None:
                    con.execute("UPDATE runs SET status_col=? WHERE id=?", (str(status_val), row["id"]))

        def _m7_a2a_context_and_push_configs(con: Any, is_pg: bool) -> None:
            if is_pg:
                con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS a2a_context_id TEXT")
            else:
                run_cols = self._sqlite_columns(con, "runs")
                if "a2a_context_id" not in run_cols:
                    con.execute("ALTER TABLE runs ADD COLUMN a2a_context_id TEXT")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_a2a_context_id "
                "ON runs(a2a_context_id, tenant_id, created_at DESC) "
                "WHERE a2a_context_id IS NOT NULL"
            )
            backfill_rows = con.execute(
                "SELECT id, body FROM runs WHERE a2a_context_id IS NULL"
            ).fetchall()
            for row in backfill_rows:
                try:
                    doc = json.loads(row["body"])
                except Exception:
                    continue
                meta = doc.get("metadata") if isinstance(doc, dict) else None
                ctx_val = meta.get("a2a_context_id") if isinstance(meta, dict) else None
                if isinstance(ctx_val, str) and ctx_val.strip():
                    con.execute(
                        "UPDATE runs SET a2a_context_id=? WHERE id=?",
                        (ctx_val.strip()[:256], row["id"]),
                    )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_push_configs (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    token_hash TEXT,
                    secret TEXT,
                    events_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_a2a_push_configs_tenant "
                "ON a2a_push_configs(tenant_id, task_id)"
            )

        def _m8_trigger_definitions_and_fires(con: Any, is_pg: bool) -> None:
            id_col = "BIGSERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS trigger_definitions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    cron_expr TEXT,
                    webhook_secret TEXT,
                    prompt_template TEXT NOT NULL,
                    workspace_path TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_fired_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_trigger_definitions_tenant_enabled "
                "ON trigger_definitions(tenant_id, enabled, created_at DESC)"
            )
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS trigger_fires (
                    id {id_col},
                    trigger_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    status TEXT NOT NULL DEFAULT 'dispatched',
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_trigger_fires_trigger_created "
                "ON trigger_fires(trigger_id, id DESC)"
            )

        return (
            (1, "baseline_core_schema", _m1_baseline_core_schema),
            (2, "proposals_fifo_seq", _m2_proposals_fifo_seq),
            (3, "run_checkpoints_table", _m3_run_checkpoints_table),
            (4, "security_audit_logs_table", _m4_security_audit_logs_table),
            (5, "events_trace_span_columns", _m5_events_trace_span_columns),
            (6, "runs_session_management_columns", _m6_runs_session_management_columns),
            (7, "a2a_context_and_push_configs", _m7_a2a_context_and_push_configs),
            (8, "trigger_definitions_and_fires", _m8_trigger_definitions_and_fires),
        )

    def _run_migrations(self, con: Any) -> None:
        """在调用方已开启的排他/常规事务内执行幂等版本化 Schema 迁移。"""
        is_pg = self.pg_config is not None
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            int(row["version"])
            for row in con.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, name, migrate_fn in self._migrations():
            if version in applied:
                continue
            migrate_fn(con, is_pg)
            con.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?) "
                "ON CONFLICT(version) DO NOTHING",
                (version, name, _now()),
            )

    def applied_migrations(self) -> list[dict[str, Any]]:
        """返回已按版本号升序落库执行的全部 schema_migrations 记录。"""
        with self._connect() as con:
            rows = con.execute(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    @property
    def _is_postgres(self) -> bool:
        return self.pg_config is not None or self.backend in {"postgresql", "postgres"}

    def _pg_lock(self, clause: str = "FOR UPDATE") -> str:
        """在 PostgreSQL 后端附加行级锁子句（如 FOR UPDATE / FOR KEY SHARE），SQLite 返回空串。"""
        return f" {clause}" if self._is_postgres else ""

    def _notify_queue_tx(self, con: Any, run_id: str, action: str = "execute") -> None:
        """在 PostgreSQL 写事务内最佳努力发送 `pg_notify('agent_lab_queue', payload)` 唤醒空闲 Worker；SQLite 下安全忽略。"""
        if not self._is_postgres:
            return
        payload = _dump({"run_id": run_id, "action": action, "ts": _now()})
        with contextlib.suppress(Exception):
            con.execute(f"SELECT pg_notify('{PG_QUEUE_NOTIFY_CHANNEL}', ?)", (payload,))

    def _initialize_postgres_schema(self) -> None:
        with self._connect() as con:
            con.execute("BEGIN")
            con.execute("SELECT pg_advisory_xact_lock(7281946510293847)")
            self._run_migrations(con)

    def _initialize_schema(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            # 全程在单个排他写事务内完成版本化建表与旧库加法迁移，保护既有数据行与并发启动幂等性。
            con.execute("BEGIN IMMEDIATE")
            self._run_migrations(con)

    @contextmanager
    def _connect(self):
        if self.pg_config is not None:
            pool = self._get_or_create_pg_pool()
            if pool is not None:
                with pool.connection() as raw_con:
                    adapter = _PostgresConnectionAdapter(raw_con, self.pg_config, close_raw=False)
                    with adapter:
                        yield adapter
                return
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
                "title",
                "archived",
                "forked_from_run_id",
                "forked_at_message_idx",
                "a2a_context_id",
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

    @staticmethod
    def _extract_a2a_context_id(metadata: Any) -> str | None:
        if not isinstance(metadata, dict):
            return None
        raw = metadata.get("a2a_context_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:256]
        return None

    def create_run(
        self,
        prompt: str,
        mode: str = "agent",
        provider: str = "demo",
        *,
        tenant_id: str = "local",
        metadata: dict[str, Any] | None = None,
        a2a_context_id: str | None = None,
    ) -> dict:
        now = _now()
        clean_tenant = str(tenant_id or "local").strip() or "local"
        run_metadata: dict[str, Any] = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        if clean_tenant != "local":
            run_metadata.setdefault("tenant_id", clean_tenant)
        if isinstance(a2a_context_id, str) and a2a_context_id.strip():
            run_metadata["a2a_context_id"] = a2a_context_id.strip()[:256]
        resolved_a2a_ctx = self._extract_a2a_context_id(run_metadata)
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
            "metadata": run_metadata,
        }
        summary = self._build_run_summary(run, tenant_id=clean_tenant)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO runs(id, created_at, body, state_version, status_col, updated_at_col, "
                "tenant_id, summary_json, title, archived, a2a_context_id) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL, 0, ?)",
                (
                    run["id"],
                    now,
                    _dump(run),
                    run["status"],
                    now,
                    clean_tenant,
                    _dump(summary),
                    resolved_a2a_ctx,
                ),
            )
            con.execute(
                "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, lease_until, heartbeat_at, updated_at) "
                "VALUES (?, 'idle', 'execute', NULL, 0, 0, NULL, NULL, ?)",
                (run["id"], now),
            )
        return run

    def _verify_epoch_tx(
        self,
        con: Any,
        run_id: str,
        expected_epoch: int | None,
        *,
        for_update: bool = False,
    ) -> Any:
        lock_clause = self._pg_lock("FOR UPDATE" if for_update else "FOR KEY SHARE")
        row = con.execute(
            f"SELECT epoch, queue_state, owner_id, lease_until FROM run_leases WHERE run_id=?{lock_clause}",
            (run_id,),
        ).fetchone()
        if expected_epoch is None:
            if row is not None and row["queue_state"] == "claimed" and (row["lease_until"] or 0.0) > time.time():
                raise StaleEpochError(f"fencing_inline_blocked_by_claimed_lease: run={run_id}")
            return row
        if row is None or row["epoch"] != expected_epoch or row["queue_state"] != "claimed":
            actual = row["epoch"] if row is not None else None
            state = row["queue_state"] if row is not None else None
            raise StaleEpochError(
                f"fencing_token_rejected: run={run_id} expected_epoch={expected_epoch} actual_epoch={actual} state={state}"
            )
        return row

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
            row = con.execute(
                f"SELECT * FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
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
            row = con.execute(
                f"SELECT * FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
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
            self._notify_queue_tx(con, run_id, action=action)
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
                        "(queue_state IN ('idle', 'queued') OR (queue_state='claimed' AND lease_until <= ?))"
                        f"{self._pg_lock('FOR UPDATE SKIP LOCKED')}",
                        (run_id, ts),
                    ).fetchone()
                else:
                    row = con.execute(
                        "SELECT * FROM run_leases WHERE queue_state='queued' "
                        "OR (queue_state='claimed' AND lease_until <= ?) "
                        f"ORDER BY updated_at ASC, run_id ASC LIMIT 1{self._pg_lock('FOR UPDATE SKIP LOCKED')}",
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
                        f"SELECT body, state_version, tenant_id FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                        (candidate_id,),
                    ).fetchone()
                    if run_row is not None:
                        run_doc = json.loads(run_row["body"])
                        run_doc["status"] = "failed"
                        run_doc["error"] = "DeadLetterExceeded"
                        run_doc["updated_at"] = now
                        run_doc["state_version"] = (
                            int(run_row["state_version"] or run_doc.get("state_version") or 1) + 1
                        )
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
                        "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                        "VALUES (?, 'run_dead_lettered', ?, ?, ?, ?)",
                        (candidate_id, now, _dump(dlq_payload), current_trace_id(), current_span_id()),
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
                    "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                    "VALUES (?, 'lease_claimed', ?, ?, ?, ?)",
                    (candidate_id, now, _dump(payload), current_trace_id(), current_span_id()),
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
            row = self._verify_epoch_tx(con, run_id, epoch, for_update=True)
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
                f"SELECT epoch, queue_state, owner_id FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
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

    @staticmethod
    def make_run_cursor(run: dict[str, Any]) -> str:
        """为 list_runs 键集分页生成确定性游标 `<created_at>|<id>`。"""
        return f"{run['created_at']}|{run['id']}"

    def list_runs(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 200,
        query: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
    ) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("run_limit_invalid")
        clean_query = str(query).strip().lower() if query is not None and str(query).strip() else None
        clean_status = str(status).strip() if status is not None and str(status).strip() else None
        clean_cursor = str(cursor).strip() if cursor is not None and str(cursor).strip() else None

        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if not include_archived:
            clauses.append("COALESCE(archived, 0) = 0")
        if clean_status is not None:
            clauses.append("status_col = ?")
            params.append(clean_status)

        with self._connect() as con:
            if clean_cursor is not None:
                if "|" in clean_cursor:
                    cursor_created, cursor_id = clean_cursor.rsplit("|", 1)
                    clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
                    params.extend([cursor_created, cursor_created, cursor_id])
                else:
                    anchor = con.execute(
                        "SELECT created_at, id FROM runs WHERE id = ?", (clean_cursor,)
                    ).fetchone()
                    if anchor is not None:
                        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
                        params.extend([anchor["created_at"], anchor["created_at"], anchor["id"]])
                    elif self._parse_iso_timestamp(clean_cursor) is not None:
                        clauses.append("created_at < ?")
                        params.append(clean_cursor)
                    else:
                        return []

            if clean_query is not None:
                escaped = clean_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                clauses.append(
                    "(LOWER(COALESCE(title, '')) LIKE ? ESCAPE '\\' "
                    "OR LOWER(COALESCE(summary_json, body)) LIKE ? ESCAPE '\\' "
                    "OR LOWER(id) LIKE ? ESCAPE '\\')"
                )
                params.extend([pattern, pattern, pattern])

            where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            if clean_query is None:
                rows = con.execute(
                    f"SELECT id, title, archived, summary_json, body FROM runs "
                    f"{where_sql}ORDER BY created_at DESC, id DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    f"SELECT id, title, archived, summary_json, body FROM runs "
                    f"{where_sql}ORDER BY created_at DESC, id DESC",
                    tuple(params),
                ).fetchall()

        results: list[dict] = []
        for row in rows:
            doc = json.loads(row["summary_json"]) if row["summary_json"] is not None else json.loads(row["body"])
            if row["title"] is not None and "title" not in doc:
                doc["title"] = row["title"]
            doc.setdefault("archived", bool(row["archived"]))
            if clean_query is not None:
                haystack = "\n".join(
                    str(part).lower()
                    for part in (
                        doc.get("id"),
                        doc.get("title"),
                        doc.get("prompt"),
                        doc.get("answer"),
                        (doc.get("metadata") or {}).get("title"),
                    )
                    if part
                )
                if clean_query not in haystack:
                    continue
            results.append(doc)
            if len(results) >= limit:
                break
        return results

    def rename_run(self, run_id: str, title: str, tenant_id: str | None = None) -> dict:
        """原子重命名会话标题，支持多租户隔离校验并同步更新 summary_json 与 title 列。"""
        if not isinstance(title, str):
            raise ValueError("run_title_invalid")
        clean_title = title.strip()
        if not 1 <= len(clean_title) <= 200:
            raise ValueError("run_title_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"SELECT body, state_version, tenant_id, archived FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError("run_not_found")
            row_tenant = str(row["tenant_id"] or "local")
            if tenant_id is not None and row_tenant != tenant_id:
                raise KeyError("run_not_found")
            run = json.loads(row["body"])
            run["title"] = clean_title
            run.setdefault("metadata", {})["title"] = clean_title
            run["archived"] = bool(row["archived"])
            run["updated_at"] = now
            run["state_version"] = int(row["state_version"] or 1) + 1
            summary = self._build_run_summary(run, tenant_id=row_tenant)
            con.execute(
                "UPDATE runs SET body=?, title=?, state_version=state_version+1, updated_at_col=?, summary_json=? "
                "WHERE id=?",
                (_dump(run), clean_title, now, _dump(summary), run_id),
            )
            con.execute(
                "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                "VALUES (?, 'run_renamed', ?, ?, ?, ?)",
                (run_id, now, _dump({"title": clean_title}), current_trace_id(), current_span_id()),
            )
        return run

    def archive_run(
        self, run_id: str, archived: bool = True, tenant_id: str | None = None
    ) -> dict:
        """原子归档或取消归档指定会话，支持多租户隔离校验。"""
        if not isinstance(archived, bool):
            raise ValueError("archived_must_be_boolean")
        now = _now()
        archived_int = 1 if archived else 0
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"SELECT body, state_version, tenant_id, title FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError("run_not_found")
            row_tenant = str(row["tenant_id"] or "local")
            if tenant_id is not None and row_tenant != tenant_id:
                raise KeyError("run_not_found")
            run = json.loads(row["body"])
            if row["title"] is not None and "title" not in run:
                run["title"] = row["title"]
            run["archived"] = archived
            meta = run.setdefault("metadata", {})
            meta["archived"] = archived
            if archived:
                meta["archived_at"] = now
            else:
                meta.pop("archived_at", None)
            run["updated_at"] = now
            run["state_version"] = int(row["state_version"] or 1) + 1
            summary = self._build_run_summary(run, tenant_id=row_tenant)
            con.execute(
                "UPDATE runs SET body=?, archived=?, state_version=state_version+1, updated_at_col=?, summary_json=? "
                "WHERE id=?",
                (_dump(run), archived_int, now, _dump(summary), run_id),
            )
        return run

    def delete_run(self, run_id: str, tenant_id: str | None = None) -> bool:
        """原子删除会话及其全部从属记录（严禁触碰 append-only 的 security_audit_logs）；存活租约运行中时拒绝删除。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            lease_row = con.execute(
                f"SELECT queue_state, lease_until FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            row = con.execute(
                f"SELECT id, tenant_id FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            row_tenant = str(row["tenant_id"] or "local")
            if tenant_id is not None and row_tenant != tenant_id:
                return False
            if (
                lease_row is not None
                and lease_row["queue_state"] == "claimed"
                and (lease_row["lease_until"] or 0.0) > time.time()
            ):
                raise ValueError("run_currently_claimed")
            for table in (
                "run_checkpoints",
                "run_steering_messages",
                "workspace_archives",
                "tool_calls",
                "proposals",
                "events",
                "run_leases",
            ):
                con.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM a2a_push_configs WHERE task_id=?", (run_id,))
            cur = con.execute("DELETE FROM runs WHERE id=?", (run_id,))
            return int(cur.rowcount or 0) > 0

    def fork_run(
        self,
        source_run_id: str,
        *,
        up_to_message_idx: int | None = None,
        tenant_id: str | None = None,
        title: str | None = None,
    ) -> dict:
        """从已有会话（可选截断至 up_to_message_idx）原子分叉创建新会话分支，隔离活跃执行态并保留血缘元数据。"""
        if up_to_message_idx is not None and (
            isinstance(up_to_message_idx, bool)
            or not isinstance(up_to_message_idx, int)
            or up_to_message_idx < 0
        ):
            raise ValueError("message_idx_invalid")
        clean_title: str | None = None
        if title is not None:
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
                raise ValueError("run_title_invalid")
            clean_title = title.strip()

        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT body, tenant_id, title FROM runs WHERE id=?", (source_run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("run_not_found")
            source_tenant = str(row["tenant_id"] or "local")
            if tenant_id is not None and source_tenant != tenant_id:
                raise KeyError("run_not_found")
            source = json.loads(row["body"])
            src_messages = copy.deepcopy(source.get("messages") or [])
            effective_up_to_idx = up_to_message_idx
            if up_to_message_idx is not None:
                if up_to_message_idx == 0 and src_messages:
                    effective_up_to_idx = 1
                forked_messages = src_messages[:effective_up_to_idx]
            else:
                forked_messages = src_messages
            healed_dangling = _heal_dangling_tool_uses(forked_messages)

            meta = copy.deepcopy(source.get("metadata") or {})
            for transient_key in (
                "pending_calls",
                "pending_results",
                "waiting_proposal_ids",
                "executing_proposal_id",
                "approval_failure",
                "cancel_requested",
                "cancel_reason",
                "cancel_requested_at",
                "cancelled_by",
                "error_detail",
                "archived",
                "archived_at",
                "billed_tokens",
                "cost_usd",
                "cost_breakdown",
                "kv_cache",
                "active_checkpoint_id",
                "turn_skills_snapshot",
                "stop_veto_count",
                "truncation_continuations",
                "context_stats",
            ):
                meta.pop(transient_key, None)
            # 当分叉对历史消息执行了截断或悬空工具闭合修复时，父会话的压缩游标与摘要不再与子分支消息组对齐，必须清零重置；
            # 完整无损分叉（up_to_message_idx=None 且未修改消息尾部）则保留与之匹配的压缩摘要与游标。
            if up_to_message_idx is not None or healed_dangling:
                for compaction_key in (
                    "compaction_cursors",
                    "compaction_summaries",
                    "compaction_summary",
                    "micro_compaction_cursors",
                ):
                    meta.pop(compaction_key, None)

            if clean_title is None:
                base_label = (
                    row["title"] or source.get("title") or source.get("prompt") or source_run_id
                )
                clean_title = f"Fork of {str(base_label).strip()[:160]}"[:200]

            meta["title"] = clean_title
            meta["forked_from_run_id"] = source_run_id
            meta["forked_at_message_idx"] = len(forked_messages)
            meta["forked_at"] = now
            if source_tenant != "local":
                meta["tenant_id"] = source_tenant

            new_id = uuid.uuid4().hex
            forked_status = "completed" if forked_messages else "queued"
            forked: dict[str, Any] = {
                "id": new_id,
                "prompt": source.get("prompt", ""),
                "title": clean_title,
                "archived": False,
                "mode": source.get("mode", "agent"),
                "provider": source.get("provider", "demo"),
                "status": forked_status,
                "answer": source.get("answer", "")
                if (up_to_message_idx is None and not healed_dangling)
                else "",
                "error": None,
                "messages": forked_messages,
                "steps": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "state_version": 1,
                "created_at": now,
                "updated_at": now,
                "metadata": meta,
            }
            summary = self._build_run_summary(forked, tenant_id=source_tenant)
            forked_a2a_ctx = self._extract_a2a_context_id(meta)
            con.execute(
                "INSERT INTO runs(id, created_at, body, state_version, status_col, updated_at_col, "
                "tenant_id, summary_json, title, archived, a2a_context_id) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 0, ?)",
                (
                    new_id,
                    now,
                    _dump(forked),
                    forked_status,
                    now,
                    source_tenant,
                    _dump(summary),
                    clean_title,
                    forked_a2a_ctx,
                ),
            )
            con.execute(
                "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, "
                "lease_until, heartbeat_at, updated_at) "
                "VALUES (?, 'idle', 'execute', NULL, 0, 0, NULL, NULL, ?)",
                (new_id, now),
            )
            con.execute(
                "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                "VALUES (?, 'run_forked', ?, ?, ?, ?)",
                (
                    new_id,
                    now,
                    _dump(
                        {
                            "source_run_id": source_run_id,
                            "up_to_message_idx": effective_up_to_idx,
                            "copied_messages": len(forked_messages),
                            "healed_dangling_tool_use": healed_dangling,
                        }
                    ),
                    current_trace_id(),
                    current_span_id(),
                ),
            )
        return forked

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
                f"SELECT state_version, tenant_id, title, archived, a2a_context_id FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run["id"],),
            ).fetchone()
            if existing is None:
                raise KeyError("run_not_found")
            current_version = int(existing["state_version"] or 1)
            if expected_version is not None and current_version != expected_version:
                raise StaleEpochError("state_version_conflict")
            next_version = current_version + 1
            run_to_save = {**run, "updated_at": now, "state_version": next_version}
            tenant = str(
                run_to_save.get("tenant_id")
                or (run_to_save.get("metadata") or {}).get("tenant_id")
                or existing["tenant_id"]
                or "local"
            )
            resolved_title = run_to_save["title"] if "title" in run_to_save else existing["title"]
            resolved_archived = (
                (1 if run_to_save["archived"] else 0)
                if "archived" in run_to_save
                else int(existing["archived"] or 0)
            )
            resolved_a2a_ctx = (
                self._extract_a2a_context_id(run_to_save.get("metadata"))
                if isinstance(run_to_save.get("metadata"), dict)
                else existing["a2a_context_id"]
            )
            summary = self._build_run_summary(run_to_save, tenant_id=tenant)
            if resolved_title is not None and "title" not in summary:
                summary["title"] = resolved_title
            summary["archived"] = bool(resolved_archived)
            where_version = expected_version if expected_version is not None else current_version
            changed = con.execute(
                "UPDATE runs SET body=?, state_version=?, status_col=?, updated_at_col=?, "
                "tenant_id=?, summary_json=?, title=?, archived=?, a2a_context_id=? WHERE id=? AND state_version=?",
                (
                    _dump(run_to_save),
                    next_version,
                    run_to_save.get("status"),
                    now,
                    tenant,
                    _dump(summary),
                    resolved_title,
                    resolved_archived,
                    resolved_a2a_ctx,
                    run["id"],
                    where_version,
                ),
            )
            if changed.rowcount != 1:
                raise StaleEpochError("state_version_conflict")
        run["updated_at"] = now
        run["state_version"] = next_version

    def add_event(
        self,
        run_id: str,
        kind: str,
        payload: dict,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict:
        now = _now()
        resolved_trace = trace_id if trace_id is not None else current_trace_id()
        resolved_span = span_id if span_id is not None else current_span_id()
        with self._connect() as con:
            if self.pg_config is not None:
                row = con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                    "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                    (run_id, kind, now, _dump(payload), resolved_trace, resolved_span),
                ).fetchone()
                event_id = int(row["id"])
            else:
                cur = con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, kind, now, _dump(payload), resolved_trace, resolved_span),
                )
                event_id = cur.lastrowid
        return {
            "id": event_id,
            "run_id": run_id,
            "kind": kind,
            "created_at": now,
            "payload": payload,
            "trace_id": resolved_trace,
            "span_id": resolved_span,
        }

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
        """按时间倒序读取指定租户最近的完整会话记录，供跨会话 Transcript 混合检索与离线 Dreaming 真值核验。

        注意：list_runs 默认仅反序列化轻量的 summary_json（不含 messages 完整轨迹），而 Dreaming 阶段 1
        与 memory_search(scope="transcripts") 需审查工具回执、salient_tags 与修复证据，故拉取完整 body。
        """
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
        """在 dream_runs 表原子写入离线 Dreaming 整理回执（纠错、Wiki 与断链修复统计），供审计与展示。"""
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
        """按租户与可选项目标识查询最近的离线 Dreaming 整理报告。"""
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
                    f"SELECT body FROM proposals WHERE run_id=? AND operation_id=?{self._pg_lock('FOR UPDATE')}",
                    (run_id, operation_id),
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
            if self.pg_config is not None:
                con.execute(
                    "INSERT INTO proposals(id, run_id, body, operation_id) VALUES (?, ?, ?, ?)",
                    (proposal["id"], run_id, _dump(proposal), operation_id),
                )
            else:
                max_row = con.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM proposals").fetchone()
                next_seq = int(max_row["m"] or 0) + 1
                con.execute(
                    "INSERT INTO proposals(id, run_id, body, operation_id, seq) VALUES (?, ?, ?, ?, ?)",
                    (proposal["id"], run_id, _dump(proposal), operation_id, next_seq),
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
            order_clause = (
                "ORDER BY seq ASC, id ASC"
                if self.pg_config is not None
                else "ORDER BY COALESCE(seq, rowid) ASC, id ASC"
            )
            rows = con.execute(
                f"SELECT body FROM proposals WHERE run_id=? {order_clause}", (run_id,)
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def approve_proposal(
        self,
        proposal_id: str,
        approved: bool,
        *,
        actor_id: str | None = None,
        answer_text: str | None = None,
    ) -> dict:
        """审批不可反复翻转；同一决定重试幂等，已拒绝补丁需重新提出。"""
        if not isinstance(approved, bool):
            raise ValueError("approval_must_be_boolean")
        if answer_text is not None and not isinstance(answer_text, str):
            raise ValueError("answer_text_invalid")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"SELECT body FROM proposals WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (proposal_id,),
            ).fetchone()
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
            if answer_text is not None:
                proposal["answer_text"] = answer_text[:4000]
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
        grant_pattern: str | None = None,
        answer_text: str | None = None,
    ) -> dict:
        """单事务原子完成补丁审批决策（及可选权限模式/会话放行模式更新）与持久队列恢复入队。"""
        if not isinstance(approved, bool):
            raise ValueError("approval_must_be_boolean")
        if answer_text is not None and not isinstance(answer_text, str):
            raise ValueError("answer_text_invalid")
        clean_grant: str | None = None
        if grant_pattern is not None:
            if not isinstance(grant_pattern, str):
                raise ValueError("grant_pattern_invalid")
            g_str = grant_pattern.strip()
            if g_str:
                if any(ch in g_str for ch in ("\x00", "\n", "\r")):
                    raise ValueError("grant_pattern_invalid")
                clean_grant = g_str[:200]
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            lease_row = con.execute(
                f"SELECT * FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            run_row = con.execute(
                f"SELECT body, state_version, tenant_id FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError("run_not_found")
            row = con.execute(
                f"SELECT run_id, body FROM proposals WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (proposal_id,),
            ).fetchone()
            if row is None or row["run_id"] != run_id:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
            if proposal.get("run_id") != run_id:
                raise KeyError("proposal_not_found")
            target = "approved" if approved else "rejected"
            if proposal["status"] not in ("pending", target):
                raise ValueError("approval_already_decided")
            if lease_row is not None and lease_row["queue_state"] in {
                "completed",
                "cancelled",
                "dead_letter",
            }:
                raise ValueError(f"run_already_terminal:{lease_row['queue_state']}")

            if permission_mode is not None or (approved and clean_grant is not None):
                run_doc = json.loads(run_row["body"])
                run_dirty = False
                grant_added = False
                if permission_mode is not None:
                    run_doc.setdefault("metadata", {})["permission_mode"] = permission_mode
                    run_dirty = True
                if approved and clean_grant is not None:
                    grants = run_doc.setdefault("metadata", {}).setdefault("session_grants", [])
                    if not isinstance(grants, list):
                        grants = []
                        run_doc["metadata"]["session_grants"] = grants
                    if clean_grant not in grants and len(grants) < 64:
                        grants.append(clean_grant)
                        run_dirty = True
                        grant_added = True
                if run_dirty:
                    run_doc["updated_at"] = now
                    run_doc["state_version"] = (
                        int(run_row["state_version"] or run_doc.get("state_version") or 1) + 1
                    )
                    tenant_val = str(
                        run_row["tenant_id"] or run_doc["metadata"].get("tenant_id") or "local"
                    )
                    summary_doc = self._build_run_summary(run_doc, tenant_id=tenant_val)
                    con.execute(
                        "UPDATE runs SET body=?, state_version=state_version+1, updated_at_col=?, summary_json=? WHERE id=?",
                        (_dump(run_doc), now, _dump(summary_doc), run_id),
                    )
                    if grant_added:
                        con.execute(
                            "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                            "VALUES (?, 'session_grant_added', ?, ?, ?, ?)",
                            (
                                run_id,
                                now,
                                _dump(
                                    {
                                        "proposal_id": proposal_id,
                                        "pattern": clean_grant,
                                        "actor_id": actor_id,
                                    }
                                ),
                                current_trace_id(),
                                current_span_id(),
                            ),
                        )

            proposal["status"] = target
            proposal["decided_at"] = now
            proposal["decided_by"] = actor_id
            if answer_text is not None:
                proposal["answer_text"] = answer_text[:4000]
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))

            if lease_row is None:
                con.execute(
                    "INSERT INTO run_leases(run_id, queue_state, action, owner_id, epoch, takeover_count, "
                    "lease_until, heartbeat_at, updated_at) "
                    "VALUES (?, 'queued', 'resume', NULL, 0, 0, NULL, NULL, ?)",
                    (run_id, now),
                )
            else:
                con.execute(
                    "UPDATE run_leases SET queue_state='queued', action='resume', owner_id=NULL, "
                    "takeover_count=0, lease_until=NULL, updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
            updated_lease = dict(
                con.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
            )
            self._notify_queue_tx(con, run_id, action="resume")
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
            id_row = con.execute("SELECT run_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if id_row is None:
                raise KeyError("proposal_not_found")
            self._verify_epoch_tx(con, id_row["run_id"], expected_epoch)
            row = con.execute(
                f"SELECT body FROM proposals WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
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
        """创建终端命令（command）、桌面 GUI（gui_action）、外部 MCP/A2A（mcp_call / a2a_call）或交互规划（user_question / plan_approval）的待审批动作提案。

        为什么非文件动作提案必须复用 proposals 表及 (run_id, operation_id) 唯一索引：
        1. 统一状态机：API 审批端点、双人复核（AGENT_LAB_REQUIRE_DISTINCT_APPROVER）与原子 approve_and_enqueue
           无需维护两套审批表即可无缝管理所有高风险动作；
        2. 崩溃幂等性：将规范化参数摘要写入 new_sha256 并绑定 call_id（operation_id），若进程在落库后、
           写入 tool_calls 账本前崩溃，恢复重放时会幂等找回同一份 pending 提案，同时阻断参数篡改冲突。
        """
        if kind not in {"command", "gui_action", "mcp_call", "a2a_call", "user_question", "plan_approval"}:
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
                    f"SELECT body FROM proposals WHERE run_id=? AND operation_id=?{self._pg_lock('FOR UPDATE')}",
                    (run_id, operation_id),
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
            if self.pg_config is not None:
                con.execute(
                    "INSERT INTO proposals(id, run_id, body, operation_id) VALUES (?, ?, ?, ?)",
                    (proposal["id"], run_id, _dump(proposal), operation_id),
                )
            else:
                max_row = con.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM proposals").fetchone()
                next_seq = int(max_row["m"] or 0) + 1
                con.execute(
                    "INSERT INTO proposals(id, run_id, body, operation_id, seq) VALUES (?, ?, ?, ?, ?)",
                    (proposal["id"], run_id, _dump(proposal), operation_id, next_seq),
                )
        return proposal

    def mark_action_proposal_executed(
        self,
        proposal_id: str,
        execution_result: dict[str, Any],
        expected_epoch: int | None = None,
    ) -> dict:
        """原子记录非文件动作提案已执行及其输出回执（applied=True + execution_result）。

        弥合关键竞态窗口：Shell 命令、GUI 点击或外部 MCP 调用不像本地文件写入那样可以通过校验磁盘文件 SHA-256
        来事后推断“是否已经应用”。因此一旦副作用执行完毕，立刻在校验 epoch 后把回执封存入提案记录；若随后在
        追加 REVIEW_APPROVED 消息前发生进程崩溃，resume() 会直接读取此回执而绝不二次触发外部副作用。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            id_row = con.execute("SELECT run_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if id_row is None:
                raise KeyError("proposal_not_found")
            self._verify_epoch_tx(con, id_row["run_id"], expected_epoch)
            row = con.execute(
                f"SELECT body FROM proposals WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
            if proposal["status"] != "approved":
                raise ValueError("approval_required")
            proposal["applied"] = True
            proposal["applied_at"] = _now()
            proposal["execution_result"] = execution_result
            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(proposal), proposal_id))
        return proposal

    def enqueue_steering_message(
        self, run_id: str, prompt: str, *, actor_id: str | None = None
    ) -> dict:
        """向独立收件箱表写入运行期实时人类转向指令，零触碰 runs.body / state_version 以避免与活跃 Worker 发生 CAS 写冲突。"""
        if not isinstance(prompt, str):
            raise ValueError("prompt_length_invalid")
        clean_prompt = prompt.strip()
        if not 1 <= len(clean_prompt) <= 8000:
            raise ValueError("prompt_length_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
            if self.pg_config is not None:
                row = con.execute(
                    "INSERT INTO run_steering_messages(run_id, prompt, actor_id, created_at, consumed_at) "
                    "VALUES (?, ?, ?, ?, NULL) RETURNING id",
                    (run_id, clean_prompt, actor_id, now),
                ).fetchone()
                row_id = int(row["id"])
            else:
                cur = con.execute(
                    "INSERT INTO run_steering_messages(run_id, prompt, actor_id, created_at, consumed_at) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (run_id, clean_prompt, actor_id, now),
                )
                row_id = int(cur.lastrowid)
        self.add_event(
            run_id,
            "user_steering_queued",
            {"steering_id": row_id, "prompt": clean_prompt, "actor_id": actor_id},
        )
        return {
            "id": row_id,
            "run_id": run_id,
            "prompt": clean_prompt,
            "actor_id": actor_id,
            "created_at": now,
        }

    def drain_steering_messages(
        self, run_id: str, *, expected_epoch: int | None = None
    ) -> list[dict]:
        """在单事务内经 epoch 世代栅栏校验后，按 id 升序原子取出并标记消费指定 run 的全部未消费转向消息。"""
        now = _now()
        lock_clause = self._pg_lock("FOR UPDATE")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_epoch_tx(con, run_id, expected_epoch)
            rows = con.execute(
                f"SELECT id, prompt, actor_id, created_at FROM run_steering_messages "
                f"WHERE run_id=? AND consumed_at IS NULL ORDER BY id ASC{lock_clause}",
                (run_id,),
            ).fetchall()
            if not rows:
                return []
            ids = [int(r["id"]) for r in rows]
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                con.execute(
                    f"UPDATE run_steering_messages SET consumed_at=? "
                    f"WHERE id IN ({placeholders}) AND consumed_at IS NULL",
                    (now, *batch),
                )
        return [
            {
                "id": int(r["id"]),
                "prompt": str(r["prompt"]),
                "actor_id": r["actor_id"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def pending_steering_messages(self, run_id: str) -> list[dict]:
        """只读查询指定 run 当前尚未被消费的转向消息列表（按 id 升序）。"""
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, run_id, prompt, actor_id, created_at FROM run_steering_messages "
                "WHERE run_id=? AND consumed_at IS NULL ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "run_id": str(r["run_id"]),
                "prompt": str(r["prompt"]),
                "actor_id": r["actor_id"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def append_user_turn(
        self,
        run_id: str,
        prompt: str,
        *,
        step_extension: int = 24,
        token_extension: int = 120_000,
        enqueue: bool = False,
        actor_id: str | None = None,
        supersede_waiting_approval: bool = False,
    ) -> dict:
        """在已结束/暂停/取消（及可选等待审批接管）的任务上原子追加新一轮用户指令，完成悬空工具闭合修复、线性配额延展与世代递增。

        两大核心工程细节：
        1. 悬空 tool_use 1:1 协议自愈：若上一轮在模型发出 assistant.tool_use 后因用户取消、超预算或崩溃终止，
           历史末尾缺少对应的 user.tool_result。若直接追加纯文本新提问，Anthropic / OpenAI 会因协议失配立即报
           400 错误，且 ContextManager._atomic_groups 配对不变量会破坏；故必须按原顺序逐一补齐合成回执。
        2. 按实际消耗线性延展预算：以 `已用步数/Token + 单轮基线(base_max_*)` 计算新上限，既防止第 2 轮因沿用
           首轮总额度而立刻触发 BudgetExceeded，又避免累加 `max_tokens += ext` 在多轮短对话后囤积巨额预算。
        """
        if not isinstance(prompt, str):
            raise ValueError("prompt_length_invalid")
        clean_prompt = prompt.strip()
        if not 1 <= len(clean_prompt) <= 8000:
            raise ValueError("prompt_length_invalid")
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            lease_row = con.execute(
                f"SELECT * FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            run_row = con.execute(
                f"SELECT body, state_version, tenant_id FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError("run_not_found")
            run = json.loads(run_row["body"])
            cur_status = run.get("status")
            if cur_status in {"running", "queued"}:
                raise ValueError(f"run_busy:{cur_status}")
            if cur_status == "waiting_approval" and not supersede_waiting_approval:
                raise ValueError("run_busy:waiting_approval")
            if lease_row is not None and lease_row["queue_state"] == "dead_letter":
                raise ValueError("run_dead_lettered")
            if (
                lease_row is not None
                and lease_row["queue_state"] == "claimed"
                and (lease_row["lease_until"] or 0.0) > time.time()
            ):
                raise ValueError("run_busy:claimed")

            superseded_waiting = bool(cur_status == "waiting_approval" and supersede_waiting_approval)
            meta = run.setdefault("metadata", {})
            meta.setdefault("initial_prompt", run.get("prompt", ""))
            msgs = run.setdefault("messages", [])
            superseded_ids: list[str] = []
            waiting_ids = [
                pid for pid in (meta.get("waiting_proposal_ids") or []) if isinstance(pid, str) and pid
            ]
            if superseded_waiting:
                decisions = meta.setdefault("decisions", [])
                for pid in waiting_ids:
                    prop_row = con.execute(
                        f"SELECT body FROM proposals WHERE id=? AND run_id=?{self._pg_lock('FOR UPDATE')}",
                        (pid, run_id),
                    ).fetchone()
                    if prop_row is not None:
                        prop_doc = json.loads(prop_row["body"])
                        if prop_doc.get("status") == "pending":
                            prop_doc["status"] = "rejected"
                            prop_doc["decided_at"] = now
                            prop_doc["decided_by"] = actor_id
                            prop_doc["decision_reason"] = "superseded_by_user_steering"
                            con.execute("UPDATE proposals SET body=? WHERE id=?", (_dump(prop_doc), pid))
                    superseded_ids.append(pid)
                    decisions.append(
                        {
                            "proposal_id": pid,
                            "approved": False,
                            "reason": "superseded_by_user_steering",
                            "actor_id": actor_id,
                        }
                    )

            # 悬空 tool_use 1:1 闭合修复：优先复用 pending_results 中已完成工具的真实回执，其余未执行项填入
            # aborted_before_followup / superseded_by_user_steering 占位，确保每条 assistant.tool_use 都紧跟配对的 user.tool_result。
            synth_code = "superseded_by_user_steering" if superseded_waiting else "aborted_before_followup"
            synth_err = (
                "待审批操作已被用户新指令接管取消，未执行。"
                if superseded_waiting
                else "上一轮在该工具完成前已终止，现由新用户指令接续。"
            )
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
                                "code": synth_code,
                                "error": synth_err,
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
            elif (
                superseded_waiting
                and len(msgs) >= 2
                and isinstance(msgs[-2], dict)
                and msgs[-2].get("role") == "assistant"
                and isinstance(msgs[-2].get("content"), list)
                and isinstance(msgs[-1], dict)
                and msgs[-1].get("role") == "user"
            ):
                expected_uses = [
                    b
                    for b in msgs[-2]["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str)
                ]
                if expected_uses:
                    if isinstance(msgs[-1].get("content"), list):
                        paired_ids = {
                            b.get("tool_use_id")
                            for b in msgs[-1]["content"]
                            if isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            and isinstance(b.get("tool_use_id"), str)
                        }
                        for tu in expected_uses:
                            if tu["id"] not in paired_ids:
                                msgs[-1]["content"].append(
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tu["id"],
                                        "content": json.dumps(
                                            {"ok": False, "code": synth_code, "error": synth_err},
                                            ensure_ascii=False,
                                        ),
                                        "is_error": True,
                                    }
                                )
                    else:
                        msgs.insert(
                            len(msgs) - 1,
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tu["id"],
                                        "content": json.dumps(
                                            {"ok": False, "code": synth_code, "error": synth_err},
                                            ensure_ascii=False,
                                        ),
                                        "is_error": True,
                                    }
                                    for tu in expected_uses
                                ],
                            },
                        )

            if superseded_waiting:
                cancelled_label = ", ".join(superseded_ids) if superseded_ids else "pending"
                msgs.append(
                    {
                        "role": "user",
                        "content": (
                            f"[用户转向接管 (superseded_by_user_steering)：上一轮待审批提案 ({cancelled_label}) "
                            "已被取消拒绝且未执行；请按以下最新用户指令继续。]"
                        ),
                    }
                )
            msgs.append({"role": "user", "content": clean_prompt})
            run["prompt"] = clean_prompt
            run["answer"] = ""
            run["status"] = "queued" if enqueue else "running"
            run["error"] = None
            meta.pop("error_detail", None)
            meta.pop("approval_failure", None)
            meta.pop("executing_proposal_id", None)
            meta.pop("pending_calls", None)
            meta.pop("pending_results", None)
            if superseded_waiting:
                meta["waiting_proposal_ids"] = []
            else:
                meta.pop("waiting_proposal_ids", None)
            meta.pop("cancel_requested", None)
            meta.pop("cancel_reason", None)
            meta.pop("truncation_continuations", None)
            meta["refresh_pinned_memory"] = True
            # 线性每轮预算延展：锚定当前累计消耗（steps / used_tokens）加单轮基准配额，杜绝预算饥饿与膨胀
            per_turn_steps = max(1, int(meta.get("base_max_steps") or step_extension))
            per_turn_tokens = max(2048, int(meta.get("base_max_tokens") or token_extension))
            meta["max_steps"] = max(
                int(meta.get("max_steps", 12)),
                int(run.get("steps", 0)) + per_turn_steps,
            )
            billed_so_far = int(meta.get("billed_tokens", 0) or 0)
            raw_so_far = int(run.get("input_tokens", 0)) + int(run.get("output_tokens", 0))
            used_tokens = billed_so_far if billed_so_far > 0 else raw_so_far
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
            followup_payload: dict[str, Any] = {"prompt": clean_prompt[:500], "actor_id": actor_id}
            if superseded_waiting:
                followup_payload["superseded_proposals"] = superseded_ids
            con.execute(
                "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                "VALUES (?, 'user_followup', ?, ?, ?, ?)",
                (run_id, now, _dump(followup_payload), current_trace_id(), current_span_id()),
            )
            # 同步推进租约世代号（epoch + 1），确保任何上一轮遗留的迟到写入被立即隔离
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
            if enqueue:
                self._notify_queue_tx(con, run_id, action="execute")
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
                f"SELECT body FROM tool_calls WHERE run_id=? AND call_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id, call_id),
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

    def _require_call(self, con: Any, run_id: str, call_id: str) -> dict:
        row = con.execute(
            f"SELECT body FROM tool_calls WHERE run_id=? AND call_id=?{self._pg_lock('FOR UPDATE')}",
            (run_id, call_id),
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
            lease_row = con.execute(
                f"SELECT epoch FROM run_leases WHERE run_id=?{self._pg_lock('FOR KEY SHARE')}",
                (run_id,),
            ).fetchone()
            if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError("run_not_found")
            if lease_row is not None and int(lease_row["epoch"] or 0) > epoch:
                raise StaleEpochError(
                    f"stale_archive_epoch: run={run_id} archive_epoch={epoch} lease_epoch={lease_row['epoch']}"
                )
            existing = con.execute(
                f"SELECT epoch FROM workspace_archives WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
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
            lease_row = con.execute(
                f"SELECT * FROM run_leases WHERE run_id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
            ).fetchone()
            run_row = con.execute(
                f"SELECT body, state_version, tenant_id FROM runs WHERE id=?{self._pg_lock('FOR UPDATE')}",
                (run_id,),
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
                    "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                    "VALUES (?, 'cancelled', ?, ?, ?, ?)",
                    (
                        run_id,
                        now,
                        _dump({"actor_id": actor_id, "reason": reason, "immediate": True}),
                        current_trace_id(),
                        current_span_id(),
                    ),
                )
            else:
                con.execute(
                    "INSERT INTO events(run_id, kind, created_at, payload, trace_id, span_id) "
                    "VALUES (?, 'cancel_requested', ?, ?, ?, ?)",
                    (
                        run_id,
                        now,
                        _dump({"actor_id": actor_id, "reason": reason, "immediate": False}),
                        current_trace_id(),
                        current_span_id(),
                    ),
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
        """回收已进入终态且超过保留窗口的临时工作区/worktree、数据库归档、截图产物与陈旧内核锁文件。

        安全铁律（direct_workspace 保护）：
        托管模板任务（sample / multi_file）的工作区位于 .agent-lab/workspaces/<run_id> 下，超期可销毁；
        而 host / computer 直连任务的 metadata["workspace"] 指向用户本机真实工程源码目录，即使管理员误将
        workspaces_dir 传成父目录，GC 也必须凭 direct_workspace 无条件跳过磁盘删除，仅回收锁与截图产物。
        """
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

    @staticmethod
    def _format_checkpoint_row(row: Any) -> dict[str, Any]:
        raw_manifest = str(row["file_manifest_json"] or "{}")
        try:
            parsed_manifest = json.loads(raw_manifest)
        except Exception:
            parsed_manifest = {}
        return {
            "checkpoint_id": str(row["checkpoint_id"]),
            "run_id": str(row["run_id"]),
            "tenant_id": str(row["tenant_id"] or "local"),
            "turn_index": int(row["turn_index"]),
            "message_idx": int(row["message_idx"]),
            "label": str(row["label"] or ""),
            "file_manifest": parsed_manifest,
            "file_manifest_json": raw_manifest,
            "git_tree": row["git_tree"],
            "created_at": str(row["created_at"]),
        }

    def save_checkpoint(
        self,
        run_id: str,
        turn_index: int = 0,
        message_idx: int = 0,
        *,
        checkpoint_id: str | None = None,
        tenant_id: str | None = None,
        label: str = "",
        file_manifest: dict[str, Any] | list[Any] | str | None = None,
        file_manifest_json: str | None = None,
        git_tree: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """持久化会话检查点（关联轮次、消息下标、文件清单与可选 Git tree），供回滚与分叉使用。"""
        self._identifier(run_id, "run_id")
        if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
            raise ValueError("turn_index_invalid")
        if isinstance(message_idx, bool) or not isinstance(message_idx, int) or message_idx < 0:
            raise ValueError("message_idx_invalid")
        cp_id = checkpoint_id if checkpoint_id is not None else uuid.uuid4().hex
        self._identifier(cp_id, "checkpoint_id")
        clean_label = str(label or "").strip()[:200]
        clean_git_tree = str(git_tree).strip()[:128] if git_tree is not None and str(git_tree).strip() else None

        if file_manifest_json is not None:
            if isinstance(file_manifest_json, str):
                try:
                    manifest_obj = json.loads(file_manifest_json)
                except json.JSONDecodeError as exc:
                    raise ValueError("file_manifest_json_invalid") from exc
                manifest_str = _dump(manifest_obj)
            else:
                manifest_obj = file_manifest_json
                manifest_str = _dump(manifest_obj)
        elif file_manifest is not None:
            if isinstance(file_manifest, str):
                try:
                    manifest_obj = json.loads(file_manifest)
                except json.JSONDecodeError as exc:
                    raise ValueError("file_manifest_json_invalid") from exc
                manifest_str = _dump(manifest_obj)
            else:
                manifest_obj = file_manifest
                manifest_str = _dump(manifest_obj)
        else:
            manifest_obj = {}
            manifest_str = "{}"

        now = str(created_at) if created_at else _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if tenant_id is not None:
                clean_tenant = str(tenant_id or "local").strip()[:128] or "local"
            else:
                run_row = con.execute("SELECT tenant_id FROM runs WHERE id=?", (run_id,)).fetchone()
                clean_tenant = str(run_row["tenant_id"] or "local") if run_row is not None else "local"
            con.execute(
                "INSERT INTO run_checkpoints("
                "checkpoint_id, run_id, tenant_id, turn_index, message_idx, label, "
                "file_manifest_json, git_tree, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(checkpoint_id) DO UPDATE SET "
                "run_id=excluded.run_id, tenant_id=excluded.tenant_id, turn_index=excluded.turn_index, "
                "message_idx=excluded.message_idx, label=excluded.label, "
                "file_manifest_json=excluded.file_manifest_json, git_tree=excluded.git_tree, "
                "created_at=excluded.created_at",
                (
                    cp_id,
                    run_id,
                    clean_tenant,
                    turn_index,
                    message_idx,
                    clean_label,
                    manifest_str,
                    clean_git_tree,
                    now,
                ),
            )
        return {
            "checkpoint_id": cp_id,
            "run_id": run_id,
            "tenant_id": clean_tenant,
            "turn_index": turn_index,
            "message_idx": message_idx,
            "label": clean_label,
            "file_manifest": manifest_obj,
            "file_manifest_json": manifest_str,
            "git_tree": clean_git_tree,
            "created_at": now,
        }

    def list_checkpoints(
        self, run_id: str, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """按 turn_index 与时间升序返回指定会话的全部检查点（可选租户隔离过滤）。"""
        with self._connect() as con:
            if tenant_id is not None:
                rows = con.execute(
                    "SELECT * FROM run_checkpoints WHERE run_id=? AND tenant_id=? "
                    "ORDER BY turn_index ASC, message_idx ASC, created_at ASC, checkpoint_id ASC",
                    (run_id, tenant_id),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM run_checkpoints WHERE run_id=? "
                    "ORDER BY turn_index ASC, message_idx ASC, created_at ASC, checkpoint_id ASC",
                    (run_id,),
                ).fetchall()
        return [self._format_checkpoint_row(row) for row in rows]

    def get_checkpoint(
        self, run_id: str, checkpoint_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        """按 run_id 与 checkpoint_id（及可选 tenant_id）查询单个检查点。"""
        with self._connect() as con:
            if tenant_id is not None:
                row = con.execute(
                    "SELECT * FROM run_checkpoints WHERE run_id=? AND checkpoint_id=? AND tenant_id=?",
                    (run_id, checkpoint_id, tenant_id),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM run_checkpoints WHERE run_id=? AND checkpoint_id=?",
                    (run_id, checkpoint_id),
                ).fetchone()
        return self._format_checkpoint_row(row) if row is not None else None

    def delete_checkpoints_after(
        self, run_id: str, turn_index: int, *, tenant_id: str | None = None
    ) -> int:
        """删除指定会话中 turn_index 严格大于给定轮次的全部后续检查点，返回删除条数。"""
        if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < -1:
            raise ValueError("turn_index_invalid")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if tenant_id is not None:
                cur = con.execute(
                    "DELETE FROM run_checkpoints WHERE run_id=? AND turn_index > ? AND tenant_id=?",
                    (run_id, turn_index, tenant_id),
                )
            else:
                cur = con.execute(
                    "DELETE FROM run_checkpoints WHERE run_id=? AND turn_index > ?",
                    (run_id, turn_index),
                )
            return int(cur.rowcount or 0)

    @staticmethod
    def _format_audit_row(row: Any) -> dict[str, Any]:
        raw_detail = str(row["detail_json"] or "{}")
        try:
            parsed_detail = json.loads(raw_detail)
        except Exception:
            parsed_detail = {}
        return {
            "id": int(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "actor": str(row["actor"]),
            "action": str(row["action"]),
            "resource": str(row["resource"]),
            "outcome": str(row["outcome"]),
            "detail": parsed_detail,
            "detail_json": raw_detail,
            "trace_id": str(row["trace_id"] or ""),
            "created_at": str(row["created_at"]),
        }

    def record_audit_log(
        self,
        tenant_id: str = "local",
        actor: str = "system",
        action: str = "",
        resource: str = "",
        outcome: str = "allowed",
        detail: dict[str, Any] | None = None,
        *,
        detail_json: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """向仅追加（append-only）的 security_audit_logs 表写入脱敏后的安全审计记录。"""
        clean_tenant = str(tenant_id or "local").strip()[:128] or "local"
        clean_actor = str(actor or "system").strip()[:128] or "system"
        clean_action = str(action or "").strip()[:128]
        clean_resource = str(resource or "").strip()[:256]
        clean_outcome = str(outcome or "").strip()[:64]
        if not clean_action:
            raise ValueError("audit_action_invalid")
        if not clean_resource:
            raise ValueError("audit_resource_invalid")
        if not clean_outcome:
            raise ValueError("audit_outcome_invalid")

        if detail_json is not None:
            if isinstance(detail_json, str):
                try:
                    raw_obj = json.loads(detail_json)
                except json.JSONDecodeError as exc:
                    raise ValueError("audit_detail_json_invalid") from exc
            else:
                raw_obj = detail_json
            redacted_detail = redact_secrets(raw_obj)
        elif detail is not None:
            redacted_detail = redact_secrets(detail)
        else:
            redacted_detail = {}
        detail_str = _dump(redacted_detail)
        resolved_trace = str(
            trace_id if trace_id is not None else (current_trace_id() or "")
        ).strip()[:128]
        now = _now()

        with self._connect() as con:
            if self.pg_config is not None:
                row = con.execute(
                    "INSERT INTO security_audit_logs("
                    "tenant_id, actor, action, resource, outcome, detail_json, trace_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (
                        clean_tenant,
                        clean_actor,
                        clean_action,
                        clean_resource,
                        clean_outcome,
                        detail_str,
                        resolved_trace,
                        now,
                    ),
                ).fetchone()
                row_id = int(row["id"])
            else:
                cur = con.execute(
                    "INSERT INTO security_audit_logs("
                    "tenant_id, actor, action, resource, outcome, detail_json, trace_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clean_tenant,
                        clean_actor,
                        clean_action,
                        clean_resource,
                        clean_outcome,
                        detail_str,
                        resolved_trace,
                        now,
                    ),
                )
                row_id = int(cur.lastrowid)

        return {
            "id": row_id,
            "tenant_id": clean_tenant,
            "actor": clean_actor,
            "action": clean_action,
            "resource": clean_resource,
            "outcome": clean_outcome,
            "detail": redacted_detail,
            "detail_json": detail_str,
            "trace_id": resolved_trace,
            "created_at": now,
        }

    def list_audit_logs(
        self,
        tenant_id: str | None = None,
        limit: int = 100,
        *,
        action: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """按时间倒序（id DESC）分页读取安全审计日志，支持按租户、动作与主体过滤。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("audit_limit_invalid")
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if action is not None and str(action).strip():
            clauses.append("action = ?")
            params.append(str(action).strip())
        if actor is not None and str(actor).strip():
            clauses.append("actor = ?")
            params.append(str(actor).strip())
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM security_audit_logs {where_sql}ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._format_audit_row(row) for row in rows]

    @staticmethod
    def _format_a2a_push_config_row(row: Any) -> dict[str, Any]:
        raw_events = str(row["events_json"] or "[]")
        try:
            parsed_events = json.loads(raw_events)
            if not isinstance(parsed_events, list):
                parsed_events = []
        except Exception:
            parsed_events = []
        secret_val = row["secret"] if row["secret"] else None
        return {
            "task_id": str(row["task_id"]),
            "tenant_id": str(row["tenant_id"]),
            "url": str(row["url"]),
            "token_hash": row["token_hash"],
            "secret": secret_val,
            "has_secret": bool(secret_val),
            "events": [str(e) for e in parsed_events if isinstance(e, str)],
            "events_json": raw_events,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_a2a_push_config(
        self,
        task_id: str,
        tenant_id: str,
        url: str,
        *,
        secret: str | None = None,
        events: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """原子创建或更新指定 A2A 任务的 Push Notification Webhook 配置（带多租户隔离与签名密钥摘要）。"""
        self._identifier(task_id, "task_id")
        clean_tenant = str(tenant_id or "local").strip()[:128] or "local"
        if not isinstance(url, str) or not url.strip():
            raise ValueError("a2a_push_url_invalid")
        clean_url = url.strip()
        if len(clean_url) > 2048 or any(ch in clean_url for ch in ("\r", "\n", "\x00")):
            raise ValueError("a2a_push_url_invalid")

        clean_secret: str | None = None
        token_hash: str | None = None
        if secret is not None:
            if not isinstance(secret, str):
                raise ValueError("a2a_push_secret_invalid")
            stripped_secret = secret.strip()
            if stripped_secret:
                if len(stripped_secret) > 512 or any(ch in stripped_secret for ch in ("\r", "\n", "\x00")):
                    raise ValueError("a2a_push_secret_invalid")
                clean_secret = stripped_secret
                token_hash = digest(clean_secret)

        norm_events: list[str] = []
        if events is not None:
            if not isinstance(events, (list, tuple, set)):
                raise ValueError("a2a_push_events_invalid")
            seen_ev: set[str] = set()
            for item in events:
                if not isinstance(item, str):
                    raise ValueError("a2a_push_events_invalid")
                ev_str = item.strip()[:64]
                if ev_str and ev_str not in seen_ev:
                    seen_ev.add(ev_str)
                    norm_events.append(ev_str)
                if len(norm_events) >= 32:
                    break
        events_json = _dump(norm_events)
        now = _now()

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                f"SELECT tenant_id, created_at FROM a2a_push_configs WHERE task_id=?{self._pg_lock('FOR UPDATE')}",
                (task_id,),
            ).fetchone()
            if existing is not None and str(existing["tenant_id"]) != clean_tenant:
                raise KeyError("task_not_found")
            created_at = str(existing["created_at"]) if existing is not None else now
            con.execute(
                "INSERT INTO a2a_push_configs("
                "task_id, tenant_id, url, token_hash, secret, events_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "url=excluded.url, token_hash=excluded.token_hash, secret=excluded.secret, "
                "events_json=excluded.events_json, updated_at=excluded.updated_at",
                (
                    task_id,
                    clean_tenant,
                    clean_url,
                    token_hash,
                    clean_secret,
                    events_json,
                    created_at,
                    now,
                ),
            )
        return {
            "task_id": task_id,
            "tenant_id": clean_tenant,
            "url": clean_url,
            "token_hash": token_hash,
            "secret": clean_secret,
            "has_secret": bool(clean_secret),
            "events": norm_events,
            "events_json": events_json,
            "created_at": created_at,
            "updated_at": now,
        }

    def get_a2a_push_config(
        self, task_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        """读取指定 A2A 任务的 Push Notification 配置（可选按租户过滤隔离）。"""
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        with self._connect() as con:
            if tenant_id is not None:
                row = con.execute(
                    "SELECT * FROM a2a_push_configs WHERE task_id=? AND tenant_id=?",
                    (task_id.strip(), tenant_id),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM a2a_push_configs WHERE task_id=?",
                    (task_id.strip(),),
                ).fetchone()
        return self._format_a2a_push_config_row(row) if row is not None else None

    def delete_a2a_push_config(
        self, task_id: str, tenant_id: str | None = None
    ) -> bool:
        """删除指定 A2A 任务的 Push Notification 配置，返回是否命中删除。"""
        if not isinstance(task_id, str) or not task_id.strip():
            return False
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if tenant_id is not None:
                cur = con.execute(
                    "DELETE FROM a2a_push_configs WHERE task_id=? AND tenant_id=?",
                    (task_id.strip(), tenant_id),
                )
            else:
                cur = con.execute(
                    "DELETE FROM a2a_push_configs WHERE task_id=?",
                    (task_id.strip(),),
                )
            return int(cur.rowcount or 0) > 0

    def list_runs_by_a2a_context(
        self,
        context_id: str,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """利用 `idx_runs_a2a_context_id` 索引以 O(log N) 按 A2A `contextId` 倒序检索完整任务列表。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("run_limit_invalid")
        if not isinstance(context_id, str) or not context_id.strip():
            return []
        clean_ctx = context_id.strip()[:256]
        with self._connect() as con:
            if tenant_id is not None:
                rows = con.execute(
                    "SELECT body FROM runs WHERE a2a_context_id=? AND tenant_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (clean_ctx, tenant_id, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT body FROM runs WHERE a2a_context_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (clean_ctx, limit),
                ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    @staticmethod
    def _format_trigger_row(row: Any) -> dict[str, Any]:
        raw_meta = str(row["metadata_json"] or "{}")
        try:
            parsed_meta = json.loads(raw_meta)
            if not isinstance(parsed_meta, dict):
                parsed_meta = {}
        except Exception:
            parsed_meta = {}
        secret_val = row["webhook_secret"] if row["webhook_secret"] else None
        return {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"] or "local"),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "cron_expr": row["cron_expr"],
            "webhook_secret": secret_val,
            "has_webhook_secret": bool(secret_val),
            "prompt_template": str(row["prompt_template"]),
            "workspace_path": row["workspace_path"],
            "enabled": bool(row["enabled"]),
            "last_fired_at": row["last_fired_at"],
            "metadata": parsed_meta,
            "metadata_json": raw_meta,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _format_trigger_fire_row(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "trigger_id": str(row["trigger_id"]),
            "dedup_key": str(row["dedup_key"]),
            "run_id": row["run_id"],
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
        }

    def create_trigger(
        self,
        name: str,
        kind: str,
        prompt_template: str,
        *,
        trigger_id: str | None = None,
        tenant_id: str = "local",
        cron_expr: str | None = None,
        webhook_secret: str | None = None,
        workspace_path: str | Path | None = None,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
        metadata_json: str | None = None,
    ) -> dict[str, Any]:
        """创建一条持久化触发器定义（Cron / Webhook / 事件驱动），支持多租户隔离与扩展元数据。"""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError("trigger_name_invalid")
        clean_name = name.strip()
        if not isinstance(kind, str) or not 1 <= len(kind.strip()) <= 64:
            raise ValueError("trigger_kind_invalid")
        clean_kind = kind.strip()
        if not isinstance(prompt_template, str) or not 1 <= len(prompt_template.strip()) <= 16000:
            raise ValueError("trigger_prompt_template_invalid")
        clean_prompt = prompt_template.strip()

        if trigger_id is not None:
            self._identifier(trigger_id, "trigger_id")
            resolved_id = trigger_id.strip()
        else:
            resolved_id = uuid.uuid4().hex
        clean_tenant = str(tenant_id or "local").strip()[:128] or "local"

        clean_cron: str | None = None
        if cron_expr is not None:
            if not isinstance(cron_expr, str):
                raise ValueError("trigger_cron_expr_invalid")
            stripped_cron = cron_expr.strip()
            if stripped_cron:
                if len(stripped_cron) > 128 or any(ch in stripped_cron for ch in ("\r", "\n", "\x00")):
                    raise ValueError("trigger_cron_expr_invalid")
                clean_cron = stripped_cron

        clean_secret: str | None = None
        if webhook_secret is not None:
            if not isinstance(webhook_secret, str):
                raise ValueError("trigger_webhook_secret_invalid")
            stripped_secret = webhook_secret.strip()
            if stripped_secret:
                if len(stripped_secret) > 512 or any(ch in stripped_secret for ch in ("\r", "\n", "\x00")):
                    raise ValueError("trigger_webhook_secret_invalid")
                clean_secret = stripped_secret

        clean_workspace: str | None = None
        if workspace_path is not None:
            if not isinstance(workspace_path, (str, Path)):
                raise ValueError("trigger_workspace_path_invalid")
            stripped_ws = str(workspace_path).strip()
            if stripped_ws:
                if len(stripped_ws) > 1024 or any(ch in stripped_ws for ch in ("\r", "\n", "\x00")):
                    raise ValueError("trigger_workspace_path_invalid")
                clean_workspace = stripped_ws

        if isinstance(enabled, bool):
            enabled_int = 1 if enabled else 0
        elif isinstance(enabled, int) and enabled in (0, 1):
            enabled_int = enabled
        else:
            raise ValueError("trigger_enabled_invalid")

        if metadata_json is not None:
            if isinstance(metadata_json, str):
                try:
                    meta_obj = json.loads(metadata_json)
                except json.JSONDecodeError as exc:
                    raise ValueError("trigger_metadata_invalid") from exc
            else:
                meta_obj = metadata_json
            if not isinstance(meta_obj, dict):
                raise ValueError("trigger_metadata_invalid")
            meta_str = _dump(meta_obj)
        elif metadata is not None:
            if isinstance(metadata, str):
                try:
                    meta_obj = json.loads(metadata)
                except json.JSONDecodeError as exc:
                    raise ValueError("trigger_metadata_invalid") from exc
            else:
                meta_obj = metadata
            if not isinstance(meta_obj, dict):
                raise ValueError("trigger_metadata_invalid")
            meta_str = _dump(meta_obj)
        else:
            meta_obj = {}
            meta_str = "{}"

        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO trigger_definitions("
                "id, tenant_id, name, kind, cron_expr, webhook_secret, prompt_template, "
                "workspace_path, enabled, last_fired_at, metadata_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    resolved_id,
                    clean_tenant,
                    clean_name,
                    clean_kind,
                    clean_cron,
                    clean_secret,
                    clean_prompt,
                    clean_workspace,
                    enabled_int,
                    meta_str,
                    now,
                    now,
                ),
            )
        return {
            "id": resolved_id,
            "tenant_id": clean_tenant,
            "name": clean_name,
            "kind": clean_kind,
            "cron_expr": clean_cron,
            "webhook_secret": clean_secret,
            "has_webhook_secret": bool(clean_secret),
            "prompt_template": clean_prompt,
            "workspace_path": clean_workspace,
            "enabled": bool(enabled_int),
            "last_fired_at": None,
            "metadata": meta_obj,
            "metadata_json": meta_str,
            "created_at": now,
            "updated_at": now,
        }

    def list_triggers(
        self, tenant_id: str | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        """列出触发器定义（支持按租户隔离与仅返回已启用项）。"""
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if enabled_only:
            clauses.append("enabled = 1")
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM trigger_definitions {where_sql}ORDER BY created_at DESC, id DESC",
                tuple(params),
            ).fetchall()
        return [self._format_trigger_row(row) for row in rows]

    def get_trigger(
        self, trigger_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        """按 trigger_id（及可选 tenant_id）读取单个触发器定义。"""
        if not isinstance(trigger_id, str) or not trigger_id.strip():
            return None
        clean_id = trigger_id.strip()
        with self._connect() as con:
            if tenant_id is not None:
                row = con.execute(
                    "SELECT * FROM trigger_definitions WHERE id=? AND tenant_id=?",
                    (clean_id, tenant_id),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM trigger_definitions WHERE id=?",
                    (clean_id,),
                ).fetchone()
        return self._format_trigger_row(row) if row is not None else None

    def delete_trigger(
        self, trigger_id: str, tenant_id: str | None = None
    ) -> bool:
        """原子删除触发器定义及其关联触发记录（强制多租户归属校验），返回是否命中删除。"""
        if not isinstance(trigger_id, str) or not trigger_id.strip():
            return False
        clean_id = trigger_id.strip()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if tenant_id is not None:
                cur = con.execute(
                    "DELETE FROM trigger_definitions WHERE id=? AND tenant_id=?",
                    (clean_id, tenant_id),
                )
            else:
                cur = con.execute(
                    "DELETE FROM trigger_definitions WHERE id=?",
                    (clean_id,),
                )
            deleted = int(cur.rowcount or 0) > 0
            if deleted:
                con.execute("DELETE FROM trigger_fires WHERE trigger_id=?", (clean_id,))
            return deleted

    def record_trigger_fire(
        self,
        trigger_id: str,
        dedup_key: str,
        *,
        run_id: str | None = None,
        status: str = "dispatched",
    ) -> dict[str, Any] | None:
        """以 `dedup_key` 唯一约束原子记录触发派发；若该去重键已存在则幂等返回 `None`。"""
        self._identifier(trigger_id, "trigger_id")
        clean_trigger_id = trigger_id.strip()
        if not isinstance(dedup_key, str) or not 1 <= len(dedup_key.strip()) <= 256:
            raise ValueError("trigger_dedup_key_invalid")
        clean_dedup_key = dedup_key.strip()
        clean_run_id: str | None = None
        if run_id is not None:
            if not isinstance(run_id, str):
                raise ValueError("run_id_invalid")
            stripped_rid = run_id.strip()
            if stripped_rid:
                clean_run_id = stripped_rid[:128]
        if not isinstance(status, str) or not 1 <= len(status.strip()) <= 64:
            raise ValueError("trigger_fire_status_invalid")
        clean_status = status.strip()
        now = _now()

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if self._is_postgres:
                row = con.execute(
                    "INSERT INTO trigger_fires(trigger_id, dedup_key, run_id, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(dedup_key) DO NOTHING RETURNING id",
                    (clean_trigger_id, clean_dedup_key, clean_run_id, clean_status, now),
                ).fetchone()
                if row is None:
                    return None
                fire_id = int(row["id"])
            else:
                cur = con.execute(
                    "INSERT INTO trigger_fires(trigger_id, dedup_key, run_id, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(dedup_key) DO NOTHING",
                    (clean_trigger_id, clean_dedup_key, clean_run_id, clean_status, now),
                )
                if int(cur.rowcount or 0) == 0:
                    return None
                fire_id = int(cur.lastrowid)
            con.execute(
                "UPDATE trigger_definitions SET last_fired_at=?, updated_at=? WHERE id=?",
                (now, now, clean_trigger_id),
            )
        return {
            "id": fire_id,
            "trigger_id": clean_trigger_id,
            "dedup_key": clean_dedup_key,
            "run_id": clean_run_id,
            "status": clean_status,
            "created_at": now,
        }

    def list_trigger_fires(
        self, trigger_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """按 id 倒序查询指定触发器的近期触发派发记录。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("trigger_fires_limit_invalid")
        if not isinstance(trigger_id, str) or not trigger_id.strip():
            return []
        clean_id = trigger_id.strip()
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, trigger_id, dedup_key, run_id, status, created_at "
                "FROM trigger_fires WHERE trigger_id=? ORDER BY id DESC LIMIT ?",
                (clean_id, limit),
            ).fetchall()
        return [self._format_trigger_fire_row(row) for row in rows]


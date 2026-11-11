"""SQLite 持久化：把 Agent 的执行状态与模型上下文分开保存。

模型只看到当前上下文；数据库保留完整轨迹、待审批补丁和跨会话笔记。每次
操作使用独立连接，避免 FastAPI 后台线程共用连接。这里是单机教学存储，
不声称具备分布式工作队列、租约或多个主机间的 exactly-once 保证。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        with self._connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;
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
        return run

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def list_runs(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT body FROM runs ORDER BY created_at DESC LIMIT 200").fetchall()
        return [json.loads(row["body"]) for row in rows]

    def save_run(self, run: dict) -> None:
        # JSON 整体保存，新增字段无需迁移。生产环境通常拆出可检索的状态列。
        run["updated_at"] = _now()
        with self._connect() as con:
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
        self, run_id: str, path: str, old_content: str, new_content: str, operation_id: str | None = None
    ) -> dict:
        """同一工具调用只产生一个提案，弥合创建提案后、保存工具结果前的崩溃窗口。

        这只是本地数据库中提案创建的幂等键。文件写入仍需独立审批和 CAS；
        不能把它推广为任意外部系统副作用的 exactly-once 保证。
        """
        if operation_id is not None:
            self._identifier(operation_id, "operation_id")
        proposal = {
            "id": uuid.uuid4().hex,
            "run_id": run_id,
            "path": path,
            "old_content": old_content,
            "new_content": new_content,
            "old_sha256": digest(old_content),
            "new_sha256": digest(new_content),
            "status": "pending",
            "applied": False,
            "created_at": _now(),
            "operation_id": operation_id,
        }
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
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

    def mark_proposal_applied(self, proposal_id: str) -> dict:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT body FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise KeyError("proposal_not_found")
            proposal = json.loads(row["body"])
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

    def schedule_call(self, run_id: str, call_id: str, name: str, arguments: dict) -> dict:
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

    def start_call(self, run_id: str, call_id: str) -> dict:
        """明确开始一次尝试。是否允许重放由 Runtime 的工具恢复策略决定。

        running 只说明数据库里没有完成记录，不能证明上个进程没产生副作用。
        因而这里不会自行把 running/unknown 当成“可以安全重试”。本项目的
        单进程执行器需先判断只读、幂等提案或未知外部效果，再显式调用本方法。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
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

    def finish_call(self, run_id: str, call_id: str, result: Any, status: str | None = None) -> dict:
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

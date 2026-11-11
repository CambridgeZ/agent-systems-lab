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


def digest(content: str) -> str:
    """对 UTF-8 原文取摘要；审批绑定此版本，避免批准后覆盖他人的修改。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Storage:
    """一个文件保存状态；写入事务用 WAL 并限制锁等待时间。"""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        # :memory: 每个连接是一份独立数据库，不适合本实现的连接生命周期。
        if str(db_path) == ":memory:":
            raise ValueError("请使用临时目录中的 SQLite 文件，不能使用 :memory:")
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
                """
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

    def create_proposal(self, run_id: str, path: str, old_content: str, new_content: str) -> dict:
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
        }
        with self._connect() as con:
            con.execute("INSERT INTO proposals VALUES (?, ?, ?)", (proposal["id"], run_id, _dump(proposal)))
        return proposal

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

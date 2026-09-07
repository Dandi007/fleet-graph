"""事务事件日志是真相；状态快照可从最后一个事件重建。"""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, root: str | Path):
        self._transaction = None
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.sqlite3"
        with self._database() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, time REAL, "
                "kind TEXT, payload TEXT, state_after TEXT)"
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _database(self):
        if self._transaction is not None:
            yield self._transaction
            return
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def atomic(self):
        """同步业务折叠的事务；作用域内不得 await 外部 I/O。"""
        if self._transaction is not None:
            yield
            return
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            self._transaction = db
            try:
                yield
            finally:
                self._transaction = None

    @staticmethod
    def initial():
        return {
            "goal": None,
            "status": "preparing",
            "requests": {},
            "dds": {},
            "runs": {},
            "actions": {},
            "repos": {},
            "finalized": {},
            "observations": [],
            "scribe_cursor": 0,
            "stop": None,
        }

    def read(self) -> dict[str, Any]:
        with self._database() as db:
            row = db.execute("SELECT state_after FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else self.initial()

    def change(self, kind: str, payload: dict, mutate=lambda state: None):
        with self._database() as db:
            if self._transaction is None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_after FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            state = json.loads(row[0]) if row else self.initial()
            mutate(state)
            cur = db.execute(
                "INSERT INTO events(time,kind,payload,state_after) VALUES(?,?,?,?)",
                (
                    time.time(),
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(state, ensure_ascii=False),
                ),
            )
            return cur.lastrowid

    def events(self, after: int = 0, limit: int = 100):
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("分页参数无效")
        with self._database() as db:
            rows = db.execute(
                "SELECT seq,time,kind,payload FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (after, limit),
            ).fetchall()
        result = [
            {"seq": r[0], "time": r[1], "kind": r[2], "payload": json.loads(r[3])} for r in rows
        ]
        return {"events": result, "next": result[-1]["seq"] if result else after}

    def enqueue(self, request: dict):
        request = copy.deepcopy(request)

        def apply(state):
            old = state["requests"].get(request["request_id"])
            if old:
                if old["envelope"] != request:
                    raise ValueError("request_id 已用于不同请求")
                return
            state["requests"][request["request_id"]] = {"envelope": request, "status": "pending"}
            if state["status"] in {"waiting", "blocked"} and not state["stop"]:
                state["status"] = "active"

        return self.change("request.enqueued", request, apply)

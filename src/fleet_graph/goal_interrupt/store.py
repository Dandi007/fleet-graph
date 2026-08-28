"""Fail-closed SQLite durability for the E2 goal interrupt.

The interrupt checkpoint, the resume receipt and the cursor-compensation
receipt all live in one SQLite database because they are three views of one
fact -- a human decision answered a suspended question -- and losing any one of
them turns the resume into a duplicate or a drop. WAL + ``synchronous=FULL`` and
immediate transactions, mirroring ``decision_bridge/store.py``, because the same
crash discipline applies: a decision that answered a question must survive a
SIGKILL and must not be applied twice.

Invariants pinned by tests:

- A resume receipt is unique per ``resume_key`` -- one resumed envelope per
  ``(folder_id, generation, question_note_id)`` (spec item 8).
- The interrupt checkpoint is unique per ``resume_key`` and is inserted
  idempotently, because a LangGraph interrupt node re-executes on resume and
  must be able to re-state its own checkpoint without mutating it.
- The per-turn usage ledger has at most one charge row per ``turn_id``.
- A cursor-compensation receipt may be re-recorded only in the forward
  direction: a decision newer than ``last_decision_message_id`` is recorded,
  anything else is a no-op (never a cursor rollback, spec item 7).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_DB_NAME = "goal-interrupt.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS = 3000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interrupts (
    resume_key TEXT PRIMARY KEY,
    folder_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    round_id INTEGER NOT NULL,
    question_note_id TEXT NOT NULL,
    card_entity_id TEXT NOT NULL DEFAULT '',
    prior_terminal_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resume_receipts (
    resume_key TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    channel_seq INTEGER NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    question_note_id TEXT NOT NULL,
    card_entity_id TEXT NOT NULL DEFAULT '',
    refs TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compensation_receipts (
    resume_key TEXT PRIMARY KEY,
    last_decision_message_id TEXT NOT NULL,
    last_decision_seq INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_usage (
    turn_id TEXT PRIMARY KEY,
    model_invocations INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_results (
    turn_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    board_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class GoalInterruptStoreError(RuntimeError):
    """The interrupt store cannot be read or written. Callers must fail closed."""


class GoalInterruptStore:
    """The durable state surface of one line's E2 interrupt."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        db_name: str = DEFAULT_DB_NAME,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / db_name
        self.busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._conn: sqlite3.Connection | None = None

    def open(self) -> GoalInterruptStore:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            with contextlib.suppress(NameError, sqlite3.Error):
                conn.close()  # type: ignore[possibly-undefined]
            raise GoalInterruptStoreError(
                f"goal-interrupt store unusable at {self.db_path}: {type(exc).__name__}: {exc}"
            ) from exc
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise GoalInterruptStoreError("goal-interrupt store is not open")
        return self._conn

    # --- interrupts -------------------------------------------------------

    def put_interrupt(self, checkpoint: dict[str, Any]) -> bool:
        """Persist one interrupt checkpoint, idempotently.

        Returns True when a new row was inserted, False when the ``resume_key``
        already existed (a resume re-execution re-stating its own checkpoint).
        """
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = (
                conn.execute(
                    """
                    INSERT INTO interrupts (
                        resume_key, folder_id, generation, round_id,
                        question_note_id, card_entity_id, prior_terminal_digest,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (resume_key) DO NOTHING
                    """,
                    (
                        str(checkpoint["resume_key"]),
                        str(checkpoint["folder_id"]),
                        int(checkpoint["generation"]),
                        int(checkpoint["round_id"]),
                        str(checkpoint["question_note_id"]),
                        str(checkpoint.get("card_entity_id") or ""),
                        str(checkpoint["prior_terminal_digest"]),
                        now,
                        now,
                    ),
                ).rowcount
                or 0
            ) > 0
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise GoalInterruptStoreError(f"goal-interrupt checkpoint write failed: {exc}") from exc
        return inserted

    def interrupt(self, resume_key: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT * FROM interrupts WHERE resume_key = ?", (resume_key,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(
                f"goal-interrupt checkpoint unreadable for {resume_key}: {exc}"
            ) from exc
        return dict(row) if row is not None else None

    def interrupts(self) -> list[dict[str, Any]]:
        conn = self._require_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM interrupts ORDER BY created_at, resume_key"
            ).fetchall()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(f"goal-interrupt checkpoints unreadable: {exc}") from exc
        return [dict(row) for row in rows]

    # --- resumes ----------------------------------------------------------

    def record_resume(self, resume: dict[str, Any]) -> bool:
        """Persist one resumed decision. Unique per ``resume_key`` -- the second
        delivery of the same decision is a logical no-op, never a second envelope.

        Returns True when a new row was inserted, False on a deduped duplicate.
        """
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = (
                conn.execute(
                    """
                    INSERT INTO resume_receipts (
                        resume_key, message_id, channel_seq, decision, rationale,
                        decided_by, question_note_id, card_entity_id, refs, decided_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (resume_key) DO NOTHING
                    """,
                    (
                        str(resume["resume_key"]),
                        str(resume["message_id"]),
                        int(resume.get("channel_seq") or 0),
                        str(resume["decision"]),
                        str(resume.get("rationale") or ""),
                        str(resume.get("decided_by") or ""),
                        str(resume["question_note_id"]),
                        str(resume.get("card_entity_id") or ""),
                        json.dumps(resume.get("refs") or [], sort_keys=True),
                        str(resume.get("decided_at") or ""),
                        now,
                        now,
                    ),
                ).rowcount
                or 0
            ) > 0
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise GoalInterruptStoreError(f"goal-interrupt resume write failed: {exc}") from exc
        return inserted

    def resume_receipt(self, resume_key: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT * FROM resume_receipts WHERE resume_key = ?", (resume_key,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(
                f"goal-interrupt resume receipt unreadable for {resume_key}: {exc}"
            ) from exc
        return dict(row) if row is not None else None

    # --- cursor compensation ----------------------------------------------

    def record_compensation(self, resume_key: str, message_id: str, seq: int) -> bool:
        """Record a cursor-compensation receipt for a newer decision.

        Monotonic in the forward direction only: a message id older than (or
        equal to) ``last_decision_message_id`` is a no-op. Returns True when a
        newer decision superseded the recorded one.
        """
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT last_decision_message_id, last_decision_seq "
                "FROM compensation_receipts WHERE resume_key = ?",
                (resume_key,),
            ).fetchone()
            if existing is not None:
                existing_pair = (
                    int(existing["last_decision_seq"]),
                    str(existing["last_decision_message_id"]),
                )
                if existing_pair >= (int(seq), str(message_id)):
                    conn.execute("COMMIT")
                    return False
            conn.execute(
                """
                INSERT INTO compensation_receipts (
                    resume_key, last_decision_message_id, last_decision_seq, recorded_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (resume_key) DO UPDATE SET
                    last_decision_message_id = excluded.last_decision_message_id,
                    last_decision_seq = excluded.last_decision_seq,
                    recorded_at = excluded.recorded_at
                """,
                (resume_key, str(message_id), int(seq), now),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise GoalInterruptStoreError(
                f"goal-interrupt compensation write failed: {exc}"
            ) from exc
        return True

    def compensation_receipt(self, resume_key: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT * FROM compensation_receipts WHERE resume_key = ?", (resume_key,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(
                f"goal-interrupt compensation receipt unreadable for {resume_key}: {exc}"
            ) from exc
        return dict(row) if row is not None else None

    # --- usage ledger -----------------------------------------------------

    def claim_turn(self, turn_id: str) -> bool:
        """Claim one usage/charge row for a ``turn_id``. At most one charge ever.

        Returns True on a fresh claim (this model invocation is the first for
        this turn), False when the turn was already charged -- a duplicate
        delivery must not invoke the model a second time (spec item 8)."""
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = (
                conn.execute(
                    """
                    INSERT INTO turn_usage (turn_id, model_invocations, created_at, updated_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT (turn_id) DO NOTHING
                    """,
                    (str(turn_id), now, now),
                ).rowcount
                or 0
            ) > 0
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise GoalInterruptStoreError(f"goal-interrupt turn claim failed: {exc}") from exc
        return inserted

    def turn_invocations(self, turn_id: str) -> int:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT model_invocations FROM turn_usage WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(f"goal-interrupt turn usage unreadable: {exc}") from exc
        return int(row["model_invocations"]) if row is not None else 0

    def record_turn_result(self, turn_id: str, result: dict[str, Any]) -> None:
        """Persist one coordinator turn's result, keyed by ``turn_id``.

        This is the durable half of the no-second-invocation guard: once a turn
        is claimed and its coordinator result is in hand, the result is written
        back so a duplicate delivery or a crash-and-restart that re-enters the
        interrupt node can re-adopt the same result instead of invoking the
        coordinator (and the model) a second time. Re-recording the same
        ``turn_id`` is idempotent (an UPSERT), never a second charge.
        """
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO turn_results (turn_id, result, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (turn_id) DO UPDATE SET
                    result = excluded.result,
                    updated_at = excluded.updated_at
                """,
                (str(turn_id), json.dumps(result, sort_keys=True), now, now),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise GoalInterruptStoreError(
                f"goal-interrupt turn result write failed: {exc}"
            ) from exc

    def turn_result(self, turn_id: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT result FROM turn_results WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(
                f"goal-interrupt turn result unreadable for {turn_id}: {exc}"
            ) from exc
        if row is None:
            return None
        try:
            payload = json.loads(row["result"])
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    # --- board cursor -----------------------------------------------------

    def cursor(self) -> int:
        conn = self._require_conn()
        try:
            row = conn.execute("SELECT board_seq FROM cursor WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise GoalInterruptStoreError(f"goal-interrupt cursor unreadable: {exc}") from exc
        return int(row["board_seq"]) if row is not None else 0

    def advance_cursor(self, seq: int) -> None:
        """Move the board cursor forward, monotonic only (``MAX``)."""
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO cursor (id, board_seq, updated_at) VALUES (1, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    board_seq = MAX(cursor.board_seq, excluded.board_seq),
                    updated_at = excluded.updated_at
                """,
                (int(seq), now),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise GoalInterruptStoreError(f"goal-interrupt cursor advance failed: {exc}") from exc


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_DB_NAME",
    "GoalInterruptStore",
    "GoalInterruptStoreError",
]

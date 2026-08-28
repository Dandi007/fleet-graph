"""Real SQLite durability for the decision bridge, fail-closed on every surface.

The bridge cannot afford an in-memory cursor: the whole point of the resume
side is that a decision the human cast must survive a restart *and* must not be
answered twice. So the source cursor (`board_seq`) and the receipt/intent live
in one SQLite database, opened with WAL + `synchronous=FULL`, and every
mutation is an immediate transaction that either lands entirely or raises.

Two invariants are load-bearing and pinned by tests:

- **The cursor advances only after a terminal disposition.** A receipt can sit
  in ``intent_recorded`` while its event's cursor position stays put, so a
  kill-restart re-reads the event and completes it instead of skipping it. The
  only statement that moves ``board_seq`` is the one that also seals the
  receipt to a terminal status, in the same immediate transaction.
- **Fail-closed, never degraded.** An unreadable, unwritable, locked or corrupt
  database raises :class:`BridgeStoreError`. The bridge catches it and refuses
  to resume -- it never continues on an empty, in-memory cursor, because losing
  the cursor is exactly what would let a decision be reapplied or dropped.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Receipt status vocabulary (closed). ``intent_recorded`` is the crash point;
#: the terminal statuses are the only states after which the cursor advances.
STATUS_INTENT_RECORDED = "intent_recorded"
STATUS_RESUMED = "resumed"
STATUS_NOOP = "noop"
STATUS_REFUSED = "refused"

TERMINAL_STATUSES = frozenset({STATUS_RESUMED, STATUS_NOOP, STATUS_REFUSED})

DEFAULT_DB_NAME = "bridge.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS = 3000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    board_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    source_message_id TEXT PRIMARY KEY,
    action_key TEXT,
    target_kind TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    generation INTEGER,
    question_note_id TEXT NOT NULL DEFAULT '',
    card_entity_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source_event TEXT NOT NULL,
    intent_recorded_at TEXT,
    owner_at TEXT,
    resumed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_action_key
    ON receipts (action_key) WHERE action_key IS NOT NULL;
"""


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class BridgeStoreError(RuntimeError):
    """The durable store cannot be read or written. The bridge must fail closed."""


class BridgeStore:
    """The bridge's one durable state surface: a cursor and a receipt table.

    Every read and write is guarded: ``sqlite3.Error`` and ``OSError`` are
    translated into :class:`BridgeStoreError` rather than swallowed, because a
    swallowed durability failure is how a bridge turns into either a duplicate
    accepter or a decision dropper.
    """

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

    # --- lifecycle --------------------------------------------------------

    def open(self) -> BridgeStore:
        """Open (and initialise if needed) the database. Raising is the contract."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,  # we drive transactions explicitly
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            with contextlib.suppress(NameError, sqlite3.Error):
                conn.close()  # type: ignore[possibly-undefined]
            raise BridgeStoreError(
                f"decision-bridge store unusable at {self.db_path}: {type(exc).__name__}: {exc}"
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
            raise BridgeStoreError("decision-bridge store is not open")
        return self._conn

    # --- reads ------------------------------------------------------------

    def cursor(self) -> int:
        """The persisted source cursor (``board_seq``). No row means 0."""
        conn = self._require_conn()
        try:
            row = conn.execute("SELECT board_seq FROM cursor WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise BridgeStoreError(f"decision-bridge cursor unreadable: {exc}") from exc
        return int(row["board_seq"]) if row is not None else 0

    def receipt(self, source_message_id: str) -> dict[str, Any] | None:
        """The receipt for one source event, or None.

        The dict keyset is the closed receipt shape (source message, exact
        target/generation/question, action key, status, reason, source event),
        so callers and the acceptance script read it without drifting.
        """
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT * FROM receipts WHERE source_message_id = ?", (source_message_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise BridgeStoreError(
                f"decision-bridge receipt unreadable for {source_message_id}: {exc}"
            ) from exc
        return dict(row) if row is not None else None

    def receipts(self) -> list[dict[str, Any]]:
        conn = self._require_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM receipts ORDER BY created_at, source_message_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise BridgeStoreError(f"decision-bridge receipts unreadable: {exc}") from exc
        return [dict(row) for row in rows]

    def advance_cursor(self, seq: int) -> None:
        """Move the cursor forward for a message that is conclusively not a
        decision (or a decision already terminally sealed). Monotonic only: a
        lower value is a no-op, so a replayed skip can never move the cursor
        backwards past an unprocessed decision."""
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
                (int(seq), _iso(self._clock())),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise BridgeStoreError(f"decision-bridge cursor advance failed: {exc}") from exc

    # --- writes -----------------------------------------------------------

    def record_intent(self, receipt: dict[str, Any]) -> None:
        """Persist ``intent_recorded`` *before* the outward call.

        One immediate transaction. The cursor is deliberately untouched: the
        event has not reached a terminal disposition, so a crash that lands
        here replays the event and completes it rather than skipping past it.

        A re-insert of the same source event raises (fail-closed): the bridge
        never records two intents for one decision.
        """
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO receipts (
                    source_message_id, action_key, target_kind, target_id,
                    generation, question_note_id, card_entity_id, status,
                    reason, source_event, intent_recorded_at, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(receipt["source_message_id"]),
                    receipt.get("action_key"),
                    str(receipt.get("target_kind") or ""),
                    str(receipt.get("target_id") or ""),
                    receipt.get("generation"),
                    str(receipt.get("question_note_id") or ""),
                    str(receipt.get("card_entity_id") or ""),
                    STATUS_INTENT_RECORDED,
                    str(receipt.get("reason") or ""),
                    json.dumps(receipt.get("source_event") or {}, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise BridgeStoreError(f"decision-bridge intent write failed: {exc}") from exc

    def seal_terminal(
        self,
        receipt: dict[str, Any],
        *,
        advance_seq: int,
        owner_at: str | None = None,
    ) -> None:
        """Seal a receipt to a terminal status and advance the cursor, atomically.

        The one and only statement that moves ``board_seq``. A terminal no-op
        receipt (zero/multiple/stale/invalid) also flows through here, so the
        cursor advances for a conclusively-considered decision exactly as it
        does for a resumed one. The cursor update is monotonic (``MAX``), so a
        seal can never move it backwards past a decision another seal already
        advanced it over. Status must be terminal.
        """
        status = str(receipt["status"])
        if status not in TERMINAL_STATUSES:
            raise BridgeStoreError(f"seal_terminal requires a terminal status, got {status!r}")
        now = _iso(self._clock())
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO receipts (
                    source_message_id, action_key, target_kind, target_id,
                    generation, question_note_id, card_entity_id, status,
                    reason, source_event, intent_recorded_at, owner_at,
                    resumed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_message_id) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    action_key = excluded.action_key,
                    target_kind = excluded.target_kind,
                    target_id = excluded.target_id,
                    generation = excluded.generation,
                    question_note_id = excluded.question_note_id,
                    card_entity_id = excluded.card_entity_id,
                    owner_at = excluded.owner_at,
                    resumed_at = excluded.resumed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    str(receipt["source_message_id"]),
                    receipt.get("action_key"),
                    str(receipt.get("target_kind") or ""),
                    str(receipt.get("target_id") or ""),
                    receipt.get("generation"),
                    str(receipt.get("question_note_id") or ""),
                    str(receipt.get("card_entity_id") or ""),
                    status,
                    str(receipt.get("reason") or ""),
                    json.dumps(receipt.get("source_event") or {}, sort_keys=True),
                    receipt.get("intent_recorded_at"),
                    owner_at,
                    receipt.get("resumed_at"),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO cursor (id, board_seq, updated_at) VALUES (1, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    board_seq = MAX(cursor.board_seq, excluded.board_seq),
                    updated_at = excluded.updated_at
                """,
                (int(advance_seq), now),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise BridgeStoreError(f"decision-bridge terminal seal failed: {exc}") from exc


__all__ = [
    "DEFAULT_DB_NAME",
    "STATUS_INTENT_RECORDED",
    "STATUS_NOOP",
    "STATUS_REFUSED",
    "STATUS_RESUMED",
    "TERMINAL_STATUSES",
    "BridgeStore",
    "BridgeStoreError",
]

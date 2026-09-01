"""The enrollment pending queue: applications awaiting the supervisory face.

``goal_enroll`` is an *application*, not an ignition. A passing submission
lands in the pending queue (``enroll-queue.jsonl``) as a ``pending`` entry the
supervisory face can see (read-model ``/v1/enrollments``, E8, the board
question note) and then decide. The real roster (``config/ronin-lines.json``)
is only ever written by the roster-PR path -- this store never admits a line,
it only holds the application.

State machine: ``pending -> admitted | rejected | withdrawn``. The terminal
states carry ``decided_by`` / ``decision_ref`` (the decision pointer). Withdraw
keeps the row in place and flips the status -- 失败留痕原则: an application
that was submitted is history, never a deleted row. Idempotency is per
``folder_id``: a repeated submit while the folder is still ``pending`` returns
the existing entry with ``already_pending`` (the ``already_enrolled`` answer is
the roster reader's job, checked against the real roster).

Persisted as newline-delimited JSON, one line per folder (the current state,
rewritten atomically on change), under a store root -- the same shape the
state read-model already reads for the roster, so ``/v1/enrollments`` can
re-read this file per request. Rejections (拒绝史) are kept in a separate
append-only file keyed by folder.

The store root is the goal service's **independent queue home** (default
``/data/fleet-graph/goal/``), deliberately separate from the work-folder-root
that owns goal folders: goal enrollment reads/writes only this queue home and
never pollutes another governance warehouse. ``migrate_queue_home`` relocates
legacy queue files out of an old root into the queue home deterministically
and idempotently.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fleet_graph.goal_enroll.contract import (
    CODE_NOT_PENDING,
    QUEUE_STATUS_ADMITTED,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_REJECTED,
    QUEUE_STATUS_WITHDRAWN,
    GoalEnrollError,
    iso_timestamp,
)

QUEUE_FILE = "enroll-queue.jsonl"
REJECTIONS_FILE = "enroll-rejections.jsonl"

#: Terminal states a decision leaves behind; only ``pending`` is actionable.
_TERMINAL = (QUEUE_STATUS_ADMITTED, QUEUE_STATUS_REJECTED, QUEUE_STATUS_WITHDRAWN)


def migrate_queue_home(legacy_root: str | Path | None, queue_home: str | Path) -> tuple[str, ...]:
    """Relocate legacy queue files out of the old root into the goal queue home.

    Deterministic and idempotent: for each queue file (``enroll-queue.jsonl``
    and ``enroll-rejections.jsonl``), if it exists at ``legacy_root`` and the
    destination does not already exist, it is moved (never copied/duplicated,
    never overwritten) into ``queue_home``. Re-running is a no-op: an already
    relocated file is no longer at the legacy root, and a file already present
    at the destination is left untouched. Returns the file names moved.
    """
    if legacy_root in (None, ""):
        return ()
    base = Path(legacy_root)
    home = Path(queue_home)
    moved: list[str] = []
    for name in (QUEUE_FILE, REJECTIONS_FILE):
        src = base / name
        dst = home / name
        if src.is_file() and not dst.exists():
            home.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
            moved.append(name)
    return tuple(moved)


class EnrollQueue:
    """One current application per ``folder_id``, with its state machine.

    ``root=None`` keeps an in-memory queue (tests and the acceptance drill use
    this); a real root persists ``enroll-queue.jsonl`` and
    ``enroll-rejections.jsonl`` under the goal service's independent queue
    home (default ``/data/fleet-graph/goal/``), never inside the
    work-folder-root.
    """

    def __init__(self, root: str | Path | None = None, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._by_folder: dict[str, dict[str, Any]] = {}
        self._rejections: dict[str, list[dict[str, Any]]] = {}
        self._queue_path: Path | None = None
        self._rejections_path: Path | None = None
        if root is not None:
            base = Path(root)
            base.mkdir(parents=True, exist_ok=True)
            self._queue_path = base / QUEUE_FILE
            self._rejections_path = base / REJECTIONS_FILE
            self._load()

    # --- persistence ------------------------------------------------------

    def _load(self) -> None:
        if self._queue_path is not None and self._queue_path.is_file():
            for line in self._queue_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                folder_id = record.get("folder_id")
                if folder_id:
                    self._by_folder[folder_id] = record
        if self._rejections_path is not None and self._rejections_path.is_file():
            for line in self._rejections_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                folder_id = record.get("folder_id")
                if folder_id:
                    self._rejections.setdefault(folder_id, []).append(record)

    def _persist_queue(self) -> None:
        if self._queue_path is None:
            return
        body = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in self._by_folder.values()
        )
        self._queue_path.write_text(body, encoding="utf-8")

    def _persist_rejections(self) -> None:
        if self._rejections_path is None:
            return
        lines: list[dict[str, Any]] = []
        for folder_id in sorted(self._rejections):
            lines.extend(self._rejections[folder_id])
        self._rejections_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in lines
            ),
            encoding="utf-8",
        )

    # --- the state machine ------------------------------------------------

    def submit(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Land one validated application as a ``pending`` entry.

        Idempotent per folder: a folder that is still ``pending`` returns the
        existing entry with ``already_pending`` instead of forking a second
        application. A terminal state (admitted/rejected/withdrawn) is a
        decision already made, so the same folder submitting again starts a
        fresh ``pending`` application.
        """
        folder_id = str(entry["folder_id"])
        existing = self._by_folder.get(folder_id)
        if existing is not None and existing.get("status") == QUEUE_STATUS_PENDING:
            return {**existing, "already_pending": True}
        record = {
            **entry,
            "folder_id": folder_id,
            "status": QUEUE_STATUS_PENDING,
            "submitted_at": entry.get("submitted_at") or iso_timestamp(self._clock()),
            "history": [{"status": QUEUE_STATUS_PENDING, "at": entry.get("submitted_at")}],
        }
        self._by_folder[folder_id] = record
        self._persist_queue()
        return {**record, "already_pending": False}

    def record_board_notify(self, folder_id: str, value: str) -> dict[str, Any] | None:
        """Attach the best-effort board question-note result to the entry."""
        existing = self._by_folder.get(folder_id)
        if existing is None:
            return None
        existing["board_notify"] = value
        self._persist_queue()
        return {**existing}

    def _require_pending(self, folder_id: str) -> dict[str, Any]:
        existing = self._by_folder.get(folder_id)
        if existing is None or existing.get("status") != QUEUE_STATUS_PENDING:
            raise GoalEnrollError(
                CODE_NOT_PENDING,
                f"enrollment {folder_id!r} is not pending (cannot be moved from "
                f"{existing.get('status') if existing else 'absent'})",
            )
        return existing

    def _transition(self, folder_id: str, status: str, **extra: Any) -> dict[str, Any]:
        existing = self._require_pending(folder_id)
        now = iso_timestamp(self._clock())
        record = {**existing, "status": status, **extra}
        record["history"] = [
            *list(existing.get("history") or []),
            {"status": status, "at": now},
        ]
        self._by_folder[folder_id] = record
        self._persist_queue()
        return {**record}

    def withdraw(self, folder_id: str, *, by: str) -> dict[str, Any]:
        """Withdraw a pending application. Only ``pending`` can be withdrawn;
        the row stays (status ``withdrawn``) -- 失败留痕原则."""
        return self._transition(folder_id, QUEUE_STATUS_WITHDRAWN, withdrawn_by=by)

    def mark_admitted(
        self, folder_id: str, *, decided_by: str, decision_ref: str
    ) -> dict[str, Any]:
        """The release flow's mechanical step: a decided application becomes
        ``admitted`` and carries its decision pointer."""
        return self._transition(
            folder_id,
            QUEUE_STATUS_ADMITTED,
            decided_by=decided_by,
            decision_ref=decision_ref,
        )

    def mark_rejected(
        self, folder_id: str, *, decided_by: str, decision_ref: str
    ) -> dict[str, Any]:
        """A decided application becomes ``rejected`` with its decision pointer."""
        return self._transition(
            folder_id,
            QUEUE_STATUS_REJECTED,
            decided_by=decided_by,
            decision_ref=decision_ref,
        )

    def record_rejection(
        self, folder_id: str, *, code: str, detail: str, alias: str | None = None
    ) -> None:
        """Append one gate refusal to the folder's rejection history (拒绝史)."""
        record = {
            "folder_id": folder_id,
            "code": code,
            "detail": detail[:300],
            "at": iso_timestamp(self._clock()),
        }
        if alias:
            record["alias"] = alias
        self._rejections.setdefault(folder_id, []).append(record)
        self._persist_rejections()

    # --- reads ------------------------------------------------------------

    def get(self, folder_id: str) -> dict[str, Any] | None:
        return self._by_folder.get(folder_id)

    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._by_folder.values())

    def pending(self) -> tuple[dict[str, Any], ...]:
        return tuple(e for e in self._by_folder.values() if e.get("status") == QUEUE_STATUS_PENDING)

    def rejections(self, folder_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(self._rejections.get(folder_id, ()))

    def __len__(self) -> int:
        return len(self._by_folder)


__all__ = ["QUEUE_FILE", "REJECTIONS_FILE", "EnrollQueue", "migrate_queue_home"]

"""The goal-line roster: the durable, engine-versioned admission registry.

``goal_enroll`` refuses a folder that is already admitted rather than admitting
a second, conflicting entry -- the roster is a registry, not a log, so one line
has one seat. Admission is idempotent per ``folder_id``: re-running the tool
against the same admitted folder returns the existing entry (``already_admitted``)
instead of forking a second record, matching the adoption ledger's replay
discipline in the dev-dispatch plane.

Entries are persisted as newline-delimited JSON (one admitted entry per line)
under a store root, appended atomically. The store is a rebuildable cache of
the admitted lines: losing the file loses the admitted set, but the file is the
only authority -- there is no second truth to drift against.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fleet_graph.goal_enroll.contract import GoalRosterEntry

ROSTER_FILE = "goal-roster.jsonl"


class GoalEnrollRoster:
    """The admitted goal lines, keyed by ``folder_id``.

    Persists to one JSONL file under ``root``; ``root=None`` keeps an in-memory
    registry (tests and the acceptance drill use this). ``admit`` is idempotent:
    an already-admitted folder returns the existing entry with
    ``already_admitted=True`` rather than a second record.
    """

    def __init__(self, root: str | Path | None = None, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._by_folder: dict[str, dict[str, Any]] = {}
        self._path: Path | None = None
        if root is not None:
            base = Path(root)
            base.mkdir(parents=True, exist_ok=True)
            self._path = base / ROSTER_FILE
            self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
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

    def _append(self, record: dict[str, Any]) -> None:
        if self._path is None:
            return
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def admit(self, entry: GoalRosterEntry) -> dict[str, Any]:
        """Admit one entry, or return the existing one on a re-admit."""
        existing = self._by_folder.get(entry.folder_id)
        if existing is not None:
            return {**existing, "already_admitted": True}
        record = entry.as_dict()
        self._by_folder[entry.folder_id] = record
        self._append(record)
        return {**record, "already_admitted": False}

    def get(self, folder_id: str) -> dict[str, Any] | None:
        return self._by_folder.get(folder_id)

    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._by_folder.values())

    def __len__(self) -> int:
        return len(self._by_folder)


__all__ = ["ROSTER_FILE", "GoalEnrollRoster"]

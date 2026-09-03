"""The read-only real-roster reader: ``config/ronin-lines.json``.

The spec's background gap is that the old ``goal_enroll`` wrote a parallel
``goal-roster.jsonl`` that zero production readers consumed -- the scheduler,
the state read-model and the supervisor all read ``config/ronin-lines.json``.
So the real roster is this file, and this module reads it *read-only*: it is
what ``goal_list`` merges with the pending queue, what ``goal_status`` answers
against, and what ``already_enrolled`` means. Nothing here ever writes the
roster -- roster writes belong to the supervisory roster-PR path.

Fail-soft in the same spirit as the rest of the fleet: a missing or unreadable
roster degrades to an empty line set (and therefore "not enrolled" / "alias
free"), never a crash, never a 5xx.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The default roster path, relative to the fleet-graph checkout (the same
#: default the state read-model and the scheduler use).
DEFAULT_LINES_CONFIG = Path("config/ronin-lines.json")


class RealRosterReader:
    """One line per roster entry, as the scheduler's own loader sees it.

    ``path=None`` uses the repository-default ``config/ronin-lines.json``.
    Missing/unreadable/malformed files degrade to an empty roster -- "not
    enrolled" is the safe reading of not knowing.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_LINES_CONFIG

    def _lines_raw(self) -> list[Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, dict):
            return []
        entries = raw.get("lines") or []
        return entries if isinstance(entries, list) else []

    def entries(self) -> tuple[dict[str, Any], ...]:
        out: list[dict[str, Any]] = []
        for entry in self._lines_raw():
            if not isinstance(entry, dict):
                continue
            folder_id = entry.get("folder_id")
            if not folder_id:
                continue
            projected: dict[str, Any] = {
                "folder_id": str(folder_id),
                "seat": str(entry.get("seat") or ""),
                "alias": str(entry.get("alias") or ""),
                "max_rounds": entry.get("max_rounds"),
                "enabled": bool(entry.get("enabled", False)),
                "generation": entry.get("generation"),
            }
            # M4 acceptance-freeze pin (both optional, roster-PR authored):
            # the dd-acceptance block digest pinned at enlistment, and the
            # declared acceptance argv when the roster carries it. Absent
            # fields stay absent -- never guessed.
            if entry.get("acceptance_digest"):
                projected["acceptance_digest"] = str(entry["acceptance_digest"])
            declared_argv = entry.get("acceptance_argv") or entry.get("acceptance")
            if declared_argv:
                projected["acceptance_argv"] = declared_argv
            out.append(projected)
        return tuple(out)

    def get(self, folder_id: str) -> dict[str, Any] | None:
        for entry in self.entries():
            if entry["folder_id"] == folder_id:
                return entry
        return None

    def has(self, folder_id: str) -> bool:
        return self.get(folder_id) is not None

    def aliases(self) -> set[str]:
        return {entry["alias"] for entry in self.entries() if entry["alias"]}


__all__ = ["DEFAULT_LINES_CONFIG", "RealRosterReader"]

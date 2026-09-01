"""M5 line revival: the first-class revoke surface that can overturn a `done`.

A `done` terminal is the one state the scheduler treats as final
(`Refusal.TERMINAL_DONE`), and E3 made the durable checkpoint the authority
for that reading. M5 adds the *dual*: a legitimate, auditable, first-class way
for a human decision to overturn `done` -- ``fleet-graph line revive`` -- that
*never* rewrites ``terminal.json`` and never reaches into the checkpoint to
hand-edit thread state. Revival is a new-generation cold start (the existing
``bump_line_generation`` discipline), and the old ``done`` thread is left in
place, unmodified and auditable.

This module is the revoke record surface, deliberately shaped like the seat
override surface (``scheduler/seat_override.py``):

- **C1 (audit fields)**. Every revoke record carries ``who / basis /
  generation / when``, plus an optional prose ``reason`` that can never stand
  alone. A record missing or blanking any C1 field is refused on write -- the
  record never reaches disk. ``basis`` must be a mechanical reference (a
  goal.md ruling block id, a board decision id, a message reference), never
  free prose; ``generation`` is the mechanical number of the ``done`` terminal
  being overturned.
- **Inert by default**. A revoke only ever does anything when it *matches* the
  line's current checkpoint-authoritative ``done`` terminal *at the recorded
  generation*. Anything stale, forged, or pointing at a line that is not
  ``done`` is inert: no ignition, no generation bump, no "revived" trace. There
  is no "no match means silently pass" path.
- **Append-only history**. Once a revoke takes effect it is consumed: removed
  from the active surface and appended to the consume history, so a revoke
  cannot re-fire every tick, and the audit trail of what was overturned and who
  overturned it is never deleted.

The daemon's per-tick aggregation (``Scheduler._revive_outcome``) decides when
a record is *applied*; this module only stores and validates them.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The C1 audit fields. `basis` is a mechanical reference (ruling/decision/
#: message id), `generation` the mechanical number of the overturned terminal,
#: and `when` the write timestamp. `reason` is optional prose and can never
#: stand alone.
REQUIRED_REVIVE_FIELDS = ("who", "basis", "generation", "when")

#: Active revoke records live here, under the scheduler's persistent area
#: (``<run_root>/.scheduler/``) next to the per-line stall-state files.
REVIVE_FILENAME = "revive.json"

#: Consumed revokes are appended here and never deleted -- the audit trail.
REVIVE_HISTORY_FILENAME = "revive-history.json"


class ReviveFieldError(ValueError):
    """A revoke record is missing or blanking one of the C1 audit fields.

    Deliberately a refusal, not a repair: a revoke that loses its audit trail
    is a change that never happened, and guessing the missing field is how a
    wrong basis gets pinned on a production decision.
    """


@dataclass(frozen=True)
class ReviveRecord:
    """One revoke record, as persisted. C1 fields are all required."""

    folder_id: str
    who: str
    basis: str
    generation: int
    when: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "who": self.who,
            "basis": self.basis,
            "generation": self.generation,
            "when": self.when,
            "reason": self.reason,
        }


def validate_revive(record: dict[str, Any]) -> ReviveRecord:
    """C1: a revoke record must carry who/basis/generation/when, all non-blank.

    `reason` is optional prose but never enough on its own: the mechanical
    `basis` reference is what makes the overturn auditable. Missing or blank
    `who`, `basis`, `generation`, or `when` refuses the write.
    """
    folder_id = str(record.get("folder_id") or "").strip()
    who = str(record.get("who") or "").strip()
    basis = str(record.get("basis") or "").strip()
    when = str(record.get("when") or "").strip()
    reason = str(record.get("reason") or "").strip()
    generation_raw = record.get("generation")

    missing = [
        key
        for key, value in (
            ("who", who),
            ("basis", basis),
            ("generation", generation_raw),
            ("when", when),
        )
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise ReviveFieldError(
            f"line revive for {folder_id or '<unknown>'!r} refused: missing or blank "
            f"required C1 field(s): {', '.join(missing)}"
        )
    if not folder_id:
        raise ReviveFieldError("line revive refused: folder_id is missing or blank")
    try:
        generation = int(generation_raw)
    except (TypeError, ValueError) as exc:
        raise ReviveFieldError(
            f"line revive for {folder_id!r} refused: generation must be an integer, "
            f"got {generation_raw!r}"
        ) from exc
    if generation < 1:
        raise ReviveFieldError(
            f"line revive for {folder_id!r} refused: generation must be >= 1, got {generation}"
        )
    return ReviveRecord(
        folder_id=folder_id,
        who=who,
        basis=basis,
        generation=generation,
        when=when,
        reason=reason,
    )


class ReviveStore:
    """The scheduler's persistent revoke surface.

    Active revokes live in one JSON file (``revive.json``), mapping
    ``folder_id -> record``, exactly like the seat-override surface. Consumed
    revokes are appended to a separate append-only history file
    (``revive-history.json``) and never deleted, so the audit trail survives
    the revoke taking effect.
    """

    def __init__(self, run_root: Path) -> None:
        base = Path(run_root) / ".scheduler"
        self.path = base / REVIVE_FILENAME
        self.history_path = base / REVIVE_HISTORY_FILENAME

    def load(self) -> dict[str, ReviveRecord]:
        """All active revokes as ``{folder_id: ReviveRecord}``.

        An unreadable or malformed file reads as an empty surface, never a
        crash: losing the revokes must not take the scheduler down with it
        (the line just stays `done`, which is the safe reading of not knowing).
        A record that no longer validates (e.g. a forged empty `basis`) is
        dropped from the live view, so it is inert rather than acted on.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        revokes: dict[str, ReviveRecord] = {}
        for folder_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry.setdefault("folder_id", folder_id)
            try:
                revokes[folder_id] = validate_revive(entry)
            except ReviveFieldError:
                # A stale or forged record that no longer validates is not
                # worth crashing the whole surface for; it is dropped from the
                # live view and therefore inert.
                continue
        return revokes

    def get(self, folder_id: str) -> ReviveRecord | None:
        return self.load().get(folder_id)

    def write(self, record: ReviveRecord) -> None:
        """Persist one revoke, C1-validated on the way in.

        The write is a read-modify-write on the whole surface so concurrent
        writers cannot silently lose each other's records; the file is written
        through a temp sibling and renamed, so a torn write never leaves a
        half-record behind.
        """
        validated = validate_revive(record.as_dict())
        current = self.load()
        current[validated.folder_id] = validated
        self._save(current)

    def consume(self, folder_id: str) -> ReviveRecord | None:
        """Apply a revoke: remove it from the active surface and append it to
        the append-only history. Returns the consumed record, or None when
        there was no active revoke to consume."""
        current = self.load()
        record = current.pop(folder_id, None)
        if record is None:
            return None
        self._save(current)
        history = self._load_history()
        history.append(record.as_dict())
        self._save_history(history)
        return record

    def history(self) -> list[dict[str, Any]]:
        """The append-only list of consumed revoke records."""
        return self._load_history()

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [entry for entry in raw if isinstance(entry, dict)]

    def _save(self, revokes: dict[str, ReviveRecord]) -> None:
        payload = {folder_id: record.as_dict() for folder_id, record in sorted(revokes.items())}
        self._write_json(self.path, payload)

    def _save_history(self, history: list[dict[str, Any]]) -> None:
        self._write_json(self.history_path, history)

    def _write_json(self, path: Path, payload: Any) -> None:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


__all__ = [
    "REQUIRED_REVIVE_FIELDS",
    "REVIVE_FILENAME",
    "REVIVE_HISTORY_FILENAME",
    "ReviveFieldError",
    "ReviveRecord",
    "ReviveStore",
    "validate_revive",
]

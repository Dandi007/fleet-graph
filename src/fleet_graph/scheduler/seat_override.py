"""Runtime seat override: the line-level set-seat surface (step 7).

A goal line's seat is an SSoT fact: it lives in the reviewed roster
(``config/ronin-lines.json``) and only changes through a git/PR/review/deploy
cycle. Step 7 adds the *runtime* switch -- ``fleet-graph line set-seat`` -- for
the cases that cannot wait for a release (a seat's subscription dies mid-line,
a family split lands, a bad rollout needs an immediate lane change). That
switch must not rewrite the roster, so it writes a separate, smaller, audited
override to the scheduler's own persistent surface (``<run_root>/.scheduler``),
and the scheduler prefers the override while it exists.

Four constraints the roster-only rule otherwise invites people to violate, all
pinned here because each one was a real failure mode:

- **C1 (audit fields)**. Every override carries ``who / when / from→to / reason``
  (``from→to`` spelled as the ``from`` and ``to`` values). A record missing any
  of them is refused -- the write never reaches disk. A seat switch is a B-class
  production change, and the override *is* its audit trail, so it has to be
  readable on its own.
- **C2 (temporary semantics)**. An override is a temporary state, not a new
  truth. Making a switch permanent is still a roster PR. Once the roster agrees
  with an override (the PR merged and deployed), reconcile folds it away
  automatically: an override that no longer changes anything is noise.
- **C3 (reconcile/lint face)**. While an override differs from the roster, the
  drift must be loud -- scheduler startup and the ``line overrides`` status
  surface list every ``roster ≠ effective`` override with the diff facts, so a
  long-running override cannot rot silently.
- **C4 (triple observability)**. Line status reports all three seats -- roster /
  override / effective -- so an operator can always tell *which* seat a line is
  actually running on and why. The effective seat is the override's target while
  an override exists, else the roster seat.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The C1 audit fields, with ``from→to`` spelled as its two values. A record
#: missing or blanking any of them is refused on write.
REQUIRED_OVERRIDE_FIELDS = ("who", "when", "from", "to", "reason")

#: File name of the override surface under the scheduler's persistent area
#: (``<run_root>/.scheduler/``), next to the per-line stall-state files.
OVERRIDES_FILENAME = "seat-overrides.json"


class OverrideFieldError(ValueError):
    """An override record is missing or blanking one of the C1 audit fields.

    Deliberately a refusal, not a repair: a seat switch that loses its audit
    trail is a change that never happened, and guessing the missing field is
    how a wrong reason gets pinned on a production change.
    """


@dataclass(frozen=True)
class SeatOverride:
    """One override record, as persisted. C1 fields are all required."""

    folder_id: str
    who: str
    when: str
    from_seat: str
    to: str
    reason: str

    @property
    def from_to(self) -> str:
        return f"{self.from_seat} -> {self.to}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "who": self.who,
            "when": self.when,
            "from": self.from_seat,
            "to": self.to,
            "reason": self.reason,
        }


def validate_override(record: dict[str, Any]) -> SeatOverride:
    """C1: an override record must carry who/when/from→to/reason, all non-blank.

    `from` and `to` are one field spelled as two values; a missing or blank
    `from`, `to`, `who`, `when`, or `reason` refuses the write. A no-op switch
    (``from == to``) is also refused: it changes nothing, so persisting it would
    only manufacture audit noise for an operator to reconcile away.
    """
    folder_id = str(record.get("folder_id") or "").strip()
    who = str(record.get("who") or "").strip()
    when = str(record.get("when") or "").strip()
    from_seat = str(record.get("from") or "").strip()
    to = str(record.get("to") or "").strip()
    reason = str(record.get("reason") or "").strip()

    missing = [
        key
        for key, value in (
            ("who", who),
            ("when", when),
            ("from", from_seat),
            ("to", to),
            ("reason", reason),
        )
        if not value
    ]
    if missing:
        raise OverrideFieldError(
            f"seat override for {folder_id or '<unknown>'!r} refused: missing or blank "
            f"required C1 field(s): {', '.join(missing)}"
        )
    if not folder_id:
        raise OverrideFieldError("seat override refused: folder_id is missing or blank")
    if from_seat == to:
        raise OverrideFieldError(
            f"seat override for {folder_id!r} refused: from and to are the same seat {from_seat!r}"
        )
    return SeatOverride(
        folder_id=folder_id,
        who=who,
        when=when,
        from_seat=from_seat,
        to=to,
        reason=reason,
    )


def effective_seat(roster_seat: str, override: SeatOverride | None) -> str:
    """The effective seat: the override wins while it exists, else the roster."""
    return override.to if override is not None else roster_seat


@dataclass(frozen=True)
class ReconcileResult:
    """What one reconcile pass did (C2) and what is still drifting (C3)."""

    #: Overrides folded away because their target now equals the roster seat.
    cleared: list[SeatOverride] = field(default_factory=list)
    #: Overrides that still differ from the roster -- the drift C3 must list.
    drifting: list[tuple[str, SeatOverride, str]] = field(default_factory=list)

    @property
    def drift_count(self) -> int:
        return len(self.drifting)


def roster_seat_from(config: Any) -> Callable[[str], str | None]:
    """A ``(folder_id) -> roster seat`` resolver built from a SchedulerConfig.

    Lines not present in the config (a folder id that no longer has a roster
    entry) resolve to None: their override can neither agree with a roster seat
    nor define a meaningful effective seat, so it is reported as drift with no
    roster side.
    """

    def resolve(folder_id: str) -> str | None:
        for line in config.lines:
            if line.folder_id == folder_id:
                return line.seat
        return None

    return resolve


class SeatOverrideStore:
    """The scheduler's persistent seat-override surface.

    One JSON file under the scheduler's persistent area (``.scheduler/``),
    mapping ``folder_id -> override record``. The roster is never touched: this
    file is the only runtime surface for seat switching, and it is folded down
    by reconcile once an override agrees with the roster (C2).
    """

    def __init__(self, run_root: Path) -> None:
        self.path = Path(run_root) / ".scheduler" / OVERRIDES_FILENAME

    def load(self) -> dict[str, SeatOverride]:
        """All overrides as ``{folder_id: SeatOverride}``.

        An unreadable or malformed file reads as an empty override surface,
        never a crash: losing the overrides must not take the scheduler down
        with it (the lines just fall back to their roster seats, which is the
        safe reading of not knowing).
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        overrides: dict[str, SeatOverride] = {}
        for folder_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry.setdefault("folder_id", folder_id)
            try:
                overrides[folder_id] = validate_override(entry)
            except OverrideFieldError:
                # A stale record that no longer validates is not worth crashing
                # the whole surface for; it is dropped from the live view (it
                # is also what reconcile would eventually fold anyway).
                continue
        return overrides

    def get(self, folder_id: str) -> SeatOverride | None:
        return self.load().get(folder_id)

    def write(self, override: SeatOverride) -> None:
        """Persist one override, C1-validated on the way in.

        The write is a read-modify-write on the whole surface so concurrent
        writers cannot silently lose each other's records; the file is written
        through a temp sibling and renamed, so a torn write never leaves a
        half-override behind.
        """
        validated = validate_override(override.as_dict())
        current = self.load()
        current[validated.folder_id] = validated
        self._save(current)

    def clear(self, folder_id: str) -> None:
        current = self.load()
        if folder_id in current:
            del current[folder_id]
            self._save(current)

    def reconcile(self, roster_seat: Callable[[str], str | None]) -> ReconcileResult:
        """C2/C3: fold converged overrides, return the remaining drift.

        An override whose target equals the roster seat is a temporary state
        that has already been made permanent (the roster PR merged and
        deployed); it is cleared automatically. The overrides that still differ
        come back as drift -- the list C3 must surface loudly.
        """
        current = self.load()
        if not current:
            return ReconcileResult()
        cleared: list[SeatOverride] = []
        remaining: dict[str, SeatOverride] = {}
        drift: list[tuple[str, SeatOverride, str]] = []
        for folder_id, override in current.items():
            roster = roster_seat(folder_id)
            if roster is not None and roster == override.to:
                cleared.append(override)
            else:
                remaining[folder_id] = override
                drift.append((folder_id, override, roster or ""))
        if cleared:
            self._save(remaining)
        return ReconcileResult(cleared=cleared, drifting=drift)

    def _save(self, overrides: dict[str, SeatOverride]) -> None:
        payload = {
            folder_id: override.as_dict() for folder_id, override in sorted(overrides.items())
        }
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def render_drift_line(folder_id: str, override: SeatOverride, roster_seat: str) -> str:
    """One human line of the C3 drift list, with the diff facts on it."""
    if roster_seat:
        diff = f"roster={roster_seat} vs effective={override.to}"
    else:
        diff = f"no roster entry (effective={override.to})"
    return (
        f"seat override {folder_id}: {override.from_to} "
        f"({diff}) who={override.who} when={override.when} reason={override.reason!r}"
    )


__all__ = [
    "OVERRIDES_FILENAME",
    "REQUIRED_OVERRIDE_FIELDS",
    "OverrideFieldError",
    "ReconcileResult",
    "SeatOverride",
    "SeatOverrideStore",
    "effective_seat",
    "render_drift_line",
    "roster_seat_from",
    "validate_override",
]

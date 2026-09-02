"""Owner discovery and recovery behind the strict resolver.

The resolver maps a decision to *zero or more* waiting owners; it never talks
to an owner directly. Owner access is behind one seam so the same bridge runs
against the real dd control plane in production and against an isolated fake
owner in the process drill (`scripts/e1_decision_bridge_acceptance.py`), with
the same contract on both sides:

- :meth:`OwnerSource.discover` answers "which owners are waiting on this
  question note right now?", re-reading the owner's card / question /
  generation / current state from its own authoritative source (the dd
  admission record for a development, the parked line state for a line).
- :meth:`OwnerSource.discover_all` lists every currently-waiting owner, which
  is what the resolver needs: the real bus refs endpoint is reverse-only, so
  a decision's question is found by checking each waiting question in reverse.
- :meth:`OwnerSource.resume` performs the recovery through the owner's
  controlled entry and passes the action key through untouched. The owner is
  responsible for the durable action-key + generation unique constraint, so a
  duplicate transport call returns the same logical success.

Both methods may raise; the bridge treats a raising discover as a fail-open
"resolve nothing" and a raising resume as a terminal ``refused`` receipt --
never a crash, never a guess.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: Owner kinds. ``dd`` recovers through the dd gate's valueless resume; ``http``
#: is the isolated-drill owner the acceptance script hosts; ``line`` names the
#: registered line entry (recovery through a controlled entry, never a raw
#: launch).
OWNER_KIND_DD = "dd"
OWNER_KIND_LINE = "line"
OWNER_KIND_HTTP = "http"

#: A resume the owner has already carried out for this action key -- a
#: duplicate transport call, not a second recovery.
RESUME_ALREADY_RESUMED = "already_resumed"
RESUME_RESUMED = "resumed"
RESUME_REFUSED = "refused"


@dataclass(frozen=True)
class OwnerTarget:
    """One waiting owner, with the facts the resolver re-verifies."""

    kind: str
    id: str
    generation: int
    question_note_id: str
    card_entity_id: str
    state: str
    #: The bounded principal (a line folder or a human subject) that dispatched
    #: this owner. Carried on dd targets so a consumed dd decision can wake the
    #: parked line that dispatched it (the 4th wake fact); empty means no
    #: dispatch provenance was recorded.
    dispatched_by: str = ""


@dataclass(frozen=True)
class OwnerResult:
    """The owner's answer to one recovery call."""

    status: str  # resumed | already_resumed | refused
    detail: str

    @property
    def logical(self) -> bool:
        """Whether this call performed a *new* recovery (not a dedup replay)."""
        return self.status == RESUME_RESUMED


@runtime_checkable
class OwnerSource(Protocol):
    def discover(self, question_note_id: str) -> list[OwnerTarget]: ...

    def discover_all(self) -> list[OwnerTarget]: ...

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult: ...


class DdOwnerSource:
    """Recovery through the dd gate's valueless resume.

    ``discover`` lists ``awaiting_gate`` developments whose pending question
    note matches, re-reading the admission record for card/generation/state --
    the same facts the gate itself holds. ``resume`` calls
    ``DdControlPlane.gate(development_id, resume=True)``, which re-reads the
    board itself and carries no verdict.
    """

    def __init__(self, dd_root: str | Path = "/data/fleet-graph/dd") -> None:
        self.dd_root = Path(dd_root)

    def _control_plane(self) -> Any:
        from fleet_graph.dd.control_plane import DdControlPlane

        return DdControlPlane(root=self.dd_root)

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        return [
            target for target in self.discover_all() if target.question_note_id == question_note_id
        ]

    def discover_all(self) -> list[OwnerTarget]:
        # A control-plane read failure *raises* rather than reading as "no
        # owner": the resolver turns that into a structured discovery-failure
        # no-op, so a control-plane outage is distinguishable from a genuine
        # zero-owner decision instead of silently sealing a no-waiting-owner
        # receipt and advancing the cursor.
        rows = self._control_plane().list(state="awaiting_gate", limit=1000).get("developments", [])
        targets: list[OwnerTarget] = []
        for row in rows:
            awaiting = row.get("awaiting") or {}
            question_note_id = str(awaiting.get("question_note_id") or "")
            if not question_note_id:
                continue
            card_entity_id = str(awaiting.get("card_entity_id") or "")
            if not card_entity_id:
                record = self._control_plane().get(str(row["development_id"]))
                card_entity_id = str(record.get("card_entity_id") or "")
            targets.append(
                OwnerTarget(
                    kind=OWNER_KIND_DD,
                    id=str(row["development_id"]),
                    generation=int(row.get("generation") or 1),
                    question_note_id=question_note_id,
                    card_entity_id=card_entity_id,
                    state=str(row.get("state") or ""),
                    #: The bounded principal that dispatched this development
                    #: (a line folder or a human subject), from the authoritative
                    #: status row -- the same fact the gate holds. Carried so a
                    #: consumed decision can wake the parked line that dispatched
                    #: this dd single (the 4th wake fact).
                    dispatched_by=str(row.get("dispatched_by") or ""),
                )
            )
        return targets

    def dispatched_by(self, development_id: str) -> str:
        """The development's `dispatched_by` provenance, straight from the
        admission record. Empty when absent. A cheap read (one file), so the
        bridge can attribute a consumed decision to the line that dispatched it
        even on the replay path, where the reconstructed target carries no
        provenance.
        """
        record_path = self.dd_root / development_id / "record.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(record.get("dispatched_by") or "")

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        from fleet_graph.dd.control_plane import ControlPlaneError

        try:
            result = self._control_plane().gate(target.id, resume=True, action_key=action_key)
        except ControlPlaneError as exc:
            if exc.code == "ALREADY_RUNNING":
                # The gate is already advancing; the decision is in effect.
                return OwnerResult(RESUME_ALREADY_RESUMED, f"{exc.code}: {exc.detail}")
            return OwnerResult(RESUME_REFUSED, f"{exc.code}: {exc.detail}")
        if result.get("already_resumed"):
            # The owner's durable (action_key, generation) unique constraint
            # already fired: this recovery is already in effect for this
            # generation, so it is the same logical success, not a second one.
            return OwnerResult(
                RESUME_ALREADY_RESUMED,
                "durable action-key dedup: recovery already in effect for this generation",
            )
        if not result.get("resume"):
            return OwnerResult(
                RESUME_ALREADY_RESUMED, json.dumps(result, ensure_ascii=False, sort_keys=True)
            )
        return OwnerResult(RESUME_RESUMED, json.dumps(result, ensure_ascii=False, sort_keys=True))


class HttpOwnerSource:
    """The isolated-drill owner: a ``discover``/``resume`` HTTP surface.

    The acceptance script hosts a fake owner and points the bridge at it, so
    the drill exercises the *real* bridge process over a *fake* owner -- never
    a mock calling the handler directly. ``trust_env=False`` mirrors
    bus/client.py: loopback traffic must not route through the host SOCKS proxy.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _client(self) -> Any:
        import httpx

        return httpx.Client(base_url=self.base_url, timeout=10.0, trust_env=False)

    def _to_target(self, owner: dict[str, Any], question_note_id: str) -> OwnerTarget:
        return OwnerTarget(
            kind=str(owner.get("kind") or OWNER_KIND_HTTP),
            id=str(owner.get("id") or ""),
            generation=int(owner.get("generation") or 1),
            question_note_id=str(owner.get("question_note_id") or question_note_id),
            card_entity_id=str(owner.get("card_entity_id") or ""),
            state=str(owner.get("state") or ""),
        )

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        with self._client() as client:
            response = client.get("/owners", params={"question_note_id": question_note_id})
        response.raise_for_status()
        owners = (response.json() or {}).get("owners", []) or []
        return [self._to_target(o, question_note_id) for o in owners]

    def discover_all(self) -> list[OwnerTarget]:
        with self._client() as client:
            response = client.get("/owners")
        response.raise_for_status()
        owners = (response.json() or {}).get("owners", []) or []
        return [self._to_target(o, "") for o in owners]

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        body = {
            "action_key": action_key,
            "kind": target.kind,
            "id": target.id,
            "generation": target.generation,
            "question_note_id": target.question_note_id,
            "card_entity_id": target.card_entity_id,
        }
        with self._client() as client:
            response = client.post("/resume", json=body)
        response.raise_for_status()
        payload = response.json() or {}
        status = str(payload.get("status") or RESUME_REFUSED)
        return OwnerResult(status=status, detail=str(payload.get("detail") or ""))


#: A parked line's waiting state, mirroring resolver.WAITING_LINE_STATE. Kept as
#: a literal here so the resolver (which imports this module) stays the single
#: owner of that vocabulary.
_LINE_PARKED_STATE = "parked"


class LineOwnerSource:
    """Recovery of a parked goal line through its registered control entry.

    A line waiting on a human decision is parked by the scheduler (``blocked``
    + ``waiting_on: "decision"``) and its first escalation asks the board a
    question. That question note id and the parked run identity are persisted in
    the line's stall-state file, so ``discover`` re-reads the *same* authoritative
    state the scheduler writes -- the parked run, the waiting generation and the
    card -- instead of guessing. ``resume`` wakes exactly that parked run through
    the registered entry (the scheduler's stall-state file), gated on the waiting
    run id and a durable ``(action_key, generation)`` claim, so a duplicate
    transport call is the same logical success, never a second recovery.
    """

    def __init__(self, run_root: str | Path, lines: list[Any]) -> None:
        self.run_root = Path(run_root)
        self.lines = list(lines)

    @staticmethod
    def _folder_id(line: Any) -> str:
        if isinstance(line, dict):
            return str(line.get("folder_id") or "")
        return str(getattr(line, "folder_id", line))

    def _stall_path(self, folder_id: str) -> Path:
        return self.run_root / ".scheduler" / f"{folder_id}.json"

    def _read_state(self, folder_id: str) -> dict[str, Any]:
        try:
            raw = json.loads(self._stall_path(folder_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _generation(self, state: dict[str, Any], line: Any) -> int:
        base = line.get("generation") if isinstance(line, dict) else getattr(line, "generation", 1)
        try:
            return int(state.get("generation") or base or 1)
        except (TypeError, ValueError):
            return 1

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        return [
            target for target in self.discover_all() if target.question_note_id == question_note_id
        ]

    def discover_all(self) -> list[OwnerTarget]:
        targets: list[OwnerTarget] = []
        for line in self.lines:
            folder_id = self._folder_id(line)
            state = self._read_state(folder_id)
            if not state.get("parked_run_id") or state.get("parked_at") is None:
                continue
            targets.append(
                OwnerTarget(
                    kind=OWNER_KIND_LINE,
                    id=folder_id,
                    generation=self._generation(state, line),
                    question_note_id=str(state.get("board_question_note_id") or ""),
                    card_entity_id=str(state.get("board_card_entity_id") or ""),
                    state=_LINE_PARKED_STATE,
                )
            )
        return targets

    def _claim_path(self, folder_id: str, generation: int, action_key: str) -> Path:
        digest = hashlib.sha256(action_key.encode("utf-8")).hexdigest()
        return (
            self.run_root
            / ".decision-bridge"
            / "resume-claims"
            / folder_id
            / f"g{generation}"
            / f"{digest}.json"
        )

    def _claim(self, folder_id: str, generation: int, action_key: str) -> bool:
        path = self._claim_path(folder_id, generation, action_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "action_key": action_key,
                    "folder_id": folder_id,
                    "generation": generation,
                    "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                handle,
                sort_keys=True,
            )
        return True

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        claimed = self._claim(target.id, target.generation, action_key)
        state = self._read_state(target.id)
        if not state.get("parked_run_id"):
            # The line is no longer parked: either the recovery completed (the
            # stall snapshot was cleared and the claim already existed, so this
            # is the same logical success, not a second one) or there was never
            # anything parked to recover.
            if not claimed:
                return OwnerResult(
                    RESUME_ALREADY_RESUMED,
                    "durable action-key dedup: this line generation already recovered",
                )
            return OwnerResult(RESUME_REFUSED, "line is not parked")
        if str(state.get("board_question_note_id") or "") != target.question_note_id:
            return OwnerResult(
                RESUME_REFUSED,
                f"line is no longer parked on question {target.question_note_id!r}",
            )
        # ``claimed`` is True here or the interrupted-recovery replay path: if a
        # previous attempt claimed but the line is still parked, it died before
        # waking the line. Wake it now so the receipt is honest about a recovery
        # that actually happened, rather than reporting a finished one it never
        # performed.
        self._wake(target.id, state)
        return OwnerResult(
            RESUME_RESUMED, f"woke line {target.id} parked run {state.get('parked_run_id')}"
        )

    def _wake(self, folder_id: str, state: dict[str, Any]) -> None:
        """Clear the parked snapshot through the registered entry: the normal
        decide order takes over on the next scheduler tick. The ``park_considered``
        marker survives so the same terminal is not immediately re-parked."""
        cleared = {
            **state,
            "parked_run_id": None,
            "parked_at": None,
            "parked_goal_revision": None,
            "parked_inbox_available": None,
            "dispatched_decision_consumed_at": None,
        }
        path = self._stall_path(folder_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cleared, sort_keys=True), encoding="utf-8")

    def record_decision_consumed(self, folder_id: str, at: float) -> bool:
        """Wake fact 4: mark that a ``work.decision.v1`` for a dd development
        this line dispatched has been consumed.

        Best-effort and never raises: this is a mechanical fact the scheduler
        reads on its next tick to wake the parked line (``_check_wake``). The
        bridge must never fail a decision's terminal seal because a wake-fact
        write could not land. Returns whether the fact was recorded.
        """
        state = self._read_state(folder_id)
        state["dispatched_decision_consumed_at"] = at
        try:
            path = self._stall_path(folder_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        except OSError:
            return False
        return True


class CompositeOwnerSource:
    """Fan a discovery out to several owners and dispatch a resume by kind.

    Production runs the dd owner and the line owner together: one decision note
    can name a dd development *or* a parked line, and the bridge must resolve
    whichever is waiting without guessing the other kind. ``resume`` routes on
    the target's kind, so a dd target never reaches the line entry and vice
    versa.
    """

    def __init__(
        self, sources: list[OwnerSource], *, kinds: dict[str, OwnerSource] | None = None
    ) -> None:
        self.sources = list(sources)
        self.kinds = dict(kinds or {})

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        targets: list[OwnerTarget] = []
        for source in self.sources:
            targets.extend(source.discover(question_note_id))
        return targets

    def discover_all(self) -> list[OwnerTarget]:
        targets: list[OwnerTarget] = []
        for source in self.sources:
            targets.extend(source.discover_all())
        return targets

    def dispatched_by(self, development_id: str) -> str:
        """The dd admission record's `dispatched_by`, routed to the dd owner.

        Exists so the bridge can attribute a consumed decision to the line that
        dispatched it even on the replay path, where the reconstructed target
        carries no provenance (see ``DdOwnerSource.dispatched_by``). The line
        owner has nothing to say about dd provenance, so only the dd source is
        asked; a missing dd source reads as no provenance.
        """
        source = self.kinds.get(OWNER_KIND_DD)
        read = getattr(source, "dispatched_by", None)
        if read is None:
            return ""
        try:
            return str(read(development_id) or "")
        except Exception:  # a provenance read must never break a resume
            return ""

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        source = self.kinds.get(target.kind)
        if source is None:
            return OwnerResult(RESUME_REFUSED, f"no owner source for kind {target.kind!r}")
        return source.resume(target, action_key)

    def record_decision_consumed(self, folder_id: str, at: float) -> bool:
        """Wake fact 4, routed to the line owner: record that a decision for a
        dd development this line dispatched was consumed. The scheduler reads
        the fact on its next tick and wakes the parked line. A missing line
        owner reads as nothing recorded (best-effort)."""
        source = self.kinds.get(OWNER_KIND_LINE)
        write = getattr(source, "record_decision_consumed", None)
        if write is None:
            return False
        try:
            return bool(write(folder_id, at))
        except Exception:  # a wake-fact write must never break a resume
            return False


__all__ = [
    "OWNER_KIND_DD",
    "OWNER_KIND_HTTP",
    "OWNER_KIND_LINE",
    "RESUME_ALREADY_RESUMED",
    "RESUME_REFUSED",
    "RESUME_RESUMED",
    "CompositeOwnerSource",
    "DdOwnerSource",
    "HttpOwnerSource",
    "LineOwnerSource",
    "OwnerResult",
    "OwnerSource",
    "OwnerTarget",
]

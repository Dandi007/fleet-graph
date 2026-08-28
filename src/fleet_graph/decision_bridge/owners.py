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
- :meth:`OwnerSource.resume` performs the recovery through the owner's
  controlled entry and passes the action key through untouched. The owner is
  responsible for the durable action-key + generation unique constraint, so a
  duplicate transport call returns the same logical success.

Both methods may raise; the bridge treats a raising discover as a fail-open
"resolve nothing" and a raising resume as a terminal ``refused`` receipt --
never a crash, never a guess.
"""

from __future__ import annotations

import json
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
        try:
            rows = (
                self._control_plane()
                .list(state="awaiting_gate", limit=1000)
                .get("developments", [])
            )
        except Exception:
            return []
        targets: list[OwnerTarget] = []
        for row in rows:
            awaiting = row.get("awaiting") or {}
            if str(awaiting.get("question_note_id") or "") != question_note_id:
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
                )
            )
        return targets

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        from fleet_graph.dd.control_plane import ControlPlaneError

        try:
            result = self._control_plane().gate(target.id, resume=True)
        except ControlPlaneError as exc:
            if exc.code == "ALREADY_RUNNING":
                # The gate is already advancing; the decision is in effect.
                return OwnerResult(RESUME_ALREADY_RESUMED, f"{exc.code}: {exc.detail}")
            return OwnerResult(RESUME_REFUSED, f"{exc.code}: {exc.detail}")
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

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        with self._client() as client:
            response = client.get("/owners", params={"question_note_id": question_note_id})
        response.raise_for_status()
        owners = (response.json() or {}).get("owners", []) or []
        return [
            OwnerTarget(
                kind=str(o.get("kind") or OWNER_KIND_HTTP),
                id=str(o.get("id") or ""),
                generation=int(o.get("generation") or 1),
                question_note_id=str(o.get("question_note_id") or question_note_id),
                card_entity_id=str(o.get("card_entity_id") or ""),
                state=str(o.get("state") or ""),
            )
            for o in owners
        ]

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


__all__ = [
    "OWNER_KIND_DD",
    "OWNER_KIND_HTTP",
    "OWNER_KIND_LINE",
    "RESUME_ALREADY_RESUMED",
    "RESUME_REFUSED",
    "RESUME_RESUMED",
    "DdOwnerSource",
    "HttpOwnerSource",
    "OwnerResult",
    "OwnerSource",
    "OwnerTarget",
]

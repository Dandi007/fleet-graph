"""B2 human recovery: an authenticated exit for work automation cannot resolve.

Adoption handles what automation can safely fold into the governed workflow.
When it cannot -- the situation is ambiguous, or acting would risk a extensive
change -- the workflow must not guess and must not deadlock. This module is the
deliberate human exit: a decision is recorded, authenticated by the governance
path, sealed in the immutable evidence trail, and the suspended work may resume
only from that recorded decision.

Three hard rules keep this a gate and not a bypass:

- **Authentication is delegated.** The decision must be authenticated by the
  caller-supplied governance authenticator; the default refuses anything that
  does not name a real actor and a real question note. The exit itself carries
  no verdict -- it records a decision a human already cast.
- **The target is immutable.** A recovery decision without a target reference
  is refused at record time -- you cannot resume "something, somehow".
- **Resume is gated on the record.** ``resume`` returns nothing unless a
  recorded decision exists for that exact target, so suspended work can only
  resume from the decision the evidence trail already attests to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fleet_graph.dd.upstream_constants import compute_json_digest

#: What produced a recovery decision. Stored on the record itself (and bound by
#: its digest) so a downstream evidence link can assert the artifact was produced
#: by this exact mechanism rather than re-typing the name at the assertion site.
RECOVERY_MECHANISM = "HumanRecoveryExit.record"


class RecoveryError(RuntimeError):
    """A recovery cannot proceed. Refuse rather than guess."""


@dataclass(frozen=True)
class RecoveryDecision:
    """One authenticated, sealed human recovery decision."""

    target_ref: str
    decision: str
    decided_by: str
    question_note_id: str = ""
    at: str = ""
    digest: str = ""
    mechanism: str = RECOVERY_MECHANISM

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_ref": self.target_ref,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "question_note_id": self.question_note_id,
            "at": self.at,
            "digest": self.digest,
            "mechanism": self.mechanism,
        }


def decision_digest(
    *,
    target_ref: str,
    decision: str,
    decided_by: str,
    question_note_id: str,
    at: str,
    mechanism: str = RECOVERY_MECHANISM,
) -> str:
    return compute_json_digest(
        {
            "target_ref": target_ref,
            "decision": decision,
            "decided_by": decided_by,
            "question_note_id": question_note_id,
            "at": at,
            "mechanism": mechanism,
        }
    )


def _require_authenticated(decided_by: str, question_note_id: str) -> bool:
    """The default governance authenticator: a real actor and a real anchor.

    A decision that names no actor, or points at no question note, is not a
    human decision the governance path could have produced -- it is a bare
    claim. The real control plane replaces this with the board-read check, so
    this default is a fail-closed floor, never a looser path.
    """
    return bool(decided_by) and bool(question_note_id)


class HumanRecoveryExit:
    """Records authenticated recovery decisions and gates resumption on them."""

    def __init__(
        self,
        *,
        authenticate: Callable[[str, str], bool] | None = None,
        records: Iterable[RecoveryDecision] = (),
    ) -> None:
        self._authenticate = authenticate if authenticate is not None else _require_authenticated
        self._records: list[RecoveryDecision] = []
        self._by_target: dict[str, RecoveryDecision] = {}
        for record in records:
            self._records.append(record)
            self._by_target[record.target_ref] = record

    def record(
        self,
        *,
        target_ref: str,
        decision: str,
        decided_by: str,
        question_note_id: str = "",
        at: str = "",
    ) -> RecoveryDecision:
        """Seal one recovery decision, or refuse.

        The immutable target reference and the human decision are both
        required; authentication runs before anything is recorded, so an
        unauthenticated claim never reaches the trail.
        """
        if not target_ref:
            raise RecoveryError("a recovery decision needs an immutable target reference")
        if not decision or not decided_by:
            raise RecoveryError("a recovery decision needs a decision and an actor")
        if not self._authenticate(decided_by, question_note_id):
            raise RecoveryError(
                f"recovery for {target_ref!r} is not authenticated by the governance path"
            )

        record = RecoveryDecision(
            target_ref=target_ref,
            decision=decision,
            decided_by=decided_by,
            question_note_id=question_note_id,
            at=at,
            digest=decision_digest(
                target_ref=target_ref,
                decision=decision,
                decided_by=decided_by,
                question_note_id=question_note_id,
                at=at,
                mechanism=RECOVERY_MECHANISM,
            ),
        )
        self._records.append(record)
        self._by_target[record.target_ref] = record
        return record

    def recorded_for(self, target_ref: str) -> RecoveryDecision | None:
        return self._by_target.get(target_ref)

    def resume(self, *, target_ref: str) -> dict[str, Any]:
        """Resume the suspended work, only from its recorded decision.

        There is no path here that resumes from a claim: without a recorded
        decision for this exact target, this refuses. The returned payload
        points at the sealed digest, so the caller (and the evidence trail)
        can name exactly which decision authorised the resumption.
        """
        record = self.recorded_for(target_ref)
        if record is None:
            raise RecoveryError(
                f"no recorded recovery decision for {target_ref!r}; "
                "suspended work resumes only from a recorded decision"
            )
        return {
            "target_ref": target_ref,
            "resumed": True,
            "decision": record.decision,
            "decided_by": record.decided_by,
            "digest": record.digest,
            "question_note_id": record.question_note_id,
        }

    def records(self) -> tuple[RecoveryDecision, ...]:
        return tuple(self._records)


__all__ = [
    "RECOVERY_MECHANISM",
    "HumanRecoveryExit",
    "RecoveryDecision",
    "RecoveryError",
    "decision_digest",
]

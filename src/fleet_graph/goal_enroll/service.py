"""The goal-line enrollment service: validator + pending queue, wired for MCP.

The spec re-reads ``goal_enroll`` from "admit to the roster" into "submit an
application": ``submit`` runs the fail-closed validator (gates 1-7), lands the
passing application in the **pending queue** (never the roster -- the roster is
only ever written by the supervisory roster-PR path), and best-effort posts a
``question`` note to the board so the application is structurally visible to
the supervisory face. Bus degradation never blocks the queue (the E8 event is
the fallback visibility); the entry records ``board_notify`` either way.

Idempotency is per ``folder_id`` and follows the spec: a folder already in the
real roster answers ``already_enrolled`` (对照真名册判定), a folder already
``pending`` in the queue answers ``already_pending``.

U4 closeout adds the supervisor release edge: :meth:`GoalEnrollService.admit`
marks a *pending* application ``admitted`` from a supervisor release verdict,
reusing the queue's existing ``mark_admitted`` write-back primitive (no
state-machine rewrite). It is supervisor-only and fail-closed: the invoking
identity must be a supervisor-plane principal (default seam = the real
supervision-root credential check), re-admitting an already-admitted
enrollment under the same ``decision_ref`` is idempotent, and a
rejected/withdrawn (or differently-admitted) enrollment refuses.

U2 adds the mirror-image supervisor rejection edge:
:meth:`GoalEnrollService.reject` marks a *pending* application ``rejected``
from a supervisor verdict, reusing the queue's existing ``mark_rejected``
primitive (again no state-machine rewrite). It shares the ``admit`` authority
boundary exactly: supervisor-only, fail-closed, idempotent for the
already-rejected-same-decision case, and refusing every non-pending (admitted,
withdrawn, differently-rejected, absent) enrollment with the existing
not-pending refusal.
"""

from __future__ import annotations

from typing import Any

from fleet_graph.goal_enroll.contract import (
    CODE_ALIAS_CONFLICT,
    CODE_DECISION_REF_REQUIRED,
    CODE_NOT_PENDING,
    CODE_NOT_SUPERVISOR,
    DRIFT_ADMITTED_MISSING_FROM_ROSTER,
    DRIFT_ROSTER_BUT_PENDING,
    ORIGIN_PENDING,
    ORIGIN_ROSTER,
    QUEUE_STATUS_ADMITTED,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_REJECTED,
    GoalEnrollError,
)
from fleet_graph.goal_enroll.queue import EnrollQueue
from fleet_graph.goal_enroll.roster import RealRosterReader
from fleet_graph.goal_enroll.validator import GoalEnrollValidator

DEFAULT_SUBMITTED_BY = "goal-mcp"


def _default_supervisor_identity_check() -> Any:
    """The U4 admission gate's production default: a supervisor-plane principal.

    Mirrors gate 6 in reverse: a line's token must NOT resolve into the
    supervision/control-plane credential root, and a supervisor identity's
    credential MUST live there. Defaults to the real ownership check over the
    fleet's supervision token root (``/data/agent-bus/tokens``); drills bind a
    scratch supervision dir so the negative cases run against the real
    canonicalization logic.
    """
    from fleet_graph.bus.tokens import build_supervisor_identity_check

    return build_supervisor_identity_check()


class GoalEnrollService:
    """The one entry point the ``goal_enroll`` / ``goal_list`` / ``goal_status``
    / ``goal_withdraw`` / ``goal_admit`` / ``goal_reject`` MCP tools drive.

    ``queue`` is the pending-queue store (spec deliverable A.1); ``roster`` is
    the read-only real-roster reader (``config/ronin-lines.json``). ``board``
    is the best-effort question-note writer (deliverable B.3); when None (no
    bus credential) the entry records ``board_notify: failed`` and E8 is the
    fallback visibility.
    """

    def __init__(
        self,
        validator: GoalEnrollValidator,
        queue: EnrollQueue | None = None,
        roster: RealRosterReader | None = None,
        *,
        board: Any = None,
        submitted_by: str = DEFAULT_SUBMITTED_BY,
        supervisor_identity_check: Any = None,
    ) -> None:
        self._validator = validator
        self._queue = queue if queue is not None else EnrollQueue()
        self._roster = roster if roster is not None else RealRosterReader()
        self._board = board
        self._submitted_by = submitted_by
        #: U4 admission gate: ``(identity) -> bool``, True only for a
        #: supervisor-plane principal. Defaults to the real supervision-root
        #: credential check; drills inject a fake.
        self._supervisor_identity_check = (
            supervisor_identity_check
            if supervisor_identity_check is not None
            else _default_supervisor_identity_check()
        )

    # --- submission -------------------------------------------------------

    def submit(
        self,
        folder_id: str,
        alias: str,
        seat_hint: str | None = None,
        max_rounds: int | None = None,
        note: str | None = None,
        submitted_by: str | None = None,
    ) -> dict[str, Any]:
        """Submit one enrollment application, or answer the idempotency case.

        Order follows the spec: an already-enrolled folder (real roster) and an
        already-pending folder (pending queue) return before any gate runs; a
        fresh folder runs every gate and lands ``pending`` on success.
        """
        roster_entry = self._roster.get(folder_id)
        if roster_entry is not None:
            return {**roster_entry, "origin": ORIGIN_ROSTER, "already_enrolled": True}

        existing = self._queue.get(folder_id)
        if existing is not None and existing.get("status") == QUEUE_STATUS_PENDING:
            return {**existing, "origin": ORIGIN_PENDING, "already_pending": True}

        # Gate 7 (service wiring): the alias must not already be claimed by a
        # real roster line or by a pending application. The validator's seam is
        # the pure rule; here it is wired against the actual roster and queue.
        claimant = self._alias_claimant(alias, skip_folder=folder_id)
        if claimant is not None:
            raise GoalEnrollError(
                CODE_ALIAS_CONFLICT,
                f"alias {alias!r} is already claimed by {claimant!r} "
                "(roster or pending queue); one line has one alias",
            )

        facts = self._validator.validate(
            folder_id, alias=alias, seat_hint=seat_hint, max_rounds=max_rounds
        )

        applicant = submitted_by or self._submitted_by
        entry = self._queue.submit(
            {
                "folder_id": folder_id,
                "alias": alias,
                "seat_hint": seat_hint,
                "max_rounds": max_rounds,
                "briefing_version": facts["briefing_version"],
                "submitted_by": applicant,
                "submitted_at": facts["admitted_at"],
                "note": note,
                "mechanism": facts["mechanism"],
                "acceptance_argv": [list(argv) for argv in facts["acceptance_argv"]],
                "liveness": [dict(result) for result in facts["liveness"]],
                "lint_warnings": list(facts["lint_warnings"]),
            }
        )

        board_notify = self._notify_board(folder_id, alias, applicant, note)
        entry = self._queue.record_board_notify(folder_id, board_notify) or entry
        return {
            **entry,
            "origin": ORIGIN_PENDING,
            "already_pending": entry.get("already_pending", False),
        }

    def _alias_claimant(self, alias: str, *, skip_folder: str) -> str | None:
        """The folder already claiming ``alias``, or None when it is free.

        Checks the real roster (read-only ``ronin-lines.json``) and the pending
        queue. ``skip_folder`` is the applicant's own folder: its own pending
        entry (already handled by the ``already_pending`` idempotency answer)
        must not look like a conflict with itself.
        """
        for entry in self._roster.entries():
            if entry.get("alias") == alias and entry.get("folder_id") != skip_folder:
                return str(entry.get("folder_id") or "")
        for entry in self._queue.entries():
            if (
                entry.get("alias") == alias
                and entry.get("folder_id") != skip_folder
                and entry.get("status") == QUEUE_STATUS_PENDING
            ):
                return str(entry.get("folder_id") or "")
        return None

    def _notify_board(self, folder_id: str, alias: str, applicant: str, note: str | None) -> str:
        """Best-effort ``question`` note on ``board:work-notes`` (B.3).

        The application is a question needing a human decision, so it posts as
        a ``question`` note -- the existing decision protocol (only
        ``work.decision.v1`` answers). ``work.note.v1`` requires a ref to an
        *existing* board entity, so the notifier materialises an application
        card first (the same pattern the scheduler's parking escalation uses),
        then asks against it. Any failure (no board, bus down, token missing,
        422) degrades to a ``failed:`` string recorded on the entry; it never
        blocks the queue, and E8 is the fallback visibility.
        """
        if self._board is None:
            return "failed:no_board_bound"
        try:
            card = self._board.publish_card(
                {
                    "title": folder_id,
                    "status": "doing",
                    "intent": f"goal enrollment application for {folder_id} (alias {alias})",
                    "work_folder_id": folder_id,
                },
                idempotency_key=f"enroll-card:{folder_id}",
            )
            ticket = self._board.ask(
                card_entity_id=card.entity_id,
                question=(
                    f"goal enrollment application {folder_id} (alias {alias}, "
                    f"submitted by {applicant}): needs a human decision; "
                    f"roster `enabled` flips only via roster PR. {note or ''}".strip()
                ),
                idempotency_key=f"enroll-question:{folder_id}",
            )
            return f"sent:{ticket.question_note_id}"
        except Exception as exc:  # telemetry must not bite
            return f"failed:{type(exc).__name__}:{str(exc)[:200]}"

    # --- unified views ----------------------------------------------------

    def list_all(self) -> dict[str, Any]:
        """Unified view: the real roster plus the pending queue, with origin and drift.

        Every entry carries ``origin`` (``roster`` or ``pending``) and a status.
        The two reconciliation drifts are *reported, never fixed* (对账分歧按宪法
        立案): a queue entry already ``admitted`` whose line is missing from
        the real roster, and a roster line that still has a ``pending`` queue
        entry.
        """
        roster_entries = [dict(e) for e in self._roster.entries()]
        queue_entries = [dict(e) for e in self._queue.entries()]

        roster_folders = {e["folder_id"] for e in roster_entries}
        for entry in roster_entries:
            entry["origin"] = ORIGIN_ROSTER
            entry["status"] = "enrolled"

        for entry in queue_entries:
            entry["origin"] = ORIGIN_PENDING
            # Drift 1: queue already admitted but the real roster has no line.
            if (
                entry.get("status") == QUEUE_STATUS_ADMITTED
                and entry["folder_id"] not in roster_folders
            ):
                entry["drift"] = DRIFT_ADMITTED_MISSING_FROM_ROSTER
            # Drift 2: the roster has the line but the queue still marks it
            # pending (an application for a line that is already in).
            elif (
                entry["folder_id"] in roster_folders and entry.get("status") == QUEUE_STATUS_PENDING
            ):
                entry["drift"] = DRIFT_ROSTER_BUT_PENDING

        return {"entries": roster_entries + queue_entries}

    def status(self, folder_id: str) -> dict[str, Any]:
        """One application's detail: roster/pending entry + rejection history."""
        roster_entry = self._roster.get(folder_id)
        queue_entry = self._queue.get(folder_id)
        rejections = list(self._queue.rejections(folder_id))
        return {
            "folder_id": folder_id,
            "roster": roster_entry,
            "queue": queue_entry,
            "rejections": rejections,
            "origin": (
                ORIGIN_ROSTER
                if roster_entry is not None
                else (ORIGIN_PENDING if queue_entry is not None else None)
            ),
            "status": (
                "enrolled"
                if roster_entry is not None
                else (queue_entry.get("status") if queue_entry is not None else None)
            ),
        }

    # --- admission (U4 supervisor release path) ----------------------------

    def admit(
        self,
        folder_id: str,
        decision_ref: str,
        *,
        decided_by: str | None = None,
    ) -> dict[str, Any]:
        """Admit one *pending* enrollment from a supervisor release verdict.

        Supervisor-only, fail-closed: the invoking identity must be a
        supervisor-plane principal (default seam = the real supervision-root
        credential check); a non-supervisor identity refuses with
        ``GOAL_ENROLL_NOT_SUPERVISOR`` and nothing changes. The queue state
        machine is untouched -- this method reuses the existing
        ``mark_admitted`` write-back primitive.

        Idempotency per spec: re-admitting an enrollment that is *already*
        ``admitted`` under the **same** ``decision_ref`` returns the existing
        entry with ``already_admitted: True`` and appends no history row.
        An already ``admitted`` enrollment under a *different* ``decision_ref``
        is refused (``GOAL_ENROLL_NOT_PENDING``), as are ``rejected`` /
        ``withdrawn`` / absent enrollments (the queue's own refusal).
        """
        identity = decided_by or self._submitted_by
        if not self._supervisor_identity_check(identity):
            raise GoalEnrollError(
                CODE_NOT_SUPERVISOR,
                f"identity {identity!r} is not a supervisor-plane principal; "
                "admission authority stays exclusively with the supervisor plane",
            )
        if not decision_ref or not str(decision_ref).strip():
            raise GoalEnrollError(
                CODE_DECISION_REF_REQUIRED,
                "admission needs the supervisor release verdict message id as decision_ref",
            )

        existing = self._queue.get(folder_id)
        if existing is not None and existing.get("status") == QUEUE_STATUS_ADMITTED:
            if existing.get("decision_ref") == decision_ref:
                return {**existing, "already_admitted": True}
            raise GoalEnrollError(
                CODE_NOT_PENDING,
                f"enrollment {folder_id!r} is already admitted under a different "
                f"decision_ref {existing.get('decision_ref')!r}; refusing a second, "
                "conflicting admission",
            )
        return self._queue.mark_admitted(folder_id, decided_by=identity, decision_ref=decision_ref)

    # --- rejection (U2 supervisor decision path) ---------------------------

    def reject(
        self,
        folder_id: str,
        decision_ref: str,
        *,
        decided_by: str | None = None,
    ) -> dict[str, Any]:
        """Reject one *pending* enrollment from a supervisor verdict.

        Supervisor-only, fail-closed: the invoking identity must be a
        supervisor-plane principal (default seam = the real supervision-root
        credential check); a non-supervisor identity refuses with
        ``GOAL_ENROLL_NOT_SUPERVISOR`` and nothing changes. The queue state
        machine is untouched -- this method reuses the existing
        ``mark_rejected`` write-back primitive and stays distinct from
        ``withdraw`` (which never produces a ``rejected`` status).

        Idempotency per spec: re-rejecting an enrollment that is *already*
        ``rejected`` under the **same** ``decision_ref`` returns the existing
        entry with ``already_rejected: True`` and appends no history row.
        An already ``rejected`` enrollment under a *different* ``decision_ref``
        is refused (``GOAL_ENROLL_NOT_PENDING``), as are ``admitted`` /
        ``withdrawn`` / absent enrollments (the queue's own refusal).
        """
        identity = decided_by or self._submitted_by
        if not self._supervisor_identity_check(identity):
            raise GoalEnrollError(
                CODE_NOT_SUPERVISOR,
                f"identity {identity!r} is not a supervisor-plane principal; "
                "rejection authority stays exclusively with the supervisor plane",
            )
        if not decision_ref or not str(decision_ref).strip():
            raise GoalEnrollError(
                CODE_DECISION_REF_REQUIRED,
                "rejection needs the supervisor verdict message id as decision_ref",
            )

        existing = self._queue.get(folder_id)
        if existing is not None and existing.get("status") == QUEUE_STATUS_REJECTED:
            if existing.get("decision_ref") == decision_ref:
                return {**existing, "already_rejected": True}
            raise GoalEnrollError(
                CODE_NOT_PENDING,
                f"enrollment {folder_id!r} is already rejected under a different "
                f"decision_ref {existing.get('decision_ref')!r}; refusing a second, "
                "conflicting rejection",
            )
        return self._queue.mark_rejected(folder_id, decided_by=identity, decision_ref=decision_ref)

    # --- withdrawal -------------------------------------------------------

    def withdraw(self, folder_id: str, *, by: str | None = None) -> dict[str, Any]:
        """Withdraw a *pending* application; the row stays as ``withdrawn``."""
        return self._queue.withdraw(folder_id, by=by or self._submitted_by)

    def record_rejection(
        self, folder_id: str, *, code: str, detail: str, alias: str | None = None
    ) -> None:
        """Fold a gate refusal into the folder's rejection history (拒绝史)."""
        self._queue.record_rejection(folder_id, code=code, detail=detail, alias=alias)

    # --- compatibility reads ---------------------------------------------

    def queue_entries(self) -> tuple[dict[str, Any], ...]:
        return self._queue.entries()


__all__ = ["DEFAULT_SUBMITTED_BY", "GoalEnrollService"]

"""The E5 fail-closed enrollment contract, in one immutable place.

Every refusal of ``goal_enroll`` is an explicit ``GoalEnrollError`` carrying a
stable machine-readable code plus the failing clause -- never a warning, never
a partial roster entry, never a deferred admission. This module holds the
codes, the error type, and the engine-versioned roster entry the tool admits.

The roster entry is the *engine-versioned artifact* the spec names: it records
the briefing version id that was live when the line was opened, so a line
admitted under briefing ``vN`` stays auditable against ``vN`` even after a
release bump ships different briefing text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

#: The version id of the briefing (交底) this engine release ships. Bumped with
#: the engine release so the ``goal-open`` prompt and the roster entries it
#: admits atomically move to the new briefing text. A version bump must come
#: with a matching bump of the briefing text in ``briefing.py``.
BRIEFING_VERSION = "v1"

#: The stable versioned resource uri serving the briefing text.
BRIEFING_RESOURCE_URI = "fleet-graph://goal-open/briefing"

#: The Phase-0 prompt name registered on the MCP surface.
GOAL_OPEN_PROMPT_NAME = "goal-open"

#: What produced a roster entry. Stored on the artifact itself so a downstream
#: reader names the mechanism instead of re-typing it.
GOAL_ENROLL_MECHANISM = "goal_enroll"

#: Fail-closed refusal codes. A code lands here only when it means "no
#: admission happened", so a refusal is never confusable with a partial admit.
CODE_SOURCE_UNBOUND = "GOAL_ENROLL_SOURCE_UNBOUND"
CODE_FOLDER_NOT_FOUND = "FOLDER_NOT_FOUND"
CODE_NOT_A_GOAL_LINE = "NOT_A_GOAL_LINE"
CODE_GOLDEN_ORDER_EMPTY = "GOLDEN_ORDER_EMPTY"
CODE_NO_ACCEPTANCE_COMMAND = "NO_ACCEPTANCE_COMMAND"
CODE_ACCEPTANCE_DECLARATION_INVALID = "ACCEPTANCE_DECLARATION_INVALID"
CODE_SPEC_LINT_BAN = "SPEC_LINT_BAN"
CODE_ACCEPTANCE_ARGV_UNEXECUTABLE = "ACCEPTANCE_ARGV_UNEXECUTABLE"

#: Gate 6: the applicant's alias token (`/data/ronin/secrets/<alias>.token`)
#: must already exist. An application whose alias has no bus credential would
#: start a line whose inbox/board face is silently half-broken
#: (bus/tokens.py:76-87), so the submission refuses closed up front.
CODE_ALIAS_TOKEN_MISSING = "GOAL_ENROLL_ALIAS_TOKEN_MISSING"

#: Gate 7: the applicant's alias must not already be claimed by a roster line
#: or a pending application. One line has one alias; a second claimant would
#: forge a second identity on the same inbox.
CODE_ALIAS_CONFLICT = "GOAL_ENROLL_ALIAS_CONFLICT"

#: ``goal_withdraw`` only ever moves a *pending* application. Anything already
#: decided (admitted/rejected) or already withdrawn refuses with this code.
CODE_NOT_PENDING = "GOAL_ENROLL_NOT_PENDING"

#: ``goal_admit`` (the supervisor release path) is supervisor-only, fail-closed:
#: the identity invoking admission must be a supervisor-plane principal. A
#: non-supervisor identity refuses with this code and nothing changes -- the
#: callable capability is created without ever broadening the authorization
#: boundary.
CODE_NOT_SUPERVISOR = "GOAL_ENROLL_NOT_SUPERVISOR"

#: ``goal_admit`` must persist the supervisor release verdict's message id as
#: ``decision_ref``; an admission without it refuses fail-closed.
CODE_DECISION_REF_REQUIRED = "GOAL_ENROLL_DECISION_REF_REQUIRED"

#: The real U4 closeout decision reference: the ``work.decision.v1`` release
#: verdict message id (board seq 1564) for wf-e7b0dd. The supervisor release
#: path persists exactly this message id as the queue entry's ``decision_ref``
#: when the real U4 closeout is harvested.
U4_CLOSEOUT_DECISION_REF = "msg_01M1EK40MW5PKWB8HKQF1EH9HJ"

#: The reserved identity paths the spec-lint bans from product code and tests.
#: References here are a refusal, not a warning: these namespaces are the
#: controller's identity boundary, and a goal that routes work into them is
#: impersonating the control plane.
RESERVED_PATHS = (".dev-dispatch", ".dd-evidence")

#: The lint warning for a critical-path table that pins a rolling 40-hex SHA.
#: It is a warning, never a refusal (the spec says so explicitly): a pinned
#: SHA in a critical path is a smell, but it does not invalidate admission.
LINT_WARNING_PINNED_SHA = "pinned_sha_in_critical_path"

#: The pending-queue state machine: ``pending -> admitted | rejected |
#: withdrawn``. Only ``pending`` is actionable (withdrawable); the terminal
#: states are decisions already made and carry ``decided_by`` / ``decision_ref``.
QUEUE_STATUS_PENDING = "pending"
QUEUE_STATUS_ADMITTED = "admitted"
QUEUE_STATUS_REJECTED = "rejected"
QUEUE_STATUS_WITHDRAWN = "withdrawn"

#: ``goal_list`` origins: every listed entry is either a real roster line
#: (read-only ``config/ronin-lines.json``) or a pending-queue application.
ORIGIN_ROSTER = "roster"
ORIGIN_PENDING = "pending"

#: The two reconciliation drifts ``goal_list`` reports (报告不修 -- 只报不修):
#: a queue entry already marked ``admitted`` while the real roster has no such
#: line, and a roster line that still has a ``pending`` queue entry. Both are
#: disagreements to file per constitution, not fixes this surface performs.
DRIFT_ADMITTED_MISSING_FROM_ROSTER = "admitted_missing_from_roster"
DRIFT_ROSTER_BUT_PENDING = "roster_but_pending"


class GoalEnrollError(RuntimeError):
    """A refusal with one stable machine-readable cause per code.

    Mirrors ``ControlPlaneError`` so the MCP layer can serialize it the same
    way the dev-dispatch refusals already travel: ``{"code": ..., "message":
    ...}`` over the wire.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.detail}


@dataclass(frozen=True)
class GoalRosterEntry:
    """One admitted goal line, sealed with its briefing version.

    The entry is the artifact the tool admits: it names the folder, the
    briefing version the line was opened under, the acceptance argv that
    passed every gate (including the server-side liveness probe), the lint
    warnings that were recorded (never refusals), and the moment of admission.
    """

    folder_id: str
    briefing_version: str
    acceptance_argv: tuple[tuple[str, ...], ...]
    liveness: tuple[dict[str, Any], ...]
    lint_warnings: tuple[str, ...]
    mechanism: str
    admitted_at: str
    engine: str = "fleet-graph"

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "briefing_version": self.briefing_version,
            "acceptance_argv": [list(argv) for argv in self.acceptance_argv],
            "liveness": [dict(result) for result in self.liveness],
            "lint_warnings": list(self.lint_warnings),
            "mechanism": self.mechanism,
            "admitted_at": self.admitted_at,
            "engine": self.engine,
        }


def iso_timestamp(ts: float) -> str:
    """UTC, second precision -- the same shape the run artifacts use."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


__all__ = [
    "BRIEFING_RESOURCE_URI",
    "BRIEFING_VERSION",
    "CODE_ACCEPTANCE_ARGV_UNEXECUTABLE",
    "CODE_ACCEPTANCE_DECLARATION_INVALID",
    "CODE_ALIAS_CONFLICT",
    "CODE_ALIAS_TOKEN_MISSING",
    "CODE_DECISION_REF_REQUIRED",
    "CODE_FOLDER_NOT_FOUND",
    "CODE_GOLDEN_ORDER_EMPTY",
    "CODE_NOT_A_GOAL_LINE",
    "CODE_NOT_PENDING",
    "CODE_NOT_SUPERVISOR",
    "CODE_NO_ACCEPTANCE_COMMAND",
    "CODE_SOURCE_UNBOUND",
    "CODE_SPEC_LINT_BAN",
    "DRIFT_ADMITTED_MISSING_FROM_ROSTER",
    "DRIFT_ROSTER_BUT_PENDING",
    "GOAL_ENROLL_MECHANISM",
    "GOAL_OPEN_PROMPT_NAME",
    "LINT_WARNING_PINNED_SHA",
    "ORIGIN_PENDING",
    "ORIGIN_ROSTER",
    "QUEUE_STATUS_ADMITTED",
    "QUEUE_STATUS_PENDING",
    "QUEUE_STATUS_REJECTED",
    "QUEUE_STATUS_WITHDRAWN",
    "RESERVED_PATHS",
    "U4_CLOSEOUT_DECISION_REF",
    "GoalEnrollError",
    "GoalRosterEntry",
    "iso_timestamp",
]

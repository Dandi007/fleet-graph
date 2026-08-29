"""The versioned opening briefing (交底) for a goal line, shipped as an MCP prompt.

The Phase-0 opening contract of a goal line moved out of the ``goal-driven-work``
skill and into this MCP surface (E5): the handoff is now pinned to an engine
release, not to a skill file. The briefing is a single versioned text rendered
from a stable resource, so a release bump atomically ships new briefing text --
and ``goal_enroll`` records the same version id on the roster entry it admits,
making every line auditable against the briefing that opened it.

The briefing carries the recorded constraints *verbatim*, so they are enforced
at the write gate rather than re-learned per line:

- the durable branch never merges to main directly (main merge and deploy are
  the supervisor's harvest);
- product code and tests never reference the reserved identity paths
  ``.dev-dispatch`` / ``.dd-evidence``;
- bus alias enrollment atomically creates the bus agent and a ``0600`` token;
- the deployment request lists every unit env dependency;
- the critical-path table must not pin a rolling SHA.
"""

from __future__ import annotations

from fleet_graph.goal_enroll.contract import (
    BRIEFING_RESOURCE_URI,
    BRIEFING_VERSION,
    GOAL_OPEN_PROMPT_NAME,
)

#: The briefing text for version ``BRIEFING_VERSION``. Changing the text is a
#: version bump: bump ``BRIEFING_VERSION`` and this text in the same change, so
#: the prompt a client reads and the version a roster entry records stay
#: consistent by construction.
BRIEFING_TEXT = f"""# Goal-line opening briefing (交底) v{BRIEFING_VERSION}

This is the engine-versioned opening contract for one goal line. It is served
by the fleet-graph MCP surface, not by a skill file; the line's roster entry
records briefing version {BRIEFING_VERSION}, so a line opened under this text
is auditable against it even after a later release ships different text.

## DoD form

The goal line's Definition of Done must be a mechanical, checkable list. State
what observable facts prove the goal is done -- never a promise of effort, and
never a term like "implemented" that a reviewer must interpret.

## Executable acceptance form

The goal must declare at least one executable acceptance command line whose
exit code is the acceptance criterion, in a ```dd-acceptance fenced block of
goal.md -- the same argv contract the roster already enforces. A goal without
an acceptance command is refused at the write gate (NO_ACCEPTANCE_COMMAND), and
a declared command that cannot even start is refused as unexecutable
(ACCEPTANCE_ARGV_UNEXECUTABLE). The server dry-runs the declared argv before
admitting, so acceptance is never free text.

## Golden-order authority

golden-order.md is the line's authority boundary and outranks the spec. It must
exist and be non-empty at admission. When a goal/spec conflict arises, the
golden order decides.

## Supervisor channel

The durable branch is the delivery endpoint; the gate decision is the delivery
criterion. The durable branch never merges to main directly, and main merge and
deploy belong to the supervisor harvest only. Delivery to main is never the
goal line's job.

## Production-safety lines

- Product code and tests never reference the reserved identity paths
  `.dev-dispatch` / `.dd-evidence`; those namespaces are the control plane's
  identity boundary.
- Bus alias enrollment atomically creates the bus agent and a `0600` token.
- The deployment request lists every unit env dependency.
- The critical-path table must not pin a rolling SHA (a pinned 40-hex SHA there
  is a lint warning, not an admission blocker).
"""


def goal_open_prompt_text() -> str:
    """The Phase-0 ``goal-open`` prompt, rendered from the versioned briefing."""
    return f"# goal-open\n\nBriefing version: {BRIEFING_VERSION}\n\n{BRIEFING_TEXT}"


__all__ = [
    "BRIEFING_RESOURCE_URI",
    "BRIEFING_TEXT",
    "BRIEFING_VERSION",
    "GOAL_OPEN_PROMPT_NAME",
    "goal_open_prompt_text",
]

"""goal-driven 入册流水线: the goal-line enrollment application surface.

This package owns the Phase-0 opening contract of a goal line as an
**application**, not an ignition: ``goal_enroll`` validates a candidate goal
folder before a ``pending`` application lands in the enrollment queue
(``enroll-queue.jsonl``, under the goal service's independent queue home
``/data/fleet-graph/goal/``), refusing closed with a stable machine-readable
code for every failing clause. Admission to the real roster
(``config/ronin-lines.json``) is deliberately NOT granted here -- the roster is
only ever written by the supervisory roster-PR path. The supervisory face sees
every application through the state read-model (``/v1/enrollments``), the E8
``enrollment_pending`` event, and the best-effort board question note.

The public surface is small and split along durability lines:

- :mod:`fleet_graph.goal_enroll.contract` -- the refusal codes, the error type,
  the queue state machine, and the engine-versioned application entry.
- :mod:`fleet_graph.goal_enroll.briefing` -- the versioned briefing text and the
  ``goal-open`` prompt rendered from it.
- :mod:`fleet_graph.goal_enroll.validator` -- the fail-closed gates
  (folder valid, acceptance argv, golden order, spec-lint, liveness probe,
  alias token existence, alias uniqueness).
- :mod:`fleet_graph.goal_enroll.queue` -- the pending-queue store
  (``pending -> admitted | rejected | withdrawn``).
- :mod:`fleet_graph.goal_enroll.roster` -- the read-only real-roster reader
  (``config/ronin-lines.json``).
- :mod:`fleet_graph.goal_enroll.source` -- the concrete goal-folder source.
- :mod:`fleet_graph.goal_enroll.service` -- validator + queue + roster,
  wired for MCP.
"""

from fleet_graph.goal_enroll.contract import (
    BRIEFING_VERSION,
    GOAL_ENROLL_MECHANISM,
    GOAL_OPEN_PROMPT_NAME,
    GoalEnrollError,
    GoalRosterEntry,
)
from fleet_graph.goal_enroll.queue import EnrollQueue
from fleet_graph.goal_enroll.roster import RealRosterReader
from fleet_graph.goal_enroll.service import GoalEnrollService
from fleet_graph.goal_enroll.store import GoalEnrollRoster
from fleet_graph.goal_enroll.validator import (
    GoalEnrollValidator,
    GoalFolderSource,
)

__all__ = [
    "BRIEFING_VERSION",
    "GOAL_ENROLL_MECHANISM",
    "GOAL_OPEN_PROMPT_NAME",
    "EnrollQueue",
    "GoalEnrollError",
    "GoalEnrollRoster",
    "GoalEnrollService",
    "GoalEnrollValidator",
    "GoalFolderSource",
    "GoalRosterEntry",
    "RealRosterReader",
]

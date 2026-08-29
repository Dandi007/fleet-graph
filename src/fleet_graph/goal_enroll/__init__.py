"""E5: the goal-line enroll MCP surface (fail-closed + versioned briefing).

This package owns the Phase-0 opening contract of a goal line as a write gate:
``goal_enroll`` validates a candidate goal folder before it can be admitted to
the roster, refusing closed with a stable machine-readable code for every
failing clause, and the opening handoff (交底) is served as a versioned MCP
prompt/resource so it is pinned to an engine release, not to a skill file.

The public surface is small and split along durability lines:

- :mod:`fleet_graph.goal_enroll.contract` -- the refusal codes, the error type,
  and the engine-versioned roster entry (briefing version id included).
- :mod:`fleet_graph.goal_enroll.briefing` -- the versioned briefing text and the
  ``goal-open`` prompt rendered from it.
- :mod:`fleet_graph.goal_enroll.validator` -- the fail-closed gates
  (folder valid, acceptance argv, golden order, spec-lint, liveness probe).
- :mod:`fleet_graph.goal_enroll.store` -- the durable roster registry.
- :mod:`fleet_graph.goal_enroll.source` -- the concrete goal-folder source.
- :mod:`fleet_graph.goal_enroll.service` -- validator + roster, wired for MCP.
"""

from fleet_graph.goal_enroll.contract import (
    BRIEFING_VERSION,
    GOAL_ENROLL_MECHANISM,
    GOAL_OPEN_PROMPT_NAME,
    GoalEnrollError,
    GoalRosterEntry,
)
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
    "GoalEnrollError",
    "GoalEnrollRoster",
    "GoalEnrollService",
    "GoalEnrollValidator",
    "GoalFolderSource",
    "GoalRosterEntry",
]

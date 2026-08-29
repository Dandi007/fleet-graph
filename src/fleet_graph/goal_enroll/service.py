"""The goal-line enrollment service: validator + roster, wired for the MCP.

``GoalEnrollService.enroll`` runs the fail-closed validator and, on success,
seals the engine-versioned roster entry (briefing version id included). On any
refusal it raises ``GoalEnrollError`` with the failing clause's code -- the MCP
layer serializes that as the machine-readable refusal.
"""

from __future__ import annotations

from typing import Any

from fleet_graph.goal_enroll.contract import GoalRosterEntry
from fleet_graph.goal_enroll.store import GoalEnrollRoster
from fleet_graph.goal_enroll.validator import GoalEnrollValidator


class GoalEnrollService:
    """The one entry point the ``goal_enroll`` MCP tool drives."""

    def __init__(
        self,
        validator: GoalEnrollValidator,
        roster: GoalEnrollRoster | None = None,
    ) -> None:
        self._validator = validator
        self._roster = roster if roster is not None else GoalEnrollRoster()

    def enroll(self, folder_id: str) -> dict[str, Any]:
        """Admit one goal line, or refuse with the failing clause's code."""
        facts = self._validator.validate(folder_id)
        entry = GoalRosterEntry(
            folder_id=facts["folder_id"],
            briefing_version=facts["briefing_version"],
            acceptance_argv=facts["acceptance_argv"],
            liveness=facts["liveness"],
            lint_warnings=facts["lint_warnings"],
            mechanism=facts["mechanism"],
            admitted_at=facts["admitted_at"],
        )
        return self._roster.admit(entry)

    def roster_entries(self) -> tuple[dict[str, Any], ...]:
        return self._roster.entries()


__all__ = ["GoalEnrollService"]

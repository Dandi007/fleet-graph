"""E2: the in-graph decision interrupt for a goal line.

This package owns the durable "park an open human question" state that E2
replaces the goal-line ``blocked + waiting_on=decision`` hand-off with. Instead
of terminating the line and letting the scheduler bump its generation, the line
suspends *in place* through a LangGraph interrupt and resumes the same
generation and continuation once a validated board decision lands.

The public surface is deliberately small and split along durability lines:

- :mod:`fleet_graph.goal_interrupt.contract` -- the immutable value objects
  (``DecisionInput``, ``InterruptCheckpoint``) and the deterministic helpers
  (``resume_key_for``, ``prior_terminal_digest``).
- :mod:`fleet_graph.goal_interrupt.store` -- the fail-closed SQLite store for
  the interrupt checkpoint, the resume receipt, the cursor-compensation receipt
  and the per-turn usage ledger.
- :mod:`fleet_graph.goal_interrupt.resolver` -- the decision-to-interrupt
  mapping, including the bounded legacy-owner fallback and the newest-decision
  selection used by cursor compensation.
- :mod:`fleet_graph.goal_interrupt.bridge` -- the resident loop that reads
  board decisions and drives each into a resume through the same ``resume_key``.
"""

from fleet_graph.goal_interrupt.contract import (
    DecisionInput,
    DecisionRef,
    InterruptCheckpoint,
    resume_key_for,
)

__all__ = [
    "DecisionInput",
    "DecisionRef",
    "InterruptCheckpoint",
    "resume_key_for",
]

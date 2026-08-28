"""Cost-observability data plane: source facts, recording rules, and reconciliation.

The package restores the four silent `cost-observability` recording rules by
re-establishing the producers they were missing. Expose the pieces the fleet
and the acceptance fixture need:

- `CostDataPlane` -- idempotent lifecycle fact emission plus exact-once
  launch/settlement reconciliation.
- `classify_tokens` / `TokenRecord` -- token attribution, with `unknown`
  kept explicit and distinct from producer-absence.
- `RECORDING_RULES` / `query` -- the five rule expressions and the small
  PromQL evaluator that answers them.
- `Sample`, `render`, `parse` -- the exposition wire format and its rendering.
"""

from __future__ import annotations

from fleet_graph.cost_obs.classify import TokenRecord, classify_tokens
from fleet_graph.cost_obs.data_plane import CostDataPlane, ReconcileReport
from fleet_graph.cost_obs.exposition import Sample, parse, render
from fleet_graph.cost_obs.query import PromQLError, query
from fleet_graph.cost_obs.rules import RECORDING_RULES, RULE_GROUP, RecordingRule

__all__ = [
    "RECORDING_RULES",
    "RULE_GROUP",
    "CostDataPlane",
    "PromQLError",
    "ReconcileReport",
    "RecordingRule",
    "Sample",
    "TokenRecord",
    "classify_tokens",
    "parse",
    "query",
    "render",
]

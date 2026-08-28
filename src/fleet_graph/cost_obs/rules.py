"""The five cost-observability recording rules.

A recording rule is a name and a PromQL expression over the source facts the
`data_plane` emits. The five here are the fleet's `cost-observability` group:
one was already emitting a series (`cost_obs:management_execution:ratio`) and
four were silent because their source facts had no producers. This module is
the single source of truth for the rule names and expressions, so the
acceptance fixture can query each one rather than re-typing PromQL throughout
the tests.

The expressions stay on the small PromQL subset the mini engine in
`query.py` evaluates: instant vector selectors with ``=`` / ``=~`` label
matches, ``sum`` / ``count`` aggregation with ``by`` grouping, and one binary
``/`` (scalar division for the ratio, ``on(order_id)`` vector matching for the
settlement reconciliation).
"""

from __future__ import annotations

from dataclasses import dataclass

RULE_GROUP = "cost-observability"

#: Base metric name for token spend, bucketed by attribution class.
#: `unknown` is a first-class attribution value, never a fallthrough.
COST_METRIC = "cost_obs_execution_cost_total"
LAUNCH_METRIC = "cost_obs_launch_total"
REVIEW_METRIC = "cost_obs_review_total"
PROMOTION_METRIC = "cost_obs_promotion_total"
SETTLEMENT_METRIC = "cost_obs_settlement_total"
PRESENCE_METRIC = "cost_obs_lifecycle_present"


@dataclass(frozen=True)
class RecordingRule:
    name: str
    expr: str
    doc: str


RECORDING_RULES: tuple[RecordingRule, ...] = (
    RecordingRule(
        name="cost_obs:management_execution:ratio",
        expr=(f'sum({COST_METRIC}{{attribution="management"}}) / sum({COST_METRIC})'),
        doc=(
            "Management-attributed spend as a fraction of all token spend. "
            "This is the one rule that never broke: it needs no lifecycle "
            "fact, only the cost metric, so it emitted a series even while "
            "the other four were silent."
        ),
    ),
    RecordingRule(
        name="cost_obs:launch_lifecycle:total",
        expr=f"sum({LAUNCH_METRIC})",
        doc="Total DD launch lifecycle events, one series per emitted launch fact.",
    ),
    RecordingRule(
        name="cost_obs:review_lifecycle:total",
        expr=f'sum({REVIEW_METRIC}{{phase=~"continuous|final"}})',
        doc=(
            "Total review lifecycle events. The `=~` matcher is the join: a "
            "review fact still carries the label the rule selects on, so the "
            "continuous and final phases both contribute to the same series."
        ),
    ),
    RecordingRule(
        name="cost_obs:promotion_lifecycle:total",
        expr=f"sum({PROMOTION_METRIC})",
        doc="Total promotion (merge) lifecycle events.",
    ),
    RecordingRule(
        name="cost_obs:settlement_reconciliation:ratio",
        expr=(
            f'sum({SETTLEMENT_METRIC}{{status="settled"}}) by (order_id) '
            f"/ on(order_id) sum({LAUNCH_METRIC}) by (order_id)"
        ),
        doc=(
            "The exact-once reconciliation assertion: for each settled order, "
            "settlements divided by launches must be exactly 1. The "
            "`on(order_id)` vector match correlates a launch to its DD "
            "settlement by the stable order identity, so a replayed launch or "
            "settlement that double-counted would push this to 2 and the "
            "assertion would fail."
        ),
    ),
)

RULE_BY_NAME = {rule.name: rule for rule in RECORDING_RULES}


__all__ = [
    "COST_METRIC",
    "LAUNCH_METRIC",
    "PRESENCE_METRIC",
    "PROMOTION_METRIC",
    "RECORDING_RULES",
    "REVIEW_METRIC",
    "RULE_BY_NAME",
    "RULE_GROUP",
    "SETTLEMENT_METRIC",
    "RecordingRule",
]

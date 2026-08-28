"""The acceptance scenario shared by the executable fixture and its tests.

The scenario drives one real launch through review, promotion and settlement,
scrapes the exposition file, and evaluates all five `cost-observability`
recording rules against the scraped bytes. Keeping it here -- importable --
means the executable (`scripts/cost_obs_acceptance.py`) and the pytest
acceptance test assert the exact same facts instead of two near-copies.
"""

from __future__ import annotations

from pathlib import Path

from fleet_graph.cost_obs import RECORDING_RULES, CostDataPlane, query
from fleet_graph.cost_obs.exposition import parse
from fleet_graph.cost_obs.rules import COST_METRIC, PRESENCE_METRIC

LAUNCH_ORDER = "order-1"
ORPHAN_ORDER = "order-2"

#: The total token spend the scenario emits: 10+20+30+5+5 known plus 7 unknown.
EXPECTED_TOTAL = 77.0
EXPECTED_MANAGEMENT = 10.0


def run_acceptance_scenario(exposition_dir: Path) -> dict[str, object]:
    """Drive launch -> review -> promotion -> settlement, scrape, and query.

    Returns a plain dict of named results so both the script and pytest can
    assert on the same facts without re-implementing the scenario.
    """
    plane = CostDataPlane(exposition_dir=exposition_dir)

    # A real launch correlated to its settlement by the stable order identity,
    # through the full review and promotion lifecycle.
    plane.record_launch(
        order_id=LAUNCH_ORDER,
        development_id="dev-fg-6e4f9345b320",
        generation=1,
        seat="opencode-gpt-terra",
        model="deepseek-v4-pro",
    )
    plane.record_review(order_id=LAUNCH_ORDER, phase="continuous", verdict="approve")
    plane.record_review(order_id=LAUNCH_ORDER, phase="final", verdict="approve")
    plane.record_promotion(order_id=LAUNCH_ORDER, target_ref="refs/heads/main")
    plane.record_settlement(order_id=LAUNCH_ORDER)

    # Token spend: known lifecycle classes plus a batch nothing attributes.
    plane.record_execution_cost(attribution="management", tokens=10, event_id="run-1")
    plane.record_execution_cost(attribution="launch", tokens=20, event_id="run-1")
    plane.record_execution_cost(attribution="review", tokens=30, event_id="run-1")
    plane.record_execution_cost(attribution="promotion", tokens=5, event_id="run-1")
    plane.record_execution_cost(attribution="settlement", tokens=5, event_id="run-1")
    plane.record_unknown_cost(tokens=7, event_id="run-1")

    # A second order that launched but whose later lifecycle producers stayed
    # silent: absent source data, accounted as 0 and distinct from unknown.
    plane.record_launch(order_id=ORPHAN_ORDER, development_id="dev-fg-6e4f9345b320")
    plane.mark_absent(order_id=ORPHAN_ORDER, lifecycle="review")
    plane.mark_absent(order_id=ORPHAN_ORDER, lifecycle="promotion")
    plane.mark_absent(order_id=ORPHAN_ORDER, lifecycle="settlement")

    # Scrape wiring: render to a file, then read it back and query the bytes.
    exposition_path = plane.write_exposition()
    scraped = parse(exposition_path.read_text(encoding="utf-8"))

    per_rule: dict[str, bool] = {}
    for rule in RECORDING_RULES:
        per_rule[rule.name] = len(query(rule.expr, scraped)) > 0

    management_ratio = query(RECORDING_RULES[0].expr, scraped)
    unknown = query(f'{COST_METRIC}{{attribution="unknown"}}', scraped)
    missing_present = query(
        f'{PRESENCE_METRIC}{{order_id="{ORPHAN_ORDER}",lifecycle="settlement"}}', scraped
    )
    present_served = query(
        f'{PRESENCE_METRIC}{{order_id="{LAUNCH_ORDER}",lifecycle="settlement"}}', scraped
    )
    reconciliation = query(RECORDING_RULES[4].expr, scraped)

    # Exact-once: replay the launch and its settlement; the ratio must stay 1.
    replayed_launch = plane.record_launch(
        order_id=LAUNCH_ORDER, development_id="dev-fg-6e4f9345b320"
    )
    replayed_settlement = plane.record_settlement(order_id=LAUNCH_ORDER)
    report = plane.reconcile()
    rerun_reconciliation = query(RECORDING_RULES[4].expr, plane.samples())

    return {
        "rules_non_empty": all(per_rule.values()),
        "per_rule": per_rule,
        "management_ratio": [s.value for s in management_ratio],
        "unknown_tokens": [s.value for s in unknown],
        "missing_visible": [s.value for s in missing_present],
        "present_visible": [s.value for s in present_served],
        "reconciliation": [s.value for s in reconciliation],
        "reconciled_orders": report.orders,
        "exact_once": report.exact_once,
        "replay_noop": (replayed_launch is False, replayed_settlement is False),
        "rerun_reconciliation": [s.value for s in rerun_reconciliation],
    }


__all__ = [
    "EXPECTED_MANAGEMENT",
    "EXPECTED_TOTAL",
    "LAUNCH_ORDER",
    "ORPHAN_ORDER",
    "run_acceptance_scenario",
]

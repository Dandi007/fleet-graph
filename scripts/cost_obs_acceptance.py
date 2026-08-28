#!/usr/bin/env python3
"""Executable acceptance fixture for the cost-observability data plane.

Drives one real launch plus its DD settlement through `CostDataPlane`, renders
the exposition file, scrapes it back, and then evaluates all five
`cost-observability` recording rules against the scraped bytes. The four
assertions map one-to-one onto the spec's acceptance criteria:

- every one of the five recording-rule queries returns a non-empty result;
- `unknown` token attribution and `missing` source facts are separately
  visible (0-bounded presence series for the absent lifecycle, never folded
  into `unknown`);
- replaying the launch and settlement does not double-count: the exact-once
  reconciliation stays at a launch/settlement ratio of exactly 1 per order.

Exit 0 when every assertion holds, 1 otherwise. Run it directly:

    uv run python scripts/cost_obs_acceptance.py

The scenario itself lives in `fleet_graph.cost_obs.acceptance`, where the
pytest acceptance test imports it from -- one scenario, two drivers.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fleet_graph.cost_obs.acceptance import (
    EXPECTED_MANAGEMENT,
    EXPECTED_TOTAL,
    run_acceptance_scenario,
)


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI flags; the fixture is self-contained
    exposition_dir = Path(tempfile.mkdtemp(prefix="cost-obs-acceptance-"))
    results = run_acceptance_scenario(exposition_dir)

    failures: list[str] = []
    if not results["rules_non_empty"]:
        silent = [n for n, ok in results["per_rule"].items() if not ok]
        failures.append(f"some recording rules emitted no series: {silent}")
    if results["unknown_tokens"] != [7.0]:
        failures.append(f"unknown attribution not visible as 7.0: {results['unknown_tokens']}")
    if results["missing_visible"] != [0.0]:
        failures.append(f"missing source facts not visible as 0.0: {results['missing_visible']}")
    if results["present_visible"] != [1.0]:
        failures.append(f"present source facts not visible as 1.0: {results['present_visible']}")
    if results["management_ratio"] != [EXPECTED_MANAGEMENT / EXPECTED_TOTAL]:
        failures.append(f"management ratio wrong: {results['management_ratio']}")
    if results["reconciliation"] != [1.0]:
        failures.append(f"settlement reconciliation not exactly-once: {results['reconciliation']}")
    if not results["exact_once"]:
        failures.append(f"reconcile() reports a double-count: {results['reconciled_orders']}")
    if results["rerun_reconciliation"] != [1.0]:
        failures.append(f"replay changed the ratio: {results['rerun_reconciliation']}")

    print("cost-observability acceptance:")
    for rule, ok in results["per_rule"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {rule} -> non-empty")
    print("  unknown | missing | present | replay-noop | exact-once:")
    print(
        f"    {results['unknown_tokens']} | {results['missing_visible']} | "
        f"{results['present_visible']} | {results['replay_noop']} | {results['exact_once']}"
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("cost-observability acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

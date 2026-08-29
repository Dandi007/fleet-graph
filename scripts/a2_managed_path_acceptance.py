#!/usr/bin/env python3
"""Executable acceptance fixture for the A2 managed periodic path.

Runs the shared managed-path scenario and prints bounded JSON proving the four
acceptance counters the spec's dd-acceptance block requires:

- ``referenced_note_or_suggestion_count >= 1`` -- at least one referenced
  ``work.note.v1`` finding/progress/suggestion;
- ``work.decision.v1 == 0`` and ``work.decision.v2 == 0`` -- the arbiter emitted
  no decision;
- ``decision_marked_chat == 0`` -- no decision-marked chat.

Exit 0 when every counter holds, 1 otherwise. Run it directly:

    uv run python scripts/a2_managed_path_acceptance.py

The scenario itself lives in ``fleet_graph.arbiter.managed_path``, where the
pytest acceptance test imports it from -- one scenario, two drivers.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fleet_graph.arbiter.managed_path import run_managed_path_scenario


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI flags; the fixture is self-contained
    counters = run_managed_path_scenario()

    referenced = counters["referenced_note_or_suggestion_count"] >= 1
    no_decision_v1 = counters["work.decision.v1"] == 0
    no_decision_v2 = counters["work.decision.v2"] == 0
    no_decision_chat = counters["decision_marked_chat"] == 0
    passed = bool(referenced and no_decision_v1 and no_decision_v2 and no_decision_chat)

    evidence: dict[str, Any] = {
        "acceptance": "a2-managed-path",
        "utc_timestamp": utc_now(),
        "referenced_note_or_suggestion_count": counters["referenced_note_or_suggestion_count"],
        "work.decision.v1": counters["work.decision.v1"],
        "work.decision.v2": counters["work.decision.v2"],
        "decision_marked_chat": counters["decision_marked_chat"],
        "emitted_count": counters["emitted_count"],
        "refused_count": counters["refused_count"],
        "suppressed_count": counters["suppressed_count"],
        "kinds": counters["kinds"],
        "published_kinds": counters["published_kinds"],
        "dry_run": counters["dry_run"],
        "pass": passed,
    }
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

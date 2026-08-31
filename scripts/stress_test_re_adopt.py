#!/usr/bin/env python3
"""Stress the re-adopt liveness race test (dev-fg-312338c8e635).

The spec's mechanical completion criterion for the deflake: the
liveness-race test must survive a >=50-iteration loop all green. The
pre-deflake version raced a real fake-agent-run subprocess's exit against
AgentRunLauncher.poll() and flaked under load (`assert 'running' ==
'succeeded'`); the rewritten test constructs the "result lands during the
liveness check" timing via monkeypatched find_result + a dead pid, with no
wall-clock dependency, so any red here is a regression, not a dice roll.

Runs in-process via pytest on the single test of interest. Exit 0 only when
every iteration is green.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = (
    "tests/test_re_adopt.py::TestFailureModes"
    "::test_result_landing_during_the_liveness_check_is_not_called_lost"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=50, help="iterations (default 50)")
    args = parser.parse_args()

    for i in range(1, args.iters + 1):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", TARGET, "-q", "-x"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"iteration {i}/{args.iters} FAILED (exit {proc.returncode})")
            print(proc.stdout[-2000:] if proc.stdout else proc.stderr[-2000:])
            return proc.returncode

    print(f"{args.iters}/{args.iters} iterations green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

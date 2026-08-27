#!/usr/bin/env python3
"""A slow agent-run standing in for an in-flight *coordinator* turn.

Unlike fake_agent_run.py this one (a) blocks until a `release` file appears in
its session root, so a test can hold it "in flight" across a kill-restart of
the line process for as long as the assertions need, and (b) answers with a
structured coordinator verdict, so the resumed line can carry the round to a
real terminal instead of faulting on a shapeless envelope.

It still writes the dispatch ledger: one line per exec is the whole
kill-restart contract ("the fake process was not spawned twice").
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            break
        if token.startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[token] = argv[i + 1]
                i += 2
                continue
            opts[token] = "true"
        i += 1

    session_root = Path(opts["--session-root"])
    run_id = opts["--run-id"]

    with (session_root / "dispatch.log").open("a") as ledger:
        ledger.write(f"{run_id} pid={os.getpid()}\n")

    release = session_root / "release"
    deadline = time.monotonic() + 120  # backstop so a broken test cannot leak us
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    run_dir = session_root / f"run-{run_id[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "state": "succeeded",
        "exit_code": 0,
        "exit_reason": "normal",
        "run_dir": str(run_dir),
        "run_id": run_id,
        "structured_result": {"verdict": "done", "reason": "kill-restart contract test"},
    }
    tmp = run_dir / "result.json.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.replace(run_dir / "result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

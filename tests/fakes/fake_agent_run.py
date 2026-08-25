#!/usr/bin/env python3
"""A stand-in for bin/agent-run that is honest about the parts we depend on.

It records every invocation (so a test can prove we dispatched exactly once),
creates a run directory under --session-root the way the real CLI does, and
writes result.json when it finishes. Sleep duration and exit state come from
the prompt so a test can steer them.
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
    prompt = ""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            prompt = argv[i + 1] if i + 1 < len(argv) else ""
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

    # Dispatch ledger: one line per exec. The re-adopt contract is "exactly one".
    with (session_root / "dispatch.log").open("a") as ledger:
        ledger.write(f"{run_id} pid={os.getpid()}\n")

    directives = dict(part.split("=", 1) for part in prompt.split() if "=" in part)
    sleep_s = float(directives.get("sleep", "0"))
    exit_code = int(directives.get("exit", "0"))

    run_dir = session_root / f"2026-08-26-02-30-00-000-{run_id[:6]}"
    (run_dir / "workdir").mkdir(parents=True, exist_ok=True)

    if sleep_s:
        time.sleep(sleep_s)

    result = {
        "state": "succeeded" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "exit_reason": "normal" if exit_code == 0 else "error",
        "runtime": opts.get("--runtime"),
        "route": opts.get("--route"),
        "run_dir": str(run_dir),
        "run_id": run_id,
        "stdout": f"fake run for {run_id}",
        "duration_seconds": sleep_s,
    }
    # Write atomically: a half-written result.json read by a poller would be
    # indistinguishable from a corrupt run.
    tmp = run_dir / "result.json.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.replace(run_dir / "result.json")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

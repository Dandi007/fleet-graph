#!/usr/bin/env python3
"""A stand-in for `agent-run --role supervisor_auditor`.

Same contract as fake_agent_run.py (dispatch ledger, run dir, result.json),
plus a structured_result shaped like supervisor-audit.result.v1. Behaviour is
steered by FAKE_AUDIT_BEHAVIOR so a test can ask for a verdict, a malformed
answer, a failure, or a long sleep (the kill-restart re-adopt drill).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def structured(behavior: str) -> dict | None:
    if behavior == "malformed":
        return {"status": "completed"}  # no verdict object at all
    recommendation = {"reject": "reject", "approve": "approve"}.get(behavior, "hold")
    return {
        "status": "completed",
        "verdict": {
            "recommendation": recommendation,
            "summary": f"fake audit: {recommendation}",
            "evidence": [
                {
                    "claim": "terminal record read",
                    "command": "cat terminal.json",
                    "output_excerpt": '{"terminal": "fault"}',
                }
            ],
        },
    }


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
    behavior = os.environ.get("FAKE_AUDIT_BEHAVIOR", "hold")

    with (session_root / "dispatch.log").open("a") as ledger:
        ledger.write(f"{run_id} pid={os.getpid()} behavior={behavior}\n")

    if behavior == "sleep":
        time.sleep(float(os.environ.get("FAKE_AUDIT_SLEEP", "5")))
        behavior = "hold"

    run_dir = session_root / f"2026-08-27-00-00-00-000-{run_id[:6]}"
    (run_dir / "workdir").mkdir(parents=True, exist_ok=True)

    exit_code = 1 if behavior == "fail" else 0
    result = {
        "state": "succeeded" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "exit_reason": "normal" if exit_code == 0 else "error",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "role": opts.get("--role"),
        "input": opts.get("--input"),
        "write_flag_present": "--write" in argv,
    }
    if exit_code == 0:
        result["structured_result"] = structured(behavior)

    tmp = run_dir / "result.json.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.replace(run_dir / "result.json")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

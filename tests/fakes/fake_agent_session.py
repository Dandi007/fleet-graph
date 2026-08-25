#!/usr/bin/env python3
"""Stand-in for bin/agent-session.

Honest about the parts the seat depends on: one JSON line per invocation, a
sessions/<id>/session.json carrying daemon_pid, and a start ledger so a test
can prove a seat was started exactly once.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def parse(argv: list[str]) -> tuple[str, dict[str, str], list[str]]:
    subcommand = argv[0] if argv and not argv[0].startswith("--") else ""
    opts: dict[str, str] = {}
    labels: list[str] = []
    i = 1 if subcommand else 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            if token == "--label":
                labels.append(argv[i + 1])
            else:
                opts[token] = argv[i + 1]
            i += 2
            continue
        if token.startswith("--"):
            opts[token] = "true"
        i += 1
    return subcommand, opts, labels


def main() -> int:
    subcommand, opts, labels = parse(sys.argv[1:])
    root = Path(opts["--session-root"])

    if subcommand == "start":
        with (root / "start.log").open("a") as ledger:
            ledger.write(f"{opts.get('--agent')}\n")
        session_id = os.environ.get("FAKE_SESSION_ID", "sess-fake-0001")
        session_dir = root / "sessions" / session_id
        (session_dir / "workdir").mkdir(parents=True, exist_ok=True)
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "session_dir": str(session_dir),
                    "agent": opts.get("--agent"),
                    "labels": labels,
                    # This process exits immediately, so point at a pid that is
                    # alive for the duration of the test instead.
                    "daemon_pid": int(os.environ.get("FAKE_DAEMON_PID", os.getpid())),
                }
            )
        )
        print(json.dumps({"ok": True, "session_id": session_id}))
        return 0

    if subcommand == "send":
        prompt = sys.stdin.read()
        if os.environ.get("FAKE_SEND_FAILS"):
            print(
                json.dumps(
                    {"ok": False, "error": {"code": "TURN_FAILED", "message": "model said no"}}
                )
            )
            return 1
        # Noise before the envelope: some runtimes log to stdout.
        print("runtime chatter that is not json")
        print(json.dumps({"ok": True, "session_id": opts.get("--session"), "text": prompt.strip()}))
        return 0

    if subcommand in {"status", "stop"}:
        print(json.dumps({"ok": True, "session_id": opts.get("--session"), "state": subcommand}))
        return 0

    print(json.dumps({"ok": False, "error": {"code": "BAD_USAGE", "message": subcommand}}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

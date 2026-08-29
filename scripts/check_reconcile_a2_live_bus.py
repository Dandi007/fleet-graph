#!/usr/bin/env python3
"""Live Agent Bus acceptance probe for the A2 reconcile path.

Read-only probe against the actual configured/running Agent Bus HTTP API (no
mocks, no in-memory substitute): it performs a real ``GET /v1/agents/whoami``
and the real alias read ``POST /v1/aliases/<alias>/resolve`` for the
``arbiter`` alias, then verifies fail-closed that the alias response's
authoritative ``current_agent_id`` equals ``--expected-agent-id`` and that the
derived inbox ``agent:<current_agent_id>`` equals
``--expected-inbox-channel``. On success it prints the exact semantic fields
``reconcile_state=ok``, ``agent_id=arbiter``, ``inbox_channel=agent:arbiter``
and exits 0; any missing / malformed / mismatched / ambiguous / unavailable
identity data exits non-zero. It sends no decision and performs no bus write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fleet_graph.arbiter.reconcile import (
    ARBITER_ALIAS,
    BusPrincipalBindingProbe,
    ReconciliationError,
    inbox_for,
)
from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient

WHOAMI_ENDPOINT = "GET /v1/agents/whoami"
RESOLVE_ENDPOINT = "POST /v1/aliases/<alias>/resolve"


def check_reconcile(
    *,
    whoami_agent_id: str | None,
    current_agent_id: str | None,
    expected_agent_id: str,
    expected_inbox_channel: str,
    alias: str = ARBITER_ALIAS,
) -> tuple[bool, dict[str, Any]]:
    """The probe verdict over the two read facts. Pure, testable, fail-closed.

    The alias response's ``current_agent_id`` is the authoritative identity.
    Returns ``(ok, verdict)`` where the verdict carries the semantic fields
    ``reconcile_state`` / ``agent_id`` / ``inbox_channel``.
    """
    failures: list[str] = []
    if not (whoami_agent_id or "").strip():
        failures.append("whoami returned no usable agent id (caller identity missing)")
    resolved = (current_agent_id or "").strip()
    if not resolved:
        failures.append("alias resolve returned no usable current_agent_id (binding missing)")
    elif resolved != expected_agent_id:
        failures.append(f"alias resolves to {resolved!r}, expected agent {expected_agent_id!r}")
    inbox = inbox_for(resolved) if resolved else ""
    if inbox and inbox != expected_inbox_channel:
        failures.append(f"derived inbox {inbox!r}, expected {expected_inbox_channel!r}")
    ok = not failures
    return ok, {
        "reconcile_state": "ok" if ok else "failed",
        "agent_id": resolved,
        "inbox_channel": inbox,
        "whoami_agent_id": (whoami_agent_id or "").strip(),
        "alias": alias,
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-agent-id", default=ARBITER_ALIAS)
    parser.add_argument("--expected-inbox-channel", default="agent:arbiter")
    parser.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    parser.add_argument(
        "--bus-token-file",
        default=os.environ.get("FLEET_GRAPH_BUS_TOKEN_FILE"),
        help="read credential for the probe (default $FLEET_GRAPH_BUS_TOKEN_FILE)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.bus_token_file:
        token: str | None = Path(args.bus_token_file).read_text().strip()
    elif os.environ.get("FLEET_GRAPH_BUS_TOKEN"):
        token = os.environ["FLEET_GRAPH_BUS_TOKEN"].strip()
    else:
        token = None  # BusClient falls back to FLEET_GRAPH_BUS_TOKEN[_FILE]
    client = BusClient(base_url=args.bus_url, token=token)
    probe = BusPrincipalBindingProbe(client)

    try:
        whoami_agent_id = probe.whoami()
        current_agent_id = probe.alias_agent_id(ARBITER_ALIAS)
    except ReconciliationError as exc:
        ok, verdict = (
            False,
            {
                "reconcile_state": "failed",
                "agent_id": "",
                "inbox_channel": "",
                "whoami_agent_id": "",
                "alias": ARBITER_ALIAS,
                "failures": [exc.detail],
            },
        )
    else:
        ok, verdict = check_reconcile(
            whoami_agent_id=whoami_agent_id,
            current_agent_id=current_agent_id,
            expected_agent_id=args.expected_agent_id,
            expected_inbox_channel=args.expected_inbox_channel,
        )

    evidence: dict[str, Any] = {
        "acceptance": "reconcile-a2-live-bus",
        "utc_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoints": [WHOAMI_ENDPOINT, RESOLVE_ENDPOINT.replace("<alias>", ARBITER_ALIAS)],
        "expected_agent_id": args.expected_agent_id,
        "expected_inbox_channel": args.expected_inbox_channel,
        **verdict,
        "pass": ok,
    }
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    if ok:
        print(
            f"reconcile_state=ok agent_id={verdict['agent_id']} "
            f"inbox_channel={verdict['inbox_channel']}"
        )
        return 0
    for failure in verdict["failures"]:
        print(f"reconcile_state=failed: {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

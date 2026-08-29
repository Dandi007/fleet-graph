#!/usr/bin/env python3
"""Live A2 reconciliation acceptance: read the real bus, never write to it.

This probe drives the *real* agent-bus HTTP API through ``BusClient`` and the
fail-closed ``reconcile_arbiter_identity`` reconciliation. It uses no mock and no
in-memory substitute: the two reads are ``GET /v1/agents/whoami`` (the caller's
own identity) and the alias read surface ``GET /v1/aliases/<alias>``, whose
``current_agent_id`` is the authoritative arbiter identity.

It verifies the arbiter from the alias ``current_agent_id`` -- which must equal
``--expected-agent-id`` -- and derives the inbox channel as
``agent:<current_agent_id>``. On success it prints the three semantic fields and
exits 0; on any missing / malformed / mismatched / unavailable / ambiguous
identity or an unexpected inbox channel it prints a failure and exits non-zero.

The probe performs no bus write: no publish, no decision, no alias mutation.
Only the two read endpoints are touched.
"""

from __future__ import annotations

import argparse
import os
import sys

from fleet_graph.arbiter.reconcile import (
    DEFAULT_ARBITER_ALIAS,
    ArbiterReconcileError,
    reconcile_arbiter_identity,
)
from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient, BusError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bus-url",
        default=os.environ.get("FLEET_GRAPH_BUS_URL", DEFAULT_BUS_URL),
        help="agent-bus gateway base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--alias",
        default=DEFAULT_ARBITER_ALIAS,
        help="arbiter alias to resolve (default: %(default)s)",
    )
    parser.add_argument("--expected-agent-id", required=True, help="e.g. arbiter")
    parser.add_argument("--expected-inbox-channel", required=True, help="e.g. agent:arbiter")
    args = parser.parse_args(argv)

    try:
        client = BusClient(base_url=args.bus_url)
        identity = reconcile_arbiter_identity(
            client, alias=args.alias, expected_agent_id=args.expected_agent_id
        )
    except BusError as exc:
        print("reconcile_state=unavailable", file=sys.stderr)
        print(f"reason={exc}", file=sys.stderr)
        return 1
    except ArbiterReconcileError as exc:
        print("reconcile_state=failed", file=sys.stderr)
        print(f"reason={exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print("reconcile_state=failed", file=sys.stderr)
        print(f"reason={exc}", file=sys.stderr)
        return 1

    if (
        identity.agent_id != args.expected_agent_id
        or identity.inbox_channel != args.expected_inbox_channel
    ):
        print("reconcile_state=mismatch", file=sys.stderr)
        print(f"agent_id={identity.agent_id}", file=sys.stderr)
        print(f"inbox_channel={identity.inbox_channel}", file=sys.stderr)
        return 1

    print(f"reconcile_state={identity.reconcile_state}")
    print(f"agent_id={identity.agent_id}")
    print(f"inbox_channel={identity.inbox_channel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

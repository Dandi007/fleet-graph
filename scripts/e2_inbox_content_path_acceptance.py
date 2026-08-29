#!/usr/bin/env python3
"""E2 inbox content path acceptance (E1 gap #4): alias pass-through + credential
convergence + fail-open degradation.

Three scenarios:

- ``alias-passthrough-drain-receives-message`` (hermetic): a ``LaunchSpec``
  with an alias emits ``--alias``; a ``LineConfig`` carrying the alias builds a
  real ``Inbox`` through the production wiring; the coordinator drain receives
  and persists a controlled message over a faithful fake ACL transport.
- ``service-token-403-asserted`` (hermetic): the real channel ACL is modelled
  (service-token auth on ``agent:*`` -> 403, line-token -> 200); the fixed path
  authenticates with the line's own token and never 403s, and the service-token
  403 is asserted as the pre-fix failure mode.
- ``real-bus-inbox-roundtrip`` (real bus): publish a controlled message with the
  line token to a synthetic drill alias, build a ``LineConfig`` carrying
  ``--alias``, drain through ``build_line``'s real ``Inbox``, and verify the
  message arrives (``inbox_messages`` contains the sent ``message_id``) with
  head_seq before/after.

Evidence is one JSON object per scenario on stdout; the process exits non-zero
when the scenario does not pass. The token arrives via ``--line-token-file``
for the real-bus scenario and is never hard-coded.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fleet_graph.bus.client import BusClient
from fleet_graph.bus.inbox import Inbox, InboxForbidden
from fleet_graph.bus.tokens import LINE_TOKEN_PATH_ENV
from fleet_graph.graphs.runner import LineConfig, build_line
from fleet_graph.scheduler.launcher import LaunchSpec


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def evidence(scenario: str, passed: bool, **facts: Any) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "utc_timestamp": utc_now(),
        "pass": passed,
        "exit_code": 0 if passed else 1,
        **facts,
    }


class FakeAclTransport:
    """Faithful to the real channel ACL on ``agent:{alias}``.

    Owner-only readable channel: the line's own token is 200, the fleet-graph
    service token is a structural 403. Every request's bearer token is recorded.
    """

    def __init__(self, line_token: str, delivery: dict[str, Any] | None = None) -> None:
        self.line_token = line_token
        self.delivery = delivery
        self.seen_tokens: list[str] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        token = str(headers.get("Authorization", "")).removeprefix("Bearer ")
        self.seen_tokens.append(token)
        if token != self.line_token:
            return 403, {"code": "FORBIDDEN", "message": "no read ACL on agent:*"}
        if url.endswith("/consume"):
            return 200, {"deliveries": [self.delivery] if self.delivery else []}
        if url.endswith("/ack"):
            return 200, {}
        return 404, "not found"


def controlled_delivery(message_id: str) -> dict[str, Any]:
    return {
        "delivery_id": f"del-{message_id}",
        "lease_token": f"lease-{message_id}",
        "attempt": 0,
        "message": {
            "message_id": message_id,
            "sender_agent_id": "drill-agent",
            "created_at": "2026-08-29T10:00:00.000Z",
            "payload": {
                "body": "a controlled message",
                "depth": 1,
                "from_alias": "drill",
                "from_agent_id": "drill-agent",
                "thread_id": "t-1",
                "sent_at": "2026-08-29T10:00:00Z",
            },
        },
    }


def _stage_line_token(work_dir: Path, alias: str, token: str) -> None:
    secrets = work_dir / "secrets"
    (secrets / f"{alias}.token").parent.mkdir(parents=True, exist_ok=True)
    (secrets / f"{alias}.token").write_text(token + "\n", encoding="utf-8")
    os.environ[LINE_TOKEN_PATH_ENV] = str(secrets / "{alias}.token")


def _patch_runner_bus(transport: FakeAclTransport, built_with: list[str]):
    import fleet_graph.graphs.runner as runner

    original = runner.BusClient
    runner.BusClient = lambda token: (
        built_with.append(token) or BusClient(token=token, transport=transport)
    )
    return original


# --- hermetic scenarios -------------------------------------------------------


def scenario_alias_passthrough_drain_receives_message(work_dir: Path) -> dict[str, Any]:
    alias = "drill-hermetic"
    _stage_line_token(work_dir, alias, "line-token-abc")

    spec = LaunchSpec(folder_id="wf-1", seat="s", alias=alias)
    has_alias = "--alias" in spec.argv()
    argv_alias_ok = has_alias and spec.argv()[spec.argv().index("--alias") + 1] == alias

    transport = FakeAclTransport("line-token-abc", delivery=controlled_delivery("msg-hermetic-1"))
    original = _patch_runner_bus(transport, built_with=[])
    try:
        _, deps = build_line(
            LineConfig(folder_id="wf-1", seat="s", run_root=work_dir / "runs", alias=alias)
        )
    finally:
        import fleet_graph.graphs.runner as runner

        runner.BusClient = original

    real_inbox = isinstance(deps.inbox, Inbox)
    drained: list[Any] = []
    deps.inbox.drain_then_ack(drained.extend)
    received = any(m["message_id"] == "msg-hermetic-1" for m in drained)

    passed = bool(argv_alias_ok and real_inbox and drained and received)
    return evidence(
        "alias-passthrough-drain-receives-message",
        passed,
        argv_alias_present=argv_alias_ok,
        real_inbox=real_inbox,
        drained_count=len(drained),
        received_message="msg-hermetic-1" if received else None,
    )


def scenario_service_token_403_asserted(work_dir: Path) -> dict[str, Any]:
    alias = "drill-acl"
    _stage_line_token(work_dir, alias, "line-token-abc")
    os.environ["FLEET_GRAPH_BUS_TOKEN"] = "service-token-xyz"

    transport = FakeAclTransport("line-token-abc", delivery=controlled_delivery("msg-acl-1"))
    built_with: list[str] = []
    original = _patch_runner_bus(transport, built_with)
    try:
        _, deps = build_line(
            LineConfig(folder_id="wf-1", seat="s", run_root=work_dir / "runs", alias=alias)
        )
    finally:
        import fleet_graph.graphs.runner as runner

        runner.BusClient = original

    drained: list[Any] = []
    deps.inbox.drain_then_ack(drained.extend)
    fixed_path_ok = bool(
        drained
        and built_with == ["line-token-abc"]
        and set(transport.seen_tokens) == {"line-token-abc"}
    )

    # The pre-fix failure mode, asserted explicitly: the service token is
    # structurally 403'd on agent:* -- the very ACL the fixed path never hits.
    service_403 = False
    try:
        Inbox(
            BusClient(
                token="service-token-xyz",
                transport=FakeAclTransport("line-token-abc", delivery=controlled_delivery("msg-x")),
            ),
            alias,
        ).drain_then_ack(lambda messages: None)
    except InboxForbidden:
        service_403 = True

    passed = bool(fixed_path_ok and service_403)
    return evidence(
        "service-token-403-asserted",
        passed,
        inbox_client_credential=built_with[0] if built_with else None,
        service_token_403=service_403,
        drained_count=len(drained),
    )


# --- real-bus scenario --------------------------------------------------------


def scenario_real_bus_inbox_roundtrip(
    work_dir: Path, bus_url: str, line_token_file: str, alias: str | None
) -> dict[str, Any]:
    token = Path(line_token_file).read_text().strip()
    alias = alias or Path(line_token_file).stem
    _stage_line_token(work_dir, alias, token)

    client = BusClient(base_url=bus_url, token=token)
    channel = f"agent:{alias}"
    _before, head_before = client.messages(channel, limit=1)

    payload = {
        "body": "e2 gap4 inbox content path controlled roundtrip message",
        "depth": 0,
        "from_agent_id": "e2-gap4-drill",
        "from_alias": "e2-gap4-drill",
        "sent_at": utc_now(),
        "thread_id": f"e2-gap4-roundtrip-{alias}",
    }
    published = client.publish(
        channel,
        "agent.msg.v1",
        payload,
        idempotency_key=f"e2-gap4-roundtrip-{uuid.uuid4().hex}",
    )

    _, deps = build_line(
        LineConfig(folder_id="wf-e2gap4", seat="s", run_root=work_dir / "runs", alias=alias)
    )
    real_inbox = isinstance(deps.inbox, Inbox)
    drained: list[Any] = []
    _drain, ack_outcomes = deps.inbox.drain_then_ack(drained.extend)
    _after, head_after = client.messages(channel, limit=1)
    received = any(m["message_id"] == published.message_id for m in drained)

    passed = bool(real_inbox and drained and received and head_after >= head_before)
    return evidence(
        "real-bus-inbox-roundtrip",
        passed,
        bus_url=bus_url,
        drill_alias=alias,
        sent_message_id=published.message_id,
        head_seq_before=head_before,
        head_seq_after=head_after,
        real_inbox=real_inbox,
        drain_count=len(drained),
        received=received,
        ack_outcomes=ack_outcomes,
    )


# --- cli ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=[
            "alias-passthrough-drain-receives-message",
            "service-token-403-asserted",
            "real-bus-inbox-roundtrip",
        ],
    )
    parser.add_argument("--work-dir", default=None, help="scratch dir (default: a fresh temp dir)")
    parser.add_argument("--bus-url", default=None, help="real agent-bus URL (real-bus scenario)")
    parser.add_argument(
        "--line-token-file",
        default=None,
        help="the line's own bus token file (real-bus scenario; never hard-coded)",
    )
    parser.add_argument(
        "--alias",
        default=None,
        help="drill alias (defaults to the --line-token-file stem for the real-bus scenario)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="e2-gap4-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.scenario == "alias-passthrough-drain-receives-message":
        result = scenario_alias_passthrough_drain_receives_message(work_dir)
    elif args.scenario == "service-token-403-asserted":
        result = scenario_service_token_403_asserted(work_dir)
    else:
        if not args.line_token_file or not Path(args.line_token_file).exists():
            raise SystemExit("real-bus-inbox-roundtrip needs --line-token-file (a readable token)")
        result = scenario_real_bus_inbox_roundtrip(
            work_dir,
            args.bus_url or "http://127.0.0.1:7490",
            args.line_token_file,
            args.alias,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())

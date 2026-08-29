"""A2 identity reconciliation: fail-closed arbiter identity and inbox derivation.

The reconciler is driven through the real ``BusClient`` against a recording fake
transport, so the shapes asserted here are the shapes the live bus returns, and
the read-only discipline (no POST, no publish) is checked on the wire, not merely
by review:

- success: a live whoami + a consistent alias round-trip to ``agent:arbiter``,
  and the transport saw exactly two GETs and zero writes;
- fail-closed identity/error cases: missing, malformed, mismatched, unavailable
  and ambiguous identity data each refuse, and no inbox channel is derived;
- the arbiter package conformance guard still covers this module (it is scanned
  as ``arbiter/*.py``) -- see tests/test_arbiter.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.arbiter.reconcile import (
    ArbiterIdentity,
    ArbiterReconcileError,
    reconcile_arbiter_identity,
)
from fleet_graph.bus.client import BusClient, BusError


class RecordingTransport:
    """Replays canned responses and records every call (method + url + body)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.responses: list[tuple[int, Any]] = []

    def queue(self, status: int, body: Any) -> None:
        self.responses.append((status, body))

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        self.calls.append((method, url, json_body))
        if self.responses:
            return self.responses.pop(0)
        return (200, {})

    @property
    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]

    @property
    def urls(self) -> list[str]:
        return [url for _, url, _ in self.calls]

    @property
    def bodies(self) -> list[Any]:
        return [body for _, _, body in self.calls]


def make_client(transport: RecordingTransport) -> BusClient:
    return BusClient(token="tok", transport=transport)


def whoami_ok(agent_id: str = "arbiter") -> dict[str, Any]:
    return {"agent_id": agent_id, "kind": "agent", "is_admin": False}


def alias_ok(current_agent_id: str = "arbiter") -> dict[str, Any]:
    """The real dual shape: flat fields for legacy readers plus a nested alias."""
    return {
        "alias": {"alias": "arbiter", "kind": "named", "current_agent_id": current_agent_id},
        "kind": "named",
        "current_agent_id": current_agent_id,
        "wake_policy": "none",
        "delivery_mode": "push",
    }


def test_success_round_trips_identity_and_derives_inbox() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(200, alias_ok())
    client = make_client(transport)

    identity = reconcile_arbiter_identity(client)

    assert identity == ArbiterIdentity(
        reconcile_state="ok", agent_id="arbiter", inbox_channel="agent:arbiter"
    )
    assert identity.as_dict() == {
        "reconcile_state": "ok",
        "agent_id": "arbiter",
        "inbox_channel": "agent:arbiter",
    }


def test_success_makes_only_read_calls_and_no_write() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(200, alias_ok())
    client = make_client(transport)

    reconcile_arbiter_identity(client)

    assert transport.methods == ["GET", "GET"]
    assert transport.urls == [
        "http://127.0.0.1:7490/v1/agents/whoami",
        "http://127.0.0.1:7490/v1/aliases/arbiter",
    ]
    assert all(body is None for body in transport.bodies)


def test_whoami_missing_agent_id_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, {"kind": "agent"})
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="whoami"):
        reconcile_arbiter_identity(client)


def test_whoami_malformed_agent_id_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, {"agent_id": 7})
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="whoami"):
        reconcile_arbiter_identity(client)


def test_whoami_non_object_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, ["arbiter"])
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="not an object"):
        reconcile_arbiter_identity(client)


def test_alias_missing_current_agent_id_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(200, {"alias": {"alias": "arbiter", "kind": "named"}})
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="no current_agent_id"):
        reconcile_arbiter_identity(client)


def test_alias_malformed_current_agent_id_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(200, {"current_agent_id": 7, "alias": {"alias": "arbiter"}})
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="malformed"):
        reconcile_arbiter_identity(client)


def test_alias_mismatched_identity_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(200, alias_ok(current_agent_id="someone-else"))
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match=r"mismatched|expected"):
        reconcile_arbiter_identity(client)


def test_alias_ambiguous_identity_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(
        200,
        {
            "alias": {"alias": "arbiter", "current_agent_id": "arbiter"},
            "current_agent_id": "someone-else",
        },
    )
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="ambiguous"):
        reconcile_arbiter_identity(client)


def test_alias_response_non_object_refuses_closed() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(200, ["arbiter"])
    client = make_client(transport)

    with pytest.raises(ArbiterReconcileError, match="not an object"):
        reconcile_arbiter_identity(client)


def test_unavailable_whoami_is_a_bus_error() -> None:
    transport = RecordingTransport()
    transport.queue(500, {"code": "BOOM"})
    client = make_client(transport)

    with pytest.raises(BusError):
        reconcile_arbiter_identity(client)


def test_unavailable_alias_is_a_bus_error() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok())
    transport.queue(404, {"code": "NOT_FOUND"})
    client = make_client(transport)

    with pytest.raises(BusError):
        reconcile_arbiter_identity(client)


def test_custom_expected_agent_id_is_applied_to_the_alias() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_ok("watcher"))
    transport.queue(200, alias_ok(current_agent_id="arbiter"))
    client = make_client(transport)

    identity = reconcile_arbiter_identity(client, expected_agent_id="arbiter")
    assert identity.inbox_channel == "agent:arbiter"

    transport.queue(200, whoami_ok("watcher"))
    transport.queue(200, alias_ok(current_agent_id="arbiter"))
    with pytest.raises(ArbiterReconcileError, match="expected"):
        reconcile_arbiter_identity(client, expected_agent_id="not-arbiter")

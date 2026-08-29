"""Focused coverage for the A2 reconcile path: real-read identity facts.

The reconcile change performs two real read-only gateway calls -- ``GET
/v1/agents/whoami`` (caller identity) and the alias ``resolve`` read
(``POST /v1/aliases/<alias>/resolve``), whose ``current_agent_id`` is the
authoritative identity -- and derives ``agent:<current_agent_id>`` only after
both verify. Pinned here:

- the pure reconcile verdict passes idempotently and refuses closed for every
  missing / mismatched / missing-binding / rebound state, with no fallback or
  guessed identity;
- the production probe hits exactly those two read endpoints (recorded on a
  fake transport carrying the real gateway response shapes read off the running
  bus), degrades unavailable reads to a ``missing_*`` refusal, and treats
  conflicting identity data as ambiguous refusals;
- the live-bus acceptance probe's verdict function (imported from the script)
  prints the exact semantic fields and fails non-zero on any mismatch.

No live Agent Bus is needed: the shapes asserted here were read off the running
bus, so drift shows up as a test failure rather than a surprise in production.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.arbiter.reconcile import (
    ARBITER_ALIAS,
    ARBITER_INBOX,
    DEFAULT_EXPECTED_PRINCIPAL,
    STATE_AMBIGUOUS_BINDING,
    STATE_AMBIGUOUS_IDENTITY,
    STATE_MISMATCHED_PRINCIPAL,
    STATE_MISSING_BINDING,
    STATE_MISSING_PRINCIPAL,
    STATE_OK,
    STATE_REBOUND,
    BusPrincipalBindingProbe,
    ReconciliationError,
    inbox_for,
    reconcile_principal_alias,
)
from fleet_graph.bus.client import BusClient

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_SCRIPT = REPO_ROOT / "scripts" / "check_reconcile_a2_live_bus.py"


class RecordingTransport:
    """Replays canned gateway responses and records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[tuple[int, Any]] = []
        self.default: tuple[int, Any] = (200, {})

    def queue(self, status: int, body: Any) -> None:
        self.responses.append((status, body))

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json_body})
        if self.responses:
            return self.responses.pop(0)
        return self.default


def probe(transport: RecordingTransport) -> BusPrincipalBindingProbe:
    return BusPrincipalBindingProbe(BusClient(token="tok", transport=transport))


def whoami_body(agent_id: str) -> dict[str, Any]:
    """The real ``GET /v1/agents/whoami`` response shape (read off the bus)."""
    return {
        "agent_id": agent_id,
        "kind": "agent",
        "is_admin": False,
        "can_delegate": False,
        "can_register_agents": False,
    }


def resolve_body(current_agent_id: str) -> dict[str, Any]:
    """The real alias ``resolve`` response shape (read off the bus)."""
    return {
        "alias": ARBITER_ALIAS,
        "kind": "named",
        "current_agent_id": current_agent_id,
        "wake_policy": "none",
        "delivery_mode": "push",
        "inbox_channel_id": inbox_for(current_agent_id),
        "agent_active": True,
    }


def _reconcile(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "whoami_agent_id": DEFAULT_EXPECTED_PRINCIPAL,
        "current_agent_id": DEFAULT_EXPECTED_PRINCIPAL,
        "expected_principal": DEFAULT_EXPECTED_PRINCIPAL,
        "alias": ARBITER_ALIAS,
    }
    kwargs.update(overrides)
    return reconcile_principal_alias(**kwargs)


# --- pure verdict: success and every fail-closed state -----------------------


def test_success_verifies_both_facts_and_derives_inbox() -> None:
    verdict = _reconcile()
    assert verdict.state == STATE_OK
    assert verdict.ok is True
    assert verdict.agent_id == DEFAULT_EXPECTED_PRINCIPAL
    assert verdict.inbox_channel == ARBITER_INBOX
    assert verdict.as_dict() == {
        "state": STATE_OK,
        "agent_id": "arbiter",
        "expected_principal": "arbiter",
        "alias": "arbiter",
        "inbox_channel": "agent:arbiter",
    }


def test_missing_whoami_refuses_closed() -> None:
    with pytest.raises(ReconciliationError) as exc:
        _reconcile(whoami_agent_id=None)
    assert exc.value.state == STATE_MISSING_PRINCIPAL
    with pytest.raises(ReconciliationError) as exc:
        _reconcile(whoami_agent_id="   ")
    assert exc.value.state == STATE_MISSING_PRINCIPAL


def test_mismatched_caller_refuses_closed() -> None:
    with pytest.raises(ReconciliationError) as exc:
        _reconcile(whoami_agent_id="fleet-graph")
    assert exc.value.state == STATE_MISMATCHED_PRINCIPAL


def test_missing_binding_refuses_closed() -> None:
    with pytest.raises(ReconciliationError) as exc:
        _reconcile(current_agent_id=None)
    assert exc.value.state == STATE_MISSING_BINDING
    with pytest.raises(ReconciliationError) as exc:
        _reconcile(current_agent_id=" ")
    assert exc.value.state == STATE_MISSING_BINDING


def test_rebound_alias_refuses_closed() -> None:
    with pytest.raises(ReconciliationError) as exc:
        _reconcile(current_agent_id="fleet-graph")
    assert exc.value.state == STATE_REBOUND


def test_refusals_are_pure_and_idempotent() -> None:
    with pytest.raises(ReconciliationError):
        _reconcile(current_agent_id="fleet-graph")
    assert _reconcile().as_dict() == _reconcile().as_dict()


# --- probe: real read surfaces ------------------------------------------------


def test_probe_hits_whoami_get_and_alias_resolve_read() -> None:
    transport = RecordingTransport()
    transport.queue(200, whoami_body("arbiter"))
    transport.queue(200, resolve_body("arbiter"))
    source = probe(transport)
    assert source.whoami() == "arbiter"
    assert source.alias_agent_id(ARBITER_ALIAS) == "arbiter"

    methods = [call["method"] for call in transport.calls]
    urls = [call["url"] for call in transport.calls]
    assert methods == ["GET", "POST"], methods
    assert [url.endswith("/v1/agents/whoami") for url in urls[:1]] == [True]
    assert urls[1].endswith(f"/v1/aliases/{ARBITER_ALIAS}/resolve"), urls


def test_probe_degrades_unavailable_reads_to_none() -> None:
    transport = RecordingTransport()
    transport.queue(500, {"code": "BOOM"})
    transport.queue(404, {"code": "NOT_FOUND"})
    source = probe(transport)
    assert source.whoami() is None
    assert source.alias_agent_id(ARBITER_ALIAS) is None


def test_probe_refuses_malformed_responses_as_missing() -> None:
    transport = RecordingTransport()
    transport.queue(200, {"agent_id": 42})
    transport.queue(200, {"alias": ARBITER_ALIAS, "current_agent_id": None})
    source = probe(transport)
    assert source.whoami() is None
    assert source.alias_agent_id(ARBITER_ALIAS) is None


def test_probe_refuses_ambiguous_whoami_identity() -> None:
    transport = RecordingTransport()
    transport.queue(200, {"agent_id": "arbiter", "current_agent_id": "fleet-graph"})
    source = probe(transport)
    with pytest.raises(ReconciliationError) as exc:
        source.whoami()
    assert exc.value.state == STATE_AMBIGUOUS_IDENTITY


def test_probe_refuses_ambiguous_alias_binding() -> None:
    transport = RecordingTransport()
    transport.queue(
        200,
        {
            "alias": ARBITER_ALIAS,
            "current_agent_id": "arbiter",
            "agent_id": "fleet-graph",
            "inbox_channel_id": "agent:arbiter",
            "agent_active": True,
        },
    )
    source = probe(transport)
    with pytest.raises(ReconciliationError) as exc:
        source.alias_agent_id(ARBITER_ALIAS)
    assert exc.value.state == STATE_AMBIGUOUS_BINDING


def test_probe_refuses_an_alias_resolving_a_different_alias_name() -> None:
    transport = RecordingTransport()
    transport.queue(200, {"alias": "someone-else", "current_agent_id": "arbiter"})
    source = probe(transport)
    with pytest.raises(ReconciliationError) as exc:
        source.alias_agent_id(ARBITER_ALIAS)
    assert exc.value.state == STATE_AMBIGUOUS_BINDING


# --- live-bus probe verdict function (imported from the script) ---------------


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("check_reconcile_a2_live_bus", PROBE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_check_reconcile_passes_and_names_semantic_fields() -> None:
    module = _load_probe_module()
    ok, verdict = module.check_reconcile(
        whoami_agent_id="arbiter",
        current_agent_id="arbiter",
        expected_agent_id="arbiter",
        expected_inbox_channel="agent:arbiter",
    )
    assert ok is True
    assert verdict["reconcile_state"] == "ok"
    assert verdict["agent_id"] == "arbiter"
    assert verdict["inbox_channel"] == "agent:arbiter"


def test_probe_check_reconcile_fails_on_every_mismatch() -> None:
    module = _load_probe_module()
    ok, verdict = module.check_reconcile(
        whoami_agent_id="fleet-graph",
        current_agent_id="fleet-graph",
        expected_agent_id="arbiter",
        expected_inbox_channel="agent:arbiter",
    )
    assert ok is False
    assert verdict["reconcile_state"] == "failed"
    assert verdict["failures"], "a mismatch must name a failure"

    ok, verdict = module.check_reconcile(
        whoami_agent_id="arbiter",
        current_agent_id="arbiter",
        expected_agent_id="arbiter",
        expected_inbox_channel="agent:someone-else",
    )
    assert ok is False
    assert verdict["inbox_channel"] == "agent:arbiter"

    ok, verdict = module.check_reconcile(
        whoami_agent_id=None,
        current_agent_id=None,
        expected_agent_id="arbiter",
        expected_inbox_channel="agent:arbiter",
    )
    assert ok is False
    assert len(verdict["failures"]) == 2

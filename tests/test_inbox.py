"""Inbox drain, and the ordering that makes redelivery safe."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.bus.client import BusClient
from fleet_graph.bus.inbox import (
    NEAR_DEAD_ATTEMPT_THRESHOLD,
    Delivery,
    Inbox,
    InboxError,
    InboxForbidden,
)
from fleet_graph.state.run_artifacts import write_json_durable


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[tuple[int, Any]] = []

    def queue(self, status: int, body: Any) -> None:
        self.responses.append((status, body))

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        self.calls.append({"method": method, "url": url, "body": json_body})
        return self.responses.pop(0) if self.responses else (200, {})

    def paths(self) -> list[str]:
        return [c["url"].split("127.0.0.1:7490")[-1] for c in self.calls]


def delivery(n: int, attempt: int = 0) -> dict[str, Any]:
    return {
        "delivery_id": f"dl-{n}",
        "lease_token": f"lease-{n}",
        "message_id": f"msg-{n}",
        "kind": "chat.message.v1",
        "payload": {"text": f"hello {n}"},
        "attempt": attempt,
    }


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def inbox(transport: RecordingTransport) -> Inbox:
    client = BusClient(token="tok", transport=transport)
    return Inbox(client, alias="ronin-quotaalert")


class TestConsume:
    def test_targets_the_private_alias_channel(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"deliveries": []})
        inbox.consume()
        assert transport.paths() == ["/v1/channels/agent:ronin-quotaalert/consume"]
        assert transport.calls[0]["body"] == {"max_messages": 10, "lease_ms": 60_000}

    def test_preserves_bus_order(self, inbox: Inbox, transport: RecordingTransport) -> None:
        """FIFO by channel_seq. Never re-sorted, never prioritised by content."""
        transport.queue(200, {"deliveries": [delivery(1), delivery(2), delivery(3)]})
        drain = inbox.consume()
        assert [m["message_id"] for m in drain.messages] == ["msg-1", "msg-2", "msg-3"]

    def test_message_carries_id_for_dedup(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        """Redelivery is expected; message_id is how the coordinator copes."""
        transport.queue(200, {"deliveries": [delivery(1)]})
        assert inbox.consume().messages[0]["message_id"] == "msg-1"

    def test_unackable_delivery_is_skipped(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        broken = delivery(2)
        del broken["lease_token"]
        transport.queue(200, {"deliveries": [delivery(1), broken]})
        assert len(inbox.consume()) == 1

    def test_missing_deliveries_array_is_an_error(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {})
        with pytest.raises(InboxError, match="no deliveries array"):
            inbox.consume()

    def test_403_is_its_own_type(self, inbox: Inbox, transport: RecordingTransport) -> None:
        """Alias rebound: stop the line, do not retry -- someone else owns it now."""
        transport.queue(403, {"code": "FORBIDDEN"})
        with pytest.raises(InboxForbidden, match="rebound"):
            inbox.consume()

    def test_other_errors_propagate(self, inbox: Inbox, transport: RecordingTransport) -> None:
        from fleet_graph.bus.client import BusError

        transport.queue(500, {"code": "BOOM"})
        with pytest.raises(BusError):
            inbox.consume()

    def test_near_dead_deliveries_are_surfaced(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {
                "deliveries": [
                    delivery(1, attempt=0),
                    delivery(2, attempt=NEAR_DEAD_ATTEMPT_THRESHOLD),
                ]
            },
        )
        assert [d.message_id for d in inbox.consume().near_dead] == ["msg-2"]


class TestAck:
    def test_posts_the_lease_token(self, inbox: Inbox, transport: RecordingTransport) -> None:
        transport.queue(200, {})
        outcome = inbox.ack(Delivery("dl-1", "lease-1", "msg-1", "k", {}))
        assert outcome == "acked"
        assert transport.paths() == ["/v1/deliveries/dl-1/ack"]
        assert transport.calls[0]["body"] == {"lease_token": "lease-1"}

    def test_409_is_lease_lost_not_a_failure(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        transport.queue(409, {"code": "LEASE_LOST"})
        assert inbox.ack(Delivery("dl-1", "lease-1", "msg-1", "k", {})) == "lease_lost"

    def test_other_status_is_error(self, inbox: Inbox, transport: RecordingTransport) -> None:
        transport.queue(500, {"code": "BOOM"})
        assert inbox.ack(Delivery("dl-1", "lease-1", "msg-1", "k", {})) == "error"


class TestMustDeliverOrdering:
    """The protocol: persist durably, *then* ack. Never the other way round."""

    def test_persist_runs_before_any_ack(self, inbox: Inbox, transport: RecordingTransport) -> None:
        transport.queue(200, {"deliveries": [delivery(1), delivery(2)]})
        transport.queue(200, {})
        transport.queue(200, {})

        order: list[str] = []
        inbox.drain_then_ack(lambda msgs: order.append(f"persist:{len(msgs)}"))
        order += [p for p in transport.paths() if "/ack" in p]

        assert order[0] == "persist:2"
        assert order[1:] == ["/v1/deliveries/dl-1/ack", "/v1/deliveries/dl-2/ack"]

    def test_a_failed_persist_acks_nothing(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        """Losing the write must leave the messages redeliverable."""
        transport.queue(200, {"deliveries": [delivery(1), delivery(2)]})

        def boom(_msgs: list[dict[str, Any]]) -> None:
            raise OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            inbox.drain_then_ack(boom)

        assert [p for p in transport.paths() if "/ack" in p] == []

    def test_empty_drain_still_persists_an_empty_list(
        self, inbox: Inbox, transport: RecordingTransport
    ) -> None:
        """The coordinator input always carries inbox_messages, [] included."""
        transport.queue(200, {"deliveries": []})
        seen: list[list[dict[str, Any]]] = []
        inbox.drain_then_ack(seen.append)
        assert seen == [[]]


class TestDurableWrite:
    def test_round_trips(self, tmp_path: Path) -> None:
        path = write_json_durable(tmp_path / "coord" / "round-1-input.json", {"a": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}

    def test_creates_missing_parents(self, tmp_path: Path) -> None:
        write_json_durable(tmp_path / "deep" / "nested" / "x.json", {})
        assert (tmp_path / "deep" / "nested" / "x.json").exists()

    def test_fsyncs_before_returning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the fsync, a power loss after ack loses the message on both sides."""
        import fleet_graph.state.run_artifacts as module

        synced: list[int] = []
        monkeypatch.setattr(module.os, "fsync", lambda fd: synced.append(fd))
        write_json_durable(tmp_path / "x.json", {"a": 1})
        assert len(synced) == 1

    def test_non_ascii_is_preserved(self, tmp_path: Path) -> None:
        path = write_json_durable(tmp_path / "x.json", {"reason": "验收通过"})
        assert "验收通过" in path.read_text(encoding="utf-8")

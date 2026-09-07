"""独立验证 alias 解析与同秒消息唤醒。"""

import pytest

from fleet_graph.bus.client import BusClient, BusError
from fleet_graph.bus.inbox import Inbox, InboxForbidden
from fleet_graph.goal.line_message import BusLineMessageSink
from fleet_graph.scheduler.wake import LiveWakeSignals, parse_bus_timestamp
from test_bus import RecordingTransport


def test_inbox_resolves_alias_to_distinct_owner_channel():
    transport = RecordingTransport()
    transport.queue(200, {"current_agent_id": "owner-uuid", "inbox_channel_id": "agent:owner-uuid"})
    transport.queue(200, {"deliveries": []})
    client = BusClient(token="test", agent_id="service", transport=transport)
    assert Inbox(client, alias="line-alias").consume().messages == []
    assert transport.calls[0]["url"].endswith("/v1/aliases/line-alias/resolve")
    assert transport.calls[1]["url"].endswith("/v1/channels/agent:owner-uuid/consume")


def test_alias_403_does_not_fall_back_to_named_channel():
    transport = RecordingTransport()
    transport.queue(403, {"error": "forbidden"})
    client = BusClient(token="test", agent_id="service", transport=transport)
    with pytest.raises(BusError) as error:
        client.inbox_channel("line-alias")
    assert error.value.status == 403
    assert len(transport.calls) == 1


def test_only_legacy_404_uses_alias_channel():
    transport = RecordingTransport()
    transport.queue(404, {})
    client = BusClient(token="test", agent_id="service", transport=transport)
    assert client.inbox_channel("line-alias") == "agent:line-alias"


def test_inbox_forbidden_resolution_is_not_retried_for_error_message():
    transport = RecordingTransport()
    transport.queue(403, {"error": "forbidden"})
    client = BusClient(token="test", agent_id="service", transport=transport)
    with pytest.raises(InboxForbidden):
        Inbox(client, alias="line-alias").consume()
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "payload", [{}, [], {"inbox_channel_id": "agent:"}, {"current_agent_id": 12}]
)
def test_malformed_success_does_not_guess_alias_channel(payload):
    transport = RecordingTransport()
    transport.queue(200, payload)
    client = BusClient(token="test", agent_id="service", transport=transport)
    with pytest.raises(BusError) as error:
        client.inbox_channel("line-alias")
    assert error.value.status == 502
    assert len(transport.calls) == 1


def test_sink_uses_resolved_channel(monkeypatch):
    transport = RecordingTransport()
    transport.queue(200, {"current_agent_id": "owner-uuid"})
    transport.queue(200, {"message_id": "msg-1", "channel_seq": 1})
    client = BusClient(token="test", agent_id="service", transport=transport)
    sink = BusLineMessageSink()
    monkeypatch.setattr(sink, "_client", lambda _: client)
    assert sink.publish("line-alias", {"body": {"text": "继续"}}) == "msg-1"
    assert transport.calls[1]["url"].endswith("/v1/channels/agent:owner-uuid/publish")


@pytest.mark.parametrize("millisecond,expected", [(100, False), (101, True), (99, False)])
def test_same_second_inbox_message_wakes_only_when_newer(monkeypatch, millisecond, expected):
    transport = RecordingTransport()
    transport.queue(200, {"inbox_channel_id": "agent:owner-uuid"})
    transport.queue(200, {"head_seq": 1, "messages": []})
    transport.queue(
        200,
        {"head_seq": 1, "messages": [{"created_at": f"2026-09-07T12:00:00.{millisecond:03d}Z"}]},
    )
    client = BusClient(token="test", agent_id="service", transport=transport)
    signals = LiveWakeSignals(bus_client=client)
    monkeypatch.setattr(signals, "_line_token", lambda _: None)
    assert (
        signals.inbox_message_after("line-alias", parse_bus_timestamp("2026-09-07T12:00:00.100Z"))
        is expected
    )
    assert all("agent:owner-uuid" in call["url"] for call in transport.calls[1:])

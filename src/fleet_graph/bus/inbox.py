"""Inbox drain with the must-deliver ordering.

The pump's rule, carried over exactly (`goal-agent/src/goal_agent/inbox.py`):

    consume (take a lease)
      -> build the coordinator input including the messages
      -> fsync that file to disk
      -> only then ack each delivery
      -> only then start the coordinator

The ordering is the protocol, not a style choice. Ack before the durable write
and a crash in between loses the message outright: the bus considers it
delivered and nothing on disk remembers it. Ack after, and a crash merely
replays it -- the lease expires, the bus redelivers, and the coordinator
de-duplicates on message_id. One direction loses work, the other repeats it,
and repeating is always the cheaper mistake.

`drain_then_ack` exists so that ordering cannot be got wrong by a caller: the
ack is unreachable unless the persist callback returned.

Delivery order is whatever the bus returned (channel_seq FIFO). It is never
re-sorted and never prioritised by content -- INV-3 says this layer does not
interpret message semantics.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fleet_graph.bus.client import BusClient, BusError
from fleet_graph.state.run_artifacts import iso

DEFAULT_MAX_MESSAGES = 10
DEFAULT_LEASE_MS = 60_000

# A delivery retried this many times is close to the dead-letter threshold;
# worth surfacing rather than silently replaying forever.
NEAR_DEAD_ATTEMPT_THRESHOLD = 4

AckOutcome = Literal["acked", "lease_lost", "error"]


class InboxError(RuntimeError):
    pass


class InboxForbidden(InboxError):
    """403: the alias was rebound and this process no longer owns the inbox.

    Its own type because the correct response is to stop the line, not to
    retry -- someone else is now consuming these messages.
    """


ENVELOPE_FIELDS = (
    "message_id",
    "from_alias",
    "from_agent_id",
    "thread_id",
    "depth",
    "sent_at",
    "received_at",
    "body",
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _body_of(payload: dict[str, Any]) -> str:
    """Never return an empty body.

    A real incident is the reason this exists: a message in an unexpected
    shape arrived with every field null and an empty body, the coordinator
    input schema rejected the whole round, and the pump died on it. Anything
    can be published into an inbox, so the fallback chain degrades to readable
    text rather than letting one malformed message poison the line.
    """
    body = payload.get("body")
    if isinstance(body, str) and body:
        return body
    if body is not None and not isinstance(body, str):
        return str(body)

    parts = [payload[key] for key in ("summary", "detail") if isinstance(payload.get(key), str)]
    if parts:
        return "\n".join(parts)
    return json.dumps(payload, ensure_ascii=False)


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    lease_token: str
    message: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0

    @property
    def message_id(self) -> str:
        return str(self.message.get("message_id", ""))

    @property
    def payload(self) -> dict[str, Any]:
        payload = self.message.get("payload")
        return payload if isinstance(payload, dict) else {}

    @property
    def near_dead(self) -> bool:
        return self.attempt >= NEAR_DEAD_ATTEMPT_THRESHOLD

    def as_message(self, received_fallback: str = "") -> dict[str, Any]:
        """The fixed eight-field envelope the coordinator role's schema requires.

        Every field is coerced to the declared type. Inbox content is
        untrusted and arrives in whatever shape the sender chose, so a missing
        or wrongly typed field degrades to a usable default instead of failing
        schema validation and taking the round down with it.
        """
        payload = self.payload
        sender = _text(self.message.get("sender_agent_id"))
        depth = payload.get("depth")
        created_at = self.message.get("created_at")

        return {
            "message_id": self.message_id,
            "from_alias": (
                _text(payload.get("from_alias"))
                or _text(payload.get("from"))
                or sender
                or "unknown"
            ),
            "from_agent_id": _text(payload.get("from_agent_id")) or sender or "unknown",
            "thread_id": (
                _text(payload.get("thread_id"))
                or _text(payload.get("thread_entity_id"))
                or self.message_id
            ),
            "depth": depth if isinstance(depth, int) and not isinstance(depth, bool) else 0,
            "sent_at": (_text(payload.get("sent_at")) or _text(created_at) or received_fallback),
            "received_at": _text(created_at) or received_fallback,
            "body": _body_of(payload),
        }


@dataclass
class Drain:
    deliveries: list[Delivery] = field(default_factory=list)

    received_at: str = ""

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [d.as_message(self.received_at) for d in self.deliveries]

    @property
    def near_dead(self) -> list[Delivery]:
        return [d for d in self.deliveries if d.near_dead]

    def __len__(self) -> int:
        return len(self.deliveries)


class Inbox:
    def __init__(
        self,
        client: BusClient,
        alias: str,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        lease_ms: int = DEFAULT_LEASE_MS,
    ) -> None:
        self.client = client
        self.alias = alias
        self.max_messages = max_messages
        self.lease_ms = lease_ms

    @property
    def channel_id(self) -> str:
        return f"agent:{self.alias}"

    def consume(self) -> Drain:
        """Take a lease on up to `max_messages`. Does not ack -- that is the point."""
        try:
            result = self.client.post(
                f"/v1/channels/{self.channel_id}/consume",
                {"max_messages": self.max_messages, "lease_ms": self.lease_ms},
            )
        except BusError as exc:
            if exc.status == 403:
                raise InboxForbidden(
                    f"consume 403 on {self.channel_id}: the alias was rebound, "
                    "this process no longer owns the inbox"
                ) from exc
            raise

        deliveries = result.get("deliveries")
        if deliveries is None:
            raise InboxError("consume response had no deliveries array")

        drain = Drain(received_at=iso(time.time()))
        for raw in deliveries:
            delivery_id = raw.get("delivery_id")
            lease_token = raw.get("lease_token")
            if not delivery_id or not lease_token:
                # Unackable; skip rather than pretend we can hold it.
                continue
            message = raw.get("message")
            if not isinstance(message, dict) or not message.get("message_id"):
                # No message_id means nothing can de-duplicate it downstream.
                continue
            drain.deliveries.append(
                Delivery(
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    message=message,
                    attempt=int(raw.get("attempt") or 0),
                )
            )
        return drain

    def ack(self, delivery: Delivery) -> AckOutcome:
        try:
            self.client.post(
                f"/v1/deliveries/{delivery.delivery_id}/ack",
                {"lease_token": delivery.lease_token},
            )
        except BusError as exc:
            # 409 means the lease expired and someone else may hold it now.
            # Not an error worth stopping for -- the message will be redelivered.
            return "lease_lost" if exc.status == 409 else "error"
        return "acked"

    def drain_then_ack(
        self, persist: Callable[[list[dict[str, Any]]], None]
    ) -> tuple[Drain, list[AckOutcome]]:
        """Consume, hand the messages to `persist`, and ack only if it returns.

        `persist` must make the messages durable before returning -- see
        `write_json_durable`. If it raises, nothing is acked and the bus
        redelivers when the lease expires; that is the intended failure mode.
        """
        drain = self.consume()
        persist(drain.messages)
        return drain, [self.ack(d) for d in drain.deliveries]


__all__ = [
    "ENVELOPE_FIELDS",
    "NEAR_DEAD_ATTEMPT_THRESHOLD",
    "AckOutcome",
    "Delivery",
    "Drain",
    "Inbox",
    "InboxError",
    "InboxForbidden",
]

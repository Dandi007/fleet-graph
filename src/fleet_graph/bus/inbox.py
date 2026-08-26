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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fleet_graph.bus.client import BusClient, BusError

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


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    lease_token: str
    message_id: str
    kind: str
    payload: dict[str, Any]
    attempt: int = 0

    @property
    def near_dead(self) -> bool:
        return self.attempt >= NEAR_DEAD_ATTEMPT_THRESHOLD

    def as_message(self) -> dict[str, Any]:
        """The structured form handed to the coordinator.

        message_id is included because redelivery is expected: the coordinator
        de-duplicates on it, and that is what makes ack-after-fsync safe.
        """
        return {
            "message_id": self.message_id,
            "kind": self.kind,
            "payload": self.payload,
            "attempt": self.attempt,
        }


@dataclass
class Drain:
    deliveries: list[Delivery] = field(default_factory=list)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [d.as_message() for d in self.deliveries]

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

        drain = Drain()
        for raw in deliveries:
            delivery_id = raw.get("delivery_id")
            lease_token = raw.get("lease_token")
            if not delivery_id or not lease_token:
                # Unackable; skip rather than pretend we can hold it.
                continue
            drain.deliveries.append(
                Delivery(
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    message_id=str(raw.get("message_id", "")),
                    kind=str(raw.get("kind", "")),
                    payload=raw.get("payload") or {},
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
    "NEAR_DEAD_ATTEMPT_THRESHOLD",
    "AckOutcome",
    "Delivery",
    "Drain",
    "Inbox",
    "InboxError",
    "InboxForbidden",
]

"""The M4 supervisor line-message: ``line_message(line, text, kind)``.

The supervisor plane's one write edge into a goal line's inbox. Three
properties are structural, not conventional:

- **Supervisor-only.** The invoking principal must pass the same
  supervision-plane identity check ``goal_admit`` / ``goal_reject`` use; a
  non-supervisor identity is refused before anything is resolved or
  delivered, so a callable capability is created without broadening the
  authorization boundary.
- **Message ≠ decision.** The published payload is built by one closed
  field set that has no decision field at all -- a message can never carry
  ``APPROVE``/``REJECT`` semantics, because there is no field to put them
  in. A parked (``waiting_on=decision``) line may be *woken* by the message
  (it lands in the inbox, which is the M1 ``inbox_message`` wake fact), but
  only ``decision_deliver`` (the M2 path) lifts a park: the parking fields
  live in the scheduler's stall-state file and nothing in this module reads
  or writes them. The pump reinforces this at ack time: an instruction
  whose text is a bare decision token is mechanically acked
  ``rejected`` with reason ``message_is_not_a_decision``.
- **Kind is closed.** ``kind`` is ``instruction`` or ``info``; an
  instruction obliges the line's next round to ack
  ``(message_id, executed | rejected + reason)``. A round that leaves an
  instruction unacked counts as an idle round (the R8 idle count); alert
  rules over the count are a later unit's scope.

Delivery goes to the line's own bus inbox channel (``agent:{alias}``) with
the line's own mirrored token -- the identical credential discipline the
M1 wake probe and the inbox content path already use, so the channel ACL is
never widened for the supervisor's sake.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from fleet_graph.bus.client import DEFAULT_BUS_URL
from fleet_graph.state.run_artifacts import iso

#: The closed message-kind vocabulary. ``instruction`` obliges an ack;
#: ``info`` is a fact the line may read without answering.
KIND_INSTRUCTION = "instruction"
KIND_INFO = "info"
ALLOWED_KINDS = (KIND_INSTRUCTION, KIND_INFO)

#: Machine-readable refusal codes (closed). Each means "nothing was
#: delivered", so a refusal is never confusable with a partial send.
CODE_KIND_INVALID = "LINE_MESSAGE_KIND_INVALID"
CODE_NOT_SUPERVISOR = "LINE_MESSAGE_NOT_SUPERVISOR"
CODE_LINE_NOT_FOUND = "LINE_MESSAGE_LINE_NOT_FOUND"
CODE_TEXT_REQUIRED = "LINE_MESSAGE_TEXT_REQUIRED"
CODE_SINK_UNBOUND = "LINE_MESSAGE_SINK_UNBOUND"
CODE_DELIVERY_FAILED = "LINE_MESSAGE_DELIVERY_FAILED"

#: The payload marker that lets the pump recognise this module's messages in
#: a drained inbox (``Delivery.payload[LINE_MESSAGE_MARKER]``) and run the
#: ack obligation. Lives *inside* the payload; the coordinator-facing
#: eight-field envelope stays untouched.
LINE_MESSAGE_MARKER = "line_message"

#: The M1 wake-fact name this delivery produces (the inbox is wake source 1).
WAKE_FACT = "inbox_message"

#: The closed payload field set. A decision field is deliberately absent:
#: adding one is the exact regression the surface tests red-line on.
PAYLOAD_FIELDS = (
    "body",
    "from_alias",
    "from_agent_id",
    "thread_id",
    "depth",
    "sent_at",
    LINE_MESSAGE_MARKER,
)

#: The ack outcome vocabulary plus the one mechanical reason the pump writes.
ACK_EXECUTED = "executed"
ACK_REJECTED = "rejected"
ALLOWED_ACK_OUTCOMES = (ACK_EXECUTED, ACK_REJECTED)
DECISION_GUARD_REASON = "message_is_not_a_decision"

#: The decision vocabulary the pump refuses to execute from a message: a
#: ``line_message`` whose whole instruction text is one of these tokens is a
#: attempted verdict, and verdicts travel only via decision_deliver.
_DECISION_TOKENS = frozenset({"APPROVE", "REJECT"})


class LineMessageError(RuntimeError):
    """A refusal with one stable machine-readable cause per code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.detail}


def normalize_message_kind(raw: Any) -> str:
    """The closed kind vocabulary, or a :class:`LineMessageError` refusal."""
    kind = str(raw or "").strip()
    if kind not in ALLOWED_KINDS:
        raise LineMessageError(
            CODE_KIND_INVALID,
            f"kind must be one of {list(ALLOWED_KINDS)}, got {raw!r}",
        )
    return kind


def is_decision_text(text: str) -> bool:
    """True when the message text is a bare decision token.

    ``line_message("APPROVE")`` is the shape the M4 negative criterion pins:
    the line may be woken by it, but it must never be read as a verdict.
    """
    return str(text or "").strip().upper() in _DECISION_TOKENS


def build_line_message_payload(
    *,
    line: str,
    text: str,
    kind: str,
    sent_by: str,
    clock: Any = time.time,
) -> dict[str, Any]:
    """The closed message payload -- no decision field, by construction.

    The payload is shaped so the existing inbox drain degrades gracefully
    (``body`` is the text; the ``from_*`` fields name the supervisor plane)
    while the structured marker lets the pump run the ack obligation. The
    field set is closed: a decision field cannot ride along, because this is
    the only builder and it does not have that parameter.
    """
    kind = normalize_message_kind(kind)
    body = str(text or "").strip()
    if not body:
        raise LineMessageError(CODE_TEXT_REQUIRED, "a line message without text is not a message")
    sent_at = iso(clock())
    return {
        "body": body,
        "from_alias": "supervisor",
        "from_agent_id": sent_by,
        "thread_id": f"line-message:{line}",
        "depth": 0,
        "sent_at": sent_at,
        LINE_MESSAGE_MARKER: {
            "kind": kind,
            "sent_by": sent_by,
            "sent_at": sent_at,
        },
    }


def deliver_line_message(
    line: str,
    text: str,
    kind: str,
    sent_by: str,
    *,
    resolve_alias: Any,
    sink: Any,
    identity_check: Any,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Validate, then land one message in the line's inbox.

    Order is load-bearing: the principal check runs before the line is even
    resolved, so a non-supervisor caller learns nothing about the roster.
    ``resolve_alias(line) -> alias | None`` is the read-only roster seam;
    ``sink.publish(alias, payload) -> message_id`` is the delivery seam (the
    production sink publishes ``agent.msg.v1`` into ``agent:{alias}`` with
    the line's own token). Every refusal means nothing was delivered.
    """
    kind = normalize_message_kind(kind)
    identity = str(sent_by or "").strip()
    if not identity or not identity_check(identity):
        raise LineMessageError(
            CODE_NOT_SUPERVISOR,
            f"identity {identity!r} is not a supervisor-plane principal; "
            "line messages are supervisor-only",
        )
    alias = resolve_alias(line)
    if not alias:
        raise LineMessageError(
            CODE_LINE_NOT_FOUND,
            f"no roster line {line!r} with an inbox alias; nothing to deliver to",
        )
    if sink is None:
        raise LineMessageError(CODE_SINK_UNBOUND, "no inbox sink is bound to this surface")
    payload = build_line_message_payload(
        line=line, text=text, kind=kind, sent_by=identity, clock=clock
    )
    try:
        message_id = sink.publish(alias, payload)
    except LineMessageError:
        raise
    except Exception as exc:
        raise LineMessageError(
            CODE_DELIVERY_FAILED, f"delivering to agent:{alias} failed: {exc}"
        ) from exc
    return {
        "message_id": str(message_id),
        "line": line,
        "alias": alias,
        "kind": kind,
        "wake_fact": WAKE_FACT,
        "delivered": True,
    }


class BusLineMessageSink:
    """The production sink: publish into ``agent:{alias}`` over the bus.

    Authenticates with the line's own mirrored token -- the channel is
    owner-only and the owner is the line -- falling back to the service
    client only when no token file resolves, exactly like the M1 wake probe.
    A failed publish raises; the tool layer turns it into
    ``LINE_MESSAGE_DELIVERY_FAILED``.

    The sink's default ``base_url`` resolves once, at construction time: an
    explicit ``base_url`` wins verbatim; ``None`` means "read
    ``FLEET_GRAPH_BUS_URL``, else fall back to the bus client's
    ``DEFAULT_BUS_URL``" -- so a default-constructed sink can never hand a
    ``None`` down to :class:`~fleet_graph.bus.client.BusClient` (whose own
    default a bare ``None`` would strike through).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        line_token_template: str | None = None,
    ) -> None:
        if base_url is not None:
            self._base_url = base_url
        else:
            self._base_url = os.environ.get("FLEET_GRAPH_BUS_URL", DEFAULT_BUS_URL)
        self.line_token_template = line_token_template

    def _client(self, alias: str) -> Any:
        from fleet_graph.bus.client import BusClient
        from fleet_graph.bus.tokens import resolve_line_token

        token = resolve_line_token(alias, template=self.line_token_template).token
        if token:
            return BusClient(base_url=self._base_url, token=token)
        return BusClient(base_url=self._base_url)

    def publish(self, alias: str, payload: dict[str, Any]) -> str:
        client = self._client(alias)
        result = client.publish(
            f"agent:{alias}",
            "agent.msg.v1",
            payload,
            idempotency_key=f"line-message:{uuid.uuid4().hex}",
        )
        return str(result.message_id)


def parse_verdict_acks(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The coordinator verdict's declared acks, validated and keyed by id.

    A verdict may ack the messages its round drained:
    ``acks: [{message_id, outcome: executed | rejected, reason}]``. A
    ``rejected`` ack without a reason is a protocol defect and is dropped
    (the instruction then stays unacked -- honest, not guessed). Anything
    that is not a dict, or whose outcome is outside the closed vocabulary,
    is dropped the same way.
    """
    acks: dict[str, dict[str, Any]] = {}
    for raw in result.get("acks") or []:
        if not isinstance(raw, dict):
            continue
        message_id = str(raw.get("message_id") or "").strip()
        outcome = str(raw.get("outcome") or "").strip()
        if not message_id or outcome not in ALLOWED_ACK_OUTCOMES:
            continue
        if outcome == ACK_REJECTED and not str(raw.get("reason") or "").strip():
            continue
        acks[message_id] = {
            "message_id": message_id,
            "outcome": outcome,
            "reason": str(raw.get("reason") or ""),
        }
    return acks


def ack_rows_for_round(
    deliveries: list[tuple[str, dict[str, Any]]],
    verdict_acks: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """One round's ack rows and its unacked instruction ids.

    ``deliveries`` is ``[(message_id, payload)]`` for the round's drained
    line-messages. The pump's mechanical decision-guard wins over the
    verdict: an instruction whose text is a bare decision token is acked
    ``rejected`` / ``message_is_not_a_decision`` no matter what the verdict
    claims, because a message can never be a verdict. Instructions left
    unacked come back in ``unacked`` -- the caller counts them as an idle
    round (R8). ``info`` messages carry no obligation.
    """
    acks: list[dict[str, Any]] = []
    unacked: list[str] = []
    for message_id, payload in deliveries:
        marker = payload.get(LINE_MESSAGE_MARKER)
        if not isinstance(marker, dict):
            continue
        kind = str(marker.get("kind") or KIND_INFO)
        body = str(payload.get("body") or "")
        if kind == KIND_INSTRUCTION and is_decision_text(body):
            acks.append(
                {
                    "message_id": message_id,
                    "kind": kind,
                    "outcome": ACK_REJECTED,
                    "reason": DECISION_GUARD_REASON,
                }
            )
            continue
        declared = verdict_acks.get(message_id)
        if declared is not None:
            acks.append({"message_id": message_id, "kind": kind, **declared})
            continue
        if kind == KIND_INSTRUCTION:
            unacked.append(message_id)
    return acks, unacked


def marker_from_payload(payload: Any) -> dict[str, Any] | None:
    """The line-message marker of a drained payload, or None."""
    if not isinstance(payload, dict):
        return None
    marker = payload.get(LINE_MESSAGE_MARKER)
    return marker if isinstance(marker, dict) else None


__all__ = [
    "ACK_EXECUTED",
    "ACK_REJECTED",
    "ALLOWED_ACK_OUTCOMES",
    "ALLOWED_KINDS",
    "CODE_DELIVERY_FAILED",
    "CODE_KIND_INVALID",
    "CODE_LINE_NOT_FOUND",
    "CODE_NOT_SUPERVISOR",
    "CODE_SINK_UNBOUND",
    "CODE_TEXT_REQUIRED",
    "DECISION_GUARD_REASON",
    "KIND_INFO",
    "KIND_INSTRUCTION",
    "LINE_MESSAGE_MARKER",
    "PAYLOAD_FIELDS",
    "WAKE_FACT",
    "BusLineMessageSink",
    "LineMessageError",
    "ack_rows_for_round",
    "build_line_message_payload",
    "deliver_line_message",
    "is_decision_text",
    "marker_from_payload",
    "normalize_message_kind",
    "parse_verdict_acks",
]

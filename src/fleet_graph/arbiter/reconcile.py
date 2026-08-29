"""A2 identity reconciliation: who is the arbiter, and what inbox should it read.

Before the managed arbiter tick does any model work or publishes a note, it
must prove -- read-only -- that the arbiter identity is real and that the
``arbiter`` alias resolves to the expected agent. Two reads, both GETs, and no
write of any kind:

1. ``GET /v1/agents/whoami`` proves the credential is live and returns the
   caller's own ``agent_id``. Failures here (unavailable, or a missing /
   malformed ``agent_id``) fail the whole reconciliation: without a trustworthy
   read surface there is nothing to reconcile against.
2. ``GET /v1/aliases/{alias}`` is the real alias read surface. Its
   ``current_agent_id`` is the *authoritative* arbiter identity.

The alias's ``current_agent_id`` must equal the expected agent id (``arbiter``).
Missing, malformed, mismatched, unavailable, or ambiguous identity data all
refuse closed with no fallback and no guessed identity. Only after the identity
round-trips does the reconciler derive the inbox channel as
``agent:<current_agent_id>`` -- ``agent:arbiter`` for the fleet arbiter.

The arbiter is a read-only role with no identity-mutation authority, so this
module only verifies and refuses. It exposes no create / register / token-mint
/ token-write fallback and performs no mutation: every data path here is a
read, and the only outcomes are a pass record or a refusal naming the failed
fact. Nothing here imports a publish path, and no code path constructs a
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The managed alias. The systemd unit is pinned to this alias, which maps to
#: the inbox ``agent:arbiter``.
ARBITER_ALIAS = "arbiter"

#: The inbox channel the alias must derive to after successful verification.
ARBITER_INBOX = "agent:arbiter"

#: The alias the fleet arbiter resolves before trusting any inbox derivation.
DEFAULT_ARBITER_ALIAS = ARBITER_ALIAS

#: The agent id that alias must currently be bound to. Anything else refuses.
DEFAULT_ARBITER_AGENT_ID = "arbiter"


class ArbiterReconcileError(ValueError):
    """Reconciliation refused closed; no identity was derived or guessed."""


@dataclass(frozen=True)
class ArbiterIdentity:
    """The verified identity, plus the inbox channel derived from it.

    ``reconcile_state`` is ``ok`` only when the identity was verified fail-closed;
    a refusal raises instead of constructing this value, so a caller can never
    mistake a half-reconciled identity for a verified one.
    """

    reconcile_state: str = "ok"
    agent_id: str = ""
    inbox_channel: str = ""

    @property
    def ok(self) -> bool:
        return self.reconcile_state == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "reconcile_state": self.reconcile_state,
            "agent_id": self.agent_id,
            "inbox_channel": self.inbox_channel,
        }


def inbox_for(agent_id: str) -> str:
    """The ``agent:<agent_id>`` inbox channel a verified agent binds to."""
    return f"agent:{agent_id}"


def _whoami_agent_id(whoami: Any) -> str:
    """The caller's own id, or a refusal. ``whoami`` is untrusted wire data."""
    if not isinstance(whoami, dict):
        raise ArbiterReconcileError(
            f"whoami returned {type(whoami).__name__}, not an object; refusing closed"
        )
    agent_id = whoami.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ArbiterReconcileError(
            "whoami returned a missing or malformed agent_id; refusing closed"
        )
    return agent_id.strip()


def _alias_current_agent_id(alias_response: Any, *, alias: str) -> str:
    """The alias's authoritative ``current_agent_id``, extracted without guessing.

    agent-bus returns the alias twice: the ``alias`` object is spread flat so a
    legacy consumer can read a top-level ``current_agent_id``, and the nested
    ``alias`` object still carries its own. Both must agree; disagreement is
    ambiguous identity data and refuses closed.
    """
    if not isinstance(alias_response, dict):
        raise ArbiterReconcileError(
            f"alias {alias!r} returned {type(alias_response).__name__}, "
            "not an object; refusing closed"
        )
    values: dict[str, str] = {}

    flat = alias_response.get("current_agent_id")
    if isinstance(flat, str) and flat.strip():
        values["flat"] = flat.strip()
    elif flat is not None:
        raise ArbiterReconcileError(
            f"alias {alias!r} current_agent_id is malformed "
            "(not a non-empty string); refusing closed"
        )

    nested = alias_response.get("alias")
    if isinstance(nested, dict):
        nested_id = nested.get("current_agent_id")
        if isinstance(nested_id, str) and nested_id.strip():
            values["nested"] = nested_id.strip()
        elif nested_id is not None:
            raise ArbiterReconcileError(
                f"alias {alias!r} nested current_agent_id is malformed; refusing closed"
            )

    distinct = set(values.values())
    if not distinct:
        raise ArbiterReconcileError(f"alias {alias!r} has no current_agent_id; refusing closed")
    if len(distinct) > 1:
        raise ArbiterReconcileError(
            f"alias {alias!r} current_agent_id is ambiguous "
            f"({', '.join(sorted(distinct))}); refusing closed"
        )
    return next(iter(distinct))


def reconcile_arbiter_identity(
    client: Any,
    *,
    alias: str = DEFAULT_ARBITER_ALIAS,
    expected_agent_id: str = DEFAULT_ARBITER_AGENT_ID,
) -> ArbiterIdentity:
    """Verify the arbiter identity and derive its inbox channel, fail-closed.

    Performs only reads: ``GET /v1/agents/whoami`` (proves the credential is
    live) and ``GET /v1/aliases/{alias}`` (the authoritative identity). A
    network or HTTP failure surfaces as a bus error; every identity-shaped
    problem surfaces as :class:`ArbiterReconcileError`. Neither path falls back
    to a guessed identity. The inbox channel is derived as ``agent:<current_agent_id>``
    only after the alias identity round-trips against ``expected_agent_id``.
    """
    _whoami_agent_id(client.get("/v1/agents/whoami"))
    current_agent_id = _alias_current_agent_id(client.get(f"/v1/aliases/{alias}"), alias=alias)
    if current_agent_id != expected_agent_id:
        raise ArbiterReconcileError(
            f"alias {alias!r} resolves to {current_agent_id!r}, expected "
            f"{expected_agent_id!r}; refusing closed"
        )
    return ArbiterIdentity(
        reconcile_state="ok",
        agent_id=current_agent_id,
        inbox_channel=inbox_for(current_agent_id),
    )


__all__ = [
    "ARBITER_ALIAS",
    "ARBITER_INBOX",
    "DEFAULT_ARBITER_AGENT_ID",
    "DEFAULT_ARBITER_ALIAS",
    "ArbiterIdentity",
    "ArbiterReconcileError",
    "inbox_for",
    "reconcile_arbiter_identity",
]

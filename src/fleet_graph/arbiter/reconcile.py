"""Validation-only principal/alias reconciliation for the A2 managed path.

Before the managed arbiter tick does any model work or publishes a note, it
must prove -- read-only -- two identity facts about the caller it is running
as:

1. The authenticated principal (the subject the bus token identifies) is the
   expected arbiter principal; and
2. The ``arbiter`` alias binding resolves to the inbox channel ``agent:arbiter``.

The arbiter is a read-only role with no identity-mutation authority, so this
module only verifies and refuses. It exposes no create / register / token-mint
/ token-write fallback and performs no mutation: every data path here is a
read, and the only outcomes are a pass record or a refusal naming a missing /
mismatched / rebound / unauthorized state.

The refused states are closed and machine-readable (``Reconciliation.state``):

- ``missing_principal`` -- no authenticated principal resolved (token absent or
  unreadable);
- ``mismatched_principal`` -- an authenticated principal resolved, but it is
  not the expected arbiter principal (this is the "unauthorized" case: a
  different subject holding the arbiter's seat);
- ``missing_binding`` -- the alias has no inbox binding (``agent:arbiter`` not
  registered);
- ``rebound`` -- the alias resolves to a different inbox channel (someone else
  now owns ``arbiter``).

``ok`` means both facts held and the caller may proceed. No refusal mutates
anything; a correct existing state continues untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

#: The managed alias. The systemd unit is pinned to this alias, which maps to
#: the inbox ``agent:arbiter``.
ARBITER_ALIAS = "arbiter"

#: The inbox channel the alias must resolve to.
ARBITER_INBOX = "agent:arbiter"

#: The expected authenticated principal when none is named in the site env.
DEFAULT_EXPECTED_PRINCIPAL = "agent:arbiter"

STATE_OK = "ok"
STATE_MISSING_PRINCIPAL = "missing_principal"
STATE_MISMATCHED_PRINCIPAL = "mismatched_principal"
STATE_MISSING_BINDING = "missing_binding"
STATE_REBOUND = "rebound"

REFUSAL_STATES = frozenset(
    {
        STATE_MISSING_PRINCIPAL,
        STATE_MISMATCHED_PRINCIPAL,
        STATE_MISSING_BINDING,
        STATE_REBOUND,
    }
)


class ReconciliationError(RuntimeError):
    """The arbiter identity facts did not hold. Non-secret; names the state."""

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(detail)
        self.state = state
        self.detail = detail


@dataclass(frozen=True)
class Reconciliation:
    """The read-only identity verdict. ``state`` is one of the closed states."""

    state: str
    authenticated_principal: str
    expected_principal: str
    alias: str
    inbox_channel: str
    alias_channel: str

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "authenticated_principal": self.authenticated_principal,
            "expected_principal": self.expected_principal,
            "alias": self.alias,
            "inbox_channel": self.inbox_channel,
            "alias_channel": self.alias_channel,
        }


def inbox_for(alias: str) -> str:
    """The ``agent:<alias>`` inbox channel an alias binds to."""
    return f"agent:{alias}"


def reconcile_principal_alias(
    *,
    authenticated_principal: str | None,
    expected_principal: str,
    alias: str,
    alias_channel: str | None,
) -> Reconciliation:
    """Verify the two identity facts, refusing on any missing/mismatch/rebound.

    Pure: reads nothing on its own, mutates nothing. Returns a pass record or
    raises :class:`ReconciliationError` naming the refused state.
    """
    inbox = inbox_for(alias)
    principal = (authenticated_principal or "").strip()
    if not principal:
        raise ReconciliationError(
            STATE_MISSING_PRINCIPAL,
            "arbiter identity is missing: no authenticated principal resolved",
        )
    if principal != expected_principal:
        raise ReconciliationError(
            STATE_MISMATCHED_PRINCIPAL,
            f"authenticated principal {principal!r} is not the expected arbiter "
            f"principal {expected_principal!r}",
        )
    channel = (alias_channel or "").strip()
    if not channel:
        raise ReconciliationError(
            STATE_MISSING_BINDING,
            f"arbiter alias {alias!r} has no inbox binding",
        )
    if channel != inbox:
        raise ReconciliationError(
            STATE_REBOUND,
            f"arbiter alias {alias!r} is rebound to {channel!r}, expected {inbox!r}",
        )
    return Reconciliation(
        state=STATE_OK,
        authenticated_principal=principal,
        expected_principal=expected_principal,
        alias=alias,
        inbox_channel=inbox,
        alias_channel=channel,
    )


class PrincipalBindingProbe(Protocol):
    """The read-only identity source: authenticated principal and alias binding.

    Tests substitute a fake; production uses :class:`BusPrincipalBindingProbe`.
    """

    def authenticated_principal(self) -> str | None: ...

    def alias_channel(self, alias: str) -> str | None: ...


class BusPrincipalBindingProbe:
    """Reads the authenticated principal and alias binding off the bus.

    Both reads are plain GETs against the bus identity surface; neither creates
    nor mutates anything. A read that fails (missing credential, bus down,
    endpoint absent) degrades to ``None`` so the reconciler reports the
    ``missing_*`` state -- the arbiter refuses closed rather than guessing an
    identity.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def authenticated_principal(self) -> str | None:
        try:
            data = self._client.get("/v1/principal")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        principal = data.get("principal")
        return principal if isinstance(principal, str) and principal else None

    def alias_channel(self, alias: str) -> str | None:
        try:
            data = self._client.get(f"/v1/aliases/{alias}")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        channel = data.get("channel_id") or data.get("channel")
        return channel if isinstance(channel, str) and channel else None


__all__ = [
    "ARBITER_ALIAS",
    "ARBITER_INBOX",
    "DEFAULT_EXPECTED_PRINCIPAL",
    "REFUSAL_STATES",
    "STATE_MISMATCHED_PRINCIPAL",
    "STATE_MISSING_BINDING",
    "STATE_MISSING_PRINCIPAL",
    "STATE_OK",
    "STATE_REBOUND",
    "BusPrincipalBindingProbe",
    "PrincipalBindingProbe",
    "Reconciliation",
    "ReconciliationError",
    "inbox_for",
    "reconcile_principal_alias",
]

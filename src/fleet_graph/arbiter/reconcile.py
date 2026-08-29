"""Validation-only identity reconciliation for the A2 managed path.

Before the managed arbiter tick does any model work or publishes a note, it
must prove -- read-only, against the real Agent Bus gateway -- two identity
facts about the caller it is running as:

1. The authenticated agent the gateway sees (``GET /v1/agents/whoami``) is a
   real, resolvable identity (missing / malformed / unavailable / ambiguous ->
   refusal, never a guessed identity); and
2. The ``arbiter`` alias resolves (``POST /v1/aliases/arbiter/resolve``, the
   real alias read surface) to a real ``current_agent_id``, and that
   authoritative identity is the expected arbiter principal (bare id
   ``arbiter``).

The inbox channel is derived only after both facts hold, as
``agent:<current_agent_id>``, yielding ``agent:arbiter``. The arbiter is a
read-only role with no identity-mutation authority, so this module only
verifies and refuses. It exposes no create / register / token-mint /
token-write fallback and performs no mutation: every data path here is a read
against the gateway (``GET /v1/agents/whoami`` and the read-only alias
``resolve`` action), and the only outcomes are a pass record or a refusal
naming a missing / malformed / mismatched / rebound / ambiguous / unauthorized
state.

The refused states are closed and machine-readable (``Reconciliation.state``):

- ``missing_principal`` -- no authenticated agent identity resolved (whoami
  absent, unreadable, or returning no usable agent id);
- ``ambiguous_identity`` -- the whoami response carries conflicting agent
  identities, so no single authoritative caller identity exists;
- ``missing_binding`` -- the alias has no resolvable identity (the alias read
  surface returned no usable ``current_agent_id``);
- ``ambiguous_binding`` -- the alias response carries conflicting
  ``current_agent_id`` values (or names a different alias), so the binding is
  not authoritative;
- ``rebound`` -- the alias resolves to a different agent (someone else now owns
  ``arbiter``).

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
#: The bare principal id, never the derived inbox channel string.
DEFAULT_EXPECTED_PRINCIPAL = "arbiter"

STATE_OK = "ok"
STATE_MISSING_PRINCIPAL = "missing_principal"
STATE_AMBIGUOUS_IDENTITY = "ambiguous_identity"
STATE_MISMATCHED_PRINCIPAL = "mismatched_principal"
STATE_MISSING_BINDING = "missing_binding"
STATE_AMBIGUOUS_BINDING = "ambiguous_binding"
STATE_REBOUND = "rebound"

REFUSAL_STATES = frozenset(
    {
        STATE_MISSING_PRINCIPAL,
        STATE_AMBIGUOUS_IDENTITY,
        STATE_MISMATCHED_PRINCIPAL,
        STATE_MISSING_BINDING,
        STATE_AMBIGUOUS_BINDING,
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
    agent_id: str
    expected_principal: str
    alias: str
    inbox_channel: str

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "agent_id": self.agent_id,
            "expected_principal": self.expected_principal,
            "alias": self.alias,
            "inbox_channel": self.inbox_channel,
        }


def inbox_for(agent_id: str) -> str:
    """The ``agent:<agent_id>`` inbox channel a verified agent binds to."""
    return f"agent:{agent_id}"


def reconcile_principal_alias(
    *,
    whoami_agent_id: str | None,
    current_agent_id: str | None,
    expected_principal: str,
    alias: str,
) -> Reconciliation:
    """Verify the two identity facts, refusing on any missing/mismatch/rebound.

    Pure: reads nothing on its own, mutates nothing. Returns a pass record or
    raises :class:`ReconciliationError` naming the refused state. The caller
    (whoami) must be the expected arbiter principal, and the alias response's
    ``current_agent_id`` -- the authoritative identity -- must match it; the
    inbox channel is derived as ``agent:<current_agent_id>`` only after both
    facts hold.
    """
    whoami = (whoami_agent_id or "").strip()
    if not whoami:
        raise ReconciliationError(
            STATE_MISSING_PRINCIPAL,
            "arbiter identity is missing: no authenticated agent resolved "
            "(GET /v1/agents/whoami returned no usable agent id)",
        )
    if whoami != expected_principal:
        raise ReconciliationError(
            STATE_MISMATCHED_PRINCIPAL,
            f"authenticated agent {whoami!r} is not the expected arbiter "
            f"principal {expected_principal!r}",
        )
    resolved = (current_agent_id or "").strip()
    if not resolved:
        raise ReconciliationError(
            STATE_MISSING_BINDING,
            f"arbiter alias {alias!r} has no resolvable identity: the alias "
            "read surface returned no usable current_agent_id",
        )
    if resolved != expected_principal:
        raise ReconciliationError(
            STATE_REBOUND,
            f"arbiter alias {alias!r} resolves to {resolved!r}, expected "
            f"principal {expected_principal!r}",
        )
    inbox = inbox_for(resolved)
    return Reconciliation(
        state=STATE_OK,
        agent_id=resolved,
        expected_principal=expected_principal,
        alias=alias,
        inbox_channel=inbox,
    )


def _agent_id_candidates(data: Any) -> list[str]:
    """Every distinct non-empty agent-id value a response carries.

    The gateway shapes read off the running bus:
    ``GET /v1/agents/whoami`` -> ``{"agent_id": ...}`` and
    ``POST /v1/aliases/<alias>/resolve`` -> ``{"current_agent_id": ..., ...}``.
    Accept the familiar spellings (``agent_id`` / ``current_agent_id`` / ``id``)
    so a renamed field degrades to a refusal, never a guess.
    """
    if not isinstance(data, dict):
        return []
    candidates: list[str] = []
    for key in ("agent_id", "current_agent_id", "id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    return candidates


class PrincipalBindingProbe(Protocol):
    """The read-only identity source: caller whoami and alias binding.

    Tests substitute a fake; production uses :class:`BusPrincipalBindingProbe`.
    """

    def whoami(self) -> str | None: ...

    def alias_agent_id(self, alias: str) -> str | None: ...


class BusPrincipalBindingProbe:
    """Reads the caller identity and alias binding off the real gateway.

    ``whoami`` is a plain ``GET /v1/agents/whoami``; the alias binding is read
    through the real alias read surface ``POST /v1/aliases/<alias>/resolve``,
    whose ``current_agent_id`` is the authoritative identity. Neither call
    creates, publishes, or mutates anything -- ``resolve`` is a pure alias
    lookup. A read that fails (missing credential, bus down, endpoint absent)
    degrades to ``None`` so the reconciler reports the ``missing_*`` state --
    the arbiter refuses closed rather than guessing an identity. A response
    whose identity fields disagree is ambiguous and refuses outright.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def whoami(self) -> str | None:
        try:
            data = self._client.get("/v1/agents/whoami")
        except Exception:
            return None
        candidates = _agent_id_candidates(data)
        if not candidates:
            return None
        if len(set(candidates)) > 1:
            raise ReconciliationError(
                STATE_AMBIGUOUS_IDENTITY,
                "arbiter identity is ambiguous: GET /v1/agents/whoami returned "
                f"conflicting agent ids {sorted(set(candidates))!r}",
            )
        return candidates[0]

    def alias_agent_id(self, alias: str) -> str | None:
        try:
            data = self._client.post(f"/v1/aliases/{alias}/resolve", {})
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        named = data.get("alias")
        if isinstance(named, str) and named.strip() and named.strip() != alias:
            raise ReconciliationError(
                STATE_AMBIGUOUS_BINDING,
                f"arbiter alias {alias!r} resolves a different alias {named!r}",
            )
        candidates = _agent_id_candidates(data)
        nested = data.get("alias")
        if isinstance(nested, dict):
            candidates.extend(_agent_id_candidates(nested))
        if not candidates:
            return None
        if len(set(candidates)) > 1:
            raise ReconciliationError(
                STATE_AMBIGUOUS_BINDING,
                f"arbiter alias {alias!r} is ambiguous: the alias read surface "
                f"returned conflicting current_agent_id values "
                f"{sorted(set(candidates))!r}",
            )
        return candidates[0]


__all__ = [
    "ARBITER_ALIAS",
    "ARBITER_INBOX",
    "DEFAULT_EXPECTED_PRINCIPAL",
    "REFUSAL_STATES",
    "STATE_AMBIGUOUS_BINDING",
    "STATE_AMBIGUOUS_IDENTITY",
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

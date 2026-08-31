"""HTTP client for agent-bus.

Thin on purpose. agent-bus already owns the hard parts -- entity chains, CAS on
revisions, ref validation -- so this layer translates them into exceptions and
otherwise stays out of the way.

Credentials are env-only (plan.md P6). Either FLEET_GRAPH_BUS_TOKEN carries the
token, or FLEET_GRAPH_BUS_TOKEN_FILE points at the file holding it. Nothing is
ever defaulted to a literal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_BUS_URL = "http://127.0.0.1:7490"

# agent-bus rejects a revision whose `supersedes` is no longer the entity head.
CONFLICT_STATUS = 409


class BusError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"agent-bus returned HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class BusConflict(BusError):
    """Someone else revised the entity first. Re-read the head and retry."""


@dataclass(frozen=True)
class PublishResult:
    message_id: str
    entity_id: str
    channel_seq: int
    deduplicated: bool


class HttpTransport(Protocol):
    """The seam tests substitute. Keeps the client honest without a live bus."""

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]: ...


class HttpxTransport:
    def __init__(self, timeout: float = 30.0, *, trust_env: bool = False) -> None:
        import httpx

        # trust_env=False on purpose. This host runs a SOCKS proxy, and httpx
        # would otherwise route loopback traffic through it -- agent-bus and
        # the New API gateway both live on 127.0.0.1, so proxying them is
        # always wrong (and fails outright without the socks extra installed).
        self._client = httpx.Client(timeout=timeout, trust_env=trust_env)

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        response = self._client.request(method, url, headers=headers, json=json_body)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text


def load_token(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    token = env.get("FLEET_GRAPH_BUS_TOKEN")
    if token:
        return token.strip()
    token_file = env.get("FLEET_GRAPH_BUS_TOKEN_FILE")
    if token_file:
        return Path(token_file).read_text().strip()
    raise RuntimeError("no bus credential: set FLEET_GRAPH_BUS_TOKEN or FLEET_GRAPH_BUS_TOKEN_FILE")


def _agent_id_candidates(data: Any) -> list[str]:
    """Every distinct non-empty agent-id value a response carries.

    The gateway shapes read off the running bus: ``GET /v1/agents/whoami`` ->
    ``{"agent_id": ...}``. Accept the familiar spellings (``agent_id`` /
    ``current_agent_id`` / ``id``) so a renamed field degrades to an unknown
    identity, never a guessed one.
    """
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for key in ("agent_id", "current_agent_id", "id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


class BusClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BUS_URL,
        token: str | None = None,
        agent_id: str | None = None,
        own_agent_id: str | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token if token is not None else load_token()
        #: The agent this client intends to act on behalf of. Only when this
        #: differs from the caller's own identity (``own_agent_id``) is a
        #: delegation header sent -- see :meth:`_headers`.
        self.agent_id = agent_id
        #: The authenticated caller's own agent identity. When unknown (None)
        #: the client falls back to the non-delegation self-proof path: it
        #: publishes as the token's own agent and sends no delegation header.
        self.own_agent_id = own_agent_id
        self._transport = transport if transport is not None else HttpxTransport()
        #: whoami 解析缓存：None=未解析，str=身份，False=解析失败/无身份。
        self._whoami_cache: str | bool | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        delegate_to = self._delegate_target()
        if delegate_to:
            headers["X-Bus-On-Behalf-Of"] = delegate_to
        return headers

    def _delegate_target(self) -> str | None:
        """The on-behalf-of target, or None to publish as the token's own agent.

        The delegation header must only be sent when acting for a *different*
        agent. Sending it with the caller's own token makes agent-bus treat the
        request as a self-delegation and reject it with
        ``403 DELEGATION_NOT_PERMITTED`` -- the R1 production root cause where
        every research publish was silently rejected. We therefore only emit
        the header when the caller's own identity is known (``own_agent_id``)
        and differs from the configured ``agent_id``; otherwise we fall back to
        the non-delegation self-proof path (no header at all) and publish as
        the token's own agent.
        """
        if not self.agent_id:
            return None
        if not self.own_agent_id or self.own_agent_id == self.agent_id:
            return None
        return self.agent_id

    def whoami(self) -> str | None:
        """The authenticated caller's own agent id (GET /v1/agents/whoami), cached.

        Read-only identity read; never raises -- a failed read (bus down,
        endpoint absent, malformed body) degrades to None so the caller falls
        back to the non-delegation self-proof path instead of guessing.
        """
        if self._whoami_cache is None:
            try:
                status, payload = self._transport.request(
                    "GET",
                    f"{self.base_url}/v1/agents/whoami",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json_body=None,
                )
                candidates = _agent_id_candidates(payload) if 200 <= status < 300 else []
                self._whoami_cache = candidates[0] if len(set(candidates)) == 1 else False
            except Exception:
                self._whoami_cache = False
        return self._whoami_cache or None

    def _call(self, method: str, path: str, body: Any | None = None) -> Any:
        status, payload = self._transport.request(
            method, f"{self.base_url}{path}", headers=self._headers(), json_body=body
        )
        if status == CONFLICT_STATUS:
            raise BusConflict(status, str(payload))
        if not 200 <= status < 300:
            raise BusError(status, str(payload))
        return payload

    def post(self, path: str, body: dict[str, Any]) -> Any:
        """POST an arbitrary bus path. For endpoints outside the publish flow."""
        return self._call("POST", path, body)

    def create_channel(
        self,
        channel_id: str,
        *,
        delivery_mode: str = "fanout",
        visibility: str = "public",
        **extra: Any,
    ) -> dict[str, Any]:
        """Create (idempotently) a channel.

        agent-bus publishes only into *existing* channels (publish to a missing
        channel is 404 ``NOT_FOUND``), so a producer must ensure its channels
        before the first publish. Re-creating with the same configuration is an
        idempotent upsert; a conflicting configuration raises ``BusConflict``.
        """
        body = {"channel_id": channel_id, "delivery_mode": delivery_mode, "visibility": visibility}
        body.update(extra)
        result = self.post("/v1/channels", body)
        return result if isinstance(result, dict) else {}

    def get(self, path: str) -> Any:
        """GET an arbitrary bus path. For endpoints outside the messages/refs flows."""
        return self._call("GET", path)

    def protocols(self) -> dict[str, Any]:
        """The protocol registry: ``GET /v1/protocols`` -> ``{kind: {payload_schema,
        schema_digest, entity_role}}``.

        The registry is the SSoT for consumer-side payload validation. A
        consumer must derive its schema from this response at runtime (or
        mechanically verify ``schema_digest``), never hand-copy a schema or
        allowlist into the repository. Some gateways wrap the mapping under a
        ``protocols`` key; both shapes are tolerated here.
        """
        payload = self._call("GET", "/v1/protocols")
        if not isinstance(payload, dict):
            return {}
        inner = payload.get("protocols")
        if isinstance(inner, dict):
            return inner
        return payload

    def get_protocol(self, kind: str) -> dict[str, Any] | None:
        """The registry entry for one kind, or None when unregistered."""
        return self.protocols().get(kind)

    def publish(
        self,
        channel_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        refs: list[dict[str, str]] | None = None,
        entity_id: str | None = None,
        supersedes: str | None = None,
    ) -> PublishResult:
        body: dict[str, Any] = {
            "kind": kind,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        if refs:
            body["refs"] = refs
        if entity_id:
            body["entity_id"] = entity_id
        if supersedes:
            body["supersedes"] = supersedes
        result = self._call("POST", f"/v1/channels/{channel_id}/publish", body)
        return PublishResult(
            message_id=result["message_id"],
            entity_id=result.get("entity_id", result["message_id"]),
            channel_seq=result["channel_seq"],
            deduplicated=bool(result.get("deduplicated", False)),
        )

    def messages(
        self, channel_id: str, *, limit: int = 100, after_seq: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Messages after `after_seq` in channel order, plus the head seq.

        `after_seq` exists because the bus pages *ascending*: on a long-lived
        channel the plain call returns the oldest messages, and a reader that
        wants the newest must first learn head_seq (limit=1 is enough) and then
        re-read from just below it.
        """
        query = f"limit={limit}"
        if after_seq:
            query += f"&after_seq={after_seq}"
        result = self._call("GET", f"/v1/channels/{channel_id}/messages?{query}")
        return result.get("messages", []), int(result.get("head_seq", 0))

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        """Messages that reference `entity_id` -- how an answer finds its question."""
        result = self._call("GET", f"/v1/entities/{entity_id}/refs")
        return result.get("refs", [])

    def message(self, channel_id: str, message_id: str) -> dict[str, Any] | None:
        messages, _ = self.messages(channel_id, limit=1000)
        for candidate in messages:
            if candidate["message_id"] == message_id:
                return candidate
        return None

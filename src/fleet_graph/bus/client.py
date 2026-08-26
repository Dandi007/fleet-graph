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


class BusClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BUS_URL,
        token: str | None = None,
        agent_id: str | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token if token is not None else load_token()
        self.agent_id = agent_id
        self._transport = transport if transport is not None else HttpxTransport()

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        if self.agent_id:
            headers["X-Bus-On-Behalf-Of"] = self.agent_id
        return headers

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

    def messages(self, channel_id: str, *, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        result = self._call("GET", f"/v1/channels/{channel_id}/messages?limit={limit}")
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

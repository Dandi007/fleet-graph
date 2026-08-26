"""In-process LLM calls for pure-text roles.

Not every role needs a coding harness. A critic, a synthesiser, a persona in a
debate -- these read text and write text, and paying for an agent-runtime
subprocess to do it buys nothing. Those go through here; anything that touches
a repo goes through AgentRunNode instead.

Invariant 3: every call goes to the New API gateway on 127.0.0.1:15722, and
this module only ever names a *logical* model. Keys, channel laddering and
failover live in the gateway, not in orchestration code.

The gateway speaks two protocol faces. OpenAI (`/v1/chat/completions`) is the
default; Anthropic (`/v1/messages`) exists because some models are only sane
through it. Both are thin -- this is not an SDK.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

DEFAULT_GATEWAY_URL = "http://127.0.0.1:15722"

Face = Literal["openai", "anthropic"]


class EmptyCompletion(RuntimeError):
    """The model returned no visible text.

    Not a transport failure -- the call succeeded and billed. Reasoning models
    routinely spend the whole max_tokens budget on reasoning and emit nothing,
    and an empty string handed back to a critic or coordinator node is how a
    line silently stalls while looking healthy. Surfacing it lets the caller
    retry with a larger budget instead of propagating nothing.
    """

    def __init__(self, model: str, finish_reason: str, usage: dict[str, Any]) -> None:
        super().__init__(
            f"{model} returned no text (finish_reason={finish_reason!r}, usage={usage})"
        )
        self.model = model
        self.finish_reason = finish_reason
        self.usage = usage


class GatewayError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"gateway returned HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


def load_gateway_token(env: dict[str, str] | None = None) -> str:
    """Env-only, per plan.md. Never a literal, never read from a repo file."""
    env = os.environ if env is None else env
    token = env.get("FLEET_GRAPH_GATEWAY_TOKEN")
    if token:
        return token.strip()
    token_file = env.get("FLEET_GRAPH_GATEWAY_TOKEN_FILE")
    if token_file:
        return Path(token_file).read_text().strip()
    raise RuntimeError(
        "no gateway credential: set FLEET_GRAPH_GATEWAY_TOKEN or FLEET_GRAPH_GATEWAY_TOKEN_FILE"
    )


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]: ...


class HttpxTransport:
    def __init__(self, timeout: float = 300.0) -> None:
        import httpx

        # trust_env=False: the gateway is loopback and this host exports a
        # SOCKS proxy. Same trap the bus and MCP clients hit.
        self._client = httpx.Client(timeout=timeout, trust_env=False)

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        response = self._client.request(method, url, headers=headers, json=json_body)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text


@dataclass(frozen=True)
class TextResult:
    text: str
    model: str
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextSpec:
    """A pure-text role.

    `model` is a logical name the gateway resolves. Orchestration code must not
    know which vendor or key is behind it (invariant 3).
    """

    model: str
    system: str | None = None
    face: Face = "openai"
    temperature: float | None = None
    max_tokens: int = 4096
    # Set False only when an empty completion is genuinely acceptable.
    require_text: bool = True


class TextNode:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_GATEWAY_URL,
        token: str | None = None,
        transport: HttpTransport | None = None,
        label: str = "fleet-graph",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token if token is not None else load_gateway_token()
        self._transport = transport if transport is not None else HttpxTransport()
        self.label = label

    def complete(self, spec: TextSpec, prompt: str) -> TextResult:
        if spec.face == "anthropic":
            return self._anthropic(spec, prompt)
        return self._openai(spec, prompt)

    # --- protocol faces --------------------------------------------------

    def _openai(self, spec: TextSpec, prompt: str) -> TextResult:
        messages: list[dict[str, str]] = []
        if spec.system:
            messages.append({"role": "system", "content": spec.system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": spec.model,
            "messages": messages,
            "max_tokens": spec.max_tokens,
        }
        if spec.temperature is not None:
            body["temperature"] = spec.temperature

        payload = self._post("/v1/chat/completions", body)
        choices = payload.get("choices") or []
        if not choices:
            raise GatewayError(200, f"no choices in response: {payload}")
        text = (choices[0].get("message") or {}).get("content") or ""
        result = TextResult(
            text=text,
            model=str(payload.get("model", spec.model)),
            finish_reason=str(choices[0].get("finish_reason") or ""),
            usage=payload.get("usage") or {},
            raw=payload,
        )
        return _guard_empty(result, spec)

    def _anthropic(self, spec: TextSpec, prompt: str) -> TextResult:
        body: dict[str, Any] = {
            "model": spec.model,
            "max_tokens": spec.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if spec.system:
            body["system"] = spec.system
        if spec.temperature is not None:
            body["temperature"] = spec.temperature

        payload = self._post("/v1/messages", body)
        blocks = payload.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        result = TextResult(
            text=text,
            model=str(payload.get("model", spec.model)),
            finish_reason=str(payload.get("stop_reason") or ""),
            usage=payload.get("usage") or {},
            raw=payload,
        )
        return _guard_empty(result, spec)

    # --- internals -------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            # Attribution for DoD-6: gateway traffic must be traceable to
            # fleet-graph rather than showing up as anonymous.
            "X-Fleet-Graph-Component": self.label,
        }
        if path == "/v1/messages":
            headers["x-api-key"] = self._token
            headers["anthropic-version"] = "2023-06-01"

        status, payload = self._transport.request(
            "POST", f"{self.base_url}{path}", headers=headers, json_body=body
        )
        if not 200 <= status < 300:
            raise GatewayError(status, str(payload))
        if not isinstance(payload, dict):
            raise GatewayError(status, f"expected an object, got {type(payload).__name__}")
        return payload


def _guard_empty(result: TextResult, spec: TextSpec) -> TextResult:
    if spec.require_text and not result.text.strip():
        raise EmptyCompletion(result.model, result.finish_reason, result.usage)
    return result


__all__ = [
    "DEFAULT_GATEWAY_URL",
    "EmptyCompletion",
    "GatewayError",
    "TextNode",
    "TextResult",
    "TextSpec",
    "load_gateway_token",
]

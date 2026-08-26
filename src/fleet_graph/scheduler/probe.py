"""Gateway probes, per seat.

The babysitter's rule, and the reason it is not one health check:

    the probe must exercise the face the seat actually depends on, otherwise
    terra being alive while sol is dead lets a line ignite into a dead upstream

A probe against the wrong protocol face is a *false negative* -- it reports
healthy when the thing the line needs is broken. That is strictly worse than
not probing, because it converts "we don't know" into "we checked, it's fine".

So this is a seat -> probe mapping, not a global check. Two faces exist today:

- OpenAI-compatible `/v1/chat/completions`, used by the research seats
- `/v1/responses`, used by the subscription seats -- and it **must** stream,
  because the subscription channel only accepts streaming requests. A
  non-streaming probe there fails for the wrong reason.

A face is an endpoint *and* a credential. Gateway tokens are scoped to channel
groups, so the OpenAI-lane token cannot reach the subscription channels at all:
probing /v1/responses with it returns 503 "No available channel for model
gpt-5.6-sol under group anthropic" while the seat is perfectly healthy. That is
a false negative produced by the probe itself -- measured, not hypothesised --
which is why ProbeSpec carries the env var naming its lane. The babysitter
encodes the same thing as two separate curl header files.

Adding a seat means adding its probe. A seat with no registered probe is
refused ignition rather than silently defaulting to someone else's face.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_GATEWAY_URL = "http://127.0.0.1:15722"
PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProbeSpec:
    """One probe: the lane, where to post, what to send, and what proves life."""

    path: str
    body: dict[str, Any]
    # Env var holding the token for this lane. Tokens are scoped to channel
    # groups; the wrong one reports a healthy seat as dead.
    token_env: str
    # The response is healthy if any of these appear in it. Substring markers
    # rather than parsed JSON because the responses face answers with SSE.
    healthy_markers: tuple[str, ...]

    def is_healthy(self, raw: str) -> bool:
        return any(marker in raw for marker in self.healthy_markers)


def openai_probe(model: str, token_env: str = "FLEET_GRAPH_GATEWAY_TOKEN") -> ProbeSpec:
    return ProbeSpec(
        token_env=token_env,
        path="/v1/chat/completions",
        body={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16},
        healthy_markers=('"choices"',),
    )


def responses_probe(
    model: str, token_env: str = "FLEET_GRAPH_GATEWAY_TOKEN_RESPONSES"
) -> ProbeSpec:
    return ProbeSpec(
        token_env=token_env,
        path="/v1/responses",
        body={
            "model": model,
            # Required: the subscription channel only accepts streaming.
            "stream": True,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
            "reasoning": {"effort": "low"},
            "max_output_tokens": 16,
        },
        healthy_markers=('"status": "completed"', '"status":"completed"', '"object": "response"'),
    )


# Seat -> the face that seat actually depends on.
SEAT_PROBES: dict[str, ProbeSpec] = {
    "opencode-dsv4pro": openai_probe("deepseek-v4-pro"),
    "opencode-gpt-terra": responses_probe("gpt-5.6-terra"),
    "opencode-gpt-sol": responses_probe("gpt-5.6-sol"),
}


class UnknownSeat(LookupError):
    """No probe registered for this seat.

    Deliberately fatal to ignition: falling back to another seat's probe is
    exactly the false negative this module exists to prevent.
    """


def probe_for(seat: str) -> ProbeSpec:
    try:
        return SEAT_PROBES[seat]
    except KeyError as exc:
        raise UnknownSeat(
            f"no gateway probe registered for seat {seat!r}; register one rather than "
            "letting it borrow another seat's face"
        ) from exc


class ProbeTransport(Protocol):
    def post(self, url: str, body: dict[str, Any], token: str) -> tuple[int, str]: ...


class HttpxProbeTransport:
    """The real transport. Short timeout on purpose.

    A probe is asking "is the upstream answering right now"; waiting a long
    time for that answer is itself the answer, and a scheduler blocked on a
    hung gateway stops scheduling everything else.

    `trust_env=False` for the same reason every other loopback client in this
    repo sets it: this host exports a SOCKS proxy, and 127.0.0.1 would go
    through it and fail to connect at all.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        import httpx

        self._client = httpx.Client(timeout=timeout, trust_env=False)

    def post(self, url: str, body: dict[str, Any], token: str) -> tuple[int, str]:
        response = self._client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        return response.status_code, response.text


class MissingProbeCredential(RuntimeError):
    """The lane's env var is unset. Refuse rather than probe unauthenticated."""


class GatewayProber:
    def __init__(
        self,
        transport: ProbeTransport,
        *,
        base_url: str = DEFAULT_GATEWAY_URL,
        env: dict[str, str] | None = None,
    ) -> None:
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.env = env if env is not None else dict(os.environ)

    def check(self, seat: str) -> bool:
        """True when the seat's own dependency face answers healthily."""
        spec = probe_for(seat)
        token = self.env.get(spec.token_env)
        if not token:
            raise MissingProbeCredential(
                f"seat {seat!r} probes the {spec.token_env} lane, but that variable is unset; "
                "probing with another lane's token reports healthy seats as dead"
            )
        try:
            status, raw = self.transport.post(f"{self.base_url}{spec.path}", spec.body, token)
        except Exception:
            # Unreachable gateway is red, not an exception to propagate: the
            # caller's job is to decide about ignition, not to crash.
            return False
        if not 200 <= status < 300:
            return False
        return spec.is_healthy(raw)


__all__ = [
    "SEAT_PROBES",
    "GatewayProber",
    "HttpxProbeTransport",
    "MissingProbeCredential",
    "ProbeSpec",
    "UnknownSeat",
    "openai_probe",
    "probe_for",
    "responses_probe",
]

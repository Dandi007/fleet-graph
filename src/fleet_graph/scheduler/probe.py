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

Adding a seat means adding its probe. A seat with no registered probe is
refused ignition rather than silently defaulting to someone else's face.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_GATEWAY_URL = "http://127.0.0.1:15722"
PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProbeSpec:
    """One probe: where to post, what to send, and what proves it is alive."""

    path: str
    body: dict[str, Any]
    # The response is healthy if any of these appear in it. Substring markers
    # rather than parsed JSON because the responses face answers with SSE.
    healthy_markers: tuple[str, ...]

    def is_healthy(self, raw: str) -> bool:
        return any(marker in raw for marker in self.healthy_markers)


def openai_probe(model: str) -> ProbeSpec:
    return ProbeSpec(
        path="/v1/chat/completions",
        body={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16},
        healthy_markers=('"choices"',),
    )


def responses_probe(model: str) -> ProbeSpec:
    return ProbeSpec(
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
    def post(self, url: str, body: dict[str, Any]) -> tuple[int, str]: ...


class GatewayProber:
    def __init__(
        self,
        transport: ProbeTransport,
        *,
        base_url: str = DEFAULT_GATEWAY_URL,
    ) -> None:
        self.transport = transport
        self.base_url = base_url.rstrip("/")

    def check(self, seat: str) -> bool:
        """True when the seat's own dependency face answers healthily."""
        spec = probe_for(seat)
        try:
            status, raw = self.transport.post(f"{self.base_url}{spec.path}", spec.body)
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
    "ProbeSpec",
    "UnknownSeat",
    "openai_probe",
    "probe_for",
    "responses_probe",
]

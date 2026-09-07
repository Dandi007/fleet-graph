"""Egress resilience: transport failure is a first-class citizen, not a verdict.

The 2026-09-04 github egress incident window (board seq 2818) measured the
pattern this module exists for: three consecutive read probes failing with the
same GnuTLS handshake error, then a green window where every read/write
passed. Egress is intermittently degraded and green windows are usable but not
dependable -- so the engine must model the transport layer as its own failure
class instead of letting a network hiccup masquerade as a business verdict and
terminate the whole order.

Four pieces, each mapped to the spec clause it serves:

- **The transport closed set** (spec 交付面 2). A transport-class failure is
  exactly: DNS resolution failure, TCP connect failure/reset, TLS handshake
  failure (including GnuTLS ``The TLS connection was non-properly
  terminated``), HTTP 5xx, and the timeout family (exit 124). Everything else
  is not transport, and this module refuses to retry it.

- **Git failure classification** (spec 交付面 4). One classifier maps a remote
  git operation's stderr/exit into ``egress_transport`` / ``repo_rejected`` /
  ``repo_conflict``. Only a repo-layer outcome may terminate an order;
  conflicts keep their existing rework path; transport goes to the retry.

- **The layered failure record** (spec 交付面 3). ``PROVIDER_UNAVAILABLE``
  subdivides into three distinguishable root causes -- ``transport`` (network
  egress), ``execution`` (the command ran but its execution environment
  failed), ``business`` (a governance/semantic refusal) -- each with a
  disposition mapping. The legacy flat code stays a legal alias for
  ``transport`` so historical events keep reading.

- **Bounded exponential backoff** (spec 交付面 1). Every remote-touching git
  operation retries transport-class failures only: base 2s, factor 2, single
  delay capped at 60s, at most 5 attempts per stage, ±20% jitter, and the
  total backoff never exceeds the enclosing stage's run fence. Every attempt
  lands one evidence line -- ``{attempt, at, exit, stderr_tail}`` -- shaped
  exactly like the seq-2818 probe protocol, ready for the evidence volume.

What transport failure never does, by construction: fault the walker, mint a
business verdict, or close the resume path. Retries exhausted degrade to a
retryable failure record that keeps the generation-restart rights intact.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fleet_graph.state.run_artifacts import iso

# --- the transport closed set (交付面 2) ------------------------------------
#
# Matched case-insensitively against the failing operation's own words. The
# GnuTLS line is the measured incident fixture (seq 2818 / spec 交付面 4):
#   fatal: unable to access '…': GnuTLS, handshake failed: The TLS connection
#   was non-properly terminated.
#
# Deliberately absent: "unable to access" alone. It prefixes transport
# failures, but also local-path failures ("unable to access 'x': No such file
# or directory") that are not the network's words.
TRANSPORT_MARKERS: tuple[str, ...] = (
    # DNS resolution failure.
    "could not resolve host",
    "name or service not known",
    "temporary failure in name resolution",
    # TCP connect failure / reset.
    "failed to connect",
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    # TLS handshake failure, including the GnuTLS incident fixture.
    "the tls connection was non-properly terminated",
    "gnutls",
    "handshake failed",
    "ssl",
    "tls",
    # HTTP 5xx (git renders it as "The requested URL returned error: 5xx").
    "returned error: 5",
    # Timeout family.
    "timed out",
    "timeout",
)

#: The timeout exit-code family. A remote operation killed at its fence died
#: of the transport, whatever its stderr says.
TRANSPORT_EXITS: frozenset[int] = frozenset({124})

#: How much of a failing operation's stderr one evidence line carries.
STDERR_TAIL_CHARS = 400


def is_transport_failure(stderr: str, exit_code: int | None = None) -> bool:
    """Whether this failure belongs to the transport closed set.

    An exit in the timeout family is transport on its own; otherwise the
    operation's own words decide, against the closed marker set.
    """
    if exit_code is not None and exit_code in TRANSPORT_EXITS:
        return True
    text = (stderr or "").lower()
    return any(marker in text for marker in TRANSPORT_MARKERS)


# --- git failure classification (交付面 4) ----------------------------------

EGRESS_TRANSPORT = "egress_transport"
REPO_REJECTED = "repo_rejected"
REPO_CONFLICT = "repo_conflict"
#: A git failure that matched none of the three named classes. It reached the
#: repo layer (it is not transport) but the classifier refuses to name it
#: beyond that: fail closed, never retried, never guessed into a named class.
REPO_OTHER = "repo_other"


def classify_git_failure(stderr: str, exit_code: int | None = None) -> str:
    """One git remote operation's outcome, in the classifier's own vocabulary.

    Transport first: the network's words are the network's failure no matter
    what else the output contains. Then the repo layer's two named verdicts --
    the remote refused the push outright, or it demands the existing
    rebase/retry path. Anything else is unclassified repo-layer (``""``):
    non-transport, therefore never retried here, and never relabelled.
    """
    if is_transport_failure(stderr, exit_code):
        return EGRESS_TRANSPORT
    text = (stderr or "").lower()
    if "remote rejected" in text:
        return REPO_REJECTED
    if "non-fast-forward" in text or "fetch first" in text:
        return REPO_CONFLICT
    return ""


# --- the layered failure record (交付面 3) -----------------------------------

ROOT_CAUSE_TRANSPORT = "transport"
ROOT_CAUSE_EXECUTION = "execution"
ROOT_CAUSE_BUSINESS = "business"

#: What each root cause does next. Transport goes back to the wire with
#: backoff; an execution-environment failure goes through the R1-c
#: reconfigure channel; a business refusal belongs to governance / the human
#: gate. These are the three exits the control plane already distinguishes.
ROOT_CAUSE_DISPOSITION: dict[str, str] = {
    ROOT_CAUSE_TRANSPORT: "backoff_retry",
    ROOT_CAUSE_EXECUTION: "reconfigure",
    ROOT_CAUSE_BUSINESS: "human_gate",
}

#: The legacy flat code. Before the layering it named "git remote or GitHub
#: temporarily unavailable", so it remains a legal alias for the transport
#: root cause and historical events keep reading.
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

#: Codes whose meaning is a governance/semantic refusal: acceptance said no,
#: the gate said no, an unrecognized verdict, scope rejected the work. These
#: are verdicts about the work, never the wire.
BUSINESS_CODES: frozenset[str] = frozenset(
    {
        "ACCEPTANCE_FAILED",
        "GATE_REJECTED",
        "GATE_VERDICT_UNRECOGNIZED",
        "SCOPE_BOUNDARY_VIOLATION",
    }
)


def root_cause_for(failure_code: str, detail: str = "", exit_code: int | None = None) -> str:
    """One failure code plus its own words, mapped to one root cause.

    Transport evidence in the detail wins (a PROVIDER_UNAVAILABLE carrying a
    GnuTLS tail and one carrying an execution-environment message must land
    differently). A bare legacy code aliases to transport, which is what it
    always meant. Governance codes are business. Everything else ran and hit
    its execution environment: execution.
    """
    if is_transport_failure(detail, exit_code):
        return ROOT_CAUSE_TRANSPORT
    if failure_code in BUSINESS_CODES:
        return ROOT_CAUSE_BUSINESS
    if failure_code == PROVIDER_UNAVAILABLE:
        return ROOT_CAUSE_TRANSPORT
    return ROOT_CAUSE_EXECUTION


def layer_failure(
    failure_code: str, detail: str = "", exit_code: int | None = None
) -> dict[str, str]:
    """The structured failure record: the code, its root cause, its disposition."""
    cause = root_cause_for(failure_code, detail, exit_code)
    return {
        "code": failure_code,
        "root_cause": cause,
        "disposition": ROOT_CAUSE_DISPOSITION[cause],
    }


# --- bounded exponential backoff (交付面 1) ----------------------------------

#: Backoff policy values, fixed by the spec. Base 2s doubling per attempt with
#: a 60s per-delay cap, at most 5 attempts per stage, and each delay jittered
#: ±20% so a recovering provider does not see a synchronized retry stampede.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_DELAY_SECONDS = 60.0
MAX_ATTEMPTS_PER_STAGE = 5
BACKOFF_JITTER_FRACTION = 0.2


@dataclass(frozen=True)
class EgressPolicy:
    """The retry bounds one remote operation runs under."""

    base_seconds: float = BACKOFF_BASE_SECONDS
    factor: float = BACKOFF_FACTOR
    max_delay_seconds: float = BACKOFF_MAX_DELAY_SECONDS
    max_attempts: int = MAX_ATTEMPTS_PER_STAGE
    jitter_fraction: float = BACKOFF_JITTER_FRACTION


#: The engine-wide default policy. One instance, so every call site that does
#: not name its own bounds runs under exactly the spec's values.
DEFAULT_EGRESS_POLICY = EgressPolicy()


def backoff_delay(
    attempt: int,
    policy: EgressPolicy = DEFAULT_EGRESS_POLICY,
    rand: Callable[[], float] = random.random,
) -> float:
    """The wait before retrying after `attempt` consecutive transport failures.

    Exponential from the base, capped at the per-delay maximum, then jittered
    uniformly within ±`jitter_fraction`. ``rand`` returning exactly 0.5 (the
    deterministic test value) yields the raw exponential ladder.
    """
    raw = min(
        policy.base_seconds * (policy.factor ** (attempt - 1)),
        policy.max_delay_seconds,
    )
    jittered = raw * (1.0 + (2.0 * rand() - 1.0) * policy.jitter_fraction)
    return max(0.0, jittered)


class TransportExhausted(RuntimeError):
    """Transport-class failures consumed the retry budget.

    This is the "retryable fault" the order lands on when the egress stays
    dark past the fence: it never faults the walker and never closes the
    resume path. The walker routes it as a bounded retryable failure whose
    record keeps ``retryable`` true, so the generation-restart door the
    control plane already owns stays open.
    """

    def __init__(self, message: str, *, attempts: int, last_stderr: str = "") -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_stderr = last_stderr


class EgressRepoError(RuntimeError):
    """A remote operation failed at the repo layer, not the transport.

    The remote (or the local repo) spoke: the push was rejected, the chain
    conflicts, the ref is not there. These are never retried here -- retrying
    a verdict just amplifies it. ``classification`` names the git-layer class
    the classifier assigned.
    """

    def __init__(
        self,
        message: str,
        *,
        op_name: str,
        classification: str,
        exit_code: int,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.op_name = op_name
        self.classification = classification or REPO_OTHER
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True)
class RemoteResult:
    """One remote operation's outcome, in the shape the retry executor reads.

    Vendored helpers raise instead of returning processes; their call sites
    wrap them into this. ``returncode`` mirrors CompletedProcess so either
    shape feeds ``retry_remote`` unchanged.
    """

    exit_code: int
    stderr: str = ""
    value: Any = None

    @property
    def returncode(self) -> int:
        return self.exit_code


def evidence_line(
    *,
    attempt: int,
    at: str,
    exit_code: int,
    stderr: str,
    op_name: str = "",
) -> dict[str, Any]:
    """One probe-protocol evidence line: `{attempt, at, exit, stderr_tail}`.

    Shaped like the seq-2818 probe protocol so a line can enter the evidence
    volume directly. ``op`` and ``class``/``root_cause`` are additive fields:
    which operation spoke, and -- for a failed attempt -- which layer owns
    the failure.
    """
    classification = "" if exit_code == 0 else classify_git_failure(stderr, exit_code)
    return {
        "attempt": attempt,
        "at": at,
        "exit": exit_code,
        "stderr_tail": (stderr or "")[-STDERR_TAIL_CHARS:],
        "op": op_name,
        "class": classification,
        "root_cause": ROOT_CAUSE_TRANSPORT if classification == EGRESS_TRANSPORT else "",
    }


def retry_remote(
    operation: Callable[[], Any],
    *,
    op_name: str = "",
    policy: EgressPolicy = DEFAULT_EGRESS_POLICY,
    fence_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    now: Callable[[], str] | None = None,
    evidence: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Run one remote-touching git operation under the bounded backoff.

    The operation returns anything with ``returncode`` and ``stderr`` (a
    ``CompletedProcess`` or a ``RemoteResult``). On success its ``value``
    (or the result itself, for processes) comes back. Every attempt lands one
    evidence line through ``evidence``.

    - Transport-class failures retry with exponential backoff and jitter,
      never sleeping the cumulative budget past ``fence_seconds`` (the
      enclosing stage's run fence).
    - Any other failure raises :class:`EgressRepoError` immediately: the repo
      layer gave a verdict, and retrying a verdict amplifies it.
    - Budget exhausted raises :class:`TransportExhausted`, the retryable
      fault that keeps resume rights.
    """
    timestamp = now if now is not None else (lambda: iso(time.time()))
    spent = 0.0
    last_stderr = ""
    attempts = 0
    for attempt in range(1, policy.max_attempts + 1):
        attempts = attempt
        result = operation()
        exit_code = int(getattr(result, "returncode", 1))
        stderr = str(getattr(result, "stderr", "") or "")
        last_stderr = stderr
        if evidence is not None:
            evidence(
                evidence_line(
                    attempt=attempt,
                    at=timestamp(),
                    exit_code=exit_code,
                    stderr=stderr,
                    op_name=op_name,
                )
            )
        if exit_code == 0:
            return getattr(result, "value", result)
        classification = classify_git_failure(stderr, exit_code)
        if classification != EGRESS_TRANSPORT:
            raise EgressRepoError(
                f"git {op_name} failed ({classification or REPO_OTHER}): "
                f"{stderr.strip()[:STDERR_TAIL_CHARS] or f'exit {exit_code}'}",
                op_name=op_name,
                classification=classification,
                exit_code=exit_code,
                stderr=stderr,
            )
        if attempt >= policy.max_attempts:
            break
        delay = backoff_delay(attempt, policy, rand)
        if fence_seconds is not None and spent + delay > fence_seconds:
            break
        sleep(delay)
        spent += delay
    raise TransportExhausted(
        f"remote operation {op_name!r} still failing after {attempts} "
        f"transport-failed attempts; last: {last_stderr.strip()[:STDERR_TAIL_CHARS]}",
        attempts=attempts,
        last_stderr=last_stderr,
    )


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_FACTOR",
    "BACKOFF_JITTER_FRACTION",
    "BACKOFF_MAX_DELAY_SECONDS",
    "BUSINESS_CODES",
    "DEFAULT_EGRESS_POLICY",
    "EGRESS_TRANSPORT",
    "MAX_ATTEMPTS_PER_STAGE",
    "PROVIDER_UNAVAILABLE",
    "REPO_CONFLICT",
    "REPO_OTHER",
    "REPO_REJECTED",
    "ROOT_CAUSE_BUSINESS",
    "ROOT_CAUSE_DISPOSITION",
    "ROOT_CAUSE_EXECUTION",
    "ROOT_CAUSE_TRANSPORT",
    "STDERR_TAIL_CHARS",
    "TRANSPORT_EXITS",
    "EgressPolicy",
    "EgressRepoError",
    "RemoteResult",
    "TransportExhausted",
    "backoff_delay",
    "classify_git_failure",
    "evidence_line",
    "is_transport_failure",
    "layer_failure",
    "retry_remote",
    "root_cause_for",
]

"""The minimal agent-run adapter: session policy + agent-run argv + Stop parsing.

This is the thin, dependency-free layer between the engine and the ``agent-run``
CLI. It owns three mechanical concerns, nothing more:

- ``schema_for`` / ``ROLES``: the six roles and the output schema each call
  expects (protocol §2..§6, §12).
- ``SessionPolicy`` / ``resolve_session_policy``: per-role resume/fresh session
  policy with the compact threshold, defaulting to protocol §0.8 and overridden
  per-field from an enroll ``sessions`` map.
- ``build_argv`` / ``parse_stop`` / ``run_agent``: build the fixed-shape argv
  list, run it through an injectable runner, and turn the captured stdout into
  an :class:`AgentResult` carrying one of the :class:`FailureCode` reasons.

Two invariants hold across the module, mirroring ``gitgate.py``:

1. Every argv is a plain ``list[str]`` executed with ``shell=False`` -- no
   command string ever reaches a shell, and the role / harness / run-id tokens
   are whitelisted (``^[A-Za-z0-9._-]+$``) so a hostile value cannot smuggle a
   second command.
2. Failures are data, not exceptions: an output that does not round-trip to a
   valid protocol object yields an ``AgentResult`` whose ``failure_code`` names
   the reason (protocol §0.2 -- a nonzero exit is that agent's failure, and the
   engine does not re-run it). Only genuinely unexpected runner failures (e.g.
   timeout, handled below) surface as :class:`AgentRunTimeout`.

The module imports only ``fleet_graph.minimal.protocol``. It does not build the
engine graph, render prompts, or touch the filesystem on its own: the runner
(and therefore any subprocess) is injected here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from fleet_graph.minimal.protocol import (
    SCHEMA_GOAL_REVIEW,
    SCHEMA_GOAL_TURN,
    SCHEMA_IMPL,
    SCHEMA_MERGE,
    SCHEMA_REVIEW,
    SCHEMA_RUNTIME_ERROR,
    SCHEMA_SCRIBE,
    extract_protocol_object,
    validate,
)

# ---------------------------------------------------------------------------
# Roles and schema mapping (protocol §2..§6, §12)
# ---------------------------------------------------------------------------

ROLES: tuple[str, ...] = ("goal", "impl", "cr", "fr", "merge", "scribe")

_GOAL_CALL_KINDS = ("turn", "review")

# The schema name each (role, call_kind) pair produces. Only ``goal`` has more
# than one call kind; every other role maps onto a single schema regardless of
# ``call_kind``. Declared once so the dispatch stays a lookup, not an if/else
# tree, and so ``schema_for`` cannot drift from the protocol constants above.
_SCHEMA_BY_ROLE: dict[str, dict[str | None, str]] = {
    "goal": {"turn": SCHEMA_GOAL_TURN, "review": SCHEMA_GOAL_REVIEW},
    "impl": {None: SCHEMA_IMPL},
    "cr": {None: SCHEMA_REVIEW},
    "fr": {None: SCHEMA_REVIEW},
    "merge": {None: SCHEMA_MERGE},
    "scribe": {None: SCHEMA_SCRIBE},
}


def schema_for(role: str, call_kind: str | None = None) -> str:
    """The output schema name for ``role`` at ``call_kind``.

    ``goal`` requires a ``call_kind`` of ``"turn"`` or ``"review"``; the other
    five roles take a single schema and ignore ``call_kind`` (None). An
    unknown role or an unknown goal call_kind raises ``ValueError``.
    """
    table = _SCHEMA_BY_ROLE.get(role)
    if table is None:
        raise ValueError(f"unknown role {role!r} (expected one of {_render(ROLES)})")
    if role == "goal":
        schema = table.get(call_kind)
        if schema is None:
            raise ValueError(
                f"unknown call_kind {call_kind!r} for role 'goal' "
                f"(expected one of {_render(_GOAL_CALL_KINDS)})"
            )
        return schema
    return table[None]


# ---------------------------------------------------------------------------
# Session policy (protocol §0.8)
# ---------------------------------------------------------------------------

_MODES = ("resume", "fresh")


@dataclass(frozen=True)
class SessionPolicy:
    """Whether a role's session is resumed (with a compact threshold) or fresh."""

    mode: str
    compact_at: float | None = None


DEFAULT_SESSION_POLICIES: dict[str, SessionPolicy] = {
    "goal": SessionPolicy("resume", 0.7),
    "impl": SessionPolicy("resume", 0.7),
    "cr": SessionPolicy("resume", 0.8),
    "fr": SessionPolicy("resume", 0.8),
    "merge": SessionPolicy("fresh", None),
    "scribe": SessionPolicy("resume", 0.6),
}


def _validate_policy(role: str, mode: str, compact_at: float | None) -> None:
    if mode not in _MODES:
        raise ValueError(
            f"session policy for role {role!r}: mode={mode!r} not in {_render(_MODES)}"
        )
    if mode == "fresh" and compact_at is not None:
        raise ValueError(
            f"session policy for role {role!r}: mode='fresh' cannot carry compact_at "
            f"(got {compact_at!r})"
        )
    if compact_at is not None and not (
        isinstance(compact_at, (int, float))
        and not isinstance(compact_at, bool)
        and 0 < compact_at <= 1
    ):
        raise ValueError(
            f"session policy for role {role!r}: compact_at={compact_at!r} must be in (0, 1]"
        )


def resolve_session_policy(role: str, overrides: dict[str, dict[str, Any]] | None) -> SessionPolicy:
    """Resolve ``role``'s session policy from the defaults plus per-field overrides.

    ``overrides`` is the enroll ``sessions`` map (role name -> partial policy).
    Each key present in a role's override replaces that single field; absent
    fields fall back to ``DEFAULT_SESSION_POLICIES``. Because ``compact_at`` is
    inherited per-field, switching a role to ``"fresh"`` must clear it
    explicitly (``{"mode": "fresh", "compact_at": None}``); a fresh policy that
    still carries a compact threshold is rejected.
    """
    default = DEFAULT_SESSION_POLICIES.get(role)
    if default is None:
        raise ValueError(f"unknown role {role!r} (expected one of {_render(ROLES)})")

    override = (overrides or {}).get(role, {})
    if not isinstance(override, dict):
        raise ValueError(f"sessions[{role!r}] must be an object, got {type(override).__name__}")

    mode = override.get("mode", default.mode)
    compact_at = override.get("compact_at", default.compact_at)
    _validate_policy(role, mode, compact_at)
    return SessionPolicy(mode=mode, compact_at=compact_at)


def resume_args(
    policy: SessionPolicy,
    last_run_id: str | None,
    *,
    session_root: str,
) -> tuple[str | None, float | None]:
    """The ``(resume_dir, compact_at)`` for one stage, or ``(None, None)`` when fresh.

    A stage resumes only when its policy mode is ``"resume"`` and a prior run
    exists (``last_run_id`` is not None); the directory is
    ``<session_root>/<last_run_id>`` and the threshold is the policy's own
    ``compact_at``. Otherwise the call is that session's first, so no
    ``--resume`` and no ``--compact-at`` are emitted.
    """
    if policy.mode != "resume" or last_run_id is None:
        return None, None
    root = session_root.rstrip("/")
    resume_dir = f"{root}/{last_run_id}" if root else last_run_id
    return resume_dir, policy.compact_at


# ---------------------------------------------------------------------------
# The agent-run invocation
# ---------------------------------------------------------------------------

# role / harness / run-id are argv tokens that agent-run will forward verbatim;
# restricting them to this alphabet keeps a hostile value from injecting a
# second command. AgentCall roles are engine-controlled, but the worktree is
# agent-written, so nothing user-controlled may be trusted without this gate.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, kw_only=True)
class AgentCall:
    """Everything needed to invoke ``agent-run`` once (protocol §9 fixed args)."""

    role: str
    call_kind: str | None
    run_id: str
    cwd: str
    session_root: str
    timeout_s: int
    output_schema_json: str
    harness: str | None = None
    resume_dir: str | None = None
    compact_at: float | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if self.harness is None:
            object.__setattr__(self, "harness", self.role)


def build_argv(call: AgentCall) -> list[str]:
    """The fixed-shape, fixed-order argv for one ``agent-run`` call.

    ``agent-run --role R --harness H --session-root S --run-id ID
    --output-schema JSON --isolation full --timeout N --cwd W``, then
    ``--resume DIR``, ``--compact-at RATIO`` and/or ``--model M`` when present.
    ``compact_at`` without ``resume_dir`` raises (a fresh run carries no
    threshold, mirroring ``_validate_policy``). The three whitelisted tokens are
    validated here (see ``_SAFE_TOKEN_RE``) and ``timeout_s`` must be a positive
    integer; otherwise ``ValueError``.
    """
    for token_name, token in (
        ("role", call.role),
        ("harness", call.harness),
        ("run_id", call.run_id),
    ):
        if not isinstance(token, str) or not _SAFE_TOKEN_RE.fullmatch(token):
            raise ValueError(
                f"{token_name} {token!r} does not match the whitelist {_SAFE_TOKEN_RE.pattern!r}"
            )

    timeout_s = call.timeout_s
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise ValueError(f"timeout_s must be a positive integer, got {timeout_s!r}")

    if call.compact_at is not None and call.resume_dir is None:
        raise ValueError(
            f"compact_at={call.compact_at!r} requires a resume_dir; "
            "a fresh run carries no threshold"
        )

    argv = [
        "agent-run",
        "--role",
        call.role,
        "--harness",
        call.harness,
        "--session-root",
        call.session_root,
        "--run-id",
        call.run_id,
        "--output-schema",
        call.output_schema_json,
        "--isolation",
        "full",
        "--timeout",
        str(timeout_s),
        "--cwd",
        call.cwd,
    ]
    if call.resume_dir is not None:
        argv += ["--resume", call.resume_dir]
    if call.compact_at is not None:
        argv += ["--compact-at", str(call.compact_at)]
    if call.model is not None:
        argv += ["--model", call.model]
    return argv


# ---------------------------------------------------------------------------
# Stop parsing
# ---------------------------------------------------------------------------


class FailureCode:
    """Machine-readable reasons an agent run can fail (protocol §0.2).

    Downstream DDs reference these constants in event payloads; they must not be
    typed as string literals.
    """

    NONZERO_EXIT = "nonzero_exit"
    INVALID_OUTPUT = "invalid_output"
    NO_OBJECT = "no_object"
    SCHEMA_MISMATCH = "schema_mismatch"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class AgentResult:
    """The outcome of one agent run (a normal value, never a thrown verdict)."""

    ok: bool
    stop: str | None = None
    obj: dict[str, Any] | None = None
    failure_code: str | None = None
    detail: str | None = None
    argv: list[str] = field(default_factory=list)
    exit_code: int | None = None


def _schema_prefix(schema_name: str) -> str:
    """The ``extract_protocol_object`` prefix for ``schema_name`` (drop /version)."""
    return schema_name.rsplit("/", 1)[0]


def parse_stop(stdout: str, expected_schema: str, exit_code: int) -> AgentResult:
    """Grade an agent run's stdout against ``expected_schema`` (protocol §0.2).

    * Non-zero exit: if stdout carries a ``runtime.error/1`` object its
      ``detail`` becomes the result detail and the failure code is ``timeout``
      or ``invalid_output`` by that object's ``stop``; otherwise
      ``nonzero_exit``.
    * Zero exit: the last object whose ``schema`` starts with the expected
      prefix is validated field-for-field. No object -> ``no_object``; the
      object names a different schema -> ``schema_mismatch``; a failing
      ``validate`` -> ``invalid_output`` with the full field-level text; a
      pass -> ``ok=True`` with ``stop`` taken from the object.
    """
    if exit_code != 0:
        return _parse_runtime_failure(stdout, exit_code)

    obj = extract_protocol_object(stdout, _schema_prefix(expected_schema))
    if obj is None:
        return AgentResult(
            ok=False,
            failure_code=FailureCode.NO_OBJECT,
            detail=f"no protocol object matching {expected_schema!r} found in agent output",
            exit_code=exit_code,
        )

    actual_schema = obj.get("schema")
    if actual_schema != expected_schema:
        return AgentResult(
            ok=False,
            failure_code=FailureCode.SCHEMA_MISMATCH,
            detail=f"agent output schema {actual_schema!r} does not match {expected_schema!r}",
            obj=obj,
            exit_code=exit_code,
        )

    validation = validate(obj, expected_schema)
    if not validation.ok:
        return AgentResult(
            ok=False,
            failure_code=FailureCode.INVALID_OUTPUT,
            detail="; ".join(validation.errors),
            obj=obj,
            exit_code=exit_code,
        )

    return AgentResult(
        ok=True,
        stop=obj.get("stop"),
        obj=obj,
        exit_code=exit_code,
    )


def _parse_runtime_failure(stdout: str, exit_code: int) -> AgentResult:
    """The non-zero-exit branch: surface the runtime's own error object if any."""
    err = extract_protocol_object(stdout, _schema_prefix(SCHEMA_RUNTIME_ERROR))
    if err is None:
        return AgentResult(
            ok=False,
            failure_code=FailureCode.NONZERO_EXIT,
            detail=f"agent-run exited with non-zero exit code {exit_code}",
            exit_code=exit_code,
        )
    detail = err.get("detail")
    if not isinstance(detail, str):
        detail = f"agent-run exited with non-zero exit code {exit_code}"
    failure_code = (
        FailureCode.TIMEOUT if err.get("stop") == "timeout" else FailureCode.INVALID_OUTPUT
    )
    return AgentResult(
        ok=False,
        failure_code=failure_code,
        detail=detail,
        exit_code=exit_code,
    )


# ---------------------------------------------------------------------------
# Running through an injectable runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    """The raw outcome of one invocation: exit code plus stdout/stderr."""

    exit_code: int
    stdout: str
    stderr: str


class AgentRunner(Protocol):
    """Every ``agent-run`` call funnels through this, argv-list only."""

    def run(self, argv: list[str], cwd: str, timeout_s: int) -> Completed:
        """Run ``argv`` in ``cwd``; raise :class:`AgentRunTimeout` on timeout."""
        ...  # pragma: no cover - protocol body


class AgentRunTimeout(Exception):
    """A runner timed out; carries enough context to be mapped to a failure code."""

    def __init__(self, timeout_s: int, argv: list[str], cwd: str) -> None:
        super().__init__(f"agent-run timed out after {timeout_s}s: {' '.join(argv)} in {cwd}")
        self.timeout_s = timeout_s
        self.argv = list(argv)
        self.cwd = cwd


class SubprocessRunner:
    """Default :class:`AgentRunner`: list-argv ``subprocess.run`` with ``shell=False``."""

    def run(self, argv: list[str], cwd: str, timeout_s: int) -> Completed:
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentRunTimeout(timeout_s, argv, cwd) from exc
        return Completed(proc.returncode, proc.stdout, proc.stderr)


def run_agent(call: AgentCall, *, runner: AgentRunner) -> AgentResult:
    """Run one ``AgentCall`` and grade its output (the single orchestration seam)."""
    argv = build_argv(call)
    expected_schema = schema_for(call.role, call.call_kind)
    try:
        completed = runner.run(argv, cwd=call.cwd, timeout_s=call.timeout_s)
    except AgentRunTimeout:
        return AgentResult(
            ok=False,
            failure_code=FailureCode.TIMEOUT,
            detail=f"agent-run timed out after {call.timeout_s}s",
            argv=argv,
            exit_code=-1,
        )
    result = parse_stop(completed.stdout, expected_schema, completed.exit_code)
    return replace(result, argv=argv)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(values: tuple[str, ...]) -> str:
    return "{" + ", ".join(values) + "}"


__all__ = [
    "DEFAULT_SESSION_POLICIES",
    "ROLES",
    "AgentCall",
    "AgentResult",
    "AgentRunTimeout",
    "AgentRunner",
    "Completed",
    "FailureCode",
    "SessionPolicy",
    "SubprocessRunner",
    "build_argv",
    "parse_stop",
    "resolve_session_policy",
    "resume_args",
    "run_agent",
    "schema_for",
]

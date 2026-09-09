"""stagerunner.py — run one agent stage end-to-end (pure orchestration, zero IO).

The 12 merged minimal modules are leaves on purpose: gitgate checks git,
prompts renders prompts, agentrun builds argv and parses Stop, protocol
validates schemas, events writes logs — but nothing wires them into one agent
stage. This is that seam, and the necessary stepping stone before the LangGraph
graph (protocol §0.10's per-node check table, §0.2's "nonzero exit = the agent
failed, the engine does not re-run", §0.1's invalid-output rule, GO-28's
push + remote-tip handoff).

``run_stage`` runs the fixed sequence (protocol §0.10): pre handoff gate ->
render prompts -> invoke the agent -> (nonzero exit -> failed) -> extract +
validate the Stop object -> post handoff gate -> review-unchanged check for
``cr``/``fr`` -> success. Every side effect (git queries, the agent process,
the event writes) is injected or deferred:

* ``git_runner`` is the ``gitgate.GitRunner`` the gates already use; nothing
  here shells out.
* ``agent_invoker`` is the single agent seam: it receives the argv (built by
  ``agentrun.build_argv`` — never hand-rolled here) plus the rendered prompts
  and returns ``(exit_code, stdout)``.
* the ``StageOutcome.events`` list is *pending* ``(kind, payload)`` pairs that
  the caller hands to ``events.EventLog``; stagerunner never touches the disk.

The request carries the eight semantic handoff fields, plus the argv plumbing
(``session_root`` / ``timeout_s`` / ``resume_dir`` / ``compact_at`` / ``model``) that
``agentrun.build_argv`` needs — those are engine config the caller already has
and defaults keep them optional for the pure-orchestration tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from fleet_graph.minimal import agentrun, gitgate, prompts, protocol

STAGES: tuple[str, ...] = ("impl", "cr", "fr", "goal_turn", "goal_review", "merge", "scribe")

# stage -> (role, call_kind); mirrors agentrun._SCHEMA_BY_ROLE but keyed from
# the stage name the DD loop uses (goal_turn / goal_review are the Goal Agent's
# two call kinds, the rest map one-to-one onto a role).
_ROLE_BY_STAGE: dict[str, tuple[str, str | None]] = {
    "impl": ("impl", None),
    "cr": ("cr", None),
    "fr": ("fr", None),
    "goal_turn": ("goal", "turn"),
    "goal_review": ("goal", "review"),
    "merge": ("merge", None),
    "scribe": ("scribe", None),
}

# The DD loop's agent stages (ddflow.STAGES minus the programmatic
# "acceptance"); those finish with a ``dd.stage.finished`` event.
_DD_INTERNAL_STAGES: frozenset[str] = frozenset({"impl", "cr", "fr", "goal_review", "merge"})

_REVIEW_STAGES: frozenset[str] = frozenset({"cr", "fr"})

# The failure code for the GO-25 review rule: a reviewer must leave the
# worktree and remote tip exactly as it found them (protocol §0.10).
REVIEW_CHANGED_CODE = "review_changed_code"


def _schema_prefix(schema_name: str) -> str:
    """The ``extract_protocol_object`` prefix for ``schema_name`` (drop /version)."""
    return schema_name.rsplit("/", 1)[0]


@dataclass(frozen=True)
class StageRequest:
    """Everything one agent stage needs; all IO is injected/delayed.

    The semantic fields are the handoff (stage, run_id, the ready-made
    ``in_obj`` from ``prompts.build_*_in``, the repos to gate, the output
    schema, the session policy, the cwd, whether this is the session's first
    call). The remaining fields feed ``agentrun.build_argv`` and default out so
    pure-orchestration callers do not have to spell engine plumbing.
    """

    stage: str
    run_id: str
    in_obj: dict[str, Any]
    repos: list[gitgate.RepoRef]
    expected_schema: str
    policy: agentrun.SessionPolicy
    cwd: str
    is_first_call: bool
    session_root: str = ""
    timeout_s: int = 300
    resume_dir: str | None = None
    compact_at: float | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(
                f"unknown stage {self.stage!r}; expected one of " + "{" + ", ".join(STAGES) + "}"
            )


@dataclass(frozen=True)
class StageOutcome:
    """The result of one stage: a success object or a failure reason plus events.

    ``events`` is a flat list of *pending* ``(kind, payload)`` pairs — the
    caller writes them via ``events.EventLog`` (so stagerunner stays zero-IO).
    ``gate_failures`` carries the ``gitgate.GateFailure`` items behind a pre /
    post handoff gate failure (empty otherwise).
    """

    ok: bool
    stop: str | None
    obj: dict[str, Any] | None
    invalid_reason: str | None
    gate_failures: list[gitgate.GateFailure]
    events: list[tuple[str, dict[str, Any]]]


class AgentInvoker(Protocol):
    """The injected agent seam: run one agent call, return ``(exit_code, stdout)``.

    Receives the argv built by ``agentrun.build_argv`` plus the rendered
    system prompt (``None`` when this call does not send one) and the user
    prompt. Production wires this to ``agentrun.run_agent`` with a real runner;
    tests swap in a fake to assert exactly one call (or zero, on a pre-gate
    failure).
    """

    def __call__(
        self,
        argv: list[str],
        *,
        system_prompt: str | None,
        user_prompt: str,
    ) -> tuple[int, str]: ...  # pragma: no cover - protocol body


def _gate_failure_dict(failure: gitgate.GateFailure) -> dict[str, str]:
    return {"repo": failure.repo, "code": failure.code, "detail": failure.detail}


def _runtime_failure(stdout: str, exit_code: int) -> tuple[str, str]:
    """The non-zero-exit ``(failure_code, detail)``, graded like ``agentrun.parse_stop``.

    Protocol §0.2: no ``runtime.error/1`` object -> ``nonzero_exit``; an object
    whose ``stop`` is ``"timeout"`` -> ``timeout``; any other runtime error ->
    ``invalid_output``. The detail is the runtime's own, else a summary.
    """
    err = protocol.extract_protocol_object(stdout, _schema_prefix(protocol.SCHEMA_RUNTIME_ERROR))
    if err is None:
        return (
            agentrun.FailureCode.NONZERO_EXIT,
            f"agent exited with non-zero exit code {exit_code}",
        )
    detail = err.get("detail")
    if not isinstance(detail, str):
        detail = f"agent exited with non-zero exit code {exit_code}"
    failure_code = (
        agentrun.FailureCode.TIMEOUT
        if err.get("stop") == "timeout"
        else agentrun.FailureCode.INVALID_OUTPUT
    )
    return failure_code, detail


def _snapshot(
    repos: list[gitgate.RepoRef], runner: gitgate.GitRunner
) -> dict[str, tuple[str | None, str | None]]:
    """The pre-agent (head, remote tip) pair per repo, for the review rule."""
    snap: dict[str, tuple[str | None, str | None]] = {}
    for repo in repos:
        status = gitgate.worktree_status(repo.worktree, runner=runner)
        tip = gitgate.remote_tip(repo.worktree, repo.remote, repo.branch, runner=runner)
        snap[repo.repo_id] = (status.head, tip)
    return snap


def _build_argv(req: StageRequest, role: str, call_kind: str | None) -> list[str]:
    """The full argv via ``agentrun.build_argv`` (never a hand-rolled list)."""
    output_schema_json = json.dumps(protocol.describe_schema(req.expected_schema))
    call = agentrun.AgentCall(
        role=role,
        call_kind=call_kind,
        run_id=req.run_id,
        cwd=req.cwd,
        session_root=req.session_root,
        timeout_s=req.timeout_s,
        output_schema_json=output_schema_json,
        resume_dir=req.resume_dir,
        compact_at=req.compact_at,
        model=req.model,
    )
    return agentrun.build_argv(call)


def _review_changed_failures(
    repos: list[gitgate.RepoRef],
    pre_snapshot: dict[str, tuple[str | None, str | None]],
    runner: gitgate.GitRunner,
) -> list[gitgate.GateFailure]:
    """Whether a reviewer changed worktree/head or remote tip (GO-25 / §0.10)."""
    failures: list[gitgate.GateFailure] = []
    for repo in repos:
        status = gitgate.worktree_status(repo.worktree, runner=runner)
        tip = gitgate.remote_tip(repo.worktree, repo.remote, repo.branch, runner=runner)
        pre_head, pre_tip = pre_snapshot[repo.repo_id]
        if status.head != pre_head or tip != pre_tip:
            failures.append(
                gitgate.GateFailure(
                    repo=repo.repo_id,
                    code=REVIEW_CHANGED_CODE,
                    detail=(
                        f"review must not change the code: worktree/tip of "
                        f"{repo.worktree} changed during the stage"
                    ),
                )
            )
    return failures


def _finished_kind(stage: str) -> str | None:
    """The success event for ``stage``: dd.stage.finished / goal.turn.finished / none."""
    if stage == "goal_turn":
        return "goal.turn.finished"
    if stage in _DD_INTERNAL_STAGES:
        return "dd.stage.finished"
    return None  # scribe: only agent.exited (observations are the caller's)


def _dd_finished_payload(req: StageRequest, stop: str, obj: dict[str, Any]) -> dict[str, Any]:
    """dd.stage.finished payload: stage/stop/run_id plus the flat output fields."""
    payload: dict[str, Any] = {"stage": req.stage, "stop": stop, "run_id": req.run_id}
    payload.update({k: v for k, v in obj.items() if k not in ("schema", "stop")})
    return payload


def _goal_turn_payload(req: StageRequest, obj: dict[str, Any]) -> dict[str, Any]:
    """goal.turn.finished payload: the output object (minus its schema literal)."""
    return {"run_id": req.run_id, **{k: v for k, v in obj.items() if k != "schema"}}


def run_stage(
    req: StageRequest,
    *,
    git_runner: gitgate.GitRunner,
    agent_invoker: AgentInvoker,
) -> StageOutcome:
    """Run one agent stage through the fixed gate->prompt->agent->validate->gate order.

    The order is protocol §0.10's per-node check table, mechanically:

    1. **Pre handoff gate**: ``gitgate.check_handoff``. A failure yields
       ``ok=False`` with an ``agent.invalid_output`` event (``phase: "pre"``)
       and the agent is **not** invoked.
    2. **Prompts**: system prompt per ``prompts.needs_system_prompt``, user
       prompt every call.
    3. **Agent**: argv via ``agentrun.build_argv``, then the injected invoker.
    4. **Non-zero exit**: ``ok=False`` + ``agent.failed``; never re-run (the
       invoker is called exactly once).
    5. **Extract + validate**: ``protocol.extract_protocol_object`` then
       ``protocol.validate``; a miss yields ``agent.invalid_output``.
    6. **Post handoff gate**: ``check_handoff`` again; ``cr``/``fr`` additionally
       must keep worktree and remote tip unchanged (a reviewer that edits code
       is invalid).
    7. **Success**: ``agent.exited``, then ``dd.stage.finished`` (DD-internal
       stages) or ``goal.turn.finished`` (``goal_turn``); scribe emits only
       ``agent.exited``.
    """
    role, call_kind = _ROLE_BY_STAGE[req.stage]

    pre = gitgate.check_handoff(req.repos, runner=git_runner)
    if not pre.ok:
        return StageOutcome(
            ok=False,
            stop=None,
            obj=None,
            invalid_reason="pre_gate",
            gate_failures=list(pre.failures),
            events=[
                (
                    "agent.invalid_output",
                    {
                        "stage": req.stage,
                        "run_id": req.run_id,
                        "phase": "pre",
                        "failures": [_gate_failure_dict(f) for f in pre.failures],
                    },
                )
            ],
        )

    pre_snapshot: dict[str, tuple[str | None, str | None]] | None
    pre_snapshot = _snapshot(req.repos, git_runner) if req.stage in _REVIEW_STAGES else None

    system_prompt: str | None = None
    if prompts.needs_system_prompt(req.policy, is_first_call=req.is_first_call):
        history = req.in_obj.get("history") or {}
        system_prompt = prompts.render_system_prompt(role, call_kind=call_kind, history=history)
    user_prompt = prompts.render_user_prompt(req.in_obj)

    exit_code, stdout = agent_invoker(
        _build_argv(req, role, call_kind),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    if exit_code != 0:
        failure_code, detail = _runtime_failure(stdout, exit_code)
        return StageOutcome(
            ok=False,
            stop=None,
            obj=None,
            invalid_reason=failure_code,
            gate_failures=[],
            events=[
                (
                    "agent.failed",
                    {
                        "stage": req.stage,
                        "run_id": req.run_id,
                        "exit_code": exit_code,
                        "detail": detail,
                    },
                )
            ],
        )

    obj = protocol.extract_protocol_object(stdout, _schema_prefix(req.expected_schema))
    if obj is None:
        return StageOutcome(
            ok=False,
            stop=None,
            obj=None,
            invalid_reason=agentrun.FailureCode.NO_OBJECT,
            gate_failures=[],
            events=[
                (
                    "agent.invalid_output",
                    {
                        "stage": req.stage,
                        "run_id": req.run_id,
                        "detail": (
                            f"no protocol object matching {req.expected_schema!r} "
                            "found in agent output"
                        ),
                    },
                )
            ],
        )

    validation = protocol.validate(obj, req.expected_schema)
    if not validation.ok:
        return StageOutcome(
            ok=False,
            stop=obj.get("stop"),
            obj=obj,
            invalid_reason=agentrun.FailureCode.INVALID_OUTPUT,
            gate_failures=[],
            events=[
                (
                    "agent.invalid_output",
                    {
                        "stage": req.stage,
                        "run_id": req.run_id,
                        "detail": "; ".join(validation.errors),
                    },
                )
            ],
        )

    post = gitgate.check_handoff(req.repos, runner=git_runner)
    if not post.ok:
        return StageOutcome(
            ok=False,
            stop=obj.get("stop"),
            obj=obj,
            invalid_reason="post_gate",
            gate_failures=list(post.failures),
            events=[
                (
                    "agent.invalid_output",
                    {
                        "stage": req.stage,
                        "run_id": req.run_id,
                        "phase": "post",
                        "failures": [_gate_failure_dict(f) for f in post.failures],
                    },
                )
            ],
        )

    if pre_snapshot is not None:
        review_changed = _review_changed_failures(req.repos, pre_snapshot, git_runner)
        if review_changed:
            return StageOutcome(
                ok=False,
                stop=obj.get("stop"),
                obj=obj,
                invalid_reason=REVIEW_CHANGED_CODE,
                gate_failures=list(review_changed),
                events=[
                    (
                        "agent.invalid_output",
                        {
                            "stage": req.stage,
                            "run_id": req.run_id,
                            "phase": "post",
                            "failures": [_gate_failure_dict(f) for f in review_changed],
                        },
                    )
                ],
            )

    stop = obj["stop"]
    events: list[tuple[str, dict[str, Any]]] = [
        ("agent.exited", {"stage": req.stage, "run_id": req.run_id, "exit_code": 0, "stop": stop}),
    ]
    finished_kind = _finished_kind(req.stage)
    if finished_kind == "dd.stage.finished":
        events.append(("dd.stage.finished", _dd_finished_payload(req, stop, obj)))
    elif finished_kind == "goal.turn.finished":
        events.append(("goal.turn.finished", _goal_turn_payload(req, obj)))

    return StageOutcome(
        ok=True,
        stop=stop,
        obj=obj,
        invalid_reason=None,
        gate_failures=[],
        events=events,
    )


__all__ = [
    "REVIEW_CHANGED_CODE",
    "STAGES",
    "AgentInvoker",
    "StageOutcome",
    "StageRequest",
    "run_stage",
]

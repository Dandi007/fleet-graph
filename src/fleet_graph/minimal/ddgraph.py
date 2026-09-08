"""ddgraph.py — the DD-internal loop as a LangGraph graph (design.md §3).

The DD loop is fixed (design.md §3 / ddflow's transition table): Impl → running
the acceptance commands → CR → FR → Goal review → Merge Agent → cleanup. Every
agent stage runs through :func:`stagerunner.run_stage` (the single agent seam —
argv and gates are never hand-rolled here), and every program step (git gate,
PR open, baseline acceptance, acceptance run, merge, teardown) goes through the
injected seams in :class:`DDDeps`. The module owns zero IO: no ``subprocess``,
no ``open``, no git, no files.

Two seams deliberately belong to other DDs:

- **``merge_fn`` is the merge seam** — ``Callable[[state], (stop, payload)]``
  with stop ∈ merged / rebased / failed. This module performs no merge logic:
  it only calls the seam, records ``dd.stage.finished(stage="merge")``, and
  routes the result through :func:`ddflow.next_stage` (the *only* source of the
  transition rules — approve clearing, rebased → CR in full, merge-failed →
  Impl). The merge-agent implementation is another DD (``mergegate``).
- **The goal-level turn loop is another DD's graph** — none of it lives here.

The transition rules are *never* re-declared or copied here: every stage node
advances by calling :func:`ddflow.next_stage` / :func:`ddflow.advance`, so an
unknown ``(stage, stop)`` combination surfaces as a ``ValueError`` instead of
being silently swallowed by a duplicated table.

``events.jsonl`` is the only source of truth (protocol §11). The LangGraph
checkpointer is only a droppable cache: recovery replays the event log and
never resumes from checkpointed state (design.md §7.1 / GO-16). Derived values
that a stage needs (head commit, acceptance results, the awaiting-approval DD
object) are re-folded from ``deps.event_log.read()`` via
:func:`ddflow.build_dd_result` rather than carried in the graph state.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.minimal import acceptance as acceptance_mod
from fleet_graph.minimal import agentrun, ddflow, events, gitgate, prompts, stagerunner
from fleet_graph.minimal import dispatch as dispatch_mod

# GO-6.4: the DD loop has no hard round cap. LangGraph still enforces a
# recursion_limit to catch runaway graphs, so it is set to a value no
# legitimate DD can reach (~250k rounds at ~4 graph steps per round) instead of
# silently capping the loop.
_RECURSION_LIMIT = 1_000_000


@dataclass(frozen=True)
class DDDeps:
    """Every IO seam and policy knob the DD graph uses (zero module IO).

    ``merge_fn`` is the merge seam (another DD's ``mergegate``): it receives
    the graph state and returns ``(stop, payload)`` with stop ∈ merged /
    rebased / failed. ``pr_open`` / ``pr_cleanup`` / ``pr_mergeable`` share the
    ``prlifecycle`` signatures of the same names. ``goal`` is the enroll goal
    object (base acceptance for the baseline gate and the ``goal`` field of
    ``goal.review.in/1``); ``release_branch`` is the goal-level release branch
    name (PR base) and ``release_head`` the base commit the DD was cut from
    (the ``base_commit`` of the impl / review inputs).
    """

    event_log: events.EventLog
    agent_invoker: stagerunner.AgentInvoker
    git_runner: gitgate.GitRunner
    bash_runner: acceptance_mod.Runner
    pr_open: Callable[..., Any]
    pr_cleanup: Callable[..., Any]
    pr_mergeable: Callable[..., str]
    merge_fn: Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]
    goal: dict[str, Any]
    release_branch: str
    release_head: str
    session_overrides: dict[str, dict[str, Any]] | None = None
    session_root: str = ""
    model_by_role: dict[str, str] | None = None
    timeout_s: int = 300
    warn_dd_rounds: int = 6


class DDGraphState(TypedDict, total=False):
    """The graph state; nodes return partial dicts and never mutate in place.

    ``repos`` are the DD launch repos (``path`` / ``remote`` / ``branch`` /
    ``spec_path``, optionally ``repo_path`` for teardown). ``stage`` is the
    *next* loop stage to run (None outside the loop); ``terminal`` is
    ``"merged"`` / ``"failed"`` once the loop has resolved, and on ``"failed"``
    ``last_stop`` carries the failing stage name while ``last_obj`` carries the
    failure descriptor (``{"detail": ...}``) for the teardown node's terminal
    event. ``round`` counts Impl entries (1-based), ``approve_valid`` mirrors
    :class:`ddflow.DDState`, and ``history`` is the replayed ``(stage, stop)``
    sequence.
    """

    goal_id: str
    dd_id: str
    repos: list[dict[str, Any]]
    spec_text: str
    acceptance_cmds: list[str]
    stage: str | None
    round: int
    approve_valid: bool
    history: tuple[tuple[str, str], ...]
    last_stop: str | None
    last_obj: dict[str, Any] | None
    feedback: dict[str, Any] | None
    prs: dict[str, dict[str, Any]]
    dd_result: dict[str, Any] | None
    terminal: str | None


def _ddstate_from(state: DDGraphState) -> ddflow.DDState:
    return ddflow.DDState(
        round=state.get("round") or 1,
        approve_valid=bool(state.get("approve_valid")),
        history=tuple(state.get("history") or ()),
    )


def _fail_result(stage: str, detail: str | None) -> dict[str, Any]:
    return {
        "stage": None,
        "terminal": "failed",
        "last_stop": stage,
        "last_obj": {"detail": detail},
    }


def _failure_detail(outcome: stagerunner.StageOutcome) -> str:
    if outcome.gate_failures:
        failure = outcome.gate_failures[0]
        return f"{failure.code}: {failure.detail}"
    return outcome.invalid_reason or "agent failed"


def _feedback_for(stage: str, source: str, obj: dict[str, Any] | None) -> dict[str, Any]:
    feedback: dict[str, Any] = {"from": source}
    if stage in ("cr", "fr"):
        feedback["detail"] = (obj or {}).get("summary")
        findings = (obj or {}).get("findings")
        if isinstance(findings, list):
            feedback["findings"] = findings
    elif stage == "goal_review":
        feedback["detail"] = (obj or {}).get("message")
    elif stage == "merge":
        feedback["detail"] = (obj or {}).get("detail")
    return feedback


def _goal_acceptance(goal: dict[str, Any]) -> list[str]:
    """The goal's base acceptance (flat, order-preserving dedup).

    Reads a top-level ``acceptance`` list when present (the older
    ``goal.enroll/1`` shape) and falls back to flattening the per-repo
    ``repos[].acceptance`` of ``goal.enroll/2``.
    """
    acc = goal.get("acceptance")
    if isinstance(acc, list):
        return [command for command in acc if isinstance(command, str) and command]
    result: list[str] = []
    for repo in goal.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        for command in repo.get("acceptance") or []:
            if isinstance(command, str) and command and command not in result:
                result.append(command)
    return result


def _saw_stage(events_list: list[events.Event], stage: str, dd_id: str) -> bool:
    return any(
        ev.kind == "agent.exited" and ev.dd_id == dd_id and (ev.payload or {}).get("stage") == stage
        for ev in events_list
    )


def _has_round_warning(events_list: list[events.Event], text: str) -> bool:
    return any(
        ev.kind == "goal.warning" and (ev.payload or {}).get("message") == text
        for ev in events_list
    )


def _repo_refs(repos: list[dict[str, Any]]) -> list[gitgate.RepoRef]:
    return dispatch_mod.dd_repo_refs({"repos": repos})


def build_dd_graph(deps: DDDeps, *, checkpointer: Any = None) -> Any:
    """Wire the DD loop: dd_ready → open_pr → baseline → the stage loop → cleanup.

    The stage loop (impl → acceptance → cr → fr → goal_review → merge) is
    routed purely by ``state["stage"]``, and each stage node sets that value by
    calling :func:`ddflow.next_stage` / :func:`ddflow.advance` — the transition
    rules are never copied here. ``checkpointer`` is only a droppable cache:
    ``events.jsonl`` stays the only source of truth, and recovery replays the
    event log instead of resuming from checkpointed state (GO-16 / design §7.1).
    """
    log = deps.event_log

    def dd_ready_node(state: DDGraphState) -> dict[str, Any]:
        gate = gitgate.check_dd_ready(_repo_refs(state["repos"]), runner=deps.git_runner)
        if gate.ok:
            return {}
        failures = [
            {"repo": failure.repo, "code": failure.code, "detail": failure.detail}
            for failure in gate.failures
        ]
        log.append(
            "dd.failed",
            {"stage": "dd_ready", "failures": failures},
            dd_id=state["dd_id"],
        )
        return {"terminal": "failed", "stage": None}

    def open_pr_node(state: DDGraphState) -> dict[str, Any]:
        prs = dict(state.get("prs") or {})
        for repo in state["repos"]:
            path = repo["path"]
            pr = deps.pr_open(
                path,
                head_branch=repo["branch"],
                base_branch=deps.release_branch,
                title=state["dd_id"],
                body_file=os.path.join(path, repo["spec_path"]),
                runner=deps.git_runner,
            )
            prs[path] = {"number": pr.number, "url": pr.url}
            log.append(
                "dd.pr_opened",
                {"repo": path, "number": pr.number, "url": pr.url},
                dd_id=state["dd_id"],
            )
        return {"prs": prs}

    def baseline_node(state: DDGraphState) -> dict[str, Any]:
        base_cmds = _goal_acceptance(deps.goal)
        cwd = state["repos"][0]["path"]
        run = acceptance_mod.run_acceptance(base_cmds, cwd=cwd, runner=deps.bash_runner)
        if not run.passed:
            detail = (
                f"baseline acceptance failed: {run.failed_cmd}"
                if run.failed_cmd
                else "baseline red"
            )
            log.append("dd.failed", {"stage": "baseline", "detail": detail}, dd_id=state["dd_id"])
            return {"terminal": "failed", "stage": None}
        return {"stage": "impl", "round": 1, "approve_valid": False, "history": ()}

    def _run_stage(
        state: DDGraphState,
        *,
        stage: str,
        role: str,
        in_obj: dict[str, Any],
        repos: list[gitgate.RepoRef],
        expected_schema: str,
    ) -> dict[str, Any]:
        dd_id = state["dd_id"]
        events_list = list(log.read())
        request = stagerunner.StageRequest(
            stage=stage,
            run_id=f"{dd_id}-{stage}-{state.get('round') or 1}",
            in_obj=in_obj,
            repos=repos,
            expected_schema=expected_schema,
            policy=agentrun.resolve_session_policy(role, deps.session_overrides),
            cwd=state["repos"][0]["path"],
            is_first_call=not _saw_stage(events_list, stage, dd_id),
            session_root=deps.session_root,
            timeout_s=deps.timeout_s,
            model=(deps.model_by_role or {}).get(role),
        )
        outcome = stagerunner.run_stage(
            request, git_runner=deps.git_runner, agent_invoker=deps.agent_invoker
        )
        for kind, payload in outcome.events:
            log.append(kind, payload, dd_id=dd_id)
        if not outcome.ok:
            # protocol §0.2: an agent failure ends the DD; the engine never
            # re-runs it.
            return _fail_result(stage, _failure_detail(outcome))

        stop = outcome.stop
        obj = outcome.obj or {}
        transition, new = _advance(state, stage, stop)
        feedback = None
        if transition.feedback_from is not None:
            feedback = _feedback_for(stage, transition.feedback_from, obj)
        if new.outcome == "failed":
            return _fail_result(stage, obj.get("detail"))
        return {
            "stage": new.stage,
            "round": new.round,
            "approve_valid": new.approve_valid,
            "history": new.history,
            "terminal": new.outcome,
            "last_stop": stop,
            "last_obj": obj,
            "feedback": feedback,
        }

    def impl_node(state: DDGraphState) -> dict[str, Any]:
        ddstate = _ddstate_from(state)
        events_list = list(log.read())
        for warning in ddflow.warnings_for(ddstate, warn_dd_rounds=deps.warn_dd_rounds):
            if not _has_round_warning(events_list, warning):
                log.append("goal.warning", {"message": warning})
        in_obj = prompts.build_impl_in(
            dd_id=state["dd_id"],
            round=state.get("round") or 1,
            workspace=state["repos"][0]["path"],
            branch=state["repos"][0]["branch"],
            base_commit=deps.release_head,
            spec_text=state["spec_text"],
            acceptance=list(state["acceptance_cmds"]),
            feedback=state.get("feedback"),
            history=prompts.history_handle(
                goal_run_root=str(log.goal_run_root), dd_id=state["dd_id"]
            ),
        )
        return _run_stage(
            state,
            stage="impl",
            role="impl",
            in_obj=in_obj,
            repos=_repo_refs(state["repos"]),
            expected_schema=agentrun.schema_for("impl"),
        )

    def cr_node(state: DDGraphState) -> dict[str, Any]:
        return _review_node(state, role="cr")

    def fr_node(state: DDGraphState) -> dict[str, Any]:
        return _review_node(state, role="fr")

    def _review_node(state: DDGraphState, *, role: str) -> dict[str, Any]:
        result = ddflow.build_dd_result(list(log.read()), state["dd_id"])
        cr_result = None
        if role == "fr":
            cr_result = {
                "stop": state.get("last_stop"),
                "summary": (state.get("last_obj") or {}).get("summary"),
                "findings": (state.get("last_obj") or {}).get("findings", []),
            }
        in_obj = prompts.build_review_in(
            role=role,
            dd_id=state["dd_id"],
            round=state.get("round") or 1,
            workspace=state["repos"][0]["path"],
            branch=state["repos"][0]["branch"],
            base_commit=deps.release_head,
            head_commit=result["head_commit"] or deps.release_head,
            spec_text=state["spec_text"],
            acceptance_results=result["acceptance_results"],
            cr_result=cr_result,
            history=prompts.history_handle(
                goal_run_root=str(log.goal_run_root), dd_id=state["dd_id"]
            ),
        )
        return _run_stage(
            state,
            stage=role,
            role=role,
            in_obj=in_obj,
            repos=_repo_refs(state["repos"]),
            expected_schema=agentrun.schema_for(role),
        )

    def goal_review_node(state: DDGraphState) -> dict[str, Any]:
        dd_result = ddflow.build_dd_result(list(log.read()), state["dd_id"])
        in_obj = prompts.build_goal_review_in(
            goal=deps.goal,
            dd=dd_result,
            release_head=deps.release_head,
            history=prompts.history_handle(
                goal_run_root=str(log.goal_run_root), dd_id=state["dd_id"]
            ),
        )
        return _run_stage(
            state,
            stage="goal_review",
            role="goal",
            in_obj=in_obj,
            repos=[],
            expected_schema=agentrun.schema_for("goal", "review"),
        )

    def acceptance_node(state: DDGraphState) -> dict[str, Any]:
        cmds = list(state["acceptance_cmds"])
        cwd = state["repos"][0]["path"]
        run = acceptance_mod.run_acceptance(cmds, cwd=cwd, runner=deps.bash_runner)
        for index, result in enumerate(run.results):
            log.append(
                "dd.acceptance",
                acceptance_mod.acceptance_event_payload(result, index=index, total=len(cmds)),
                dd_id=state["dd_id"],
            )
        stop = "pass" if run.passed else "fail"
        log.append("dd.stage.finished", {"stage": "acceptance", "stop": stop}, dd_id=state["dd_id"])
        transition, new = _advance(state, "acceptance", stop)
        feedback = None
        if transition.feedback_from is not None:
            failed = run.results[-1] if run.results else None
            detail = f"acceptance command failed: {failed.cmd}" if failed else "acceptance failed"
            feedback = {"from": "acceptance", "detail": detail}
        return {
            "stage": new.stage,
            "round": new.round,
            "approve_valid": new.approve_valid,
            "history": new.history,
            "terminal": new.outcome,
            "last_stop": stop,
            "feedback": feedback,
        }

    def merge_node(state: DDGraphState) -> dict[str, Any]:
        stop, payload = deps.merge_fn(state)
        log.append(
            "dd.stage.finished",
            {"stage": "merge", "stop": stop, **payload},
            dd_id=state["dd_id"],
        )
        obj = {"stop": stop, **payload}
        transition, new = _advance(state, "merge", stop)
        feedback = None
        if transition.feedback_from is not None:
            feedback = _feedback_for("merge", transition.feedback_from, obj)
        return {
            "stage": new.stage,
            "round": new.round,
            "approve_valid": new.approve_valid,
            "history": new.history,
            "terminal": new.outcome,
            "last_stop": stop,
            "last_obj": obj,
            "feedback": feedback,
        }

    def cleanup_node(state: DDGraphState) -> dict[str, Any]:
        outcome = state["terminal"]
        records = [
            (
                repo.get("repo_path") or repo["path"],
                repo["path"],
                repo["remote"],
                repo["branch"],
                int((state.get("prs") or {}).get(repo["path"], {}).get("number") or 0),
            )
            for repo in state["repos"]
        ]
        deps.pr_cleanup(outcome=outcome, repos=records, runner=deps.git_runner)
        if outcome == "merged":
            merged_commit = (state.get("last_obj") or {}).get("merged_commit")
            log.append("dd.merged", {"merged_commit": merged_commit}, dd_id=state["dd_id"])
        else:
            log.append(
                "dd.failed",
                {
                    "stage": state.get("last_stop"),
                    "detail": (state.get("last_obj") or {}).get("detail"),
                },
                dd_id=state["dd_id"],
            )
        return {"terminal": outcome}

    graph = StateGraph(DDGraphState)
    graph.add_node("dd_ready", dd_ready_node)
    graph.add_node("open_pr", open_pr_node)
    graph.add_node("baseline", baseline_node)
    graph.add_node("impl", impl_node)
    graph.add_node("acceptance", acceptance_node)
    graph.add_node("cr", cr_node)
    graph.add_node("fr", fr_node)
    graph.add_node("goal_review", goal_review_node)
    graph.add_node("merge", merge_node)
    graph.add_node("cleanup", cleanup_node)

    graph.add_edge(START, "dd_ready")
    graph.add_conditional_edges("dd_ready", _route_after_dd_ready, {"open_pr": "open_pr", END: END})
    graph.add_edge("open_pr", "baseline")
    graph.add_conditional_edges("baseline", _route_after_baseline, {"impl": "impl", END: END})

    loop_map = {
        "impl": "impl",
        "acceptance": "acceptance",
        "cr": "cr",
        "fr": "fr",
        "goal_review": "goal_review",
        "merge": "merge",
        "cleanup": "cleanup",
        END: END,
    }
    for node_name in ("impl", "acceptance", "cr", "fr", "goal_review", "merge"):
        graph.add_conditional_edges(node_name, _route_loop, loop_map)
    graph.add_edge("cleanup", END)

    return graph.compile(checkpointer=checkpointer)


def _advance(
    state: DDGraphState, stage: str, stop: str
) -> tuple[ddflow.Transition, ddflow.DDState]:
    """Advance the DD state machine via ddflow (the single transition source)."""
    transition = ddflow.next_stage(stage, stop)
    new = ddflow.advance(_ddstate_from(state), stage, stop)
    return transition, new


def _route_after_dd_ready(state: DDGraphState) -> str:
    return END if state.get("terminal") == "failed" else "open_pr"


def _route_after_baseline(state: DDGraphState) -> str:
    return END if state.get("terminal") == "failed" else "impl"


def _route_loop(state: DDGraphState) -> str:
    if state.get("terminal"):
        return "cleanup"
    stage = state.get("stage")
    if stage is None:
        return END
    return stage


def run_dd(
    deps: DDDeps,
    *,
    goal_id: str,
    dd_id: str,
    repos: list[dict[str, Any]],
    spec_text: str,
    acceptance_cmds: list[str],
    checkpointer: Any = None,
) -> dict[str, Any]:
    """Run one DD end-to-end and return the protocol §7 DD result object.

    ``repos`` are the DD launch repos (``path`` / ``remote`` / ``branch`` /
    ``spec_path``) and ``acceptance_cmds`` the full acceptance batch (goal
    acceptance plus ``acceptance_extra``), already combined by the caller. The
    opening ``dd.dispatched`` event is written here; the result is re-folded
    from ``deps.event_log.read()`` — ``events.jsonl`` is the only source of
    truth (protocol §11), and the checkpointer is only a droppable cache:
    recovery replays the event log and never resumes from checkpointed state
    (GO-16 / design §7.1).
    """
    deps.event_log.append(
        "dd.dispatched",
        {
            "spec_text": spec_text,
            "branch": repos[0]["branch"],
            "head_commit": deps.release_head,
        },
        dd_id=dd_id,
    )
    graph = build_dd_graph(deps, checkpointer=checkpointer)
    initial: DDGraphState = {
        "goal_id": goal_id,
        "dd_id": dd_id,
        "repos": repos,
        "spec_text": spec_text,
        "acceptance_cmds": list(acceptance_cmds),
        "stage": None,
        "round": 1,
        "approve_valid": False,
        "history": (),
        "last_stop": None,
        "last_obj": None,
        "feedback": None,
        "prs": {},
        "dd_result": None,
        "terminal": None,
    }
    graph.invoke(
        initial,
        config={
            "recursion_limit": _RECURSION_LIMIT,
            "configurable": {"thread_id": dd_id},
        },
    )
    return ddflow.build_dd_result(deps.event_log.read(), dd_id)


__all__ = [
    "DDDeps",
    "DDGraphState",
    "build_dd_graph",
    "run_dd",
]

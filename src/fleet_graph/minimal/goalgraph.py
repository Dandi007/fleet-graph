"""goalgraph.py — the goal-level turn loop as a LangGraph graph (design.md §2).

One Goal Agent per goal, one Stop per turn, three exits (protocol §2):

- ``dispatch`` → ``validate_dispatch`` (``dispatch.py`` field checks plus the
  GO-36 git gate) → ``run_dd`` → back to ``read_control`` → the next turn;
- ``done`` → ``final_merge`` (release → the goal's target branch);
- ``blocked`` → ``finish_blocked`` writes ``goal.blocked`` and ends.

Seams this module deliberately keeps (each belongs to another DD):

- **No ``ddgraph`` import.** The DD-internal loop is the injected ``run_dd``
  seam — ``Callable[[dispatch_obj], dict]`` returning the protocol §7 DD
  result object — so the goal graph and the DD graph land as parallel DDs.
- **No merge logic.** ``final_merge`` is a seam returning
  ``(stop, payload)`` with stop ∈ merged / rebased / failed. On ``rebased`` /
  ``failed`` this module only writes an event and bounces the result back
  into the next turn's handoff (``warnings``); GO-15's "a rebase that touched
  code must re-run CR → FR → Goal review in full" is a later DD's job —
  nothing here re-reviews or re-merges.
- **No process entry, no MCP transport.** MCP → engine is only
  ``control.jsonl``, read at step boundaries (GO-16): ``read_control`` runs
  before every turn and after every DD.
- **The read-only scribe (GO-21) is opt-in.** ``scribe_enabled`` defaults to
  False, so an unwired graph is byte-identical to the pre-scribe graph. When
  enabled it runs the ``scribe`` stage once per goal boundary (turn finished,
  DD finished, done, blocked, and the turn-boundary warning); its failures
  never block the loop. The engine wires it on by default (``engine.build_deps``
  passes ``scribe_enabled=True``).

Beyond the caller-injected ``stagerunner`` events, the boundary events
written here are exactly the protocol §8/§10 ones: ``control.received`` (one
per new control line, before acting on it), ``goal.message`` (an MCP message
made durable — state clears it after injection), ``goal.turn.started``
(``events.fold`` counts turns from it), ``goal.steered`` (GO-20),
``goal.dispatch_rejected`` (dispatch bounced with field-level errors; the
kind is registered in ``events.GOAL_KINDS`` per the DD-13 precedent), the
terminal ``goal.done`` / ``goal.blocked`` / ``engine.exiting(stop)``, and —
only behind the opt-in seam — the scribe's ``scribe.observed`` /
``scribe.failed`` (registered in ``events.SCRIBE_KINDS``).

GO-6.4: no hard loop cap. ``warn_turns`` only produces warning text —
injected into every turn at or beyond the line — and one ``goal.warning``
event, deduped against the event log so replay never duplicates it. It never
stops the loop.

``events.jsonl`` is the only source of truth (protocol §11): every derived
value — current goal, ``goal_version``, ``steer_diff``, dd counts, whether
the goal session already saw a call — is re-folded from
``deps.event_log.read()`` at each turn. The LangGraph checkpointer is only a
droppable cache: recovery replays the event log and never resumes from
checkpointed state (design.md §7.1 / GO-16).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.minimal import (
    agentrun,
    control,
    events,
    gitgate,
    prompts,
    runroot,
    scribe,
    stagerunner,
    steer,
)
from fleet_graph.minimal import (
    dispatch as dispatch_mod,
)

# GO-6.4: the loop has no hard turn cap. LangGraph still enforces a
# recursion_limit to catch runaway graphs, so it is set to a value that no
# legitimate goal can reach (one turn cycle costs ~4 graph steps, so this
# leaves room for ~250k turns) instead of silently capping the loop.
_RECURSION_LIMIT = 1_000_000


@dataclass(frozen=True)
class GoalDeps:
    """Every IO seam and policy knob the goal graph uses (zero module IO).

    ``run_dd`` executes one dispatched DD and returns the protocol §7 DD
    result object (it is expected to write its own ``dd.*`` events into the
    shared ``event_log`` — the fold-based ``dd_summary`` reads them back).
    ``final_merge`` merges release → the goal's target branch and returns
    ``(stop, payload)`` with stop ∈ merged / rebased / failed; ``None`` means
    the seam is not wired and a ``done`` goal blocks instead of guessing.
    ``scribe_enabled`` opts into the read-only scribe stage (GO-21) at goal
    boundaries; it defaults to False so an unwired graph behaves exactly as
    before.
    """

    event_log: events.EventLog
    control_log: control.ControlLog
    agent_invoker: stagerunner.AgentInvoker
    git_runner: gitgate.GitRunner
    run_dd: Callable[[dict[str, Any]], dict[str, Any]]
    final_merge: Callable[[], tuple[str, dict[str, Any]]] | None = None
    warn_turns: int = 30
    session_root: str = ""
    session_overrides: dict[str, dict[str, Any]] | None = None
    model_by_role: dict[str, str] | None = None
    timeout_s: int = 300
    scribe_enabled: bool = False


class GoalGraphState(TypedDict, total=False):
    """The graph state; nodes return partial dicts and never mutate in place.

    ``turn_no`` counts completed turn→DD cycles (incremented in ``run_dd``,
    per the dd-18 spec), so the turn being started injects ``turn_no + 1``.
    ``last_seq`` is the control-log cursor (``control.jsonl`` seq space, not
    the event seq space). ``stop`` is the pending action from the last turn:
    ``"dispatch"`` (awaiting validation), ``"done"`` / ``"blocked"`` /
    ``"stopped"`` terminal, ``None`` nothing pending.
    """

    goal_id: str
    enroll: dict[str, Any]
    turn_no: int
    goal_version: int
    last_stop: dict[str, Any] | None
    last_dd: dict[str, Any] | None
    dd_summary: str
    pending_messages: list[dict[str, Any]]
    warnings: list[str]
    stop: str | None
    summary: str | None
    blocked: dict[str, Any] | None
    last_seq: int


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _has_warning(events_list: list[events.Event], text: str) -> bool:
    """Whether an identical ``goal.warning`` is already in the log (dedupe)."""
    return any(
        ev.kind == "goal.warning" and (ev.payload or {}).get("message") == text
        for ev in events_list
    )


def _scribe_cursor(events_list: list[events.Event]) -> int:
    """The seq the last scribe run observed up to (0 before the first run).

    GO-21 / dd-23: the scribe's cursor lives in the event log itself, not a
    state file. Every boundary event (``scribe.observed`` / ``scribe.failed``)
    carries ``until_seq``; the newest one is the cursor, so the next run starts
    at ``cursor + 1`` and never re-observes an already-seen segment.
    """
    cursor = 0
    for ev in events_list:
        if ev.kind in ("scribe.observed", "scribe.failed"):
            until_seq = (ev.payload or {}).get("until_seq")
            if isinstance(until_seq, int) and not isinstance(until_seq, bool):
                cursor = max(cursor, until_seq)
    return cursor


def _blurb(payload: dict[str, Any]) -> str:
    """The first human-readable line of a merge/result payload."""
    for key in ("summary", "detail", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "no detail"


def _one_line_dd_summary(history: list[events.DDSummary], result: dict[str, Any]) -> str:
    """GO-17: dd_history collapses to one line, never a table."""
    if not history:
        return ""
    merged = sum(1 for item in history if item.outcome == "merged")
    failed = sum(1 for item in history if item.outcome == "failed")
    dd_id = result.get("dd_id") or history[-1].dd_id
    detail = result.get("impl_summary")
    failure = result.get("failure")
    if not detail and isinstance(failure, dict):
        detail = failure.get("detail")
    blurb = detail or result.get("outcome") or ""
    return f"{len(history)} 张 DD：{merged} merged，{failed} failed（{dd_id}：{blurb}）"


def _route_after_read_control(state: GoalGraphState) -> str:
    return END if state.get("stop") == "stopped" else "goal_turn"


def _route_after_goal_turn(state: GoalGraphState) -> str:
    stop = state.get("stop")
    if stop == "dispatch":
        return "validate_dispatch"
    if stop == "done":
        return "final_merge"
    if stop == "blocked":
        return "finish_blocked"
    raise ValueError(f"unknown goal turn stop {stop!r} (expected dispatch/done/blocked)")


def _route_after_validate(state: GoalGraphState) -> str:
    # validate_dispatch clears `stop` when it bounces the dispatch back;
    # a surviving "dispatch" means the GO-36 checks passed and run_dd is next.
    return "run_dd" if state.get("stop") == "dispatch" else "goal_turn"


def _route_after_final_merge(state: GoalGraphState) -> str:
    stop = state.get("stop")
    if stop == "done":
        return END
    if stop == "blocked":
        return "finish_blocked"
    return "goal_turn"


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


def build_goal_graph(deps: GoalDeps, *, checkpointer: Any = None) -> Any:
    """Wire the turn loop: read_control → goal_turn → {dispatch, done, blocked}.

    ``dispatch`` goes through ``validate_dispatch`` (dispatch.py field checks
    + the GO-36 git gate) and ``run_dd`` before looping back to
    ``read_control``; a rejected dispatch bounces straight back to
    ``goal_turn`` with the field-level errors as the next handoff (mechanical
    principle: bounce, don't guess). ``checkpointer`` is only a droppable
    cache — ``events.jsonl`` stays the only source of truth (protocol §11).
    """
    policy = agentrun.resolve_session_policy("goal", deps.session_overrides)
    goal_model = (deps.model_by_role or {}).get("goal")

    def run_scribe(state: GoalGraphState, trigger: str) -> None:
        """Run the read-only scribe at one goal boundary; never block the loop.

        GO-21 / dd-23: on each goal-level boundary (``goal.turn.finished``,
        ``dd.merged`` / ``dd.failed``, ``goal.done`` / ``goal.blocked``, and the
        turn-boundary ``goal.warning``) the scribe reads L0 with a seq-range
        folded from the event log (``cursor+1 .. last_seq``), runs once through
        ``stagerunner`` with
        role ``scribe``, then validates the ``scribe/1`` output and gates each
        observation (§12). Passed observations append to
        ``observations.jsonl`` and are recorded as ``scribe.observed``; every
        failure — invalid output, an evidence-gate drop, a raising invoker —
        is only a ``scribe.failed`` event and the goal keeps running. The
        scribe never writes ``control.jsonl``, never touches git, never
        changes goal state.
        """
        if not deps.scribe_enabled:
            return
        log = deps.event_log
        enroll = state["enroll"]
        events_list = list(log.read())
        _, goal_version = steer.current_goal(enroll, events_list)
        since_seq = _scribe_cursor(events_list) + 1
        until_seq = max((ev.seq for ev in events_list), default=0)
        sessions_dir = deps.session_root or str(log.goal_run_root / "sessions")
        request = stagerunner.StageRequest(
            stage="scribe",
            run_id=f"goal-{state['goal_id']}-scribe-{until_seq}",
            in_obj=prompts.build_scribe_in(
                goal_id=state["goal_id"],
                goal_version=goal_version,
                trigger=trigger,
                since_seq=since_seq,
                until_seq=until_seq,
                new_runs=scribe.new_runs_from_events(
                    events_list, since_seq, until_seq, sessions_dir
                ),
                prior_observations=str(log.goal_run_root / "observations.jsonl"),
                history=prompts.history_handle(
                    goal_run_root=str(log.goal_run_root),
                    work_folder=enroll.get("work_folder"),
                ),
            ),
            repos=[],
            expected_schema=agentrun.schema_for("scribe"),
            policy=agentrun.resolve_session_policy("scribe", deps.session_overrides),
            cwd=".",
            is_first_call=not any(
                ev.kind == "agent.exited" and (ev.payload or {}).get("stage") == "scribe"
                for ev in events_list
            ),
            session_root=deps.session_root,
            timeout_s=deps.timeout_s,
            model=(deps.model_by_role or {}).get("scribe"),
        )
        try:
            outcome = stagerunner.run_stage(
                request, git_runner=deps.git_runner, agent_invoker=deps.agent_invoker
            )
        except Exception as exc:
            log.append(
                "scribe.failed",
                {
                    "trigger": trigger,
                    "until_seq": until_seq,
                    "reason": "exception",
                    "detail": str(exc),
                },
            )
            return
        for kind, payload in outcome.events:
            log.append(kind, payload)
        if not outcome.ok:
            log.append(
                "scribe.failed",
                {"trigger": trigger, "until_seq": until_seq, "reason": outcome.invalid_reason},
            )
            return
        obj = outcome.obj or {}
        observations = obj.get("observations") or []
        kept, dropped = scribe.partition_observations(
            observations,
            since_seq=since_seq,
            until_seq=until_seq,
            session_exists=os.path.isdir,
        )
        observation_log = scribe.ObservationLog(log.goal_run_root)
        for obs in kept:
            observation_log.append(obs, trigger=trigger, seq_range=[since_seq, until_seq])
        if dropped:
            detail = "; ".join("; ".join(entry.get("errors", [])) for entry in dropped)
            log.append(
                "scribe.failed",
                {
                    "trigger": trigger,
                    "until_seq": until_seq,
                    "reason": scribe.DROP_DETAIL,
                    "detail": detail,
                },
            )
            return
        log.append(
            "scribe.observed",
            {
                "trigger": trigger,
                "since_seq": since_seq,
                "until_seq": until_seq,
                "observations": len(kept),
            },
        )

    def read_control(state: GoalGraphState) -> dict[str, Any]:
        """Step boundary (GO-16): drain control.jsonl since ``last_seq``."""
        log = deps.event_log
        enroll = state["enroll"]
        messages = list(state.get("pending_messages") or [])
        goal_version = state.get("goal_version") or 1
        last_seq = state.get("last_seq") or 0
        try:
            ops = list(deps.control_log.read_new(last_seq))
        except ValueError as exc:
            # A corrupt mid-file control line is damage, not a verdict: record
            # it and keep running; nothing from this batch is applied.
            log.append("goal.warning", {"message": f"control log unreadable, ignored: {exc}"})
            return {
                "last_seq": last_seq,
                "pending_messages": messages,
                "goal_version": goal_version,
            }
        for op in ops:
            log.append("control.received", dict(op))
            last_seq = max(last_seq, int(op.get("seq") or 0))
            kind = op.get("op")
            if kind == "message":
                text = op.get("text")
                if isinstance(text, str) and text.strip():
                    messages.append({"ts": op.get("ts"), "from": "mcp", "text": text})
                    log.append("goal.message", {"text": text})
                else:
                    log.append(
                        "goal.warning",
                        {"message": "control message ignored: 'text' must be a non-empty string"},
                    )
            elif kind == "steer":
                patch = op.get("patch")
                try:
                    current, _ = steer.current_goal(enroll, log.read())
                    _new_goal, diff = steer.apply_patch(current, patch)
                except ValueError as exc:
                    log.append("goal.warning", {"message": f"control steer ignored: {exc}"})
                    continue
                goal_version += 1
                log.append(
                    "goal.steered",
                    steer.steered_payload(goal_version, diff, op.get("note")),
                )
            elif kind == "stop":
                log.append("engine.exiting", {"reason": "stop", "mode": op.get("mode")})
                return {
                    "stop": "stopped",
                    "summary": "goal_stop received at a step boundary; no further turns",
                    "last_seq": last_seq,
                    "pending_messages": messages,
                    "goal_version": goal_version,
                }
            elif kind == "resume":
                log.append(
                    "goal.warning",
                    {"message": "control resume ignored: this goal is still running"},
                )
            else:
                log.append("goal.warning", {"message": f"control op ignored: {kind!r}"})
        return {
            "last_seq": last_seq,
            "pending_messages": messages,
            "goal_version": goal_version,
        }

    def goal_turn(state: GoalGraphState) -> dict[str, Any]:
        """Run one Goal Agent turn; every derived value re-folds from the log."""
        log = deps.event_log
        enroll = state["enroll"]
        events_list = list(log.read())
        goal_obj, goal_version = steer.current_goal(enroll, events_list)
        messages = list(state.get("pending_messages") or [])
        warnings = list(state.get("warnings") or [])
        turn_no = (state.get("turn_no") or 0) + 1
        if deps.warn_turns and turn_no >= deps.warn_turns:
            text = f"turns>={deps.warn_turns}"
            warnings.append(text)
            if not _has_warning(events_list, text):
                log.append("goal.warning", {"message": text})
                # §12 触发点之一（goal.warning）：只在 turn 边界这一处起书记员，
                # 避免噪声 — control 解析失败类的内部 warning（read_control 里
                # 的 :362/:379/:388/:406/:410）不起，protocol §12 原文如此要求。
                run_scribe(state, trigger="goal.warning")
        # GO-20: steers since the previous turn's start are this turn's diff.
        since = max((ev.seq for ev in events_list if ev.kind == "goal.turn.started"), default=0)
        steer_diff = steer.steer_diff_since(events_list, since)
        repos = enroll.get("repos") or []
        release_branch = runroot.release_branch(enroll)
        release_head = ""
        if repos:
            release_head = (
                gitgate.remote_tip(
                    repos[0]["path"], repos[0]["remote"], release_branch, runner=deps.git_runner
                )
                or ""
            )
        in_obj = prompts.build_goal_turn_in(
            goal=goal_obj,
            goal_version=goal_version,
            steer_diff=steer_diff,
            turn_no=turn_no,
            release_branch=release_branch,
            release_head=release_head,
            dd_summary=state.get("dd_summary") or "",
            last_dd=state.get("last_dd"),
            last_stop=state.get("last_stop"),
            messages=messages,
            warnings=warnings,
            history=prompts.history_handle(
                goal_run_root=str(log.goal_run_root),
                work_folder=enroll.get("work_folder"),
            ),
        )
        log.append("goal.turn.started", {"turn_no": turn_no})
        saw_goal_exit = any(
            ev.kind == "agent.exited" and (ev.payload or {}).get("stage") == "goal_turn"
            for ev in events_list
        )
        request = stagerunner.StageRequest(
            stage="goal_turn",
            run_id=f"goal-{state['goal_id']}-turn-{turn_no}",
            in_obj=in_obj,
            repos=[],
            expected_schema=agentrun.schema_for("goal", "turn"),
            policy=policy,
            cwd=repos[0]["path"] if repos else ".",
            is_first_call=not saw_goal_exit,
            session_root=deps.session_root,
            timeout_s=deps.timeout_s,
            model=goal_model,
        )
        outcome = stagerunner.run_stage(
            request, git_runner=deps.git_runner, agent_invoker=deps.agent_invoker
        )
        for kind, payload in outcome.events:
            log.append(kind, payload)
        if not outcome.ok:
            # protocol §0.2: the Goal Agent failed → the goal blocks; the
            # engine never re-runs it.
            blocked = {
                "kind": "external",
                "detail": (
                    f"goal agent failed ({outcome.invalid_reason}); "
                    "engine does not re-run (protocol §0.2)"
                ),
            }
            return {
                "stop": "blocked",
                "summary": blocked["detail"],
                "blocked": blocked,
                "pending_messages": [],
                "warnings": [],
                "goal_version": goal_version,
            }
        run_scribe(state, trigger="goal.turn.finished")
        stop_obj = outcome.obj or {}
        blocked_obj = stop_obj.get("blocked") if outcome.stop == "blocked" else None
        return {
            "stop": outcome.stop,
            "summary": stop_obj.get("summary"),
            "blocked": blocked_obj,
            "last_stop": stop_obj,
            "pending_messages": [],
            "warnings": [],
            "goal_version": goal_version,
        }

    def validate_dispatch(state: GoalGraphState) -> dict[str, Any]:
        """dispatch.py field checks + the GO-36 gate; bounce, don't guess."""
        log = deps.event_log
        enroll = state["enroll"]
        dispatch_obj = dict((state.get("last_stop") or {}).get("dispatch") or {})
        errors: list[str] = []
        failures: list[dict[str, str]] = []
        try:
            dispatch_mod.dd_repo_refs(dispatch_obj)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            gate = dispatch_mod.check_dispatch_ready(
                dispatch_obj,
                goal_acceptance=list(enroll.get("acceptance") or []),
                runner=deps.git_runner,
            )
            failures = [
                {"repo": failure.repo, "code": failure.code, "detail": failure.detail}
                for failure in gate.failures
            ]
            errors.extend(
                f"{failure.repo}: {failure.code}: {failure.detail}" for failure in gate.failures
            )
        if errors:
            log.append(
                "goal.dispatch_rejected",
                {
                    "turn_no": (state.get("turn_no") or 0) + 1,
                    "errors": errors,
                    "failures": failures,
                },
            )
            return {"stop": None, "warnings": ["dispatch rejected: " + "; ".join(errors)]}
        return {}

    def run_dd_node(state: GoalGraphState) -> dict[str, Any]:
        """Execute the dispatched DD via the seam, then loop to read_control."""
        dispatch_obj = (state.get("last_stop") or {}).get("dispatch") or {}
        result = deps.run_dd(dict(dispatch_obj))
        run_scribe(state, trigger="dd.merged" if result.get("outcome") == "merged" else "dd.failed")
        history = events.fold(deps.event_log.read()).dd_history
        return {
            "last_dd": result,
            "dd_summary": _one_line_dd_summary(history, result),
            "turn_no": (state.get("turn_no") or 0) + 1,
            "stop": None,
        }

    def final_merge_node(state: GoalGraphState) -> dict[str, Any]:
        """release → target via the seam; only bounce back, never re-review.

        Boundary (deliberate, dd-18): on ``rebased`` / ``failed`` this module
        writes an event and injects the result as the next turn's handoff
        (``warnings``) so the Goal Agent can decide the next dispatch. GO-15's
        rule that a rebase which touched code must re-run CR → FR → Goal
        review in full is a later DD's graph — nothing here re-reviews.
        """
        log = deps.event_log
        if deps.final_merge is None:
            blocked = {
                "kind": "external",
                "detail": "final_merge seam is not configured; a done goal cannot finish",
            }
            return {"stop": "blocked", "summary": blocked["detail"], "blocked": blocked}
        stop, payload = deps.final_merge()
        if stop == "merged":
            log.append("goal.merged_to_target", dict(payload))
            log.append("goal.done", {"summary": state.get("summary") or ""})
            # §12 顺序要求：先落终态 event，再起书记员，使 seq 区间能覆盖到
            # goal.done 这一条。
            run_scribe(state, trigger="goal.done")
            return {"stop": "done"}
        handoff = f"final_merge {stop}: {_blurb(payload)}"
        log.append("goal.warning", {"message": handoff})
        # §12 触发点之一（goal.warning）：收尾的 handoff warning 起书记员；
        # 同样是「只在边界起，避免噪声」。
        run_scribe(state, trigger="goal.warning")
        return {"stop": None, "warnings": [handoff]}

    def finish_blocked(state: GoalGraphState) -> dict[str, Any]:
        blocked = dict(state.get("blocked") or {})
        deps.event_log.append("goal.blocked", {"summary": state.get("summary") or "", **blocked})
        # §12 顺序要求：先落终态 event，再起书记员，使 seq 区间能覆盖到
        # goal.blocked 这一条。
        run_scribe(state, trigger="goal.blocked")
        return {}

    graph = StateGraph(GoalGraphState)
    graph.add_node("read_control", read_control)
    graph.add_node("goal_turn", goal_turn)
    graph.add_node("validate_dispatch", validate_dispatch)
    graph.add_node("run_dd", run_dd_node)
    graph.add_node("final_merge", final_merge_node)
    graph.add_node("finish_blocked", finish_blocked)

    graph.add_edge(START, "read_control")
    graph.add_conditional_edges(
        "read_control",
        _route_after_read_control,
        {END: END, "goal_turn": "goal_turn"},
    )
    graph.add_conditional_edges(
        "goal_turn",
        _route_after_goal_turn,
        {
            "validate_dispatch": "validate_dispatch",
            "final_merge": "final_merge",
            "finish_blocked": "finish_blocked",
        },
    )
    graph.add_conditional_edges(
        "validate_dispatch",
        _route_after_validate,
        {"run_dd": "run_dd", "goal_turn": "goal_turn"},
    )
    graph.add_edge("run_dd", "read_control")
    graph.add_conditional_edges(
        "final_merge",
        _route_after_final_merge,
        {END: END, "goal_turn": "goal_turn", "finish_blocked": "finish_blocked"},
    )
    graph.add_edge("finish_blocked", END)
    return graph.compile(checkpointer=checkpointer)


def run_goal(
    deps: GoalDeps,
    *,
    goal_id: str,
    enroll: dict[str, Any],
    checkpointer: Any = None,
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the goal loop to a terminal stop; return ``{stop, summary, blocked}``.

    The loop has no hard turn cap (GO-6.4): ``recursion_limit`` is set to a
    deliberately huge value (``_RECURSION_LIMIT``) purely as a runaway-graph
    guard — one turn cycle costs ~4 graph steps, so this still allows ~250k
    turns — not as a loop limit. ``checkpointer`` is only a droppable cache:
    ``events.jsonl`` is the only source of truth (protocol §11), recovery
    replays the event log and never resumes from checkpointed state
    (design.md §7.1 / GO-16).

    ``initial_state`` is the resumed-start override (protocol §11): when it is
    ``None`` the loop starts exactly as before (``turn_no=0`` etc., byte-for-byte
    compatible with the pre-resume graph); otherwise its keys override the
    corresponding keys of the initial graph state (``turn_no`` / ``last_seq`` /
    ``dd_summary`` / ``warnings`` / ``last_dd`` …). The fold-upstream
    :func:`~fleet_graph.minimal.engine.resume_initial_state` produces it.
    """
    graph = build_goal_graph(deps, checkpointer=checkpointer)
    initial: GoalGraphState = {
        "goal_id": goal_id,
        "enroll": enroll,
        "turn_no": 0,
        "goal_version": 1,
        "last_stop": None,
        "last_dd": None,
        "dd_summary": "",
        "pending_messages": [],
        "warnings": [],
        "stop": None,
        "summary": None,
        "blocked": None,
        "last_seq": 0,
    }
    if initial_state is not None:
        initial.update(initial_state)
    final = graph.invoke(
        initial,
        config={
            "recursion_limit": _RECURSION_LIMIT,
            "configurable": {"thread_id": goal_id},
        },
    )
    return {
        "stop": final.get("stop"),
        "summary": final.get("summary"),
        "blocked": final.get("blocked"),
    }


__all__ = [
    "GoalDeps",
    "GoalGraphState",
    "build_goal_graph",
    "run_goal",
]

"""engine.py — 每 goal 一个常驻引擎进程（design §1 / §7.2, GO-7/9/10）。

DD-01..20 把最小系统的全部零件落在 ``fleet_graph.minimal`` 里，但没有任何入口把它们
接上：``goalgraph.GoalDeps.run_dd`` / ``final_merge`` 是空 seam，``ddgraph.DDDeps`` 的
``pr_open`` / ``pr_cleanup`` / ``pr_mergeable`` / ``merge_fn`` / ``bash_runner`` 也是
空 seam。这张 DD 就做接线：一个 ``main()`` 命令行入口 + 一个 ``run_engine()``，用各模块
已有的真实 API 把 goalgraph / ddgraph / mergegate / prlifecycle / agentrun 拼成一根线。

- **读 enroll**：只核 ``goal.enroll.json`` 存在 + ``schema`` 键正确（不重跑 enroll 校验，
  那是 MCP 侧的事）。缺失/损坏 → 非零退出、打一行错误、不抛裸栈。
- **build_deps**：构造 ``events.EventLog`` / ``control.ControlLog`` /
  ``gitgate.SubprocessGitRunner`` / acceptance 的 ``BashRunner`` / ``agentrun`` 的
  AgentInvoker（``--session-root`` 指 ``GoalRunRoot.sessions_dir``，harness 用
  ``harness.profile_for_role``），拼成 ``goalgraph.GoalDeps``。
- **run_dd seam**：用 ``ddgraph`` 跑一张 DD——``pr_open`` / ``pr_cleanup`` /
  ``pr_mergeable`` 取 ``prlifecycle`` 同名函数，``merge_fn`` 取 ``mergegate`` 的决策 +
  执行（GO-36：approve 后先看平台 mergeable，能合就合、冲突才转 Merge Agent），返回
  protocol §7 的 DD 结果对象交回 goalgraph。
- **final_merge seam**：``mergegate.final_merge_plan`` 的 release→target 收尾路径，
  每个 repo 交 Merge Agent 一次，返回 ``(stop, payload)``，stop ∈ merged/rebased/failed。
- **生命周期 event**：起来写 ``engine.started``（含 pid），退出写 ``engine.exiting``
  （reason ∈ done/blocked/stop）。kind 已在 events.py 注册（ENGINE_KINDS），本模块不加。
- **恢复 = 回放**（design §7.1 / GO-16）：起来只从 ``events.jsonl`` fold 出状态，绝不从
  LangGraph checkpointer 续跑（``run_goal`` / ``run_dd`` 都不传 checkpointer）；fold 出终态
  就直接按终态退出，不再重跑。

退出码：done→0、blocked→1、stopped→0、启动期错误→2。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fleet_graph.minimal import (
    acceptance,
    agentrun,
    control,
    ddflow,
    ddgraph,
    enroll,
    events,
    gitgate,
    goalgraph,
    harness,
    mergegate,
    prlifecycle,
    prompts,
    runroot,
    stagerunner,
    steer,
    workfolder,
)
from fleet_graph.minimal import (
    dispatch as dispatch_mod,
)

EXIT_DONE = 0
EXIT_BLOCKED = 1
EXIT_STOPPED = 0
EXIT_STARTUP_ERROR = 2

_REASON_BY_STOP = {"done": "done", "blocked": "blocked", "stopped": "stop"}
_EXIT_CODE_BY_STOP = {
    "done": EXIT_DONE,
    "blocked": EXIT_BLOCKED,
    "stopped": EXIT_STOPPED,
}
_EXIT_CODE_BY_STATE = {
    "done": EXIT_DONE,
    "blocked": EXIT_BLOCKED,
    "stopped": EXIT_STOPPED,
}


class StartupError(Exception):
    """A startup-time failure (missing / corrupt enroll); its message is one printed line."""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fleet_graph.minimal.engine",
        description="run the engine process for one enrolled goal",
    )
    parser.add_argument("--goal-id", required=True, help="the enrolled goal id (g-<6 hex>)")
    parser.add_argument(
        "--engine-root",
        default=runroot.DEFAULT_ENGINE_ROOT,
        help="the engine state root the goal run root lives under (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code (never raises on startup errors)."""
    args = build_parser().parse_args(argv)
    return run_engine(args.goal_id, engine_root=args.engine_root)


# ---------------------------------------------------------------------------
# reading enroll (no re-validation — that is the MCP's job)
# ---------------------------------------------------------------------------


def _read_enroll(run_root: runroot.GoalRunRoot) -> dict[str, Any]:
    """Read + shape-check the enroll object; raise ``StartupError`` on any failure.

    Only the file's existence and its ``schema`` key are checked (spec 第 2 条): the
    full field-level validation was already done by the MCP's validation node.
    """
    if not run_root.enroll_path.exists():
        raise StartupError(f"engine: no enroll file at {run_root.enroll_path}")
    try:
        obj = json.loads(run_root.enroll_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StartupError(f"engine: unreadable enroll {run_root.enroll_path}: {exc}") from exc
    if not isinstance(obj, dict) or obj.get("schema") != enroll.SCHEMA:
        raise StartupError(
            f"engine: enroll {run_root.enroll_path} is not a {enroll.SCHEMA!r} object"
        )
    return obj


# ---------------------------------------------------------------------------
# the real agent seam: run one stage through agent-run
# ---------------------------------------------------------------------------


def _argv_value(argv: list[str], flag: str) -> str | None:
    """The value following ``flag`` in ``argv``, or None when absent."""
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _apply_harness_profile(argv: list[str]) -> list[str]:
    """Rewrite ``--harness <role>`` to the shipped profile name.

    ``agentrun.build_argv`` bakes ``--harness <role>`` (its ``AgentCall`` default);
    the six shipped profiles are ``minimal-<role>`` (dd-20), so the engine resolves the
    profile through ``harness.profile_for_role`` before the call reaches agent-run.
    """
    role = _argv_value(argv, "--role")
    if role is None:
        return list(argv)
    result = list(argv)
    for index, token in enumerate(result):
        if token == "--harness" and index + 1 < len(result):
            result[index + 1] = harness.profile_for_role(role)
            break
    return result


class AgentRuntimeInvoker:
    """The production ``stagerunner.AgentInvoker``: run one stage through ``agent-run``.

    ``stagerunner`` hands over the argv built by ``agentrun.build_argv`` (with
    ``--session-root`` already pointing at the goal's ``sessions/`` dir, GO-22.2) plus
    the rendered system / user prompts. This invoker (spec 第 3 条):

    1. resolves the ``--harness`` value to ``harness.profile_for_role``;
    2. passes the prompts to agent-run — the system prompt (when present) first, then
       this round's user prompt — on stdin, matching protocol §0.9 (the user prompt
       *is* the protocol input object);
    3. runs it ``shell=False`` (argv is always a list) with the stage timeout, and
       returns ``(exit_code, stdout)`` per the AgentInvoker contract. Non-zero exit is
       graded by the caller (``stagerunner.run_stage``), never re-run here.
    """

    def __init__(self, *, timeout_s: int) -> None:
        self._timeout_s = timeout_s

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        argv = _apply_harness_profile(argv)
        stdin = ((system_prompt + "\n\n") if system_prompt else "") + user_prompt
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, ""
        except OSError:
            return -1, ""
        return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------
# shared injected pieces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Wiring:
    """The shared injected seams the engine threads into run_dd / final_merge."""

    log: events.EventLog
    agent_invoker: stagerunner.AgentInvoker
    git_runner: gitgate.GitRunner
    bash_runner: acceptance.Runner
    gh_runner: mergegate.GhRunner
    goal: dict[str, Any]
    release_branch: str
    session_root: str
    session_policies: dict[str, dict[str, Any]] | None
    model_by_role: dict[str, str] | None
    timeout_s: int
    warn_dd_rounds: int


def _goal_acceptance(goal: dict[str, Any]) -> list[str]:
    """The goal's base acceptance, flattened order-preserving (mirrors ddgraph).

    Reads a top-level ``acceptance`` list when present (the older ``goal.enroll/1``
    shape) and falls back to flattening the per-repo ``repos[].acceptance`` of
    ``goal.enroll/2``.
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


def _next_dd_id(log: events.EventLog) -> str:
    """The next DD id, derived from the number of ``dd.dispatched`` events already
    folded (events.jsonl is the only source of truth, so the id is stable across replay)."""
    count = sum(1 for ev in log.read() if ev.kind == "dd.dispatched")
    return f"dd-{count + 1:02d}"


# ---------------------------------------------------------------------------
# the shared merge-agent step (kind=dd and kind=release both run it)
# ---------------------------------------------------------------------------


def _run_merge_agent(
    wiring: _Wiring,
    *,
    kind: str,
    dd_id: str | None,
    workspace: str,
    repos: list[gitgate.RepoRef],
    source_branch: str,
    source_head: str,
    target_branch: str,
    target_head: str,
    acceptance_cmds: list[str],
    run_id: str,
) -> tuple[str, dict[str, Any]]:
    """Run one Merge Agent call; return ``(stop, payload)``.

    The Merge Agent is the only merger (GO-15): both the DD merge (kind=dd) and the
    release→target closing merge (kind=release) funnel through ``stagerunner.run_stage``
    with ``stage="merge"``. The ``dd.stage.finished`` event that stagerunner emits for the
    merge stage is dropped — the caller records the authoritative one (ddgraph's merge
    node for kind=dd; nothing, for kind=release, whose result is ``goal.merged_to_target``).
    """
    in_obj = prompts.build_merge_in(
        kind=kind,
        dd_id=dd_id,
        workspace=workspace,
        source_branch=source_branch,
        source_head=source_head,
        target_branch=target_branch,
        target_head=target_head,
        acceptance=acceptance_cmds,
    )
    request = stagerunner.StageRequest(
        stage="merge",
        run_id=run_id,
        in_obj=in_obj,
        repos=list(repos),
        expected_schema=agentrun.schema_for("merge"),
        policy=agentrun.resolve_session_policy("merge", wiring.session_policies),
        cwd=workspace,
        is_first_call=True,
        session_root=wiring.session_root,
        timeout_s=wiring.timeout_s,
        model=(wiring.model_by_role or {}).get("merge"),
    )
    outcome = stagerunner.run_stage(
        request, git_runner=wiring.git_runner, agent_invoker=wiring.agent_invoker
    )
    for kind_, payload in outcome.events:
        if kind_ == "dd.stage.finished":
            continue
        wiring.log.append(kind_, payload, dd_id=dd_id)
    if not outcome.ok:
        return ("failed", {"detail": outcome.invalid_reason or "merge agent failed"})
    obj = outcome.obj or {}
    payload = {key: value for key, value in obj.items() if key not in ("schema", "stop")}
    return (outcome.stop, payload)


# ---------------------------------------------------------------------------
# the DD merge seam: mergegate decide + execute (GO-36)
# ---------------------------------------------------------------------------


def _merge_fn(wiring: _Wiring, release_head: str) -> Any:
    """The DD's ``merge_fn``: GO-36 route (platform mergeable → merge, else Merge Agent).

    The DD's single PR (``repos[0]``) is routed by ``mergegate.decide`` over the
    platform's mergeable verdict: MERGEABLE → ``mergegate.platform_merge`` (gh); conflict
    or unknown → the Merge Agent, run through ``_run_merge_agent`` (GO-36 / context.md).
    """

    def merge_fn(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        dd_id = state["dd_id"]
        repo = state["repos"][0]
        path = repo["path"]
        pr_info = (state.get("prs") or {}).get(path) or {}
        pr_ref = prlifecycle.PrRef(number=int(pr_info["number"]), url=pr_info["url"])
        decision = mergegate.decide(
            pr_ref,
            mergeable_fn=lambda pr: prlifecycle.pr_mergeable(
                path, pr.number, runner=wiring.git_runner
            ),
        )
        if decision.route == mergegate.MergeRoute.PLATFORM_MERGE:
            return mergegate.platform_merge(pr_ref, gh_runner=wiring.gh_runner)
        source_head = (
            gitgate.remote_tip(path, repo["remote"], repo["branch"], runner=wiring.git_runner) or ""
        )
        refs = dispatch_mod.dd_repo_refs({"repos": [repo]})
        return _run_merge_agent(
            wiring,
            kind="dd",
            dd_id=dd_id,
            workspace=path,
            repos=refs,
            source_branch=repo["branch"],
            source_head=source_head,
            target_branch=wiring.release_branch,
            target_head=release_head,
            acceptance_cmds=list(state.get("acceptance_cmds") or []),
            run_id=f"{dd_id}-merge-{state.get('round') or 1}",
        )

    return merge_fn


def _run_dd_seam(wiring: _Wiring, enroll_obj: dict[str, Any]) -> Any:
    """The goalgraph ``run_dd`` seam: wire DDDeps and run one DD via ``ddgraph.run_dd``.

    goalgraph calls the seam with the dispatch object only (a fresh DD: the id
    is derived from the folded ``dd.dispatched`` count). The engine's §11
    resume path additionally passes ``dd_id`` (the in-flight DD) plus
    ``initial_state`` (the ``ddgraph.resume_entry`` fold) to re-enter the
    ddgraph at the lost stage instead of starting a new DD.
    """

    def run_dd(
        dispatch_obj: dict[str, Any],
        *,
        dd_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repos = list(dispatch_obj.get("repos") or [])
        if dd_id is None:
            dd_id = _next_dd_id(wiring.log)
        first = repos[0]
        release_head = (
            gitgate.remote_tip(
                first["path"], first["remote"], wiring.release_branch, runner=wiring.git_runner
            )
            or ""
        )
        acceptance_cmds = dispatch_mod.dd_acceptance(dispatch_obj, _goal_acceptance(wiring.goal))
        deps = ddgraph.DDDeps(
            event_log=wiring.log,
            agent_invoker=wiring.agent_invoker,
            git_runner=wiring.git_runner,
            bash_runner=wiring.bash_runner,
            pr_open=prlifecycle.open_pr,
            pr_cleanup=prlifecycle.cleanup_dd,
            pr_mergeable=prlifecycle.pr_mergeable,
            merge_fn=_merge_fn(wiring, release_head),
            goal=wiring.goal,
            release_branch=wiring.release_branch,
            release_head=release_head,
            session_policies=wiring.session_policies,
            session_root=wiring.session_root,
            model_by_role=wiring.model_by_role,
            timeout_s=wiring.timeout_s,
            warn_dd_rounds=wiring.warn_dd_rounds,
        )
        return ddgraph.run_dd(
            deps,
            goal_id=wiring.log.goal_id,
            dd_id=dd_id,
            repos=repos,
            spec_text=dispatch_obj.get("spec_text") or "",
            acceptance_cmds=acceptance_cmds,
            initial_state=initial_state,
        )

    return run_dd


def _final_merge_seam(wiring: _Wiring, enroll_obj: dict[str, Any]) -> Any:
    """The goalgraph ``final_merge`` seam: release→target, one Merge Agent per repo."""
    release_branch = wiring.release_branch

    def final_merge() -> tuple[str, dict[str, Any]]:
        plans = mergegate.final_merge_plan(enroll_obj, release_branch=release_branch)
        merged_commits: list[str] = []
        for index, plan in enumerate(plans):
            source_head = (
                gitgate.remote_tip(plan.repo_id, plan.remote, plan.source, runner=wiring.git_runner)
                or ""
            )
            target_head = (
                gitgate.remote_tip(plan.repo_id, plan.remote, plan.target, runner=wiring.git_runner)
                or ""
            )
            refs = [
                gitgate.RepoRef(
                    worktree=plan.repo_id,
                    remote=plan.remote,
                    branch=plan.source,
                    label=plan.repo_id,
                )
            ]
            stop, payload = _run_merge_agent(
                wiring,
                kind="release",
                dd_id=None,
                workspace=plan.repo_id,
                repos=refs,
                source_branch=plan.source,
                source_head=source_head,
                target_branch=plan.target,
                target_head=target_head,
                acceptance_cmds=_goal_acceptance(wiring.goal),
                run_id=f"{wiring.log.goal_id}-release-merge-{index + 1}",
            )
            if stop != "merged":
                return (stop, payload)
            if isinstance(payload.get("merged_commit"), str):
                merged_commits.append(payload["merged_commit"])
        return ("merged", {"repos": len(plans), "merged_commit": merged_commits})

    return final_merge


# ---------------------------------------------------------------------------
# dependency wiring
# ---------------------------------------------------------------------------


def build_deps(
    run_root: runroot.GoalRunRoot,
    enroll_obj: dict[str, Any],
    *,
    agent_invoker: stagerunner.AgentInvoker | None = None,
    git_runner: gitgate.GitRunner | None = None,
    bash_runner: acceptance.Runner | None = None,
    gh_runner: mergegate.GhRunner | None = None,
    session_policies: dict[str, dict[str, Any]] | None = None,
    model_by_role: dict[str, str] | None = None,
    timeout_s: int = 300,
    warn_turns: int = 30,
    warn_dd_rounds: int = 6,
    scribe_enabled: bool = True,
) -> goalgraph.GoalDeps:
    """Wire every IO seam to its real module and return a ready ``GoalDeps``.

    ``run_dd`` and ``final_merge`` are the two goalgraph seams built here from
    ``ddgraph`` / ``prlifecycle`` / ``mergegate``. The injectables default to the real
    subprocess-based implementations and exist so tests can swap in fakes.

    ``scribe_enabled`` defaults to True so the read-only scribe (GO-21) runs in
    production; tests that do not script the scribe role pass False explicitly.
    """
    log = events.EventLog(run_root.root)
    control_log = control.ControlLog(run_root.root)
    invoker = agent_invoker or AgentRuntimeInvoker(timeout_s=timeout_s)
    git = git_runner or gitgate.SubprocessGitRunner()
    bash = bash_runner or acceptance.BashRunner()
    gh = gh_runner or mergegate.SubprocessGhRunner()
    goal, _version = steer.current_goal(enroll_obj, log.read())
    release_branch = runroot.release_branch(enroll_obj)
    # GO-19 / GO-34: WF is a first-class citizen of enroll. A bound work_folder gets
    # the real MCP writer; None degrades to the null writer (never blocks the goal).
    # ``work_folder`` is a steer-immutable field, so the enroll value is authoritative.
    bound_wf = enroll_obj.get("work_folder")
    if isinstance(bound_wf, str) and bound_wf:
        wf_writer: workfolder.WorkFolderWriter = workfolder.McpWorkFolderWriter(
            git, cwd=str(run_root.root)
        )
    else:
        wf_writer = workfolder.NullWorkFolderWriter()
    wiring = _Wiring(
        log=log,
        agent_invoker=invoker,
        git_runner=git,
        bash_runner=bash,
        gh_runner=gh,
        goal=goal,
        release_branch=release_branch,
        session_root=str(run_root.sessions_dir),
        session_policies=session_policies,
        model_by_role=model_by_role,
        timeout_s=timeout_s,
        warn_dd_rounds=warn_dd_rounds,
    )
    return goalgraph.GoalDeps(
        event_log=log,
        control_log=control_log,
        agent_invoker=invoker,
        git_runner=git,
        run_dd=_run_dd_seam(wiring, enroll_obj),
        final_merge=_final_merge_seam(wiring, enroll_obj),
        wf_writer=wf_writer,
        warn_turns=warn_turns,
        session_root=str(run_root.sessions_dir),
        session_policies=session_policies,
        model_by_role=model_by_role,
        timeout_s=timeout_s,
        scribe_enabled=scribe_enabled,
    )


# ---------------------------------------------------------------------------
# the engine lifecycle (start → goal loop → exit)
# ---------------------------------------------------------------------------


def _fold_terminal_state(log: events.EventLog) -> str | None:
    """The folded terminal state (done/blocked/stopped), or None when still running."""
    derived = events.fold(log.read())
    return derived.state if derived.terminal else None


def _engine_has_run(events_list: list[events.Event]) -> bool:
    """Whether the goal loop has actually begun (the engine has run at least once).

    The only event written before the engine first starts is ``goal.enrolled``
    (the MCP enrollment, protocol §10); every other kind implies the engine was
    up. A log that is empty or only carries ``goal.enrolled`` is therefore a
    *fresh* goal — first spawn, so ``engine.started`` — whereas any later event
    (a turn, a DD, a control line) means the engine already ran and recovery is
    replay (``engine.resumed``, protocol §11).
    """
    return any(ev.kind != "goal.enrolled" for ev in events_list)


def _write_exiting(log: events.EventLog, reason: str) -> None:
    """Write the terminal ``engine.exiting`` event, deduped against the last event."""
    existing = list(log.read())
    if existing and existing[-1].kind == "engine.exiting":
        return
    log.append("engine.exiting", {"reason": reason})


# ---------------------------------------------------------------------------
# recovery = replay (protocol §11)
# ---------------------------------------------------------------------------

_LOST_ON_RESTART = "lost_on_restart"

# protocol §11 only checks the release_head / head_commit the engine recorded on
# these three boundaries; if none of them carries a sha there is nothing to
# verify and the state-mismatch gate is skipped.
_GIT_STATE_KINDS: tuple[str, ...] = ("dd.pr_opened", "dd.merged", "goal.merged_to_target")


def _full_sha(value: Any) -> str | None:
    """The 40-hex sha ``value`` is, or None when it is not (never guess)."""
    if not isinstance(value, str) or len(value) != 40:
        return None
    return value if all(ch in "0123456789abcdef" for ch in value) else None


def _control_cursor(events_list: list[events.Event]) -> int:
    """The control.jsonl cursor: the largest ``seq`` of any ``control.received`` event.

    This is the control-log seq space (each control.jsonl line carries its own
    ``seq``), NOT the event seq space; ``goalgraph``'s ``read_control`` re-drains
    only control lines whose seq is greater than this value, so a resume never
    re-plays — and never re-records ``control.received`` for — lines already
    consumed before the crash.
    """
    cursor = 0
    for ev in events_list:
        if ev.kind != "control.received":
            continue
        seq = (ev.payload or {}).get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            cursor = max(cursor, seq)
    return cursor


def _dd_summary_line(history: list[events.DDSummary]) -> str:
    """GO-17's one-line ``dd_summary`` from a folded ``dd_history`` (never a table).

    Shaped identically to ``goalgraph._one_line_dd_summary``; a folded history
    carries no impl summary / failure detail (those live in the not-yet-rebuilt
    DD result object), so the last DD's blurb is its folded outcome.
    """
    if not history:
        return ""
    merged = sum(1 for item in history if item.outcome == "merged")
    failed = sum(1 for item in history if item.outcome == "failed")
    last = history[-1]
    return f"{len(history)} 张 DD：{merged} merged，{failed} failed（{last.dd_id}：{last.outcome}）"


def resume_initial_state(events_list: Iterable[events.Event]) -> dict[str, Any] | None:
    """Fold the event log into the ``goalgraph.run_goal`` resumed-start overrides.

    ``events.jsonl`` is the only source of truth (protocol §11): recovery is
    replay, and this is the pure fold-to-initial-state step. Returns ``None``
    when there is nothing to resume — an empty log (fresh goal) or a terminal
    fold — so ``run_engine`` falls back to today's fresh-start / immediate-exit
    paths.

    ``turn_no`` alignment: in ``goalgraph`` the ``goal_turn`` node starts a turn
    by injecting ``state["turn_no"] + 1`` and only the ``run_dd`` node bumps the
    stored ``turn_no`` (``+1``) once a DD completes. ``events.fold`` counts
    ``goal.turn.started`` events, i.e. turns *started*, which during an in-flight
    turn is one ahead of the stored value. So:

    - a lost ``goal_turn`` (``restart_step`` / stage ``goal_turn``) re-runs the
      turn ``fold`` already counted; it must come out as ``turn_no + 1 ==
      fold.turn_no``, hence the stored ``turn_no`` is ``fold.turn_no - 1``;
    - otherwise the next turn is a *new* turn and must start at ``fold.turn_no +
      1``, hence the stored ``turn_no`` stays ``fold.turn_no``.
    """
    evs = list(events_list)
    if not evs:
        return None
    derived = events.fold(evs)
    point = events.resume_point(evs)
    if point.action == "exit":
        return None
    turn_no = derived.turn_no
    if point.action == "restart_step" and point.stage == "goal_turn":
        turn_no -= 1
    return {
        "turn_no": turn_no,
        "goal_version": derived.goal_version,
        "last_seq": _control_cursor(evs),
        "dd_summary": _dd_summary_line(derived.dd_history),
        "warnings": list(derived.warnings),
    }


def _recorded_head(events_list: list[events.Event]) -> str | None:
    """The most recently recorded ``release_head``/``head_commit`` sha, or None."""
    for ev in reversed(events_list):
        if ev.kind not in _GIT_STATE_KINDS:
            continue
        payload = ev.payload or {}
        for key in ("release_head", "head_commit"):
            sha = _full_sha(payload.get(key))
            if sha is not None:
                return sha
    return None


def _state_mismatch(
    events_list: list[events.Event],
    enroll_obj: dict[str, Any],
    git_runner: gitgate.GitRunner,
) -> dict[str, Any] | None:
    """The §11 git state-mismatch gate: block on rewritten state, never guess.

    Code state is git's truth and is NOT replayed by recovery. Before resuming,
    verify the most recently recorded ``release_head`` / ``head_commit`` (in
    ``dd.pr_opened`` / ``dd.merged`` / ``goal.merged_to_target``) is still
    reachable from the release branch's remote tip; if it is not, the branch was
    rewritten underneath us and a ``state_mismatch`` blocked payload is returned.
    Returns ``None`` when there is no recorded sha to verify (skip) or when the
    recorded sha is still reachable.
    """
    sha = _recorded_head(events_list)
    if sha is None:
        return None
    repos = enroll_obj.get("repos") or []
    if not repos:
        return None
    try:
        release = runroot.release_branch(enroll_obj)
    except ValueError:
        return None
    repo = repos[0]
    if gitgate.revision_reachable(repo["path"], repo["remote"], release, sha, runner=git_runner):
        return None
    detail = (
        f"recorded release_head/head_commit {sha} is no longer reachable on "
        f"remote {repo['remote']}/{release}; refusing to resume on rewritten "
        "git state (protocol §11)"
    )
    return {
        "kind": "state_mismatch",
        "detail": detail,
        "release_head": sha,
        "release_branch": release,
    }


def _dispatch_for_dd(events_list: list[events.Event], dd_id: str) -> dict[str, Any] | None:
    """The dispatch object of the turn that spawned ``dd_id``, folded from the log.

    The dispatching turn's ``goal.turn.finished`` payload carries the whole Stop
    object — including ``dispatch`` — and always precedes ``dd.dispatched``
    (stagerunner writes it inside the goal_turn node, before the graph routes to
    the run_dd node), so the newest one before the DD's dispatch is the DD's
    own dispatch. ``None`` when it cannot be folded — a log no real run
    produces.
    """
    dd_seq = next(
        (ev.seq for ev in events_list if ev.kind == "dd.dispatched" and ev.dd_id == dd_id),
        None,
    )
    if dd_seq is None:
        return None
    for ev in reversed(events_list):
        if ev.seq >= dd_seq:
            continue
        if ev.kind == "goal.turn.finished":
            dispatch = (ev.payload or {}).get("dispatch")
            if isinstance(dispatch, dict):
                return dict(dispatch)
    return None


def _unconsumed_dd(events_list: list[events.Event]) -> str | None:
    """The last dispatched DD id when its result never reached a next turn.

    goalgraph consumes a DD's result the moment the *next* turn starts (its
    ``goal.turn.in/1`` carries ``last_dd``). The last dispatched DD is therefore
    unconsumed when no ``goal.turn.started`` follows its ``dd.dispatched``: it
    either is still in flight (fold ``current_dd``, resumed elsewhere) or it
    already resolved (``dd.merged`` / ``dd.failed``) and the crash lost the
    handoff — the caller folds the result object and injects it, never
    re-entering the graph (protocol §11).

    Only the *last* dispatch is judged: a ``goal.turn.started`` that follows an
    earlier DD's dispatch merely consumed that DD — a DD dispatched after it is
    a fresh handoff that needs a later turn of its own, so the scan never stops
    early and the answer is whether a turn started after the final dispatch.
    """
    dd_id: str | None = None
    dispatch_seq = 0
    for ev in events_list:
        if ev.kind == "dd.dispatched" and ev.dd_id is not None:
            dd_id = ev.dd_id
            dispatch_seq = ev.seq
        elif ev.kind == "goal.turn.started" and dd_id is not None and ev.seq > dispatch_seq:
            dd_id = None
    return dd_id


def _dd_handoff(
    initial_state: dict[str, Any], log: events.EventLog, result: dict[str, Any]
) -> None:
    """Inject a finished DD's result as the next turn's handoff (mirrors run_dd).

    The same handoff goalgraph's ``run_dd`` node makes: the result lands in
    ``last_dd`` with the one-line ``dd_summary`` (GO-17). The next turn number
    is already right — ``resume_initial_state`` folded the dispatching turn's
    ``goal.turn.started`` without the completed cycle, so ``turn_no + 1`` in the
    goal graph equals what ``run_dd``'s ``+1`` would have stored.
    """
    history = events.fold(log.read()).dd_history
    initial_state["last_dd"] = result
    initial_state["dd_summary"] = goalgraph._one_line_dd_summary(history, result)


def _resume_run_dd(
    deps: goalgraph.GoalDeps,
    dispatch_obj: dict[str, Any],
    *,
    dd_id: str,
    initial_state: dict[str, Any],
) -> dict[str, Any]:
    """Re-enter the ddgraph at a §11 resume point through the ``run_dd`` seam.

    The seam closure built by :func:`_run_dd_seam` accepts the resume-only
    ``dd_id`` / ``initial_state`` kwargs; ``GoalDeps.run_dd`` types only the
    fresh one-argument call goalgraph makes, so the wider call is made here.
    """
    run_dd: Any = deps.run_dd
    return run_dd(dispatch_obj, dd_id=dd_id, initial_state=initial_state)


def run_engine(
    goal_id: str,
    *,
    engine_root: str = runroot.DEFAULT_ENGINE_ROOT,
    agent_invoker: stagerunner.AgentInvoker | None = None,
    git_runner: gitgate.GitRunner | None = None,
    bash_runner: acceptance.Runner | None = None,
    gh_runner: mergegate.GhRunner | None = None,
    session_policies: dict[str, dict[str, Any]] | None = None,
    model_by_role: dict[str, str] | None = None,
    timeout_s: int = 300,
    warn_turns: int = 30,
    warn_dd_rounds: int = 6,
    scribe_enabled: bool = True,
) -> int:
    """Run one goal's engine lifecycle to a terminal stop and return the exit code.

    Startup errors (bad goal id, missing / corrupt enroll) print one line to stderr and
    return 2. Recovery is replay (design §7.1 / GO-16): the state is folded from
    ``events.jsonl`` and a terminal fold exits immediately without re-running; otherwise
    the loop runs through ``goalgraph.run_goal`` with no checkpointer.

    Logs that already show engine progress (anything beyond ``goal.enrolled``)
    and are non-terminal resume (protocol §11): the engine writes
    ``engine.resumed``, then dispatches on ``resume_point.action``:
    a lost ``goal_turn`` is re-run at the same turn number; an in-flight DD is
    re-entered at its lost stage; any other boundary continues to the next
    turn. A state-mismatch (a recorded release_head/head_commit no longer
    reachable on the release branch) blocks with
    ``goal.blocked(kind=state_mismatch)`` and exit 1 — never guessed at.

    In-flight DD resume (protocol §11, per-stage): the lost agent run is
    recorded as ``agent.failed(detail=lost_on_restart)``, ``ddgraph.resume_entry``
    folds the DD's events into an entry node plus state overrides, and the
    ddgraph is re-entered through the ``run_dd`` seam with ``initial_state`` —
    already-finished stages (impl commits, CR / FR conclusions) are never
    re-run. A DD that already resolved (``dd.merged`` / ``dd.failed``) but whose
    handoff to the next turn the crash lost is not re-entered at all: its
    folded result object is injected as the next turn's ``last_dd`` handoff.
    """
    try:
        run_root = runroot.goal_run_root(goal_id, engine_root=engine_root)
    except ValueError as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return EXIT_STARTUP_ERROR
    try:
        enroll_obj = _read_enroll(run_root)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_STARTUP_ERROR

    deps = build_deps(
        run_root,
        enroll_obj,
        agent_invoker=agent_invoker,
        git_runner=git_runner,
        bash_runner=bash_runner,
        gh_runner=gh_runner,
        session_policies=session_policies,
        model_by_role=model_by_role,
        timeout_s=timeout_s,
        warn_turns=warn_turns,
        warn_dd_rounds=warn_dd_rounds,
        scribe_enabled=scribe_enabled,
    )

    terminal = _fold_terminal_state(deps.event_log)
    if terminal is not None:
        return _EXIT_CODE_BY_STATE[terminal]

    events_list = list(deps.event_log.read())
    if not _engine_has_run(events_list):
        # fresh goal (empty log, or only the enrollment event): today's
        # byte-for-byte behavior — write engine.started, no engine.resumed
        deps.event_log.append("engine.started", {"pid": os.getpid()})
        result = goalgraph.run_goal(deps, goal_id=goal_id, enroll=enroll_obj)
        stop = result.get("stop") or "blocked"
        reason = _REASON_BY_STOP.get(stop, "blocked")
        _write_exiting(deps.event_log, reason)
        return _EXIT_CODE_BY_STOP.get(stop, EXIT_STARTUP_ERROR)

    # non-empty + non-terminal → resume (protocol §11)
    derived = events.fold(events_list)
    point = events.resume_point(events_list)
    deps.event_log.append("engine.resumed", {"pid": os.getpid(), "from_seq": derived.last_seq})

    mismatch = _state_mismatch(events_list, enroll_obj, deps.git_runner)
    if mismatch is not None:
        deps.event_log.append("goal.blocked", {"summary": mismatch["detail"], **mismatch})
        _write_exiting(deps.event_log, "blocked")
        return EXIT_BLOCKED

    initial_state = resume_initial_state(events_list)
    assert initial_state is not None  # non-empty + non-exit => never None

    if point.action == "restart_step" and point.stage == "goal_turn":
        deps.event_log.append("agent.failed", {"stage": "goal_turn", "detail": _LOST_ON_RESTART})
        # resume_initial_state already rolled turn_no back one, so the re-run
        # turn number equals the lost one.
    elif derived.current_dd.dd_id is not None:
        dd_id = derived.current_dd.dd_id
        stage = point.stage or "impl"
        deps.event_log.append(
            "agent.failed", {"stage": stage, "detail": _LOST_ON_RESTART}, dd_id=dd_id
        )
        entry, dd_initial = ddgraph.resume_entry(events_list, dd_id)
        dispatch_obj = _dispatch_for_dd(events_list, dd_id)
        if dispatch_obj is None:
            # not a log any real run produces (the dispatching turn's Stop
            # object is gone): the DD cannot be re-entered, so close it as
            # lost rather than guess at its repos / spec / acceptance.
            deps.event_log.append(
                "dd.failed",
                {
                    "stage": stage,
                    "detail": (
                        f"{_LOST_ON_RESTART}: dispatch object for {dd_id} could not be "
                        "folded from the event log; the DD ends here and is handed "
                        "back to the Goal Agent"
                    ),
                },
                dd_id=dd_id,
            )
            _dd_handoff(
                initial_state, deps.event_log, ddflow.build_dd_result(deps.event_log.read(), dd_id)
            )
            initial_state["warnings"] = [
                *list(initial_state.get("warnings") or []),
                (
                    f"DD {dd_id} 在引擎恢复时于 stage {stage} 丢失（lost_on_restart），"
                    "且其 dispatch 无法从事件日志折出，已以 failed 收场交回你决定"
                ),
            ]
        elif entry == ddgraph.RESUME_TERMINAL:
            # defensive: the fold said in-flight but the DD's events say it
            # already resolved — take the result object, never re-enter.
            _dd_handoff(initial_state, deps.event_log, ddflow.build_dd_result(events_list, dd_id))
        else:
            result = _resume_run_dd(
                deps,
                dispatch_obj,
                dd_id=dd_id,
                initial_state={"entry": entry, **dd_initial},
            )
            _dd_handoff(initial_state, deps.event_log, result)
    else:
        unconsumed = _unconsumed_dd(events_list)
        if unconsumed is not None:
            # the DD already resolved (dd.merged / dd.failed) but the crash
            # lost the handoff: fold the result object into the next turn and
            # never re-enter the graph (protocol §11).
            _dd_handoff(
                initial_state, deps.event_log, ddflow.build_dd_result(events_list, unconsumed)
            )
        # else: rerun_acceptance / next_step → straight into the next turn.

    result = goalgraph.run_goal(
        deps, goal_id=goal_id, enroll=enroll_obj, initial_state=initial_state
    )
    stop = result.get("stop") or "blocked"
    reason = _REASON_BY_STOP.get(stop, "blocked")
    _write_exiting(deps.event_log, reason)
    return _EXIT_CODE_BY_STOP.get(stop, EXIT_STARTUP_ERROR)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())

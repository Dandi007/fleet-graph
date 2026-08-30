"""The supervisor graph: one event-triggered audit turn, as seven nodes.

    intake -> gather_evidence -> rerun_acceptance -> audit(llm) -> classify
        -> act -> receipt -> END

Script nodes bracket the single llm node, and the llm only ever *advises*:
`classify` gates the classification on mechanical predicates. Three
classifications exist: `needs_human`, `recommend_reject`, and (since R4-3)
`preauth_release` -- the fourth gate. A release is not the supervisor's own
judgement (§38b: a complete evidence chain is not a merge verdict): it is the
mechanical application of a *human-issued* preauth on the board, checked by
the three-factor predicate in `supervise/preauth.py` (coverage, git-anchored
target ref in a prefix allowlist, honest attribution), and any missing factor
degrades to needs_human -- no error, no guess.

Three structural refusals, each pinned by scripts/check_supervisor_conformance.py
or by the module simply not importing the capability:

- **Decisions publish through exactly one door.** Only
  `supervise/decision_publisher.py` may construct a `work.decision.v1`
  publish (Guard B), only this module's `act` script node may import it
  (Guard C), and its credential never reaches an agent subprocess env
  (executors/agent_run.py scrubs the namespace). The published decision is
  `scope: merge_only` and its allowlist constructively cannot cover
  main/master/production -- promotion stays human, by validation not policy.
- **This module cannot schedule.** It never imports `scheduler.ignition` or
  `scheduler.launcher`; it is a *scheduled* thing (launched as a transient
  unit by the observer), never a second scheduler (r4-design §5, D9).
- **It never writes into the line it is auditing.** Its outputs are a board
  note, at most one preauth-released decision, and files under its own state
  root. §38e's governance livelock -- the supervisor locked out of the work
  folder it was trying to write into -- is resolved by not wanting to write
  there at all.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.bus.board import DECISION_KIND_V2, NOTE_KIND, WORK_NOTES, Board, GateTicket
from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient
from fleet_graph.dd.control_plane import DEFAULT_DD_ROOT, RECORD_FILE
from fleet_graph.executors.agent_run import (
    AgentRunLauncher,
    AgentRunSpec,
    RunWaitTimeout,
    derive_run_id,
)
from fleet_graph.graphs.adapters import CoordinatorFault, parse_envelope
from fleet_graph.state.run_artifacts import write_json_durable
from fleet_graph.supervise.audit import (
    DEFAULT_ENGINE_URL,
    Assertion,
    AuditReport,
    GraphEngineSource,
    OldEngineClient,
    audit_development,
    audit_goal_line,
)
from fleet_graph.supervise.decision_publisher import publish_release_decision
from fleet_graph.supervise.events import (
    EVENT_APPROVED_UNHARVESTED,
    EVENT_BLOCKED_DECISION,
    EVENT_BOARD_QUESTION,
    EVENT_CAP_BREAKER,
    EVENT_LINE_FAULT,
    SupervisorEvent,
    validate_event,
)
from fleet_graph.supervise.harvest import (
    DEFAULT_BRANCH as DEFAULT_HARVEST_BRANCH,
)
from fleet_graph.supervise.harvest import (
    DEFAULT_VERIFY_ARGV as DEFAULT_HARVEST_VERIFY_ARGV,
)
from fleet_graph.supervise.harvest_allowlist import (
    HarvestAllowlist,
    load_harvest_allowlist,
)
from fleet_graph.supervise.preauth import (
    PREAUTH_PAYLOAD_KIND,
    ReleaseEvaluation,
    evaluate_release,
    latest_preauth_for,
)

AUDIT_ROLE = "supervisor_auditor"
AUDIT_NODE = "audit"

CLASSIFY_NEEDS_HUMAN = "needs_human"
CLASSIFY_RECOMMEND_REJECT = "recommend_reject"
CLASSIFY_PREAUTH_RELEASE = "preauth_release"

#: Failed assertions in this set are *reproducible* failures: each carries the
#: exact command, its exit code, and the error text -- the three things §38d
#: requires a rejection recommendation to quote. Nothing else may mechanically
#: ground a `recommend_reject`.
REJECT_GROUNDS = frozenset({"frozen_acceptance_digest", "acceptance_no_skips", "acceptance_rerun"})

#: What the auditor may answer with. Anything else is a malformed verdict.
AUDIT_RECOMMENDATIONS = frozenset({"approve", "reject", "hold"})


def git_target_ref(report: dict[str, Any]) -> str:
    """The merge target ref, anchored in git -- or "" when it cannot be.

    Never read from any agent's account. The dd control plane constructs a
    development's integration ref as ``refs/heads/dd/<development_id>`` at
    admission (dd/control_plane.py, `remote_ref = f"refs/heads/dd/{...}"`),
    and the audit's `identity_binding` assertion has already recomputed the
    development id from the identity file inside git history (bootstrap-commit
    anchored, edits since bootstrap refused). Ref name = fixed rule over a
    git-anchored id; nothing self-attested survives into the result. A report
    that is not a development audit, or whose identity binding did not hold,
    yields "" -- and factor 2 of the preauth predicate then fails closed.
    """
    if report.get("kind") != "development":
        return ""
    ok_by_name = {a.get("name"): bool(a.get("ok")) for a in report.get("assertions") or []}
    if not ok_by_name.get("identity_binding"):
        return ""
    development_id = str(report.get("target") or "")
    return f"refs/heads/dd/{development_id}" if development_id else ""


def evaluate_preauth_release(
    event: SupervisorEvent,
    intake_facts: dict[str, Any],
    report: dict[str, Any],
    *,
    now: float,
) -> ReleaseEvaluation:
    """R4-3's fourth gate: the three-factor mechanical predicate, assembled
    from this turn's facts. Script, not llm -- the auditor's recommendation is
    deliberately not an input, in either direction.

    Only an E1 board question can ever be released: it is the one event type
    that *is* a gate. Everything else has no question to answer and no merge
    to unlock, so the evaluation fails closed with the reason recorded.
    """
    if event.type != EVENT_BOARD_QUESTION:
        return ReleaseEvaluation(
            granted=False,
            reasons=(f"事件类型 {event.type} 不是 gate question——无可放行之物",),
        )
    card_entity_id = str(
        event.payload.get("card_entity_id") or intake_facts.get("card_entity_id") or ""
    )
    preauth, rejections = latest_preauth_for(
        list(intake_facts.get("preauth_candidates") or []), card_entity_id
    )
    report_green = (
        bool(report.get("ok")) and bool(report.get("assertions")) and not report.get("gaps")
    )
    return evaluate_release(
        preauth=preauth,
        action="approve",
        card_entity_id=card_entity_id,
        question_note_id=str(event.payload.get("question_note_id") or ""),
        target_ref=git_target_ref(report),
        report_green=report_green,
        already_decided=bool(intake_facts.get("already_decided")),
        now=now,
        rejections=tuple(rejections),
    )


class SupervisorState(TypedDict, total=False):
    event: dict[str, Any]
    intake_facts: dict[str, Any]
    report: dict[str, Any]
    rerun_facts: dict[str, Any]
    audit_verdict: dict[str, Any]
    classification: str
    preauth_evaluation: dict[str, Any]
    act_result: dict[str, Any]
    receipt_path: str


@dataclass
class SupervisorDeps:
    """Everything one supervisor turn talks to. Injected, so testable."""

    launcher: AgentRunLauncher
    state_root: Path
    run_root: Path
    thread_id: str
    bus: BusClient | None = None
    audit_role: str = AUDIT_ROLE
    audit_timeout_seconds: int = 900
    audit_poll_interval: float = 2.0
    engine_url: str = DEFAULT_ENGINE_URL
    #: Explicit local clone for development audits (E1 over a dd gate). None
    #: falls back to the dd admission record's `repo_path` (see dd_root); a
    #: development that resolves neither records the gap and goes to
    #: needs_human -- facts, not guesses.
    repo: Path | None = None
    #: dd admission records root (server-side state; agents have no write face
    #: on it). The E1 resolution chain is card head `development_id` ->
    #: `<dd_root>/<id>/record.json` -> `repo_path`.
    dd_root: Path = DEFAULT_DD_ROOT
    publish_notes: bool = True
    #: Where the decision publisher talks to. Deliberately a plain URL, not a
    #: client: the decision credential is read inside the act node's call,
    #: never held on this dataclass next to the board client.
    bus_url: str = DEFAULT_BUS_URL
    #: Test seam for the decision publisher only. Production leaves it None so
    #: the publisher builds its own client from the separated credential.
    decision_client: Any | None = None

    def thread_dir(self, key: str) -> Path:
        return self.state_root / "threads" / key


# --- helpers ----------------------------------------------------------------


def _event_of(state: SupervisorState) -> SupervisorEvent:
    return validate_event(state.get("event") or {})


def _folder_id(event: SupervisorEvent, intake: dict[str, Any]) -> str:
    """The goal-line folder this event is about, if any."""
    candidate = str(event.payload.get("folder_id") or "")
    if candidate.startswith("wf-"):
        return candidate
    card = str(event.payload.get("card_entity_id") or intake.get("card_entity_id") or "")
    if card.startswith("wf-"):
        return card
    head_payload = intake.get("card_head_payload") or {}
    folder = str(head_payload.get("work_folder_id") or "")
    return folder if folder.startswith("wf-") else ""


def _development_id(event: SupervisorEvent, intake: dict[str, Any]) -> str:
    explicit = str(event.payload.get("development_id") or "")
    if explicit:
        return explicit
    head_payload = intake.get("card_head_payload") or {}
    return str(head_payload.get("development_id") or "")


def resolve_development_repo(development_id: str, dd_root: Path) -> tuple[Path | None, list[str]]:
    """dd admission record -> the development's repo, or the gaps that say why not.

    The record (`<dd_root>/<development_id>/record.json`) is server-side state
    the dd control plane wrote at admission -- agents have no write face on it,
    so reading `repo_path` out of it is mechanical resolution, never prose
    extraction from a note. Every failure step is a recorded fact, not an
    exception: the caller degrades to needs_human on a None repo.
    """
    record_path = dd_root / development_id / RECORD_FILE
    try:
        raw = record_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"dd record 不可读（{record_path}）: {type(exc).__name__}: {exc}"[:300]]
    try:
        record = json.loads(raw)
    except ValueError as exc:
        return None, [f"dd record 非法 JSON（{record_path}）: {exc}"[:300]]
    if not isinstance(record, dict):
        return None, [f"dd record 顶层不是 JSON 对象（{record_path}）"]
    repo_path = str(record.get("repo_path") or "")
    if not repo_path:
        return None, [f"dd record 缺 repo_path 字段（{record_path}）"]
    repo = Path(repo_path)
    if not repo.is_dir():
        return None, [f"dd record repo_path 不是可用目录: {repo_path}"[:300]]
    return repo, []


def reproducible_failures(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The mechanical grounds a rejection recommendation may cite.

    Every entry carries the exact argv (or command line), the exit code, and
    the error text verbatim -- §38d's mandatory format. Prose objections from
    the llm never appear here; they cannot ground a reject on their own.
    """
    failures: list[dict[str, Any]] = []
    for result in report.get("acceptance_results") or []:
        if int(result.get("exit_code") or 0) != 0:
            excerpt = (result.get("stderr_tail") or result.get("stdout_tail") or "").strip()
            failures.append(
                {
                    "argv": result.get("command"),
                    "exit_code": result.get("exit_code"),
                    "error_excerpt": excerpt[-1200:],
                }
            )
    if failures:
        return failures
    for assertion in report.get("assertions") or []:
        if assertion.get("name") in REJECT_GROUNDS and not assertion.get("ok"):
            failures.append(
                {
                    "argv": assertion.get("command"),
                    "exit_code": assertion.get("exit_code"),
                    "error_excerpt": str(assertion.get("detail") or "")[:1200],
                }
            )
    return failures


def validate_audit_verdict(parsed: dict[str, Any]) -> dict[str, Any]:
    """Mechanically re-check the auditor's declared shape. Belt and braces:
    agent-runtime enforces the schema server-side, but a verdict this graph
    acts on gets verified where it is consumed too (§37b: runtime facts over
    configuration inference)."""
    verdict = parsed.get("verdict")
    if not isinstance(verdict, dict):
        raise CoordinatorFault(f"audit result carried no verdict object; keys={sorted(parsed)}")
    recommendation = str(verdict.get("recommendation") or "")
    if recommendation not in AUDIT_RECOMMENDATIONS:
        raise CoordinatorFault(
            f"audit recommendation {recommendation!r} not in {sorted(AUDIT_RECOMMENDATIONS)}"
        )
    evidence = verdict.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise CoordinatorFault(
            "audit verdict carried no evidence entries (R4: no claim without reproduction)"
        )
    for i, entry in enumerate(evidence):
        if (
            not isinstance(entry, dict)
            or not entry.get("command")
            or not entry.get("output_excerpt")
        ):
            raise CoordinatorFault(
                f"evidence[{i}] lacks command/output_excerpt (R4: no claim without reproduction)"
            )
    return parsed


def render_supervisor_note(
    event: SupervisorEvent,
    report: dict[str, Any],
    audit_verdict: dict[str, Any],
    classification: str,
    failures: list[dict[str, Any]],
    preauth: dict[str, Any] | None = None,
) -> str:
    """The board-facing audit note. Recommends, or names the preauth it acted
    on -- the one thing it never does is present a release as a human verdict."""
    if classification == CLASSIFY_PREAUTH_RELEASE:
        header = (
            f"supervisor {event.type} {event.key}: {classification}"
            f"（依预授权 {(preauth or {}).get('preauth_message_id')} 代行放行，"
            f"scope=merge_only——合入≠部署，production promotion 仍停人闸）"
        )
    else:
        header = (
            f"supervisor {event.type} {event.key}: {classification}"
            f"（自动审计，本单不发 work.decision.v1——人仍拍板）"
        )
    lines = [header]
    if preauth is not None and classification == CLASSIFY_PREAUTH_RELEASE:
        lines.append(
            f"[PREAUTH] 目标 ref {preauth.get('target_ref')}（git 现算锚定）∈ 前缀白名单；"
            f"decision refs 指向 question note 与 preauth 两者"
        )
    for assertion in report.get("assertions") or []:
        mark = "PASS" if assertion.get("ok") else "FAIL"
        lines.append(
            f"[{mark}] {assertion.get('name')}: {str(assertion.get('detail'))[:160]} "
            f"({str(assertion.get('command'))[:120]} -> {assertion.get('exit_code')})"
        )
    for gap in report.get("gaps") or []:
        lines.append(f"[GAP] {gap}")
    if classification == CLASSIFY_RECOMMEND_REJECT:
        # §38d's mandatory format: the exact error, the minimal repro argv,
        # the exit code. A reject recommendation without all three is not
        # allowed to leave this function.
        if not failures:
            raise RuntimeError(
                "recommend_reject without a reproducible failure -- classify() "
                "must never let this happen"
            )
        lines.append("驳回建议（机械复现依据）：")
        for failure in failures:
            argv = failure.get("argv")
            argv_text = " ".join(argv) if isinstance(argv, list) else str(argv)
            lines.append(
                f"  repro: {argv_text[:200]} -> exit {failure.get('exit_code')}\n"
                f"  报错原文: {str(failure.get('error_excerpt'))[:400]}"
            )
    verdict = (audit_verdict or {}).get("verdict") or {}
    if verdict:
        lines.append(
            f"auditor 建议: {verdict.get('recommendation')}——{str(verdict.get('summary'))[:300]}"
        )
        for entry in verdict.get("evidence") or []:
            lines.append(
                f"  [evidence] {str(entry.get('claim'))[:120]} "
                f"({str(entry.get('command'))[:120]} -> {str(entry.get('output_excerpt'))[:160]})"
            )
    if audit_verdict.get("fault"):
        lines.append(f"[GAP] llm 审计节点未产出可用 verdict: {audit_verdict['fault']}")
    return "\n".join(lines)[:3800]


# --- the graph --------------------------------------------------------------


def build_supervisor_graph(deps: SupervisorDeps) -> StateGraph:
    def intake(state: SupervisorState) -> SupervisorState:
        """Fetch the authoritative objects for this event. Mechanical fields only."""
        event = _event_of(state)
        facts: dict[str, Any] = {"gaps": []}

        if event.type == EVENT_BOARD_QUESTION:
            facts["card_entity_id"] = str(event.payload.get("card_entity_id") or "")
            if deps.bus is None:
                facts["gaps"].append("无 bus 凭证：question note 与 card head 未取到")
            else:
                try:
                    note = deps.bus.message(WORK_NOTES, str(event.payload["question_note_id"]))
                    if note is None:
                        facts["gaps"].append(
                            f"question note {event.payload['question_note_id']} 不在 "
                            f"{WORK_NOTES} 频道——事件失实或频道被截断"
                        )
                    else:
                        facts["question_payload"] = note.get("payload") or {}
                    board = Board(deps.bus)
                    ticket = GateTicket(
                        question_note_id=str(event.payload["question_note_id"]),
                        card_entity_id=facts["card_entity_id"],
                    )
                    decision = board.decision_for(ticket)
                    facts["already_decided"] = decision is not None
                    head = (
                        board.card_head(facts["card_entity_id"])
                        if facts["card_entity_id"]
                        else None
                    )
                    if head is not None:
                        facts["card_head_payload"] = head.get("payload") or {}
                    # R4-3: the raw preauth candidates for this card. Fetched
                    # here (intake is the fetch node), validated in classify
                    # (the predicate node) -- an invalid candidate is a
                    # recorded rejection there, never an error here.
                    notes, _ = deps.bus.messages(WORK_NOTES, limit=1000)
                    # Only v2: the registered v1 payload_schema
                    # (additionalProperties:false, 5 fields) structurally
                    # cannot carry a preauth payload -- the bus rejects it
                    # with VALIDATION_ERROR -- so no v1 preauth can exist on
                    # the board.
                    facts["preauth_candidates"] = [
                        m
                        for m in notes
                        if m.get("kind") == DECISION_KIND_V2
                        and (m.get("payload") or {}).get("kind") == PREAUTH_PAYLOAD_KIND
                    ]
                except Exception as exc:  # facts, not guesses
                    facts["gaps"].append(f"board 取证失败: {type(exc).__name__}: {exc}"[:300])

        elif event.type in (EVENT_BLOCKED_DECISION, EVENT_LINE_FAULT):
            folder = str(event.payload.get("folder_id") or "")
            path = deps.run_root / folder / "terminal.json"
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                facts["terminal"] = {
                    "terminal": record.get("terminal"),
                    "rounds": record.get("rounds"),
                    "run_id": record.get("run_id"),
                    "waiting_on": record.get("waiting_on"),
                    "pump_fault": record.get("pump_fault"),
                    "at": record.get("at"),
                }
            except (OSError, ValueError) as exc:
                facts["gaps"].append(f"terminal.json 读取失败: {type(exc).__name__}: {exc}"[:300])

        elif event.type == EVENT_CAP_BREAKER:
            facts["refusal_detail"] = str(event.payload.get("detail") or "")
            facts["folder_ids"] = list(event.payload.get("folder_ids") or [])

        return {"intake_facts": facts}

    def gather_evidence(state: SupervisorState) -> SupervisorState:
        """§37d's checklist, by calling the same code `supervise audit` runs."""
        event = _event_of(state)
        intake_facts = state.get("intake_facts") or {}

        if event.type == EVENT_CAP_BREAKER:
            report = AuditReport(target=event.key, kind="cap_breaker")
            report.record(
                Assertion(
                    name="cap_breaker_observed",
                    ok=True,
                    command="scheduler tick observe: TickResult.refusal",
                    exit_code=0,
                    detail=(
                        f"TOTAL_CAP_REACHED: {intake_facts.get('refusal_detail')!r}; "
                        f"lines={intake_facts.get('folder_ids')}"
                    ),
                )
            )
            return {"report": report.as_dict()}

        folder_id = _folder_id(event, intake_facts)
        if folder_id:
            report = audit_goal_line(folder_id, run_root=deps.run_root)
            return {"report": report.as_dict()}

        development_id = _development_id(event, intake_facts)
        repo = deps.repo
        resolution_gaps: list[str] = []
        if development_id and repo is None:
            # The mechanical chain: card head development_id (structured, from
            # the dd engine's own card revisions) -> dd admission record ->
            # repo_path. Nothing is ever parsed out of note prose.
            repo, resolution_gaps = resolve_development_repo(development_id, deps.dd_root)
        if development_id and repo is not None:
            # Engine selection is a fact on disk, not a flag (same rule as
            # cli._supervise_audit): a development the new control plane
            # admitted has a record under dd_root and its evidence assembles
            # in-process; anything else is legacy, old controller, GETs only.
            if (deps.dd_root / development_id / RECORD_FILE).is_file():
                from fleet_graph.dd.control_plane import DdControlPlane

                engine: Any = GraphEngineSource(DdControlPlane(root=deps.dd_root))
            else:
                engine = OldEngineClient(deps.engine_url)
            try:
                report = audit_development(
                    development_id,
                    engine=engine,
                    repo=repo,
                )
            except Exception as exc:
                report = AuditReport(target=development_id, kind="development")
                report.gaps.append(f"development 审计执行失败: {type(exc).__name__}: {exc}"[:300])
            return {"report": report.as_dict()}

        report = AuditReport(target=event.key, kind="unresolved")
        report.gaps.extend(resolution_gaps)
        report.gaps.append(
            "事件既未解析到 wf- 目标也无可审 development"
            + (
                f"（development {development_id} 已解析但无可用 repo："
                "--repo 未配置，dd record 也未给出 repo_path——缘由见前一条 gap）"
                if development_id
                else ""
            )
            + "——证据不全，不猜，径直升报"
        )
        return {"report": report.as_dict()}

    def rerun_acceptance(state: SupervisorState) -> SupervisorState:
        """State the rerun facts explicitly, including 'not applicable'.

        For a development target the rerun already happened inside
        `audit_development` -- frozen argv, one-shot detached worktree, no
        existence guards (§39b/c) -- so this node lifts those results out.
        A goal line has no frozen argv to replay (its acceptance runs inside
        the line, R0d), and saying so beats silence.
        """
        report = state.get("report") or {}
        kind = report.get("kind")
        if kind == "development":
            facts = {
                "kind": kind,
                "results": list(report.get("acceptance_results") or []),
                "note": "冻结 argv 已在一次性 detached worktree 复跑（gather 节点内）",
            }
        elif kind == "goal_line":
            facts = {
                "kind": kind,
                "results": [],
                "note": (
                    "goal 线无冻结 acceptance argv 可复跑：R0d 的 acceptance 是线内步骤，"
                    "其 exit code 事实已进 coordinator input；此处如实记录不适用"
                ),
            }
        else:
            facts = {"kind": kind, "results": [], "note": "复跑不适用于该事件类"}
        facts["evidence_incomplete"] = not report.get("assertions")
        return {"rerun_facts": facts}

    def audit(state: SupervisorState) -> SupervisorState:
        """The single llm node. It advises; it decides nothing.

        One-shot `agent-run` with a derived run id: a killed supervisor that
        restarts re-computes the same id and re-adopts the in-flight run
        instead of paying for a second one (pinned in tests)."""
        event = _event_of(state)
        input_path = write_json_durable(
            deps.thread_dir(event.key) / "audit-input.json",
            {
                "event": event.as_dict(),
                "intake": state.get("intake_facts") or {},
                "mechanical_report": state.get("report") or {},
                "rerun": state.get("rerun_facts") or {},
            },
        )
        spec = AgentRunSpec(
            prompt="",
            role=deps.audit_role,
            structured=True,
            # write=False is the default and load-bearing: the auditor's tool
            # face is read-only by role *and* by dispatch.
            timeout_seconds=deps.audit_timeout_seconds,
            input_path=str(input_path),
            prompt_file=str(input_path),
            labels={"dispatcher": "fleet-graph-supervisor", "event": event.type},
        )
        run_id = derive_run_id(deps.thread_id, AUDIT_NODE)
        ticket = deps.launcher.launch(spec, run_id)
        try:
            status = deps.launcher.wait(
                ticket,
                poll_interval=deps.audit_poll_interval,
                deadline_seconds=deps.audit_timeout_seconds + 120,
            )
        except RunWaitTimeout as timeout:
            return {
                "audit_verdict": {
                    "fault": f"audit run 超时未终局（{timeout.waited_seconds:.0f}s）；"
                    "ticket 已 checkpoint，重启将 re-adopt",
                    "run_id": run_id,
                }
            }
        if not status.ok or status.result is None:
            return {
                "audit_verdict": {
                    "fault": f"audit run ended {status.state} "
                    f"(exit_code={(status.result or {}).get('exit_code')})",
                    "run_id": run_id,
                }
            }
        try:
            parsed = validate_audit_verdict(parse_envelope(status.result))
        except CoordinatorFault as fault:
            return {"audit_verdict": {"fault": str(fault)[:400], "run_id": run_id}}
        return {"audit_verdict": {**parsed, "run_id": run_id}}

    def classify(state: SupervisorState) -> SupervisorState:
        """Mechanical gate. The llm's recommendation is advice, never the input.

        `recommend_reject` requires a reproducible failure (exact argv + exit
        code + error text) and takes precedence -- a red evidence chain is
        never released, whatever the board says. `preauth_release` requires
        the full three-factor predicate over a human-issued preauth; any
        missing factor -- including an llm that says "approve" -- degrades to
        `needs_human`, silently and on purpose.
        """
        failures = reproducible_failures(state.get("report") or {})
        if failures:
            return {"classification": CLASSIFY_RECOMMEND_REJECT}
        evaluation = evaluate_preauth_release(
            _event_of(state),
            state.get("intake_facts") or {},
            state.get("report") or {},
            now=time.time(),
        )
        if evaluation.granted:
            return {
                "classification": CLASSIFY_PREAUTH_RELEASE,
                "preauth_evaluation": evaluation.as_dict(),
            }
        return {
            "classification": CLASSIFY_NEEDS_HUMAN,
            "preauth_evaluation": evaluation.as_dict(),
        }

    def act(state: SupervisorState) -> SupervisorState:
        """Publish the audit as an evidence note; degrade to local on refusal.

        On `preauth_release`, also publish the one decision this graph may
        ever cast -- through `decision_publisher`, the repo's single sanctioned
        door, with the separated credential read inside that call. A refused
        decision publish is recorded and nothing else changes: the question
        stays open on the board, so the gate still waits for a human -- the
        failure mode of this branch is needs_human, never a silent release."""
        event = _event_of(state)
        intake_facts = state.get("intake_facts") or {}
        classification = state.get("classification") or CLASSIFY_NEEDS_HUMAN
        failures = reproducible_failures(state.get("report") or {})
        preauth_evaluation = state.get("preauth_evaluation") or {}
        result: dict[str, Any] = {"classification": classification}

        card_entity_id = str(
            event.payload.get("card_entity_id")
            or intake_facts.get("card_entity_id")
            or _folder_id(event, intake_facts)
            or ""
        )
        question_note_id = str(event.payload.get("question_note_id") or "")

        if classification == CLASSIFY_PREAUTH_RELEASE:
            report = state.get("report") or {}
            evaluation = ReleaseEvaluation(
                granted=bool(preauth_evaluation.get("granted")),
                reasons=tuple(preauth_evaluation.get("reasons") or ()),
                preauth_message_id=str(preauth_evaluation.get("preauth_message_id") or ""),
                target_ref=str(preauth_evaluation.get("target_ref") or ""),
                rejections=tuple(preauth_evaluation.get("rejections") or ()),
            )
            try:
                decision = publish_release_decision(
                    evaluation=evaluation,
                    card_entity_id=card_entity_id,
                    question_note_id=question_note_id,
                    rationale=(
                        f"机械审计全绿（{len(report.get('assertions') or [])} 条断言，"
                        f"target={report.get('target')}）；依预授权 "
                        f"{evaluation.preauth_message_id} 放行 merge_only"
                    ),
                    # Same-turn retries and kill-restarts dedup on the question:
                    # one question, at most one released decision.
                    idempotency_key=f"supervisor-preauth:{event.key}",
                    bus_url=deps.bus_url,
                    client=deps.decision_client,
                )
                result["decision_published"] = True
                result["decision_message_id"] = decision.message_id
            except Exception as exc:
                result["decision_published"] = False
                result["decision_degraded"] = f"decision 发布被拒: {type(exc).__name__}: {exc}"[
                    :400
                ]

        note = render_supervisor_note(
            event,
            state.get("report") or {},
            state.get("audit_verdict") or {},
            classification,
            failures,
            preauth=preauth_evaluation or None,
        )
        result["note"] = note

        if not deps.publish_notes:
            result["published"] = False
            result["degraded"] = "note publishing disabled (--no-note)"
        elif deps.bus is None:
            result["published"] = False
            result["degraded"] = "无 bus 凭证——审计报告仅落 supervisor run root"
        elif not card_entity_id:
            result["published"] = False
            result["degraded"] = (
                "board 无卡可挂（goal 线无板卡的既知 422 契约缺口）——报告降级本地落盘"
            )
        else:
            refs = [{"target_entity": card_entity_id}]
            if question_note_id:
                refs.append({"target_entity": question_note_id})
            try:
                published = deps.bus.publish(
                    WORK_NOTES,
                    NOTE_KIND,
                    {
                        "card_entity_id": card_entity_id,
                        "note": note,
                        "note_type": "evidence",
                    },
                    f"supervisor:{event.key}:{classification}",
                    refs=refs,
                )
                result["published"] = True
                result["evidence_note_id"] = published.message_id
            except Exception as exc:
                # The known contract gap (a goal line has no board card ->
                # 422 DERIVATION_ERROR) and any bus outage land here: degrade
                # to the local receipt, with the refusal recorded verbatim.
                result["published"] = False
                result["degraded"] = f"board note 被拒: {type(exc).__name__}: {exc}"[:400]
        return {"act_result": result}

    def receipt(state: SupervisorState) -> SupervisorState:
        """The audit lands in the supervisor's own run root -- never in the
        supervised line's work folder (§38e)."""
        event = _event_of(state)
        path = write_json_durable(
            deps.state_root / "reports" / f"{event.key}.json",
            {
                "event": event.as_dict(),
                "thread_id": deps.thread_id,
                "intake": state.get("intake_facts") or {},
                "report": state.get("report") or {},
                "rerun": state.get("rerun_facts") or {},
                "audit_verdict": state.get("audit_verdict") or {},
                "classification": state.get("classification") or CLASSIFY_NEEDS_HUMAN,
                "preauth_evaluation": state.get("preauth_evaluation") or {},
                "act_result": state.get("act_result") or {},
            },
        )
        return {"receipt_path": str(path)}

    def after_rerun(state: SupervisorState) -> str:
        # Evidence too thin to audit -> report the facts, do not guess (§2's
        # bypass edge). classification defaults to needs_human in act().
        facts = state.get("rerun_facts") or {}
        return "act" if facts.get("evidence_incomplete") else AUDIT_NODE

    graph: StateGraph = StateGraph(SupervisorState)
    graph.add_node("intake", intake)
    graph.add_node("gather_evidence", gather_evidence)
    graph.add_node("rerun_acceptance", rerun_acceptance)
    graph.add_node(AUDIT_NODE, audit)
    graph.add_node("classify", classify)
    graph.add_node("act", act)
    graph.add_node("receipt", receipt)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "gather_evidence")
    graph.add_edge("gather_evidence", "rerun_acceptance")
    graph.add_conditional_edges("rerun_acceptance", after_rerun)
    graph.add_edge(AUDIT_NODE, "classify")
    graph.add_edge("classify", "act")
    graph.add_edge("act", "receipt")
    graph.add_edge("receipt", END)
    return graph


# --- assembly ---------------------------------------------------------------


@dataclass
class SupervisorRunConfig:
    event: dict[str, Any]
    state_root: Path = Path("/data/fleet-graph/supervisor")
    run_root: Path = Path("/data/fleet-graph/runs")
    #: None -> durable: state_root / "checkpoint.sqlite3". All supervisor
    #: threads share one checkpoint db; the thread id carries the event key.
    checkpoint_path: str | None = None
    agent_run_bin: str | None = None
    audit_timeout_seconds: int = 900
    audit_poll_interval: float = 2.0
    engine_url: str = DEFAULT_ENGINE_URL
    repo: Path | None = None
    dd_root: Path = DEFAULT_DD_ROOT
    publish_notes: bool = True
    bus: BusClient | None = None
    bus_url: str = DEFAULT_BUS_URL
    decision_client: Any | None = None
    # --- M3 harvest (E5) ---
    #: The write allowlist the harvest subgraph checks before any write.
    #: Deny-all by default: allowlist 未合入前 harvest 无任何写权限（铁律）。
    harvest_allowlist: HarvestAllowlist = field(default_factory=HarvestAllowlist.default)
    #: Explicit allowlist config file; overrides harvest_allowlist when set.
    harvest_allowlist_path: str | None = None
    #: Target default branch for the harvest squash merge + ff-only pull.
    harvest_default_branch: str = DEFAULT_HARVEST_BRANCH
    #: Deploy command the harvest subgraph may run (must be allowlisted).
    harvest_deploy_command: list[str] = field(default_factory=list)
    harvest_verify_argv: list[str] = field(
        default_factory=lambda: list(DEFAULT_HARVEST_VERIFY_ARGV)
    )
    harvest_verify_real_argv: list[str] = field(
        default_factory=lambda: list(DEFAULT_HARVEST_VERIFY_ARGV)
    )
    #: Test seam: injected HarvestOps; None -> DefaultHarvestOps.
    harvest_ops: Any | None = None

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.state_root / "checkpoint.sqlite3")


def build_supervisor(config: SupervisorRunConfig) -> tuple[Any, SupervisorDeps, SupervisorEvent]:
    event = validate_event(config.event)
    launcher_kwargs: dict[str, Any] = {"state_root": str(config.state_root / "agent-runs")}
    if config.agent_run_bin:
        launcher_kwargs["bin_path"] = config.agent_run_bin
    deps = SupervisorDeps(
        launcher=AgentRunLauncher(**launcher_kwargs),
        state_root=config.state_root,
        run_root=config.run_root,
        thread_id=event.thread_id,
        bus=config.bus,
        audit_timeout_seconds=config.audit_timeout_seconds,
        audit_poll_interval=config.audit_poll_interval,
        engine_url=config.engine_url,
        repo=config.repo,
        dd_root=config.dd_root,
        publish_notes=config.publish_notes,
        bus_url=config.bus_url,
        decision_client=config.decision_client,
    )
    return build_supervisor_graph(deps), deps, event


def run_supervisor(config: SupervisorRunConfig) -> dict[str, Any]:
    """Run one supervisor turn to its receipt, resuming if the thread exists.

    An E5 `approved_unharvested` event is dispatched to the M3 harvest ReAct
    subgraph (`supervise/harvest.py`) instead of the audit turn: it is the one
    event type whose job is to *write* (squash merge + deploy), and that write
    is gated by the harvest allowlist (deny-all default). Every other event
    runs the audit graph below.

    Resume semantics mirror graphs/runner.resume_start, with one addition: a
    thread whose checkpoint already carries a receipt is *finished* -- invoke
    nothing, return the recorded outcome. Re-running a finished event must be
    a no-op, or every observer retry would republish notes.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    event = validate_event(config.event)
    if event.type == EVENT_APPROVED_UNHARVESTED:
        from fleet_graph.supervise.harvest import HarvestRunConfig, run_harvest

        allowlist = config.harvest_allowlist
        if config.harvest_allowlist_path:
            allowlist = load_harvest_allowlist(config.harvest_allowlist_path)
        harvest_config = HarvestRunConfig(
            event=event.as_dict(),
            state_root=config.state_root,
            run_root=config.run_root,
            checkpoint_path=config.checkpoint_path,
            dd_root=config.dd_root,
            repo=config.repo,
            default_branch=config.harvest_default_branch,
            deploy_command=list(config.harvest_deploy_command),
            verify_argv=(
                list(config.harvest_verify_argv)
                if config.harvest_verify_argv
                else list(DEFAULT_HARVEST_VERIFY_ARGV)
            ),
            verify_real_argv=(
                list(config.harvest_verify_real_argv)
                if config.harvest_verify_real_argv
                else list(DEFAULT_HARVEST_VERIFY_ARGV)
            ),
            allowlist=allowlist,
            ops=config.harvest_ops,
            bus=config.bus,
            publish_notes=config.publish_notes,
        )
        return run_harvest(harvest_config)

    graph, _deps, event = build_supervisor(config)
    invoke_config: dict[str, Any] = {
        "configurable": {"thread_id": event.thread_id},
        "recursion_limit": 50,
    }

    checkpoint = config.resolved_checkpoint_path
    if checkpoint != ":memory:":
        # First event ever on a fresh host: sqlite will not create the parent.
        Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(checkpoint) as saver:
        compiled = graph.compile(checkpointer=saver)
        snapshot = compiled.get_state(invoke_config)
        if snapshot.next:
            start: dict[str, Any] | None = None  # resume in place; audit re-adopts
        elif snapshot.values and snapshot.values.get("receipt_path"):
            return {
                "event": event.as_dict(),
                "thread_id": event.thread_id,
                "classification": snapshot.values.get("classification"),
                "receipt_path": snapshot.values.get("receipt_path"),
                "resumed": "already_complete",
            }
        else:
            start = {"event": event.as_dict()}
        state = compiled.invoke(start, config=invoke_config)

    return {
        "event": event.as_dict(),
        "thread_id": event.thread_id,
        "classification": state.get("classification"),
        "act_result": state.get("act_result"),
        "receipt_path": state.get("receipt_path"),
    }


__all__ = [
    "AUDIT_ROLE",
    "CLASSIFY_NEEDS_HUMAN",
    "CLASSIFY_PREAUTH_RELEASE",
    "CLASSIFY_RECOMMEND_REJECT",
    "SupervisorDeps",
    "SupervisorRunConfig",
    "SupervisorState",
    "build_supervisor",
    "build_supervisor_graph",
    "evaluate_preauth_release",
    "git_target_ref",
    "render_supervisor_note",
    "reproducible_failures",
    "resolve_development_repo",
    "run_supervisor",
    "validate_audit_verdict",
]

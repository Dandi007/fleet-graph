"""The supervisor graph: one event-triggered audit turn, as seven nodes.

    intake -> gather_evidence -> rerun_acceptance -> audit(llm) -> classify
        -> act -> receipt -> END

Script nodes bracket the single llm node, and the llm only ever *advises*:
`classify` gates the classification on mechanical predicates, and in this
ticket (R4-2) the only classifications that exist are `needs_human` and
`recommend_reject`. The `preauth_release` branch is deliberately a
NotImplementedError stub until R4-3 lands the fourth gate -- see
`preauth_release`, and do not be the person who fills it in early (§38b:
a complete evidence chain is not a merge verdict, and a supervisor that can
release work on its own judgement is a self-approval button).

Three structural refusals, each pinned by scripts/check_supervisor_conformance.py
or by the module simply not importing the capability:

- **This module cannot publish a `work.decision.v1`.** The whole repo cannot
  (bus/board.py's standing rule); the conformance guard makes the regression
  loud.
- **This module cannot schedule.** It never imports `scheduler.ignition` or
  `scheduler.launcher`; it is a *scheduled* thing (launched as a transient
  unit by the observer), never a second scheduler (r4-design §5, D9).
- **It never writes into the line it is auditing.** Its outputs are a board
  note and files under its own state root. §38e's governance livelock -- the
  supervisor locked out of the work folder it was trying to write into -- is
  resolved by not wanting to write there at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES, Board, GateTicket
from fleet_graph.bus.client import BusClient
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
    OldEngineClient,
    audit_development,
    audit_goal_line,
)
from fleet_graph.supervise.events import (
    EVENT_BLOCKED_DECISION,
    EVENT_BOARD_QUESTION,
    EVENT_CAP_BREAKER,
    EVENT_LINE_FAULT,
    SupervisorEvent,
    validate_event,
)

AUDIT_ROLE = "supervisor_auditor"
AUDIT_NODE = "audit"

CLASSIFY_NEEDS_HUMAN = "needs_human"
CLASSIFY_RECOMMEND_REJECT = "recommend_reject"

#: Failed assertions in this set are *reproducible* failures: each carries the
#: exact command, its exit code, and the error text -- the three things §38d
#: requires a rejection recommendation to quote. Nothing else may mechanically
#: ground a `recommend_reject`.
REJECT_GROUNDS = frozenset({"frozen_acceptance_digest", "acceptance_no_skips", "acceptance_rerun"})

#: What the auditor may answer with. Anything else is a malformed verdict.
AUDIT_RECOMMENDATIONS = frozenset({"approve", "reject", "hold"})


def preauth_release(
    event: dict[str, Any], report: dict[str, Any], audit_verdict: dict[str, Any]
) -> bool:
    """R4-3's fourth gate. Deliberately not implemented in R4-2.

    The three-factor predicate (preauth coverage, server-side target base in
    the allowlist, honest attribution) and the credential-separated decision
    publisher do not exist yet, so there is nothing this function could
    legitimately consult. Raising -- rather than returning False quietly -- is
    the point: code that starts calling this before R4-3 should fail its
    tests, not silently classify everything as needs_human while believing it
    evaluated a preauth.
    """
    raise NotImplementedError(
        "preauth_release is R4-3's fourth gate (three-factor mechanical "
        "predicate + credential-separated decision publisher); R4-2 ships "
        "only needs_human / recommend_reject"
    )


class SupervisorState(TypedDict, total=False):
    event: dict[str, Any]
    intake_facts: dict[str, Any]
    report: dict[str, Any]
    rerun_facts: dict[str, Any]
    audit_verdict: dict[str, Any]
    classification: str
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
    #: Local clone for development audits (E1 over a dd gate). None means a
    #: development target records the gap and goes to needs_human -- facts,
    #: not guesses.
    repo: Path | None = None
    publish_notes: bool = True

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
) -> str:
    """The board-facing audit note. Casts no verdict; recommends at most."""
    header = (
        f"supervisor {event.type} {event.key}: {classification}"
        f"（自动审计，本单不发 work.decision.v1——人仍拍板）"
    )
    lines = [header]
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
        if development_id and deps.repo is not None:
            try:
                report = audit_development(
                    development_id,
                    engine=OldEngineClient(deps.engine_url),
                    repo=deps.repo,
                )
            except Exception as exc:
                report = AuditReport(target=development_id, kind="development")
                report.gaps.append(f"development 审计执行失败: {type(exc).__name__}: {exc}"[:300])
            return {"report": report.as_dict()}

        report = AuditReport(target=event.key, kind="unresolved")
        report.gaps.append(
            "事件既未解析到 wf- 目标也无可审 development"
            + ("（development 审计需 --repo，未配置）" if development_id else "")
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

        R4-2 policy: two classes only. `recommend_reject` requires a
        reproducible failure (exact argv + exit code + error text); everything
        else -- including an llm that says "reject" without a mechanical
        ground, and an llm that says "approve" -- is `needs_human`, because
        the release path (`preauth_release`) does not exist until R4-3.
        """
        failures = reproducible_failures(state.get("report") or {})
        if failures:
            return {"classification": CLASSIFY_RECOMMEND_REJECT}
        # NOT taken in R4-2, by construction: preauth_release() raises
        # NotImplementedError. The branch is written out so R4-3 has exactly
        # one place to land, and so nobody re-invents it elsewhere.
        return {"classification": CLASSIFY_NEEDS_HUMAN}

    def act(state: SupervisorState) -> SupervisorState:
        """Publish the audit as an evidence note; degrade to local on refusal.

        Structurally incapable of publishing a decision: neither this module
        nor anything it imports has such a method, and the conformance guard
        turns any future attempt into a red CI."""
        event = _event_of(state)
        intake_facts = state.get("intake_facts") or {}
        classification = state.get("classification") or CLASSIFY_NEEDS_HUMAN
        failures = reproducible_failures(state.get("report") or {})
        note = render_supervisor_note(
            event,
            state.get("report") or {},
            state.get("audit_verdict") or {},
            classification,
            failures,
        )
        result: dict[str, Any] = {"classification": classification, "note": note}

        card_entity_id = str(
            event.payload.get("card_entity_id")
            or intake_facts.get("card_entity_id")
            or _folder_id(event, intake_facts)
            or ""
        )
        question_note_id = str(event.payload.get("question_note_id") or "")

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
    publish_notes: bool = True
    bus: BusClient | None = None

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
        publish_notes=config.publish_notes,
    )
    return build_supervisor_graph(deps), deps, event


def run_supervisor(config: SupervisorRunConfig) -> dict[str, Any]:
    """Run one supervisor turn to its receipt, resuming if the thread exists.

    Resume semantics mirror graphs/runner.resume_start, with one addition: a
    thread whose checkpoint already carries a receipt is *finished* -- invoke
    nothing, return the recorded outcome. Re-running a finished event must be
    a no-op, or every observer retry would republish notes.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

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
    "CLASSIFY_RECOMMEND_REJECT",
    "SupervisorDeps",
    "SupervisorRunConfig",
    "SupervisorState",
    "build_supervisor",
    "build_supervisor_graph",
    "preauth_release",
    "render_supervisor_note",
    "reproducible_failures",
    "run_supervisor",
    "validate_audit_verdict",
]

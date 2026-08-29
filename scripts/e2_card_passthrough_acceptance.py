#!/usr/bin/env python3
"""E2 goal-line card pass-through acceptance: one card per line.

The scheduler daemon (parking escalation) and the E2 interrupt runtime both
materialise a goal line's board card. This script proves the formal fix on the
real durable surfaces: one shared idempotency key + one shared payload
constructor, so the two producers converge on one card instead of 409-ing (the
original failure) or duplicating (the ``e2-goal-line-card:`` hotfix).

Four scenarios:

- ``daemon-first-runtime-reuses`` -- Path A: the daemon materialises the card
  first (stall-state ``board_card_entity_id``), the runtime reuses it through
  the pass-through wiring. Exactly one ``work.card.v1``, checkpoint
  ``card_entity_id`` == stall-state, no ``BusConflict``, no ``e2-goal-line-card:``.
- ``concurrent-first-create`` -- Path B: both producers race the first create,
  one wins, the other receives ``deduplicated=True`` and adopts the winner's
  entity id. Exactly one card, both agree, no 409 surfaces.
- ``decision-content-in-production-resume`` -- 12:4x mechanical proof on the
  production-shaped path (bridge ``resumer_for`` -> ``resume_goal_line`` ->
  ``resume_line`` -> graph): the persisted ``coord/round-N-input.json`` on the
  resumed round carries the injected decision + ``resume_key``; the bridge
  resumer records the full decision before resume; and the N7 round-zero-repark
  guard rejects a coordinator that re-declares ``blocked + waiting_on=decision``
  without acknowledging the decision.
- ``real-bus-stall-resume`` -- the real agent-bus drill: publish a throwaway
  card + question on the real bus with the shared key/constructor, suspend a
  synthetic drill-tagged line through the E2 interrupt against a real SQLite
  store + real checkpointer, publish a real ``work.decision.v1``, run the real
  ``GoalInterruptBridge`` once, and assert the line resumes with exactly one
  card for the drill folder. JSON evidence on stdout; non-zero exit when a
  scenario fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.bus.board import (
    CARD_KIND,
    DECISION_KIND,
    WORK_INDEX,
    WORK_NOTES,
    Board,
    BusConflict,
    goal_line_card_key,
    goal_line_card_payload,
)
from fleet_graph.bus.client import BusClient
from fleet_graph.goal_interrupt.bridge import GoalInterruptBridge, GoalInterruptBridgeConfig
from fleet_graph.goal_interrupt.contract import resume_key_for
from fleet_graph.goal_interrupt.runtime import LineInterruptPort
from fleet_graph.goal_interrupt.store import GoalInterruptStore
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.wake import parse_bus_timestamp

FOLDER_ID = "wf-1"
ALIAS = "canary"
BLOCKED_AT = "2026-08-27T10:00:00Z"
BLOCKED_EPOCH = parse_bus_timestamp(BLOCKED_AT)
PRIME_EPOCH = BLOCKED_EPOCH - 1800.0
TICK_EPOCH = BLOCKED_EPOCH + 3600.0
SEAT = "opencode-dsv4pro"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def evidence(scenario: str, passed: bool, **facts: Any) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "utc_timestamp": utc_now(),
        "pass": passed,
        "exit_code": 0 if passed else 1,
        **facts,
    }


# --- fakes for the scheduler path -------------------------------------------


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeWake:
    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return False

    def goal_revision(self, folder_id: str) -> str:
        return "sha256:rev-1"


class FakeTicket:
    question_note_id = "note-123"


class IdempotentFakeBoard:
    """Faithful bus idempotency for ``work.card.v1`` publishes.

    fresh key => new entity; same key + identical payload => deduplicate and
    return the existing entity; same key + different payload => ``BusConflict``.
    """

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.card_publishes: list[str] = []
        self.deduplicated_publishes: list[str] = []
        self.questions: dict[str, Any] = {}

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        self.card_publishes.append(idempotency_key)
        if idempotency_key in self.cards:
            existing = self.cards[idempotency_key]
            if existing["payload"] != payload:
                raise BusConflict(409, "IDEMPOTENCY_CONFLICT: same key, different payload")
            self.deduplicated_publishes.append(idempotency_key)
            return SimpleNamespace(entity_id=existing["entity_id"], deduplicated=True)
        entity_id = f"card-{idempotency_key}"
        self.cards[idempotency_key] = {"payload": payload, "entity_id": entity_id}
        return SimpleNamespace(entity_id=entity_id, deduplicated=False)

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> FakeTicket:
        if idempotency_key not in self.questions:
            self.questions[idempotency_key] = SimpleNamespace(
                question_note_id=f"note-{idempotency_key}", card_entity_id=card_entity_id
            )
        return self.questions[idempotency_key]


# --- graph-line fakes -------------------------------------------------------


class BlockingCoordinator:
    """Round 1 blocks on a human decision; the graph then suspends."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        self.calls.append((round_no, dict(coord_input)))
        return {"verdict": "blocked", "waiting_on": "decision", "reason": "need human"}


class NullInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class RecordingArtifacts:
    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        return "terminal.json"

    def write_fault_terminal(self, **kwargs: Any) -> str:
        return "fault"


class FakeBus:
    """A board serving ``messages`` + ``refs_to`` as the real bus does."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages_list: list[dict[str, Any]] = messages or []
        self.refs: dict[str, list[str]] = {}

    def link(self, question_id: str, message_id: str) -> None:
        self.refs.setdefault(question_id, []).append(message_id)

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [m for m in self.messages_list if int(m["channel_seq"]) > after_seq][:limit]
        head = max((int(m["channel_seq"]) for m in self.messages_list), default=0)
        return selected, head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {"message_id": mid, "target_entity": entity_id} for mid in self.refs.get(entity_id, [])
        ]


def decision_message(message_id: str, seq: int, question_note_id: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": DECISION_KIND,
        "created_at": "2026-08-29T00:00:00Z",
        "payload": {
            "decision": "APPROVE",
            "rationale": "accepted in drill",
            "decided_by": "e2-card-drill",
            "card_entity_id": "card-1",
            "question": f"line {FOLDER_ID} waiting on a human decision (round 1).",
        },
    }


# --- scheduler helpers ------------------------------------------------------


def make_scheduler(run_root: Path, board: Any) -> Scheduler:
    return Scheduler(
        SchedulerConfig(
            lines=[LineSpec(folder_id=FOLDER_ID, seat=SEAT, alias=ALIAS, enabled=True)],
            run_root=run_root,
            maintenance_stop_path=run_root / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=FakeLauncher(),
        units=FakeUnits(),
        clock=Clock(PRIME_EPOCH),
        sleep=lambda _s: None,
        wake=FakeWake(),
        board=board,
    )


def write_blocked(run_root: Path) -> None:
    record = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": "run-b1",
        "at": BLOCKED_AT,
        "reason": "等监督面拍板",
        "waiting_on": "decision",
    }
    path = run_root / FOLDER_ID / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def stall_file(run_root: Path) -> Path:
    return run_root / ".scheduler" / f"{FOLDER_ID}.json"


def established_card(run_root: Path, board: Any) -> str:
    scheduler = make_scheduler(run_root, board)
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    write_blocked(run_root)
    scheduler.clock = Clock(TICK_EPOCH)
    scheduler.tick()  # establish the park -> card + question
    state = json.loads(stall_file(run_root).read_text(encoding="utf-8"))
    return str(state["board_card_entity_id"])


# --- the production-shaped resume resumer -----------------------------------


def write_fake_coordinator_bin(path: Path, *, mode: str) -> Path:
    """A fake agent-run binary that acts as the coordinator.

    Reads the round input (``--input``) and writes a ``result.json`` the
    ``AgentRunCoordinator`` can parse. ``mode="accept"`` acknowledges a present
    decision and returns ``done``; ``mode="repark"`` re-declares the blocked
    decision wait without acknowledging (the N7 round-zero-repark case) unless
    no decision is present, in which case it finishes.
    """
    src = f"""#!{sys.executable}
import json
import sys
from pathlib import Path

argv = sys.argv[1:]
opts: dict[str, str] = {{}}
i = 0
while i < len(argv):
    token = argv[i]
    if token == "--":
        break
    if token.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        opts[token] = argv[i + 1]
        i += 2
    else:
        i += 1

session_root = Path(opts["--session-root"])
run_id = opts["--run-id"]
data: dict = {{}}
if opts.get("--input"):
    try:
        data = json.loads(Path(opts["--input"]).read_text())
    except Exception:
        data = {{}}

has_decision = isinstance(data.get("decision"), dict)
mode = {mode!r}
if mode == "accept":
    verdict = {{"verdict": "done", "reason": "decision accepted"}}
    if has_decision:
        verdict["acknowledged_message_id"] = data["decision"]["message_id"]
elif mode == "repark":
    if has_decision:
        verdict = {{"verdict": "blocked", "waiting_on": "decision", "reason": "same blocker"}}
    else:
        verdict = {{"verdict": "done", "reason": "finished"}}
else:
    verdict = {{"verdict": "done", "reason": "finished"}}

run_dir = session_root / f"run-{{run_id[:8]}}"
run_dir.mkdir(parents=True, exist_ok=True)
result = {{
    "state": "succeeded",
    "exit_code": 0,
    "exit_reason": "normal",
    "run_dir": str(run_dir),
    "run_id": run_id,
    "structured_result": verdict,
}}
tmp = run_dir / "result.json.tmp"
tmp.write_text(json.dumps(result, ensure_ascii=False))
tmp.replace(run_dir / "result.json")
"""
    path.write_text(src, encoding="utf-8")
    path.chmod(0o755)
    return path


def production_resumer(run_root: Path, folder_id: str, *, agent_run_bin: str | None = None) -> Any:
    """The CLI's ``resumer_for`` shape: ``resume_goal_line`` -> ``resume_line``."""
    from fleet_graph.graphs.runner import LineConfig, resume_goal_line

    def resumer(decision: Any) -> str:
        resume_store = GoalInterruptStore(run_root).open()
        try:
            record = resume_store.interrupt(decision.resume_key)
        finally:
            resume_store.close()
        generation = int(record["generation"]) if record else 1
        config = LineConfig(
            folder_id=folder_id,
            seat=SEAT,
            run_root=run_root,
            generation=generation,
            alias=None,
            agent_run_bin=agent_run_bin,
        )
        _state, status = resume_goal_line(config, decision)
        return status

    return resumer


def suspension_line(
    run_root: Path, port: LineInterruptPort, folder_id: str
) -> tuple[Any, GoalInterruptStore]:
    """A line suspended at the E2 interrupt, on a real store + checkpointer."""

    def persist_coord_input(round_no: int, payload: dict[str, Any]) -> None:
        coord_dir = run_root / "coord"
        coord_dir.mkdir(parents=True, exist_ok=True)
        (coord_dir / f"round-{round_no}-input.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    store = GoalInterruptStore(run_root).open()
    deps = LineDeps(
        coordinator=BlockingCoordinator(),
        worker=NullInbox(),
        inbox=NullInbox(),
        artifacts=RecordingArtifacts(),
        guards=LineGuards(bounds=LineBounds(max_rounds=50)),
        folder_id=folder_id,
        persist_coord_input=persist_coord_input,
        interrupt=port,
    )
    return build_goal_line_graph(deps), store


def suspend(run_root: Path, graph: Any, folder_id: str) -> dict[str, Any]:
    cfg = {"configurable": {"thread_id": f"{folder_id}:g1"}, "recursion_limit": 200}
    with SqliteSaver.from_conn_string(str(run_root / "checkpoint.sqlite3")) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke({"round_no": 1}, config=cfg)
    return state


# --- scenarios ---------------------------------------------------------------


def scenario_daemon_first_runtime_reuses(work_dir: Path) -> dict[str, Any]:
    board = IdempotentFakeBoard()
    root = work_dir / "pa"
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / "runs"

    stall_card = established_card(run_root, board)
    assert stall_card

    # The line is then launched with the scheduler's card threaded through
    # (``spec_for`` -> ``--board-card`` -> ``LineConfig.board_card_entity_id``
    # -> ``_build_interrupt`` -> ``LineInterruptPort(card_entity_id=...)``).
    line_root = root / "line"
    port = LineInterruptPort(
        folder_id=FOLDER_ID,
        generation=1,
        store=GoalInterruptStore(line_root).open(),
        board=board,
        card_entity_id=stall_card,
        run_id="",
    )
    graph, store = suspension_line(line_root, port, FOLDER_ID)
    suspend(line_root, graph, FOLDER_ID)

    resume_key = resume_key_for(FOLDER_ID, 1, "note-e2-question:wf-1:1:1")
    checkpoint = store.interrupt(resume_key)

    passed = bool(
        checkpoint is not None
        and checkpoint["card_entity_id"] == stall_card
        and len(board.cards) == 1
        and not any(key.startswith("e2-goal-line-card") for key in board.card_publishes)
        and board.deduplicated_publishes == []
    )
    store.close()
    return evidence(
        "daemon-first-runtime-reuses",
        passed,
        stall_card_entity_id=stall_card,
        checkpoint_card_entity_id=checkpoint["card_entity_id"] if checkpoint else None,
        card_count=len(board.cards),
        card_keys=sorted(board.cards),
        publishes=board.card_publishes,
        conflict_raised=False,
    )


def scenario_concurrent_first_create(work_dir: Path) -> dict[str, Any]:
    board = IdempotentFakeBoard()
    root = work_dir / "pb"
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / "runs"

    # The daemon wins the first create; the runtime's concurrent first ask
    # (no card visible) publishes through the same shared constructor/key and
    # receives ``deduplicated=True`` with the winner's entity id.
    stall_card = established_card(run_root, board)
    assert len(board.cards) == 1

    line_root = root / "line"
    port = LineInterruptPort(
        folder_id=FOLDER_ID,
        generation=1,
        store=GoalInterruptStore(line_root).open(),
        board=board,
        card_entity_id="",
        run_id="",
    )
    _question_note_id, adopted = port.ask(1, "blocker")

    passed = bool(
        adopted == stall_card
        and len(board.cards) == 1
        and board.deduplicated_publishes[-1] == goal_line_card_key(FOLDER_ID)
    )
    return evidence(
        "concurrent-first-create",
        passed,
        winner_card_entity_id=stall_card,
        runtime_adopted=adopted,
        card_count=len(board.cards),
        deduplicated_publishes=board.deduplicated_publishes,
        publishes=board.card_publishes,
        conflict_raised=False,
    )


def scenario_decision_content_in_production_resume(work_dir: Path) -> dict[str, Any]:
    root = work_dir / "dc"
    root.mkdir(parents=True, exist_ok=True)
    accept_bin = write_fake_coordinator_bin(root / "accept_bin.py", mode="accept")
    repark_bin = write_fake_coordinator_bin(root / "repark_bin.py", mode="repark")

    # --- main drill: the decision payload reaches the resumed coordinator round.
    line_root = root / "line1"
    line_root.mkdir(parents=True, exist_ok=True)
    port = LineInterruptPort(
        folder_id=FOLDER_ID,
        generation=1,
        store=GoalInterruptStore(line_root).open(),
        board=None,
        run_id="",
    )
    graph, store = suspension_line(line_root, port, FOLDER_ID)
    suspend(line_root, graph, FOLDER_ID)
    resume_key = resume_key_for(FOLDER_ID, 1, "e2-question:wf-1:1:1:q")
    checkpoint = store.interrupt(resume_key)
    assert checkpoint is not None

    question_note_id = checkpoint["question_note_id"]
    bus = FakeBus([decision_message("d-1", 1, question_note_id)])
    bus.link(question_note_id, "d-1")
    bridge = GoalInterruptBridge(
        GoalInterruptBridgeConfig(),
        store=store,
        bus=bus,
        resumer=production_resumer(line_root, FOLDER_ID, agent_run_bin=str(accept_bin)),
    )
    record = bridge.run_once()

    persisted = json.loads((line_root / "coord" / "round-1-input.json").read_text(encoding="utf-8"))
    injected = persisted.get("decision") or {}
    receipt = store.resume_receipt(resume_key)

    fields_ok = bool(
        injected.get("message_id") == "d-1"
        and injected.get("decision") == "APPROVE"
        and injected.get("rationale") == "accepted in drill"
        and isinstance(injected.get("refs"), list)
        and persisted.get("resume_key") == resume_key
    )
    receipt_ok = bool(
        receipt is not None
        and receipt["message_id"] == "d-1"
        and receipt["decision"] == "APPROVE"
        and receipt["rationale"] == "accepted in drill"
        and receipt["question_note_id"] == question_note_id
        and receipt["resume_key"] == resume_key
    )
    main_ok = bool(record["resumed"] == 1 and fields_ok and receipt_ok)

    # --- N7: a coordinator re-declaring blocked+decision without acknowledging
    # --- is rejected by the round-zero-repark guard (round advances, no
    # --- re-suspension on the stale blocker). Driven through the same
    # --- production-shaped resume path.
    from fleet_graph.goal_interrupt.contract import DecisionInput, DecisionRef
    from fleet_graph.graphs.runner import LineConfig, resume_goal_line

    line_root2 = root / "line2"
    line_root2.mkdir(parents=True, exist_ok=True)
    port2 = LineInterruptPort(
        folder_id=FOLDER_ID,
        generation=1,
        store=GoalInterruptStore(line_root2).open(),
        board=None,
        run_id="",
    )
    graph2, store2 = suspension_line(line_root2, port2, FOLDER_ID)
    suspend(line_root2, graph2, FOLDER_ID)
    resume_key2 = resume_key_for(FOLDER_ID, 1, "e2-question:wf-1:1:1:q")
    checkpoint2 = store2.interrupt(resume_key2)
    assert checkpoint2 is not None
    q2 = checkpoint2["question_note_id"]

    repark_decision = DecisionInput(
        message_id="d-2",
        channel_seq=1,
        decision="APPROVE",
        rationale="accepted in drill",
        decided_by="e2-card-drill",
        question_note_id=q2,
        card_entity_id="",
        refs=(DecisionRef("d-2", q2),),
        decided_at="2026-08-29T00:00:00Z",
        resume_key=resume_key2,
    )
    repark_config = LineConfig(
        folder_id=FOLDER_ID,
        seat=SEAT,
        run_root=line_root2,
        generation=1,
        alias=None,
        agent_run_bin=str(repark_bin),
    )
    n7_state, n7_status = resume_goal_line(repark_config, repark_decision)

    n7_ok = bool(
        n7_status == "resumed"
        and n7_state.get("terminal") == "done"
        and int(n7_state.get("round_no") or 1) > 1
        and not n7_state.get("__interrupt__")
    )

    store.close()
    store2.close()
    passed = bool(main_ok and n7_ok)
    return evidence(
        "decision-content-in-production-resume",
        passed,
        resumed=record["resumed"],
        persisted_round_input=persisted,
        injected_fields_ok=fields_ok,
        receipt_fields_ok=receipt_ok,
        receipt=receipt,
        resume_key=resume_key,
        n7_status=n7_status,
        n7_terminal=n7_state.get("terminal"),
        n7_round_no=n7_state.get("round_no"),
        n7_guard_fired=n7_ok,
    )


def scenario_real_bus_stall_resume(
    work_dir: Path, *, bus_url: str, bus_token_file: str, decision_token_file: str
) -> dict[str, Any]:
    tag = uuid.uuid4().hex[:8]
    folder = f"wf-e2card-{tag}"
    root = work_dir / "rb"
    root.mkdir(parents=True, exist_ok=True)
    line_root = root / folder
    line_root.mkdir(parents=True, exist_ok=True)

    bus_token = Path(bus_token_file).read_text(encoding="utf-8").strip()
    decision_token = Path(decision_token_file).read_text(encoding="utf-8").strip()
    client = BusClient(base_url=bus_url, token=bus_token)
    board = Board(client)
    decision_client = BusClient(base_url=bus_url, token=decision_token)

    # 1. Publish a card + question on the real bus with the shared
    #    key/constructor; read back the real entity ids.
    card_key = goal_line_card_key(folder)
    card = board.publish_card(
        goal_line_card_payload(folder_id=folder, title=folder),
        idempotency_key=card_key,
    )
    question_key = f"e2-question:{folder}:1:1"
    question_text = f"line {folder} waiting on a human decision (round 1)."
    pre_question = board.ask(
        card_entity_id=card.entity_id, question=question_text, idempotency_key=question_key
    )
    card_id = card.entity_id
    question_id = pre_question.question_note_id

    # 2. Suspend a synthetic, drill-tagged line through the E2 interrupt against
    #    a real SQLite store + real checkpointer. The interrupt reuses the card
    #    and re-asks the same question key, deduplicating to the step-1 note.
    port = LineInterruptPort(
        folder_id=folder,
        generation=1,
        store=GoalInterruptStore(line_root).open(),
        board=board,
        card_entity_id=card_id,
        run_id="",
    )
    graph, store = suspension_line(line_root, port, folder)
    state = suspend(line_root, graph, folder)
    suspended = state.get("__interrupt__") is not None

    resume_key = resume_key_for(folder, 1, question_id)
    checkpoint = store.interrupt(resume_key)
    assert checkpoint is not None
    assert checkpoint["card_entity_id"] == card_id

    cursor_before = store.cursor()

    # 3. Publish a real work.decision.v1 referencing the question, then run the
    #    real GoalInterruptBridge once.
    decision_key = f"e2card-drill-d:{tag}"
    decision = decision_client.publish(
        WORK_NOTES,
        DECISION_KIND,
        {
            "card_entity_id": card_id,
            "decided_by": "e2-card-passthrough-real-bus-drill",
            "decision": "APPROVE",
            "question": question_text,
            "rationale": "live stall->resume drill",
        },
        idempotency_key=decision_key,
        refs=[{"target_entity": question_id}],
    )
    decision_id = decision.message_id

    accept_bin = write_fake_coordinator_bin(root / "accept_bin.py", mode="accept")
    bridge = GoalInterruptBridge(
        GoalInterruptBridgeConfig(),
        store=store,
        bus=client,
        resumer=production_resumer(line_root, folder, agent_run_bin=str(accept_bin)),
    )
    record = bridge.run_once()
    cursor_after = store.cursor()

    persisted = json.loads((line_root / "coord" / "round-1-input.json").read_text(encoding="utf-8"))
    injected = persisted.get("decision") or {}
    receipt = store.resume_receipt(resume_key)

    # Exactly one card exists for the drill folder.
    index_messages, _head = client.messages(WORK_INDEX, limit=1000)
    folder_cards = [
        m
        for m in index_messages
        if m.get("kind") == CARD_KIND and (m.get("payload") or {}).get("work_folder_id") == folder
    ]

    passed = bool(
        suspended
        and record["resumed"] == 1
        and injected.get("message_id") == decision_id
        and persisted.get("resume_key") == resume_key
        and receipt is not None
        and receipt["message_id"] == decision_id
        and len(folder_cards) == 1
        and cursor_after >= cursor_before
    )
    store.close()
    return evidence(
        "real-bus-stall-resume",
        passed,
        bus_url=bus_url,
        drill_folder=folder,
        real_card_entity_id=card_id,
        real_question_note_id=question_id,
        real_decision_message_id=decision_id,
        refs_to_question=client.refs_to(question_id),
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        generation=1,
        round=1,
        resumed=record["resumed"],
        folder_card_count=len(folder_cards),
        persisted_resume_key=persisted.get("resume_key"),
        receipt_ok=receipt is not None,
    )


# --- cli ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=[
            "daemon-first-runtime-reuses",
            "concurrent-first-create",
            "decision-content-in-production-resume",
            "real-bus-stall-resume",
        ],
    )
    parser.add_argument("--bus-url", default="http://127.0.0.1:7490")
    parser.add_argument("--bus-token-file", default=None)
    parser.add_argument("--decision-token-file", default=None)
    parser.add_argument("--work-dir", default=None, help="scratch dir (default: a fresh temp dir)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.work_dir:
        work_dir = Path(args.work_dir)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="e2-card-passthrough-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    scenario = args.scenario
    if scenario == "daemon-first-runtime-reuses":
        result = scenario_daemon_first_runtime_reuses(work_dir)
    elif scenario == "concurrent-first-create":
        result = scenario_concurrent_first_create(work_dir)
    elif scenario == "decision-content-in-production-resume":
        result = scenario_decision_content_in_production_resume(work_dir)
    else:
        if not args.bus_token_file or not args.decision_token_file:
            print(
                "real-bus-stall-resume needs --bus-token-file and --decision-token-file",
                file=sys.stderr,
            )
            return 2
        result = scenario_real_bus_stall_resume(
            work_dir,
            bus_url=args.bus_url,
            bus_token_file=args.bus_token_file,
            decision_token_file=args.decision_token_file,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())

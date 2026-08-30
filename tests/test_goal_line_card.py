"""E2 goal-line card pass-through: one card per line, two converging producers.

The scheduler daemon (parking escalation) and the E2 interrupt runtime both
materialise a goal line's board card. They must converge on ONE card per line:
one shared idempotency key plus one shared payload constructor, so the bus
deduplicates the two producers instead of 409-ing (same key, divergent payload
-- the original failure) or duplicating (the ``e2-goal-line-card:`` hotfix).

The spec pins two timing paths:

- **Path A -- daemon creates the card first, runtime reuses.** The stall-state
  ``board_card_entity_id`` threads through ``spec_for`` -> ``--board-card`` ->
  ``LineConfig`` -> ``LineInterruptPort(card_entity_id=...)``, so the runtime's
  first ask reuses the existing card: exactly one ``work.card.v1``, the
  interrupt checkpoint's ``card_entity_id`` equals the stall state's, no
  ``BusConflict``, no ``e2-goal-line-card:`` publish.
- **Path B -- both producers race the first create.** No ``board_card_entity_id``
  is visible to either, both publish through the shared constructor + shared
  key, and the bus idempotency dedups: one publish wins, the other receives
  ``deduplicated=True`` with the winner's entity id, and both adopt it.

The fake board here faithfully models real idempotency semantics -- same key +
identical payload => deduplicate and return the existing entity; same key +
different payload => conflict -- because the whole bug is a contract-shape bug
the fake must not paper over.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.bus.board import (
    BusConflict,
    goal_line_card_key,
    goal_line_card_payload,
)
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


# --- fakes ------------------------------------------------------------------


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

    - fresh key => new entity, ``deduplicated=False``;
    - same key + identical payload => return the existing entity,
      ``deduplicated=True`` (no second entity);
    - same key + different payload => raise ``BusConflict`` (the real bus
      answers ``409 IDEMPOTENCY_CONFLICT``).
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


# --- helpers ----------------------------------------------------------------


def make_scheduler(tmp_path: Path, board: Any) -> Scheduler:
    return Scheduler(
        SchedulerConfig(
            lines=[
                LineSpec(folder_id=FOLDER_ID, seat="opencode-dsv4pro", alias=ALIAS, enabled=True)
            ],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=FakeLauncher(),
        units=FakeUnits(),
        clock=Clock(PRIME_EPOCH),
        sleep=lambda _s: None,
        wake=FakeWake(),
        board=board,
    )


def write_blocked(tmp_path: Path) -> None:
    record = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": "run-b1",
        "at": BLOCKED_AT,
        "reason": "等监督面拍板",
        "waiting_on": "decision",
    }
    path = tmp_path / "runs" / FOLDER_ID / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def stall_file(tmp_path: Path) -> Path:
    return tmp_path / "runs" / ".scheduler" / f"{FOLDER_ID}.json"


def established_card(tmp_path: Path, board: Any) -> str:
    """Drive the scheduler: prime launch, block, park -- the parking escalation
    materialises the line's card and persists ``board_card_entity_id``."""
    scheduler = make_scheduler(tmp_path, board)
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    write_blocked(tmp_path)
    scheduler.clock = Clock(TICK_EPOCH)
    scheduler.tick()  # establish the park -> card + question
    state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
    return str(state["board_card_entity_id"])


def build_line_graph(root: Path, port: LineInterruptPort) -> tuple[Any, GoalInterruptStore]:
    store = GoalInterruptStore(root / "gi").open()
    deps = LineDeps(
        coordinator=BlockingCoordinator(),
        worker=NullInbox(),
        inbox=NullInbox(),
        artifacts=RecordingArtifacts(),
        guards=LineGuards(bounds=LineBounds(max_rounds=50)),
        folder_id=FOLDER_ID,
        interrupt=port,
    )
    return build_goal_line_graph(deps), store


def run_graph_to_interrupt(graph: Any, checkpoint_db: str) -> dict[str, Any]:
    cfg = {"configurable": {"thread_id": f"{FOLDER_ID}:g1"}, "recursion_limit": 200}
    with SqliteSaver.from_conn_string(checkpoint_db) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke({"round_no": 1}, config=cfg)
    assert state.get("__interrupt__")
    return state


# --- Path A: daemon first, runtime reuses -----------------------------------


class TestPathADaemonFirst:
    def test_runtime_reuses_the_stall_card_and_no_second_card_is_published(
        self, tmp_path: Path
    ) -> None:
        """Exactly one ``work.card.v1`` across the whole drill; the runtime's
        ask returns the stall-state card entity id, and no ``e2-goal-line-card:``
        publish ever happens."""
        board = IdempotentFakeBoard()
        stall_card = established_card(tmp_path, board)
        assert stall_card.startswith("card-")

        root = tmp_path / "run"
        port = LineInterruptPort(
            folder_id=FOLDER_ID,
            generation=1,
            store=GoalInterruptStore(root / "gi").open(),
            board=board,
            card_entity_id=stall_card,  # threaded through --board-card
            run_id="",
        )
        question_note_id, adopted = port.ask(1, "blocker")

        assert adopted == stall_card
        assert question_note_id.startswith("note-")
        # exactly one card, the daemon's, on the shared key
        assert len(board.cards) == 1
        assert goal_line_card_key(FOLDER_ID) in board.cards
        assert not any(key.startswith("e2-goal-line-card") for key in board.card_publishes)
        # the runtime never published (its card was already known)
        assert board.deduplicated_publishes == []

    def test_the_interrupt_checkpoint_adopts_the_stall_card(self, tmp_path: Path) -> None:
        """The full graph: the runtime's ask reuses the stall card, so the
        persisted interrupt checkpoint's ``card_entity_id`` equals the stall
        state's -- and the whole drill still holds exactly one card."""
        from fleet_graph.goal_interrupt.contract import resume_key_for

        board = IdempotentFakeBoard()
        stall_card = established_card(tmp_path, board)
        root = tmp_path / "run"
        port = LineInterruptPort(
            folder_id=FOLDER_ID,
            generation=1,
            store=GoalInterruptStore(root / "gi").open(),
            board=board,
            card_entity_id=stall_card,
            run_id="",
        )
        graph, store = build_line_graph(root, port)
        run_graph_to_interrupt(graph, str(root / "cp.sqlite3"))

        resume_key = resume_key_for(FOLDER_ID, 1, "note-e2-question:wf-1:1:1")
        checkpoint = store.interrupt(resume_key)
        assert checkpoint is not None
        assert checkpoint["card_entity_id"] == stall_card
        assert len(board.cards) == 1
        store.close()


# --- Path B: both producers race the first create ---------------------------


class TestPathBBothRace:
    def test_runtime_adopts_a_deduplicated_first_create(self, tmp_path: Path) -> None:
        """The daemon wins the first create; the runtime's concurrent first ask
        (no card visible yet) publishes through the shared constructor and
        receives ``deduplicated=True`` with the winner's entity id, which it
        adopts -- one card, both producers agree."""
        board = IdempotentFakeBoard()
        stall_card = established_card(tmp_path, board)
        assert len(board.cards) == 1

        root = tmp_path / "run"
        port = LineInterruptPort(
            folder_id=FOLDER_ID,
            generation=1,
            store=GoalInterruptStore(root / "gi").open(),
            board=board,
            card_entity_id="",  # the race: no card visible to the runtime
            run_id="",
        )
        question_note_id, adopted = port.ask(1, "blocker")

        assert adopted == stall_card
        assert question_note_id.startswith("note-")
        assert len(board.cards) == 1  # still one card
        assert board.deduplicated_publishes[-1] == goal_line_card_key(FOLDER_ID)

    def test_the_daemon_dedups_when_the_runtime_wins_the_first_create(self, tmp_path: Path) -> None:
        """The other order: the runtime creates the card first; the daemon's
        parking escalation then races the same key+payload, deduplicates, and
        persists the same entity id -- one card, both producers agree."""
        board = IdempotentFakeBoard()
        root = tmp_path / "run"
        store = GoalInterruptStore(root / "gi").open()
        port = LineInterruptPort(
            folder_id=FOLDER_ID,
            generation=1,
            store=store,
            board=board,
            card_entity_id="",
            run_id="",
        )
        question_note_id, runtime_card = port.ask(1, "blocker")
        assert len(board.cards) == 1
        store.close()

        # The daemon has no stall card yet (the race): its escalation publishes
        # the same key+payload and gets the existing entity back.
        scheduler = make_scheduler(tmp_path, board)
        scheduler.tick()
        write_blocked(tmp_path)
        scheduler.clock = Clock(TICK_EPOCH)
        scheduler.tick()

        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["board_card_entity_id"] == runtime_card
        assert len(board.cards) == 1
        assert board.deduplicated_publishes[-1] == goal_line_card_key(FOLDER_ID)
        assert question_note_id.startswith("note-")


# --- E2 -> scheduler stall-state write-side convergence (#170 follow-up) ----


class TestE2WritesSchedulerStallState:
    """The second parked-write path (the E2 in-graph interrupt) must converge
    with the scheduler's stall state on the same question note. Before the fix,
    ``LineInterruptPort.persist`` wrote the question note only to
    ``goal-interrupt.sqlite3``; the decision bridge (which reads only
    ``.scheduler/<folder_id>.json``) then read a null ``board_question_note_id``
    and swallowed the human's approve as ``no_waiting_owner``. These tests
    synthesize both write surfaces and assert they converge on the same note."""

    def test_the_e2_interrupt_writes_the_question_note_into_the_stall_state(
        self, tmp_path: Path
    ) -> None:
        """Full graph to the interrupt: the persisted checkpoint and the
        scheduler stall-state carry the *same* question note and card."""
        from fleet_graph.goal_interrupt.contract import resume_key_for

        board = IdempotentFakeBoard()
        stall = stall_file(tmp_path)
        root = tmp_path / "run"
        port = LineInterruptPort(
            folder_id=FOLDER_ID,
            generation=1,
            store=GoalInterruptStore(root / "gi").open(),
            board=board,
            card_entity_id="",
            run_id="run-1",
            stall_state_path=stall,
        )
        graph, store = build_line_graph(root, port)
        run_graph_to_interrupt(graph, str(root / "cp.sqlite3"))

        question_note_id = "note-parked:wf-1:run-1"
        checkpoint = store.interrupt(resume_key_for(FOLDER_ID, 1, question_note_id))
        assert checkpoint is not None
        assert checkpoint["question_note_id"] == question_note_id
        assert checkpoint["card_entity_id"] == "card-goal-line-card:wf-1"

        state = json.loads(stall.read_text(encoding="utf-8"))
        assert state["board_question_note_id"] == question_note_id
        assert state["board_card_entity_id"] == checkpoint["card_entity_id"]
        store.close()

    def test_the_e2_question_resolves_to_the_line_not_no_waiting_owner(
        self, tmp_path: Path
    ) -> None:
        """The decision bridge's line owner reads the stall state the E2
        interrupt wrote, so a ``work.decision.v1`` answering the E2 question
        resolves to the parked line instead of ``no_waiting_owner``."""
        from fleet_graph.decision_bridge.owners import OWNER_KIND_LINE, LineOwnerSource
        from fleet_graph.decision_bridge.resolver import resolve_decision

        board = IdempotentFakeBoard()
        stall = stall_file(tmp_path)
        root = tmp_path / "run"
        port = LineInterruptPort(
            folder_id=FOLDER_ID,
            generation=1,
            store=GoalInterruptStore(root / "gi").open(),
            board=board,
            card_entity_id="",
            run_id="run-1",
            stall_state_path=stall,
        )
        graph, store = build_line_graph(root, port)
        run_graph_to_interrupt(graph, str(root / "cp.sqlite3"))
        store.close()

        # The scheduler parks the line: the parked snapshot lands in the same
        # stall-state file the E2 interrupt already wrote the question note to.
        state = json.loads(stall.read_text(encoding="utf-8"))
        state["parked_run_id"] = "run-1"
        state["parked_at"] = 1_700_000_000.0
        stall.write_text(json.dumps(state), encoding="utf-8")

        question_note_id = "note-parked:wf-1:run-1"
        decision = {
            "message_id": "d-1",
            "channel_seq": 1,
            "kind": "work.decision.v1",
            "payload": {"decision": "APPROVE", "card_entity_id": "card-goal-line-card:wf-1"},
        }
        source = LineOwnerSource(tmp_path / "runs", ["wf-1"])
        resolution = resolve_decision(
            decision,
            source,
            refs_to=lambda q: (
                [{"message_id": "d-1", "target_entity": question_note_id}]
                if q == question_note_id
                else []
            ),
        )
        assert resolution.ok
        assert resolution.target is not None
        assert resolution.target.kind == OWNER_KIND_LINE
        assert resolution.target.id == "wf-1"


# --- the fake must not paper over the contract-shape bug --------------------


class TestFaithfulIdempotency:
    def test_same_key_different_payload_conflicts(self) -> None:
        """Why the original failure existed: same key + divergent payload is a
        409, not a dedup. The fake models this so a regression to a divergent
        payload fails loudly instead of quietly passing."""
        board = IdempotentFakeBoard()
        board.publish_card(
            goal_line_card_payload(folder_id=FOLDER_ID, title=ALIAS),
            idempotency_key=goal_line_card_key(FOLDER_ID),
        )
        with pytest.raises(BusConflict):
            board.publish_card(
                {
                    "title": FOLDER_ID,
                    "status": "doing",
                    "intent": "divergent",
                    "work_folder_id": FOLDER_ID,
                },
                idempotency_key=goal_line_card_key(FOLDER_ID),
            )

    def test_same_key_identical_payload_deduplicates(self) -> None:
        board = IdempotentFakeBoard()
        first = board.publish_card(
            goal_line_card_payload(folder_id=FOLDER_ID, title=ALIAS),
            idempotency_key=goal_line_card_key(FOLDER_ID),
        )
        second = board.publish_card(
            goal_line_card_payload(folder_id=FOLDER_ID, title=ALIAS),
            idempotency_key=goal_line_card_key(FOLDER_ID),
        )
        assert second.deduplicated is True
        assert second.entity_id == first.entity_id
        assert len(board.cards) == 1


# --- the pass-through wiring (no graph, no board needed) --------------------


class TestPassThroughWiring:
    def test_spec_for_threads_the_stall_card_into_the_launch_argv(self, tmp_path: Path) -> None:
        stall = stall_file(tmp_path)
        stall.parent.mkdir(parents=True, exist_ok=True)
        stall.write_text(json.dumps({"board_card_entity_id": "card-xyz"}), encoding="utf-8")

        scheduler = make_scheduler(tmp_path, board=None)
        spec = scheduler.spec_for(
            LineSpec(folder_id=FOLDER_ID, seat="s", alias=ALIAS, enabled=True)
        )
        assert spec.board_card_entity_id == "card-xyz"
        argv = spec.argv()
        assert argv[argv.index("--board-card") + 1] == "card-xyz"

    def test_an_absent_stall_card_means_no_flag(self, tmp_path: Path) -> None:
        scheduler = make_scheduler(tmp_path, board=None)
        spec = scheduler.spec_for(
            LineSpec(folder_id=FOLDER_ID, seat="s", alias=ALIAS, enabled=True)
        )
        assert spec.board_card_entity_id == ""
        assert "--board-card" not in spec.argv()

    def test_the_cli_parses_board_card_into_the_line_config(self, tmp_path: Path) -> None:
        from fleet_graph.cli import build_parser
        from fleet_graph.graphs.runner import LineConfig

        args = build_parser().parse_args(
            ["line", "run", "--folder", FOLDER_ID, "--seat", "s", "--board-card", "card-xyz"]
        )
        config = LineConfig(
            folder_id=args.folder,
            seat=args.seat,
            run_root=tmp_path,
            alias=args.alias,
            board_card_entity_id=args.board_card or "",
        )
        assert config.board_card_entity_id == "card-xyz"

    def test_build_interrupt_threads_the_card_into_the_port(self, tmp_path: Path) -> None:
        from fleet_graph.graphs.runner import LineConfig, _build_interrupt

        config = LineConfig(
            folder_id=FOLDER_ID,
            seat="s",
            run_root=tmp_path,
            board_card_entity_id="card-xyz",
            alias=ALIAS,
        )
        port = _build_interrupt(config, run_id="run-x")
        assert port is not None
        assert port.card_entity_id == "card-xyz"
        port.store.close()

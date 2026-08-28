#!/usr/bin/env python3
"""E2 goal-interrupt acceptance: isolated drills over the real durable surfaces.

Five scenarios, each exercising the real SQLite ``GoalInterruptStore``, the real
``build_goal_line_graph`` interrupt path, and the real language-graph
checkpointer -- the only fakes are the coordinator/worker at the line's edge and
the board bus the bridge reads:

- ``decision-content-injected`` -- a decision resume injects the immutable
  ``DecisionInput`` into the resumed coordinator envelope, the coordinator
  acknowledges ``message_id``, and the line continues the same generation
  instead of re-parking.
- ``legacy-owner-fallback`` -- the bounded fallback resumes exactly one legacy
  parked owner; ambiguity is a no-resume ``legacy_owner_ambiguous``.
- ``cursor-compensation`` -- the bridge recovers a decision the cursor paged
  past by querying the decision chain, records a ``cursor_compensation``
  receipt, and never rolls the cursor back or republishes.
- ``kill-restart-no-replay`` -- a SIGKILL after a persistent turn claim is
  followed by a restart that re-adopts the same turn (one charge, no round
  replay).
- ``suspend-24h-then-bridge-resume`` -- a controllable clock advances 86400
  seconds and the bridge resumes the same generation/continuation with no new
  question, no new round, and no duplicate charge.

Evidence is one JSON object per scenario on stdout; the process exits non-zero
when the scenario does not pass.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.decision_bridge.owners import OwnerResult, OwnerTarget
from fleet_graph.goal_interrupt.bridge import GoalInterruptBridge, GoalInterruptBridgeConfig
from fleet_graph.goal_interrupt.contract import (
    DecisionInput,
    DecisionRef,
    resume_key_for,
)
from fleet_graph.goal_interrupt.resolver import (
    LEGACY_OUTCOME_AMBIGUOUS,
)
from fleet_graph.goal_interrupt.runtime import LineInterruptPort, resume_line
from fleet_graph.goal_interrupt.store import GoalInterruptStore
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards

QUESTION_ID = "e2-question:wf-1:1:1:q"
FOLDER_ID = "wf-1"
RESUME_KEY = resume_key_for(FOLDER_ID, 1, QUESTION_ID)
CFG = {"configurable": {"thread_id": f"{FOLDER_ID}:g1"}, "recursion_limit": 200}


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


class NullInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class RecordingArtifacts:
    def __init__(self) -> None:
        self.terminals: list[dict[str, Any]] = []

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        return True

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminals.append(kwargs)
        return "terminal.json"

    def write_fault_terminal(self, **kwargs: Any) -> str:
        return "fault"


class Coordinator:
    """Round 1 blocks on a decision; the resume turn acknowledges and continues."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((round_no, dict(coord_input)))
        if round_no == 1 and "decision" not in coord_input:
            return {"verdict": "blocked", "waiting_on": "decision", "reason": "need human"}
        if round_no == 1 and "decision" in coord_input:
            return {
                "verdict": "continue",
                "next_prompt": "proceed",
                "acknowledged_message_id": coord_input["decision"]["message_id"],
            }
        return {"verdict": "done", "reason": "finished"}


class Worker:
    def turn(self, prompt: str, round_no: int) -> str:
        return f"did {prompt}"


class FakeBus:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages_list = messages or []
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


class _LegacyOwners:
    """A seam owner source returning exactly the legacy owners a scenario staged."""

    def __init__(self, owners: list[OwnerTarget]) -> None:
        self.owners = owners
        self.resumed: list[tuple[str, str]] = []

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        return [t for t in self.owners if t.question_note_id == question_note_id]

    def discover_all(self) -> list[OwnerTarget]:
        return list(self.owners)

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        self.resumed.append((target.id, action_key))
        return OwnerResult("resumed", "ok")


def decision_message(message_id: str, seq: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.decision.v1",
        "created_at": "2026-08-29T00:00:00Z",
        "payload": {
            "decision": "APPROVE",
            "rationale": "accepted",
            "decided_by": "human",
            "card_entity_id": "card-1",
        },
    }


def decision_input(message_id: str) -> DecisionInput:
    return DecisionInput(
        message_id=message_id,
        channel_seq=1,
        decision="APPROVE",
        rationale="accepted",
        decided_by="human",
        question_note_id=QUESTION_ID,
        card_entity_id="card-1",
        refs=(DecisionRef(message_id, QUESTION_ID),),
        decided_at="2026-08-29T00:00:00Z",
        resume_key=RESUME_KEY,
    )


def build_line(
    root: Path, coordinator: Any
) -> tuple[Any, LineInterruptPort, GoalInterruptStore, Any]:
    store = GoalInterruptStore(root / "gi").open()
    port = LineInterruptPort(folder_id=FOLDER_ID, generation=1, store=store)
    worker = Worker()
    deps = LineDeps(
        coordinator=coordinator,
        worker=worker,
        inbox=NullInbox(),
        artifacts=RecordingArtifacts(),
        guards=LineGuards(bounds=LineBounds(max_rounds=50)),
        folder_id=FOLDER_ID,
        interrupt=port,
    )
    return build_goal_line_graph(deps), port, store, worker


# --- scenarios ---------------------------------------------------------------


def scenario_decision_content_injected(work_dir: Path) -> dict[str, Any]:
    root = work_dir / "dc"
    root.mkdir(parents=True, exist_ok=True)
    coordinator = Coordinator()
    graph, _port, store, _worker = build_line(root, coordinator)
    with SqliteSaver.from_conn_string(str(root / "cp.sqlite3")) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke({"round_no": 1}, config=CFG)
        suspended = state.get("__interrupt__") is not None

        decision = decision_input("d-1")
        state, status = resume_line(compiled, config=CFG, decision=decision, store=store)

    resume_turn = next((c for c in coordinator.calls if "decision" in c[1]), None)
    injected = resume_turn[1]["decision"] if resume_turn is not None else {}
    acknowledged = (
        resume_turn is not None
        and resume_turn[1]["decision"]["message_id"] == "d-1"
        and "resume_key" in resume_turn[1]
    )
    checkpoint = store.interrupt(RESUME_KEY)

    passed = bool(
        suspended
        and status == "resumed"
        and state.get("terminal") == "done"
        and acknowledged
        and injected.get("decision") == "APPROVE"
        and injected.get("rationale") == "accepted"
        and injected.get("decided_by") == "human"
        and injected.get("question_note_id") == QUESTION_ID
        and injected.get("card_entity_id") == "card-1"
        and injected.get("resume_key") == RESUME_KEY
        and checkpoint is not None
        and checkpoint["generation"] == 1
        and checkpoint["round_id"] == 1
        and checkpoint["folder_id"] == FOLDER_ID
    )
    return evidence(
        "decision-content-injected",
        passed,
        suspended=suspended,
        status=status,
        terminal=state.get("terminal"),
        acknowledged=acknowledged,
        resumed_envelope_fields=sorted(injected),
        checkpoint_fields=sorted(checkpoint) if checkpoint else None,
        resume_key=RESUME_KEY,
    )


def scenario_legacy_owner_fallback(work_dir: Path) -> dict[str, Any]:
    from fleet_graph.decision_bridge.bridge import DecisionBridge, DecisionBridgeConfig
    from fleet_graph.decision_bridge.owners import LineOwnerSource
    from fleet_graph.decision_bridge.store import BridgeStore

    root = work_dir / "lo"
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / "runs"

    def legacy_source(folder_id: str, generation: int) -> LineOwnerSource:
        stall = run_root / ".scheduler" / f"{folder_id}.json"
        stall.parent.mkdir(parents=True, exist_ok=True)
        stall.write_text(
            json.dumps(
                {
                    "generation": generation,
                    "parked_run_id": f"run-{folder_id}",
                    "parked_at": 1699999999.0,
                    "board_card_entity_id": "card-1",
                    "board_question_note_id": "",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return LineOwnerSource(run_root, lines=[{"folder_id": folder_id, "generation": 1}])

    def question_note(message_id: str, text: str) -> dict[str, Any]:
        return {
            "message_id": message_id,
            "channel_seq": 1,
            "kind": "work.note.v1",
            "created_at": "2026-08-29T00:00:00Z",
            "payload": {"note": text, "note_type": "question", "card_entity_id": "card-1"},
        }

    # Unique legacy owner: the real bridge resolves it, records
    # legacy_owner_resolution, and resumes exactly that parked line.
    bus = FakeBus(
        [question_note("q-1", "line wf-abc needs a decision"), decision_message("d-1", 2)]
    )
    bus.link("q-1", "d-1")
    store = BridgeStore(root / "bridge").open()
    bridge = DecisionBridge(
        DecisionBridgeConfig(state_dir=root / "bridge"),
        bus=bus,
        owner_source=legacy_source("wf-abc", 2),
        store=store,
    )
    record = bridge.run_once()
    receipt = store.receipt("d-1")
    unique_ok = bool(
        record["resumed"] == 1
        and receipt is not None
        and receipt["status"] == "resumed"
        and receipt["reason"] == "legacy_owner_resolution"
        and receipt["target_id"] == "wf-abc"
    )
    store.close()

    # Ambiguity: two same-folder legacy owners -> safe no-resume, fallback active.
    owners = _LegacyOwners(
        [
            OwnerTarget("line", "wf-abc", 2, "", "card-1", "parked"),
            OwnerTarget("line", "wf-abc", 3, "", "card-1", "parked"),
        ]
    )
    bus2 = FakeBus(
        [question_note("q-2", "line wf-abc needs a decision"), decision_message("d-2", 2)]
    )
    bus2.link("q-2", "d-2")
    store2 = BridgeStore(root / "bridge2").open()
    bridge2 = DecisionBridge(
        DecisionBridgeConfig(state_dir=root / "bridge2"),
        bus=bus2,
        owner_source=owners,
        store=store2,
    )
    record2 = bridge2.run_once()
    receipt2 = store2.receipt("d-2")
    ambiguous_ok = bool(
        record2["resumed"] == 0
        and owners.resumed == []
        and receipt2 is not None
        and receipt2["status"] == "noop"
        and receipt2["reason"] == LEGACY_OUTCOME_AMBIGUOUS
    )
    store2.close()

    return evidence(
        "legacy-owner-fallback",
        bool(unique_ok and ambiguous_ok),
        unique_outcome="resumed",
        unique_target="wf-abc",
        unique_reason=receipt["reason"] if receipt is not None else None,
        unique_status=receipt["status"] if receipt is not None else None,
        ambiguous_outcome=LEGACY_OUTCOME_AMBIGUOUS,
        ambiguous_status=receipt2["status"] if receipt2 is not None else None,
        ambiguous_resumed=record2["resumed"],
    )


def scenario_cursor_compensation(work_dir: Path) -> dict[str, Any]:
    root = work_dir / "cc"
    root.mkdir(parents=True, exist_ok=True)
    store = GoalInterruptStore(root / "gi").open()
    store.put_interrupt(
        {
            "resume_key": RESUME_KEY,
            "folder_id": FOLDER_ID,
            "generation": 1,
            "round_id": 1,
            "question_note_id": QUESTION_ID,
            "card_entity_id": "card-1",
            "prior_terminal_digest": "d",
        }
    )
    # The decision is served only through the reverse refs, and the cursor has
    # already paged past its position (seq 9): recovery comes from the chain,
    # never by rolling the cursor back.
    bus = FakeBus([decision_message("d-missed", 9)])
    bus.link(QUESTION_ID, "d-missed")
    store.advance_cursor(10)
    cursor_before = store.cursor()

    resumes: list[DecisionInput] = []
    bridge = GoalInterruptBridge(
        GoalInterruptBridgeConfig(), store=store, bus=bus, resumer=resumes.append
    )
    record = bridge.run_once()
    compensation = store.compensation_receipt(RESUME_KEY)

    passed = bool(
        record["resumed"] == 1
        and [d.message_id for d in resumes] == ["d-missed"]
        and resumes[0].resume_key == RESUME_KEY
        and compensation is not None
        and compensation["last_decision_message_id"] == "d-missed"
        and store.cursor() >= cursor_before  # never rolled back
    )
    return evidence(
        "cursor-compensation",
        passed,
        resumed=record["resumed"],
        recovered=[d.message_id for d in resumes],
        compensation_last=compensation["last_decision_message_id"] if compensation else None,
        cursor_before=cursor_before,
        cursor=store.cursor(),
    )


DRIVER = """#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from langgraph.checkpoint.sqlite import SqliteSaver

sys.path.insert(0, {src!r})

from fleet_graph.goal_interrupt.store import GoalInterruptStore
from fleet_graph.goal_interrupt.runtime import LineInterruptPort, resume_line
from fleet_graph.goal_interrupt.contract import DecisionInput, DecisionRef, resume_key_for
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards

root = Path(sys.argv[1])
FOLDER_ID = "wf-1"
QUESTION_ID = "e2-question:wf-1:1:1:q"
RESUME_KEY = resume_key_for(FOLDER_ID, 1, QUESTION_ID)
CFG = {{"configurable": {{"thread_id": f"{{FOLDER_ID}}:g1"}}, "recursion_limit": 200}}

class NullInbox:
    def drain_then_ack(self, persist):
        persist([]); return [], []

class Artifacts:
    def heartbeat(self, *a, **k): return True
    def append_round(self, l): return True
    def write_terminal(self, **k): return "t"
    def write_fault_terminal(self, **k): return "t"

class Worker:
    def turn(self, prompt, round_no): return f"did {{prompt}}"

class Coordinator:
    def __init__(self, root):
        self.root = root
        self.release = root / "release"
    def turn(self, round_no, coord_input):
        if round_no == 1 and "decision" not in coord_input:
            return {{"verdict": "blocked", "waiting_on": "decision", "reason": "need human"}}
        import os, time
        claim = self.root / "claimed"
        try:
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return {{"verdict": "done", "reason": "re-adopted"}}
        deadline = time.monotonic() + 30
        while not self.release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if "decision" in coord_input:
            return {{"verdict": "continue", "next_prompt": "go",
                     "acknowledged_message_id": coord_input["decision"]["message_id"]}}
        return {{"verdict": "done", "reason": "finished"}}

store = GoalInterruptStore(root / "gi").open()
port = LineInterruptPort(folder_id=FOLDER_ID, generation=1, store=store)
coord = Coordinator(root)
deps = LineDeps(coordinator=coord, worker=Worker(), inbox=NullInbox(),
                artifacts=Artifacts(), guards=LineGuards(bounds=LineBounds(max_rounds=50)),
                folder_id=FOLDER_ID, interrupt=port)
graph = build_goal_line_graph(deps)
with SqliteSaver.from_conn_string(str(root / "cp.sqlite3")) as saver:
    compiled = graph.compile(checkpointer=saver)
    snapshot = compiled.get_state(CFG)
    if not snapshot.next:
        compiled.invoke({{"round_no": 1}}, config=CFG)
    decision = DecisionInput(message_id="d-1", channel_seq=1, decision="APPROVE",
                             rationale="r", decided_by="human",
                             question_note_id=QUESTION_ID, card_entity_id="card-1",
                             refs=(DecisionRef("d-1", QUESTION_ID),), decided_at="t",
                             resume_key=RESUME_KEY)
    state, status = resume_line(compiled, config=CFG, decision=decision, store=store)
print(json.dumps({{"terminal": state.get("terminal"), "round_no": state.get("round_no"),
                   "status": status}}))
"""


def scenario_kill_restart_no_replay(work_dir: Path, *, kill_after: str) -> dict[str, Any]:
    root = work_dir / "kr"
    root.mkdir(parents=True, exist_ok=True)
    src = str(Path(__file__).resolve().parent.parent / "src")
    driver_text = DRIVER.format(src=json.dumps(src))
    driver = root / "driver.py"
    driver.write_text(driver_text, encoding="utf-8")

    claimed_marker = root / "claimed"
    release = root / "release"

    # Phase 1: suspend, then resume dispatches a turn claim and blocks in-flight.
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not claimed_marker.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        claimed_before_kill = claimed_marker.exists()

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

        # Phase 2: restart re-adopts the claimed turn without a second claim.
        proc2 = subprocess.Popen(
            [sys.executable, str(driver), str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc2.communicate(timeout=60)
        result = json.loads(out.decode() or "{}")

        store = GoalInterruptStore(root / "gi").open()
        turn_id = f"{RESUME_KEY}:turn:1"
        charges = store.turn_invocations(turn_id)

        passed = bool(claimed_before_kill and result.get("terminal") == "done" and charges == 1)
        return evidence(
            "kill-restart-no-replay",
            passed,
            kill_after=kill_after,
            claimed_before_kill=claimed_before_kill,
            phase2_terminal=result.get("terminal"),
            phase2_status=result.get("status"),
            turn_charges=charges,
            stderr=err.decode()[-400:] if err else "",
        )
    finally:
        release.touch()
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=5)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now_value = start

    def __call__(self) -> float:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += seconds


def scenario_suspend_24h_then_bridge_resume(
    work_dir: Path, *, suspend_seconds: float
) -> dict[str, Any]:
    root = work_dir / "s24"
    root.mkdir(parents=True, exist_ok=True)
    clock = FakeClock()
    store = GoalInterruptStore(root / "gi", clock=clock).open()

    coordinator = Coordinator()
    port = LineInterruptPort(folder_id=FOLDER_ID, generation=1, store=store)
    deps = LineDeps(
        coordinator=coordinator,
        worker=Worker(),
        inbox=NullInbox(),
        artifacts=RecordingArtifacts(),
        guards=LineGuards(bounds=LineBounds(max_rounds=50)),
        folder_id=FOLDER_ID,
        interrupt=port,
    )
    graph = build_goal_line_graph(deps)
    bus = FakeBus([decision_message("d-1", 1)])
    bus.link(QUESTION_ID, "d-1")
    bridge = GoalInterruptBridge(GoalInterruptBridgeConfig(), store=store, bus=bus)

    resume_result: list[tuple[dict[str, Any], str]] = []

    # The saver stays open across the suspend and the bridge resume, because the
    # resume re-enters the same thread's checkpoint.
    with SqliteSaver.from_conn_string(str(root / "cp.sqlite3")) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke({"round_no": 1}, config=CFG)
        suspended = state.get("__interrupt__") is not None

        # A 24-hour wall-clock advance is simulated through the controllable
        # clock -- the suspension is durable, not a sleep.
        clock.advance(suspend_seconds)

        def resumer(decision: DecisionInput) -> str:
            state, status = resume_line(compiled, config=CFG, decision=decision, store=store)
            resume_result.append((state, status))
            return status

        bridge.resumer = resumer
        record = bridge.run_once()

    state, status = resume_result[0] if resume_result else ({}, "no_resume")
    elapsed = clock.now_value - 1_700_000_000.0
    charged = store.turn_invocations(f"{RESUME_KEY}:turn:1")
    # The second bridge pass must not resume again (it already did).
    bridge.resumer = lambda d: "resumed"
    record2 = bridge.run_once()

    passed = bool(
        suspended
        and elapsed == suspend_seconds
        and len(resume_result) == 1
        and status == "resumed"
        and state.get("terminal") == "done"
        and state.get("round_no") == 2  # continuation, not a fresh round / question
        and charged == 1
        and record2["resumed"] == 0
        and record["resumed"] == 1
    )
    return evidence(
        "suspend-24h-then-bridge-resume",
        passed,
        suspend_seconds=suspend_seconds,
        elapsed_seconds=elapsed,
        terminal=state.get("terminal"),
        round_no=state.get("round_no"),
        turn_charges=charged,
        resumes=len(resume_result),
        second_pass_resumed=record2["resumed"],
    )


# --- cli ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=[
            "decision-content-injected",
            "legacy-owner-fallback",
            "cursor-compensation",
            "kill-restart-no-replay",
            "suspend-24h-then-bridge-resume",
        ],
    )
    parser.add_argument("--kill-after", default="turn_claimed", choices=["turn_claimed"])
    parser.add_argument("--suspend-seconds", type=float, default=86400)
    parser.add_argument("--work-dir", default=None, help="scratch dir (default: a fresh temp dir)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.work_dir:
        work_dir = Path(args.work_dir)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="e2-goal-interrupt-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    scenario = args.scenario
    if scenario == "decision-content-injected":
        result = scenario_decision_content_injected(work_dir)
    elif scenario == "legacy-owner-fallback":
        result = scenario_legacy_owner_fallback(work_dir)
    elif scenario == "cursor-compensation":
        result = scenario_cursor_compensation(work_dir)
    elif scenario == "kill-restart-no-replay":
        result = scenario_kill_restart_no_replay(work_dir, kill_after=args.kill_after)
    else:
        result = scenario_suspend_24h_then_bridge_resume(
            work_dir, suspend_seconds=args.suspend_seconds
        )

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())

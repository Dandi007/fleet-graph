"""spec-m4b evidence face: line_message ack 落档 + 驻停对照，端到端机械可验.

The committed evidence behind verify-lim checks 15/16 (spec-m4b 交付面): the
M4 chain is driven end-to-end over real product code -- real
``deliver_line_message`` (closed payload, supervisor/alias guards), the real
goal-line graph round (``drain_then_ack`` → ack obligation → ledger + rounds
mirror → verdict → ``finalise`` terminal), the real ``Scheduler.tick()`` (park
establish / ``woken:inbox`` / re-establish), and the real read model
(``FleetStateView`` / HTTP handler on an ephemeral port).

Red lines pinned here (spec 红靶条款):

- ack rows land in ``<run_root>/<line>/line-message-acks.jsonl`` with the
  contract shape ``{round, at, message_id, outcome, reason}`` (+``kind``),
  and mirror into rounds.jsonl (``line_message_acks``/``unacked_instructions``);
- ``info`` never produces an ack row (阴性①);
- a bare ``APPROVE`` instruction is pump-guard-rejected
  ``rejected/message_is_not_a_decision`` even when the verdict itself tries to
  execute it, and no decision artifact ever appears (阴性②);
- the state face folds ``wake_facts.line_message_acks`` latest-first and
  capped, and exposes nothing when there is no ledger;
- one full receipt event (deliver → ``woken:inbox`` tick → line re-runs and
  re-blocks → re-park) leaves every waiting_decision fact field unchanged:
  an inbox message alone never lifts the park (check 16's invariant).

Everything runs against a scratch run root; nothing outside tmp_path is read
or written.
"""

from __future__ import annotations

import json
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.goal.line_message import (
    ACK_EXECUTED,
    ACK_REJECTED,
    DECISION_GUARD_REASON,
    KIND_INSTRUCTION,
    deliver_line_message,
)
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.state.fleet_state import (
    ACK_TAIL_LIMIT,
    FleetStateConfig,
    FleetStateHTTPServer,
    FleetStateView,
)
from fleet_graph.state.run_artifacts import (
    LINE_MESSAGE_ACKS_FILE,
    RunArtifacts,
    line_message_acks_path,
)

GOAL_REVISION = "evidence-goal-rev-0001"
SUPERVISOR = "wf-evidence-supervisor"


class Clock:
    def __init__(self) -> None:
        self.now = 1_787_000_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class Inbox:
    """The line's channel over the two documented seams: drain + wake probe."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._mail: list[dict[str, Any]] = []

    def deliver(self, message_id: str, payload: dict[str, Any]) -> None:
        self._mail.append(
            {"id": message_id, "payload": payload, "created": self._clock(), "consumed": False}
        )

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return any(entry["created"] > after_epoch for entry in self._mail)

    def drain_then_ack(self, persist: Any) -> tuple[list[dict[str, Any]], list[str]]:
        pending = [entry for entry in self._mail if not entry["consumed"]]
        messages = [{"message_id": entry["id"], "payload": entry["payload"]} for entry in pending]
        for entry in pending:
            entry["consumed"] = True
        persist(messages)
        return messages, ["acked"] * len(messages)


class Sink:
    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox

    def publish(self, alias: str, payload: dict[str, Any]) -> str:
        message_id = f"msg-{uuid.uuid4().hex[:12]}"
        self._inbox.deliver(message_id, payload)
        return message_id


class Coordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], resume: bool = False
    ) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done"}

    @property
    def drained_ids(self) -> list[str]:
        return (
            [m.get("message_id") for m in self.calls[0].get("inbox_messages", [])]
            if self.calls
            else []
        )


class Worker:
    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": prompt[:20],
            "did": ["action"],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class Units:
    def is_active(self, unit_name: str) -> bool:
        return False


class Prober:
    def check(self, seat: str) -> bool:
        return True


class Launcher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class Stack:
    """One scratch fleet: temp run root + roster + bus-less channel per line."""

    def __init__(self, tmp: Path, line_ids: list[str]) -> None:
        self.tmp = tmp
        self.runs = tmp / "runs"
        self.runs.mkdir(parents=True)
        self.clock = Clock()
        self.line_ids = line_ids
        self.aliases = {line: f"evidence-{line}" for line in line_ids}
        self.inboxes = {alias: Inbox(self.clock) for alias in self.aliases.values()}
        self.sinks = {alias: Sink(self.inboxes[alias]) for alias in self.aliases.values()}
        self.launcher = Launcher()
        self.roster = tmp / "ronin-lines.json"
        self.roster.write_text(
            json.dumps(
                {
                    "run_root": str(self.runs),
                    "lines": [
                        {
                            "folder_id": line,
                            "seat": "opencode-evidence",
                            "alias": self.aliases[line],
                            "enabled": True,
                        }
                        for line in line_ids
                    ],
                }
            ),
            encoding="utf-8",
        )

    def deliver(self, line_id: str, text: str, kind: str) -> str:
        result = deliver_line_message(
            line_id,
            text,
            kind,
            SUPERVISOR,
            resolve_alias=lambda line: self.aliases.get(line),
            sink=self.sinks[self.aliases[line_id]],
            identity_check=lambda identity: identity == SUPERVISOR,
            clock=self.clock,
        )
        assert result["delivered"] is True
        return str(result["message_id"])

    def scheduler(self) -> Scheduler:
        return Scheduler(
            SchedulerConfig(
                lines=[
                    LineSpec(
                        folder_id=line,
                        seat="opencode-evidence",
                        alias=self.aliases[line],
                        enabled=True,
                    )
                    for line in self.line_ids
                ],
                run_root=self.runs,
                dd_root=self.tmp / "dd",
                maintenance_stop_path=self.tmp / "maintenance-stop",
            ),
            prober=Prober(),
            launcher=self.launcher,
            units=Units(),
            clock=self.clock,
            sleep=lambda _s: None,
            wake=self,  # the honest WakeSignals: reads the same channels
        )

    # WakeSignals protocol, answered from the scratch channels alone.
    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        inbox = self.inboxes.get(alias)
        return inbox.inbox_message_after(alias, after_epoch) if inbox else False

    def goal_revision(self, folder_id: str) -> str:
        return GOAL_REVISION

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool:
        return False

    def run_line_round(
        self, line_id: str, run_id: str, script: list[dict[str, Any]]
    ) -> Coordinator:
        coordinator = Coordinator(script)
        deps = LineDeps(
            coordinator=coordinator,
            worker=Worker(),
            inbox=self.inboxes[self.aliases[line_id]],
            artifacts=RunArtifacts(
                self.runs / line_id, run_id=run_id, folder_id=line_id, clock=self.clock
            ),
            guards=LineGuards(bounds=LineBounds(max_rounds=8)),
            folder_id=line_id,
            run_id=run_id,
            goal_revision=lambda: GOAL_REVISION,
        )
        compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": f"{line_id}:{run_id}"}, "recursion_limit": 60},
        )
        return coordinator

    def view(self) -> FleetStateView:
        return FleetStateView(
            FleetStateConfig(
                run_root=self.runs,
                lines_config=self.roster,
                dd_root=self.tmp / "dd",
                bridge_state_dir=self.tmp / "bridge",
                bus_url=None,
                enroll_queue_path=None,
                clock=self.clock,
            )
        )

    def face_line(self, line_id: str) -> dict[str, Any]:
        matches = [entry for entry in self.view().lines()["lines"] if entry["folder_id"] == line_id]
        assert len(matches) == 1
        return matches[0]

    def stall(self, line_id: str) -> dict[str, Any]:
        return json.loads(
            (self.runs / ".scheduler" / f"{line_id}.json").read_text(encoding="utf-8")
        )

    def terminal(self, line_id: str) -> dict[str, Any]:
        return json.loads((self.runs / line_id / "terminal.json").read_text(encoding="utf-8"))

    def ledger(self, line_id: str) -> list[dict[str, Any]]:
        path = line_message_acks_path(self.runs, line_id)
        if not path.exists():
            return []
        return [
            json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()
        ]

    def rounds(self, line_id: str) -> list[dict[str, Any]]:
        artifacts = RunArtifacts(self.runs / line_id, run_id="read", folder_id=line_id)
        return artifacts.read_rounds()


def acks_script(instruction_id: str, *, outcome: str = ACK_EXECUTED) -> list[dict[str, Any]]:
    return [
        {
            "verdict": "continue",
            "next_prompt": f"acknowledge {instruction_id}",
            "acks": [
                {"message_id": instruction_id, "outcome": outcome, "reason": "evidence receipt"}
            ],
        },
        {"verdict": "done", "reason": "evidence complete"},
    ]


# --- the ack lands: ledger shape, mirror, and the two negatives --------------


class TestAckLandsInLedgerAndMirror:
    @pytest.fixture()
    def delivered(self, tmp_path: Path) -> tuple[Stack, str, str, str]:
        stack = Stack(tmp_path, ["wf-evidence-ack"])
        instruction = stack.deliver("wf-evidence-ack", "report seat status", KIND_INSTRUCTION)
        info = stack.deliver("wf-evidence-ack", "FYI only", "info")
        approve = stack.deliver("wf-evidence-ack", "APPROVE", KIND_INSTRUCTION)
        stack.scheduler().tick()  # priming schedule tick over the scratch roster
        stack.run_line_round("wf-evidence-ack", "run-g1", acks_script(instruction))
        return stack, instruction, info, approve

    def test_rows_carry_the_contract_shape(self, delivered) -> None:
        stack, instruction, _info, approve = delivered
        rows = {row["message_id"]: row for row in stack.ledger("wf-evidence-ack")}
        assert set(rows) == {instruction, approve}, "info must not appear (阴性①)"
        for row in rows.values():
            assert {"round", "at", "message_id", "outcome", "reason", "kind"} <= set(row)
        assert rows[instruction]["outcome"] == ACK_EXECUTED
        assert rows[instruction]["kind"] == KIND_INSTRUCTION

    def test_info_never_produces_an_ack_row(self, delivered) -> None:
        stack, _instruction, info, _approve = delivered
        assert all(row["message_id"] != info for row in stack.ledger("wf-evidence-ack"))
        assert all(
            row["message_id"] != info
            for row in stack.face_line("wf-evidence-ack")["wake_facts"]["line_message_acks"]
        )

    def test_bare_decision_token_is_guard_rejected_not_executed(self, delivered) -> None:
        stack, _instruction, _info, approve = delivered
        row = next(row for row in stack.ledger("wf-evidence-ack") if row["message_id"] == approve)
        assert row["outcome"] == ACK_REJECTED
        assert row["reason"] == DECISION_GUARD_REASON

    def test_the_verdict_cannot_execute_a_bare_decision_token(self, tmp_path: Path) -> None:
        """The pump guard wins over the verdict: an ``executed`` ack declared for
        a bare APPROVE is overridden to the mechanical rejection."""
        stack = Stack(tmp_path, ["wf-evidence-guard"])
        approve = stack.deliver("wf-evidence-guard", " approve ", KIND_INSTRUCTION)
        stack.run_line_round("wf-evidence-guard", "run-g1", acks_script(approve))
        row = next(row for row in stack.ledger("wf-evidence-guard") if row["message_id"] == approve)
        assert row["outcome"] == ACK_REJECTED
        assert row["reason"] == DECISION_GUARD_REASON

    def test_rounds_mirror_carries_acks_and_unacked(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path, ["wf-evidence-mirror"])
        instruction = stack.deliver("wf-evidence-mirror", "do the thing", KIND_INSTRUCTION)
        stack.run_line_round("wf-evidence-mirror", "run-g1", [{"verdict": "done"}])
        mirror = [row for row in stack.rounds("wf-evidence-mirror") if "line_message_acks" in row]
        assert len(mirror) == 1
        assert mirror[0]["line_message_acks"] == []
        assert mirror[0]["unacked_instructions"] == [instruction], (
            "an instruction the round did not answer is recorded unacked, not guessed"
        )


# --- the state face folds the ledger for mechanical reads --------------------


class TestStateFaceFoldsTheLedger:
    def test_face_reads_latest_first_and_capped(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path, ["wf-evidence-face"])
        artifacts = RunArtifacts(
            stack.runs / "wf-evidence-face", run_id="run-1", folder_id="wf-evidence-face"
        )
        artifacts.record_line_message_acks(
            1,
            [
                {"message_id": f"m-{i}", "kind": "instruction", "outcome": "executed", "reason": ""}
                for i in range(ACK_TAIL_LIMIT + 3)
            ],
        )
        acks = stack.face_line("wf-evidence-face")["wake_facts"]["line_message_acks"]
        assert len(acks) == ACK_TAIL_LIMIT
        assert acks[0]["message_id"] == f"m-{ACK_TAIL_LIMIT + 2}", "latest first"
        assert acks[-1]["message_id"] == "m-3", "the head of the ledger is truncated"

    def test_face_exposes_nothing_without_a_ledger(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path, ["wf-evidence-empty"])
        (stack.runs / "wf-evidence-empty").mkdir(parents=True)
        assert "line_message_acks" not in stack.face_line("wf-evidence-empty")["wake_facts"]

    def test_ledger_path_discovery_agrees_with_the_writers(self, tmp_path: Path) -> None:
        """台账路径发现: writer (RunArtifacts), discovery seam, and reader
        (state face) resolve one and the same path."""
        stack = Stack(tmp_path, ["wf-evidence-path"])
        artifacts = RunArtifacts(
            stack.runs / "wf-evidence-path", run_id="run-1", folder_id="wf-evidence-path"
        )
        assert (
            artifacts.heartbeat_path.parent
            == line_message_acks_path(stack.runs, "wf-evidence-path").parent
        )
        assert line_message_acks_path(stack.runs, "wf-evidence-path").name == LINE_MESSAGE_ACKS_FILE
        artifacts.record_line_message_acks(
            1, [{"message_id": "m-1", "kind": "info", "outcome": "executed", "reason": ""}]
        )
        acks = stack.face_line("wf-evidence-path")["wake_facts"]["line_message_acks"]
        assert [row["message_id"] for row in acks] == ["m-1"]

    def test_the_face_serves_the_folds_over_http(self, tmp_path: Path) -> None:
        """:7494's handler serves the folded acks; probed on an ephemeral port."""
        stack = Stack(tmp_path, ["wf-evidence-http"])
        artifacts = RunArtifacts(
            stack.runs / "wf-evidence-http", run_id="run-1", folder_id="wf-evidence-http"
        )
        artifacts.record_line_message_acks(
            1,
            [{"message_id": "m-http", "kind": "instruction", "outcome": "executed", "reason": ""}],
        )
        config = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=stack.runs,
            lines_config=stack.roster,
            dd_root=tmp_path / "dd",
            bridge_state_dir=tmp_path / "bridge",
            bus_url=None,
            enroll_queue_path=None,
        )
        server = FleetStateHTTPServer(config)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/v1/lines", timeout=5
            ) as response:
                face = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
        line = next(entry for entry in face["lines"] if entry["folder_id"] == "wf-evidence-http")
        assert line["wake_facts"]["line_message_acks"][0]["message_id"] == "m-http"


# --- check 16's invariant: inbox alone never lifts waiting_decision ----------


class ParkEvidence:
    def __init__(self, stack: Stack, line_id: str) -> None:
        self.stack = stack
        self.line_id = line_id

    def snapshot(self) -> dict[str, Any]:
        stall = self.stack.stall(self.line_id)
        terminal = self.stack.terminal(self.line_id)
        face = self.stack.face_line(self.line_id)
        return {
            "terminal": terminal.get("terminal"),
            "waiting_on": terminal.get("waiting_on"),
            "line_state": stall.get("line_state"),
            "parked": face.get("parked"),
            "face_terminal": face.get("terminal"),
            "face_waiting_on": (face.get("wake_facts") or {}).get("waiting_on"),
            "decision_consumed_at": stall.get("dispatched_decision_consumed_at"),
        }


class TestInboxAloneNeverLiftsWaitingDecision:
    @pytest.mark.parametrize(
        ("suffix", "text", "declare_ack"),
        [("plain", "note this mail", True), ("approve", "APPROVE", False)],
    )
    def test_full_receipt_event_leaves_the_park_standing(
        self, tmp_path: Path, suffix: str, text: str, declare_ack: bool
    ) -> None:
        line_id = f"wf-evidence-park-{suffix}"
        stack = Stack(tmp_path, [line_id])
        scheduler = stack.scheduler()
        scheduler.tick()  # priming
        stack.run_line_round(
            line_id,
            "run-g1",
            [
                {
                    "verdict": "blocked",
                    "reason": "evidence: waiting on a human ruling",
                    "waiting_on": "decision",
                }
            ],
        )
        assert scheduler.tick()[0].parked, "the blocked decision wait parks"
        before = ParkEvidence(stack, line_id).snapshot()
        assert before["parked"] is True
        assert before["waiting_on"] == "decision"
        assert before["line_state"] == "waiting_decision"

        message_id = stack.deliver(line_id, text, KIND_INSTRUCTION)

        result = scheduler.tick()  # the receipt tick
        assert result[0].park_event == "woken:inbox", "the mail genuinely arrived"

        stack.run_line_round(
            line_id,
            "run-g2",
            [
                {
                    "verdict": "blocked",
                    "reason": "evidence: mail noted, still no ruling",
                    "waiting_on": "decision",
                    "acks": (
                        [
                            {
                                "message_id": message_id,
                                "outcome": ACK_EXECUTED,
                                "reason": "evidence receipt",
                            }
                        ]
                        if declare_ack
                        else []
                    ),
                }
            ],
        )
        settled = scheduler.tick()
        assert settled[0].parked, "the line re-parks on its re-derived blockage"
        after = ParkEvidence(stack, line_id).snapshot()

        diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        assert diff == {}, (
            f"waiting_decision facts must be unchanged across the receipt event, diff={diff}"
        )
        assert after["parked"] is True
        assert after["waiting_on"] == "decision"
        assert after["line_state"] == "waiting_decision"
        assert after["face_waiting_on"] == "decision"
        # No decision fact ever appeared: the message never acted as a ruling.
        assert before["decision_consumed_at"] is None and after["decision_consumed_at"] is None
        # ...while the receipt face did answer for the message.
        rows = [row for row in stack.ledger(line_id) if row["message_id"] == message_id]
        assert len(rows) == 1
        if declare_ack:
            assert rows[0]["outcome"] == ACK_EXECUTED
        else:
            assert rows[0]["outcome"] == ACK_REJECTED
            assert rows[0]["reason"] == DECISION_GUARD_REASON

"""M4 (wf-8d9737) acceptance: line_message + 回执义务 + stage_models 座位单一来源.

Every criterion from the committed spec, positive and negative, without a
transport layer (红管纪律: 可无传输层单测 / 纯判定 / 不触碰生产账本或生产文件):

- **消息必达必回**: ``line_message`` lands in the line's inbox (the M1
  ``inbox_message`` wake fact), the text reaches the next round's
  ``inbox_messages`` verbatim, and the round's ack lands in progress and on
  the state face; an unacked instruction counts as an idle round (R8 口径).
- **消息不能冒充裁决**: a message payload has no decision field, a parked
  line's parking is untouched by a message, and an APPROVE-shaped
  instruction is mechanically acked ``message_is_not_a_decision``.
- **座位单一来源**: seats are a ``development_create`` parameter, frozen in
  record.json with their source, read back from the record at launch
  (launches.jsonl argv == record.seats), registry-validated, and the
  server-wide ``--stage-model`` override is retired.
- **验收命令冻结**: ``goal_status`` exposes the pin and reports
  ``ACCEPTANCE_DIGEST_MISMATCH`` on carrier drift; the scheduler refuses to
  ignite a drifted carrier and ignites a matching one (no false rejection).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.dd.control_plane import (
    RECORD_FILE,
    ControlPlaneError,
    DdControlPlane,
    load_stage_seat_registry,
)
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.goal.line_message import (
    ALLOWED_ACK_OUTCOMES,
    CODE_KIND_INVALID,
    CODE_LINE_NOT_FOUND,
    CODE_NOT_SUPERVISOR,
    DECISION_GUARD_REASON,
    KIND_INFO,
    KIND_INSTRUCTION,
    LINE_MESSAGE_MARKER,
    WAKE_FACT,
    LineMessageError,
    ack_rows_for_round,
    build_line_message_payload,
    deliver_line_message,
    is_decision_text,
    normalize_message_kind,
    parse_verdict_acks,
)
from fleet_graph.goal.service import build_goal_mcp_server
from fleet_graph.goal_enroll.freeze import (
    ACCEPTANCE_DIGEST_MISMATCH,
    acceptance_block,
    acceptance_block_digest,
)
from fleet_graph.goal_enroll.roster import RealRosterReader
from fleet_graph.goal_enroll.service import GoalEnrollService
from fleet_graph.goal_enroll.validator import GoalEnrollValidator
from fleet_graph.graphs.goal_line import (
    TERMINAL_BLOCKED,
    LineDeps,
    build_goal_line_graph,
)
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.scheduler.daemon import LineSpec
from fleet_graph.scheduler.ignition import LineStatus, decide
from fleet_graph.state.run_artifacts import RunArtifacts

SPEC = """# SPEC: m4

```dd-acceptance
bash -lc 'true'
```
"""


# --- shared fakes (transport-free) ------------------------------------------


class FakeSink:
    """The inbox sink seam: records deliveries, hands back message ids."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, alias: str, payload: dict[str, Any]) -> str:
        self.published.append((alias, payload))
        return f"msg-{len(self.published)}"


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done"}


class FakeWorker:
    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "turn_id": "t-1",
            "outcome": "completed",
            "summary": prompt[:20],
            "did": ["action"],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class FakeInbox:
    """Drains pre-loaded raw bus messages (payloads intact)."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = messages or []
        self.persisted: list[list[dict[str, Any]]] = []

    def drain_then_ack(self, persist: Any) -> tuple[Any, list[str]]:
        self.persisted.append(self.messages)
        persist(self.messages)
        return list(self.messages), ["acked"] * len(self.messages)


class FakeArtifacts:
    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []
        self.ack_rows: list[dict[str, Any]] = []
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def record_line_message_acks(self, round_no: int, acks: list[dict[str, Any]]) -> bool:
        for ack in acks:
            self.ack_rows.append({"round": round_no, **ack})
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminal = kwargs
        return "terminal.json"


def run_line(
    script: list[dict[str, Any]],
    *,
    inbox: FakeInbox | None = None,
    bounds: LineBounds | None = None,
) -> tuple[FakeArtifacts, LineDeps]:
    artifacts = FakeArtifacts()
    deps = LineDeps(
        coordinator=FakeCoordinator(script),
        worker=FakeWorker(),
        inbox=inbox or FakeInbox(),
        artifacts=artifacts,
        guards=LineGuards(bounds=bounds or LineBounds()),
        folder_id="wf-m4test",
        worker_report_retry_limit=0,
    )
    compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
    compiled.invoke(
        {"round_no": 1}, config={"configurable": {"thread_id": "t1"}, "recursion_limit": 100}
    )
    return artifacts, deps


def line_message_delivery(
    message_id: str,
    text: str,
    kind: str,
    *,
    sent_by: str = "supervisor",
) -> dict[str, Any]:
    """A raw bus message as the inbox drain would hand it over."""
    return {
        "message_id": message_id,
        "payload": build_line_message_payload(
            line="wf-m4test", text=text, kind=kind, sent_by=sent_by, clock=lambda: 0.0
        ),
    }


# --- 消息必达: the line_message core -----------------------------------------


class TestLineMessageCore:
    def test_kind_is_closed(self) -> None:
        with pytest.raises(LineMessageError) as excinfo:
            normalize_message_kind("decision")
        assert excinfo.value.code == CODE_KIND_INVALID
        assert normalize_message_kind(KIND_INSTRUCTION) == KIND_INSTRUCTION
        assert normalize_message_kind(KIND_INFO) == KIND_INFO

    def test_a_non_supervisor_is_refused_before_anything_is_resolved(self) -> None:
        sink = FakeSink()
        lookups: list[str] = []

        def resolve_alias(line: str) -> str | None:
            lookups.append(line)
            return "ronin-x"

        with pytest.raises(LineMessageError) as excinfo:
            deliver_line_message(
                "wf-m4test",
                "hello",
                KIND_INSTRUCTION,
                "ronin-x",
                resolve_alias=resolve_alias,
                sink=sink,
                identity_check=lambda identity: False,
            )
        assert excinfo.value.code == CODE_NOT_SUPERVISOR
        assert sink.published == [], "nothing lands in the inbox"
        assert lookups == [], "not even a roster lookup happens"

    def test_an_unknown_line_is_refused(self) -> None:
        with pytest.raises(LineMessageError) as excinfo:
            deliver_line_message(
                "wf-ghost",
                "hello",
                KIND_INSTRUCTION,
                "supervisor",
                resolve_alias=lambda line: None,
                sink=FakeSink(),
                identity_check=lambda identity: True,
            )
        assert excinfo.value.code == CODE_LINE_NOT_FOUND

    def test_empty_text_is_refused(self) -> None:
        with pytest.raises(LineMessageError) as excinfo:
            build_line_message_payload(line="wf-m4test", text="   ", kind=KIND_INFO, sent_by="s")
        assert excinfo.value.code == "LINE_MESSAGE_TEXT_REQUIRED"

    def test_the_payload_has_no_decision_field(self) -> None:
        """结构上禁止: the payload builder is the only producer and its field
        set is closed -- there is no field a verdict could ride in."""
        payload = build_line_message_payload(
            line="wf-m4test",
            text="APPROVE",
            kind=KIND_INSTRUCTION,
            sent_by="supervisor",
        )
        assert set(payload) == {
            "body",
            "from_alias",
            "from_agent_id",
            "thread_id",
            "depth",
            "sent_at",
            LINE_MESSAGE_MARKER,
        }
        assert "decision" not in payload
        assert "APPROVE" not in payload[LINE_MESSAGE_MARKER]
        assert payload["body"] == "APPROVE", "the text travels as data, not semantics"

    def test_delivery_lands_in_the_lines_own_inbox(self) -> None:
        sink = FakeSink()
        result = deliver_line_message(
            "wf-m4test",
            "switch the implement seat",
            KIND_INSTRUCTION,
            "supervisor",
            resolve_alias=lambda line: "ronin-x",
            sink=sink,
            identity_check=lambda identity: True,
            clock=lambda: 0.0,
        )
        assert result["delivered"] is True
        assert result["wake_fact"] == WAKE_FACT == "inbox_message"
        alias, payload = sink.published[0]
        assert alias == "ronin-x"
        assert payload[LINE_MESSAGE_MARKER]["kind"] == KIND_INSTRUCTION
        assert result["message_id"] == "msg-1"

    def test_decision_tokens_are_recognised_mechanically(self) -> None:
        assert is_decision_text("APPROVE")
        assert is_decision_text(" approve ")
        assert is_decision_text("REJECT")
        assert not is_decision_text("please approve ticket 3")
        assert not is_decision_text("ship it")


# --- 消息必达: the round carries the text, the ack lands ---------------------


class TestMessageDeliveryIntoTheRound:
    def test_the_next_round_input_carries_the_message_verbatim(self) -> None:
        inbox = FakeInbox(
            [line_message_delivery("msg-9", "switch the implement seat", KIND_INSTRUCTION)]
        )
        _, deps = run_line([{"verdict": "done"}], inbox=inbox)
        carried = deps.coordinator.calls[0]["inbox_messages"]
        assert [m["message_id"] for m in carried] == ["msg-9"]
        assert carried[0]["payload"]["body"] == "switch the implement seat"

    def test_a_verdict_ack_is_recorded_in_progress_and_the_ledger(self) -> None:
        inbox = FakeInbox(
            [line_message_delivery("msg-1", "switch the implement seat", KIND_INSTRUCTION)]
        )
        script = [
            {
                "verdict": "done",
                "acks": [{"message_id": "msg-1", "outcome": "executed", "reason": ""}],
            }
        ]
        artifacts, _ = run_line(script, inbox=inbox)
        ack_rows = [r for r in artifacts.rounds if "line_message_acks" in r]
        assert ack_rows and ack_rows[0]["round"] == 1
        assert ack_rows[0]["line_message_acks"][0]["outcome"] == "executed"
        assert artifacts.ack_rows[0]["message_id"] == "msg-1"

    def test_an_unacked_instruction_counts_as_an_idle_round(self) -> None:
        """阴性判据的机械面: kind=instruction 未执行也未拒绝 → 计入 R8 空转."""
        inbox = FakeInbox(
            [line_message_delivery("msg-2", "switch the implement seat", KIND_INSTRUCTION)]
        )
        artifacts, deps = run_line([{"verdict": "done"}], inbox=inbox)
        unacked = [r for r in artifacts.rounds if r.get("unacked_instructions")]
        assert unacked and unacked[0]["unacked_instructions"] == ["msg-2"]
        assert deps.guards.noop_streak == 1, "the round is counted idle"

    def test_an_info_message_carries_no_ack_obligation(self) -> None:
        inbox = FakeInbox([line_message_delivery("msg-3", "fyi: gateway maintenance", KIND_INFO)])
        artifacts, deps = run_line([{"verdict": "done"}], inbox=inbox)
        assert deps.guards.noop_streak == 0
        assert not [r for r in artifacts.rounds if "line_message_acks" in r]

    def test_acks_are_read_strictly_from_the_verdict(self) -> None:
        acks = parse_verdict_acks(
            {
                "acks": [
                    {"message_id": "a", "outcome": "executed"},
                    {"message_id": "b", "outcome": "rejected", "reason": "obsolete"},
                    {"message_id": "c", "outcome": "rejected"},  # no reason
                    {"message_id": "d", "outcome": "approved"},  # not in the vocabulary
                    "garbage",
                ]
            }
        )
        assert set(acks) == {"a", "b"}
        assert acks["b"]["reason"] == "obsolete"
        assert set(ALLOWED_ACK_OUTCOMES) == {"executed", "rejected"}


# --- 消息不能冒充裁决 --------------------------------------------------------


class TestMessageIsNotADecision:
    def test_an_approve_text_instruction_is_acked_message_is_not_a_decision(self) -> None:
        """阴性判据的红线: 驻停不解除，回执写明「消息不是裁决」."""
        inbox = FakeInbox(
            [line_message_delivery("msg-4", "APPROVE", KIND_INSTRUCTION)],
        )
        script = [
            # The verdict even *tries* to claim the message as executed.
            {
                "verdict": "blocked",
                "reason": "still need a human ruling",
                "acks": [{"message_id": "msg-4", "outcome": "executed", "reason": ""}],
            }
        ]
        artifacts, _ = run_line(script, inbox=inbox)
        ack_rows = [r for r in artifacts.rounds if "line_message_acks" in r]
        ack = ack_rows[0]["line_message_acks"][0]
        assert ack["outcome"] == "rejected", "the pump never lets a message execute"
        assert ack["reason"] == DECISION_GUARD_REASON == "message_is_not_a_decision"
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED

    def test_a_message_never_touches_a_parked_lines_stall_state(self, tmp_path: Path) -> None:
        """The parking lives in the scheduler's stall-state file; only the M2
        decision path lifts it. A delivered message wakes the line (the inbox
        is wake fact 1) while every parked field stays byte-identical."""
        stall = tmp_path / "wf-parked.json"
        parked = {
            "parked_run_id": "run-1",
            "parked_at": "2026-09-03T08:00:00Z",
            "parked_goal_revision": "sha256:rev",
            "waiting_on": "decision",
        }
        stall.write_text(json.dumps(parked), encoding="utf-8")
        deliver_line_message(
            "wf-parked",
            "APPROVE",
            KIND_INSTRUCTION,
            "supervisor",
            resolve_alias=lambda line: "ronin-x",
            sink=FakeSink(),
            identity_check=lambda identity: True,
            clock=lambda: 0.0,
        )
        assert json.loads(stall.read_text(encoding="utf-8")) == parked, (
            "the park is not lifted by a message"
        )

    def test_ack_rows_for_round_prioritises_the_guard_over_the_verdict(self) -> None:
        deliveries = [("msg-4", line_message_delivery("m", "APPROVE", KIND_INSTRUCTION)["payload"])]
        verdict_acks = parse_verdict_acks(
            {"acks": [{"message_id": "msg-4", "outcome": "executed", "reason": ""}]}
        )
        acks, unacked = ack_rows_for_round(deliveries, verdict_acks)
        assert acks[0]["reason"] == DECISION_GUARD_REASON
        assert unacked == []


# --- stage_models 座位单一来源 ------------------------------------------------


class RecordingLauncher:
    dry_run = False

    def __init__(self) -> None:
        self.specs: list[Any] = []

    def launch(self, spec: Any) -> Any:
        from fleet_graph.scheduler.launcher import LaunchResult

        self.specs.append(spec)
        return LaunchResult(spec.unit_name, True, "recorded")


def scratch_repo(tmp_path: Path) -> Path:
    from conftest import git

    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "greet.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(repo, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    return repo


def make_plane(tmp_path: Path, launcher: RecordingLauncher) -> DdControlPlane:
    binding = tmp_path / "plugin-binding.json"
    binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        launcher=launcher,
        unit_probe=lambda unit: False,
        board_factory=lambda: None,
    )


def seat_pairs_from_argv(argv: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for index, part in enumerate(argv):
        if part == "--stage-model":
            stage, seat = argv[index + 1].split("=", 1)
            pairs[stage] = seat
    return pairs


class TestStageSeatSingleSource:
    def test_seats_freeze_into_the_record_with_their_source(self, tmp_path: Path) -> None:
        plane = make_plane(tmp_path, RecordingLauncher())
        result = plane.create(
            str(scratch_repo(tmp_path)),
            spec_text=SPEC,
            stage_models={"implement": "glm-5.3-flash"},
        )
        assert result["seats"]["implement"] == "glm-5.3-flash"
        assert result["seats_source"]["implement"] == "line-explicit"
        record = json.loads(
            (plane.root / result["development_id"] / RECORD_FILE).read_text(encoding="utf-8")
        )
        assert record["seats"]["implement"] == "glm-5.3-flash"
        assert record["seats_source"]["implement"] == "line-explicit"
        assert record["seats_source"]["final_review"] == "registry-default"

    def test_the_consumer_dispatch_shape_is_accepted(self, tmp_path: Path) -> None:
        """交付 5: the line's own dispatch shape validates against the registry."""
        plane = make_plane(tmp_path, RecordingLauncher())
        result = plane.create(
            str(scratch_repo(tmp_path)),
            spec_text=SPEC,
            stage_models={
                "implement": "glm-5.3-flash",
                "continuous_review": "glm-5.3",
                "final_review": "glm-5.3",
            },
        )
        assert result["seats"] == {
            "implement": "glm-5.3-flash",
            "continuous_review": "glm-5.3",
            "final_review": "glm-5.3",
        }
        assert set(result["seats_source"].values()) == {"line-explicit"}

    def test_launches_jsonl_measured_argv_matches_record_seats(self, tmp_path: Path) -> None:
        """阳性「座位单一来源」: a unit dispatched with stage_models has
        launches.jsonl 实测座位 == record.seats, uncovered by any global."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(
            str(scratch_repo(tmp_path)),
            spec_text=SPEC,
            stage_models={"implement": "glm-5.3-flash"},
        )["development_id"]
        plane.start(dev)

        record = json.loads((plane.root / dev / RECORD_FILE).read_text(encoding="utf-8"))
        launches = (plane.root / dev / "launches.jsonl").read_text(encoding="utf-8")
        measured_argv = json.loads(launches.strip().splitlines()[-1])["argv"]
        assert seat_pairs_from_argv(measured_argv) == record["seats"]
        assert seat_pairs_from_argv(launcher.specs[0].argv()) == record["seats"]

    def test_a_seat_outside_the_registry_refuses_and_no_unit_is_created(
        self, tmp_path: Path
    ) -> None:
        """阴性（座位越权）: a structured refusal, 单不建立."""
        plane = make_plane(tmp_path, RecordingLauncher())
        with pytest.raises(ControlPlaneError) as excinfo:
            plane.create(
                str(scratch_repo(tmp_path)),
                spec_text=SPEC,
                stage_models={"implement": "gpt-9"},
            )
        assert excinfo.value.code == "STAGE_SEAT_NOT_ALLOWED"
        root = tmp_path / "dd"
        assert not root.exists() or not any(root.iterdir())

    def test_a_seat_for_a_non_agent_stage_refuses(self, tmp_path: Path) -> None:
        plane = make_plane(tmp_path, RecordingLauncher())
        llm_stages = {stage for stage, spec in Lifecycle.load().stages.items() if spec.is_llm}
        with pytest.raises(ControlPlaneError) as excinfo:
            plane.create(
                str(scratch_repo(tmp_path)),
                spec_text=SPEC,
                stage_models={"human_gate": "glm-5.3"},
            )
        assert excinfo.value.code == "STAGE_SEAT_STAGE_UNKNOWN"
        assert llm_stages == {"implement", "continuous_review", "final_review"}

    def test_the_registry_projection_is_the_committed_single_source(self) -> None:
        registry = load_stage_seat_registry()
        assert registry["default_seats"] == {
            "implement": "glm-5.3-flash",
            "continuous_review": "glm-5.3",
            "final_review": "claude-opus-5",
        }
        assert {"glm-5.3-flash", "glm-5.3", "claude-opus-5"} <= set(registry["allowed_seats"])

    def test_a_missing_registry_projection_refuses_closed(self, tmp_path: Path) -> None:
        from fleet_graph.dd.control_plane import load_stage_seat_registry as load

        with pytest.raises(ControlPlaneError) as excinfo:
            load(tmp_path / "absent.json")
        assert excinfo.value.code == "STAGE_SEAT_REGISTRY_UNREADABLE"

    def test_the_dd_serve_cli_refuses_the_retired_override(self) -> None:
        """CLI --stage-model 去功能化: keep parsing, refuse with a structured
        exit -- the server never starts with a seat policy attached."""
        from fleet_graph.cli import _dd_serve, build_parser

        args = build_parser().parse_args(
            ["dd", "serve", "--port", "0", "--stage-model", "continuous_review=glm-5.3"]
        )
        assert _dd_serve(args) == 2
        assert args.stage_model == ["continuous_review=glm-5.3"]

    def test_the_dd_serve_service_signature_has_no_seat_policy(self) -> None:
        import inspect

        from fleet_graph.dd.service import serve

        assert "stage_models" not in inspect.signature(serve).parameters


# --- 验收命令冻结面 ----------------------------------------------------------

GOLDEN_ORDER = "1. Do the thing.\n2. Stop at the boundary.\n"
ACCEPTANCE_MD = "# goal\n\nDo the thing.\n\n```dd-acceptance\nbash -lc 'true'\n```\n"
CHANGED_ACCEPTANCE_MD = "# goal\n\nDo the thing.\n\n```dd-acceptance\nbash -lc 'false'\n```\n"


class FakeFolderSource:
    """A GoalFolderSource over ``{folder_id: {filename: content}}``."""

    def __init__(self, folders: dict[str, dict[str, str]]) -> None:
        self._folders = folders

    def exists(self, folder_id: str) -> bool:
        return folder_id in self._folders

    def read(self, folder_id: str, filename: str) -> str | None:
        return self._folders.get(folder_id, {}).get(filename)


def enrolling_validator() -> GoalEnrollValidator:
    return GoalEnrollValidator(
        FakeFolderSource(
            {
                "wf-frozen": {"goal.md": ACCEPTANCE_MD, "golden-order.md": GOLDEN_ORDER},
            }
        ),
        probe=lambda argv: {"argv": argv, "exit_code": 0, "started": True},
        alias_token_check=lambda alias: True,
        alias_conflict_check=lambda alias: None,
    )


def roster_with(tmp_path: Path, entries: list[dict[str, Any]]) -> RealRosterReader:
    path = tmp_path / "ronin-lines.json"
    path.write_text(json.dumps({"lines": entries}), encoding="utf-8")
    return RealRosterReader(path)


FROZEN_ROSTER_ENTRY = {
    "folder_id": "wf-frozen",
    "seat": "opencode-glm53",
    "alias": "ronin-x",
    "enabled": True,
    "acceptance_argv": [["bash", "-lc", "true"]],
    "acceptance_digest": acceptance_block_digest(ACCEPTANCE_MD),
}


class TestAcceptanceCommandFreeze:
    def _service(
        self,
        *,
        roster: RealRosterReader,
        carrier_digest: Any,
    ) -> GoalEnrollService:
        return GoalEnrollService(
            enrolling_validator(),
            roster=roster,
            goal_carrier_digest=carrier_digest,
        )

    def test_enlistment_pins_the_carrier_digest(self) -> None:
        """The validator pins the digest of the exact dd-acceptance block."""
        facts = enrolling_validator().validate("wf-frozen", alias="ronin-x")
        assert facts["acceptance_digest"] == acceptance_block_digest(ACCEPTANCE_MD)
        assert facts["acceptance_digest"].startswith("sha256:")

    def test_the_roster_reader_passes_the_pin_through(self, tmp_path: Path) -> None:
        roster = roster_with(tmp_path, [FROZEN_ROSTER_ENTRY])
        entry = roster.get("wf-frozen")
        assert entry["acceptance_digest"] == acceptance_block_digest(ACCEPTANCE_MD)
        assert entry["acceptance_argv"] == [["bash", "-lc", "true"]]

    def test_goal_status_exposes_the_freeze_fields_per_enlisted_goal(self, tmp_path: Path) -> None:
        service = self._service(
            roster=roster_with(tmp_path, [FROZEN_ROSTER_ENTRY]),
            carrier_digest=lambda folder: acceptance_block_digest(ACCEPTANCE_MD),
        )
        status = service.status("wf-frozen")
        assert status["acceptance"] == [["bash", "-lc", "true"]]
        assert status["acceptance_digest"] == acceptance_block_digest(ACCEPTANCE_MD)
        assert status["acceptance_digest_current"] == acceptance_block_digest(ACCEPTANCE_MD)
        assert status["acceptance_digest_mismatch"] is False
        assert "code" not in status

    def test_goal_status_reports_the_structured_code_on_drift(self, tmp_path: Path) -> None:
        """阳性「验收命令冻结」: change the carrier's acceptance -> the code."""
        service = self._service(
            roster=roster_with(tmp_path, [FROZEN_ROSTER_ENTRY]),
            carrier_digest=lambda folder: acceptance_block_digest(CHANGED_ACCEPTANCE_MD),
        )
        status = service.status("wf-frozen")
        assert status["acceptance_digest_mismatch"] is True
        assert status["code"] == ACCEPTANCE_DIGEST_MISMATCH

    def test_an_unpinnable_carrier_is_never_reported_as_a_mismatch(self, tmp_path: Path) -> None:
        service = self._service(
            roster=roster_with(tmp_path, []),
            carrier_digest=lambda folder: None,
        )
        status = service.status("wf-frozen")
        assert status["acceptance_digest"] is None
        assert status["acceptance_digest_mismatch"] is False

    def test_the_scheduler_refuses_to_ignite_a_drifted_carrier(self) -> None:
        """阳性: the line does not ignite, with the spec's structured code."""
        decision = decide(
            LineStatus(folder_id="wf-frozen", seat="opencode-glm53"),
            now=1000.0,
            enabled=True,
            maintenance_stop=False,
            gateway_healthy=True,
            unproductive_recent=0,
            zero_progress_streak=0,
            acceptance_digest_pinned=acceptance_block_digest(ACCEPTANCE_MD),
            acceptance_digest_current=acceptance_block_digest(CHANGED_ACCEPTANCE_MD),
        )
        assert decision.refused
        assert decision.refusal == ACCEPTANCE_DIGEST_MISMATCH
        assert decision.refusal.value == "ACCEPTANCE_DIGEST_MISMATCH"

    def test_a_matching_carrier_ignites_no_false_rejection(self) -> None:
        """阴性（误拒）: any reject-even-when-consistent implementation is red."""
        decision = decide(
            LineStatus(folder_id="wf-frozen", seat="opencode-glm53"),
            now=1000.0,
            enabled=True,
            maintenance_stop=False,
            gateway_healthy=True,
            unproductive_recent=0,
            zero_progress_streak=0,
            acceptance_digest_pinned=acceptance_block_digest(ACCEPTANCE_MD),
            acceptance_digest_current=acceptance_block_digest(ACCEPTANCE_MD),
        )
        assert decision.ignite

    def test_an_unpinned_line_fails_open(self) -> None:
        """Pre-M4 lines carry no pin; a missing pin can never mismatch."""
        decision = decide(
            LineStatus(folder_id="wf-old", seat="opencode-glm53"),
            now=1000.0,
            enabled=True,
            maintenance_stop=False,
            gateway_healthy=True,
            unproductive_recent=0,
            zero_progress_streak=0,
            acceptance_digest_pinned=None,
            acceptance_digest_current=None,
        )
        assert decision.ignite

    def test_the_line_spec_carries_the_roster_pin(self) -> None:
        """The roster-PR field flows through the LineSpec loader unchanged."""
        spec = LineSpec(folder_id="wf-frozen", seat="s", acceptance_digest="sha256:ab" + "0" * 60)
        assert spec.acceptance_digest == "sha256:ab" + "0" * 60

    def test_the_block_digest_moves_with_any_edit(self) -> None:
        assert acceptance_block(ACCEPTANCE_MD).startswith("```dd-acceptance")
        assert acceptance_block_digest(ACCEPTANCE_MD) != acceptance_block_digest(
            CHANGED_ACCEPTANCE_MD
        )


# --- 状态面: the ack ledger is observable ------------------------------------


class TestAckLedgerOnTheStateFace:
    def test_acks_reach_the_read_models_wake_facts(self, tmp_path: Path) -> None:
        from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

        lines = tmp_path / "lines.json"
        lines.write_text(
            json.dumps({"lines": [{"folder_id": "wf-m4test", "seat": "s", "enabled": True}]}),
            encoding="utf-8",
        )
        run_root = tmp_path / "runs"
        line_root = run_root / "wf-m4test"
        line_root.mkdir(parents=True)
        artifacts = RunArtifacts(line_root, run_id="run-1", folder_id="wf-m4test")
        artifacts.record_line_message_acks(
            1,
            [
                {
                    "message_id": "msg-1",
                    "kind": "instruction",
                    "outcome": "executed",
                    "reason": "",
                }
            ],
        )
        view = FleetStateView(
            FleetStateConfig(run_root=run_root, lines_config=lines, clock=lambda: 0.0)
        )
        line = view.lines()["lines"][0]
        acks = line["wake_facts"]["line_message_acks"]
        assert acks[0]["message_id"] == "msg-1"
        assert acks[0]["outcome"] == "executed"

    def test_a_line_without_acks_has_no_ack_field(self, tmp_path: Path) -> None:
        from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

        lines = tmp_path / "lines.json"
        lines.write_text(
            json.dumps({"lines": [{"folder_id": "wf-m4test", "seat": "s", "enabled": True}]}),
            encoding="utf-8",
        )
        run_root = tmp_path / "runs"
        run_root.mkdir()
        view = FleetStateView(
            FleetStateConfig(run_root=run_root, lines_config=lines, clock=lambda: 0.0)
        )
        assert "line_message_acks" not in view.lines()["lines"][0]["wake_facts"]


# --- the goal MCP surface ----------------------------------------------------


class TestLineMessageSurface:
    def _tools(self, server: Any) -> set[str]:
        return {tool.name for tool in asyncio.run(server.list_tools())}

    def test_line_message_is_registered_on_the_goal_face_only(self) -> None:
        server = build_goal_mcp_server()
        assert "line_message" in self._tools(server)

    def test_a_non_supervisor_refusal_reaches_the_client_machine_readably(self) -> None:
        server = build_goal_mcp_server(
            line_message_sink=FakeSink(),
            supervisor_identity_check=lambda identity: False,
            line_alias_resolver=lambda line: "ronin-x",
        )
        with pytest.raises(Exception) as excinfo:
            asyncio.run(
                server.call_tool(
                    "line_message",
                    {
                        "line": "wf-m4test",
                        "text": "hello",
                        "kind": "instruction",
                        "sent_by": "ronin-x",
                    },
                )
            )
        message = str(excinfo.value)
        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == CODE_NOT_SUPERVISOR
        assert payload["tool"] == "line_message"

    def test_an_unbound_sink_refuses_explicitly(self) -> None:
        server = build_goal_mcp_server(
            supervisor_identity_check=lambda identity: True,
        )
        with pytest.raises(Exception) as excinfo:
            asyncio.run(
                server.call_tool(
                    "line_message",
                    {
                        "line": "wf-m4test",
                        "text": "hello",
                        "kind": "instruction",
                        "sent_by": "supervisor",
                    },
                )
            )
        payload = json.loads(str(excinfo.value)[str(excinfo.value).index("{") :])
        assert payload["code"] == "LINE_MESSAGE_SINK_UNBOUND"

    def test_the_surface_delivers_through_the_injected_seams(self) -> None:
        sink = FakeSink()
        server = build_goal_mcp_server(
            line_message_sink=sink,
            supervisor_identity_check=lambda identity: identity == "supervisor",
            line_alias_resolver=lambda line: "ronin-x",
            clock=lambda: 0.0,
        )
        result = asyncio.run(
            server.call_tool(
                "line_message",
                {
                    "line": "wf-m4test",
                    "text": "switch the implement seat",
                    "kind": "instruction",
                    "sent_by": "supervisor",
                },
            )
        ).structured_content
        assert result["delivered"] is True
        assert result["wake_fact"] == "inbox_message"
        assert sink.published[0][0] == "ronin-x"

    def test_an_invalid_kind_refuses_before_delivery(self) -> None:
        sink = FakeSink()
        server = build_goal_mcp_server(
            line_message_sink=sink,
            supervisor_identity_check=lambda identity: True,
            line_alias_resolver=lambda line: "ronin-x",
        )
        with pytest.raises(Exception) as excinfo:
            asyncio.run(
                server.call_tool(
                    "line_message",
                    {
                        "line": "wf-m4test",
                        "text": "hello",
                        "kind": "verdict",
                        "sent_by": "supervisor",
                    },
                )
            )
        payload = json.loads(str(excinfo.value)[str(excinfo.value).index("{") :])
        assert payload["code"] == CODE_KIND_INVALID
        assert sink.published == []

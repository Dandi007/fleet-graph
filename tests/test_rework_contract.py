"""The wf-8d9737 rework contracts, pinned end to end.

Two independent developments failed the same way (dev-fg-79d528db4375 g2
re-seal replay; dev-fg-eee4da1e3649 g2 misdirected dispatch): after a
human_gate REJECT, the engine either replayed the sealed receipt chain into a
"next generation" that dispatched no implementer at all, or dispatched one
whose prompt never carried the gate's verdict. Common root: the gate REJECT
rationale was not an authoritative, mandatory input of the rework generation.

**Contract A -- the rationale injection is provable.** A generation started
after a GATE_REJECTED terminal carries the rejecting verdict -- decision
message id, ``decision: REJECT``, rationale -- in its implement prompt, seeded
by the control plane from the gate decision record at ``gate_decision_path``
and injected at the engine-side prompt builder under the greppable
``gate-reject-rationale:`` anchor.

**Contract B -- re-seal replay is identified and refused.** A "new
generation" the engine cannot assemble a new implement dispatch for (a sealed
prefix replay: no new prompt, no new agent run) is refused with
``REWORK_REPLAY_REFUSED`` instead of being launched.

The pre-enumerated mutation targets this file kills:

- **MUT-R1** (rationale injection missing): emptying the injection point
  turns ``test_gate_reject_then_start_dispatches_a_real_implement_with_the_rationale``
  red -- the prompt loses the anchor and the verdict message id.
- **MUT-R2** (replay refusal missing): making replay always win turns
  ``test_a_gate_rework_generation_cannot_replay_the_sealed_prefix`` red -- the
  fake generation is launched instead of refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.bus.board import Decision
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    CODE_REWORK_REPLAY_REFUSED,
    GATE_REJECT_FILE,
    RECORD_FILE,
    RESULT_FILE,
    ControlPlaneError,
    DdControlPlane,
)
from fleet_graph.dd.prompt import GATE_REJECT_ANCHOR
from fleet_graph.dd.upstream_constants import ATTEMPT_CONTEXT_CONTRACT_VERSION
from fleet_graph.graphs.dd_runner import (
    REWORK_REPLAY_REFUSED,
    DevelopmentConfig,
    ReworkReplayRefused,
    run_pipeline,
)
from fleet_graph.graphs.dd_scripts import GATE_PATH
from test_dd_runner import AgentRunStub, FakeBoard, RealCommitSealer

SPEC = "# SPEC: greet\n\nMake greet() personal.\n\n```dd-acceptance\ntrue\n```\n"

REJECT_MESSAGE_ID = "msg_01M1M8VKWB03SJ9JKBJJSMRNVW"
REJECT_RATIONALE = "返工面：dd_runner LineRebase 接线覆盖用例缺失，MUT-1 删发射零红"


# --- fixtures ---------------------------------------------------------------


class RecordingLauncher:
    """Stands in for TransientLauncher; records the specs it was handed."""

    dry_run = False

    def __init__(self) -> None:
        self.specs: list[Any] = []

    def launch(self, spec: Any) -> Any:
        from fleet_graph.scheduler.launcher import LaunchResult

        self.specs.append(spec)
        return LaunchResult(spec.unit_name, True, "recorded")


class AgentRunDirs(AgentRunStub):
    """AgentRunStub, plus the session directory the real launcher creates."""

    state_root: str = "."

    def launch(self, spec: Any, run_id: str) -> Any:
        ticket = super().launch(spec, run_id)
        session = Path(self.state_root) / run_id
        session.mkdir(parents=True, exist_ok=True)
        return ticket


def approve_stub(**kwargs: Any) -> AgentRunDirs:
    """A run stub whose reviews approve, so a walk reaches the gate."""
    return AgentRunDirs({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}, **kwargs)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A dedicated worktree with a local bare origin -- the §24 shape."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(repo, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    return repo


def make_plane(tmp_path: Path, launcher: RecordingLauncher) -> DdControlPlane:
    binding = tmp_path / "plugin-binding.json"
    if not binding.exists():
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher,
        unit_probe=lambda unit: False,
        board_factory=lambda: None,
        clock=lambda: 1_700_000_000.0,
    )


def stub_plugin_seals(repo: Path, monkeypatch: pytest.MonkeyPatch) -> RealCommitSealer:
    """Stand-in plugin sealers: real commits, persisted receipt bytes, and
    prompt resources from a bundle stand-in (the keystone shape)."""
    from fleet_graph.dd.prompt import IMPLEMENT_PERSONA, IMPLEMENT_TEMPLATE
    from fleet_graph.dd.vendor import plugin_adapter
    from fleet_graph.graphs.dd_pipeline import StageOutcome

    sealer = RealCommitSealer(repo)

    def write_receipt(request: dict[str, Any], name: str, receipt: dict[str, Any]) -> None:
        path = Path(request["state_root"]) / "receipts" / request["dispatch"]["attempt_id"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))

    def implement_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        receipt = sealer.seal("implement", StageOutcome())
        write_receipt(request, "implement-receipt.json", receipt)
        return receipt

    def review_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        stage = request["dispatch"]["stage"]
        receipt = {
            **sealer.seal(stage, StageOutcome()),
            "verdict": request["review_result"]["verdict"],
        }
        if stage == "continuous_review":
            write_receipt(request, "continuous-review-receipt.json", receipt)
        return receipt

    class Resource:
        def __init__(self, path: str, text: str) -> None:
            self.relative_path = path
            self.content = text.encode("utf-8")
            self.digest = "sha256:" + "0" * 64

    monkeypatch.setattr(plugin_adapter, "invoke_implement_materializer", implement_seal)
    monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", review_seal)
    monkeypatch.setattr(
        plugin_adapter,
        "load_implement_stage_resources",
        lambda binding, **kwargs: (
            Resource(IMPLEMENT_PERSONA, "You are the Implementer."),
            Resource(
                IMPLEMENT_TEMPLATE,
                "input_commit: {{input_commit}}\nacceptance: {{acceptance_commands}}\n",
            ),
        ),
    )
    return sealer


def execute_generation(
    plane: DdControlPlane,
    repo: Path,
    dev: str,
    *,
    generation: int,
    board: FakeBoard,
    launcher: AgentRunStub,
    gate_reject: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the transient unit does, in-process: `dd run` with the record's
    derived context, this generation's identity, and the frozen rework
    mandate exactly as `--gate-reject-file` would hand it over."""
    record = json.loads((plane.root / dev / RECORD_FILE).read_text())
    run_root = plane.root / dev if generation <= 1 else plane.root / dev / f"g{generation}"
    config = DevelopmentConfig(
        development_id=dev,
        workspace_path=repo,
        state_root=run_root / "state",
        run_root=run_root,
        remote_url=record["remote_url"],
        remote_ref=record["remote_ref"],
        target_base_commit=record["target_base_commit"],
        root_handoff_digest=record["root_handoff_digest"],
        plugin_binding=object(),
        head_commit=git(repo, "rev-parse", "HEAD"),
        generation=generation,
        checkpoint_path=str(plane.root / dev / CHECKPOINT_FILE),
        run_config={
            "acceptance_commands": [list(c) for c in record["acceptance_commands"]],
            "setup_commands": [list(c) for c in record.get("setup_commands") or []],
            "acceptance_env": dict(record.get("acceptance_env") or {}),
        },
        gate_reject=gate_reject or {},
    )
    launcher.state_root = str(run_root / "agent-runs")
    return run_pipeline(
        config,
        board=board,
        gate_card_entity_id="card-1",
        launcher=launcher,
    )


def frozen_mandate(plane: DdControlPlane, dev: str, generation: int) -> dict[str, Any]:
    path = plane.root / dev / f"g{generation}" / GATE_REJECT_FILE
    return json.loads(path.read_text())


def launch_count(plane: DdControlPlane, dev: str) -> int:
    path = plane.root / dev / "launches.jsonl"
    if not path.is_file():
        return 0
    return len([line for line in path.read_text().splitlines() if line])


# --- contract A: the rationale injection is provable ------------------------


class TestReworkContractA:
    def test_gate_reject_then_start_dispatches_a_real_implement_with_the_rationale(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REJECT -> start -> the new generation's implement prompt lands on
        disk carrying the `gate-reject-rationale:` anchor and the decision
        message id; a new agent-run directory is created for it; the launch
        and the generation's events record the real rework segment."""
        stub_plugin_seals(scratch, monkeypatch)
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        # g1 runs for real and dies at the gate on a REJECT.
        reject_board = FakeBoard()
        reject_board.decision = Decision(
            message_id=REJECT_MESSAGE_ID,
            decision="REJECT",
            decided_by="青林",
            question="",
            rationale=REJECT_RATIONALE,
            card_entity_id="card-1",
            raw={},
        )
        first = execute_generation(
            plane, scratch, dev, generation=1, board=reject_board, launcher=approve_stub()
        )
        assert first["terminal"] == "refused", first["terminal_reason"]
        assert first["terminal_code"] == "GATE_REJECTED"
        # The gate sealed its refusal at the decision path (uncommitted: the
        # refusal terminalises before any materialize step).
        sealed = json.loads((scratch / GATE_PATH.format(generation=1)).read_text())
        assert sealed["decision"] == "REJECT"
        assert sealed["decision_message_id"] == REJECT_MESSAGE_ID

        # The exit: start opens generation 2 as a gate rework.
        started = plane.start(dev)
        assert started["generation"] == 2
        assert started["mode"] == "fresh"

        argv = launcher.specs[-1].argv()
        assert "--gate-reject-file" in argv
        mandate_path = Path(argv[argv.index("--gate-reject-file") + 1])
        assert mandate_path == plane.root / dev / "g2" / GATE_REJECT_FILE
        mandate = json.loads(mandate_path.read_text())
        assert mandate["decision"] == "REJECT"
        assert mandate["decision_message_id"] == REJECT_MESSAGE_ID
        assert mandate["rationale"] == REJECT_RATIONALE
        assert mandate["rejected_generation"] == 1

        # The verdict became a durable part of the chain: committed, readable
        # at the standard gate_decision_path.
        committed = json.loads(git(scratch, "show", f"HEAD:{GATE_PATH.format(generation=1)}"))
        assert committed["decision"] == "REJECT"

        # The launch is on the record, and so is the rework dispatch.
        launches = [
            json.loads(line)
            for line in (plane.root / dev / "launches.jsonl").read_text().splitlines()
            if line
        ]
        assert launches[-1]["generation"] == 2
        assert launches[-1]["mode"] == "fresh"
        events = (plane.root / dev / "g2" / "events.jsonl").read_text()
        assert "gate_rework_dispatch" in events
        assert REJECT_MESSAGE_ID in events

        # g2, as the launched unit would run it: a real implement dispatch
        # whose prompt carries the rejecting verdict.
        approve_board = FakeBoard()
        approve_board.decision = Decision(
            message_id="msg-approve-g2",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        g2_launcher = approve_stub()
        second = execute_generation(
            plane,
            scratch,
            dev,
            generation=2,
            board=approve_board,
            launcher=g2_launcher,
            gate_reject=mandate,
        )
        assert second["terminal"] == "complete", second["terminal_reason"]

        prompt = plane.root / dev / "g2" / "stages" / "implement-g2-a1-prompt.md"
        assert prompt.is_file(), "the rework generation must dispatch a new implementer"
        body = prompt.read_text(encoding="utf-8")
        assert GATE_REJECT_ANCHOR in body
        assert REJECT_MESSAGE_ID in body
        assert REJECT_RATIONALE in body
        assert "decision: REJECT" in body

        # A new agent-run directory backs the new dispatch: one per dispatched
        # llm stage, implement first.
        agent_runs = sorted(
            entry.name for entry in (plane.root / dev / "g2" / "agent-runs").iterdir()
        )
        assert len(agent_runs) == 3
        assert g2_launcher.dispatched == ["implement", "continuous_review", "final_review"]

        # The generation's own event trail records its real segment.
        g2_events = (plane.root / dev / "g2" / "events.jsonl").read_text()
        assert '"stage": "implement"' in g2_events

    def test_a_resume_of_the_rework_generation_still_carries_the_mandate(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A killed rework generation resumes with its mandate: the relaunched
        unit is wired exactly as the fresh one was."""
        stub_plugin_seals(scratch, monkeypatch)
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        reject_board = FakeBoard()
        reject_board.decision = Decision(
            message_id=REJECT_MESSAGE_ID,
            decision="REJECT",
            decided_by="青林",
            question="",
            rationale=REJECT_RATIONALE,
            card_entity_id="card-1",
            raw={},
        )
        execute_generation(
            plane, scratch, dev, generation=1, board=reject_board, launcher=approve_stub()
        )
        plane.start(dev)
        (plane.root / dev / CHECKPOINT_FILE).touch()
        (plane.root / dev / "g2" / RESULT_FILE).write_text(
            json.dumps({"development_id": dev, "awaiting": None, "history": []}),
            encoding="utf-8",
        )

        second = plane.start(dev)
        assert second["generation"] == 2
        argv = launcher.specs[-1].argv()
        assert "--gate-reject-file" in argv
        mandate = json.loads(Path(argv[argv.index("--gate-reject-file") + 1]).read_text())
        assert mandate["decision_message_id"] == REJECT_MESSAGE_ID


# --- contract B: re-seal replay is identified and refused --------------------


class TestReworkContractB:
    def _rejected_dev(
        self, plane: DdControlPlane, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> str:
        stub_plugin_seals(scratch, monkeypatch)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        board = FakeBoard()
        board.decision = Decision(
            message_id=REJECT_MESSAGE_ID,
            decision="REJECT",
            decided_by="青林",
            question="",
            rationale=REJECT_RATIONALE,
            card_entity_id="card-1",
            raw={},
        )
        result = execute_generation(
            plane, scratch, dev, generation=1, board=board, launcher=approve_stub()
        )
        assert result["terminal"] == "refused"
        return dev

    def test_a_contradicting_verdict_record_refuses_the_start(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GATE_REJECTED terminal whose sealed record says otherwise cannot
        be turned into a rework generation: start refuses by name."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = self._rejected_dev(plane, scratch, monkeypatch)

        # Tamper: the record now claims the gate approved.
        record_path = scratch / GATE_PATH.format(generation=1)
        tampered = json.loads(record_path.read_text())
        tampered["decision"] = "APPROVE"
        record_path.write_text(json.dumps(tampered), encoding="utf-8")
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "tamper")

        launches_before = launch_count(plane, dev)
        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == CODE_REWORK_REPLAY_REFUSED
        assert "gate-reject-rationale" in refused.value.detail
        assert launch_count(plane, dev) == launches_before, "a refused start launches nothing"

    def test_an_unreadable_verdict_record_refuses_the_start(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = self._rejected_dev(plane, scratch, monkeypatch)

        record_path = scratch / GATE_PATH.format(generation=1)
        record_path.write_text("{not json", encoding="utf-8")
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "corrupt the verdict record")

        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == CODE_REWORK_REPLAY_REFUSED

    def test_a_gate_rework_generation_cannot_replay_the_sealed_prefix(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """The MUT-R2 pin: a generation whose own tree seals a rejecting gate
        verdict for its predecessor is refused unless its launch carries the
        rework mandate -- a receipt replayer would open a "new generation"
        with no new implement prompt and no new agent run."""
        from fleet_graph.dd.dispatch import derive_attempt_id
        from fleet_graph.dd.upstream_constants import compute_json_digest

        dev_dir = tmp_path / "dd" / "dev-001"
        g2_root = dev_dir / "g2"

        # g1's sealed prefix on disk: seed -> configure -> implement, the
        # implement receipt chaining back to its configure link exactly as
        # the plugin sealer freezes it.
        commit_file = scratch / "greet.py"
        commit_file.write_text('def greet():\n    return "hi"\n', encoding="utf-8")
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "seed")
        seed = head(scratch)

        configure_commit_content = {
            "development_id": "dev-001",
            "generation": 1,
            "acceptance_commands": [["true"]],
            "setup_commands": [],
            "acceptance_env": {},
        }
        (scratch / ".dev-dispatch" / "run-config.json").parent.mkdir(parents=True, exist_ok=True)
        (scratch / ".dev-dispatch" / "run-config.json").write_text(
            json.dumps(configure_commit_content), encoding="utf-8"
        )
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "dev-dispatch: configure")
        configure = head(scratch)

        commit_file.write_text('def greet():\n    return "hello there"\n', encoding="utf-8")
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "implement")
        implement_commit = head(scratch)

        implement_receipt = {
            "actor_job_id": "job-1",
            "artifacts": [],
            "attempt_id": derive_attempt_id("dev-001", 1, 1),
            "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
            "development_id": "dev-001",
            "feedback_digest": "sha256:" + "0" * 64,
            "input_commit": configure,
            "materialization_intent_id": "intent-implement",
            "output_commit": implement_commit,
            "parent_handoff_receipt_digest": compute_json_digest(
                {"stage": "configure", "input_commit": seed, "output_commit": configure}
            ),
            "spec_digest": "sha256:" + "1" * 64,
            "verification_record": {"verification_commands": []},
            "work_head_commit": implement_commit,
        }
        receipt_dir = dev_dir / "state" / "receipts" / derive_attempt_id("dev-001", 1, 1)
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / "implement-receipt.json").write_bytes(
            json.dumps(implement_receipt, sort_keys=True).encode("utf-8")
        )

        # The tree seals g1's rejecting gate verdict, committed at HEAD.
        gate_record = {
            "development_id": "dev-001",
            "decision": "REJECT",
            "decided_by": "青林",
            "decision_message_id": REJECT_MESSAGE_ID,
            "rationale": REJECT_RATIONALE,
            "question_note_id": "note-1",
            "card_entity_id": "card-1",
            "output_commit": implement_commit,
        }
        (scratch / GATE_PATH.format(generation=1)).parent.mkdir(parents=True, exist_ok=True)
        (scratch / GATE_PATH.format(generation=1)).write_text(
            json.dumps(gate_record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "dev-dispatch: seal gate reject g1")

        bare = tmp_path / "durable.git"
        git(scratch, "init", "-q", "--bare", str(bare))
        config = DevelopmentConfig(
            development_id="dev-001",
            workspace_path=scratch,
            state_root=g2_root / "state",
            run_root=g2_root,
            remote_url=str(bare),
            remote_ref="refs/heads/dev-001",
            target_base_commit="b" * 40,
            root_handoff_digest="sha256:" + "c" * 64,
            plugin_binding=object(),
            head_commit=head(scratch),
            generation=2,
            run_config={"acceptance_commands": [["true"]]},
        )
        # No --gate-reject-file, no gate_reject: the launch cannot assemble a
        # contract-carrying implement dispatch, so the generation is refused
        # instead of being replayed into existence.
        with pytest.raises(ReworkReplayRefused) as refused:
            run_pipeline(config, launcher=AgentRunStub())
        assert refused.value.code == REWORK_REPLAY_REFUSED
        assert "new-implement-prompt" in refused.value.missing
        assert not (g2_root / "stages").exists(), "nothing ran: no stage, no prompt"
        assert not (g2_root / RESULT_FILE).exists(), "a refused generation writes no result"

    def test_a_gate_rework_generation_refuses_an_explicit_replayer(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with the mandate wired, a receipt replayer beside it is the
        fake-generation machine: refused, never silently preferred."""
        from fleet_graph.graphs.dd_replay import ReceiptReplayer
        from fleet_graph.graphs.dd_runner import build_pipeline

        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = self._rejected_dev(plane, scratch, monkeypatch)
        plane.start(dev)

        mandate = frozen_mandate(plane, dev, 2)
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        g2_root = plane.root / dev / "g2"
        config = DevelopmentConfig(
            development_id=dev,
            workspace_path=scratch,
            state_root=g2_root / "state",
            run_root=g2_root,
            remote_url=record["remote_url"],
            remote_ref=record["remote_ref"],
            target_base_commit=record["target_base_commit"],
            root_handoff_digest=record["root_handoff_digest"],
            plugin_binding=object(),
            head_commit=git(scratch, "rev-parse", "HEAD"),
            generation=2,
            run_config={
                "acceptance_commands": [list(c) for c in record["acceptance_commands"]],
                "setup_commands": [],
                "acceptance_env": {},
            },
            gate_reject=mandate,
        )
        replayer = ReceiptReplayer(
            workspace=scratch,
            state_root=g2_root / "state",
            prior_state_roots=((1, plane.root / dev / "state"),),
            development_id=dev,
            generation=2,
        )
        with pytest.raises(ReworkReplayRefused):
            build_pipeline(config, replayer=replayer)


# --- the negatives: the path applies to gate rework and nothing else ---------


class TestThePathIsGateReworkOnly:
    def test_a_failed_terminal_bump_carries_no_rework_mandate(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """The environment/contract exit keeps its own semantics: the bumped
        generation is launched with no verdict, no mandate file, no refusal."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)
        (plane.root / dev / CHECKPOINT_FILE).touch()
        (plane.root / dev / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": "failed",
                    "terminal_reason": "acceptance failed: [['true']]",
                    "terminal_code": "ACCEPTANCE_FAILED",
                    "terminal_detail": "",
                    "stage": "acceptance",
                    "head_commit": head(scratch),
                    "awaiting": None,
                    "history": [],
                }
            ),
            encoding="utf-8",
        )

        started = plane.start(dev)
        assert started["generation"] == 2
        argv = launcher.specs[-1].argv()
        assert "--gate-reject-file" not in argv
        assert not (plane.root / dev / "g2" / GATE_REJECT_FILE).exists()

    def test_complete_and_fabrication_still_refuse_and_inject_nothing(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path, RecordingLauncher())
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        (plane.root / dev / CHECKPOINT_FILE).touch()
        (plane.root / dev / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": "complete",
                    "terminal_reason": "merger is the last declared stage",
                    "terminal_code": "",
                    "terminal_detail": "",
                    "stage": "merger",
                    "head_commit": head(scratch),
                    "awaiting": None,
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ControlPlaneError) as complete:
            plane.start(dev)
        assert complete.value.code == "DEVELOPMENT_COMPLETE"

        (plane.root / dev / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": "failed",
                    "terminal_reason": "implement failed (UNVERIFIED_TEST_CLAIM)",
                    "terminal_code": "UNVERIFIED_TEST_CLAIM",
                    "terminal_detail": "claimed exit 0, measured exit 1",
                    "stage": "implement",
                    "head_commit": "",
                    "awaiting": None,
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ControlPlaneError) as fabrication:
            plane.start(dev)
        assert fabrication.value.code == "FABRICATION_FINAL"

    def test_the_inner_rework_loop_is_never_injected(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engine's existing cr/fr REJECT inner loop is untouched -- and a
        rework attempt inside a generation carries no gate anchor, because no
        gate verdict steered it."""
        stub_plugin_seals(scratch, monkeypatch)
        plane = make_plane(tmp_path, RecordingLauncher())
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        approve = Decision(
            message_id="msg-approve",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        board = FakeBoard()
        board.decision = approve
        # The inner loop: the continuous review REJECTs attempt 1, the rework
        # implement runs as attempt 2, and the second review approves.
        launcher = AgentRunDirs(
            {"continuous_review": ["REJECT", "APPROVE"], "final_review": ["APPROVE"]}
        )
        launcher.state_root = str(plane.root / dev / "agent-runs")
        result = run_pipeline(
            DevelopmentConfig(
                development_id=dev,
                workspace_path=scratch,
                state_root=plane.root / dev / "state",
                run_root=plane.root / dev,
                remote_url=f"file://{tmp_path / 'origin.git'}",
                remote_ref=f"refs/heads/dd/{dev}",
                target_base_commit="b" * 40,
                root_handoff_digest="sha256:" + "c" * 64,
                plugin_binding=object(),
                head_commit=git(scratch, "rev-parse", "HEAD"),
                run_config={"acceptance_commands": [["true"]]},
                checkpoint_path=str(plane.root / dev / CHECKPOINT_FILE),
            ),
            board=board,
            gate_card_entity_id="card-1",
            launcher=launcher,
        )

        assert result["terminal"] == "complete", result["terminal_reason"]
        rework_prompt = plane.root / dev / "stages" / "implement-g1-a2-prompt.md"
        assert rework_prompt.is_file(), "the cr REJECT must steer a real rework attempt"
        assert GATE_REJECT_ANCHOR not in rework_prompt.read_text(encoding="utf-8")


# --- the anchor itself -------------------------------------------------------


def test_the_anchor_renders_message_id_and_rationale() -> None:
    from fleet_graph.dd.prompt import render_gate_reject_section

    section = render_gate_reject_section(
        {
            "decision": "REJECT",
            "decision_message_id": REJECT_MESSAGE_ID,
            "decided_by": "青林",
            "rejected_generation": 1,
            "rationale": REJECT_RATIONALE,
        }
    )
    assert f"## {GATE_REJECT_ANCHOR} {REJECT_MESSAGE_ID}" in section
    assert "decision: REJECT" in section
    assert REJECT_RATIONALE in section

    # Spec ⑮-b: an unbound verdict (no message id, no rationale) never
    # renders -- the prompt layer refuses it loudly instead of shipping a
    # placeholder task book with an empty binding.
    from fleet_graph.dd.prompt import PromptError

    with pytest.raises(PromptError) as unbound:
        render_gate_reject_section({"decision_message_id": "", "rationale": ""})
    assert "decision_message_id" in str(unbound.value)

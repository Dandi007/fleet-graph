"""Spec ⑮-b (wf-8d9737): the gate-reject verdict's board binding, end to end.

The live defect this pins shut: dev-fg-eee4da1e3649 g3 dispatched two real
`gate_rework_dispatch` events whose `decision_message_id` was `""` -- the
second one 13 minutes after the board had already rejected the empty binding.
The engine's ``terminal-facts`` fallback turned a verdict it could not bind
into a "success" task book, and the implement prompt never carried the
board's words. Three faces, one red target:

1. **source rebind** -- `g<N>/gate-reject.json` must come from the board
   `work.decision.v1` the gate actually consumed: `decision_message_id`,
   `decided_by`, `rationale` all non-empty, the rationale verbatim in full,
   `source` naming the board message (never `terminal-facts` as a success
   path).
2. **full text in the prompt** -- the implement prompt's
   `gate-reject-rationale:` anchor carries the full rationale verbatim and
   the decision message id; every rework keyword the board wrote is
   greppable under the anchor.
3. **the red target** -- a REJECT with an empty `decision_message_id` (the
   binding unavailable) must refuse the dispatch by structured code
   (`REWORK_DECISION_UNBOUND`) with nothing launched, nothing dispatched;
   the same refusal covers the legacy no-record-at-all shape, whose old
   `terminal-facts` fallback is what produced the g3 empty bindings.

Against the base (7f20b340a69b) the red-target tests here FAIL: the old code
silently dispatches an unbound rework. That is the required old-red proof.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git
from fleet_graph.bus.board import Decision
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    GATE_REJECT_FILE,
    ControlPlaneError,
    DdControlPlane,
)
from fleet_graph.dd.prompt import GATE_REJECT_ANCHOR, PromptError, render_gate_reject_section
from fleet_graph.graphs.dd_runner import DevelopmentConfig, build_pipeline, run_pipeline
from fleet_graph.graphs.dd_scripts import GATE_PATH
from test_dd_runner import AgentRunStub, FakeBoard, RealCommitSealer

SPEC = "# SPEC: greet\n\nMake greet() personal.\n\n```dd-acceptance\ntrue\n```\n"

REJECT_MESSAGE_ID = "msg_01M1RJCT5VBSRCBIND15BPROOF"
# The board's own words, multi-line: the rework keywords (`LineRebase`,
# `MUT-1`) must survive verbatim into the mandate file and the prompt anchor.
REJECT_RATIONALE = (
    "返工面：LineRebase 接线必须补齐 dispatch 面拒绝路径的回归覆盖；\n"
    "MUT-1 删发射零红说明测试网没有张好，先钉红再谈放行；\n"
    "gate-reject 来源必须绑定 board work.decision.v1，不得 terminal-facts 兜底。"
)
DECIDED_BY = "青林"


# --- fixtures (the test_rework_contract shape) -------------------------------


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
    record = json.loads((plane.root / dev / "record.json").read_text())
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


def decision(message_id: str, rationale: str = REJECT_RATIONALE) -> Decision:
    """A board work.decision.v1 as the gate consumes it."""
    return Decision(
        message_id=message_id,
        decision="REJECT",
        decided_by=DECIDED_BY,
        question="",
        rationale=rationale,
        card_entity_id="card-1",
        raw={},
    )


def launch_count(plane: DdControlPlane, dev: str) -> int:
    path = plane.root / dev / "launches.jsonl"
    if not path.is_file():
        return 0
    return len([line for line in path.read_text().splitlines() if line])


# --- ① the source rebind: gate-reject.json carries the board binding ---------


class TestSourceRebind:
    def test_gate_reject_json_is_bound_to_the_board_decision(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gate REJECT whose board message carries the full binding seals a
        gate-reject.json with the three fields non-empty, the rationale
        verbatim in full, and `source` naming board work.decision.v1."""
        stub_plugin_seals(scratch, monkeypatch)
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        reject_board = FakeBoard()
        reject_board.decision = decision(REJECT_MESSAGE_ID)
        first = execute_generation(
            plane, scratch, dev, generation=1, board=reject_board, launcher=approve_stub()
        )
        assert first["terminal"] == "refused", first["terminal_reason"]
        assert first["terminal_code"] == "GATE_REJECTED"

        started = plane.start(dev)
        assert started["generation"] == 2

        # The dispatch happened -- the anti-false-reject face: a *bound*
        # verdict is never refused for being bound. (g1 ran in-process; the
        # plane's own launch trail holds exactly the g2 rework launch.)
        assert launch_count(plane, dev) == 1
        events = (plane.root / dev / "g2" / "events.jsonl").read_text()
        assert "gate_rework_dispatch" in events
        assert f'"decision_message_id": "{REJECT_MESSAGE_ID}"' in events

        mandate_path = plane.root / dev / "g2" / GATE_REJECT_FILE
        mandate = json.loads(mandate_path.read_text())
        assert mandate["decision"] == "REJECT"
        # The binding triple, all three non-empty.
        assert mandate["decision_message_id"] == REJECT_MESSAGE_ID
        assert mandate["decided_by"] == DECIDED_BY
        # The rationale verbatim, full text -- newlines and every keyword.
        assert mandate["rationale"] == REJECT_RATIONALE
        assert "LineRebase" in mandate["rationale"]
        assert "MUT-1" in mandate["rationale"]
        # The source names the board message class -- `terminal-facts` is no
        # longer a success-path source anywhere.
        assert mandate["source"] == "board:work.decision.v1"
        assert mandate["source_record"] in {"committed", "worktree"}


# --- ② the full text under the prompt anchor ---------------------------------


class TestFullTextUnderTheAnchor:
    def test_the_implement_prompt_carries_the_full_rationale_and_message_id(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rework implement prompt's `gate-reject-rationale:` anchor
        carries the decision message id and the rationale verbatim in full --
        so every rework keyword the board wrote is greppable under it."""
        stub_plugin_seals(scratch, monkeypatch)
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        reject_board = FakeBoard()
        reject_board.decision = decision(REJECT_MESSAGE_ID)
        execute_generation(
            plane, scratch, dev, generation=1, board=reject_board, launcher=approve_stub()
        )
        plane.start(dev)
        mandate = json.loads((plane.root / dev / "g2" / GATE_REJECT_FILE).read_text())

        approve_board = FakeBoard()
        approve_board.decision = Decision(
            message_id="msg-approve-g2",
            decision="APPROVE",
            decided_by=DECIDED_BY,
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        second = execute_generation(
            plane,
            scratch,
            dev,
            generation=2,
            board=approve_board,
            launcher=approve_stub(),
            gate_reject=mandate,
        )
        assert second["terminal"] == "complete", second["terminal_reason"]

        prompt = plane.root / dev / "g2" / "stages" / "implement-g2-a1-prompt.md"
        assert prompt.is_file(), "the rework generation must dispatch a new implementer"
        body = prompt.read_text(encoding="utf-8")
        anchor_at = body.index(f"## {GATE_REJECT_ANCHOR} {REJECT_MESSAGE_ID}")
        # The message id, on the anchor line and again as a labeled field.
        assert body.count(REJECT_MESSAGE_ID) >= 2
        assert f"decision_message_id: {REJECT_MESSAGE_ID}" in body
        # The rationale verbatim, in full, multi-line intact -- never a
        # summary, never the terminal's one-line face.
        assert REJECT_RATIONALE in body
        assert body.index(REJECT_RATIONALE) > anchor_at
        # Board-declared rework keywords grep under the anchor.
        for keyword in ("LineRebase", "MUT-1", "terminal-facts"):
            assert keyword in body
            assert body.index(keyword) > anchor_at
        assert "decision: REJECT" in body
        assert GATE_REJECT_ANCHOR in body

    def test_the_anchor_renderer_refuses_an_unbound_payload(self) -> None:
        """Defense in depth: the prompt layer itself never renders an empty
        binding -- it fails loudly instead of shipping a hollow task book."""
        with pytest.raises(PromptError) as unbound:
            render_gate_reject_section({"decision": "REJECT", "decision_message_id": ""})
        assert "decision_message_id" in str(unbound.value)
        with pytest.raises(PromptError) as no_rationale:
            render_gate_reject_section(
                {"decision": "REJECT", "decision_message_id": REJECT_MESSAGE_ID, "rationale": ""}
            )
        assert "rationale" in str(no_rationale.value)


# --- ③ the red target: an unbound verdict refuses the dispatch ----------------


class TestUnboundVerdictRefusesDispatch:
    def test_an_empty_decision_message_id_refuses_the_start(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE RED TARGET (board 2815's g3 shape, pinned): a REJECT whose
        board message id is empty -- the binding unavailable -- must refuse
        the rework dispatch by structured code, with no launch, no
        gate-reject.json, no gate_rework_dispatch event. The old code
        silently dispatched exactly this; against the base this test FAILS."""
        stub_plugin_seals(scratch, monkeypatch)
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        unbound_board = FakeBoard()
        unbound_board.decision = decision("")  # the binding is unavailable
        first = execute_generation(
            plane, scratch, dev, generation=1, board=unbound_board, launcher=approve_stub()
        )
        assert first["terminal"] == "refused"
        assert first["terminal_code"] == "GATE_REJECTED"
        # The gate did seal its refusal -- with an empty message id.
        sealed = json.loads((scratch / GATE_PATH.format(generation=1)).read_text())
        assert sealed["decision"] == "REJECT"
        assert sealed["decision_message_id"] == ""

        launches_before = launch_count(plane, dev)
        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "REWORK_DECISION_UNBOUND"
        assert "decision_message_id" in refused.value.detail

        # 未派发: nothing launched, nothing dispatched, nothing sealed for g2.
        assert launch_count(plane, dev) == launches_before, "a refused start launches nothing"
        assert not (plane.root / dev / "g2" / GATE_REJECT_FILE).exists()
        assert not (plane.root / dev / "g2" / "events.jsonl").exists(), (
            "no gate_rework_dispatch event may exist for an unbound verdict"
        )

    def test_a_missing_verdict_record_refuses_instead_of_terminal_facts(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy shape that produced the g3 empty bindings: a
        GATE_REJECTED terminal with no sealed verdict record anywhere. The
        old `terminal-facts` fallback dispatched an empty-binding task book;
        it is now a refusal, never a success path."""
        stub_plugin_seals(scratch, monkeypatch)
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]

        reject_board = FakeBoard()
        reject_board.decision = decision(REJECT_MESSAGE_ID)
        first = execute_generation(
            plane, scratch, dev, generation=1, board=reject_board, launcher=approve_stub()
        )
        assert first["terminal"] == "refused"
        assert first["terminal_code"] == "GATE_REJECTED"
        # The refusal terminalised before any materialize step, so the record
        # exists only as an uncommitted worktree copy: remove it and the
        # binding is gone for good -- the legacy result's exact shape.
        (scratch / GATE_PATH.format(generation=1)).unlink()

        launches_before = launch_count(plane, dev)
        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "REWORK_DECISION_UNBOUND"
        assert launch_count(plane, dev) == launches_before
        assert not (plane.root / dev / "g2" / GATE_REJECT_FILE).exists()

    def test_the_dispatch_face_refuses_an_unbound_mandate_file(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """Defense at the `dd run` face: even a hand-launched unit carrying an
        unbound `--gate-reject-file` payload refuses to build a pipeline."""
        unbound = {
            "development_id": "dev-unbound",
            "rejected_generation": 1,
            "decision": "REJECT",
            "decision_message_id": "",
            "decided_by": "",
            "rationale": "",
            "source": "terminal-facts",
        }
        config = DevelopmentConfig(
            development_id="dev-unbound",
            workspace_path=scratch,
            state_root=tmp_path / "state",
            run_root=tmp_path / "run",
            remote_url=f"file://{tmp_path / 'origin.git'}",
            remote_ref="refs/heads/dd/dev-unbound",
            target_base_commit="b" * 40,
            root_handoff_digest="sha256:" + "c" * 64,
            plugin_binding=object(),
            head_commit=git(scratch, "rev-parse", "HEAD"),
            generation=2,
            run_config={"acceptance_commands": [["true"]]},
            gate_reject=unbound,
        )
        with pytest.raises(RuntimeError) as refused:
            build_pipeline(config)
        assert "REWORK_DECISION_UNBOUND" in str(refused.value)

    def test_a_bound_mandate_builds_the_pipeline(self, scratch: Path, tmp_path: Path) -> None:
        """The reverse face: a fully bound verdict is never refused *for being
        bound* -- the dispatch face builds the pipeline it owes the rework."""
        bound = {
            "development_id": "dev-bound",
            "rejected_generation": 1,
            "decision": "REJECT",
            "decision_message_id": REJECT_MESSAGE_ID,
            "decided_by": DECIDED_BY,
            # Even carrying the refusal code's own words, a bound verdict is
            # a bound verdict: field-based refusal, never text-based.
            "rationale": f"REWORK_DECISION_UNBOUND is not the reason; {REJECT_RATIONALE}",
            "source": "board:work.decision.v1",
        }
        config = DevelopmentConfig(
            development_id="dev-bound",
            workspace_path=scratch,
            state_root=tmp_path / "state",
            run_root=tmp_path / "run",
            remote_url=f"file://{tmp_path / 'origin.git'}",
            remote_ref="refs/heads/dd/dev-bound",
            target_base_commit="b" * 40,
            root_handoff_digest="sha256:" + "c" * 64,
            plugin_binding=object(),
            head_commit=git(scratch, "rev-parse", "HEAD"),
            generation=2,
            run_config={"acceptance_commands": [["true"]]},
            gate_reject=bound,
        )
        _graph, deps = build_pipeline(config)
        assert deps.dispatcher is not None

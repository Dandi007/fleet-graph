"""R3 gate node: six evidence obligations -> one consumed release.

The spec (wf-4601c8 R3) makes the dispatching line's own graph gate node the
sole ``awaiting_gate`` release path: it consumes the line's
``dd.gate_release.v1`` action, discharges the six evidence obligations
mechanically, asserts ``decided_by == dispatched_by``, and only then seals the
verdict into the subject workspace, publishes it to the decision read model and
resumes the suspended pipeline. This file pins the non-negotiable field of each
obligation, the negative criteria (missing/failed obligation -> failed receipt;
green->red regression refused; foreign decider refused; unwired workspace /
resume-not-consumed honestly receipted), and the S12 mutation-enumeration
binding -- including the instance from the previous disposition's return:
deleting the gate node's ``consume`` call in ``goal_line.py`` must leave the
frozen acceptance without coverage.

Everything here runs against pure data or a duck-typed dd control plane; the
decision seal is a real git commit in a throwaway workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.self_gate import (
    EVIDENCE_REGRESSION,
    REQUIRED_EVIDENCE,
    EvidenceItem,
    RegressionBaseline,
    collect_evidence,
    enumerate_mutation_targets,
    evidence_acceptance_frozen,
    evidence_diff_within_scope,
    evidence_personally_rerun,
    evidence_regression,
    evidence_zero_test_deletion,
    render_rationale,
    verify_mutation_receipt,
)
from fleet_graph.graphs.dd_gate import (
    CODE_NOT_DISPATCHER,
    CODE_OBLIGATIONS_FAILED,
    GraphGateNode,
)
from fleet_graph.graphs.stop_response import (
    KIND_GATE_RELEASE,
    REASON_PAYLOAD_SCHEMA,
    STATUS_CONSUMED,
    STATUS_FAILED,
)

DD_ID = "dev-fg-abc"
PRINCIPAL = "wf-8d9737"
FROZEN = [
    ["bash", "-lc", "uv sync --frozen && uv run pytest -q tests/test_m3_line_selfgate.py"],
    ["bash", "-lc", "make verify"],
]


def _all_pass() -> list[EvidenceItem]:
    return [EvidenceItem(name, name, True, "grounded") for name in REQUIRED_EVIDENCE]


def _gate_workspace(tmp_path: Path) -> Path:
    import subprocess

    workspace = tmp_path / "gate-subject"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )
    return workspace


def _gate_action(
    verdict: str = "APPROVE",
    decided_by: str = PRINCIPAL,
    key: str = "k1",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "development_id": DD_ID,
        "verdict": verdict,
        "decided_by": decided_by,
    }
    if verdict == "REJECT":
        payload["board_decision"] = {
            "problem": "p",
            "suggested_answer": "a",
            "cost_of_no_answer": "c",
        }
    return {"kind": KIND_GATE_RELEASE, "payload": payload, "idempotency_key": key}


class FakeDd:
    """A duck-typed dd control plane: ``get`` + ``gate`` + publish + refusals.

    The resume consumes the verdict per its semantics (M3.1): a REJECT
    terminalises the single as ``refused``; an APPROVE moves it off the gate.
    """

    def __init__(
        self,
        *,
        state: str = "awaiting_gate",
        dispatched_by: str = PRINCIPAL,
        generation: int = 2,
        worktree_path: str = "",
        consume_on_resume: bool = True,
        resume_exit_code: str = "",
    ) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.generation = generation
        self.worktree_path = worktree_path
        self.consume_on_resume = consume_on_resume
        self.resume_exit_code = resume_exit_code
        self.resumed: list[tuple[str, str]] = []
        self.published: list[dict[str, Any]] = []
        self.refusals: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": self.generation,
            "awaiting": {"question_note_id": "q-dd-1", "card_entity_id": "card-dd-1"},
        }
        if self.worktree_path:
            payload["worktree_path"] = self.worktree_path
            payload["repo_path"] = self.worktree_path
        return payload

    def publish_gate_decision(
        self,
        development_id: str,
        *,
        decision: str,
        decided_by: str,
        reason: str = "",
        action_key: str = "",
    ) -> dict[str, Any]:
        self.published.append(
            {
                "development_id": development_id,
                "decision": decision,
                "decided_by": decided_by,
                "reason": reason,
                "action_key": action_key,
            }
        )
        return {"development_id": development_id, "decision": decision}

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append((development_id, action_key or ""))
        if self.consume_on_resume and self.state == "awaiting_gate":
            self.state = "refused" if action_key and action_key.endswith(":REJECT") else "running"
        resume_payload: dict[str, Any] = {
            "development_id": development_id,
            "generation": self.generation,
        }
        if self.resume_exit_code:
            resume_payload["exit_code"] = self.resume_exit_code
        return {"state": self.state, "development_id": development_id, "resume": resume_payload}

    def record_gate_refusal(
        self, development_id: str, *, code: str, reason: str, exit_code: str = ""
    ) -> None:
        self.refusals.append(
            {
                "development_id": development_id,
                "code": code,
                "reason": reason,
                "exit_code": exit_code,
            }
        )


class TestObligationAcceptanceFrozen:
    def test_three_way_equality_passes(self) -> None:
        item = evidence_acceptance_frozen(
            spec_argv=FROZEN, record_acceptance_commands=FROZEN, receipt_command=FROZEN
        )
        assert item.passed is True

    def test_a_divergent_record_is_red(self) -> None:
        item = evidence_acceptance_frozen(
            spec_argv=FROZEN,
            record_acceptance_commands=[["make", "verify"]],
            receipt_command=FROZEN,
        )
        assert item.passed is False


class TestObligationDiffWithinScope:
    def test_product_changes_inside_scope_pass(self) -> None:
        item = evidence_diff_within_scope(
            changed_product_paths=["src/fleet_graph/dd/self_gate.py"],
            spec_deliverable_prefixes=["src/fleet_graph/"],
        )
        assert item.passed is True

    def test_machine_files_are_exempt(self) -> None:
        item = evidence_diff_within_scope(
            changed_product_paths=[
                ".dev-dispatch/spec/approved.md",
                ".dd-evidence/acceptance.json",
            ],
            spec_deliverable_prefixes=["src/fleet_graph/"],
        )
        assert item.passed is True

    def test_an_out_of_scope_product_change_is_red(self) -> None:
        item = evidence_diff_within_scope(
            changed_product_paths=["src/fleet_graph/research_anchor.py"],
            spec_deliverable_prefixes=["src/fleet_graph/dd/"],
        )
        assert item.passed is False


class TestObligationZeroTestDeletion:
    def test_no_deleted_tests_pass(self) -> None:
        assert evidence_zero_test_deletion(deleted_paths=[]).passed is True

    def test_a_deleted_test_file_is_red(self) -> None:
        item = evidence_zero_test_deletion(deleted_paths=["tests/test_old.py"])
        assert item.passed is False

    def test_an_updated_test_is_not_a_deletion(self) -> None:
        # --diff-filter=D lists only deletions; an edit is not in this set.
        assert (
            evidence_zero_test_deletion(deleted_paths=["src/fleet_graph/dd/self_gate.py"]).passed
            is True
        )


class TestObligationPersonallyRerun:
    def test_reran_frozen_command_with_echo_and_exit_0_passes(self) -> None:
        item = evidence_personally_rerun(
            rerun_command=FROZEN[0], frozen_command=FROZEN[0], rerun_echo="echo", rerun_exit_code=0
        )
        assert item.passed is True

    def test_a_divergent_command_or_failed_run_is_red(self) -> None:
        assert (
            evidence_personally_rerun(
                rerun_command=["make", "verify"],
                frozen_command=FROZEN[0],
                rerun_echo="echo",
                rerun_exit_code=0,
            ).passed
            is False
        )
        assert (
            evidence_personally_rerun(
                rerun_command=FROZEN[0],
                frozen_command=FROZEN[0],
                rerun_echo="echo",
                rerun_exit_code=1,
            ).passed
            is False
        )


class TestMutationEnumerationAndReceipt:
    def test_enumeration_extracts_added_production_call_sites(self) -> None:
        added = {
            "src/fleet_graph/graphs/runner.py": [
                (123, "    result = deliver_self_gate_decision("),
            ],
            "src/fleet_graph/dd/self_gate.py": [
                (10, "from fleet_graph.decision_mcp import deliver_decision")
            ],
        }
        targets = enumerate_mutation_targets(added)
        assert [t.call for t in targets] == ["deliver_self_gate_decision"]

    def test_enumeration_ignores_imports_defs_and_test_files(self) -> None:
        added = {
            "src/fleet_graph/dd/self_gate.py": [
                (1, "import deliver_self_gate_decision"),
                (2, "def deliver_self_gate_decision():"),
            ],
            "tests/test_x.py": [(3, "    foo()")],
        }
        assert enumerate_mutation_targets(added) == []

    def test_receipt_equal_to_enumeration_all_red_passes(self) -> None:
        enumerated = enumerate_mutation_targets(
            {"src/fleet_graph/graphs/runner.py": [(7, "    result = deliver_self_gate_decision(")]}
        )
        receipt = [
            {"file": t.file, "line": t.line, "call": t.call, "red": True} for t in enumerated
        ]
        assert (
            verify_mutation_receipt(enumerated=enumerated, receipt_targets=receipt).passed is True
        )

    def test_receipt_mismatch_or_not_red_is_refused(self) -> None:
        enumerated = enumerate_mutation_targets(
            {"src/fleet_graph/graphs/runner.py": [(7, "    result = deliver_self_gate_decision(")]}
        )
        missing = []
        assert (
            verify_mutation_receipt(enumerated=enumerated, receipt_targets=missing).passed is False
        )
        not_red = [
            {"file": t.file, "line": t.line, "call": t.call, "red": False} for t in enumerated
        ]
        item = verify_mutation_receipt(enumerated=enumerated, receipt_targets=not_red)
        assert item.passed is False
        assert "land red" in item.detail


class TestObligationRegression:
    def test_red_set_not_expanded_passes_even_when_baseline_is_red(self) -> None:
        baseline = RegressionBaseline(frozenset(["test_a", "test_b"]), failed_count=2)
        item = evidence_regression(
            baseline=baseline,
            patched_failed={"test_a", "test_b"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        assert item.passed is True

    def test_green_to_red_flip_is_refused(self) -> None:
        baseline = RegressionBaseline(frozenset(["test_a"]), failed_count=1)
        item = evidence_regression(
            baseline=baseline,
            patched_failed={"test_a", "test_newly_red"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        assert item.passed is False
        assert "test_newly_red" in item.detail

    def test_red_to_green_is_an_admitted_improvement(self) -> None:
        baseline = RegressionBaseline(frozenset(["test_a", "test_b"]), failed_count=2)
        item = evidence_regression(
            baseline=baseline,
            patched_failed={"test_b"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        assert item.passed is True

    def test_a_drifted_main_head_as_baseline_is_refused(self) -> None:
        baseline = RegressionBaseline(frozenset())
        item = evidence_regression(
            baseline=baseline,
            patched_failed=set(),
            target_base_commit="base" * 10,
            comparison_base_commit="main" * 10,
        )
        assert item.passed is False
        assert "drifted" in item.detail

    def test_a_known_flake_with_rerun_attribution_is_admitted(self) -> None:
        flaky = "tests/test_x.py::test_flaky"
        baseline = RegressionBaseline(frozenset())
        item = evidence_regression(
            baseline=baseline,
            patched_failed={flaky},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
            flake_attributions={flaky: "isolated rerun on clean base passed 3/4"},
        )
        assert item.passed is True


class TestGateNodeConsumption:
    """The gate node consumes the release end to end once the six obligations
    ground it; anything missing fails the action closed."""

    def test_all_six_pass_consumes_approve_as_the_dispatching_line(self, tmp_path: Path) -> None:
        workspace = _gate_workspace(tmp_path)
        dd = FakeDd(worktree_path=str(workspace))
        node = GraphGateNode(dd, evidence=_all_pass())

        receipt = node.consume(_gate_action(), folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_CONSUMED
        assert receipt["decision"] == "APPROVE"
        assert receipt["decided_by"] == PRINCIPAL
        assert dd.resumed == [(DD_ID, "dd-gate-node:dev-fg-abc:g2:k1:APPROVE")]
        # The verdict reached the single's decision read model, decided_by the
        # dispatching line (M3.1 defect 1 carries over to the new path).
        assert dd.published[0]["decided_by"] == PRINCIPAL
        assert dd.published[0]["decision"] == "APPROVE"
        # The verdict is sealed into the subject workspace by the node itself.
        sealed = workspace / ".dev-dispatch" / "gate" / "decision-g2.json"
        assert sealed.is_file()
        import json

        assert json.loads(sealed.read_text(encoding="utf-8"))["decided_by"] == PRINCIPAL

    def test_a_failed_obligation_fails_the_action_not_a_swallow(self, tmp_path: Path) -> None:
        evidence = _all_pass()
        evidence[5] = evidence_regression(
            baseline=RegressionBaseline(frozenset()),
            patched_failed={"test_newly_red"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        dd = FakeDd(worktree_path=str(_gate_workspace(tmp_path)))
        node = GraphGateNode(dd, evidence=evidence)

        receipt = node.consume(_gate_action(), folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_FAILED
        assert receipt["reason"] == CODE_OBLIGATIONS_FAILED
        assert dd.resumed == []

    def test_missing_an_obligation_leaves_the_release_refused(self, tmp_path: Path) -> None:
        evidence = [e for e in _all_pass() if e.id != EVIDENCE_REGRESSION]
        dd = FakeDd(worktree_path=str(_gate_workspace(tmp_path)))
        node = GraphGateNode(dd, evidence=evidence)

        receipt = node.consume(_gate_action(), folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_FAILED
        assert "missing required gate obligation" in receipt["detail"]
        assert dd.resumed == []

    def test_a_foreign_decider_cannot_gate_this_single(self, tmp_path: Path) -> None:
        dd = FakeDd(dispatched_by="wf-other-line", worktree_path=str(_gate_workspace(tmp_path)))
        node = GraphGateNode(dd, evidence=_all_pass())

        receipt = node.consume(_gate_action(), folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_FAILED
        assert receipt["reason"] == CODE_NOT_DISPATCHER
        assert dd.resumed == []

    def test_an_invalid_payload_is_a_schema_refusal(self, tmp_path: Path) -> None:
        dd = FakeDd(worktree_path=str(_gate_workspace(tmp_path)))
        node = GraphGateNode(dd, evidence=_all_pass())
        action = _gate_action()
        action["payload"]["verdict"] = "MAYBE"

        receipt = node.consume(action, folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_FAILED
        assert receipt["reason"] == REASON_PAYLOAD_SCHEMA
        assert dd.resumed == []

    def test_the_six_required_obligations_are_exactly_named(self) -> None:
        assert len(REQUIRED_EVIDENCE) == 6
        assert collect_evidence(_all_pass()) is None
        assert render_rationale(_all_pass()).count("=") >= 6


class TestS10ConsumptionIsHonest:
    """S10 on the new path: the receipt is the consumption evidence, so it
    must never claim a consumption the single's own state contradicts."""

    def test_a_resume_that_was_not_consumed_records_the_parked_read_back(
        self, tmp_path: Path
    ) -> None:
        dd = FakeDd(
            worktree_path=str(_gate_workspace(tmp_path)),
            consume_on_resume=False,
        )
        node = GraphGateNode(dd, evidence=_all_pass())

        receipt = node.consume(_gate_action(), folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_CONSUMED
        assert receipt["post_release_state"] == "awaiting_gate", (
            "the receipt must record honestly that the single stayed parked"
        )

    def test_a_missing_workspace_fails_the_release_before_any_resume(self, tmp_path: Path) -> None:
        dd = FakeDd(worktree_path="/nonexistent/workspace/path")
        node = GraphGateNode(dd, evidence=_all_pass())

        receipt = node.consume(_gate_action(), folder_id=PRINCIPAL, round_no=1)

        assert receipt["status"] == STATUS_FAILED
        assert receipt["reason"] == "release_refused"
        assert dd.resumed == []


class TestS11SecondDeliveryPathDeleted:
    def test_a_dev_fg_target_down_the_decision_surface_is_refused(self) -> None:
        from fleet_graph.decision_mcp import (
            CODE_DD_NOT_DELIVERABLE_HERE,
            OUTCOME_REFUSED,
            deliver_decision,
        )

        result = deliver_decision(
            line=DD_ID,
            decision="APPROVE",
            reason="live",
            run_root=Path("/tmp"),
            lines=[],
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_DELIVERABLE_HERE

    def test_the_deleted_dd_delivery_symbols_are_gone(self) -> None:
        import fleet_graph.decision_mcp as surface
        import fleet_graph.graphs.runner as runner

        for gone in (
            "deliver_decision_dd",
            "TARGET_KIND_DD",
            "_deliver_dd",
            "CODE_NOT_DISPATCHING_LINE",
        ):
            assert not hasattr(surface, gone), gone
        for gone in ("run_self_gate", "deliver_self_gate_decision"):
            assert not hasattr(runner, gone), gone

    def test_the_gate_node_is_the_only_release_consumer_wired(self) -> None:
        from fleet_graph.graphs import goal_line
        from fleet_graph.graphs.dd_gate import DdGatePort

        deps = goal_line.LineDeps(
            coordinator=object(),
            worker=object(),
            inbox=object(),
            artifacts=object(),
            gate=None,
        )
        assert deps.gate is None
        assert hasattr(deps, "dd_awaiting_gate_development_id")
        assert DdGatePort is not None


class TestS12GateNodeCallSiteCoverage:
    def test_the_gate_node_obligation_call_is_a_production_call(self, tmp_path: Path) -> None:
        """The S12 instance, R3 shape: the gate node's mechanical obligation
        collection is a real production call the mechanically-enumerated
        mutation targets see, and the gate node itself is *covered* by the
        frozen acceptance (the consumption tests drive it end to end)."""
        import inspect

        from fleet_graph.graphs import dd_gate

        source = inspect.getsource(dd_gate)
        assert "return collect_gate_evidence(" in source

        added = {
            "src/fleet_graph/graphs/dd_gate.py": [
                (1, "        return collect_gate_evidence("),
            ]
        }
        calls = [target.call for target in enumerate_mutation_targets(added)]
        assert "collect_gate_evidence" in calls


def test_runner_wires_the_gate_node_and_the_wake_anchor(tmp_path: Path) -> None:
    """The wake identity and the gate plane flow through ``build_line``: the
    wake rides the coordinator envelope, and a bound plane becomes a real
    gate node."""
    import fleet_graph.graphs.runner as runner

    config = runner.LineConfig(
        folder_id=PRINCIPAL,
        seat="s",
        run_root=tmp_path,
        dd_awaiting_gate_development_id=DD_ID,
        dd_gate_plane=object(),
    )
    _graph, deps = runner.build_line(config)
    assert deps.dd_awaiting_gate_development_id == DD_ID
    assert isinstance(deps.gate, GraphGateNode)
    assert deps.gate.plane is config.dd_gate_plane


def test_an_unbound_gate_plane_leaves_the_release_failed_closed(tmp_path: Path) -> None:
    import fleet_graph.graphs.runner as runner

    config = runner.LineConfig(folder_id=PRINCIPAL, seat="s", run_root=tmp_path)
    _graph, deps = runner.build_line(config)
    assert deps.gate is None


def _fake_agent_run(tmp_path: Path) -> str:
    """A fake agent-run binary whose envelope faults the coordinator adapter,
    so run_line can be driven past the wake envelope without a gateway."""
    import sys

    fake_run = Path(__file__).parent / "fakes" / "fake_agent_run.py"
    bin_path = tmp_path / "agent-run"
    bin_path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_run}" "$@"\n')
    bin_path.chmod(0o755)
    return str(bin_path)


class TestWakeReachesTheLineProcess:
    """Finding 1 (blocker), fixed: a dd_awaiting_gate wake names the next
    launch a self-gate run all the way down -- daemon capture, LaunchSpec
    argv, CLI flag, LineConfig. An ordinary run carries none of it."""

    def test_launchspec_argv_forwards_the_dd_wake(self) -> None:
        from fleet_graph.scheduler.launcher import LaunchSpec

        spec = LaunchSpec(folder_id="wf-1", seat="s", dd_awaiting_gate_development_id=DD_ID)
        argv = spec.argv()
        assert argv[argv.index("--dd-awaiting-gate") + 1] == DD_ID

    def test_an_ordinary_launch_carries_no_dd_wake(self) -> None:
        from fleet_graph.scheduler.launcher import LaunchSpec

        assert "--dd-awaiting-gate" not in LaunchSpec(folder_id="wf-1", seat="s").argv()

    def test_the_cli_parses_the_dd_wake_flag(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(
            ["line", "run", "--folder", "wf-1", "--seat", "s", "--dd-awaiting-gate", DD_ID]
        )
        assert args.dd_awaiting_gate == DD_ID
        ordinary = build_parser().parse_args(["line", "run", "--folder", "wf-1", "--seat", "s"])
        assert ordinary.dd_awaiting_gate == ""


class _GateClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _GateUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class _GateProber:
    def check(self, seat: str) -> bool:
        return True


class _GateLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> Any:
        from fleet_graph.scheduler.launcher import LaunchResult

        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class _ScriptedDdFacts:
    def __init__(self, fact: str | None) -> None:
        self.fact = fact

    def dd_fact(self, development_id: str) -> str | None:
        return self.fact


def _parked_dispatch_line(
    tmp_path: Path, fact: str | None
) -> tuple[Any, _GateClock, _GateLauncher]:
    """A line the scheduler launched once, now parked ``waiting_dd``."""
    from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig

    clock = _GateClock(1_787_000_000.0)
    launcher = _GateLauncher()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[LineSpec(folder_id="wf-1", seat="s", enabled=True)],
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=_GateProber(),
        launcher=launcher,
        units=_GateUnits(),
        clock=clock,
        sleep=lambda _s: None,
        dd=_ScriptedDdFacts(None),
    )
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    launcher.launched.clear()
    terminal = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": "run-d1",
        "at": "2026-08-27T10:00:00Z",
        "reason": "dispatched, waiting for the development",
        "waiting_on": "dd",
        "dd_development_id": DD_ID,
    }
    path = tmp_path / "runs" / "wf-1" / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(terminal), encoding="utf-8")
    clock.now += 3600.0
    scheduler.dd = _ScriptedDdFacts(fact)
    return scheduler, clock, launcher


def test_an_awaiting_gate_wake_launches_the_line_as_the_gate(tmp_path: Any) -> None:
    scheduler, clock, launcher = _parked_dispatch_line(tmp_path, fact=None)
    scheduler.tick()  # the park establishes; nothing launches
    assert launcher.launched == []

    scheduler.dd = _ScriptedDdFacts("awaiting_gate")
    clock.now += 600.0  # past cooldown
    results = scheduler.tick()

    assert results[0].decision.ignite
    spec = launcher.launched[0]
    assert spec.dd_awaiting_gate_development_id == DD_ID
    argv = spec.argv()
    assert argv[argv.index("--dd-awaiting-gate") + 1] == DD_ID


def test_the_wake_identity_is_consumed_by_exactly_one_launch(tmp_path: Any) -> None:
    scheduler, clock, launcher = _parked_dispatch_line(tmp_path, fact=None)
    scheduler.tick()
    scheduler.dd = _ScriptedDdFacts("awaiting_gate")
    clock.now += 600.0
    scheduler.tick()
    assert launcher.launched[0].dd_awaiting_gate_development_id == DD_ID

    clock.now += 600.0
    scheduler.tick()
    assert launcher.launched[1].dd_awaiting_gate_development_id == ""


def test_an_already_awaiting_gate_fact_at_establish_is_still_a_gate_wake(
    tmp_path: Any,
) -> None:
    """The establish path parks nothing when the fact already exists -- the
    line would then have re-ignited as an ordinary run. It must not."""
    scheduler, _clock, launcher = _parked_dispatch_line(tmp_path, fact="awaiting_gate")
    results = scheduler.tick()

    assert results[0].decision.ignite
    assert results[0].park_event == "not_parked:dd_awaiting_gate"
    assert launcher.launched[0].dd_awaiting_gate_development_id == DD_ID


def test_a_terminal_wake_is_an_ordinary_run_not_a_gate_run(tmp_path: Any) -> None:
    scheduler, clock, launcher = _parked_dispatch_line(tmp_path, fact=None)
    scheduler.tick()
    scheduler.dd = _ScriptedDdFacts("terminal")
    clock.now += 600.0
    results = scheduler.tick()

    assert results[0].decision.ignite
    assert launcher.launched[0].dd_awaiting_gate_development_id == ""


class TestRunLineCarriesTheWakeIntoTheEnvelope:
    """R3 shape of the rc-aa907dfb fix: ``run_line`` no longer pre-delivers a
    gate verdict; the wake identity rides the coordinator envelope as a fact,
    and the release travels only through the graph's gate node."""

    def test_a_dd_wake_run_line_injects_the_wake_into_the_coordinator_envelope(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        import fleet_graph.graphs.runner as runner
        from fleet_graph.graphs.adapters import CoordinatorFault

        config = runner.LineConfig(
            folder_id="wf-1",
            seat="s",
            run_root=tmp_path / "run",
            checkpoint_path=":memory:",
            agent_run_bin=_fake_agent_run(tmp_path),
            dd_awaiting_gate_development_id=DD_ID,
        )
        with pytest.raises(CoordinatorFault):
            runner.run_line(config)

        envelope = json.loads(
            (tmp_path / "run" / "coord" / "round-1-input.json").read_text(encoding="utf-8")
        )
        assert envelope["dd_awaiting_gate_development_id"] == DD_ID
        assert not (tmp_path / "run" / "self-gate.json").exists(), (
            "no pre-graph gate delivery may happen any more"
        )

    def test_an_ordinary_run_line_carries_no_wake_anchor(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        import fleet_graph.graphs.runner as runner
        from fleet_graph.graphs.adapters import CoordinatorFault

        config = runner.LineConfig(
            folder_id="wf-1",
            seat="s",
            run_root=tmp_path / "run",
            checkpoint_path=":memory:",
            agent_run_bin=_fake_agent_run(tmp_path),
        )
        with pytest.raises(CoordinatorFault):
            runner.run_line(config)

        envelope = json.loads(
            (tmp_path / "run" / "coord" / "round-1-input.json").read_text(encoding="utf-8")
        )
        assert "dd_awaiting_gate_development_id" not in envelope


# --- the mechanical collector (dd/self_gate_evidence.py) --------------------


def _git(workspace: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True, text=True)


def _git_head(workspace: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


BASE_SPEC = "```dd-acceptance\nbash -lc 'echo gate-ok'\n```\n"
FROZEN_ARGV = ["bash", "-lc", "echo gate-ok"]


def _make_workspace(tmp_path: Path) -> tuple[Path, str, str]:
    """A real two-commit repo: base, then a head that adds one production
    call site (the S12 shape) plus a test file. The base also carries an
    older test file so a deletion inside ``base..head`` is expressible."""
    workspace = tmp_path / "ws"
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / ".dev-dispatch" / "spec").mkdir(parents=True)
    (workspace / "src" / "pkg" / "api.py").write_text("def serve():\n    return 1\n")
    (workspace / "tests" / "test_legacy.py").write_text("def test_legacy():\n    assert True\n")
    (workspace / ".dev-dispatch" / "spec" / "approved.md").write_text(BASE_SPEC)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "gate@example.invalid")
    _git(workspace, "config", "user.name", "gate")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "base")
    base = _git_head(workspace)

    (workspace / "src" / "pkg" / "api.py").write_text("def serve():\n    return gate()\n")
    (workspace / "tests" / "test_api.py").write_text("def test_serve():\n    assert True\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "head")
    return workspace, base, _git_head(workspace)


def _seal_receipt(dd_root: Path, attempt: str, name: str, payload: dict[str, Any]) -> Path:
    path = dd_root / DD_ID / "state" / "receipts" / attempt / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _record_dd(workspace: Path, base: str) -> Any:
    class RecordDd:
        def __init__(self) -> None:
            self.record = {
                "development_id": DD_ID,
                "state": "awaiting_gate",
                "dispatched_by": PRINCIPAL,
                "generation": 1,
                "target_base_commit": base,
                "worktree_path": str(workspace),
                "acceptance_commands": [FROZEN_ARGV],
            }

        def get(self, development_id: str) -> dict[str, Any]:
            assert development_id == DD_ID
            return dict(self.record)

    return RecordDd()


class TestMechanicalCollector:
    """The production collector: real git facts, sealed receipts, seams."""

    def test_diff_facts_are_read_from_the_real_repo(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate import enumerate_mutation_targets
        from fleet_graph.dd.self_gate_evidence import (
            diff_added_lines,
            diff_changed_paths,
            diff_deleted_paths,
        )

        workspace, base, head = _make_workspace(tmp_path)

        changed = diff_changed_paths(workspace, base, head)
        assert "src/pkg/api.py" in changed
        assert "tests/test_api.py" in changed
        assert diff_deleted_paths(workspace, base, head) == []

        targets = enumerate_mutation_targets(diff_added_lines(workspace, base, head))
        assert [(t.file, t.call) for t in targets] == [("src/pkg/api.py", "gate")]

    def test_an_updated_test_is_changed_but_not_deleted(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate_evidence import (
            diff_changed_paths,
            diff_deleted_paths,
        )

        workspace, base, _head = _make_workspace(tmp_path)
        (workspace / "tests" / "test_api.py").write_text(
            "def test_serve():\n    assert gate() == 1\n"
        )
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-q", "-m", "edit test")

        new_head = _git_head(workspace)
        assert "tests/test_api.py" in diff_changed_paths(workspace, base, new_head)
        assert diff_deleted_paths(workspace, base, new_head) == []

    def test_a_deleted_test_file_is_seen(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate_evidence import diff_deleted_paths

        workspace, base, _head = _make_workspace(tmp_path)
        (workspace / "tests" / "test_legacy.py").unlink()
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-q", "-m", "drop tests")

        assert "tests/test_legacy.py" in diff_deleted_paths(workspace, base, _git_head(workspace))

    def test_all_six_obligations_pass_when_the_sources_align(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate import (
            EVIDENCE_ACCEPTANCE_FROZEN,
            EVIDENCE_DIFF_WITHIN_SCOPE,
            EVIDENCE_MUTATION_RECEIPT,
            EVIDENCE_PERSONALLY_RERUN,
            EVIDENCE_REGRESSION,
            EVIDENCE_ZERO_TEST_DELETION,
            enumerate_mutation_targets,
        )
        from fleet_graph.dd.self_gate_evidence import (
            collect_gate_evidence,
            diff_added_lines,
        )

        workspace, base, head = _make_workspace(tmp_path)
        dd_root = tmp_path / "dd"
        _seal_receipt(
            dd_root,
            "attempt-1",
            "implement-receipt.json",
            {
                "output_commit": head,
                "verification_record": {
                    "verification_commands": [{"argv": FROZEN_ARGV, "exit_code": 0}]
                },
            },
        )
        enumerated = enumerate_mutation_targets(diff_added_lines(workspace, base, head))
        _seal_receipt(
            dd_root,
            "attempt-1",
            "final-review-receipt.json",
            {
                "implementation_subject_commit": head,
                "mutation_targets": [
                    {"file": t.file, "line": t.line, "call": t.call, "red": True}
                    for t in enumerated
                ],
                "verified_items": ["acceptance_frozen", "mutation_experiment"],
            },
        )
        (workspace / ".dd-evidence").mkdir()
        (workspace / ".dd-evidence" / "regression-baseline.json").write_text(
            json.dumps(
                {
                    "base_commit": base,
                    "passed": 10,
                    "failed": 1,
                    "skipped": 0,
                    "failed_tests": ["tests/test_legacy.py::test_old"],
                }
            ),
            encoding="utf-8",
        )

        evidence = collect_gate_evidence(
            development_id=DD_ID,
            dd=_record_dd(workspace, base),
            dd_root=dd_root,
            rerun=lambda _ws, _argv: ("gate-ok\n", 0),
            regression_probe=lambda _ws: {"tests/test_legacy.py::test_old"},
        )

        assert {item.id for item in evidence} == {
            EVIDENCE_ACCEPTANCE_FROZEN,
            EVIDENCE_DIFF_WITHIN_SCOPE,
            EVIDENCE_ZERO_TEST_DELETION,
            EVIDENCE_PERSONALLY_RERUN,
            EVIDENCE_MUTATION_RECEIPT,
            EVIDENCE_REGRESSION,
        }
        assert all(item.passed for item in evidence), {
            item.id: item.detail for item in evidence if not item.passed
        }

    def test_a_final_review_receipt_without_the_record_fails_the_mutation_obligation(
        self, tmp_path: Path
    ) -> None:
        from fleet_graph.dd.self_gate import EVIDENCE_MUTATION_RECEIPT
        from fleet_graph.dd.self_gate_evidence import collect_gate_evidence

        workspace, base, head = _make_workspace(tmp_path)
        dd_root = tmp_path / "dd"
        _seal_receipt(
            dd_root,
            "attempt-1",
            "implement-receipt.json",
            {
                "output_commit": head,
                "verification_record": {
                    "verification_commands": [{"argv": FROZEN_ARGV, "exit_code": 0}]
                },
            },
        )
        _seal_receipt(
            dd_root,
            "attempt-1",
            "final-review-receipt.json",
            {"implementation_subject_commit": head, "findings": []},
        )
        (workspace / ".dd-evidence").mkdir()
        (workspace / ".dd-evidence" / "regression-baseline.json").write_text(
            json.dumps({"base_commit": base, "failed_tests": []}), encoding="utf-8"
        )

        evidence = collect_gate_evidence(
            development_id=DD_ID,
            dd=_record_dd(workspace, base),
            dd_root=dd_root,
            rerun=lambda _ws, _argv: ("gate-ok\n", 0),
            regression_probe=lambda _ws: set(),
        )

        mutation = next(item for item in evidence if item.id == EVIDENCE_MUTATION_RECEIPT)
        assert mutation.passed is False
        assert "mutation_targets" in mutation.detail
        assert "verified_items" in mutation.detail

    def test_a_mutation_receipt_short_of_the_enumeration_is_refused(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate import EVIDENCE_MUTATION_RECEIPT
        from fleet_graph.dd.self_gate_evidence import collect_gate_evidence

        workspace, base, head = _make_workspace(tmp_path)
        dd_root = tmp_path / "dd"
        _seal_receipt(
            dd_root,
            "attempt-1",
            "final-review-receipt.json",
            {
                "implementation_subject_commit": head,
                "mutation_targets": [],  # enumerated the call site, receipt names none
                "verified_items": ["mutation_experiment"],
            },
        )
        (workspace / ".dd-evidence").mkdir()
        (workspace / ".dd-evidence" / "regression-baseline.json").write_text(
            json.dumps({"base_commit": base, "failed_tests": []}), encoding="utf-8"
        )

        evidence = collect_gate_evidence(
            development_id=DD_ID,
            dd=_record_dd(workspace, base),
            dd_root=dd_root,
            rerun=lambda _ws, _argv: ("gate-ok\n", 0),
            regression_probe=lambda _ws: set(),
        )

        mutation = next(item for item in evidence if item.id == EVIDENCE_MUTATION_RECEIPT)
        assert mutation.passed is False
        assert "src/pkg/api.py" in mutation.detail

    def test_a_baseline_anchored_on_a_drifted_head_is_refused(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate import EVIDENCE_REGRESSION
        from fleet_graph.dd.self_gate_evidence import collect_gate_evidence

        workspace, base, head = _make_workspace(tmp_path)
        dd_root = tmp_path / "dd"
        _seal_receipt(
            dd_root,
            "attempt-1",
            "implement-receipt.json",
            {
                "output_commit": head,
                "verification_record": {
                    "verification_commands": [{"argv": FROZEN_ARGV, "exit_code": 0}]
                },
            },
        )
        _seal_receipt(
            dd_root,
            "attempt-1",
            "final-review-receipt.json",
            {
                "implementation_subject_commit": head,
                "mutation_targets": [],
                "verified_items": ["x"],
            },
        )
        (workspace / ".dd-evidence").mkdir()
        (workspace / ".dd-evidence" / "regression-baseline.json").write_text(
            json.dumps(
                {
                    "base_commit": "f" * 40,  # a drifted main head, not the frozen base
                    "failed_tests": [],
                }
            ),
            encoding="utf-8",
        )

        evidence = collect_gate_evidence(
            development_id=DD_ID,
            dd=_record_dd(workspace, base),
            dd_root=dd_root,
            rerun=lambda _ws, _argv: ("gate-ok\n", 0),
            regression_probe=lambda _ws: set(),
        )

        regression = next(item for item in evidence if item.id == EVIDENCE_REGRESSION)
        assert regression.passed is False
        assert "drifted" in regression.detail

    def test_a_failed_personal_rerun_fails_the_obligation_with_the_echo(
        self, tmp_path: Path
    ) -> None:
        from fleet_graph.dd.self_gate import EVIDENCE_PERSONALLY_RERUN
        from fleet_graph.dd.self_gate_evidence import collect_gate_evidence

        workspace, base, head = _make_workspace(tmp_path)
        dd_root = tmp_path / "dd"
        _seal_receipt(
            dd_root,
            "attempt-1",
            "implement-receipt.json",
            {
                "output_commit": head,
                "verification_record": {
                    "verification_commands": [{"argv": FROZEN_ARGV, "exit_code": 0}]
                },
            },
        )
        _seal_receipt(
            dd_root,
            "attempt-1",
            "final-review-receipt.json",
            {
                "implementation_subject_commit": head,
                "mutation_targets": [],
                "verified_items": ["x"],
            },
        )
        (workspace / ".dd-evidence").mkdir()
        (workspace / ".dd-evidence" / "regression-baseline.json").write_text(
            json.dumps({"base_commit": base, "failed_tests": []}), encoding="utf-8"
        )

        evidence = collect_gate_evidence(
            development_id=DD_ID,
            dd=_record_dd(workspace, base),
            dd_root=dd_root,
            rerun=lambda _ws, _argv: ("FAILED tests/test_api.py\n", 1),
            regression_probe=lambda _ws: set(),
        )

        rerun_item = next(item for item in evidence if item.id == EVIDENCE_PERSONALLY_RERUN)
        assert rerun_item.passed is False
        assert "FAILED tests/test_api.py" in rerun_item.detail

    def test_an_unresolvable_single_fails_every_obligation_closed(self, tmp_path: Path) -> None:
        from fleet_graph.dd.self_gate import REQUIRED_EVIDENCE
        from fleet_graph.dd.self_gate_evidence import collect_gate_evidence

        class DyingDd:
            def get(self, development_id: str) -> dict[str, Any]:
                raise RuntimeError("dd root unreadable")

        evidence = collect_gate_evidence(
            development_id=DD_ID,
            dd=DyingDd(),
            rerun=lambda _ws, _argv: ("", 0),
            regression_probe=lambda _ws: set(),
        )

        assert {item.id for item in evidence} == set(REQUIRED_EVIDENCE)
        assert all(item.passed is False for item in evidence)
        assert all("gate evidence collection fault" in item.detail for item in evidence)

    def test_the_default_rerun_runs_the_frozen_argv_and_keeps_the_exit(
        self, tmp_path: Path
    ) -> None:
        from fleet_graph.dd.self_gate_evidence import default_rerun

        echo, exit_code = default_rerun(tmp_path, ["bash", "-lc", "echo personal; exit 3"])
        assert "personal" in echo
        assert exit_code == 3

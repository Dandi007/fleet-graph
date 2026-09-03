"""M3 线自判闸 + S10/S11/S12 收束 —— 冻结验收用例。

覆盖（spec §判据 + §测试与验收）：

- 六项取证义务逐项（含第 6 条 S9 四款），漏任一项投递被拒、阳性路径、principal 校验。
- S10 三条阴性：错路径 workspace 在起 unit **之前** REFUSED；unit 起了但单据仍在
  awaiting_gate → REFUSED 且带 unit 退出码；每次 REFUSED → `gate_refused` 写入 +
  `events.jsonl` 追加（旧形态「进程死了单据一个字没变」必须能红）。
- S11 三条：形态 A（target_kind="dd"）与形态 B 同判；非派单方（含空串）对真实
  awaiting 单投递必须 NOT_DISPATCHING_LINE；代码中不存在绕过 principal 校验的
  dd 投递路径。
- S12：机械枚举 == base..head 产品 diff 新增生产侧调用点全集；final_review 在
  一次性副本执行变异并落逐靶子红/绿回执；gate 只核验回执；review 回执必填
  checked/verified_items；删 runner.py 实例靶行后冻结验收必须红；final_review
  执行入口在生产 review 模块调用图静态可达（D8）。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.control_plane import ControlPlaneError, DdControlPlane
from fleet_graph.dd.mutation import (
    CHECKED_ITEMS_FIELD,
    MUTATION_TARGET_NOT_RED,
    enumerate_mutation_targets,
    execute_final_review_mutations,
    static_call_reachable,
    validate_review_receipt,
    verify_mutation_receipt,
)
from fleet_graph.decision_mcp import (
    CODE_DD_GATE_NOT_CONSUMED,
    CODE_DD_NOT_AWAITING_GATE,
    CODE_DD_NOT_FOUND,
    CODE_DD_WORKSPACE_MISSING,
    CODE_NOT_DISPATCHING_LINE,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    deliver_decision,
)
from fleet_graph.self_gate.runner import (
    DUTY_ACCEPTANCE_RUN,
    DUTY_ACCEPTANCE_THREE_WAY,
    DUTY_MUTATION_RECEIPT,
    DUTY_PRODUCT_DIFF_BOUNDARY,
    DUTY_REGRESSION_BASELINE,
    DUTY_ZERO_TEST_DELETIONS,
    deliver_self_gate_decision,
    duty_acceptance_run,
    duty_acceptance_three_way,
    duty_mutation_receipt,
    duty_product_diff_boundary,
    duty_regression_baseline,
    duty_zero_test_deletions,
    handle_dd_awaiting_gate_wake,
)

RUNNER_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "fleet_graph" / "self_gate" / "runner.py"
)
ACTORS_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "fleet_graph" / "graphs" / "dd_actors.py"
)
DECISION_MCP_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "fleet_graph" / "decision_mcp.py"
)
VERIFY_LIM_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify-lim.sh"

LINE = "wf-8d9737"
DD_ID = "dev-fg-36c2d76baca7"
FROZEN_ACCEPTANCE = [["uv", "run", "pytest", "-q", "tests/test_m3_line_selfgate.py"]]

BASELINE_RED = [
    "tests/test_supervisor_graph.py::TestKillRestartReAdopt::"
    "test_killed_supervisor_re_adopts_its_audit_run"
]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


@dataclass
class DevRepo:
    """A real repo plus its ``base..head`` pins (head adds the call site)."""

    path: Path
    base: str
    head: str


class FakeDdPlane:
    """A duck-typed dd control plane exercising the S10/S11 contract."""

    def __init__(
        self,
        *,
        state: str = "awaiting_gate",
        dispatched_by: str = LINE,
        worktree: Path | None = None,
        post_resume_state: str = "running",
        unit_exit_code: int | None = None,
        development_exists: bool = True,
        target_base_commit: str = "",
        head_commit: str = "",
        acceptance_commands: list[list[str]] | None = None,
    ) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.worktree = worktree
        self.post_resume_state = post_resume_state
        self.unit_exit_code = unit_exit_code
        self.development_exists = development_exists
        self.target_base_commit = target_base_commit
        self.head_commit = head_commit
        self.acceptance_commands = (
            [list(argv) for argv in acceptance_commands] if acceptance_commands is not None else []
        )
        self.resumed: list[tuple[str, str]] = []
        self.refusals: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        if not self.development_exists:
            raise ControlPlaneError(
                "DEVELOPMENT_NOT_FOUND", f"no admission record for {development_id}"
            )
        return {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": 2,
            "awaiting": {"question_note_id": "q-1", "card_entity_id": "card-1"},
            "worktree_path": str(self.worktree) if self.worktree is not None else "",
            "unit_exit_code": self.unit_exit_code,
            "target_base_commit": self.target_base_commit,
            "head_commit": self.head_commit,
            "acceptance_commands": self.acceptance_commands,
        }

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append((development_id, action_key or ""))
        # The unit runs and writes its state change (or dies leaving the
        # single where it was -- that is what post_resume_state models).
        self.state = self.post_resume_state
        return {"resume": {"development_id": development_id, "generation": 2}}

    def record_gate_refusal(
        self,
        development_id: str,
        *,
        reason: str,
        unit_exit_code: int | None = None,
        source: str = "decision_mcp",
    ) -> dict[str, Any]:
        refusal = {
            "development_id": development_id,
            "reason": reason,
            "unit_exit_code": unit_exit_code,
            "source": source,
        }
        self.refusals.append(refusal)
        return refusal


class RecordingDeliverer:
    """A stand-in for the M2 delivery core that remembers every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class R:
            def as_dict(self) -> dict[str, Any]:
                return {"status": "delivered", "outcome": "consumed"}

        return R()


@pytest.fixture
def dev_repo(tmp_path: Path) -> DevRepo:
    """A real repo: base..head adds one production call site plus its test."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def handler(value):\n    return value\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_seed():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text(
        "def handler(value):\n    return value\n\n\nresult = handler(value)\n",
        encoding="utf-8",
    )
    (tests / "test_app.py").write_text(
        "def test_seed():\n    assert True\n\n\ndef test_call():\n    assert handler(1) == 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head: wire handler")
    return DevRepo(path=repo, base=base, head=_git(repo, "rev-parse", "HEAD"))


def _green_runner(cwd: Path, argv: list[str]) -> int:
    """An acceptance runner green exactly when the wired call site survives.

    This is what the frozen acceptance means mechanically: delete the wired
    call and the suite goes red.
    """
    text = (cwd / "app.py").read_text(encoding="utf-8")
    return 0 if "result = handler(value)" in text else 1


def _coverage_free_runner(cwd: Path, argv: list[str]) -> int:
    """A broken acceptance: green no matter what was deleted (no coverage)."""
    return 0


def _mutation_receipt(repo: DevRepo, runner: Any = _green_runner) -> dict[str, Any]:
    return execute_final_review_mutations(
        repo.path,
        repo.base,
        repo.head,
        FROZEN_ACCEPTANCE,
        runner=runner,
        worktree_parent=repo.path.parent,
    )


def _snapshot(
    base_commit: str, red: list[str], *, passed: int = 10, failed: int | None = None
) -> dict[str, Any]:
    return {
        "base_commit": base_commit,
        "counts": {
            "passed": passed,
            "failed": failed if failed is not None else len(red),
            "skipped": 2,
        },
        "failed_tests": list(red),
    }


def _deliver(
    *,
    line: str = DD_ID,
    decision: str = "APPROVE",
    reason: str = "self gate",
    principal: str = LINE,
    target_kind: str = "line",
    target_id: str = "",
    dd: Any = None,
    tmp_path: Path | None = None,
) -> Any:
    return deliver_decision(
        line=line,
        decision=decision,
        reason=reason,
        principal=principal,
        run_root=tmp_path or Path("/tmp"),
        lines=[{"folder_id": LINE, "generation": 1}],
        target_kind=target_kind,
        target_id=target_id,
        dd=dd,
    )


# ---------------------------------------------------------------------------
# 六项取证义务
# ---------------------------------------------------------------------------


class TestDuty1AcceptanceThreeWay:
    def test_equal_spec_record_receipt_passes(self) -> None:
        entry = duty_acceptance_three_way(
            FROZEN_ACCEPTANCE, FROZEN_ACCEPTANCE, FROZEN_ACCEPTANCE[0]
        )
        assert entry["duty"] == DUTY_ACCEPTANCE_THREE_WAY
        assert entry["passed"] is True

    def test_any_mismatch_refuses(self) -> None:
        widened = [["uv", "run", "pytest", "-q", "tests/"]]
        assert (
            duty_acceptance_three_way(FROZEN_ACCEPTANCE, widened, FROZEN_ACCEPTANCE[0])["passed"]
            is False
        )
        assert (
            duty_acceptance_three_way(FROZEN_ACCEPTANCE, FROZEN_ACCEPTANCE, ["make", "test"])[
                "passed"
            ]
            is False
        )
        assert (
            duty_acceptance_three_way(FROZEN_ACCEPTANCE, FROZEN_ACCEPTANCE, None)["passed"] is False
        )


class TestDuty2ProductDiffBoundary:
    def test_inside_spec_surface_passes(self) -> None:
        entry = duty_product_diff_boundary(
            ["app.py", "tests/test_app.py"], ["app.py", "tests/test_app.py"]
        )
        assert entry["passed"] is True

    def test_undeclared_product_file_refuses(self) -> None:
        entry = duty_product_diff_boundary(
            ["app.py", "scheduler/daemon.py"], ["app.py", "tests/test_app.py"]
        )
        assert entry["passed"] is False
        assert "scheduler/daemon.py" in entry["outside_surface"]

    def test_machine_namespaces_are_excluded_by_construction(self) -> None:
        entry = duty_product_diff_boundary(
            [".dev-dispatch/spec/approved.md", ".dd-evidence/acceptance.json", "app.py"],
            ["app.py"],
        )
        assert entry["passed"] is True


class TestDuty3ZeroTestDeletions:
    def test_a_repo_that_deletes_no_test_passes(self, dev_repo: DevRepo) -> None:
        entry = duty_zero_test_deletions(dev_repo.path, dev_repo.base, dev_repo.head)
        assert entry["passed"] is True
        assert entry["deleted_tests"] == []

    def test_deleting_a_test_refuses(self, dev_repo: DevRepo) -> None:
        (dev_repo.path / "tests" / "test_app.py").unlink()
        _git(dev_repo.path, "add", "-A")
        _git(dev_repo.path, "commit", "-q", "-m", "head: delete the test")
        head = _git(dev_repo.path, "rev-parse", "HEAD")
        entry = duty_zero_test_deletions(dev_repo.path, dev_repo.base, head)
        assert entry["passed"] is False
        assert entry["deleted_tests"] == ["tests/test_app.py"]


class TestDuty4AcceptanceRunAtGate:
    def test_gate_reruns_the_frozen_acceptance_and_keeps_the_echo(self, dev_repo: DevRepo) -> None:
        entry = duty_acceptance_run(FROZEN_ACCEPTANCE, dev_repo.path, _green_runner)
        assert entry["duty"] == DUTY_ACCEPTANCE_RUN
        assert entry["passed"] is True
        assert entry["runs"] == [{"argv": FROZEN_ACCEPTANCE[0], "exit_code": 0}]

    def test_a_red_rerun_refuses(self, dev_repo: DevRepo) -> None:
        entry = duty_acceptance_run(FROZEN_ACCEPTANCE, dev_repo.path, lambda cwd, argv: 1)
        assert entry["passed"] is False


class TestDuty5MutationReceipt:
    def test_receipt_verifies_against_the_mechanical_enumeration(self, dev_repo: DevRepo) -> None:
        entry = duty_mutation_receipt(
            _mutation_receipt(dev_repo),
            dev_repo.path,
            dev_repo.base,
            dev_repo.head,
            FROZEN_ACCEPTANCE,
        )
        assert entry["passed"] is True

    def test_a_receipt_that_dropped_a_target_refuses(self, dev_repo: DevRepo) -> None:
        receipt = _mutation_receipt(dev_repo)
        receipt["targets"] = receipt["targets"][:-1]
        entry = duty_mutation_receipt(
            receipt, dev_repo.path, dev_repo.base, dev_repo.head, FROZEN_ACCEPTANCE
        )
        assert entry["passed"] is False

    def test_the_gate_never_re_runs_the_gun(self, dev_repo: DevRepo) -> None:
        receipt = _mutation_receipt(dev_repo)
        assert receipt["executor"] == "final_review"
        assert receipt["executed_in"] == "one_shot_copy"
        assert receipt["subject_workspace_writes"] == 0
        entry = duty_mutation_receipt(
            {**receipt, "executor": "gate"},
            dev_repo.path,
            dev_repo.base,
            dev_repo.head,
            FROZEN_ACCEPTANCE,
        )
        assert entry["passed"] is False


class TestDuty6RegressionBaselineS9:
    def test_red_set_unchanged_passes_even_when_the_baseline_is_red(self) -> None:
        entry = duty_regression_baseline(
            _snapshot("basec", BASELINE_RED),
            _snapshot("basec", BASELINE_RED),
            frozen_target_base="basec",
        )
        assert entry["passed"] is True
        assert entry["green_to_red"] == []

    def test_missing_baseline_fields_refuse(self) -> None:
        entry = duty_regression_baseline(
            {"base_commit": "basec", "counts": {"passed": 1, "failed": 0, "skipped": 0}},
            _snapshot("basec", []),
            frozen_target_base="basec",
        )
        assert entry["passed"] is False
        assert "baseline.failed_tests" in entry["missing"]

    def test_green_to_red_flip_refuses(self) -> None:
        new_red = [*BASELINE_RED, "tests/test_new.py::test_flipped"]
        entry = duty_regression_baseline(
            _snapshot("basec", BASELINE_RED),
            _snapshot("basec", new_red),
            frozen_target_base="basec",
        )
        assert entry["passed"] is False
        assert entry["green_to_red"] == ["tests/test_new.py::test_flipped"]

    def test_a_sole_new_red_requires_net_base_flake_attribution_and_keeps_the_evidence(
        self,
    ) -> None:
        new_red = [*BASELINE_RED, "tests/test_flaky.py::test_sometimes_red"]
        refused = duty_regression_baseline(
            _snapshot("basec", BASELINE_RED),
            _snapshot("basec", new_red),
            frozen_target_base="basec",
        )
        assert refused["passed"] is False
        entry = duty_regression_baseline(
            _snapshot("basec", BASELINE_RED),
            _snapshot("basec", new_red),
            frozen_target_base="basec",
            flake_evidence={
                "tests/test_flaky.py::test_sometimes_red": {
                    "isolated_rerun": "pass",
                    "on": "basec",
                    "runs": "1 red / 4 isolated",
                }
            },
        )
        assert entry["passed"] is True
        assert (
            entry["flake_attributions"]["tests/test_flaky.py::test_sometimes_red"]["on"] == "basec"
        )

    def test_comparing_against_drifted_main_refuses(self) -> None:
        entry = duty_regression_baseline(
            _snapshot("drifted-main-head", BASELINE_RED),
            _snapshot("drifted-main-head", BASELINE_RED),
            frozen_target_base="basec",
        )
        assert entry["passed"] is False
        assert "target_base_commit" in entry["detail"]


# ---------------------------------------------------------------------------
# 投递：漏项被拒、阳性路径、principal 校验
# ---------------------------------------------------------------------------


def _gate_inputs(dev_repo: DevRepo, **overrides: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "development_id": DD_ID,
        "principal": LINE,
        "record": {
            "acceptance_commands": FROZEN_ACCEPTANCE,
            "target_base_commit": dev_repo.base,
            "repo_path": str(dev_repo.path),
            "run_root": str(dev_repo.path.parent),
        },
        "repo": dev_repo.path,
        "base": dev_repo.base,
        "head": dev_repo.head,
        "spec_acceptance": FROZEN_ACCEPTANCE,
        "spec_surface": ["app.py", "tests/test_app.py"],
        "stage_receipt_command": FROZEN_ACCEPTANCE[0],
        "mutation_receipt": _mutation_receipt(dev_repo),
        "regression_baseline": _snapshot(dev_repo.base, BASELINE_RED),
        "regression_current": _snapshot(dev_repo.base, BASELINE_RED),
        "workspace": dev_repo.path,
        "acceptance_runner": _green_runner,
        "deliver": RecordingDeliverer(),
    }
    inputs.update(overrides)
    return inputs


class TestSelfGateDelivery:
    def test_all_six_duties_pass_approves_with_the_line_principal(self, dev_repo: DevRepo) -> None:
        inputs = _gate_inputs(dev_repo)
        decision = deliver_self_gate_decision(**inputs)
        assert decision.verdict == "APPROVE"
        assert decision.decided_by == LINE
        assert [entry["duty"] for entry in decision.evidence] == [
            DUTY_ACCEPTANCE_THREE_WAY,
            DUTY_PRODUCT_DIFF_BOUNDARY,
            DUTY_ZERO_TEST_DELETIONS,
            DUTY_ACCEPTANCE_RUN,
            DUTY_MUTATION_RECEIPT,
            DUTY_REGRESSION_BASELINE,
        ]
        assert all(entry["passed"] for entry in decision.evidence)
        # The rationale payload is the template, delivered with the principal.
        deliverer = inputs["deliver"]
        assert len(deliverer.calls) == 1
        kwargs = deliverer.calls[0]
        assert kwargs["principal"] == LINE
        payload = json.loads(kwargs["reason"])
        assert payload["decided_by"] == LINE
        assert payload["evidence_version"] == 1
        assert len(payload["evidence"]) == 6

    def test_missing_any_duty_rejects_with_the_failing_duty_named(self, dev_repo: DevRepo) -> None:
        decision = deliver_self_gate_decision(
            **_gate_inputs(dev_repo, stage_receipt_command=["make", "test"])
        )
        assert decision.verdict == "REJECT"
        assert decision.rationale["failed_duties"] == [DUTY_ACCEPTANCE_THREE_WAY]
        assert "acceptance argv differ" in decision.rationale["reason"]

    def test_undeclared_surface_rejects(self, dev_repo: DevRepo) -> None:
        decision = deliver_self_gate_decision(**_gate_inputs(dev_repo, spec_surface=["app.py"]))
        assert decision.verdict == "REJECT"
        assert DUTY_PRODUCT_DIFF_BOUNDARY in decision.rationale["failed_duties"]

    def test_mutation_receipt_without_coverage_rejects(self, dev_repo: DevRepo) -> None:
        decision = deliver_self_gate_decision(
            **_gate_inputs(
                dev_repo, mutation_receipt=_mutation_receipt(dev_repo, _coverage_free_runner)
            )
        )
        assert decision.verdict == "REJECT"
        assert DUTY_MUTATION_RECEIPT in decision.rationale["failed_duties"]

    def test_regression_refusal_rejects(self, dev_repo: DevRepo) -> None:
        flipped = _snapshot(dev_repo.base, [*BASELINE_RED, "tests/test_new.py::test_x"])
        decision = deliver_self_gate_decision(**_gate_inputs(dev_repo, regression_current=flipped))
        assert decision.verdict == "REJECT"
        assert DUTY_REGRESSION_BASELINE in decision.rationale["failed_duties"]


class TestWakeWiring:
    def test_the_wake_handler_runs_the_gate_and_delivers(self, dev_repo: DevRepo) -> None:
        deliverer = RecordingDeliverer()
        plane = FakeDdPlane(
            worktree=dev_repo.path,
            target_base_commit=dev_repo.base,
            head_commit=dev_repo.head,
            acceptance_commands=FROZEN_ACCEPTANCE,
        )
        result = handle_dd_awaiting_gate_wake(
            DD_ID,
            principal=LINE,
            dd=plane,
            spec_acceptance=FROZEN_ACCEPTANCE,
            spec_surface=["app.py", "tests/test_app.py"],
            stage_receipt_command=FROZEN_ACCEPTANCE[0],
            regression_baseline=_snapshot(dev_repo.base, BASELINE_RED),
            regression_current=_snapshot(dev_repo.base, BASELINE_RED),
            mutation_receipt=_mutation_receipt(dev_repo),
            acceptance_runner=_green_runner,
            deliver=deliverer,
        )
        assert result["verdict"] == "APPROVE"
        assert result["delivery"]["status"] == "delivered"
        assert deliverer.calls and deliverer.calls[0]["principal"] == LINE

    def test_the_instance_target_line_is_present_and_wired(self) -> None:
        """S12.4 / 引擎侧收束判据 ①: 删掉这行,冻结验收必须红。

        The single ``result = deliver_self_gate_decision(...)`` line inside
        ``handle_dd_awaiting_gate_wake`` is the instance mutation target. Two
        reds hang on it: the source inventory below (the line must exist,
        inside that function) and
        :meth:`test_the_wake_handler_runs_the_gate_and_delivers` (the wake
        must actually deliver -- delete the line and that test NameErrors).
        Either one failing turns the frozen acceptance red, which is exactly
        the mutation experiment the final_review stage performs.
        """
        source = RUNNER_SOURCE_PATH.read_text(encoding="utf-8")
        matches = [
            (index, line)
            for index, line in enumerate(source.splitlines(), start=1)
            if re.match(r"^\s*result = deliver_self_gate_decision\(", line)
        ]
        assert len(matches) == 1, "exactly one instance target line must exist"
        tree = ast.parse(source)
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "handle_dd_awaiting_gate_wake"
        )
        assert handler.lineno <= matches[0][0] <= (handler.end_lineno or 0)


# ---------------------------------------------------------------------------
# S10: workspace 校验、消费判据、拒绝留痕
# ---------------------------------------------------------------------------


class TestS10WorkspaceCheckedBeforeAnyUnit:
    def test_a_missing_workspace_refuses_before_a_unit_is_started(self) -> None:
        plane = FakeDdPlane(worktree=None)
        result = _deliver(dd=plane)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_WORKSPACE_MISSING
        # Never started the doomed unit.
        assert plane.resumed == []
        # And the refusal is on the single, not only in the reply.
        assert plane.refusals and "workspace" in plane.refusals[0]["reason"]

    def test_a_workspace_path_that_no_longer_exists_refuses_before_launch(
        self, tmp_path: Path
    ) -> None:
        stale = tmp_path / "20260902-wrong-suffix-worktree"
        plane = FakeDdPlane(worktree=stale)
        result = _deliver(dd=plane, tmp_path=tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_WORKSPACE_MISSING
        assert plane.resumed == []


class TestS10ConsumptionIsTheSuccessCriterion:
    def test_unit_started_but_single_still_awaiting_gate_is_refused_with_the_exit_code(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane(
            worktree=tmp_path,
            post_resume_state="awaiting_gate",
            unit_exit_code=75,
        )
        result = _deliver(dd=plane, tmp_path=tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_GATE_NOT_CONSUMED
        assert "75" in result.message
        assert plane.resumed != []
        assert plane.refusals[-1]["unit_exit_code"] == 75

    def test_a_resumed_single_that_left_awaiting_gate_is_delivered(self, tmp_path: Path) -> None:
        plane = FakeDdPlane(worktree=tmp_path, post_resume_state="running")
        result = _deliver(dd=plane, tmp_path=tmp_path)
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"


class TestS10RefusalLeavesATraceOnTheSingle:
    @pytest.fixture
    def real_plane(self, tmp_path: Path) -> DdControlPlane:
        root = tmp_path / "dd"
        dev = root / DD_ID
        dev.mkdir(parents=True)
        (dev / "record.json").write_text(
            json.dumps(
                {
                    "development_id": DD_ID,
                    "repo_path": str(tmp_path / "gone-workspace"),
                    "remote_url": "ssh://example.invalid/x",
                    "remote_ref": "refs/heads/dd/x",
                    "target_base_commit": "0" * 40,
                    "spec_digest": "sha256:" + "0" * 64,
                    "bootstrap_commit": "0" * 40,
                    "root_handoff_digest": "sha256:" + "0" * 64,
                    "acceptance_commands": FROZEN_ACCEPTANCE,
                    "dispatched_by": LINE,
                    "generation": 1,
                }
            ),
            encoding="utf-8",
        )
        # The single sits at awaiting_gate (its result carries the awaiting ticket).
        (dev / "result.json").write_text(
            json.dumps({"stage": "human_gate", "awaiting": {"question_note_id": "q"}}),
            encoding="utf-8",
        )
        return DdControlPlane(
            root=root,
            plugin_binding=dev / "binding.json",
            unit_probe=lambda unit: False,
            board_factory=lambda: None,
        )

    def test_the_refusal_is_queryable_from_the_read_model(
        self, tmp_path: Path, real_plane: DdControlPlane
    ) -> None:
        result = _deliver(dd=real_plane, tmp_path=tmp_path)
        assert result.code == CODE_DD_WORKSPACE_MISSING
        status = real_plane.get(DD_ID)
        assert status["gate_refused"] is not None
        assert "workspace" in status["gate_refused"]["reason"]
        events = real_plane.events(DD_ID)["events"]
        assert events and events[-1]["event"] == "gate_refused"
        assert "workspace" in events[-1]["reason"]

    def test_record_gate_refusal_keeps_the_unit_exit_code(
        self, tmp_path: Path, real_plane: DdControlPlane
    ) -> None:
        real_plane.record_gate_refusal(DD_ID, reason="unit died", unit_exit_code=75)
        assert real_plane.get(DD_ID)["gate_refused"]["unit_exit_code"] == 75
        assert real_plane.events(DD_ID)["events"][-1]["unit_exit_code"] == 75


# ---------------------------------------------------------------------------
# S11: dd 投递双路径合一
# ---------------------------------------------------------------------------


class TestS11UnifiedDeliveryPaths:
    def test_form_a_with_a_foreign_principal_is_refused_not_dispatching_line(
        self, tmp_path: Path
    ) -> None:
        """阴性判据（不可弱）: 形态 A + 非派单方 → NOT_DISPATCHING_LINE。"""
        plane = FakeDdPlane(worktree=tmp_path)
        result = deliver_decision(
            line="",
            decision="APPROVE",
            reason="foreign delivery",
            principal="uther-tui",
            run_root=tmp_path,
            lines=[],
            target_kind="dd",
            target_id=DD_ID,
            dd=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert plane.resumed == []

    def test_form_a_with_an_empty_principal_is_refused(self, tmp_path: Path) -> None:
        plane = FakeDdPlane(worktree=tmp_path)
        result = deliver_decision(
            line="",
            decision="APPROVE",
            reason="anonymous delivery",
            principal="",
            run_root=tmp_path,
            lines=[],
            target_kind="dd",
            target_id=DD_ID,
            dd=plane,
        )
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert plane.resumed == []

    def test_form_b_with_a_foreign_principal_is_refused_the_same_way(self, tmp_path: Path) -> None:
        plane = FakeDdPlane(worktree=tmp_path)
        result = deliver_decision(
            line=DD_ID,
            decision="APPROVE",
            reason="form b foreign",
            principal="uther-tui",
            run_root=tmp_path,
            lines=[{"folder_id": LINE, "generation": 1}],
            dd=plane,
        )
        assert result.code == CODE_NOT_DISPATCHING_LINE

    def test_both_forms_judge_identically(self, tmp_path: Path) -> None:
        """合一后形态 A 与形态 B 同判（同一 _deliver_dd 核心）。"""
        plane_a = FakeDdPlane(worktree=tmp_path, post_resume_state="running")
        plane_b = FakeDdPlane(worktree=tmp_path, post_resume_state="running")
        delivered_a = deliver_decision(
            line="",
            decision="REJECT",
            reason="form a",
            principal=LINE,
            run_root=tmp_path,
            lines=[],
            target_kind="dd",
            target_id=DD_ID,
            dd=plane_a,
        )
        delivered_b = deliver_decision(
            line=DD_ID,
            decision="REJECT",
            reason="form b",
            principal=LINE,
            run_root=tmp_path,
            lines=[{"folder_id": LINE, "generation": 1}],
            dd=plane_b,
        )
        assert delivered_a.status == delivered_b.status == OUTCOME_DELIVERED
        assert delivered_a.code is None and delivered_b.code is None

    def test_unknown_dd_is_still_an_explicit_not_found(self) -> None:
        plane = FakeDdPlane(development_exists=False)
        result = deliver_decision(
            line="",
            decision="APPROVE",
            reason="probe",
            principal=LINE,
            run_root=Path("/tmp"),
            lines=[],
            target_kind="dd",
            target_id=DD_ID,
            dd=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_FOUND

    def test_a_single_not_at_the_gate_is_refused(self, tmp_path: Path) -> None:
        plane = FakeDdPlane(state="running", worktree=tmp_path)
        result = deliver_decision(
            line=DD_ID,
            decision="APPROVE",
            reason="too early",
            principal=LINE,
            run_root=tmp_path,
            lines=[{"folder_id": LINE, "generation": 1}],
            dd=plane,
        )
        assert result.code == CODE_DD_NOT_AWAITING_GATE
        assert result.retryable is True

    def test_no_dd_delivery_path_bypasses_the_principal_check(self) -> None:
        """合一的实现断言: 代码中不存在绕过 principal 校验的 dd 投递路径。"""
        source = DECISION_MCP_SOURCE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        def function_source(name: str) -> str:
            node = next(
                candidate
                for candidate in ast.walk(tree)
                if isinstance(candidate, ast.FunctionDef) and candidate.name == name
            )
            return ast.get_source_segment(source, node) or ""

        # Both entry points must delegate to the one principal-checked core,
        # and neither may resume a dd single through the old un-checked seam.
        assert "_deliver_dd(" in function_source("deliver_decision_dd")
        assert "_deliver_dd(" in function_source("deliver_decision")
        for entry in ("deliver_decision_dd", "deliver_decision"):
            body = function_source(entry)
            assert "dd_source.resume" not in body, (
                f"{entry} still resumes without the identity check"
            )


# ---------------------------------------------------------------------------
# S12: 机械枚举、一次性副本执行、回执 schema、可达性
# ---------------------------------------------------------------------------


class TestS12MechanicalEnumeration:
    def test_enumeration_equals_the_new_production_call_sites(self, dev_repo: DevRepo) -> None:
        targets = enumerate_mutation_targets(dev_repo.path, dev_repo.base, dev_repo.head)
        assert [target.key for target in targets] == [("app.py", 5, "handler")]

    def test_tests_and_machine_trees_are_never_targets(self, dev_repo: DevRepo) -> None:
        targets = enumerate_mutation_targets(dev_repo.path, dev_repo.base, dev_repo.head)
        assert all(target.path == "app.py" for target in targets)


class TestS12OneShotCopyExecution:
    def test_each_target_is_deleted_and_must_turn_the_acceptance_red(
        self, dev_repo: DevRepo
    ) -> None:
        receipt = _mutation_receipt(dev_repo)
        assert receipt["all_red"] is True
        assert [entry["location"] for entry in receipt["targets"]] == ["app.py:5 (handler)"]
        assert receipt["targets"][0]["red"] is True
        assert receipt["targets"][0]["acceptance_exit_code"] == 1
        assert receipt[CHECKED_ITEMS_FIELD]
        assert receipt["verified_items"]

    def test_the_subject_workspace_is_never_written(self, dev_repo: DevRepo) -> None:
        before = (dev_repo.path / "app.py").read_text(encoding="utf-8")
        _mutation_receipt(dev_repo)
        assert (dev_repo.path / "app.py").read_text(encoding="utf-8") == before
        # The one-shot copy is gone; only the subject itself remains listed.
        listed = _git(dev_repo.path, "worktree", "list", "--porcelain")
        assert listed.count("worktree") == 1

    def test_a_target_that_survives_green_is_reported_not_red(self, dev_repo: DevRepo) -> None:
        receipt = _mutation_receipt(dev_repo, _coverage_free_runner)
        assert receipt["all_red"] is False
        ok, violations = verify_mutation_receipt(
            receipt,
            enumerate_mutation_targets(dev_repo.path, dev_repo.base, dev_repo.head),
        )
        assert ok is False
        assert any(MUTATION_TARGET_NOT_RED in violation for violation in violations)


class TestS12GateVerifiesReceiptOnly:
    def test_target_set_must_equal_the_mechanical_enumeration(self, dev_repo: DevRepo) -> None:
        expected = enumerate_mutation_targets(dev_repo.path, dev_repo.base, dev_repo.head)
        receipt = _mutation_receipt(dev_repo)
        ok, _ = verify_mutation_receipt(receipt, expected)
        assert ok is True
        # One target missing from the receipt -> refused.
        trimmed = {
            **receipt,
            "targets": receipt["targets"][:-1],
            "verified_items": receipt["verified_items"][:-1],
        }
        ok, violations = verify_mutation_receipt(trimmed, expected)
        assert ok is False
        assert any("target set" in violation for violation in violations)
        # One target the diff never had -> refused.
        padded = {
            **receipt,
            "targets": [
                *receipt["targets"],
                {
                    "path": "app.py",
                    "line": 99,
                    "call": "ghost",
                    "removed": True,
                    "red": True,
                },
            ],
        }
        ok, violations = verify_mutation_receipt(padded, expected)
        assert ok is False
        assert any("target set" in violation for violation in violations)

    def test_frozen_acceptance_must_match_the_receipts_commands(self, dev_repo: DevRepo) -> None:
        receipt = _mutation_receipt(dev_repo)
        ok, violations = verify_mutation_receipt(
            receipt,
            enumerate_mutation_targets(dev_repo.path, dev_repo.base, dev_repo.head),
            acceptance_commands=[["make", "verify"]],
        )
        assert ok is False
        assert any("frozen acceptance" in violation for violation in violations)

    def test_a_receipt_without_the_checklists_is_invalid(self, dev_repo: DevRepo) -> None:
        receipt = _mutation_receipt(dev_repo)
        stripped = {
            key: value
            for key, value in receipt.items()
            if key not in (CHECKED_ITEMS_FIELD, "verified_items")
        }
        ok, violations = verify_mutation_receipt(
            stripped,
            enumerate_mutation_targets(dev_repo.path, dev_repo.base, dev_repo.head),
        )
        assert ok is False
        assert any("checked_items" in violation for violation in violations)


class TestS12CheckedItemsSchemaAndPrompt:
    def test_the_engine_enforces_the_checklist_where_reviews_are_ingested(self) -> None:
        """S12.5: 缺 checked/verified_items → 回执无效（引擎侧 schema 检查）。

        The vendored contract mirror stays byte-faithful to what production
        pins (provenance), so the engine owns the requirement in code: the
        actor ingesting a reviewer's result refuses a receipt without the
        checklist.
        """
        from fleet_graph.dd.lifecycle import Lifecycle
        from fleet_graph.graphs.dd_actors import AgentRunStageActor
        from fleet_graph.graphs.dd_pipeline import FAILURE_EVENT

        actor = AgentRunStageActor(
            launcher=None,
            development_id=DD_ID,
            run_root=Path("/tmp"),
            lifecycle=Lifecycle.load(),
        )
        stage = actor.lifecycle.stages["continuous_review"]
        outcome = actor._outcome_from(stage, {"verdict": "APPROVE", "findings": []})
        assert outcome.event == FAILURE_EVENT
        assert "checked/verified_items" in outcome.detail
        audited = {
            "verdict": "APPROVE",
            "findings": [],
            CHECKED_ITEMS_FIELD: ["read the diff against the spec"],
        }
        ok = actor._outcome_from(stage, audited)
        assert ok.event == "APPROVE"
        assert ok.receipt["review_result"] == audited

    def test_a_findings_zero_review_without_a_checklist_is_invalid(self) -> None:
        passing_but_silent = {"verdict": "APPROVE", "findings": []}
        assert validate_review_receipt(passing_but_silent) != []
        audited = {**passing_but_silent, CHECKED_ITEMS_FIELD: ["read the diff against the spec"]}
        assert validate_review_receipt(audited) == []

    def test_the_alias_verified_items_is_accepted(self) -> None:
        assert validate_review_receipt({"verified_items": ["checked the boundary"]}) == []

    def test_both_review_prompts_carry_the_reworded_read_only_rule(self) -> None:
        from fleet_graph.dd.prompt import REVIEW_PROMPT

        assert "The subject workspace is read-only" in REVIEW_PROMPT
        assert "one-shot copy" in REVIEW_PROMPT
        assert "voids the verdict" in REVIEW_PROMPT
        assert "checked_items" in REVIEW_PROMPT
        assert "All twelve keys" in REVIEW_PROMPT


class TestS12InstanceTargetAndReachability:
    def test_final_review_entry_is_reachable_in_the_review_call_graph(self) -> None:
        """D8: 静态可达性断言,不可达即红 —— 不是断言某进程在跑。"""
        assert static_call_reachable(ACTORS_SOURCE_PATH, "act", "execute_final_review_mutations")
        assert static_call_reachable(ACTORS_SOURCE_PATH, "act", "verify_mutation_receipt")
        # Negative control: an unrelated entry stays unreachable.
        assert not static_call_reachable(
            ACTORS_SOURCE_PATH, "stage_role", "execute_final_review_mutations"
        )

    def test_the_gate_side_helper_exists_for_the_walker(self) -> None:
        from fleet_graph.graphs.dd_actors import final_review_mutation_receipt

        assert callable(final_review_mutation_receipt)

    def test_the_actor_refuses_a_review_when_a_target_survives_green(
        self, dev_repo: DevRepo
    ) -> None:
        from fleet_graph.dd.lifecycle import Lifecycle
        from fleet_graph.graphs.dd_actors import GATE_REJECT, AgentRunStageActor
        from fleet_graph.graphs.dd_pipeline import StageOutcome

        actor = AgentRunStageActor(
            launcher=None,
            development_id=DD_ID,
            run_root=dev_repo.path.parent,
            lifecycle=Lifecycle.load(),
            mutation_inputs=lambda dispatch: {
                "repo": dev_repo.path,
                "base": dev_repo.base,
                "head": dev_repo.head,
                "acceptance_commands": FROZEN_ACCEPTANCE,
                "runner": _coverage_free_runner,
                "worktree_parent": dev_repo.path.parent,
            },
        )
        stage = actor.lifecycle.stages["final_review"]
        outcome = actor._with_final_review_mutations(
            stage, {"input_commit": dev_repo.head}, StageOutcome(event="APPROVE")
        )
        assert outcome.event == GATE_REJECT
        assert outcome.failure_code == MUTATION_TARGET_NOT_RED
        assert outcome.receipt["mutation_receipt"]["all_red"] is False

    def test_the_actor_attaches_the_receipt_when_everything_fell_red(
        self, dev_repo: DevRepo
    ) -> None:
        from fleet_graph.dd.lifecycle import Lifecycle
        from fleet_graph.graphs.dd_actors import AgentRunStageActor
        from fleet_graph.graphs.dd_pipeline import StageOutcome

        actor = AgentRunStageActor(
            launcher=None,
            development_id=DD_ID,
            run_root=dev_repo.path.parent,
            lifecycle=Lifecycle.load(),
            mutation_inputs=lambda dispatch: {
                "repo": dev_repo.path,
                "base": dev_repo.base,
                "head": dev_repo.head,
                "acceptance_commands": FROZEN_ACCEPTANCE,
                "runner": _green_runner,
                "worktree_parent": dev_repo.path.parent,
            },
        )
        stage = actor.lifecycle.stages["final_review"]
        outcome = actor._with_final_review_mutations(
            stage,
            {"input_commit": dev_repo.head},
            StageOutcome(event="APPROVE", receipt={"review_result": {"verdict": "APPROVE"}}),
        )
        assert outcome.event == "APPROVE"
        assert outcome.receipt["mutation_receipt"]["all_red"] is True

    def test_a_non_final_review_stage_runs_no_mutations(self, dev_repo: DevRepo) -> None:
        from fleet_graph.dd.lifecycle import Lifecycle
        from fleet_graph.graphs.dd_actors import AgentRunStageActor
        from fleet_graph.graphs.dd_pipeline import StageOutcome

        actor = AgentRunStageActor(
            launcher=None,
            development_id=DD_ID,
            run_root=dev_repo.path.parent,
            lifecycle=Lifecycle.load(),
            mutation_inputs=lambda dispatch: pytest.fail("must not be called"),
        )
        stage = actor.lifecycle.stages["continuous_review"]
        outcome = actor._with_final_review_mutations(
            stage, {"input_commit": dev_repo.head}, StageOutcome(event="APPROVE")
        )
        assert outcome.event == "APPROVE"


# ---------------------------------------------------------------------------
# S7: 收割触发点在 merge 段之后
# ---------------------------------------------------------------------------


class TestS7HarvestAfterMerge:
    def _record(self, repo: Path, generation: int = 1) -> dict[str, Any]:
        return {"development_id": DD_ID, "repo_path": str(repo), "generation": generation}

    def test_a_committed_merge_result_marks_the_merge_landed(self, dev_repo: DevRepo) -> None:
        from fleet_graph.state.fleet_state import merge_result_committed

        assert merge_result_committed(self._record(dev_repo.path)) is False
        merge_dir = dev_repo.path / ".dev-dispatch" / "merge"
        merge_dir.mkdir(parents=True)
        (merge_dir / "result-g1.json").write_text(json.dumps({"merged": True}), encoding="utf-8")
        _git(dev_repo.path, "add", "-A")
        _git(dev_repo.path, "commit", "-q", "-m", "merge result sealed")
        assert merge_result_committed(self._record(dev_repo.path)) is True

    def test_an_unreadable_repo_reads_as_not_merged(self) -> None:
        from fleet_graph.state.fleet_state import merge_result_committed

        assert merge_result_committed(self._record(Path("/nonexistent/repo"))) is False
        assert merge_result_committed({"development_id": DD_ID}) is False


# ---------------------------------------------------------------------------
# S11.3: verify-lim.sh 第 12 条探针判据修正
# ---------------------------------------------------------------------------


class TestVerifyLimProbe12:
    def test_the_probe_targets_a_real_awaiting_single_and_asserts_the_refusal_code(self) -> None:
        source = VERIFY_LIM_PATH.read_text(encoding="utf-8")
        block = source.split("# ---------------- 12 foreign-delivery-refused ----------------", 1)[
            1
        ]
        block = block.split("# ---------------- 13", 1)[0]
        # 真实存在、非本方派单的 awaiting_gate 单。
        assert "dev-fg-36c2d76baca7" in block
        # 断言拿到 NOT_DISPATCHING_LINE。
        assert "NOT_DISPATCHING_LINE" in block
        # 不再使用不存在的合成 id。
        assert "SELFTEST_LINE" not in block
        # 投递面是 decision_deliver。
        assert "decision_deliver" in block

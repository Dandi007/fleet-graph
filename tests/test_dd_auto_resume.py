"""决议落板自动 resume 巡检（GateAutoResumer）—— R4-3 链路的收尾。

四条纪律各由测试钉死：

- **决议落板即 resume**：awaiting_gate + 板上有 decision → 巡检走与
  development_gate(resume=True) 完全相同的路径把挂起线程拉起来，argv 里
  依旧只有无值 `--resume`，夹带不了任何 verdict。
- **无决议不动**：板上没有 decision（或板不可达）→ 一次 launch 都不发生。
- **fail-open**：板读异常、单个 development 判定异常、扫描本身异常，都只
  记日志跳过，tick 正常返回，巡检不崩。
- **幂等**：running 的 development 不进 awaiting_gate 扫描；判定与启动之间
  的竞态由 gate(resume=True) 既有的 ALREADY_RUNNING refuse 兜住，按跳过计。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.bus.board import Decision, GateTicket
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    RESULT_FILE,
    STATE_AWAITING_GATE,
    ControlPlaneError,
    DdControlPlane,
)
from fleet_graph.dd.service import (
    AUTO_RESUME_ENABLED_ENV,
    AUTO_RESUME_INTERVAL_ENV,
    DEFAULT_AUTO_RESUME_INTERVAL,
    GateAutoResumer,
    auto_resume_enabled_from_env,
    auto_resume_interval_from_env,
)
from fleet_graph.scheduler.launcher import LaunchResult

SPEC = """# SPEC: add a name parameter to greet()

Make `greet(name)` return a personalised greeting.

```dd-acceptance
python3 -m pytest -q
```
"""


class RecordingLauncher:
    """Stands in for TransientLauncher; records the specs it was handed and
    marks each launched unit active, so the probe sees what production sees."""

    dry_run = False

    def __init__(self, active: set[str] | None = None) -> None:
        self.specs: list[Any] = []
        self.active = active if active is not None else set()

    def launch(self, spec: Any) -> LaunchResult:
        self.specs.append(spec)
        self.active.add(spec.unit_name)
        return LaunchResult(spec.unit_name, True, "recorded")


class DecisionBoard:
    """decision_for only -- exactly what `_decision_on_board` reads."""

    def __init__(self) -> None:
        self.decision: Decision | None = None
        self.unreachable = False
        self.asked: list[GateTicket] = []

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        if self.unreachable:
            raise RuntimeError("bus unreachable")
        self.asked.append(ticket)
        return self.decision


def approve(card_entity_id: str = "ent-dd-card") -> Decision:
    return Decision(
        message_id="msg-decision-1",
        decision="APPROVE",
        decided_by="青林",
        question="",
        rationale="",
        card_entity_id=card_entity_id,
        raw={},
    )


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


def make_plane(
    tmp_path: Path,
    *,
    launcher: Any = None,
    unit_probe: Any = None,
    board: Any = None,
) -> DdControlPlane:
    binding = tmp_path / "plugin-binding.json"
    if not binding.exists():
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    launcher = launcher if launcher is not None else RecordingLauncher()
    if unit_probe is None:
        active = getattr(launcher, "active", set())
        unit_probe = lambda unit: unit in active  # noqa: E731
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher,
        unit_probe=unit_probe,
        board_factory=lambda: board,
        clock=lambda: 1_700_000_000.0,
    )


def suspend_at_gate(plane: DdControlPlane, scratch: Path) -> str:
    """A development parked at the human gate: awaiting note + durable checkpoint."""
    dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
    dev_root = plane.root / dev
    dev_root.mkdir(parents=True, exist_ok=True)
    (dev_root / RESULT_FILE).write_text(
        json.dumps(
            {
                "development_id": dev,
                "terminal": None,
                "stage": "human_gate",
                "head_commit": head(scratch),
                "awaiting": {
                    "question_note_id": "msg_question_1",
                    "card_entity_id": "ent-dd-card",
                },
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    (dev_root / CHECKPOINT_FILE).touch()
    return dev


class TestDecisionOnBoardTriggersResume:
    def test_awaiting_gate_with_a_decision_on_board_is_resumed(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        board = DecisionBoard()
        board.decision = approve()
        plane = make_plane(tmp_path, launcher=launcher, board=board)
        dev = suspend_at_gate(plane, scratch)

        summary = GateAutoResumer(plane).tick()

        assert summary["resumed"] == [dev]
        assert summary["errors"] == []
        # The launch is the gate's own resume path: valueless --resume, and
        # nothing in the argv can carry a verdict.
        assert len(launcher.specs) == 1
        argv = launcher.specs[0].argv()
        assert "--resume" in argv
        for forbidden in ("APPROVE", "REJECT", "--decision", "--verdict"):
            assert forbidden not in argv
        # The board question the patrol read is the awaiting ticket itself.
        assert board.asked[0].question_note_id == "msg_question_1"

    def test_a_resumed_development_is_not_resumed_again(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """幂等：launch 后 unit active，状态转 running，不再进扫描。"""
        launcher = RecordingLauncher()
        board = DecisionBoard()
        board.decision = approve()
        plane = make_plane(tmp_path, launcher=launcher, board=board)
        dev = suspend_at_gate(plane, scratch)

        resumer = GateAutoResumer(plane)
        assert resumer.tick()["resumed"] == [dev]
        again = resumer.tick()
        assert again["resumed"] == []
        assert again["scanned"] == 0
        assert len(launcher.specs) == 1


class TestNoDecisionMeansNoMotion:
    def test_awaiting_gate_without_a_decision_stays_suspended(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        board = DecisionBoard()  # decision stays None
        plane = make_plane(tmp_path, launcher=launcher, board=board)
        dev = suspend_at_gate(plane, scratch)

        summary = GateAutoResumer(plane).tick()

        assert summary["resumed"] == []
        assert launcher.specs == []
        assert {"development_id": dev, "reason": "no_decision_on_board"} in summary["skipped"]


class TestFailOpen:
    def test_an_unreachable_board_is_a_skip_not_a_crash(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """板不可达 → decision_on_board 为 None → 当作未落板跳过。"""
        launcher = RecordingLauncher()
        board = DecisionBoard()
        board.unreachable = True
        plane = make_plane(tmp_path, launcher=launcher, board=board)
        dev = suspend_at_gate(plane, scratch)

        summary = GateAutoResumer(plane).tick()

        assert summary["resumed"] == []
        assert launcher.specs == []
        assert {"development_id": dev, "reason": "no_decision_on_board"} in summary["skipped"]

    def test_a_scan_failure_skips_the_tick_without_raising(self, tmp_path: Path) -> None:
        class BrokenPlane:
            def list(self, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("artifacts unreadable")

        summary = GateAutoResumer(BrokenPlane()).tick()
        assert summary["resumed"] == []
        assert summary["errors"] and "artifacts unreadable" in summary["errors"][0]["error"]

    def test_one_broken_development_does_not_stop_the_patrol(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        board = DecisionBoard()
        board.decision = approve()
        plane = make_plane(tmp_path, launcher=launcher, board=board)
        healthy = suspend_at_gate(plane, scratch)

        class OneBadGate:
            """First id explodes in gate(); the rest go through the real plane."""

            def __init__(self, real: DdControlPlane) -> None:
                self.real = real

            def list(self, **kwargs: Any) -> dict[str, Any]:
                page = self.real.list(**kwargs)
                rows = list(page["developments"])
                rows.insert(0, {"development_id": "dev-fg-exploding", "state": "awaiting_gate"})
                return {"developments": rows, "cursor": page["cursor"]}

            def gate(self, development_id: str, resume: bool = False) -> dict[str, Any]:
                if development_id == "dev-fg-exploding":
                    raise RuntimeError("corrupt artifacts")
                return self.real.gate(development_id, resume=resume)

        summary = GateAutoResumer(OneBadGate(plane)).tick()
        assert summary["resumed"] == [healthy]
        assert any(e["development_id"] == "dev-fg-exploding" for e in summary["errors"])


class TestAlreadyRunningIsSkipped:
    def test_a_running_development_never_enters_the_scan(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        board = DecisionBoard()
        board.decision = approve()
        plane = make_plane(tmp_path, launcher=launcher, board=board)
        dev = suspend_at_gate(plane, scratch)
        # Something else already resumed it: its unit is active.
        started = plane.gate(dev, resume=True)
        assert started["resume"]["started"] is True

        summary = GateAutoResumer(plane).tick()
        assert summary["scanned"] == 0
        assert summary["resumed"] == []
        assert len(launcher.specs) == 1

    def test_the_start_race_lands_on_the_existing_already_running_refusal(self) -> None:
        """判定与启动之间被别人抢先 → gate(resume=True) 的既有 refuse 兜住。"""

        class RacingPlane:
            def list(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "developments": [{"development_id": "dev-fg-race", "state": "awaiting_gate"}],
                    "cursor": None,
                }

            def gate(self, development_id: str, resume: bool = False) -> dict[str, Any]:
                if resume:
                    raise ControlPlaneError(
                        "ALREADY_RUNNING", "dev-fg-race is running as fleet-graph-dd-race-r2"
                    )
                return {
                    "development_id": development_id,
                    "state": STATE_AWAITING_GATE,
                    "pending": True,
                    "decision_on_board": True,
                }

        summary = GateAutoResumer(RacingPlane()).tick()
        assert summary["resumed"] == []
        assert summary["errors"] == []
        assert {"development_id": "dev-fg-race", "reason": "ALREADY_RUNNING"} in summary["skipped"]


class TestConfiguration:
    def test_the_switch_defaults_on_and_only_explicit_negatives_turn_it_off(self) -> None:
        assert auto_resume_enabled_from_env({}) is True
        for word in ("0", "false", "no", "off", "False", " OFF "):
            assert auto_resume_enabled_from_env({AUTO_RESUME_ENABLED_ENV: word}) is False
        for word in ("1", "true", "on", "yes", ""):
            assert auto_resume_enabled_from_env({AUTO_RESUME_ENABLED_ENV: word}) is True

    def test_the_interval_defaults_to_sixty_and_rejects_nonsense(self) -> None:
        assert auto_resume_interval_from_env({}) == DEFAULT_AUTO_RESUME_INTERVAL
        assert auto_resume_interval_from_env({AUTO_RESUME_INTERVAL_ENV: "5"}) == 5.0
        for bad in ("-3", "0", "soon"):
            assert (
                auto_resume_interval_from_env({AUTO_RESUME_INTERVAL_ENV: bad})
                == DEFAULT_AUTO_RESUME_INTERVAL
            )

    def test_the_patrol_thread_starts_ticking_and_stops_on_request(self) -> None:
        ticked = threading.Event()

        class CountingPlane:
            def list(self, **kwargs: Any) -> dict[str, Any]:
                ticked.set()
                return {"developments": [], "cursor": None}

        resumer = GateAutoResumer(CountingPlane(), interval=0.01)
        thread = resumer.start()
        assert ticked.wait(timeout=5), "the patrol never ticked"
        assert thread.daemon, "the patrol must die with the service process"
        resumer.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

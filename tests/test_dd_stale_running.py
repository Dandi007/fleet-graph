"""Stale-running development recovery.

The control plane's liveness model is "a development is running iff its recorded
generation unit is live". A development can outlive that unit: its run is
killed (or the box reboots) after a `status.json` was written as ``running``,
leaving a record whose generation unit is now ``inactive/dead`` while the
persisted cache still says ``running``.

`development_start` must reconcile that contradiction. This file pins the full
contract (spec "Stale-running development recovery"):

1. the reproduction: a stale-running record (dead unit, persisted ``running``)
   is reconciled by the read side and no longer reports ``running``;
2. `start` does not report ``already_running`` for a dead unit -- it resumes
   the same generation in place, and it returns/persists recovery evidence;
3. an actually-active generation remains a no-op and never double-dispatches;
4. recovery is idempotent, creates no new development, and never re-dispatches
   a completed sealed stage (the relaunch carries ``--resume``);
5. the recovery is distinguishably recorded from a terminal resolution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    LAUNCHES_FILE,
    RECORD_FILE,
    STATUS_FILE,
    DdControlPlane,
)
from fleet_graph.scheduler.launcher import LaunchResult

SPEC = """# SPEC: recover a stale-running development

The unit died; the record says running; start must resume, not claim it is running.

```dd-acceptance
python3 -m pytest -q
```
"""


class RecordingLauncher:
    """Records the specs it is handed; marks launched units active so the probe
    sees what production's systemd sees, unless told otherwise."""

    dry_run = False

    def __init__(self) -> None:
        self.specs: list[Any] = []
        self.active: set[str] = set()
        self.started = True

    def launch(self, spec: Any) -> LaunchResult:
        self.specs.append(spec)
        if self.started:
            self.active.add(spec.unit_name)
        return LaunchResult(spec.unit_name, self.started, "recorded")


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
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
    binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher,
        unit_probe=lambda unit: unit in launcher.active,
        board_factory=lambda: None,
        clock=lambda: 1_700_000_000.0,
    )


def stale_running(
    plane: DdControlPlane, launcher: RecordingLauncher, scratch: Path
) -> tuple[str, str]:
    """A development launched, then killed: its recorded unit is dead while its
    persisted status cache still says ``running``."""
    dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
    first = plane.start(dev)
    dev_root = plane.root / dev
    (dev_root / CHECKPOINT_FILE).touch()
    # Persist the running cache, then let the unit die.
    (dev_root / STATUS_FILE).write_text(
        json.dumps(
            {
                "development_id": dev,
                "state": "running",
                "generation": 1,
                "stage": "implement",
                "terminal": "",
                "terminal_reason": "",
                "head_commit": head(scratch),
                "failure": None,
                "awaiting": None,
                "gate_refused": None,
                "active_unit": first["unit"],
                "launches": 1,
            }
        ),
        encoding="utf-8",
    )
    launcher.active.discard(first["unit"])
    return dev, first["unit"]


class TestReadSideReconciliation:
    def test_a_persisted_running_record_is_reconciled_once_the_unit_is_dead(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, _unit = stale_running(plane, launcher, scratch)

        status = plane.get(dev)
        assert status["state"] != "running"
        assert status["state"] == "interrupted"
        assert status["active_unit"] == ""

    def test_list_never_serves_the_stale_running_cache(self, scratch: Path, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, _unit = stale_running(plane, launcher, scratch)

        listed = plane.list(state="running")
        assert [row["development_id"] for row in listed["developments"]] != [dev]


class TestStartReconciliation:
    def test_start_recovers_a_dead_unit_instead_of_claiming_already_running(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, unit = stale_running(plane, launcher, scratch)

        resumed = plane.start(dev)

        assert resumed["already_running"] is False
        assert resumed["started"] is True
        assert resumed["mode"] == "resume"
        assert resumed["recovered"] is True
        assert resumed["generation"] == 1
        assert resumed["thread_id"] == f"{dev}:g1"
        assert resumed["unit"] != unit, "a fresh unit (the old one is still tearing down)"
        argv = launcher.specs[-1].argv()
        assert "--resume" in argv, "resume must not re-dispatch a sealed stage"
        assert argv[argv.index("--generation") + 1] == "1"

    def test_start_again_after_recovery_is_a_visible_noop(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, _unit = stale_running(plane, launcher, scratch)
        plane.start(dev)  # recovery: the resumed unit is now active

        again = plane.start(dev)
        assert again["already_running"] is True
        assert len(launcher.specs) == 2, "recovery launched exactly once, then adopted"

    def test_an_actually_active_generation_stays_a_noop(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)

        again = plane.start(dev)
        assert again["already_running"] is True
        assert len(launcher.specs) == 1, "an active unit must not duplicate dispatch"

    def test_recovery_creates_no_new_development(self, scratch: Path, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, _unit = stale_running(plane, launcher, scratch)
        plane.start(dev)

        listed = plane.list()
        assert [row["development_id"] for row in listed["developments"]] == [dev]

    def test_a_failed_launch_during_recovery_does_not_burn_a_generation(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """The launch entry is written before the launch result is checked for
        failure, so a dead unit that fails to relaunch must still keep its
        generation identity (no phantom bump, no re-dispatch)."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, _unit = stale_running(plane, launcher, scratch)
        launcher.started = False
        launcher.active.clear()

        from fleet_graph.dd.control_plane import ControlPlaneError

        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "LAUNCH_FAILED"
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        assert record.get("generation", 1) == 1


class TestRecoveryEvidence:
    def test_the_launch_record_carries_the_recovery_marker(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev, _unit = stale_running(plane, launcher, scratch)
        plane.start(dev)

        launches = [
            json.loads(line)
            for line in (plane.root / dev / LAUNCHES_FILE).read_text().splitlines()
            if line
        ]
        assert launches[0]["mode"] == "fresh"
        assert launches[0]["recovered"] is False
        assert launches[1]["mode"] == "resume"
        assert launches[1]["recovered"] is True

    def test_a_terminal_resolution_is_distinct_from_a_resumed_recovery(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """A completed development refuses to restart with a terminal state,
        and never carries the recovery marker -- the two outcomes are told apart
        by concrete state, not by inference."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)
        (plane.root / dev / CHECKPOINT_FILE).touch()
        from fleet_graph.dd.control_plane import RESULT_FILE

        (plane.root / dev / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": "complete",
                    "terminal_reason": "merger is the last declared stage",
                    "stage": "merger",
                    "head_commit": head(scratch),
                    "awaiting": None,
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        launcher.active.clear()

        from fleet_graph.dd.control_plane import ControlPlaneError

        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "DEVELOPMENT_COMPLETE"

"""入编写服务数据，已有 scheduler/read model 无需代码部署即可发现。"""

from pathlib import Path

import pytest

from fleet_graph.goal_enroll.runtime_roster import admit_line, runtime_entries
from fleet_graph.scheduler.daemon import Scheduler, SchedulerConfig
from test_goal_enroll import TestGoalAdmitSupervisorSurface


def test_admit_replay_repairs_roster_after_interrupted_write(tmp_path: Path):
    service, queue, _ = TestGoalAdmitSupervisorSurface()._service(tmp_path)
    service.submit("wf-1", "ronin-fresh")

    def interrupted(entry):
        raise OSError("写名册之前中断")

    service._roster_admitter = interrupted
    with pytest.raises(OSError):
        service.admit("wf-1", decided_by="supervisor")
    assert queue.get("wf-1")["status"] == "admitted"
    assert runtime_entries() == []
    service._roster_admitter = admit_line
    service.admit("wf-1", decided_by="supervisor")
    assert len(runtime_entries()) == 1
    assert service._roster.get("wf-1")["enabled"]


def test_running_scheduler_loads_new_admission_without_restart(tmp_path: Path):
    config = SchedulerConfig(lines=[], run_root=tmp_path / "runs")
    stop = tmp_path / "stop"
    stop.touch()
    config.maintenance_stop_path = stop
    scheduler = Scheduler(config)
    assert scheduler.tick() == []
    entry = {
        "folder_id": "wf-new",
        "alias": "new",
        "acceptance_digest": "sha256:test",
        "acceptance_argv": [["true"]],
        "seat_hint": "opencode-glm53",
    }
    admit_line(entry)
    result = scheduler.tick()
    assert len(result) == 1 and result[0].folder_id == "wf-new"
    assert not result[0].decision.ignite
    assert scheduler.config.lines[0].acceptance_digest == "sha256:test"


def test_alias_collision_does_not_overwrite_existing_admission():
    entry = {
        "folder_id": "wf-one",
        "alias": "same",
        "acceptance_digest": "x",
        "acceptance_argv": [["true"]],
    }
    admit_line(entry)
    with pytest.raises(ValueError):
        admit_line({**entry, "folder_id": "wf-two"})
    assert len(runtime_entries()) == 1


def test_seed_alias_cannot_be_reassigned_by_runtime_admission(tmp_path: Path, monkeypatch):
    import json

    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"lines": [{"folder_id": "wf-old", "alias": "owned"}]}))
    monkeypatch.setenv("FLEET_GRAPH_LINES_CONFIG", str(seed))
    with pytest.raises(ValueError, match="种子"):
        admit_line(
            {
                "folder_id": "wf-new",
                "alias": "owned",
                "acceptance_digest": "x",
                "acceptance_argv": [["true"]],
            }
        )
    assert runtime_entries() == []

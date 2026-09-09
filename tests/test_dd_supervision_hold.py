import json
from contextlib import nullcontext
import pytest
from fleet_graph.dd.control_plane import DdControlPlane, ControlPlaneError


def test_site_hold_blocks_only_target_mutations(tmp_path, monkeypatch):
    config = tmp_path / "holds.json"
    config.write_text(json.dumps({"held": "监督接管：待修复回执"}))
    monkeypatch.setenv("FLEET_GRAPH_DD_SUPERVISION_HOLDS_FILE", str(config))
    plane = DdControlPlane(root=tmp_path / "dd", board_factory=lambda: None)
    monkeypatch.setattr(plane, "operation_lock", lambda _: nullcontext())
    for action in (plane.start, plane.reconfigure):
        with pytest.raises(ControlPlaneError, match="监督接管"):
            action("held")
    plane._assert_supervision_control("unheld")
    # Merely checking the read side still reaches the ordinary record lookup.
    with pytest.raises(ControlPlaneError) as exc:
        plane.get("held")
    assert "SUPERVISION" not in str(exc.value)
    assert not (tmp_path / "dd" / "held").exists()
    config.write_text("{}")
    plane._assert_supervision_control("held")


def test_invalid_hold_configuration_fails_closed(tmp_path, monkeypatch):
    config = tmp_path / "holds.json"
    config.write_text("[]")
    monkeypatch.setenv("FLEET_GRAPH_DD_SUPERVISION_HOLDS_FILE", str(config))
    plane = DdControlPlane(root=tmp_path / "dd", board_factory=lambda: None)
    monkeypatch.setattr(plane, "operation_lock", lambda _: nullcontext())
    with pytest.raises(ControlPlaneError, match="expected development-id"):
        plane.start("held")

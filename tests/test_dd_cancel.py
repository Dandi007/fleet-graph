"""取消必须停止当前代并持久化事实，失败或过期请求不得伪造终态。"""

import json
import subprocess
from pathlib import Path

import pytest

from fleet_graph.dd.control_plane import RESULT_FILE, ControlPlaneError
from test_dd_control_plane import SPEC, make_plane
from test_dd_control_plane import scratch as scratch


@pytest.mark.usefixtures("scratch")
def test_cancel_is_durable_and_replay_does_not_add_event(tmp_path: Path, scratch: Path) -> None:
    plane = make_plane(tmp_path)
    dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
    first = plane.cancel(dev, generation=1, reason="样例已被替代")
    assert first["cancelled"]
    result = json.loads((plane.root / dev / RESULT_FILE).read_text())
    assert result["terminal_code"] == "CANCELLED"
    assert result["awaiting"] is None
    before = (plane.root / dev / "events.jsonl").read_bytes()
    assert plane.cancel(dev, generation=1, reason="重投")["already_terminal"]
    assert (plane.root / dev / "events.jsonl").read_bytes() == before


@pytest.mark.usefixtures("scratch")
def test_stale_cancel_does_not_stop_anything(tmp_path: Path, scratch: Path, monkeypatch) -> None:
    plane = make_plane(tmp_path)
    dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
    monkeypatch.setattr(plane, "_unit_active", lambda _: pytest.fail("过期请求不能查停进程"))
    with pytest.raises(ControlPlaneError, match="当前代") as exc:
        plane.cancel(dev, generation=2, reason="过期")
    assert exc.value.code == "GENERATION_MISMATCH"
    assert not (plane.root / dev / RESULT_FILE).exists()


@pytest.mark.usefixtures("scratch")
def test_failed_stop_does_not_report_cancelled(tmp_path: Path, scratch: Path, monkeypatch) -> None:
    plane = make_plane(tmp_path)
    dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
    monkeypatch.setattr(plane, "_unit_active", lambda _: "fixture-dd")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "stop failed")
    )
    with pytest.raises(ControlPlaneError) as exc:
        plane.cancel(dev, generation=1, reason="停止")
    assert exc.value.code == "CANCEL_FAILED"
    assert not (plane.root / dev / RESULT_FILE).exists()

"""Step 7: line set-seat override surface -- C1..C4, end to end.

Covers the frozen-scope acceptance:

1. set-seat operation writes a C1-complete override to the scheduler's
   persistent surface and the next scheduler launch cold-starts on the
   override seat as a new generation.
2. C1: an override must carry who/when/from→to/reason; missing any field
   refuses the write.
3. C2: an override that equals the roster seat is reconciled away.
4. C3: reconcile/lint lists every roster ≠ effective override loudly (with
   the diff facts); zero drift exits clean.
5. C4: line status carries the roster/override/effective triple, and the
   pre-switch probe refuses a switch onto a seat that is not healthy.
6. Regression: the shipped roster SSoT read/validation path is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.cli import perform_set_seat
from fleet_graph.scheduler.daemon import (
    LineSpec,
    Scheduler,
    SchedulerConfig,
    bump_line_generation,
)
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.probe import UnknownSeat
from fleet_graph.scheduler.seat_override import (
    SeatOverride,
    SeatOverrideStore,
    effective_seat,
    render_drift_line,
    validate_override,
)
from fleet_graph.state.run_artifacts import iso


def make_store(tmp_path: Path, run_root: str = "runs") -> SeatOverrideStore:
    return SeatOverrideStore(tmp_path / run_root)


def make_override(
    *,
    folder_id: str = "wf-9b5931",
    who: str = "alice",
    when: str = "2026-08-29T12:00:00Z",
    from_seat: str = "opencode-dsv4pro",
    to: str = "opencode-gpt-terra",
    reason: str = "subscription lane died",
) -> SeatOverride:
    return validate_override(
        {
            "folder_id": folder_id,
            "who": who,
            "when": when,
            "from": from_seat,
            "to": to,
            "reason": reason,
        }
    )


def write_roster(tmp_path: Path, *, run_root: str = "runs") -> Path:
    path = tmp_path / "lines.json"
    path.write_text(
        json.dumps(
            {
                "run_root": f"{tmp_path}/{run_root}",
                "lines": [
                    {
                        "folder_id": "wf-9b5931",
                        "seat": "opencode-dsv4pro",
                        "alias": "ronin-model-switch",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeProber:
    def __init__(self, healthy: bool | Exception = True) -> None:
        self.healthy = healthy
        self.asked: list[str] = []

    def check(self, seat: str) -> bool:
        self.asked.append(seat)
        if isinstance(self.healthy, Exception):
            raise self.healthy
        return self.healthy


class FakeLauncher:
    def __init__(self, started: bool = True) -> None:
        self.started = started
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, self.started, "")


class FakeUnits:
    def __init__(self, active: set[str] | None = None) -> None:
        self.active = active or set()

    def is_active(self, unit_name: str) -> bool:
        return unit_name in self.active


def make_scheduler(
    tmp_path: Path, *, store: SeatOverrideStore | None = None, prober: Any = None
) -> Scheduler:
    run_root = tmp_path / "runs"
    return Scheduler(
        SchedulerConfig(
            lines=[LineSpec(folder_id="wf-9b5931", seat="opencode-dsv4pro", enabled=True)],
            run_root=run_root,
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber() if prober is None else prober,
        launcher=FakeLauncher(),
        units=FakeUnits(),
        clock=lambda: 1000.0,
        sleep=lambda _s: None,
        seat_overrides=store or SeatOverrideStore(run_root),
    )


# --- C1: audit fields -----------------------------------------------------


class TestC1AuditFields:
    def test_a_complete_record_validates_and_round_trips(self) -> None:
        override = make_override()
        assert override.who == "alice"
        assert override.when == "2026-08-29T12:00:00Z"
        assert override.from_seat == "opencode-dsv4pro"
        assert override.to == "opencode-gpt-terra"
        assert override.reason == "subscription lane died"
        assert override.from_to == "opencode-dsv4pro -> opencode-gpt-terra"
        assert validate_override(override.as_dict()) == override

    @pytest.mark.parametrize("field", ["who", "when", "from", "to", "reason"])
    def test_missing_any_one_field_refuses_the_write(self, field: str) -> None:
        record = {
            "folder_id": "wf-9b5931",
            "who": "alice",
            "when": "2026-08-29T12:00:00Z",
            "from": "opencode-dsv4pro",
            "to": "opencode-gpt-terra",
            "reason": "lane died",
        }
        del record[field]
        with pytest.raises(ValueError):
            validate_override(record)

    @pytest.mark.parametrize("field", ["who", "when", "from", "to", "reason"])
    def test_blanking_any_one_field_refuses_the_write(self, field: str) -> None:
        record = {
            "folder_id": "wf-9b5931",
            "who": "alice",
            "when": "2026-08-29T12:00:00Z",
            "from": "opencode-dsv4pro",
            "to": "opencode-gpt-terra",
            "reason": "lane died",
        }
        record[field] = "   "
        with pytest.raises(ValueError):
            validate_override(record)

    def test_a_no_op_switch_is_refused(self) -> None:
        record = {
            "folder_id": "wf-9b5931",
            "who": "alice",
            "when": "2026-08-29T12:00:00Z",
            "from": "opencode-dsv4pro",
            "to": "opencode-dsv4pro",
            "reason": "no-op",
        }
        with pytest.raises(ValueError, match="same seat"):
            validate_override(record)

    def test_a_missing_folder_id_is_refused(self) -> None:
        record = make_override().as_dict()
        record["folder_id"] = ""
        with pytest.raises(ValueError):
            validate_override(record)


# --- store: persistent surface --------------------------------------------


class TestStorePersistence:
    def test_write_lands_the_record_on_the_persistent_surface(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.write(make_override())
        surface = tmp_path / "runs" / ".scheduler" / "seat-overrides.json"
        assert surface.is_file()
        loaded = json.loads(surface.read_text(encoding="utf-8"))
        assert loaded["wf-9b5931"]["who"] == "alice"
        assert loaded["wf-9b5931"]["when"] == "2026-08-29T12:00:00Z"
        assert loaded["wf-9b5931"]["from"] == "opencode-dsv4pro"
        assert loaded["wf-9b5931"]["to"] == "opencode-gpt-terra"
        assert loaded["wf-9b5931"]["reason"] == "subscription lane died"

    def test_load_get_and_clear(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        override = make_override()
        store.write(override)
        assert store.load() == {"wf-9b5931": override}
        assert store.get("wf-9b5931") == override
        store.clear("wf-9b5931")
        assert store.get("wf-9b5931") is None
        assert store.load() == {}

    def test_a_malformed_surface_reads_as_empty_not_a_crash(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        (tmp_path / "runs" / ".scheduler").mkdir(parents=True)
        (tmp_path / "runs" / ".scheduler" / "seat-overrides.json").write_text("not json")
        assert store.load() == {}

    def test_never_touches_the_roster(self, tmp_path: Path) -> None:
        roster = write_roster(tmp_path)
        roster_bytes = roster.read_bytes()
        store = make_store(tmp_path)
        store.write(make_override())
        assert roster.read_bytes() == roster_bytes


# --- C2: temporary semantics ----------------------------------------------


class TestC2OverrideIsTemporary:
    def test_reconcile_clears_an_override_that_equals_the_roster(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        roster = SchedulerConfig.from_json(write_roster(tmp_path))
        store.write(make_override(from_seat="opencode-gpt-terra", to=roster.lines[0].seat))
        result = store.reconcile(lambda folder: roster.lines[0].seat)
        assert [o.folder_id for o in result.cleared] == ["wf-9b5931"]
        assert result.drifting == []
        assert store.load() == {}

    def test_reconcile_keeps_a_switch_that_still_differs(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        roster = {
            line.folder_id: line.seat
            for line in SchedulerConfig.from_json(write_roster(tmp_path)).lines
        }
        store.write(make_override(to="opencode-gpt-terra"))
        result = store.reconcile(lambda folder: roster.get(folder))
        assert result.cleared == []
        assert [(f, o.to, r) for f, o, r in result.drifting] == [
            ("wf-9b5931", "opencode-gpt-terra", "opencode-dsv4pro")
        ]
        assert store.get("wf-9b5931") is not None

    def test_an_override_for_an_unknown_folder_is_reported_as_drift(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.write(make_override(folder_id="wf-ghost"))
        result = store.reconcile(lambda folder: None)
        assert result.drifting == [("wf-ghost", store.get("wf-ghost"), "")]

    def test_zero_drift_is_a_clean_reconcile(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        result = store.reconcile(lambda folder: "opencode-dsv4pro")
        assert result.cleared == []
        assert result.drifting == []
        assert result.drift_count == 0


# --- C3: reconcile/lint drift listing -------------------------------------


class TestC3DriftIsLoud:
    def test_drift_line_carries_the_diff_facts(self) -> None:
        override = make_override()
        line = render_drift_line("wf-9b5931", override, "opencode-dsv4pro")
        assert "roster=opencode-dsv4pro" in line
        assert "effective=opencode-gpt-terra" in line
        assert "opencode-dsv4pro -> opencode-gpt-terra" in line

    def test_drift_listing_has_no_roster_side_for_an_unknown_folder(self) -> None:
        override = make_override(folder_id="wf-ghost")
        line = render_drift_line("wf-ghost", override, "")
        assert "no roster entry" in line

    def test_scheduler_startup_prints_the_drift_loudly(self, tmp_path: Path, capsys) -> None:
        store = make_store(tmp_path)
        store.write(make_override())
        scheduler = make_scheduler(tmp_path, store=store)
        scheduler.tick()
        err = capsys.readouterr().err
        assert "seat override drift" in err
        assert "wf-9b5931" in err
        assert "effective=opencode-gpt-terra" in err

    def test_scheduler_is_silent_when_there_is_no_drift(self, tmp_path: Path, capsys) -> None:
        scheduler = make_scheduler(tmp_path)
        scheduler.tick()
        assert "seat override drift" not in capsys.readouterr().err


# --- C4: effective seat and the triple -------------------------------------


class TestC4TripleObservability:
    def test_effective_seat_prefers_the_override(self) -> None:
        assert effective_seat("opencode-dsv4pro", None) == "opencode-dsv4pro"
        assert effective_seat("opencode-dsv4pro", make_override()) == "opencode-gpt-terra"

    def test_tick_result_carries_the_triple(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.write(make_override())
        record = make_scheduler(tmp_path, store=store).tick()[0].as_dict()
        assert record["seat_roster"] == "opencode-dsv4pro"
        assert record["seat_override"] == "opencode-gpt-terra"
        assert record["seat_effective"] == "opencode-gpt-terra"

    def test_tick_result_without_an_override_reports_the_roster_seat(self, tmp_path: Path) -> None:
        record = make_scheduler(tmp_path).tick()[0].as_dict()
        assert record["seat_roster"] == "opencode-dsv4pro"
        assert record["seat_override"] is None
        assert record["seat_effective"] == "opencode-dsv4pro"

    def test_the_gateway_probe_checks_the_effective_seat(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.write(make_override())
        prober = FakeProber(healthy=False)
        scheduler = make_scheduler(tmp_path, store=store, prober=prober)
        scheduler.tick()
        assert prober.asked == ["opencode-gpt-terra"]
        assert scheduler.tick()[0].decision.refusal is Refusal.GATEWAY_RED

    def test_the_launch_uses_the_effective_seat(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.write(make_override())
        launcher = FakeLauncher()
        scheduler = make_scheduler(tmp_path, store=store)
        scheduler.launcher = launcher
        scheduler.tick()
        assert launcher.launched[0].seat == "opencode-gpt-terra"


# --- set-seat operation ----------------------------------------------------


class TestSetSeatOperation:
    def test_set_seat_writes_the_override_and_bumps_the_generation(self, tmp_path: Path) -> None:
        roster = write_roster(tmp_path)
        result = perform_set_seat(
            folder_id="wf-9b5931",
            to_seat="opencode-gpt-terra",
            reason="lane died",
            who="alice",
            lines_config=roster,
            prober=FakeProber(healthy=True),
            clock=lambda: 1724_921_664.0,
        )
        assert result["who"] == "alice"
        assert result["when"] == iso(1724_921_664.0)
        assert result["from"] == "opencode-dsv4pro"
        assert result["to"] == "opencode-gpt-terra"
        assert result["reason"] == "lane died"
        assert result["generation"] == 2
        store = SeatOverrideStore(tmp_path / "runs")
        assert store.get("wf-9b5931").to == "opencode-gpt-terra"

    def test_set_seat_from_an_existing_override_records_that_seat_as_from(
        self, tmp_path: Path
    ) -> None:
        store = make_store(tmp_path)
        store.write(make_override())
        result = perform_set_seat(
            folder_id="wf-9b5931",
            to_seat="opencode-glm53",
            reason="rebalance",
            who="alice",
            lines_config=write_roster(tmp_path),
            prober=FakeProber(healthy=True),
        )
        assert result["from"] == "opencode-gpt-terra"

    def test_a_red_probe_refuses_the_switch_and_writes_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="probe red"):
            perform_set_seat(
                folder_id="wf-9b5931",
                to_seat="opencode-gpt-terra",
                reason="lane died",
                who="alice",
                lines_config=write_roster(tmp_path),
                prober=FakeProber(healthy=False),
            )
        assert make_store(tmp_path).load() == {}

    def test_an_unanswerable_probe_refuses_the_switch(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="could not be run"):
            perform_set_seat(
                folder_id="wf-9b5931",
                to_seat="opencode-gpt-terra",
                reason="lane died",
                who="alice",
                lines_config=write_roster(tmp_path),
                prober=FakeProber(healthy=UnknownSeat("no probe for 'wat'")),
            )

    def test_a_no_op_switch_is_refused_before_any_probe(self, tmp_path: Path) -> None:
        prober = FakeProber(healthy=True)
        with pytest.raises(SystemExit, match="already runs on seat"):
            perform_set_seat(
                folder_id="wf-9b5931",
                to_seat="opencode-dsv4pro",
                reason="no change",
                who="alice",
                lines_config=write_roster(tmp_path),
                prober=prober,
            )
        assert prober.asked == []

    def test_a_folder_outside_the_roster_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="not in the roster"):
            perform_set_seat(
                folder_id="wf-unknown",
                to_seat="opencode-gpt-terra",
                reason="lane died",
                who="alice",
                lines_config=write_roster(tmp_path),
                prober=FakeProber(healthy=True),
            )

    def test_a_missing_reason_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="--reason"):
            perform_set_seat(
                folder_id="wf-9b5931",
                to_seat="opencode-gpt-terra",
                reason="",
                who="alice",
                lines_config=write_roster(tmp_path),
                prober=FakeProber(healthy=True),
            )

    def test_probe_can_be_disabled_for_drills(self, tmp_path: Path) -> None:
        result = perform_set_seat(
            folder_id="wf-9b5931",
            to_seat="opencode-gpt-terra",
            reason="drill",
            who="alice",
            lines_config=write_roster(tmp_path),
            prober=FakeProber(healthy=False),
            probe_enabled=False,
        )
        assert result["to"] == "opencode-gpt-terra"


# --- end to end: new generation cold-start on the override seat ------------


class TestNewGenerationColdStart:
    def test_the_next_scheduler_launch_is_a_fresh_generation_on_the_override_seat(
        self, tmp_path: Path
    ) -> None:
        store = make_store(tmp_path)
        roster = write_roster(tmp_path)
        launcher = FakeLauncher()
        scheduler = make_scheduler(tmp_path, store=store)
        scheduler.launcher = launcher
        line = scheduler.config.lines[0]

        assert scheduler.generation_of(line) == 1
        perform_set_seat(
            folder_id="wf-9b5931",
            to_seat="opencode-gpt-terra",
            reason="lane died",
            who="alice",
            lines_config=roster,
            prober=FakeProber(healthy=True),
        )
        scheduler.tick()
        launched = launcher.launched[0]
        assert launched.seat == "opencode-gpt-terra"
        assert launched.generation == 2

    def test_bump_line_generation_persists_across_a_store_read(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        assert bump_line_generation(run_root, "wf-9b5931", base_generation=1) == 2
        assert bump_line_generation(run_root, "wf-9b5931", base_generation=1) == 3

    def test_a_converged_override_is_folded_at_tick(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.write(make_override(from_seat="opencode-gpt-terra", to="opencode-dsv4pro"))
        scheduler = make_scheduler(tmp_path, store=store)
        scheduler.tick()
        assert store.load() == {}

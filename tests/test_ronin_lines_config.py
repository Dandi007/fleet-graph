"""The shipped ronin line config, checked against the loader that will read it.

A config file that no test loads is a config file that can rot silently: the
first thing to notice would be a scheduler that refuses to start.
"""

from __future__ import annotations

import json
from pathlib import Path

from fleet_graph.scheduler.daemon import SchedulerConfig

CONFIG = Path(__file__).resolve().parent.parent / "config" / "ronin-lines.json"

# The lines babysitter v28 carried that were still live when the fleet stopped
# (last terminal=killed at the maintenance-stop moment), minus the three whose
# subject is the engine that P4 retired. See wf-3f30cd findings §32.
MIGRATED = {
    "wf-5664e5",
    "wf-386b2f",
    "wf-7bc4d1",
    "wf-6475fd",
    "wf-9b5931",
    "wf-541832",
    "wf-3f87f3",
    "wf-40fa8d",
}


class TestTheShippedConfigLoads:
    def test_the_real_loader_accepts_it(self) -> None:
        config = SchedulerConfig.from_json(CONFIG)
        assert {line.folder_id for line in config.lines} == MIGRATED

    def test_every_line_names_a_seat_and_an_alias(self) -> None:
        for line in SchedulerConfig.from_json(CONFIG).lines:
            assert line.seat, line.folder_id
            assert line.alias, line.folder_id

    def test_the_maintenance_gate_is_the_fleet_wide_one(self) -> None:
        """Not a private flag: the gate that stopped the old fleet must be the
        same file, or a stop would only stop half the fleet."""
        config = SchedulerConfig.from_json(CONFIG)
        assert str(config.maintenance_stop_path) == "/data/ronin/maintenance-stop"

    def test_no_line_carries_the_retired_mcp(self) -> None:
        """babysitter passed --session-mcp-allow loop-engine-development to
        every line. That MCP is retired (P4); a migrated config that still
        named it would hand every seat a dead tool."""
        assert "loop-engine-development" not in CONFIG.read_text(encoding="utf-8")

    def test_the_bounds_are_the_ones_the_old_pump_ran_with(self) -> None:
        """9999, not the LineSpec default of 10. Migration is equivalence, and
        silently tightening a bound would end lines that used to keep going."""
        for line in SchedulerConfig.from_json(CONFIG).lines:
            assert line.max_rounds == 9999, line.folder_id

    def test_folder_ids_are_well_formed(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        for entry in raw["lines"]:
            assert entry["folder_id"].startswith("wf-")
            assert len(entry["folder_id"]) == 9

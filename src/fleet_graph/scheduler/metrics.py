"""Prometheus textfile metrics for the enabled line roster."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LineLiveness:
    folder_id: str
    unit_name: str
    active: bool


class TextfileMetrics:
    """Replace the full scrape on each tick so retired labels cannot linger."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, lines: list[LineLiveness]) -> None:
        records = [
            "# HELP fleet_graph_line_unit_active Whether an enabled line unit is active.",
            "# TYPE fleet_graph_line_unit_active gauge",
        ]
        for line in lines:
            labels = f'folder_id="{_escape(line.folder_id)}",unit="{_escape(line.unit_name)}"'
            records.append(f"fleet_graph_line_unit_active{{{labels}}} {int(line.active)}")
        payload = "\n".join(records) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, self.path)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


__all__ = ["LineLiveness", "TextfileMetrics"]

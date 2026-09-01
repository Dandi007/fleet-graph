"""Line-level metric counters for the worker turn report protocol path (D3).

Before this single, a malformed worker turn report was only discoverable by
grepping line logs: the alerting surface stayed silent on all 25 of them across
the fleet. This module is the observer side -- two counters, labelled by line
and ``exc.kind``, rendered to Prometheus text exposition so the fleet can
actually alert:

- ``fleet_graph_worker_report_protocol_failures_total{line, kind}`` -- every
  time a worker turn report fails the v1 protocol.
- ``fleet_graph_worker_report_protocol_recovered_total{line, kind}`` -- of
  those, the ones the bounded re-ask (D1) recovered within the same round.

The producer is ``graphs/goal_line.py``'s ``worker_turn``; this class is the
thin recording surface it talks to through ``LineDeps.metrics``. The exposition
wire format is the same one ``cost_obs`` uses, so a node_exporter textfile
directory configured via ``FLEET_GRAPH_LINE_METRICS_DIR`` (or the line's own
``metrics_dir``) re-exposes the same ``*.prom`` files the fleet already scrapes.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from fleet_graph.cost_obs.exposition import Sample, render

WORKER_REPORT_PROTOCOL_FAILURES_METRIC = "fleet_graph_worker_report_protocol_failures_total"
WORKER_REPORT_PROTOCOL_RECOVERED_METRIC = "fleet_graph_worker_report_protocol_recovered_total"

#: The environment variable naming the shared node_exporter textfile directory.
#: An explicit empty string means "not configured" -- a line then declines
#: collection rather than inventing a directory.
LINE_METRICS_DIR_ENV = "FLEET_GRAPH_LINE_METRICS_DIR"


def line_metrics_exposition_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """The textfile directory to render into, or ``None`` when not configured.

    An explicit empty string means "not configured", so a line that is not wired
    to a scrape path declines collection rather than inventing a directory.
    """
    configured = (env if env is not None else os.environ).get(LINE_METRICS_DIR_ENV, "")
    return Path(configured) if configured else None


def line_metrics_filename(folder_id: str) -> str:
    """The per-line exposition filename in the shared textfile directory.

    node_exporter re-exposes every ``*.prom`` file under the textfile directory,
    so each line writes its own file rather than overwriting a single fixed one.
    That is what lets ``sum(...)`` accumulate across the fleet instead of showing
    only the most recent line's counts.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", folder_id.strip())
    return f"line-metrics-{safe}.prom"


@dataclass
class LineMetrics:
    """Two line-level counters, keyed by (line, ``exc.kind``).

    `folder_id` is the line this recorder belongs to; every recorded sample is
    tagged with it so per-line series stay distinct in the shared textfile
    directory. ``exposition_dir``/``exposition_filename`` are where
    ``write_exposition`` drops the render; both default to ``None``/per-line
    filename when not given.
    """

    folder_id: str
    exposition_dir: Path | None = None
    exposition_filename: str | None = None

    _failures: dict[tuple[str, str], int] = field(default_factory=dict)
    _recovered: dict[tuple[str, str], int] = field(default_factory=dict)

    def record_worker_report_protocol_failure(self, line: str, kind: str) -> None:
        key = (line, kind)
        self._failures[key] = self._failures.get(key, 0) + 1

    def record_worker_report_protocol_recovered(self, line: str, kind: str) -> None:
        key = (line, kind)
        self._recovered[key] = self._recovered.get(key, 0) + 1

    def samples(self) -> list[Sample]:
        # Labels are emitted in canonical (sorted) key order so a render ->
        # parse roundtrip reproduces the same Sample objects.
        out: list[Sample] = []
        for (line, kind), count in sorted(self._failures.items()):
            out.append(
                Sample(
                    name=WORKER_REPORT_PROTOCOL_FAILURES_METRIC,
                    labels=(("kind", kind), ("line", line)),
                    value=float(count),
                )
            )
        for (line, kind), count in sorted(self._recovered.items()):
            out.append(
                Sample(
                    name=WORKER_REPORT_PROTOCOL_RECOVERED_METRIC,
                    labels=(("kind", kind), ("line", line)),
                    value=float(count),
                )
            )
        return out

    def render(self) -> str:
        return render(self.samples())

    def write_exposition(self, filename: str | None = None) -> Path:
        """Atomically render the counters to this line's textfile and return it.

        The write is atomic (render to a temp file, then rename into place), so
        a concurrent node_exporter scrape can never read a half-written textfile.
        """
        if self.exposition_dir is None:
            raise ValueError("no exposition_dir set; nothing to write to")
        filename = filename or self.exposition_filename or line_metrics_filename(self.folder_id)
        target = self.exposition_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=str(target.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.render())
        try:
            os.replace(tmp, target)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        return target


__all__ = [
    "LINE_METRICS_DIR_ENV",
    "WORKER_REPORT_PROTOCOL_FAILURES_METRIC",
    "WORKER_REPORT_PROTOCOL_RECOVERED_METRIC",
    "LineMetrics",
    "line_metrics_exposition_dir",
    "line_metrics_filename",
]

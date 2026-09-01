"""The D3 worker-report protocol counters: line + kind labelled, scrapable.

Before this single, a malformed worker turn report was only discoverable by
grepping line logs. This module pins the two counters -- failures and
recovered-by-re-ask -- to the Prometheus exposition wire format the fleet
already scrapes, and the per-line textfile filename that lets ``sum(...)``
accumulate across lines in a shared node_exporter directory.
"""

from __future__ import annotations

from fleet_graph.cost_obs.exposition import Sample, parse
from fleet_graph.state.line_metrics import (
    LINE_METRICS_DIR_ENV,
    WORKER_REPORT_PROTOCOL_FAILURES_METRIC,
    WORKER_REPORT_PROTOCOL_RECOVERED_METRIC,
    LineMetrics,
    line_metrics_exposition_dir,
    line_metrics_filename,
)


def sample(name: str, labels: tuple[tuple[str, str], ...], value: float) -> Sample:
    return Sample(name=name, labels=labels, value=value)


class TestRecordAndRender:
    def test_failure_and_recovered_are_distinct_labelled_samples(self) -> None:
        m = LineMetrics(folder_id="wf-3f30cd")
        m.record_worker_report_protocol_failure("wf-3f30cd", "malformed")
        m.record_worker_report_protocol_failure("wf-3f30cd", "truncated")
        m.record_worker_report_protocol_failure("wf-3f30cd", "malformed")
        m.record_worker_report_protocol_recovered("wf-3f30cd", "malformed")
        samples = m.samples()
        assert {s.name for s in samples} == {
            WORKER_REPORT_PROTOCOL_FAILURES_METRIC,
            WORKER_REPORT_PROTOCOL_RECOVERED_METRIC,
        }
        assert samples == [
            sample(
                WORKER_REPORT_PROTOCOL_FAILURES_METRIC,
                (("kind", "malformed"), ("line", "wf-3f30cd")),
                2.0,
            ),
            sample(
                WORKER_REPORT_PROTOCOL_FAILURES_METRIC,
                (("kind", "truncated"), ("line", "wf-3f30cd")),
                1.0,
            ),
            sample(
                WORKER_REPORT_PROTOCOL_RECOVERED_METRIC,
                (("kind", "malformed"), ("line", "wf-3f30cd")),
                1.0,
            ),
        ]

    def test_render_roundtrips_through_the_exposition_parser(self) -> None:
        m = LineMetrics(folder_id="wf-3f30cd")
        m.record_worker_report_protocol_failure("wf-3f30cd", "truncated")
        m.record_worker_report_protocol_recovered("wf-3f30cd", "truncated")
        assert parse(m.render()) == m.samples()

    def test_empty_recorder_renders_nothing(self) -> None:
        assert LineMetrics(folder_id="wf-x").render() == ""


class TestExpositionFile:
    def test_per_line_filename_is_scrape_safe(self) -> None:
        assert line_metrics_filename("wf-3f30cd") == "line-metrics-wf-3f30cd.prom"
        assert line_metrics_filename("wf 3f30cd:g1") == "line-metrics-wf-3f30cd-g1.prom"

    def test_write_exposition_is_atomic_and_readable(self, tmp_path) -> None:
        m = LineMetrics(folder_id="wf-3f30cd", exposition_dir=tmp_path)
        m.record_worker_report_protocol_failure("wf-3f30cd", "malformed")
        target = m.write_exposition()
        assert target.name == "line-metrics-wf-3f30cd.prom"
        assert parse(target.read_text(encoding="utf-8")) == m.samples()

    def test_write_exposition_requires_a_directory(self) -> None:
        m = LineMetrics(folder_id="wf-3f30cd")
        try:
            m.write_exposition()
        except ValueError:
            return
        raise AssertionError("write_exposition without exposition_dir must raise")


class TestDirectoryResolution:
    def test_env_var_names_the_textfile_dir(self, monkeypatch) -> None:
        monkeypatch.setenv(LINE_METRICS_DIR_ENV, "/var/lib/node_exporter/textfile")
        assert line_metrics_exposition_dir() is not None
        assert str(line_metrics_exposition_dir()) == "/var/lib/node_exporter/textfile"

    def test_unset_or_empty_env_declines_collection(self, monkeypatch) -> None:
        monkeypatch.delenv(LINE_METRICS_DIR_ENV, raising=False)
        assert line_metrics_exposition_dir() is None
        monkeypatch.setenv(LINE_METRICS_DIR_ENV, "")
        assert line_metrics_exposition_dir() is None

    def test_explicit_env_wins_over_the_process_environment(self) -> None:
        from fleet_graph.state.line_metrics import LINE_METRICS_DIR_ENV

        resolved = line_metrics_exposition_dir({LINE_METRICS_DIR_ENV: "/tmp/prom"})
        assert str(resolved) == "/tmp/prom"


class TestLifecycleWiring:
    """D3's effect side lives in the runner, not the graph: the counters are
    recorded in memory during worker_turn, and run_line/resume_goal_line render
    them to the line's textfile once the run is over. Before the rework these
    were discarded at process exit and never reached node_exporter."""

    def test_build_line_wires_the_recorder_when_a_metrics_dir_is_given(self, tmp_path) -> None:
        from fleet_graph.graphs.runner import LineConfig, build_line

        prom_dir = tmp_path / "textfile"
        config = LineConfig(folder_id="wf-wired", seat="s", run_root=tmp_path, metrics_dir=prom_dir)
        _, deps = build_line(config)
        assert deps.metrics is not None
        assert deps.metrics.exposition_dir == prom_dir

    def test_build_line_stays_silent_without_a_metrics_dir(self, tmp_path) -> None:
        from fleet_graph.graphs.runner import LineConfig, build_line

        config = LineConfig(folder_id="wf-unwired", seat="s", run_root=tmp_path)
        _, deps = build_line(config)
        assert deps.metrics is None

    def test_flush_line_metrics_renders_the_recorded_counters_to_disk(self, tmp_path) -> None:
        from fleet_graph.graphs.goal_line import LineDeps
        from fleet_graph.graphs.runner import _flush_line_metrics

        metrics = LineMetrics(folder_id="wf-3f30cd", exposition_dir=tmp_path)
        metrics.record_worker_report_protocol_failure("wf-3f30cd", "malformed")
        metrics.record_worker_report_protocol_recovered("wf-3f30cd", "malformed")

        class _Coordinator:
            def turn(self, round_no, coord_input):
                return {"verdict": "done"}

        class _Worker:
            def turn(self, prompt, round_no):
                return {}

        class _Inbox:
            def drain_then_ack(self, persist):
                persist([])
                return [], []

        class _Artifacts:
            def heartbeat(self, *args, **kwargs):
                return True

            def append_round(self, line):
                return True

            def write_worker_report(self, round_no, report):
                return "worker-report.json"

            def write_terminal(self, **kwargs):
                return "terminal.json"

        deps = LineDeps(
            coordinator=_Coordinator(),
            worker=_Worker(),
            inbox=_Inbox(),
            artifacts=_Artifacts(),
            folder_id="wf-3f30cd",
            metrics=metrics,
        )
        _flush_line_metrics(deps)

        target = tmp_path / line_metrics_filename("wf-3f30cd")
        assert target.exists()
        assert parse(target.read_text(encoding="utf-8")) == metrics.samples()

"""Focused tests for the cost-observability data plane.

Each test pins one of the correctness risks the spec names -- duplicate
events, ordering, missing labels, PromQL vector matching, and the
unknown-vs-missing distinction -- rather than exercising the happy path a
second time (the acceptance fixture owns the end-to-end cycle).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from fleet_graph.cost_obs import (
    RECORDING_RULES,
    CostDataPlane,
    PromQLError,
    Sample,
    TokenRecord,
    classify_tokens,
    parse,
    query,
    render,
)


def scalar(expr: str, samples: list[Sample]) -> float:
    result = query(expr, samples)
    assert len(result) == 1
    return result[0].value


class TestExposition:
    def test_render_parse_roundtrip(self) -> None:
        samples = [
            Sample("cost_obs_execution_cost_total", (("attribution", "unknown"),), 7.0),
            Sample("cost_obs_launch_total", (("order_id", "order-1"), ("seat", "terra")), 1.0),
        ]
        parsed = parse(render(samples))
        assert parsed == samples

    def test_label_values_are_escaped_and_unescaped(self) -> None:
        samples = [Sample("m", (("label", 'a"b\nc\\d'),), 1.0)]
        text = render(samples)
        assert 'a\\"b\\nc\\\\d' in text
        assert parse(text) == samples

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        text = '# HELP m comment\n\nm{a="1"} 2'
        parsed = parse(text)
        assert parsed == [Sample("m", (("a", "1"),), 2.0)]


class TestClassifier:
    def test_known_and_unknown_are_separate(self) -> None:
        buckets = classify_tokens(
            [
                TokenRecord(tokens=10, attribution="management"),
                TokenRecord(tokens=20, attribution="launch"),
                TokenRecord(tokens=7, attribution=None),
                TokenRecord(tokens=3, attribution="not-a-class"),
            ]
        )
        assert buckets["management"] == 10.0
        assert buckets["launch"] == 20.0
        assert buckets["unknown"] == 10.0

    def test_every_known_class_is_always_present(self) -> None:
        buckets = classify_tokens([])
        assert set(buckets) == {
            "management",
            "launch",
            "review",
            "promotion",
            "settlement",
            "unknown",
        }
        assert all(value == 0.0 for value in buckets.values())


class TestQuery:
    def test_selector_equality_and_regex(self) -> None:
        samples = [
            Sample("cost_obs_review_total", (("phase", "continuous"),), 1.0),
            Sample("cost_obs_review_total", (("phase", "final"),), 1.0),
        ]
        assert scalar('sum(cost_obs_review_total{phase="final"})', samples) == 1.0
        assert scalar('sum(cost_obs_review_total{phase=~"continuous|final"})', samples) == 2.0

    def test_negation_matchers(self) -> None:
        samples = [Sample("m", (("x", "a"),), 1.0), Sample("m", (("x", "b"),), 2.0)]
        assert scalar('sum(m{x!="a"})', samples) == 2.0
        assert scalar('sum(m{x!~"a"})', samples) == 2.0

    def test_scalar_binary_division(self) -> None:
        samples = [
            Sample("cost_obs_execution_cost_total", (("attribution", "management"),), 10.0),
            Sample("cost_obs_execution_cost_total", (("attribution", "launch"),), 20.0),
        ]
        assert scalar(
            'sum(cost_obs_execution_cost_total{attribution="management"})'
            " / sum(cost_obs_execution_cost_total)",
            samples,
        ) == pytest.approx(1.0 / 3.0)

    def test_on_vector_matching_is_exact_once(self) -> None:
        samples = [
            Sample("cost_obs_launch_total", (("order_id", "o1"),), 1.0),
            Sample("cost_obs_launch_total", (("order_id", "o1"),), 2.0),
            Sample("cost_obs_settlement_total", (("order_id", "o1"), ("status", "settled")), 1.0),
        ]
        result = query(
            'sum(cost_obs_settlement_total{status="settled"}) by (order_id)'
            " / on(order_id) sum(cost_obs_launch_total) by (order_id)",
            samples,
        )
        assert len(result) == 1
        # One settlement relative to two (double-counted) launches is not 1.
        assert result[0].value == pytest.approx(1.0 / 3.0)

    def test_unsupported_subset_fails_loudly(self) -> None:
        with pytest.raises(PromQLError):
            query("rate(m[5m])", [])

    def test_non_existent_metric_is_empty(self) -> None:
        assert query("sum(cost_obs_launch_total)", []) == []

    def test_zero_over_zero_yields_nan_like_prometheus(self) -> None:
        samples = [Sample("cost_obs_execution_cost_total", (("attribution", "management"),), 0.0)]
        result = query(
            'sum(cost_obs_execution_cost_total{attribution="management"})'
            " / sum(cost_obs_execution_cost_total)",
            samples,
        )
        assert len(result) == 1
        assert math.isnan(result[0].value)

    def test_finite_numerator_over_zero_yields_inf_like_prometheus(self) -> None:
        samples = [
            Sample("m", (("a", "x"),), 5.0),
            Sample("m", (("a", "y"),), 0.0),
        ]
        result = query('sum(m{a="x"}) / sum(m{a="y"})', samples)
        assert len(result) == 1
        assert result[0].value == float("inf")


class TestDataPlane:
    def test_emission_is_idempotent_across_replays(self) -> None:
        plane = CostDataPlane()
        assert plane.record_launch(order_id="o", development_id="d") is True
        assert plane.record_settlement(order_id="o") is True
        assert plane.record_launch(order_id="o", development_id="d") is False
        assert plane.record_settlement(order_id="o") is False
        report = plane.reconcile()
        assert report.exact_once is True
        assert report.orders["o"] == {"launch": 1, "settlement": 1}

    def test_open_order_is_not_a_double_count(self) -> None:
        plane = CostDataPlane()
        plane.record_launch(order_id="open", development_id="d")
        plane.mark_absent(order_id="open", lifecycle="settlement")
        report = plane.reconcile()
        assert report.exact_once is True
        assert report.orders["open"] == {"launch": 1, "settlement": 0}

    def test_missing_and_present_are_distinct_from_unknown(self) -> None:
        plane = CostDataPlane()
        plane.record_launch(order_id="o", development_id="d")
        plane.record_settlement(order_id="o")
        plane.record_unknown_cost(order_id="o", tokens=7, event_id="e")
        plane.mark_absent(order_id="o", lifecycle="promotion")

        samples = plane.samples()
        unknown = [
            s.value
            for s in samples
            if s.name == "cost_obs_execution_cost_total"
            and s.label_map().get("attribution") == "unknown"
        ]
        present = query('cost_obs_lifecycle_present{order_id="o",lifecycle="settlement"}', samples)
        missing = query('cost_obs_lifecycle_present{order_id="o",lifecycle="promotion"}', samples)
        assert unknown == [7.0]
        assert [s.value for s in present] == [1.0]
        assert [s.value for s in missing] == [0.0]

    def test_all_five_rules_emit_a_series_after_a_full_lifecycle(self) -> None:
        plane = CostDataPlane()
        plane.record_launch(order_id="o", development_id="d", generation=1)
        plane.record_review(order_id="o", phase="continuous", verdict="approve")
        plane.record_review(order_id="o", phase="final", verdict="approve")
        plane.record_promotion(order_id="o", target_ref="refs/heads/main")
        plane.record_settlement(order_id="o")
        plane.record_execution_cost(
            attribution="management", order_id="o", tokens=1, event_id="e:mgmt"
        )
        plane.record_execution_cost(
            attribution="launch", order_id="o", tokens=1, event_id="e:launch"
        )
        plane.record_execution_cost(
            attribution="review", order_id="o", tokens=1, event_id="e:review"
        )
        plane.record_execution_cost(
            attribution="promotion", order_id="o", tokens=1, event_id="e:promo"
        )
        plane.record_execution_cost(
            attribution="settlement", order_id="o", tokens=1, event_id="e:settle"
        )
        plane.record_unknown_cost(order_id="o", tokens=1, event_id="e:unknown")

        for rule in RECORDING_RULES:
            assert query(rule.expr, plane.samples()), rule.name

    def test_review_phase_is_validated(self) -> None:
        plane = CostDataPlane()
        with pytest.raises(ValueError):
            plane.record_review(order_id="o", phase="typo", verdict="approve")

    def test_a_reworked_review_is_a_new_fact_not_a_frozen_verdict(self) -> None:
        plane = CostDataPlane()
        assert plane.record_review(order_id="o", phase="continuous", verdict="reject", attempt=1)
        assert plane.record_review(order_id="o", phase="continuous", verdict="approve", attempt=2)
        # Replaying attempt 2 is still idempotent -- a retried run never double-counts.
        assert (
            plane.record_review(order_id="o", phase="continuous", verdict="approve", attempt=2)
            is False
        )

        reviews = [s for s in plane.samples() if s.name == "cost_obs_review_total"]
        assert {(s.label_map()["phase"], s.label_map()["verdict"]) for s in reviews} == {
            ("continuous", "reject"),
            ("continuous", "approve"),
        }

    def test_exposition_writes_and_reads_back(self, tmp_path: Path) -> None:
        plane = CostDataPlane(exposition_dir=tmp_path)
        plane.record_launch(order_id="o", development_id="d")
        path = plane.write_exposition()
        assert path.name == "cost-obs.prom"
        scraped = parse(path.read_text(encoding="utf-8"))
        assert [s.value for s in query("sum(cost_obs_launch_total)", scraped)] == [1.0]

    def test_rehydrate_merges_previous_facts_and_stays_idempotent(self, tmp_path: Path) -> None:
        first = CostDataPlane(exposition_dir=tmp_path)
        first.record_launch(order_id="o", development_id="d")
        first.record_review(order_id="o", phase="continuous", verdict="approve")
        first.write_exposition()

        # A fresh plane, as a resumed process builds: nothing in memory yet.
        resumed = CostDataPlane(exposition_dir=tmp_path)
        assert resumed.rehydrate_from_file() is True
        resumed.record_promotion(order_id="o", target_ref="refs/heads/main")
        resumed.write_exposition()

        names = {s.name for s in resumed.samples()}
        assert {"cost_obs_launch_total", "cost_obs_review_total", "cost_obs_promotion_total"} <= (
            names
        )
        # The rehydrated launch keeps its stable identity, so a replay is a no-op.
        assert resumed.record_launch(order_id="o", development_id="d") is False
        assert resumed.reconcile().orders["o"]["launch"] == 1

    def test_rehydrated_management_spend_is_not_double_counted(self, tmp_path: Path) -> None:
        """A resumed run that re-emits management spend must not double-count it.

        The walker emits management spend under the stable identity
        ``management:<order_id>`` on every terminal, and the cost sample itself
        carries no order id. Rehydrating a previous render must therefore re-key
        management spend to that same identity, so the resumed terminal's
        re-emission is a no-op and rule 1's numerator stays exactly one spend
        instead of two.
        """
        first = CostDataPlane(exposition_dir=tmp_path)
        first.record_launch(order_id="o", development_id="d")
        first.record_execution_cost(
            attribution="management", order_id="o", tokens=10.0, event_id="management:o"
        )
        first.write_exposition()

        resumed = CostDataPlane(exposition_dir=tmp_path)
        assert resumed.rehydrate_from_file() is True
        # The resumed walker reaches its terminal and re-emits the same spend.
        resumed.record_execution_cost(
            attribution="management", order_id="o", tokens=10.0, event_id="management:o"
        )

        costs = [
            s
            for s in resumed.samples()
            if s.name == "cost_obs_execution_cost_total"
            and s.label_map().get("attribution") == "management"
        ]
        assert [s.value for s in costs] == [10.0]

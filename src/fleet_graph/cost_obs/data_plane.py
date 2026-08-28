"""The cost-observability data plane: lifecycle fact emission and reconciliation.

This is the producer side of the five `cost-observability` recording rules.
It has one job per lifecycle -- launch, review (continuous + final),
promotion, and settlement/order -- and three standing duties that every
emission obeys:

**Idempotent by stable identity.** Every emitted fact carries an identity key,
and re-emitting the same key is a no-op. A retried launch or a replayed
settlement therefore cannot double-count; the key stays the same event, so the
second emission changes nothing.

**Launch correlated to settlement by order id.** A launch and the DD
settlement that later closes it share an `order_id` label. The recording rule
`cost_obs:settlement_reconciliation:ratio` divides settled orders by launched
orders *on that id*, so exact-once is an observable ratio of 1 rather than a
claim in prose.

**Unknown and missing stay distinct.** Token spend that nothing attributes is
emitted under `attribution="unknown"` (see `classify.py`). A lifecycle class
whose producer simply emitted no fact is marked *absent*, which the derived
presence series (`cost_obs_lifecycle_present`) renders as an explicit `0` --
so absent source data is observable and never silently relabelled as unknown.

The data plane is deliberately decoupled from the scheduling graph: the DD
pipeline is a *caller* of these emitters, not an owner, so the facts can be
produced, tested, and reconciled without a live systemd unit or a model round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fleet_graph.cost_obs.classify import (
    KNOWN_CLASSES,
    LAUNCH,
    PROMOTION,
    REVIEW,
    SETTLEMENT,
    UNKNOWN,
)
from fleet_graph.cost_obs.exposition import Sample, render
from fleet_graph.cost_obs.query import query
from fleet_graph.cost_obs.rules import (
    COST_METRIC,
    LAUNCH_METRIC,
    PRESENCE_METRIC,
    PROMOTION_METRIC,
    RECORDING_RULES,
    REVIEW_METRIC,
    SETTLEMENT_METRIC,
    RecordingRule,
)

#: The four lifecycle classes whose source facts feed the statistical outputs.
LIFECYCLES = (LAUNCH, REVIEW, PROMOTION, SETTLEMENT)


@dataclass
class ReconcileReport:
    """Per-order launch/settlement correlation and its exact-once verdict."""

    orders: dict[str, dict[str, int]] = field(default_factory=dict)
    exact_once: bool = True


class CostDataPlane:
    """Emits labelled source facts and reconciles launch -> settlement.

    `exposition_dir`, when given, is where `write_exposition()` drops the
    `cost-obs.prom` textfile the scrape layer reads. The in-memory sample set
    is always authoritative; the file is a rendering of it.
    """

    def __init__(self, exposition_dir: str | Path | None = None) -> None:
        self.exposition_dir = Path(exposition_dir) if exposition_dir else None
        self._seen: set[str] = set()
        self._samples: dict[str, Sample] = {}
        # order_id -> set of lifecycle classes that produced at least one fact
        self._fact_lifecycles: dict[str, set[str]] = {}
        # order_id -> set of lifecycle classes explicitly marked absent
        self._absent: dict[str, set[str]] = {}

    # --- emission (idempotent by identity key) ---------------------------

    def _emit(self, name: str, labels: dict[str, str], value: float, *, key: str) -> bool:
        """Record one fact. Returns False when the same key was already emitted."""
        if key in self._seen:
            return False
        self._seen.add(key)
        self._samples[key] = Sample(name=name, labels=tuple(sorted(labels.items())), value=value)
        return True

    def record_launch(
        self,
        *,
        order_id: str,
        development_id: str,
        generation: int = 1,
        seat: str = "",
        model: str = "",
    ) -> bool:
        """Emit a DD launch fact. Replaying the same order is a no-op."""
        emitted = self._emit(
            LAUNCH_METRIC,
            {
                "order_id": order_id,
                "development_id": development_id,
                "generation": str(generation),
                "seat": seat,
                "model": model,
            },
            1.0,
            key=f"launch:{order_id}",
        )
        self._fact_lifecycles.setdefault(order_id, set()).add(LAUNCH)
        return emitted

    def record_review(self, *, order_id: str, phase: str, verdict: str) -> bool:
        """Emit a review fact for the `continuous` or `final` phase."""
        if phase not in {"continuous", "final"}:
            raise ValueError(f"review phase must be continuous or final, got {phase!r}")
        emitted = self._emit(
            REVIEW_METRIC,
            {"order_id": order_id, "phase": phase, "verdict": verdict},
            1.0,
            key=f"review:{order_id}:{phase}",
        )
        self._fact_lifecycles.setdefault(order_id, set()).add(REVIEW)
        return emitted

    def record_promotion(self, *, order_id: str, target_ref: str, via: str = "merge") -> bool:
        """Emit a promotion (merge) fact."""
        emitted = self._emit(
            PROMOTION_METRIC,
            {"order_id": order_id, "target_ref": target_ref, "via": via},
            1.0,
            key=f"promotion:{order_id}",
        )
        self._fact_lifecycles.setdefault(order_id, set()).add(PROMOTION)
        return emitted

    def record_settlement(self, *, order_id: str) -> bool:
        """Emit a settled-order fact correlated to the launch by `order_id`."""
        emitted = self._emit(
            SETTLEMENT_METRIC,
            {"order_id": order_id, "status": "settled"},
            1.0,
            key=f"settlement:{order_id}",
        )
        self._fact_lifecycles.setdefault(order_id, set()).add(SETTLEMENT)
        return emitted

    def record_execution_cost(self, *, attribution: str, tokens: float, event_id: str) -> bool:
        """Emit one token spend batch, bucketed by attribution class.

        `attribution` is a known class or `unknown`; a genuinely unattributed
        batch is emitted under `unknown` by the caller, never guessed here.
        """
        if attribution not in KNOWN_CLASSES and attribution != UNKNOWN:
            raise ValueError(f"attribution must be a known class or 'unknown', got {attribution!r}")
        return self._emit(
            COST_METRIC,
            {"attribution": attribution},
            float(tokens),
            key=f"cost:{event_id}:{attribution}",
        )

    def record_unknown_cost(self, *, tokens: float, event_id: str) -> bool:
        """Emit a token batch that nothing attributes to a lifecycle class."""
        return self.record_execution_cost(attribution=UNKNOWN, tokens=tokens, event_id=event_id)

    def mark_absent(self, *, order_id: str, lifecycle: str) -> None:
        """Explicitly account a lifecycle class whose producer emitted nothing.

        This is the `missing` half of the unknown/missing distinction: the
        presence series will carry a bounded `0` for this order + lifecycle.
        """
        if lifecycle not in LIFECYCLES:
            raise ValueError(f"lifecycle must be one of {LIFECYCLES}, got {lifecycle!r}")
        self._absent.setdefault(order_id, set()).add(lifecycle)

    def mark_absent_if_missing(self, order_id: str) -> None:
        """Account every lifecycle whose producer never emitted for this order.

        This is the caller-friendly form of `mark_absent`: handed a closed
        order, the data plane itself decides which of the four lifecycles are
        absent (no fact) rather than asking the caller to know, one by one,
        which producers stayed silent. The result is the same bounded-zero
        presence series, and marking is idempotent.
        """
        facts = self._fact_lifecycles.get(order_id, set())
        for lifecycle in LIFECYCLES:
            if lifecycle not in facts:
                self._absent.setdefault(order_id, set()).add(lifecycle)

    # --- reads ------------------------------------------------------------

    def samples(self) -> list[Sample]:
        """All emitted facts plus the derived presence series.

        Presence is derived, not stored: for every order that produced a fact
        or was flagged absent, each lifecycle is `1` when a fact exists, else
        `0` when explicitly absent -- a bounded zero-compatible series that is
        distinguishable from `unknown` attribution.
        """
        result = list(self._samples.values())
        orders = set(self._fact_lifecycles) | set(self._absent)
        for order_id in sorted(orders):
            facts = self._fact_lifecycles.get(order_id, set())
            absent = self._absent.get(order_id, set())
            for lifecycle in LIFECYCLES:
                if lifecycle in absent and lifecycle not in facts:
                    value = 0.0
                elif lifecycle in facts:
                    value = 1.0
                else:
                    continue
                result.append(
                    Sample(
                        name=PRESENCE_METRIC,
                        labels=(
                            ("lifecycle", lifecycle),
                            ("order_id", order_id),
                        ),
                        value=value,
                    )
                )
        return result

    def query_rule(self, rule: RecordingRule) -> list[Sample]:
        """Evaluate one recording rule against the emitted facts."""
        return query(rule.expr, self.samples())

    def query_all_rules(self) -> dict[str, list[Sample]]:
        """Evaluate all five recording rules, keyed by rule name."""
        return {rule.name: self.query_rule(rule) for rule in RECORDING_RULES}

    def write_exposition(self, filename: str = "cost-obs.prom") -> Path:
        """Render the data plane to the scrape file and return its path."""
        if self.exposition_dir is None:
            raise ValueError("no exposition_dir set; nothing to write to")
        self.exposition_dir.mkdir(parents=True, exist_ok=True)
        path = self.exposition_dir / filename
        path.write_text(render(self.samples()), encoding="utf-8")
        return path

    def reconcile(self) -> ReconcileReport:
        """Correlate launches to settlements and assert exact-once.

        Exact-once is two predicates here. No order may carry more than one
        launch or more than one settlement (a retried or replayed lifecycle
        must not double-count); and every *settled* order must correlate to
        exactly one launch by the stable order identity. An order that launched
        but never settled is an open order, not a double-count -- its absence
        is accounted by the presence series instead.
        """
        launch_counts = self._count_by_order(LAUNCH_METRIC)
        settlement_counts = self._count_by_order(SETTLEMENT_METRIC)
        order_ids = set(launch_counts) | set(settlement_counts)
        report = ReconcileReport(exact_once=True)
        for order_id in sorted(order_ids):
            launch = launch_counts.get(order_id, 0)
            settled = settlement_counts.get(order_id, 0)
            report.orders[order_id] = {"launch": launch, "settlement": settled}
            if launch > 1 or settled > 1:
                report.exact_once = False
            if settled == 1 and launch != 1:
                report.exact_once = False
        return report

    def _count_by_order(self, metric_name: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sample in self._samples.values():
            if sample.name != metric_name:
                continue
            order_id = sample.label_map().get("order_id")
            if order_id is None:
                continue
            counts[order_id] = counts.get(order_id, 0) + 1
        return counts


__all__ = [
    "LIFECYCLES",
    "CostDataPlane",
    "ReconcileReport",
]

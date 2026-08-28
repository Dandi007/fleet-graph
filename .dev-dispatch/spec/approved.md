# Restore cost-observability data plane

## Situation
Prometheus loads five healthy `cost-observability` recording rules, but only `cost_obs:management_execution:ratio` emits a series. Most tokens are classified as `unknown`; settled-order and related source facts are absent. The fleet-graph-dd development, review, and promotion flow lacks the data-plane facts needed by four statistical outputs.

## Required work
Use dev-dispatch for all implementation and code review. Diagnose the missing producers, label/cardinality joins, and scrape/exposition wiring in this repository. Repair the four statistical data-plane gaps at their responsible components, rather than masking empty inputs with synthetic totals.

1. Ensure every source fact required by each of the five loaded cost-observability recording rules is emitted, scraped, and retains the labels required by its rule joins.
2. Restore source facts for real DD launch lifecycle, final/continuous review lifecycle, promotion lifecycle, and settlement/order lifecycle, including settled-order facts.
3. Preserve an explicit `unknown` classification for tokens that genuinely lack attribution. Add explicit `missing`/absence accounting or bounded zero-compatible series where a source fact is absent, so absent source data is observable and distinguishable from unknown attribution. Do not silently relabel unknown as a known class.
4. Make lifecycle emission idempotent: a retried or replayed launch/settlement must not double-count. Correlate a real launch to its DD settlement by a stable identity and provide an exact-once reconciliation assertion.
5. Add focused automated tests and an executable acceptance fixture that drives a real launch plus DD settlement and queries all five recording-rule expressions. The fixture must assert all five query results are non-empty, explicit unknown/missing visibility, and exact-once reconciliation after replay/retry.

## Acceptance
- `make verify` passes.
- The executable acceptance test exercises one actual launch and DD settlement, evaluates all five cost-observability recording-rule queries, and proves each has a result.
- The test proves unknown and missing are separately visible.
- The test replays the relevant lifecycle operation and proves launch/settlement reconciliation remains exact-once.
- Review must identify and resolve correctness risks around duplicate events, ordering, missing labels, and PromQL vector matching.

```dd-acceptance
make verify
```

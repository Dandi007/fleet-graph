# Narrow repair: real human recovery re-entry and non-empty B3 evidence validity

## Context

This is a narrow corrective successor to the completed B1-B3 development at exact base `3390d635af982662208b080501ff4f1973b45d72`. Final review found two semantic gaps. Preserve all existing B1 scope isolation, B2 adoption/recovery authentication, auditability, concurrency and safety semantics.

## Requirements

1. A successful `DdControlPlane.recover` / supported human-recovery MCP operation must actually restart or re-enter the suspended development thread after the authenticated recovery record is sealed. It must not return `resumed=true` unless a real resume launch/re-entry occurred or the same thread is already running and mechanically identified.
2. Recovery remains record-gated: no launch without the authenticated, target-bound immutable recovery record; malformed, missing, mismatched, replayed or unauthorized recovery input must fail closed and must not launch.
3. Return structured, truthful resume fields including whether launch occurred, already-running state where applicable, unit/thread/generation identity, and any raw launch failure. Do not fabricate success.
4. Add control-plane/MCP regression tests that exercise the real recovery entrypoint and prove: suspended thread is actually relaunched/re-entered; missing record does not launch; duplicate invocation is idempotent and does not create duplicate live threads; launch failure is surfaced as failure rather than `resumed=true`.
5. B3 `EvidenceChain.validate` and the assembled `b3_evidence_chain` must be invalid when required links are absent. Define the minimum required link set from the existing B1-B3 contract and report deterministic missing-link reasons. An empty link tuple must never be `valid=true`.
6. Add regressions for empty chain, each required-link omission, malformed/target-mismatched link, and a complete valid phenomenon -> mechanism -> evidence chain. Preserve existing attribution and scope evidence.
7. Keep changes narrowly within fleet-graph DD recovery/evidence code and tests. Do not modify acceptance configuration, deployment, production services, work-folder MCP, B4 monitoring, or unrelated behavior.
8. All implementation and review are performed by dev-dispatch. The production checkout is never used for development or verification.

## Acceptance

- `uv sync --frozen` exits 0.
- `make verify` exits 0 and includes the new recovery re-entry and evidence-chain regressions.

```dd-acceptance
uv sync --frozen
make verify
```
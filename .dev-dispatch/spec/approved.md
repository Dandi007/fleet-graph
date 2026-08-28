# Fix dev-dispatch feedback-carrier replay ordering successor

## Context

`dev-fg-a342eab2a32b` remains unfinished after final review rejection. Its review identified that its attempted replay-plan preference does not repair the reported cross-generation feedback-carrier ordering failure and lacks the required fail-closed regression coverage. E1 decision bridging and E4b verdict normalization are accepted ancestors of the target base; this increment is limited to the remaining replay-carrier defect.

## Required behavior

1. Preserve feedback-carrier ordering, ownership, immutable history, and the requirement that a genuine new implementation attempt has the protocol-required preceding `REJECT`.
2. Identify cross-generation replay from stable attempt, handoff, and receipt identities. Replayed configure/implement/review materialization must reuse sealed identities and must not append duplicate feedback attempts or review entries.
3. For a valid replay of an inherited accepted/reviewed chain whose feedback history lacks a terminal `REJECT`, continue through continuous review, final review, and acceptance without `ORDER_VIOLATION`; do not globally weaken ordering validation.
4. If the newest viable replay prefix cannot be prepared because it has product-tree drift, try the next viable replay prefix rather than disabling replay and dispatching a fresh implementation against an already modified tree.
5. Add regression coverage that exercises the actual materializer/feedback carrier path: a later generation reuses the accepted/reviewed chain, reaches continuous-review materialization and subsequent boundaries, produces no illegal new attempt, and passes acceptance.
6. Add a complementary regression proving a genuine new attempt with no required preceding `REJECT` remains refused.
7. Add coverage for the product-drift fallback in requirement 4.

## Constraints

- Scope changes to fleet-graph dev-dispatch replay, feedback, handoff, evidence-carrier responsibilities, and their tests.
- Do not modify unrelated product behavior, deployment configuration, production services, or board decision normalization.
- All implementation and code review work must be performed by dev-dispatch.

## Acceptance

```dd-acceptance
uv sync --frozen
make verify
```
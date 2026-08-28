# Fix cross-generation ORDER_VIOLATION in continuous-review materialization

## Problem and immutable source evidence

Fix the dev-dispatch engine defect that makes an otherwise valid post-generation restart fail when it reaches `continuous_review`.

The exact observed raw error is:

```text
materialize failed on continuous_review: PLUGIN_CONTRACT_MISMATCH: materialize-handoff.sh returned non-JSON output (exit 1): [attempt-context] ERROR ORDER_VIOLATION: entry 4 (rc-20096c0b-5929-5d20-8dc8-604b0e7e7aef): a new attempt requires a prior REJECT
```

This occurred for `dev-fg-31b963659d16` after generation boundaries had been crossed. The relevant historical evidence is:

- Generation 1: attempt 1 continuous review `APPROVE`, final review `REJECT`; attempt 2 continuous review `APPROVE`, final review `APPROVE`, acceptance `success`.
- Generation 2 and generation 3 then failed while materializing `continuous_review`, because the persisted feedback index contains an earlier review record (`rc-20096c0b-5929-5d20-8dc8-604b0e7e7aef`) and the validator classified it as a new attempt without a preceding reject.
- The new required fact: review/feedback entries belonging to an older generation or older attempt chain must be preserved as immutable history and must not be misclassified as a new attempt in the current generation's attempt ordering.

## Required behavior

1. Make ordering validation generation-aware and/or chain-aware using the durable receipt/attempt identity already recorded by the engine. Do not erase, rewrite, or weaken validation of historical feedback records.
2. Preserve rejection requirements for a genuinely new attempt within the same relevant chain.
3. Permit a valid later generation to materialize `continuous_review` after inherited historical reviews, without requiring a synthetic prior `REJECT` for the current attempt.
4. Add focused deterministic regression coverage that constructs the reported cross-generation history and proves materialization of the current generation's continuous review succeeds.
5. Add a complementary refusal regression proving a genuinely invalid same-chain new attempt without its required prior `REJECT` is still rejected.
6. The regression must assert the structured materialization result, not merely absence of a shell error.
7. Do not alter the B1-B3 product specification or treat the historic review records as disposable test fixtures. This development fixes the dev-dispatch ordering engine only.

## Evidence and acceptance

The implementation handoff must identify the changed ordering invariant and the focused regression tests. Continuous and final reviews must be performed by dev-dispatch and their immutable receipts must be present. Acceptance must execute exactly the following commands from the dedicated worktree and record their exit status.

```dd-acceptance
uv sync --frozen
make verify
```

# Fix dev-dispatch feedback carrier replay ordering

## Problem
`dev-fg-31b963659d16` generation 2 and generation 3 both replayed configure and implement successfully, then failed while materializing `continuous_review` with:

```text
PLUGIN_CONTRACT_MISMATCH: materialize-handoff.sh returned non-JSON output (exit 1):
[attempt-context] ERROR ORDER_VIOLATION:
entry 4 (rc-20096c0b-5929-5d20-8dc8-604b0e7e7aef):
a new attempt requires a prior REJECT
```

The replayed implementation is not a new implementation attempt. The feedback carrier/index is inherited across generations, but its ordering validation treats the replayed continuous-review materialization as a new attempt and requires a preceding `REJECT` even when the prior accepted chain ended successfully.

## Required behavior

1. Preserve the feedback carrier's ordering and ownership checks for genuine new implementation attempts: a genuinely new attempt still requires the valid preceding feedback state required by the protocol.
2. Recognize idempotent cross-generation replay from stable attempt/handoff/receipt identity. Replaying configure and implement must not create a duplicate feedback attempt or duplicate review entry.
3. A valid replayed candidate whose inherited feedback chain did not end in `REJECT` must continue into continuous review, final review, and acceptance rather than failing `ORDER_VIOLATION`.
4. Preserve immutable, linear evidence and feedback history. Do not weaken the ordering rule globally, discard feedback, or silently overwrite an existing entry.
5. Add automated regression coverage that reproduces the reported sequence: an accepted/reviewed chain is replayed into a later generation; replayed configure and implement are reused; continuous-review handoff materialization succeeds; subsequent review and acceptance boundaries can be reached; and no illegal new attempt is created. Include a complementary assertion that a genuine new attempt without its required prior `REJECT` remains rejected.

## Constraints

- Keep the repair within fleet-graph/dev-dispatch feedback, replay, handoff, and evidence carrier responsibilities.
- Do not modify unrelated product behavior or bypass plugin contract validation.
- All implementation and code review work must be performed by dev-dispatch.

## Acceptance

```dd-acceptance
uv sync --frozen
make verify
```

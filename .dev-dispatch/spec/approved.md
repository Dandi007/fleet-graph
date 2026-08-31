# U4 Enrollment REJECT-with-fixes Correction Spec

## Status

This is the frozen correction spec for the U4 first-round E2E enrollment candidate `wf-e7b0dd`. The implementation and review must be performed through dev-dispatch only. Do not modify `/data/code/self/fleet-graph` directly. All git work must occur in an independent worktree under `/data/worktrees`; the production checkout is deployment-only and may only receive a fast-forward-only pull, with no checkout, switch, reset, or branch changes.

## Scope

Fix exactly these three real defects found by supervision:

1. **Goal serve queue-home isolation.** Add an independent queue home for goal serve, defaulting to `/data/fleet-graph/goal/`. Move the existing `enroll-queue.jsonl` and `enroll-rejections.jsonl` out of `work-records` into this goal queue home, and ensure goal enrollment reads/writes only that queue home by default. The default must not pollute or consume another governance warehouse.
2. **State enrollment read model.** Make the state service's `:7494 /v1/enrollments` read model use the same queue home by default as goal serve, so it observes the actual enrollment queue rather than going blind. Preserve the E8 emission path and ensure a valid enrollment can cause E8 to be emitted.
3. **Gate 6 token ownership.** Upgrade gate 6 from token-exists validation to token-specific ownership validation. Resolve token paths with `realpath`; a token is valid only when it belongs to the current governed line and does not resolve into the supervision plane or another line's token. Symlink masquerading must be rejected. Add explicit negative tests for supervision-plane token, other-line token, and symlink alias cases, plus the ordinary valid owned-token case.

## Implementation requirements

- Inspect the existing configuration, service entrypoints, queue consumers/producers, state read model, gate 6 implementation, and acceptance harness before editing; follow established project conventions.
- Preserve explicit configuration overrides where they already exist, but make the stated goal queue home the default for both goal and state enrollment paths.
- Perform any migration in a deterministic, idempotent manner. Do not silently duplicate or overwrite records; retain data and make the resulting queue location unambiguous. Update service/config/docs/tests as needed.
- Gate 6 ownership must be based on canonicalized paths and a positive ownership boundary, not filename or token presence. Reject missing, non-regular, outside-boundary, supervision-plane, other-line, and symlink-alias tokens as appropriate to the existing contract. Avoid weakening any existing authorization checks.
- Expand `scripts/e2_goal_enroll_acceptance.py` with executable coverage for all three defects, including negative gate-6 cases and an E8-observable enrollment through the aligned queue home.
- Review the complete diff for regressions, path leakage, unsafe migration behavior, and tests that can pass without exercising the real behavior. No unrelated refactor.

## Frozen acceptance criteria

The change is accepted only if both commands pass from the repository root, and the acceptance script covers all requirements above:

```dd-acceptance
make verify
uv run python scripts/e2_goal_enroll_acceptance.py
```

`make verify` must remain green. The acceptance script must fail for the three gate-6 negative cases (supervision token, other-line token, and symlink alias), pass for a genuinely owned token, verify the default goal queue home is `/data/fleet-graph/goal/` (or the repository's test-isolated equivalent when the harness injects a temporary root), verify both queue files are outside `work-records`, and verify `/v1/enrollments` sees the same queue and the E8 path emits for a valid enrollment.

## Delivery

Return the development id and bootstrap commit. Include test commands and exit codes in the development evidence. Do not deploy or alter the production checkout as part of this development.

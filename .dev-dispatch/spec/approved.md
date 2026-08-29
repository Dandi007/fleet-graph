# E5 Design — Goal Line Enroll MCP Surface（fail-closed + versioned briefing）

**Status:** dispatch-ready design only. Implementation and code review are delegated to dev-dispatch in a clean, non-detached worktree under `/data/worktrees/`. This document does not authorize Git operations in the production checkout, a deployment, or any production-unit restart.

## Scope and decision

Move the Phase-0 opening contract of a goal line (DoD, executable acceptance, golden order) out of the `goal-driven-work` skill and into the fleet-graph MCP surface. The MCP server gains a fail-closed `goal_enroll` tool that validates a candidate goal folder before it can be admitted to the roster, and delivers the versioned briefing (交底) through MCP prompts/resources so the handoff is pinned to an engine release, not to a skill file.

This is E5 only. Do not change agent-bus semantics, the checkpoint/terminal contract, scheduling/accounting policy, the dev-dispatch control plane, or worker prompts. The `goal-driven-work` skill is deprecated (one redirect sentence) only after this development's acceptance, as a separate step.

## Fail-closed enrollment contract

`goal_enroll(folder_id)` must refuse (never half-admit) unless every gate below passes. A refusal is an explicit, machine-readable error with a stable code and the failing clause — it is not a warning, not a partial roster entry, and not a deferred admission.

1. **folder_id valid**: the folder exists in the work-folder source and resolves to a folder whose layout is a goal line (contains `goal.md` and `golden-order.md`).
2. **goal.md carries executable acceptance argv**: `goal.md` must declare at least one executable acceptance command line whose exit code is the acceptance criterion (same contract the roster already enforces). A goal without an acceptance command is refused with `NO_ACCEPTANCE_COMMAND` — this is the negative sample the acceptance drill must prove.
3. **golden-order.md present**: the golden order exists and is non-empty (it is the line's authority boundary, outranking the spec).
4. **spec-lint (machine-readable bans)**: the admitted goal/spec text must not instruct `merge`/`push` to remote `main` (delivery endpoint is the durable branch + gate decision; main merge and deploy belong to the supervisor harvest only), and must not reference the reserved identity paths `.dev-dispatch/**` / `.dd-evidence/**` from product code or tests. A critical-path table containing a pinned 40-hex SHA is a lint warning, not a refusal.
5. **server-side liveness probe**: the server dry-runs the declared acceptance argv in a throwaway environment to prove they are executable commands (exit code reachable), not free text. A command that cannot even start is refused with `ACCEPTANCE_ARGV_UNEXECUTABLE`.

Enrollment succeeds only when all gates pass, and the resulting roster entry is an engine-versioned artifact.

## Versioned briefing (交底) via MCP prompts

The opening handoff is served from the MCP server's prompt/resource registry, versioned together with the engine release:

- A `goal-open` prompt (Phase-0 contract: DoD form, executable acceptance form, golden-order authority, supervisor channel, production-safety lines) rendered from a versioned resource, so a release bump atomically ships the new briefing text.
- The enroll tool embeds the same version id into the roster entry it admits, so a line opened under briefing `vN` is auditable against `vN`.

## Defect-family context this surface absorbs

The briefing and the lint must carry the recorded constraints so they are enforced at the write gate, not re-learned per line: durable branch never merges to main directly; product code/tests never reference `.dev-dispatch`/`.dd-evidence`; bus alias enrollment atomically creates the bus agent and a `0600` token; the deployment request lists every unit env dependency; the critical-path table must not pin a rolling SHA. These are facts the versioned prompt carries verbatim, and `spec-lint` makes the merge/push and reserved-path bans mechanical.

## Deliverables

1. `goal_enroll` MCP tool in the fleet-graph MCP server surface (next to the dev-dispatch tools), backed by the fail-closed validator above, with the negative-path refusal codes.
2. The versioned briefing prompt/resource and its version id recorded on the roster entry.
3. Focused tests `tests/test_goal_enroll.py` covering every gate and refusal code, plus the two acceptance scenarios below.
4. The `goal-driven-work` skill deprecation is NOT part of this development's code; it happens only after acceptance.

## Definition of done

- `goal_enroll` refuses a goal with no acceptance command (`NO_ACCEPTANCE_COMMAND`) and refuses an unexecutable argv (`ACCEPTANCE_ARGV_UNEXECUTABLE`), each with an explicit code.
- `goal_enroll` admits a valid goal folder and records the briefing version id.
- The end-to-end drill enrolls one throwaway line through the MCP and it starts.
- `uv sync --frozen` and `make verify` exit 0.

## Acceptance argv

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_goal_enroll.py -q
uv run python scripts/e2_goal_enroll_acceptance.py --scenario no-acceptance-goal-fail-closed
uv run python scripts/e2_goal_enroll_acceptance.py --scenario enroll-drill-line-end-to-end
```

The dispatching spec must retain these argv verbatim. The `no-acceptance-goal-fail-closed` scenario is the negative-sample assertion (a goal without an acceptance command is refused, exit 0 for the drill that proves the refusal); `enroll-drill-line-end-to-end` enrolls one throwaway drill line and asserts it starts. No deployment, service restart, or production-checkout validation is part of acceptance.

## Dispatch handoff

Create one new E5-only dev-dispatch development from this document's frozen scope. All business-code writing and final review occur inside that development. The dedicated worktree must be under `/data/worktrees/`; do not use the production main checkout for checkout, switch, reset, detach, test, or verification. After a separately approved remote-main merge, the production checkout may only receive `git pull --ff-only`; that action is not authorized by this document.

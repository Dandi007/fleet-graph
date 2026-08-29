# E4a Design - Structured Worker Turn Report

**Status:** dispatch-ready design only. Implementation and code review must be delegated to dev-dispatch in a clean, non-detached worktree under `/data/worktrees/`. This change does not authorize Git operations in the production checkout, a deployment, or any production-unit restart.

## Scope and decision

Replace prose as the orchestration control surface with one versioned, machine-readable worker turn report. The report is the only worker output from which the orchestration layer may derive turn outcome, next action, produced files, and self-test result. Human-facing prose remains an optional immutable attachment for inspection only.

This is E4a only. Do not implement E4b normalization, change agent-bus semantics, modify the checkpoint/terminal contract, alter scheduling/accounting policy, change worker prompts beyond requesting the report, or remove an externally required legacy response field in this development.

## Canonical report schema

A worker turn must emit exactly one JSON object conforming to `fleet-graph.worker-turn-report/v1`:

```json
{
  "schema_version": "fleet-graph.worker-turn-report/v1",
  "turn_id": "opaque worker turn identifier",
  "outcome": "completed",
  "summary": "bounded factual summary",
  "did": ["completed action"],
  "files": [{"path": "relative/path", "change": "created"}],
  "self_tests": [{"argv": ["uv", "run", "pytest", "tests/test_goal_line.py", "-q"], "exit_code": 0}],
  "blocker": null,
  "prose_attachment": {"media_type": "text/markdown", "content": "optional human-facing detail"}
}
```

Required fields are `schema_version`, `turn_id`, `outcome`, `summary`, `did`, `files`, `self_tests`, and `blocker`. Unknown top-level fields are rejected. `schema_version` must equal the literal above. `turn_id` and `summary` are non-empty bounded strings. `did` is an array of non-empty bounded strings. `files` is an array of objects with only `path` and `change`; `path` is a non-empty relative repository path and `change` is one of `created`, `modified`, `deleted`, or `unchanged`. `self_tests` is an array of objects with only `argv` and `exit_code`; `argv` is a non-empty string array and `exit_code` is a non-negative integer. `outcome` is exactly one of `completed`, `blocked`, or `failed`.

For `completed`, `blocker` is `null`. For `blocked`, `blocker` is an object with only non-empty bounded `kind` and `detail`; it is the sole source of a blocked transition. For `failed`, `blocker` may be `null` or the same object shape. The v1 report has no free-form status aliases, no embedded model envelope, and no control field derived from attachment prose.

`prose_attachment` is optional. When present it contains only `media_type` and `content`; `media_type` is `text/plain` or `text/markdown`, and `content` is bounded text. Attachment size limits must be explicit in code and reject, rather than truncate, oversized content so the persisted record remains unambiguous.

## Orchestration consumption

1. Validate and parse the report at the worker-result ingress before any checkpoint mutation, accounting transition, retry decision, or scheduler wake decision.
2. Persist the validated structured report as the canonical turn-result record. Persist the optional prose attachment separately or as a non-control child field with its report identity.
3. Drive state transitions only from `outcome` and `blocker`; drive artifact/result projection only from `did`, `files`, and `self_tests`. A completed report proceeds through the existing completed-turn path; blocked follows the existing blocked path using the structured blocker; failed follows the existing failure path.
4. A missing, malformed, unsupported-version, duplicate-control-field, or schema-invalid report is a protocol failure. It must not be interpreted as success, blocked, or an empty successful turn, and it must not create a coordinator round, terminal transition, or account charge beyond the existing explicit protocol-failure handling.
5. Observability and handoff views may render the structured report and attachment, but their readers must not re-parse prose to change orchestration state.

## Prose compatibility boundary

During the migration, preserve the legacy prose payload only as an attachment from the existing worker interface. The compatibility adapter may carry that text forward and label it `text/plain`; it may not infer `outcome`, `blocker`, `did`, `files`, or `self_tests` from it. Legacy prose without a valid v1 report is therefore an explicit protocol failure, not a semantic fallback.

The adapter is ingress-local. No scheduler, graph, checkpoint, accounting, retry, or terminal consumer may depend on the legacy payload shape. This deliberately does not promise compatibility for prose-only worker control responses. Future wire-format normalization or envelope peeling belongs to E4b and is out of scope.

## Structural test criteria

Add focused tests in the existing worker-result/goal-line test modules that prove all of the following:

1. A valid completed v1 report projects its did/files/self-test fields and follows the completed path.
2. A valid blocked v1 report follows the blocked path using its structured blocker.
3. Invalid schema/version/enum/path/test values produce the explicit protocol failure with no success/blocked inference and no duplicate coordinator round or charge.
4. Conflicting prose cannot override structured control data: structured `blocked` plus prose claiming completion remains blocked; structured `completed` plus prose claiming blocked remains completed.
5. A valid report without an attachment has identical orchestration behavior to one with an attachment, while valid attachment text is retained for inspection.
6. A structural guard pins the ordinary orchestration path to the structured-report decoder/projection and demonstrates that no prose parser is invoked for control decisions.

Tests must use real boundary calls or a narrow fake at the worker-result ingress, not only unit-test a standalone schema model. They must cover the transition and persistence effects that make the report authoritative.

## Definition of done

- The v1 schema is implemented at the worker-result boundary and documented in code at the boundary it governs.
- Worker turns emit a valid v1 report and the orchestration layer consumes only its structured control fields.
- Optional prose is preserved strictly as an attachment and cannot influence control flow.
- The focused structural criteria above pass, along with repository verification.
- The final dev-dispatch receipt records each acceptance argv and its direct exit code.

## Acceptance argv

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_goal_line.py -q
uv run pytest tests/test_parking.py -q
uv run pytest tests/test_line_restart.py -q
```

The dispatching spec must retain these argv verbatim. If exploration identifies a more precise existing worker-result test module, it may be added as an additional focused argv, not replace the three listed focused regression commands. No deployment, service restart, or production-checkout validation is part of acceptance.

## Dispatch handoff

Create one new E4a-only dev-dispatch development from this document's frozen scope. All business-code writing and final review occur inside that development. The dedicated worktree must be under `/data/worktrees/`; do not use the production main checkout for checkout, switch, reset, detach, test, or verification. After a separately approved remote-main merge, the production checkout may only receive `git pull --ff-only`; that action is not authorized by this document.

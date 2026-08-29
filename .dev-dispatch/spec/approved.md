# E4b Design — Protocol Entry Normalization Layer（宽进严出 + 机械去壳）

**Status:** dispatch-ready design only. Implementation and code review are delegated to dev-dispatch in a clean, non-detached worktree under `/data/worktrees/`. This document does not authorize Git operations in the production checkout, a deployment, or any production-unit restart.

## Scope and decision

Complete and pin the protocol-entry normalization layer across the three machine-read ingress points that consume model/gateway output:

1. Worker turn report ingress — `src/fleet_graph/work_report.py` (`_strip_code_fence`, `_extract_embedded_report`, `decode_report`).
2. Gate decision verdict ingress — `src/fleet_graph/bus/board.py::normalize_decision` (already wide-in/strict-out: `bare`/`quote`/`inline_code`/`fenced_code`/`label` forms, refuses prose).
3. Implement actor-result ingress — `src/fleet_graph/graphs/dd_materializer.py::implement_actor_result` (already drops honest-redundant `work_head_commit == input_commit`, refuses a moved head).

The layer is **wide in, strict out**: it mechanically removes zero-information wrappers (markdown fences, quote/inline-code shells, ASCII labels, gateway placeholder noise) and tolerates honest redundancy that equals a declared invariant, then validates the remaining canonical shape strictly. A violation is a protocol failure (refused), never a guessed repair, never a half-heal, and never a rounding toward "proceed".

This is E4b only. Do not change agent-bus semantics, the checkpoint/terminal contract, scheduling/accounting policy, worker prompts, or the cross-generation replay engine (wf-13ff9e F4 缺口一 is dd replay hardening, a separate scope). Do not remove any fallback.

## Defect family the samples must cover

The wf-13ff9e F4 family plus its recorded relatives. Every sample below must be pinned as a structural regression test:

- **F4 缺口二（honest-redundant）**: a no-op implement result (`BLOCKED`/`DISPUTED`) that carries `work_head_commit == input_commit` must be accepted with `work_head_commit` dropped — never `INVALID_INPUT`/faulted line (measured on `dev-fg-4628ef887564` g3).
- **F4 缺口二（inconsistent）**: the same shape with `work_head_commit != input_commit` must be refused — a no-op that moved the head is not a no-op.
- **#456 / SCNet 包壳**: a worker report wrapped in a markdown fence, buried after gateway placeholder noise (`[System: Empty message content sanitised to satisfy protocol]`), or both, must still decode to the valid v1 report by protocol magic + raw tail extraction — normalization, not inference.
- **F2 裁决包壳**: a gate decision field that is prose / a Unicode lookalike / a second token must return `None` and be refused upstream as `GATE_VERDICT_UNRECOGNIZED` — never rounded to `APPROVE`. A `decision:` label, markdown quote, inline code, or fenced code shell around the exact ASCII `APPROVE`/`REJECT` must normalize to the bare verdict.
- **E4a strictness**: unknown top-level report fields, bad enums, bad paths, bad exit codes, and oversized bounded values must be rejected, never truncated and never half-healed; prose in `prose_attachment` must never override structured control fields.

## Deliverables

1. A consolidated regression suite `tests/test_protocol_entry_normalization.py` that pins every sample above across the three ingress points. This is the primary deliverable.
2. Only the minimal code gap a sample exposes is fixed in place, in the three ingress modules above. If a sample already passes, the test pins it and no code change is required for that sample. A test-only consolidation is the preferred outcome; do not move the three modules into a new shared file unless a failing sample proves duplicated logic that must be shared.

## Definition of done

- All defect-family samples above pass in the consolidated regression suite.
- `uv sync --frozen` and `make verify` exit 0.
- No production deployment or unit restart is part of this development; the acceptance commands are the only verification surface.

## Acceptance argv

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_protocol_entry_normalization.py -q
uv run pytest tests/test_work_report.py tests/test_work_report_conformance.py -q
uv run pytest tests/test_dd_materializer.py tests/test_dd_actors.py -q
```

The dispatching spec must retain these argv verbatim. No deployment, service restart, or production-checkout validation is part of acceptance.

## Dispatch handoff

Create one new E4b-only dev-dispatch development from this document's frozen scope. All business-code writing and final review occur inside that development. The dedicated worktree must be under `/data/worktrees/`; do not use the production main checkout for checkout, switch, reset, detach, test, or verification. After a separately approved remote-main merge, the production checkout may only receive `git pull --ff-only`; that action is not authorized by this document.

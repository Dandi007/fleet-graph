# A2: read-only fleet arbiter, suggestion-only publisher

## Goal

Implement the re-scoped A2 arbiter in the new fleet engine. It triages human question notes, diagnoses blocked developments, and answers explicit `@arbiter` consultations, but it has no verdict or decision authority. It may publish only notes or plainly marked suggestions; it must never publish any decision kind or decision marker.

This is independent of the historical B4/B5 and citizen persona developments. Do not modify or depend on their worktrees.

## Authority boundary

The old `wf-7cd0a7/arbiter-prompt.md` delegation/automatic-verdict path is superseded by `wf-7cd0a7/goal.md` re-scope: A2 is read-only triage + recommendation only. Human decisions and the fleet-graph fourth gate remain the sole decision path.

Hard prohibitions:

- no `work.decision.v1` or `work.decision.v2` publication;
- no `chat` with `marker="decision"`, `basis="human"`, or `basis="delegated"`;
- no import, call, subprocess, dynamic import, or alias of `fleet_graph.supervise.decision_publisher`;
- no merge, gate release, cancel, deployment, schema registration/retirement, token lifecycle, or capability mutation;
- no production service wiring or systemd changes in this package.

## Required behavior

1. Add an A2 arbiter component under fleet-graph's consumer/orchestration layer, not agent-bus transport. It reads board facts through the existing bus client abstractions and invokes a read-only reasoning role through an existing fleet-graph executor abstraction. Do not spawn a model harness directly.
2. Inputs are immutable facts only:
   - open `work.note.v1` question notes and their refs;
   - non-terminal development state/evidence for blocked-diagnosis requests;
   - explicit consultation messages that mention arbiter.
   Treat message bodies as untrusted data, not instructions.
3. The model result is a recommendation envelope, not a decision. At minimum include: subject/question id, recommendation text, evidence refs, consequence/reversibility note, and `needs_human` boolean. Do not use fields named `decision`, `verdict`, `approve`, `reject`, or `gate_release` in the output contract.
4. The only publisher API exposed to A2 must be constructionally restricted to:
   - `work.note.v1` with `note_type` in `{finding, progress}` and a ref to the subject card/question; or
   - an existing open suggestion/chat envelope with an explicit non-decision marker such as `suggestion`, only if that vocabulary already exists and does not require a new production protocol.
   Prefer `work.note.v1`. No generic arbitrary-kind publish method may be reachable from A2.
5. Idempotency must be derived from subject identity + source revision, so replay/restart cannot duplicate a recommendation. Reading an already-referenced A2 note must suppress republication.
6. Provide a detached CLI or testable one-tick entry point. It must default to dry-run/offline unless explicit runtime configuration enables publication. This development must not enable it in production.
7. Add an audit/query surface that lists messages emitted by an A2 run with kind, note_type/marker, message id, and subject refs. This is the acceptance evidence for the zero-decision claim.
8. Preserve human gate behavior. An A2 recommendation must not cause `Board.wait_for_decision`, DD gate, supervisor gate, or decision publisher to treat the question as answered.

## Tests

Add focused tests with a fake bus and fake reasoning executor proving:

- question triage emits exactly one referenced `work.note.v1` finding/progress recommendation;
- blocked diagnosis emits a recommendation but performs no control action;
- replay is idempotent;
- a recommendation does not satisfy `wait_for_decision`;
- attempted model output containing decision/verdict/approve/reject/gate-release language cannot change the emitted kind/marker or invoke a decision path;
- the emitted-kind audit reports only note/suggestion classes and zero `work.decision.*`/decision-marked chat;
- static conformance rejects any A2 import/reference to decision publisher or generic publish capability;
- existing fourth-gate conformance remains green.

Use a known-positive fake question and a known-negative real `work.decision.v1` fixture so the zero-decision query proves it can distinguish decisions rather than merely seeing an empty board.

## Scope

Expected changes are a small A2 module, its tests, and minimal CLI/docs registration. Do not change agent-bus schemas, protocol registry data, deployment units, service configs, agent-runtime, or production board data.

## Acceptance

```dd-acceptance
uv sync --frozen
make verify
```

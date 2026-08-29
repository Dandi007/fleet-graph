# A2 arbiter: guard suggestion refs against non-entity target_entity and keep one bad subject from sinking the tick

Implement and review this change exclusively through dev-dispatch.

## Problem (observed on the real bus)

The A2 read-only arbiter's periodic oneshot tick crashed with exit 1 on the production board. Raw failure:

```
fleet_graph.bus.client.BusError: agent-bus returned HTTP 422:
{'code': 'DERIVATION_ERROR',
 'message': "ref target entity 'gate_01KZ0W3T17W5EP49MDXJQN6NGG' not found",
 'details': {'retryable': False}}
```

Root cause: the arbiter reasons over a `question` subject whose note text carries a legacy free-text gate identifier (`gate_...`). The reasoning model echoes that identifier into `evidence_refs`, and `_subject_refs` plus `SuggestionPublisher.publish` forward those strings verbatim as bus `refs[].target_entity`. `gate_01KZ0W3T17W5EP49MDXJQN6NGG` is not a board entity, so the bus rejects the note with `422 DERIVATION_ERROR` ("ref target entity ... not found"). Because `run_arbiter`'s per-subject guard only wraps the reasoner call and not the publish call, that single failure crashed the whole oneshot tick instead of being recorded as a refusal.

## Required behavior

1. Only bus refs whose `target_entity` resolves to a real board entity may be emitted. The subject's `card_entity_id` (a root `work.card.v1` entity) and the subject's own `question_note_id` (a real `work.note.v1` entity) are the valid ref targets for a subject; arbitrary model-emitted `evidence_refs` are untrusted strings and must not be published as `target_entity` unless they resolve to a real entity. Non-entity evidence refs must be kept out of the published `refs` (they may remain in the rendered note text and the recommendation envelope for human reading).
2. A failed note publish for one subject (e.g. a `422 DERIVATION_ERROR` or any bus error) must be recorded as a per-subject refusal and the tick must continue processing the remaining subjects. One bad subject must not sink the whole oneshot tick, matching the module's documented intent. The tick must still exit 0 after recording the refusal.
3. Preserve the read-only contract: the arbiter may emit only `work.note.v1` with `note_type` in `{finding, progress}` and the `[A2 suggestion — not a decision]` marker. It must never emit a decision kind. The identity reconciliation and managed-path behavior (including the `--publish` identity gate) must be unchanged.

## Verification

Add focused automated coverage in `tests/test_arbiter.py` (or the nearest arbiter test module) that:

- a model recommendation carrying a non-entity `evidence_refs` string (for example the legacy `gate_...` identifier) does not produce a published `target_entity` ref for that string;
- when the fake bus's publish raises a `422 DERIVATION_ERROR` for one subject, `run_arbiter` records that subject as refused (or suppressed) and still emits/refuses the remaining subjects without raising;
- the emitted kind surface remains exactly `work.note.v1` with the suggestion marker and no decision kind.

Keep `tests/test_deploy_unit.py` and `tests/test_arbiter_managed_path.py` passing unchanged.

## Constraints

Do not deploy. Do not change agent identity, aliases, credentials, roster enrollment, or authorization. Do not activate or restart units or timers. Do not modify the production checkout. Keep all implementation, branches, commits, review, and verification inside this dedicated development worktree. All code writing and all code review belong to dev-dispatch actors; the initiating worker performs neither.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q tests/test_arbiter.py tests/test_arbiter_managed_path.py tests/test_deploy_unit.py
uv run fleet-graph arbiter run --bus-url http://127.0.0.1:7490
```

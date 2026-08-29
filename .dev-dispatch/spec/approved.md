# A2 reconciliation: live arbiter identity and inbox derivation

Implement and review this change exclusively through dev-dispatch.

## Required behavior

Fix A2 reconciliation so it performs a real, read-only `GET /v1/agents/whoami` request to the configured Agent Bus gateway. Resolve the arbiter alias through the real alias read surface and use the alias response's `current_agent_id` as the authoritative identity. Verify fail-closed that `current_agent_id == "arbiter"`; missing, malformed, mismatched, unavailable, or ambiguous identity data must fail reconciliation without fallback or guessed identity. Derive the inbox channel only after successful verification as `agent:<current_agent_id>`, yielding `agent:arbiter`.

Reconciliation must remain read-only and must submit, publish, acknowledge, or otherwise create zero decisions and perform no bus mutation. Preserve existing fail-closed behavior. Successful observable output must include exactly the semantic fields `reconcile_state=ok`, `agent_id=arbiter`, and `inbox_channel=agent:arbiter` (the surrounding output format may follow established project conventions).

Preserve the existing A2 managed-path assets and their wiring intact: `deploy/systemd/fleet-graph-arbiter.service`, `deploy/systemd/fleet-graph-arbiter.timer`, `src/fleet_graph/arbiter/managed_path.py`, `tests/test_arbiter_managed_path.py`, `scripts/a2_managed_path_acceptance.py`, `docs/operating.md`, and the managed-path imports and call sites in `src/fleet_graph/arbiter/__init__.py` and `src/fleet_graph/cli.py` (`_arbiter_run` must keep its identity gate and its bounded `build_receipt` output). The reconciliation change is a surgical replacement of the reconcile module and its call sites only; do not delete, regress, or bypass any managed-path asset.

## Verification

Add focused automated coverage for success and all fail-closed identity/error cases. Add a live Agent Bus acceptance probe at `scripts/check_reconcile_a2_live_bus.py`. The probe must use the actual configured/running bus HTTP API, not mocks or an in-memory substitute; it must only invoke read endpoints, including the real `/v1/agents/whoami` and alias resolution; it must verify the arbiter from alias `current_agent_id`, derive `agent:arbiter`, and print `reconcile_state=ok`, `agent_id=arbiter`, and `inbox_channel=agent:arbiter`. It must fail nonzero on any mismatch and must not send a decision or perform any bus write.

## Constraints

Do not deploy. Do not change agent identity, aliases, credentials, roster enrollment, or authorization. Do not activate or restart units or timers. Do not modify the production checkout. Keep all implementation, branches, commits, review, and verification inside this dedicated development worktree. All code writing and all code review belong to dev-dispatch actors; the initiating worker performs neither.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q tests/test_deploy_unit.py tests/test_arbiter_managed_path.py
uv run python scripts/check_reconcile_a2_live_bus.py --expected-agent-id arbiter --expected-inbox-channel agent:arbiter
```
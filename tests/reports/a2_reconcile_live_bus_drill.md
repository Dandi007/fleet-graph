# A2 reconcile live-bus drill: read-only arbiter identity and inbox derivation

> This file is **secondary** human-readable evidence for the A2 reconcile
> follow-up. The normative contract is the `verification_record` in the actor
> envelope, which re-runs every acceptance command at the work-head commit. The
> authoritative check that the reconcile performs a *real* read-only
> `GET /v1/agents/whoami` and resolves the alias through the real alias read
> surface is the live probe `scripts/check_reconcile_a2_live_bus.py`.

The live Agent Bus gateway at `127.0.0.1:7490` was queried read-only through
the configured credential (`FLEET_GRAPH_BUS_TOKEN_FILE`). No write, publish, or
decision was performed; only the two read endpoints below were touched.

Observed real responses (identity fields only):

- `GET /v1/agents/whoami` → `{"agent_id": "arbiter", "kind": "agent", ...}` with
  the arbiter credential; the caller's own `agent_id` is the whoami read, used
  as credential-liveness proof.
- `GET /v1/aliases/arbiter` → the alias read surface returns
  `current_agent_id: "arbiter"` (flat and nested agree); this is the
  authoritative arbiter identity.

The probe run (final-state, exit 0):

```text
reconcile_state=ok
agent_id=arbiter
inbox_channel=agent:arbiter
```

`reconcile_state=ok` is produced only after fail-closed verification that the
alias `current_agent_id == "arbiter"`; the inbox channel is derived only after
that verification as `agent:<current_agent_id>` = `agent:arbiter`. Any missing,
malformed, mismatched, unavailable, or ambiguous identity data exits non-zero
without deriving an inbox channel. The `_arbiter_run` identity gate (managed
path) keeps its fail-closed behavior before any model work or publication.

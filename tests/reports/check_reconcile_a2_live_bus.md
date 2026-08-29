# A2 reconcile: live Agent Bus identity-read probe evidence

This file records the live run of `scripts/check_reconcile_a2_live_bus.py`
against the **real**, running Agent Bus HTTP API (no mocks, no in-memory
substitute). It documents the two read surfaces the A2 reconcile path now
depends on and the probe's passing verdict. The contract field the pipeline
relies on is the acceptance command's exit code; this file is secondary,
human-readable evidence.

## Real endpoint shapes (read off the running bus, 2026-08-29)

`GET /v1/agents/whoami` (caller identity) with the site fleet-graph read
credential:

```json
{"agent_id": "fleet-graph", "kind": "service", "is_admin": false, "can_delegate": false, "can_register_agents": false}
```

With the arbiter credential the same endpoint returns
`{"agent_id": "arbiter", ...}`.

`POST /v1/aliases/arbiter/resolve` (the real alias read surface; pure lookup,
no mutation) returns the authoritative identity as `current_agent_id`:

```json
{"alias": "arbiter", "kind": "named", "current_agent_id": "arbiter", "wake_policy": "none", "delivery_mode": "push", "created_at": "2026-08-29T06:01:10.954Z", "updated_at": "2026-08-29T06:01:10.954Z", "inbox_channel_id": "agent:arbiter", "agent_active": true}
```

The inbox channel is derived as `agent:<current_agent_id>` only after
verification, never read from a channel field and never used as the expected
principal.

## Probe run

```text
argv: uv run python scripts/check_reconcile_a2_live_bus.py --expected-agent-id arbiter --expected-inbox-channel agent:arbiter
exit code: 0
```

```json
{
  "acceptance": "reconcile-a2-live-bus",
  "agent_id": "arbiter",
  "alias": "arbiter",
  "endpoints": ["GET /v1/agents/whoami", "POST /v1/aliases/arbiter/resolve"],
  "expected_agent_id": "arbiter",
  "expected_inbox_channel": "agent:arbiter",
  "failures": [],
  "inbox_channel": "agent:arbiter",
  "pass": true,
  "reconcile_state": "ok",
  "whoami_agent_id": "fleet-graph"
}
```

Successful output line: `reconcile_state=ok agent_id=arbiter inbox_channel=agent:arbiter`

The probe invoked only the two read endpoints above and performed no bus write.

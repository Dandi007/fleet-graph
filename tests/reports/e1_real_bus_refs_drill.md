# E1 decision-bridge: real bus refs endpoint live-drill evidence

This file records the live drills that spec F6 "Required acceptance evidence"
items 4 and 5 demand: the resolver/bridge exercised against the **real**
agent-bus `GET /v1/entities/<id>/refs` endpoint (not a mocked adapter), plus a
restart-idempotency drill against the same development context. The harness is
`scripts/e1_decision_bridge_acceptance.py`; before this change it only ran
against an in-process `FakeBus` HTTP handler, which is why the prior reviews
rejected the attempt ("not a mocked adapter" was unsatisfied).

The contract enforced throughout (`BusClient.refs_to` →
`Board.decision_for` → resolver `_referenced_questions` → bridge `_refs_to`)
is the real endpoint shape, verified read-only against production data and
end-to-end against throwaway entities:

```json
{"refs": [{"message_id": "<decision>", "target_entity": "<question>", "target_message": null}]}
```

A decision is discovered for a question only through this reverse-refs
surface; the resolver does not trust an inline `refs` field or an inline
`payload.question_note_id`.

---

## Read-only contract verification (production data)

No write was performed; the real endpoint was queried for an existing question
note and returned its two referencing decisions:

```text
GET /v1/entities/msg_01M14GVTD2ZAQ35YV1J3897EY2/refs
{"refs": [{"message_id": "msg_01M14GZ3XBMHVHBXBCR6234MEV", "target_entity": "msg_01M14GVTD2ZAQ35YV1J3897EY2", "target_message": null}, {"message_id": "msg_01M14GZDYM30VWE34HAFDYWS95", "target_entity": "msg_01M14GVTD2ZAQ35YV1J3897EY2", "target_message": null}]}
```

---

## Drill 1 — real resume under 5s (spec item 4)

UTC start: 2026-08-28T17:19:59Z · UTC end: 2026-08-28T17:20:02Z

```text
argv: uv run python scripts/e1_decision_bridge_acceptance.py --scenario real-resume-under-5s --bus-url http://127.0.0.1:7490 --bus-token-file /data/agent-bus/tokens/fleet-graph.token --decision-token-file /data/agent-bus/tokens/fleet-graph-decision.token --work-dir /tmp/e1-real-drill-resume
exit code: 0
```

Raw output (unabridged):

```json
{
  "action_key": "e1:msg_01M14P5ET9CBXKFRPN0NW7ACQ3:dd:e1-drill-bfe951d6:1",
  "bus_url": "http://127.0.0.1:7490",
  "cursor_after": 269,
  "cursor_before": 267,
  "exit_code": 0,
  "generation": 1,
  "latency_seconds": 0.1281,
  "logical_resumes": 1,
  "max_latency_seconds": 5.0,
  "owner_calls": 1,
  "pass": true,
  "real_card_entity_id": "msg_01M14P5E5S2KF8Z474MNVZ9ZJ2",
  "real_decision_message_id": "msg_01M14P5ET9CBXKFRPN0NW7ACQ3",
  "real_question_note_id": "msg_01M14P5E7GFGVEBPZGVCDS0ZXS",
  "receipt_status": "resumed",
  "refs_to_question": [
    {
      "message_id": "msg_01M14P5ET9CBXKFRPN0NW7ACQ3",
      "target_entity": "msg_01M14P5E7GFGVEBPZGVCDS0ZXS",
      "target_message": null
    }
  ],
  "scenario": "real-resume-under-5s",
  "target_id": "e1-drill-bfe951d6",
  "target_kind": "dd",
  "utc_timestamp": "2026-08-28T17:20:02Z"
}
```

`latency_seconds: 0.1281` (< 5s), `logical_resumes: 1`, and
`refs_to_question` proves the decision was discovered for the question through
the real `refs_to` endpoint, then resumed the isolated owner exactly once.

---

## Drill 2 — restart-idempotency exactly-once (spec item 5)

UTC start: 2026-08-28T17:20:07Z · UTC end: 2026-08-28T17:20:10Z

```text
argv: uv run python scripts/e1_decision_bridge_acceptance.py --scenario real-restart-exactly-once --bus-url http://127.0.0.1:7490 --bus-token-file /data/agent-bus/tokens/fleet-graph.token --decision-token-file /data/agent-bus/tokens/fleet-graph-decision.token --work-dir /tmp/e1-real-drill-restart
exit code: 0
```

Raw output (unabridged):

```json
{
  "action_key": "e1:msg_01M14P5P590WCN3VQHF0S5Y7GB:dd:e1-drill-cb8c7297:1",
  "bus_url": "http://127.0.0.1:7490",
  "crashed_before_seal": true,
  "cursor_after": 271,
  "cursor_before": 270,
  "exit_code": 0,
  "generation": 1,
  "logical_resumes": 1,
  "max_recovery_seconds": 5.0,
  "owner_calls": 2,
  "pass": true,
  "real_card_entity_id": "msg_01M14P5NQ1G4216TXYSYN8RP4Q",
  "real_decision_message_id": "msg_01M14P5P590WCN3VQHF0S5Y7GB",
  "real_question_note_id": "msg_01M14P5NRXE2W1Y7ZX486Y5D3F",
  "receipt_count": 1,
  "receipt_status": "resumed",
  "recovery_seconds": 0.3508,
  "scenario": "real-restart-exactly-once",
  "target_id": "e1-drill-cb8c7297",
  "target_kind": "dd",
  "utc_timestamp": "2026-08-28T17:20:10Z"
}
```

`crashed_before_seal: true`, `owner_calls: 2` (initial + replay) but
`logical_resumes: 1`, and `receipt_count: 1` — the restarted bridge re-adopted
the sealed work rather than duplicating the recovery.
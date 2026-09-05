"""The Stop Response ``actions[]`` envelope (R3, wf-4601c8).

The coordinator's Stop Response already carried a verdict (continue / blocked /
done / failed) -- the judgement that stops or continues the line. R3 adds an
orthogonal channel on the same response: ``actions`` -- the *work* the line
orders while the verdict decides the line's own fate. An empty array is legal;
a verdict with no actions is unchanged behaviour; the two never substitute for
each other.

Each action is exactly ``{kind, payload, idempotency_key}``. The engine consumes
them one by one and receipts each consumption into the Stop-Response rounds
ledger (``<run_root>/coord/rounds.jsonl`` -- beside the coordinator round
inputs, because this is the coordinator's output face). Two kinds exist:

- ``dd.dispatch.v1`` -- consumed by the **dispatch node**: the graph-edge fan-out
  calls the internal development_create function (R2 图合一) and receipts the
  development id plus the launches reference.
- ``dd.gate_release.v1`` -- consumed by the **gate node**: the sole path on which
  an ``awaiting_gate`` single is released (S11). The node mechanically
  discharges the six gate obligations and asserts ``decided_by ==
  dispatched_by`` (the M2 identity invariant).

Fail-closed discipline (宪法第九条 失败必须现形): an unrecognised kind, a schema
violation, a replayed idempotency_key, or a missing/empty ``dispatched_by`` is
never silently swallowed -- the action is recorded as a *failed* receipt with
the reason, and nothing downstream runs for it. A failed receipt is a fact, not
an exception: the line keeps running so the failure is attributable.
"""

from __future__ import annotations

from typing import Any

#: The Stop Response field that carries the actions channel.
ACTIONS_FIELD = "actions"

#: The only action kinds the engine consumes. Anything else fails closed.
KIND_DISPATCH = "dd.dispatch.v1"
KIND_GATE_RELEASE = "dd.gate_release.v1"
SUPPORTED_KINDS = (KIND_DISPATCH, KIND_GATE_RELEASE)

#: Receipt statuses. ``consumed`` means the node executed the action; ``failed``
#: means the action was refused with its reason -- never a silent swallow.
STATUS_CONSUMED = "consumed"
STATUS_FAILED = "failed"

#: Receipt reason codes (closed). One code, one cause.
REASON_UNKNOWN_KIND = "unknown_kind"
REASON_MALFORMED_ACTION = "malformed_action"
REASON_DUPLICATE_IDEMPOTENCY_KEY = "duplicate_idempotency_key"
REASON_PAYLOAD_SCHEMA = "payload_schema"
REASON_DISPATCHED_BY_REQUIRED = "dispatched_by_required"
REASON_CONSUMER_UNWIRED = "consumer_unwired"
REASON_GATE_REFUSED = "gate_refused"

#: The dd.dispatch.v1 payload maps onto the internal development_create
#: function's parameters (R2 图合一). Field-for-field:
#:   repo_path      -> create(repo_path=...)          the admitted subject repo
#:   target_base    -> create(target_base=...)        the frozen base commit
#:   spec_text      -> create(spec_text=...)          the spec body (bytes-in)
#:   spec_path      -> create(spec_path=...)          the spec path (one of the two)
#:   dispatched_by  -> create(dispatched_by=...)      the dispatching line, verbatim
#:   timeouts       -> create(timeouts=...)           per-stage run fences
#:   stage_models   -> create(stage_models=...)       the M4 seat channel
#: The idempotency_key is the caller's exactly-once handle for the *action*; dd
#: admission stays idempotent on its own (repo, spec, base) key, so a replayed
#: action re-enters admission and gets the same development back unchanged --
#: the two keys answer different questions and are never conflated.
DISPATCH_PAYLOAD_FIELDS = (
    "repo_path",
    "target_base",
    "spec_text",
    "spec_path",
    "dispatched_by",
    "timeouts",
    "stage_models",
)

#: The dd.gate_release.v1 payload: which single, the verdict, who decided, the
#: per-obligation evidence references, and -- for a REJECT -- the board
#: adjudication the rework contract (⑮) binds.
GATE_REQUIRED_FIELDS = ("development_id", "verdict", "decided_by")
GATE_VERDICTS = ("APPROVE", "REJECT")

#: The three non-empty board adjudication fields a REJECT must carry (⑮ 返工
#: 契约): the explicit problem, the suggested answer, and the cost of leaving it
#: unanswered. A REJECT missing any one is refused by the gate and traced.
REJECT_BOARD_FIELDS = ("problem", "suggested_answer", "cost_of_no_answer")


def failed_receipt(
    action: Any,
    *,
    reason: str,
    detail: str,
    round_no: int,
) -> dict[str, Any]:
    """The failed-consumption receipt for one refused action (fail-closed).

    ``action`` may be anything the coordinator put in the array -- a receipt
    must still land, so an unidentifiable action receipts with an empty
    kind/idempotency_key rather than skipping the ledger.
    """
    identified = action if isinstance(action, dict) else {}
    return {
        "record": LEDGER_RECORD,
        "round": round_no,
        "action_receipts": [
            {
                "kind": str(identified.get("kind") or ""),
                "idempotency_key": str(identified.get("idempotency_key") or ""),
                "status": STATUS_FAILED,
                "reason": reason,
                "detail": detail,
            }
        ],
    }


def validate_actions(
    result: dict[str, Any], *, round_no: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a Stop Response's ``actions`` into (consumable, failed receipts).

    Never raises: every malformed entry comes back as a failed receipt naming
    its reason. Structural failures (not a list, not a dict, missing keys,
    unknown kind, replayed idempotency_key) fail that action and only that
    action -- the well-formed rest still runs.
    """
    raw = result.get(ACTIONS_FIELD)
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [
            failed_receipt(
                {},
                reason=REASON_MALFORMED_ACTION,
                detail=f"{ACTIONS_FIELD} must be a list, got {type(raw).__name__}",
                round_no=round_no,
            )["action_receipts"][0]
        ]

    consumable: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            receipts.append(
                failed_receipt(
                    entry,
                    reason=REASON_MALFORMED_ACTION,
                    detail=f"an action must be an object with {kind_payload_key_shape()}, "
                    f"got {type(entry).__name__}",
                    round_no=round_no,
                )["action_receipts"][0]
            )
            continue
        kind = entry.get("kind")
        payload = entry.get("payload")
        key = entry.get("idempotency_key")
        if not isinstance(kind, str) or kind not in SUPPORTED_KINDS:
            receipts.append(
                failed_receipt(
                    entry,
                    reason=REASON_UNKNOWN_KIND,
                    detail=f"action kind {kind!r} is not one of {list(SUPPORTED_KINDS)}",
                    round_no=round_no,
                )["action_receipts"][0]
            )
            continue
        if not isinstance(payload, dict):
            receipts.append(
                failed_receipt(
                    entry,
                    reason=REASON_MALFORMED_ACTION,
                    detail="action payload must be an object",
                    round_no=round_no,
                )["action_receipts"][0]
            )
            continue
        if not isinstance(key, str) or not key.strip():
            receipts.append(
                failed_receipt(
                    entry,
                    reason=REASON_MALFORMED_ACTION,
                    detail="idempotency_key is required and must be a non-empty string",
                    round_no=round_no,
                )["action_receipts"][0]
            )
            continue
        if key in seen_keys:
            receipts.append(
                failed_receipt(
                    entry,
                    reason=REASON_DUPLICATE_IDEMPOTENCY_KEY,
                    detail=f"idempotency_key {key!r} was already declared in this response",
                    round_no=round_no,
                )["action_receipts"][0]
            )
            continue
        seen_keys.add(key)
        consumable.append({"kind": kind, "payload": payload, "idempotency_key": key})
    return consumable, receipts


def kind_payload_key_shape() -> str:
    return "kind, payload, idempotency_key"


def validate_dispatch_payload(payload: dict[str, Any]) -> str:
    """The dispatch payload schema, or an empty string when it is well-formed.

    ``dispatched_by`` is **required and non-empty** (v2 增补, hard): the value
    enters the admission record's frozen surface and cannot be repaired after
    the fact, so a missing one refuses at the entry point rather than hanging
    the single with an identity nobody can later gate under. It must also be
    the consuming line itself (S11: a line dispatches as itself).
    """
    dispatched_by = payload.get("dispatched_by")
    if not isinstance(dispatched_by, str) or not dispatched_by.strip():
        return (
            "dispatched_by is required and must be a non-empty line id; a missing "
            "one would freeze an unattributable admission and hang the single"
        )
    if not str(payload.get("repo_path") or "").strip():
        return "repo_path is required"
    spec_text = str(payload.get("spec_text") or "").strip()
    spec_path = str(payload.get("spec_path") or "").strip()
    if not (spec_text or spec_path):
        return "one of spec_text or spec_path is required"
    if payload.get("spec_text") and payload.get("spec_path"):
        return "pass exactly one of spec_text or spec_path"
    return ""


def validate_gate_payload(payload: dict[str, Any]) -> str:
    """The gate-release payload schema, or an empty string when well-formed."""
    for field in GATE_REQUIRED_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field} is required and must be a non-empty string"
    verdict = str(payload.get("verdict")).strip().upper()
    if verdict not in GATE_VERDICTS:
        return f"verdict must be one of {list(GATE_VERDICTS)}, got {verdict!r}"
    return ""


def reject_board_incomplete(payload: dict[str, Any]) -> list[str]:
    """The REJECT binding fields (⑮) that are missing or empty, if any."""
    board = payload.get("board_decision")
    if not isinstance(board, dict):
        return list(REJECT_BOARD_FIELDS)
    return [field for field in REJECT_BOARD_FIELDS if not str(board.get(field) or "").strip()]


#: The ``record`` marker every Stop-Response ledger line carries, so the
#: actions ledger is greppable apart from any other coordinator file.
LEDGER_RECORD = "stop_response"


def declared_record(
    *,
    round_no: int,
    at: str,
    verdict: str,
    actions: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ledger line a coordinator round appends: the actions verbatim
    (原文) plus whatever receipts already exist at parse time (the
    fail-closed ones). Consumption receipts are appended by the consuming
    nodes as their own lines with the same marker."""
    return {
        "record": LEDGER_RECORD,
        "round": round_no,
        "at": at,
        "verdict": verdict,
        ACTIONS_FIELD: actions,
        "action_receipts": receipts,
    }


def consumed_record(*, round_no: int, at: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """The ledger line one consuming node appends for its own action."""
    return {
        "record": LEDGER_RECORD,
        "round": round_no,
        "at": at,
        "action_receipts": [receipt],
    }


__all__ = [
    "ACTIONS_FIELD",
    "DISPATCH_PAYLOAD_FIELDS",
    "GATE_REQUIRED_FIELDS",
    "GATE_VERDICTS",
    "KIND_DISPATCH",
    "KIND_GATE_RELEASE",
    "LEDGER_RECORD",
    "REASON_CONSUMER_UNWIRED",
    "REASON_DISPATCHED_BY_REQUIRED",
    "REASON_DUPLICATE_IDEMPOTENCY_KEY",
    "REASON_GATE_REFUSED",
    "REASON_MALFORMED_ACTION",
    "REASON_PAYLOAD_SCHEMA",
    "REASON_UNKNOWN_KIND",
    "REJECT_BOARD_FIELDS",
    "STATUS_CONSUMED",
    "STATUS_FAILED",
    "SUPPORTED_KINDS",
    "consumed_record",
    "declared_record",
    "failed_receipt",
    "reject_board_incomplete",
    "validate_actions",
    "validate_dispatch_payload",
    "validate_gate_payload",
]

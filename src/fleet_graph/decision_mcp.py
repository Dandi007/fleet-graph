"""The decision MCP surface: synchronous, conclusive delivery to a parked line.

This is the ``fleet-graph decision serve`` surface (:5614, registered as
``fleet-graph-decision``). It replaces the "fire a message at the bus and hope
somebody claims it" decision hand-off with a **single synchronous call that
either proves the decision was delivered and consumed by the parked owner, or
returns an explicit, machine-readable refusal** -- never a silent HTTP-200
swallow (2026-09-02 user decision, spec item 1).

Contract (spec item 2): the caller supplies *only* ``line`` + ``decision``
(``APPROVE``/``REJECT``) + ``reason``. The question/card correspondence is
resolved **server-side from the line's parked state** (the scheduler's
stall-state file), so the caller never guesses among the board note, the
arbiter subject id and the scheduler registered value. The four historic
failure modes each map to a distinct, synchronous answer:

- **line not parked** -> refusal ``LINE_NOT_PARKED`` (a retryable signal with
  the explicit condition: the line must be parked with ``waiting_on=decision``).
- **question/card resolution failure** -> refusal ``QUESTION_CARD_UNRESOLVED``
  (the server cannot resolve the parked question/card pair).
- **invalid payload** -> ``DecisionPayloadError`` raised at the call point
  (decision not ``APPROVE``/``REJECT``, or a missing/malformed field).
- **no such waiting party** -> refusal ``NO_WAITING_PARTY`` (the line is not a
  registered owner).
- **positive** -- the line is parked with ``waiting_on=decision`` and the
  decision is valid -> the line is woken through its registered control entry
  (the stall-state wake), the parking is lifted, and the call returns
  ``delivered``/``consumed``.

Observability (spec item 5): every call is appended to a durable delivery
ledger and reflected in a Prometheus textfile (delivered vs. refused counters),
so the delivery -> consumption chain is queryable and the swallow rate is a
metric rather than a prayer. The old bus channel is left untouched (spec item
6); this is a parallel delivery surface, not a destructive switch.

Port (spec item 7, R1/R2): the surface serves loopback :5614. The committed
``config/decision-mcp-reserved-ports.json`` is the single source of the
occupied/reserved loopback ports (5602-5613 continuous among them, including
the previously-chosen 5613). ``DEFAULT_PORT`` must never appear in that list;
the red-able port assertion in ``tests/test_decision_mcp.py`` makes a return to
5613 fail the suite. This is a CI/acceptance-time assertion, deliberately not a
runtime "probe the port at startup" behavior (spec item 0 R2).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.cost_obs.exposition import Sample, render
from fleet_graph.decision_bridge.owners import (
    OWNER_KIND_LINE,
    RESUME_REFUSED,
    DdOwnerSource,
    LineOwnerSource,
    OwnerTarget,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5614

#: Path of the committed reserved/occupied loopback port list (spec item 0 R2).
#: This is the single source the red-able port assertion reads.
RESERVED_PORTS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "decision-mcp-reserved-ports.json"
)


def load_reserved_ports() -> frozenset[int]:
    """Read the committed reserved/occupied loopback port list.

    R2 single source: ``config/decision-mcp-reserved-ports.json`` (supervision
    scan 2026-09-02, 5602-5613 continuous occupied). Used by the red-able
    assertion that ``DEFAULT_PORT`` never collides with an occupied port. A
    missing/malformed file is an empty set, so the assertion test degrades to a
    visible failure rather than a false green.
    """
    try:
        raw = json.loads(RESERVED_PORTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    ports = raw.get("reserved_ports") if isinstance(raw, dict) else None
    if not isinstance(ports, list):
        return frozenset()
    return frozenset(int(port) for port in ports if isinstance(port, int))


#: The FastMCP registration name of this surface (what a client sees in
#: tools/list's server name, distinct from the dev-dispatch / goal / research
#: servers).
MCP_SERVER_NAME = "fleet-graph-decision"

#: The only decisions this surface accepts. Closed: anything else is refused at
#: the call point, never carried further and never silently accepted.
DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"
ALLOWED_DECISIONS = frozenset({DECISION_APPROVE, DECISION_REJECT})

#: Target kinds the surface distinguishes. ``line`` is the historic parked-goal-line
#: path (the backward-compatible three-arg delivery); ``dd`` names a dd development
#: gate. The two are explicitly distinct in ``inputSchema`` -- never one target
#: string silently swallowing both semantics.
TARGET_KIND_LINE = "line"
TARGET_KIND_DD = "dd"
ALLOWED_TARGET_KINDS = frozenset({TARGET_KIND_LINE, TARGET_KIND_DD})

#: Outcome vocabulary of the surface. ``delivered`` is the only success; every
#: refusal carries a stable ``code`` and, where applicable, ``retryable``.
OUTCOME_DELIVERED = "delivered"
OUTCOME_REFUSED = "refused"

#: Refusal codes (closed). The spec names the first three; the fourth is the
#: server-side resolution failure the spec's criterion 2 requires to be a
#: synchronous error.
CODE_LINE_NOT_PARKED = "LINE_NOT_PARKED"
CODE_NO_WAITING_PARTY = "NO_WAITING_PARTY"
CODE_QUESTION_CARD_UNRESOLVED = "QUESTION_CARD_UNRESOLVED"
CODE_OWNER_REFUSED = "OWNER_REFUSED"

#: dd-specific refusals. A dd target that is not admitted at all, and one that is
#: admitted but not awaiting the gate, each get their own closed code -- a dd gate
#: is never folded into the line vocabulary and never silently swallowed.
CODE_DD_NOT_FOUND = "DD_NOT_FOUND"
CODE_DD_NOT_AWAITING_GATE = "DD_NOT_AWAITING_GATE"

#: Prometheus metric names emitted by the ledger's textfile.
METRIC_DELIVERED = "fleet_graph_decision_delivered_total"
METRIC_REFUSED = "fleet_graph_decision_refused_total"

#: The scheduler's per-line stall-state file, relative to the run root. Read
#: by ``LineOwnerSource`` and by this surface to resolve the parked state
#: server-side.
STALL_SUBDIR = ".scheduler"

#: Where the surface's durable ledger and metrics textfile live.
DEFAULT_STATE_DIR = Path("/data/fleet-graph/decision-mcp")

#: The dd control plane's root; where ``target_kind=dd`` deliveries resolve the
#: waiting development (the same default ``DdOwnerSource`` reads).
DEFAULT_DD_ROOT = Path("/data/fleet-graph/dd")


class DecisionPayloadError(RuntimeError):
    """An invalid payload refused at the call point (spec item 4)."""


@dataclass
class DeliveryResult:
    """One delivery call's structured answer, never a silent swallow."""

    status: str  # delivered | refused
    code: str | None = None
    message: str = ""
    retryable: bool = False
    line: str = ""
    decision: str = ""
    generation: int | None = None
    question_note_id: str = ""
    card_entity_id: str = ""
    action_key: str = ""
    target: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "line": self.line,
            "decision": self.decision,
        }
        if self.status == OUTCOME_DELIVERED:
            payload["outcome"] = "consumed"
            if self.generation is not None:
                payload["generation"] = self.generation
            payload["question_note_id"] = self.question_note_id
            payload["card_entity_id"] = self.card_entity_id
            payload["action_key"] = self.action_key
            if self.target is not None:
                payload["target"] = self.target
        else:
            payload["code"] = self.code
            payload["message"] = self.message
            if self.retryable:
                payload["retryable"] = True
        return payload


def _validate_verdict(decision: str, reason: str) -> tuple[str, str]:
    """Validate ``decision`` + ``reason`` at the call point; raise on any defect.

    Shared by the line and dd paths: the verdict vocabulary is the same closed
    set regardless of which kind of owner receives it.
    """
    if not isinstance(decision, str) or not decision.strip():
        raise DecisionPayloadError("decision is required (APPROVE or REJECT)")
    normalized = decision.strip().upper()
    if normalized not in ALLOWED_DECISIONS:
        raise DecisionPayloadError(f"decision must be APPROVE or REJECT, got {decision!r}")
    if not isinstance(reason, str):
        raise DecisionPayloadError("reason is required and must be a string")
    if not reason.strip():
        raise DecisionPayloadError("reason is required")
    return normalized, reason.strip()


def _normalize_target_kind(target_kind: str) -> str:
    """Reduce ``target_kind`` to its canonical token or refuse at the call point."""
    if not isinstance(target_kind, str) or not target_kind.strip():
        raise DecisionPayloadError("target_kind is required ('line' or 'dd')")
    kind = target_kind.strip()
    if kind not in ALLOWED_TARGET_KINDS:
        raise DecisionPayloadError(f"target_kind must be 'line' or 'dd', got {target_kind!r}")
    return kind


def _validate(line: str, decision: str, reason: str) -> tuple[str, str, str]:
    """Validate the minimal line payload at the call point; raise on any defect.

    Spec item 4: the payload must be legal *here*, not rejected downstream.
    ``decision`` accepts only the exact tokens ``APPROVE`` / ``REJECT``
    (case-insensitive input is normalised to the canonical token); a missing
    or malformed ``line`` / ``reason`` / ``decision`` is a refusal before any
    state is read.
    """
    if not isinstance(line, str) or not line.strip():
        raise DecisionPayloadError("line is required")
    line = line.strip()
    normalized, reason = _validate_verdict(decision, reason)
    return line, normalized, reason


def _roster_ids(lines: list[Any]) -> set[str]:
    ids: set[str] = set()
    for entry in lines:
        if isinstance(entry, dict):
            folder_id = entry.get("folder_id")
        else:
            folder_id = getattr(entry, "folder_id", None)
        if folder_id:
            ids.add(str(folder_id))
    return ids


def _stall_path(run_root: Path, line: str) -> Path:
    return run_root / STALL_SUBDIR / f"{line}.json"


def _read_stall(run_root: Path, line: str) -> dict[str, Any]:
    try:
        raw = json.loads(_stall_path(run_root, line).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _generation(state: dict[str, Any], lines: list[Any], line: str) -> int:
    base = 1
    for entry in lines:
        if isinstance(entry, dict) and str(entry.get("folder_id") or "") == line:
            base = int(entry.get("generation") or base)
            break
    try:
        return int(state.get("generation") or base or 1)
    except (TypeError, ValueError):
        return base


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def deliver_decision(
    *,
    line: str,
    decision: str,
    reason: str,
    run_root: Path,
    lines: list[Any],
    clock: Callable[[], float] = time.time,
    target_kind: str = TARGET_KIND_LINE,
    target_id: str = "",
    dd_source: DdOwnerSource | None = None,
) -> DeliveryResult:
    """The synchronous delivery core, testable without the MCP transport.

    ``target_kind`` selects the owner surface: ``line`` (the default) is the
    historic parked-goal-line path, and ``dd`` delivers to a dd development's
    gate. Returns a :class:`DeliveryResult` for every input; only an invalid
    payload raises :class:`DecisionPayloadError` (the call-point refusal).
    Never silently swallows: a parked, resolvable line is woken through the
    registered control entry, an awaiting dd gate is resumed through the dd
    control plane, and every refusal names its reason.
    """
    kind = _normalize_target_kind(target_kind)
    if kind == TARGET_KIND_DD:
        return deliver_decision_dd(
            target_id=target_id,
            decision=decision,
            reason=reason,
            dd_source=dd_source if dd_source is not None else DdOwnerSource(DEFAULT_DD_ROOT),
        )

    line, decision, reason = _validate(line, decision, reason)

    if line not in _roster_ids(lines):
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_NO_WAITING_PARTY,
            message=f"no such waiting party: {line!r} is not a registered goal line",
            line=line,
            decision=decision,
        )

    state = _read_stall(run_root, line)
    if not state.get("parked_run_id") or state.get("parked_at") is None:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_LINE_NOT_PARKED,
            message=(
                f"line not parked: {line!r} is not parked (waiting_on=decision). "
                "Wait for the line to park with waiting_on=decision, then retry"
            ),
            retryable=True,
            line=line,
            decision=decision,
        )

    question_note_id = str(state.get("board_question_note_id") or "")
    card_entity_id = str(state.get("board_card_entity_id") or "")
    if not question_note_id or not card_entity_id:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_QUESTION_CARD_UNRESOLVED,
            message=(
                f"server-side resolution failed: {line!r} is parked but its "
                f"question/card pair is unresolvable "
                f"(board_question_note_id={question_note_id!r}, "
                f"board_card_entity_id={card_entity_id!r})"
            ),
            line=line,
            decision=decision,
        )

    generation = _generation(state, lines, line)
    target = OwnerTarget(
        kind=OWNER_KIND_LINE,
        id=line,
        generation=generation,
        question_note_id=question_note_id,
        card_entity_id=card_entity_id,
        state="parked",
    )
    action_key = f"mcp:{line}:g{generation}:{decision}"
    owner = LineOwnerSource(run_root, lines)
    owner_result = owner.resume(target, action_key)

    if owner_result.status == RESUME_REFUSED:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_OWNER_REFUSED,
            message=f"owner refused delivery: {owner_result.detail}",
            line=line,
            decision=decision,
            generation=generation,
            question_note_id=question_note_id,
            card_entity_id=card_entity_id,
        )

    return DeliveryResult(
        status=OUTCOME_DELIVERED,
        line=line,
        decision=decision,
        generation=generation,
        question_note_id=question_note_id,
        card_entity_id=card_entity_id,
        action_key=action_key,
        message=(
            "delivered and consumed: line woken through its registered control "
            f"entry ({owner_result.status})"
        ),
        target={
            "kind": target.kind,
            "id": target.id,
            "generation": target.generation,
            "question_note_id": target.question_note_id,
            "card_entity_id": target.card_entity_id,
            "resume_status": owner_result.status,
        },
    )


def deliver_decision_dd(
    *,
    target_id: str,
    decision: str,
    reason: str,
    dd_source: DdOwnerSource,
) -> DeliveryResult:
    """Deliver one decision to a dd development gate, synchronously.

    The caller names the dd development by id; the server resolves the pending
    question/card from the dd control plane's ``awaiting_gate`` record and
    delivers the verdict through ``DdControlPlane.gate(development_id,
    resume=True)``. An unknown dd, a dd that is not awaiting the gate, or an
    owner-side refusal each map to a distinct, explicit refusal -- never a
    silent HTTP-200 swallow.
    """
    if not isinstance(target_id, str) or not target_id.strip():
        raise DecisionPayloadError("target_id is required for a dd target")
    target_id = target_id.strip()
    decision, reason = _validate_verdict(decision, reason)

    try:
        target, refusal = _resolve_dd_target(dd_source, target_id)
    except Exception as exc:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_OWNER_REFUSED,
            message=f"dd control plane unavailable: {type(exc).__name__}: {exc}",
            line=target_id,
            decision=decision,
        )
    if refusal is not None:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=refusal,
            message=_dd_refusal_message(refusal, target_id),
            line=target_id,
            decision=decision,
        )

    action_key = f"mcp:dd:{target.id}:g{target.generation}:{decision}"
    owner_result = dd_source.resume(target, action_key)
    if owner_result.status == RESUME_REFUSED:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_OWNER_REFUSED,
            message=f"owner refused delivery: {owner_result.detail}",
            line=target_id,
            decision=decision,
            generation=target.generation,
            question_note_id=target.question_note_id,
            card_entity_id=target.card_entity_id,
        )

    return DeliveryResult(
        status=OUTCOME_DELIVERED,
        line=target_id,
        decision=decision,
        generation=target.generation,
        question_note_id=target.question_note_id,
        card_entity_id=target.card_entity_id,
        action_key=action_key,
        message=(
            f"delivered and consumed: dd gate {target.id} resumed through the "
            f"control plane ({owner_result.status})"
        ),
        target={
            "kind": target.kind,
            "id": target.id,
            "generation": target.generation,
            "question_note_id": target.question_note_id,
            "card_entity_id": target.card_entity_id,
            "resume_status": owner_result.status,
        },
    )


def _resolve_dd_target(
    dd_source: DdOwnerSource, target_id: str
) -> tuple[OwnerTarget | None, str | None]:
    """(awaiting dd target, None) or (None, closed refusal code).

    The awaiting owner is found by development id among the control plane's
    ``awaiting_gate`` rows; when the id is absent from those rows, the control
    plane is asked whether the development exists at all, so an unknown dd
    (``DD_NOT_FOUND``) is distinguished from a dd that is admitted but not
    awaiting the gate (``DD_NOT_AWAITING_GATE``).
    """
    from fleet_graph.dd.control_plane import ControlPlaneError

    for target in dd_source.discover_all():
        if target.id == target_id:
            return target, None
    try:
        dd_source._control_plane().get(target_id)
    except ControlPlaneError as exc:
        if exc.code == "DEVELOPMENT_NOT_FOUND":
            return None, CODE_DD_NOT_FOUND
        raise
    return None, CODE_DD_NOT_AWAITING_GATE


def _dd_refusal_message(code: str, target_id: str) -> str:
    if code == CODE_DD_NOT_FOUND:
        return f"no such dd development: {target_id!r} is not admitted"
    return (
        f"dd development {target_id!r} is not awaiting the gate; "
        "deliver only to a development in awaiting_gate state"
    )


class _NullLedger:
    """A no-op ledger for servers built without an explicit ledger.

    Deliberately the *default* for :func:`build_decision_mcp_server`: a test
    that builds the surface without a ledger must never silently write to
    ``DEFAULT_STATE_DIR`` (the production ledger/metrics files). Production
    serving always passes a real :class:`DeliveryLedger` via ``serve()``.
    """

    def record(self, result: DeliveryResult) -> dict[str, Any]:
        return {}


@dataclass
class DeliveryLedger:
    """Durable, queryable record of every delivery call (spec item 5).

    Append-only JSONL under the state dir (``deliveries.jsonl``) plus a
    Prometheus textfile (``decision-delivery.prom``) whose counters make the
    delivered/refused split a scrapeable metric. Writing is atomic (temp +
    rename) so a concurrent scrape never reads a half-written file.
    """

    state_dir: Path = DEFAULT_STATE_DIR
    ledger_name: str = "deliveries.jsonl"
    metrics_name: str = "decision-delivery.prom"
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / self.ledger_name

    @property
    def metrics_path(self) -> Path:
        return self.state_dir / self.metrics_name

    def record(self, result: DeliveryResult) -> dict[str, Any]:
        entry = {
            "at": _iso(self.clock()),
            "line": result.line,
            "decision": result.decision,
            "status": result.status,
            "code": result.code if result.status == OUTCOME_REFUSED else None,
            "retryable": result.retryable or None,
            "action_key": result.action_key or None,
            "generation": result.generation,
            "question_note_id": result.question_note_id,
            "card_entity_id": result.card_entity_id,
        }
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass
        with contextlib.suppress(OSError):
            self._write_metrics()
        return entry

    def _write_metrics(self) -> None:
        delivered = 0
        refused: dict[str, int] = {}
        if self.ledger_path.exists():
            for raw in self.ledger_path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get("status") == OUTCOME_DELIVERED:
                    delivered += 1
                else:
                    code = str(entry.get("code") or "REFUSED")
                    refused[code] = refused.get(code, 0) + 1
        samples = [Sample(METRIC_DELIVERED, value=float(delivered))]
        samples.extend(
            Sample(METRIC_REFUSED, labels=(("code", code),), value=float(count))
            for code, count in sorted(refused.items())
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.metrics_path, render(samples))

    def entries(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for raw in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
        return out


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load_line_roster(lines_config: str | None) -> tuple[list[object], Path]:
    """Read the goal-line roster, fail-soft on an unreadable/malformed file.

    Mirrors ``cli._load_line_roster`` so the surface never imports the CLI
    entrypoint: a missing roster degrades to "no registered lines" (every
    delivery then answers ``NO_WAITING_PARTY``) rather than crashing at start.
    """
    run_root = Path("/data/fleet-graph/runs")
    if not lines_config:
        return [], run_root
    try:
        with open(lines_config, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return [], run_root
    if not isinstance(raw, dict):
        return [], run_root
    entries = raw.get("lines")
    line_owners: list[object] = []
    if isinstance(entries, list):
        line_owners = [
            {k: v for k, v in entry.items() if not str(k).startswith("_")}
            if isinstance(entry, dict)
            else entry
            for entry in entries
        ]
    if raw.get("run_root"):
        run_root = Path(str(raw["run_root"]))
    return line_owners, run_root


def build_decision_mcp_server(
    run_root: Path,
    lines: list[Any],
    *,
    ledger: DeliveryLedger | None = None,
    deliver: Callable[..., DeliveryResult] | None = None,
    dd_source: DdOwnerSource | None = None,
) -> Any:
    """Build the standalone decision MCP surface.

    ``run_root`` + ``lines`` bind the server-side parked-state resolution;
    ``ledger`` / ``deliver`` / ``dd_source`` are seams so tests can drive the
    surface against a scratch state dir, an injectable deliverer, and an
    isolated dd owner. The one tool, ``decision_deliver``, is the synchronous
    delivery contract described in the module docstring: ``target_kind``
    explicitly separates the ``line`` path (the historic ``line`` + ``decision``
    + ``reason`` three-arg delivery, still accepted) from the ``dd`` path
    (``decision`` + ``reason`` + ``target_id``).

    Health-isolation rule (2026-09-02): the ledger is **never** silently
    defaulted to ``DEFAULT_STATE_DIR``. A server built without an explicit
    ledger uses a no-op ledger, so no test/acceptance run can append to the
    production ledger; only ``serve()`` (which always passes a real
    :class:`DeliveryLedger`) writes production files.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    ledger = ledger if ledger is not None else _NullLedger()
    deliverer = deliver or (
        lambda line, decision, reason, target_kind=TARGET_KIND_LINE, target_id="": deliver_decision(
            line=line,
            decision=decision,
            reason=reason,
            run_root=run_root,
            lines=lines,
            target_kind=target_kind,
            target_id=target_id,
            dd_source=dd_source,
        )
    )

    mcp = FastMCP(MCP_SERVER_NAME)

    def refuse(message: str) -> None:
        raise ToolError(
            json.dumps(
                {"code": "DECISION_DELIVER_REFUSED", "message": message},
                sort_keys=True,
            )
        )

    @mcp.tool()
    def decision_deliver(
        decision: str,
        reason: str,
        line: str = "",
        target_kind: str = TARGET_KIND_LINE,
        target_id: str = "",
    ) -> dict[str, Any]:
        """Deliver one decision to a parked goal line or a dd gate, synchronously.

        ``target_kind`` is explicit: ``line`` (default) delivers to the parked
        line named by ``line``; ``dd`` delivers to the dd development named by
        ``target_id``. The question/card correspondence is resolved server-side
        (the line's parked state, or the dd control plane's ``awaiting_gate``
        record). Returns either ``delivered``/``consumed`` or an explicit
        refusal with a stable code -- never a silent success. An invalid payload
        is refused at the call point.
        """
        try:
            result = deliverer(
                line=line,
                decision=decision,
                reason=reason,
                target_kind=target_kind,
                target_id=target_id,
            )
        except DecisionPayloadError as exc:
            refuse(f"invalid payload: {exc}")
        ledger.record(result)
        return result.as_dict()

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    run_root: str | None = None,
    lines_config: str | None = None,
    state_dir: str | None = None,
    dd_root: str | None = None,
) -> None:
    """Run the standalone decision MCP surface on loopback.

    The R2 port discipline is a CI/acceptance-time assertion (the red-able
    ``tests/test_decision_mcp.py`` check that ``DEFAULT_PORT`` never sits in
    ``config/decision-mcp-reserved-ports.json``), deliberately not a runtime
    "probe the port at startup" behavior (spec item 0 R2): FastMCP itself
    surfaces a bind failure visibly. An unreadable state dir is reported
    loudly. A missing roster degrades to "no registered lines" (deliveries
    answer ``NO_WAITING_PARTY``) rather than crashing at start -- the same
    fail-soft posture the decision bridge takes for its roster.
    """
    lines, resolved_run_root = _load_line_roster(lines_config)
    effective_run_root = Path(run_root) if run_root else resolved_run_root
    if not state_dir:
        state_dir = os.environ.get("FLEET_GRAPH_DECISION_MCP_STATE_DIR")
    state = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    ledger = DeliveryLedger(state_dir=state)
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"decision MCP state dir unusable at {state}: {exc}") from exc
    dd_source = DdOwnerSource(Path(dd_root) if dd_root else DEFAULT_DD_ROOT)
    build_decision_mcp_server(effective_run_root, lines, ledger=ledger, dd_source=dd_source).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "ALLOWED_DECISIONS",
    "ALLOWED_TARGET_KINDS",
    "CODE_DD_NOT_AWAITING_GATE",
    "CODE_DD_NOT_FOUND",
    "CODE_LINE_NOT_PARKED",
    "CODE_NO_WAITING_PARTY",
    "CODE_OWNER_REFUSED",
    "CODE_QUESTION_CARD_UNRESOLVED",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "DEFAULT_DD_ROOT",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STATE_DIR",
    "MCP_SERVER_NAME",
    "METRIC_DELIVERED",
    "METRIC_REFUSED",
    "OUTCOME_DELIVERED",
    "OUTCOME_REFUSED",
    "RESERVED_PORTS_FILE",
    "TARGET_KIND_DD",
    "TARGET_KIND_LINE",
    "DecisionPayloadError",
    "DeliveryLedger",
    "DeliveryResult",
    "build_decision_mcp_server",
    "deliver_decision",
    "deliver_decision_dd",
    "load_reserved_ports",
    "serve",
]

"""The durable, serial request-to-Goal-call kernel (DD01, wf-2bf703).

This module is the "request kernel" that replaces the coordinator/worker
round-prompt progression for the goal-facing responsibility: routing the
goal-driven family of requests (enroll / message / steer / DD result / DD
review) through a single durable, serial journal into at most one Goal call per
goal, validating the Goal's Stop List before executing its effects, and
persisting every action intent and independent result so a replay can never
duplicate an already-confirmed external effect.

It is deliberately *not* a LangGraph graph and *not* a second agent: there is
no model invocation, no prompt that a "coordinator" turns into prose, and no
guest interpretation of the Goal's answer. The only thing a Goal call produces
that this kernel consumes is a structurally validated Stop List -- and the only
thing the kernel does with it is route each typed effect through an injected
port and receipt the result. Judgement stays with the Goal; this module is
judgement-free plumbing.

Design anchors (spec inputs/design.md v2.1):

- **One durable ordered journal per goal, one fence per goal.** Accepting a
  request persists it before the caller is acknowledged. Requests arriving
  while a Goal call is in flight stay queued in submission order and are
  delivered one at a time, oldest first, after the in-flight call finishes.
  The fence is per-goal: a call on goal A never blocks a call on goal B.
- **One-current-request prompt.** A Goal call's prompt carries exactly the
  current request's input. Historical requests are addressable *by pointer*
  (their ``request_id``), never appended as text -- the multi-request prompt
  is the exact thing this kernel was built to remove.
- **Typed Stop List.** The Goal's answer is ``actions[]`` of
  ``{kind, payload, idempotency_key}`` with kinds dispatch / approve / reject /
  add_repo / reply, plus an opaque terminal intent (waiting / blocked / done).
  Every action is validated structurally before any effect runs; a malformed
  entry is a fail-closed receipt, never a silent swallow.
- **Effect ports are injected.** dispatch / approve / reject / add_repo / reply
  and the runtime stop/resume control surface are all ports. Every business
  guard (one repo per dispatch, version-bound review) fails closed with an
  explicit receipt; unsupported downstream capability returns not-ready /
  failure, never simulated success.
- **Intent and result are both persisted, linked.** Each action intent is a
  journal record carrying request_id / run_id / list_index; each independent
  delivery result is a second record keyed by the same action identity. A
  replay re-observes the external-effect port for uncertain outcomes before
  retrying and never re-confirms an already-confirmed dispatch or reply.
- **Raw events are queryable.** request / call / action-intent / action-result /
  control records are all first-class, losslessly pageable journal lines -- not
  summaries and not tails.

This kernel is wired into the product composition at ``build_line`` (see
``fleet_graph.graphs.kernel_coordinator.KernelCoordinator``) so it *replaces*
the coordinator/worker round-prompt progression for the goal-facing
responsibility, rather than sitting beside it: one accepted request produces
one Goal ReAct call and one validated Stop action List, and the round-prompt
line no longer carries that responsibility. The kernel itself owns the
request-to-call seam, Stop List validation and effect routing.

Follow-up slices are the *live binding* only: the real runtime ReAct call, real
runtime-process stop/resume, full Session query and per-repo merge
serialization. Until those land, this kernel reports its unsupported portions
precisely instead of pretending (the Goal ReAct port reports
``goal_call_unwired``; unwired effect ports fail closed with ``not_ready``).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The one queue the kernel accepts. Enroll, message, steer and DD
#: result/review all enter here; nothing else does.
KIND_ENROLL = "enroll"
KIND_MESSAGE = "message"
KIND_STEER = "steer"
KIND_DD_RESULT = "dd_result"
KIND_DD_REVIEW = "dd_review"
REQUEST_KINDS = (KIND_ENROLL, KIND_MESSAGE, KIND_STEER, KIND_DD_RESULT, KIND_DD_REVIEW)

#: The Stop List action kinds the kernel routes. Anything else fails closed.
ACTION_DISPATCH = "dispatch"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_ADD_REPO = "add_repo"
ACTION_REPLY = "reply"
ACTION_KINDS = (ACTION_DISPATCH, ACTION_APPROVE, ACTION_REJECT, ACTION_ADD_REPO, ACTION_REPLY)

#: The Stop List terminal intents (opaque routing, never a merge-of-work).
INTENT_WAITING = "waiting"
INTENT_BLOCKED = "blocked"
INTENT_DONE = "done"
INTENT_KINDS = (INTENT_WAITING, INTENT_BLOCKED, INTENT_DONE)

#: Kernel lifecycle modes (the per-goal state machine).
MODE_RUNNING = "running"
MODE_WAITING = "waiting"
MODE_BLOCKED = "blocked"
MODE_STOPPED = "stopped"
MODE_STOPPING = "stopping"
MODES = (MODE_RUNNING, MODE_WAITING, MODE_BLOCKED, MODE_STOPPED, MODE_STOPPING)

#: Stop modes for the runtime control port.
STOP_GRACEFUL = "graceful"
STOP_IMMEDIATE = "immediate"
STOP_MODES = (STOP_GRACEFUL, STOP_IMMEDIATE)

#: Journal record markers.
RECORD_REQUEST = "request"
RECORD_CALL = "call"
RECORD_CALL_RESULT = "call_result"
RECORD_ACTION = "action"
RECORD_ACTION_RESULT = "action_result"
RECORD_CONTROL = "control"
RECORD_VERSION = "version"
RECORD_STOP_LIST = "stop_list"
RECORDS = (
    RECORD_REQUEST,
    RECORD_CALL,
    RECORD_CALL_RESULT,
    RECORD_ACTION,
    RECORD_ACTION_RESULT,
    RECORD_CONTROL,
    RECORD_VERSION,
    RECORD_STOP_LIST,
)

#: Action delivery statuses.
DELIVERED = "delivered"
FAILED = "failed"
NOT_READY = "not_ready"
UNKNOWN = "unknown"

#: External-effect observation outcomes (for crash reconciliation).
OBSERVED_CONFIRMED = "confirmed"
OBSERVED_ABSENT = "absent"
OBSERVED_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Errors (typed, so callers never have to string-match)
# ---------------------------------------------------------------------------


class RequestKernelError(RuntimeError):
    """A request the kernel must refuse, with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class StaleVersionError(RequestKernelError):
    """A version-bound action named a goal version that is not active."""


class DuplicateActionError(RequestKernelError):
    """An action was already confirmed in a prior run and must not re-run."""


# ---------------------------------------------------------------------------
# Small immutable records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One durable, per-goal request (spec behavior 1)."""

    request_id: str
    goal: str
    caller: str
    kind: str
    input: dict[str, Any]
    goal_version: str
    reply_to: str | None = None

    def as_record(self, *, accepted_at: str, goal_version: str = "") -> dict[str, Any]:
        return {
            "record": RECORD_REQUEST,
            "goal": self.goal,
            "request_id": self.request_id,
            "caller": self.caller,
            "kind": self.kind,
            "input": self.input,
            "goal_version": goal_version or self.goal_version,
            "reply_to": self.reply_to,
            "accepted_at": accepted_at,
        }


@dataclass(frozen=True)
class StopList:
    """The Goal's validated answer: actions plus an optional terminal intent."""

    actions: tuple[dict[str, Any], ...] = ()
    intent: str | None = None

    def as_call_result(self) -> dict[str, Any]:
        return {
            "actions": [dict(a) for a in self.actions],
            "intent": self.intent,
        }


# ---------------------------------------------------------------------------
# Effect ports
# ---------------------------------------------------------------------------


class EffectPorts(Protocol):
    """The external-effect seam. Every method is injectable; every method
    returns an independent delivery result (spec behavior 3/4), never a bare
    string that a printer owns."""

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]: ...

    def approve(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]: ...

    def reject(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]: ...

    def add_repo(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]: ...

    def reply(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]: ...

    def observe(self, effect: str, key: str) -> str:
        """Reconcile a possibly-crashed effect (spec behavior 4).

        Returns ``confirmed`` (the effect is present downstream and must not
        re-run), ``absent`` (not present; safe to run), or ``unknown`` (this
        service cannot tell -- stays recoverable and requires Goal judgement).
        """


class RuntimePort(Protocol):
    """The runtime control surface. The kernel never pretends that an absent
    cancellation capability terminated a process (spec behavior 5)."""

    def stop(self, goal: str, *, mode: str) -> dict[str, Any]: ...

    def resume(self, goal: str) -> dict[str, Any]: ...

    def observe(self, goal: str, action_id: str) -> str: ...


class _NullEffects:
    """A default effect port that fails closed with ``not_ready``."""

    def _nope(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "status": NOT_READY, "detail": "no effect port bound"}

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._nope()

    def approve(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._nope()

    def reject(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._nope()

    def add_repo(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._nope()

    def reply(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._nope()

    def observe(self, effect: str, key: str) -> str:
        return OBSERVED_UNKNOWN


# ---------------------------------------------------------------------------
# The durable journal
# ---------------------------------------------------------------------------


def _iso(clock: Callable[[], float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


@dataclass
class Journal:
    """One append-only JSONL journal plus a per-goal ownership fence.

    ``path`` may be a real file (durable) or a string key for an in-memory
    journal (tests). Every line carries a monotonic ``seq`` so pagination can
    traverse every recorded payload losslessly and in order.
    """

    home: Path | None = None
    scope: str = "goal"
    #: The clock is injected so "accepted before acked" ordering is testable.
    clock: Callable[[], float] = time.time
    _lines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _seq: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: The ownership fence: goal -> call_id of the in-flight call, or None.
    _inflight: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A journal pointed at a real directory reconstructs its lines on
        # construction, so a rebuilt product sees the same durable state.
        if self.home is not None:
            self.load()

    # -- persistence ------------------------------------------------------

    def _path(self, goal: str) -> Path:
        assert self.home is not None
        return self.home / f"{self.scope}-{goal}.jsonl"

    def load(self) -> Journal:
        """Load every existing journal line for every goal from disk.

        Idempotent: re-running replaces the in-memory lines with what is on
        disk, so a rebuilt product reconstructs the same durable state. A
        trailing partially-written line (a crash mid-``append``) is isolated
        into a ``*.jsonl.corrupt`` sidecar and removed from the live journal so
        the next ``append`` writes a clean, standalone line instead of
        concatenating onto the fragment (P6).
        """
        if self.home is None:
            return self
        with self._lock:
            loaded: dict[str, list[dict[str, Any]]] = {}
            for path in sorted(self.home.glob(f"{self.scope}-*.jsonl")):
                goal = path.name[len(self.scope) + 1 : -len(".jsonl")]
                raw_text = path.read_text(encoding="utf-8")
                lines: list[dict[str, Any]] = []
                tail_fragment: str | None = None
                raw_lines = raw_text.split("\n")
                for i, raw in enumerate(raw_lines):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        # Only the *trailing* fragment is tolerated. If any
                        # non-empty line follows this one, it is out-of-band
                        # mid-file corruption: skip just this line (best
                        # effort) rather than silently deleting later records.
                        if any(ln.strip() for ln in raw_lines[i + 1 :]):
                            continue
                        tail_fragment = raw
                        break
                    lines.append(record)
                if tail_fragment is not None:
                    self._isolate_incomplete_tail(path, lines, tail_fragment)
                loaded[goal] = lines
            self._lines = loaded
            self._seq = {}
            for goal, lines in loaded.items():
                self._seq[goal] = max(
                    (int(r.get("seq", 0)) for r in lines if "seq" in r), default=0
                )
        return self

    def _isolate_incomplete_tail(
        self, path: Path, lines: list[dict[str, Any]], fragment: str
    ) -> None:
        """Rewrite ``path`` as only its complete, newline-terminated records and
        archive the trailing fragment so a later ``append`` no longer writes
        after a partial line."""
        clean = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in lines
        )
        # Crash-safe repair (finding 3 / behavior 7 / P6): never truncate the
        # acknowledged prefix in place. Write the retained records to a scratch
        # file in the same directory, flush it, then atomically rename over the
        # original. A crash at any point before the rename leaves the original
        # journal intact (the valid prefix plus its trailing fragment), so the
        # next load re-runs the same repair instead of reading a torn file.
        tmp = path.with_suffix(path.suffix + ".repair")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(clean)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        corrupt_path = path.with_suffix(path.suffix + ".corrupt")
        with corrupt_path.open("a", encoding="utf-8") as fh:
            fh.write(fragment + "\n")

    def _next_seq(self, goal: str) -> int:
        seq = self._seq.get(goal, 0) + 1
        self._seq[goal] = seq
        return seq

    def append(self, goal: str, record: dict[str, Any]) -> dict[str, Any]:
        """Append one line; return it with its assigned ``seq`` (if absent)."""
        with self._lock:
            if "seq" not in record:
                record = {**record, "seq": self._next_seq(goal)}
            else:
                self._seq[goal] = max(self._seq.get(goal, 0), int(record["seq"]))
            if self.home is not None:
                self._path(goal).parent.mkdir(parents=True, exist_ok=True)
                with self._path(goal).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    fh.flush()
            self._lines.setdefault(goal, []).append(record)
            return record

    def scan(self, goal: str | None = None) -> list[dict[str, Any]]:
        """All lines for one goal (or every goal, in append order), in order."""
        with self._lock:
            if goal is not None:
                return list(self._lines.get(goal, []))
            flat: list[dict[str, Any]] = []
            for key in sorted(self._lines):
                flat.extend(self._lines[key])
            flat.sort(key=lambda r: (int(r.get("seq", 0)), str(r.get("goal", ""))))
            return flat

    # -- fence ------------------------------------------------------------

    def inflight(self, goal: str) -> str | None:
        return self._inflight.get(goal)

    def claim_call(self, goal: str, call_id: str) -> None:
        """Acquire the fence: exactly one call per goal may be in flight."""
        with self._lock:
            if self._inflight.get(goal):
                raise AssertionError(f"goal {goal} already has an in-flight call")
            self._inflight[goal] = call_id

    def release_call(self, goal: str, call_id: str) -> None:
        with self._lock:
            if self._inflight.get(goal) != call_id:
                raise AssertionError(f"call {call_id} does not hold goal {goal}'s fence")
            self._inflight[goal] = None


# ---------------------------------------------------------------------------
# Stop List validation
# ---------------------------------------------------------------------------

_DISPATCH_FIELDS = ("repo_path", "target_base", "spec_text", "spec_path", "dispatched_by")
_REVIEW_FIELDS = ("development_id", "verdict", "goal_version")


def validate_stop_list(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Structurally validate a Stop List -> (routable actions, receipts, intent).

    Never raises: every malformed entry becomes a fail-closed receipt naming its
    reason; well-formed siblings still route. The intent is normalized to one of
    the closed intents or ``None`` when absent/unrecognized (an unknown intent
    is therefore observable, never fabricated into a terminal)."""
    raw = result.get("actions")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return [], [_receipt(None, reason="actions must be a list")], _norm_intent(result)

    consumable: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            receipts.append(_receipt(None, reason="action must be an object"))
            continue
        kind = entry.get("kind")
        payload = entry.get("payload")
        key = entry.get("idempotency_key")
        if kind not in ACTION_KINDS:
            receipts.append(_receipt(entry, reason=f"unknown action kind {kind!r}"))
            continue
        if not isinstance(payload, dict):
            receipts.append(_receipt(entry, reason="action payload must be an object"))
            continue
        if not isinstance(key, str) or not key.strip():
            receipts.append(_receipt(entry, reason="idempotency_key is required"))
            continue
        if key in seen:
            receipts.append(_receipt(entry, reason=f"duplicate idempotency_key {key!r}"))
            continue
        seen.add(key)
        consumable.append({"kind": kind, "payload": payload, "idempotency_key": key})
    return consumable, receipts, _norm_intent(result)


def _norm_intent(result: dict[str, Any]) -> str | None:
    intent = result.get("intent")
    return intent if intent in INTENT_KINDS else None


def _receipt(entry: dict[str, Any] | None, *, reason: str) -> dict[str, Any]:
    identified = entry if isinstance(entry, dict) else {}
    return {
        "kind": str(identified.get("kind") or ""),
        "idempotency_key": str(identified.get("idempotency_key") or ""),
        "status": FAILED,
        "reason": reason,
    }


def business_guards(action: dict[str, Any], *, active_version: str) -> str | None:
    """The per-action business guards (spec behavior 3). Returns a refusal
    reason string, or ``None`` when the action passes."""
    kind = action["kind"]
    payload = action["payload"]
    if kind == ACTION_DISPATCH:
        repo_path = str(payload.get("repo_path") or "").strip()
        if not repo_path:
            return "dispatch requires exactly one repo_path"
        if "repo_paths" in payload:
            return "dispatch supports one repo per action; repo_paths is unsupported"
        if not str(payload.get("dispatched_by") or "").strip():
            return "dispatch payload requires dispatched_by"
    if kind in (ACTION_APPROVE, ACTION_REJECT):
        for field in _REVIEW_FIELDS:
            if not str(payload.get(field) or "").strip():
                return f"{kind} requires {field}"
        version = str(payload.get("goal_version") or "")
        if version and version != active_version:
            return f"stale_version: action names {version!r}, active is {active_version!r}"
    return None


# ---------------------------------------------------------------------------
# The prompt builder
# ---------------------------------------------------------------------------


def build_goal_prompt(current: Request, *, history: list[Request] | None = None) -> dict[str, Any]:
    """One-current-request prompt (spec behavior 1).

    The prompt carries exactly the current request's input. Historical
    requests are referenced only by ``request_id`` pointer, never by their
    text -- so a long conversation can never be re-appended as current input.
    """
    return {
        "request_id": current.request_id,
        "kind": current.kind,
        "caller": current.caller,
        "goal_version": current.goal_version,
        "input": current.input,
        "history_refs": [r.request_id for r in (history or [])],
    }


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------


@dataclass
class GoalRequestKernel:
    """The serial request-to-Goal-call kernel for one or many goals.

    Wires a durable :class:`Journal`, injected :class:`EffectPorts`, and
    injected :class:`RuntimePort` into the six spec behaviors. Pure state
    transitions; no model, no prompt-prose interpretation.
    """

    journal: Journal
    effects: EffectPorts | None = None
    runtime: RuntimePort | None = None
    clock: Callable[[], float] = time.time
    _versions: dict[str, str] = field(default_factory=dict)
    _mode: dict[str, str] = field(default_factory=dict)
    _state: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.effects is None:
            self.effects = _NullEffects()
        self.restore()

    def restore(self) -> None:
        """Reconstruct per-goal derived state from the durable journal.

        Rebuilds the version map, the lifecycle mode, the unserved request
        queue and the ownership fence from the journal lines alone, so a rebuilt
        product does not lose accepted requests, forget a stop, or re-run an
        already-confirmed effect (behaviors 1/4/7, P6). A call whose result was
        never written is an interruption (behavior 7). How it is recovered
        depends on what was already durable:

        - A call with a persisted Stop List (validated *before* effects, per
          finding 1) is *resumed*, not re-answered: the fence is re-acquired
          with the original call id and the outstanding items are reconciled
          against their original action identities -- never re-invoking Goal,
          which could answer with a different list and duplicate a confirmed
          effect or drop a not-yet-run one.
        - A call with no persisted Stop List ran no effect yet, so its request
          is re-queued and the fence dropped, and a fresh Goal call is safe.

        Called automatically at construction; safe to re-run.
        """
        self.journal.load()
        self._versions = {}
        self._mode = {}
        inflight: dict[str, str | None] = {}
        for goal in sorted(self.journal._lines):
            lines = self.journal._lines[goal]
            version = ""
            mode = MODE_RUNNING
            called: set[str] = set()
            current_call: str | None = None
            current_call_request: str | None = None
            for line in lines:
                record = line.get("record")
                if record == RECORD_VERSION:
                    version = str(line.get("version") or "")
                elif record == RECORD_CONTROL and line.get("control") == "mode":
                    mode = str(line.get("value") or MODE_RUNNING)
                elif record == RECORD_CALL:
                    called.add(str(line.get("request_id") or ""))
                    current_call = str(line.get("call_id") or "") or None
                    current_call_request = str(line.get("request_id") or "") or None
                elif (
                    record == RECORD_CALL_RESULT
                    and current_call is not None
                    and line.get("call_id") == current_call
                ):
                    current_call = None
                    current_call_request = None
            resume_call: str | None = None
            if current_call is not None and current_call_request:
                if self._stop_list_line(goal, current_call) is not None:
                    # Interrupted *after* the validated list was durable: resume
                    # its original identities/outstanding items, keep the fence.
                    resume_call = current_call
                else:
                    # Interrupted before any effect: re-queue and re-answer.
                    called.discard(current_call_request)
                    current_call = None
            queue = [
                line
                for line in lines
                if line.get("record") == RECORD_REQUEST
                and str(line.get("request_id") or "") not in called
            ]
            self._versions[goal] = version
            self._mode[goal] = mode
            self._state[goal] = {
                "queue": queue,
                "delivered": [],
                "mode": mode,
                "stopping_drain": None,
                "resume": resume_call,
            }
            inflight[goal] = current_call
        self.journal._inflight = inflight

    # -- goal versioning ---------------------------------------------------

    def activate_version(self, goal: str, version: str, *, source: str = "event") -> dict[str, Any]:
        """Explicit, immutable version event (spec behavior 5).

        Only this call moves the active version. Anything else that touches the
        goal (a message, a DD result, an edit elsewhere) leaves it unmoved, so
        editing the work folder alone never changes what the kernel answers for
        ``active_version``.
        """
        if not version or not version.strip():
            raise RequestKernelError("version_required", "a goal version is required")
        record = self.journal.append(
            goal,
            {
                "record": RECORD_VERSION,
                "goal": goal,
                "version": version,
                "source": source,
                "at": _iso(self.clock),
            },
        )
        self._versions[goal] = version
        return record

    def active_version(self, goal: str) -> str:
        return self._versions.get(goal, "")

    # -- requests ----------------------------------------------------------

    def submit(self, goal: str, request: Request) -> dict[str, Any]:
        """Accept one request: persist it, then acknowledge (spec behavior 1).

        The persistence happens strictly before the returned acknowledgement, so
        a caller that observes the ack can rely on the request already being
        durable. Duplicate request identity reuses the persisted record (P5);
        it never re-appends and never re-runs.
        """
        existing = self._find_request(request.request_id)
        if existing is not None:
            return {"request_id": request.request_id, "duplicate": True, "record": existing}
        record = self.journal.append(
            goal,
            request.as_record(
                accepted_at=_iso(self.clock),
                goal_version=request.goal_version or self.active_version(goal),
            ),
        )
        self._queue(goal, record)
        if self._mode_of(goal) in (MODE_BLOCKED, MODE_WAITING):
            # A new request is new input: a blocked goal is woken by it
            # (behavior 5). ``stopped`` is not -- only resume lifts a stop.
            self._set_mode(goal, MODE_RUNNING)
        return {"request_id": request.request_id, "duplicate": False, "record": record}

    def _find_request(self, request_id: str) -> dict[str, Any] | None:
        for goal in sorted(self.journal._lines):
            for line in self.journal._lines[goal]:
                if line.get("record") == RECORD_REQUEST and line.get("request_id") == request_id:
                    return line
        return None

    # -- per-goal state ----------------------------------------------------

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "queue": [],
            "delivered": [],
            "mode": MODE_RUNNING,
            "stopping_drain": None,
            "resume": None,
        }

    def _queue(self, goal: str, record: dict[str, Any]) -> None:
        st = self._state.setdefault(goal, self._default_state())
        st["queue"].append(record)

    def _mode_of(self, goal: str) -> str:
        return self._state.get(goal, {}).get("mode", MODE_RUNNING)

    def _set_mode(self, goal: str, mode: str, *, record: bool = True) -> None:
        st = self._state.setdefault(goal, self._default_state())
        st["mode"] = mode
        if record:
            self.journal.append(
                goal,
                {
                    "record": RECORD_CONTROL,
                    "goal": goal,
                    "control": "mode",
                    "value": mode,
                    "at": _iso(self.clock),
                },
            )

    # -- the fence: one Goal call per goal ---------------------------------

    def next_goal_call(self, goal: str) -> dict[str, Any] | None:
        """Open the next Goal call for ``goal``, or ``None`` when none is due.

        Returns ``None`` when a call is already in flight (the fence), when the
        goal is ``stopped`` (a stop must be lifted by resume before any new call
        or effect), or when the queue is empty. Otherwise it pops the oldest
        queued request, claims the fence, and returns a call envelope.

        A call that was interrupted *after* its validated Stop List was durable
        (finding 1) is returned as a ``resume`` envelope: its original action
        identities and outstanding items are resumed through reconciliation and
        the Goal is *not* re-invoked. A stopped goal suspends that resume until
        ``resume()`` lifts the stop (finding 2).
        """
        st = self._state.setdefault(goal, self._default_state())
        resume_id = st.get("resume")
        if resume_id:
            if self._mode_of(goal) == MODE_STOPPED:
                return None  # suspended unstarted effects; wait for resume()
            persisted = self._stop_list_line(goal, resume_id)
            if persisted is None:
                st["resume"] = None
            else:
                call_line = self._call_line(goal, resume_id) or {}
                return {
                    "call_id": resume_id,
                    "goal": goal,
                    "request_id": call_line.get("request_id"),
                    "run_id": call_line.get("run_id"),
                    "request": call_line,
                    "resume": True,
                    "stop_list": persisted,
                }
        if self.journal.inflight(goal):
            return None
        if self._mode_of(goal) in (MODE_STOPPING, MODE_STOPPED, MODE_BLOCKED):
            return None
        if not st["queue"]:
            return None
        request = st["queue"].pop(0)
        call_id = f"call:{goal}:{uuid.uuid4().hex}"
        run_id = str(request.get("run_id") or "") or f"run::{goal}::{request.get('request_id')}"
        self.journal.claim_call(goal, call_id)
        self.journal.append(
            goal,
            {
                "record": RECORD_CALL,
                "goal": goal,
                "call_id": call_id,
                "request_id": request.get("request_id"),
                "caller": request.get("caller"),
                "run_id": run_id,
                "goal_version": request.get("goal_version") or self.active_version(goal),
                "at": _iso(self.clock),
            },
        )
        return {
            "call_id": call_id,
            "goal": goal,
            "request_id": request.get("request_id"),
            "run_id": run_id,
            "request": request,
        }

    def _call_line(self, goal: str, call_id: str) -> dict[str, Any] | None:
        for line in self.journal.scan(goal):
            if line.get("record") == RECORD_CALL and line.get("call_id") == call_id:
                return line
        return None

    def _stop_list_line(self, goal: str, call_id: str) -> dict[str, Any] | None:
        for line in self.journal.scan(goal):
            if line.get("record") == RECORD_STOP_LIST and line.get("call_id") == call_id:
                return line
        return None

    def _persist_stop_list(
        self,
        goal: str,
        call_id: str,
        request_id: str,
        run_id: str,
        consumable: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        intent: str | None,
    ) -> dict[str, Any]:
        """Durably record the *validated* Stop List before any effect runs.

        This is the fork point for resume (finding 1): once this record is on
        disk, an interruption no longer needs a fresh Goal answer -- the
        original action identities (kind/idempotency_key/list_index) are
        recoverable and only outstanding items are executed on resume.
        """
        return self.journal.append(
            goal,
            {
                "record": RECORD_STOP_LIST,
                "goal": goal,
                "call_id": call_id,
                "request_id": request_id,
                "run_id": run_id,
                "intent": intent,
                "actions": [
                    {
                        "kind": a["kind"],
                        "payload": a["payload"],
                        "idempotency_key": a["idempotency_key"],
                        "list_index": i,
                    }
                    for i, a in enumerate(consumable, start=1)
                ],
                "malformed": [dict(r) for r in receipts],
                "at": _iso(self.clock),
            },
        )

    def finish_goal_call(
        self, goal: str, call_id: str, stop_list: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate and execute one Goal call's Stop List, then release the call.

        Execution is the only place effects run. It is ordered, per-item: a
        failed item does not erase earlier successes, and an already-confirmed
        item is observed -- never blindly re-run (spec behavior 4).

        The *validated* Stop List is persisted to the journal before the first
        effect runs (finding 1). A subsequent interruption is then resumed from
        that durable list -- the same ``call_id``, action identities and
        idempotency keys -- rather than by re-invoking Goal, so a differing
        second answer can neither duplicate a confirmed effect nor drop a
        not-yet-run one.

        A goal that is ``stopped`` (an immediate stop the runtime confirmed)
        *suspends* unstarted effects: the in-flight result is preserved, the
        fence stays held, and nothing runs until ``resume()`` (finding 2)."""
        call = next((r for r in self.journal.scan(goal) if r.get("call_id") == call_id), None)
        request_id = str(call.get("request_id") or "") if call else ""
        run_id = str(call.get("run_id") or "") if call else ""
        caller = str(call.get("caller") or "") if call else ""

        persisted = self._stop_list_line(goal, call_id)
        if persisted is not None:
            # Resuming an interrupted call: reconstruct the original validated
            # identities, never revalidate a possibly-different re-answer.
            consumable = [dict(a) for a in persisted.get("actions") or []]
            receipts = [dict(r) for r in (persisted.get("malformed") or [])]
            intent = persisted.get("intent")
        else:
            consumable, receipts, intent = validate_stop_list(stop_list)
            self._persist_stop_list(goal, call_id, request_id, run_id, consumable, receipts, intent)

        active_version = self._versions.get(goal, "")

        results: list[dict[str, Any]] = []
        # P1 same-list dependency: a ``dispatch`` is only admitted for a
        # repository whose same-list ``add_repo`` succeeded first. A refused or
        # lost admission blocks the dependent dispatch without erasing the
        # independent results already delivered in this list.
        repo_admission: dict[str, str] = {}
        for position, action in enumerate(consumable, start=1):
            if self._mode_of(goal) == MODE_STOPPED:
                # Finding 2: a confirmed stop suspends unstarted effects. The
                # already-persisted list (and any earlier results) survive; the
                # fence is held and resume() re-runs these items through
                # reconciliation -- never by re-invoking Goal.
                st = self._state.setdefault(goal, self._default_state())
                st["resume"] = call_id
                return {
                    "call_id": call_id,
                    "goal": goal,
                    "request_id": request_id,
                    "intent": intent,
                    "receipts": [*receipts],
                    "results": results,
                    "suspended": True,
                }
            index = int(action.get("list_index", position))
            refusal = business_guards(action, active_version=active_version)
            if refusal:
                results.append(
                    self._refuse(goal, action, call_id, request_id, run_id, index, refusal)
                )
                continue
            if action["kind"] == ACTION_DISPATCH:
                repo = str((action.get("payload") or {}).get("repo_path") or "")
                admission = repo_admission.get(repo)
                if admission is not None and admission != DELIVERED:
                    results.append(
                        self._refuse(
                            goal,
                            action,
                            call_id,
                            request_id,
                            run_id,
                            index,
                            f"dependent dispatch refused: add_repo for {repo!r} did not succeed",
                        )
                    )
                    continue
            result = self._effect_outcome(goal, action, call_id, request_id, run_id, index, caller)
            if action["kind"] == ACTION_ADD_REPO:
                repo_admission[str((action.get("payload") or {}).get("repo_path") or "")] = str(
                    result["status"]
                )
            results.append(result)

        # Behavior 6: persist the returned Stop List, its terminal intent and
        # every malformed entry (not just the routed results) -- pagination must
        # be able to reconstruct the raw outcome, never a reduced projection.
        for receipt in receipts:
            self.journal.append(
                goal,
                self._result_record(
                    goal,
                    f"{call_id}:malformed:{receipt.get('idempotency_key') or 'unknown'}",
                    request_id,
                    run_id,
                    0,
                    str(receipt.get("kind") or ""),
                    FAILED,
                    str(receipt.get("reason") or "malformed action"),
                    final=True,
                ),
            )
        raw_actions = stop_list.get("actions") if isinstance(stop_list, dict) else None
        self.journal.append(
            goal,
            {
                "record": RECORD_CALL_RESULT,
                "goal": goal,
                "call_id": call_id,
                "request_id": request_id,
                "run_id": run_id,
                "intent": intent,
                "actions": raw_actions if isinstance(raw_actions, list) else [],
                "at": _iso(self.clock),
            },
        )

        self.journal.release_call(goal, call_id)

        # A call was fully drained (fresh or resumed): clear any resume marker.
        st = self._state.get(goal)
        if st and st.get("resume") == call_id:
            st["resume"] = None

        # A stop that landed while this call was in flight is authoritative:
        # graceful stop drains this result then rests ``stopped``; an immediate
        # stop the runtime confirmed holds ``stopped``. Neither is overwritten
        # by the returned intent (behavior 5).
        mode = self._mode_of(goal)
        if mode == MODE_STOPPING:
            self._set_mode(goal, MODE_STOPPED)
        elif mode == MODE_STOPPED:
            pass
        else:
            self._set_mode(goal, _intent_mode(intent) or mode)

        return {
            "call_id": call_id,
            "goal": goal,
            "request_id": request_id,
            "intent": intent,
            "receipts": [*receipts],
            "results": results,
        }

    @staticmethod
    def _action_id(call_id: str, index: int, action: dict[str, Any]) -> str:
        return f"{call_id}:{index}:{action['kind']}:{action['idempotency_key']}"

    @staticmethod
    def _result(
        action: dict[str, Any],
        action_id: str,
        status: str,
        detail: str,
        *,
        reconciled: bool = False,
        downstream: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action_id": action_id,
            "kind": action["kind"],
            "idempotency_key": action["idempotency_key"],
            "status": status,
            "detail": detail,
        }
        if reconciled:
            out["reconciled"] = True
        if isinstance(downstream, dict):
            for key, value in downstream.items():
                if key in ("ok", "status", "detail", "reconciled") or key in out:
                    continue
                out[key] = value
        return out

    @staticmethod
    def _downstream(delivery: dict[str, Any]) -> dict[str, Any]:
        """The non-projection fields of an effect delivery (behavior 6)."""
        return {
            key: value for key, value in delivery.items() if key not in ("ok", "status", "detail")
        }

    def _effect_outcome(
        self,
        goal: str,
        action: dict[str, Any],
        call_id: str,
        request_id: str,
        run_id: str,
        index: int,
        caller: str,
    ) -> dict[str, Any]:
        """Deliver one effect, or reconcile it against a prior attempt.

        The durable prior-attempt memory is the journal: an action whose
        ``(kind, idempotency_key)`` already has an action-intent record is a
        *replay* (of every kind, so a resumed interrupted call reconciles
        approve/reject/add_repo exactly like dispatch/reply), not a fresh send.
        A replay that already has a result is answered from that stored status
        and never re-run; a replay whose result was lost (a crash after the
        effect receipt, before the result write) is arbitrated by the
        external-effect port's ``observe`` -- ``confirmed`` skips, ``absent``
        re-sends, ``unknown`` stays recoverable and requires Goal judgement.
        This is exactly-once-with-truth, never exactly-once-as-theatre (spec
        behavior 4)."""
        kind = action["kind"]
        key = action["idempotency_key"]
        prior = self._prior_intent(goal, kind, key)
        if prior is None:
            return self._deliver(goal, action, call_id, request_id, run_id, index, caller=caller)
        action_id = str(prior.get("action_id") or self._action_id(call_id, index, action))
        existing = self._prior_result(goal, action_id)
        if existing is not None and (existing.get("final") or existing.get("status") == DELIVERED):
            return self._result(
                action, action_id, existing["status"], existing.get("detail", ""), reconciled=True
            )
        # No durable result, or an uncertain / retryable one (an UNKNOWN reply,
        # or a FAILED that was never confirmed downstream): reconcile through
        # the external-effect observe port before deciding, so a later
        # confirmation is not locked out by a stale UNKNOWN/FAILED receipt
        # (behavior 4).
        observed = self.effects.observe(kind, key)  # type: ignore[union-attr]
        if observed == OBSERVED_CONFIRMED:
            self.journal.append(
                goal,
                self._result_record(
                    goal,
                    action_id,
                    request_id,
                    run_id,
                    index,
                    kind,
                    DELIVERED,
                    "already confirmed downstream; not re-run",
                    final=True,
                ),
            )
            return self._result(
                action,
                action_id,
                DELIVERED,
                "already confirmed downstream; not re-run",
                reconciled=True,
            )
        if observed == OBSERVED_ABSENT:
            return self._deliver(
                goal,
                action,
                call_id,
                request_id,
                run_id,
                index,
                caller=caller,
                action_id=action_id,
            )
        return self._result(
            action,
            action_id,
            UNKNOWN,
            "outcome unknown; recoverable and requires Goal judgement",
            reconciled=True,
        )

    def _prior_intent(self, goal: str, kind: str, key: str) -> dict[str, Any] | None:
        for line in self.journal.scan(goal):
            if (
                line.get("record") == RECORD_ACTION
                and line.get("kind") == kind
                and line.get("idempotency_key") == key
            ):
                return line
        return None

    def _prior_result(self, goal: str, action_id: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for line in self.journal.scan(goal):
            if line.get("record") == RECORD_ACTION_RESULT and line.get("action_id") == action_id:
                found = line
        return found

    def _result_record(
        self,
        goal: str,
        action_id: str,
        request_id: str,
        run_id: str,
        list_index: int,
        kind: str,
        status: str,
        detail: str,
        *,
        delivery: dict[str, Any] | None = None,
        final: bool = False,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "record": RECORD_ACTION_RESULT,
            "goal": goal,
            "action_id": action_id,
            "request_id": request_id,
            "run_id": run_id,
            "list_index": list_index,
            "kind": kind,
            "status": status,
            "detail": detail,
            "final": final,
            "at": _iso(self.clock),
        }
        if isinstance(delivery, dict):
            # Behavior 6: downstream facts (development_id, evidence references,
            # launches, ...) are part of the raw result, never collapsed into
            # a reduced status/detail projection.
            for key, value in delivery.items():
                if key in ("ok", "status", "detail") or key in record:
                    continue
                record[key] = value
        return record

    def _deliver(
        self,
        goal: str,
        action: dict[str, Any],
        call_id: str,
        request_id: str,
        run_id: str,
        index: int,
        *,
        caller: str = "",
        action_id: str | None = None,
    ) -> dict[str, Any]:
        is_replay = action_id is not None
        action_id = action_id or self._action_id(call_id, index, action)
        if not is_replay:
            self.journal.append(
                goal,
                {
                    "record": RECORD_ACTION,
                    "goal": goal,
                    "action_id": action_id,
                    "call_id": call_id,
                    "request_id": request_id,
                    "caller": caller,
                    "run_id": run_id,
                    "list_index": index,
                    "kind": action["kind"],
                    "payload": action["payload"],
                    "idempotency_key": action["idempotency_key"],
                    "at": _iso(self.clock),
                },
            )
        ctx = {
            "goal": goal,
            "request_id": request_id,
            "caller": caller,
            "call_id": call_id,
            "run_id": run_id,
            "idempotency_key": action["idempotency_key"],
        }
        try:
            method = getattr(self.effects, action["kind"])
            delivery = method(action["payload"], ctx=ctx)
        except Exception as exc:  # an effect fault is a fact, never a crash
            delivery = {"ok": False, "status": FAILED, "detail": f"{type(exc).__name__}: {exc}"}
            raised = True
        else:
            raised = False
        if not isinstance(delivery, dict):
            delivery = {"ok": False, "status": FAILED, "detail": "effect port returned a non-dict"}
            raised = True
        status = delivery.get("status")
        if delivery.get("ok") is True or status == DELIVERED:
            status = DELIVERED
            final = True
        elif status == NOT_READY:
            # Unsupported downstream capability: an explicit, definitive
            # not-ready receipt, never a simulated success and never retried.
            final = True
        elif status == UNKNOWN or raised:
            # An unknown outcome, or a failure whose true downstream state is
            # uncertain (a port raised mid-effect), is retryable and must be
            # re-observed on replay rather than locked in.
            status = UNKNOWN if status == UNKNOWN else FAILED
            final = False
        elif status == FAILED:
            final = False
        else:
            status = FAILED
            final = False
        downstream = self._downstream(delivery)
        self.journal.append(
            goal,
            self._result_record(
                goal,
                action_id,
                request_id,
                run_id,
                index,
                action["kind"],
                status,
                delivery.get("detail", ""),
                delivery=delivery,
                final=final,
            ),
        )
        return self._result(
            action, action_id, status, delivery.get("detail", ""), downstream=downstream
        )

    def _refuse(
        self,
        goal: str,
        action: dict[str, Any],
        call_id: str,
        request_id: str,
        run_id: str,
        index: int,
        reason: str,
    ) -> dict[str, Any]:
        action_id = self._action_id(call_id, index, action)
        existing = self._prior_result(goal, action_id)
        if existing is not None and existing.get("final"):
            # A resumed call must not re-refuse an already-recorded refusal
            # (at most one terminal intent, P1).
            return self._result(
                action, action_id, existing["status"], existing.get("detail", ""), reconciled=True
            )
        self.journal.append(
            goal,
            {
                "record": RECORD_ACTION,
                "goal": goal,
                "action_id": action_id,
                "call_id": call_id,
                "request_id": request_id,
                "run_id": run_id,
                "list_index": index,
                "kind": action["kind"],
                "payload": action["payload"],
                "idempotency_key": action["idempotency_key"],
                "at": _iso(self.clock),
            },
        )
        self.journal.append(
            goal,
            self._result_record(
                goal,
                action_id,
                request_id,
                run_id,
                index,
                action["kind"],
                FAILED,
                reason,
                final=True,
            ),
        )
        return self._result(action, action_id, FAILED, reason)

    # -- waiting / blocked / stopped / stop-resume -------------------------

    def waiting(self, goal: str, *, waiting_on: str = "") -> None:
        """``waiting`` must not suppress already-queued requests (behavior 5)."""
        self._set_mode(goal, MODE_WAITING)

    def blocked(self, goal: str, *, blocker: str = "") -> None:
        self._set_mode(goal, MODE_BLOCKED)

    def wake(self, goal: str) -> None:
        """A new request (or message) lifts ``blocked``/``waiting`` (behavior 5)."""
        if self._mode_of(goal) in (MODE_BLOCKED, MODE_WAITING):
            self._set_mode(goal, MODE_RUNNING)

    def stop(self, goal: str, *, mode: str = STOP_GRACEFUL) -> dict[str, Any]:
        """Persist message/steer but start no new call/effect until resume.

        Graceful stop drains an in-flight result before stopping; immediate
        stop routes to the runtime control port and records the answer without
        pretending that absent cancellation support terminated a process
        (behavior 5)."""
        if mode not in STOP_MODES:
            raise RequestKernelError("stop_mode", f"{mode!r} not in {list(STOP_MODES)}")

        if mode == STOP_IMMEDIATE:
            cancel: dict[str, Any] = {}
            if self.runtime is not None:
                cancel = self.runtime.stop(goal, mode=mode) or {}
            terminated = bool(cancel.get("terminated", False))
            # Behavior 5/6: the cancellation answer is a recorded control fact --
            # never a printout, never a simulated termination.
            self.journal.append(
                goal,
                {
                    "record": RECORD_CONTROL,
                    "goal": goal,
                    "control": "stop",
                    "mode": mode,
                    "terminated": terminated,
                    "cancel": cancel,
                    "at": _iso(self.clock),
                },
            )
            if terminated:
                self._set_mode(goal, MODE_STOPPED)
            # Not terminated: absent/refused cancellation is recorded honestly
            # and the goal's mode is left as it was -- it is not 'stopped'.
            return {"goal": goal, "mode": mode, "stopped": terminated, "cancel": cancel}

        # graceful: stop admitting new calls/effects, but drain an in-flight
        # result first (behavior 5). Without an in-flight call it stops now.
        if self.journal.inflight(goal):
            self._set_mode(goal, MODE_STOPPING)
            return {
                "goal": goal,
                "mode": mode,
                "stopped": False,
                "draining": True,
                "cancel": None,
            }
        self._set_mode(goal, MODE_STOPPED)
        return {"goal": goal, "mode": mode, "stopped": True, "cancel": None}

    def resume(self, goal: str) -> dict[str, Any]:
        """Lift a ``stopped``/``waiting``/``blocked`` goal back to running."""
        self._set_mode(goal, MODE_RUNNING)
        if self.runtime is not None:
            return self.runtime.resume(goal)
        return {"goal": goal, "resumed": True}

    # -- pagination --------------------------------------------------------

    def list_events(
        self, goal: str | None = None, *, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Losslessly pageable raw events (behavior 6).

        ``cursor`` is an opaque seq offset; ``limit`` bounds one page. The
        returned ``next_cursor`` is ``None`` at the end. Every recorded payload
        is traversable -- summaries and tails are simply not kept."""
        rows = self.journal.scan(goal)
        start = int(cursor) if cursor is not None and cursor.isdigit() else 0
        page = rows[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(rows) else None
        return {
            "events": page,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
            "total": len(rows),
        }


def _intent_mode(intent: str | None) -> str | None:
    # ``done`` is an intent, never a terminal shortcut (P4): until every
    # required delivery receipt exists it stays pending, so it moves no mode.
    if intent == INTENT_WAITING:
        return MODE_WAITING
    if intent == INTENT_BLOCKED:
        return MODE_BLOCKED
    return None


__all__ = [
    "ACTION_ADD_REPO",
    "ACTION_APPROVE",
    "ACTION_DISPATCH",
    "ACTION_KINDS",
    "ACTION_REJECT",
    "ACTION_REPLY",
    "DELIVERED",
    "FAILED",
    "INTENT_BLOCKED",
    "INTENT_DONE",
    "INTENT_KINDS",
    "INTENT_WAITING",
    "KIND_DD_RESULT",
    "KIND_DD_REVIEW",
    "KIND_ENROLL",
    "KIND_MESSAGE",
    "KIND_STEER",
    "MODES",
    "MODE_BLOCKED",
    "MODE_RUNNING",
    "MODE_STOPPED",
    "MODE_STOPPING",
    "MODE_WAITING",
    "NOT_READY",
    "OBSERVED_ABSENT",
    "OBSERVED_CONFIRMED",
    "OBSERVED_UNKNOWN",
    "RECORDS",
    "RECORD_ACTION",
    "RECORD_ACTION_RESULT",
    "RECORD_CALL",
    "RECORD_CALL_RESULT",
    "RECORD_CONTROL",
    "RECORD_REQUEST",
    "RECORD_STOP_LIST",
    "RECORD_VERSION",
    "REQUEST_KINDS",
    "STOP_GRACEFUL",
    "STOP_IMMEDIATE",
    "STOP_MODES",
    "UNKNOWN",
    "DuplicateActionError",
    "EffectPorts",
    "GoalRequestKernel",
    "Journal",
    "Request",
    "RequestKernelError",
    "RuntimePort",
    "StaleVersionError",
    "StopList",
    "build_goal_prompt",
    "business_guards",
    "validate_stop_list",
]

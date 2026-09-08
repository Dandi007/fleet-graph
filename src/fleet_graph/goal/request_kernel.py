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
RECORD_CALL_UNAVAILABLE = "call_unavailable"
RECORD_ACTION = "action"
RECORD_ACTION_RESULT = "action_result"
RECORD_CONTROL = "control"
RECORD_VERSION = "version"
RECORD_STOP_LIST = "stop_list"
RECORDS = (
    RECORD_REQUEST,
    RECORD_CALL,
    RECORD_CALL_RESULT,
    RECORD_CALL_UNAVAILABLE,
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


def _fsync_file(fd: int) -> None:
    """Durability barrier for a freshly appended journal file.

    ``fh.flush()`` alone only moves Python's buffer into the OS page cache; a
    host crash can still lose an acknowledged request or a recorded action
    intent whose external effect already ran. This fsync forces the bytes to
    stable storage *before* the caller issues an acceptance ack or starts an
    effect. A sync failure propagates: no ack, no effect, no in-memory admission.
    """
    os.fsync(fd)


def _fsync_directory(path: Path) -> None:
    """Persist a freshly created journal file's directory entry.

    After the first append creates a new ``*.jsonl`` file, its parent directory
    entry must reach stable storage too, otherwise a crash can lose the file's
    very existence even though its first record was already fsync'd. Unlike a
    file-content fsync, this is *not* best-effort: an ``OSError`` here (open or
    fsync) propagates, so the caller issues no acceptance ack and admits nothing
    in memory while the file's directory entry is not yet durable (behavior 1,
    4, 7 / P6). Swallowing it would let ``submit`` acknowledge a request whose
    journal file a host crash can still erase outright.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    #: File-descriptor fsync, injected so offline tests can fault-inject sync
    #: failures and assert the write/flush/sync/ACK (and sync/effect) ordering.
    file_sync: Callable[[int], None] = field(default=_fsync_file)
    #: Directory-entry fsync for a freshly created journal file. A failure here
    #: must block the caller's acceptance (behavior 1, 4, 7 / P6), so unlike a
    #: best-effort sync it is allowed to raise.
    dir_sync: Callable[[Path], None] = field(default=_fsync_directory)
    #: Parent directories whose journal-file entry has already been durably
    #: synced. A path enters here only after its ``dir_sync`` succeeds (or after
    #: ``load`` observes a file that already survived to disk), so a failed sync
    #: is not skipped on retry merely because the file now exists.
    _dir_synced: set[Path] = field(default_factory=set, init=False)

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
                # A file that survived to be read back already has a durable
                # directory entry; mark its parent as synced so a later append
                # does not re-sync (or skip) it incorrectly.
                self._dir_synced.add(path.parent)
                goal = path.name[len(self.scope) + 1 : -len(".jsonl")]
                # Read *bytes* and split on newlines before decoding: a crash
                # mid-``append`` can leave a torn multi-byte UTF-8 character at
                # the tail, and decoding the whole file up front
                # (``read_text(encoding='utf-8')``) would raise
                # ``UnicodeDecodeError`` and throw away the valid prefix
                # (behavior 7, P6). Decoding one newline-delimited chunk at a
                # time preserves every complete record before the torn tail and
                # isolates the undecodable fragment.
                raw_bytes = path.read_bytes()
                newline_terminated = raw_bytes.endswith(b"\n")
                lines: list[dict[str, Any]] = []
                tail_fragment: bytes | None = None
                chunks = raw_bytes.split(b"\n")
                for i, chunk in enumerate(chunks):
                    # A torn tail is tolerated only at the *end*: the last
                    # non-empty chunk is the fragment; anything after it is only
                    # newline padding.
                    is_last_nonempty = not any(c.strip() for c in chunks[i + 1 :])
                    if not chunk.strip():
                        continue
                    try:
                        text = chunk.decode("utf-8")
                    except UnicodeDecodeError:
                        # A torn multi-byte character. Only the trailing
                        # fragment is tolerated -- it is isolated so the next
                        # append writes a clean, standalone line. A mid-file
                        # undecodable chunk is out-of-band corruption: archive it
                        # (never silently drop it) and keep recovering any later
                        # lines.
                        if is_last_nonempty:
                            tail_fragment = chunk
                            break
                        self._archive_corrupt(path, chunk)
                        continue
                    text = text.strip()
                    if not text:
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        # Only the *trailing* fragment is tolerated. If any
                        # non-empty line follows this one, it is out-of-band
                        # mid-file corruption: archive it (never silently drop
                        # it) and skip just this line rather than silently
                        # deleting later records.
                        if is_last_nonempty:
                            tail_fragment = chunk
                            break
                        self._archive_corrupt(path, chunk)
                        continue
                    lines.append(record)
                if tail_fragment is not None:
                    self._isolate_incomplete_tail(path, lines, tail_fragment)
                elif not newline_terminated and lines:
                    # A complete, valid record terminated without its final
                    # newline (a crash between the JSON object write and its
                    # '\n') decodes here, but must be normalized before any
                    # further append: otherwise the next record concatenates
                    # onto it and the combined line no longer decodes on the
                    # next load, losing both records (behavior 1/7, P6).
                    self._rewrite_clean(path, lines)
                loaded[goal] = lines
            self._lines = loaded
            self._seq = {}
            for goal, lines in loaded.items():
                self._seq[goal] = max(
                    (int(r.get("seq", 0)) for r in lines if "seq" in r), default=0
                )
        return self

    def _isolate_incomplete_tail(
        self, path: Path, lines: list[dict[str, Any]], fragment: bytes
    ) -> None:
        """Rewrite ``path`` as only its complete, newline-terminated records and
        archive the trailing fragment so a later ``append`` no longer writes
        after a partial line."""
        self._rewrite_clean(path, lines)
        self._archive_corrupt(path, fragment)

    def _archive_corrupt(self, path: Path, raw: bytes) -> None:
        """Append raw journal bytes to the ``*.corrupt`` sidecar in binary mode,
        so an undecodable partial UTF-8 fragment is preserved verbatim rather
        than rounded through a text encoder. Torn / corrupt material is parked
        here where it is observable, never silently dropped."""
        corrupt_path = path.with_suffix(path.suffix + ".corrupt")
        with corrupt_path.open("ab") as fh:
            fh.write(raw + b"\n")

    def _rewrite_clean(self, path: Path, lines: list[dict[str, Any]]) -> None:
        """Atomically rewrite ``path`` as its complete, newline-terminated
        records (crash-safe repair, finding 3 / behavior 7 / P6).

        Never truncate the acknowledged prefix in place: write the retained
        records to a scratch file in the same directory, flush it, then
        atomically rename over the original. A crash at any point before the
        rename leaves the original journal intact (the valid prefix plus its
        trailing fragment), so the next load re-runs the same repair instead of
        reading a torn file."""
        clean = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in lines
        )
        tmp = path.with_suffix(path.suffix + ".repair")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(clean)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _next_seq(self, goal: str) -> int:
        seq = self._seq.get(goal, 0) + 1
        self._seq[goal] = seq
        return seq

    def _ensure_directory(self, path: Path) -> list[Path]:
        """Create ``path`` and any missing ancestors, returning the newly
        created directories deepest-first (empty when the chain already exists).

        ``mkdir(parents=True, exist_ok=True)`` cannot report which ancestor
        links are new, and a journal file's parent directory plus every
        newly-created ancestor's parent directory entry must each reach stable
        storage or a host crash can still lose the file or an entire directory
        level (behavior 1, 4, 7 / P6).
        """
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:  # filesystem root; cannot go higher
                break
            current = parent
        for directory in reversed(missing):
            directory.mkdir()
        return missing

    def _persist_append(self, goal: str, blob: str) -> None:
        """Durably append ``blob`` to ``goal``'s journal file.

        The bytes are written, flushed, then fsync'd *before* this returns, so
        a caller's acceptance acknowledgement (``submit``) or the start of an
        external effect (``_deliver``) cannot run ahead of the record reaching
        stable storage (behavior 1, 4, 7 / P6). A freshly created file also
        syncs its parent directory entry *and* every newly-created ancestor
        directory entry, so a crash cannot lose the file's very existence or a
        whole directory level. A file or directory fsync failure propagates: the
        caller issues no ack and admits nothing in memory, and the directory
        entry is not marked synced, so a retry re-attempts the incomplete sync
        instead of skipping it because the path now exists.
        """
        path = self._path(goal)
        new_dirs = self._ensure_directory(path.parent)
        needs_dir_sync = path.parent not in self._dir_synced
        with path.open("a", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            self.file_sync(fh.fileno())
        if needs_dir_sync:
            # Deepest-first: the journal file's own parent directory (which
            # gained the file entry), then each newly-created directory's parent
            # (which gained the new child entry).
            dirs: list[Path] = [path.parent]
            for directory in new_dirs:
                if directory.parent not in dirs:
                    dirs.append(directory.parent)
            for directory in dirs:
                self.dir_sync(directory)
            self._dir_synced.add(path.parent)

    def append(self, goal: str, record: dict[str, Any]) -> dict[str, Any]:
        """Append one line; return it with its assigned ``seq`` (if absent).

        The line is fsync'd to stable storage before this returns, so a caller
        that observes the returned record can rely on it surviving a host crash.
        """
        with self._lock:
            if "seq" not in record:
                record = {**record, "seq": self._next_seq(goal)}
            else:
                self._seq[goal] = max(self._seq.get(goal, 0), int(record["seq"]))
            if self.home is not None:
                self._persist_append(
                    goal,
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
                )
            self._lines.setdefault(goal, []).append(record)
            return record

    def append_many(self, goal: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Append several lines as one atomic append (single lock, single write).

        The lines share the lock, the write and the fsync, so a crash either
        leaves them all durable or a single trailing fragment (isolated by
        ``load``), never a torn prefix where the first line is durable but the
        rest are absent. ``finish_goal_call`` uses this to persist a completed
        call result and its resulting lifecycle mode *together* so the crash
        window between the two can no longer strand a drained call (final review
        finding)."""
        with self._lock:
            assigned: list[dict[str, Any]] = []
            for record in records:
                if "seq" not in record:
                    record = {**record, "seq": self._next_seq(goal)}
                else:
                    self._seq[goal] = max(self._seq.get(goal, 0), int(record["seq"]))
                assigned.append(record)
            if self.home is not None:
                blob = "".join(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    for record in assigned
                )
                self._persist_append(goal, blob)
            self._lines.setdefault(goal, []).extend(assigned)
            return assigned

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
    # ``position`` is the action's 1-based index in the *original* raw list, so
    # list-index attribution is never rewritten by the normalization: a
    # malformed entry interleaved between two valid ones does not shift the
    # later entries' original positions (behavior 4, lossless raw events).
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            receipts.append(
                _receipt(entry, reason="action must be an object", list_index=position)
            )
            continue
        kind = entry.get("kind")
        payload = entry.get("payload")
        key = entry.get("idempotency_key")
        if kind not in ACTION_KINDS:
            receipts.append(
                _receipt(entry, reason=f"unknown action kind {kind!r}", list_index=position)
            )
            continue
        if not isinstance(payload, dict):
            receipts.append(
                _receipt(entry, reason="action payload must be an object", list_index=position)
            )
            continue
        if not isinstance(key, str) or not key.strip():
            receipts.append(
                _receipt(entry, reason="idempotency_key is required", list_index=position)
            )
            continue
        if key in seen:
            receipts.append(
                _receipt(entry, reason=f"duplicate idempotency_key {key!r}", list_index=position)
            )
            continue
        seen.add(key)
        consumable.append(
            {
                "kind": kind,
                "payload": payload,
                "idempotency_key": key,
                "list_index": position,
            }
        )
    return consumable, receipts, _norm_intent(result)


def _norm_intent(result: dict[str, Any]) -> str | None:
    intent = result.get("intent")
    return intent if intent in INTENT_KINDS else None


def _receipt(
    entry: dict[str, Any] | None, *, reason: str, list_index: int | None = None
) -> dict[str, Any]:
    identified = entry if isinstance(entry, dict) else {}
    receipt: dict[str, Any] = {
        "kind": str(identified.get("kind") or ""),
        "idempotency_key": str(identified.get("idempotency_key") or ""),
        "status": FAILED,
        "reason": reason,
    }
    if list_index is not None:
        receipt["list_index"] = list_index
    return receipt


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
    The request's ``reply_to`` association rides alongside the input as the
    reply target pointer (behavior 1 "reply association"), so the Goal can
    address its ``reply`` action to the original caller without re-deriving it.
    """
    return {
        "request_id": current.request_id,
        "kind": current.kind,
        "caller": current.caller,
        "goal_version": current.goal_version,
        "input": current.input,
        "reply_to": current.reply_to,
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
    #: Serializes version activation against version-bound review admission
    #: (approve/reject). The journal lock guards individual journal operations,
    #: not the read-active-version -> deliver-effect boundary: without this lock,
    #: ``activate_version`` could persist and activate a new version in the gap
    #: after ``business_guards`` reads the active version and before the review
    #: reaches the external port, letting a review naming the then-stale version
    #: through after activation (behavior 3's version-bound review guard).
    _version_lock: threading.RLock = field(default_factory=threading.RLock)
    #: Serializes per-goal request acceptance and call admission. The journal
    #: lock guards *individual* journal operations, not the multi-step
    #: transitions this kernel performs: ``submit`` (dedup -> durable append ->
    #: enqueue -> mode wake), ``next_goal_call`` (resume/fence/mode check -> pop
    #: -> claim -> call record) and ``abort_unavailable_call`` (release fence ->
    #: re-queue front). Without this lock two concurrent ``submit`` callers of
    #: the same identity could both pass ``_find_request`` and both append/queue,
    #: two distinct callers could append A then B durably but enqueue B then A,
    #: and two ``next_goal_call`` callers could both pass the inflight check and
    #: pop separate requests before ``claim_call`` -- the loser raising after
    #: removing its request and leaving it unserved until reconstruction
    #: (behaviors 1/2/7, P5/P6). The guarded sections are in-memory +
    #: journal-mutating only -- never an external effect -- so this serializes
    #: *transitions*, not Goal calls, and a call on goal A is never serialized
    #: behind a call on goal B. It also serializes the lifecycle transitions
    #: (``stop``/``resume``) and the completion tail of ``finish_goal_call``
    #: against each other and against call admission (final review
    #: rf-1b50bcf1, behavior 5, P6): a concurrent stop must neither be lost by
    #: completion's stale mode read nor let a queued request start before
    #: resume.
    _request_lock: threading.RLock = field(default_factory=threading.RLock)
    #: Serializes resumed-execution *ownership* (final review rf-25eeb86e,
    #: behaviors 2/4/7, P6). The journal lock guards individual journal
    #: operations and ``_request_lock`` guards request/queue/fence *transitions*;
    #: neither guards the single-executor claim for a call whose execution was
    #: interrupted after its Stop List became durable. ``next_goal_call`` claims
    #: the pending-resume marker under ``_request_lock`` so only one caller
    #: receives the interrupted call; ``finish_goal_call`` additionally claims a
    #: per-goal executor token here so a second (erroneous) executor can neither
    #: re-run an effect nor release the fence a second time. The guarded section
    #: is a check-and-set only -- the effect loop runs outside it -- so a call on
    #: goal A is never serialized behind an external delivery on goal B.
    _execution_lock: threading.Lock = field(default_factory=threading.Lock)
    #: goal -> call_id currently executing ``finish_goal_call`` (ownership token).
    _executing: dict[str, str] = field(default_factory=dict)

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
                elif record == RECORD_CONTROL:
                    control = line.get("control")
                    if control == "mode":
                        mode = str(line.get("value") or MODE_RUNNING)
                    elif control == "stop" and line.get("terminated"):
                        # A persisted immediate-stop confirmation the runtime
                        # confirmed (``terminated``) is itself a durable stop
                        # event: derive ``stopped`` from it even when the
                        # separately-persisted mode record is missing (a crash
                        # between the two writes). A later ``control=mode`` line
                        # still overrides -- e.g. a resume *after* the stop --
                        # because records are replayed in durable order.
                        mode = MODE_STOPPED
                elif record == RECORD_REQUEST:
                    # A durable request is a wake event: it lifts
                    # blocked/waiting to running (behavior 5). Replaying it here
                    # in durable order recovers a wake that was lost when the
                    # process crashed after the request append but before the
                    # separately-persisted ``mode=running`` control line (final
                    # review finding; behaviors 5/7, P6). ``stopped`` and
                    # ``stopping`` are deliberately not lifted -- only
                    # ``resume`` moves a stopped goal, and a request recorded
                    # while stopped stays parked.
                    if mode in (MODE_BLOCKED, MODE_WAITING):
                        mode = MODE_RUNNING
                elif record == RECORD_CALL:
                    called.add(str(line.get("request_id") or ""))
                    current_call = str(line.get("call_id") or "") or None
                    current_call_request = str(line.get("request_id") or "") or None
                elif (
                    record == RECORD_CALL_RESULT
                    and current_call is not None
                    and line.get("call_id") == current_call
                ):
                    # Completing the call also moved the lifecycle mode. Derive
                    # it here so an interruption between the call-result write
                    # and the separately-persisted mode record (see
                    # ``_finish_goal_call_owned``) is recovered on replay: a
                    # drained graceful stop must rest ``stopped`` and a terminal
                    # intent must still move waiting/blocked, so a rebuilt
                    # product is neither stuck ``stopping`` forever nor left
                    # ``running`` when the Goal meant ``waiting``/``blocked``. A
                    # later ``control``/``mode`` line still overrides ``mode``.
                    mode = _derive_completion_mode(mode, line.get("intent"))
                    current_call = None
                    current_call_request = None
                elif (
                    record == RECORD_CALL_UNAVAILABLE
                    and current_call is not None
                    and line.get("call_id") == current_call
                ):
                    # A Goal call that could not be executed (the ReAct port was
                    # unavailable) is a recoverable failure, never a served
                    # request: its request stays pending for a later bound port
                    # instead of being dropped as "called" (final review finding).
                    if current_call_request:
                        called.discard(current_call_request)
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
            # A graceful stop that recorded ``stopping`` leaves no in-flight call
            # to drain once its call is interrupted before persisting a Stop List
            # (there is no result produced to drain). Reconcile that interrupted
            # drain to ``stopped`` instead of stranding the goal in ``stopping``
            # with no completion left to move it (behaviors 5/7, rf-823fba32).
            # ``stopping`` is only retained when a resume marker keeps a drain
            # alive (current_call still references a persisted Stop List).
            if mode == MODE_STOPPING and current_call is None:
                mode = MODE_STOPPED
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
                "effect_lock": threading.RLock(),
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
        # The persist + activate pair must be atomic w.r.t. a concurrent
        # version-bound review admission (final review finding): a review that
        # already read the active version and is about to reach the external
        # port must not deliver against a version that this activation lands
        # first. Both ``activate_version`` and the approve/reject delivery path
        # take ``_version_lock``, so they serialize.
        with self._version_lock:
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
        # Dedup + durable append + enqueue + mode wake are one serialized
        # transition: two concurrent submissions of the same identity must not
        # both pass the dedup check and both append, and two distinct
        # submissions must be appended and enqueued in the same order (they
        # share ``_request_lock`` with ``next_goal_call``).
        with self._request_lock:
            existing = self._find_request(request.request_id)
            if existing is not None:
                # A duplicate acceptance reuses the persisted record (P5) but
                # must still complete the wake a prior acceptance began: if the
                # original wake was lost to a crash (request durable, the
                # ``mode=running`` control line never written), the
                # re-submission restores it instead of acknowledging a request
                # that ``next_goal_call`` keeps refusing until an unrelated new
                # request or an explicit resume arrives (final review finding;
                # behavior 5). ``stopped``/``stopping`` are deliberately not
                # lifted -- only ``resume`` moves a stopped goal.
                if self._mode_of(goal) in (MODE_BLOCKED, MODE_WAITING):
                    self._set_mode(goal, MODE_RUNNING)
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
            "effect_lock": threading.RLock(),
        }

    def _effect_admission_lock(self, goal: str) -> threading.RLock:
        """The per-goal lock that serializes a confirmed stop against effect
        admission (final review rf-3c65b36f, behavior 5)."""
        return self._state.setdefault(goal, self._default_state())["effect_lock"]

    def _queue(self, goal: str, record: dict[str, Any]) -> None:
        st = self._state.setdefault(goal, self._default_state())
        st["queue"].append(record)

    def _queue_front(self, goal: str, record: dict[str, Any]) -> None:
        """Re-queue a request at its original head position (an aborted Goal
        call restores its place so submission order is preserved)."""
        st = self._state.setdefault(goal, self._default_state())
        st["queue"].insert(0, record)

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
        # The resume/fence/mode/queue admission is one serialized transition,
        # shared with ``submit`` and ``abort_unavailable_call`` via
        # ``_request_lock``: two concurrent callers must not both pass the
        # inflight check and pop separate requests before ``claim_call`` (the
        # loser raising after removing its request and leaving it unserved), and
        # a request must not be re-queued by ``abort_unavailable_call`` between
        # the queue-is-empty check and this pop.
        with self._request_lock:
            resume_id = st.get("resume")
            if resume_id:
                if self._mode_of(goal) == MODE_STOPPED:
                    return None  # suspended unstarted effects; wait for resume()
                persisted = self._stop_list_line(goal, resume_id)
                if persisted is None:
                    st["resume"] = None
                else:
                    # Atomically claim the interrupted call for exactly this
                    # caller (final review rf-25eeb86e): clear the pending-resume
                    # marker before returning the envelope, so a second caller
                    # observes no pending resume and -- with the fence still held
                    # by this call id -- falls through to ``None`` instead of
                    # receiving the same interrupted call and re-executing it.
                    # The durable journal, not this in-memory marker, remains the
                    # recovery source: a crash before ``finish_goal_call`` leaves
                    # the uncompleted call + Stop List on disk and the next
                    # ``restore()`` re-derives a fresh resume marker.
                    st["resume"] = None
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

    def _call_result_line(self, goal: str, call_id: str) -> dict[str, Any] | None:
        for line in self.journal.scan(goal):
            if line.get("record") == RECORD_CALL_RESULT and line.get("call_id") == call_id:
                return line
        return None

    def _claim_execution(self, goal: str, call_id: str) -> bool:
        """Atomically claim the single-executor slot for one call (rf-25eeb86e).

        Returns ``True`` if this caller now owns execution; ``False`` if another
        caller is already executing a call for ``goal`` -- in which case the
        caller must neither run effects nor release the fence. Check-and-set
        only: the effect loop runs outside ``_execution_lock``, so a delivery on
        goal A is never serialized behind a delivery on goal B."""
        with self._execution_lock:
            if goal in self._executing:
                return False
            self._executing[goal] = call_id
            return True

    def _release_execution(self, goal: str, call_id: str) -> None:
        with self._execution_lock:
            if self._executing.get(goal) == call_id:
                del self._executing[goal]

    def _execution_busy(self, goal: str, call_id: str) -> dict[str, Any]:
        """The no-op result for a caller that lost execution ownership."""
        call = self._call_line(goal, call_id) or {}
        return {
            "call_id": call_id,
            "goal": goal,
            "request_id": str(call.get("request_id") or ""),
            "intent": None,
            "receipts": [],
            "results": [],
            "executing_elsewhere": True,
        }

    def _persist_stop_list(
        self,
        goal: str,
        call_id: str,
        request_id: str,
        run_id: str,
        consumable: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        intent: str | None,
        *,
        raw_actions: Any,
        raw_intent: Any,
    ) -> dict[str, Any]:
        """Durably record the *validated* Stop List before any effect runs.

        This is the fork point for resume (finding 1): once this record is on
        disk, an interruption no longer needs a fresh Goal answer -- the
        original action identities (kind/idempotency_key/list_index) are
        recoverable and only outstanding items are executed on resume.

        The *complete raw Goal response* is persisted here too, before effects
        (finding: lossless raw events): ``raw_actions`` are the original action
        entries verbatim -- a malformed entry's payload and original position
        survive even though it is not routable -- and ``raw_intent`` is the
        original, possibly unsupported intent value. Neither may be reconstructed
        from the normalized projection, so both are written first and retained
        through resume so pagination never reduces the raw outcome.
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
                "raw_actions": raw_actions,
                "raw_intent": raw_intent,
                "actions": [
                    {
                        "kind": a["kind"],
                        "payload": a["payload"],
                        "idempotency_key": a["idempotency_key"],
                        "list_index": int(a.get("list_index", i)),
                    }
                    for i, a in enumerate(consumable, start=1)
                ],
                "malformed": [dict(r) for r in receipts],
                "at": _iso(self.clock),
            },
        )

    def abort_unavailable_call(self, goal: str, call_id: str, *, reason: str) -> dict[str, Any]:
        """Record an explicit Goal-call capability failure and release the fence
        *without* consuming the request.

        ``finish_goal_call`` persists a validated Stop List and a completed call
        result -- but when the Goal-call port is unavailable there is no Goal
        response to validate or execute, and fabricating an empty Stop List would
        mark the accepted request served (restore would then treat it as called
        and resubmission of its stable identity would be refused as a duplicate,
        so a later bound port could not deliver the original request). Instead
        this records the capability failure as a first-class journal line,
        releases the ownership fence, and re-queues the request at its original
        head position so it stays pending for a later bound port (final review
        finding)."""
        call = self._call_line(goal, call_id) or {}
        request_id = str(call.get("request_id") or "")
        record = self.journal.append(
            goal,
            {
                "record": RECORD_CALL_UNAVAILABLE,
                "goal": goal,
                "call_id": call_id,
                "request_id": request_id,
                "reason": reason,
                "at": _iso(self.clock),
            },
        )
        # Releasing the fence and putting the request back at the head is part
        # of the same admission serialization as ``next_goal_call``'s pop: a
        # concurrent ``next_goal_call`` must not observe the released fence and
        # an empty queue before this re-queues the request.
        with self._request_lock:
            self.journal.release_call(goal, call_id)
            if request_id:
                request = self._find_request(request_id)
                if request is not None:
                    self._queue_front(goal, request)
        return record

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
        fence stays held, and nothing runs until ``resume()`` (finding 2).

        Execution is also *exclusively owned* (final review rf-25eeb86e,
        behaviors 2/4/7, P6): the executor claims the per-goal token up front and
        releases it on every exit path, so a second (duplicated) execution of the
        same call neither re-runs an effect nor releases the fence a second time.
        The durable journal -- not the in-memory token -- remains the recovery
        source across a rebuild."""
        if not self._claim_execution(goal, call_id):
            # A concurrent executor already owns this call. Do not run effects or
            # release the fence (the owner releases it on completion).
            return self._execution_busy(goal, call_id)
        try:
            return self._finish_goal_call_owned(goal, call_id, stop_list)
        finally:
            self._release_execution(goal, call_id)

    def _finish_goal_call_owned(
        self, goal: str, call_id: str, stop_list: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self._call_result_line(goal, call_id)
        if existing is not None:
            # This call already completed durably (a sequential re-finish of a
            # drained call). Return the durable result without re-running effects
            # or releasing the fence again.
            return {
                "call_id": call_id,
                "goal": goal,
                "request_id": str(existing.get("request_id") or ""),
                "intent": existing.get("intent"),
                "receipts": [],
                "results": [],
                "reconciled": True,
            }
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
            # The complete raw response survives the interruption: retain the
            # original action entries and unsupported intent verbatim so the
            # resumed call result never collapses into a reduced projection.
            raw_actions = persisted.get("raw_actions")
            raw_intent = persisted.get("raw_intent")
        else:
            consumable, receipts, intent = validate_stop_list(stop_list)
            raw_actions = stop_list.get("actions") if isinstance(stop_list, dict) else None
            raw_intent = stop_list.get("intent") if isinstance(stop_list, dict) else None
            self._persist_stop_list(
                goal,
                call_id,
                request_id,
                run_id,
                consumable,
                receipts,
                intent,
                raw_actions=raw_actions,
                raw_intent=raw_intent,
            )

        results: list[dict[str, Any]] = []
        # P1 same-list dependency: a ``dispatch`` is only admitted for a
        # repository whose same-list ``add_repo`` succeeded first. The set of
        # repos the whole list must admit is identified *before* any effect
        # runs, so a dispatch ordered ahead of its ``add_repo`` (or after a
        # failed one) is refused rather than executing before admission. A
        # refused or lost admission blocks the dependent dispatch without
        # erasing the independent results already delivered in this list.
        admission_required = {
            str((a.get("payload") or {}).get("repo_path") or "")
            for a in consumable
            if a["kind"] == ACTION_ADD_REPO
            and (a.get("payload") or {}).get("repo_path")
        }
        repo_admission: dict[str, str] = {}
        for position, action in enumerate(consumable, start=1):
            index = int(action.get("list_index", position))
            # Read the active version at the review-effect boundary, not from a
            # snapshot taken before the list executes: an earlier effect may
            # have activated a new version mid-list, and an approve/reject naming
            # the then-stale version must be refused against the *current* one.
            #
            # A version-bound review (approve/reject) is admitted *and* delivered
            # under ``_version_lock``: the guard read and the external port call
            # are one critical section, so a concurrent ``activate_version``
            # cannot persist and activate a new version in the gap between
            # "naming v1 still passes" and "v1 reaches the port". The lock is
            # taken again per action, so a mid-list activation by an earlier
            # effect is still observed by a later review (the earlier activation
            # finished and released the lock).
            if action["kind"] in (ACTION_APPROVE, ACTION_REJECT):
                with self._version_lock:
                    result = self._admit_action(
                        goal,
                        action,
                        call_id,
                        request_id,
                        run_id,
                        index,
                        caller,
                        admission_required=admission_required,
                        repo_admission=repo_admission,
                    )
            else:
                result = self._admit_action(
                    goal,
                    action,
                    call_id,
                    request_id,
                    run_id,
                    index,
                    caller,
                    admission_required=admission_required,
                    repo_admission=repo_admission,
                )
            if result.get("suspended"):
                # Finding 2 + rf-3c65b36f: a confirmed stop suspends unstarted
                # effects. The already-persisted list (and any earlier results)
                # survive; the fence is held and resume() re-runs these items
                # through reconciliation -- never by re-invoking Goal.
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
                    int(receipt.get("list_index") or 0),
                    str(receipt.get("kind") or ""),
                    FAILED,
                    str(receipt.get("reason") or "malformed action"),
                    final=True,
                ),
            )
        # A stop that landed while this call was in flight is authoritative:
        # graceful stop drains this result then rests ``stopped``; an immediate
        # stop the runtime confirmed holds ``stopped``. Neither is overwritten
        # by the returned intent (behavior 5). The completion's lifecycle mode
        # is derived with the *same* rule ``restore`` replays, and it is
        # persisted atomically *with* the completed call result: a crash can no
        # longer leave a drained call whose durable mode still reads
        # ``stopping`` (or ``running`` when the Goal meant waiting/blocked) with
        # no record to finish the transition (final review finding).
        #
        # The *whole* completion tail -- mode derivation, persistence, the
        # in-memory projection update and the fence release -- is one serialized
        # transition shared with ``stop``/``resume`` and call admission via
        # ``_request_lock`` (final review rf-1b50bcf1, behavior 5, P6). Without
        # it completion could read ``running``; a concurrent stop could then
        # record ``stopping``/``stopped`` while the fence is still held; and
        # completion would subsequently persist its stale ``running``/``waiting``
        # mode and drop the fence -- letting queued requests start without
        # resume.
        with self._request_lock:
            mode = self._mode_of(goal)
            next_mode = _derive_completion_mode(mode, intent)
            records: list[dict[str, Any]] = [
                {
                    "record": RECORD_CALL_RESULT,
                    "goal": goal,
                    "call_id": call_id,
                    "request_id": request_id,
                    "run_id": run_id,
                    "intent": intent,
                    "raw_intent": raw_intent,
                    "actions": raw_actions if isinstance(raw_actions, list) else [],
                    "at": _iso(self.clock),
                }
            ]
            if mode != MODE_STOPPED:
                # A stop the runtime already confirmed needs no further
                # transition; every other completion moves the lifecycle (a
                # graceful stop reaches ``stopped``, a terminal intent moves
                # waiting/blocked, and otherwise the mode is re-asserted) and is
                # written with the call result as one atomic append.
                records.append(
                    {
                        "record": RECORD_CONTROL,
                        "goal": goal,
                        "control": "mode",
                        "value": next_mode,
                        "at": _iso(self.clock),
                    }
                )
            self.journal.append_many(goal, records)

            self.journal.release_call(goal, call_id)

            # A call was fully drained (fresh or resumed): clear any resume
            # marker.
            st = self._state.get(goal)
            if st and st.get("resume") == call_id:
                st["resume"] = None

            # The transition is already durable above; keep the in-memory
            # projection in step without a second journal write.
            self._state.setdefault(goal, self._default_state())["mode"] = next_mode
            self._mode[goal] = next_mode

        return {
            "call_id": call_id,
            "goal": goal,
            "request_id": request_id,
            "intent": intent,
            "receipts": [*receipts],
            "results": results,
        }

    def _admit_action(
        self,
        goal: str,
        action: dict[str, Any],
        call_id: str,
        request_id: str,
        run_id: str,
        index: int,
        caller: str,
        *,
        admission_required: set[str],
        repo_admission: dict[str, str],
    ) -> dict[str, Any]:
        """Admit and deliver one action, serialized against a confirmed stop.

        This is the single effect-admission point (final review rf-3c65b36f,
        behavior 5): the re-check of ``MODE_STOPPED`` and the guard read /
        intent persistence / external delivery are one critical section held by
        a per-goal reentrant lock that ``stop`` also acquires when it persists
        ``MODE_STOPPED``. Without it, an action could pass an earlier mode
        check, pause on ``_version_lock`` (approve/reject) or on journal writes
        (dispatch/reply), and then reach its external port after a stop has
        already persisted ``MODE_STOPPED`` and returned ``stopped`` -- starting
        an unstarted effect after the stop, with no resume in between.

        The lock is per-goal and taken per-action, so a delivery on goal A is
        never serialized behind a delivery on goal B. It is reentrant, so a
        stop issued from a surface already inside the admission (a port that
        stops itself mid-delivery) does not deadlock and still suspends the
        remaining unstarted items.

        Returns ``{"suspended": True}`` when a stop has confirmed while this
        action was still unstarted; the caller records the resume marker and
        returns the suspended call without running this or any later effect.
        """
        with self._effect_admission_lock(goal):
            if self._mode_of(goal) == MODE_STOPPED:
                return {"suspended": True}
            return self._process_action(
                goal,
                action,
                call_id,
                request_id,
                run_id,
                index,
                caller,
                admission_required=admission_required,
                repo_admission=repo_admission,
            )

    def _process_action(
        self,
        goal: str,
        action: dict[str, Any],
        call_id: str,
        request_id: str,
        run_id: str,
        index: int,
        caller: str,
        *,
        admission_required: set[str],
        repo_admission: dict[str, str],
    ) -> dict[str, Any]:
        """Admit and deliver one action (guard -> dependency -> effect).

        The business guard is read here at the effect boundary, so an
        approve/reject is refused against the *current* active version, never a
        snapshot cached before the list executed. Callers decide whether version
        activation must be excluded across the admission-delivery boundary (the
        approve/reject path takes ``_version_lock`` around this call)."""
        refusal = business_guards(action, active_version=self.active_version(goal))
        if refusal:
            return self._refuse(goal, action, call_id, request_id, run_id, index, refusal)
        if action["kind"] == ACTION_DISPATCH:
            repo = str((action.get("payload") or {}).get("repo_path") or "")
            if repo in admission_required and repo_admission.get(repo) != DELIVERED:
                return self._refuse(
                    goal,
                    action,
                    call_id,
                    request_id,
                    run_id,
                    index,
                    f"dependent dispatch refused: add_repo for {repo!r} did not succeed",
                )
        return self._effect_outcome(goal, action, call_id, request_id, run_id, index, caller)

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
        # A prior result short-circuits only when the obligation is *closed*:
        # ``delivered`` (fulfilled) or a definitive ``failed`` refusal (explicit
        # disposition). A ``not_ready`` receipt is final for the *attempt* but is
        # neither fulfilled nor disposed, so a later Goal-authorized replay must
        # NOT replay to the stale ``not_ready`` answer -- it falls through to
        # observe / re-deliver once a port is bound, exactly as ``unknown`` (or a
        # retryable ``failed``) already does (P3/P4, behavior 4/7).
        if existing is not None and self._delivery_retired(existing):
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
        # An unknown observation is a first-class reconciliation outcome, not a
        # silent return: persist it against the original action identity with
        # the current request/run/index attribution so pagination and a later
        # reconstruction can expose that the outcome is unknown and requires
        # Goal judgement (behaviors 4 and 6). It stays non-final, so a future
        # confirmation or observed absence is never locked out by this receipt.
        self.journal.append(
            goal,
            self._result_record(
                goal,
                action_id,
                request_id,
                run_id,
                index,
                kind,
                UNKNOWN,
                "outcome unknown; recoverable and requires Goal judgement",
                final=False,
            ),
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

    def outstanding_deliveries(self, goal: str) -> list[dict[str, Any]]:
        """The goal's deliveries whose obligation is still unresolved.

        A delivery obligation is born when an action intent is recorded. It is
        retired only by *fulfillment* (``delivered``) or an *explicit
        disposition* (a definitive refusal, e.g. a business-guard refusal) --
        the finality of the attempt is deliberately kept separate from whether
        the required delivery ever happened or was explicitly retired. A
        ``not_ready`` receipt is final for the attempt (never blindly retried)
        but is neither fulfilled nor explicitly disposed, so it stays
        outstanding; ``unknown`` and a retryable ``failed`` delivery stay
        outstanding too. Derived purely from the durable journal, so a rebuilt
        product reconstructs the same obligations (P4): ``done`` must not
        complete while any remain.
        """
        latest: dict[str, dict[str, Any]] = {}
        for line in self.journal.scan(goal):
            action_id = str(line.get("action_id") or "")
            if not action_id:
                continue
            if line.get("record") == RECORD_ACTION:
                latest.setdefault(action_id, {"intent": line, "result": None})
            elif line.get("record") == RECORD_ACTION_RESULT:
                entry = latest.setdefault(action_id, {"intent": None, "result": None})
                entry["result"] = line
        outstanding: list[dict[str, Any]] = []
        for action_id, entry in latest.items():
            result = entry["result"]
            if result is not None and self._delivery_retired(result):
                continue
            intent = entry["intent"] or {}
            record = dict(intent)
            record["action_id"] = action_id
            if result is not None:
                record["latest_status"] = result.get("status")
            outstanding.append(record)
        return outstanding

    @staticmethod
    def _delivery_retired(result: dict[str, Any]) -> bool:
        """Whether a delivery obligation is closed: fulfilled, or explicitly
        disposed by a definitive refusal.

        ``delivered`` is fulfillment. A definitive ``failed`` receipt (``final``
        truthy) is only ever produced by a *refusal* (a business guard or a
        malformed entry), which is an explicit disposition -- the delivery was
        rejected, never forgotten. A ``not_ready`` receipt is ``final`` for the
        attempt yet is neither fulfilled nor disposed, so it is *not* retired.
        """
        status = result.get("status")
        return status == DELIVERED or (status == FAILED and bool(result.get("final")))

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
            # not-ready receipt, never a simulated success. The *attempt* is
            # final (it is never blindly retried), but the required delivery is
            # neither fulfilled nor explicitly disposed: it remains an
            # outstanding obligation (P4) until Goal explicitly retires it, so
            # a later ``done`` must not forget it.
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
            # never a printout, never a simulated termination. Recording it and
            # moving the mode is serialized with call completion and call
            # admission via ``_request_lock`` so a concurrent completion cannot
            # overwrite a confirmed stop (final review rf-1b50bcf1, behavior 5,
            # P6). The runtime cancellation port is invoked *outside* the lock.
            #
            # The persisted ``MODE_STOPPED`` transition is additionally
            # serialized with effect admission via the per-goal admission lock
            # (final review rf-3c65b36f, behavior 5): without it, an action that
            # already passed its mode check could reach its external port after
            # this stop has persisted ``MODE_STOPPED`` and returned ``stopped``,
            # starting an unstarted effect after the stop with no resume in
            # between. The admission lock is acquired *before* ``_request_lock``
            # so a stop issued from a surface already inside an effect admission
            # (reentrant) can never deadlock against a concurrent admission.
            with self._effect_admission_lock(goal):
                with self._request_lock:
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
        # The inflight check + mode transition is serialized with call
        # completion and call admission via ``_request_lock`` (rf-1b50bcf1).
        with self._request_lock:
            if self._mode_of(goal) == MODE_STOPPED:
                # A confirmed stop (immediate, runtime ``terminated``) is
                # authoritative over a repeated or *weaker* graceful stop: it
                # must never be downgraded to ``stopping``, which would re-open
                # admission of the suspended unstarted list without a resume
                # (behavior 5, final review finding rf-b8cad9f2).
                return {"goal": goal, "mode": mode, "stopped": True, "cancel": None}
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
        with self._request_lock:
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


def _derive_completion_mode(mode: str, intent: str | None) -> str:
    """The lifecycle mode a *completed* Goal call transitions into.

    Shared by ``restore`` (reconstruction) and ``_finish_goal_call_owned`` (the
    live path) so both land on the same mode. A call that drained a graceful
    stop rests ``stopped``; an already-stopped goal stays stopped; otherwise a
    terminal intent (waiting/blocked) moves the mode and an absent/unknown
    intent (``None`` or ``done``) leaves it unchanged -- ``done`` is an intent,
    never a terminal shortcut (P4).
    """
    if mode in (MODE_STOPPING, MODE_STOPPED):
        return MODE_STOPPED
    return _intent_mode(intent) or mode


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
    "RECORD_CALL_UNAVAILABLE",
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

#!/usr/bin/env python3
"""E1 decision-bridge acceptance: isolated process drills over real SQLite.

Two families of scenario, both running the *real* bridge process
(`fleet-graph decision-bridge run`) -- never a mock calling the handler
directly. The isolated owner is always a fake resume owner, but the bus is
either fake (isolated loop, no network) or real:

- ``resume-under-5s`` / ``kill-restart-exactly-once``: the fake-bus loop. The
  fake bus serves the real reverse-refs shape, but it is still an in-process
  stand-in, so it does not satisfy spec item 4's "not a mocked adapter".
- ``real-resume-under-5s`` / ``real-restart-exactly-once``: the *real-bus*
  drill. Publish a throwaway card + question note + ``work.decision.v1`` whose
  refs carry the question entity id onto the real agent-bus, then point the
  bridge's ``GET /v1/entities/<id>/refs`` at that real endpoint and measure
  decision-to-resume latency (item 4) and a kill/restart exactly-once replay
  (item 5). These require ``--bus-token-file`` + ``--decision-token-file``.

Evidence is one JSON object per scenario on stdout, containing the UTC
timestamp, source message id (real entity/message ids for the real-bus drill),
target/action key, per-owner call count, cursor before/after, receipt status,
and the scenario exit code.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

#: The one waiting owner the fake world contains.
QUESTION_ID = "q-1"
CARD_ID = "card-1"
DEVELOPMENT_ID = "dev-abc"
DECISION_MESSAGE_ID = "d-1"
DECISION_SEQ = 1
DECISION_KIND = "work.decision.v1"
WORK_NOTES = "board:work-notes"
DEFAULT_BUS_URL = "http://127.0.0.1:7490"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FakeBusState:
    """The board: read by the bridge via messages and the reverse-refs endpoint.

    The real bus does not inline refs on served messages; a decision's question
    is discovered through ``GET /v1/entities/<question>/refs``. The fake mirrors
    that: ``accept_decision`` records the message, and the decision's reference
    to the question is served from a reverse index.
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.refs: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.accepted_monotonic: float | None = None
        self.poll_count = 0

    def accept_decision(self) -> None:
        with self.lock:
            self.messages.append(
                {
                    "message_id": DECISION_MESSAGE_ID,
                    "channel_seq": DECISION_SEQ,
                    "kind": DECISION_KIND,
                    "payload": {"decision": "APPROVE", "card_entity_id": CARD_ID},
                }
            )
            self.refs[QUESTION_ID] = [DECISION_MESSAGE_ID]
            self.accepted_monotonic = time.monotonic()

    def refs_to(self, entity_id: str) -> list[dict]:
        with self.lock:
            return [
                {"message_id": mid, "target_entity": entity_id}
                for mid in self.refs.get(entity_id, [])
            ]

    def messages_after(self, after_seq: int, limit: int) -> tuple[list[dict], int]:
        with self.lock:
            self.poll_count += 1
            selected = [m for m in self.messages if int(m["channel_seq"]) > after_seq]
            head = max((int(m["channel_seq"]) for m in self.messages), default=0)
            return selected[: max(1, limit)], head


class FakeBusHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == f"/v1/channels/{WORK_NOTES}/messages":
            query = parse_qs(parsed.query)
            after_seq = int((query.get("after_seq") or ["0"])[0])
            limit = int((query.get("limit") or ["100"])[0])
            messages, head = self.server.bus_state.messages_after(after_seq, limit)  # type: ignore[attr-defined]
            self._json({"messages": messages, "head_seq": head})
            return
        prefix = "/v1/entities/"
        suffix = "/refs"
        if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
            entity_id = parsed.path[len(prefix) : -len(suffix)]
            self._json({"refs": self.server.bus_state.refs_to(entity_id)})  # type: ignore[attr-defined]
            return
        self._json({"messages": [], "head_seq": 0})

    def _json(self, obj: object) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeOwnerState:
    """The resume owner: dedups on the action key, records calls, persists them.

    The (question, card, development) identity is parameterisable so the real
    bus drill can run the same owner against the throwaway entities it just
    published on the real bus, instead of the fixed ``q-1`` / ``card-1``.
    """

    def __init__(
        self,
        work_dir: Path,
        *,
        question_note_id: str = QUESTION_ID,
        card_entity_id: str = CARD_ID,
        development_id: str = DEVELOPMENT_ID,
    ) -> None:
        self.work_dir = work_dir
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.logical_resumes = 0
        self.calls: list[dict] = []
        self.first_response_monotonic: float | None = None
        self.record_file = work_dir / "owner_responses.jsonl"
        self.question_note_id = question_note_id
        self.card_entity_id = card_entity_id
        self.development_id = development_id

    def discover(self, question_note_id: str) -> list[dict]:
        if question_note_id != self.question_note_id:
            return []
        return self.discover_all()

    def discover_all(self) -> list[dict]:
        return [
            {
                "kind": "dd",
                "id": self.development_id,
                "generation": 1,
                "question_note_id": self.question_note_id,
                "card_entity_id": self.card_entity_id,
                "state": "awaiting_gate",
            }
        ]

    def resume(self, action_key: str, generation: int) -> dict:
        with self.lock:
            already = action_key in self.seen
            if already:
                status, logical = "already_resumed", False
            else:
                self.seen.add(action_key)
                self.logical_resumes += 1
                status, logical = "resumed", True
                if self.first_response_monotonic is None:
                    self.first_response_monotonic = time.monotonic()
            record = {
                "action_key": action_key,
                "generation": generation,
                "state": status,
                "logical": logical,
                "at_utc": utc_now(),
                "at_monotonic": time.monotonic(),
            }
            self.calls.append(record)
            # Persist the owner's response on disk before returning it, so the
            # kill-restart scenario can observe "owner's first response persisted".
            with self.record_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return {"status": status, "detail": "dedup" if already else "ok", "logical": logical}

    def first_response_persisted(self) -> bool:
        return self.record_file.exists() and self.record_file.stat().st_size > 0


class FakeOwnerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/owners":
            self._json({"owners": []}, code=404)
            return
        query = parse_qs(parsed.query)
        if "question_note_id" in query:
            owners = self.server.owner_state.discover((query.get("question_note_id") or [""])[0])  # type: ignore[attr-defined]
        else:
            owners = self.server.owner_state.discover_all()  # type: ignore[attr-defined]
        self._json({"owners": owners})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/resume":
            self._json({"status": "refused", "detail": "unknown route"}, code=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        result = self.server.owner_state.resume(  # type: ignore[attr-defined]
            str(body.get("action_key") or ""), int(body.get("generation") or 1)
        )
        self._json(result)

    def _json(self, obj: object, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(
    handler: type[BaseHTTPRequestHandler], state: object, attr: str
) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    setattr(server, attr, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def read_store(state_dir: Path) -> tuple[int, list[dict]]:
    """Read the bridge's durable state without taking a write lock."""
    db = state_dir / "bridge.sqlite3"
    if not db.exists():
        return 0, []
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT board_seq FROM cursor WHERE id = 1").fetchone()
        cursor = int(row["board_seq"]) if row is not None else 0
        receipts = [dict(r) for r in conn.execute("SELECT * FROM receipts ORDER BY created_at")]
        conn.close()
        return cursor, receipts
    except sqlite3.Error:
        return 0, []


def wait_for(cond, timeout: float, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def start_bridge(
    state_dir: Path,
    bus_url: str,
    owner_url: str,
    log: Path,
    extra: list[str],
    *,
    bus_token_file: str | None = None,
) -> subprocess.Popen:
    env = dict(os.environ)
    if bus_token_file:
        # Real-bus drill: drive the bridge with a real read credential instead
        # of a dummy one, so its GET /v1/entities/<id>/refs hits the real bus.
        env["FLEET_GRAPH_BUS_TOKEN_FILE"] = str(bus_token_file)
    else:
        token_file = state_dir / "bus.token"
        token_file.write_text("dummy-read-token", encoding="utf-8")
        env["FLEET_GRAPH_BUS_TOKEN_FILE"] = str(token_file)
    env.pop("FLEET_GRAPH_DECISION_TOKEN_FILE", None)  # never inherited, belt and braces
    argv = [
        sys.executable,
        "-m",
        "fleet_graph.cli",
        "decision-bridge",
        "run",
        "--bus-url",
        bus_url,
        "--state-dir",
        str(state_dir),
        "--owner-url",
        owner_url,
        "--poll-interval",
        "0.05",
        *extra,
    ]
    out = log.open("wb")
    return subprocess.Popen(argv, env=env, stdout=out, stderr=subprocess.STDOUT)


def terminate(proc: subprocess.Popen, how: int = signal.SIGTERM) -> None:
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(how)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def find_receipt(receipts: list[dict], message_id: str) -> dict | None:
    for receipt in receipts:
        if receipt.get("source_message_id") == message_id:
            return receipt
    return None


# --- scenarios --------------------------------------------------------------


def scenario_resume_under_5s(work_dir: Path, max_latency: float) -> dict:
    state_dir = work_dir / "resume" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    bus_state = FakeBusState()
    owner_state = FakeOwnerState(work_dir / "resume")
    bus_server, bus_port = start_server(FakeBusHandler, bus_state, "bus_state")
    owner_server, owner_port = start_server(FakeOwnerHandler, owner_state, "owner_state")
    try:
        bus_url = f"http://127.0.0.1:{bus_port}"
        owner_url = f"http://127.0.0.1:{owner_port}"
        proc = start_bridge(state_dir, bus_url, owner_url, work_dir / "resume" / "bridge.log", [])
        try:
            # Let the bridge open its store and do its first (empty) poll.
            wait_for(lambda: bus_state.poll_count >= 1, timeout=5.0)

            bus_state.accept_decision()  # T0: the fake bus accepts the event
            cursor_before = 0

            # The <5s bound is on the owner's *persisted* success: the moment
            # the fake owner durably records its first logical resume.
            converged = wait_for(lambda: owner_state.logical_resumes >= 1, timeout=max_latency + 5)
            latency = (
                (owner_state.first_response_monotonic - bus_state.accepted_monotonic)
                if converged
                and owner_state.first_response_monotonic
                and bus_state.accepted_monotonic is not None
                else None
            )

            def sealed() -> bool:
                _cursor, _receipts = read_store(state_dir)
                rec = find_receipt(_receipts, DECISION_MESSAGE_ID)
                return rec is not None and rec.get("status") == "resumed"

            # The receipt seal lands a moment after the owner answers; converge
            # on it before reading the terminal cursor/receipt for the evidence.
            wait_for(sealed, timeout=5.0)
            cursor_after, receipts = read_store(state_dir)
            receipt = find_receipt(receipts, DECISION_MESSAGE_ID)

            passed = bool(
                converged
                and latency is not None
                and latency < max_latency
                and owner_state.logical_resumes == 1
                and receipt is not None
                and receipt.get("status") == "resumed"
                and cursor_after >= DECISION_SEQ
            )
            evidence = {
                "scenario": "resume-under-5s",
                "utc_timestamp": utc_now(),
                "pass": passed,
                "source_message_id": DECISION_MESSAGE_ID,
                "target_kind": "dd",
                "target_id": DEVELOPMENT_ID,
                "generation": 1,
                "action_key": f"e1:{DECISION_MESSAGE_ID}:dd:{DEVELOPMENT_ID}:1",
                "owner_calls": len(owner_state.calls),
                "logical_resumes": owner_state.logical_resumes,
                "cursor_before": cursor_before,
                "cursor_after": cursor_after,
                "receipt_status": receipt.get("status") if receipt else None,
                "latency_seconds": round(latency, 4) if latency is not None else None,
                "max_latency_seconds": max_latency,
                "exit_code": 0 if passed else 1,
            }
            return evidence
        finally:
            terminate(proc)
    finally:
        bus_server.shutdown()
        owner_server.shutdown()


def scenario_kill_restart_exactly_once(work_dir: Path, max_recovery: float) -> dict:
    state_dir = work_dir / "killrestart" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = work_dir / "killrestart" / "kill-window.json"

    bus_state = FakeBusState()
    owner_state = FakeOwnerState(work_dir / "killrestart")
    bus_server, bus_port = start_server(FakeBusHandler, bus_state, "bus_state")
    owner_server, owner_port = start_server(FakeOwnerHandler, owner_state, "owner_state")
    try:
        bus_url = f"http://127.0.0.1:{bus_port}"
        owner_url = f"http://127.0.0.1:{owner_port}"

        proc = start_bridge(
            state_dir,
            bus_url,
            owner_url,
            work_dir / "killrestart" / "bridge1.log",
            ["--kill-window-file", str(sentinel), "--kill-window-seconds", "3.0"],
        )
        try:
            wait_for(lambda: bus_state.poll_count >= 1, timeout=5.0)
            bus_state.accept_decision()

            # The crash window: intent recorded, owner's first response
            # persisted, terminal seal not yet written.
            window = wait_for(
                lambda: sentinel.exists() and owner_state.first_response_persisted(),
                timeout=max_recovery + 5,
            )
            cursor_before, receipts = read_store(state_dir)
            receipt_in_window = find_receipt(receipts, DECISION_MESSAGE_ID)
            crashed_before_seal = bool(
                window
                and receipt_in_window is not None
                and receipt_in_window.get("status") == "intent_recorded"
                and cursor_before == 0  # cursor untouched before terminal disposal
            )
        finally:
            terminate(proc, how=signal.SIGKILL)

        sentinel.unlink(missing_ok=True)

        restart_at = time.monotonic()
        proc2 = start_bridge(
            state_dir, bus_url, owner_url, work_dir / "killrestart" / "bridge2.log", []
        )
        try:

            def converged() -> bool:
                _cursor, _receipts = read_store(state_dir)
                rec = find_receipt(_receipts, DECISION_MESSAGE_ID)
                return rec is not None and rec.get("status") == "resumed"

            ok = wait_for(converged, timeout=max_recovery + 5)
            recovery_seconds = time.monotonic() - restart_at
            cursor_after, receipts = read_store(state_dir)
            receipt = find_receipt(receipts, DECISION_MESSAGE_ID)

            exactly_one = owner_state.logical_resumes == 1
            single_target = {call["action_key"] for call in owner_state.calls} == {
                f"e1:{DECISION_MESSAGE_ID}:dd:{DEVELOPMENT_ID}:1"
            }

            passed = bool(
                crashed_before_seal
                and ok
                and recovery_seconds < max_recovery
                and exactly_one
                and single_target
                and len(receipts) == 1
                and receipt is not None
                and receipt.get("status") == "resumed"
                and cursor_after >= cursor_before
            )
            evidence = {
                "scenario": "kill-restart-exactly-once",
                "utc_timestamp": utc_now(),
                "pass": passed,
                "crashed_before_seal": crashed_before_seal,
                "source_message_id": DECISION_MESSAGE_ID,
                "target_kind": "dd",
                "target_id": DEVELOPMENT_ID,
                "generation": 1,
                "action_key": f"e1:{DECISION_MESSAGE_ID}:dd:{DEVELOPMENT_ID}:1",
                "owner_calls": len(owner_state.calls),
                "logical_resumes": owner_state.logical_resumes,
                "cursor_before": cursor_before,
                "cursor_after": cursor_after,
                "receipt_count": len(receipts),
                "receipt_status": receipt.get("status") if receipt else None,
                "recovery_seconds": round(recovery_seconds, 4),
                "max_recovery_seconds": max_recovery,
                "exit_code": 0 if passed else 1,
            }
            return evidence
        finally:
            terminate(proc2)
    finally:
        bus_server.shutdown()
        owner_server.shutdown()


# --- real-bus drills ---------------------------------------------------------


class RealBusClient:
    """The *real* agent-bus refs surface, driven through ``BusClient``/``Board``.

    ``refs_to`` on the real bus is ``GET /v1/entities/<id>/refs`` returning
    ``{"refs": [{"message_id", "target_entity", ...}]}``. This class publishes
    one throwaway card + question note + decision and reads the reverse refs
    back through the same real endpoint -- no in-process fake, no mocked
    adapter. The resume owner stays isolated (see ``--owner-url``), because the
    spec's "not a mocked adapter" bound is on the bus endpoint.
    """

    def __init__(self, bus_url: str, *, decision_token_file: str | None = None) -> None:
        from fleet_graph.bus.board import Board
        from fleet_graph.bus.client import BusClient

        self.bus_url = bus_url
        self.client = BusClient(base_url=bus_url)
        self.board = Board(self.client)
        self.decision_client: BusClient | None = None
        if decision_token_file:
            from fleet_graph.bus.client import BusClient

            self.decision_client = BusClient(
                base_url=bus_url, token=Path(decision_token_file).read_text().strip()
            )

    def head_seq(self) -> int:
        _messages, head = self.client.messages(WORK_NOTES, limit=1)
        return int(head)

    def publish_card(self, tag: str) -> str:
        result = self.board.publish_card(
            {
                "title": f"e1 real-bus refs drill {tag}",
                "status": "doing",
                "intent": "e1 decision-bridge real-bus refs acceptance drill",
                "development_id": f"e1-drill-{tag}",
                "links": [],
            },
            idempotency_key=f"e1-real-bus-drill-card:{tag}",
        )
        return result.entity_id

    def publish_question(self, card_entity_id: str, tag: str) -> str:
        ticket = self.board.ask(
            card_entity_id=card_entity_id,
            question=f"e1 real-bus refs drill question {tag}",
            idempotency_key=f"e1-real-bus-drill-q:{tag}",
        )
        return ticket.question_note_id

    def publish_decision(self, question_note_id: str, card_entity_id: str, tag: str) -> str:
        payload = {
            "card_entity_id": card_entity_id,
            "decided_by": "e1-real-bus-refs-drill",
            "decision": "APPROVE",
            "question": f"e1 real-bus refs drill question {tag}",
            "rationale": "live acceptance drill",
        }
        client = self.decision_client if self.decision_client is not None else self.client
        result = client.publish(
            WORK_NOTES,
            DECISION_KIND,
            payload,
            idempotency_key=f"e1-real-bus-drill-d:{tag}",
            refs=[{"target_entity": question_note_id}],
        )
        return result.message_id

    def refs_to(self, entity_id: str) -> list[dict]:
        return self.client.refs_to(entity_id)


def seed_cursor(state_dir: Path, seq: int) -> None:
    """Point a fresh isolated store at the board head so the drill does not
    replay the board's entire history -- the bridge must only consider the
    throwaway decision this drill just published on the real bus."""
    from fleet_graph.decision_bridge.store import BridgeStore

    store = BridgeStore(state_dir).open()
    try:
        store.advance_cursor(seq)
    finally:
        store.close()


def scenario_real_resume_under_5s(
    work_dir: Path,
    bus_url: str,
    decision_token_file: str,
    bus_token_file: str,
    max_latency: float,
) -> dict:
    tag = uuid.uuid4().hex[:8]
    state_dir = work_dir / "real-resume" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    bus = RealBusClient(bus_url, decision_token_file=decision_token_file)

    head = bus.head_seq()
    seed_cursor(state_dir, head)

    owner_dev = f"e1-drill-{tag}"
    card_id = bus.publish_card(tag)
    question_id = bus.publish_question(card_id, tag)

    owner_state = FakeOwnerState(
        work_dir / "real-resume",
        question_note_id=question_id,
        card_entity_id=card_id,
        development_id=owner_dev,
    )
    owner_server, owner_port = start_server(FakeOwnerHandler, owner_state, "owner_state")
    try:
        owner_url = f"http://127.0.0.1:{owner_port}"
        log = work_dir / "real-resume" / "bridge.log"
        proc = start_bridge(state_dir, bus_url, owner_url, log, [], bus_token_file=bus_token_file)
        try:
            wait_for(lambda: log.exists() and log.stat().st_size > 0, timeout=5.0)

            t0 = time.monotonic()
            decision_id = bus.publish_decision(question_id, card_id, tag)
            cursor_before = head

            converged = wait_for(lambda: owner_state.logical_resumes >= 1, timeout=max_latency + 5)
            latency = (
                (owner_state.first_response_monotonic - t0)
                if converged and owner_state.first_response_monotonic is not None
                else None
            )

            def sealed() -> bool:
                _cursor, _receipts = read_store(state_dir)
                rec = find_receipt(_receipts, decision_id)
                return rec is not None and rec.get("status") == "resumed"

            wait_for(sealed, timeout=5.0)
            cursor_after, receipts = read_store(state_dir)
            receipt = find_receipt(receipts, decision_id)

            refs = bus.refs_to(question_id)
            ref_names_decision = any(str(r.get("message_id")) == decision_id for r in refs)

            passed = bool(
                converged
                and latency is not None
                and latency < max_latency
                and owner_state.logical_resumes == 1
                and ref_names_decision
                and receipt is not None
                and receipt.get("status") == "resumed"
                and cursor_after >= cursor_before
            )
            evidence = {
                "scenario": "real-resume-under-5s",
                "utc_timestamp": utc_now(),
                "bus_url": bus_url,
                "pass": passed,
                "real_card_entity_id": card_id,
                "real_question_note_id": question_id,
                "real_decision_message_id": decision_id,
                "refs_to_question": refs,
                "target_kind": "dd",
                "target_id": owner_dev,
                "generation": 1,
                "action_key": f"e1:{decision_id}:dd:{owner_dev}:1",
                "owner_calls": len(owner_state.calls),
                "logical_resumes": owner_state.logical_resumes,
                "cursor_before": cursor_before,
                "cursor_after": cursor_after,
                "receipt_status": receipt.get("status") if receipt else None,
                "latency_seconds": round(latency, 4) if latency is not None else None,
                "max_latency_seconds": max_latency,
                "exit_code": 0 if passed else 1,
            }
            return evidence
        finally:
            terminate(proc)
    finally:
        owner_server.shutdown()


def scenario_real_restart_exactly_once(
    work_dir: Path,
    bus_url: str,
    decision_token_file: str,
    bus_token_file: str,
    max_recovery: float,
) -> dict:
    tag = uuid.uuid4().hex[:8]
    state_dir = work_dir / "real-restart" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = work_dir / "real-restart" / "kill-window.json"
    bus = RealBusClient(bus_url, decision_token_file=decision_token_file)

    head = bus.head_seq()
    seed_cursor(state_dir, head)

    owner_dev = f"e1-drill-{tag}"
    card_id = bus.publish_card(tag)
    question_id = bus.publish_question(card_id, tag)

    owner_state = FakeOwnerState(
        work_dir / "real-restart",
        question_note_id=question_id,
        card_entity_id=card_id,
        development_id=owner_dev,
    )
    owner_server, owner_port = start_server(FakeOwnerHandler, owner_state, "owner_state")
    try:
        owner_url = f"http://127.0.0.1:{owner_port}"
        proc = start_bridge(
            state_dir,
            bus_url,
            owner_url,
            work_dir / "real-restart" / "bridge1.log",
            ["--kill-window-file", str(sentinel), "--kill-window-seconds", "3.0"],
            bus_token_file=bus_token_file,
        )
        try:
            wait_for(
                lambda: (work_dir / "real-restart" / "bridge1.log").stat().st_size > 0,
                timeout=5.0,
            )
            decision_id = bus.publish_decision(question_id, card_id, tag)
            window = wait_for(
                lambda: sentinel.exists() and owner_state.first_response_persisted(),
                timeout=max_recovery + 5,
            )
            cursor_before, receipts = read_store(state_dir)
            receipt_in_window = find_receipt(receipts, decision_id)
            crashed_before_seal = bool(
                window
                and receipt_in_window is not None
                and receipt_in_window.get("status") == "intent_recorded"
            )
        finally:
            terminate(proc, how=signal.SIGKILL)

        sentinel.unlink(missing_ok=True)

        restart_at = time.monotonic()
        proc2 = start_bridge(
            state_dir,
            bus_url,
            owner_url,
            work_dir / "real-restart" / "bridge2.log",
            [],
            bus_token_file=bus_token_file,
        )
        try:

            def converged() -> bool:
                _cursor, _receipts = read_store(state_dir)
                rec = find_receipt(_receipts, decision_id)
                return rec is not None and rec.get("status") == "resumed"

            ok = wait_for(converged, timeout=max_recovery + 5)
            recovery_seconds = time.monotonic() - restart_at
            cursor_after, receipts = read_store(state_dir)
            receipt = find_receipt(receipts, decision_id)

            exactly_one = owner_state.logical_resumes == 1
            single_target = {call["action_key"] for call in owner_state.calls} == {
                f"e1:{decision_id}:dd:{owner_dev}:1"
            }

            passed = bool(
                crashed_before_seal
                and ok
                and recovery_seconds < max_recovery
                and exactly_one
                and single_target
                and receipt is not None
                and receipt.get("status") == "resumed"
                and cursor_after >= cursor_before
            )
            evidence = {
                "scenario": "real-restart-exactly-once",
                "utc_timestamp": utc_now(),
                "bus_url": bus_url,
                "pass": passed,
                "crashed_before_seal": crashed_before_seal,
                "real_card_entity_id": card_id,
                "real_question_note_id": question_id,
                "real_decision_message_id": decision_id,
                "target_kind": "dd",
                "target_id": owner_dev,
                "generation": 1,
                "action_key": f"e1:{decision_id}:dd:{owner_dev}:1",
                "owner_calls": len(owner_state.calls),
                "logical_resumes": owner_state.logical_resumes,
                "cursor_before": cursor_before,
                "cursor_after": cursor_after,
                "receipt_count": len(receipts),
                "receipt_status": receipt.get("status") if receipt else None,
                "recovery_seconds": round(recovery_seconds, 4),
                "max_recovery_seconds": max_recovery,
                "exit_code": 0 if passed else 1,
            }
            return evidence
        finally:
            terminate(proc2)
    finally:
        owner_server.shutdown()


# --- cli --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=[
            "resume-under-5s",
            "kill-restart-exactly-once",
            "real-resume-under-5s",
            "real-restart-exactly-once",
        ],
    )
    parser.add_argument("--max-latency-seconds", type=float, default=5.0)
    parser.add_argument("--kill-after", default="intent_recorded", choices=["intent_recorded"])
    parser.add_argument("--max-recovery-seconds", type=float, default=5.0)
    parser.add_argument("--work-dir", default=None, help="scratch dir (default: a fresh temp dir)")
    parser.add_argument("--bus-url", default=None, help="real agent-bus URL (real-bus scenarios)")
    parser.add_argument(
        "--bus-token-file",
        default=os.environ.get("FLEET_GRAPH_BUS_TOKEN_FILE"),
        help="read credential for the bridge subprocess",
    )
    parser.add_argument(
        "--decision-token-file",
        default=os.environ.get("FLEET_GRAPH_DECISION_TOKEN_FILE"),
        help="decision-publish credential for the human decision",
    )
    return parser


def _real_bus_deps(args: argparse.Namespace) -> tuple[str, str, str]:
    bus_url = args.bus_url or DEFAULT_BUS_URL
    if not args.bus_token_file or not Path(args.bus_token_file).exists():
        raise SystemExit("real-bus scenario needs --bus-token-file (read credential)")
    if not args.decision_token_file or not Path(args.decision_token_file).exists():
        raise SystemExit("real-bus scenario needs --decision-token-file (publish credential)")
    return bus_url, args.bus_token_file, args.decision_token_file


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.work_dir:
        work_dir = Path(args.work_dir)
    else:
        import tempfile

        work_dir = Path(tempfile.mkdtemp(prefix="e1-decision-bridge-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.scenario == "resume-under-5s":
        evidence = scenario_resume_under_5s(work_dir, args.max_latency_seconds)
    elif args.scenario == "kill-restart-exactly-once":
        evidence = scenario_kill_restart_exactly_once(work_dir, args.max_recovery_seconds)
    elif args.scenario == "real-resume-under-5s":
        bus_url, bus_token_file, decision_token_file = _real_bus_deps(args)
        evidence = scenario_real_resume_under_5s(
            work_dir, bus_url, decision_token_file, bus_token_file, args.max_latency_seconds
        )
    else:
        bus_url, bus_token_file, decision_token_file = _real_bus_deps(args)
        evidence = scenario_real_restart_exactly_once(
            work_dir, bus_url, decision_token_file, bus_token_file, args.max_recovery_seconds
        )

    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if evidence.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())

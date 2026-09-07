"""引擎拥有的审单请求与裁决；无需已经退役的看板卡创建路径。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from fleet_graph.bus.board import Decision, GateTicket


def _write_atomic(path: Path, payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".gate-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GateStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, request_id: str, kind: str) -> Path:
        digest = hashlib.sha256(request_id.encode()).hexdigest()
        return self.root / f"{digest}.{kind}.json"

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        request_id = "engine:" + idempotency_key
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(request_id, "request")
        payload = {"request_id": request_id, "question": question}
        with (self.root / "store.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                if json.loads(path.read_text()) != payload:
                    raise ValueError("同一审单请求的待审内容发生变化")
            else:
                _write_atomic(path, payload)
        return GateTicket(question_note_id=request_id, card_entity_id="")

    def decide(
        self, request_id: str, *, decision: str, decided_by: str, reason: str, action_key: str
    ) -> dict:
        if decision not in {"APPROVE", "REJECT"} or not decided_by.strip():
            raise ValueError("裁决必须为 APPROVE/REJECT 且包含裁决者")
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "store.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            request = json.loads(self._path(request_id, "request").read_text())
            payload = {
                "decision": decision,
                "decided_by": decided_by,
                "rationale": reason,
                "request_id": request_id,
                "question": request["question"],
                "message_id": "engine-decision:"
                + hashlib.sha256((request_id + ":" + action_key).encode()).hexdigest(),
                "action_key": action_key,
            }
            path = self._path(request_id, "decision")
            if path.exists():
                existing = json.loads(path.read_text())
                identity = ("request_id", "action_key", "decision", "decided_by")
                if any(existing.get(key) != payload[key] for key in identity):
                    raise ValueError("审单请求已有不同裁决，拒绝覆盖")
                return existing
            _write_atomic(path, payload)
            return payload

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        path = self._path(ticket.question_note_id, "decision")
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        return Decision(
            message_id=payload["message_id"],
            decision=payload["decision"],
            decided_by=payload["decided_by"],
            question=payload["question"],
            rationale=payload["rationale"],
            card_entity_id="",
            raw=payload,
        )

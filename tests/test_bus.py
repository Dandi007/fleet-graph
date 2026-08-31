"""Bus client and board behaviour, against a recording transport.

No live agent-bus needed. The shapes asserted here were read off the running
bus (protocol registry plus real messages), so drift shows up as a test
failure rather than a 422 in production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.bus.board import (
    CARD_KIND,
    DECISION_KIND,
    DECISION_KIND_V2,
    DECISION_KINDS,
    NOTE_KIND,
    Board,
    GateTicket,
)
from fleet_graph.bus.client import (
    BusClient,
    BusConflict,
    BusError,
    load_token,
)


class RecordingTransport:
    """Replays canned responses and records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[tuple[int, Any]] = []
        self.default: tuple[int, Any] = (200, {})

    def queue(self, status: int, body: Any) -> None:
        self.responses.append((status, body))

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json_body})
        if self.responses:
            return self.responses.pop(0)
        return self.default

    @property
    def published(self) -> list[dict[str, Any]]:
        return [c["body"] for c in self.calls if c["method"] == "POST" and c["body"]]


def publish_ok(message_id: str = "msg_1", seq: int = 1) -> tuple[int, Any]:
    return (
        200,
        {
            "message_id": message_id,
            "entity_id": message_id,
            "channel_seq": seq,
            "deduplicated": False,
        },
    )


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def client(transport: RecordingTransport) -> BusClient:
    return BusClient(token="tok", agent_id="fleet-graph", transport=transport)


@pytest.fixture
def board(client: BusClient) -> Board:
    return Board(client, observability_channel="gd:fleet-graph")


class TestCredentials:
    def test_token_from_env(self) -> None:
        assert load_token({"FLEET_GRAPH_BUS_TOKEN": " secret \n"}) == "secret"

    def test_token_from_file(self, tmp_path) -> None:
        path = tmp_path / "bus.token"
        path.write_text("filetoken\n")
        assert load_token({"FLEET_GRAPH_BUS_TOKEN_FILE": str(path)}) == "filetoken"

    def test_env_wins_over_file(self, tmp_path) -> None:
        path = tmp_path / "bus.token"
        path.write_text("filetoken")
        env = {"FLEET_GRAPH_BUS_TOKEN": "envtoken", "FLEET_GRAPH_BUS_TOKEN_FILE": str(path)}
        assert load_token(env) == "envtoken"

    def test_missing_credential_is_an_error_not_a_default(self) -> None:
        with pytest.raises(RuntimeError, match="no bus credential"):
            load_token({})


class TestLineTokenOwnership:
    """Gate-6 ownership: canonicalized paths + positive secrets boundary.

    A line's token is owned only when its realpath is exactly the canonical
    ``<secrets_root>/<alias>.token`` regular file -- resolving into the
    supervision plane, another line's token, outside the boundary, or a
    symlink masquerade is a refusal (U4 defect 3).
    """

    def _secrets(self, tmp_path: Path, names: list[str]) -> Path:
        secrets = tmp_path / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        for name in names:
            (secrets / name).write_text("token", encoding="utf-8")
        return secrets

    def test_a_regular_owned_token_is_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = self._secrets(tmp_path, ["ronin-owned.token"])
        result = resolve_line_token_ownership(
            "ronin-owned", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is True
        assert result.status == "owned"

    def test_a_missing_token_is_not_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        result = resolve_line_token_ownership(
            "ronin-nope", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is False
        assert result.status == "missing"

    def test_a_directory_token_is_non_regular(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "ronin-dir.token").mkdir()
        result = resolve_line_token_ownership(
            "ronin-dir", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is False
        assert result.status == "non_regular"

    def test_a_supervision_plane_token_is_not_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = self._secrets(tmp_path, [])
        supervision = tmp_path / "supervision"
        supervision.mkdir()
        (supervision / "fleet-graph.token").write_text("control", encoding="utf-8")
        (secrets / "ronin-sup.token").symlink_to(supervision / "fleet-graph.token")
        result = resolve_line_token_ownership(
            "ronin-sup",
            template=str(secrets / "{alias}.token"),
            secrets_root=secrets,
            supervision_roots=(supervision,),
        )
        assert result.owned is False
        assert result.status == "supervision_plane"

    def test_an_other_line_token_is_not_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = self._secrets(tmp_path, ["ronin-other.token"])
        (secrets / "ronin-x.token").symlink_to(secrets / "ronin-other.token")
        result = resolve_line_token_ownership(
            "ronin-x", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is False
        assert result.status == "other_line"

    def test_a_symlink_masquerade_is_not_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = self._secrets(tmp_path, [])
        (secrets / "real").mkdir()
        (secrets / "real" / "ronin-x.token").write_text("real", encoding="utf-8")
        (secrets / "ronin-x.token").symlink_to(secrets / "real" / "ronin-x.token")
        result = resolve_line_token_ownership(
            "ronin-x", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is False
        assert result.status == "symlink_alias"

    def test_an_outside_boundary_token_is_not_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = self._secrets(tmp_path, [])
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ronin-x.token").write_text("elsewhere", encoding="utf-8")
        (secrets / "ronin-x.token").symlink_to(outside / "ronin-x.token")
        result = resolve_line_token_ownership(
            "ronin-x", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is False
        assert result.status == "outside_boundary"

    def test_an_unsafe_alias_is_refused(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = self._secrets(tmp_path, [])
        result = resolve_line_token_ownership(
            "../escape", template=str(secrets / "{alias}.token"), secrets_root=secrets
        )
        assert result.owned is False
        assert result.status == "unsafe"


class TestClient:
    def test_auth_and_identity_headers(
        self, client: BusClient, transport: RecordingTransport
    ) -> None:
        transport.queue(*publish_ok())
        client.publish("ch", "k", {"a": 1}, "idem")
        headers = transport.calls[0]["headers"]
        assert headers["Authorization"] == "Bearer tok"
        # R1-返工：own_agent_id 未知（未显式给出）时**不发**委托头——改走非委托
        # 自证路径，以 token 自身身份发布，避免 403 DELEGATION_NOT_PERMITTED。
        assert "X-Bus-On-Behalf-Of" not in headers

    def test_no_delegation_header_when_acting_as_self(self, transport: RecordingTransport) -> None:
        """R1-返工 根修：token 即该 agent 自身（own_agent_id == agent_id）时
        不得发 X-Bus-On-Behalf-Of——发了会被 bus 当成无权委托，403
        DELEGATION_NOT_PERMITTED（生产实锤：fleet-graph 服务 token 每 publish
        全 403，best-effort 静默吞掉）。
        """
        self_client = BusClient(
            token="tok", agent_id="fleet-graph", own_agent_id="fleet-graph", transport=transport
        )
        transport.queue(*publish_ok())
        self_client.publish("ch", "k", {"a": 1}, "idem")
        headers = transport.calls[0]["headers"]
        assert headers["Authorization"] == "Bearer tok"
        assert "X-Bus-On-Behalf-Of" not in headers

    def test_delegation_header_only_for_a_different_agent(
        self, transport: RecordingTransport
    ) -> None:
        """真正替别的 agent 行事（agent_id != own_agent_id）才发委托头。"""
        delegating = BusClient(
            token="tok", agent_id="someone-else", own_agent_id="fleet-graph", transport=transport
        )
        transport.queue(*publish_ok())
        delegating.publish("ch", "k", {"a": 1}, "idem")
        headers = transport.calls[0]["headers"]
        assert headers["X-Bus-On-Behalf-Of"] == "someone-else"

    def test_publish_body_carries_optional_fields_only_when_set(
        self, client: BusClient, transport: RecordingTransport
    ) -> None:
        transport.queue(*publish_ok())
        client.publish("ch", "k", {"a": 1}, "idem")
        assert transport.calls[0]["body"] == {
            "kind": "k",
            "payload": {"a": 1},
            "idempotency_key": "idem",
        }

        transport.queue(*publish_ok())
        client.publish(
            "ch",
            "k",
            {"a": 1},
            "idem2",
            refs=[{"target_entity": "e"}],
            entity_id="e",
            supersedes="s",
        )
        body = transport.calls[1]["body"]
        assert body["refs"] == [{"target_entity": "e"}]
        assert body["entity_id"] == "e"
        assert body["supersedes"] == "s"

    def test_conflict_is_its_own_exception(
        self, client: BusClient, transport: RecordingTransport
    ) -> None:
        transport.queue(409, {"code": "VERSION_CONFLICT"})
        with pytest.raises(BusConflict):
            client.publish("ch", "k", {}, "idem")

    def test_other_errors_raise_bus_error(
        self, client: BusClient, transport: RecordingTransport
    ) -> None:
        transport.queue(422, {"code": "DERIVATION_ERROR", "message": "needs a ref"})
        with pytest.raises(BusError) as excinfo:
            client.publish("ch", "k", {}, "idem")
        assert excinfo.value.status == 422
        assert "needs a ref" in excinfo.value.body

    def test_conflict_is_a_bus_error_subclass(self) -> None:
        assert issubclass(BusConflict, BusError)


class TestNotes:
    def test_note_always_carries_a_ref_to_its_card(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        """work.note.v1 is refs_required on the live bus; omitting it is a 422."""
        transport.queue(*publish_ok())
        board.note(card_entity_id="card_1", text="hello", note_type="progress", idempotency_key="i")
        body = transport.calls[0]["body"]
        assert body["kind"] == NOTE_KIND
        assert body["refs"] == [{"target_entity": "card_1"}]
        assert body["payload"] == {
            "card_entity_id": "card_1",
            "note": "hello",
            "note_type": "progress",
        }

    @pytest.mark.parametrize(
        ("method", "expected_type"),
        [("evidence", "evidence"), ("progress", "progress")],
    )
    def test_note_helpers_set_their_type(
        self, board: Board, transport: RecordingTransport, method: str, expected_type: str
    ) -> None:
        transport.queue(*publish_ok())
        getattr(board, method)(card_entity_id="card_1", text="t", idempotency_key="i")
        assert transport.calls[0]["body"]["payload"]["note_type"] == expected_type


class TestCards:
    def test_revision_sends_entity_and_supersedes(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(*publish_ok())
        board.revise_card(
            entity_id="card_1",
            supersedes="msg_prev",
            payload={"status": "doing"},
            idempotency_key="i",
        )
        body = transport.calls[0]["body"]
        assert body["kind"] == CARD_KIND
        assert body["entity_id"] == "card_1"
        assert body["supersedes"] == "msg_prev"

    def test_losing_the_cas_race_raises_rather_than_clobbering(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(409, {"code": "VERSION_CONFLICT"})
        with pytest.raises(BusConflict):
            board.revise_card(
                entity_id="card_1", supersedes="stale", payload={}, idempotency_key="i"
            )

    def test_card_head_is_the_newest_revision(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "m1",
                        "entity_id": "card_1",
                        "kind": CARD_KIND,
                        "channel_seq": 3,
                        "payload": {"status": "ready"},
                    },
                    {
                        "message_id": "m2",
                        "entity_id": "card_1",
                        "kind": CARD_KIND,
                        "channel_seq": 9,
                        "payload": {"status": "doing"},
                    },
                    {
                        "message_id": "m3",
                        "entity_id": "other",
                        "kind": CARD_KIND,
                        "channel_seq": 11,
                        "payload": {},
                    },
                ],
                "head_seq": 11,
            },
        )
        head = board.card_head("card_1")
        assert head is not None
        assert head["message_id"] == "m2"

    def test_card_head_is_none_when_unknown(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"messages": [], "head_seq": 0})
        assert board.card_head("nope") is None


class TestHumanGate:
    def test_ask_posts_a_question_note_and_returns_a_ticket(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(*publish_ok("msg_q", 7))
        ticket = board.ask(card_entity_id="card_1", question="a or b?", idempotency_key="i")

        body = transport.calls[0]["body"]
        assert body["payload"]["note_type"] == "question"
        assert ticket.question_note_id == "msg_q"
        assert ticket.card_entity_id == "card_1"

    def test_ticket_round_trips_through_a_checkpoint(self) -> None:
        ticket = GateTicket(question_note_id="msg_q", card_entity_id="card_1")
        assert GateTicket.from_dict(json.loads(json.dumps(ticket.to_dict()))) == ticket

    def test_open_question_has_no_decision(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"refs": []})
        ticket = GateTicket("msg_q", "card_1")
        assert board.decision_for(ticket) is None

    def test_decision_is_resolved_through_the_ref_graph(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"refs": [{"message_id": "msg_d", "target_entity": "msg_q"}]})
        transport.queue(200, {"messages": [], "head_seq": 12})  # head 学习针
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "msg_d",
                        "kind": DECISION_KIND,
                        "channel_seq": 12,
                        "payload": {
                            "decision": "take option (a)",
                            "decided_by": "human:operator",
                            "question": "a or b?",
                            "rationale": "cheapest",
                            "card_entity_id": "card_1",
                        },
                    }
                ],
                "head_seq": 12,
            },
        )
        decision = board.decision_for(GateTicket("msg_q", "card_1"))
        assert decision is not None
        assert decision.decision == "take option (a)"
        assert decision.decided_by == "human:operator"
        assert decision.message_id == "msg_d"

    def test_v1_question_answer_decision_is_still_recognized(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        """兼收：v1 留给人工问答裁决，decision_for 必须继续认它。"""
        transport.queue(200, {"refs": [{"message_id": "msg_d", "target_entity": "msg_q"}]})
        transport.queue(200, {"messages": [], "head_seq": 12})  # head 学习针
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "msg_d",
                        "kind": DECISION_KIND,
                        "channel_seq": 5,
                        "payload": {
                            "card_entity_id": "card_1",
                            "question": "merge?",
                            "decision": "REJECT",
                            "decided_by": "human:operator",
                        },
                    }
                ],
                "head_seq": 5,
            },
        )
        decision = board.decision_for(GateTicket("msg_q", "card_1"))
        assert decision is not None
        assert decision.decision == "REJECT"

    def test_v2_gate_release_decision_is_recognized_by_the_gate_read_path(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        """兼收：decision_publisher 发的 v2 gate_release 必须解锁 gate。"""
        transport.queue(200, {"refs": [{"message_id": "msg_d", "target_entity": "msg_q"}]})
        transport.queue(200, {"messages": [], "head_seq": 12})  # head 学习针
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "msg_d",
                        "kind": DECISION_KIND_V2,
                        "channel_seq": 7,
                        "payload": {
                            "kind": "gate_release",
                            "decision": "APPROVE",
                            "decided_by": "supervisor-graph (依预授权 msg-p-1 代行；非人逐条拍板)",
                            "card_entity_id": "card_1",
                            "question_note_id": "msg_q",
                            "preauth_message_id": "msg-p-1",
                            "target_ref": "refs/heads/dd/dev-abc",
                            "scope": "merge_only",
                        },
                    }
                ],
                "head_seq": 7,
            },
        )
        decision = board.decision_for(GateTicket("msg_q", "card_1"))
        assert decision is not None
        assert decision.decision == "APPROVE"
        assert decision.message_id == "msg_d"

    def test_a_non_decision_reply_does_not_count_as_a_verdict(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        """Someone commenting on the question is not someone deciding it."""
        transport.queue(200, {"refs": [{"message_id": "msg_n", "target_entity": "msg_q"}]})
        transport.queue(200, {"messages": [], "head_seq": 12})  # head 学习针
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "msg_n",
                        "kind": NOTE_KIND,
                        "channel_seq": 12,
                        "payload": {"note": "good question", "note_type": "progress"},
                    }
                ],
                "head_seq": 12,
            },
        )
        assert board.decision_for(GateTicket("msg_q", "card_1")) is None

    def test_newest_decision_wins_when_a_verdict_is_revised(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {
                "refs": [
                    {"message_id": "msg_d1", "target_entity": "msg_q"},
                    {"message_id": "msg_d2", "target_entity": "msg_q"},
                ]
            },
        )
        transport.queue(200, {"messages": [], "head_seq": 30})  # head 学习针
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "msg_d1",
                        "kind": DECISION_KIND,
                        "channel_seq": 12,
                        "payload": {"decision": "first"},
                    },
                    {
                        "message_id": "msg_d2",
                        "kind": DECISION_KIND,
                        "channel_seq": 30,
                        "payload": {"decision": "second, after more thought"},
                    },
                ],
                "head_seq": 30,
            },
        )
        decision = board.decision_for(GateTicket("msg_q", "card_1"))
        assert decision is not None
        assert decision.decision == "second, after more thought"


class TestAgentMayNotDecide:
    """plan.md: 裁决只认 work.decision.v1/v2，agent 不得代拍.

    Enforced structurally -- the Board simply has no verdict-publishing method.
    """

    def test_board_exposes_no_way_to_publish_a_verdict(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.default = (200, {"messages": [], "head_seq": 0, "refs": []})
        for _ in range(6):
            transport.queue(*publish_ok())

        board.publish_card({"title": "t"}, "i1")
        board.revise_card(entity_id="c", supersedes="s", payload={}, idempotency_key="i2")
        board.note(card_entity_id="c", text="n", note_type="progress", idempotency_key="i3")
        board.evidence(card_entity_id="c", text="e", idempotency_key="i4")
        board.progress(card_entity_id="c", text="p", idempotency_key="i5")
        board.ask(card_entity_id="c", question="q", idempotency_key="i6")
        board.observe({"event": "tick"}, "i7")

        kinds = {body.get("kind") for body in transport.published}
        assert not kinds & set(DECISION_KINDS)

    def test_no_public_method_smells_like_deciding(self, board: Board) -> None:
        forbidden = ("decide", "verdict", "approve", "resolve_question")
        public = [name for name in dir(board) if not name.startswith("_")]
        assert not [n for n in public if any(word in n.lower() for word in forbidden)]


class TestObservabilityIsBestEffort:
    def test_failures_are_swallowed(self, board: Board, transport: RecordingTransport) -> None:
        """The pump's rule (INV: 写失败不阻塞) carried forward."""
        transport.queue(500, {"code": "BOOM"})
        board.observe({"event": "round"}, "i")  # must not raise

    def test_noop_without_a_configured_channel(
        self, client: BusClient, transport: RecordingTransport
    ) -> None:
        Board(client).observe({"event": "round"}, "i")
        assert transport.calls == []


class TestLoopbackIsNeverProxied:
    """This host runs a SOCKS proxy; loopback services must bypass it."""

    def test_httpx_transport_ignores_proxy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fleet_graph.bus.client import HttpxTransport

        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7891")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        # Constructing must not raise, and must not pick up a proxy mount.
        transport = HttpxTransport()
        assert transport._client.trust_env is False


class TestGatePaginationRegression:
    """2026-08-31 生产实锤：频道破千后裸 limit=1000（升序=最老窗）令新裁决不可见，
    全部 gate 假等。decision_for 必须先学 head 再读尾窗。"""

    def test_decision_beyond_first_thousand_is_found(
        self, board: Board, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"refs": [{"message_id": "msg_d", "target_entity": "msg_q"}]})
        # 第一针：head 学习（频道已有 1500 条）
        transport.queue(200, {"messages": [], "head_seq": 1500})
        # 第二针：尾窗返回裁决（seq 1490，在最老千条窗之外）
        transport.queue(
            200,
            {
                "messages": [
                    {
                        "message_id": "msg_d",
                        "kind": DECISION_KIND,
                        "channel_seq": 1490,
                        "payload": {
                            "decision": "APPROVE",
                            "decided_by": "human:operator",
                            "question": "q",
                            "rationale": "r",
                            "card_entity_id": "card_1",
                        },
                    }
                ],
                "head_seq": 1500,
            },
        )
        decision = board.decision_for(GateTicket("msg_q", "card_1"))
        assert decision is not None and decision.message_id == "msg_d"
        # 尾窗请求必须带 after_seq=head-1000
        tail_call = transport.calls[-1]
        assert "after_seq=500" in str(tail_call), tail_call

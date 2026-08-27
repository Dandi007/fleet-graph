"""decision_publisher：窄门的每条边都要顶一下。

它只会发一种东西（merge_only 的 APPROVE、payload.kind=gate_release、代行
署名、refs 双指向），并且只在拿到 granted 的三要素判定和独立凭证时才发。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fleet_graph.supervise.decision_publisher import (
    APPROVE_DECISION,
    DECISION_TOKEN_ENV,
    RELEASE_PAYLOAD_KIND,
    SCOPE_MERGE_ONLY,
    DecisionPublishRefused,
    decided_by_for,
    load_decision_token,
    publish_release_decision,
)
from fleet_graph.supervise.preauth import ReleaseEvaluation


class FakeClient:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, channel, kind, payload, idempotency_key, *, refs=None, **_kw):
        self.published.append(
            {
                "channel": channel,
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "refs": refs or [],
            }
        )

        class _Result:
            message_id = "msg-decision-1"
            entity_id = "msg-decision-1"
            channel_seq = 1
            deduplicated = False

        return _Result()


def granted_evaluation() -> ReleaseEvaluation:
    return ReleaseEvaluation(
        granted=True,
        reasons=(),
        preauth_message_id="msg-preauth-1",
        target_ref="refs/heads/dd/dev-abc",
    )


def publish(client: FakeClient, evaluation: ReleaseEvaluation | None = None):
    return publish_release_decision(
        evaluation=evaluation if evaluation is not None else granted_evaluation(),
        card_entity_id="card-7",
        question_note_id="msg-q-1",
        rationale="机械审计全绿；依预授权放行",
        idempotency_key="supervisor-preauth:e1-msg-q-1",
        client=client,  # type: ignore[arg-type]
    )


class TestPublishShape:
    def test_decision_is_approve_with_merge_only_scope(self) -> None:
        client = FakeClient()
        publish(client)
        [record] = client.published
        # v2：注册的 v1 schema（additionalProperties:false）装不下
        # gate_release 载荷；放行走 work.decision.v2 的 gate_release 变体。
        assert record["kind"] == "work.decision.v2"
        assert record["payload"]["decision"] == APPROVE_DECISION
        assert record["payload"]["scope"] == SCOPE_MERGE_ONLY
        assert record["payload"]["kind"] == RELEASE_PAYLOAD_KIND

    def test_decided_by_names_the_preauth_and_the_delegation(self) -> None:
        client = FakeClient()
        publish(client)
        decided_by = client.published[0]["payload"]["decided_by"]
        assert decided_by == "supervisor-graph (依预授权 msg-preauth-1 代行；非人逐条拍板)"
        assert decided_by == decided_by_for("msg-preauth-1")

    def test_refs_point_to_both_question_and_preauth(self) -> None:
        client = FakeClient()
        publish(client)
        refs = client.published[0]["refs"]
        assert {"target_entity": "msg-q-1"} in refs
        assert {"target_entity": "msg-preauth-1"} in refs

    def test_approve_value_matches_the_dd_gate_vocabulary(self) -> None:
        # BoardGate 只认 dd_actors.GATE_APPROVE；两个常量漂移 = 放行的
        # decision 卡在 GATE_VERDICT_UNRECOGNIZED 上。
        from fleet_graph.graphs.dd_actors import GATE_APPROVE

        assert APPROVE_DECISION == GATE_APPROVE

    def test_payload_kind_can_never_be_preauth(self) -> None:
        # preauth 的签发与展期停在人闸：publisher 的 payload.kind 是固定
        # 常量，不是参数——构造性发不出一条 preauth。
        from fleet_graph.supervise.preauth import PREAUTH_PAYLOAD_KIND

        assert RELEASE_PAYLOAD_KIND != PREAUTH_PAYLOAD_KIND


class TestRefusals:
    def test_ungranted_evaluation_is_refused(self) -> None:
        client = FakeClient()
        losing = ReleaseEvaluation(
            granted=False, reasons=("preauth 已过期",), preauth_message_id="msg-preauth-1"
        )
        with pytest.raises(DecisionPublishRefused, match="三要素"):
            publish(client, evaluation=losing)
        assert client.published == []

    def test_missing_preauth_anchor_is_refused(self) -> None:
        client = FakeClient()
        anchorless = ReleaseEvaluation(granted=True, reasons=(), preauth_message_id="")
        with pytest.raises(DecisionPublishRefused, match="锚点不齐"):
            publish(client, evaluation=anchorless)
        assert client.published == []


class TestCredential:
    def test_token_comes_only_from_the_decision_env_file(self, tmp_path: Path) -> None:
        token_file = tmp_path / "decision.token"
        token_file.write_text("sekrit-decision\n")
        token = load_decision_token({DECISION_TOKEN_ENV: str(token_file)})
        assert token == "sekrit-decision"

    def test_no_env_means_refused_not_fallback(self) -> None:
        # 板凭证在场也不回退：凭证分离的全部意义就在这条不回退上。
        env = {"FLEET_GRAPH_BUS_TOKEN": "board-token", "FLEET_GRAPH_BUS_TOKEN_FILE": "/x"}
        with pytest.raises(DecisionPublishRefused, match="板凭证不可代用"):
            load_decision_token(env)

"""preauth 协议与三要素判定的钉死测试。

负例优先：本模块存在的第一理由是证明"什么放不过去"——无期限 preauth、
覆盖 main/master/production/release 的 allowlist、approve 之外的动作、
三要素任缺其一。正例只有一条窄路。
"""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.supervise.preauth import (
    HUMAN_ONLY_CATEGORIES,
    PREAUTH_ALLOWED_ACTIONS,
    PreauthError,
    evaluate_release,
    latest_preauth_for,
    parse_preauth,
    ref_in_allowlist,
)

NOW = 1_700_000_000.0
FUTURE = "2099-01-01T00:00:00Z"


def preauth_message(**payload_overrides: Any) -> dict[str, Any]:
    payload = {
        "kind": "preauth",
        "card_entity_id": "card-7",
        "allowed_actions": ["approve"],
        "target_ref_allowlist": ["refs/heads/dd/"],
        "expires_at": FUTURE,
        "decided_by": "张三（人签发）",
    }
    payload.update(payload_overrides)
    return {"message_id": "msg-preauth-1", "channel_seq": 10, "payload": payload}


class TestParsePreauth:
    def test_valid_preauth_parses(self) -> None:
        preauth = parse_preauth(preauth_message())
        assert preauth.card_entity_id == "card-7"
        assert preauth.allowed_actions == ("approve",)
        assert preauth.target_ref_allowlist == ("refs/heads/dd/",)

    def test_missing_expiry_is_refused(self) -> None:
        with pytest.raises(PreauthError, match="常开自批"):
            parse_preauth(preauth_message(expires_at=""))

    def test_naive_expiry_is_refused(self) -> None:
        with pytest.raises(PreauthError, match="时区"):
            parse_preauth(preauth_message(expires_at="2099-01-01T00:00:00"))

    def test_wrong_payload_kind_is_refused(self) -> None:
        with pytest.raises(PreauthError, match=r"payload\.kind"):
            parse_preauth(preauth_message(kind="gate_release"))

    def test_action_beyond_approve_is_refused(self) -> None:
        # 封闭枚举 HUMAN_ONLY_CATEGORIES 里的动作在协议上就发不出来。
        for action in ("reject", "deploy", "cancel", "preauth"):
            with pytest.raises(PreauthError, match="只能预授权"):
                parse_preauth(preauth_message(allowed_actions=["approve", action]))

    def test_empty_actions_refused(self) -> None:
        with pytest.raises(PreauthError, match="非空"):
            parse_preauth(preauth_message(allowed_actions=[]))

    def test_missing_card_binding_refused(self) -> None:
        with pytest.raises(PreauthError, match="card_entity_id"):
            parse_preauth(preauth_message(card_entity_id=""))


class TestProtectedAllowlist:
    """ "main"/"master"/production/release 构造性不可能被 allowlist 覆盖。"""

    @pytest.mark.parametrize(
        "prefix",
        [
            "refs/heads/main",
            "refs/heads/master",
            "refs/heads/mai",  # 前缀探进 main
            "refs/heads/",  # 覆盖一切 heads，含 main
            "refs/",
            "main",
            "m",
            "refs/heads/production/",
            "refs/heads/release/",
            "production",
        ],
    )
    def test_prefix_covering_protected_branch_is_refused(self, prefix: str) -> None:
        with pytest.raises(PreauthError):
            parse_preauth(preauth_message(target_ref_allowlist=[prefix]))

    def test_one_bad_prefix_poisons_the_whole_preauth(self) -> None:
        with pytest.raises(PreauthError):
            parse_preauth(
                preauth_message(target_ref_allowlist=["refs/heads/dd/", "refs/heads/main"])
            )

    def test_glob_characters_are_refused(self) -> None:
        with pytest.raises(PreauthError, match="通配"):
            parse_preauth(preauth_message(target_ref_allowlist=["refs/heads/dd/*"]))

    def test_integration_prefix_survives(self) -> None:
        preauth = parse_preauth(preauth_message(target_ref_allowlist=["refs/heads/dd/"]))
        assert ref_in_allowlist("refs/heads/dd/dev-abc123", preauth.target_ref_allowlist)
        assert not ref_in_allowlist("refs/heads/main", preauth.target_ref_allowlist)


class TestLatestPreauthFor:
    def test_newest_valid_preauth_wins(self) -> None:
        old = preauth_message()
        old["message_id"], old["channel_seq"] = "msg-old", 5
        new = preauth_message()
        new["message_id"], new["channel_seq"] = "msg-new", 9
        preauth, rejections = latest_preauth_for([old, new], "card-7")
        assert preauth is not None and preauth.message_id == "msg-new"
        assert rejections == []

    def test_invalid_candidates_are_rejected_with_reasons_not_errors(self) -> None:
        bad = preauth_message(expires_at="")
        preauth, rejections = latest_preauth_for([bad], "card-7")
        assert preauth is None
        assert rejections and "常开自批" in rejections[0]

    def test_other_cards_preauth_is_not_borrowed(self) -> None:
        other = preauth_message(card_entity_id="card-8")
        preauth, _ = latest_preauth_for([other], "card-7")
        assert preauth is None

    def test_non_preauth_decisions_are_ignored(self) -> None:
        ordinary = {"message_id": "m1", "channel_seq": 3, "payload": {"decision": "APPROVE"}}
        preauth, rejections = latest_preauth_for([ordinary], "card-7")
        assert preauth is None and rejections == []


class TestEvaluateRelease:
    def _evaluate(self, **overrides: Any):
        kwargs: dict[str, Any] = {
            "preauth": parse_preauth(preauth_message()),
            "action": "approve",
            "card_entity_id": "card-7",
            "question_note_id": "msg-q-1",
            "target_ref": "refs/heads/dd/dev-abc",
            "report_green": True,
            "already_decided": False,
            "now": NOW,
        }
        kwargs.update(overrides)
        return evaluate_release(**kwargs)

    def test_all_three_factors_green_grants(self) -> None:
        evaluation = self._evaluate()
        assert evaluation.granted
        assert evaluation.reasons == ()
        assert evaluation.preauth_message_id == "msg-preauth-1"
        assert evaluation.target_ref == "refs/heads/dd/dev-abc"

    def test_no_preauth_degrades(self) -> None:
        evaluation = self._evaluate(preauth=None)
        assert not evaluation.granted
        assert any("没有合法的 preauth" in r for r in evaluation.reasons)

    def test_factor1_action_not_covered_degrades(self) -> None:
        evaluation = self._evaluate(action="reject")
        assert not evaluation.granted
        assert any("allowed_actions" in r for r in evaluation.reasons)

    def test_factor1_card_mismatch_degrades(self) -> None:
        evaluation = self._evaluate(card_entity_id="card-999")
        assert not evaluation.granted
        assert any("不精确匹配" in r for r in evaluation.reasons)

    def test_factor1_expired_degrades(self) -> None:
        expired = parse_preauth(preauth_message(expires_at="2020-01-01T00:00:00Z"))
        evaluation = self._evaluate(preauth=expired)
        assert not evaluation.granted
        assert any("已过期" in r for r in evaluation.reasons)

    def test_factor2_unanchored_target_ref_degrades(self) -> None:
        evaluation = self._evaluate(target_ref="")
        assert not evaluation.granted
        assert any("无法从 git 锚定" in r for r in evaluation.reasons)

    def test_factor2_ref_outside_allowlist_degrades(self) -> None:
        evaluation = self._evaluate(target_ref="refs/heads/other/dev-abc")
        assert not evaluation.granted
        assert any("不在 allowlist" in r for r in evaluation.reasons)

    def test_factor3_missing_question_note_degrades(self) -> None:
        evaluation = self._evaluate(question_note_id="")
        assert not evaluation.granted
        assert any("question note" in r for r in evaluation.reasons)

    def test_red_or_gappy_report_degrades(self) -> None:
        evaluation = self._evaluate(report_green=False)
        assert not evaluation.granted
        assert any("全绿" in r for r in evaluation.reasons)

    def test_already_decided_degrades(self) -> None:
        evaluation = self._evaluate(already_decided=True)
        assert not evaluation.granted
        assert any("已有 decision" in r for r in evaluation.reasons)


class TestHumanOnlyEnumeration:
    def test_the_enumeration_is_closed_and_exact(self) -> None:
        # 封闭枚举写进代码，不写散文；改这个集合 = 改人闸边界，必须过 review。
        expected = frozenset(
            {
                "production_promotion",
                "deployment_authorization",
                "criteria_or_spec_change",
                "cancel_running_development",
                "preauth_issuance_or_extension",
                "reject",
            }
        )
        assert expected == HUMAN_ONLY_CATEGORIES

    def test_preauth_can_only_express_approve(self) -> None:
        # 枚举里的类别无法被 preauth 表达：v1 唯一可授权动作是 approve。
        expected = frozenset({"approve"})
        assert expected == PREAUTH_ALLOWED_ACTIONS

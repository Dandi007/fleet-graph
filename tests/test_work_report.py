"""The v1 worker turn report decoder: strict acceptance, strict rejection."""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.work_report import (
    SCHEMA_VERSION,
    ReportProtocolError,
    decode_report,
    project_control,
)


def report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "turn_id": "t-1",
        "outcome": "completed",
        "summary": "built the thing",
        "did": ["built the thing"],
        "files": [{"path": "src/a.py", "change": "created"}],
        "self_tests": [{"argv": ["uv", "run", "pytest", "-q"], "exit_code": 0}],
        "blocker": None,
    }
    base.update(overrides)
    return base


class TestDecodeAccepts:
    def test_minimal_completed_report_decodes(self) -> None:
        decoded = decode_report(report())
        assert decoded["schema_version"] == SCHEMA_VERSION
        assert decoded["outcome"] == "completed"
        assert decoded["blocker"] is None

    def test_a_json_string_report_decodes_like_a_dict(self) -> None:
        import json

        assert decode_report(json.dumps(report())) == decode_report(report())

    def test_a_fenced_json_string_report_is_mechanically_unshelled(self) -> None:
        """deepseek-v4 家族实测（2026-08-29 舰队瘫痪事故）：无视裸 JSON 指令，把
        report 包进 markdown 栅栏。栅栏零信息量，去壳属 E4b 规范化不属修补；
        去壳后仍坏的照旧 malformed。"""
        import json

        body = json.dumps(report())
        fenced = f"```json\n{body}\n```"
        assert decode_report(fenced) == decode_report(report())
        bare_fence = f"```\n{body}\n```"
        assert decode_report(bare_fence) == decode_report(report())
        with pytest.raises(ReportProtocolError):
            decode_report("```json\nnot json at all\n```")

    def test_a_report_buried_after_gateway_noise_is_extracted(self) -> None:
        """SCNet 网关实测（2026-08-29）：空 assistant 消息被替换成
        '[System: Empty message content sanitised to satisfy protocol]'，seat 把
        全部 text part 拼接后报告埋在噪音尾部。报告以 {"schema_version" 协议魔数
        自识别，尾部提取是去噪不是猜测；没有可解析报告的噪音照旧 malformed。"""
        import json

        noise = "[System: Empty message content sanitised to satisfy protocol]\n\n" * 24
        body = json.dumps(report())
        assert decode_report(noise + body) == decode_report(report())
        assert decode_report(noise + body + "\n\n") == decode_report(report())
        with pytest.raises(ReportProtocolError):
            decode_report(noise + '{"schema_version": broken')
        with pytest.raises(ReportProtocolError):
            decode_report(noise)

    def test_all_four_change_values_are_accepted(self) -> None:
        for change in ("created", "modified", "deleted", "unchanged"):
            decoded = decode_report(report(files=[{"path": "src/a.py", "change": change}]))
            assert decoded["files"][0]["change"] == change

    def test_blocked_report_with_structured_blocker_decodes(self) -> None:
        decoded = decode_report(
            report(
                outcome="blocked",
                blocker={"kind": "external", "detail": "waiting on a service"},
            )
        )
        assert decoded["blocker"] == {"kind": "external", "detail": "waiting on a service"}

    def test_failed_report_may_have_null_or_object_blocker(self) -> None:
        assert decode_report(report(outcome="failed"))["outcome"] == "failed"
        decoded = decode_report(
            report(outcome="failed", blocker={"kind": "external", "detail": "boom"})
        )
        assert decoded["blocker"]["kind"] == "external"

    def test_prose_attachment_is_retained_for_inspection(self) -> None:
        decoded = decode_report(
            report(prose_attachment={"media_type": "text/markdown", "content": "## notes"})
        )
        assert decoded["prose_attachment"]["content"] == "## notes"

    def test_projection_drops_the_attachment(self) -> None:
        decoded = decode_report(
            report(prose_attachment={"media_type": "text/plain", "content": "hi"})
        )
        assert "prose_attachment" not in project_control(decoded)
        assert project_control(decoded)["outcome"] == "completed"


class TestDecodeRejects:
    def test_not_an_object(self) -> None:
        for bad in (None, "not json", ["a", "b"], 42):
            with pytest.raises(ReportProtocolError):
                decode_report(bad)

    def test_missing_required_fields(self) -> None:
        for required in ("turn_id", "outcome", "summary", "did", "files", "self_tests", "blocker"):
            payload = report()
            del payload[required]
            with pytest.raises(ReportProtocolError) as caught:
                decode_report(payload)
            assert caught.value.kind in ("missing", "schema_invalid")

    def test_missing_schema_version_is_missing(self) -> None:
        payload = report()
        del payload["schema_version"]
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(payload)
        assert caught.value.kind == "missing"

    def test_unsupported_version_is_blanket_rejected(self) -> None:
        for version in ("fleet-graph.worker-turn-report/v2", "v1", "other"):
            with pytest.raises(ReportProtocolError) as caught:
                decode_report(report(schema_version=version))
            assert caught.value.kind == "unsupported_version"

    def test_an_unknown_top_level_field_is_rejected(self) -> None:
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(report(verdict="done"))
        assert caught.value.kind == "schema_invalid"

    @pytest.mark.parametrize("outcome", ["unknown", "done", "COMPLETED", 1, None])
    def test_bad_outcome_enum(self, outcome: Any) -> None:
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(report(outcome=outcome))
        assert caught.value.kind == "schema_invalid"

    def test_completed_with_a_non_null_blocker_is_rejected(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(blocker={"kind": "external", "detail": "x"}))

    def test_blocked_without_a_blocker_is_rejected(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(outcome="blocked", blocker=None))

    def test_blocker_requires_non_empty_kind_and_detail(self) -> None:
        for blocker in (
            {"kind": "", "detail": "d"},
            {"kind": "k", "detail": ""},
            {"kind": "k"},
            {"kind": "k", "detail": "d", "extra": 1},
        ):
            with pytest.raises(ReportProtocolError):
                decode_report(report(outcome="blocked", blocker=blocker))

    @pytest.mark.parametrize("path", ["", "   ", "/abs/path"])
    def test_files_reject_bad_paths(self, path: str) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(files=[{"path": path, "change": "created"}]))

    def test_files_reject_unknown_change_and_extra_fields(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(files=[{"path": "src/a.py", "change": "renamed"}]))
        with pytest.raises(ReportProtocolError):
            decode_report(report(files=[{"path": "src/a.py", "change": "created", "size": 1}]))

    def test_self_tests_reject_bad_argv_and_exit_code(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(self_tests=[{"argv": [], "exit_code": 0}]))
        with pytest.raises(ReportProtocolError):
            decode_report(report(self_tests=[{"argv": ["uv", 1], "exit_code": 0}]))
        for bad_code in (-1, "0", True, 1.5):
            with pytest.raises(ReportProtocolError):
                decode_report(report(self_tests=[{"argv": ["uv"], "exit_code": bad_code}]))
        with pytest.raises(ReportProtocolError):
            decode_report(report(self_tests=[{"argv": ["uv"], "exit_code": 0, "tail": "x"}]))

    def test_non_empty_bounded_strings_are_enforced(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(turn_id=""))
        with pytest.raises(ReportProtocolError):
            decode_report(report(summary="   "))
        with pytest.raises(ReportProtocolError):
            decode_report(report(turn_id="x" * 300))

    def test_attachment_rejects_unknown_media_type_and_oversize_content(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(prose_attachment={"media_type": "text/html", "content": "x"}))
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(
                report(prose_attachment={"media_type": "text/plain", "content": "x" * 200_001})
            )
        assert caught.value.kind == "schema_invalid"

    def test_did_rejects_non_strings_and_empty_items(self) -> None:
        with pytest.raises(ReportProtocolError):
            decode_report(report(did=[1]))
        with pytest.raises(ReportProtocolError):
            decode_report(report(did=[""]))

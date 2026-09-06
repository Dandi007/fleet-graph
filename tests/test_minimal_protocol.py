"""Output protocol: Stop schema table + envelope validator (DD-101)."""

from __future__ import annotations

import json

from fleet_graph.minimal.protocol import (
    ENVELOPE_KEYS,
    SCHEMA_GOAL_REVIEW,
    SCHEMA_GOAL_TURN,
    SCHEMA_IMPL,
    SCHEMA_MERGE,
    SCHEMA_REVIEW,
    SCHEMA_RUNTIME_ERROR,
    SCHEMA_SCRIBE,
    describe_schema,
    extract_protocol_object,
    validate,
)

SHA = "0" * 40


class TestHappyPaths:
    def test_goal_turn_dispatch(self) -> None:
        obj = {
            "schema": SCHEMA_GOAL_TURN,
            "stop": "dispatch",
            "summary": "派一张单",
            "dispatch": {"spec_text": "做这个"},
        }
        assert validate(obj, SCHEMA_GOAL_TURN).ok

    def test_goal_review_reject(self) -> None:
        obj = {"schema": SCHEMA_GOAL_REVIEW, "stop": "reject", "message": "改这里"}
        assert validate(obj, SCHEMA_GOAL_REVIEW).ok

    def test_impl_committed(self) -> None:
        obj = {"schema": SCHEMA_IMPL, "stop": "committed", "commit": SHA, "summary": "改了"}
        assert validate(obj, SCHEMA_IMPL).ok

    def test_review_pass(self) -> None:
        obj = {"schema": SCHEMA_REVIEW, "stop": "pass", "role": "cr", "summary": "ok"}
        assert validate(obj, SCHEMA_REVIEW).ok

    def test_merge_merged(self) -> None:
        obj = {"schema": SCHEMA_MERGE, "stop": "merged", "merged_commit": "a" * 40}
        assert validate(obj, SCHEMA_MERGE).ok

    def test_scribe_observed(self) -> None:
        obj = {
            "schema": SCHEMA_SCRIBE,
            "stop": "observed",
            "observations": [
                {
                    "kind": "progress",
                    "severity": "info",
                    "title": "一条观测",
                    "summary": "大致如此",
                    "evidence": [{"event_seq": 1}],
                }
            ],
        }
        assert validate(obj, SCHEMA_SCRIBE).ok

    def test_runtime_error(self) -> None:
        obj = {"schema": SCHEMA_RUNTIME_ERROR, "stop": "invalid_output", "detail": "坏了"}
        assert validate(obj, SCHEMA_RUNTIME_ERROR).ok


class TestEnvelope:
    def test_stop_out_of_enum(self) -> None:
        result = validate({"schema": SCHEMA_IMPL, "stop": "banana"}, SCHEMA_IMPL)
        assert not result.ok
        assert any("stop" in error for error in result.errors)

    def test_missing_schema(self) -> None:
        result = validate({"stop": "committed"}, SCHEMA_IMPL)
        assert not result.ok
        assert any("schema" in error for error in result.errors)

    def test_missing_stop(self) -> None:
        result = validate({"schema": SCHEMA_IMPL}, SCHEMA_IMPL)
        assert not result.ok
        assert any("stop" in error for error in result.errors)

    def test_schema_mismatch(self) -> None:
        result = validate({"schema": SCHEMA_REVIEW, "stop": "pass", "role": "cr"}, SCHEMA_IMPL)
        assert not result.ok
        assert any("schema" in error for error in result.errors)


class TestStopBranches:
    def test_goal_review_reject_missing_message(self) -> None:
        result = validate({"schema": SCHEMA_GOAL_REVIEW, "stop": "reject"}, SCHEMA_GOAL_REVIEW)
        assert not result.ok
        assert any("message" in error for error in result.errors)

    def test_review_fail_with_only_minor_and_note(self) -> None:
        obj = {
            "schema": SCHEMA_REVIEW,
            "stop": "fail",
            "role": "cr",
            "findings": [{"severity": "minor"}, {"severity": "note"}],
        }
        result = validate(obj, SCHEMA_REVIEW)
        assert not result.ok
        assert any("blocker, major" in error for error in result.errors)

    def test_review_fail_with_one_blocker(self) -> None:
        obj = {
            "schema": SCHEMA_REVIEW,
            "stop": "fail",
            "role": "cr",
            "findings": [{"severity": "blocker"}],
        }
        assert validate(obj, SCHEMA_REVIEW).ok

    def test_impl_committed_with_non_sha_commit(self) -> None:
        obj = {"schema": SCHEMA_IMPL, "stop": "committed", "commit": "not-a-sha", "summary": "x"}
        result = validate(obj, SCHEMA_IMPL)
        assert not result.ok
        assert any("commit" in error for error in result.errors)

    def test_scribe_empty_observations(self) -> None:
        obj = {"schema": SCHEMA_SCRIBE, "stop": "observed", "observations": []}
        assert validate(obj, SCHEMA_SCRIBE).ok

    def test_scribe_observation_without_evidence(self) -> None:
        obj = {
            "schema": SCHEMA_SCRIBE,
            "stop": "observed",
            "observations": [
                {
                    "kind": "progress",
                    "severity": "info",
                    "title": "t",
                    "summary": "s",
                    "evidence": [],
                }
            ],
        }
        result = validate(obj, SCHEMA_SCRIBE)
        assert not result.ok
        assert any("evidence" in error for error in result.errors)


class TestDescribeSchema:
    def test_envelope_keys(self) -> None:
        assert ENVELOPE_KEYS == ("schema", "stop")

    def test_impl_schema_description(self) -> None:
        desc = describe_schema(SCHEMA_IMPL)
        assert desc["schema"] == SCHEMA_IMPL
        assert desc["stops"] == ["committed", "failed"]
        assert desc["required"] == []
        assert desc["stop_fields"] == {
            "committed": ["commit", "summary"],
            "failed": ["detail"],
        }

    def test_review_schema_description(self) -> None:
        desc = describe_schema(SCHEMA_REVIEW)
        assert desc["stops"] == ["pass", "fail"]
        assert desc["required"] == ["role"]


class TestExtractProtocolObject:
    def test_picks_last_matching_prefix(self) -> None:
        text = " ".join(
            json.dumps({"schema": "impl/1", "stop": "committed", "commit": sha, "summary": summary})
            for sha, summary in (("0" * 40, "first"), ("1" * 40, "last"))
        )
        obj = extract_protocol_object(text, "impl")
        assert obj is not None
        assert obj["summary"] == "last"

    def test_prose_and_code_fence(self) -> None:
        text = (
            "some prose\n"
            "```json\n"
            '{\n  "schema": "goal.turn/1",\n  "stop": "done",\n  "summary": "x"\n}\n'
            "```\n"
            "more prose"
        )
        obj = extract_protocol_object(text, "goal.turn")
        assert obj is not None
        assert obj["stop"] == "done"

    def test_braces_and_escaped_quotes_in_strings(self) -> None:
        obj = {"schema": "impl/1", "stop": "failed", "detail": 'used {} and "quoted" ok'}
        text = f"noise before {json.dumps(obj)} noise after"
        assert extract_protocol_object(text, "impl") == obj

    def test_literal_braces_and_escaped_quotes(self) -> None:
        text = '{"schema":"impl/1","stop":"failed","detail":"used {} and \\"quote\\" ok"}'
        obj = extract_protocol_object(text, "impl")
        assert obj is not None
        assert obj["detail"] == 'used {} and "quote" ok'

    def test_unbalanced_prose_brace_does_not_hide_object(self) -> None:
        text = 'oops { unbalanced then {"schema":"impl/1","stop":"failed","detail":"x"}'
        obj = extract_protocol_object(text, "impl")
        assert obj is not None
        assert obj["stop"] == "failed"

    def test_fallback_to_last_parseable_object(self) -> None:
        obj = extract_protocol_object('no schema here: {"a": 1, "b": 2}', "impl")
        assert obj == {"a": 1, "b": 2}

    def test_none_when_no_object(self) -> None:
        assert extract_protocol_object("just prose, nothing balanced", "impl") is None

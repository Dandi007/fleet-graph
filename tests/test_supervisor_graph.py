"""The supervisor graph: seven nodes, three classifications, one narrow door.

The load-bearing cases:

- an E3 fault event runs to a receipt in the supervisor's own state root and
  writes nothing into the supervised line's directory;
- `classify` is gated on mechanical predicates -- an llm shouting "reject"
  without a reproducible failure lands in needs_human;
- `preauth_release` (R4-3) fires only on the full three-factor predicate over
  a human-issued preauth, publishes exactly one merge_only APPROVE through the
  decision publisher, and degrades to needs_human -- never to a release -- on
  any missing factor or refused publish;
- a finished event is idempotent, and a killed run re-adopts its in-flight
  audit instead of dispatching a second one (the R0/R4 contract).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.graphs.adapters import CoordinatorFault
from fleet_graph.graphs.supervisor import (
    CLASSIFY_NEEDS_HUMAN,
    CLASSIFY_PREAUTH_RELEASE,
    CLASSIFY_RECOMMEND_REJECT,
    SupervisorRunConfig,
    git_target_ref,
    render_supervisor_note,
    reproducible_failures,
    run_supervisor,
    validate_audit_verdict,
)
from fleet_graph.supervise.audit import Assertion, AuditReport
from fleet_graph.supervise.events import (
    SupervisorEventError,
    line_fault_event,
    validate_event,
)

FAKE_AUDIT = str(Path(__file__).parent / "fakes" / "fake_supervisor_audit.py")


class FakeBus:
    """The four client methods the graph touches, scripted."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.refuse_publish: Exception | None = None

    def message(self, channel: str, message_id: str) -> dict[str, Any] | None:
        for note in self.notes:
            if note["message_id"] == message_id:
                return note
        return None

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [n for n in self.notes if n["channel_seq"] > after_seq]
        head = max((n["channel_seq"] for n in self.notes), default=0)
        return selected[:limit], head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return []

    def publish(self, channel, kind, payload, idempotency_key, *, refs=None, **_kw):
        if self.refuse_publish is not None:
            raise self.refuse_publish
        record = {
            "channel": channel,
            "kind": kind,
            "payload": payload,
            "idempotency_key": idempotency_key,
            "refs": refs or [],
        }
        self.published.append(record)

        class _Result:
            message_id = f"msg_pub_{len(self.published)}"
            entity_id = message_id
            channel_seq = len(self.published)
            deduplicated = False

        return _Result()


def fake_bin(tmp_path: Path) -> str:
    wrapper = tmp_path / "agent-run"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AUDIT}" "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


def fault_line(run_root: Path, folder_id: str = "wf-testfault", run_id: str = "run-e3-1") -> None:
    line_root = run_root / folder_id
    line_root.mkdir(parents=True, exist_ok=True)
    (line_root / "terminal.json").write_text(
        json.dumps(
            {
                "folder_id": folder_id,
                "terminal": "fault",
                "rounds": 2,
                "run_id": run_id,
                "pump_fault": True,
                "reason": "unrecognised verdict 'shrug'",
                "at": "2026-08-27T10:00:00Z",
            }
        )
    )
    (line_root / "rounds.jsonl").write_text(
        '{"round": 1, "verdict": "continue"}\n{"round": 2, "verdict": "continue"}\n'
    )


def config_for(tmp_path: Path, event: dict[str, Any], **overrides: Any) -> SupervisorRunConfig:
    defaults: dict[str, Any] = {
        "event": event,
        "state_root": tmp_path / "supervisor",
        "run_root": tmp_path / "runs",
        "agent_run_bin": fake_bin(tmp_path),
        "audit_timeout_seconds": 30,
        "audit_poll_interval": 0.05,
        "publish_notes": False,
    }
    defaults.update(overrides)
    return SupervisorRunConfig(**defaults)


class TestThreadIdentity:
    """R4 generation semantics: the attempt is part of the thread identity."""

    def test_thread_id_carries_the_attempt_suffix(self) -> None:
        event = validate_event({"type": "line_fault", "key": "e3-run-1", "payload": {}})
        assert event.attempt == 1
        assert event.thread_id == "supervisor:e3-run-1:a1"

    def test_attempt_round_trips_through_the_event_json(self) -> None:
        raw = {"type": "line_fault", "key": "e3-run-1", "payload": {}, "attempt": 3}
        event = validate_event(raw)
        assert event.thread_id == "supervisor:e3-run-1:a3"
        assert validate_event(event.as_dict()) == event

    def test_run_id_derivation_differs_per_attempt(self) -> None:
        """The audit run id derives from the thread id, so a new attempt pays
        for a genuinely new run while the same attempt re-adopts."""
        from fleet_graph.executors.agent_run import derive_run_id

        a1 = validate_event({"type": "line_fault", "key": "e3-r", "attempt": 1})
        a2 = validate_event({"type": "line_fault", "key": "e3-r", "attempt": 2})
        assert derive_run_id(a1.thread_id, "audit") != derive_run_id(a2.thread_id, "audit")
        assert derive_run_id(a1.thread_id, "audit") == derive_run_id(a1.thread_id, "audit")

    @pytest.mark.parametrize("attempt", [0, -1, True, "2", 1.5])
    def test_non_positive_or_non_int_attempts_are_refused(self, attempt: Any) -> None:
        with pytest.raises(SupervisorEventError, match="attempt"):
            validate_event({"type": "line_fault", "key": "e3-run-1", "attempt": attempt})


class TestEventVocabulary:
    def test_unknown_event_type_is_refused_not_mapped(self) -> None:
        with pytest.raises(SupervisorEventError, match="vocabulary is closed"):
            validate_event({"type": "gate_pending_v0", "key": "e1-x", "payload": {}})

    def test_missing_key_is_refused(self) -> None:
        with pytest.raises(SupervisorEventError, match="non-empty"):
            validate_event({"type": "line_fault", "key": "", "payload": {}})

    def test_unsafe_key_is_refused(self) -> None:
        with pytest.raises(SupervisorEventError, match="unit-safe"):
            validate_event({"type": "line_fault", "key": "e3/../../etc", "payload": {}})


class TestGitTargetRef:
    def _report(self, **overrides: Any) -> dict[str, Any]:
        report = {
            "kind": "development",
            "target": "dev-abc",
            "assertions": [{"name": "identity_binding", "ok": True}],
        }
        report.update(overrides)
        return report

    def test_git_anchored_development_yields_the_constructive_ref(self) -> None:
        assert git_target_ref(self._report()) == "refs/heads/dd/dev-abc"

    def test_non_development_report_yields_nothing(self) -> None:
        assert git_target_ref(self._report(kind="goal_line")) == ""

    def test_broken_identity_binding_yields_nothing(self) -> None:
        report = self._report(assertions=[{"name": "identity_binding", "ok": False}])
        assert git_target_ref(report) == ""


class TestClassify:
    def test_reproducible_failure_grounds_recommend_reject(self) -> None:
        report = {
            "acceptance_results": [
                {
                    "command": ["npm", "test"],
                    "exit_code": 1,
                    "stdout_tail": "",
                    "stderr_tail": "1 test failed: retry.spec.ts",
                }
            ]
        }
        failures = reproducible_failures(report)
        assert failures == [
            {
                "argv": ["npm", "test"],
                "exit_code": 1,
                "error_excerpt": "1 test failed: retry.spec.ts",
            }
        ]

    def test_green_report_yields_no_grounds(self) -> None:
        report = {
            "assertions": [{"name": "terminal_mechanical_fields", "ok": True}],
            "acceptance_results": [{"command": ["npm", "test"], "exit_code": 0}],
        }
        assert reproducible_failures(report) == []

    def test_failed_digest_assertion_is_a_ground(self) -> None:
        report = {
            "assertions": [
                {
                    "name": "frozen_acceptance_digest",
                    "ok": False,
                    "command": "git show ...:acceptance.json | sha256sum",
                    "exit_code": 1,
                    "detail": "现算 sha256:aa vs receipt 声明 sha256:bb",
                }
            ]
        }
        failures = reproducible_failures(report)
        assert failures and failures[0]["exit_code"] == 1

    def test_non_acceptance_failures_are_not_reject_grounds(self) -> None:
        # A fault line's red terminal assertion is a fact for a human, not a
        # mechanical basis for recommending rejection of anything.
        report = {
            "assertions": [
                {
                    "name": "terminal_present",
                    "ok": False,
                    "command": "cat terminal.json",
                    "exit_code": 1,
                    "detail": "terminal.json 不存在",
                }
            ]
        }
        assert reproducible_failures(report) == []


class TestAuditVerdictValidation:
    def _verdict(self, **overrides: Any) -> dict[str, Any]:
        verdict = {
            "recommendation": "hold",
            "summary": "looks fine",
            "evidence": [{"claim": "x", "command": "cat y", "output_excerpt": "z"}],
        }
        verdict.update(overrides)
        return {"status": "completed", "verdict": verdict}

    def test_valid_verdict_passes(self) -> None:
        assert validate_audit_verdict(self._verdict())["verdict"]["recommendation"] == "hold"

    def test_missing_verdict_object_faults(self) -> None:
        with pytest.raises(CoordinatorFault, match="no verdict"):
            validate_audit_verdict({"status": "completed"})

    def test_unknown_recommendation_faults(self) -> None:
        with pytest.raises(CoordinatorFault, match="recommendation"):
            validate_audit_verdict(self._verdict(recommendation="merge_it"))

    def test_evidence_without_command_faults(self) -> None:
        broken = self._verdict(evidence=[{"claim": "x", "output_excerpt": "z"}])
        with pytest.raises(CoordinatorFault, match="no claim without reproduction"):
            validate_audit_verdict(broken)

    def test_empty_evidence_faults(self) -> None:
        with pytest.raises(CoordinatorFault, match="no claim without reproduction"):
            validate_audit_verdict(self._verdict(evidence=[]))


class TestRejectNoteFormat:
    def test_reject_note_carries_argv_exit_code_and_error_verbatim(self) -> None:
        event = line_fault_event("wf-x", "run-1")
        failures = [
            {
                "argv": ["uv", "run", "pytest", "tests/test_x.py"],
                "exit_code": 2,
                "error_excerpt": "ImportError: cannot import name 'frobnicate'",
            }
        ]
        note = render_supervisor_note(event, {}, {}, CLASSIFY_RECOMMEND_REJECT, failures)
        assert "uv run pytest tests/test_x.py" in note
        assert "exit 2" in note
        assert "ImportError: cannot import name 'frobnicate'" in note

    def test_reject_without_failures_is_refused(self) -> None:
        event = line_fault_event("wf-x", "run-1")
        with pytest.raises(RuntimeError, match="reproducible failure"):
            render_supervisor_note(event, {}, {}, CLASSIFY_RECOMMEND_REJECT, [])


class TestFaultEventEndToEnd:
    def test_e3_runs_to_receipt_and_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        fault_line(tmp_path / "runs")
        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        result = run_supervisor(config_for(tmp_path, event))

        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["event"]["type"] == "line_fault"
        assert receipt["report"]["kind"] == "goal_line"
        names = [a["name"] for a in receipt["report"]["assertions"]]
        assert "terminal_mechanical_fields" in names
        assert receipt["audit_verdict"]["verdict"]["recommendation"] == "hold"
        # Degraded act: no bus, so the note stayed local and says so.
        assert receipt["act_result"]["published"] is False

    def test_supervisor_writes_nothing_into_the_supervised_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§38e: the audit lands in the supervisor's root, full stop."""
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        run_root = tmp_path / "runs"
        fault_line(run_root)
        line_dir = run_root / "wf-testfault"
        before = {p: p.stat().st_mtime_ns for p in line_dir.rglob("*")}

        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        result = run_supervisor(config_for(tmp_path, event))

        after = {p: p.stat().st_mtime_ns for p in line_dir.rglob("*")}
        assert before == after
        assert Path(result["receipt_path"]).is_relative_to(tmp_path / "supervisor")

    def test_llm_reject_without_mechanical_ground_stays_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "reject")
        fault_line(tmp_path / "runs")
        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        result = run_supervisor(config_for(tmp_path, event))
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        # The llm's opinion is recorded as advice, and only as advice.
        assert receipt["audit_verdict"]["verdict"]["recommendation"] == "reject"

    def test_failed_audit_run_degrades_to_needs_human_with_fact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "fail")
        fault_line(tmp_path / "runs")
        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        result = run_supervisor(config_for(tmp_path, event))
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert "fault" in receipt["audit_verdict"]

    def test_malformed_audit_answer_is_a_recorded_fault_not_a_guess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "malformed")
        fault_line(tmp_path / "runs")
        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        result = run_supervisor(config_for(tmp_path, event))
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert "no verdict" in receipt["audit_verdict"]["fault"]


class TestEvidenceIncompleteBypass:
    def test_missing_terminal_skips_the_llm_and_reports_facts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        (tmp_path / "runs").mkdir()
        # No terminal.json at all: the report has a red assertion, which still
        # counts as evidence -- but a completely unresolvable event must skip
        # the llm. Use an event that resolves to nothing.
        event = {
            "type": "board_question",
            "key": "e1-msg_unresolvable",
            "payload": {"question_note_id": "msg_unresolvable", "card_entity_id": "dd-card-1"},
        }
        config = config_for(tmp_path, event)
        result = run_supervisor(config)
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["report"]["kind"] == "unresolved"
        assert receipt["classification"] == CLASSIFY_NEEDS_HUMAN
        # The llm was never dispatched: no agent-run session root exists.
        assert not (tmp_path / "supervisor" / "agent-runs").exists()
        assert receipt["report"]["gaps"]


class TestActPublishing:
    def test_note_published_with_refs_when_card_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        fault_line(tmp_path / "runs")
        bus = FakeBus()
        event = {
            "type": "line_fault",
            "key": "e3-run-e3-1",
            "payload": {
                "folder_id": "wf-testfault",
                "run_id": "run-e3-1",
                "card_entity_id": "card-7",
            },
        }
        result = run_supervisor(config_for(tmp_path, event, publish_notes=True, bus=bus))
        assert result["act_result"]["published"] is True
        [record] = bus.published
        assert record["payload"]["note_type"] == "evidence"
        assert {"target_entity": "card-7"} in record["refs"]
        assert "work.decision" not in record["kind"]

    def test_publish_refusal_degrades_with_the_error_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The known 422 gap (goal line has no board card) must not fault the turn."""
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        fault_line(tmp_path / "runs")
        bus = FakeBus()
        bus.refuse_publish = RuntimeError("HTTP 422: DERIVATION_ERROR ref target missing")
        event = {
            "type": "line_fault",
            "key": "e3-run-e3-1",
            "payload": {"folder_id": "wf-testfault", "run_id": "run-e3-1"},
        }
        result = run_supervisor(config_for(tmp_path, event, publish_notes=True, bus=bus))
        act = result["act_result"]
        assert act["published"] is False
        assert "DERIVATION_ERROR" in act["degraded"]
        assert Path(result["receipt_path"]).exists()


def board_with_gate(preauth_payload: dict[str, Any] | None = None) -> FakeBus:
    """A board holding one open gate question on card-7, optionally preauthed."""
    bus = FakeBus()
    bus.notes = [
        {
            "message_id": "msg-q-1",
            "channel_seq": 1,
            "kind": "work.note.v1",
            "payload": {"note_type": "question", "card_entity_id": "card-7", "note": "放行 merge?"},
        },
        {
            "message_id": "msg-card-1",
            "channel_seq": 2,
            "kind": "work.card.v1",
            "entity_id": "card-7",
            "payload": {"development_id": "dev-abc"},
        },
    ]
    if preauth_payload is not None:
        bus.notes.append(
            {
                "message_id": "msg-preauth-1",
                "channel_seq": 3,
                "kind": "work.decision.v2",
                "payload": preauth_payload,
            }
        )
    return bus


def valid_preauth_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "kind": "preauth",
        "card_entity_id": "card-7",
        "allowed_actions": ["approve"],
        "target_ref_allowlist": ["refs/heads/dd/"],
        "expires_at": "2099-01-01T00:00:00Z",
        "decided_by": "张三（人签发）",
    }
    payload.update(overrides)
    return payload


def green_dev_report() -> AuditReport:
    report = AuditReport(target="dev-abc", kind="development")
    for name in (
        "evidence_present",
        "verified_bit",
        "receipt_chain_linked",
        "identity_binding",
        "target_base_recomputed",
        "acceptance_rerun",
    ):
        report.record(Assertion(name=name, ok=True, command="fake", exit_code=0, detail="green"))
    report.acceptance_results.append(
        {"command": ["true"], "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
    )
    return report


GATE_EVENT = {
    "type": "board_question",
    "key": "e1-msg-q-1",
    "payload": {"question_note_id": "msg-q-1", "card_entity_id": "card-7"},
}


class TestPreauthReleaseEndToEnd:
    """R4-3 DoD: three factors green -> exactly one merge_only APPROVE through
    the publisher; anything less -> needs_human, and the decision client is
    never touched."""

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        bus: FakeBus,
        decision_client: Any,
        report: AuditReport | None = None,
    ) -> dict[str, Any]:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        monkeypatch.setattr(
            "fleet_graph.graphs.supervisor.audit_development",
            lambda development_id, *, engine, repo: report or green_dev_report(),
        )
        config = config_for(
            tmp_path,
            dict(GATE_EVENT),
            publish_notes=True,
            bus=bus,
            repo=tmp_path,
            decision_client=decision_client,
        )
        return run_supervisor(config)

    def test_three_green_factors_release_one_merge_only_approve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = board_with_gate(valid_preauth_payload())
        decision_client = FakeBus()
        result = self._run(tmp_path, monkeypatch, bus=bus, decision_client=decision_client)

        assert result["classification"] == CLASSIFY_PREAUTH_RELEASE
        act = result["act_result"]
        assert act["decision_published"] is True

        [decision] = decision_client.published
        assert decision["kind"] == "work.decision.v2"
        assert decision["payload"]["decision"] == "APPROVE"
        assert decision["payload"]["scope"] == "merge_only"
        assert decision["payload"]["target_ref"] == "refs/heads/dd/dev-abc"
        assert "依预授权 msg-preauth-1 代行" in decision["payload"]["decided_by"]
        assert {"target_entity": "msg-q-1"} in decision["refs"]
        assert {"target_entity": "msg-preauth-1"} in decision["refs"]

        # 板 client 只发 evidence note，决策只经独立 client（凭证分离的图侧）。
        assert all(not r["kind"].startswith("work.decision") for r in bus.published)
        assert any(r["payload"].get("note_type") == "evidence" for r in bus.published)

        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["preauth_evaluation"]["granted"] is True
        assert receipt["preauth_evaluation"]["preauth_message_id"] == "msg-preauth-1"

    def test_no_preauth_on_board_stays_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = board_with_gate(None)
        decision_client = FakeBus()
        result = self._run(tmp_path, monkeypatch, bus=bus, decision_client=decision_client)
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        assert decision_client.published == []
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert any("preauth" in r for r in receipt["preauth_evaluation"]["reasons"])

    def test_llm_approve_without_preauth_is_not_a_release(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An llm that says "approve" moves nothing: the predicate never reads it."""
        bus = board_with_gate(None)
        decision_client = FakeBus()
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "approve")
        monkeypatch.setattr(
            "fleet_graph.graphs.supervisor.audit_development",
            lambda development_id, *, engine, repo: green_dev_report(),
        )
        config = config_for(
            tmp_path,
            dict(GATE_EVENT),
            publish_notes=True,
            bus=bus,
            repo=tmp_path,
            decision_client=decision_client,
        )
        result = run_supervisor(config)
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        assert decision_client.published == []
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["audit_verdict"]["verdict"]["recommendation"] == "approve"

    def test_expired_preauth_stays_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = board_with_gate(valid_preauth_payload(expires_at="2020-01-01T00:00:00Z"))
        decision_client = FakeBus()
        result = self._run(tmp_path, monkeypatch, bus=bus, decision_client=decision_client)
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        assert decision_client.published == []

    def test_ref_outside_allowlist_stays_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = board_with_gate(valid_preauth_payload(target_ref_allowlist=["refs/heads/other/"]))
        decision_client = FakeBus()
        result = self._run(tmp_path, monkeypatch, bus=bus, decision_client=decision_client)
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        assert decision_client.published == []

    def test_red_report_never_releases_even_with_valid_preauth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = green_dev_report()
        report.record(
            Assertion(
                name="target_base_is_ancestor",
                ok=False,
                command="git merge-base --is-ancestor",
                exit_code=1,
                detail="not an ancestor",
            )
        )
        bus = board_with_gate(valid_preauth_payload())
        decision_client = FakeBus()
        result = self._run(
            tmp_path, monkeypatch, bus=bus, decision_client=decision_client, report=report
        )
        assert result["classification"] == CLASSIFY_NEEDS_HUMAN
        assert decision_client.published == []

    def test_refused_decision_publish_degrades_and_keeps_the_gate_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = board_with_gate(valid_preauth_payload())
        decision_client = FakeBus()
        decision_client.refuse_publish = RuntimeError("HTTP 503: bus down")
        result = self._run(tmp_path, monkeypatch, bus=bus, decision_client=decision_client)
        act = result["act_result"]
        assert act["decision_published"] is False
        assert "503" in act["decision_degraded"]
        # 板上没出现 decision，question 仍开着——失败模式是 needs_human，
        # 不是静默放行。
        assert all(not r["kind"].startswith("work.decision") for r in bus.published)
        assert Path(result["receipt_path"]).exists()


def dd_record_root(tmp_path: Path, development_id: str = "dev-abc", **overrides: Any) -> Path:
    """A dd admission root holding one record.json whose repo_path exists."""
    repo = tmp_path / "clone"
    repo.mkdir(exist_ok=True)
    dd_root = tmp_path / "dd"
    dev_dir = dd_root / development_id
    dev_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"development_id": development_id, "repo_path": str(repo)}
    record.update(overrides)
    (dev_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return dd_root


class TestDevelopmentResolution:
    """E1 without --repo: card head development_id -> dd record -> repo_path.

    The mechanical chain only -- the dev id lives structurally in the
    `work.card.v1` head payload (the dd engine wrote it there), never parsed
    out of the question note's prose. Every failed step is a recorded gap and
    a needs_human, not an error."""

    def test_repo_resolves_from_card_head_and_admission_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fleet_graph.supervise.audit import GraphEngineSource

        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        dd_root = dd_record_root(tmp_path)
        seen: dict[str, Any] = {}

        def fake_audit(development_id: str, *, engine: Any, repo: Path) -> Any:
            seen["development_id"] = development_id
            seen["engine"] = engine
            seen["repo"] = repo
            return green_dev_report()

        monkeypatch.setattr("fleet_graph.graphs.supervisor.audit_development", fake_audit)
        bus = board_with_gate(None)
        result = run_supervisor(config_for(tmp_path, dict(GATE_EVENT), bus=bus, dd_root=dd_root))

        assert seen["development_id"] == "dev-abc"
        assert seen["repo"] == tmp_path / "clone"
        # Record on disk -> the in-process engine, same rule as `supervise audit`.
        assert isinstance(seen["engine"], GraphEngineSource)
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["report"]["kind"] == "development"
        assert receipt["report"]["assertions"]

    def test_card_head_without_development_id_gaps_to_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        bus = board_with_gate(None)
        for note in bus.notes:
            if note.get("kind") == "work.card.v1":
                note["payload"] = {"status": "gate"}  # no development_id, no wf-
        result = run_supervisor(
            config_for(tmp_path, dict(GATE_EVENT), bus=bus, dd_root=tmp_path / "dd")
        )

        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["classification"] == CLASSIFY_NEEDS_HUMAN
        assert receipt["report"]["kind"] == "unresolved"
        assert any("未解析到 wf- 目标也无可审 development" in g for g in receipt["report"]["gaps"])

    def test_missing_record_json_gaps_to_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        bus = board_with_gate(None)  # card head does carry development_id dev-abc
        result = run_supervisor(
            config_for(tmp_path, dict(GATE_EVENT), bus=bus, dd_root=tmp_path / "dd-empty")
        )

        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["classification"] == CLASSIFY_NEEDS_HUMAN
        assert receipt["report"]["kind"] == "unresolved"
        gaps = receipt["report"]["gaps"]
        assert any("dd record 不可读" in g for g in gaps)
        assert any("dev-abc 已解析但无可用 repo" in g for g in gaps)

    def test_record_without_repo_path_gaps_to_needs_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        dd_root = dd_record_root(tmp_path, repo_path="")
        bus = board_with_gate(None)
        result = run_supervisor(config_for(tmp_path, dict(GATE_EVENT), bus=bus, dd_root=dd_root))

        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["classification"] == CLASSIFY_NEEDS_HUMAN
        assert any("缺 repo_path 字段" in g for g in receipt["report"]["gaps"])

    def test_e1_gate_end_to_end_reaches_the_fourth_gate_without_repo_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production gap this line fixes: an E1 over a dd gate, no --repo
        configured, must still produce a development audit with assertions --
        and with a valid preauth the fourth gate is finally reachable."""
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        monkeypatch.setattr(
            "fleet_graph.graphs.supervisor.audit_development",
            lambda development_id, *, engine, repo: green_dev_report(),
        )
        dd_root = dd_record_root(tmp_path)
        bus = board_with_gate(valid_preauth_payload())
        decision_client = FakeBus()
        result = run_supervisor(
            config_for(
                tmp_path,
                dict(GATE_EVENT),
                publish_notes=True,
                bus=bus,
                dd_root=dd_root,
                decision_client=decision_client,
            )
        )

        assert result["classification"] == CLASSIFY_PREAUTH_RELEASE
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["report"]["kind"] == "development"
        assert receipt["report"]["assertions"]
        [decision] = decision_client.published
        assert decision["payload"]["decision"] == "APPROVE"
        assert decision["payload"]["target_ref"] == "refs/heads/dd/dev-abc"


class TestIdempotency:
    def test_finished_event_is_a_no_op_on_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        fault_line(tmp_path / "runs")
        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        config = config_for(tmp_path, event)
        first = run_supervisor(config)
        second = run_supervisor(config_for(tmp_path, event, agent_run_bin=config.agent_run_bin))

        assert second["resumed"] == "already_complete"
        assert second["receipt_path"] == first["receipt_path"]
        # Exactly one audit dispatch, ever.
        ledgers = list((tmp_path / "supervisor" / "agent-runs").rglob("dispatch.log"))
        assert len(ledgers) == 1
        assert len(ledgers[0].read_text().splitlines()) == 1

    def test_a_new_attempt_is_a_fresh_run_not_already_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production re-run path: same event key, next attempt. The new
        thread starts clean (no already_complete), dispatches its own audit
        run, and the receipt is overwritten in place -- one file per key."""
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        fault_line(tmp_path / "runs")
        base = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        first = run_supervisor(config_for(tmp_path, {**base, "attempt": 1}))
        assert "resumed" not in first
        config = config_for(tmp_path, {**base, "attempt": 2})
        second = run_supervisor(config)

        assert "resumed" not in second, "attempt 2 must not resolve to attempt 1's thread"
        assert second["thread_id"] == "supervisor:e3-run-e3-1:a2"
        assert first["thread_id"] == "supervisor:e3-run-e3-1:a1"
        # One receipt per key, overwritten -- not one per attempt.
        assert second["receipt_path"] == first["receipt_path"]
        assert json.loads(Path(second["receipt_path"]).read_text())["thread_id"].endswith(":a2")
        # Two genuinely separate audit dispatches (per-attempt run ids).
        ledgers = list((tmp_path / "supervisor" / "agent-runs").rglob("dispatch.log"))
        assert sum(len(ledger.read_text().splitlines()) for ledger in ledgers) == 2

        # And attempt 2 re-run stays idempotent within its own generation.
        third = run_supervisor(config_for(tmp_path, {**base, "attempt": 2}))
        assert third["resumed"] == "already_complete"


class TestKillRestartReAdopt:
    def test_killed_supervisor_re_adopts_its_audit_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4-2 DoD: kill mid-audit, restart, and the in-flight run is adopted
        -- the dispatch ledger stays at one line."""
        fault_line(tmp_path / "runs")
        event = line_fault_event("wf-testfault", "run-e3-1").as_dict()
        bin_path = fake_bin(tmp_path)
        state_root = tmp_path / "supervisor"

        script = (
            "import json, pathlib, sys\n"
            "from fleet_graph.graphs.supervisor import SupervisorRunConfig, run_supervisor\n"
            f"config = SupervisorRunConfig(event=json.loads({json.dumps(json.dumps(event))!s}),\n"
            f"    state_root=pathlib.Path({str(state_root)!r}),\n"
            f"    run_root=pathlib.Path({str(tmp_path / 'runs')!r}),\n"
            f"    agent_run_bin={bin_path!r},\n"
            "    audit_timeout_seconds=60, audit_poll_interval=0.05, publish_notes=False)\n"
            "run_supervisor(config)\n"
        )
        env = dict(os.environ)
        env["FAKE_AUDIT_BEHAVIOR"] = "sleep"
        env["FAKE_AUDIT_SLEEP"] = "3"
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            cwd=str(Path(__file__).parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def ledger_lines() -> int:
            total = 0
            for ledger in (state_root / "agent-runs").rglob("dispatch.log"):
                total += len(ledger.read_text().splitlines())
            return total

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ledger_lines() == 0:
            time.sleep(0.05)
        assert ledger_lines() == 1, "the first process never dispatched the audit"

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

        # Restart in-process: same event, same state root. The audit fake is
        # still sleeping (detached, survived the kill); the restarted graph
        # must adopt it rather than dispatch again.
        monkeypatch.setenv("FAKE_AUDIT_BEHAVIOR", "hold")
        result = run_supervisor(
            SupervisorRunConfig(
                event=event,
                state_root=state_root,
                run_root=tmp_path / "runs",
                agent_run_bin=bin_path,
                audit_timeout_seconds=60,
                audit_poll_interval=0.05,
                publish_notes=False,
            )
        )
        assert result["receipt_path"]
        assert ledger_lines() == 1, "restart double-dispatched the audit run"


class TestE6E7Dispatch:
    """M4: run_supervisor 把 E6/E7 分派到各自处置反应器，不进入审计图。"""

    class FakeE6Ops:
        def resolve_line_unit(self, folder_id: str, run_root: Path) -> dict[str, Any]:
            return {"ok": True, "unit": f"fleet-graph-line-{folder_id}-g1", "source": "list-units"}

        def is_active(self, unit_name: str) -> bool:
            return False

        def stop_unit(self, unit_name: str) -> int:
            return 0

        def line_heartbeat_age_s(self, folder_id: str) -> float | None:
            return None

    class FakeE7Ops:
        def resolve_folder_id(self, bus: Any, source_message_id: str) -> str:
            return "wf-a"

        def goal_revision(self, folder_id: str) -> str:
            return "rev-before"

        def append_delivery_fail_block(self, folder_id: str, block: str) -> dict[str, Any]:
            return {
                "before_revision": "rev-before",
                "after_revision": "rev-after",
                "revision_changed": True,
                "readback_present": True,
                "marker": "## E7 送达失败（监督面直写）",
            }

        def read_goal(self, folder_id: str) -> str:
            return "## E7 送达失败（监督面直写）\n"

    def test_e6_dispatches_to_the_stop_reactor(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.e6_stop import OUTCOME_STOPPED
        from fleet_graph.supervise.events import heartbeat_stale_event

        event = heartbeat_stale_event(
            folder_id="wf-a", heartbeat_age_s=600.0, round=3, phase="coordinator"
        ).as_dict()
        result = run_supervisor(
            SupervisorRunConfig(
                event=event,
                state_root=tmp_path / "supervisor",
                run_root=tmp_path / "runs",
                publish_notes=False,
                e6_ops=self.FakeE6Ops(),
            )
        )
        assert result["outcome"] == OUTCOME_STOPPED
        assert result["receipt_path"]

    def test_e7_dispatches_to_the_write_reactor(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.e7_allowlist import E7WriteAllowlist
        from fleet_graph.supervise.e7_write import OUTCOME_DELIVERED
        from fleet_graph.supervise.events import decision_swallowed_event

        event = decision_swallowed_event(source_message_id="msg_sw", reason="noop").as_dict()
        result = run_supervisor(
            SupervisorRunConfig(
                event=event,
                state_root=tmp_path / "supervisor",
                run_root=tmp_path / "runs",
                publish_notes=False,
                e7_ops=self.FakeE7Ops(),
                e7_allowlist=E7WriteAllowlist(folder_ids=("wf-a",)),
            )
        )
        assert result["outcome"] == OUTCOME_DELIVERED
        assert result["receipt_path"]

    def test_e7_outside_allowlist_refuses_without_write(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.e7_allowlist import E7WriteAllowlist
        from fleet_graph.supervise.e7_write import OUTCOME_REFUSED
        from fleet_graph.supervise.events import decision_swallowed_event

        event = decision_swallowed_event(source_message_id="msg_sw", reason="noop").as_dict()
        result = run_supervisor(
            SupervisorRunConfig(
                event=event,
                state_root=tmp_path / "supervisor",
                run_root=tmp_path / "runs",
                publish_notes=False,
                e7_ops=self.FakeE7Ops(),
                e7_allowlist=E7WriteAllowlist.default(),
            )
        )
        assert result["outcome"] == OUTCOME_REFUSED
        assert result["receipt_path"]

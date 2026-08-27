"""The supervisor graph: seven nodes, two classifications, no verdicts.

The load-bearing cases:

- an E3 fault event runs to a receipt in the supervisor's own state root and
  writes nothing into the supervised line's directory;
- `classify` is gated on mechanical predicates -- an llm shouting "reject"
  without a reproducible failure lands in needs_human;
- `preauth_release` raises NotImplementedError (the R4-3 stub is a stub);
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
    CLASSIFY_RECOMMEND_REJECT,
    SupervisorRunConfig,
    preauth_release,
    render_supervisor_note,
    reproducible_failures,
    run_supervisor,
    validate_audit_verdict,
)
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


class TestPreauthStub:
    def test_preauth_release_is_not_implemented_in_r4_2(self) -> None:
        with pytest.raises(NotImplementedError, match="R4-3"):
            preauth_release({}, {}, {})


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

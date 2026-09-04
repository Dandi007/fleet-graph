"""Spec ⑫-b: the two mechanical-audit note renders name their destination.

缺陷⑫ 收尾 (wf-8d9737, 监督面 2026-09-04 10:15 指令 c 项): the board-facing
renders must route by the subject's form into arbiter/a2.py's closed
ESCALATION_TARGETS vocabulary (``dispatching_line`` / ``supervisor_escalation``
/ ``needs_evidence``) and must never broadcast the legacy human-decides phrase
again. Pinned here:

- full-repo grep: the legacy phrase has zero product-surface occurrences,
  against an explicit whitelist whose every entry is asserted to exist and to
  carry a reason (spec 指令 c 项);
- both render paths give named-target positives per subject form: the
  supervisor note (graphs/supervisor.py) for every event shape, the supervise
  evidence note (supervise/audit.py) for development / goal_line / gaps;
- negative regression: no render ever puts the legacy phrase or
  ``needs_human`` into the named-destination slot (``去向: ...``) -- the note
  may still say it emits no ``work.decision.v1``, and the classification
  label (unchanged classify semantics) is not a destination;
- the routing tables cover all three targets, and both modules reuse the
  shared a2.py constants by identity -- never a second copy of the vocabulary.

Same-family enumeration (spec 指令 b 项, re-enumerated at implementation time
over the whole repo, src+tests+config+scripts):

- ``src/fleet_graph/graphs/supervisor.py`` non-preauth header -- 改: named
  destination routed by subject form (this commit);
- ``src/fleet_graph/supervise/audit.py`` ``render_note`` header -- 改: named
  destination routed by report form (this commit);
- ``src/fleet_graph/arbiter/a2.py`` legacy ``needs_human: true`` back-compat
  parsing -- 不改: parsing/routing of legacy payloads, not a render surface
  (spec excludes it explicitly);
- ``CLASSIFY_NEEDS_HUMAN`` classification vocabulary (supervisor.py,
  preauth.py, cli.py help text) -- 不改: classify semantics are out of bounds
  (spec 边界 d 项); the label stays, only the rendered destination changed;
- no other render surface emits a destination at all (grep-verified: the
  legacy phrase had exactly the two census hits; ``needs_human`` appears in
  no other rendered output slot).
"""

from __future__ import annotations

import re
from pathlib import Path

import fleet_graph.arbiter.a2 as a2
import fleet_graph.graphs.supervisor as supervisor_mod
import fleet_graph.supervise.audit as audit_mod
from fleet_graph.graphs.supervisor import (
    CLASSIFY_NEEDS_HUMAN,
    CLASSIFY_PREAUTH_RELEASE,
    CLASSIFY_RECOMMEND_REJECT,
    render_supervisor_note,
)
from fleet_graph.supervise.audit import Assertion, AuditReport, render_note
from fleet_graph.supervise.events import (
    EVENT_TYPES,
    approved_unharvested_event,
    blocked_decision_event,
    board_question_event,
    cap_breaker_event,
    decision_swallowed_event,
    enrollment_pending_event,
    heartbeat_stale_event,
    line_fault_event,
)

#: The exact phrase spec ⑫-b bans from every render surface.
LEGACY_PHRASE = "人仍拍板"

#: Destination slot shared by both render paths; its value must always be a
#: member of the shared vocabulary.
DESTINATION_RE = re.compile(r"去向: (\S+?)[，。）]")

#: Paths where the legacy phrase may still exist, each with its reason. The
#: grep test asserts this list is exact: every repo hit is whitelisted, and
#: every whitelisted path really contains the phrase.
LEGACY_PHRASE_WHITELIST = {
    ".dev-dispatch/spec/approved.md": ("spec 原文引用旧措辞（禁令本身）——控制器保留文档，非渲染面"),
    "tests/test_d12b_audit_note_targets.py": ("本测试以旧措辞构造阴性断言——测试文本，非渲染面"),
}

#: Directories that are not repo text surfaces (caches, dependency installs).
GREP_SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}


def _named_destination(note: str) -> str:
    """The single value the note puts in its named-destination slot."""
    matches = DESTINATION_RE.findall(note)
    assert len(matches) == 1, f"expected exactly one 去向 slot, got {matches!r} in:\n{note}"
    return matches[0]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# --- spec 指令 c 项: the full-repo legacy-phrase grep -----------------------


class TestLegacyPhraseGoneFromRepo:
    def test_full_repo_grep_legacy_phrase_is_whitelisted_only(self) -> None:
        needle = LEGACY_PHRASE.encode("utf-8")
        root = _repo_root()
        hits: dict[str, int] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part in GREP_SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            data = path.read_bytes()
            count = data.count(needle)
            if count:
                hits[rel] = count

        assert set(hits) == set(LEGACY_PHRASE_WHITELIST), (
            f"legacy phrase hits outside the explicit whitelist: "
            f"{sorted(set(hits) - set(LEGACY_PHRASE_WHITELIST))}; full set: {hits}"
        )
        for rel, reason in LEGACY_PHRASE_WHITELIST.items():
            assert reason.strip(), f"whitelist entry {rel} must document its reason"
            whitelisted = root / rel
            assert whitelisted.is_file(), f"whitelisted path {rel} does not exist"
            assert needle in whitelisted.read_bytes(), (
                f"whitelisted path {rel} no longer contains the phrase; "
                "shrink the whitelist instead of keeping dead entries"
            )

    def test_product_surface_has_zero_legacy_phrase(self) -> None:
        needle = LEGACY_PHRASE.encode("utf-8")
        root = _repo_root()
        for path in sorted((root / "src").rglob("*.py")):
            assert needle not in path.read_bytes(), f"legacy phrase in {path}"


# --- spec 指令 a/c 项: the supervisor note names its destination ------------


class TestSupervisorNoteNamedTargets:
    def test_dd_unit_at_gate_routes_to_dispatching_line(self) -> None:
        event = blocked_decision_event("wf-x", "run-1")
        note = render_supervisor_note(event, {}, {}, CLASSIFY_NEEDS_HUMAN, [])
        assert _named_destination(note) == "dispatching_line"
        assert "本单不发 work.decision.v1" in note

    def test_dd_unit_past_acceptance_routes_to_dispatching_line(self) -> None:
        event = approved_unharvested_event("dev-x", "abc123", "implement")
        note = render_supervisor_note(event, {}, {}, CLASSIFY_NEEDS_HUMAN, [])
        assert _named_destination(note) == "dispatching_line"

    def test_direction_ask_routes_to_supervisor_escalation(self) -> None:
        event = board_question_event("note-1", "card-1")
        note = render_supervisor_note(event, {}, {}, CLASSIFY_NEEDS_HUMAN, [])
        assert _named_destination(note) == "supervisor_escalation"

    def test_production_fault_routes_to_supervisor_escalation(self) -> None:
        for event in (
            line_fault_event("wf-x", "run-1"),
            cap_breaker_event(7, "TOTAL_CAP_REACHED", ["wf-x"]),
            decision_swallowed_event("msg-1", "bus loss"),
        ):
            note = render_supervisor_note(event, {}, {}, CLASSIFY_NEEDS_HUMAN, [])
            assert _named_destination(note) == "supervisor_escalation", event.type

    def test_thin_signal_routes_to_needs_evidence(self) -> None:
        for event in (
            heartbeat_stale_event("wf-x", 999.0, 2, "audit"),
            enrollment_pending_event("wf-x"),
        ):
            note = render_supervisor_note(event, {}, {}, CLASSIFY_NEEDS_HUMAN, [])
            assert _named_destination(note) == "needs_evidence", event.type

    def test_mechanical_reject_outranks_subject_form_to_dispatching_line(self) -> None:
        event = board_question_event("note-1", "card-1")
        failures = [
            {
                "argv": ["uv", "run", "pytest", "-q"],
                "exit_code": 2,
                "error_excerpt": "ImportError: cannot import name 'frobnicate'",
            }
        ]
        note = render_supervisor_note(event, {}, {}, CLASSIFY_RECOMMEND_REJECT, failures)
        assert _named_destination(note) == "dispatching_line"
        assert "uv run pytest -q" in note

    def test_preauth_header_keeps_its_named_preauth_form(self) -> None:
        event = board_question_event("note-1", "card-1")
        preauth = {"preauth_message_id": "pm-1"}
        note = render_supervisor_note(event, {}, {}, CLASSIFY_PREAUTH_RELEASE, [], preauth=preauth)
        assert "代行放行" in note
        assert "merge_only" in note
        assert "去向" not in note

    def test_no_subject_form_ever_names_a_human_as_destination(self) -> None:
        for event_type in sorted(EVENT_TYPES):
            event = supervisor_mod.SupervisorEvent(type=event_type, key="k-1")
            for classification in (CLASSIFY_NEEDS_HUMAN, CLASSIFY_RECOMMEND_REJECT):
                failures = (
                    []
                    if classification == CLASSIFY_NEEDS_HUMAN
                    else [
                        {
                            "argv": ["true"],
                            "exit_code": 1,
                            "error_excerpt": "x",
                        }
                    ]
                )
                note = render_supervisor_note(event, {}, {}, classification, failures)
                assert LEGACY_PHRASE not in note, (event_type, classification)
                destination = _named_destination(note)
                assert destination in a2.ESCALATION_TARGETS, (event_type, classification)
                assert destination != "needs_human", (event_type, classification)


# --- spec 指令 a/c 项: the supervise evidence note names its destination ----


class TestAuditNoteNamedTargets:
    def test_development_audit_routes_to_dispatching_line(self) -> None:
        report = AuditReport(target="dev_x", kind="development")
        note = render_note(report)
        assert "全绿" in note
        assert _named_destination(note) == "dispatching_line"
        assert "本单不发 work.decision.v1" in note

    def test_red_development_with_mechanical_evidence_still_goes_to_dispatching_line(
        self,
    ) -> None:
        report = AuditReport(target="dev_x", kind="development")
        report.record(
            Assertion(
                name="acceptance_rerun",
                ok=False,
                command="uv run pytest -q",
                exit_code=2,
                detail="2/9 条失败",
            )
        )
        note = render_note(report)
        assert "有红" in note
        assert _named_destination(note) == "dispatching_line"

    def test_goal_line_audit_routes_to_supervisor_escalation(self) -> None:
        report = AuditReport(target="wf-x", kind="goal_line")
        note = render_note(report)
        assert _named_destination(note) == "supervisor_escalation"

    def test_gaps_route_to_needs_evidence_for_either_kind(self) -> None:
        for kind in ("development", "goal_line"):
            report = AuditReport(target="x", kind=kind)
            report.gaps.append("acceptance_rerun 降级为 env_unverified（advisory）")
            note = render_note(report)
            assert _named_destination(note) == "needs_evidence", kind
            assert "env_unverified" in note

    def test_no_report_form_names_a_human_as_destination(self) -> None:
        plain = AuditReport(target="dev_x", kind="development")
        red = AuditReport(target="dev_x", kind="goal_line")
        red.record(
            Assertion(name="terminal_fields", ok=False, command="cat", exit_code=1, detail="d")
        )
        red.gaps.append("先 git fetch 对应 durable ref 再重跑审计")
        gapped = AuditReport(target="wf-x", kind="goal_line")
        gapped.gaps.append("缺少 rounds.jsonl")
        for report in (plain, red, gapped):
            note = render_note(report)
            assert LEGACY_PHRASE not in note
            destination = _named_destination(note)
            assert destination in a2.ESCALATION_TARGETS
            assert destination != "needs_human"


# --- spec 边界: the vocabulary is reused, never copied ----------------------


class TestSharedVocabularyReuse:
    def test_both_modules_use_the_a2_constants_by_identity(self) -> None:
        for mod in (supervisor_mod, audit_mod):
            assert mod.ESCALATION_DISPATCHING_LINE is a2.ESCALATION_DISPATCHING_LINE
            assert mod.ESCALATION_SUPERVISOR_ESCALATION is a2.ESCALATION_SUPERVISOR_ESCALATION
            assert mod.ESCALATION_NEEDS_EVIDENCE is a2.ESCALATION_NEEDS_EVIDENCE

    def test_supervisor_routing_table_covers_exactly_the_vocabulary(self) -> None:
        values = set(supervisor_mod._ESCALATION_TARGET_BY_SUBJECT.values())
        assert values == a2.ESCALATION_TARGETS
        assert set(supervisor_mod._ESCALATION_TARGET_RATIONALE) == a2.ESCALATION_TARGETS

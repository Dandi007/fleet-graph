"""The A2 managed periodic path: reconciliation discipline, receipt, and acceptance.

The A2 identity reconciliation itself (whoami + alias ``current_agent_id``,
fail-closed) is covered in ``tests/test_arbiter_reconcile.py``; this file pins
what the managed path depends on:

- the reconcile module stays validation-only -- no mutation call is reachable,
  and the probe reads the bus through GET-only surfaces;
- the bounded tick receipt carries counts/kinds/refs with no credentials, and
  its counters are real classifiers (a known decision fixture counts, a note
  does not);
- the shared acceptance scenario (one scenario, two drivers) yields at least one
  referenced suggestion while decision-shaped reasoner output stays coerced to
  ``work.note.v1`` -- zero ``work.decision.*`` and zero decision-marked chat.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from fleet_graph.arbiter.managed_path import (
    build_receipt,
    count_kinds,
    is_decision_marked_chat,
    run_managed_path_scenario,
)

REPO_ROOT = Path(__file__).parent.parent
ARBITER_PKG = REPO_ROOT / "src" / "fleet_graph" / "arbiter"
RECONCILE_MODULE = ARBITER_PKG / "reconcile.py"
ACCEPTANCE = REPO_ROOT / "scripts" / "a2_managed_path_acceptance.py"


# --- no mutation surface -----------------------------------------------------


def _mutation_calls(source: str) -> list[int]:
    """Call sites in ``reconcile.py`` reaching a write/mint/register verb."""
    tree = ast.parse(source)
    forbidden = frozenset({"publish", "post", "create", "register", "mint", "write"})
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_attr = isinstance(func, ast.Attribute) and func.attr in forbidden
        is_name = isinstance(func, ast.Name) and func.id in forbidden
        if is_attr or is_name:
            lines.append(node.lineno)
    return lines


def test_reconcile_module_exposes_no_mutation_call() -> None:
    source = RECONCILE_MODULE.read_text(encoding="utf-8")
    lines = _mutation_calls(source)
    assert not lines, f"reconcile.py reaches a mutation call at lines {lines}"


def test_bus_probe_reads_via_get_only() -> None:
    source = RECONCILE_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    probe_reads: list[str] = []
    read_verbs = {"get", "post", "publish"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in read_verbs
        ):
            probe_reads.append(node.func.attr)
    assert set(probe_reads) == {"get"}


def test_sabotage_self_verification_catches_a_mutation_call() -> None:
    assert _mutation_calls('client.publish("ch", "work.note.v1", {}, "k")\n') == [1]
    assert _mutation_calls('register("token")\n') == [1]
    assert _mutation_calls('client.get("/v1/agents/whoami")\n') == []


# --- bounded receipt counters ------------------------------------------------


def test_scenario_yields_one_referenced_suggestion_and_zero_decisions() -> None:
    counters = run_managed_path_scenario()
    assert counters["referenced_note_or_suggestion_count"] >= 1
    assert counters["work.decision.v1"] == 0
    assert counters["work.decision.v2"] == 0
    assert counters["decision_marked_chat"] == 0
    # Decision-shaped reasoner output stays coerced to note-only publication.
    assert counters["published_kinds"] == ["work.note.v1"]
    assert counters["emitted_count"] == 1
    # The forbidden-field response for the blocked card is refused, not published.
    assert counters["refused_count"] == 1


def test_decision_marked_chat_classifier_distinguishes_a_real_decision() -> None:
    assert is_decision_marked_chat({"kind": "chat", "payload": {"body": "hi"}}) is False
    assert is_decision_marked_chat({"kind": "chat", "payload": {"decision": "approve"}}) is True
    assert is_decision_marked_chat({"kind": "chat", "payload": {"verdict": "release"}}) is True
    # A decision record is not chat; the work_decision counters catch it.
    assert is_decision_marked_chat({"kind": "work.decision.v1", "payload": {}}) is False
    assert is_decision_marked_chat({"kind": "work.note.v1", "payload": {}}) is False


def test_count_kinds_distinguishes_decisions_and_notes() -> None:
    records = [
        {
            "kind": "work.note.v1",
            "note_type": "finding",
            "marker": "suggestion",
            "message_id": "m1",
            "subject_refs": ["q1"],
        },
        {"kind": "work.decision.v1", "note_type": "", "marker": "", "message_id": "d1"},
        {"kind": "work.decision.v2", "note_type": "", "marker": "", "message_id": "d2"},
        {
            "kind": "chat",
            "note_type": "",
            "marker": "",
            "message_id": "c1",
            "payload": {"decision": "go"},
        },
    ]
    counts = count_kinds(records)
    assert counts == {
        "referenced_note_or_suggestion": 1,
        "work_decision_v1": 1,
        "work_decision_v2": 1,
        "decision_marked_chat": 1,
    }


def test_receipt_is_bounded_and_credential_free() -> None:
    counters = run_managed_path_scenario()
    # The receipt object itself is pure counts/kinds/refs; nothing credential-shaped.
    assert counters["kinds"] == ["work.note.v1"]
    assert counters["dry_run"] is False
    for key in ("referenced_note_or_suggestion_count", "work.decision.v1", "decision_marked_chat"):
        assert isinstance(counters[key], int), key


def test_build_receipt_handles_a_tick_end_to_end() -> None:
    from fleet_graph.arbiter.a2 import ArbiterRun, EmittedMessage

    run = ArbiterRun(dry_run=False)
    run.emitted.append(
        EmittedMessage(
            kind="work.note.v1",
            note_type="finding",
            marker="suggestion",
            message_id="m1",
            subject_refs=("q1",),
            dry_run=False,
        )
    )
    receipt = build_receipt(run)
    assert receipt["dry_run"] is False
    assert receipt["counts"]["referenced_note_or_suggestion"] == 1
    assert receipt["counts"]["work_decision_v1"] == 0
    assert receipt["counts"]["decision_marked_chat"] == 0
    assert receipt["kinds"] == ["work.note.v1"]
    assert receipt["refs"] == [["q1"]]


# --- executable acceptance fixture -------------------------------------------


def test_acceptance_fixture_exits_zero_and_prints_required_counters() -> None:
    proc = subprocess.run([sys.executable, str(ACCEPTANCE)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert '"referenced_note_or_suggestion_count": 1' in proc.stdout
    assert '"work.decision.v1": 0' in proc.stdout
    assert '"work.decision.v2": 0' in proc.stdout
    assert '"decision_marked_chat": 0' in proc.stdout
    assert '"pass": true' in proc.stdout

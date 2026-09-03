"""缺陷⑬: the self-gate docstring speaks D5 authority and the repo is shadow-free.

golden-order D5 fixed the caliber: the gate is judged by the dispatching line
itself, ``decided_by`` must equal the single's ``record.json.dispatched_by``,
and the human/supervision surface appears only at enrollment release, goal-level
acceptance, and the answer escalation report. These tests pin the docstring to
that caliber and sweep the tree for any surviving superseded-shadow token (the
"S"+"8" verdict that the gate belongs to the supervision surface).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCSTRING_PATH = REPO_ROOT / "src" / "fleet_graph" / "dd" / "self_gate.py"
SCAN_EXCLUDED_DIRS = {".git", ".dev-dispatch", ".dd-evidence", ".venv", "__pycache__"}
D5_ANCHORS = ("D5", "decided_by", "dispatched_by")
SHADOW = "S" + "8"


def _check_d5_caliber(docstring: str) -> None:
    """Assert one docstring text carries the D5 anchors and no shadow token."""
    for anchor in D5_ANCHORS:
        assert anchor in docstring, f"missing D5 anchor: {anchor}"
    assert SHADOW not in docstring, "superseded supervision-surface gate resurfaced"


def _docstring_text() -> str:
    """Return the live self_gate module docstring, parsed from the source."""
    tree = ast.parse(DOCSTRING_PATH.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree)
    assert doc is not None, "self_gate.py lost its module docstring"
    return doc


def test_docstring_carries_d5_anchors_and_no_shadow() -> None:
    _check_d5_caliber(_docstring_text())


def test_repo_scan_finds_no_shadow() -> None:
    hits: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if any(part in SCAN_EXCLUDED_DIRS for part in path.parts) or not path.is_file():
            continue
        if SHADOW.encode() in path.read_bytes():
            hits.append(path)
    assert hits == [], f"shadow token survived in: {hits}"


def test_swapping_d5_anchor_for_shadow_turns_the_check_red() -> None:
    mutated = _docstring_text().replace("D5", SHADOW)
    with pytest.raises(AssertionError):
        _check_d5_caliber(mutated)

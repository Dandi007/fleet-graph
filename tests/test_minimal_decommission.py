"""Mechanical decommission guards (dd-36, docs/specs/minimal/decommission.md).

(a) Self-containment: nothing under ``src/fleet_graph/minimal/`` may import a
    ``fleet_graph`` module outside ``fleet_graph.minimal`` itself (relative
    imports included in the allowance). dd-04 already removed the last such
    dependency (``fleet_graph.dd``); this pins that against regression.
(b) Inventory reality: every data row of the decommission table whose first
    column is a backticked repo-relative path must point at something that
    exists in the tree — the document may not list ghost paths. Parsing rule,
    fixed here and mirrored in the document header: a data row starts with
    ``| `` and its first column is a repo-relative path wrapped in backticks;
    header rows, ``|---`` separators and any other non-path first column are
    ignored.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MINIMAL_DIR = REPO_ROOT / "src" / "fleet_graph" / "minimal"
DECOMMISSION_DOC = REPO_ROOT / "docs" / "specs" / "minimal" / "decommission.md"


def _is_minimal_module(dotted: str) -> bool:
    return dotted == "fleet_graph.minimal" or dotted.startswith("fleet_graph.minimal.")


def test_minimal_imports_no_legacy_fleet_graph() -> None:
    """Guard (a): the minimal package imports nothing else from fleet_graph."""
    offenders: list[str] = []
    for path in sorted(MINIMAL_DIR.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    is_fg = name == "fleet_graph" or name.startswith("fleet_graph.")
                    if is_fg and not _is_minimal_module(name):
                        offenders.append(f"{rel}: import {name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import: inside fleet_graph.minimal
                    continue
                module = node.module or ""
                if module == "fleet_graph":
                    for alias in node.names:
                        if alias.name != "minimal":
                            offenders.append(f"{rel}: from fleet_graph import {alias.name}")
                elif module.startswith("fleet_graph.") and not _is_minimal_module(module):
                    offenders.append(f"{rel}: from {module} import ...")
    assert not offenders, "minimal must stay self-contained:\n" + "\n".join(offenders)


def test_decommission_table_paths_exist() -> None:
    """Guard (b): the decommission table lists only real repo paths."""
    assert DECOMMISSION_DOC.is_file(), "docs/specs/minimal/decommission.md must exist"
    listed: list[str] = []
    for line in DECOMMISSION_DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        first = line[2:].split("|", 1)[0].strip()
        if len(first) > 2 and first.startswith("`") and first.endswith("`"):
            listed.append(first[1:-1])
    assert listed, "decommission.md must list at least one backticked path"
    missing = [p for p in listed if not (REPO_ROOT / p).exists()]
    assert not missing, f"ghost paths in decommission.md: {missing}"

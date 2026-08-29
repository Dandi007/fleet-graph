#!/usr/bin/env python3
"""Structural guard for the E4a worker turn report control path.

Guard W1 -- **the ordinary orchestration path is pinned to the structured
decoder/projection**. The ``worker_turn`` node in ``graphs/goal_line.py`` is
where a worker turn's outcome, blocked transition, produced files and self-test
results are derived; it must, in its executable body, call ``decode_report`` (the
strict v1 decoder) and ``project_control`` (the control slice that omits the
prose attachment). If the graph stops routing through either, worker prose can
creep back into the control surface, so the guard names the missing call rather
than inferring success from an import or a comment.

Guard W2 -- **no prose parser is invoked for control decisions**. The
``prose_attachment`` field is the only place a worker's prose may live, and the
orchestration module must never read it: any reference to the ``prose_attachment``
name, attribute or ``"prose_attachment"`` key inside ``goal_line.py`` is a
violation. The control path can only see ``project_control(report)``, which drops
the attachment; a module that reaches for the attachment is, structurally, a
prose parser.

Both guards are AST assertions (comments and docstrings are not code), and both
are delivered with sabotage self-verification in
``tests/test_work_report_conformance.py``: a guard that has never caught
anything is a guard you know nothing about.

Exit codes: 0 clean, 1 violations found, 2 usage/IO error.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

GOAL_LINE_RELPATH = "fleet_graph/graphs/goal_line.py"

#: The worker-turn node whose body Guard W1 inspects.
WORKER_TURN_FN = "worker_turn"

#: The decoder and projection calls the ordinary path must route through.
REQUIRED_CALLS = frozenset({"decode_report", "project_control"})


def _call_names(func_node: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def check_worker_turn_routes(path: Path, tree: ast.AST) -> list[str]:
    errors: list[str] = []
    worker_turn: ast.AST | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == WORKER_TURN_FN
        ):
            worker_turn = node
            break
    if worker_turn is None:
        return [f"{path}: no {WORKER_TURN_FN!r} node found -- the orchestration path is gone"]
    calls = _call_names(worker_turn)
    missing = sorted(REQUIRED_CALLS - calls)
    if missing:
        errors.append(
            f"{path}: {WORKER_TURN_FN} does not call {', '.join(missing)} -- "
            "the ordinary orchestration path is not pinned to the structured "
            "decoder/projection"
        )
    return errors


def _references_prose(node: ast.AST) -> bool:
    """Does this AST node name the prose_attachment field (any spelling)?"""
    if isinstance(node, ast.Constant):
        return node.value == "prose_attachment"
    if isinstance(node, ast.Name):
        return node.id == "prose_attachment"
    if isinstance(node, ast.Attribute):
        return node.attr == "prose_attachment"
    return False


def check_no_prose_control(path: Path, tree: ast.AST) -> list[str]:
    errors: list[str] = []
    for node in ast.walk(tree):
        if _references_prose(node):
            errors.append(
                f"{path}:{node.lineno}: references prose_attachment -- "
                "the orchestration module must not read worker prose"
            )
    return errors


def run(src_root: Path) -> list[str]:
    if not src_root.is_dir():
        raise SystemExit(f"not a directory: {src_root}")
    path = src_root / GOAL_LINE_RELPATH
    if not path.is_file():
        return [f"missing orchestration module: {path}"]
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    errors: list[str] = []
    errors.extend(check_worker_turn_routes(path, tree))
    errors.extend(check_no_prose_control(path, tree))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src-root",
        default=str(Path(__file__).resolve().parent.parent / "src"),
        help="source tree to check (tests point this at sabotage samples)",
    )
    args = parser.parse_args(argv)
    try:
        errors = run(Path(args.src_root))
    except (OSError, SyntaxError) as exc:
        print(f"work-report conformance check could not run: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} work-report conformance violation(s)", file=sys.stderr)
        return 1
    print("work-report conformance: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

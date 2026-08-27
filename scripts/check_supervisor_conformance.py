#!/usr/bin/env python3
"""Two structural guarantees of the supervision face, as AST assertions.

Guard A -- **the supervisor cannot schedule** (r4-design §5, D9). The modules
that make up the supervisor graph (`graphs/supervisor.py` and everything under
`supervise/`) must not import `fleet_graph.scheduler.ignition` or
`fleet_graph.scheduler.launcher`, in any spelling. The observer
(`scheduler/supervisor_events.py`) is deliberately *outside* this set: it
lives on the scheduler's side and holds the launcher -- that is the one
sanctioned direction.

Guard B -- **nothing in this repo publishes a `work.decision.v1`**
(bus/board.py's standing rule, made regression-loud). No call expression
anywhere under `src/` may carry the literal ``"work.decision.v1"`` or the
``DECISION_KIND`` name as an argument. Read paths survive: the constant's
definition is an assignment, and `m.get("kind") == DECISION_KIND` is a
comparison -- neither is a call argument.

The technique is lifted from the old supervisor's check_no_local_scheduler.py,
and so is its delivery discipline: tests/test_supervisor_conformance.py feeds
this script deliberately violating samples and asserts a non-zero exit
(sabotage self-verification -- a guard that has never caught anything is a
guard you know nothing about).

Exit codes: 0 clean, 1 violations found, 2 usage/IO error.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

FORBIDDEN_SCHEDULER_MODULES = (
    "fleet_graph.scheduler.ignition",
    "fleet_graph.scheduler.launcher",
)
FORBIDDEN_SCHEDULER_NAMES = frozenset({"ignition", "launcher"})

DECISION_LITERAL = "work.decision.v1"
DECISION_NAME = "DECISION_KIND"


def supervisor_module_paths(src_root: Path) -> list[Path]:
    """The modules Guard A covers: the graph and the supervise package."""
    paths: list[Path] = []
    graph = src_root / "fleet_graph" / "graphs" / "supervisor.py"
    if graph.is_file():
        paths.append(graph)
    supervise = src_root / "fleet_graph" / "supervise"
    if supervise.is_dir():
        paths.extend(sorted(supervise.rglob("*.py")))
    return paths


def check_no_scheduler_imports(path: Path, tree: ast.AST) -> list[str]:
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == mod or alias.name.startswith(mod + ".")
                    for mod in FORBIDDEN_SCHEDULER_MODULES
                ):
                    errors.append(f"{path}:{node.lineno}: imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(
                module == mod or module.startswith(mod + ".") for mod in FORBIDDEN_SCHEDULER_MODULES
            ):
                errors.append(f"{path}:{node.lineno}: imports from {module}")
            elif module == "fleet_graph.scheduler":
                bad = [a.name for a in node.names if a.name in FORBIDDEN_SCHEDULER_NAMES]
                if bad:
                    errors.append(
                        f"{path}:{node.lineno}: imports {', '.join(bad)} from fleet_graph.scheduler"
                    )
    return errors


def _is_decision_reference(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and node.value == DECISION_LITERAL:
        return True
    if isinstance(node, ast.Name) and node.id == DECISION_NAME:
        return True
    return isinstance(node, ast.Attribute) and node.attr == DECISION_NAME


def check_no_decision_publish(path: Path, tree: ast.AST) -> list[str]:
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        for argument in arguments:
            if _is_decision_reference(argument):
                errors.append(
                    f"{path}:{node.lineno}: a call carries "
                    f"{DECISION_LITERAL!r}/{DECISION_NAME} as an argument -- "
                    "there is no sanctioned publish path for a decision"
                )
    return errors


def run(src_root: Path) -> list[str]:
    if not src_root.is_dir():
        raise SystemExit(f"not a directory: {src_root}")
    errors: list[str] = []

    for path in supervisor_module_paths(src_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        errors.extend(check_no_scheduler_imports(path, tree))

    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        errors.extend(check_no_decision_publish(path, tree))
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
        print(f"conformance check could not run: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} supervisor conformance violation(s)", file=sys.stderr)
        return 1
    print("supervisor conformance: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

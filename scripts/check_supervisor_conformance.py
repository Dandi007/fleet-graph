#!/usr/bin/env python3
"""Three structural guarantees of the supervision face, as AST assertions.

Guard A -- **the supervisor cannot schedule** (r4-design §5, D9). The modules
that make up the supervisor graph (`graphs/supervisor.py` and everything under
`supervise/`) must not import `fleet_graph.scheduler.ignition` or
`fleet_graph.scheduler.launcher`, in any spelling. The observer
(`scheduler/supervisor_events.py`) is deliberately *outside* this set: it
lives on the scheduler's side and holds the launcher -- that is the one
sanctioned direction.

Guard B -- **exactly one module may publish a decision** (`work.decision.v1`
or `work.decision.v2` alike): `supervise/decision_publisher.py`, the R4-3
preauth release path. Everywhere else, no call expression under `src/` may
carry the literal ``"work.decision.v1"``/``"work.decision.v2"`` or the
``DECISION_KIND``/``DECISION_KIND_V2`` name as an argument (bus/board.py's
standing rule, made regression-loud; before R4-3 the exemption set was
empty). Read paths survive: the constant's definition is an assignment, and
`m.get("kind") in DECISION_KINDS` is a comparison -- neither is a call
argument.

Guard C -- **the publish entry has exactly one caller**: only
`graphs/supervisor.py` (whose `act` node is a script node) may import
`fleet_graph.supervise.decision_publisher`, in any spelling. The llm
execution paths (`executors/`, the dd graphs) and everything else in the repo
structurally cannot reach the publisher -- an llm that talks a node into
publishing a decision has no import to do it with.

Guard D -- **the harvest subgraph's writes are allowlist-gated** (M3). The
harvest ReAct subgraph (`supervise/harvest.py`) is the one supervisor path that
writes (squash merge + deploy). Its write permission has exactly one source: a
hit on the harvest allowlist (`supervise/harvest_allowlist.py`), and an
out-of-bounds write is a refusal with recorded evidence, never a silent pass.
Structurally, Guard D asserts that every function in `supervise/harvest.py`
whose body performs a *write primitive* (a git write, a subprocess/OS write, a
deploy execution -- anything that can touch the target repo or the deployed
host) also calls the allowlist gate (`authorize_harvest_write` / `authorize` /
`allowlist.authorize`, any spelling) in the same function body. Read-only
helpers and the gate definition itself are exempt; a write function without
the gate call is a diagnostic, so an llm-adjacent refactor cannot silently add
an ungated write. The mechanical ops layer (`supervise/harvest_ops.py`) is the
other side of the gate (it executes only what the gated orchestration asked),
so it is exempt from Guard D -- the gate is the orchestrator's, and the
diagnostic is scoped to the orchestration module.

Guard E -- **the E6/E7 dispatch reactors' writes are gated too** (M4). The E6
stop reactor (`supervise/e6_stop.py`) may only stop its own event.folder_id's
line unit (prefix-exact match, no arbitrary unit), and the E7 goal.md reactor
(`supervise/e7_write.py`) may only write the folder_id it resolved (圈点, default
deny-all). Same discipline as Guard D, applied to both modules: every function
in `supervise/e6_stop.py` that performs a stop write primitive (`stop_unit` /
`systemctl` / subprocess) must also call the stop gate (`authorize_e6_stop` /
`authorize`) in the same body; every function in `supervise/e7_write.py` that
performs a goal.md write primitive (`append_delivery_fail_block` / `fs_write` /
`fs_edit` / `write` / `edit` / `create`) must also call the write gate
(`authorize_e7_write` / `authorize`) in the same body. The ops layers
(`supervise/e6_ops.py`, `supervise/e7_ops.py`) and the allowlist module are
exempt, exactly like `harvest_ops.py` is under Guard D.

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

DECISION_LITERALS = frozenset({"work.decision.v1", "work.decision.v2"})
DECISION_NAMES = frozenset({"DECISION_KIND", "DECISION_KIND_V2"})

# Guard B exemption: the single sanctioned decision publisher (R4-3).
DECISION_PUBLISHER_RELPATH = "fleet_graph/supervise/decision_publisher.py"

#: M3.1 (S10 裁决送达必须落地): the dd gate-delivery publish point. The dd
#: control plane's ``publish_gate_decision`` is the second sanctioned decision
#: publish path: reachable only behind the S11 ``dispatched_by`` authority
#: check (the decision MCP's dd delivery and the line self-gate -- the
#: authorized principal is exactly what the publish records as ``decided_by``),
#: never from an llm execution path. The exemption is scoped to exactly this
#: function in exactly this module; any other call anywhere -- including any
#: other function in the same module -- still trips the guard.
GUARD_B_PUBLISH_POINTS = {
    ("fleet_graph/dd/control_plane.py", "publish_gate_decision"),
}

# Guard C: who may import the publisher. The act script node, and nobody else.
DECISION_PUBLISHER_MODULE = "fleet_graph.supervise.decision_publisher"
DECISION_PUBLISHER_IMPORTERS = frozenset({"fleet_graph/graphs/supervisor.py"})

# Guard D: the harvest orchestration module whose write functions must be gated.
HARVEST_RELPATH = "fleet_graph/supervise/harvest.py"

# Guard E: the E6/E7 dispatch orchestration modules whose write functions must
# be gated (M4).
E6_STOP_RELPATH = "fleet_graph/supervise/e6_stop.py"
E7_WRITE_RELPATH = "fleet_graph/supervise/e7_write.py"

#: Write primitives: call names (function or attribute) that can write to the
#: target repo, the deployed host, or the supervised line's own unit/goal.md.
#: Anything in this set in a gated module's function makes the function a write
#: function that must also call the corresponding gate.
HARVEST_WRITE_PRIMITIVES = frozenset(
    {
        # git writes.
        "git",
        "git_argv",
        "run_git",
        "cherry_pick",
        "merge",
        "push",
        "pull",
        "squash_merge",
        "worktree",
        "commit",
        # process / OS writes.
        "subprocess",
        "Popen",
        "check_call",
        "check_output",
        "run_process",
        "os.system",
        "shutil",
        "deploy",
        "exec",
        # ops-layer executions that reach the repo or host.
        "fetch_dd_ref",
        "worktree_cherry_pick",
        "remove_worktree",
        "run_verify",
        "pr_squash_merge",
        "ff_only_pull",
        "verify_real",
    }
)

#: E6 stop write primitives: anything that stops a line unit or shells out to
#: systemd on behalf of the stop.
E6_STOP_WRITE_PRIMITIVES = frozenset(
    {
        "stop_unit",
        "systemctl",
        "systemd",
        "subprocess",
        "Popen",
        "check_call",
        "check_output",
        "os.system",
        "exec",
    }
)

#: E7 goal.md write primitives: anything that writes the supervised line's
#: goal.md (via the ops layer or directly through the work-folder client).
E7_WRITE_WRITE_PRIMITIVES = frozenset(
    {
        "append_delivery_fail_block",
        "goal_write",
        "fs_write",
        "fs_edit",
        "fs_create",
        "write",
        "edit",
        "create",
        "WorkFolder",
        "subprocess",
        "os.system",
    }
)

#: Allowlist/gate names: calling any of these counts as gating the write.
#: (``authorize`` matches both the pure function and ``allowlist.authorize``.)
HARVEST_GATE_NAMES = frozenset({"authorize_harvest_write", "authorize"})
E6_STOP_GATE_NAMES = frozenset({"authorize_e6_stop", "authorize"})
E7_WRITE_GATE_NAMES = frozenset({"authorize_e7_write", "authorize"})

#: The write-gating specs: relpath -> (write primitives, gate names, label).
WRITE_GATING_SPECS: tuple[tuple[str, frozenset[str], frozenset[str], str], ...] = (
    (HARVEST_RELPATH, HARVEST_WRITE_PRIMITIVES, HARVEST_GATE_NAMES, "harvest"),
    (E6_STOP_RELPATH, E6_STOP_WRITE_PRIMITIVES, E6_STOP_GATE_NAMES, "E6 stop"),
    (E7_WRITE_RELPATH, E7_WRITE_WRITE_PRIMITIVES, E7_WRITE_GATE_NAMES, "E7 goal.md write"),
)


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
    if isinstance(node, ast.Constant) and node.value in DECISION_LITERALS:
        return True
    if isinstance(node, ast.Name) and node.id in DECISION_NAMES:
        return True
    return isinstance(node, ast.Attribute) and node.attr in DECISION_NAMES


class _DecisionPublishScan(ast.NodeVisitor):
    """Guard B's scan: every call carrying a decision kind, except the
    sanctioned publish points (the R4-3 publisher module and the M3.1
    S11-gated dd gate-delivery function)."""

    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.function_stack: list[str] = []
        self.errors: list[str] = []

    def _exempt(self) -> bool:
        return any((self.relpath, name) in GUARD_B_PUBLISH_POINTS for name in self.function_stack)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if not self._exempt():
            arguments = list(node.args) + [kw.value for kw in node.keywords]
            for argument in arguments:
                if _is_decision_reference(argument):
                    self.errors.append(
                        f"{self.relpath}:{node.lineno}: a call carries "
                        f"{'/'.join(sorted(DECISION_LITERALS | DECISION_NAMES))} "
                        "as an argument -- the only sanctioned decision publish "
                        f"paths are {DECISION_PUBLISHER_RELPATH} and the S11-gated "
                        "dd gate delivery"
                    )
        self.generic_visit(node)


def check_no_decision_publish(path: Path, relpath: str, tree: ast.AST) -> list[str]:
    scan = _DecisionPublishScan(relpath)
    scan.visit(tree)
    return scan.errors


def check_publisher_import_whitelist(path: Path, relpath: str, tree: ast.AST) -> list[str]:
    """Guard C: importing the decision publisher outside the whitelist."""
    if relpath in DECISION_PUBLISHER_IMPORTERS or relpath == DECISION_PUBLISHER_RELPATH:
        return []
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == DECISION_PUBLISHER_MODULE or alias.name.startswith(
                    DECISION_PUBLISHER_MODULE + "."
                ):
                    errors.append(
                        f"{path}:{node.lineno}: imports {alias.name} -- only the "
                        "supervisor act node may reach the decision publisher"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            hit = module == DECISION_PUBLISHER_MODULE or module.startswith(
                DECISION_PUBLISHER_MODULE + "."
            )
            if not hit and module == "fleet_graph.supervise":
                hit = any(a.name == "decision_publisher" for a in node.names)
            if hit:
                errors.append(
                    f"{path}:{node.lineno}: imports from {module or 'fleet_graph.supervise'} "
                    "-- only the supervisor act node may reach the decision publisher"
                )
    return errors


def _called_names(tree: ast.AST) -> set[str]:
    """Every name and attribute actually invoked as a call in `tree`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
            value = func.value
            if isinstance(value, ast.Name):
                names.add(value.id)
    return names


def check_write_gating(
    path: Path,
    relpath: str,
    tree: ast.AST,
    *,
    write_primitives: frozenset[str],
    gate_names: frozenset[str],
    label: str,
) -> list[str]:
    """Guard D/E: every write primitive call lives in a function that also calls
    the gate, and no write primitive is called at module scope.

    A write primitive (git write / subprocess / OS write / deploy / ops-layer
    execution / stop / goal.md write) is a diagnostic the moment it appears in a
    gated orchestration module without a gate call in the same function body --
    an ungated write is the exact thing the allowlist-first ordering forbids.
    """
    errors: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and node.name in {
                "authorize_harvest_write",
                "authorize_e6_stop",
                "authorize_e7_write",
            }:
                continue
            body: ast.AST = node
            called = _called_names(body)
            writes = called & write_primitives
            if not writes:
                continue
            if not (called & gate_names):
                errors.append(
                    f"{path}:{node.lineno}: function {node.name} performs a {label} "
                    f"write ({sorted(writes)}) without calling the {label} gate "
                    f"({sorted(gate_names)}) -- {label} writes must be "
                    "allowlist-gated (default deny-all)"
                )

    module_called = _called_names(tree)
    if module_called & write_primitives and not (module_called & gate_names):
        errors.append(
            f"{path}:1: the {label} module invokes a write primitive "
            f"({sorted(module_called & write_primitives)}) without importing "
            f"the allowlist gate ({sorted(gate_names)}) -- ungated {label} write"
        )
    return errors


def check_harvest_write_gating(path: Path, relpath: str, tree: ast.AST) -> list[str]:
    """Guard D: the harvest orchestration module's writes must be gated."""
    if relpath != HARVEST_RELPATH:
        return []
    return check_write_gating(
        path,
        relpath,
        tree,
        write_primitives=HARVEST_WRITE_PRIMITIVES,
        gate_names=HARVEST_GATE_NAMES,
        label="harvest",
    )


def check_e6_stop_gating(path: Path, relpath: str, tree: ast.AST) -> list[str]:
    """Guard E: the E6 stop reactor may only stop its own folder's line unit."""
    if relpath != E6_STOP_RELPATH:
        return []
    return check_write_gating(
        path,
        relpath,
        tree,
        write_primitives=E6_STOP_WRITE_PRIMITIVES,
        gate_names=E6_STOP_GATE_NAMES,
        label="E6 stop",
    )


def check_e7_write_gating(path: Path, relpath: str, tree: ast.AST) -> list[str]:
    """Guard E: the E7 reactor may only write its resolved folder's goal.md."""
    if relpath != E7_WRITE_RELPATH:
        return []
    return check_write_gating(
        path,
        relpath,
        tree,
        write_primitives=E7_WRITE_WRITE_PRIMITIVES,
        gate_names=E7_WRITE_GATE_NAMES,
        label="E7 goal.md write",
    )


def run(src_root: Path) -> list[str]:
    if not src_root.is_dir():
        raise SystemExit(f"not a directory: {src_root}")
    errors: list[str] = []

    for path in supervisor_module_paths(src_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        errors.extend(check_no_scheduler_imports(path, tree))

    for path in sorted(src_root.rglob("*.py")):
        relpath = path.relative_to(src_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if relpath != DECISION_PUBLISHER_RELPATH:
            errors.extend(check_no_decision_publish(path, relpath, tree))
        errors.extend(check_publisher_import_whitelist(path, relpath, tree))
        errors.extend(check_harvest_write_gating(path, relpath, tree))
        errors.extend(check_e6_stop_gating(path, relpath, tree))
        errors.extend(check_e7_write_gating(path, relpath, tree))
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

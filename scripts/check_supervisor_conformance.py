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

Guard F -- **a self-adjudication ruling's rationale must carry the full
mechanical echo** (G3, goal.md 2026-09-02 16:4x). The dispatch line may judge
its own gate tickets (wf-6475fd judged dev-fg-e760435f2a6d's ledger outcome and
dev-fg-977e5280d628's metrics zero-I/O ticket itself), but that self-judging
must not degrade into a one-word APPROVE. A legal self-adjudication
APPROVE/REJECT ``rationale`` must echo, in machine-readable form: the
three-party acceptance verbatim equal (``spec dd-acceptance`` == ``run-config``
== ``record acceptance_commands``), the product diff staying within the spec
boundary (never the reserved ``.dev-dispatch/`` namespace), the existing tests
not deleted (``LC_ALL=C comm -23`` per-name comparison), and the personally-run
acceptance exit code plus tail echo. A REJECT ``rationale`` must additionally
carry a verbatim rework instruction that is non-empty and names the rework
point. Fed via ``--adjudication-record <json>``; the fixtures hardcode the
wf-6475fd two-vote rationale morphology samples (one positive, one negative).

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
import json
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
                    f"{'/'.join(sorted(DECISION_LITERALS | DECISION_NAMES))} "
                    "as an argument -- the only sanctioned decision publish "
                    f"path is {DECISION_PUBLISHER_RELPATH}"
                )
    return errors


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


# --- Guard F: self-adjudication rationale morphology (G3) -------------------
#
# A self-adjudication must not degrade into a one-word APPROVE. A legal
# APPROVE/REJECT ruling's `rationale` must echo, in machine-readable form:
# the three-party acceptance verbatim equal, the product diff within the spec
# boundary, the existing tests not deleted (comm -23 per-name comparison), and
# the personally-run acceptance exit code plus tail. A REJECT rationale must
# additionally carry a verbatim rework instruction naming the rework point.

SELF_ADJUDICATION_VOTES = ("APPROVE", "REJECT")

ACCEPTANCE_SPEC_LABEL = "spec dd-acceptance"
ACCEPTANCE_RUN_LABEL = "run-config"
ACCEPTANCE_RECORD_LABEL = "record acceptance_commands"
PRODUCT_DIFF_LABEL = "产品 diff"
TESTS_INTACT_LABEL = "既有测试"
ACCEPTANCE_EXIT_LABEL = "亲跑验收退出码"
ACCEPTANCE_TAIL_LABEL = "尾部回显"
REWORK_LABEL = "返工指令"

#: The reserved namespace that no product diff may touch, whatever the spec.
RESERVED_NAMESPACE_PREFIX = ".dev-dispatch/"

#: A rework instruction that names no location is a rework instruction that
#: tells the next implement nothing. At least one location hint is required.
REWORK_POINT_HINTS = ("/", ".py", "tests/", "src/", "scripts/")


def _labeled_value(rationale: str, label: str) -> str | None:
    """The value of the first line ``label: value`` in `rationale`, or None."""
    prefix = f"{label}:"
    for line in rationale.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _names_rework_point(rework: str) -> bool:
    """Whether a rework instruction names at least one concrete rework point."""
    return any(hint in rework for hint in REWORK_POINT_HINTS)


def check_self_adjudication_rationale(record: dict) -> list[str]:
    """Guard F (G3): the mechanical-echo morphology of one self-adjudication.

    `record` is one ruling: ``{"decision": "APPROVE"|"REJECT", "rationale": str}``.
    Returns the list of violations; empty means the ruling is a legal
    self-adjudication.
    """
    errors: list[str] = []
    decision = record.get("decision")
    rationale = record.get("rationale")
    if decision not in SELF_ADJUDICATION_VOTES:
        errors.append(f"decision must be one of {SELF_ADJUDICATION_VOTES}, got {decision!r}")
        return errors
    if not rationale or not rationale.strip():
        errors.append("rationale must not be empty for a self-adjudication")
        return errors

    spec = _labeled_value(rationale, ACCEPTANCE_SPEC_LABEL)
    run = _labeled_value(rationale, ACCEPTANCE_RUN_LABEL)
    record_cmd = _labeled_value(rationale, ACCEPTANCE_RECORD_LABEL)
    if not (spec and run and record_cmd):
        errors.append(
            "rationale must echo the three-party acceptance "
            f"( {ACCEPTANCE_SPEC_LABEL} / {ACCEPTANCE_RUN_LABEL} / {ACCEPTANCE_RECORD_LABEL} )"
        )
    elif not (spec == run == record_cmd):
        errors.append(
            "three-party acceptance must be verbatim equal "
            f"( {ACCEPTANCE_SPEC_LABEL} == {ACCEPTANCE_RUN_LABEL} == {ACCEPTANCE_RECORD_LABEL} )"
        )

    diff = _labeled_value(rationale, PRODUCT_DIFF_LABEL)
    if diff is None:
        errors.append(f"rationale must echo the product diff ({PRODUCT_DIFF_LABEL})")
    else:
        paths = [p.strip() for p in diff.split(",") if p.strip()]
        if not paths:
            errors.append(f"{PRODUCT_DIFF_LABEL} echo must name at least one changed product path")
        for path in paths:
            if path.startswith(RESERVED_NAMESPACE_PREFIX):
                errors.append(
                    f"product diff crosses the spec boundary into the reserved namespace: {path}"
                )

    tests = _labeled_value(rationale, TESTS_INTACT_LABEL)
    if tests is None or "comm -23" not in tests:
        errors.append(
            f"rationale must echo the existing-tests-not-deleted check "
            f"({TESTS_INTACT_LABEL} LC_ALL=C comm -23 per-name comparison)"
        )
    elif not any(marker in tests for marker in ("0", "无删除", "no deletion", "deleted 0")):
        errors.append(f"{TESTS_INTACT_LABEL} echo must show zero deleted tests")

    exit_code = _labeled_value(rationale, ACCEPTANCE_EXIT_LABEL)
    tail = _labeled_value(rationale, ACCEPTANCE_TAIL_LABEL)
    if exit_code is None or tail is None or not tail.strip():
        errors.append(
            "rationale must echo the personally-run acceptance "
            f"({ACCEPTANCE_EXIT_LABEL} and {ACCEPTANCE_TAIL_LABEL})"
        )
    else:
        codes = [c.strip() for c in exit_code.split(",") if c.strip()]
        if not codes:
            errors.append(f"{ACCEPTANCE_EXIT_LABEL} must carry at least one exit code")
        elif decision == "APPROVE" and any(c != "0" for c in codes):
            errors.append(
                f"APPROVE must report an all-zero personally-run acceptance exit code, "
                f"got {exit_code!r}"
            )

    if decision == "REJECT":
        rework = _labeled_value(rationale, REWORK_LABEL)
        if not rework:
            errors.append(
                f"REJECT rationale must carry a verbatim rework instruction ({REWORK_LABEL})"
            )
        elif not _names_rework_point(rework):
            errors.append(f"REJECT rework instruction must name the rework point, got {rework!r}")
    return errors


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
            errors.extend(check_no_decision_publish(path, tree))
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
    parser.add_argument(
        "--adjudication-record",
        default=None,
        metavar="JSON",
        help="validate one self-adjudication ruling record (Guard F / G3)",
    )
    args = parser.parse_args(argv)

    if args.adjudication_record is not None:
        try:
            record = json.loads(Path(args.adjudication_record).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"adjudication record could not be read: {exc}", file=sys.stderr)
            return 2
        errors = check_self_adjudication_rationale(record)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            print(f"{len(errors)} self-adjudication rationale violation(s)", file=sys.stderr)
            return 1
        print("self-adjudication rationale: clean")
        return 0

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

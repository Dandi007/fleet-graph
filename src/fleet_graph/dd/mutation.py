"""S12: mutation testing executed by the dd engine's final_review stage.

The 2026-09-03 user decision (design.md §6.3 table + §6.3.1, commits ``76de20f``
and ``0f68885``) moves the mutation gun out of the dispatching line's hands: a
line that picks its own targets shoots only at its own blind spots. The
final_review **stage** therefore executes the mutations itself, mechanically:

1. **Mechanical enumeration** (:func:`enumerate_mutation_targets`): the targets
   are every *new production-side call site* in ``base..head``'s product diff --
   not a hand-picked subset. Test files, ``.dev-dispatch/`` and ``.dd-evidence/``
   are never targets; everything else that the diff adds a call on is.
2. **One-shot copy execution** (:func:`execute_final_review_mutations`): each
   target is deleted in turn inside a throwaway ``git worktree`` copy and the
   frozen acceptance commands run there. The subject workspace is never written:
   an experiment that writes the subject voids the verdict. The copy is removed
   afterwards.
3. **Receipt** (:func:`execute_final_review_mutations`): the artifact records
   every target's location (path/line/call) with its red/green result, plus the
   mandatory ``checked_items`` / ``verified_items`` checklists (S12.5 -- a
   receipt that does not say what it checked is invalid even at ``findings == 0``).
   Any target whose deletion leaves the acceptance green has no test coverage,
   and the review is a ``REJECT``.

4. **Gate verifies, never re-runs** (:func:`verify_mutation_receipt`): the gate
   checks the receipt against its own mechanical enumeration -- set equality,
   every target red, mandatory fields present. Re-running the gun at the gate is
   itself a refusal.
5. **Static reachability** (:func:`static_call_reachable`): the D8 equivalence
   assertion. The final_review execution entry must be reachable in the
   production review module's call graph (import/call edges, resolved statically
   over the AST) -- reachable or the assertion is red. Not "some process is
   running": a call-graph property, checkable in frozen acceptance.

S12.5 enforcement lives here in engine code on purpose: the vendored
``contracts/`` mirror is pinned byte-for-byte against what production walks
(provenance), so the engine's own review-receipt schema check --
:func:`validate_review_receipt`, applied where the engine ingests reviewer
output -- is what makes the checklist mandatory, alongside the review prompt
that tells the reviewer the contract before it answers.
"""

from __future__ import annotations

import ast
import contextlib
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git

#: Product-diff namespaces that are never mutation targets (controller-reserved
#: and machine-evidence trees, per the spec's boundary item 2).
EXCLUDED_DIFF_PREFIXES = (".dev-dispatch/", ".dd-evidence/")

#: A test path is never a production-side call site. Matched against the
#: repo-relative posix path.
TEST_PATH_PATTERN = re.compile(r"(^|/)(tests?/|conftest\.py)|(^|/)test_[^/]*\.py$|_test\.py$")

#: A call site on an added line: ``name(`` with a Python identifier. Keywords
#: and syntactic keywords that look like calls are excluded.
CALL_SITE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

#: Python keywords / soft keywords that are not calls when followed by ``(``.
NON_CALL_KEYWORDS = frozenset(
    {
        "if",
        "elif",
        "while",
        "for",
        "return",
        "yield",
        "and",
        "or",
        "not",
        "in",
        "is",
        "assert",
        "raise",
        "with",
        "as",
        "def",
        "class",
        "lambda",
        "print",  # not a keyword, but a call no product change should hang on
        "case",
        "match",
        "await",
    }
)

#: Receipt schema version of the mutation receipt artifact.
MUTATION_RECEIPT_VERSION = 1

#: The S12.5 mandatory checklist fields. Both are required on the receipt:
#: ``checked_items`` is the schema-level name, ``verified_items`` its alias.
CHECKED_ITEMS_FIELD = "checked_items"
VERIFIED_ITEMS_FIELD = "verified_items"

#: Refusal code for a receipt whose target set or red/green facts disagree with
#: the gate's own mechanical enumeration.
MUTATION_RECEIPT_INVALID = "MUTATION_RECEIPT_INVALID"

#: Refusal code when a receipt target survived green: no test coverage.
MUTATION_TARGET_NOT_RED = "MUTATION_TARGET_NOT_RED"

#: Stage-failure code when the mutation gate itself cannot execute (a diff
#: that will not resolve, a one-shot copy that will not open). The gate never
#: answers "pass" on a broken experiment: the stage fails and the bounded
#: retry machinery owns what happens next.
MUTATION_EXECUTION_FAILED = "MUTATION_EXECUTION_FAILED"

Runner = Callable[[Path, list[str]], int]


#: The production default: run the frozen acceptance argv in the copy, capture
#: everything, report only the exit code.
def _default_runner(cwd: Path, argv: list[str]) -> int:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    ).returncode


@dataclass(frozen=True)
class MutationTarget:
    """One new production-side call site: the mechanical mutation target."""

    path: str
    line: int
    call: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.path, self.line, self.call)

    @property
    def location(self) -> str:
        """The receipt's ``file:line (call)`` location form."""
        return f"{self.path}:{self.line} ({self.call})"

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "call": self.call}


def is_product_path(path: str) -> bool:
    """Not a test and not a controller-reserved/machine-evidence namespace."""
    posix = path.replace("\\", "/")
    if posix.startswith(EXCLUDED_DIFF_PREFIXES):
        return False
    return not TEST_PATH_PATTERN.search(posix)


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, ignoring ``#`` inside string literals."""
    quote = ""
    escape = False
    for index, char in enumerate(line):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#":
            return line[:index]
    return line


def _call_names_on(line: str) -> list[str]:
    """The call-site names on one source line, mechanically extracted."""
    code = _strip_comment(line).strip()
    if not code:
        return []
    if code.startswith(("import ", "from ")):
        return []
    names: list[str] = []
    for match in CALL_SITE.finditer(code):
        name = match.group(1)
        prefix = code[: match.start(1)]
        last_word = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", prefix)
        # A ``def name(`` / ``class name(`` line is a definition, not a call.
        definitional = code.startswith(("def ", "class ")) and (
            not last_word or last_word[-1] in {"def", "class"}
        )
        if definitional:
            continue
        if name in NON_CALL_KEYWORDS:
            continue
        names.append(name)
    return names


def enumerate_mutation_targets(repo: Path, base: str, head: str) -> list[MutationTarget]:
    """Every new production-side call site in ``base..head``'s product diff.

    Mechanical, and deliberately boring: walk the unified diff, keep added
    lines of product files, extract their call sites with new-file line
    numbers. No taste, no selection -- the spec's whole point is that nobody
    picks the targets. Deterministically ordered by (path, line, call).
    """
    diff = run_git(repo, "diff", "--unified=0", f"{base}..{head}")
    if diff.returncode != 0:
        raise RuntimeError(f"mutation enumeration cannot diff {base}..{head}: {diff.stderr}")
    targets: list[MutationTarget] = []
    current_path: str | None = None
    new_line = 0
    for raw in diff.stdout.splitlines():
        if raw.startswith("+++ b/"):
            candidate = raw[len("+++ b/") :]
            current_path = candidate if is_product_path(candidate) else None
            continue
        if raw.startswith(("--- ", "diff ", "index ", "new file mode", "deleted file mode")):
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            new_line = int(match.group(1)) if match else 0
            continue
        if current_path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            for name in _call_names_on(raw[1:]):
                targets.append(MutationTarget(current_path, new_line, name))
            new_line += 1
        elif raw.startswith("-") or raw.startswith(" "):
            continue
    ordered = sorted(targets, key=lambda target: target.key)
    deduped: list[MutationTarget] = []
    seen: set[tuple[str, int, str]] = set()
    for target in ordered:
        if target.key in seen:
            continue
        seen.add(target.key)
        deduped.append(target)
    return deduped


def _one_shot_copy(repo: Path, head: str, parent: Path | None) -> tuple[Path, Path]:
    """A detached worktree at ``head``: the disposable experiment subject."""
    tmp = Path(tempfile.mkdtemp(prefix="dd-mutation-", dir=parent))
    result = run_git(repo, "worktree", "add", "--detach", str(tmp / "copy"), head)
    if result.returncode != 0:
        raise RuntimeError(f"one-shot copy failed: {result.stderr.strip()}")
    return tmp, tmp / "copy"


def _drop_worktree(repo: Path, tmp: Path, copy: Path) -> None:
    run_git(repo, "worktree", "remove", "--force", str(copy))
    with contextlib.suppress(OSError):
        tmp.rmdir()


def _target_red(copy: Path, target: MutationTarget, argv: list[str], runner: Runner) -> int:
    """Delete the target's line in the copy, run one acceptance command."""
    source = copy / target.path
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    if not 1 <= target.line <= len(lines):
        raise RuntimeError(f"mutation target out of range: {target.location}")
    mutated = lines[: target.line - 1] + lines[target.line :]
    source.write_text("".join(mutated), encoding="utf-8")
    return runner(copy, argv)


def execute_final_review_mutations(
    repo: Path,
    base: str,
    head: str,
    acceptance_commands: list[list[str]],
    *,
    runner: Runner | None = None,
    worktree_parent: Path | None = None,
) -> dict[str, Any]:
    """The final_review stage's mutation execution -- the engine-side entry.

    Runs in a one-shot worktree copy at ``head`` (the subject workspace is only
    read, never written): every mechanically enumerated target is deleted in
    turn and the frozen acceptance commands run there. The receipt records each
    target's location and red/green result, plus the mandatory checklists. The
    copy is always removed, including on failure.
    """
    run = runner or _default_runner
    targets = enumerate_mutation_targets(repo, base, head)
    tmp, copy = _one_shot_copy(repo, head, worktree_parent)
    results: list[dict[str, Any]] = []
    try:
        originals: dict[str, str] = {}
        for target in targets:
            source = copy / target.path
            if target.path not in originals:
                originals[target.path] = source.read_text(encoding="utf-8")
            codes = [_target_red(copy, target, argv, run) for argv in acceptance_commands]
            source.write_text(originals[target.path], encoding="utf-8")
            red = any(code != 0 for code in codes)
            results.append(
                {
                    **target.as_dict(),
                    "location": target.location,
                    "removed": True,
                    "acceptance_exit_codes": codes,
                    "acceptance_exit_code": codes[0] if codes else None,
                    "red": red,
                }
            )
    finally:
        _drop_worktree(repo, tmp, copy)

    checked = [
        "targets mechanically enumerated from base..head product diff",
        "each target deleted inside a one-shot worktree copy",
        "frozen acceptance commands run against every mutated copy",
        "subject workspace untouched (copy removed after the experiment)",
    ]
    verified = [target.location for target in targets]
    if not targets:
        # An empty enumeration is a checked fact, not a skipped check: the
        # diff added no production-side call site, so there was nothing to
        # shoot. The checklist still says so -- S12.5 refuses an empty
        # checklist, and "nothing new to mutate" is exactly what was checked.
        verified = [
            "mechanical enumeration over base..head product diff found no new "
            "production-side call sites: nothing to mutate"
        ]
    return {
        "mutation_receipt_version": MUTATION_RECEIPT_VERSION,
        "executor": "final_review",
        "executed_in": "one_shot_copy",
        "subject_workspace_writes": 0,
        "base_commit": base,
        "subject_commit": head,
        "acceptance_commands": [list(argv) for argv in acceptance_commands],
        "targets": results,
        "all_red": all(entry["red"] for entry in results) if results else True,
        CHECKED_ITEMS_FIELD: checked,
        VERIFIED_ITEMS_FIELD: verified,
    }


def _target_key(entry: dict[str, Any]) -> tuple[str, int, str]:
    return (str(entry.get("path") or ""), int(entry.get("line") or 0), str(entry.get("call") or ""))


def verify_mutation_receipt(
    receipt: dict[str, Any] | None,
    expected_targets: list[MutationTarget],
    *,
    acceptance_commands: list[list[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Gate-side verification: the receipt only, never a re-run.

    The receipt is valid when it carries the mandatory checklists, every target
    entry names its location with a red/green result, the target set equals the
    gate's own mechanical enumeration, and every target fell red. Returns
    ``(ok, violations)``; every violation names the missing or disagreeing fact.
    """
    violations: list[str] = []
    if not isinstance(receipt, dict):
        return False, ["receipt is not an object"]
    for field in (CHECKED_ITEMS_FIELD, VERIFIED_ITEMS_FIELD):
        items = receipt.get(field)
        if not isinstance(items, list) or not items:
            violations.append(f"missing or empty {field}")
    if str(receipt.get("executor") or "") != "final_review":
        violations.append("executor must be final_review (the gate never re-runs the gun)")
    if str(receipt.get("executed_in") or "") != "one_shot_copy":
        violations.append("mutations must have executed in a one-shot copy")
    entries = receipt.get("targets")
    if not isinstance(entries, list):
        violations.append("missing targets list")
        entries = []
    observed: list[tuple[str, int, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            violations.append(f"malformed target entry: {entry!r}")
            continue
        key = _target_key(entry)
        observed.append(key)
        if not str(entry.get("path") or "") or not str(entry.get("call") or ""):
            violations.append(f"target entry lacks its location: {entry!r}")
        if "red" not in entry or "removed" not in entry:
            violations.append(f"target entry lacks red/removed facts: {entry!r}")
        elif entry["removed"] is not True:
            violations.append(f"target not actually removed: {entry!r}")
        elif entry["red"] is not True:
            violations.append(f"{MUTATION_TARGET_NOT_RED}: target survived green: {entry!r}")
    expected_keys = [target.key for target in expected_targets]
    if sorted(observed) != sorted(expected_keys):
        missing = sorted(set(expected_keys) - set(observed))
        extra = sorted(set(observed) - set(expected_keys))
        violations.append(
            f"target set != mechanical enumeration (missing {missing}, extra {extra})"
        )
    if receipt.get("all_red") is not True and expected_targets:
        violations.append("all_red is not true")
    if acceptance_commands is not None:
        frozen = [list(argv) for argv in acceptance_commands]
        if [list(argv) for argv in receipt.get("acceptance_commands") or []] != frozen:
            violations.append("receipt's acceptance commands != frozen acceptance commands")
    return not violations, violations


def validate_review_receipt(receipt: dict[str, Any] | None) -> list[str]:
    """S12.5: a review receipt must list what it checked, always.

    ``checked_items`` (or its alias ``verified_items``) is mandatory and must be
    a non-empty list even when ``findings`` is empty -- a passing review with no
    record of what it checked is an invalid receipt. Returns the violations.
    """
    if not isinstance(receipt, dict):
        return ["review receipt is not an object"]
    violations: list[str] = []
    items = receipt.get(CHECKED_ITEMS_FIELD)
    alias = receipt.get(VERIFIED_ITEMS_FIELD)
    effective = items if isinstance(items, list) and items else alias
    if not isinstance(effective, list) or not effective:
        violations.append(
            "missing required checked/verified_items: a review that does not say "
            "what it checked is an invalid receipt, even at findings == 0"
        )
    elif not all(isinstance(item, str) and item.strip() for item in effective):
        violations.append("checked/verified_items entries must be non-empty strings")
    return violations


def static_call_reachable(path: Path, entry: str, target: str) -> bool:
    """D8: is ``target`` reachable from ``entry`` in this module's call graph?

    A static AST walk over the shipped source. Nodes are the module's
    functions *and methods*; an edge is drawn for every referenced name --
    plain names and ``self.method`` attributes alike -- so a call chain that
    crosses methods is followed. Transitively closed from ``entry``. This
    asserts a call-graph property of the shipped source, not that some process
    happens to be running.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            references: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    references.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    references.add(sub.attr)
            # Two nodes may share a name (a module defines several ``act``s);
            # the call graph unions them rather than letting one shadow the
            # other -- a static approximation that errs toward reachability.
            functions.setdefault(node.name, set()).update(references)

    def names_reachable(name: str, seen: set[str]) -> set[str]:
        if name not in functions or name in seen:
            return set()
        seen.add(name)
        referenced = set(functions[name])
        for reference in list(referenced):
            referenced |= names_reachable(reference, seen)
        return referenced

    return target in names_reachable(entry, set())


__all__ = [
    "CHECKED_ITEMS_FIELD",
    "EXCLUDED_DIFF_PREFIXES",
    "MUTATION_EXECUTION_FAILED",
    "MUTATION_RECEIPT_INVALID",
    "MUTATION_RECEIPT_VERSION",
    "MUTATION_TARGET_NOT_RED",
    "TEST_PATH_PATTERN",
    "VERIFIED_ITEMS_FIELD",
    "MutationTarget",
    "enumerate_mutation_targets",
    "execute_final_review_mutations",
    "is_product_path",
    "static_call_reachable",
    "validate_review_receipt",
    "verify_mutation_receipt",
]

"""The production self-gate port: the line's own dd gate, judged mechanically.

The continuous reviews rejected M3 twice for the same defect ("imported by no
production module / exported but never invoked"): the six evidence obligations
existed as modules but no production path ever ran them. This module is the
wiring the reviews demanded -- the concrete :class:`SelfGatePort` that
``build_line`` injects into every goal line (``LineDeps.self_gate``), so a
``dd_awaiting_gate`` wake mechanically performs the six obligations against the
single's own facts and delivers the resulting verdict through the *real*
``deliver_decision`` path (whose principal check remains the S11 authority).

Every fact the six obligations need is read from where production keeps it:

- the single's admission record/status (``dd.get``): the frozen acceptance
  argv, the frozen ``target_base_commit``, the workspace and head commit, and
  the ``dispatched_by`` principal the delivery is authorized against;
- the committed attempt context in the single's worktree
  (``.dev-dispatch/spec/approved.md``): the spec leg of the three-way
  acceptance equality, re-derived from the frozen spec text itself;
- the acceptance stage's receipt (``.dd-evidence/acceptance.json``): the
  receipt leg, exactly the file ``AcceptanceStage`` writes;
- guarded git over the worktree: the changed/deleted paths the diff
  obligations judge;
- real subprocess execution for the re-run, the mutation gun, and the S9
  suite snapshots (baseline anchored at the frozen target base via a
  throwaway ``git worktree``, never the drifting main head).

Failure discipline is fail-closed and never faults the line: a fact that
cannot be produced degrades into an obligation answer that *fails* (a legible
gather note rides the recorded facts), and ``is_pending`` answers False on any
probe failure so a broken probe costs the self-gate turn, never the loop.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fleet_graph.dd.bootstrap import SPEC_PATH
from fleet_graph.dd.control_plane import derive_acceptance_commands
from fleet_graph.dd.git import run_git
from fleet_graph.graphs.self_gate import perform_line_self_gate
from fleet_graph.selfgate import GateEvidenceInputs, SuiteSnapshot

#: The acceptance stage's receipt (``AcceptanceStage`` writes it into the
#: single's worktree): the third leg of the three-way acceptance equality.
ACCEPTANCE_RECEIPT_PATH = ".dd-evidence/acceptance.json"

#: The default suite probe's argv (S9 obligation 6). ``-rA`` makes pytest
#: print one short summary line per test, which is what makes the snapshot's
#: green/failed sets machine-parseable instead of counted-only.
SUITE_PROBE_ARGV: tuple[str, ...] = ("uv", "run", "pytest", "-q", "-rA", "--no-header")

#: Subprocess fences for the real seams (mirrors AcceptanceStage's fence).
ACCEPTANCE_TIMEOUT_SECONDS = 1800
SUITE_TIMEOUT_SECONDS = 3600

#: pytest's terminal summary line, parsed into the snapshot's counts.
_SUITE_SUMMARY = re.compile(
    r"(?P<passed>\d+) passed(?:, (?P<failed>\d+) failed)?(?:, (?P<skipped>\d+) skipped)?"
    r"(?:, (?P<errors>\d+) error)?"
)

_MARKER_DIFF_UNAVAILABLE = "<git-diff-unavailable>"


def suite_snapshot_from_output(output: str) -> SuiteSnapshot:
    """Parse one pytest ``-q -rA`` output into a machine-comparable snapshot.

    The counts come from the terminal summary line; the green/failed sets come
    from the ``PASSED``/``FAILED`` short-summary lines. A summary that cannot
    be parsed yields an all-zero snapshot whose green set is empty -- the
    comparison then fail-closes on any head failure instead of guessing green.
    """
    passed = failed = skipped = errors = 0
    match = _SUITE_SUMMARY.search(output)
    if match:
        passed = int(match.group("passed") or 0)
        failed = int(match.group("failed") or 0)
        skipped = int(match.group("skipped") or 0)
        errors = int(match.group("errors") or 0)
    green: set[str] = set()
    red: set[str] = set()
    for line in output.splitlines():
        token, _, rest = line.strip().partition(" ")
        if rest and token in ("PASSED", "FAILED"):
            (green if token == "PASSED" else red).add(rest.split(" - ", 1)[0].strip())
    total = passed + failed + skipped + errors
    return SuiteSnapshot(
        passed=passed,
        failed=failed + errors,
        total=total,
        failed_tests=frozenset(red),
        skipped=skipped,
        green_tests=frozenset(green),
    )


def subprocess_suite_probe(
    workspace: Path, *, timeout: int = SUITE_TIMEOUT_SECONDS
) -> SuiteSnapshot | None:
    """The real S9 probe: run the full suite in ``workspace``, parse the snap."""
    try:
        proc = subprocess.run(
            list(SUITE_PROBE_ARGV),
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return suite_snapshot_from_output((proc.stdout or "") + (proc.stderr or ""))


def baseline_suite_probe(
    workspace: Path, target_base_commit: str, *, timeout: int = SUITE_TIMEOUT_SECONDS
) -> SuiteSnapshot | None:
    """The S9 baseline: the full suite at the *frozen* target base, not main.

    A throwaway ``git worktree`` of ``target_base_commit`` gives the probe a
    clean, unpatched tree; it is removed afterwards. Any failure yields None,
    which the regression obligation answers as ``missing_baseline`` -- a
    refusal, never a guessed baseline.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="m3-baseline-") as tmp:
            base_tree = Path(tmp) / "base"
            proc = run_git(
                workspace, "worktree", "add", "--detach", str(base_tree), target_base_commit
            )
            if proc.returncode != 0:
                return None
            try:
                return subprocess_suite_probe(base_tree, timeout=timeout)
            finally:
                run_git(workspace, "worktree", "remove", "--force", str(base_tree))
    except Exception:
        return None


def acceptance_receipt_commands(workspace: Path) -> tuple[list[list[str]], str | None]:
    """The receipt leg: the commands the acceptance stage actually ran.

    Reads ``.dd-evidence/acceptance.json`` -- the artifact the pipeline's own
    acceptance stage writes -- and returns its per-command argv. The second
    element is a gather note when the receipt cannot be read (the obligation
    then fail-closes on the empty leg).
    """
    path = workspace / ACCEPTANCE_RECEIPT_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], f"acceptance receipt unreadable at {ACCEPTANCE_RECEIPT_PATH}"
    if not isinstance(raw, dict):
        return [], f"acceptance receipt at {ACCEPTANCE_RECEIPT_PATH} is not an object"
    results = raw.get("results")
    if not isinstance(results, list):
        return [], f"acceptance receipt at {ACCEPTANCE_RECEIPT_PATH} has no results"
    commands = [
        [str(word) for word in (entry.get("command") or [])]
        for entry in results
        if isinstance(entry, dict)
    ]
    return commands, None


def default_mutations() -> list[Callable[[bytes], bytes]]:
    """Two deterministic product mutations the frozen acceptance must red.

    Both are byte-breaking for any structured file (a truncated head, a
    doubled newline after the first line), so a green acceptance after either
    shot means the acceptance does not bite -- exactly what obligation 5
    exists to catch. Restore is byte-exact and sha/mode-verified by
    ``mutation_gun`` itself.
    """

    def shot_one(original: bytes) -> bytes:
        return original[:1]

    def shot_two(original: bytes) -> bytes:
        head, _, tail = original.partition(b"\n")
        return head + b"\n\n" + tail if tail else original + b"\n\n"

    return [shot_one, shot_two]


class DdGateSelfGatePort:
    """The concrete production ``SelfGatePort`` (goal_line's Protocol).

    One port owns one dd anchor: the single this line dispatched and whose
    gate it is entitled to judge (``decided_by == dispatched_by`` is enforced
    again by the delivery path itself -- the S11 single authority). Wiring it
    into ``LineDeps.self_gate`` (``build_line``) is what makes the line
    self-gate the fleet default: on a wake the graph's ``self_gate`` node
    calls :meth:`perform`, which gathers -> decides -> delivers.

    The execution seams (``acceptance_runner`` / ``suite_probe`` /
    ``baseline_probe`` / ``mutation_accept``) default to real subprocess
    execution against the single's workspace; tests inject scripted seams to
    keep the suite fast, exactly like every other port in this codebase.
    """

    def __init__(
        self,
        *,
        line_id: str,
        development_id: str,
        dd: Any,
        run_root: Path,
        declared_paths: tuple[str, ...] = (),
        reason: str = "line self-gate (M3): the six evidence obligations",
        acceptance_runner: Callable[[list[str]], tuple[int, str]] | None = None,
        mutation_accept: Callable[[], int] | None = None,
        suite_probe: Callable[[Path], SuiteSnapshot | None] | None = None,
        baseline_probe: Callable[[Path, str], SuiteSnapshot | None] | None = None,
        mutations: list[Callable[[bytes], bytes]] | None = None,
        acceptance_timeout: int = ACCEPTANCE_TIMEOUT_SECONDS,
        suite_timeout: int = SUITE_TIMEOUT_SECONDS,
    ) -> None:
        self.line_id = line_id
        self.development_id = development_id
        self.dd = dd
        #: The *runs root* (the line run root's parent): the delivery's wake
        #: fact must land in ``<runs>/.scheduler/<line>.json``, the same file
        #: the scheduler's park and the decision bridge read.
        self.run_root = Path(run_root)
        self.declared_paths = tuple(declared_paths)
        self.reason = reason
        self.acceptance_timeout = acceptance_timeout
        self.suite_timeout = suite_timeout
        self._acceptance_runner = acceptance_runner
        self._mutation_accept = mutation_accept
        self._suite_probe = suite_probe
        self._baseline_probe = baseline_probe
        self._mutations = mutations if mutations is not None else default_mutations()
        self.notes: list[str] = []

    # --- SelfGatePort protocol -------------------------------------------

    def is_pending(self) -> bool:
        """Is this line's dd single sitting at its gate right now?

        The live fact, not the wake event: the scheduler may wake the line,
        but the gate turn only runs while the single is actually
        ``awaiting_gate``. A probe failure is *not pending* (fail-open for the
        line's ordinary loop; the scheduler's own wake discipline still owns
        ignition) -- a broken probe must never fault the line.
        """
        try:
            status = self.dd.get(self.development_id)
        except Exception:
            return False
        return str((status or {}).get("state") or "") == "awaiting_gate"

    def perform(self) -> dict[str, Any]:
        """Gather the six obligations from the single's own facts, then deliver.

        The return value is the orchestration's structured result (evidence +
        verdict + delivery answer) plus this port's ``gather_notes`` -- the
        engine facts the graph records into ``last_self_gate`` for the
        coordinator to weigh, never agent prose.
        """
        self.notes = []
        status = self.dd.get(self.development_id) or {}
        workspace = Path(str(status.get("repo_path") or status.get("worktree_path") or ""))
        base = str(status.get("target_base_commit") or "")
        head = str(status.get("head_commit") or "")
        record_argv = [
            [str(word) for word in command] for command in (status.get("acceptance_commands") or [])
        ]

        spec_argv = self._spec_leg(workspace)
        receipt_argv, receipt_note = acceptance_receipt_commands(workspace)
        if receipt_note:
            self.notes.append(receipt_note)

        changed, changed_note = self._changed_paths(workspace, base, head)
        if changed_note:
            self.notes.append(changed_note)

        inputs = GateEvidenceInputs(
            spec_argv=spec_argv,
            record_argv=record_argv,
            receipt_argv=receipt_argv,
            changed_paths=changed,
            declared_paths=list(self.declared_paths),
            deleted_paths=self._deleted_test_paths(workspace, base, head),
            acceptance_commands=record_argv,
            acceptance_runner=self._runner(workspace),
            mutation_target=self._mutation_target(workspace, changed),
            mutations=self._mutations,
            mutation_accept=self._mut_accept(workspace),
            target_base_commit=base,
            baseline=self._baseline(workspace, base),
            head=self._head_snapshot(workspace),
            main_head_commit=self._main_head(workspace),
        )
        result = perform_line_self_gate(
            development_id=self.development_id,
            principal=self.line_id,
            inputs=inputs,
            deliver=self._deliver,
        )
        facts = result.as_dict()
        if self.notes:
            facts["gather_notes"] = list(self.notes)
        return facts

    # --- fact gatherers (fail-closed) -------------------------------------

    def _spec_leg(self, workspace: Path) -> list[list[str]]:
        """Obligation 1's spec leg: re-derived from the committed frozen spec."""
        try:
            spec_text = (workspace / SPEC_PATH).read_text(encoding="utf-8")
        except OSError:
            self.notes.append(f"spec unreadable in the single's worktree at {SPEC_PATH}")
            return []
        return derive_acceptance_commands(spec_text.encode("utf-8"))

    def _changed_paths(self, workspace: Path, base: str, head: str) -> tuple[list[str], str | None]:
        if not base or not head:
            return (
                [_MARKER_DIFF_UNAVAILABLE],
                "head/target_base unavailable; the product diff cannot be read",
            )
        proc = run_git(workspace, "diff", "--name-only", f"{base}..{head}")
        if proc.returncode != 0:
            return (
                [_MARKER_DIFF_UNAVAILABLE],
                f"git diff failed: {(proc.stderr or proc.stdout).strip()[:200]}",
            )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()], None

    def _deleted_test_paths(self, workspace: Path, base: str, head: str) -> list[str]:
        if not base or not head:
            return []
        proc = run_git(workspace, "diff", "--diff-filter=D", "--name-only", f"{base}..{head}")
        if proc.returncode != 0:
            self.notes.append(
                f"git diff --diff-filter=D failed: {(proc.stderr or proc.stdout).strip()[:200]}"
            )
            return ["tests/<diff-unavailable>"]
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _mutation_target(self, workspace: Path, changed: list[str]) -> Path | None:
        """A product file the gun may fire on.

        The gun's question is "does the frozen acceptance actually bite?" --
        it prefers a file the single changed, but any product file in the
        worktree answers it. Only a worktree with no product file at all has
        no target, and that is a recorded refusal, never a silent pass.
        """
        for relative in changed:
            if relative.startswith(".dev-dispatch/") or relative.startswith(".dd-evidence/"):
                continue
            candidate = workspace / relative
            if candidate.is_file():
                return candidate
        if workspace.is_dir():
            for candidate in sorted(workspace.iterdir()):
                if candidate.is_file() and not candidate.name.startswith("."):
                    return candidate
        self.notes.append("no product file in the single's worktree is mutable")
        return None

    def _runner(self, workspace: Path) -> Callable[[list[str]], tuple[int, str]]:
        if self._acceptance_runner is not None:
            return self._acceptance_runner

        def run(argv: list[str]) -> tuple[int, str]:
            try:
                proc = subprocess.run(
                    [str(word) for word in argv],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=self.acceptance_timeout,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return 1, f"{type(exc).__name__}: {exc}"
            return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-4000:]

        return run

    def _mut_accept(self, workspace: Path) -> Callable[[], int]:
        if self._mutation_accept is not None:
            return self._mutation_accept
        runner = self._runner(workspace)
        record_argv = self._record_argv_cached

        def accept() -> int:
            code = 0
            for argv in record_argv:
                exit_code, _ = runner(argv)
                if exit_code != 0:
                    code = exit_code or 1
            return code

        return accept

    @property
    def _record_argv_cached(self) -> list[list[str]]:
        try:
            status = self.dd.get(self.development_id) or {}
        except Exception:
            return []
        return [
            [str(word) for word in command] for command in (status.get("acceptance_commands") or [])
        ]

    def _baseline(self, workspace: Path, base: str) -> SuiteSnapshot | None:
        if self._baseline_probe is not None:
            return self._baseline_probe(workspace, base)
        if not base:
            return None
        return baseline_suite_probe(workspace, base, timeout=self.suite_timeout)

    def _head_snapshot(self, workspace: Path) -> SuiteSnapshot | None:
        if self._suite_probe is not None:
            return self._suite_probe(workspace)
        return subprocess_suite_probe(workspace, timeout=self.suite_timeout)

    def _main_head(self, workspace: Path) -> str | None:
        """The current main head -- recorded into the verdict, never consulted.

        S9 anchors the comparison at the frozen target base alone; carrying
        the drifted main head (when it even exists) makes the ignore explicit
        and machine-visible (``ignored_main_head_commit``).
        """
        try:
            proc = run_git(workspace, "rev-parse", "main")
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    # --- the delivery seam (production) ------------------------------------

    def _deliver(self, decision: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """The real M2 delivery: one principal-checked dd gate resume.

        Imported here, not at module top, so the port (and every line built
        with one) never pays the MCP transport import -- and so tests that
        build the port without exercising the delivery stay transport-free.
        """
        from fleet_graph.decision_mcp import deliver_decision

        return deliver_decision(
            line="",
            decision=decision,
            reason=self.reason,
            principal=self.line_id,
            run_root=self.run_root,
            lines=[],
            target_kind="dd",
            target_id=self.development_id,
            dd=self.dd,
            evidence=evidence,
        ).as_dict()


def build_line_self_gate(
    *,
    line_id: str,
    development_id: str,
    dd_root: Path | None = None,
    run_root: Path,
    declared_paths: tuple[str, ...] = (),
) -> DdGateSelfGatePort | None:
    """Build the production port for one line anchor, or None when it cannot.

    The ``build_line`` wiring point's fail-soft posture (mirrors
    ``_build_interrupt``): a line whose anchor exists but whose control plane
    cannot be built starts unchanged -- the self-gate is wired the moment the
    machinery is available, and never bricks a line.
    """
    try:
        from fleet_graph.dd.control_plane import DEFAULT_DD_ROOT, DdControlPlane

        dd = DdControlPlane(root=Path(dd_root) if dd_root is not None else DEFAULT_DD_ROOT)
    except Exception:
        return None
    return DdGateSelfGatePort(
        line_id=line_id,
        development_id=development_id,
        dd=dd,
        run_root=run_root,
        declared_paths=tuple(declared_paths),
    )


__all__ = [
    "ACCEPTANCE_RECEIPT_PATH",
    "ACCEPTANCE_TIMEOUT_SECONDS",
    "SUITE_PROBE_ARGV",
    "SUITE_TIMEOUT_SECONDS",
    "DdGateSelfGatePort",
    "acceptance_receipt_commands",
    "baseline_suite_probe",
    "build_line_self_gate",
    "default_mutations",
    "subprocess_suite_probe",
    "suite_snapshot_from_output",
]

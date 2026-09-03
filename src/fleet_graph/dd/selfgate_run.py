"""Mechanical execution of the three performable self-gate obligations.

``selfgate.py`` holds the pure judgement, ``selfgate_flow.py`` the orchestration.
This module is the *executor* -- the engine code that actually performs spec
§2.4 / §2.5 / §2.6 rather than reading them off a record that no one wrote:

- :meth:`SelfGateExecutor.rerun_acceptance` re-runs the frozen acceptance argv
  in the single's worktree and keeps the ``{argv, exit_code}`` transcript
  (§2.4 -- "线在 gate 侧亲自复跑冻结验收命令并留回显").
- :meth:`SelfGateExecutor.fire_mutation_gun` applies two byte-mutations to the
  product, re-runs the frozen acceptance against each (each must turn it red),
  and restores the bytes with a byte + mode verification (§2.5 -- "射后字节复原
  (sha/mode 校验)").
- :meth:`SelfGateExecutor.full_regression` runs the full suite once on the
  *frozen* target base and once on the head, returning the two
  machine-comparable ``{passed, failed, skipped, failed_set}`` tuples (§2.6).

The trust rule is the acceptance module's: the only argv this module runs is the
frozen acceptance the operator declared -- read off the admission record, never
off a file the agent wrote -- and every command runs in the single's own worktree
(or a private checkout of its frozen base), never in the engine's working
directory. A measurement that cannot be made is reported as ``ok: False`` by the
gatherer, never guessed green.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git

#: How many mutations the mutation-gun obligation fires. Mirrors
#: ``selfgate.MUTATION_GUN_SHOTS``; kept local so the executor never imports the
#: judgement module (execution stays judgement-free).
MUTATION_GUN_SHOTS = 2

#: The full regression argv the spec §2.6 names ("跑 ``uv run pytest -q`` 全量").
DEFAULT_REGRESSION_ARGV = ("uv", "run", "pytest", "-q")

#: Synthetic exit code for a command that timed out or could not start, matching
#: the ``acceptance`` module's ``EXIT_TIMEOUT`` / ``EXIT_NOT_FOUND`` conventions.
EXIT_ERROR = 127


def parse_pytest_summary(output: str) -> dict[str, Any]:
    """Parse ``pytest -q`` output into the machine-comparable run tuple.

    Returns ``{passed, failed, skipped, failed_set}`` where ``failed_set`` is the
    sorted list of red test ids from the short-test-summary section (``-q`` still
    emits ``FAILED <test-id>`` lines when a run is red -- measured). A line the
    parser cannot confidently reduce is left out rather than guessed: the
    comparison is over a red *set*, so a missed red is a missed regression, and
    an empty parse is a count tuple of zeros, not a guess.
    """
    passed = failed = skipped = 0
    summary_lines = [
        line
        for line in output.splitlines()
        if re.search(r"\d+\s+(?:passed|failed|skipped|error|errors).+?\bin\s+\d+(?:\.\d+)?s", line)
    ]
    if summary_lines:
        for count, kind in re.findall(
            r"(\d+)\s*(passed|failed|skipped|error|errors)", summary_lines[0]
        ):
            value = int(count)
            if kind in ("failed", "error", "errors"):
                failed += value
            elif kind == "passed":
                passed += value
            elif kind == "skipped":
                skipped += value
    failed_set: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^(?:FAILED|ERROR)\s+(\S+)", line.strip())
        if match:
            failed_set.append(match.group(1))
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "failed_set": sorted(set(failed_set)),
    }


def _clean_environment() -> dict[str, str]:
    """PATH + HOME only, with proxy variables explicitly stripped.

    The S6 clean-proxy rule and ``acceptance.ENV_KEEP`` give the same whitelist:
    nothing secret and nothing agent-authored leaks into a command that grades
    the work. Running under ``uv run`` needs only PATH on top of that.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    for proxy in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        env.pop(proxy, None)
    return env


class SelfGateExecutor:
    """The production executor of §2.4 / §2.5 / §2.6, with injectable seams.

    ``run`` is the subprocess primitive (defaults to ``subprocess.run``); ``git``
    the guarded git primitive (defaults to :func:`fleet_graph.dd.git.run_git`).
    Both are seams so the gatherer's tests can prove the executor is *called*
    without shelling out.
    """

    def __init__(
        self,
        *,
        run: Callable[..., Any] = subprocess.run,
        git: Callable[..., Any] = run_git,
        timeout_seconds: int = 1800,
    ) -> None:
        self._run = run
        self._git = git
        self._timeout = timeout_seconds

    # --- §2.4 ------------------------------------------------------------

    def rerun_acceptance(self, argvs: list[list[str]], *, cwd: str) -> list[dict[str, Any]]:
        """Re-run the frozen acceptance argv in ``cwd``; return ``{argv, exit_code}``.

        Every command runs regardless of what the previous one returned, matching
        the acceptance step's "report everything" rule. A command that cannot
        start is ``exit_code`` = :data:`EXIT_ERROR` -- never a guessed zero.
        """
        runs: list[dict[str, Any]] = []
        for raw in argvs:
            argv = [str(part) for part in raw]
            exit_code = self._execute(argv, cwd)
            runs.append({"argv": argv, "exit_code": exit_code})
        return runs

    # --- §2.5 ------------------------------------------------------------

    def fire_mutation_gun(
        self,
        argvs: list[list[str]],
        *,
        cwd: str,
        product_paths: list[str],
        shots: int = MUTATION_GUN_SHOTS,
    ) -> list[dict[str, Any]]:
        """Two mutations -> frozen acceptance must red -> byte-for-byte restore.

        Each shot mutates one product file (append a trailing marker byte), re-runs
        the frozen acceptance against the mutated tree, records whether it turned
        red, then restores the original bytes and file mode and verifies both the
        bytes and the mode. A shot whose file cannot be read is reported with
        ``red=False``/``restored=False`` so the judgement refuses rather than
        fabricating a green mutation.
        """
        results: list[dict[str, Any]] = []
        for path in self._mutation_targets(product_paths, cwd, shots):
            results.append(self._one_shot(argvs, cwd, path))
        return results

    def _one_shot(self, argvs: list[list[str]], cwd: str, path: str) -> dict[str, Any]:
        full = Path(cwd) / path
        try:
            original = full.read_bytes()
            mode = os.stat(full).st_mode
        except OSError as exc:
            return {"path": path, "red": False, "restored": False, "reason": str(exc)}
        try:
            full.write_bytes(original + b"\n# fleet-graph self-gate mutation\n")
            red = self._acceptance_any_nonzero(argvs, cwd)
        finally:
            full.write_bytes(original)
            os.chmod(full, stat.S_IMODE(mode))
        return {"path": path, "red": red, "restored": self._bytes_restored(full, original, mode)}

    def _bytes_restored(self, full: Path, original: bytes, mode: int) -> bool:
        try:
            current_mode = os.stat(full).st_mode
            current_bytes = full.read_bytes()
        except OSError:
            return False
        return current_bytes == original and stat.S_IMODE(current_mode) == stat.S_IMODE(mode)

    def _mutation_targets(self, product_paths: list[str], cwd: str, shots: int) -> list[str]:
        targets: list[str] = []
        for raw in product_paths:
            if len(targets) >= shots:
                break
            path = str(raw).strip().lstrip("/")
            if not path:
                continue
            full = Path(cwd) / path
            try:
                if full.is_file() and not full.is_symlink():
                    targets.append(path)
            except OSError:
                continue
        return targets

    def _acceptance_any_nonzero(self, argvs: list[list[str]], cwd: str) -> bool:
        return any(self._execute([str(p) for p in argv], cwd) != 0 for argv in argvs)

    # --- §2.6 ------------------------------------------------------------

    def full_regression(
        self,
        *,
        repo: str,
        base_commit: str,
        head_commit: str,
        test_argv: tuple[str, ...] = DEFAULT_REGRESSION_ARGV,
    ) -> dict[str, Any]:
        """Run the full suite on the frozen base and on the head; return both tuples.

        ``baseline_run`` is measured on a *private* checkout of ``base_commit``
        (the frozen ``target_base_commit``, never the drifted main -- the S9
        anchor), ``head_run`` on the single's worktree itself (already at
        ``head_commit``). The two are the machine-comparable pair
        :func:`fleet_graph.dd.selfgate.regression_ok` weighs.
        """
        return {
            "baseline_run": self._regression_on_base(repo, base_commit, test_argv),
            "head_run": self._regression_summary([list(test_argv)], cwd=repo),
        }

    def _regression_on_base(
        self, repo: str, base_commit: str, test_argv: tuple[str, ...]
    ) -> dict[str, Any]:
        checkout: str | None = None
        try:
            checkout = tempfile.mkdtemp(prefix="selfgate-base-")
            proc = self._git(repo, "worktree", "add", "--detach", "--quiet", checkout, base_commit)
            if proc.returncode != 0:
                return self._empty_run("baseline checkout refused")
            return self._regression_summary([list(test_argv)], cwd=checkout)
        except Exception as exc:
            return self._empty_run(f"baseline regression unavailable: {type(exc).__name__}: {exc}")
        finally:
            if checkout:
                self._git(repo, "worktree", "remove", "--force", checkout)

    def _regression_summary(self, argv: list[list[str]], *, cwd: str) -> dict[str, Any]:
        if not argv:
            return self._empty_run("no regression argv declared")
        return parse_pytest_summary(self._capture(argv[0], cwd))

    def _empty_run(self, reason: str) -> dict[str, Any]:
        return {"passed": 0, "failed": 0, "skipped": 0, "failed_set": [], "reason": reason}

    # --- primitives ------------------------------------------------------

    def _execute(self, argv: list[str], cwd: str) -> int:
        try:
            proc = self._run(
                argv,
                cwd=cwd,
                env=_clean_environment(),
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            return int(proc.returncode)
        except FileNotFoundError:
            return EXIT_ERROR
        except subprocess.TimeoutExpired:
            return EXIT_ERROR

    def _capture(self, argv: list[str], cwd: str) -> str:
        try:
            proc = self._run(
                argv,
                cwd=cwd,
                env=_clean_environment(),
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return ""
        return f"{proc.stdout or ''}\n{proc.stderr or ''}"


__all__ = [
    "DEFAULT_REGRESSION_ARGV",
    "EXIT_ERROR",
    "MUTATION_GUN_SHOTS",
    "SelfGateExecutor",
    "parse_pytest_summary",
]

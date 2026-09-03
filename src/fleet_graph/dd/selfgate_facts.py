"""The engine-side gatherer of the six line self-gate obligations (spec §2).

``dd/selfgate_flow.py`` declares the :class:`SelfGateFacts` protocol and the
``run_line_selfgate`` caller, but a protocol is not a measurement. This module is
the *production* gatherer the engine wires when a line is woken by
``dd_awaiting_gate``, and it does not fabricate a single positive answer: each
fact is either read off the single's committed state or -- for the three
performable obligations -- *performed* against the single's worktree via
:class:`fleet_graph.dd.selfgate_run.SelfGateExecutor` (spec §2.4 re-run, §2.5
mutation gun, §2.6 full regression). An obligation whose facts are absent and
cannot be measured is reported ``ok: False`` with the reason, never a guessed
green (the "refuse on missing" rule the whole gate stands on).

The gatherer touches nothing mutable: git is read through the guarded
:func:`fleet_graph.dd.git.run_git` corridor, and the executor's chance to write
is confined to the single's own worktree (and its private base checkout), so
judgement stays out of the write path (INV-3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.dd import selfgate
from fleet_graph.dd.git import run_git
from fleet_graph.dd.selfgate_run import SelfGateExecutor

#: The spec's declared delivery surface, as a directory-prefix scope, when the
#: read model does not carry an explicit ``scope_paths``. The fleet-graph "仓"
#: surface is its product code + config + scripts; a product change landing
#: anywhere else (docs, deployment ops, top-level prose) misses this surface and
#: is refused by ``product_diff_in_scope``. Trailing ``/`` marks a directory
#: prefix, which is the honest shape of a prose-declared surface.
PRODUCT_SCOPE_ROOTS: tuple[str, ...] = ("src/", "tests/", "config/", "scripts/")

#: The acceptance stage's committed receipt (``.dd-evidence/acceptance.json``)
#: recorded commands are the "阶段回执 command" of spec §2.1's three-way compare.
ACCEPTANCE_RECORD_PATH = ".dd-evidence/acceptance.json"


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class EngineSelfGateFacts:
    """The production :class:`SelfGateFacts` gatherer, bound to a dd control plane.

    ``executor`` is the :class:`~fleet_graph.dd.selfgate_run.SelfGateExecutor`
    that performs §2.4 / §2.5 / §2.6; a test injects a fake to prove the gatherer
    *calls* it, while production uses the real subprocess/git-backed executor.
    """

    def __init__(self, control: Any, *, executor: SelfGateExecutor | None = None) -> None:
        self._control = control
        self._executor = executor

    def _get(self, development_id: str) -> dict[str, Any]:
        info = self._control.get(development_id)
        return info if isinstance(info, dict) else {}

    def _perform(self) -> SelfGateExecutor:
        if self._executor is None:
            self._executor = SelfGateExecutor()
        return self._executor

    def _worktree(self, info: dict[str, Any]) -> str:
        for key in ("worktree_path", "repo_path", "workspace"):
            value = str(info.get(key) or "")
            if value and Path(value).is_dir():
                return value
        return ""

    def _acceptance_argvs(self, info: dict[str, Any]) -> list[list[str]]:
        """The frozen acceptance commands as argv lists.

        The control plane's ``acceptance_commands`` is ``[argv, ...]`` (a list of
        lists). A duck-typed read model may carry a single flat argv instead; that
        is normalised to one command so the executor still re-runs exactly it.
        """
        commands = _list(info.get("acceptance_commands"))
        if not commands:
            return []
        if all(isinstance(command, (list, tuple)) for command in commands):
            return [[str(part) for part in command] for command in commands]
        return [[str(part) for part in commands]]

    def _scope_paths(self, info: dict[str, Any]) -> list[str]:
        declared = info.get("scope_paths")
        if declared is not None:
            return [str(path) for path in _list(declared)]
        return list(PRODUCT_SCOPE_ROOTS)

    def _receipt_commands(self, info: dict[str, Any]) -> list[str]:
        """The committed acceptance stage's recorded commands (spec §2.1 receipt)."""
        provided = info.get("verification_commands")
        if provided is not None:
            return [str(command) for command in _list(provided)]
        repo, _, head = self._git_anchors(info)
        if not repo or not head:
            return []
        try:
            proc = run_git(repo, "show", f"{head}:{ACCEPTANCE_RECORD_PATH}")
        except Exception:
            return []
        if proc.returncode != 0:
            return []
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        return [
            str(entry["command"])
            for entry in payload.get("results") or []
            if isinstance(entry, dict) and entry.get("command")
        ]

    # --- measured members (one per obligation) ---------------------------

    def _acceptance_verbatim(self, info: dict[str, Any]) -> dict[str, Any]:
        spec_argv = _list(info.get("spec_acceptance_commands") or info.get("acceptance_commands"))
        record = _list(info.get("acceptance_commands"))
        return selfgate.acceptance_argv_verbatim(
            spec=spec_argv,
            record=record,
            receipt=self._receipt_commands(info),
        )

    def _git_changed(self, info: dict[str, Any]) -> list[str]:
        """The product files changed base..head, read from the worktree when git
        can answer. A read failure yields the read model's ``changed_paths`` (an
        already-measured transcript) or an empty list -- which the other facts
        (base/head presence) still validate elsewhere."""
        repo, base, head = self._git_anchors(info)
        if not repo or not base or not head:
            return [str(path) for path in _list(info.get("changed_paths"))]
        try:
            proc = run_git(repo, "diff", "--name-only", f"{base}..{head}")
            if proc.returncode != 0:
                return [str(path) for path in _list(info.get("changed_paths"))]
            return [line for line in proc.stdout.splitlines() if line.strip()]
        except Exception:
            return [str(path) for path in _list(info.get("changed_paths"))]

    def _git_deleted(self, info: dict[str, Any]) -> list[str]:
        repo, base, head = self._git_anchors(info)
        if not repo or not base or not head:
            return [str(path) for path in _list(info.get("deleted_paths"))]
        try:
            proc = run_git(repo, "diff", "--diff-filter=D", "--name-only", f"{base}..{head}")
            if proc.returncode != 0:
                return [str(path) for path in _list(info.get("deleted_paths"))]
            return [line for line in proc.stdout.splitlines() if line.strip()]
        except Exception:
            return [str(path) for path in _list(info.get("deleted_paths"))]

    def _git_anchors(self, info: dict[str, Any]) -> tuple[str, str, str]:
        repo = self._worktree(info)
        base = str(info.get("target_base_commit") or "")
        head = str(info.get("head_commit") or "")
        if not repo:
            return "", "", ""
        return repo, base, head

    def _product_diff(self, info: dict[str, Any]) -> dict[str, Any]:
        return selfgate.product_diff_in_scope(
            changed_paths=self._git_changed(info),
            scope_paths=self._scope_paths(info),
        )

    def _zero_deletion(self, info: dict[str, Any]) -> dict[str, Any]:
        return selfgate.zero_test_deletion(deleted_paths=self._git_deleted(info))

    def _personally_ran(self, info: dict[str, Any]) -> dict[str, Any]:
        runs = info.get("acceptance_runs")
        if runs is None:
            worktree = self._worktree(info)
            argvs = self._acceptance_argvs(info)
            runs = self._perform().rerun_acceptance(argvs, cwd=worktree) if worktree else []
        return selfgate.personally_ran_acceptance(runs=_list(runs))

    def _mutation_gun(self, info: dict[str, Any]) -> dict[str, Any]:
        mutations = info.get("mutations")
        if mutations is None:
            worktree = self._worktree(info)
            argvs = self._acceptance_argvs(info)
            mutations = (
                self._perform().fire_mutation_gun(
                    argvs, cwd=worktree, product_paths=self._git_changed(info)
                )
                if worktree
                else []
            )
        return selfgate.mutation_gun_satisfied(mutations=_list(mutations))

    def _regression(self, info: dict[str, Any]) -> dict[str, Any]:
        base_run = info.get("baseline_run")
        head_run = info.get("head_run")
        if base_run is None or head_run is None:
            worktree = self._worktree(info)
            base_commit = str(info.get("target_base_commit") or "")
            head_commit = str(info.get("head_commit") or "")
            if worktree and base_commit:
                both = self._perform().full_regression(
                    repo=worktree,
                    base_commit=base_commit,
                    head_commit=head_commit,
                )
                base_run = both.get("baseline_run")
                head_run = both.get("head_run")
        base_commit = str(info.get("target_base_commit") or "")
        compared = str(info.get("compared_base_commit") or base_commit)
        if not isinstance(base_run, dict) or not isinstance(head_run, dict):
            return {
                "ok": False,
                "reason": "regression baseline missing: no machine-comparable runs",
            }
        attribution: list[selfgate.FlakeAttribution] = []
        for entry in _list(info.get("flake_attribution")):
            if isinstance(entry, dict):
                attribution.append(
                    selfgate.FlakeAttribution(
                        test_id=str(entry.get("test_id") or ""),
                        red_count=int(entry.get("red_count") or 0),
                        clean_base_reruns=int(entry.get("clean_base_reruns") or 0),
                    )
                )
        return selfgate.regression_ok(
            base=_run(base_run),
            head=_run(head_run),
            base_commit=base_commit,
            compared_base_commit=compared,
            flake_attribution=attribution,
        )

    def gather(self, development_id: str) -> dict[str, Any]:
        info = self._get(development_id)
        return {
            "acceptance_verbatim": self._acceptance_verbatim(info),
            "product_diff_in_scope": self._product_diff(info),
            "zero_test_deletion": self._zero_deletion(info),
            "personally_ran_acceptance": self._personally_ran(info),
            "mutation_gun": self._mutation_gun(info),
            "regression_baseline": self._regression(info),
        }


def _run(value: dict[str, Any]) -> selfgate.RegressionRun:
    failed_set = frozenset(str(t) for t in _list(value.get("failed_set")))
    return selfgate.RegressionRun(
        passed=int(value.get("passed") or 0),
        failed=int(value.get("failed") or 0),
        skipped=int(value.get("skipped") or 0),
        failed_set=failed_set,
    )


__all__ = ["PRODUCT_SCOPE_ROOTS", "EngineSelfGateFacts"]

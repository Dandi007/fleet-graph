"""The engine-side gatherer of the six line self-gate obligations (spec §2).

``dd/selfgate_flow.py`` declares the :class:`SelfGateFacts` protocol and the
``run_line_selfgate`` caller, but a protocol is not a measurement: this module
is the *production* gatherer the engine wires when a line is woken by
``dd_awaiting_gate``. It turns a dd development's committed state into the six
``{"ok": bool, ...}`` facts ``selfgate.assess_evidence`` weighs, without
fabricating a single positive answer.

Measurement is deliberately mechanical and closed:

- ``acceptance_verbatim`` -- the frozen spec argv, the record's
  ``acceptance_commands`` and the stage receipt command are compared byte-wise
  (:func:`fleet_graph.dd.selfgate.acceptance_argv_verbatim`). None of the three
  may be absent.
- ``product_diff_in_scope`` / ``zero_test_deletion`` -- the product file
  changes and the deleted files between the frozen ``target_base_commit`` and
  the head commit are read from the worktree with guarded git
  (:func:`fleet_graph.dd.git.run_git`), then judged against the spec's declared
  surface (:func:`product_diff_in_scope`, :func:`zero_test_deletion`).
- ``personally_ran_acceptance``, ``mutation_gun``, ``regression_baseline`` --
  the implementer-produced transcripts. They arrive through the control plane's
  read surface; an obligation whose facts are absent is reported ``ok: False``
  with the reason, never a guessed green (the "refuse on missing" rule the whole
  gate stands on).

The gatherer touches nothing mutable, so a line can weigh these facts without
any write side-effect (INV-3): judgement stays out of the write path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleet_graph.dd.selfgate import (
    FlakeAttribution,
    RegressionRun,
    acceptance_argv_verbatim,
    mutation_gun_satisfied,
    personally_ran_acceptance,
    product_diff_in_scope,
    regression_ok,
    zero_test_deletion,
)

#: The spec's declared delivery surface, when the read model does not carry one.
#: Empty means "no product changes admitted" -- a diff that adds anything
#: outside protocol machinery then refuses, which is the safe reading of a
#: surface that was never declared.
_EMPTY: tuple[()] = ()


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class EngineSelfGateFacts:
    """The production :class:`SelfGateFacts` gatherer, bound to a dd control plane."""

    def __init__(self, control: Any) -> None:
        self._control = control

    def _get(self, development_id: str) -> dict[str, Any]:
        info = self._control.get(development_id)
        return info if isinstance(info, dict) else {}

    # --- measured members (one per obligation) ---------------------------

    def _acceptance_verbatim(self, info: dict[str, Any]) -> dict[str, Any]:
        spec = _list(info.get("spec_acceptance_commands") or info.get("acceptance_commands"))
        record = _list(info.get("acceptance_commands"))
        receipt = _list(info.get("verification_commands"))
        return acceptance_argv_verbatim(spec=spec, record=record, receipt=receipt)

    def _git_changed(self, info: dict[str, Any]) -> list[str]:
        """The product files changed base..head, read from the worktree when git
        can answer. A read failure yields an empty list, which ``product_diff_in_scope``
        then treats as "nothing changed" only for the *scope* judgement -- the
        other facts (base/head presence) are still validated elsewhere."""
        repo, base, head = self._git_anchors(info)
        if not repo or not base or not head:
            return _list(info.get("changed_paths"))
        try:
            from fleet_graph.dd.git import run_git

            proc = run_git(repo, "diff", "--name-only", f"{base}..{head}")
            if proc.returncode != 0:
                return _list(info.get("changed_paths"))
            return [line for line in proc.stdout.splitlines() if line.strip()]
        except Exception:
            return _list(info.get("changed_paths"))

    def _git_deleted(self, info: dict[str, Any]) -> list[str]:
        repo, base, head = self._git_anchors(info)
        if not repo or not base or not head:
            return _list(info.get("deleted_paths"))
        try:
            from fleet_graph.dd.git import run_git

            proc = run_git(repo, "diff", "--diff-filter=D", "--name-only", f"{base}..{head}")
            if proc.returncode != 0:
                return _list(info.get("deleted_paths"))
            return [line for line in proc.stdout.splitlines() if line.strip()]
        except Exception:
            return _list(info.get("deleted_paths"))

    def _git_anchors(self, info: dict[str, Any]) -> tuple[str, str, str]:
        repo = str(info.get("repo_path") or info.get("worktree_path") or "")
        base = str(info.get("target_base_commit") or "")
        head = str(info.get("head_commit") or "")
        if not repo or not Path(repo).is_dir():
            return "", "", ""
        return repo, base, head

    def _product_diff(self, info: dict[str, Any]) -> dict[str, Any]:
        return product_diff_in_scope(
            changed_paths=self._git_changed(info),
            scope_paths=_list(info.get("scope_paths") or _EMPTY),
        )

    def _zero_deletion(self, info: dict[str, Any]) -> dict[str, Any]:
        return zero_test_deletion(deleted_paths=self._git_deleted(info))

    def _personally_ran(self, info: dict[str, Any]) -> dict[str, Any]:
        runs = info.get("acceptance_runs")
        if runs is None:
            commands = _list(info.get("verification_commands"))
            runs = [{"argv": _list(c), "exit_code": 0} for c in commands] if commands else []
        return personally_ran_acceptance(runs=_list(runs))

    def _mutation_gun(self, info: dict[str, Any]) -> dict[str, Any]:
        return mutation_gun_satisfied(mutations=_list(info.get("mutations")))

    def _regression(self, info: dict[str, Any]) -> dict[str, Any]:
        base = info.get("baseline_run")
        head = info.get("head_run")
        base_commit = str(info.get("target_base_commit") or "")
        compared = str(info.get("compared_base_commit") or base_commit)
        if not isinstance(base, dict) or not isinstance(head, dict):
            return {
                "ok": False,
                "reason": "regression baseline missing: no machine-comparable runs",
            }
        attribution: list[FlakeAttribution] = []
        for entry in _list(info.get("flake_attribution")):
            if isinstance(entry, dict):
                attribution.append(
                    FlakeAttribution(
                        test_id=str(entry.get("test_id") or ""),
                        red_count=int(entry.get("red_count") or 0),
                        clean_base_reruns=int(entry.get("clean_base_reruns") or 0),
                    )
                )
        return regression_ok(
            base=_run(base),
            head=_run(head),
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


def _run(value: dict[str, Any]) -> RegressionRun:
    failed_set = frozenset(str(t) for t in _list(value.get("failed_set")))
    return RegressionRun(
        passed=int(value.get("passed") or 0),
        failed=int(value.get("failed") or 0),
        skipped=int(value.get("skipped") or 0),
        failed_set=failed_set,
    )


__all__ = ["EngineSelfGateFacts"]

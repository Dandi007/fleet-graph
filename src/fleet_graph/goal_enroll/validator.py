"""The E5 fail-closed validator: every gate, every refusal code.

``GoalEnrollValidator.validate`` admits a goal line only when every gate
passes, and refuses otherwise with exactly one machine-readable code naming the
failing clause. There is no partial admission, no warning-as-admission, no
deferred acceptance: a refusal means no roster entry was produced.

Gates, in order (the first failure wins, because any one failing clause is
enough to refuse and the earliest one is the least misleading):

1. **folder_id valid** -- the folder exists in the goal-folder source and its
   layout is a goal line (contains ``goal.md`` and ``golden-order.md``).
2. **goal.md carries executable acceptance argv** -- at least one executable
   acceptance command line, using the same ```dd-acceptance contract the
   roster already enforces. Absent -> ``NO_ACCEPTANCE_COMMAND``.
3. **golden-order.md present and non-empty** -- the line's authority boundary.
4. **spec-lint (machine-readable bans)** -- the admitted goal/spec text must
   not instruct merge/push to remote ``main`` and must not reference the
   reserved identity paths ``.dev-dispatch`` / ``.dd-evidence``. A pinned
   40-hex SHA in a critical-path table is a *warning*, never a refusal.
5. **server-side liveness probe** -- the declared acceptance argv is dry-run
   in a throwaway environment to prove the commands can start (exit code
   reachable). A command that cannot even start is refused with
   ``ACCEPTANCE_ARGV_UNEXECUTABLE``.
6. **alias token ownership** -- the applicant's alias token must be owned by
   the governed line (realpath-canonicalized: a regular file exactly at the
   canonical ``<secrets_root>/<alias>.token``, inside the secrets boundary,
   resolving into neither the supervision plane nor another line's token, and
   not a symlink masquerade).
7. **alias uniqueness** -- the alias must not already be claimed by a roster
   line or a pending application.

The validator is deliberately deterministic and self-contained: it reads goal
folder text through an injectable ``GoalFolderSource`` seam and runs argv
through an injectable ``run`` seam, so tests never need a real process tree to
prove every gate.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Protocol

from fleet_graph.acceptance import acceptance_environment
from fleet_graph.dd.control_plane import (
    ACCEPTANCE_FENCE,
    ControlPlaneError,
    derive_acceptance_commands,
)
from fleet_graph.goal_enroll.contract import (
    BRIEFING_VERSION,
    CODE_ACCEPTANCE_ARGV_UNEXECUTABLE,
    CODE_ACCEPTANCE_DECLARATION_INVALID,
    CODE_ALIAS_CONFLICT,
    CODE_ALIAS_TOKEN_MISSING,
    CODE_FOLDER_NOT_FOUND,
    CODE_GOLDEN_ORDER_EMPTY,
    CODE_NO_ACCEPTANCE_COMMAND,
    CODE_NOT_A_GOAL_LINE,
    CODE_SOURCE_UNBOUND,
    CODE_SPEC_LINT_BAN,
    GOAL_ENROLL_MECHANISM,
    LINT_WARNING_PINNED_SHA,
    RESERVED_PATHS,
    GoalEnrollError,
    iso_timestamp,
)

GOAL_MD = "goal.md"
GOLDEN_ORDER_MD = "golden-order.md"

#: Gate 6's token path template -- must agree with bus/tokens.py's
#: LINE_TOKEN_PATH_TEMPLATE (the fleet's own credential layout).
_ALIAS_TOKEN_TEMPLATE = "/data/ronin/secrets/{alias}.token"

#: A short liveness probe budget. The probe only proves a command can start,
#: so a long-running declared command is timed out and still counted as
#: executable (its exit code was reachable in principle); only a command that
#: cannot even start refuses.
LIVENESS_TIMEOUT_SECONDS = 30

#: Machine-readable spec-lint bans, as patterns. A hit refuses with
#: ``SPEC_LINT_BAN``. The banned clauses are the delivery-verb forms that
#: instruct a goal line to merge/push to remote ``main``; the briefing's own
#: prose (which merely *states* the rule) is not admitted goal text and is not
#: linted here.
_BAN_MERGE_PUSH_MAIN = (
    re.compile(r"\bgit\s+merge\b[^\n]*\bmain\b"),
    re.compile(r"\bgit\s+push\b[^\n]*\bmain\b"),
    re.compile(r"\bmerge\s+(?:to|into)\s+(?:remote\s+)?main\b"),
    re.compile(r"\bpush\s+(?:to|onto)\s+(?:remote\s+)?main\b"),
)

#: A pinned 40-hex SHA (a rolling SHA pin) inside a critical-path table row.
#: Detected mechanically as a markdown table row containing a 40-hex token.
_PINNED_SHA_TABLE_ROW = re.compile(r"^\s*\|.*\b[0-9a-f]{40}\b.*\|\s*$", re.M)
_HEX40 = re.compile(r"\b[0-9a-f]{40}\b")


class GoalFolderSource(Protocol):
    """The seam between the validator and the goal-folder store."""

    def exists(self, folder_id: str) -> bool: ...
    def read(self, folder_id: str, filename: str) -> str | None: ...


@dataclass(frozen=True)
class LintBan:
    """One machine-readable spec-lint ban: the clause that refused admission."""

    clause: str
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        return {"clause": self.clause, "snippet": self.snippet}


def spec_lint(text: str) -> tuple[tuple[LintBan, ...], tuple[str, ...]]:
    """The machine-readable spec-lint: bans and warnings over one text.

    Returns ``(bans, warnings)``. Bans refuse admission; warnings are recorded
    on the roster entry but never refuse. A critical-path table pinning a
    40-hex SHA is the one warning the spec names.
    """
    bans: list[LintBan] = []
    for pattern in _BAN_MERGE_PUSH_MAIN:
        match = pattern.search(text)
        if match is not None:
            start = max(0, match.start() - 60)
            snippet = text[start : match.end() + 60].replace("\n", " ")
            bans.append(LintBan(clause="merge_or_push_to_main", snippet=snippet.strip()))

    lower = text.lower()
    for reserved in RESERVED_PATHS:
        idx = lower.find(reserved)
        if idx >= 0:
            snippet = text[max(0, idx - 40) : idx + len(reserved) + 40].replace("\n", " ")
            bans.append(LintBan(clause=f"reserved_path:{reserved}", snippet=snippet.strip()))

    warnings: list[str] = []
    for row in _PINNED_SHA_TABLE_ROW.findall(text):
        if _HEX40.search(row):
            warnings.append(LINT_WARNING_PINNED_SHA)
            break
    return tuple(bans), tuple(warnings)


def _derive_acceptance_argv(goal_md: str) -> list[list[str]]:
    """The acceptance argv a goal.md declares, using the roster's own contract.

    The same ```dd-acceptance fence and shell-quoting rules the control plane
    applies to a spec (``derive_acceptance_commands``), so the goal line and
    the roster speak one argv language. A malformed declaration refuses with
    ``ACCEPTANCE_DECLARATION_INVALID`` -- never a silent guess.
    """
    if not ACCEPTANCE_FENCE.search(goal_md):
        return []
    try:
        return derive_acceptance_commands(goal_md.encode("utf-8", errors="replace"))
    except ControlPlaneError as exc:
        raise GoalEnrollError(CODE_ACCEPTANCE_DECLARATION_INVALID, exc.detail) from exc


def liveness_probe(
    argv: list[str],
    *,
    run: Any = subprocess.run,
    timeout_seconds: int = LIVENESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Dry-run one declared acceptance command in a throwaway environment.

    The probe proves the command is *executable*, not that it passes: any
    reachable exit code (including a timeout on a long-running command) means
    the command started and its exit code is a real acceptance criterion. A
    command that cannot even start (missing executable) refuses the admission.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="goal-enroll-probe-") as cwd:
        try:
            proc = run(
                argv,
                cwd=cwd,
                env=acceptance_environment(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            return {
                "argv": argv,
                "exit_code": int(proc.returncode),
                "started": True,
                "duration_s": round(time.monotonic() - started, 3),
            }
        except FileNotFoundError as exc:
            return {
                "argv": argv,
                "exit_code": 127,
                "started": False,
                "detail": f"command not found: {exc}",
            }
        except (PermissionError, OSError) as exc:
            return {
                "argv": argv,
                "exit_code": 126,
                "started": False,
                "detail": f"cannot execute: {exc}",
            }
        except subprocess.TimeoutExpired:
            return {
                "argv": argv,
                "exit_code": 124,
                "started": True,
                "detail": f"timed out after {timeout_seconds}s (long-running command)",
            }


class GoalEnrollValidator:
    """Runs every gate in order and refuses closed at the first failure.

    Gates 6 and 7 are the application-face gates the spec adds. They take
    injectable seams so the validator stays deterministic and self-contained:
    the alias-token ownership check (the ``/data/ronin/secrets/<alias>.token``
    ownership, realpath-canonicalized) and the alias-uniqueness check (against
    the real roster and the pending queue) are both supplied by the caller --
    the service wires them to the real token store and the queue/roster
    readers, tests inject fakes.
    """

    def __init__(
        self,
        source: GoalFolderSource | None,
        *,
        briefing_version: str = BRIEFING_VERSION,
        clock: Any = time.time,
        probe: Any = liveness_probe,
        alias_token_check: Any = None,
        alias_conflict_check: Any = None,
    ) -> None:
        self._source = source
        self._briefing_version = briefing_version
        self._clock = clock
        self._probe = probe
        #: Gate 6 seam: ``(alias) -> bool``, True when the alias's line token
        #: is owned by the governed line. Defaults to a real ownership check
        #: against the fleet's token template (the same one bus/tokens.py
        #: resolves), realpath-canonicalized over the secrets boundary.
        self._alias_token_check = alias_token_check or _default_alias_token_check()
        #: Gate 7 seam: ``(alias) -> str | None``, the folder_id already
        #: claiming the alias (roster or pending queue), or None when free.
        self._alias_conflict_check = alias_conflict_check or (lambda alias: None)

    def validate(
        self,
        folder_id: str,
        *,
        alias: str | None = None,
        seat_hint: str | None = None,
        max_rounds: int | None = None,
    ) -> dict[str, Any]:
        """Admit one goal line, or refuse with the failing clause's code.

        On success returns the gate facts (acceptance argv, liveness results,
        lint warnings) so the service can seal the engine-versioned roster
        entry. On refusal raises ``GoalEnrollError`` with exactly one code.
        """
        if self._source is None:
            raise GoalEnrollError(
                CODE_SOURCE_UNBOUND, "no goal-folder source is bound to this server"
            )

        # Gate 1: folder_id valid, and its layout is a goal line.
        if not self._source.exists(folder_id):
            raise GoalEnrollError(CODE_FOLDER_NOT_FOUND, f"work folder {folder_id!r} not found")
        goal_md = self._source.read(folder_id, GOAL_MD)
        golden_order = self._source.read(folder_id, GOLDEN_ORDER_MD)
        if goal_md is None or golden_order is None:
            raise GoalEnrollError(
                CODE_NOT_A_GOAL_LINE,
                f"work folder {folder_id!r} is not a goal line "
                f"(needs {GOAL_MD} and {GOLDEN_ORDER_MD})",
            )

        # Gate 2: goal.md carries executable acceptance argv.
        acceptance_argv = _derive_acceptance_argv(goal_md)
        if not acceptance_argv:
            raise GoalEnrollError(
                CODE_NO_ACCEPTANCE_COMMAND,
                f"goal.md of {folder_id!r} declares no executable acceptance command",
            )

        # Gate 3: golden-order.md present and non-empty.
        if not golden_order.strip():
            raise GoalEnrollError(
                CODE_GOLDEN_ORDER_EMPTY, f"golden-order.md of {folder_id!r} is empty"
            )

        # Gate 4: spec-lint machine-readable bans over the admitted text.
        bans, lint_warnings = spec_lint(goal_md + "\n" + golden_order)
        if bans:
            clause = bans[0].clause
            snippet = bans[0].snippet
            raise GoalEnrollError(
                CODE_SPEC_LINT_BAN,
                f"spec-lint refused {folder_id!r}: banned clause {clause!r} near {snippet[:160]!r}",
            )

        # Gate 5: server-side liveness probe -- every declared argv must start.
        liveness: list[dict[str, Any]] = []
        for argv in acceptance_argv:
            result = self._probe(argv)
            liveness.append(result)
            if not result.get("started"):
                raise GoalEnrollError(
                    CODE_ACCEPTANCE_ARGV_UNEXECUTABLE,
                    f"acceptance argv {argv!r} of {folder_id!r} cannot start: "
                    f"{result.get('detail', 'unexecutable')}",
                )

        # Gate 6: the applicant's alias token must be *owned* by the governed
        # line. Only runs when an alias is supplied (the MCP tool always
        # supplies one). Ownership is a positive boundary over canonicalized
        # paths: the token must be a regular file whose realpath is exactly
        # `<secrets_root>/<alias>.token`, inside the secrets boundary, and
        # must not resolve into the supervision plane, another line's token,
        # or a symlink masquerade.
        if alias is not None and not self._alias_token_check(alias):
            raise GoalEnrollError(
                CODE_ALIAS_TOKEN_MISSING,
                f"alias token for {alias!r} is not owned by the governed line "
                f"({_ALIAS_TOKEN_TEMPLATE.format(alias=alias)}); "
                "the token must be the line's own regular file at the canonical "
                "path, not a supervision-plane credential, another line's token, "
                "or a symlink masquerade",
            )

        # Gate 7: the alias must not already be claimed by a roster line or a
        # pending application.
        if alias is not None:
            claimant = self._alias_conflict_check(alias)
            if claimant is not None:
                raise GoalEnrollError(
                    CODE_ALIAS_CONFLICT,
                    f"alias {alias!r} is already claimed by {claimant!r} "
                    "(roster or pending queue); one line has one alias",
                )

        return {
            "folder_id": folder_id,
            "alias": alias,
            "seat_hint": seat_hint,
            "max_rounds": max_rounds,
            "briefing_version": self._briefing_version,
            "acceptance_argv": tuple(tuple(argv) for argv in acceptance_argv),
            "liveness": tuple(liveness),
            "lint_warnings": tuple(lint_warnings),
            "mechanism": GOAL_ENROLL_MECHANISM,
            "admitted_at": iso_timestamp(self._clock()),
        }


def _default_alias_token_check() -> Any:
    """Gate 6's production default: the alias's token is *owned* by the line.

    Reuses the exact template bus/tokens.py resolves (``LINE_TOKEN_PATH_TEMPLATE``
    = ``/data/ronin/secrets/{alias}.token``) so the validator and the line's
    inbox/board credential agree on the same path -- and honours the
    ``FLEET_GRAPH_LINE_TOKEN_PATH`` env override (drills use it to point at a
    scratch secrets dir). The check is **ownership**, not presence: the token
    must be a regular file whose realpath is exactly the governed line's own
    token path, inside the secrets boundary, resolving into neither the
    supervision plane nor another line's token, and not a symlink masquerade.
    The token bytes never leave the file.
    """
    from fleet_graph.bus.tokens import resolve_line_token_ownership

    def check(alias: str) -> bool:
        return resolve_line_token_ownership(alias).owned

    return check


__all__ = [
    "GOAL_MD",
    "GOLDEN_ORDER_MD",
    "LIVENESS_TIMEOUT_SECONDS",
    "GoalEnrollValidator",
    "GoalFolderSource",
    "LintBan",
    "liveness_probe",
    "spec_lint",
]

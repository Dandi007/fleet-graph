"""Minimal-system output protocol: Stop schema table + envelope validator.

This module is the single, dependency-free landing point for the discipline that
every agent output is exactly one JSON object carrying ``schema`` and ``stop``
(protocol §0.1). Later nodes reuse :func:`validate` and
:func:`extract_protocol_object`; later DDs read :func:`describe_schema` to render
``--output-schema`` arguments. The schema constants, stop enums and required
fields live in the declarative :data:`SCHEMA_SPECS` table, never in if/else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ENVELOPE_KEYS = ("schema", "stop")

SCHEMA_GOAL_TURN = "goal.turn/1"
SCHEMA_GOAL_REVIEW = "goal.review/1"
SCHEMA_IMPL = "impl/1"
SCHEMA_REVIEW = "review/1"
SCHEMA_MERGE = "merge/1"
SCHEMA_SCRIBE = "scribe/1"
SCHEMA_RUNTIME_ERROR = "runtime.error/1"

BLOCKED_KINDS = ("needs_human", "external", "contradiction")
REVIEW_ROLES = ("cr", "fr")
FINDING_SEVERITIES = ("blocker", "major", "minor", "note")
BLOCKER_MAJOR = ("blocker", "major")
OBSERVATION_KINDS = ("progress", "anomaly", "cost", "quality", "decision", "pattern")
OBSERVATION_SEVERITIES = ("info", "warn", "high")

_HEX_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass
class ValidationResult:
    """Outcome of :func:`validate`; never raises for control-flow purposes."""

    ok: bool
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaSpec:
    """Declarative description of one output protocol."""

    name: str
    stops: tuple[str, ...]
    required: tuple[str, ...] = ()
    stop_required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    checks: tuple[Callable[[dict[str, Any], str], list[str]], ...] = ()


def _render(values: tuple[str, ...]) -> str:
    return "{" + ", ".join(values) + "}"


def _type_name(value: Any) -> str:
    return type(value).__name__


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_SHA.match(value))


def _check_goal_turn(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    if not _nonempty_str(obj.get("summary")):
        errors.append(f"{schema}: 'summary' must be a non-empty string")
    stop = obj.get("stop")
    if stop == "dispatch":
        dispatch = obj.get("dispatch")
        if not isinstance(dispatch, dict):
            errors.append(f"{schema}: 'dispatch' must be an object")
        else:
            errors.extend(_check_dispatch(dispatch, schema))
    if stop == "blocked":
        blocked = obj.get("blocked")
        if not isinstance(blocked, dict):
            errors.append(f"{schema}: 'blocked' must be an object")
        else:
            kind = blocked.get("kind")
            if kind not in BLOCKED_KINDS:
                errors.append(f"{schema}: blocked.kind={kind!r} not in {_render(BLOCKED_KINDS)}")
            if not _nonempty_str(blocked.get("detail")):
                errors.append(f"{schema}: 'blocked.detail' must be a non-empty string")
    return errors


def _is_valid_branch_name(name: str) -> bool:
    """The GO-36 sub-rules for a dispatch repo branch (mirrors the git-ref shape
    enforced by ``enroll.is_valid_git_branch_name`` without importing enroll)."""

    if name.startswith("-"):
        return False
    if ".." in name:
        return False
    if name.endswith("/"):
        return False
    return not any(ch.isspace() for ch in name)


def _is_repo_relative_path(path: str) -> bool:
    """Whether ``path`` is a repo-relative spec path: not absolute, no ``..``
    segment (protocol.md:11 -- file paths are always relative to workspace root)."""

    if not path or path.startswith("/"):
        return False
    return ".." not in path.split("/")


def _check_dispatch(dispatch: dict[str, Any], schema: str) -> list[str]:
    """Field-level checks for a goal.turn/1 ``dispatch`` object (GO-36).

    The dispatch object is the DD launch protocol: ``spec_text`` plus one or
    more ``repos`` whose branch / worktree path / spec path the engine later
    verifies mechanically. Only the keys named here are inspected; unlisted
    keys are ignored (protocol.md:14 §0.6).
    """

    errors: list[str] = []
    if not _nonempty_str(dispatch.get("spec_text")):
        errors.append(f"{schema}: dispatch.spec_text must be a non-empty string")

    repos = dispatch.get("repos")
    if not isinstance(repos, list) or not repos:
        errors.append(f"{schema}: dispatch.repos must be a non-empty list")
    else:
        seen_paths: dict[str, int] = {}
        seen_pairs: dict[tuple[str, str], int] = {}
        for index, repo in enumerate(repos):
            if not isinstance(repo, dict):
                errors.append(f"{schema}: dispatch.repos[{index}] must be an object")
                continue

            path = repo.get("path")
            if not _nonempty_str(path):
                errors.append(f"{schema}: dispatch.repos[{index}].path must be a non-empty string")
            elif not path.startswith("/"):
                errors.append(f"{schema}: dispatch.repos[{index}].path must be an absolute path")

            if not _nonempty_str(repo.get("remote")):
                errors.append(
                    f"{schema}: dispatch.repos[{index}].remote must be a non-empty string"
                )

            branch = repo.get("branch")
            if not _nonempty_str(branch):
                errors.append(
                    f"{schema}: dispatch.repos[{index}].branch must be a non-empty string"
                )
            elif not _is_valid_branch_name(branch):
                errors.append(
                    f"{schema}: dispatch.repos[{index}].branch={branch!r} "
                    "is not a valid git branch name"
                )

            spec_path = repo.get("spec_path")
            if not _nonempty_str(spec_path):
                errors.append(
                    f"{schema}: dispatch.repos[{index}].spec_path must be a non-empty string"
                )
            elif not _is_repo_relative_path(spec_path):
                errors.append(
                    f"{schema}: dispatch.repos[{index}].spec_path "
                    "must be a repo-relative path (no leading '/', no '..')"
                )

            if _nonempty_str(path):
                if path in seen_paths:
                    errors.append(
                        f"{schema}: dispatch.repos[{index}].path is duplicated by "
                        f"repos[{seen_paths[path]}]"
                    )
                else:
                    seen_paths[path] = index
                if _nonempty_str(branch):
                    pair = (path, branch)
                    if pair in seen_pairs:
                        errors.append(
                            f"{schema}: dispatch.repos[{index}] "
                            f"duplicates repos[{seen_pairs[pair]}]"
                        )
                    else:
                        seen_pairs[pair] = index

    acceptance_extra = dispatch.get("acceptance_extra")
    if acceptance_extra is not None:
        if not isinstance(acceptance_extra, list):
            errors.append(f"{schema}: dispatch.acceptance_extra must be a list")
        else:
            for index, command in enumerate(acceptance_extra):
                if not _nonempty_str(command):
                    errors.append(
                        f"{schema}: dispatch.acceptance_extra[{index}] must be a non-empty string"
                    )

    return errors


def _check_goal_review(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    if obj.get("stop") == "reject" and not _nonempty_str(obj.get("message")):
        errors.append(f"{schema}: stop=reject requires non-empty 'message'")
    return errors


def _check_impl(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    stop = obj.get("stop")
    if stop == "committed":
        if not _is_sha(obj.get("commit")):
            errors.append(f"{schema}: stop=committed requires 'commit' to be a 40-hex sha")
        if not _nonempty_str(obj.get("summary")):
            errors.append(f"{schema}: stop=committed requires non-empty 'summary'")
    elif stop == "failed" and not _nonempty_str(obj.get("detail")):
        errors.append(f"{schema}: stop=failed requires non-empty 'detail'")
    return errors


def _check_review(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    role = obj.get("role")
    if "role" in obj and role not in REVIEW_ROLES:
        errors.append(f"{schema}: role={role!r} not in {_render(REVIEW_ROLES)}")
    findings = obj.get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            errors.append(f"{schema}: 'findings' must be a list")
        else:
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    errors.append(f"{schema}: findings[{index}] must be an object")
                    continue
                severity = finding.get("severity")
                if severity not in FINDING_SEVERITIES:
                    errors.append(
                        f"{schema}: findings[{index}].severity={severity!r} not in "
                        f"{_render(FINDING_SEVERITIES)}"
                    )
    if obj.get("stop") == "fail":
        has_blocker = isinstance(findings, list) and any(
            isinstance(finding, dict) and finding.get("severity") in BLOCKER_MAJOR
            for finding in findings
        )
        if not has_blocker:
            errors.append(
                f"{schema}: stop=fail requires at least one finding with severity in "
                "{blocker, major}"
            )
    return errors


def _check_merge(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    stop = obj.get("stop")
    if stop == "merged" and not _is_sha(obj.get("merged_commit")):
        errors.append(f"{schema}: stop=merged requires 'merged_commit' to be a 40-hex sha")
    if stop == "rebased" and not _is_sha(obj.get("new_head")):
        errors.append(f"{schema}: stop=rebased requires 'new_head' to be a 40-hex sha")
    if stop == "failed" and not _nonempty_str(obj.get("detail")):
        errors.append(f"{schema}: stop=failed requires non-empty 'detail'")
    return errors


def _check_scribe(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    observations = obj.get("observations")
    if not isinstance(observations, list):
        errors.append(f"{schema}: 'observations' must be a list")
        return errors
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            errors.append(f"{schema}: observations[{index}] must be an object")
            continue
        kind = observation.get("kind")
        if kind not in OBSERVATION_KINDS:
            errors.append(
                f"{schema}: observations[{index}].kind={kind!r} not in {_render(OBSERVATION_KINDS)}"
            )
        severity = observation.get("severity")
        if severity not in OBSERVATION_SEVERITIES:
            errors.append(
                f"{schema}: observations[{index}].severity={severity!r} not in "
                f"{_render(OBSERVATION_SEVERITIES)}"
            )
        if not _nonempty_str(observation.get("title")):
            errors.append(f"{schema}: observations[{index}].title must be a non-empty string")
        if not _nonempty_str(observation.get("summary")):
            errors.append(f"{schema}: observations[{index}].summary must be a non-empty string")
        evidence = observation.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{schema}: observations[{index}] requires at least one 'evidence' entry")
    return errors


def _check_runtime_error(obj: dict[str, Any], schema: str) -> list[str]:
    errors: list[str] = []
    if not _nonempty_str(obj.get("detail")):
        errors.append(f"{schema}: requires non-empty 'detail'")
    return errors


SCHEMA_SPECS: tuple[SchemaSpec, ...] = (
    SchemaSpec(
        name=SCHEMA_GOAL_TURN,
        stops=("dispatch", "done", "blocked"),
        stop_required={
            "dispatch": ("summary", "dispatch"),
            "done": ("summary",),
            "blocked": ("summary", "blocked"),
        },
        checks=(_check_goal_turn,),
    ),
    SchemaSpec(
        name=SCHEMA_GOAL_REVIEW,
        stops=("approve", "reject"),
        stop_required={"reject": ("message",)},
        checks=(_check_goal_review,),
    ),
    SchemaSpec(
        name=SCHEMA_IMPL,
        stops=("committed", "failed"),
        stop_required={
            "committed": ("commit", "summary"),
            "failed": ("detail",),
        },
        checks=(_check_impl,),
    ),
    SchemaSpec(
        name=SCHEMA_REVIEW,
        stops=("pass", "fail"),
        required=("role",),
        checks=(_check_review,),
    ),
    SchemaSpec(
        name=SCHEMA_MERGE,
        stops=("merged", "rebased", "failed"),
        stop_required={
            "merged": ("merged_commit",),
            "rebased": ("new_head",),
            "failed": ("detail",),
        },
        checks=(_check_merge,),
    ),
    SchemaSpec(
        name=SCHEMA_SCRIBE,
        stops=("observed",),
        stop_required={"observed": ("observations",)},
        checks=(_check_scribe,),
    ),
    SchemaSpec(
        name=SCHEMA_RUNTIME_ERROR,
        stops=("invalid_output",),
        stop_required={"invalid_output": ("detail",)},
        checks=(_check_runtime_error,),
    ),
)

_SCHEMA_BY_NAME: dict[str, SchemaSpec] = {spec.name: spec for spec in SCHEMA_SPECS}

SCHEMA_NAMES: tuple[str, ...] = tuple(spec.name for spec in SCHEMA_SPECS)


def validate(obj: dict[str, Any], expected_schema: str) -> ValidationResult:
    """Validate ``obj`` against the schema named ``expected_schema``.

    Returns field-level errors as strings ready to drop into an event payload;
    never raises.
    """
    spec = _SCHEMA_BY_NAME.get(expected_schema)
    if spec is None:
        return ValidationResult(ok=False, errors=[f"{expected_schema}: unknown schema"])

    if not isinstance(obj, dict):
        return ValidationResult(
            ok=False,
            errors=[f"{expected_schema}: expected a JSON object, got {_type_name(obj)}"],
        )

    errors: list[str] = []

    if "schema" not in obj:
        errors.append(f"{expected_schema}: missing required key 'schema'")
    elif obj["schema"] != expected_schema:
        errors.append(
            f"{expected_schema}: schema={obj['schema']!r} does not match {expected_schema!r}"
        )

    stop = obj.get("stop")
    if "stop" not in obj:
        errors.append(f"{expected_schema}: missing required key 'stop'")
    elif stop not in spec.stops:
        errors.append(f"{expected_schema}: stop={stop!r} not in {_render(spec.stops)}")

    for field_name in spec.required:
        if field_name not in obj:
            errors.append(f"{expected_schema}: missing required field '{field_name}'")

    for field_name in spec.stop_required.get(stop, ()):
        if field_name not in obj:
            errors.append(f"{expected_schema}: stop={stop!r} requires field '{field_name}'")

    for check in spec.checks:
        errors.extend(check(obj, expected_schema))

    return ValidationResult(ok=not errors, errors=errors)


def describe_schema(name: str) -> dict[str, Any]:
    """Read-only export of a schema's stop enum and required fields."""
    spec = _SCHEMA_BY_NAME[name]
    return {
        "schema": spec.name,
        "stops": list(spec.stops),
        "required": list(spec.required),
        "stop_fields": {stop: list(fields) for stop, fields in spec.stop_required.items()},
    }


def _matched_close(text: str, start: int) -> int | None:
    """Index just past the '}' that balances ``text[start] == '{'``, or None."""
    depth = 0
    in_string = False
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _skip_string(text: str, index: int) -> int:
    """Index just past the closing quote of the string starting at ``text[index]``."""
    length = len(text)
    index += 1
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    return length


def _object_spans(text: str) -> list[tuple[int, int]]:
    """Ordered (start, end) spans of every top-level balanced ``{...}`` region."""
    spans: list[tuple[int, int]] = []
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char == "{":
            end = _matched_close(text, index)
            if end is not None:
                spans.append((index, end))
                index = end
                continue
        index += 1
    return spans


def extract_protocol_object(text: str, schema_prefix: str) -> dict[str, Any] | None:
    """Pull the protocol object out of noisy stdout (000-smoke §3).

    Scan every balanced ``{...}`` candidate and return the last object whose
    ``schema`` starts with ``schema_prefix`` and parses as JSON; failing that,
    the last object that parses at all; otherwise ``None``.
    """
    matching: list[dict[str, Any]] = []
    fallback: dict[str, Any] | None = None
    for start, end in _object_spans(text):
        raw = text[start:end]
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        fallback = obj
        schema = obj.get("schema")
        if isinstance(schema, str) and schema.startswith(schema_prefix):
            matching.append(obj)
    if matching:
        return matching[-1]
    return fallback

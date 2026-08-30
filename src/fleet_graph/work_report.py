"""The structured worker turn report: ``fleet-graph.worker-turn-report/v1``.

This is the machine-readable replacement for worker prose (E4a). A worker turn
emits exactly one report object; the orchestration layer derives turn outcome,
blocker, produced files and self-test results *only* from its structured fields,
never from any human-facing prose. Prose survives solely as an optional
``prose_attachment`` carrying ``media_type``/``content`` -- inspectable, never a
control surface.

The decoder is strict on purpose. A missing, malformed, unsupported-version,
unknown-field, bad-enum, bad-path or bad-exit-code report is a
:class:`ReportProtocolError`, not something to half-heal: every bounded value is
*rejected* when it exceeds its limit rather than truncated, so the persisted
record stays unambiguous about what the worker actually said.

Limits are explicit constants so the boundary's own code is the documentation of
the schema's bounds (spec: "Attachment size limits must be explicit in code").
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The one schema_version this decoder accepts. The literal ``fleet-graph``
#: turns the version into a ship we can pin down; anything else is a protocol
#: failure, never a guessed downgrade.
SCHEMA_VERSION = "fleet-graph.worker-turn-report/v1"

OUTCOME_COMPLETED = "completed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAILED = "failed"
OUTCOMES = frozenset({OUTCOME_COMPLETED, OUTCOME_BLOCKED, OUTCOME_FAILED})

CHANGE_CREATED = "created"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"
CHANGE_UNCHANGED = "unchanged"
CHANGES = frozenset({CHANGE_CREATED, CHANGE_MODIFIED, CHANGE_DELETED, CHANGE_UNCHANGED})

MEDIA_TYPE_PLAIN = "text/plain"
MEDIA_TYPE_MARKDOWN = "text/markdown"
MEDIA_TYPES = frozenset({MEDIA_TYPE_PLAIN, MEDIA_TYPE_MARKDOWN})

#: Top-level keys a v1 report may carry. Everything else is rejected so a
#: worker cannot smuggle a second, prose-derived control field under a new name.
REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "turn_id",
        "outcome",
        "summary",
        "did",
        "files",
        "self_tests",
        "blocker",
        "prose_attachment",
    }
)

# --- explicit bounds (reject, never truncate) -------------------------------

TURN_ID_MAX_CHARS = 256
SUMMARY_MAX_CHARS = 8000
DID_MAX_ITEMS = 1024
DID_ITEM_MAX_CHARS = 8000
FILES_MAX_ITEMS = 4096
PATH_MAX_CHARS = 4096
SELF_TESTS_MAX_ITEMS = 1024
ARGV_MAX_ITEMS = 256
ARGV_ITEM_MAX_CHARS = 4096
BLOCKER_KIND_MAX_CHARS = 256
BLOCKER_DETAIL_MAX_CHARS = 8000
ATTACHMENT_CONTENT_MAX_CHARS = 200_000


class ReportProtocolError(Exception):
    """A worker turn report failed the v1 protocol, with a stable ``kind``.

    ``kind`` is one of ``missing`` (a required field is absent), ``malformed``
    (the payload is not a JSON object), ``unsupported_version`` (``schema_version``
    is present but not the one literal) or ``schema_invalid`` (any other shape,
    enum, path or size violation). The kind is the machine-facing discriminator
    ``worker_turn`` uses to name the round it refuses; ``detail`` is for humans.
    """

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


def _strip_code_fence(text: str) -> str:
    """Mechanically unshell a markdown code fence around a JSON body (E4b 精神：
    去壳不碰语义). Some seats (deepseek-v4 家族实测 2026-08-29) wrap the report
    in ```json ... ``` despite the raw-JSON instruction; the fence carries no
    information, so stripping it is normalization, not repair. Anything that
    still fails to parse afterwards stays a malformed fault."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


#: The report's protocol magic. A gateway may prepend zero-information noise to
#: the turn text (SCNet 实测 2026-08-29: 空 assistant 消息被替换成
#: "[System: Empty message content sanitised to satisfy protocol]"，seat 拼接后
#: 报告被埋在噪音后面); the report self-identifies by this head, so extracting
#: the trailing object that starts with it is normalization, not inference.
_REPORT_HEAD = re.compile(r"\{\s*\"schema_version\"")


def _extract_embedded_report(text: str) -> dict[str, Any] | None:
    # strict=False: control characters inside string values are seat noise
    # (raw newlines/tabs from the model), not protocol violations -- a whole
    # generation died on one (wf-216dc3 g1, 2026-08-30).
    decoder = json.JSONDecoder(strict=False)
    for match in reversed(list(_REPORT_HEAD.finditer(text))):
        try:
            parsed, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _coerce_object(raw: Any) -> dict[str, Any]:
    """A report dict, or a JSON-string report decoded to one -- else malformed."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(_strip_code_fence(raw), strict=False)
        except json.JSONDecodeError as exc:
            embedded = _extract_embedded_report(raw)
            if embedded is None:
                raise ReportProtocolError("malformed", f"report is not valid JSON: {exc}") from exc
            parsed = embedded
        if isinstance(parsed, dict):
            return parsed
        raise ReportProtocolError("malformed", "report must be a JSON object")
    raise ReportProtocolError("malformed", f"report must be an object, got {type(raw).__name__}")


def _nonempty_bounded(data: dict[str, Any], key: str, max_chars: int) -> str:
    if key not in data:
        raise ReportProtocolError("missing", key)
    value = data[key]
    if not isinstance(value, str):
        raise ReportProtocolError("schema_invalid", f"{key} must be a string")
    if not value.strip():
        raise ReportProtocolError("schema_invalid", f"{key} must be non-empty")
    if len(value) > max_chars:
        raise ReportProtocolError("schema_invalid", f"{key} exceeds {max_chars} characters")
    return value


def _outcome(data: dict[str, Any]) -> str:
    if "outcome" not in data:
        raise ReportProtocolError("missing", "outcome")
    value = data["outcome"]
    if not isinstance(value, str) or value not in OUTCOMES:
        raise ReportProtocolError(
            "schema_invalid", f"outcome={value!r} is not one of {sorted(OUTCOMES)}"
        )
    return value


def _did(data: dict[str, Any]) -> list[str]:
    if "did" not in data:
        raise ReportProtocolError("missing", "did")
    did = data["did"]
    if not isinstance(did, list):
        raise ReportProtocolError("schema_invalid", "did must be an array")
    if len(did) > DID_MAX_ITEMS:
        raise ReportProtocolError("schema_invalid", f"did exceeds {DID_MAX_ITEMS} items")
    items: list[str] = []
    for index, entry in enumerate(did):
        if not isinstance(entry, str):
            raise ReportProtocolError("schema_invalid", f"did[{index}] must be a string")
        if not entry.strip():
            raise ReportProtocolError("schema_invalid", f"did[{index}] must be non-empty")
        if len(entry) > DID_ITEM_MAX_CHARS:
            raise ReportProtocolError(
                "schema_invalid", f"did[{index}] exceeds {DID_ITEM_MAX_CHARS} characters"
            )
        items.append(entry)
    return items


def _files(data: dict[str, Any]) -> list[dict[str, str]]:
    if "files" not in data:
        raise ReportProtocolError("missing", "files")
    files = data["files"]
    if not isinstance(files, list):
        raise ReportProtocolError("schema_invalid", "files must be an array")
    if len(files) > FILES_MAX_ITEMS:
        raise ReportProtocolError("schema_invalid", f"files exceeds {FILES_MAX_ITEMS} items")
    result: list[dict[str, str]] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ReportProtocolError("schema_invalid", f"files[{index}] must be an object")
        if set(entry) != {"path", "change"}:
            raise ReportProtocolError(
                "schema_invalid", f"files[{index}] must have only path and change"
            )
        path = entry["path"]
        if not isinstance(path, str):
            raise ReportProtocolError("schema_invalid", f"files[{index}].path must be a string")
        if not path.strip():
            raise ReportProtocolError("schema_invalid", f"files[{index}].path must be non-empty")
        if len(path) > PATH_MAX_CHARS:
            raise ReportProtocolError(
                "schema_invalid", f"files[{index}].path exceeds {PATH_MAX_CHARS} characters"
            )
        if path.startswith("/"):
            raise ReportProtocolError("schema_invalid", f"files[{index}].path must be relative")
        change = entry["change"]
        if change not in CHANGES:
            raise ReportProtocolError(
                "schema_invalid",
                f"files[{index}].change={change!r} is not one of {sorted(CHANGES)}",
            )
        result.append({"path": path, "change": change})
    return result


def _self_tests(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "self_tests" not in data:
        raise ReportProtocolError("missing", "self_tests")
    self_tests = data["self_tests"]
    if not isinstance(self_tests, list):
        raise ReportProtocolError("schema_invalid", "self_tests must be an array")
    if len(self_tests) > SELF_TESTS_MAX_ITEMS:
        raise ReportProtocolError(
            "schema_invalid", f"self_tests exceeds {SELF_TESTS_MAX_ITEMS} items"
        )
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(self_tests):
        if not isinstance(entry, dict):
            raise ReportProtocolError("schema_invalid", f"self_tests[{index}] must be an object")
        if set(entry) != {"argv", "exit_code"}:
            raise ReportProtocolError(
                "schema_invalid", f"self_tests[{index}] must have only argv and exit_code"
            )
        argv = entry["argv"]
        if not isinstance(argv, list) or not argv:
            raise ReportProtocolError(
                "schema_invalid", f"self_tests[{index}].argv must be a non-empty array"
            )
        if len(argv) > ARGV_MAX_ITEMS:
            raise ReportProtocolError(
                "schema_invalid", f"self_tests[{index}].argv exceeds {ARGV_MAX_ITEMS} items"
            )
        parts: list[str] = []
        for arg_index, part in enumerate(argv):
            if not isinstance(part, str):
                raise ReportProtocolError(
                    "schema_invalid", f"self_tests[{index}].argv[{arg_index}] must be a string"
                )
            if len(part) > ARGV_ITEM_MAX_CHARS:
                raise ReportProtocolError(
                    "schema_invalid",
                    f"self_tests[{index}].argv[{arg_index}] exceeds "
                    f"{ARGV_ITEM_MAX_CHARS} characters",
                )
            parts.append(part)
        exit_code = entry["exit_code"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ReportProtocolError(
                "schema_invalid", f"self_tests[{index}].exit_code must be an integer"
            )
        if exit_code < 0:
            raise ReportProtocolError(
                "schema_invalid", f"self_tests[{index}].exit_code must be non-negative"
            )
        result.append({"argv": parts, "exit_code": exit_code})
    return result


def _blocker(data: dict[str, Any], outcome: str) -> dict[str, str] | None:
    if "blocker" not in data:
        raise ReportProtocolError("missing", "blocker")
    blocker = data["blocker"]
    if outcome == OUTCOME_COMPLETED:
        if blocker is not None:
            raise ReportProtocolError(
                "schema_invalid", "blocker must be null for a completed report"
            )
        return None
    if blocker is None:
        if outcome == OUTCOME_BLOCKED:
            raise ReportProtocolError("schema_invalid", "blocker is required for a blocked report")
        return None
    if not isinstance(blocker, dict):
        raise ReportProtocolError("schema_invalid", "blocker must be an object or null")
    if set(blocker) != {"kind", "detail"}:
        raise ReportProtocolError("schema_invalid", "blocker must have only kind and detail")
    kind = blocker["kind"]
    if not isinstance(kind, str) or not kind.strip():
        raise ReportProtocolError("schema_invalid", "blocker.kind must be a non-empty string")
    if len(kind) > BLOCKER_KIND_MAX_CHARS:
        raise ReportProtocolError(
            "schema_invalid", f"blocker.kind exceeds {BLOCKER_KIND_MAX_CHARS} characters"
        )
    detail = blocker["detail"]
    if not isinstance(detail, str) or not detail.strip():
        raise ReportProtocolError("schema_invalid", "blocker.detail must be a non-empty string")
    if len(detail) > BLOCKER_DETAIL_MAX_CHARS:
        raise ReportProtocolError(
            "schema_invalid", f"blocker.detail exceeds {BLOCKER_DETAIL_MAX_CHARS} characters"
        )
    return {"kind": kind, "detail": detail}


def validate_attachment(media_type: str, content: str) -> dict[str, str]:
    """Validate an attachment's ``media_type``/``content`` as a pair.

    This is the single enforcement point for the explicit
    ``ATTACHMENT_CONTENT_MAX_CHARS`` bound, shared by the decoder's
    ``prose_attachment`` field and by the adapter that carries legacy prose
    forward into an attachment at the ingress. The bound must bite on *every*
    path that puts prose into a report -- including prose carried forward after
    ``decode_report`` has already run -- so the persisted record stays
    unambiguous. Oversize content is rejected (``ReportProtocolError``), never
    truncated.
    """
    if media_type not in MEDIA_TYPES:
        raise ReportProtocolError(
            "schema_invalid",
            f"prose_attachment.media_type={media_type!r} is not one of {sorted(MEDIA_TYPES)}",
        )
    if not isinstance(content, str):
        raise ReportProtocolError("schema_invalid", "prose_attachment.content must be a string")
    if len(content) > ATTACHMENT_CONTENT_MAX_CHARS:
        raise ReportProtocolError(
            "schema_invalid",
            f"prose_attachment.content exceeds {ATTACHMENT_CONTENT_MAX_CHARS} "
            "characters (rejected, not truncated)",
        )
    return {"media_type": media_type, "content": content}


def _prose_attachment(data: dict[str, Any]) -> dict[str, str]:
    attachment = data["prose_attachment"]
    if not isinstance(attachment, dict):
        raise ReportProtocolError("schema_invalid", "prose_attachment must be an object")
    if set(attachment) != {"media_type", "content"}:
        raise ReportProtocolError(
            "schema_invalid", "prose_attachment must have only media_type and content"
        )
    return validate_attachment(attachment["media_type"], attachment["content"])


def decode_report(raw: Any) -> dict[str, Any]:
    """Validate ``raw`` as ``fleet-graph.worker-turn-report/v1`` and normalise it.

    Accepts a report dict or a JSON-string report (the compatibility shape a
    worker seat that only knows ``text`` produces). Returns a fresh dict holding
    exactly the required fields plus ``prose_attachment`` when present -- never
    any unknown top-level field. Raises :class:`ReportProtocolError` on every
    violation; it never truncates and never half-heals.
    """
    data = _coerce_object(raw)
    unknown = sorted(set(data) - REPORT_FIELDS)
    if unknown:
        raise ReportProtocolError("schema_invalid", f"unknown top-level field(s): {unknown}")

    if "schema_version" not in data:
        raise ReportProtocolError("missing", "schema_version")
    schema_version = data["schema_version"]
    if not isinstance(schema_version, str):
        raise ReportProtocolError("schema_invalid", "schema_version must be a string")
    if schema_version != SCHEMA_VERSION:
        raise ReportProtocolError("unsupported_version", f"schema_version={schema_version!r}")

    outcome = _outcome(data)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "turn_id": _nonempty_bounded(data, "turn_id", TURN_ID_MAX_CHARS),
        "outcome": outcome,
        "summary": _nonempty_bounded(data, "summary", SUMMARY_MAX_CHARS),
        "did": _did(data),
        "files": _files(data),
        "self_tests": _self_tests(data),
        "blocker": _blocker(data, outcome),
    }
    if "prose_attachment" in data:
        report["prose_attachment"] = _prose_attachment(data)
    return report


def project_control(report: dict[str, Any]) -> dict[str, Any]:
    """The structured control slice of a validated report, without its prose.

    This is the only projection the orchestration layer may derive turn outcome
    / next action / produced files / self-test results from. ``prose_attachment``
    is deliberately absent: whatever prose claims is never a control input.
    """
    return {
        "schema_version": report["schema_version"],
        "turn_id": report["turn_id"],
        "outcome": report["outcome"],
        "summary": report["summary"],
        "did": report["did"],
        "files": report["files"],
        "self_tests": report["self_tests"],
        "blocker": report["blocker"],
    }


__all__ = [
    "ATTACHMENT_CONTENT_MAX_CHARS",
    "CHANGES",
    "MEDIA_TYPE_MARKDOWN",
    "MEDIA_TYPE_PLAIN",
    "OUTCOMES",
    "OUTCOME_BLOCKED",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "SCHEMA_VERSION",
    "ReportProtocolError",
    "decode_report",
    "project_control",
    "validate_attachment",
]

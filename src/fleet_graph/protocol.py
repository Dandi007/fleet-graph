"""输入与输出协议；业务前置条件由引擎结合现场核实。"""

from __future__ import annotations

import jsonschema

TEXT = {"type": "string", "minLength": 1}


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


REPO = obj(
    {
        "path": TEXT,
        "remote": TEXT,
        "target_branch": TEXT,
        "acceptance": {"type": "array", "minItems": 1, "items": TEXT},
    }
)
ENROLL = obj(
    {
        "schema": {"const": "goal.enroll/2"},
        "request_id": TEXT,
        "work_folder": TEXT,
        "title": TEXT,
        "source_branch": TEXT,
        "repos": {"type": "array", "minItems": 1, "items": REPO},
        "takeover": {"type": "boolean"},
    },
    ["schema", "request_id", "work_folder", "title", "source_branch", "repos"],
)


def action(kind, fields=None, optional=None):
    fields = fields or {}
    properties = {"type": {"const": kind}, **fields, **(optional or {})}
    return obj(properties, ["type", *fields])


ACTION_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {
        "oneOf": [
            action(
                "dispatch",
                {
                    "repo_ref": TEXT,
                    "source_branch": TEXT,
                    "target_branch": TEXT,
                    "worktree": TEXT,
                    "spec_path": TEXT,
                    "summary": TEXT,
                },
            ),
            action("approve", {"dd_id": TEXT, "review_ref": TEXT, "summary": TEXT}),
            action("reject", {"dd_id": TEXT, "message": TEXT}),
            action("revise", {"dd_id": TEXT, "message": TEXT}),
            action("cancel", {"dd_id": TEXT, "message": TEXT}),
            action("add_repo", {"repo": REPO}, {"takeover": {"type": "boolean"}}),
            action("reply", {"in_reply_to": TEXT, "text": TEXT}),
            action("waiting", {"summary": TEXT}),
            action(
                "blocked",
                {"summary": TEXT, "missing": TEXT, "attempted": TEXT, "help_needed": TEXT},
            ),
            action(
                "done",
                {"summary": TEXT, "evidence_refs": {"type": "array", "minItems": 1, "items": TEXT}},
            ),
        ]
    },
}
IMPL_SCHEMA = {
    "oneOf": [
        action("committed", {"summary": TEXT, "evidence_refs": {"type": "array", "items": TEXT}}),
        action(
            "needs_goal",
            {"message": TEXT, "evidence_refs": {"type": "array", "minItems": 1, "items": TEXT}},
        ),
    ]
}
REVIEW_SCHEMA = {
    "oneOf": [
        action(
            "pass",
            {"summary": TEXT, "evidence_refs": {"type": "array", "minItems": 1, "items": TEXT}},
        ),
        action(
            "fail",
            {
                "summary": TEXT,
                "findings": {
                    "type": "array",
                    "minItems": 1,
                    "items": obj({"severity": {"enum": ["blocker", "major"]}, "message": TEXT}),
                },
                "evidence_refs": {"type": "array", "minItems": 1, "items": TEXT},
            },
        ),
    ]
}
SCRIBE_SCHEMA = obj(
    {
        "observations": {
            "type": "array",
            "items": obj(
                {
                    "title": TEXT,
                    "summary": TEXT,
                    "kind": TEXT,
                    "severity": TEXT,
                    "evidence_refs": {"type": "array", "minItems": 1, "items": TEXT},
                }
            ),
        }
    }
)


def validate(value, schema):
    jsonschema.Draft202012Validator(schema).validate(value)
    return value


def validate_actions(value):
    validate(value, ACTION_SCHEMA)
    intentions = [i for i, a in enumerate(value) if a["type"] in {"waiting", "blocked", "done"}]
    if len(intentions) > 1 or (intentions and intentions[0] != len(value) - 1):
        raise ValueError("最多一个状态意图且必须位于末尾")
    if (
        intentions
        and value[-1]["type"] == "blocked"
        and any(a["type"] == "dispatch" for a in value)
    ):
        raise ValueError("派单期间不能声明无可推进工作")
    return value

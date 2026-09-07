"""Minimal-system prompt layer: ``*.in/1`` input objects + prompt rendering.

This is the GO-17 / GO-24 landing point between the engine graph and
``agentrun``: every agent call's user prompt *is* that round's protocol input
object, and the system prompt (role persona, output-schema constraints,
history handle) is rendered once per session instead of repeated (protocol
§0.9). The module owns four mechanical concerns, nothing more:

- ``SCHEMA_*_IN`` / ``IN_SCHEMAS``: the six input-object schema literals
  (protocol §2..§6, §12).
- ``history_handle``: the GO-17 two-layer history handle -- paths into
  ``<goal_run_root>``, never bulk content.
- ``build_goal_turn_in`` .. ``build_scribe_in``: construct the six input
  objects field-for-field in protocol key order; each signature accepts only
  this handoff's content (there is deliberately no parameter that could carry
  whole-table history).
- ``ROLE_PERSONA`` / ``render_user_prompt`` / ``render_system_prompt`` /
  ``needs_system_prompt``: turn those objects into the two prompt layers and
  decide when the system layer is sent.

Invariants, mirroring ``agentrun.py``:

1. Pure functions, zero IO: no filesystem access, no git, no subprocess, no
   clock. The module only shapes data the engine graph later hands to
   ``agentrun``; paths are joined with ``posixpath`` and never checked.
2. Every built object's first key is ``"schema"`` and the remaining key order
   matches protocol.md exactly; optional fields that are ``None`` stay
   ``null`` instead of being silently dropped (first-round ``feedback`` /
   ``last_stop``, a CR's absent ``cr_result``, a release merge's ``dd_id``).
3. Contract misuse fails loudly before any object is built: an unknown review
   role, a ``cr`` carrying ``cr_result``, a ``release`` merge carrying
   ``dd_id``, or an inverted scribe seq range all raise ``ValueError``.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any

from fleet_graph.minimal.agentrun import SessionPolicy, schema_for
from fleet_graph.minimal.protocol import describe_schema

# ---------------------------------------------------------------------------
# Input schema constants (protocol §2..§6, §12)
# ---------------------------------------------------------------------------

SCHEMA_GOAL_TURN_IN = "goal.turn.in/1"
SCHEMA_GOAL_REVIEW_IN = "goal.review.in/1"
SCHEMA_IMPL_IN = "impl.in/1"
SCHEMA_REVIEW_IN = "review.in/1"
SCHEMA_MERGE_IN = "merge.in/1"
SCHEMA_SCRIBE_IN = "scribe.in/1"

IN_SCHEMAS: tuple[str, ...] = (
    SCHEMA_GOAL_TURN_IN,
    SCHEMA_GOAL_REVIEW_IN,
    SCHEMA_IMPL_IN,
    SCHEMA_REVIEW_IN,
    SCHEMA_MERGE_IN,
    SCHEMA_SCRIBE_IN,
)

# ---------------------------------------------------------------------------
# The history handle (GO-17 / protocol §0.7)
# ---------------------------------------------------------------------------

HISTORY_NOTE = (
    "全部历史（每张 DD 的 spec / review / 验收输出、所有 event、旧消息、"
    "WF 的 progress / findings）都在这里，需要就自己读"
)


def history_handle(
    *, goal_run_root: str, work_folder: str | None = None, dd_id: str | None = None
) -> dict[str, Any]:
    """The GO-17 history handle: where everything older lives, paths only.

    The handle never carries content -- only the ``events`` / ``dd_dir``
    locations under ``goal_run_root`` (plus the ``work_folder`` id when there
    is one) and the fixed note telling the agent to read it itself. Paths are
    joined with ``posixpath``; nothing is stat'ed or read. With ``dd_id`` the
    ``dd_dir`` points at that DD's own directory, without it at the ``dd/``
    root. Key order matches protocol §2: ``work_folder``, ``events``,
    ``dd_dir``, ``note``.
    """
    handle: dict[str, Any] = {}
    if work_folder is not None:
        handle["work_folder"] = work_folder
    handle["events"] = posixpath.join(goal_run_root, "events.jsonl")
    if dd_id is None:
        handle["dd_dir"] = posixpath.join(goal_run_root, "dd", "")
    else:
        handle["dd_dir"] = posixpath.join(goal_run_root, "dd", dd_id, "")
    handle["note"] = HISTORY_NOTE
    return handle


# ---------------------------------------------------------------------------
# The six *.in/1 builders (protocol §2..§6, §12)
# ---------------------------------------------------------------------------

_REVIEW_IN_ROLES = ("cr", "fr")
_MERGE_KINDS = ("dd", "release")


def build_goal_turn_in(
    *,
    goal: dict[str, Any],
    goal_version: int,
    steer_diff: list[dict[str, Any]],
    turn_no: int,
    release_branch: str,
    release_head: str,
    dd_summary: str,
    last_dd: dict[str, Any] | None,
    last_stop: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    warnings: list[str],
    history: dict[str, Any],
) -> dict[str, Any]:
    """One Goal Agent turn input (protocol §2), keys in protocol order."""
    return {
        "schema": SCHEMA_GOAL_TURN_IN,
        "goal": goal,
        "goal_version": goal_version,
        "steer_diff": steer_diff,
        "turn_no": turn_no,
        "release_branch": release_branch,
        "release_head": release_head,
        "dd_summary": dd_summary,
        "last_dd": last_dd,
        "last_stop": last_stop,
        "messages": messages,
        "warnings": warnings,
        "history": history,
    }


def build_goal_review_in(
    *,
    goal: dict[str, Any],
    dd: dict[str, Any],
    release_head: str,
    history: dict[str, Any],
) -> dict[str, Any]:
    """One Goal Agent approval input (protocol §3): the awaiting-``dd`` handoff."""
    return {
        "schema": SCHEMA_GOAL_REVIEW_IN,
        "goal": goal,
        "dd": dd,
        "release_head": release_head,
        "history": history,
    }


def build_impl_in(
    *,
    dd_id: str,
    round: int,
    workspace: str,
    branch: str,
    base_commit: str,
    spec_text: str,
    acceptance: list[str],
    feedback: dict[str, Any] | None,
    history: dict[str, Any],
) -> dict[str, Any]:
    """One Impl round input (protocol §4); first-round ``feedback`` stays ``null``."""
    return {
        "schema": SCHEMA_IMPL_IN,
        "dd_id": dd_id,
        "round": round,
        "workspace": workspace,
        "branch": branch,
        "base_commit": base_commit,
        "spec_text": spec_text,
        "acceptance": acceptance,
        "feedback": feedback,
        "history": history,
    }


def build_review_in(
    *,
    role: str,
    dd_id: str,
    round: int,
    workspace: str,
    branch: str,
    base_commit: str,
    head_commit: str,
    spec_text: str,
    acceptance_results: list[dict[str, Any]],
    cr_result: dict[str, Any] | None,
    history: dict[str, Any],
) -> dict[str, Any]:
    """One CR/FR round input (protocol §5), shared protocol, ``role`` split.

    ``role`` must be ``"cr"`` or ``"fr"``. ``cr_result`` (this round's CR
    conclusion, handed to FR only) may be non-``None`` only for ``"fr"``; a
    ``"cr"`` call carrying one raises ``ValueError``. The key is always
    present -- ``null`` for CR -- so the object shape stays fixed.
    """
    if role not in _REVIEW_IN_ROLES:
        raise ValueError(f"role={role!r} not in {_render(_REVIEW_IN_ROLES)}")
    if role == "cr" and cr_result is not None:
        raise ValueError("cr_result is only allowed for role='fr' (protocol §5)")
    return {
        "schema": SCHEMA_REVIEW_IN,
        "role": role,
        "dd_id": dd_id,
        "round": round,
        "workspace": workspace,
        "branch": branch,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "spec_text": spec_text,
        "acceptance_results": acceptance_results,
        "cr_result": cr_result,
        "history": history,
    }


def build_merge_in(
    *,
    kind: str,
    dd_id: str | None,
    workspace: str,
    source_branch: str,
    source_head: str,
    target_branch: str,
    target_head: str,
    acceptance: list[str],
) -> dict[str, Any]:
    """One Merge Agent input (protocol §6); no history key by design.

    ``kind`` is ``"dd"`` (branch -> this line's release) or ``"release"``
    (release -> the goal's target branch). A ``release`` merge must carry
    ``dd_id=None`` (protocol.md:190); anything else raises ``ValueError``.
    """
    if kind not in _MERGE_KINDS:
        raise ValueError(f"kind={kind!r} not in {_render(_MERGE_KINDS)}")
    if kind == "release" and dd_id is not None:
        raise ValueError("kind='release' requires dd_id=None (protocol §6)")
    return {
        "schema": SCHEMA_MERGE_IN,
        "kind": kind,
        "dd_id": dd_id,
        "workspace": workspace,
        "source_branch": source_branch,
        "source_head": source_head,
        "target_branch": target_branch,
        "target_head": target_head,
        "acceptance": acceptance,
    }


def build_scribe_in(
    *,
    goal_id: str,
    goal_version: int,
    trigger: str,
    since_seq: int,
    until_seq: int,
    new_runs: list[dict[str, Any]],
    prior_observations: str,
    history: dict[str, Any],
) -> dict[str, Any]:
    """One scribe input (protocol §12): seq-range boundaries, not event content.

    ``until_seq`` must be ``>=`` ``since_seq``; an inverted range raises
    ``ValueError``.
    """
    if until_seq < since_seq:
        raise ValueError(f"until_seq={until_seq!r} must be >= since_seq={since_seq!r}")
    return {
        "schema": SCHEMA_SCRIBE_IN,
        "goal_id": goal_id,
        "goal_version": goal_version,
        "trigger": trigger,
        "since_seq": since_seq,
        "until_seq": until_seq,
        "new_runs": new_runs,
        "prior_observations": prior_observations,
        "history": history,
    }


# ---------------------------------------------------------------------------
# Prompt rendering (protocol §0.9 / GO-24)
# ---------------------------------------------------------------------------

_USER_PROMPT_PREAMBLE = (
    "这是本轮的协议化输入：下面的 JSON 对象就是本次调用的 user prompt，"
    "只含本次交接内容；更早的内容不会在这里重复，需要时经对象内的 history 句柄自己读。"
)


def render_user_prompt(in_obj: dict[str, Any]) -> str:
    """The user prompt for one call: one fixed line + the input object as JSON.

    The preamble says what the object is (this round's protocol input, this
    handoff only); the JSON dump carries ``ensure_ascii=False`` so the protocol's
    Chinese note text stays readable. Nothing else is appended -- no history
    content, no schema explanation, no trailing prose.
    """
    return _USER_PROMPT_PREAMBLE + "\n" + json.dumps(in_obj, ensure_ascii=False, indent=2)


ROLE_PERSONA: dict[str, str] = {
    "goal": (
        "你是 Goal Agent。你的第一性原理是判断这条 goal 的工作是否完成：每个 turn "
        "你看 goal 当前值、steer、release 状态与上一张 DD 的结果，在派下一张 DD、done、"
        "blocked 之间做裁决。你原则上拥有写码权限，但不亲自改任何代码——所有改动只能"
        "通过派发 DD（写出完整、自含验收期望的 spec_text）完成；一个 turn 只派一张 DD，"
        "dispatch 的 spec 非空是硬约束。"
    ),
    "impl": (
        "你是 Impl。你只在输入给定的 workspace（worktree）里干活：不选分支、不切目录、"
        "不碰其它 worktree；workspace / branch / base_commit 由引擎填好并前后核对，"
        "你不越界。按 spec_text 实现、过验收，把改动 commit 到给定分支；做不了就 "
        "failed 并写清原因，不猜、不静默缩水。本轮回到你手上的原因只在 feedback 这一条，"
        "更早的经 history 自己读。"
    ),
    "cr": (
        "你是 CR（Continuous Reviewer）。你审的是输入指名的 head_commit 相对 "
        "base_commit 的改动是否符合 spec_text、是否会让验收变脆。你不改工作树——"
        "review 改了代码即判无效；要 fail 必须至少带一条 blocker 或 major 的 finding，"
        "不许无理由打回。"
    ),
    "fr": (
        "你是 FR（Final Reviewer），做最终验收：可以部署、可以跑任何东西，做了什么"
        "写进 evidence。你不改工作树——review 改了代码即判无效；要 fail 必须至少带一条 "
        "blocker 或 major 的 finding，不许无理由打回。你与 CR 协议相同，区别只在职责"
        "与模型。"
    ),
    "merge": (
        "你是 Merge Agent，所有合并的唯一执行者：源分支 → 目标分支由引擎指定并核对，"
        "引擎自己不做 merge、不做 fast-forward。冲突自行 rebase；rebase 动了代码就不"
        "合并，停在源分支新 head 上交回完整再审。你 Stop 的时候必须已经处理完：merged "
        "则目标分支已包含改动且验收已在合并结果上跑过，rebased 则源分支停在新 head，"
        "failed 则写清原因且两分支尖均未动。"
    ),
    "scribe": (
        "你是书记员（Scribe），第六个 agent，只读。你读 L0（events.jsonl、agent "
        "session 文件、各 Stop 输出），产出 L1 observation：把细节抽象成 high level 的"
        "结构化理解。你不改任何状态、不参与任何流程分支、不发 control，输出不进任何"
        "流程 agent 的注入层；每条 observation 必须带能定位回 L0 具体位置的证据，"
        "没有证据的整条丢弃。"
    ),
}


def render_system_prompt(
    role: str,
    *,
    call_kind: str | None = None,
    history: dict[str, Any],
    harness_note: str | None = None,
) -> str:
    """The once-per-session system prompt (protocol §0.9).

    persona + the output contract rendered from
    ``describe_schema(schema_for(role, call_kind))`` (schema literal, allowed
    stop enum, per-stop required fields) + the exactly-one-JSON-object rule +
    the history handle as verbatim JSON + an optional harness boundary note.
    """
    persona = ROLE_PERSONA.get(role)
    if persona is None:
        raise ValueError(f"unknown role {role!r} (expected one of {_render(tuple(ROLE_PERSONA))})")

    description = describe_schema(schema_for(role, call_kind))
    required = description["required"]
    stop_fields = description["stop_fields"]

    lines: list[str] = [persona, "", "## 输出契约"]
    lines.append(f'- schema 字面量："{description["schema"]}"')
    lines.append(f"- 允许的 stop 枚举：{' | '.join(description['stops'])}")
    lines.append("- 所有 stop 都必填的字段：" + (", ".join(required) if required else "无"))
    if stop_fields:
        lines.append("- 各 stop 分支的必填字段：")
        for stop_value, fields in stop_fields.items():
            lines.append(f"  - {stop_value} -> {', '.join(fields)}")
    else:
        lines.append("- 各 stop 分支的必填字段：无")
    lines.append(
        "- 你的最后一条输出必须是恰好一个 JSON 对象：无 prose、无代码围栏、"
        "对象前后没有任何其它文字。"
    )
    lines += [
        "",
        "## history 句柄",
        "更早的内容（全部 event、之前各轮 review、之前的 DD、MCP 送入的消息）"
        "不注入给你，都在下面这个句柄里，需要就自己读：",
        json.dumps(history, ensure_ascii=False, indent=2),
    ]
    if harness_note is not None:
        lines += ["", "## harness 边界", harness_note]
    return "\n".join(lines)


def needs_system_prompt(policy: SessionPolicy, *, is_first_call: bool) -> bool:
    """Whether this call carries the system prompt (protocol.md:30).

    ``fresh`` sends it every call; ``resume`` sends it on the first call of
    the session only -- afterwards it already lives in the session and is not
    re-sent. Any other mode is a policy-construction bug and raises.
    """
    if policy.mode == "fresh":
        return True
    if policy.mode == "resume":
        return is_first_call
    raise ValueError(f"unknown session policy mode {policy.mode!r} (expected resume or fresh)")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(values: tuple[str, ...]) -> str:
    return "{" + ", ".join(values) + "}"


__all__ = [
    "HISTORY_NOTE",
    "IN_SCHEMAS",
    "ROLE_PERSONA",
    "SCHEMA_GOAL_REVIEW_IN",
    "SCHEMA_GOAL_TURN_IN",
    "SCHEMA_IMPL_IN",
    "SCHEMA_MERGE_IN",
    "SCHEMA_REVIEW_IN",
    "SCHEMA_SCRIBE_IN",
    "build_goal_review_in",
    "build_goal_turn_in",
    "build_impl_in",
    "build_merge_in",
    "build_review_in",
    "build_scribe_in",
    "history_handle",
    "needs_system_prompt",
    "render_system_prompt",
    "render_user_prompt",
]

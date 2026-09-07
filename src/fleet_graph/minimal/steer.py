"""minimal steer 层：goal_steer 的 patch 应用与 goal_version / steer_diff 投射（GO-20）。

`goal_steer`（protocol §10）可改或新增任意 goal 字段；每次 steer 落一条
`goal.steered` event（protocol §8，payload = version, diff, note），Goal Agent
下一个 turn 的输入（protocol §2）注入 fold 出的 ``goal_version`` 与自上次
turn 以来的 ``steer_diff``。本模块补齐这层投射的四个纯函数，零 IO：不写
control.jsonl、不写 events、不做磁盘上的版本号自增——版本号只从 events
fold 出来，events.jsonl 是唯一状态来源（protocol §11）。

规则对齐（defence-in-depth）：

- ``IMMUTABLE_FIELDS`` **直接复用** ``control.FORBIDDEN_PATCH_KEYS``
  （``goal_id`` / ``work_folder`` / ``repo.path``），不存在第二份清单；
  ``apply_patch`` 应用前还按 ``control.validate_control`` 对 steer patch 的
  同一拒绝集复查（含空 patch、非 dict patch、repo dict 内带 ``path``），
  错误文案也一字不差。control 在写入口拦第一道，本模块在应用前拦第二道。
- ``apply_patch`` 只处理顶层 key：key 已在原对象里 → ``changed``，不在 →
  ``added``；嵌套 dict 一律**整值替换**、不做递归 merge——机械化原则：不猜
  用户想合并还是想覆盖，要合并就把合并后的整块新值写进 patch。
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from fleet_graph.minimal.control import FORBIDDEN_PATCH_KEYS
from fleet_graph.minimal.events import Event

IMMUTABLE_FIELDS: tuple[str, ...] = FORBIDDEN_PATCH_KEYS


def _ensure_patch_shape(patch: Any) -> dict[str, Any]:
    """与 ``control.validate_control`` 的 steer patch 校验同一拒绝集，不过抛 ``ValueError``。"""
    if not isinstance(patch, dict):
        raise ValueError("steer 'patch' must be a JSON object")
    if not patch:
        raise ValueError("steer 'patch' must not be empty")
    for key in IMMUTABLE_FIELDS:
        if key in patch:
            raise ValueError(f"steer 'patch' must not touch {key!r}")
    repo = patch.get("repo")
    if isinstance(repo, dict) and "path" in repo:
        raise ValueError("steer 'patch' must not touch 'repo.path'")
    return patch


def apply_patch(
    goal_obj: dict[str, Any], patch: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 steer patch 应用到 goal 对象的拷贝上，返回 ``(new_goal_obj, diff)``。

    绝不改入参：``goal_obj`` 深拷贝后再改，patch 的值也深拷贝进返回值，
    返回值与入参不共享任何可变结构。``diff`` 形如
    ``{"changed": {...}, "added": {...}}``，按 key 是否已在**原** goal 对象里
    拆分（protocol §2 ``steer_diff[]`` 的 changed / added 语义），值为 patch
    里的新值。只处理顶层 key，嵌套 dict 整值替换（见模块 docstring）。
    patch 先过与 ``control.validate_control`` 一致的校验：空、非 dict、碰
    ``IMMUTABLE_FIELDS``（含 repo dict 内 ``path``）一律 ``ValueError``。
    """
    _ensure_patch_shape(patch)
    if not isinstance(goal_obj, dict):
        raise ValueError("goal object must be a JSON object")
    new_goal = copy.deepcopy(goal_obj)
    changed: dict[str, Any] = {}
    added: dict[str, Any] = {}
    for key, value in patch.items():
        bucket = changed if key in goal_obj else added
        bucket[key] = copy.deepcopy(value)
        new_goal[key] = copy.deepcopy(value)
    return new_goal, {"changed": changed, "added": added}


def steered_payload(version: int, diff: dict[str, Any], note: str | None) -> dict[str, Any]:
    """``goal.steered`` 的 event payload（protocol §8）：version、diff、note。"""
    return {"version": version, "diff": diff, "note": note}


def _diff_parts(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """从 steered payload 里取 ``(changed, added)``，缺的按空 dict 读。"""
    diff = payload.get("diff")
    if not isinstance(diff, dict):
        diff = {}
    changed = diff.get("changed")
    added = diff.get("added")
    return (changed if isinstance(changed, dict) else {}, added if isinstance(added, dict) else {})


def steer_diff_since(events: Iterable[Event], since_seq: int) -> list[dict[str, Any]]:
    """从 ``seq > since_seq`` 的 ``goal.steered`` event 造 protocol §2 的条目。

    每条形如 ``{version, ts, changed, added, note}``（键顺序同 §2 示例），
    按 seq 升序返回；其它 kind 一概忽略。diff 里缺 changed / added 的旧
    事件按空 dict 读。
    """
    steered = sorted(
        (ev for ev in events if ev.kind == "goal.steered" and ev.seq > since_seq),
        key=lambda ev: ev.seq,
    )
    entries: list[dict[str, Any]] = []
    for ev in steered:
        payload = ev.payload or {}
        changed, added = _diff_parts(payload)
        entries.append(
            {
                "version": payload.get("version"),
                "ts": ev.ts,
                "changed": changed,
                "added": added,
                "note": payload.get("note"),
            }
        )
    return entries


def current_goal(enroll_obj: dict[str, Any], events: Iterable[Event]) -> tuple[dict[str, Any], int]:
    """把全部 ``goal.steered`` 的 patch 按 seq 序 fold 到 enroll 对象上。

    返回 ``(goal_obj, version)``：goal_obj 是 §2 ``goal`` 字段该带的当前值
    （enroll 深拷贝后依次应用每条 steered 的 changed+added，同 key 后写覆盖
    先写）；version = enroll 为 1、每条 steered +1，与 ``events.fold`` 的
    ``goal_version`` 派生一致。每条 patch 重放时同样过 ``apply_patch`` 的
    校验：历史事件里出现空 patch 或不可变字段，说明日志被污染，宁可抛
    ``ValueError`` 也不静默给错值。
    """
    goal_obj = copy.deepcopy(enroll_obj)
    steered = sorted(
        (ev for ev in events if ev.kind == "goal.steered"),
        key=lambda ev: ev.seq,
    )
    for ev in steered:
        payload = ev.payload or {}
        changed, added = _diff_parts(payload)
        goal_obj, _ = apply_patch(goal_obj, {**changed, **added})
    return goal_obj, 1 + len(steered)


__all__ = [
    "IMMUTABLE_FIELDS",
    "apply_patch",
    "current_goal",
    "steer_diff_since",
    "steered_payload",
]

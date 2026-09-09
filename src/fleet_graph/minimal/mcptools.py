"""protocol §10 / §12's nine MCP tools: declarative table + read/write handlers.

MCP is the only resident service; its whole surface is the nine tools in
:data:`TOOLS` (protocol §10 lists eight, §12's last line adds
``goal_observations``). Two iron rules bound what any handler may do: reads touch
only ``events.jsonl`` (and ``observations.jsonl`` for ``goal_observations``),
writes do exactly one of two things — append *one* line to that goal's
``control.jsonl``, or hand back a :class:`SpawnPlan` (spawn the engine process).
There is no other channel between MCP and the engine.

This module binds no MCP framework and no transport layer: the table and the
handlers are plain functions and dataclasses over ``events`` / ``control`` /
``runroot`` / ``enroll``, so they stay pure-testable. It never ``import
subprocess``, never sends a signal, never calls ``os.kill``, never runs git, and
never appends to ``events.jsonl`` — a spawn is only ever *described* as a
:class:`SpawnPlan` for the transport layer (a later DD) to execute. Crashed
goals are only ever *reported* (server ``crashed``), never auto-resumed (GO-16).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.minimal import control, enroll, runroot
from fleet_graph.minimal.control import _default_alive_probe, _event_to_dict, _last_engine_pid
from fleet_graph.minimal.events import EventLog

# 输入字段类型 → (谓词, 人读名)。bool 单独排除：它是 int 的子类，但从来不是合法的
# ``tail`` / ``since_seq``。
_FIELD_TYPES: dict[str, tuple[Callable[[Any], bool], str]] = {
    "str": (lambda value: isinstance(value, str), "a string"),
    "int": (lambda value: isinstance(value, int) and not isinstance(value, bool), "an integer"),
    "dict": (lambda value: isinstance(value, dict), "a JSON object"),
}


def _type_name(value: Any) -> str:
    return type(value).__name__


@dataclass(frozen=True)
class ToolSpec:
    """声明式描述一个工具：名字、read/write 归类、输入字段（名 + 类型）、必填。"""

    name: str
    kind: str
    fields: tuple[tuple[str, str], ...] = ()
    required: tuple[str, ...] = ()


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="goal_enroll",
        kind="write",
        fields=(("enroll", "dict"),),
        required=("enroll",),
    ),
    ToolSpec(name="goal_list", kind="read"),
    ToolSpec(
        name="goal_status",
        kind="read",
        fields=(("goal_id", "str"), ("tail", "int")),
        required=("goal_id",),
    ),
    ToolSpec(
        name="goal_events",
        kind="read",
        fields=(("goal_id", "str"), ("since_seq", "int")),
        required=("goal_id",),
    ),
    ToolSpec(
        name="goal_message",
        kind="write",
        fields=(("goal_id", "str"), ("text", "str")),
        required=("goal_id", "text"),
    ),
    ToolSpec(
        name="goal_steer",
        kind="write",
        fields=(("goal_id", "str"), ("patch", "dict")),
        required=("goal_id", "patch"),
    ),
    ToolSpec(
        name="goal_stop",
        kind="write",
        fields=(("goal_id", "str"), ("mode", "str")),
        required=("goal_id", "mode"),
    ),
    ToolSpec(
        name="goal_resume",
        kind="write",
        fields=(("goal_id", "str"),),
        required=("goal_id",),
    ),
    ToolSpec(
        name="goal_observations",
        kind="read",
        fields=(("goal_id", "str"), ("since_ts", "str"), ("severity", "str")),
        required=("goal_id",),
    ),
)

_TOOL_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}

TOOL_NAMES: tuple[str, ...] = tuple(tool.name for tool in TOOLS)


def validate_tool_call(name: str, args: Any) -> list[str]:
    """校验一次工具调用，返回字段级错误列表；空列表即通过。

    只做三件事（protocol §10 的机械门槛）：未知工具、缺必填、类型不对。
    值级校验（steer 的 patch 内容、stop 的 mode 枚举、enroll 的节点校验）不在
    这里，交给各 handler 复用 ``control.validate_control`` / ``enroll.validate_enroll``。
    """
    spec = _TOOL_BY_NAME.get(name)
    if spec is None:
        return [f"unknown tool {name!r}"]
    if not isinstance(args, dict):
        return [f"{name}: args must be a JSON object, got {_type_name(args)}"]

    errors: list[str] = []
    for field in spec.required:
        if field not in args:
            errors.append(f"{name}: missing required field {field!r}")
    for field, field_type in spec.fields:
        if field not in args:
            continue
        check, human = _FIELD_TYPES[field_type]
        if not check(args[field]):
            errors.append(f"{name}: field {field!r} must be {human}, got {_type_name(args[field])}")
    return errors


@dataclass(frozen=True)
class SpawnPlan:
    """描述「spawn 引擎进程」这唯一一件本模块不做、由传输层做的事。

    ``action`` ∈ ``enroll``（新 goal 的引擎进程）/ ``resume``（对 stopped /
    blocked / crashed 的 goal 重新 spawn，protocol §10）；``enroll`` 仅在 enroll
    时携带规范化后的 enroll 对象。本模块绝不真正 spawn。
    """

    goal_id: str
    action: str
    run_root: str
    enroll: dict[str, Any] | None = None


def _run_root(engine_root: str | Path, goal_id: str) -> Path:
    """engine_root + goal_id 的状态根目录（顺带强校验 goal_id 形状）。"""
    return runroot.goal_run_root(goal_id, engine_root=engine_root).root


# --- 读 handler（只读 events.jsonl / observations.jsonl） -------------------


def goal_list(
    engine_root: str | Path,
    *,
    alive_probe: Callable[[int | None], bool] | None = None,
) -> list[dict[str, Any]]:
    """扫 ``<engine_root>/g-*/``，每 goal 一行（复用 ``control.goal_list_row``）。"""
    root = Path(engine_root)
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for directory in sorted(root.glob("g-*")):
        if not directory.is_dir():
            continue
        if alive_probe is None:
            rows.append(control.goal_list_row(directory))
        else:
            rows.append(control.goal_list_row(directory, alive_probe=alive_probe))
    return rows


def goal_status(
    engine_root: str | Path,
    goal_id: str,
    *,
    tail: int = 20,
    alive_probe: Callable[[int | None], bool] | None = None,
    reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """派生状态 + 最近 ``tail`` 条 event（复用 ``control.goal_status_view``），
    顶部附最近 3 条 L1 observation（protocol §12 末行）。

    ``recent_observations`` 键先于 ``goal_status_view`` 的其余键插入，使
    ``json.dumps`` 序列化里它排最前（dict 本身无序，只是插入顺序）。``reader``
    注入式，默认 :func:`default_observations_reader`（读不到 / 半行坏 JSON 一律
    跳过，绝不抛）。"""
    root = _run_root(engine_root, goal_id)
    events = list(EventLog(root).read())
    probe = alive_probe if alive_probe is not None else _default_alive_probe
    pid = _last_engine_pid(events)
    alive = False if pid is None else bool(probe(pid))
    tail_events = events[-tail:] if tail and tail > 0 else None
    view = control.goal_status_view(events, alive=alive, tail_events=tail_events)
    read_observations = default_observations_reader if reader is None else reader
    observations = read_observations(str(root / "observations.jsonl"))
    result: dict[str, Any] = {"recent_observations": observations[-3:]}
    result.update(view)
    return result


def goal_events(
    engine_root: str | Path,
    goal_id: str,
    *,
    since_seq: int = 0,
) -> list[dict[str, Any]]:
    """seq 大于 ``since_seq`` 的原始 event 流（序列化为 dict）。"""
    root = _run_root(engine_root, goal_id)
    return [_event_to_dict(ev) for ev in EventLog(root).read(since_seq)]


def default_observations_reader(path: str | Path) -> list[dict[str, Any]]:
    """本地行解析器：逐行读 observations.jsonl，解析失败的半行跳过。"""
    obs_path = Path(path)
    if not obs_path.exists():
        return []
    observations: list[dict[str, Any]] = []
    with obs_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                observations.append(obj)
    return observations


def goal_observations(
    engine_root: str | Path,
    goal_id: str,
    *,
    since_ts: str | None = None,
    severity: str | None = None,
    reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """读 L1 观测（protocol §12）。``reader`` 注入式；默认本地行解析器，因此本
    DD 不依赖 scribe 那张 DD 是否已合。``since_ts`` 过滤 ts 严格大于，``severity``
    精确匹配；都给则同时满足。"""
    root = _run_root(engine_root, goal_id)
    read_observations = default_observations_reader if reader is None else reader
    observations = read_observations(str(root / "observations.jsonl"))
    matched: list[dict[str, Any]] = []
    for obs in observations:
        if severity is not None and obs.get("severity") != severity:
            continue
        ts = obs.get("ts")
        if since_ts is not None and not (isinstance(ts, str) and ts > since_ts):
            continue
        matched.append(obs)
    return matched


# --- 写 handler（control.jsonl 一行 / SpawnPlan，二选一） -------------------


def goal_message(engine_root: str | Path, goal_id: str, text: str) -> dict[str, Any]:
    """写 control ``{op: message}``，恰好一行，无别的副作用。"""
    root = _run_root(engine_root, goal_id)
    return control.ControlLog(root).append({"op": "message", "text": text})


def goal_steer(engine_root: str | Path, goal_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """写 control ``{op: steer}``，恰好一行。patch 校验直接复用
    ``control.validate_control``（碰 goal_id / work_folder / repo.path 即拒）。"""
    root = _run_root(engine_root, goal_id)
    return control.ControlLog(root).append({"op": "steer", "patch": patch})


def goal_stop(engine_root: str | Path, goal_id: str, mode: str) -> dict[str, Any]:
    """graceful 写 control ``{op: stop}``；kill 直接 SIGTERM 进程组，那是信号、
    本模块不产出，留给传输层，故在此明确拒绝。"""
    if mode == "kill":
        raise ValueError(
            "goal_stop kill sends SIGTERM to the process group; the tool layer "
            "produces no signal (spec §4) — the transport layer must handle kill"
        )
    root = _run_root(engine_root, goal_id)
    return control.ControlLog(root).append({"op": "stop", "mode": mode})


def goal_resume(engine_root: str | Path, goal_id: str) -> SpawnPlan:
    """对 stopped / blocked / crashed 的 goal 产一个重新 spawn 的 SpawnPlan。"""
    root = _run_root(engine_root, goal_id)
    return SpawnPlan(goal_id=goal_id, action="resume", run_root=str(root))


def goal_enroll(
    enroll_obj: dict[str, Any],
    *,
    goal_id: str | None = None,
    git_probe: enroll.GitProbe | None = None,
    engine_root: str | Path = runroot.DEFAULT_ENGINE_ROOT,
) -> SpawnPlan:
    """校验 enroll 请求（复用 ``enroll`` 校验节点）并产一个 spawn 的 SpawnPlan。"""
    normalized = enroll.normalize_enroll(enroll_obj, goal_id=goal_id, git_probe=git_probe)
    root = _run_root(engine_root, normalized["goal_id"])
    return SpawnPlan(
        goal_id=normalized["goal_id"],
        action="enroll",
        run_root=str(root),
        enroll=normalized,
    )


__all__ = [
    "TOOLS",
    "TOOL_NAMES",
    "SpawnPlan",
    "ToolSpec",
    "default_observations_reader",
    "goal_enroll",
    "goal_events",
    "goal_list",
    "goal_message",
    "goal_observations",
    "goal_resume",
    "goal_status",
    "goal_steer",
    "goal_stop",
    "validate_tool_call",
]

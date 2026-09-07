"""minimal 控制面：control.jsonl 写读 + goal_list/goal_status 投射。

MCP 对引擎的写只有两件事：spawn 引擎进程，或往该 goal 的
``<goal_run_root>/control.jsonl`` 追加一行；引擎在步骤边界读新行、落
``control.received`` event 后执行（docs/specs/minimal/protocol.md §10）。

never_auto_resume（GO-16）：本模块只从 events.jsonl 派生并**报告** ``crashed``
（最后一条 event 非终态且进程不在），绝不产生 resume 动作——崩溃的 goal 由
人或外部观测面显式调 ``goal_resume``，自动重启会掩盖问题。本模块不 spawn
进程、不发信号，进程存活探测是注入式的（``alive_probe``）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleet_graph.minimal.events import Event, EventLog, fold

CONTROL_OPS: tuple[str, ...] = ("message", "steer", "stop", "resume")

STOP_MODES: tuple[str, ...] = ("graceful", "kill")

FORBIDDEN_PATCH_KEYS: tuple[str, ...] = ("goal_id", "work_folder", "repo.path")

_CONTROL_KEY_ORDER: tuple[str, ...] = ("ts", "seq", "op")

_OP_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "message": ("text",),
    "steer": ("patch",),
    "stop": ("mode",),
    "resume": (),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def validate_control(op_obj: Any) -> list[str]:
    """校验一条 control op 对象，返回字段级错误列表；空列表即通过。"""
    if not isinstance(op_obj, dict):
        return ["control op must be a JSON object"]
    errors: list[str] = []
    op = op_obj.get("op")
    if not isinstance(op, str) or op not in CONTROL_OPS:
        return [f"op must be one of: {', '.join(CONTROL_OPS)}"]
    if op == "message":
        text = op_obj.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append("message requires a non-empty string 'text'")
    elif op == "steer":
        if "patch" not in op_obj:
            errors.append("steer requires a 'patch' object")
        else:
            patch = op_obj["patch"]
            if not isinstance(patch, dict):
                errors.append("steer 'patch' must be a JSON object")
            else:
                if not patch:
                    errors.append("steer 'patch' must not be empty")
                for key in FORBIDDEN_PATCH_KEYS:
                    if key in patch:
                        errors.append(f"steer 'patch' must not touch {key!r}")
                repo = patch.get("repo")
                if isinstance(repo, dict) and "path" in repo:
                    errors.append("steer 'patch' must not touch 'repo.path'")
    elif op == "stop":
        if op_obj.get("mode") not in STOP_MODES:
            errors.append(f"stop 'mode' must be one of: {', '.join(STOP_MODES)}")
    elif op == "resume":
        extra = sorted(set(op_obj) - {"op"})
        if extra:
            errors.append(f"resume takes no extra fields: {', '.join(extra)}")
    return errors


def _parse_control_line(line: str) -> dict[str, Any]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"control line is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("control line is not a JSON object")
    op = obj.get("op")
    if not isinstance(op, str) or op not in CONTROL_OPS:
        raise ValueError(f"control line has unknown op: {op!r}")
    try:
        obj["seq"] = int(obj["seq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("control line must carry an integer 'seq'") from exc
    return obj


class ControlLog:
    """某个 goal 的控制日志，落在 ``<goal_run_root>/control.jsonl``。

    行为与 ``events.EventLog`` 对齐但独立实现：append + fsync、键顺序固定、
    ``ensure_ascii=False``、紧凑分隔符；打开已有文件时 seq 从最后一条可解析行
    续下去；最后一行截断（半行）视为未写完并忽略，中间行损坏则抛错。
    """

    def __init__(self, goal_run_root: str | os.PathLike[str]) -> None:
        self.goal_run_root = Path(goal_run_root)
        self.path = self.goal_run_root / "control.jsonl"
        self._next_seq = self._load_next_seq()

    def _load_next_seq(self) -> int:
        if not self.path.exists():
            return 1
        last_seq = 0
        with self.path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = _parse_control_line(line)
                except ValueError:
                    continue
                last_seq = obj["seq"]
        return last_seq + 1

    def append(self, op_obj: dict[str, Any]) -> dict[str, Any]:
        """校验并追加一条 control op；不过抛 ``ValueError``，补 ``ts`` 与 ``seq``。"""
        errors = validate_control(op_obj)
        if errors:
            raise ValueError("; ".join(errors))
        op = op_obj["op"]
        line_obj: dict[str, Any] = {"ts": _now_iso(), "seq": self._next_seq, "op": op}
        known = _OP_FIELD_ORDER[op]
        for key in known:
            if key in op_obj:
                line_obj[key] = op_obj[key]
        for key in sorted(set(op_obj) - {"op"} - set(known)):
            line_obj[key] = op_obj[key]
        self.goal_run_root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line_obj, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._next_seq += 1
        return dict(line_obj)

    def read_new(self, since_seq: int = 0) -> Iterator[dict[str, Any]]:
        """增量读 seq 大于 ``since_seq`` 的行；半行尾部忽略，中间损坏抛错。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for idx, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = _parse_control_line(line)
            except ValueError as exc:
                if idx == len(lines) - 1:
                    return
                raise ValueError(f"corrupt control line {idx + 1}: {exc}") from exc
            if obj["seq"] > since_seq:
                yield obj


def _event_to_dict(ev: Event) -> dict[str, Any]:
    return {
        "ts": ev.ts,
        "goal_id": ev.goal_id,
        "dd_id": ev.dd_id,
        "kind": ev.kind,
        "seq": ev.seq,
        "payload": ev.payload,
    }


def _human_step(events: list[Event], derived: Any) -> str:
    """人读的一句：在途 DD 带开着的 stage（如 ``dd-03/impl``），否则
    ``dd-03/r2``；无在途 DD 时 ``goal.turn#N``；终态给状态名。"""
    open_stage: str | None = None
    open_stage_dd: str | None = None
    turn_open = False
    for ev in events:
        if ev.kind == "goal.turn.started":
            turn_open = True
        elif ev.kind == "goal.turn.finished":
            turn_open = False
        elif ev.kind == "dd.stage.started":
            stage = (ev.payload or {}).get("stage")
            open_stage = str(stage) if stage else None
            open_stage_dd = ev.dd_id
        elif ev.kind == "dd.stage.finished":
            open_stage = None
            open_stage_dd = None
    current = derived.current_dd
    if current.dd_id is not None:
        if open_stage is not None and open_stage_dd == current.dd_id:
            return f"{current.dd_id}/{open_stage}"
        return f"{current.dd_id}/r{current.round}"
    if turn_open or derived.turn_no:
        return f"goal.turn#{derived.turn_no}"
    if derived.state in ("done", "blocked", "stopped"):
        return derived.state
    return "idle"


def goal_status_view(
    events: Iterable[Event],
    *,
    alive: bool,
    tail_events: list[Event] | None = None,
) -> dict[str, Any]:
    """基于 ``events.fold`` 的 goal_status 投射（protocol §10/§11）。

    ``state``：fold 的终态（done/blocked/stopped）原样透出；非终态且
    ``alive=False`` → ``crashed``（只报告，绝不触发 resume，GO-16）。
    给了 ``tail_events`` 时额外附 ``tail`` 键（最近 N 条 event 的序列化）。
    """
    events_list = list(events)
    derived = fold(events_list)
    state = derived.state if (derived.terminal or alive) else "crashed"
    current = derived.current_dd
    view: dict[str, Any] = {
        "goal_id": events_list[0].goal_id if events_list else None,
        "state": state,
        "step": _human_step(events_list, derived),
        "turn_no": derived.turn_no,
        "dd_count": len(derived.dd_history),
        "goal_version": derived.goal_version,
        "last_event_ts": events_list[-1].ts if events_list else None,
        "last_seq": derived.last_seq,
        "warnings": list(derived.warnings),
        "current_dd": {"dd_id": current.dd_id, "round": current.round}
        if current.dd_id is not None
        else None,
    }
    if tail_events is not None:
        view["tail"] = [_event_to_dict(ev) for ev in tail_events]
    return view


def _default_alive_probe(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _last_engine_pid(events: list[Event]) -> int | None:
    """最后一条 ``engine.started`` / ``engine.resumed`` payload 里的 pid。"""
    payload: dict[str, Any] | None = None
    for ev in events:
        if ev.kind in ("engine.started", "engine.resumed"):
            payload = ev.payload or {}
    if payload is None:
        return None
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid


def _goal_title(events: list[Event]) -> str | None:
    for ev in events:
        if ev.kind == "goal.enrolled":
            title = (ev.payload or {}).get("title")
            if isinstance(title, str) and title:
                return title
    return None


def goal_list_row(
    goal_run_root: str | os.PathLike[str],
    *,
    alive_probe: Callable[[int | None], bool] = _default_alive_probe,
) -> dict[str, Any]:
    """protocol §10 ``goal_list`` 的一行：读该目录的 events.jsonl 派生。

    pid 取不到（None）视为不活；``alive_probe`` 注入式，默认实现用
    ``os.kill(pid, 0)`` 探测，测试一律注入假 probe。
    """
    root = Path(goal_run_root)
    log = EventLog(root)
    events = list(log.read())
    pid = _last_engine_pid(events)
    alive = False if pid is None else bool(alive_probe(pid))
    view = goal_status_view(events, alive=alive)
    goal_id = view["goal_id"] if view["goal_id"] is not None else root.name
    row: dict[str, Any] = {"goal_id": goal_id}
    title = _goal_title(events)
    if title is not None:
        row["title"] = title
    row.update(
        {
            "state": view["state"],
            "step": view["step"],
            "turn_no": view["turn_no"],
            "dd_count": view["dd_count"],
            "last_event_ts": view["last_event_ts"],
            "warnings": view["warnings"],
            "pid": pid,
        }
    )
    return row

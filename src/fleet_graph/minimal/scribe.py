"""minimal 书记员 L1 observation 层：observations.jsonl 读写 + §12 证据闸（GO-21）。

书记员是第六个 agent，只读 L0（events.jsonl、agent session 文件、各 Stop 输出），产
L1 结构化 observation，每条带指回 L0 的证据指针（docs/specs/minimal/protocol.md §12）。
本模块是引擎侧的机械层，只做四件事：

- ``SCRIBE_TRIGGERS``：§12 只在 goal 级边界起书记员的触发点全集。
- ``ObservationLog``：``<goal_run_root>/observations.jsonl`` 的写入与读回。写盘纪律
  与 :mod:`fleet_graph.minimal.events` 完全一致（append + flush + ``os.fsync``、一行
  一个 JSON、``ensure_ascii=False``）；``read`` 容忍被截断的最后一行（崩溃点）。
- ``validate_observation`` / ``partition_observations``：§12 证据闸 —— ``evidence``
  至少一条，每条恰好是 ``event_seq`` / ``session`` / ``stop_of`` 三种指针之一；
  ``event_seq`` 落在 ``[since_seq, until_seq]`` 内，``session`` 路径过注入的探针；
  不过闸的 observation 整条丢弃（``dropped`` 每条带原因，供调用方落
  ``agent.failed(detail: observation_without_evidence)``）。空 ``observations`` 合法。
- ``findings_subset`` / ``new_runs_from_events``：WF findings 的 ``warn`` / ``high``
  子集（只返回内容，不调 work-folder MCP），以及从 L0 event 构造 ``scribe.in/1`` 的
  ``new_runs``。

书记员不参与流程、不改任何状态（GO-21）：本模块不调 agent、不调 work-folder MCP、
不写 events.jsonl、不 import langgraph，返回值里没有任何影响 DD / goal 走向的判定。
"""

from __future__ import annotations

import json
import os
import posixpath
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleet_graph.minimal.events import Event
from fleet_graph.minimal.protocol import OBSERVATION_KINDS, OBSERVATION_SEVERITIES

SCRIBE_TRIGGERS: tuple[str, ...] = (
    "goal.turn.finished",
    "dd.merged",
    "dd.failed",
    "goal.done",
    "goal.blocked",
    "goal.warning",
)

# §12「存放」段：只有 severity ∈ {warn, high} 的 observation 进 WF findings.md。
WF_FINDING_SEVERITIES: tuple[str, ...] = ("warn", "high")

# §12 证据闸丢弃一条 observation 时，调用方落 agent.failed 用的 detail 字面量。
DROP_DETAIL = "observation_without_evidence"

# 三种合法的 L0 证据指针键（protocol §12 输出示例）；每条 evidence 恰好携带其一
# （``session`` 指针可另带 ``line`` 定位行号，不算第二种指针）。
_EVIDENCE_POINTER_KEYS: tuple[str, ...] = ("event_seq", "session", "stop_of")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _render(values: tuple[str, ...]) -> str:
    return "{" + ", ".join(values) + "}"


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _normalize_seq_range(seq_range: Any) -> tuple[int, int]:
    if isinstance(seq_range, (str, bytes)) or not isinstance(seq_range, (list, tuple)):
        raise ValueError(f"seq_range must be a (since, until) pair, got {seq_range!r}")
    if len(seq_range) != 2:
        raise ValueError(f"seq_range must have exactly 2 items, got {len(seq_range)}")
    since, until = seq_range
    for value in (since, until):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"seq_range items must be integers, got {seq_range!r}")
    return int(since), int(until)


class ObservationLog:
    """某个 goal 的 L1 observation 日志，落在 ``<goal_run_root>/observations.jsonl``。

    写盘纪律照抄 :class:`fleet_graph.minimal.events.EventLog`：append + flush +
    ``os.fsync``、一行一个 JSON、``ensure_ascii=False``。每行是 ``obs`` 原样再补上
    §12「存放」段的三个书记字段 ``ts`` / ``trigger`` / ``seq_range``（同名 agent 自带
    键被引擎侧取值覆盖）。``read`` 与 events 一样容忍被截断的最后一行，中间损坏仍抛
    ``ValueError``（前者是崩溃点、后者是数据损坏）。
    """

    def __init__(self, goal_run_root: str | os.PathLike[str]) -> None:
        self.goal_run_root = Path(goal_run_root)
        self.path = self.goal_run_root / "observations.jsonl"

    def append(
        self,
        obs: dict[str, Any],
        *,
        trigger: str,
        seq_range: Any,
        ts: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(obs, dict):
            raise ValueError(f"observation must be a JSON object, got {type(obs).__name__}")
        since, until = _normalize_seq_range(seq_range)
        record: dict[str, Any] = {
            **obs,
            "ts": ts if ts is not None else _now_iso(),
            "trigger": trigger,
            "seq_range": [since, until],
        }
        self.goal_run_root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for idx, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                if idx == len(lines) - 1:
                    return
                raise ValueError(f"corrupt observation line {idx + 1}") from None
            if not isinstance(obj, dict):
                raise ValueError(f"observation line {idx + 1} is not a JSON object")
            yield obj


def validate_observation(
    obs: dict[str, Any],
    *,
    since_seq: int,
    until_seq: int,
    session_exists: Callable[[str], bool],
) -> list[str]:
    """§12 证据闸的单条校验：不过闸的原因以字符串列表返回，可解耦地全部收齐。

    只查 spec §12 定的机械规则（``kind`` / ``severity`` 枚举、``evidence`` 非空、
    每条 evidence 恰好是 ``event_seq`` / ``session`` / ``stop_of`` 之一、``event_seq``
    落在 ``[since_seq, until_seq]``、``session`` 过注入探针）；``title`` / ``summary``
    等信封级字段由 :func:`fleet_graph.minimal.protocol.validate` 在 scribe/1 层负责，
    这里不重复。
    """
    errors: list[str] = []
    kind = obs.get("kind")
    if kind not in OBSERVATION_KINDS:
        errors.append(f"kind={kind!r} not in {_render(OBSERVATION_KINDS)}")
    severity = obs.get("severity")
    if severity not in OBSERVATION_SEVERITIES:
        errors.append(f"severity={severity!r} not in {_render(OBSERVATION_SEVERITIES)}")

    evidence = obs.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty list")
        return errors

    for index, entry in enumerate(evidence):
        if not isinstance(entry, dict):
            errors.append(f"evidence[{index}] must be an object")
            continue
        pointers = [key for key in _EVIDENCE_POINTER_KEYS if key in entry]
        if len(pointers) != 1:
            errors.append(
                f"evidence[{index}] must carry exactly one of {_render(_EVIDENCE_POINTER_KEYS)}"
            )
            continue
        pointer = pointers[0]
        if pointer == "event_seq":
            seq = entry["event_seq"]
            if isinstance(seq, bool) or not isinstance(seq, int):
                errors.append(f"evidence[{index}].event_seq must be an integer")
            elif not since_seq <= seq <= until_seq:
                errors.append(
                    f"evidence[{index}].event_seq={seq} outside [{since_seq}, {until_seq}]"
                )
        elif pointer == "session":
            session = entry["session"]
            if not _nonempty_str(session):
                errors.append(f"evidence[{index}].session must be a non-empty string")
            elif not session_exists(session):
                errors.append(f"evidence[{index}].session does not exist: {session!r}")
        else:
            if not _nonempty_str(entry["stop_of"]):
                errors.append(f"evidence[{index}].stop_of must be a non-empty string")
    return errors


def partition_observations(
    observations: list[dict[str, Any]],
    *,
    since_seq: int,
    until_seq: int,
    session_exists: Callable[[str], bool],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按证据闸把 ``observations`` 分成 ``(kept, dropped)``。

    空 ``observations`` 合法：返回两个空列表。每条 ``dropped`` 元素是
    ``{"detail": "observation_without_evidence", "errors": [...], "observation": 原条}``，
    供调用方原样落 ``agent.failed(detail: observation_without_evidence)`` 并保留具体
    原因。本函数只分拣，不落 event、不改任何状态。
    """
    if not isinstance(observations, list):
        raise ValueError(f"observations must be a list, got {type(observations).__name__}")
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for obs in observations:
        if not isinstance(obs, dict):
            dropped.append(
                {
                    "detail": DROP_DETAIL,
                    "errors": ["observation must be an object"],
                    "observation": obs,
                }
            )
            continue
        errors = validate_observation(
            obs, since_seq=since_seq, until_seq=until_seq, session_exists=session_exists
        )
        if errors:
            dropped.append({"detail": DROP_DETAIL, "errors": errors, "observation": obs})
        else:
            kept.append(obs)
    return kept, dropped


def findings_subset(kept: Iterable[dict[str, Any]]) -> list[str]:
    """``severity ∈ {warn, high}`` 的 observation 渲染成 WF findings 的行内容。

    只返回内容（``[severity] title: summary`` 一条一行，按 kept 顺序），不调
    work-folder MCP —— 追加进 WF ``findings.md`` 是调用方的事（§12「存放」段）。
    """
    lines: list[str] = []
    for obs in kept:
        severity = obs.get("severity")
        if severity in WF_FINDING_SEVERITIES:
            lines.append(f"[{severity}] {obs.get('title', '')}: {obs.get('summary', '')}")
    return lines


def new_runs_from_events(
    events: Iterable[Event],
    since_seq: int,
    until_seq: int,
    sessions_dir: str | os.PathLike[str],
) -> list[dict[str, Any]]:
    """从 L0 event 构造 §12 ``scribe.in/1`` 的 ``new_runs``。

    只看 ``since_seq <= seq <= until_seq`` 区间内的三种 event：``agent.spawned``
    注册一个 run（``run_id`` / ``role`` / ``session_dir``），``*.finished``
    （``goal.turn.finished`` / ``dd.stage.finished`` 等）按 payload 里的 ``run_id``
    把该 run 的 Stop 输出原样挂到 ``stop``，``agent.exited`` 补 ``agent.spawned``
    缺失时的 ``role``。没有 finished 的 run（崩溃在半路的）``stop`` 保持 ``None``，
    不编造；区间外或未注册 ``run_id`` 的 finished 一律忽略 —— ``new_runs`` 只描述
    本区间新起的 run。``stop`` 取 finished event 的 payload 原样（引擎从 agent
    stdout 落 event 时即如此，机械键 ``stage`` / ``run_id`` / ``usage`` 一并保留）。
    """
    sessions_root = os.fspath(sessions_dir)
    runs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in events:
        if not since_seq <= ev.seq <= until_seq:
            continue
        payload = ev.payload or {}
        if ev.kind == "agent.spawned":
            run_id = payload.get("run_id")
            if not _nonempty_str(run_id) or run_id in runs:
                continue
            runs[run_id] = {
                "run_id": run_id,
                "role": payload.get("role"),
                "session_dir": posixpath.join(sessions_root, run_id, ""),
                "stop": None,
            }
            order.append(run_id)
        elif ev.kind == "agent.exited":
            run_id = payload.get("run_id")
            if not _nonempty_str(run_id) or run_id not in runs:
                continue
            if runs[run_id].get("role") is None:
                runs[run_id]["role"] = payload.get("role")
        elif ev.kind.endswith(".finished"):
            run_id = payload.get("run_id")
            if not _nonempty_str(run_id) or run_id not in runs:
                continue
            runs[run_id]["stop"] = dict(payload)
    return [runs[run_id] for run_id in order]


__all__ = [
    "DROP_DETAIL",
    "SCRIBE_TRIGGERS",
    "WF_FINDING_SEVERITIES",
    "ObservationLog",
    "findings_subset",
    "new_runs_from_events",
    "partition_observations",
    "validate_observation",
]

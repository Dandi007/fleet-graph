#!/usr/bin/env python3
"""归因报表：turn-timeout 两轨口径分桶（spec 缺陷⑩ d10b 返工，wf-8d9737）。

两轨（d10 监督面更正后的口径）：

    线侧轨  worker_turn_timeout@3000s 预算 —— rounds.jsonl 落档面，
            按 seat_session_id x turn_ordinal x session_age 分桶（seat/model
            降为显示列；旧键 seat x model x round_index 不再是分桶键）。
            缺任一矩阵字段的旧记录进「变量缺失」单列桶，绝不静默丢弃。
            真挂/长 turn 撞顶分类：record 的 timeout_class（落档时按
            「TURN_TIMEOUT 回执时刻 - 会话最后活动时刻」机械判定，见
            goal_line.classify_turn_timeout）；两类之外的如实计
            unclassified，绝不硬塞进某一类。
    dd 侧轨 PROVIDER_UNAVAILABLE@9000s implement fence —— 独立一节，
            只读既有 dd events（events.jsonl），绝不写任何 dd 面。
            按 development x re_prepare 代数 x detail 可析出的 provider
            端点分桶；不可析出的字段如实标「不可得」，严禁编造。

不写任何文件，不改任何状态；无数据时如实报空（buckets 为空、计数为零）
并 exit 0，绝不把空数据报成非空。

用法::

    uv run python scripts/turn-timeout-report.py [PATH ...] [--dd-events PATH ...]

PATH 可以是 rounds.jsonl 文件，也可以是目录（目录会递归查找其中的
rounds.jsonl，例如 /data/fleet-graph/runs/<folder_id>/rounds.jsonl）。
--dd-events 同理，找的是 events.jsonl（dd 引擎事件，只读）。缺省 PATH 为
/data/fleet-graph/runs。输出为 JSON（stdout），恒 exit 0。

线侧变量矩阵（超时轮 record 必带字段，缺任一字段进「变量缺失」桶；与
goal_line.TIMEOUT_MATRIX_FIELDS 同名同序，两处不一致时以 goal_line 为准）::

    seat                  agents.yaml 座位名（显示列）
                          （AgentSessionWorker.turn_variables）
    model                 agent-session argv 解析出的 -m 值/链；取座位会话
                          元数据的 model 字段，runtime 未落时如实记 null
                          （显示列，不再是分桶键）
    round_index           轮序号（worker_turn 的 round_no；不再是分桶键）
    turn_timeout_seconds  本轮超时预算（只记录，绝不调整）
    seat_session_id       座位会话 id（agent-session 侧 session id；分桶键）
    turn_ordinal          turn 序号（本进程对该座位会话的逐 turn 计数；
                          分桶键）
    session_age           会话年龄，秒（runtime 落了 start 时间戳则从之，
                          否则本进程首次 open 的观察起点；分桶键）
    input_bytes           本轮输入 prompt+工具面载荷量级（以实际注入
                          prompt 字节数为机械代理）
    output_evidence       截止超时的产出信号
                          {stdout_lines, last_output_at, zero_output, source}

两轨分类口径（线侧）::

    delta = TURN_TIMEOUT 回执时刻(receipt_at) - 会话最后活动时刻
            (session_last_activity_at，session 目录最新 mtime)
    真挂   true_hang     delta ≈ 0（回执之外会话再无任何可观察活动）
                         或 output_evidence.zero_output 为真（全程零产出）
    撞顶   ceiling_hit   仍在产出（zero_output 为假）且 delta < 预算
                         —— turn 活着、只是预算到顶
    不可得 None         两类都判定不了的如实记 None，报表计 unclassified

已知观察（两轨首批数据点；字段落地前的轮次没有矩阵可录，此处为书面卷宗）::

    轨      座位/单               时刻/预算          产出信号   观察
    线侧    flash 座位            2026-09-03 3000s  零产出     >=1 例 3000s
            （模型未录）                                        零产出超时
                                                               （首轮亲历）
    线侧    glm5.3 座位           2026-09-03 01:5x   ——        切 glm-5.3 后
            glm-5.3               监督面                        round2/3
                                                               零超时
    dd 侧   M5 单 e2/e3           16:10:00Z /        ——        PROVIDER_
                                  16:55:33Z                    UNAVAILABLE
                                                               两例，引擎
                                                               re_prepare
                                                               自愈

最小缓解边界（spec 第 4 条）：goal_line 只做机械透传——超时轮的变量矩阵随
``last_turn_status`` 进入下一轮 coordinator 输入，让接手模型看见上一轮死因；
超时预算与换座策略不属于本仓，是监督面/用户面的事。

本脚本刻意只用标准库，便于在任意环境只读体检。

Exit codes: 0 正常（含无数据/空桶），2 用法错误（路径不存在等）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: 只认这一个 reason 字面量（与 goal_line.WORKER_TURN_TIMEOUT_REASON 同源）。
TURN_TIMEOUT_REASON = "worker_turn_timeout"

#: 超时轮 record 必带的变量矩阵字段（与 goal_line.TIMEOUT_MATRIX_FIELDS 同序）。
MATRIX_FIELDS = (
    "seat",
    "model",
    "round_index",
    "turn_timeout_seconds",
    "seat_session_id",
    "turn_ordinal",
    "session_age",
    "input_bytes",
    "output_evidence",
)

#: 线侧分桶键（d10b：seat x model x round_index 不再是分桶键）。
BUCKET_KEY_FIELDS = ("seat_session_id", "turn_ordinal", "session_age")

#: 线侧两类 timeout_class 字面量（与 goal_line 分类常量同源）。
TIMEOUT_CLASS_TRUE_HANG = "true_hang"
TIMEOUT_CLASS_CEILING_HIT = "ceiling_hit"

#: dd 侧轨：只读的 PROVIDER_UNAVAILABLE 族与 implement fence 阶段名。
DD_FAILURE_CODE = "PROVIDER_UNAVAILABLE"
DD_FENCE_STAGE = "implement"

#: dd 侧不可析出字段的唯一如实写法。
DD_UNAVAILABLE = "不可得"

#: detail 里可析出的 provider 端点（host[:port]）。
_DD_ENDPOINT_PATTERN = re.compile(r"https?://([A-Za-z0-9._-]+(?::\d+)?)")

DEFAULT_RUN_ROOT = "/data/fleet-graph/runs"


def missing_matrix_fields(record: dict[str, Any]) -> list[str]:
    """该超时轮缺失的矩阵字段。「变量缺失」桶的判据，不静默丢弃。"""
    return [name for name in MATRIX_FIELDS if name not in record]


def is_zero_output(record: dict[str, Any]) -> bool:
    """产出信号里的零产出布尔；字段缺失或形态不对一律 False（如实）。"""
    evidence = record.get("output_evidence")
    return isinstance(evidence, dict) and evidence.get("zero_output") is True


def timeout_class_of(record: dict[str, Any]) -> str | None:
    """record 落档时判好的两轨分类；两类之外（含缺失）如实 None。"""
    klass = record.get("timeout_class")
    if klass in (TIMEOUT_CLASS_TRUE_HANG, TIMEOUT_CLASS_CEILING_HIT):
        return str(klass)
    return None


def find_jsonl_files(paths: list[Path], name: str) -> list[Path]:
    """展开参数：文件原样保留，目录递归找同名 jsonl；按路径排序保证确定性。"""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob(name)))
        else:
            files.append(path)
    return sorted(dict.fromkeys(files))


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读一个 jsonl：返回（可解析 record 列表, 不可解析行数）。"""
    records: list[dict[str, Any]] = []
    unparsable = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            unparsable += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            unparsable += 1
    return records, unparsable


def _display_values(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    """显示列（seat/model 不再是分桶键，降为展示）：记首个非空观察值。"""
    for key in ("seat", "model"):
        if bucket.get(key) is None and record.get(key) is not None:
            bucket[key] = record.get(key)


def _count_class(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    klass = timeout_class_of(record)
    if klass == TIMEOUT_CLASS_TRUE_HANG:
        bucket["true_hangs"] += 1
    elif klass == TIMEOUT_CLASS_CEILING_HIT:
        bucket["ceiling_hits"] += 1
    else:
        bucket["unclassified"] += 1


def bucket_rows(
    source: Path, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """一个来源的线侧分桶：键 seat_session_id x turn_ordinal x session_age，
    缺矩阵字段单列变量缺失桶。"""
    buckets: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
    missing_rows: list[dict[str, Any]] = []
    by_missing_field: dict[str, int] = {}

    for record in records:
        if record.get("reason") != TURN_TIMEOUT_REASON:
            continue
        absent = missing_matrix_fields(record)
        if absent:
            for name in absent:
                by_missing_field[name] = by_missing_field.get(name, 0) + 1
            missing_rows.append(
                {
                    "round": record.get("round"),
                    "missing_fields": absent,
                    "zero_output": is_zero_output(record),
                    "timeout_class": timeout_class_of(record),
                }
            )
            continue
        key = (
            str(source),
            record.get("seat_session_id"),
            record.get("turn_ordinal"),
            record.get("session_age"),
        )
        bucket = buckets.setdefault(
            key,
            {
                "source": str(source),
                "seat_session_id": record.get("seat_session_id"),
                "turn_ordinal": record.get("turn_ordinal"),
                "session_age": record.get("session_age"),
                "seat": None,
                "model": None,
                "timeout_rounds": 0,
                "zero_output_timeouts": 0,
                "true_hangs": 0,
                "ceiling_hits": 0,
                "unclassified": 0,
            },
        )
        _display_values(bucket, record)
        bucket["timeout_rounds"] += 1
        if is_zero_output(record):
            bucket["zero_output_timeouts"] += 1
        _count_class(bucket, record)

    rows = sorted(
        buckets.values(),
        key=lambda b: (
            b["source"],
            str(b["seat_session_id"]),
            str(b["turn_ordinal"]),
            str(b["session_age"]),
        ),
    )
    summary = {
        "timeout_rounds": sum(b["timeout_rounds"] for b in rows) + len(missing_rows),
        "zero_output_timeouts": sum(b["zero_output_timeouts"] for b in rows)
        + sum(1 for row in missing_rows if row["zero_output"]),
        "true_hangs": sum(b["true_hangs"] for b in rows)
        + sum(1 for row in missing_rows if row["timeout_class"] == TIMEOUT_CLASS_TRUE_HANG),
        "ceiling_hits": sum(b["ceiling_hits"] for b in rows)
        + sum(1 for row in missing_rows if row["timeout_class"] == TIMEOUT_CLASS_CEILING_HIT),
        "unclassified": sum(b["unclassified"] for b in rows)
        + sum(1 for row in missing_rows if row["timeout_class"] is None),
        "count": len(missing_rows),
        "by_field": dict(sorted(by_missing_field.items())),
        "rows": missing_rows,
    }
    return rows, summary


def _dd_endpoint(detail: Any) -> str | None:
    """detail 可析出的 provider 端点（host[:port]）；析不出如实 None。"""
    if not isinstance(detail, str):
        return None
    match = _DD_ENDPOINT_PATTERN.search(detail)
    return match.group(1) if match else None


def _dd_dimension(value: Any, *, want_int: bool = False) -> Any:
    """分桶维度的如实取值：形态不对就是「不可得」，绝不编造。"""
    if want_int:
        return value if isinstance(value, int) and not isinstance(value, bool) else DD_UNAVAILABLE
    if isinstance(value, str) and value.strip():
        return value
    return DD_UNAVAILABLE


def dd_section(paths: list[Path]) -> dict[str, Any]:
    """dd 侧轨：只读 events.jsonl 的 PROVIDER_UNAVAILABLE 族（implement
    fence 内），按 development x re_prepare 代数 x provider 端点分桶。

    代数与 development 优先取失败事件自带字段；缺失时按同一事件文件里
    implement re_prepare 事件（时间序在前者）如实回填，再没有就标
    「不可得」。fence 外（stage 非 implement）的族事件单计 out_of_fence，
    不静默丢弃，也绝不混进 fence 桶。
    """
    section: dict[str, Any] = {
        "failure_code": DD_FAILURE_CODE,
        "fence_stage": DD_FENCE_STAGE,
        "sources": [],
        "buckets": [],
        "re_prepare": [],
        "totals": {"provider_unavailable": 0, "in_fence": 0, "out_of_fence": 0, "buckets": 0},
    }
    all_buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    re_prepare_rows: dict[tuple[str, str], dict[str, Any]] = {}
    totals = section["totals"]

    for path in find_jsonl_files(paths, "events.jsonl"):
        source = {
            "path": str(path),
            "events": 0,
            "provider_unavailable": 0,
            "in_fence": 0,
            "out_of_fence": 0,
            "re_prepare": 0,
            "unparsable_lines": 0,
        }
        if not path.is_file():
            source["error"] = "not a file"
            section["sources"].append(source)
            continue
        try:
            records, unparsable = read_jsonl(path)
        except OSError as exc:
            source["error"] = f"{type(exc).__name__}: {exc}"
            section["sources"].append(source)
            continue
        source["events"] = len(records)
        source["unparsable_lines"] = unparsable

        # re_prepare 索引（implement fence 内，时间序），供回填失败事件缺失的
        # development/代数——两处都取不到才标「不可得」。
        re_prepares = sorted(
            (
                record
                for record in records
                if record.get("event") == "re_prepare"
                and record.get("stage") == DD_FENCE_STAGE
                and isinstance(record.get("at"), str)
            ),
            key=lambda record: record["at"],
        )

        source["provider_unavailable"] = sum(
            1 for record in records if record.get("failure_code") == DD_FAILURE_CODE
        )
        source["re_prepare"] = len(re_prepares)

        for record in re_prepares:
            development = _dd_dimension(record.get("development_id"))
            generation = _dd_dimension(record.get("generation"), want_int=True)
            row = re_prepare_rows.setdefault(
                (development, generation),
                {"development": development, "generation": generation, "count": 0, "at_times": []},
            )
            row["count"] += 1
            row["at_times"].append(record["at"])

        for record in records:
            if record.get("failure_code") != DD_FAILURE_CODE:
                continue
            if record.get("stage") != DD_FENCE_STAGE:
                source["out_of_fence"] += 1
                totals["out_of_fence"] += 1
                continue
            development = _dd_dimension(record.get("development_id"))
            generation = _dd_dimension(record.get("generation"), want_int=True)
            endpoint = _dd_dimension(_dd_endpoint(record.get("detail")))
            at = record.get("at")
            if (development == DD_UNAVAILABLE or generation == DD_UNAVAILABLE) and isinstance(
                at, str
            ):
                prior = [prep for prep in re_prepares if prep["at"] <= at]
                if prior:
                    latest = prior[-1]
                    if development == DD_UNAVAILABLE:
                        development = _dd_dimension(latest.get("development_id"))
                    if generation == DD_UNAVAILABLE:
                        generation = _dd_dimension(latest.get("generation"), want_int=True)
            key = (development, str(generation), endpoint)
            bucket = all_buckets.setdefault(
                key,
                {
                    "development": development,
                    "generation": generation,
                    "provider_endpoint": endpoint,
                    "count": 0,
                    "at_times": [],
                    "details": [],
                },
            )
            bucket["count"] += 1
            if isinstance(at, str):
                bucket["at_times"].append(at)
            if isinstance(record.get("detail"), str):
                bucket["details"].append(record["detail"])
            source["in_fence"] += 1
            totals["in_fence"] += 1

        section["sources"].append(source)
        totals["provider_unavailable"] += source["provider_unavailable"]

    section["buckets"] = sorted(
        all_buckets.values(),
        key=lambda b: (b["development"], str(b["generation"]), b["provider_endpoint"]),
    )
    section["re_prepare"] = sorted(
        re_prepare_rows.values(),
        key=lambda b: (b["development"], str(b["generation"])),
    )
    for bucket in section["buckets"]:
        bucket["at_times"].sort()
        bucket["details"].sort()
    for row in section["re_prepare"]:
        row["at_times"].sort()
    totals["buckets"] = len(section["buckets"])
    return section


def build_report(paths: list[Path], dd_events: list[Path]) -> dict[str, Any]:
    """整份报表。任何输入缺失/为空都如实报空，绝不虚构数据。"""
    sources: list[dict[str, Any]] = []
    all_bucket_rows: list[dict[str, Any]] = []
    missing_total: dict[str, Any] = {"count": 0, "by_field": {}, "rows": []}
    totals = {
        "records": 0,
        "timeout_rounds": 0,
        "zero_output_timeouts": 0,
        "true_hangs": 0,
        "ceiling_hits": 0,
        "unclassified": 0,
    }

    for path in find_jsonl_files(paths, "rounds.jsonl"):
        if not path.is_file():
            sources.append({"path": str(path), "error": "not a file"})
            continue
        try:
            records, unparsable = read_jsonl(path)
        except OSError as exc:
            sources.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows, missing = bucket_rows(path, records)
        timeout_count = missing["timeout_rounds"]
        zero_count = missing["zero_output_timeouts"]
        sources.append(
            {
                "path": str(path),
                "records": len(records),
                "timeout_records": timeout_count,
                "unparsable_lines": unparsable,
            }
        )
        all_bucket_rows.extend(rows)
        missing_total["count"] += missing["count"]
        for name, count in missing["by_field"].items():
            missing_total["by_field"][name] = missing_total["by_field"].get(name, 0) + count
        missing_total["rows"].extend({"source": str(path), **row} for row in missing["rows"])
        totals["records"] += len(records)
        totals["timeout_rounds"] += timeout_count
        totals["zero_output_timeouts"] += zero_count
        totals["true_hangs"] += missing["true_hangs"]
        totals["ceiling_hits"] += missing["ceiling_hits"]
        totals["unclassified"] += missing["unclassified"]

    all_bucket_rows.sort(
        key=lambda b: (
            b["source"],
            str(b["seat_session_id"]),
            str(b["turn_ordinal"]),
            str(b["session_age"]),
        )
    )
    return {
        "reason": TURN_TIMEOUT_REASON,
        "sources": sources,
        "buckets": all_bucket_rows,
        "missing_variables": missing_total,
        "totals": totals,
        "dd_provider_unavailable": dd_section(dd_events),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        default=[DEFAULT_RUN_ROOT],
        help="rounds.jsonl 文件或目录（目录递归查找）；缺省 /data/fleet-graph/runs",
    )
    parser.add_argument(
        "--dd-events",
        nargs="*",
        default=[],
        help="dd 引擎 events.jsonl 文件或目录（只读）；缺省不读 dd 侧",
    )
    args = parser.parse_args(argv)
    paths = [Path(raw) for raw in args.paths]
    dd_events = [Path(raw) for raw in args.dd_events]
    if not paths:
        parser.error("at least one path is required")
    missing = [str(path) for path in [*paths, *dd_events] if not path.exists()]
    if missing:
        print(f"turn-timeout-report: path(s) do not exist: {', '.join(missing)}", file=sys.stderr)
        return 2
    print(json.dumps(build_report(paths, dd_events), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

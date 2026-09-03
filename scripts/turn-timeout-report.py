#!/usr/bin/env python3
"""归因报表：worker_turn_timeout 变量矩阵分桶（spec 缺陷⑩，wf-8d9737）。

只读 rounds.jsonl（rounds/progress 落档面），按 seat x model x round_index
分桶输出超时率。不写任何文件，不改任何状态；无数据时如实报空（buckets 为
空、计数为零）并 exit 0，绝不把空数据报成非空。

用法::

    uv run python scripts/turn-timeout-report.py [PATH ...]

PATH 可以是 rounds.jsonl 文件，也可以是目录（目录会递归查找其中的
rounds.jsonl，例如 /data/fleet-graph/runs/<folder_id>/rounds.jsonl）。
缺省 PATH 为 /data/fleet-graph/runs。输出为 JSON（stdout），恒 exit 0。

变量矩阵（超时轮 record 必带字段，缺任一字段进「变量缺失」桶）::

    seat                  agents.yaml 座位名
                          （AgentSessionWorker.turn_variables）
    model                 agent-session argv 解析出的 -m 值/链；取座位会话
                          元数据的 model 字段，runtime 未落时如实记 null
    round_index           轮序号（worker_turn 的 round_no）
    turn_timeout_seconds  本轮超时预算（只记录，绝不调整；
                          AgentSessionWorker.turn_timeout_seconds）
    input_bytes           本轮输入 prompt+工具面载荷量级（以实际注入
                          prompt 字节数为机械代理）
    output_evidence       截止超时的产出信号
                          {stdout_lines, last_output_at, zero_output, source}

已知观察（首批数据点；字段落地前的轮次没有矩阵可录，此处为书面卷宗）::

    座位     模型                 round  输入体量      超时预算  产出信号  观察
    flash   flash 档（模型未录）  1      未录（机制前）  3000s    零产出   >=1 例 3000s
                                                                 零产出超时（2026-09-03
                                                                 首轮亲历）
    glm5.3  glm-5.3             2/3    未录（机制前）  3000s    ——      切 glm-5.3 后
                                                                 round2/3 零超时
                                                                 （监督面 01:5x）

最小缓解边界（spec 第 4 条）：goal_line 只做机械透传——超时轮的变量矩阵随
``last_turn_status`` 进入下一轮 coordinator 输入，让接手模型看见上一轮死因；
超时预算与换座策略不属于本仓，是监督面/用户面的事。

本脚本刻意只用标准库，便于在任意环境只读体检；与
``fleet_graph.graphs.goal_line.TIMEOUT_MATRIX_FIELDS`` 保持同名同序，
两处字段清单不一致时以 goal_line 为准（tests/test_turn_timeout_variables.py
盯住这一点）。

Exit codes: 0 正常（含无数据/空桶），2 用法错误（路径不存在等）。
"""

from __future__ import annotations

import argparse
import json
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
    "input_bytes",
    "output_evidence",
)

DEFAULT_RUN_ROOT = "/data/fleet-graph/runs"


def missing_matrix_fields(record: dict[str, Any]) -> list[str]:
    """该超时轮缺失的矩阵字段。「变量缺失」桶的判据，不静默丢弃。"""
    return [name for name in MATRIX_FIELDS if name not in record]


def is_zero_output(record: dict[str, Any]) -> bool:
    """产出信号里的零产出布尔；字段缺失或形态不对一律 False（如实）。"""
    evidence = record.get("output_evidence")
    return isinstance(evidence, dict) and evidence.get("zero_output") is True


def find_rounds_files(paths: list[Path]) -> list[Path]:
    """展开参数：文件原样保留，目录递归找 rounds.jsonl；按路径排序保证确定性。"""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("rounds.jsonl")))
        else:
            files.append(path)
    return sorted(dict.fromkeys(files))


def read_rounds(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读一个 rounds.jsonl：返回（可解析 record 列表, 不可解析行数）。"""
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


def bucket_rows(
    source: Path, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """一个来源的分桶结果：主桶按 seat x model x round_index，缺字段单列变量缺失桶。"""
    buckets: dict[tuple[str, Any, Any, Any], dict[str, Any]] = {}
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
                }
            )
            continue
        key = (
            str(source),
            record.get("seat"),
            record.get("model"),
            record.get("round_index"),
        )
        bucket = buckets.setdefault(
            key,
            {
                "source": str(source),
                "seat": record.get("seat"),
                "model": record.get("model"),
                "round_index": record.get("round_index"),
                "total_rounds": 0,
                "timeout_rounds": 0,
                "zero_output_timeouts": 0,
            },
        )
        bucket["timeout_rounds"] += 1
        if is_zero_output(record):
            bucket["zero_output_timeouts"] += 1

    # 总轮数 = 该来源里同一 round 序号落档的轮记录数（含正常轮）——超时率的分母。
    rounds_at: dict[int, int] = {}
    for record in records:
        round_no = record.get("round")
        if isinstance(round_no, int):
            rounds_at[round_no] = rounds_at.get(round_no, 0) + 1
    for bucket in buckets.values():
        round_index = bucket["round_index"]
        bucket["total_rounds"] = rounds_at.get(round_index, 0)
        bucket["timeout_rate"] = (
            round(bucket["timeout_rounds"] / bucket["total_rounds"], 4)
            if bucket["total_rounds"]
            else 0.0
        )

    rows = sorted(
        buckets.values(),
        key=lambda b: (b["source"], str(b["seat"]), str(b["model"]), str(b["round_index"])),
    )
    summary = {
        "timeout_rounds": sum(b["timeout_rounds"] for b in rows) + len(missing_rows),
        "zero_output_timeouts": sum(b["zero_output_timeouts"] for b in rows)
        + sum(1 for row in missing_rows if row["zero_output"]),
        "count": len(missing_rows),
        "by_field": dict(sorted(by_missing_field.items())),
        "rows": missing_rows,
    }
    return rows, summary


def build_report(paths: list[Path]) -> dict[str, Any]:
    """整份报表。任何输入缺失/为空都如实报空，绝不虚构数据。"""
    sources: list[dict[str, Any]] = []
    all_bucket_rows: list[dict[str, Any]] = []
    missing_total = {"count": 0, "by_field": {}, "rows": []}
    totals = {"records": 0, "timeout_rounds": 0, "zero_output_timeouts": 0}

    for path in find_rounds_files(paths):
        if not path.is_file():
            sources.append({"path": str(path), "error": "not a file"})
            continue
        try:
            records, unparsable = read_rounds(path)
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

    all_bucket_rows.sort(
        key=lambda b: (b["source"], str(b["seat"]), str(b["model"]), str(b["round_index"]))
    )
    return {
        "reason": TURN_TIMEOUT_REASON,
        "sources": sources,
        "buckets": all_bucket_rows,
        "missing_variables": missing_total,
        "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        default=[DEFAULT_RUN_ROOT],
        help="rounds.jsonl 文件或目录（目录递归查找）；缺省 /data/fleet-graph/runs",
    )
    args = parser.parse_args(argv)
    paths = [Path(raw) for raw in args.paths]
    if not paths:
        parser.error("at least one path is required")
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print(f"turn-timeout-report: path(s) do not exist: {', '.join(missing)}", file=sys.stderr)
        return 2
    print(json.dumps(build_report(paths), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

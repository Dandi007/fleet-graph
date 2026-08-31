"""R5：锚点核验（anchor-check）——机器可核验锚点 + 核验率 + 报告头 dr-anchor-rate。

把「机器可核验锚点 + 核验率」接入 ``research_pipeline``：终验 run 的报告每条
conclusion/claim 的 ``[anchor: …]`` 引用必须可机器核验回 evidence，产出
``anchor-check.json``，核验率 >90% 为**软闸门**；``report.md`` 报告头写
``dr-anchor-rate``。

纯脚本（零 LLM、零外呼 IO）：state 只装 id 与计数，正文/verdict 一律落
``run_root/anchor-check.json``，不进 checkpoint。anchor 派生复用
``research_bus.finding_anchor``（同一条 finding 恒得同一条 anchor），不重写、
不新造中间协议。

核验率 ≤90% 是软闸门：响亮记录（报告头 + anchor-check.json + events），不判红、
不改 converge 路由。红绿判定由判据脚本 ``scripts/check_research_anchor.py`` 独立
执行（自检：阴性 fixture 判红、阳性判绿）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fleet_graph.research_bus import EVIDENCE_FILE, REPORT_FILE, finding_anchor

ANCHOR_CHECK_FILE = "anchor-check.json"
ANCHOR_RATE_HEADER = "dr-anchor-rate"
SOFT_GATE_RATE = 0.90

VERDICT_OK = "ok"
VERDICT_FAILED = "failed"
VERDICT_UNANCHORED = "unanchored"

#: 报告内 ``[anchor: …]`` 引用的机械提取（零 LLM）。
ANCHOR_REF_RE = re.compile(r"\[anchor:\s*([^\]]+)\]")


def extract_anchor_refs(text: str) -> list[str]:
    """报告文本里的全部 ``[anchor: …]`` 引用（去壳后的锚点串，按出现顺序）。"""
    return [m.group(1).strip() for m in ANCHOR_REF_RE.finditer(text)]


def report_claim_lines(report: str) -> list[str]:
    """报告的 conclusion/claim 行：非空、非标题、非分隔线（零 LLM 机械切分）。

    跳过空行、markdown 标题（``#`` 开头）与本模块写入的 ``dr-anchor-rate`` 报告头
    行——标题与核验率头是结构不是结论；其余每行视为一条结论。
    """
    lines: list[str] = []
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith(ANCHOR_RATE_HEADER + ":"):
            continue
        if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            continue
        lines.append(stripped)
    return lines


def load_evidence(run_root: Path | str) -> list[dict[str, str]]:
    """读 ``evidence.jsonl``，逐条派生 finding 形状 ``{anchor, quote, claim}``。

    anchor 由 ``research_bus.finding_anchor`` 派生（source@locator，带版本 URI）——
    同一条 finding 恒得同一条 anchor，双源对账据此逐条匹配。
    """
    path = Path(run_root) / EVIDENCE_FILE
    if not path.is_file():
        return []
    out: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        finding = entry.get("finding") or {}
        out.append(
            {
                "anchor": finding_anchor(finding),
                "quote": str(finding.get("quote", "")),
                "claim": str(finding.get("claim", "")),
            }
        )
    return out


def check_anchors(report: str, evidences: list[dict[str, str]]) -> dict[str, Any]:
    """逐条核验 report 的 conclusion/claim 行，产出 ``{claims, summary}``。

    - 有 ``[anchor: …]`` 引用的行：每条引用是一条 claim，命中 evidence ⇒ ok、
      未命中 ⇒ failed；
    - 无 anchor 的行：单列 unanchored（计入分母、不计 ok）；
    - ``rate = ok / total``；``sums_ok = (ok + failed + unanchored == total)``。
    """
    by_anchor: dict[str, dict[str, str]] = {e["anchor"]: e for e in evidences if e.get("anchor")}
    claims: list[dict[str, Any]] = []
    for line in report_claim_lines(report):
        refs = extract_anchor_refs(line)
        if refs:
            for ref in refs:
                matched = by_anchor.get(ref)
                claims.append(
                    {
                        "anchor": ref,
                        "quote": matched["quote"] if matched else "",
                        "claim": line,
                        "verdict": VERDICT_OK if matched else VERDICT_FAILED,
                    }
                )
        else:
            claims.append({"anchor": "", "quote": "", "claim": line, "verdict": VERDICT_UNANCHORED})

    total = len(claims)
    ok = sum(1 for c in claims if c["verdict"] == VERDICT_OK)
    failed = sum(1 for c in claims if c["verdict"] == VERDICT_FAILED)
    unanchored = sum(1 for c in claims if c["verdict"] == VERDICT_UNANCHORED)
    summary = {
        "total": total,
        "ok": ok,
        "failed": failed,
        "unanchored": unanchored,
        "rate": (ok / total) if total else 0.0,
        "sums_ok": (ok + failed + unanchored) == total,
    }
    return {"claims": claims, "summary": summary}


def rate_header(rate: float) -> str:
    """报告头 ``dr-anchor-rate`` 行：三位小数（例 ``dr-anchor-rate: 0.962``）。"""
    return f"{ANCHOR_RATE_HEADER}: {rate:.3f}"


def with_rate_header(report: str, rate: float) -> str:
    """报告头写 ``dr-anchor-rate``：已带同名前缀行则原位替换，否则插入顶部。

    幂等：两次应用产出同一正文（保留尾部换行，不重复插入）。
    """
    header = rate_header(rate)
    lines = report.splitlines()
    if lines and lines[0].strip().startswith(ANCHOR_RATE_HEADER + ":"):
        lines[0] = header
        joined = "\n".join(lines)
        return joined + ("\n" if report.endswith("\n") else "")
    return header + "\n" + report


def check_run(run_root: Path | str) -> dict[str, Any] | None:
    """对一个 run 执行锚点核验：产出 ``anchor-check.json`` + 报告头 ``dr-anchor-rate``。

    无 ``report.md``（fault 路径）时返回 None，不写任何产物。
    """
    run_root = Path(run_root)
    report_path = run_root / REPORT_FILE
    if not report_path.is_file():
        return None
    report = report_path.read_text(encoding="utf-8")
    evidences = load_evidence(run_root)
    result = check_anchors(report, evidences)
    summary = result["summary"]
    (run_root / ANCHOR_CHECK_FILE).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path.write_text(with_rate_header(report, summary["rate"]), encoding="utf-8")
    return result


def judge_run(run_root: Path | str) -> tuple[bool, dict[str, Any]]:
    """终验 run 的机器判据 ①②③：绿 = 全过，红 = 任一不过。

    - ① ``anchor-check.json`` 存在，且 ``summary.rate > 0.90``；
    - ② ``summary.sums_ok == true``（字段为真 **且** ok+failed+unanchored 与 total
      守恒——伪造 sums 判红）；
    - ③ ``report.md`` 报告头含 ``dr-anchor-rate`` 字段。
    """
    run_root = Path(run_root)
    check_path = run_root / ANCHOR_CHECK_FILE
    if not check_path.is_file():
        return False, {
            "reason": f"{ANCHOR_CHECK_FILE} 缺失",
            "rate": 0.0,
            "sums_ok": False,
            "has_header": False,
        }
    data = json.loads(check_path.read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    total = int(summary.get("total", 0))
    ok = int(summary.get("ok", 0))
    failed = int(summary.get("failed", 0))
    unanchored = int(summary.get("unanchored", 0))
    rate = float(summary.get("rate", 0.0))
    declared_sums_ok = bool(summary.get("sums_ok", False))
    conserved = (ok + failed + unanchored) == total
    sums_ok = declared_sums_ok and conserved

    report_path = run_root / REPORT_FILE
    has_header = False
    if report_path.is_file():
        first = report_path.read_text(encoding="utf-8").splitlines()
        if first:
            has_header = first[0].strip().startswith(ANCHOR_RATE_HEADER + ":")

    rate_met = rate > SOFT_GATE_RATE
    verdict = {
        "rate": rate,
        "rate_met": rate_met,
        "sums_ok": sums_ok,
        "has_header": has_header,
    }
    return (rate_met and sums_ok and has_header), verdict


__all__ = [
    "ANCHOR_CHECK_FILE",
    "ANCHOR_RATE_HEADER",
    "ANCHOR_REF_RE",
    "SOFT_GATE_RATE",
    "VERDICT_FAILED",
    "VERDICT_OK",
    "VERDICT_UNANCHORED",
    "check_anchors",
    "check_run",
    "extract_anchor_refs",
    "judge_run",
    "load_evidence",
    "rate_header",
    "report_claim_lines",
    "with_rate_header",
]

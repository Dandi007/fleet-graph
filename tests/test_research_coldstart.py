"""R8：冷启动终验（DoD）——五件套机器可判（判据 ①②③④⑤）。

覆盖六类验证：
1. 判据①：发起命令原文在案（canonical argv 机械记录）+ 全新题目（历史题目判红）。
2. 判据②：run 证据链完整（dispatch/collect 事件 + agent-runs + evidence.jsonl
   可回放 + coverage>0），缺 agent-runs / 缺事件 / 空 evidence 判红。
3. 判据③：报告存在且非空（DeepThought/<topic>/report.md 字节>0），缺归位判红。
4. 判据④：anchor 核验率 >90% 且 sums_ok（复用 R5），核验率 ≤90% 判红。
5. 判据⑤：冷读 subagent verdict==PASS（标题/小节/锚点/实质正文），结构残缺判 FAIL。
6. 判据脚本自检：`scripts/check_research_coldstart.py` 无参运行 exit 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fleet_graph.graphs.research_pipeline import REPORT_FILE
from fleet_graph.research_anchor import (
    ANCHOR_CHECK_FILE,
    SOFT_GATE_RATE,
    check_run,
    judge_run,
)
from fleet_graph.research_bus import EVIDENCE_FILE
from fleet_graph.research_coldstart import (
    COLDREAD_FAIL,
    COLDREAD_PASS,
    HISTORICAL_QUESTIONS,
    canonical_launch_argv,
    cold_read_report,
    judge_cold_read,
    judge_coldstart,
    judge_evidence_chain,
    judge_launch_command,
    judge_report_placed,
    record_launch_argv,
)
from fleet_graph.research_entry import DEEP_THOUGHT_DIR, topic_slug

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_research_coldstart.py"

QUESTION = "R8 冷启动终验：全新题目一条命令无人搀扶，端到端出带锚点可冷读报告"
ARGV = canonical_launch_argv(QUESTION)

ANCHORS = [f"wiki@fake.md:{i}" for i in range(1, 7)]


def _finding(anchor: str) -> dict[str, str]:
    source, _, locator = anchor.partition("@")
    return {
        "claim": f"结论 {anchor}",
        "source": source,
        "quote": f"引文 {anchor}",
        "locator": locator,
    }


def write_evidence(run_root: Path, anchors: list[str]) -> None:
    with (run_root / EVIDENCE_FILE).open("a", encoding="utf-8") as handle:
        for anchor in anchors:
            entry = {
                "at": "2026-09-01T00:00:00Z",
                "clue_id": "c-1",
                "depth": 0,
                "finding": _finding(anchor),
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_events(run_root: Path) -> None:
    with (run_root / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in ("dispatch", "collect", "harvest"):
            handle.write(json.dumps({"event": event, "at": "2026-09-01T00:00:00Z"}) + "\n")


def anchored_report(question: str = QUESTION) -> str:
    lines = [f"# {question}", ""]
    for i, anchor in enumerate(ANCHORS, 1):
        lines.append(f"- 结论 {i} [anchor: {anchor}]")
    lines.extend(["", "## 分歧裁定", "", "### 已裁定分歧"])
    for i, anchor in enumerate(ANCHORS, 1):
        lines.append(f"- RULE: 分歧 {i} 裁决：wiki 证据成立 [anchor: {anchor}]")
    lines.extend(["", "### 开放分歧"])
    lines.append(f"- OPEN DISAGREEMENT: 分歧 7 留待后续 [anchor: {ANCHORS[0]}]")
    lines.extend(["", "### arbiter 裁决"])
    lines.append(f"- verdict: enough [anchor: {ANCHORS[0]}]")
    lines.append(f"- rationale: 证据已充分 [anchor: {ANCHORS[0]}]")
    return "\n".join(lines)


def build_full_run(
    tmp_path: Path, *, name: str = "run", with_wiki: bool = True
) -> tuple[Path, Path]:
    """完整合法终验产物：run_root + wiki_root（含 DeepThought 归位与 anchor-check）。"""
    run_root = tmp_path / name
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "agent-runs").mkdir()
    write_events(run_root)
    write_evidence(run_root, ANCHORS)
    (run_root / REPORT_FILE).write_text(anchored_report(), encoding="utf-8")
    check_run(run_root)
    record_launch_argv(run_root, ARGV)

    wiki = tmp_path / f"{name}-wiki"
    if with_wiki:
        topic_dir = wiki / DEEP_THOUGHT_DIR / topic_slug(QUESTION)
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / "2026-09-01-topic.md").write_bytes((run_root / REPORT_FILE).read_bytes())
        (topic_dir / ANCHOR_CHECK_FILE).write_bytes((run_root / ANCHOR_CHECK_FILE).read_bytes())
    return run_root, wiki


class TestLaunchCommand:
    def test_canonical_argv_is_green(self) -> None:
        ok, verdict = judge_launch_command(ARGV, QUESTION)
        assert ok is True
        assert verdict["exact"] is True
        assert verdict["fresh"] is True

    def test_non_canonical_argv_is_red(self) -> None:
        bad_argv = ["fleet-graph", "research", "run", "--tier", "heavy"]
        ok, verdict = judge_launch_command(bad_argv, QUESTION)
        assert ok is False
        assert verdict["exact"] is False

    def test_historical_question_is_red(self) -> None:
        historical = next(iter(HISTORICAL_QUESTIONS))
        ok, verdict = judge_launch_command(canonical_launch_argv(historical), historical)
        assert ok is False
        assert verdict["fresh"] is False

    def test_record_and_reload_roundtrip(self, tmp_path: Path) -> None:
        path = record_launch_argv(tmp_path, ARGV)
        assert path.is_file()
        from fleet_graph.research_coldstart import load_launch_argv

        assert load_launch_argv(tmp_path) == ARGV


class TestEvidenceChain:
    def _chain_run(
        self,
        tmp_path: Path,
        *,
        agent_runs: bool = True,
        events: bool = True,
        evidence: bool = True,
    ) -> Path:
        run_root = tmp_path / "chain"
        run_root.mkdir(parents=True, exist_ok=True)
        if agent_runs:
            (run_root / "agent-runs").mkdir()
        if events:
            write_events(run_root)
        if evidence:
            write_evidence(run_root, ANCHORS)
        return run_root

    def test_complete_chain_is_green(self, tmp_path: Path) -> None:
        run_root = self._chain_run(tmp_path)
        ok, verdict = judge_evidence_chain(run_root)
        assert ok is True
        assert verdict["coverage"] == len(ANCHORS)
        assert verdict["replayable"] is True
        assert verdict["has_dispatch"] is True
        assert verdict["has_collect"] is True
        assert verdict["agent_runs"] is True

    def test_missing_agent_runs_is_red(self, tmp_path: Path) -> None:
        run_root = self._chain_run(tmp_path, agent_runs=False)
        ok, verdict = judge_evidence_chain(run_root)
        assert ok is False
        assert verdict["agent_runs"] is False

    def test_missing_events_is_red(self, tmp_path: Path) -> None:
        run_root = self._chain_run(tmp_path, events=False)
        ok, verdict = judge_evidence_chain(run_root)
        assert ok is False
        assert verdict["has_dispatch"] is False

    def test_empty_evidence_is_red(self, tmp_path: Path) -> None:
        run_root = self._chain_run(tmp_path, evidence=False)
        ok, verdict = judge_evidence_chain(run_root)
        assert ok is False
        assert verdict["coverage"] == 0

    def test_corrupt_evidence_is_not_replayable(self, tmp_path: Path) -> None:
        run_root = self._chain_run(tmp_path)
        with (run_root / EVIDENCE_FILE).open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        ok, verdict = judge_evidence_chain(run_root)
        assert ok is False
        assert verdict["replayable"] is False


class TestReportPlaced:
    def test_placed_report_is_green(self, tmp_path: Path) -> None:
        run_root, wiki = build_full_run(tmp_path)
        ok, verdict = judge_report_placed(run_root, QUESTION, wiki_root=wiki)
        assert ok is True
        assert verdict["bytes"] > 0

    def test_missing_wiki_placement_is_red(self, tmp_path: Path) -> None:
        run_root, wiki = build_full_run(tmp_path, with_wiki=False)
        ok, _ = judge_report_placed(run_root, QUESTION, wiki_root=wiki)
        assert ok is False

    def test_empty_report_is_red(self, tmp_path: Path) -> None:
        run_root = tmp_path / "empty"
        run_root.mkdir()
        (run_root / REPORT_FILE).write_text("", encoding="utf-8")
        ok, verdict = judge_report_placed(run_root, QUESTION)
        assert ok is False
        assert verdict["bytes"] == 0


class TestAnchor:
    def test_anchored_run_is_green(self, tmp_path: Path) -> None:
        run_root, _ = build_full_run(tmp_path)
        ok, verdict = judge_run(run_root)
        assert ok is True
        assert verdict["rate"] > SOFT_GATE_RATE
        assert verdict["sums_ok"] is True

    def test_low_rate_is_red(self, tmp_path: Path) -> None:
        run_root = tmp_path / "low"
        run_root.mkdir()
        (run_root / REPORT_FILE).write_text(
            "# 问题\n- 结论一\n- 结论二\n- 结论三\n", encoding="utf-8"
        )
        write_evidence(run_root, ANCHORS)
        check_run(run_root)
        ok, verdict = judge_run(run_root)
        assert ok is False
        assert verdict["rate"] <= SOFT_GATE_RATE


class TestColdRead:
    def test_readable_report_passes(self) -> None:
        ok, verdict = cold_read_report(anchored_report())
        assert ok is True
        assert verdict["verdict"] == COLDREAD_PASS

    def test_bare_heading_fails(self) -> None:
        ok, verdict = cold_read_report(f"# {QUESTION}\n")
        assert ok is False
        assert verdict["verdict"] == COLDREAD_FAIL

    def test_no_anchors_fails(self) -> None:
        report = "# 问题\n\n## 结论\n\n只有文字没有锚点，且正文极少。\n"
        ok, verdict = cold_read_report(report)
        assert ok is False
        assert verdict["verdict"] == COLDREAD_FAIL

    def test_run_coldread_matches_run_report(self, tmp_path: Path) -> None:
        run_root, _ = build_full_run(tmp_path)
        ok, verdict = judge_cold_read(run_root)
        assert ok is True
        assert verdict["verdict"] == COLDREAD_PASS


class TestColdstartFivePiece:
    def test_full_positive_is_green(self, tmp_path: Path) -> None:
        run_root, wiki = build_full_run(tmp_path)
        ok, verdict = judge_coldstart(run_root, QUESTION, wiki_root=wiki)
        assert ok is True
        assert verdict["launch_command"]["pass"] is True
        assert verdict["evidence_chain"]["pass"] is True
        assert verdict["report_placed"]["pass"] is True
        assert verdict["anchor"]["pass"] is True
        assert verdict["cold_read"]["pass"] is True

    def test_any_red_piece_makes_overall_red(self, tmp_path: Path) -> None:
        run_root, wiki = build_full_run(tmp_path)
        (run_root / "agent-runs").rmdir()
        ok, verdict = judge_coldstart(run_root, QUESTION, wiki_root=wiki)
        assert ok is False
        assert verdict["evidence_chain"]["pass"] is False


class TestCriterionScript:
    def test_self_check_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(CHECK_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        assert "self_check: pass" in proc.stdout
        first = proc.stdout.splitlines()[0]
        data = json.loads(first)
        assert data["positive"] == "green"
        assert data["negative_launch"] == "red"
        assert data["negative_chain"] == "red"
        assert data["negative_report"] == "red"
        assert data["negative_anchor"] == "red"
        assert data["negative_coldread"] == "red"

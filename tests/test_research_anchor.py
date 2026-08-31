"""R5：锚点核验（anchor-check）——机器可核验锚点 + 核验率软闸门 + 报告头 dr-anchor-rate。

覆盖五类验证：
1. 纯函数核验：`check_anchors` 对 ok / failed / unanchored 的逐条归类，rate 与
   sums_ok 的机器判据（软闸门 rate > 0.90）。
2. 落盘产物：`check_run` 产出 `anchor-check.json`（claims + summary）并在
   report.md 报告头写 `dr-anchor-rate`。
3. 判据：`judge_run` 阳性绿、三种阴性红（无 anchor / 核验率 ≤90% / sums 不平）。
4. 图级端到端：一次真实 run 后 anchor-check.json 存在、报告头有 dr-anchor-rate、
   events 响亮记录；核验率 ≤90% 不改 converge 路由（软闸门，run 正常终态）。
5. 判据脚本自检：`scripts/check_research_anchor.py` 无参运行 exit 0（判据 ④）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fleet_graph.research_anchor import (
    ANCHOR_CHECK_FILE,
    ANCHOR_RATE_HEADER,
    SOFT_GATE_RATE,
    VERDICT_FAILED,
    VERDICT_OK,
    VERDICT_UNANCHORED,
    check_anchors,
    check_run,
    judge_run,
    load_evidence,
    with_rate_header,
)
from fleet_graph.research_bus import finding_anchor

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_research_anchor.py"


def write_evidence(run_root: Path, findings: list[dict]) -> None:
    path = run_root / "evidence.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for finding in findings:
            entry = {"at": "2026-09-01T00:00:00Z", "clue_id": "c1", "depth": 0, "finding": finding}
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def evidence_finding(source: str, locator: str, quote: str = "引文", claim: str = "结论") -> dict:
    return {"claim": claim, "source": source, "quote": quote, "locator": locator}


class TestCheckReport:
    def test_ok_failed_unanchored_classified_per_line(self) -> None:
        report = "\n".join(
            [
                "# 问题",
                "- 结论一 [anchor: wiki@fake.md:1]",
                "- 结论二 [anchor: web@missing.md:9]",
                "- 结论三",
                "",
                "## 分歧裁定",
            ]
        )
        evidences = [
            {"anchor": "wiki@fake.md:1", "quote": "引文一", "claim": "结论一"},
        ]
        result = check_anchors(report, evidences)

        verdicts = [c["verdict"] for c in result["claims"]]
        assert verdicts == [VERDICT_OK, VERDICT_FAILED, VERDICT_UNANCHORED]
        ok_claim = result["claims"][0]
        assert ok_claim["anchor"] == "wiki@fake.md:1"
        assert ok_claim["quote"] == "引文一"

        summary = result["summary"]
        assert summary == {
            "total": 3,
            "ok": 1,
            "failed": 1,
            "unanchored": 1,
            "rate": 1 / 3,
            "sums_ok": True,
        }

    def test_anchor_derivation_reuses_finding_anchor(self, tmp_path: Path) -> None:
        # 同一条 finding 恒得同一条 anchor（source@locator，复用 research_bus）。
        finding = evidence_finding("wiki", "fake.md:1")
        assert finding_anchor(finding) == "wiki@fake.md:1"
        # load_evidence 逐条用 finding_anchor 派生，落盘后仍可读回。
        run_root = tmp_path / "run"
        run_root.mkdir()
        write_evidence(run_root, [finding])
        assert load_evidence(run_root) == [
            {"anchor": "wiki@fake.md:1", "quote": "引文", "claim": "结论"}
        ]


class TestCheckRun:
    def test_writes_anchor_check_and_rate_header(self, tmp_path: Path) -> None:
        run_root = tmp_path / "run"
        run_root.mkdir()
        (run_root / "report.md").write_text(
            "# 问题\n- 结论一 [anchor: wiki@fake.md:1]\n", encoding="utf-8"
        )
        write_evidence(run_root, [evidence_finding("wiki", "fake.md:1")])

        result = check_run(run_root)

        assert result is not None
        assert result["summary"]["rate"] == 1.0
        check_path = run_root / ANCHOR_CHECK_FILE
        assert check_path.is_file()
        persisted = json.loads(check_path.read_text(encoding="utf-8"))
        assert persisted["summary"]["sums_ok"] is True
        assert persisted["summary"]["rate"] == 1.0

        report = (run_root / "report.md").read_text(encoding="utf-8")
        assert report.startswith(f"{ANCHOR_RATE_HEADER}: 1.000")

    def test_missing_report_returns_none(self, tmp_path: Path) -> None:
        run_root = tmp_path / "run"
        run_root.mkdir()
        assert check_run(run_root) is None
        assert not (run_root / ANCHOR_CHECK_FILE).exists()

    def test_header_write_is_idempotent(self) -> None:
        report = "# 问题\n- 结论\n"
        once = with_rate_header(report, 0.5)
        twice = with_rate_header(once, 0.5)
        assert once == twice
        assert twice.startswith(f"{ANCHOR_RATE_HEADER}: 0.500")


class TestJudgeRun:
    def test_positive_green(self, tmp_path: Path) -> None:
        run_root = tmp_path / "positive"
        run_root.mkdir()
        (run_root / "report.md").write_text(
            "# 问题\n- 结论一 [anchor: wiki@fake.md:1]\n- 结论二 [anchor: web@fake.md:2]\n",
            encoding="utf-8",
        )
        write_evidence(
            run_root,
            [
                evidence_finding("wiki", "fake.md:1"),
                evidence_finding("web", "fake.md:2"),
            ],
        )
        check_run(run_root)
        ok, verdict = judge_run(run_root)
        assert ok is True
        assert verdict["rate"] > SOFT_GATE_RATE
        assert verdict["sums_ok"] is True
        assert verdict["has_header"] is True

    def test_negative_no_anchor_red(self, tmp_path: Path) -> None:
        run_root = tmp_path / "no-anchor"
        run_root.mkdir()
        (run_root / "report.md").write_text("# 问题\n- 结论一\n- 结论二\n", encoding="utf-8")
        write_evidence(run_root, [evidence_finding("wiki", "fake.md:1")])
        check_run(run_root)
        ok, verdict = judge_run(run_root)
        assert ok is False
        assert verdict["rate"] == 0.0

    def test_negative_rate_below_or_at_gate_red(self, tmp_path: Path) -> None:
        run_root = tmp_path / "rate"
        run_root.mkdir()
        # 1 命中 + 2 未命中：rate = 1/3 ≤ 0.90 → 红。
        (run_root / "report.md").write_text(
            "# 问题\n"
            "- 结论一 [anchor: wiki@fake.md:1]\n"
            "- 结论二 [anchor: web@missing.md:9]\n"
            "- 结论三 [anchor: web@missing.md:9]\n",
            encoding="utf-8",
        )
        write_evidence(run_root, [evidence_finding("wiki", "fake.md:1")])
        check_run(run_root)
        ok, verdict = judge_run(run_root)
        assert ok is False
        assert verdict["rate"] <= SOFT_GATE_RATE

    def test_negative_sums_unbalanced_red(self, tmp_path: Path) -> None:
        run_root = tmp_path / "sums"
        run_root.mkdir()
        (run_root / "report.md").write_text(
            with_rate_header("# 问题\n- 结论一 [anchor: wiki@fake.md:1]\n", 1.0),
            encoding="utf-8",
        )
        # 伪造 anchor-check.json：ok+failed+unanchored != total（rate 高也判红）。
        fake = {
            "claims": [
                {"anchor": "wiki@fake.md:1", "quote": "", "claim": "x", "verdict": VERDICT_OK}
            ],
            "summary": {
                "total": 3,
                "ok": 3,
                "failed": 1,
                "unanchored": 0,
                "rate": 1.0,
                "sums_ok": False,
            },
        }
        (run_root / ANCHOR_CHECK_FILE).write_text(
            json.dumps(fake, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ok, verdict = judge_run(run_root)
        assert ok is False
        assert verdict["sums_ok"] is False

    def test_missing_anchor_check_red(self, tmp_path: Path) -> None:
        run_root = tmp_path / "empty"
        run_root.mkdir()
        ok, verdict = judge_run(run_root)
        assert ok is False
        assert "缺失" in verdict["reason"]


class TestScriptSelfCheck:
    def test_script_self_check_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(CHECK_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "self_check=pass" in proc.stdout
        assert "positive=green" in proc.stdout
        assert "negative_no_anchor=red" in proc.stdout
        assert "negative_rate=red" in proc.stdout
        assert "negative_sums=red" in proc.stdout


class TestEndToEnd:
    def _run_pipeline(self, tmp_path: Path) -> Path:
        from types import SimpleNamespace
        from typing import Any

        from fleet_graph.executors.agent_run import RunStatus, RunTicket
        from fleet_graph.graphs.research_pipeline import (
            ADVOCATE_ROLE,
            ARBITER_ROLE,
            JUDGE_ROLE,
            OPPONENT_ROLE,
        )
        from fleet_graph.graphs.research_runner import ResearchConfig, run_research

        class FakeTextNode:
            def __init__(self, seed_text: str) -> None:
                self.seed_text = seed_text

            def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
                return SimpleNamespace(
                    text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
                )

        def worker_payload() -> dict[str, Any]:
            return {
                "evidences": [
                    {
                        "quote": "引文一",
                        "claim": "结论一",
                        "source": "wiki",
                        "locator": "fake.md:1",
                        "revision": "r1",
                    }
                ],
                "proposed_clues": [],
                "materials": [],
            }

        def debater_result(body: str) -> dict[str, Any]:
            return {
                "state": "succeeded",
                "exit_code": 0,
                "structured_result": {"body": body},
            }

        judge_body = "RULE: 分歧一 裁决：wiki 证据成立 [anchor: wiki@fake.md:1]"

        class FakeLauncher:
            def __init__(self) -> None:
                self._roles: dict[str, str] = {}
                self._launched: set[str] = set()
                self.dispatched: list[str] = []

            def launch(self, spec: Any, run_id: str) -> RunTicket:
                if run_id not in self._launched:
                    self._launched.add(run_id)
                    self._roles[run_id] = spec.role
                return RunTicket(run_id, f"/tmp/ra/{run_id}", None)

            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                role = self._roles[ticket.run_id]
                payload: dict[str, Any]
                if role == ARBITER_ROLE:
                    payload = {"verdict": "enough", "rationale": "证据已充分"}
                elif role in {ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE}:
                    body = judge_body if role == JUDGE_ROLE else "# body\n支持。"
                    payload = {"body": body}
                else:
                    payload = worker_payload()
                return RunStatus(
                    "succeeded",
                    {"state": "succeeded", "exit_code": 0, "structured_result": payload},
                )

        run_root = tmp_path / "run"
        config = ResearchConfig(question="R5 锚点核验端到端", run_root=run_root)
        result = run_research(
            config, text_node=FakeTextNode(json.dumps(["单一 wiki 线索"])), launcher=FakeLauncher()
        )
        assert result["terminal"] in {"converged", "capped", "partial"}
        return run_root

    def test_pipeline_produces_anchor_check_and_header(self, tmp_path: Path) -> None:
        run_root = self._run_pipeline(tmp_path)

        check_path = run_root / ANCHOR_CHECK_FILE
        assert check_path.is_file()
        persisted = json.loads(check_path.read_text(encoding="utf-8"))
        summary = persisted["summary"]
        # 机器判据：exists + sums_ok 恒真（由 check_anchors 构造，必然守恒）。
        assert summary["sums_ok"] is True
        assert summary["total"] == summary["ok"] + summary["failed"] + summary["unanchored"]

        report = (run_root / "report.md").read_text(encoding="utf-8")
        assert report.startswith(ANCHOR_RATE_HEADER + ":")
        # 软闸门：核验率按真实产物写入报告头。
        assert f"{ANCHOR_RATE_HEADER}: {summary['rate']:.3f}" in report

        events = [
            json.loads(line)
            for line in (run_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        anchor_event = next(e for e in events if e["event"] == "anchor_check")
        assert anchor_event["rate"] == summary["rate"]
        assert anchor_event["met"] == (summary["rate"] > SOFT_GATE_RATE)
        assert anchor_event["total"] == summary["total"]

    def test_soft_gate_does_not_change_terminal(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from typing import Any

        from fleet_graph.executors.agent_run import RunStatus, RunTicket
        from fleet_graph.graphs.research_pipeline import (
            ADVOCATE_ROLE,
            ARBITER_ROLE,
            JUDGE_ROLE,
            OPPONENT_ROLE,
        )
        from fleet_graph.graphs.research_runner import ResearchConfig, run_research

        class FakeTextNode:
            def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
                return SimpleNamespace(
                    text=json.dumps(["单一 wiki 线索"]),
                    model="fake",
                    finish_reason="stop",
                    usage={},
                    raw={},
                )

        # judge 产出无 [anchor: …] 引用的分歧裁定 → 报告核验率 0（≤90%），软闸门未过。
        judge_body = "RULE: 分歧一 裁决：wiki 证据成立"

        class FakeLauncher:
            def __init__(self) -> None:
                self._roles: dict[str, str] = {}
                self._launched: set[str] = set()

            def launch(self, spec: Any, run_id: str) -> RunTicket:
                if run_id not in self._launched:
                    self._launched.add(run_id)
                    self._roles[run_id] = spec.role
                return RunTicket(run_id, f"/tmp/ra/{run_id}", None)

            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                role = self._roles[ticket.run_id]
                if role == ARBITER_ROLE:
                    payload: dict[str, Any] = {"verdict": "enough", "rationale": "证据已充分"}
                elif role in {ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE}:
                    payload = {"body": judge_body if role == JUDGE_ROLE else "# body\n支持。"}
                else:
                    payload = {
                        "evidences": [
                            {
                                "quote": "引文一",
                                "claim": "结论一",
                                "source": "wiki",
                                "locator": "fake.md:1",
                            }
                        ],
                        "proposed_clues": [],
                        "materials": [],
                    }
                return RunStatus(
                    "succeeded",
                    {"state": "succeeded", "exit_code": 0, "structured_result": payload},
                )

        run_root = tmp_path / "run"
        config = ResearchConfig(question="R5 软闸门不改路由", run_root=run_root)
        result = run_research(config, text_node=FakeTextNode(), launcher=FakeLauncher())
        # 软闸门 ≠ 放行：run 正常 finalise，终态与 route 不受核验率影响。
        assert result["terminal"] in {"converged", "capped", "partial"}
        check_path = run_root / ANCHOR_CHECK_FILE
        assert check_path.is_file()
        summary = json.loads(check_path.read_text(encoding="utf-8"))["summary"]
        assert summary["rate"] <= SOFT_GATE_RATE
        events = [
            json.loads(line)
            for line in (run_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        anchor_event = next(e for e in events if e["event"] == "anchor_check")
        assert anchor_event["met"] is False
        # 响亮记录：报告头仍写核验率（未达标），但 run 已正常 finalise。
        report = (run_root / "report.md").read_text(encoding="utf-8")
        assert report.startswith(ANCHOR_RATE_HEADER + ":")

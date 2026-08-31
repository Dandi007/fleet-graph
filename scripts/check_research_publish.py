"""R1-返工 acceptance：中间态真机落 bus + 降级响亮化（机器可判）。

三个判据（对应 approved.md「判据」节，可重复执行、exit code 判定）：

- **R1-a**：一次真实 run 之后，``research:r-<id>.{index,evidence,docs}`` 三个
  频道在 bus 上**存在且 ``head_seq > 0``**（clue/evidence/doc 真机落 bus）。
- **R1-b**：受控 probe 令 publish 全程 403 时，run 必须以**可观测降级态**
  收尾（``publish_degraded`` 非空），**不得报绿**。
- **阴性 fixture**：``r-2193db185d0f``（bus 上零频道）——新判据必须在它上面
  判红，否则脚本无效。

R1-a 需要真实 bus（默认 ``--bus-url`` 指向本机 agent-bus，凭据走
``FLEET_GRAPH_BUS_TOKEN`` / ``FLEET_GRAPH_BUS_TOKEN_FILE``）；R1-b 用纯 stub
（publish 恒 403）跑真实 pipeline，不依赖网络。三者全绿 exit 0，任一红 exit 非零。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient
from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    JUDGE_ROLE,
    OPPONENT_ROLE,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research
from fleet_graph.research_bus import clue_index_channel, docs_channel, evidence_channel

NEGATIVE_FIXTURE_RESEARCH_ID = "r-2193db185d0f"

CHANNEL_SUFFIXES = ("index", "evidence", "docs")


def channel_head_seq(client: Any, channel: str) -> int:
    """一个频道的 head_seq；频道缺失 / 读取失败记 0（零频道 = 判红素材）。"""
    try:
        _, head = client.messages(channel, limit=1)
        return int(head or 0)
    except Exception:
        return 0


def research_channel_heads(client: Any, research_id: str) -> dict[str, int]:
    """三个 research 频道的 head_seq 快照：``{suffix: head_seq}``。"""
    heads: dict[str, int] = {}
    for suffix in CHANNEL_SUFFIXES:
        if suffix == "index":
            channel = clue_index_channel(research_id)
        elif suffix == "evidence":
            channel = evidence_channel(research_id)
        else:
            channel = docs_channel(research_id)
        heads[suffix] = channel_head_seq(client, channel)
    return heads


def judge_publish(
    channel_heads: dict[str, int], publish_degraded: dict[str, Any] | None
) -> tuple[bool, dict[str, Any]]:
    """R1-a / R1-b 的机器判据。

    绿 = 三个频道 head_seq 全 > 0 且 ``publish_degraded`` 为空（count == 0）。
    任一频道 head_seq == 0，或 ``publish_degraded`` 非空（降级发生了却没报出来
    / 发布了却没落地）→ 红。``publish_degraded`` 非空时一律判红——降级必须
    响亮可观测，不得报绿（P2 硬约束）。
    """
    degraded = bool(publish_degraded) and int(publish_degraded.get("count", 0)) > 0
    missing = sorted(s for s, h in channel_heads.items() if h <= 0)
    ok = (not missing) and (not degraded)
    verdict = {
        "channel_heads": channel_heads,
        "publish_degraded": publish_degraded or {"count": 0, "first_error": None},
        "degraded": degraded,
        "missing_channels": missing,
        "pass": ok,
    }
    return ok, verdict


class FakeTextNode:
    """seed 的替身：回放脚本化文本（可复现）。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload(claim: str) -> dict[str, Any]:
    return {
        "evidences": [{"claim": claim, "source": "wiki", "quote": claim, "locator": "fake.md:1"}],
        "proposed_clues": [],
        "materials": [],
    }


class FakeLauncher:
    """worker / synthesis 的替身：按 role 回放脚本（可复现）。

    worker/synthesis 回放的是裸结构化结果，wait 时包成 agent-run 信封
    （``structured_result``）——与 tests 里 worker_result/synthesis_result
    同形，pipeline 侧 parse_envelope 才能拆出来。
    """

    #: R4 后终局 LLM 面 = 对抗子图四角色（advocate/opponent/judge/arbiter），
    #: 按角色常量回放固定信封——与 tests 里 default_debate() 同形。
    DEBATE_REPLAY: ClassVar[dict[str, dict[str, Any]]] = {
        ADVOCATE_ROLE: {"body": "# advocate 论证\n正面。"},
        OPPONENT_ROLE: {"body": "# opponent 论证\n反驳。"},
        JUDGE_ROLE: {"body": "# judge 裁定\n暂无分歧。"},
        ARBITER_ROLE: {"verdict": "enough", "rationale": "证据已充分"},
    }

    def __init__(self, workers: list[dict[str, Any]]) -> None:
        self.workers = list(workers)
        self.roles: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self.roles[run_id] = spec.role
        return RunTicket(run_id, f"/tmp/check-research/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self.roles[ticket.run_id]
        if role in self.DEBATE_REPLAY:
            return RunStatus(
                "succeeded",
                {
                    "state": "succeeded",
                    "exit_code": 0,
                    "structured_result": self.DEBATE_REPLAY[role],
                },
            )
        item = self.workers.pop(0)
        return RunStatus(
            "succeeded", {"state": "succeeded", "exit_code": 0, "structured_result": item}
        )


class ForbiddenPublisher:
    """R1-b 的受控 probe：publish 全程 403（模拟旧根因：无权委托）。"""

    def publish(self, *args: Any, **kwargs: Any) -> Any:
        from fleet_graph.bus.client import BusError

        raise BusError(403, '{"code": "DELEGATION_NOT_PERMITTED"}')


def run_one_question(question: str, publisher: Any, result_path: Path) -> dict[str, Any]:
    """跑一次真实 pipeline（fake text/launcher + 注入 publisher），返回 result。"""
    seed = FakeTextNode(json.dumps(["调度器的基本循环"]))
    launcher = FakeLauncher([worker_payload("每轮 tick 检查所有 line")])
    config = ResearchConfig(question=question, run_root=result_path)
    return run_research(config, text_node=seed, launcher=launcher, publisher=publisher)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    parser.add_argument(
        "--bus-token-file",
        default=os.environ.get("FLEET_GRAPH_BUS_TOKEN_FILE"),
        help="real-bus 凭据（默认 $FLEET_GRAPH_BUS_TOKEN_FILE）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.bus_token_file:
        token = Path(args.bus_token_file).read_text().strip()
    elif os.environ.get("FLEET_GRAPH_BUS_TOKEN"):
        token = os.environ["FLEET_GRAPH_BUS_TOKEN"].strip()
    else:
        token = None
    client = BusClient(
        base_url=args.bus_url,
        token=token,
        agent_id="fleet-graph",
        own_agent_id="fleet-graph",
    )
    results = {
        "acceptance": "check-research-publish",
        "utc_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    with tempfile.TemporaryDirectory() as td:
        unique = uuid.uuid4().hex[:8]
        result = run_one_question(
            f"R1-返工 验收真实 bus 落盘判据 {unique}", client, Path(td) / "run"
        )
        rid = result["research_id"]
        heads = research_channel_heads(client, rid)
        ok_a, verdict_a = judge_publish(heads, result.get("publish_degraded"))
        results["r1_a"] = {"research_id": rid, "terminal": result.get("terminal"), **verdict_a}

    with tempfile.TemporaryDirectory() as td:
        result = run_one_question(
            "R1-返工 验收：403 降级响亮化判据", ForbiddenPublisher(), Path(td) / "run"
        )
        heads = research_channel_heads(client, result["research_id"])
        ok_b, verdict_b = judge_publish(heads, result.get("publish_degraded"))
        degraded_seen = verdict_b["degraded"]
        ok_b_expected = degraded_seen and (not ok_b)
        results["r1_b"] = {
            "research_id": result["research_id"],
            "terminal": result.get("terminal"),
            **verdict_b,
            "degraded_observable": degraded_seen,
        }

    fixture_heads = research_channel_heads(client, NEGATIVE_FIXTURE_RESEARCH_ID)
    ok_neg, verdict_neg = judge_publish(fixture_heads, None)
    results["negative_fixture"] = {
        "research_id": NEGATIVE_FIXTURE_RESEARCH_ID,
        **verdict_neg,
    }

    all_ok = ok_a and ok_b_expected and (not ok_neg)
    results["pass"] = all_ok
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))

    if not ok_a:
        print("r1_a=RED: 真实 run 后三个频道未全部落地 (head_seq>0)", file=sys.stderr)
    if not ok_b_expected:
        print(
            "r1_b=RED: 403 probe 未能以可观测降级态收尾（publish_degraded 非空、不得报绿）",
            file=sys.stderr,
        )
    if ok_neg:
        print(
            f"negative_fixture=RED: 阴性 fixture {NEGATIVE_FIXTURE_RESEARCH_ID} "
            " 本应零频道判红却判绿，脚本无效",
            file=sys.stderr,
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

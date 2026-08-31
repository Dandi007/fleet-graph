#!/usr/bin/env python3
"""R7 acceptance：部署契约 preflight + 失败语义哨兵（机器可判，判据 ①②③）。

判据（approved.md「判据」节，零 LLM、只读探测）：

- **① preflight 四面**：检出对齐（build head == 期望 head）/ 依赖齐（.venv/uv +
  12 个 dr-* 与 research synth 的 role yaml 在位可解析）/ role 可派（route/runtime
  声明可解析）/ channel 可建（bus 探测待建 channel 可创建）。已知好 fixture 判绿、
  坏 fixture 判红，**缺一面判红**。
- **② worker 无产出**：受控 probe（fake worker 返回全空）复现 → run 必须以响亮
  终态收尾——非 succeeded/exit 0、不判 converged（判据脚本对阴性 fixture 判红）。
- **③ 哨兵被杀 / checkpoint 卡死**：受控 probe 复现 → 响亮终态；且 agent-runtime
  立案号 dev-fg-67feadc91821 在案。

无参运行 = 全部判据在临时 fixture / fake 上自检（禁触真网/真库），任一失当
exit 非零。可解析输出为 JSON（``pass`` 布尔），stderr 打 ``self_check: pass|fail``。
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_pipeline import DEBATE_ROLES
from fleet_graph.graphs.research_runner import ResearchConfig, run_research
from fleet_graph.research_preflight import (
    REQUIRED_ROLE_YAMLS,
    judge_channel,
    judge_checkout,
    judge_deps,
    judge_role,
    preflight_green,
)
from fleet_graph.research_sentinel import (
    AGENT_RUNTIME_SEAT_CONTRACT_CASE,
    judge_loud,
    tracking_case_on_file,
    worker_output_all_empty,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACKING_DOC = REPO_ROOT / "docs" / "findings" / "agent-runtime-seat-contract-tracking.md"


# --- 判据① fixture 自检 --------------------------------------------------------


def _write_role_yaml(roles_root: Path, name: str, *, runtime: str = "claude") -> Path:
    path = roles_root / f"{name}.yaml"
    path.write_text(
        f"role: {name}\nversion: 1\nruntime: {runtime}\nmodel: fake\n"
        f"protocol:\n  output:\n    kind: fake.result.v1\n",
        encoding="utf-8",
    )
    return path


def _good_roles_root(tmp: Path) -> Path:
    roles = tmp / "roles-good"
    roles.mkdir()
    for name in REQUIRED_ROLE_YAMLS:
        _write_role_yaml(roles, name)
    return roles


def self_check_facets() -> tuple[bool, dict[str, Any]]:
    """四面在已知好/坏 fixture 上判绿/红；缺一面判红（脚本自身无效即判据失败）。"""
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        good_roles = _good_roles_root(tmp)
        ok_checkout, verdict = judge_checkout("abc123", "abc123")
        results["facets_checkout"] = "green" if ok_checkout else "red"
        results["checkout"] = verdict
        ok_deps, verdict = judge_deps(good_roles, venv_ok=True, uv_ok=True)
        results["facets_deps"] = "green" if ok_deps else "red"
        results["deps"] = verdict
        ok_role, verdict = judge_role(good_roles)
        results["facets_role"] = "green" if ok_role else "red"
        results["role"] = verdict
        ok_channel, verdict = judge_channel(lambda: None)
        results["facets_channel"] = "green" if ok_channel else "red"
        results["channel"] = verdict
        results["good_fixture_green"] = preflight_green(
            {"checkout": ok_checkout, "deps": ok_deps, "role": ok_role, "channel": ok_channel}
        )

        # 坏 fixture：每个 facet 各造一个必红样本。
        _, verdict = judge_checkout("abc123", "def456")
        results["bad_checkout_red"] = not verdict["aligned"]
        results["bad_checkout"] = verdict

        bad_roles = tmp / "roles-bad"
        bad_roles.mkdir()
        for i, name in enumerate(REQUIRED_ROLE_YAMLS):
            if i == 0:
                continue  # 缺 dr-arbiter → 依赖齐必红
            _write_role_yaml(bad_roles, name)
        ok_bad, verdict = judge_deps(bad_roles, venv_ok=True, uv_ok=True)
        results["bad_deps_red"] = not ok_bad
        results["bad_deps"] = verdict

        role_bad = tmp / "roles-role-bad"
        role_bad.mkdir()
        _write_role_yaml(role_bad, "dr-arbiter", runtime="")
        for name in REQUIRED_ROLE_YAMLS[1:]:
            _write_role_yaml(role_bad, name)
        ok_bad, verdict = judge_role(role_bad)
        results["bad_role_red"] = not ok_bad
        results["bad_role"] = verdict

        def _bad_probe() -> None:
            raise RuntimeError("channel create refused")

        ok_bad, verdict = judge_channel(_bad_probe)
        results["bad_channel_red"] = not ok_bad
        results["bad_channel"] = verdict

        # 任一 facet 红 → 整体红（缺一面判红）。
        results["missing_facet_red"] = not preflight_green(
            {"checkout": False, "deps": True, "role": True, "channel": True}
        )

    passed = (
        results["facets_checkout"] == "green"
        and results["facets_deps"] == "green"
        and results["facets_role"] == "green"
        and results["facets_channel"] == "green"
        and results["good_fixture_green"]
        and results["bad_checkout_red"]
        and results["bad_deps_red"]
        and results["bad_role_red"]
        and results["bad_channel_red"]
        and results["missing_facet_red"]
    )
    return passed, results


# --- 判据②③：受控 probe（fake worker/launcher 驱动真实图，不碰真网/真库） -------


class FakeTextNode:
    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def _debater_payload(body: str) -> dict[str, Any]:
    return {"state": "succeeded", "exit_code": 0, "structured_result": {"body": body}}


def _arbiter_payload() -> dict[str, Any]:
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"verdict": "enough", "rationale": "证据已充分"},
    }


class Boom(RuntimeError):
    """哨兵被杀：在 collect 的 wait 中炸掉（站替 SIGKILL）。"""


class FakeLauncher:
    def __init__(self, worker_payloads: list[dict[str, Any]] | None = None, *, boom: bool = False):
        self.workers = list(worker_payloads or [])
        self.boom = boom
        self._roles: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self._roles[run_id] = spec.role
        return RunTicket(run_id, f"/tmp/preflight/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        if self.boom:
            raise Boom("sentinel killed during worker run")
        role = self._roles[ticket.run_id]
        if role in DEBATE_ROLES:
            if role.endswith("dr-arbiter"):
                return RunStatus("succeeded", _arbiter_payload())
            return RunStatus("succeeded", _debater_payload(f"#{role} 论证\npreflight 通过。"))
        payload = self.workers.pop(0)
        return RunStatus(
            "succeeded", {"state": "succeeded", "exit_code": 0, "structured_result": payload}
        )


def _empty_worker_payload() -> dict[str, Any]:
    """worker.result.v1 全空：三字段全无产出（判据②的受控形态）。"""
    return {"evidences": [], "proposed_clues": [], "materials": []}


def probe_worker_no_output() -> tuple[bool, dict[str, Any]]:
    """判据②：fake worker 返回全空 → run 以响亮终态收尾（非 succeeded/exit 0、
    不判 converged）。"""
    with tempfile.TemporaryDirectory() as td:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher([_empty_worker_payload()])
        config = ResearchConfig(question="worker 无产出", run_root=Path(td) / "run")
        result = run_research(config, text_node=seed, launcher=launcher)

        loud, verdict = judge_loud(result)
        not_converged = result.get("terminal") != "converged"
        evidence_empty = not (Path(td) / "run" / "evidence.jsonl").is_file()
        predicate_ok = worker_output_all_empty(_empty_worker_payload())

        # 阴性 fixture：伪造一个「全空却判 converged」的结果，判据脚本必须判红。
        fake_silent: dict[str, Any] = {
            "terminal": "converged",
            "terminal_reason": "coverage 收敛：1/1 clues done",
        }
        loud_ok_fake, fake_verdict = judge_loud(fake_silent)
        negative_red = (not loud_ok_fake) and fake_verdict["exit_zero"]

    verdict["negative_fixture_red"] = negative_red
    verdict["not_converged"] = not_converged
    verdict["evidence_empty"] = evidence_empty
    verdict["predicate_all_empty"] = predicate_ok
    ok = loud and not_converged and evidence_empty and negative_red and predicate_ok
    return ok, verdict


def probe_sentinel_killed() -> tuple[bool, dict[str, Any]]:
    """判据③前半：哨兵被杀（wait 中 Boom）→ run 以响亮 fault 收尾。"""
    with tempfile.TemporaryDirectory() as td:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(boom=True)
        config = ResearchConfig(question="哨兵被杀", run_root=Path(td) / "run")
        result = run_research(config, text_node=seed, launcher=launcher)
        loud, verdict = judge_loud(result)
    return loud, verdict


def probe_checkpoint_stuck() -> tuple[bool, dict[str, Any]]:
    """判据③后半：checkpoint 卡死（resume 时哨兵仍被杀）→ run 以响亮 fault 收尾。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from fleet_graph.graphs.research_runner import build_research, resume_start

    with tempfile.TemporaryDirectory() as td:
        run_root = Path(td) / "run"
        run_root.mkdir(parents=True, exist_ok=True)
        config = ResearchConfig(question="checkpoint 卡死", run_root=run_root)
        cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 100}

        # 第一次：collect 的 wait 中炸掉，留下指向 collect 的 checkpoint。
        boom = FakeLauncher(boom=True)
        graph, _deps = build_research(
            config, text_node=FakeTextNode(json.dumps(["clue one"])), launcher=boom
        )
        with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
            compiled = graph.compile(checkpointer=saver)
            with contextlib.suppress(Boom):
                compiled.invoke(resume_start(compiled, cfg, config), config=cfg)

        # 第二次：同 identity 续跑，哨兵仍被杀 → checkpoint 卡死，必须响亮 fault。
        result = run_research(
            ResearchConfig(question="checkpoint 卡死", run_root=run_root),
            text_node=FakeTextNode(json.dumps(["clue one"])),
            launcher=FakeLauncher(boom=True),
        )
        loud, verdict = judge_loud(result)
    return loud, verdict


def check_tracking_case() -> tuple[bool, dict[str, Any]]:
    """判据③的「在案」：立案号 dev-fg-67feadc91821 有跟踪文件引用。"""
    on_file = tracking_case_on_file()
    doc_present = TRACKING_DOC.is_file()
    verdict: dict[str, Any] = {
        "case": AGENT_RUNTIME_SEAT_CONTRACT_CASE,
        "tracking_doc_present": doc_present,
        "case_referenced": on_file,
    }
    return (doc_present and on_file), verdict


def self_check() -> tuple[bool, dict[str, Any]]:
    results: dict[str, Any] = {}
    ok_facets, results["facets"] = self_check_facets()
    ok_no_out, results["worker_no_output"] = probe_worker_no_output()
    ok_killed, results["sentinel_killed"] = probe_sentinel_killed()
    ok_stuck, results["checkpoint_stuck"] = probe_checkpoint_stuck()
    ok_tracking, results["tracking"] = check_tracking_case()
    ok = ok_facets and ok_no_out and ok_killed and ok_stuck and ok_tracking
    results["pass"] = ok
    return ok, results


def main(argv: list[str] | None = None) -> int:
    ok, results = self_check()
    print(json.dumps(results, ensure_ascii=False, sort_keys=True, default=str))
    if not ok:
        print("research-preflight self_check: fail", file=sys.stderr)
        return 1
    print("research-preflight self_check: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

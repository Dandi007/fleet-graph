"""R1 acceptance：clue / evidence / doc 中间态落 agent-bus append-only。

一律用 fake transport 注入，**不写真实 channel**（元宪法「bus append-only 敬畏」）。
四个 acceptance 场景：

1. 三类中间态确实发布：一次 fake run 后，fake transport 记录里出现
   ``research.clue.v2`` / ``research.evidence.v2`` / ``research.doc.v2``，
   kind / 字段 / channel 命名与本 spec 约定一致；clue 版本链（``supersedes``）正确。
2. 双源 diff 检查可跑且绿：本地 mirror 与 bus 实体逐条对账一致 exit 0；
   人为制造不一致（删一条 evidence）exit 非零。
3. consumer schema 从 registry 派生：payload 校验 schema 来自 fake transport 的
   ``GET /v1/protocols`` 响应；改写 registry 后校验行为随之改变（未手抄 schema）。
4. kill-restart 从 bus 完整回放一次 run：部分节点完成中断 -> resume -> bus 回放
   重建完整轨迹，与本地镜像 / 最终 result.json 一致，无重复实体（幂等）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.bus.client import BusClient
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    DEFAULT_SOURCE,
    JUDGE_ROLE,
    OPPONENT_ROLE,
    TERMINAL_CONVERGED,
    derive_clue_id,
    derive_research_id,
)
from fleet_graph.graphs.research_runner import (
    RESULT,
    ResearchConfig,
    build_research,
    resume_start,
    run_research,
)
from fleet_graph.research_bus import (
    RESEARCH_CLUE_KIND,
    RESEARCH_DOC_KIND,
    RESEARCH_EVIDENCE_KIND,
    check_dual_source,
    clue_idempotency_key,
    clue_index_channel,
    clue_payload,
    docs_channel,
    dual_source_diff,
    evidence_channel,
    finding_anchor,
    payload_errors,
    publish_best_effort,
    replay_research,
)


class FakeTextNode:
    """seed 的替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text
        self.calls = 0

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    return {
        "evidences": [
            {"claim": c, "source": "wiki", "quote": c, "locator": f"fake.md:{i + 1}"}
            for i, c in enumerate(claims)
        ],
        "proposed_clues": [{"clue": t, "reason": "测试线索"} for t in proposed],
        "materials": [],
    }


def worker_result(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": worker_payload(claims, proposed),
    }


def debater_result(body: str) -> dict[str, Any]:
    """dr-doc.result.v1 形状的成功信封。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"body": body},
    }


def arbiter_result() -> dict[str, Any]:
    """dr-arbiter.result.v1 形状的成功信封。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"verdict": "enough", "rationale": "证据已充分"},
    }


def default_debate() -> dict[str, Any]:
    """R4 四角色的回放信封（按角色常量寻址）。"""
    return {
        ADVOCATE_ROLE: debater_result("# advocate 论证\n正面。"),
        OPPONENT_ROLE: debater_result("# opponent 论证\n反驳。"),
        JUDGE_ROLE: debater_result("# judge 裁定\n暂无分歧。"),
        ARBITER_ROLE: arbiter_result(),
    }


class Boom(RuntimeError):
    """站替 SIGKILL：在 collect 的 wait 中炸掉，留下可续跑的 checkpoint。"""


class FakeLauncher:
    def __init__(
        self,
        worker_script: list[Any],
        debate: dict[str, Any] | None = None,
        *,
        boom: bool = False,
    ) -> None:
        self.worker_script = list(worker_script)
        self.debate = debate or {}
        self.boom = boom
        self.dispatched: list[str] = []
        self.specs: dict[str, Any] = {}
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()

    def launch(self, spec: Any, run_id: str):
        from fleet_graph.executors.agent_run import RunTicket

        if run_id in self._launched:
            return RunTicket(run_id, f"/tmp/fake/{run_id}", None, adopted=True)
        self._launched.add(run_id)
        self._roles[run_id] = spec.role
        self.specs[run_id] = spec
        self.dispatched.append(run_id)
        return RunTicket(run_id, f"/tmp/fake/{run_id}", None)

    def wait(self, ticket: Any, **kwargs: Any):
        from fleet_graph.executors.agent_run import RunStatus, RunWaitTimeout

        if self.boom:
            raise Boom("killed during worker run")
        role = self._roles[ticket.run_id]
        if role in self.debate:
            return RunStatus("succeeded", self.debate[role])
        item = self.worker_script.pop(0)
        if item == "fail":
            return RunStatus("failed", {"state": "failed", "exit_code": 1})
        if item == "timeout":
            raise RunWaitTimeout(ticket, waited_seconds=999.0)
        return RunStatus("succeeded", item)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class FakeBusTransport:
    """模拟 agent-bus append-only 的 fake transport。

    - POST publish：按 (channel, idempotency_key) 去重（幂等），分配 message_id /
      channel_seq，记录 entity_id / supersedes / kind / payload；
    - GET /v1/protocols：返回可配置 registry（改写后校验行为随之改变）；
    - GET messages：按 channel 返回已发布消息，供回放 / 双源 diff。
    """

    def __init__(self, protocols: dict[str, Any] | None = None) -> None:
        self.protocols = protocols if protocols is not None else default_registry()
        self.messages: list[dict[str, Any]] = []
        self._seq = 0
        self._by_key: dict[tuple[str, str], str] = {}

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        if method == "POST" and "/publish" in url:
            channel = url.split("/v1/channels/")[1].split("/publish")[0]
            key = (channel, json_body["idempotency_key"])
            if key in self._by_key:
                mid = self._by_key[key]
                existing = next(m for m in self.messages if m["message_id"] == mid)
                return (
                    200,
                    {
                        "message_id": mid,
                        "entity_id": existing["entity_id"],
                        "channel_seq": existing["channel_seq"],
                        "deduplicated": True,
                    },
                )
            self._seq += 1
            mid = f"m{self._seq}"
            message = {
                "message_id": mid,
                "entity_id": json_body.get("entity_id", mid),
                "channel_seq": self._seq,
                "kind": json_body["kind"],
                "payload": json_body["payload"],
                "supersedes": json_body.get("supersedes"),
                "channel": channel,
            }
            self.messages.append(message)
            self._by_key[key] = mid
            return (
                200,
                {
                    "message_id": mid,
                    "entity_id": message["entity_id"],
                    "channel_seq": self._seq,
                    "deduplicated": False,
                },
            )
        if method == "GET" and url.endswith("/v1/protocols"):
            return (200, self.protocols)
        if method == "GET" and "/messages" in url:
            channel = url.split("/v1/channels/")[1].split("/messages")[0]
            msgs = [m for m in self.messages if m["channel"] == channel]
            return (200, {"messages": msgs, "head_seq": len(msgs)})
        return (200, {})

    def published(self, channel: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["channel"] == channel]


def default_registry() -> dict[str, Any]:
    """fake 的 registry（测试侧），供 consumer schema 派生。"""
    return {
        RESEARCH_CLUE_KIND: {
            "payload_schema": {
                "type": "object",
                "required": ["text", "status", "depth"],
                "properties": {"text": {"type": "string"}, "status": {"type": "string"}},
            },
            "schema_digest": "sha256:clue-test",
            "entity_role": "root",
        },
        RESEARCH_EVIDENCE_KIND: {
            "payload_schema": {
                "type": "object",
                "required": ["clue_id", "anchor", "quote", "claim"],
                "properties": {"clue_id": {"type": "string"}},
            },
            "schema_digest": "sha256:evidence-test",
            "entity_role": "leaf",
        },
        RESEARCH_DOC_KIND: {
            "payload_schema": {
                "type": "object",
                "required": ["doc_kind", "digest", "body", "origin"],
                "properties": {"doc_kind": {"type": "string"}},
            },
            "schema_digest": "sha256:doc-test",
            "entity_role": "leaf",
        },
    }


class TestPublishing:
    """acceptance 1：三类中间态确实发布，kind / 字段 / channel / 版本链正确。"""

    def test_three_kinds_published_with_channels_and_version_chain(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        fake = FakeBusTransport()
        client = BusClient(token="tok", transport=fake)
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        launcher = FakeLauncher(
            [
                worker_result(["每轮 tick 检查所有 line"], ["tick 的唤醒源"]),
                worker_result(["systemd timer 唤醒"], []),
            ],
            default_debate(),
        )
        config = ResearchConfig(question=question, run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher, publisher=client)
        assert result["terminal"] == TERMINAL_CONVERGED
        rid = config.research_id

        # 三个 kind 都出现。
        kinds = {m["kind"] for m in fake.messages}
        assert {RESEARCH_CLUE_KIND, RESEARCH_EVIDENCE_KIND, RESEARCH_DOC_KIND} <= kinds

        # channel 命名。
        clue_msgs = fake.published(clue_index_channel(rid))
        evidence_msgs = fake.published(evidence_channel(rid))
        docs_msgs = fake.published(docs_channel(rid))
        assert clue_msgs and evidence_msgs and docs_msgs

        # clue payload 字段与状态机词汇。
        first_clue = clue_msgs[0]["payload"]
        assert set(first_clue) >= {"text", "status", "depth", "sources"}
        assert first_clue["status"] in {"open", "in_flight", "explored", "blocked"}

        # evidence payload 字段（leaf）：clue_id / anchor / quote / claim。
        ev = evidence_msgs[0]["payload"]
        assert set(ev) == {"clue_id", "anchor", "quote", "claim"}
        assert ev["clue_id"] == derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)

        # doc payload（leaf）：doc_kind=report / origin / digest（R4 报告由脚本节点组装）。
        doc = docs_msgs[0]["payload"]
        assert doc["doc_kind"] == "report"
        assert doc["origin"] == rid
        local_report = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
        assert doc["body"] == local_report
        assert "## 分歧裁定" in doc["body"]
        from fleet_graph.research_bus import body_digest

        assert doc["digest"] == body_digest(doc["body"])

        # clue 版本链（supersedes）正确：open -> dispatched -> done。
        # R1-返工：root 实体版本链是 bus 原生 的——首条发布不传 entity_id（bus 分配
        # entity_id = message_id 作锚），本地线索身份走 payload.clue_id 归组。
        clue_id = derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)
        chain = [m for m in clue_msgs if (m.get("payload") or {}).get("clue_id") == clue_id]
        chain.sort(key=lambda m: m["channel_seq"])
        assert [m["payload"]["status"] for m in chain] == ["open", "in_flight", "explored"]
        assert chain[0]["supersedes"] is None
        assert chain[1]["supersedes"] == chain[0]["message_id"]
        assert chain[2]["supersedes"] == chain[1]["message_id"]


class TestDualSourceDiff:
    """acceptance 2：双源 diff 可跑且绿；人为制造不一致 exit 非零。"""

    def _run_and_diff(self, tmp_path: Path) -> tuple[Path, Any, str]:
        question = "fleet-graph 的调度器如何工作?"
        fake = FakeBusTransport()
        client = BusClient(token="tok", transport=fake)
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        launcher = FakeLauncher(
            [worker_result(["每轮 tick 检查所有 line"], [])],
            default_debate(),
        )
        config = ResearchConfig(question=question, run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher, publisher=client)
        assert result["terminal"] == TERMINAL_CONVERGED
        return tmp_path / "run", client, config.research_id

    def test_diff_green_then_breaks_on_deleted_evidence(self, tmp_path: Path) -> None:
        run_root, client, rid = self._run_and_diff(tmp_path)

        assert check_dual_source(run_root, client, rid) == 0
        assert dual_source_diff(run_root, client, rid) == []

        # 人为制造不一致：删一条本地 evidence。
        ev_path = run_root / "evidence.jsonl"
        lines = ev_path.read_text(encoding="utf-8").splitlines()
        assert lines
        ev_path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")

        assert check_dual_source(run_root, client, rid) != 0
        assert dual_source_diff(run_root, client, rid) != []


class TestConsumerSchemaFromRegistry:
    """acceptance 3：consumer schema 从 registry 派生，未手抄。"""

    def test_validation_uses_registry_response_and_rewrites_with_it(self, tmp_path: Path) -> None:
        fake = FakeBusTransport()
        client = BusClient(token="tok", transport=fake)
        payload = clue_payload(text="x", status="open", depth=0)

        # registry 的 schema 允许 -> 无错误。
        assert payload_errors(client, RESEARCH_CLUE_KIND, payload) == []

        # 改写 registry：要求额外必填字段，payload 缺失 -> 校验行为随之改变。
        fake.protocols[RESEARCH_CLUE_KIND]["payload_schema"] = {
            "type": "object",
            "required": ["text", "status", "depth", "mandatory"],
        }
        assert payload_errors(client, RESEARCH_CLUE_KIND, payload) != []

    def test_registry_derivation_not_a_hardcoded_schema(self) -> None:
        # 校验函数必须在运行时读 registry；仓库里不应有手抄 schema 字面量。
        src = (
            Path(__file__).resolve().parents[1] / "src" / "fleet_graph" / "research_bus.py"
        ).read_text(encoding="utf-8")
        # payload_schema 只能作为 registry 读取的键出现，不能是手写 schema 字面量。
        assert "payload_schema" in src
        assert "additionalProperties" not in src
        assert '"required":' not in src
        assert '"$schema"' not in src


class TestKillRestartReplay:
    """acceptance 4：kill-restart 从 bus 完整回放一次 run，无重复实体（幂等）。"""

    def test_interrupted_run_resumes_and_replays_full_trace(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        fake = FakeBusTransport()
        client = BusClient(token="tok", transport=fake)
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        config = ResearchConfig(question=question, run_root=tmp_path / "run")
        (tmp_path / "run").mkdir(parents=True)
        cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 100}
        db = str(tmp_path / "run" / "checkpoint.sqlite3")

        # 第一次：collect 的 wait 中炸掉，留下指向 collect 的 checkpoint。
        boom_launcher = FakeLauncher([], default_debate(), boom=True)
        graph, _deps = build_research(
            config, text_node=seed, launcher=boom_launcher, publisher=client
        )
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            with pytest.raises(Boom):
                compiled.invoke(resume_start(compiled, cfg, config), config=cfg)

        clue_one = derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)
        from fleet_graph.executors.agent_run import derive_run_id

        assert boom_launcher.dispatched == [
            derive_run_id(config.thread_id, f"worker/{clue_one}", 1)
        ]

        # 第二次：同 identity 精确续跑（run_research 经 resume_start 续跑并写 result.json）。
        good = FakeLauncher(
            [worker_result(["每轮 tick 检查所有 line"], [])],
            default_debate(),
        )
        result = run_research(config, text_node=seed, launcher=good, publisher=client)
        assert result["terminal"] == TERMINAL_CONVERGED
        assert seed.calls == 1, "resume 不得重放 seed"

        rid = config.research_id

        # 从 bus 回放重建完整轨迹。
        replay = replay_research(client, rid)
        assert set(replay["clues"]) == {clue_one}

        # 回放轨迹与本地镜像逐条对账一致（双源 diff 绿）。
        assert dual_source_diff(tmp_path / "run", client, rid) == []

        # 与最终 result.json 一致。
        persisted = json.loads((tmp_path / "run" / RESULT).read_text(encoding="utf-8"))
        assert persisted["terminal"] == TERMINAL_CONVERGED
        assert persisted["research_id"] == rid

        # 无重复实体（幂等）：clue 版本链 open -> dispatched -> done 恰好各一条。
        clue_chain = replay["clues"][clue_one]
        assert [r["payload"]["status"] for r in clue_chain] == [
            "open",
            "in_flight",
            "explored",
        ]
        assert len(clue_chain) == 3
        # evidence 条数与本地 evidence.jsonl 一致。
        local_evidence = read_jsonl(tmp_path / "run" / "evidence.jsonl")
        assert len(replay["evidence"]) == len(local_evidence)
        # doc 恰一条。
        assert len(replay["docs"]) == 1

    def test_idempotent_republish_deduplicates(self) -> None:
        fake = FakeBusTransport()
        client = BusClient(token="tok", transport=fake)
        rid = derive_research_id("幂等测试")
        clue_id = derive_clue_id("同一条 clue")
        key = clue_idempotency_key(rid, clue_id, "open", 0)
        payload = clue_payload(text="同一条 clue", status="open", depth=0)

        first = publish_best_effort(
            client,
            channel_id=clue_index_channel(rid),
            kind=RESEARCH_CLUE_KIND,
            payload=payload,
            idempotency_key=key,
            entity_id=clue_id,
        )
        second = publish_best_effort(
            client,
            channel_id=clue_index_channel(rid),
            kind=RESEARCH_CLUE_KIND,
            payload=payload,
            idempotency_key=key,
            entity_id=clue_id,
        )
        assert first == second  # 同 key 重派拿到同一条消息
        assert len(fake.published(clue_index_channel(rid))) == 1  # 无重复实体

    def test_evidence_anchor_is_content_addressed(self) -> None:
        finding = {"claim": "c", "source": "wiki", "quote": "c", "locator": "fake.md:1"}
        assert finding_anchor(finding) == "wiki@fake.md:1"


class TestPublishDegradation:
    """R1-返工：best-effort 发布降级必须响亮可观测（publish_degraded），不许静默。"""

    def test_publish_best_effort_records_failures(self) -> None:
        from fleet_graph.bus.client import BusError
        from fleet_graph.research_bus import PublishDegradation

        degradation = PublishDegradation()
        client = BusClient(token="tok", transport=FakeBusTransport())

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise BusError(403, '{"code": "DELEGATION_NOT_PERMITTED"}')

        class Forbidden:
            publish = boom

        mid = publish_best_effort(
            Forbidden(),
            channel_id=clue_index_channel("r-abc"),
            kind=RESEARCH_CLUE_KIND,
            payload={},
            idempotency_key="k",
            degraded=degradation,
        )
        assert mid is None
        assert degradation.count == 1
        assert "DELEGATION_NOT_PERMITTED" in (degradation.first_error or "")
        assert degradation.as_dict() == {"count": 1, "first_error": degradation.first_error}

        # 成功发布不再计降级。
        assert publish_best_effort(
            client,
            channel_id=clue_index_channel("r-abc"),
            kind=RESEARCH_CLUE_KIND,
            payload={"text": "x", "status": "open", "depth": 0},
            idempotency_key="k2",
            degraded=degradation,
        )
        assert degradation.count == 1

    def test_run_with_forbidden_publisher_ends_degraded_not_green(self, tmp_path: Path) -> None:
        from fleet_graph.bus.client import BusError

        class Forbidden:
            def publish(self, *args: Any, **kwargs: Any) -> Any:
                raise BusError(403, '{"code": "DELEGATION_NOT_PERMITTED"}')

        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        launcher = FakeLauncher(
            [worker_result(["每轮 tick 检查所有 line"], [])],
            default_debate(),
        )
        config = ResearchConfig(question="降级判据问题", run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher, publisher=Forbidden())

        persisted = json.loads((tmp_path / "run" / RESULT).read_text(encoding="utf-8"))
        for record in (result, persisted):
            degraded = record["publish_degraded"]
            assert degraded["count"] > 0, "403 全程 publish 必须可观测降级，不得静默"
            assert degraded["first_error"], "first_error 必须非空"

    def test_run_without_publisher_records_zero_degradation(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], [])],
            default_debate(),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["publish_degraded"] == {"count": 0, "first_error": None}


class TestCheckScriptJudge:
    """check_research_publish.py 的机器判据：importlib 载入脚本，纯函数可测。"""

    def _load(self):
        import importlib.util

        repo = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "check_research_publish", repo / "scripts" / "check_research_publish.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_green_when_all_channels_land_and_no_degradation(self) -> None:
        module = self._load()
        ok, verdict = module.judge_publish(
            {"index": 3, "evidence": 1, "docs": 1},
            {"count": 0, "first_error": None},
        )
        assert ok is True
        assert verdict["pass"] is True
        assert verdict["missing_channels"] == []

    def test_red_when_channels_missing(self) -> None:
        module = self._load()
        ok, verdict = module.judge_publish(
            {"index": 3, "evidence": 0, "docs": 1},
            {"count": 0, "first_error": None},
        )
        assert ok is False
        assert verdict["missing_channels"] == ["evidence"]

    def test_red_when_publish_degraded_non_empty(self) -> None:
        module = self._load()
        ok, verdict = module.judge_publish(
            {"index": 3, "evidence": 1, "docs": 1},
            {"count": 5, "first_error": "BusError: 403"},
        )
        assert ok is False
        assert verdict["degraded"] is True

    def test_negative_fixture_research_id_must_be_red(self) -> None:
        module = self._load()
        assert module.NEGATIVE_FIXTURE_RESEARCH_ID == "r-2193db185d0f"
        ok, verdict = module.judge_publish(
            {"index": 0, "evidence": 0, "docs": 0},
            {"count": 0, "first_error": None},
        )
        assert ok is False
        assert verdict["missing_channels"] == ["docs", "evidence", "index"]

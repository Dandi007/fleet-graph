"""全仓唯一的 decision 发布点（发 `work.decision.v2`）。独立主体，独立凭证。

发布走 v2：bus 端注册的 v1 payload_schema 是 additionalProperties:false 的
5 字段旧形状，gate_release 载荷发不出去（生产实测 VALIDATION_ERROR），而
协议注册后不可变；v2 的 gate_release 变体与本模块构造的 payload 逐字段一致。
v1 继续留给人工问答裁决，读径（bus/board.py `DECISION_KINDS`）两版兼收。

bus/board.py 的标准规则（Board 没有发布 decision 的方法）原样成立；R4-3 在
它旁边开出一条被三重结构约束的窄门，而不是给 Board 加方法：

- **唯一发布点**：构造/发布 decision 的调用只允许出现在本模块
  （scripts/check_supervisor_conformance.py Guard B 的唯一豁免），且只有
  graphs/supervisor.py 的 act script 节点可以 import 本模块（Guard C）。
  llm 执行路径连发布入口都拿不到。
- **凭证分离**：发布用的 token 经独立 env `FLEET_GRAPH_DECISION_TOKEN_FILE`
  （只收文件路径，不收裸 token——argv/env 里的裸 token 就是 /proc 里的
  token），只在 act script 节点进程内读取；executors/agent_run.py 把
  `FLEET_GRAPH_DECISION_` 前缀整个从 agent 子进程 env 里剥掉，
  dd/control_plane.py 的 env 白名单从不转发它。板凭证
  （FLEET_GRAPH_BUS_TOKEN*）与决策凭证互不相认。
- **只会放行，发不出别的**：payload 由本模块固定构造——decision 恒为
  APPROVE、`scope: "merge_only"`（合入≠部署）、payload.kind 恒为
  gate_release（构造性发不出一条 preauth，自签发即自批）、`decided_by`
  固定为代行署名并点名 preauth 消息 id、refs 同时指向 question note 与
  preauth 消息两者。REJECT 走不了这里：驳回只出建议，人拍。

入参要求一份 `granted=True` 的 ReleaseEvaluation：三要素判定
（supervise/preauth.py）不通过的调用在这里再被拒一次——belt and braces，
守卫防的是旁路，这里防的是把未通过的判定递进来。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleet_graph.bus.board import DECISION_KIND_V2, WORK_NOTES
from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient, PublishResult
from fleet_graph.supervise.preauth import ReleaseEvaluation

#: 决策凭证的唯一入口。与 FLEET_GRAPH_BUS_TOKEN(_FILE) 刻意不同名不同前缀：
#: 拿到板凭证的进程不因此拿到决策凭证。
DECISION_TOKEN_ENV = "FLEET_GRAPH_DECISION_TOKEN_FILE"

#: 放行 decision 的 payload.kind。固定值——本模块没有任何参数能把它改成
#: "preauth"（preauth 的签发与展期停在人闸，HUMAN_ONLY_CATEGORIES）。
RELEASE_PAYLOAD_KIND = "gate_release"

#: 合入≠部署。带着这个 scope 的 APPROVE 只解锁 merge，部署授权仍停在人闸。
SCOPE_MERGE_ONLY = "merge_only"

#: 与 graphs/dd_actors.py 的 GATE_APPROVE 同值（测试钉死相等）；不 import
#: 是因为 supervise 层不依赖图层，且那个模块拖着 langgraph。
APPROVE_DECISION = "APPROVE"


class DecisionPublishRefused(RuntimeError):
    """这次发布请求不满足窄门条件。拒绝，不降级。"""


def load_decision_token(env: dict[str, str] | None = None) -> str:
    """从独立凭证文件读决策 token。没有文件路径 env 就是没有凭证，不回退到
    板凭证——凭证分离的全部意义就在这条不回退上。"""
    import os

    env = dict(os.environ) if env is None else env
    token_file = env.get(DECISION_TOKEN_ENV)
    if not token_file:
        raise DecisionPublishRefused(f"无决策凭证：{DECISION_TOKEN_ENV} 未设置（板凭证不可代用）")
    return Path(token_file).read_text().strip()


def decided_by_for(preauth_message_id: str) -> str:
    """代行署名。诚实到能被 grep：谁看到这条 decision 都知道它不是人逐条拍的，
    以及它依据的是哪条 preauth。"""
    return f"supervisor-graph (依预授权 {preauth_message_id} 代行；非人逐条拍板)"


def publish_release_decision(
    *,
    evaluation: ReleaseEvaluation,
    card_entity_id: str,
    question_note_id: str,
    rationale: str,
    idempotency_key: str,
    bus_url: str = DEFAULT_BUS_URL,
    client: BusClient | None = None,
) -> PublishResult:
    """依一份 granted 的三要素判定，发布一条 merge_only 的 APPROVE。

    `client` 是测试缝；生产路径留 None，此刻才读决策凭证并新建 client——
    凭证的生命周期被压缩到这一个调用内。
    """
    if not evaluation.granted:
        raise DecisionPublishRefused(f"三要素判定未通过，拒绝发布：{list(evaluation.reasons)}")
    if not evaluation.preauth_message_id or not question_note_id or not card_entity_id:
        raise DecisionPublishRefused(
            "署名锚点不齐（preauth 消息 id / question note id / 卡 entity 缺一）——"
            "refs 必须同时指向问题与预授权两者"
        )

    if client is None:
        client = BusClient(base_url=bus_url, token=load_decision_token())

    payload: dict[str, Any] = {
        "kind": RELEASE_PAYLOAD_KIND,
        "decision": APPROVE_DECISION,
        "decided_by": decided_by_for(evaluation.preauth_message_id),
        "rationale": rationale,
        "card_entity_id": card_entity_id,
        "question_note_id": question_note_id,
        "preauth_message_id": evaluation.preauth_message_id,
        "target_ref": evaluation.target_ref,
        "scope": SCOPE_MERGE_ONLY,
    }
    refs = [
        {"target_entity": question_note_id},
        {"target_entity": evaluation.preauth_message_id},
    ]
    return client.publish(WORK_NOTES, DECISION_KIND_V2, payload, idempotency_key, refs=refs)


__all__ = [
    "APPROVE_DECISION",
    "DECISION_TOKEN_ENV",
    "RELEASE_PAYLOAD_KIND",
    "SCOPE_MERGE_ONLY",
    "DecisionPublishRefused",
    "decided_by_for",
    "load_decision_token",
    "publish_release_decision",
]

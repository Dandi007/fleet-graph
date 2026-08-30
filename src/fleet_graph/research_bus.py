"""R1：deep-research 中间态落 agent-bus append-only。

本模块把 research 图的 clue / evidence / doc 三类中间态发布到 bus 的三个
research 专用 channel，并在此基础上提供双源对账与从 bus 回放：

- channel：``research:{research_id}.index``（clue，root 版本链）、
  ``research:{research_id}.evidence``（evidence，leaf）、
  ``research:{research_id}.docs``（doc，leaf）。
- 协议 kind：``research.clue.v2`` / ``research.evidence.v2`` / ``research.doc.v2``
  （沿用已注册协议，本开发不重注册新 kind）。
- consumer 侧 payload 校验：schema **运行时从 registry 派生**
  （``GET /v1/protocols`` 的 ``payload_schema`` 用 jsonschema 校验），
  严禁在仓库里手抄 schema / allowlist。

发布一律 best-effort：失败只降级记录（与 observe 同义），绝不 fault 整图。
幂等：同一 run 同一中间态用确定性 idempotency_key（run/clue/finding 内容寻址
派生），kill-restart 重派同 key 不产生重复实体。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RESEARCH_CLUE_KIND = "research.clue.v2"
RESEARCH_EVIDENCE_KIND = "research.evidence.v2"
RESEARCH_DOC_KIND = "research.doc.v2"

DOC_KIND_REPORT = "report"

# research.clue.v2 的 status 状态机（registered）：proposed|open|in_flight|explored|dropped|blocked
# 图内 pipeline status -> 协议 status 词汇。
PIPELINE_STATUS_TO_PROTOCOL = {
    "open": "open",
    "dispatched": "in_flight",
    "done": "explored",
    "blocked": "blocked",
}

#: 本地镜像的产物文件名（与 research_pipeline.py 保持一致）。
EVIDENCE_FILE = "evidence.jsonl"
REPORT_FILE = "report.md"


def clue_index_channel(research_id: str) -> str:
    return f"research:{research_id}.index"


def evidence_channel(research_id: str) -> str:
    return f"research:{research_id}.evidence"


def docs_channel(research_id: str) -> str:
    return f"research:{research_id}.docs"


# --- payload 构造 -----------------------------------------------------------


def clue_payload(
    *,
    text: str,
    status: str,
    depth: int,
    sources: list[str] | None = None,
    parent: str | None = None,
    assignee: str | None = None,
    run_id: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    """research.clue.v2 的 payload（root 实体，字段与注册 schema 一致）。"""
    payload: dict[str, Any] = {
        "text": text,
        "status": status,
        "depth": depth,
        "sources": list(sources or []),
    }
    if parent is not None:
        payload["parent"] = parent
    if assignee is not None:
        payload["assignee"] = assignee
    if run_id is not None:
        payload["run_id"] = run_id
    if rationale is not None:
        payload["rationale"] = rationale
    return payload


def finding_anchor(finding: dict[str, Any]) -> str:
    """evidence 的 ``anchor``：带版本 URI，由 finding 的 source + locator 派生。

    同一条 finding 恒得同一条 anchor，双源对账据此逐条匹配。
    """
    source = str(finding.get("source") or "")
    locator = str(finding.get("locator") or "")
    return f"{source}@{locator}"


def evidence_payload(*, clue_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    """research.evidence.v2 的 payload（leaf 实体）。"""
    return {
        "clue_id": clue_id,
        "anchor": finding_anchor(finding),
        "quote": finding.get("quote", ""),
        "claim": finding.get("claim", ""),
    }


def body_digest(body: str) -> str:
    """正文内容寻址（全局去重键）。"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def doc_payload(*, doc_kind: str, digest: str, body: str, origin: str) -> dict[str, Any]:
    """research.doc.v2 的 payload（leaf 实体）。"""
    return {"doc_kind": doc_kind, "digest": digest, "body": body, "origin": origin}


# --- 幂等 key（内容寻址派生） ------------------------------------------------


def clue_idempotency_key(research_id: str, clue_id: str, status: str, retry: int = 0) -> str:
    """clue 中间态的幂等 key：run/clue/status/retry 内容寻址。"""
    return f"{research_id}:clue:{clue_id}:{status}:{retry}"


def evidence_idempotency_key(research_id: str, clue_id: str, finding: dict[str, Any]) -> str:
    """evidence 的幂等 key：finding 内容寻址。"""
    canonical = json.dumps(finding, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{research_id}:evidence:{clue_id}:{digest}"


def doc_idempotency_key(research_id: str, digest: str) -> str:
    """doc 的幂等 key：正文 digest 寻址。"""
    return f"{research_id}:doc:{digest}"


# --- best-effort 发布 -------------------------------------------------------


def publish_best_effort(
    publisher: Any,
    *,
    channel_id: str,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    entity_id: str | None = None,
    supersedes: str | None = None,
) -> str | None:
    """发布一个实体，best-effort：失败只降级记录，绝不抛给图（与 observe 同义）。

    返回 bus 分配的 message_id；publisher 缺失 / 失败时返回 None。
    """
    if publisher is None:
        return None
    try:
        result = publisher.publish(
            channel_id,
            kind,
            payload,
            idempotency_key,
            entity_id=entity_id,
            supersedes=supersedes,
        )
        return getattr(result, "message_id", None)
    except Exception as exc:
        log.warning("research publish degraded (kind=%s channel=%s): %s", kind, channel_id, exc)
        return None


# --- consumer 侧 schema 校验（从 registry 派生） -----------------------------


def payload_errors(client: Any, kind: str, payload: dict[str, Any]) -> list[str]:
    """用 registry 返回的 ``payload_schema`` 校验 payload，返回错误消息列表。

    schema **只来自 registry 读取结果**（``client.get_protocol(kind)``），仓库里
    没有手抄的 schema / allowlist——改写 registry 响应后校验行为随之改变。
    """
    protocol = client.get_protocol(kind)
    if not protocol:
        return ["protocol not registered"]
    schema = protocol.get("payload_schema")
    if not schema:
        return ["registry entry has no payload_schema"]
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    return [f"{list(err.path)}: {err.message}" for err in validator.iter_errors(payload)]


# --- 读 bus 实体 ------------------------------------------------------------


def read_channel(client: Any, channel: str, *, limit: int = 1000) -> list[dict[str, Any]]:
    messages, _ = client.messages(channel, limit=limit)
    return list(messages)


def replay_research(client: Any, research_id: str) -> dict[str, Any]:
    """从 bus 回放一个 research 的完整过程轨迹。

    返回：
    - ``clues``：entity_id -> 按 channel_seq 升序的 revision 列表
      （每项含 message_id / supersedes / channel_seq / payload）；
    - ``evidence``：payload 列表；
    - ``docs``：payload 列表。
    供 kill-restart 后与本地镜像 / result.json 核对。
    """
    index = read_channel(client, clue_index_channel(research_id))
    evidence = read_channel(client, evidence_channel(research_id))
    docs = read_channel(client, docs_channel(research_id))

    chains: dict[str, list[dict[str, Any]]] = {}
    for msg in index:
        if msg.get("kind") != RESEARCH_CLUE_KIND:
            continue
        eid = msg.get("entity_id")
        if not eid:
            continue
        chains.setdefault(eid, []).append(msg)

    clues = {eid: sorted(msgs, key=lambda m: m["channel_seq"]) for eid, msgs in chains.items()}
    return {
        "clues": clues,
        "evidence": [
            m.get("payload", {}) for m in evidence if m.get("kind") == RESEARCH_EVIDENCE_KIND
        ],
        "docs": [m.get("payload", {}) for m in docs if m.get("kind") == RESEARCH_DOC_KIND],
    }


# --- 双源 diff：本地镜像 vs bus 实体 -----------------------------------------

_CLUE_FILE_RE = re.compile(r"^([0-9a-fc-]+)\.json$")


def read_local_clues(run_root: Path) -> dict[str, dict[str, Any]]:
    """读本地 ``clues/*.json`` 镜像：``{id, query, depth}``。

    只认 ``{clue_id}.json``（排除 ``*-result.json`` / ``*-prompt.md``）。
    """
    out: dict[str, dict[str, Any]] = {}
    clues_dir = Path(run_root) / "clues"
    if not clues_dir.is_dir():
        return out
    for path in sorted(clues_dir.glob("*.json")):
        if not _CLUE_FILE_RE.match(path.name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            out[str(data["id"])] = data
    return out


def read_local_evidence(run_root: Path) -> list[dict[str, Any]]:
    path = Path(run_root) / EVIDENCE_FILE
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def read_local_report(run_root: Path) -> str | None:
    path = Path(run_root) / REPORT_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else None


def dual_source_diff(run_root: Path, client: Any, research_id: str) -> list[str]:
    """逐条对账本地镜像与 bus 实体（clue / evidence / doc 三类），返回不一致列表。

    全绿 = 两边一致（空列表）。机器可跑（库函数 + 测试）。
    """
    issues: list[str] = []
    replay = replay_research(client, research_id)

    # --- clue：本地 clues/*.json 与 bus 版本链逐条对账 ---
    local_clues = read_local_clues(run_root)
    for cid, data in sorted(local_clues.items()):
        chain = replay["clues"].get(cid)
        if not chain:
            issues.append(f"clue {cid} 在本地镜像但 bus 缺失")
            continue
        head = chain[-1]["payload"]
        if head.get("text") != data.get("query"):
            issues.append(
                f"clue {cid} text 不一致: local={data.get('query')!r} bus={head.get('text')!r}"
            )
        if head.get("depth") != data.get("depth"):
            issues.append(
                f"clue {cid} depth 不一致: local={data.get('depth')} bus={head.get('depth')}"
            )
    for cid in sorted(set(replay["clues"]) - set(local_clues)):
        issues.append(f"clue {cid} 在 bus 但本地镜像缺失")

    # --- evidence：本地 evidence.jsonl 与 bus evidence 逐条对账 ---
    local_ev = {
        (
            e.get("clue_id"),
            finding_anchor(e.get("finding", {})),
            (e.get("finding", {}) or {}).get("quote"),
            (e.get("finding", {}) or {}).get("claim"),
        )
        for e in read_local_evidence(run_root)
    }
    bus_ev = {
        (p.get("clue_id"), p.get("anchor"), p.get("quote"), p.get("claim"))
        for p in replay["evidence"]
    }
    if local_ev != bus_ev:
        issues.append("evidence 双源不一致")

    # --- doc：本地 report.md 与 bus docs 逐条对账 ---
    local_report = read_local_report(run_root)
    bus_docs = [p for p in replay["docs"] if p.get("doc_kind") == DOC_KIND_REPORT]
    if local_report is None:
        if bus_docs:
            issues.append("bus 有 report doc 但本地无 report.md")
    else:
        if not bus_docs:
            issues.append("本地有 report.md 但 bus 无 report doc")
        else:
            head = bus_docs[-1]
            if head.get("body") != local_report:
                issues.append("report doc body 与本地 report.md 不一致")
            if head.get("digest") != body_digest(local_report):
                issues.append("report doc digest 与本地正文寻址不一致")
            if head.get("origin") != research_id:
                issues.append(
                    f"report doc origin 不一致: {head.get('origin')!r} != {research_id!r}"
                )

    return issues


def check_dual_source(run_root: Path, client: Any, research_id: str) -> int:
    """双源 diff 的 exit code 形态：全绿 exit 0，任一不一致 exit 非零。"""
    issues = dual_source_diff(run_root, client, research_id)
    for issue in issues:
        print(f"dual-source mismatch: {issue}")
    return 0 if not issues else 1


__all__ = [
    "DOC_KIND_REPORT",
    "EVIDENCE_FILE",
    "PIPELINE_STATUS_TO_PROTOCOL",
    "REPORT_FILE",
    "RESEARCH_CLUE_KIND",
    "RESEARCH_DOC_KIND",
    "RESEARCH_EVIDENCE_KIND",
    "body_digest",
    "check_dual_source",
    "clue_idempotency_key",
    "clue_index_channel",
    "clue_payload",
    "doc_idempotency_key",
    "doc_payload",
    "docs_channel",
    "dual_source_diff",
    "evidence_channel",
    "evidence_idempotency_key",
    "evidence_payload",
    "finding_anchor",
    "payload_errors",
    "publish_best_effort",
    "read_channel",
    "read_local_clues",
    "read_local_evidence",
    "read_local_report",
    "replay_research",
]

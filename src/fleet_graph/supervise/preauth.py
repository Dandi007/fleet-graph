"""机械预授权（preauth）：第四道防代拍闸的协议对象与三要素判定。

前三道闸原样不动：Board 没有发布 decision 的方法（bus/board.py 的
标准规则）、gate resume 值被丢弃每次重读板、decision 只经 ref 图解析。这里
加的是第四道：**独立主体 + 机械预授权 + 凭证分离**——人（或依全权委托代行、
诚实署名）在板上发一条 payload `kind: "preauth"` 的 `work.decision.v2`
（v1 的注册 schema 装不下 preauth 载荷，preauth 只存在于 v2 上），
supervisor 图据此机械放行集成分支 gate；production promotion 在结构上不可能
被这条路放行，因为校验层拒绝任何能覆盖 main/master/production/release 的
allowlist 前缀（负例测试钉死）。

本模块只做协议校验与判定，是纯函数：不发布任何东西。发布唯一走
`supervise/decision_publisher.py`（AST 守卫钉死）。

三要素（缺一 → needs_human，不报错、不猜）：

- ① 原文覆盖：待批动作 ∈ preauth.allowed_actions，preauth.card_entity_id 与
  gate 的卡 entity 精确匹配，且 now < expires_at。`expires_at` 必填——没有
  期限的 preauth 是常开自批按钮，校验层直接拒收。
- ② target base：从 git 现算锚定的合入目标 ref（不是任何 agent 自述）∈
  target_ref_allowlist。scope 用前缀白名单表达，不用正则——正则的覆盖范围
  人读不出来，前缀读得出来。
- ③ 署名诚实：发布的 decision `decided_by` 固定为代行署名，refs 同时指向
  question note 与 preauth 消息两者（由 decision_publisher 构造保证；这里
  判定的是两个 id 是否都在手上）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: work.decision.v2 preauth 变体的 payload.kind 值。preauth 与 gate_release
#: 共用消息 kind，靠 payload.kind 区分——机器可读字段，不是散文。
PREAUTH_PAYLOAD_KIND = "preauth"

#: v1 preauth 只能预授权 approve。REJECT 不纳入：驳回只出建议（人看着
#: recommend_reject 的机械复现依据自己拍），自动驳回和自动放行一样是代拍。
PREAUTH_ALLOWED_ACTIONS = frozenset({"approve"})

#: 构造性不可能出现在任何 allowlist 覆盖范围里的分支名。校验层在 preauth
#: 入口拒绝，而不是在放行时刻比对——一条能覆盖 production 的 preauth
#: 根本不该存在于板上。
PROTECTED_BRANCHES = ("main", "master", "production", "release")

#: 必须停在 Inbox（needs_human）的类别，封闭枚举——不写散文。preauth 只能
#: 表达 approve + 集成 ref 前缀，这些类别在协议上无法被一条合法 preauth
#: 覆盖（allowed_actions 校验 + allowlist 校验 + publisher 只会发 APPROVE），
#: 枚举存在的意义是把"为什么覆盖不到"钉进测试。
HUMAN_ONLY_CATEGORIES = frozenset(
    {
        # production main/release promotion：allowlist 前缀构造性排除。
        "production_promotion",
        # 部署授权：合入≠部署，放行 decision 的 scope 固定 merge_only。
        "deployment_authorization",
        # 判据/spec 改判：preauth 没有表达它的字段。
        "criteria_or_spec_change",
        # cancel 在跑的 development：同上，approve 之外无动作可授权。
        "cancel_running_development",
        # preauth 本身的签发与展期：自签发即自批，publisher 的 payload.kind
        # 固定为放行、永远发不出一条 preauth。
        "preauth_issuance_or_extension",
        # REJECT：v1 不纳入 preauth，驳回只出建议。
        "reject",
    }
)


class PreauthError(ValueError):
    """这条消息不是一条合法的 preauth。拒收，不猜。"""


@dataclass(frozen=True)
class Preauth:
    """一条通过协议校验的机械预授权。字段全部机器可读。"""

    message_id: str
    card_entity_id: str
    allowed_actions: tuple[str, ...]
    target_ref_allowlist: tuple[str, ...]
    expires_at: str
    decided_by: str
    raw: dict[str, Any]


def _refusal_for_prefix(prefix: str) -> str | None:
    """一个 allowlist 前缀被拒绝的理由，合法则 None。

    覆盖两种走私姿势：前缀本身探进受保护分支（"refs/heads/mai" 能覆盖
    refs/heads/main），以及前缀的某个路径段就是受保护名（"refs/heads/
    production/" 段里含 production）。
    """
    if not prefix or not isinstance(prefix, str):
        return "前缀必须是非空字符串"
    if any(ch.isspace() for ch in prefix):
        return f"前缀 {prefix!r} 含空白字符"
    if any(ch in prefix for ch in "*?["):
        return f"前缀 {prefix!r} 含通配字符——scope 只认前缀白名单，不认模式"
    for branch in PROTECTED_BRANCHES:
        for protected in (branch, f"refs/heads/{branch}"):
            if protected.startswith(prefix):
                return f"前缀 {prefix!r} 覆盖受保护 ref {protected!r}"
        if branch in prefix.split("/"):
            return f"前缀 {prefix!r} 的路径段命中受保护分支名 {branch!r}"
    return None


def _parse_expiry(value: Any) -> float:
    """expires_at 的 epoch 秒。必须是带时区的 RFC3339——naive 时间在跨主机
    的到期比较里就是歧义，拒收。"""
    if not isinstance(value, str) or not value.strip():
        raise PreauthError("expires_at 必填：无期限 preauth 是常开自批按钮，拒收")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreauthError(f"expires_at {value!r} 不是合法 RFC3339 时间: {exc}") from exc
    if parsed.tzinfo is None:
        raise PreauthError(f"expires_at {value!r} 缺时区——到期判定不接受歧义时间")
    return parsed.timestamp()


def parse_preauth(message: dict[str, Any]) -> Preauth:
    """把一条板上消息解析为 Preauth；任何一处不合协议都是 PreauthError。"""
    payload = message.get("payload") or {}
    if payload.get("kind") != PREAUTH_PAYLOAD_KIND:
        raise PreauthError(f"payload.kind={payload.get('kind')!r} 不是 {PREAUTH_PAYLOAD_KIND!r}")

    card_entity_id = str(payload.get("card_entity_id") or "")
    if not card_entity_id:
        raise PreauthError("card_entity_id 必填：preauth 必须精确绑定一张卡")

    actions_raw = payload.get("allowed_actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        raise PreauthError("allowed_actions 必须是非空列表")
    actions = tuple(str(a) for a in actions_raw)
    illegal = [a for a in actions if a not in PREAUTH_ALLOWED_ACTIONS]
    if illegal:
        raise PreauthError(
            f"allowed_actions 含 {illegal!r}；v1 preauth 只能预授权 "
            f"{sorted(PREAUTH_ALLOWED_ACTIONS)}（REJECT/部署/cancel 都停在人闸）"
        )

    allowlist_raw = payload.get("target_ref_allowlist")
    if not isinstance(allowlist_raw, list) or not allowlist_raw:
        raise PreauthError("target_ref_allowlist 必须是非空前缀列表")
    allowlist = tuple(str(p) for p in allowlist_raw)
    for prefix in allowlist:
        refusal = _refusal_for_prefix(prefix)
        if refusal is not None:
            raise PreauthError(refusal)

    expires_at = str(payload.get("expires_at") or "")
    _parse_expiry(expires_at)

    message_id = str(message.get("message_id") or "")
    if not message_id:
        raise PreauthError("消息缺 message_id——署名 refs 无从指向 preauth 本体")

    return Preauth(
        message_id=message_id,
        card_entity_id=card_entity_id,
        allowed_actions=actions,
        target_ref_allowlist=allowlist,
        expires_at=expires_at,
        decided_by=str(payload.get("decided_by") or ""),
        raw=dict(message),
    )


def latest_preauth_for(
    messages: list[dict[str, Any]], card_entity_id: str
) -> tuple[Preauth | None, list[str]]:
    """这张卡最新的一条合法 preauth，以及被拒收候选的理由清单。

    不合法的候选不报错、不采信——理由随 evaluation 落 receipt，人能看到
    自己发的 preauth 为什么没生效。
    """
    rejections: list[str] = []
    best: Preauth | None = None
    best_seq = -1
    for message in messages:
        payload = message.get("payload") or {}
        if payload.get("kind") != PREAUTH_PAYLOAD_KIND:
            continue
        try:
            candidate = parse_preauth(message)
        except PreauthError as exc:
            rejections.append(f"{message.get('message_id')}: {exc}")
            continue
        if card_entity_id and candidate.card_entity_id != card_entity_id:
            continue
        seq = int(message.get("channel_seq") or 0)
        if seq >= best_seq:
            best, best_seq = candidate, seq
    return best, rejections


def ref_in_allowlist(ref: str, allowlist: tuple[str, ...]) -> bool:
    return bool(ref) and any(ref.startswith(prefix) for prefix in allowlist)


@dataclass(frozen=True)
class ReleaseEvaluation:
    """一次三要素判定的完整结果。reasons 为空即 granted。"""

    granted: bool
    reasons: tuple[str, ...]
    preauth_message_id: str = ""
    target_ref: str = ""
    rejections: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "reasons": list(self.reasons),
            "preauth_message_id": self.preauth_message_id,
            "target_ref": self.target_ref,
            "rejections": list(self.rejections),
        }


def evaluate_release(
    *,
    preauth: Preauth | None,
    action: str,
    card_entity_id: str,
    question_note_id: str,
    target_ref: str,
    report_green: bool,
    already_decided: bool,
    now: float,
    rejections: tuple[str, ...] = (),
) -> ReleaseEvaluation:
    """三要素机械判定。script，不是 llm；缺一即 needs_human。

    `report_green`（机械审计全绿、无 gap）是三要素之外的前置：证据链上有任何
    红或缺口时，"依预授权放行"就失去了它唯一的正当性来源——完整且全绿的机械
    证据。
    """
    reasons: list[str] = []
    if already_decided:
        reasons.append("该 question 已有 decision 应答——不重复放行")
    if not report_green:
        reasons.append("机械审计非全绿或有 gap——preauth 只放行完整全绿的证据链")
    if preauth is None:
        reasons.append("卡上没有合法的 preauth")
        return ReleaseEvaluation(
            granted=False, reasons=tuple(reasons), rejections=tuple(rejections)
        )

    # 要素 ①：原文覆盖。
    if action not in preauth.allowed_actions:
        reasons.append(f"动作 {action!r} 不在 preauth.allowed_actions 内")
    if not card_entity_id or preauth.card_entity_id != card_entity_id:
        reasons.append(
            f"preauth 绑定卡 {preauth.card_entity_id!r} 与 gate 卡 {card_entity_id!r} 不精确匹配"
        )
    if now >= _parse_expiry(preauth.expires_at):
        reasons.append(f"preauth 已过期（expires_at={preauth.expires_at}）")

    # 要素 ②：git 现算的目标 ref 落在前缀白名单内。
    if not target_ref:
        reasons.append("合入目标 ref 无法从 git 锚定现算——不采信任何自述 ref")
    elif not ref_in_allowlist(target_ref, preauth.target_ref_allowlist):
        reasons.append(
            f"目标 ref {target_ref!r} 不在 allowlist {list(preauth.target_ref_allowlist)} 内"
        )

    # 要素 ③：署名诚实的两个锚点都在手上。
    if not question_note_id:
        reasons.append("无 question note id——decision refs 无从指向被应答的问题")

    return ReleaseEvaluation(
        granted=not reasons,
        reasons=tuple(reasons),
        preauth_message_id=preauth.message_id,
        target_ref=target_ref,
        rejections=tuple(rejections),
    )


__all__ = [
    "HUMAN_ONLY_CATEGORIES",
    "PREAUTH_ALLOWED_ACTIONS",
    "PREAUTH_PAYLOAD_KIND",
    "PROTECTED_BRANCHES",
    "Preauth",
    "PreauthError",
    "ReleaseEvaluation",
    "evaluate_release",
    "latest_preauth_for",
    "parse_preauth",
    "ref_in_allowlist",
]

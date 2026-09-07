"""minimal DD 循环状态机：阶段转移表 + approve 清零 + DD 结果对象。

design.md §3 把 DD 循环定死：Impl → 验收命令 → CR → FR → Goal 审单 → Merge Agent。
任何一步不过回 Impl，且 impl 每跑一次 approve 清零（GO-14）；Merge ``rebased`` 动了
代码要完整再走 CR → FR → Goal 审单（GO-15）。喂回 Goal Agent 的 DD 结果对象格式见
protocol.md §7。

本模块把这些规则做成纯函数：零 IO、不改事件、不调 agent、不做 git。引擎图届时直接
调用 :func:`next_stage` / :func:`advance` 推进状态、:func:`warnings_for` 产告警、
:func:`build_dd_result` 从 event 日志 fold 出喂给 Goal Agent 的结果对象（只读
:mod:`fleet_graph.minimal.events`）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fleet_graph.minimal.events import Event

STAGES: tuple[str, ...] = ("impl", "acceptance", "cr", "fr", "goal_review", "merge")

TERMINAL_OUTCOMES: tuple[str, ...] = ("merged", "failed")


@dataclass(frozen=True)
class Transition:
    """一次阶段转移的结果：要么进入下一个 stage（``next_stage``），要么终结（``outcome``）。

    ``feedback_from`` 标记本轮回 Impl 的源头（acceptance / cr / fr / goal / merge）；
    ``approve_reset`` 为 True 时，已有的 Goal 审单 approve 失效（GO-14 / GO-15）。
    """

    next_stage: str | None
    outcome: str | None
    feedback_from: str | None = None
    approve_reset: bool = False


# 完整转移表（design.md §3）。回到 impl 的转移一律 approve_reset=True；merge rebased
# 也置 approve_reset=True（GO-15：rebase 动了代码，之前的 approve 失效，回 CR 完整再审）。
_TRANSITIONS: dict[str, dict[str, dict[str, Any]]] = {
    "impl": {
        "committed": {"next_stage": "acceptance"},
        "failed": {"outcome": "failed"},
    },
    "acceptance": {
        "pass": {"next_stage": "cr"},
        "fail": {"next_stage": "impl", "feedback_from": "acceptance", "approve_reset": True},
    },
    "cr": {
        "pass": {"next_stage": "fr"},
        "fail": {"next_stage": "impl", "feedback_from": "cr", "approve_reset": True},
    },
    "fr": {
        "pass": {"next_stage": "goal_review"},
        "fail": {"next_stage": "impl", "feedback_from": "fr", "approve_reset": True},
    },
    "goal_review": {
        "approve": {"next_stage": "merge"},
        "reject": {"next_stage": "impl", "feedback_from": "goal", "approve_reset": True},
    },
    "merge": {
        "merged": {"outcome": "merged"},
        "rebased": {"next_stage": "cr", "approve_reset": True},
        "failed": {"next_stage": "impl", "feedback_from": "merge", "approve_reset": True},
    },
}


def next_stage(stage: str, stop: str) -> Transition:
    """查转移表：``stage`` 收到 ``stop`` 之后的转移。

    未知组合抛 ``ValueError``（机械化原则：不猜）。
    """
    stops = _TRANSITIONS.get(stage)
    if stops is None:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    entry = stops.get(stop)
    if entry is None:
        valid = ", ".join(sorted(stops))
        raise ValueError(f"unknown stop {stop!r} for stage {stage!r}; expected one of {{{valid}}}")
    return Transition(
        next_stage=entry.get("next_stage"),
        outcome=entry.get("outcome"),
        feedback_from=entry.get("feedback_from"),
        approve_reset=entry.get("approve_reset", False),
    )


@dataclass(frozen=True)
class DDState:
    """一张 DD 的累计状态。纯函数式：:func:`advance` 返回新实例，绝不改动入参。

    ``round`` 从 1 起，每次进入 impl 加一；``approve_valid`` 在 goal_review approve
    时置 True，任何 ``approve_reset`` 转移置 False；``stage`` 在终态时为 None。
    """

    round: int = 1
    approve_valid: bool = False
    stage: str | None = "impl"
    outcome: str | None = None
    history: tuple[tuple[str, str], ...] = ()


def advance(state: DDState, stage: str, stop: str) -> DDState:
    """按 ``(stage, stop)`` 推进状态，返回新 ``DDState``，不改入参。"""
    transition = next_stage(stage, stop)
    new_round = state.round + (1 if transition.next_stage == "impl" else 0)
    if stage == "goal_review" and stop == "approve":
        approve_valid = True
    elif transition.approve_reset:
        approve_valid = False
    else:
        approve_valid = state.approve_valid
    return DDState(
        round=new_round,
        approve_valid=approve_valid,
        stage=transition.next_stage,
        outcome=transition.outcome,
        history=(*state.history, (stage, stop)),
    )


def warnings_for(state: DDState, *, warn_dd_rounds: int) -> list[str]:
    """DD 轮数越 warning 线（``round >= warn_dd_rounds``）只产告警文本，不改 outcome（GO-6.4）。"""
    if state.round >= warn_dd_rounds:
        return [f"dd_rounds>={warn_dd_rounds}"]
    return []


def build_dd_result(events: Iterable[Event], dd_id: str) -> dict:
    """从一串事件 fold 出 protocol §7 的 DD 结果对象（只读，不写、不调 agent）。

    字段按 §7 逐个产出，缺失用 None；不编造。``outcome`` 的判定写在下面：

    - 有 ``dd.merged`` 终态事件 → ``merged``；
    - 有 ``dd.failed`` 终态事件 → ``failed``（其 ``failure.stage/detail`` 直接取事件 payload，
      普通路径是 impl ``failed``；agent 崩在别的 stage 也由引擎直接写 ``dd.failed``）；
    - 没有终态事件时，按状态机重放结果分两档：走到了 ``goal_review``（FR 已过、等 Goal 审单）
      → ``awaiting_approval``；其余在途（impl / acceptance / cr / fr / merge）→ ``in_progress``。

    ``rounds`` 取状态机重放出的当前轮数（每次进入 impl 加一，首轮为 1）；``head_commit``
    跟随 impl 的 commit 与 merge ``rebased`` 的 new_head 更新；``reviews`` /
    ``acceptance_results`` 按事件顺序累加，不合并不删。
    """
    state = DDState()
    spec_text: str | None = None
    spec_digest: str | None = None
    branch: str | None = None
    head_commit: str | None = None
    merged_commit: str | None = None
    impl_summary: str | None = None
    impl_failed_detail: str | None = None
    acceptance_results: list[dict] = []
    reviews: list[dict] = []
    failure: dict | None = None
    terminal_kind: str | None = None

    for ev in events:
        if ev.dd_id != dd_id:
            continue
        payload = ev.payload or {}
        kind = ev.kind

        if kind == "dd.dispatched":
            if spec_text is None:
                spec_text = payload.get("spec_text") or payload.get("spec")
            if spec_digest is None:
                spec_digest = payload.get("spec_digest")
            if branch is None:
                branch = payload.get("branch")
            if head_commit is None:
                head_commit = payload.get("head_commit")
        elif kind == "dd.acceptance":
            acceptance_results.append(dict(payload))
        elif kind == "dd.stage.finished":
            stage = payload.get("stage")
            stop = payload.get("stop")
            if stage == "impl" and stop == "committed":
                if payload.get("commit"):
                    head_commit = payload.get("commit")
                if payload.get("summary"):
                    impl_summary = payload.get("summary")
            elif stage == "impl" and stop == "failed":
                impl_failed_detail = payload.get("detail")
            elif stage in ("cr", "fr"):
                reviews.append(
                    {
                        "role": payload.get("role", stage),
                        "stop": stop,
                        "summary": payload.get("summary"),
                        "findings": payload.get("findings", []),
                    }
                )
            elif stage == "merge" and stop == "merged":
                if payload.get("merged_commit"):
                    merged_commit = payload.get("merged_commit")
            elif stage == "merge" and stop == "rebased":
                if payload.get("new_head"):
                    head_commit = payload.get("new_head")
            if isinstance(stage, str) and isinstance(stop, str):
                state = advance(state, stage, stop)
        elif kind == "dd.merged":
            terminal_kind = "merged"
            if payload.get("merged_commit"):
                merged_commit = payload.get("merged_commit")
        elif kind == "dd.failed":
            terminal_kind = "failed"
            failure = {"stage": payload.get("stage"), "detail": payload.get("detail")}

    if terminal_kind == "merged":
        outcome = "merged"
    elif terminal_kind == "failed":
        outcome = "failed"
    elif state.outcome is not None:
        outcome = state.outcome
    elif state.stage == "goal_review":
        outcome = "awaiting_approval"
    else:
        outcome = "in_progress"

    if outcome == "failed" and failure is None:
        failure = {"stage": "impl", "detail": impl_failed_detail}

    return {
        "dd_id": dd_id,
        "spec_text": spec_text,
        "spec_digest": spec_digest,
        "outcome": outcome,
        "rounds": state.round,
        "branch": branch,
        "head_commit": head_commit,
        "merged_commit": merged_commit,
        "acceptance_results": acceptance_results,
        "reviews": reviews,
        "impl_summary": impl_summary,
        "failure": failure,
    }


__all__ = [
    "STAGES",
    "TERMINAL_OUTCOMES",
    "DDState",
    "Transition",
    "advance",
    "build_dd_result",
    "next_stage",
    "warnings_for",
]

"""R7：失败语义哨兵 —— 部署契约机器可判 + 哨兵静默死亡/checkpoint 卡死响亮。

零 LLM、只读。哨兵覆盖两侧（spec「落地约定」）：

- 「该响必响」：worker 无产出（全空）/ 哨兵被杀 / checkpoint 卡死必须以响亮
  终态（fault）收尾——不得报 succeeded/exit 0、不得把「全空」判成 converged；
- 「响后重试尊重 retryable=false」：终局错误不得被磨成循环。

上游跟踪（spec「上游跟踪」节）：agent-runtime 座位层「契约违约报 succeeded/
exit 0」不本图修，判据只求立案号 dev-fg-67feadc91821 在案。本模块只登记该号并
提供机器判定的查找函数，绝不做容忍式补丁（不得把「succeeded/exit0」当合法）。
"""

from __future__ import annotations

from typing import Any

#: agent-runtime 座位契约违约的跟踪立案号（在案 = 有跟踪记录文件引用它）。
#: 判据 ③ 只核对它被引用，不修 agent-runtime。
AGENT_RUNTIME_SEAT_CONTRACT_CASE = "dev-fg-67feadc91821"

#: 响亮终态：fault（非 null 的退出口，绝不伪装成 succeeded）。
LOUD_TERMINAL = "fault"

#: 成功终态词汇（CLI 对其 exit 0）——哨兵确认「不得报 succeeded/exit 0」的范围。
SUCCESS_TERMINALS = {"converged", "capped", "partial"}


def worker_output_all_empty(declared: dict[str, Any] | None) -> bool:
    """worker.result.v1 是否「全空」：evidences / proposed_clues / materials 全无。

    全空 = worker 没有任何产出（既有注释「evidences 空 = not_found = done」被 R7
    收紧：只带 leads 的 not_found 仍属 done，但三字段全空是静默无产出，必须响亮）。
    """
    if not isinstance(declared, dict):
        return False
    return (
        not declared.get("evidences")
        and not declared.get("proposed_clues")
        and not (declared.get("materials"))
    )


def judge_loud(result: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """机器判据：run 终态是否「响亮」——必须是 fault，不是 succeeded/exit 0。

    - 响亮 = terminal == fault（真实非 null 终态，spec「响亮 ≠ 标错」）；
    - 把「全空」判成 converged / 报 succeeded / exit 0 一律不响亮（判红）。
    """
    terminal = result.get("terminal")
    loud = terminal == LOUD_TERMINAL
    verdict: dict[str, Any] = {
        "terminal": terminal,
        "loud": loud,
        "exit_zero": terminal in SUCCESS_TERMINALS,
    }
    return loud, verdict


def escalate_all_empty_terminal(
    terminal: str, evidence_count: int, *, converged: str = "converged", fault: str = "fault"
) -> tuple[str, str | None]:
    """哨兵收敛终态：converged 但零 evidence 产出（全空）→ 响亮 fault。

    spec「失败语义哨兵」：不得把「全空」判成 converged、不得报 succeeded/exit 0。
    partial（有 blocked）是另一条响亮路径，不在这里动。返回 ``(terminal, reason)``，
    无升级时 reason 为 None。
    """
    if terminal == converged and evidence_count == 0:
        return fault, "全空：run converged 但零 evidence 产出（worker 无产出哨兵）"
    return terminal, None


def tracking_case_on_file(case: str = AGENT_RUNTIME_SEAT_CONTRACT_CASE) -> bool:
    """判据 ③ 的「在案」：立案号被仓库里的跟踪文件引用。

    只读扫描 ``docs/`` 下引用该立案号的跟踪记录（不修 agent-runtime，不落地任何
    新文件）。任何一处引用即算在案。
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent
    docs = repo_root / "docs"
    if not docs.is_dir():
        return False
    for path in sorted(docs.rglob("*.md")):
        try:
            if case in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


__all__ = [
    "AGENT_RUNTIME_SEAT_CONTRACT_CASE",
    "LOUD_TERMINAL",
    "SUCCESS_TERMINALS",
    "escalate_all_empty_terminal",
    "judge_loud",
    "tracking_case_on_file",
    "worker_output_all_empty",
]

# fleet-graph 线「裁决送达 dd 单但线不醒」唤醒机制 + 可观测判据 spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：A 类漏报（宪法执行含义①漏报即缺口）。监督面 2026-09-02 14:3x 立案案 1(P0)。

## 1. 现象与真因（读源码坐实）

- 现象：14:1x 监督面发 3 条 `work.decision.v1` 到 board:work-notes(seq 2045/2046/2047)，三张 dd 单立刻消费裁决并推进（terminal=complete/stage=merger）；14:2x `wf-216dc3` 仍 parked。调度器 refusal 原文：`parked_awaiting_decision … parked until a wake fact appears (inbox message, goal.md change, or the parked fields are cleared from its stall-state file)`。
- 真因（坐实）：线 parked 的唤醒事实**只有三种**（`daemon.py::_check_wake`）：① inbox 消息（`wake.inbox_message_after`）；② `goal.md` content_revision 变化（`wake.goal_revision()` != `parked_goal_revision`）；③ 手工清 `.scheduler/<wf>.json` 的 parked 字段。**板上裁决不是线的唤醒事实**。
- 裁决落地路径（`decision_bridge`）：`work.decision.v1` 依 `refs[].target_entity`（question_note_id）反查 waiting owner——`DdOwnerSource`（dd 单 `awaiting_gate`）或 `LineOwnerSource`（线 parked，仅当线的 `board_question_note_id` 与裁决 question note **相同**才 wake）。当裁决目标是一条 dd 单、而某条线正 parked 等这张单（线的 `dispatched_by`/阻塞理由指向该 dd 单）时，裁决只 resume 了 dd 单，**线自己的 `board_question_note_id` 与 dd 单的 question 不是同一个 note，线不会被唤醒**——wf-216dc3 实证。

## 2. 修复方向（契约；方向自决，约束+判据如下）

1. **机制（唤醒）**：给 parked 线补第 4 种唤醒事实——「该线等待的裁决已被消费」。落地方向（dev 自决其一或等价）：当 dd development 离开 `awaiting_gate`（gate 被裁决 resume）且其 `dispatched_by == 该线 folder_id` 时，唤醒该线；和/或当 `work.decision.v1` 命中某 dd 单的 question_note_id 时，额外唤醒 `dispatched_by` 指向的 parked 线。唤醒必须走既有受控入口（`LineOwnerSource._wake` / scheduler `_wake`，清 parked 字段），**不直接点火**（点火由下一次 `decide` 接管）。
2. **可观测/可告警**：新增一个能红的判据信号——「线 parked 等裁决 → 裁决落地并被单据消费 → 线在 N 个调度 tick 内仍未点火」可观测/可告警（漏报不得再静默发生）。

## 3. 判据（两向能红）

1. **阳性（唤醒生效 + 漏报可观测）**：造一条 `parked + waiting_on=decision` 的线 L（stall-state 有 `board_question_note_id`），造一张 dd 单 `dispatched_by=L` 停在 `awaiting_gate`；落地一条 `work.decision.v1` 命中该 dd 单的 question note → dd 单被 resume → 线 L 在 N tick 内不再 parked（`parked_run_id` 清空）。且「裁决已被消费但线仍 parked 达 N tick」必须可观测（信号/告警红，annotations 带 line id 与等待时长）。
2. **阴性（不许恒亮）**：一条正常 parked 等裁决、裁决尚未落地的线（如本线 wf-6475fd 此刻）→ 不得触发唤醒判据/告警信号开火。变异：把「裁决已消费」换成「只要 parked」即唤醒 → 必须红。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_parking.py tests/test_scheduler_daemon.py tests/test_decision_bridge.py
make verify
```

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree add 到 `/data/worktrees`；生产主 checkout 只读、仅 `git pull --ff-only`。
- 唤醒是「清 parked 字段走 decide」，不是直接 systemd 点火；与裁决 MCP/decision_bridge 既有 resume 语义不冲突。
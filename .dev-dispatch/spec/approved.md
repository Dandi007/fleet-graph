# 缺陷⑭ · wake fact 词表纳入 work.decision.v1（wf-8d9737）

## 背景
`src/fleet_graph/scheduler/wake.py` 是驻停线的机械唤醒词表（M1），现役事实源：inbox 新消息、goal.md content_revision 变化、dd `awaiting_gate`/终态、operator 手清 stall 文件。D5 口径下闸裁决以 `work.decision.v1` 落 board 频道（`board:work-notes`，`refs[].target_entity == 驻停时记下的 question_note_id`，`payload.decided_by` 非空）——但词表里没有这个事实源：真机上 board 裁决落了，驻停在 `waiting_decision` 的线只能靠外力 resume（本轮 wf-8d9737 闸即手动 `resume=true` 才走）。裁决落板是事实，事实就该能点火。

## 交付
1. **wake.py 新增 board-decision 探针**：`decision_landed(question_note_id, after_epoch) -> bool`——对 `board:work-notes` 频道做只读 GET（复用现有 service/line token 解析纪律与 `WAKE_TIMEOUT_SECONDS` 短超时），存在一条 `kind == "work.decision.v1"`、`refs[].target_entity == question_note_id`、`created_at > after_epoch`（或按频道 seq 水位）且 `payload.decided_by` 非空的消息即 True。
2. **stall-state 记 `question_note_id`**：线驻停进 `waiting_decision` 时把 awaiting 的 question_note_id 写进驻停档（若 M1/M2 已写则复用，不重复造字段）；探针以它为靶。
3. **scheduler 接线**：daemon 对 `waiting_on=decision` 的驻停线在 tick 里多探一次 board-decision 事实；命中即点火下一代（唤醒≠消费：驻停档的 lifted 仍归 decision 面/M2 路径，唤醒只负责让线回来观察事实——与 inbox 唤醒同一语义，不得在 wake 里清 stall 或写任何裁决状态）。
4. **失败纪律沿用**：探针失败 raise、调度器 fail-open（当未驻停退避），与现有 probe_error_tag 归因一致；读用 GET，禁止 publish/consume/lease。

## 判据（正/负双向）
- 阳性：`waiting_decision` 驻停线 + 板上出现靶向其 question_note_id 的 work.decision.v1 → 下一 tick 点火，round 输入可见唤醒事实；inbox/goal-revision/dd 既有词表行为逐字不回归。
- 阴性（脱靶不点）：板上 work.decision.v1 的 refs 指向**别的** question note / 无 refs / `decided_by` 空 → 不点火。
- 阴性（时间向）：`created_at` 早于驻停时刻的旧裁决 → 不点火。
- 阴性（越权面）：探针不做任何写动作（无 publish/consume/stall 清写）；探针挂 → fail-open 不锁线。
- 阴性（回归）：删掉新探针的发射 → 用例必须红（不是靠轮询兜住）。

## 边界
- 只动 `src/fleet_graph/scheduler/wake.py`、`daemon.py`（接线最小面）、驻停档读写最小点；测试新增 `tests/test_wake_fact_decision.py`；零测试删除。
- 不改 decision_deliver/M2 路径语义；不做 D20 Stop-Response 拓扑；不动 goal 面；不引入新依赖。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_wake_fact_decision.py'
bash -lc 'make verify'
```

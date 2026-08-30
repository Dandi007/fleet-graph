# decision-bridge 吞裁决（no_waiting_owner）第二条驻停写路径——#170 追修 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：wf-216dc3 M4 顺带立案跟进（goal.md 顶块第 3 条「🔴 新立案（P1，给你的 #170 追修）」）。🔴 生产缺陷：人类监督面裁决 seq 1002 被桥吞，且 #170 部署后新代（g14）仍复现。
- 类别：缺陷修复，非判据变更。goal.md 不改。
- 依赖：#170（PR #170，main 9627a8c）只修了 daemon 主路径；本单补上它没覆盖的第二条驻停写路径。

## 根因（已实读，非推断）

#170 修的是 daemon 侧驻停写点（`_ask_board` 单写原子化 + `account_last_run` 推进/基线分支显式保留
`board_question_note_id`）。但存在一条 daemon 之外的驻停写路径——goal 线图内 E2 中断：

- `graphs/goal_line.py::decision_interrupt`：coordinator 判 `blocked + waiting_on=decision` 时，调
  `deps.interrupt.ask()` 物化 board question，并 `deps.interrupt.persist(checkpoint)` 把
  `question_note_id`/`card_entity_id` 写入 **`goal-interrupt.sqlite3`**（`interrupts` 表，
  `goal_interrupt/store.py`）；同时 `deps.artifacts.write_terminal(...)` 直写 `terminal.json`
  （blocked + waiting_on=decision）。
- 这条通道 **从不** 把 `question_note_id` 写进 scheduler 驻停态 `.scheduler/<folder_id>.json` 的
  `board_question_note_id`。

而决策桥 `decision_bridge/owners.py::LineOwnerSource.discover_all` **只读** `.scheduler/<folder_id>.json`
（要求 `parked_run_id` 非空，并以 `board_question_note_id` 做映射）。故回答 E2 question 的
`work.decision.v1` 无法经此解析 → `noop`（no waiting owner references this question）→ cursor 越过 →
裁决永久被吞。

### 真机证据（wf-216dc3，g14，已实读）

1. `runs/wf-216dc3/goal-interrupt.sqlite3` `interrupts` 两行：
   - g11/round1 question `msg_01M19S4S0ZQE5QKT8ZS37ER4AV`（card `msg_01M19S4S0W57Q07Y8PFMN67P95`）
   - g13/round5 question `msg_01M1A6F9CSBMGBBE47HD5ZBAZN`（card 同上）
   `resume_receipts` 空 —— 两问均未resume。
2. `runs/.scheduler/wf-216dc3.json`：`board_card_entity_id=msg_01M19S4S0W57Q07Y8PFMN67P95`（在）
   而 `board_question_note_id=null`（丢）。
3. `decision-bridge/bridge.sqlite3` `receipts`：`source_message_id=msg_01M1A6N3H9SC76HK716R1KFFFN`
   → `noop | no waiting owner references this question`；cursor 已越过 seq 1002（现 1003）。

## 交付 A：两条驻停写面收敛（主修）

使「图上 E2 中断提问」与「scheduler 驻停态」收敛到同一 question note，令 `board_question_note_id` 在
任一驻停后都非空且与 E2 `question_note_id` 一致。机制二选一（实现前先读本节与对应测试，勿破坏既有
语义与幂等 key）：

- 写面收敛（推荐）：E2 中断持久化 checkpoint 的同时，把 `question_note_id`/`card_entity_id` 同一次
  写入 scheduler 驻停态 `.scheduler/<folder_id>.json` 的 `board_question_note_id`/
  `board_card_entity_id`（幂等；须处理与 daemon 每 tick 全量覆盖的写写竞态——要么同一 writer 写、要么
  daemon 写前重读）。
- 读面收敛：`decision_bridge/owners.py::LineOwnerSource.discover_all` 除读驻停态外，并入
  `goal-interrupt.sqlite3` `interrupts`（未 resume 的）作为等待方，令回答 E2 question 的裁决也能精确
  命中该线。

无论哪种，落地后必须满足：任一驻停（daemon park 或 E2 图上中断）后，桥都能把 refs 该 question note 的
`work.decision.v1` 解析到该线，不再 `no_waiting_owner`。

## 交付 B：全部驻停写点核尽（不仅 daemon 主路径）

逐点核对（既有写点必须保留/一致写 `board_question_note_id`，新增写点必须纳入同一次覆盖）：
1. `daemon.py::_ask_board`
2. `daemon.py::_establish_park`（含 inbox 有信 / probe 失败两条早退写）
3. `daemon.py::account_last_run` 推进分支 + 基线采纳分支
4. `daemon.py::_wake` 与 `decision_bridge/owners.py::_wake`
5. `graphs/goal_line.py::decision_interrupt` → `goal_interrupt/runtime.py::LineInterruptPort.ask`
   （本条为本单补上）

## 交付 C：测试（合成 stall-state + 合成 E2 store，禁触真网）

1. E2 中断后 `board_question_note_id` == 该轮 `question_note_id`（新回归：合成两条写面，断言收敛）。
2. 回答 E2 question 的 `work.decision.v1` 经决议返回命中该线，不返回 `no_waiting_owner`
   （沿用 `test_decision_bridge.py` 同款）。
3. #170 既有两测试零回归；M1/M2/M3/M4 既有测试零回归（`make verify`）。

## 可复现验收

```dd-acceptance
make verify
```

## 落地后真机复检（worker，不并入本 dd acceptance）

- 重建后 `.scheduler/wf-216dc3.json` 不再出现 `board_card_entity_id` 在而 `board_question_note_id=null`
  的矛盾；任一 E2 驻停的 question note 跨 terminal 入账存活，`board_question_note_id` 非空且与
  goal-interrupt store 一致。

## 铁律

- 只改上述收敛相关的 scheduler / decision_bridge / goal_interrupt 代码与对应测试；不碰判据
  （goal.md 验收断言）、harvest 子图、allowlist 语义、E1–E7 词表。
- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only。
- 判据（goal.md 验收断言）只有用户能改。
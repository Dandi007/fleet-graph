# decision-bridge 吞裁决（no_waiting_owner）缺陷修复 spec

- 目标仓：`/data/code/self/fleet-graph`。
- 归属：M4 顺带立案。🔴 生产缺陷：人类白名单授权裁决被桥吞。
- 类别：缺陷修复，非判据变更。goal.md 不改。
- 依赖：M1/M2/M3 已合入 main。本单只改 `scheduler/daemon.py` + 对应 scheduler 测试，不碰 harvest 子图 / allowlist / E1–E7 词表。

## 根因（已实读，非推断）

入点 `/v1/decisions` `source_message_id=msg_01M19S85P5EV00HTHV96GTSXSA`（board seq 972，`work.decision.v1`，`payload.card_entity_id=msg_01M19S4S0W57Q07Y8PFMN67P95`，`decision=APPROVE`）。

`decision-bridge/bridge.sqlite3` receipts 该 id 已 seal：`status='noop'` `reason='no waiting owner references this question'`（target_kind/id/question_note_id/card_entity_id 全空）。即人类授权裁决被桥判 no_waiting_owner 吞掉（consumed-not-durable 同族）。

等待方登记缺陷两处（均在 `scheduler/daemon.py`）：

1. **登记随入账被抹**：`account_last_run` 推进分支（L661–677）重建 stall-state JSON 时显式保留 `board_card_entity_id`（L671）却漏掉 `board_question_note_id`。`_write_stall_state` 整文件覆盖，故每次新 terminal 入账都把等待方登记的 question note id 抹掉。实锤：`/data/fleet-graph/runs/.scheduler/wf-216dc3.json` 现 `board_card_entity_id` 在而 `board_question_note_id=null`。
2. **park 与提问两段式写**：`_establish_park` 先写 `parked_run_id`/`parked_at`（L805）再 `_ask_board` 持久化 question note id（L915），存在「已 park 但 question id=null」的可观测窗口。

决议时 `LineOwnerSource.discover_all()` 读到 `board_question_note_id=null` → `resolve_decision` `candidate_ids` 过滤空 id → `referenced` 空 → `matches` 空 → 返回 `no_waiting_owner` → `_seal_noop` 终态 → cursor 越过 seq 972 → 裁决永久被吞。

## 交付 A：登记持久化（主修）

1. `account_last_run` 推进分支把 `board_question_note_id` 与 `board_card_entity_id` 同列保留（均 per-line，与 `stall_state` L546–551 注释一致）；写出的 dict 加入 `"board_question_note_id": state["board_question_note_id"]`。
2. 采纳基线分支同样显式写 `board_card_entity_id=None` / `board_question_note_id=None`（schema 稳定）。

## 交付 B：park 与提问一致写（消除 null-question 窗口）

`_establish_park` 调整为：先 `_ask_board`（物化 card + 发布 question，把 `board_card_entity_id`/`board_question_note_id` 写进 state 但不单独落盘），再一次性写含 parked 快照与两 board 字段的完整 JSON。语义不变：提问失败仅丢 note，parking 照旧 effective。

## 交付 C：测试（合成 stall-state，禁触真网）

1. `account_last_run` 推进分支：预置 stall-state 含两 board 字段，新 terminal 入账后断言两字段均仍在。
2. `LineOwnerSource` 决议回归：line 已 park 且 `board_question_note_id=q`，`resolve_decision` 对引用 q 的 decision 返回 ok。
3. M1/M2/M3 既有测试零回归（make verify 含 E1–E7 词表负例、conformance clean）。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `scheduler/daemon.py` + 对应测试；不碰判据、harvest 子图、allowlist 语义、E1–E7 词表。
- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull。
- 判据（goal.md 验收断言）只有用户能改。
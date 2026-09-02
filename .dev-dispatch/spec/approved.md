# M0 决策读模型对账补口：owner.id 为空却「已送达且被消费」被误记 swallowed 的分支

## 背景（监督面实锤的事实，非推断）

读模型 `:7494` `/v1/decisions` 的终结对账（`src/fleet_graph/state/fleet_state.py`
`_receipt_to_decision`）只在 receipt 的 `target_kind == "dd"` **且** `target_id` 非空时，
才拿单据侧（events.jsonl `human_gate` + `success` / status.json 离开 `awaiting_gate`）再对一声。
当 bridge receipt 在评估瞬间无法把裁决归到某个「等待方」——`target_kind`/`target_id` 为空、
`reason = "no waiting owner references this question"`——即使该裁决事后被单据消费，读模型仍把它
记为 `swallowed` 且 `owner.id` 为空，错把「已送达且被消费」记成 swallowed。

监督面已实锤一例：发布裁决 `msg_01M1H8T8A5M5YBQ8EDV9AHGB5Y` 后 39s，单据
`dev-fg-14a5a4b5f7b9` `human_gate success` 并 `merger success` 完成，而同一时刻读模型对这条
消息记的是 `state=swallowed`、`owner.kind="" / owner.id=""`、
`reason="no waiting owner references this question"`。

## 修法（监督面已给方向，照此实现，不得自造另一套）

当 receipt 的 `target_id` 为空时，用 `card_entity_id` 反查其所属 development：遍历
`dd_root/*/record.json`（`record.json` 含 `card_entity_id` 字段），找 `card_entity_id` 命中的单，
把 `owner.kind="dd"`、`owner.id=<development_id>` 补上，再走既有 `_document_gate_consumed` 对账。
bridge receipts 表已带 `card_entity_id` 列（`src/fleet_graph/decision_bridge/store.py`），可直接读，
无需新增写面/新数据源。

## 双向判据（不可弱，逐字）

### 阳性（positive）

构造一条「发布后单据立刻推进」的用例：receipt 的 `target_id` 为空（`owner.id` 为空、
reason 含 `no waiting owner`），但 `card_entity_id` 能反查到一条 development，且该单单据侧
证明已消费——events.jsonl 出现 `human_gate` + `success`，或 status.json 已离开 `awaiting_gate`。
该裁决必须记 `consumed`，不许记 `swallowed`，且 `owner.id` 被补成这条 development 的 id。

### 阴性（negative）

构造一条「真丢」的用例：refs 空（无 `card_entity_id` 可反查、或反查不到任何 development）
或卡片错配（`card_entity_id` 与任何单都对不上）。该裁决仍必须记 `swallowed`，不许因为上面的
放宽而把真丢的漏掉。

## 不可放宽

反查只能「补 owner 并据此提升为 consumed」，不得把「真丢」（refs 空 / 卡片错配 / 单据侧仍
waiting / 单据侧缺失不可读）误提升为 consumed。反查不到 development，或单据侧无消费证据时，
维持 swallowed（或显式标注 unreconciled），绝不静默归 consumed。

## 验收

新增回归夹具（owner.id 为空分支的阳性 + 阴性）落进 `tests/test_decisions_reconciliation.py`。
不得删除既有测试。

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_decisions_reconciliation.py
```
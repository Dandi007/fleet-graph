# 读模型裁决口径修正 —— 从「快照」改「对账」

读模型 :7494 `/v1/decisions` 当前把「已送达且被消费」的裁决误记 swallowed（口径不可靠，监督面 progress 第100行更正）。`src/fleet_graph/state/fleet_state.py` 的 `_receipt_to_decision`（`_RECEIPT_STATE`）直接按 bridge receipt status（intent_recorded/resumed/noop/refused）映射 consumed/swallowed——这是「评估瞬间 owner 是否在等」的快照，不是「该裁决是否真被消费」的对账。

## 交付物

1. 改读模型 `/v1/decisions` 的**终结对账**逻辑：一门裁决记 `consumed` 还是 `swallowed`，改由**单据侧对账**判定——单据侧 events.jsonl 出现 `human_gate success`、或 status.json 离开 `awaiting_gate`，即 consumed；而非单凭 bridge receipt 瞬时 status。bridge receipt 仍作送达链参考，但不再单凭它定 swallowed。
2. 补两组回归夹具：
   - **阳性夹具**：栽「发布后单据立刻推进」（events.jsonl 记 human_gate success / status.json 离开 awaiting_gate）→ 该裁决必须记 consumed。
   - **阴性夹具**：真丢（refs 空 / 卡片错配）→ 仍必须 swallowed。
3. `tests/test_decisions_reconciliation.py`（本单验收目标，可重复执行）。

## 口径判据（不可弱，逐字对齐监督面更正）

- **仅「refs empty 归零」仍成立**；`swallowed` 总数归零**不再作为判据**。
- **阳性**：栽「发布后单据立刻推进」→ 必须记 consumed。
- **阴性**：真丢（refs 空 / 卡片错配）→ 仍必须 swallowed。

## 红线纪律

- 判定改为对账（单据侧 events.jsonl / status.json）而非快照；单据侧对不上/不可判定时须显式标注，不得静默归 swallowed 或 consumed。
- 不删除/改写其它既有测试或 scripts/；`make verify` 不回归；新增文件过 ruff/format。
- 验收命令可重复执行。

## 验收

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_decisions_reconciliation.py
```
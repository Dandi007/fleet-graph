# E6 evidence_note 落板 ref 目标实体错填 folder_id 修复 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面（wf-216dc3 worker 取证，2026-08-31 14:14 有界复证导出新缺陷），E6 heartbeat_stale 首发射实证。
- 类别：缺陷修复，非判据变更。goal.md 不改。
- 依赖：main@fb75492（#183）已含 E6 `e6_stop.py`（#172 交付）。

## 根因（已实读，非推断）

真机原始回显（reports/e6-wf-fdd6ac.json，findings#82）：E6 停转本体成功（outcome=stopped /
stop_exit_code=0 / active_after=false），但 `evidence_note` 步 FAILED：

```
BusError('agent-bus returned HTTP 422: {'code': 'DERIVATION_ERROR',
'message': "ref target entity 'wf-fdd6ac' not found", 'details': {'retryable': False}}')
```

代码位置 `src/fleet_graph/supervise/e6_stop.py::evidence`（编排 evidence 节点，L303-309）：

```python
published = deps.bus.publish(
    WORK_NOTES, NOTE_KIND,
    {"card_entity_id": folder_id, "note": note, "note_type": "evidence"},
    f"e6-stop:{event.key}",
    refs=[{"target_entity": folder_id}] if folder_id else [],
)
```

把 `payload.card_entity_id` 与 `refs[].target_entity` 都填成了 raw folder_id（`wf-fdd6ac`），而
agent-bus 的 refs 解析要求 `target_entity` 是**已注册的板实体 id**（`msg_01M...`），folder_id 不是
实体 → 422 DERIVATION_ERROR。正确契约见 `audit.py::publish_report`（refs=[{target_entity: card_entity_id}]，card_entity_id 为真实板实体）与 `Board.note`；A2 arbiter 已有同款纪律并被 `test_arbiter.py::test_non_entity_evidence_ref_is_not_published_as_target_entity` 钉死——E6 绕过了 `Board.note`，直接裸调 `bus.publish` 才把非实体串塞进了 ref。

E6 的正确 ref 目标 = 该线 goal-line board card `board_card_entity_id`，持久化于调度器 stall-state `<run_root>/.scheduler/<folder_id>.json` 的 `board_card_entity_id` 字段（launcher `--board-card` 线程下发）。wf-fdd6ac 现该项为 `null`（尚无卡）——此时必须如实 skip（best-effort），绝不把 folder_id 当 ref 伪造。

系统侧同族（如实记，不在本单修）：`harvest.py::evidence` 同构（`development_id` 当 `card_entity_id`/`refs.target_entity`），因 harvest 被 deny-all / 无真实 fleet-sentinel 单而从未走到 evidence 步，属潜伏同族缺陷，留监督面后续轮再修。

## 交付 A：E6Ops 增 board 卡 id 读口

`supervise/e6_ops.py`：`E6Ops` 协议与 `DefaultE6Ops` 各加一个方法 `board_card_entity_id(folder_id, run_root) -> str | None`，读 `<run_root>/.scheduler/<folder_id>.json` 的 `board_card_entity_id`（空/null/缺失 → None），复用既有 `_stall_generation` 的文件读取模式。不改 stop/gate 语义。

## 交付 B：evidence 节点改用真实实体、缺失即 skip

`supervise/e6_stop.py::evidence`：
1. `card_entity_id = deps.ops.board_card_entity_id(folder_id, deps.run_root)`。
2. 若空/None：记录 `evidence_note` 步 `ok=False`、detail 含「board_card_entity_id 缺失……note 未挂卡（best-effort）」，**不**发布任何 note、**绝不**发射 `refs=[{target_entity: folder_id}]`。
3. 若非空：以 `card_entity_id` 作 `payload.card_entity_id` 与 `refs=[{"target_entity": card_entity_id}]` 发布，idempotency_key 保持 `e6-stop:{event.key}`（建议经 `Board.evidence`/`Board.note` 走规范路径）。

## 交付 C：测试（合成 fake bus + fake ops，禁触真网/真 systemctl）

1. 合成一例 heartbeat_stale 事件 + fake ops `board_card_entity_id -> "card-xyz"`，跑图后断言已发布 note 的 `payload.card_entity_id == "card-xyz"` 且 `{ref["target_entity"]} == {"card-xyz"}`、`"wf-fdd6ac"` 不作为任何 target_entity 出现。
2. fake ops 返回 None：断言零发布、`evidence_note` 步 `ok=False` 且 detail 含「缺失」。
3. `make verify` 全绿；M1–M4 / supervisor conformance 既有测试零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `supervise/e6_ops.py`、`supervise/e6_stop.py`（evidence 节点）+ 对应测试；不碰判据（goal.md）、harvest 子图/allowlist 语义、E1–E7 词表、authorize_e6_stop 门禁语义。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅 ff-only pull。
- harvest.py 同族潜伏缺陷不在本单改（如实记 findings 留后续）。
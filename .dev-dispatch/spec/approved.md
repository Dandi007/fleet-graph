# decision-bridge #170 追修——park 提问 idempotency_key 随意图单调变体 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 05:0x goal 直写新立案 P1，decision-bridge #170 追修。
- 类别：缺陷修复，非判据变更。goal.md 不改。
- 依赖：#170（9627a8c）已合入 main。

## 根因（已实读，非推断）

`_ask_board`（scheduler/daemon.py）与 goal_interrupt 的 `ask` 都以**不变**的 `idempotency_key = parked:<folder_id>:<run_id>` 发布提问 note，但 note 正文（尤其 `blocker` 字段 / `round_no`）会随重 park / 重试变化 → 同一 key + 不同 intent → agent-bus 409 IDEMPOTENCY_CONFLICT（`Same idempotency_key with different intent`, retryable=False），使 board_question_note_id 登记保持 null，未来人类裁决被 no_waiting_owner 吞（seq 1002）。

两处独立真机复现（据）：
1. wf-216dc3 g14：`/data/fleet-graph/runs/.scheduler/wf-216dc3.json` 现 `board_card_entity_id=msg_01M19S4S0W57Q07Y8PFMN67P95`（在）/ `board_question_note_id=null`；journal `park established` 行 `board_question: "question_failed:BusConflict:agent-bus returned HTTP 409: {'code':'IDEMPOTENCY_CONFLICT','message':'Same idempotency_key with different intent','details':{'retryable':False}}"`。
2. wf-d002a6：`/data/fleet-graph/supervisor/reports/e1-msg_01M15G99W3JR80AGV385GFQASQ.json` `question_payload.note` 把上一次 409 错误当 blocker 塞回、key 未变 → 409 自锁循环。

## 交付 A：写点枚举（全部 park 提问 idempotency_key 构造点，不只 daemon 主路径）

1. `scheduler/daemon.py::_ask_board`（~L913）：`idempotency_key=f"parked:{line.folder_id}:{record['run_id']}"`，note 含 `blocker`（变体）。
2. `goal_interrupt/runtime.py::ask`（~L118）：run_id 在场时 `question_key = f"parked:{self.folder_id}:{self.run_id}"`，note 含 `round_no`（变体）；无 run_id 走 `e2-question:{folder}:{generation}:{round}`（已含 round，无缺陷，保留）。
   —— 两处都必须改；任何新出现的 `parked:` key 构造点同样受此约束。

## 交付 B：key 随提问意图单调变体

把两处 `parked:<folder>:<run_id>` 改为 `parked:<folder>:<run_id>:<content-variant>`，`content-variant = sha256(canonical(note_text)).hexdigest()[:12]`（note_text=最终拼好的提问正文）。不变量：同 key ⟺ 同意图；异意图必得异 key（绝不 409）；同 key 重发（同正文）仍 dedup 幂等。实施须保证两写点用同一 key 派生函数、同一 canonical 序列化，避免两处各自拼 key 再次漂移。

## 交付 C：测试（合成 board fake，禁触真网）

1. **复现回归**：同一 run 两次 ask——第二次 blocker 变化 → 断言两次 idempotency_key 不同、fake board 不再 409；第二次 blocker 不变 → key 相同（幂等 dedup）。
2. **写点枚举负例**：conformance/单测断言两处 `parked:` key 均含 content-variant（缺 variant 判红），防止未来回归到不变 key。
3. `make verify` 全绿；M1–M4 既有测试零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改上述两写点 + 对应测试；不碰判据（goal.md 验收断言）、harvest 子图、allowlist 语义、E1–E7 词表。
- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull。
- 判据（goal.md 验收断言）只有用户能改。
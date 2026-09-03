# Spec M3.1（wf-8d9737）· 引擎六缺陷收束——裁决落地链修复（监督面 2026-09-03 派工）

> 谱系：c18ba1ac025f（901b8c00…，base 误从 main 切——监督面已 REJECT，非 release/wf-8d9737 后代）之后，本谱以 **release/wf-8d9737（rebase 后头 a53f93c）** 为 target_base 承载监督面六缺陷派工（①②阻断优先）。判据锚：goal.md §七 监督面 2026-09-03 派工令 + S10（裁决送达必须落地）/S11（不允许绕过授权的投递路径）。与 design.md/golden-order 冲突以后两者为准。

## 要交付的行为（全部在 fleet-graph 仓）

1. **【阻断·①】`_deliver_dd` 只 resume 不发裁决**：现形态 resume 成功即返回 delivered，但单据只被 resume、从未收到裁决语义（REJECT 应导致 terminal=refused/rework 而非再次进普通 run）。修复：`_deliver_dd` 在 resume 后**必须把裁决（decision+reason）真正送达单据**——REJECT ⇒ 单据 terminal=refused（exit=rework 语义，engine 自行进 rework 或驻停）；APPROVE ⇒ 单据按既定 gate APPROVE 语义放行。回执语义随之收紧：**delivered/consumed 只在「单据已按裁决语义转移」时报**（S10① 被消费判据）。
2. **【阻断·②】一次性认领被 failed resume 烧掉**：现形态对 awaiting_gate 单的认领（claim/lease）在 resume unit 失败（如 TEMPFAIL）后即耗尽，同一裁决无法重投（already_resumed/claimed 拒绝）。修复：**resume 失败必须归还认领**——同一裁决（同 action_key）重投必须被接受并再次尝试 resume，不得因上次失败而拒绝；只有裁决真正被消费（单据按语义转移）后同 action_key 才幂等去重。
3. **implement 失败重试的回执 parent 不推进**：implement 失败（如 PROVIDER_UNAVAILABLE）后重试成功时，新回执的 `parent_handoff_receipt_digest` 仍指向过期链头（re_prepare 后不推进）。修复：每次 re_prepare/重试后回执 parent 必须锚定最新链头，receipt chain 无断链/回卷。
4. **`LINE_NOT_PARKED` 拒绝语硬编码 wrong 状态**：拒绝信息里写死某个状态词，实际拒绝原因可能是其它状态。修复：拒绝语携带单据**实际当前状态**（动态读），不得硬编码。
5. **两份状态真相（读模型 vs stall 文件）**：同一单据状态在 read-model 与 stall/terminal 文件两处各写一份、可能不一致。修复：收敛为**单一真相源**——读模型派生或文件为源（择一为 authority，另一侧只读派生），不一致时以 authority 为准并消除产生分叉的写入路径。
6. **`status.json` 说谎缓存删除**：status.json 是无失效逻辑的缓存副本（见过期/滞后值被当真机事实引用）。修复：**删除该缓存文件**（或改为带失效逻辑的派生视图），消费方一律读 checkpoint/authority。

## 判据（监督面红靶，测试必须走注册的 @mcp.tool 面，不得绕过 MCP 层直调内部函数）

- **红靶①（对应缺陷 1+2，阻断）**：造一张真实 `awaiting_gate` 单，经注册的 `decision_deliver` MCP 工具（@mcp.tool 面）投 REJECT：断言 **`board.decision_for`（或等价的 decision 读模型）可解析出该裁决** 且 **单据 terminal=refused**（非仅 resume）。
- **红靶②（对应缺陷 2，阻断）**：对同一单据、同一裁决 action_key，在首次 resume **失败**（unit TEMPFAIL/未消费）后重投：断言重投**不被 `already_resumed`/认领耗尽拒绝**，可再次尝试 resume；且裁决真正被消费后同 action_key 幂等。
- 其余缺陷（3-6）各有对应机械断言：receipt chain parent 单调推进（断链即红）；LINE_NOT_PARKED 拒绝语含实际状态（硬编码词不匹配即红）；读模型/stall 单一 authority（构造分叉场景断言以 authority 为准）；status.json 不再作为可消费缓存被读（读即红/文件已删）。
- S10/S11 回归不倒退：形态 A/B 投递路径合一仍生效（非派单方投递必 NOT_DISPATCHING_LINE）；裁决成功判据仍=被消费。

## 测试与验收

- 新增/扩展 `tests/test_m3_engine_defects.py`（或并入现有文件）：上述判据逐条用例，**MCP 工具面（@mcp.tool 注册路径）为测试入口**——用 FastMCP in-process 客户端或引擎注册表调用，不得直调 `deliver_decision()` 内部函数冒充 MCP 通路。**零测试删除**。
- 全量回归与冻结基线对比：基线锚定本单冻结 target_base（release 头 a53f93c），判据=红项集合不得扩大、绿→红翻转即拒。

## 边界

- 只动 fleet-graph 仓（dd 引擎 deliver/resume/claim/receipt 链、状态面、MCP 工具注册面）；不改外部 plugin 契约（review-result.schema.json 等 pinned schema 不动）。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m3_engine_defects.py tests/test_m3_line_selfgate.py'
bash -lc 'make verify'
```

# goal-driven 入册流水线补全（提交→监管可见→放行）spec

> 依赖：goal-mcp-surface-split-spec.md（goal 面 :5611 独立服务）先合入。
> 设计原则（用户方向 + 现有治理）：**统一入口统一的是提交（submit），不是点火（ignite）**。
> 任何 agent（TUI/内置）都从同一个 goal-driven MCP 提交入册申请；申请对监管面结构性可见；
> 放行权（roster `enabled`）仍属监督面，走既有 roster PR + release 路径不变。
> （随卷副本，源 wf-216dc3 同名文件，内容一致）

- 目标仓：`/data/code/self/fleet-graph`。
- 背景缺口（2026-08-31 探查实证）：
  1. `goal_enroll` 落的 `goal-roster.jsonl` 是平行登记簿，**零生产读者**——scheduler / state read-model / supervisor 全部只认 `config/ronin-lines.json`；入册成功 ≠ 线会跑。
  2. `GoalRosterEntry` 不携带 `seat / alias / max_rounds`，与 `LineSpec` 不同构，放行者无从接手。
  3. alias token 铸造（`/data/ronin/secrets/<alias>.token`）是带外手工步骤，缺失不报错、线跑起来 inbox/board 静默半残（`bus/tokens.py:76-87`）。
  4. 旁路提交监管面看不见：roster 闭包 = 可见性闭包，roster 之外无任何「有人想开线」的信号面。

## 交付

### A. 提交面（goal 面工具扩容）
1. `goal_enroll(folder_id, alias, seat_hint?, max_rounds?, note?)` 语义升级为**入册申请**：
   - 既有 5 道闸全部保留（goal line 结构 / acceptance argv / golden-order / spec-lint / 真机 liveness 空跑）；
   - **新增闸 6：alias token 存在性**——`/data/ronin/secrets/<alias>.token` 不存在即 REJECT（新拒绝码 `GOAL_ENROLL_ALIAS_TOKEN_MISSING`），把静默半残前置为提交期可见失败；
   - **新增闸 7：alias 唯一性**——与现 roster 及 pending queue 内 alias 冲突即 REJECT；
   - `seat_hint` 仅为建议：座位平衡是监督面裁量，放行时终定；
   - 通过后落 **pending queue**（`enroll-queue.jsonl`，entry 补齐 `alias/seat_hint/max_rounds/briefing_version/submitted_by/submitted_at`，状态 `pending`），幂等语义沿用（重复提交返回 already_pending / already_enrolled——后者对照真名册判定）。
2. `goal_list()`：统一视图 = 真名册（只读 `ronin-lines.json`）∪ pending queue，每条带 `origin∈{roster,pending}` 与状态。
3. `goal_status(folder_id)`：单条详情（roster/pending entry + 拒绝史）。
4. `goal_withdraw(folder_id)`：撤回 pending 申请（仅 pending 态可撤，落 `withdrawn` 留痕不删行——失败留痕原则）。
5. **不提供任何放行/点火工具**：`enabled` 翻转、seat 终定、roster 写入均不在 MCP 面——放行仍走 roster PR（监督面）。

### B. 可见性（申请必达监管面）
1. state read-model（:7494）新增 `GET /v1/enrollments`：`{"schema_version": <str>, "enrollments": [...]}`，每请求重读 enroll-queue（与 `_read_roster` 同法）；坏行降级不 5xx 全链。
2. `supervise/events.py` 词表新增 **E8 `enrollment_pending`**：observer 消费 `/v1/enrollments` 发射（与 E5–E7 同源同纪律），dedup key = `enroll:{folder_id}`，pending 超龄（默认 24h 未裁）追加提醒 attempt（`{key}:g{n}` 语义沿用）；unknown 事件名拒绝的负例测试保留。
3. `goal_enroll` 通过时向 agent-bus `board:work-notes` 发一条 `question` note（既有裁决协议：需人拍板的问题发 question，裁决只认 `work.decision.v1`）——申请即挂板，监督面/用户按既有裁决信道拍板。bus 降级（token 缺失等）不阻断入队，但 entry 记 `board_notify: failed`，E8 事件兜底可见性。

### C. 放行侧对接（机械部分可代跑，裁决不可代拍）
1. `docs/operating.md`「闸零」扩写完整流水线：提交（:5611）→ 挂板/E8 → 监督面裁决 → roster PR（含 `tests/test_ronin_lines_config.py` ENROLLED 集合同步）→ release → restart fleet-graphd → queue entry 置 `admitted`（记 decision 指针）。
2. queue 状态机：`pending → admitted | rejected | withdrawn`，终态带 `decided_by/decision_ref`；roster 里已存在但 queue 无记录的存量线不回填。
3. 对账：`goal_list` 对「queue 已 admitted 但 roster 无此线」「roster 有而 queue 标 pending」两类漂移显式标 `drift` 字段（只报不修——对账分歧按宪法立案）。

## 可复现验收

```dd-acceptance
make verify
uv run python scripts/e2_goal_enroll_acceptance.py
```

（演练脚本扩展：提交→queue 落行→`/v1/enrollments` 可见→withdraw 留痕；alias token 缺失 REJECT 负例。）

## 量化判据（部署后真机）

1. `make verify` 通过（含新拒绝码、E8 发射/去重/超龄单测、queue 状态机测试、`/v1/enrollments` schema 测试）。
2. 真机演练：对一个合规测试 folder 提交 → `curl -sf http://127.0.0.1:7494/v1/enrollments` 可见该申请且带 `schema_version`。
3. alias token 缺失的提交被拒且拒绝码稳定；withdraw 后 entry 留痕为 `withdrawn`。
4. E8 事件在合成快照单测中正确发射与去重；unknown 事件名仍被拒绝。

## 铁律

- 代码与评审全委 dev-dispatch；一切改动走 PR，不直改 main；生产 checkout ff-only。
- 本单不获得 roster 写权、不翻转 `enabled`、不发布 `work.decision.v1`、不重启任何 unit——放行权力边界不动。
- scheduler（fleet-graphd）零改动：本单不引入热重载/overlay；「审批通过免 release 生效」属后续阶梯，需用户另行拍板。

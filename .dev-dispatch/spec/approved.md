# fleet-graph decision-mcp 账本持久化 outcome 字段 spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：A 类可观测缺口（账本不落 outcome，投递消费结论不可查询）。监督面 2026-09-02 14:3x 立案案 5(P2)。

## 1. 现象与真因（读源码坐实）

- 现象：`/data/fleet-graph/decision-mcp/deliveries.jsonl` 里真实投递 4 行（wf-6475fd×3、wf-a87b04×1），这些行的 outcome 未持久化（监督面 13:0x 报「ledger 不持久化 outcome 字段」）。
- 真因（坐实）：`decision_mcp.py::DeliveryLedger.record()` 手写 entry，字段为 `{at,line,decision,status,code,retryable,action_key,generation,question_note_id,card_entity_id}`，**漏掉 `DeliveryResult.as_dict()` 里 `delivered` 才带的 `outcome`（值 `"consumed"`）以及 `target.resume_status`**。即返回给调用方的 payload 有 `outcome:"consumed"`，落账本时丢了，消费结论不可从账本查询。
- 另（监督面所询，非本单实施内容）：账本保留期/分区——见文末建议，**清理动作不在本线范围，勿代做**。

## 2. 修复方向（契约）

1. `DeliveryLedger.record()` 的 entry 与 `DeliveryResult.as_dict()` **字段对齐**：至少持久化 `outcome`（`delivered` → `"consumed"`；`refused` → 无 `outcome` 或不写），并把 `target.resume_status`（若有）一并落账，使「投递 → 消费」结论在账本上可查询、可对账。
2. 只读纪律不变：账本/metrics 文件仍由 `state_dir` 派生；测试/验收仍走临时 state-dir（闸 50，不落 `DEFAULT_STATE_DIR`）。
3. 判据（能红）：一条 `delivered` 投递落账后，账本该行必须含 `outcome` 字段（= `"consumed"`）；一条 `refused` 投递该行不得凭空出现 `outcome:"consumed"`（不得把拒绝标成消费）。

## 3. 判据（两向能红）

1. **阳性（outcome 落账）**：deliver 一次 `delivered` → 账本行含 `outcome:"consumed"`（且 `_write_metrics` 仍正确计 delivered）。
2. **阴性（不虚标）**：deliver 一次 `refused`（如 `LINE_NOT_PARKED`）→ 账本行**不得**含 `outcome:"consumed"`；变异：把 `outcome` 恒写成 `"consumed"` → 必红。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_decision_mcp.py
make verify
```

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree add 到 `/data/worktrees`；生产主 checkout 只读、仅 `git pull --ff-only`。

## 附：账本保留期/分区建议（回复监督面所询，非实施）

真实投递量极小（今日 4 行真实 + 50 行历史合成）；建议：① 不引入分区，维持 append-only 单文件；② 由监督面**就地清空**历史 50 行合成数据（清理动作归监督面）；③ 后续若要防再污染，靠闸 50 的「默认 `_NullLedger()` + 测试临时 state-dir」已足够，不需要保留期机制。若要长期，可加「按 `at` 保留最近 90 天」的 append-only 裁剪脚本，但**不在本线范围实施**。
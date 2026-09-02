# fleet-graph `/v1/lines` 驻停声明按 run 一致性门控 spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：A 类可观测误报（被取代 run 的驻停声明被当「当前状态」发布且不可分辨，直接误导监督面处置）

## 1. 现象与真因（同刻三侧实测）

- `wf-216dc3`：`/v1/lines` 报 `terminal=blocked, parked=true`，`wake_facts.run_id=87e7e509-60f0-4b8f-aab7-df69b8231a41`；而活 `heartbeat.json.run_id=6f42ec99-606b-402b-9b10-63c4c4c03390`（round=8、phase=coordinator、updated_at 03:21:39Z 新鲜）；`.scheduler/wf-216dc3.json = {"parked_at":null,"parked_run_id":null}`。
- `wf-6475fd`：`/v1/lines` 报 `terminal=blocked`（`wake_facts.run_id=a4ef0b69…`、at 13h 前的旧 reason），而活 `heartbeat.json.run_id=1a2e2a2e…`（round=31、worker、03:32:43Z 新鲜）。
- 真因：`fleet_state.py` `lines()` 读 folder 级 `terminal.json` 取 `terminal/waiting_on/wake_facts`，**不**与「活 run」`heartbeat.json.run_id` 做一致性门控——已被取代 run 的驻停声明跨 run 存活，被平铺成当前状态且无从分辨（端点字段 `folder_id/generation/round/phase/heartbeat_age_s/terminal/parked/wake_facts/release_id` 无活 run_id）。

## 2. SSoT 结论（先答，不两边各推一套）

`parked/terminal` 的「当前性」SSoT = **run_id 一致性**：`terminal.json.run_id == heartbeat.json.run_id` 时该驻停声明才属活 run，否则为过期声明；与 `.scheduler/<wf>.json` 的 `parked_at/parked_run_id`（run 级、换代即清）等价。本项目走**读模型门控**：`/v1/lines` 已同时读 `heartbeat.json` 与 `terminal.json`，直接比对两处 run_id 即可，无需改调度侧。

## 3. 修复方向（选路自决，端点至少可判定）

- (a) 声明 run ≠ 活 run 时**清掉**该 run 的 `terminal/parked/wake_facts`（驻停是 run 级事实，不该跨 run 存活）；或
- (b) 保留历史声明，**同时暴露活 run 的 `run_id`**（或 `wake_facts_stale: true`），使消费者机械可判。
- 任一路都必须让读端能回答「这份声明是否属于当前活 run」。

## 4. 真机判据（双向必须能红）

1. **阴性**：线在 run A 声明驻停（`terminal.json: blocked + waiting_on=decision`）、run A 结束、run B（新 run_id）起来推进若干轮后，`/v1/lines` **不得**仍把该行呈现为「当前驻停中且不可分辨」（(a) 则字段已清，(b) 则 `wake_facts_stale=true`）。
2. **阳性**：线当下真的驻停等裁决（`terminal.json.run_id == heartbeat.run_id`）→ 该行**照常** `parked=true` 且 `wake_facts` 属活 run。只做①会拿漏报换误报，本卷不收。

## 5. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_fleet_state_readmodel.py
make verify
```

## 6. 铁律

- 代码/review 一律交 dev-dispatch；git worktree；生产主 checkout 只读、仅 ff-only。
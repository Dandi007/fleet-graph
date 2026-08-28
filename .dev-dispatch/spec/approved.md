# fleet-graph lifecycle producer labels：role·goal·launch·round / order·attempt·dispatched_by

## 背景（2026-08-29 生产新鲜事实，coordinator 只读取证）

- collector DB `/data/agent-runtime/metrics/metrics.sqlite3`（runs 表 25759 行）实测：**25726 行无 role label**；带 order/development 标识 304 行；launch/goal 各 21 行（历史 e2e 残留）；**round=0、attempt=1、dispatched_by=0**；近 24h 新增 run 全部无 role。
- 根因在 fleet-graph 生产派发面：
  - `src/fleet_graph/graphs/runner.py:117` `AgentRunCoordinator` 未传 `extra_labels` → coordinator run labels 仅 `{work_folder, dispatcher}`；
  - 同文件 `AgentSessionWorker` seat labels 仅 `{work_folder, dispatcher}`；
  - `src/fleet_graph/graphs/dd_actors.py:229` stage run labels 仅 `{development, dispatcher, stage}`。
- 上游观测契约（wf-386b2f spec 验收标准）：loop/line spawn 的 supervisor run 含 `role/goal/launch/round`；dd spawn 的 worker run 含 `role/goal/order/attempt/dispatched_by`。当前生产面完全缺失，导致 role=unknown 残差 3.88B input tokens、`cost_obs:idle_noop_rate`/`rework_tax` 的 role 切片无生产者、逐 order 对账无维度。

## 目标（DoD）

1. **coordinator per-turn labels**：`AgentRunCoordinator` 支持按 round 注 label；每轮 coordinator run labels 增 `role=supervisor`、`goal=<folder_id>`、`launch=<launch_id>`、`round=<round_no>`。
   - `launch_id` 在 line generation 启动时铸造：稳定、可读（形如 `launch-<folder>-g<generation>-<start-ts>`）、**进程重启 = 新 launch**（对齐上游 spec D4）；kill-resume 场景 re-adopt 的 run 保持首次派发时 label（run_id 幂等 upsert，不得因 resume 改写历史 label）。
2. **seat worker labels**：`AgentSessionWorker` 的 seat labels 增 `role=worker`、`goal=<folder_id>`（launch 可得则一并）。
3. **dd stage labels**：`graphs/dd_actors.py` stage run labels 增 `role=dd-worker`、`order=<development_id>`（**保留**既有 `development` 键不删，INV-2）、`attempt=<该 stage 的 attempt 序数>`、`dispatched_by=<派单主体>`。
   - `dispatched_by` 取 DevelopmentConfig 既有 provenance（board card 创建者/派单 line）；若无现成字段则在 development 配置链路新增并在 create 时透传；值必须是有界主体名（line folder 或 human 主体），**禁止**放 run_id/uuid。
4. **边界**：这些 label 只进 agent-run run labels（collector SQLite 钻取面）；**不得**进入 metricsd Prometheus exposition 的 bounded label 投影（INV-5 无界 id 不进 Prometheus）；metricsd 侧零改动。
5. **测试**：三处 label 注入单测（coordinator per-round label 正确性、seat worker、dd stage 含 attempt 序数与 dispatched_by）；kill-resume 下 re-adopt 不改写 label 的回归；`make verify` 全绿；既有冻结验收不降。

## 边界

- 生产部署与 line 重启不在本单内（合入后由编排层按 goal.md §66-71 受控 release 指针切换）。
- 不改 agent-runtime、不改 recording rules。

## Acceptance

- `make verify` 通过（fleet-graph 冻结验收：ruff + pytest + supervisor conformance）。

```dd-acceptance
make verify
```

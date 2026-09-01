# fleet-graph 心跳 phase 内周期性 tick（PumpHeartbeatStale 长回合恒误报，A 类）spec

- 目标仓：`/data/code/self/fleet-graph`（本仓，https://github.com/Dandi007/fleet-graph）
- 分类：A 类可观测误报（`PumpHeartbeatStale` 对长回合恒误报）
- 立案：goal.md 2026-09-02「立案二——PumpHeartbeatStale 对长回合恒误报」（wf-6475fd observability-onboarding 常驻线）

## 1. 现象与真因（机械可核）

- 现象：04:1x `wf-e6560a`、04:2x `wf-c106b9` 相继触发 `PumpHeartbeatStale`，两条都健康（真机反证用 TasksCurrent + 8s CPU 增量判活，未采信告警）。
- 根因：`heartbeat.json` 只在 **phase 边界**写一次（`graphs/goal_line.py` 仅在 `check_bounds`/`coordinator_turn`/`worker_turn`/`acceptance_step` 四处调 `deps.artifacts.heartbeat(...)`），phase 内从不更新。于是
  `mtime == phase_started_at == updated_at`（逐秒相等），长 worker turn（常态，可 ≥45min）期间 mtime 冻结，
  fleet-sentinel `fleet_pump_heartbeat_age_seconds = now - mtime(heartbeat.json)` 单调涨，越过 1800s 即误报。
- 本轮 fresh 复现（合并前实测）：
  `wf-6475fd` worker 相 `phase_started_at=2026-09-01T20:44:06Z`、`updated_at=20:44:06Z`、mtime=04:44:06（三者逐秒相等）。

## 2. 修复方向（选 a：phase 内周期性更新；b 留作监控侧后续加固）

1. **加 `last_tick_at`**：heartbeat payload 增 `last_tick_at` 字段并归入 `HEARTBEAT_FIELDS`（`src/fleet_graph/state/run_artifacts.py`）。
2. **phase 内周期 tick**：`RunArtifacts` 提供周期心跳——一旦 phase 已定（首次 phase 边界写过后），每 `HEARTBEAT_INTERVAL_SECONDS`（5s）以 `force` 再写一次，使 `updated_at` 与文件 mtime 在 phase 内持续推进；`phase_started_at` 保持 phase 进入时刻**不变**（tick 不 reset 它）。
3. **线程安全**：ticker 写入与主图线程的 phase 边界写入共用一个写守卫锁，不竞争、不撕裂。
4. **fail-soft**：tick 写失败沿用现有 `OSError → warning` 降级，绝不 crash、绝不阻塞主循环。
5. **起停**：line 进程启动后 start，terminal/shutdown 时 stop（daemon 线程 + stop event）。
6. **阴性语义**：真停（进程级死亡/挂死致 ticker 死亡）→ mtime 停 → `PumpHeartbeatStale` 仍报；长回合（进程活、`deps.worker.turn` 阻塞等待中）→ ticker 持续刷 mtime → 不误报。更强的「活性」判别（cgroup CPU 增量）判据必须是增量、不得用 MainPID 累计 CPU——已在 goal.md 留意，留作后续监控侧选项，本轮不采用（本轮修在线层）。

## 3. 真机判据（量化）

1. 长回合（带近活动）不误报：长 worker turn 期间 heartbeat.json 的 mtime 持续前进（age 保持 < 数倍 `HEARTBEAT_INTERVAL_SECONDS`），`phase_started_at` 不变。
2. 短窗口真停（无活动且超合同/进程死）仍报：ticker 死 → mtime 停 → `fleet_pump_heartbeat_age_seconds` 递增过 1800 → `PumpHeartbeatStale` 开火。
3. 合并后自证：`curl :9090/api/v1/query?query=fleet_pump_heartbeat_age_seconds` 对照本线长回合期间 age 不涨；人为停一线 ticker 源 → 阈值后告警开火。

## 4. 验收（dd-acceptance，代码级）

```dd-acceptance
uv run pytest -q tests/test_run_artifacts.py
make verify
```

## 5. 铁律

- 代码编写与 review 一律交 dev-dispatch（本 spec 只定义契约与判据）。
- git 一律 `git worktree add` 到 `/data/worktrees` 隔离 worktree；生产主 checkout（`/data/code/self/fleet-graph`）仅 `git pull --ff-only`。
- 测试需覆盖：`last_tick_at` 存在且随 tick 前进；`phase_started_at`/`updated_at` 在 phase 内的正确分置（tick 只推 updated_at/last_tick_at）；phase 未定前 ticker 不写；写失败 fail-soft；`HEARTBEAT_FIELDS` 精确集合含 `last_tick_at`。
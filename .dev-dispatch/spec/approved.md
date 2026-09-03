# fleet-graph 心跳 phase 内周期性 tick（PumpHeartbeatStale 长回合恒误报，A 类）spec

- 目标仓：`/data/code/self/fleet-graph`（本仓，https://github.com/Dandi007/fleet-graph）
- 分类：A 类可观测误报（`PumpHeartbeatStale` 对长回合恒误报）
- 立案：goal.md 2026-09-02「立案二——PumpHeartbeatStale 对长回合恒误报」（wf-6475fd observability-onboarding 常驻线）

## 1. 现象与真因

- 现象：04:1x `wf-e6560a`、04:2x `wf-c106b9` 相继触发 `PumpHeartbeatStale`，两条都健康（真机反证用 TasksCurrent + 8s CPU 增量判活）。
- 根因：`heartbeat.json` 只在 phase 边界写一次（`goal_line.py` check_bounds/coordinator_turn/worker_turn/acceptance_step 四处），phase 内从不更新，`mtime == phase_started_at == updated_at` 逐秒相等，长 worker turn 期间 mtime 冻结，`fleet_pump_heartbeat_age_seconds = now - mtime` 超 1800s 即误报。

## 2. 修复方向（选 a；b 留作后续监控侧加固）

1. heartbeat payload 增 `last_tick_at` 并归入 `HEARTBEAT_FIELDS`。
2. `RunArtifacts` phase 内周期 tick：phase 已定后每 5s 以 force 写一次，推 `updated_at`+mtime，`phase_started_at` 不变。
3. tick 与主图线程共用写守卫锁；写失败 fail-soft（OSError→warning 降级）。
4. 起停：line 启动 start、terminal/shutdown stop（daemon 线程 + stop event）。
5. 阴性：真停（进程死→ticker 死）仍报；长回合（进程活、worker.turn 阻塞中）刷 mtime 不误报。cgroup CPU 增量更强活性信号判据须为增量，留后续。

## 3. 真机判据

1. 长回合带近活动不误报（mtime 持续前进、phase_started_at 不变）。
2. 短窗口真停仍报（ticker 死→mtime 停→超 1800 开火）。

## 4. 验收

```dd-acceptance
uv run pytest -q tests/test_run_artifacts.py
make verify
```

## 5. 铁律

代码编写与 review 全交 dev-dispatch；git 一律 worktree add 到 /data/worktrees；生产主 checkout 仅 git pull --ff-only。
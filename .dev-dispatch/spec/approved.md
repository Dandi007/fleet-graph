# fleet-graph `/v1/lines` 暴露每代 release_id（A 类可观测缺口）spec

- 目标仓：`/data/code/self/fleet-graph`（本仓，https://github.com/Dandi007/fleet-graph）
- 分类：A 类可观测缺口（read-model 不暴露「这一代在跑哪个 release」）
- 立案：goal.md 2026-09-02 03:5x CST 立案本体（wf-6475fd observability-onboarding 常驻线）

## 1. 现象与真因

- 现象：`wf-a6cfea` 报 `worker turn report malformed`，而这一族恰是 PR #217（worker 报告有界重问）已修掉的，险些把干净单判成假缺陷。
- 真因：release 是内容寻址快照 + `current` 符号链接；line unit 的 `ExecStart` 走
  `/data/apps/fleet-graph/current/.venv/bin/fleet-graph`，进程 **exec 时解析一次符号链接，此后不再切**。修复只对下一代（generation turnover）生效，已在跑的代继续执行旧快照。
- 缺口：`GET /v1/lines`（`src/fleet_graph/state/fleet_state.py` 的 `FleetStateView.lines()`）每一行只有 `folder_id/generation/round/phase/heartbeat_age_s/terminal/parked/wake_facts`，不暴露该代实际在跑哪个 release。当前只能靠 unit `ActiveEnterTimestamp` vs `current` mtime 手工反推——易错且正是误判来源。

## 2. 修复方向（观测契约；实现细节交 dev-dispatch）

1. **捕获一次、冻结在启动瞬间**：line 启动时读一次 `realpath(/data/apps/fleet-graph/current)`，取 basename 作为该代的 `release_id`。缺失/不可读/不可解析 → fail-soft 置 null，绝不 crash line。
2. **落进该代 state**：把 `release_id` 写入 per-generation 的 state（`heartbeat.json`，`src/fleet_graph/state/run_artifacts.py` 的 `RunArtifacts` 在 line 进程构建时冻结并随每次 heartbeat 写出）。
3. **读模型只消费持久化值**：`FleetStateView.lines()` 从 `heartbeat.json` 读 `release_id` 并在每行暴露；**read 路径绝不重新 `realpath(current)`**——这是阴性判据成立的根因。
4. **换代自然更新**：换代 = 新 line 进程 = 重新 `realpath` 一次 = 新 `release_id` 覆盖 heartbeat。
5. **阴性保证**：`current` 在该代运行期间被重新指向时，该代已持久化的 `release_id` **不得**改变（进程没有切，字段必须反映进程实际在跑的那个 release，而非符号链接当下指向）。

字段名固定为 `release_id`，字符串型（可为 null），每行必带。

## 3. 真机判据（量化）

1. 起一条线后，`GET /v1/lines` 该行的 `release_id` 等于其 unit 启动时刻 `current` 的 realpath basename。
2. 换代（generation turnover）后该字段跟随更新为新 release。
3. 阴性：`current` 在该代运行期间被重新指向时，该代 `release_id` 不得改变。

真机自证命令（对照）：
- `realpath /data/apps/fleet-graph/current`（当前实际 = `/data/apps/fleet-graph/releases/20260902-030934-05dec3709ba0`，故 `release_id = 20260902-030934-05dec3709ba0`）
- `curl -s http://127.0.0.1:7494/v1/lines`

## 4. 验收（dd-acceptance，代码级）

```dd-acceptance
uv run pytest -q tests/test_fleet_state_readmodel.py tests/test_run_artifacts.py
make verify
```

## 5. 铁律

- 代码编写与 review 一律交 dev-dispatch（本 spec 只定义契约与判据）。
- git 一律 `git worktree add` 到 `/data/worktrees` 隔离 worktree；生产主 checkout（`/data/code/self/fleet-graph`）仅 `git pull --ff-only`。
- 测试需覆盖：release_id 随启动冻结写入；阴性（re-point 不改已写值）；fail-soft 置 null；read-model 字段清单含 `release_id`。
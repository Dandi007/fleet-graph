# E5 收割观察器接线 — SupervisorLaunchSpec 补传 harvest 写权旗标

## 目标

E5「approved_unharvested」触发的 supervisor run 目前拿不到真实 harvest 写权配置，
恒走 `HarvestAllowlist.default()`=deny-all 且 `harvest_default_branch='main'`。本
development 只在 observer 侧补接 `--harvest-allowlist` / `--harvest-default-branch`
（及 `--harvest-deploy` / `--repo` 旗标透传），使 `run_supervisor` 能对白名单内
沙箱仓 `authorize(repo=/data/code/self/fleet-harvest-sandbox, branch='refs/heads/master')`
得 `granted=true`。

## 权威事实（真机取证，非自述）

- 收割反应器已合入 main（M3 dev-fg-e528d049dd9d）：`supervise/harvest.py`、
  `supervise/harvest_allowlist.py`、`graphs/supervisor.py` 的 E5 分派、`cli.py`
  `supervisor run` 的 `--harvest-allowlist/--harvest-default-branch/--harvest-deploy/
  --harvest-verify/--harvest-verify-real` 旗标（1684-1713 行）均已就绪。
- 唯一缺口在 observer：`src/fleet_graph/scheduler/supervisor_events.py`
  `SupervisorLaunchSpec.argv()` 只生成 `--event-json/--run-root/--state-root`，不传
  `--harvest-allowlist/--harvest-default-branch/--harvest-deploy/--repo`；
  `ObserverConfig`/`_spec_for` 亦无对应字段。
- 真机回执后果：`/data/fleet-graph/supervisor/reports/e5-*` 全
  `allowlist_auth.granted=false`（理由「默认 deny-all」）且 `default_branch='main'`；
  沙箱仓 `/data/code/self/fleet-harvest-sandbox` 默认分支为 `master`
  （`.git/HEAD=refs/heads/master`）。
- 监督面已订正 `/data/fleet-graph/supervisor/harvest-allowlist.json`：两 entry
  `allowed_branches=["refs/heads/harvest/","refs/heads/master"]`。该文件按铁律**禁本
  development 改动**；实现只读它，不生成、不改写、不触碰。

## 契约：observer 侧 argv 补传旗标（唯一代码改动面）

`SupervisorLaunchSpec` 新增可选字段（全部缺省 None/空，未配置即**不发射**，保持
deny-all 默认拒绝语义零放宽）：

- `harvest_allowlist_path: str | None = None`
- `harvest_default_branch: str | None = None`
- `harvest_deploy: tuple[str, ...] = ()`
- `repo: str | None = None`

`argv()` 在 `--state-root` 之后按需追加（词法顺序稳定，测试按 `in argv` 断言）：

- `--harvest-allowlist <path>`（非 None 时）
- `--harvest-default-branch <branch>`（非 None 时）
- `--harvest-deploy <word>`（deploy 每个词一个旗标，对应 cli `action="append"`）
- `--repo <path>`（非 None 时）

`ObserverConfig` 增加同名字段（`harvest_deploy` 用 `field(default_factory=list)`），
`_spec_for` 把它们透传给 `SupervisorLaunchSpec`。未配置任何 harvest 字段的 observer
行为与现状**逐字节一致**。

## 生产接线（调度配置面，纯配置透传，无业务逻辑）

`SchedulerConfig`（`src/fleet_graph/scheduler/daemon.py`）新增 `harvest_allowlist_path` /
`harvest_default_branch` / `harvest_deploy` / `repo` 四个可选字段并 `from_json` 读入；
`cli.py _scheduler_run` 把这些值传入 `ObserverConfig`。生产配置
`config/ronin-lines.json` 顶层增加：

```json
"harvest_allowlist_path": "/data/fleet-graph/supervisor/harvest-allowlist.json",
"harvest_default_branch": "master",
"harvest_deploy": ["bash", "scripts/deploy.sh"]
```

未配置这些键时行为与现状一致：不发射 harvest 旗标 → supervisor run 默认 deny-all +
`main`（默认拒绝零放宽）。

## 最小实现边界

只改 `src/fleet_graph/scheduler/supervisor_events.py`、`src/fleet_graph/scheduler/daemon.py`、
`src/fleet_graph/cli.py`、`config/ronin-lines.json` 与测试。不改 `supervise/harvest*.py`、
`graphs/supervisor.py`、`cli.py supervisor run` 的参数解析、E1–E7 词表；不触碰
`/data/fleet-graph/supervisor/harvest-allowlist.json` 与沙箱仓；不改判据。

## 测试（钉死行为，不空断言）

1. `tests/test_supervisor_events.py` 新增：构造带 harvest 字段的 `ObserverConfig` +
   `SupervisorObserver`，`spec.argv()` 中 `--harvest-allowlist`/`--harvest-default-branch`
   （及 `--harvest-deploy`/`--repo`）逐项 `in argv`；未配置这些字段时上述旗标**不出现**。
2. 阴性（默认拒绝零放宽）：`SupervisorLaunchSpec` 不带 harvest 字段时 `argv()` 无任何
   `--harvest-*`；`load_harvest_allowlist` 不传路径仍 deny-all——既有
   `tests/test_harvest_allowlist.py` 拒绝路径零回归。
3. `make verify` 全绿（含既有 allowlist 拒绝路径测试与 E1–E7 词表负例零回归）。

## Executable Acceptance

```dd-acceptance
make verify
```

## 铁律与交付约束

- 一切代码编写与 review 由 dev-dispatch 完成，独立 `/data/worktrees/` worktree；
  生产主 checkout 不 checkout/switch/reset/切分支。
- 不触 `/data/fleet-graph/supervisor/harvest-allowlist.json`；不代造收割单。
- 合入 main 后由监督面照常收割 + release + 重启 fleet-graphd，再在沙箱仓跑真机 E5
  e2e（granted=true + MERGED PR + evidence note id）。
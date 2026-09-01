# M4 E7 goal.md 直写 allowlist argv 透传缺口修复——调度器不发 --e7-allowlist 旗标

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`scheduler/supervisor_events.py`（E7 goal.md 直写 allowlist 的调度透传）。真机触因：E7 直写恒 0 成功（deny-all），e7=203 发射全部 refused。
- 类别：代码接线缺口修复（argv 透传），**不代签、不代造 allowlist 内容文件**（内容文件仍留监督面亲落）。

## 根因（已实读 release 14938bf 源，非推断）

`--e7-allowlist` 旗标与 `--harvest-allowlist` 是**同型**机械缺口，但一个透传了一个没透传：

1. CLI `supervisor run` **已有** `--e7-allowlist` 旗标（`cli.py` L2017：`--e7-allowlist`，help「E7 goal.md 直写目标线白名单 config file. Deny-all when unset」），且已接入 `run_supervisor`（`cli.py` L1010 `e7_allowlist_path=args.e7_allowlist` → `graphs/supervisor.py` L982-983 `if config.e7_allowlist_path: allowlist = load_e7_write_allowlist(...)`）。即**手工 `supervisor run --e7-allowlist <file>` 全程可用**。
2. 但**在线调度器不发射该旗标**：`scheduler/supervisor_events.py::SupervisorLaunchSpec.argv()` 只按需发射 `--harvest-allowlist / --harvest-default-branch / --harvest-deploy / --repo`（L196-204），**无 `--e7-allowlist` 分支**；`SupervisorLaunchSpec`/`ObserverConfig` 数据类也只有 `harvest_allowlist_path / harvest_default_branch / harvest_deploy / repo` 字段，**无 `e7_allowlist_path` 字段**。故线上 E7 子进程永远以 deny-all（`E7WriteAllowlist.default()`）运行，即使监督面将来亲落 allowlist 文件也无法透传进去。

对比：`--harvest-allowlist` 已透传（harvest-allowlist.json 生效、M3 第五轮沙箱 e2e 成功收割即佐证）；`--wiki` 与 `--e7-allowlist` 同属「漏透传」缺口，`--wiki` 已在 dev-fg-4d06db4493ea（m4-wiki-trigger-wiring-fix-spec.md 交付 B）覆盖，本单补 `--e7-allowlist`。

## 交付 A：argv 透传补 `--e7-allowlist`（`scheduler/supervisor_events.py`）

1. `SupervisorLaunchSpec` 与 `ObserverConfig` 各增 `e7_allowlist_path: str | None = None` 字段（与既有 `harvest_allowlist_path` 同位、同缺省语义）。
2. `SupervisorLaunchSpec.argv()` 在 `--state-root` 之后、与 `--harvest-allowlist` 并列处，`if self.e7_allowlist_path is not None: argv += ["--e7-allowlist", self.e7_allowlist_path]`（词法顺序稳定，测试按 `in argv` 断言）；缺省 None → 不发射（字节不变，零回归）。
3. `daemon.py` 配置面（`SchedulerConfig`/observer 装配）补 `e7_allowlist_path` 透传字段（与 `harvest_allowlist_path` L275 同款），使监督面后续可直接经配置/文件供给该路径。

## 交付 B：阴性/正向测试（`tests/test_supervisor_events*.py` / `tests/test_scheduler*.py`，纯词法，禁触真网）

1. `e7_allowlist_path=None` → `argv()` 不含 `--e7-allowlist`、仍含既有 `--harvest-allowlist`（若设）——零回归。
2. `e7_allowlist_path="/x/e7.json"` → `argv()` 含 `["--e7-allowlist","/x/e7.json"]`；`--harvest-allowlist` 同存量行为不变。
3. **deny-all 语义不破**：本单不生成/不写任何 e7 allowlist 文件；`E7WriteAllowlist.default()` 仍 deny-all（`make verify` 全绿 + 既有 `test_harvest_allowlist.py`/E7 测试零语义回归）。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 词法证据可复现：`ObserverConfig(e7_allowlist_path=...)` 构造的 `SupervisorObserver` 产出的 `SystemdRun` argv 含 `--e7-allowlist <path>`；缺省不含。
3. 不新建/不改写任何 allowlist 内容文件；deny-all 缺省不变。

## 铁律

- 只改 `src/fleet_graph/scheduler/supervisor_events.py`（+ 如需 `scheduler/daemon.py`、`cli.py` 装配面）+ `tests/`；不触 `harvest_allowlist.py`/`e7_allowlist.py`/`e7_write.py` 判定语义、不改判据、不改 E1–E7 词表、不改 harvest 14 步管线。
- **不代签、不代造 E7 allowlist 内容文件**、不触 `/data/fleet-graph/supervisor/harvest-allowlist.json`；allowlist 内容仍由监督面亲落。
- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach；不 import scheduler/ignition/launcher（Guard A 不动）。
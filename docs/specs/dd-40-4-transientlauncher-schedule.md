# 批次 4 前置：TransientLauncher 搬出 scheduler 包，解 dd/control_plane 依赖

## 背景
decommission 批次 4 要删 `src/fleet_graph/scheduler/`（4427 行，10 文件），前置条件是「保留下来的生产代码不再 import 它」。在 release head 3867f2ba 实测：唯一的生产耦合是 `src/fleet_graph/dd/control_plane.py:626` 的 `from fleet_graph.scheduler.launcher import TransientLauncher`（`DdControlPlane.__init__` 的默认 launcher）。dd-36 的 decommission.md 还把 `src/fleet_graph/supervise/events.py` 列为 scheduler 的引用者，这是**误判**——该文件开头明写「deliberately imports nothing from fleet_graph.scheduler」，实测无 import；本张顺带纠正盘点表。本张只解依赖，**不删 scheduler 任何文件**。

## 要改什么
1. `git mv src/fleet_graph/scheduler/launcher.py src/fleet_graph/launcher.py`：systemd transient 启动器成为中立模块。公开符号与行为一律不变（LaunchSpec / LaunchResult / TransientLauncher / 单元名前缀常量 / dry_run 语义 / argv 组装逐字不变），只改模块内部 import 路径。
2. 全树把 `from fleet_graph.scheduler.launcher import ...` 改成 `from fleet_graph.launcher import ...`：`src/fleet_graph/dd/control_plane.py`、`src/fleet_graph/scheduler/daemon.py`、`src/fleet_graph/cli.py`、（若仍在树上）`src/fleet_graph/scheduler/supervisor_events.py`、`tests/test_launcher.py` 及其它 import 点。**不留 re-export shim**——下线就是下线。
3. 一致性守卫同步：`scripts/check_supervisor_conformance.py` 的 Guard A 把「不许 import 启动器」钉在 `fleet_graph/scheduler/launcher.py` 这个路径/模块名上，`tests/test_supervisor_conformance.py` 里还有两处 sabotage 字面串（约 line 58、78）。若这两个文件仍在树上（另一张 DD 可能已删），把常量与字面串同步改到 `fleet_graph/launcher.py` / `fleet_graph.launcher`，Guard A 的语义必须保持等价；若已被删，跳过并在 PR 说明里写明。
4. `docs/specs/minimal/decommission.md`：批次 4 一行改为「前置解依赖已完成（本 PR）：启动器已中立化，`supervise/events.py` 引用 scheduler 系误判，已纠正；scheduler 剩余引用者只有 cli.py 子命令、scheduler 自身与簇内测试」。`context.md` 同步一句。

## 不改什么
- 不删 `src/fleet_graph/scheduler/` 任何其它文件、不动 cli.py 的 `scheduler run` / `line set-seat` / `line revive` / `line overrides` 子命令、不动 `deploy/systemd/fleet-graphd.service`（批次 4 删除张的活）。
- 不改 TransientLauncher 的行为、argv、单元命名、超时与 dry-run 语义；不趁机重构 dd/control_plane。
- 不动 `src/fleet_graph/minimal/**` 与 `tests/test_minimal_*.py`。

## 怎么验收
`make verify` 全绿；全树无 `from fleet_graph.scheduler.launcher` 出现；launcher / dd control plane / scheduler 三套测试单独跑也绿。

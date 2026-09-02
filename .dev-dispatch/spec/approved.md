# fleet-graph 测试/验收不得在生产 systemd user manager 拉起 transient unit（隔离 systemd 面）spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：A 类生产污染缺陷（测试隔离的 systemd 面）。监督面 2026-09-02 14:3x 立案案 3(P1)。
- 区别于已完成的「state-dir 隔离」（dev-fg-13f71e5fa937，闸 50 修的是账本文件写穿）：那条管文件，这条管 systemd transient unit。

## 1. 现象与真因（读源码坐实）

- 现象：journal 实证（13:18:35–13:19:19）跑全量套件真的起了 `fleet-graph-dd-dev-abc-r2.service`（workspace 在 `/tmp/pytest-of-uther/pytest-179/...`）与 `fleet-graph-line-wf-1-g1.service`（ExecStart `/bin/false`，一条假线）。
- 真因：两个启动入口都在**真实生产用户 manager 上**执行 `systemd-run --user`：
  - `dd/control_plane.py` `DdLaunchSpec.argv()`（`systemd-run --user --collect --unit fleet-graph-dd-dev-<dev>-r<seq> ...`），由 `_launch()` 经 `self.launcher.launch(spec)` 实际执行；`launcher` 已有 `dry_run` 属性（`getattr(self.launcher, "dry_run", False)`），但缺一个**测试/验收默认注入 no-op launcher** 的机制。
  - `scheduler/launcher.py` `LaunchSpec.argv()`（`systemd-run --user ... --unit fleet-graph-line-<folder>-g<gen>`）直接子进程执行，**无 dry_run 缝**。
- 面：与闸 50 同族（账本写穿 vs systemd 拉起），都是「测试/验收跑在真生产面上」。

## 2. 修复方向（契约；方向自决，约束+判据如下）

1. 两个启动入口都必须有**可注入、测试/验收默认关闭真实 `systemd-run`** 的缝：dd 端让 `launcher` 在测试/验收上下文默认 no-op（沿用 `dry_run` 或等价注入）；`scheduler/launcher.py` 增一个等价 no-op/dry-run 路径（注入对象或环境守卫），使测试/验收构建 `LaunchSpec` 并校验 argv 时**绝不把 `systemd-run --user` 递给真 user manager**。
2. 判据（能红）：**跑一遍全量套件前后，`systemctl --user list-units --type=service --state=active` 里 `fleet-graph-dd-*` / `fleet-graph-line-*` 前缀的 transient 单元无新增残留**；若某个用例仍然真拉 transient → 必须红。

## 3. 判据（两向能红）

1. **阳性（隔离生效）**：在隔离测试上下文中构建/`start` 一个 dd development 与一条 line → argv 完整可校验（unit 名、args 冻结不变），但**系统里不出现新的 `fleet-graph-dd-*`/`fleet-graph-line-*` transient 单元**。
2. **阴性（不被隔离打成瞎子）**：production 路径（无 no-op 注入）仍全量走 `systemd-run --user`（真实 launch 语义不变）；变异：把 no-op/dry-run 织进 production 默认 → 必红（真实启动丢失）。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_dd_control_plane.py tests/test_acceptance.py tests/test_launcher.py
make verify
```

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree add 到 `/data/worktrees`；生产主 checkout 只读、仅 `git pull --ff-only`。
# M3 收割反应器 verify 指令硬编码 `make verify` 缺陷（按目标仓解析）—— dd-admissible spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/harvest*`（M3 收割反应器）。真机触因同 dev-fg-eac9840bdd52：首个真实 fleet-sentinel 单 `dev-fg-2e44f0e61516` 收割 `escaped`。
- 类别：收割链缺陷修复（verify 指令解析）。**不重复 dev-fg-eac9840bdd52 已覆盖的 (1) fetch_dd_ref remote_url / (2) cherry-pick identity 两条**。
- 真机原始错误（e5-dev-fg-2e44f0e61516.json 回显）：`run_verify` ok:false `exit_code`=127 `argv`=`["make","verify"]`。目标仓 `/data/code/self/fleet-sentinel` 真机**无 Makefile**（已核），`make` 不存在 / 无 `verify` 目标 → 恒 127。

## 根因（实读源码，非推断）

- `harvest.py` L74 定义 `DEFAULT_VERIFY_ARGV = ["make", "verify"]`；L179（HarvestState）与 L888（config）都以它为默认 `verify_argv`；L509 `exit_code = int(deps.ops.run_verify(worktree, deps.verify_argv))` 直接跑默认值——**全链无任何「按目标仓解析 verify 指令」的机械口**，对任何被收割仓一律硬跑 `make verify`。fleet-sentinel 无 Makefile（有 pyproject.toml + uv.lock + tests/，其全量套件不是 make），故必然 127。
- 对比参照：部署指令已按 allowlist `allowed_deploy` 注入（`deploy` 步 `deploy_command`），而 verify 指令未做等价按仓注入。

## 交付 A：verify argv 按目标仓解析，不再全局硬编码 `make verify`

1. 建立「目标仓 verify 指令解析」机械口：优先目标仓自身声明（根目录 `Makefile` 含 `verify` 目标 → `["make","verify"]`；无 Makefile 但 `pyproject.toml`/`uv.lock` → repo-canonical 全量套件如 `["uv","run","pytest","-q"]`），可被测试注入 fake。
2. 解析不到可执行 verify 指令 → 该步如实 `ok:false` + 机器可读 `detail`（`no resolvable verify command`）→ 收割 `escalated`；**绝不硬跑 `make verify` 制造误导性 127**（现状的 127 既可能是「无 make」也可能是「套件真红」，无法区分）。
3. 不改 allowlist 语义、不改 `allowed_deploy`、不往 `harvest-allowlist.json` 加任何字段（本线不触该文件）。

## 交付 B：阴性测试（必须能红，合成本地仓，禁触真网/生产 checkout）

1. 阴性：合成目标仓**不含 Makefile**（仅任意文件）→ `run_verify` 不返回 exit 127 意义上的假失败；未修复时恒 `argv==["make","verify"] exit 127`，修复后要么跑 repo-canonical 套件成功、要么 `escalated` 且 detail=`no resolvable verify command`。
2. 反向不抖动：合成目标仓含 `Makefile` 且带 `verify` 目标 → 行为不变，仍 `["make","verify"]` 且 exit 0。
3. `make verify` 全绿；`test_harvest*` / H 系列 / dev-fg-eac9840bdd52 即将合入的 fetch/identity 两条零回归（不改同一函数签名冲突之处）。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 阴性能红：无 Makefile 目标仓在未修复时恒 127（现现状）；修复后不再出现「以 make verify 硬跑无 make 仓」的 127。

## 铁律

- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach。
- 只改 `supervise/harvest.py`（+`harvest_ops.py` 若需透传）+ `tests/`；不触 `decide()`、E3、harvest/allowlist 语义与 `allowed_deploy`、判据。
- 不触 `harvest-allowlist.json`、不自造收割单、不重新收割 dev-fg-2e44f0e61516。
# H1 收割反应器 deploy/verify_real 以 canonical 仓为 cwd 执行 + verify_real 先断言 HEAD==已合并 commit

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 10:1x 立案（goal.md 顶部 🔴块 H1「最重，假绿」）。不是判据变更，是缺陷修复。
- 类别：缺陷修复（cwd 缺失导致假绿），不改 deny-all 语义、不改 allowlist 配置文件、不改判据。

## 根因（已实读，非推断）

真机 M3 e2e 第三轮回执 `reports/e5-dev-fg-a324ae06f67c.json`：`deploy ok:false exit_code 127`、
`verify_real ok:true exit 0（假绿）`。代码 `src/fleet_graph/supervise/harvest_ops.py`：

- `DefaultHarvestOps.deploy(command)`（L387-388）与 `DefaultHarvestOps.verify_real(argv)`
  （L390-391）都调 `_run(...)` **不传 cwd**，于是命令在 supervisor 自身 cwd
  （`/data/apps/fleet-graph/current`）下执行：那里没有 `scripts/deploy.sh` → deploy 127；
  但那里正好有 Makefile 且有 `verify` 目标 → `make verify` 误跑 fleet-graph 自己的
  2171 条测试并全绿（76s 墙钟 / 40s CPU 与 fleet-graph 套件吻合，与沙箱平凡 verify 不吻合）。
- 对照组在同一文件：`run_verify(worktree, argv)`（L280-281）**传了 `cwd=worktree`**，
  所以 cherry-pick 后的 verify（exit 0）是真的。只有作用于 canonical 仓的
  `deploy` 与 `verify_real` 漏了 cwd。
- 更深一层：`verify_real` 会对任何仓报绿，比不做验证更坏——它在 pull 失败时仍去验一份
  陈旧的树。它必须先断言 HEAD == 已合并的那个 commit，pull 失败时它就该跑不起来。

## 交付 A：ops 层补 cwd 与 HEAD 断言（机械层，Guard D 豁免）

`src/fleet_graph/supervise/harvest_ops.py`（`HarvestOps` 协议 + `DefaultHarvestOps`）：

1. `deploy` 改签名 `deploy(self, command: list[str], repo: Path) -> int`：实现为
   `_run(list(command), cwd=repo)`。cwd 一律 canonical 仓绝对路径。
2. `verify_real` 改签名 `verify_real(self, argv: list[str], repo: Path, expected_head: str | None) -> int`：
   - 先 `run_git(repo, "rev-parse", "HEAD")` 读当前 HEAD（cwd=repo，机械读口）。
   - 若 `expected_head is None`（pull 未成功、未捕获到已合并 commit）或当前 HEAD !=
     `expected_head`：**不执行 verify 命令**，返回非零合成退出码（沿用 `EXIT_*` 哨兵，
     新增如 `EXIT_HEAD_MISMATCH = 3` 或复用既有非零哨兵），并在返回值/留痕里说明
     「HEAD 与已合并 commit 不一致——拒绝在陈旧树上报绿」。
   - 相等时才 `_run(list(argv), cwd=repo)`。
3. `ff_only_pull` 成功时**额外返回 pull 后的 HEAD**（`run_git(repo, "rev-parse",
   "HEAD")`），字段名 `head`（字符串）；失败时 `head` 为 `None` 并保留既有 `ok:false` +
   `detail`。这是「已合并 commit」的唯一机械来源，绝不猜、不另造。

## 交付 B：编排层透传 canonical 仓库与已合并 commit

`src/fleet_graph/supervise/harvest.py`：

1. `HarvestState` 增 `merged_head: str`。
2. `pull` 节点：`ff_only_pull` 返回结果里的 `head` 存进 `state["merged_head"]`（成功才有值，
   失败为 None）；`steps` 记录照旧。
3. `deploy` 节点：`deps.ops.deploy(command, repo)`，`repo = Path(state["repo_path"])`
   （canonical 仓，非 worktree 路径）。
4. `verify_real` 节点：`deps.ops.verify_real(deps.verify_real_argv, repo, merged_head)`，
   `repo` 同上、`merged_head = state.get("merged_head")`。
5. Guard D 纪律不变：编排层**不新增**任何裸 `git`/`subprocess`/`run_git` 调用；HEAC
   断言读口与 cwd 全部落在 ops 层。conformance（`scripts/check_supervisor_conformance.py`）
   零回归。

## 交付 C：测试（tests/test_harvest.py，fake ops 注入，禁触真网/生产 checkout）

1. **cwd 断言**：fake ops 的 `deploy`/`verify_real` 记录收到的 `repo` 参数；断言
   `repo == 解析出的 canonical repo`（`state["repo_path"]`，即 `config.dd_root.parent/repos/fleet-graph`
   合成仓），且调用 `ff_only_pull` 后 `verify_real` 收到的 `expected_head` 等于 fake 返回的 `head`。
2. **HEAD 断言（正向）**：pull 成功 + fake `head` == 当前 HEAD → `verify_real` 执行、
   exit 0。
3. **HEAD 断言（负向，不可省略）**：`pull_ok=False`（fake 返回 `head=None`）→
   `verify_real` 不执行 verify（ops 收到 `expected_head=None`）→ 该步 exit 非 0/ok:false。
4. 既有用例零回归（`make verify` 全绿；`tests/test_harvest_allowlist.py` 不变）。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py` + `harvest_ops.py` + `tests/test_harvest.py`；
  不触碰 `/data/fleet-graph/supervisor/harvest-allowlist.json`、不改 deny-all、不改判据。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读、
  禁 checkout/switch/reset/切分支。
- 修复落地（合入+release+重启）后由监督面再造沙箱低风险单重跑 e2e；本单只交付修复。
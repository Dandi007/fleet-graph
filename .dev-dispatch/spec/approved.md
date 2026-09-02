# harvest-allowlist 扩容安全判据（P0：卡自动收割）

## 背景

收割写白名单 `supervise/harvest_allowlist.py` 已先行（M3 交付 A），但「谁能把新仓
加进 allowlist、进之前要验什么、误加一个仓的最坏后果」三项安全判据尚未机化为代码
判据与钉测试，成为「支持自动收割扩围」的卡点。本开发把这三问的机械答案落成判定
逻辑 + 钉测试，并明确签发权边界。

## 三问机械答案

### 谁能进（资格）
1. 仅监督面亲签的条目（`harvest-allowlist.json` 的 entry）有资格进入；引擎本身无
   签发权、无扩展权。
2. 目标仓必须位于受治代码根（`/data/code/self/` 物理前缀）之内，且必须是「干净、
   真实的 git worktree、路径即 top-level」。
3. fleet-graph 自身产品源（本线 self）永不在列（自写禁止）；生产仓扩围仅监督面在
   并行对账窗分歧清零后重签；低风险/零生产价值沙箱仓仅在 e2e 实证期可进。

### 进之前要验什么（机械核验，任一失败即 deny）
1. `repo_path` 绝对路径 + 存在 + `git rev-parse --is-inside-work-tree` 为真 +
   `--show-toplevel` 等于 repo_path（防子目录/符号替换）。
2. 仓库无未提交改动（干净 worktree）。
3. 默认分支（HEAD symbolic-ref 解析出的 ref）必须被 `allowed_branches` 的某个
   全-ref 前缀覆盖（`refs/heads/...` 全称 startswith；与既有 h 系列前缀语义一致）。
4. `allowed_branches` 每项是合法 ref 字符集（已有）；`allowed_deploy` 每项是非空
   精确 argv（已有）。
5. 条目（或文件的顶层签发块）必须携带可机读的签发出处与期限；过期或缺出处即 deny。
6. 任一核验失败 → `granted=False` + 机器可读 reasons（留痕），绝不部分放行、绝不
   静默；allowlist 缺失/不可读/解析失败 → 默认 deny-all（已有，保持）。

### 误加一个仓的最坏后果（爆炸半径）
harvest 反应器对该仓获得「把产品 commit 推进默认分支 + 执行白名单部署 argv」两项
写能力。最坏情况 = 在无 diff-review、无人工 gate 的条件下，工程链自动把未受审产物
写进一个未授权仓的生产面并触发其部署，等价于任意外带代码进入该仓生产路径；爆炸
半径 = 该仓的整个生产面，且失败是静默的（无人读 diff、无红灯）。因此：默认
deny-all、签发权唯一归监督面、条目带期限（事故即删回 deny-all）、默认分支锁定、
部署 argv 精确匹配。

## 判据（验收双向）

- 阳性：合资格仓（现有 fleet-sentinel、fleet-harvest-sandbox 条目）在满足全部核验
  后正常授收割权（`granted=True`），与既有 `test_harvest_allowlist.py` 语义一致、
  零回归。
- 阴性：越界仓（受治根外路径）、非 git 路径、脏 worktree、默认分支未覆盖、过期
  条目、缺出处/期限条目 —— 恒拒（`granted=False` + reasons）；缺配置/坏配置 =
  deny-all。未实现（仍放行）必红。

## 交付约束

- 只改 `src/fleet_graph/supervise/harvest_allowlist.py` 与
  `tests/test_harvest_allowlist.py`（必要时新增测试）。
- 本开发不得改动/生成/签发 `/data/fleet-graph/supervisor/harvest-allowlist.json`
  或任何 allowlist 数据文件的通行证内容——发通行证/发证权归监督面，引擎只有
  「验」没有「发」。
- 不改 E5/E6/E7 词表；不部署、不重启、不触碰生产 checkout。

```dd-acceptance
uv sync --frozen
make verify
```
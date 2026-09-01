# M3 收割反应器原生 git 缺陷——cherry-pick 缺 committer identity + dd-ref 误取 origin 而非 remote_url —— dd-admissible spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/harvest*`（M3 收割反应器）。真机触因：首个真实 fleet-sentinel 单 `dev-fg-2e44f0e61516`（repo=`/data/code/self/fleet-sentinel`、remote_url=本地路径）被收割反应器 `outcome=escalated`。
- 类别：收割链缺陷修复（git 机械原语两处原生缺陷），不改 allowlist 语义、不改 decide()、不改判据、不改 harvest 14 步管线骨。
- 真机原始错误（e5-dev-fg-2e44f0e61516.json 回显）：
  1. `fetch_dd_ref` ok:false → `fatal: couldn't find remote ref refs/heads/dd/dev-fg-2e44f0e61516`；
  2. `worktree_cherry_pick` ok:false → `Committer identity unknown ... fatal: unable to auto-detect email address (got 'uther@e300-nuc.(none)')`；
  3. `run_verify` ok:false exit_code=127（cascade：cherry-pick 已败致 worktree 半残 + 默认 `make verify` 非本仓指令）；
  4. `pr_squash_merge` ok:false（harvest 分支被残留 worktree 占用，删分支失败）。
  本 spec 修 (1)(2) 两条确定性 git 缺陷；(3) 默认 verify 指令与 (4) 分支清理另案（见「前置说明/另案」）。

## 根因（已实读源码 `src/fleet_graph/supervise/harvest_ops.py`，非推断）

1. `worktree_cherry_pick`（L331 直采 cherry-pick、L371 `-X theirs` 重试、`build_harvest_tip` L416 同款）调 `run_git(worktree_root, "cherry-pick", head_commit)` **不传 `env=_commit_env()`**——cherry-pick 本身要落地一个新 commit，在无全局 git identity 的机器上必然 `Committer identity unknown`。对比：洗树重提交 `_strip_dd_subtrees` L159 的 `commit` 已传 `_commit_env()`（固定 `GIT_AUTHOR/COMMITTER_NAME/EMAIL`），唯独 cherry-pick 漏了。
2. `fetch_dd_ref`（L267-272）固定 `run_git(repo, "fetch", "origin", ref)`。但 dd 引擎 merger 是把 dd ref 推到 record 的 `remote_url`：URL remote 时推到 GitHub（`origin` 恰好同源可用）；**本地路径 remote_url 时推到本地仓**（`refs/heads/dd/<id>` 已在本地 `refs` 里），而 `origin` 指向 GitHub——两者不同源 → `couldn't find remote ref`。经真机核实：`git -C /data/code/self/fleet-sentinel show-ref` 确有 `refs/heads/dd/dev-fg-2e44f0e61516`，但 GitHub 侧无。

## 交付 A：所有落地 commit 的 git 子进程统一带 committer identity

1. `harvest_ops.py` 内 `worktree_cherry_pick` 的直接 cherry-pick（L331）与 `-X theirs` 重试（L371）、`build_harvest_tip` 的 cherry-pick（L416）一律传 `env=_commit_env()`（复用现有 `_commit_env()` 帮助器：GIT_AUTHOR_NAME/EMAIL + GIT_COMMITTER_NAME/EMAIL）。洗树 `commit` 已在用，不改。
2. 不引入新身份机制、不写全局 git config（机器级副作用零）。

## 交付 B：fetch_dd_ref 从 record.remote_url 取 dd ref，不硬编码 origin

1. `fetch_dd_ref` 签名增加 `remote_url` 透传（编排层 `harvest.py` 已读 record 的 `remote_url`，从 `resolve_canonical_repo` 同一处透传即可，不引入第二解析）。
2. 取 ref 目标 = `remote_url`：URL → `git fetch <remote_url> <dd_ref>`；本地路径 → `git fetch <本地路径> <dd_ref>`（或直接解析本地 `refs/heads/dd/<id>`）；两者都解析不到才 ok:false + 机器可读 detail。**绝不 fallback 到 `origin` 猜源**（本缺陷正是 origin 与 remote_url 不同源造成的）。URL remote 行为不变（此时 remote_url==origin 等价）。

## 交付 C：阴性测试（必须能红，合成本地仓，禁触真网/生产 checkout）

1. 阴性 A（identity）：合成无全局 identity 环境（清空 GIT_AUTHOR/COMMITTER、无 user.name/user.email config）→ `worktree_cherry_pick` 返回 ok:true、`harvest_tip` 非空；对照未修复时必然 `Committer identity unknown`。
2. 阴性 B（remote_url）：合成 record 其 `remote_url` 为**本地路径**仓、dd ref 只推在该本地仓、`origin` 故意指向不存在的远端 → `fetch_dd_ref` ok:true；对照未修复时 `couldn't find remote ref`。
3. 反向不抖动：URL remote_url 且 `origin` 同源 → 行为不变（既有路径零回归）；有 identity 环境 → cherry-pick/洗树/冲突重试路径不变。
4. `make verify` 全绿；`test_harvest*` / `test_supervisor_conformance` / H 系列（H1-H8 写前闸/清场/冲突重试）零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据（goal.md M3/M4 尾项口径）

1. `make verify` 通过。
2. 两条阴性（A identity / B remote_url）在无修复时分别红在 `Committer identity unknown` 与 `couldn't find remote ref`，修复后转绿——机械判，不采信自述。

## 前置说明/另案（不并入本单）

- (3) 默认 verify 指令 `DEFAULT_VERIFY_ARGV=["make","verify"]` 对无 make 的仓（fleet-sentinel）不适用，属「收割器 verify 指令按仓解析」另案，本单不改。
- (4) pr_squash_merge 分支被残留 worktree 占用（/data/worktrees/fs-harvest-glm53-20260901）属 cleanup 顺序另案，本单不改。
- 本单不改 allowlist 语义、不触 `harvest-allowlist.json`（只读）、不代造收割单、不重新收割 dev-fg-2e44f0e61516（该单收割成败由监督面判）。

## 铁律

- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach。
- 只改 `supervise/harvest_ops.py`（+`supervise/harvest.py` 若需透传 remote_url）+ `tests/`；不触 `decide()`、E3/checkpoint-authoritative、harvest/allowlist 语义、判据（goal.md 验收断言）。
- 不写全局 git 配置、不产生机器级副作用；身份一律 `-c`/env 级注入。
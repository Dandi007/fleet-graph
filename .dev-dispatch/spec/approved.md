# 线级完成闸：done 不再线自己说了算（goal.md 16:4x G2）

来源：监督面直写 goal.md 2026-09-02 16:4x「第四节：真正的空缺：线级完全无闸」+ 交付物 G2。
实证：两笔「已批准、已完成、从未进默认分支」的交付——`dev-fg-81dbb77434fa`（新增 253 行，
canonical 一行都没有，躺四天）、`dev-fg-b0ea914caf0e`（102 行只命中 2/8）——都是线声明 done
之后才被发现。现状：单级过度设闸（342 次 92% 走过场），线级完全无闸，正好拧反。

## 要什么

线声明 `terminal=done` 时**不直接退场**，先进「待验收」，由监督面核**产物是否真在默认分支上**，
过了才退役。监督面核产物在默认分支用的机械法已验证过，照抄别自己发明：

- 可用法（两法）：「产物补丁能否在目标树上反向干净撤销」+「产物新增特征行 grep 目标分支」；
  推 canonical 用 `remote_url`，不要用 `git rev-parse --git-common-dir`
  （对「worktree 套 worktree」会指到中间那棵树）。
- 不可用法（已证伪）：`git merge-base --is-ancestor` 因 squash 合并系统性假阴；
  整文件内容比对系统性假阳。

## 判据（两方向可红）

### 阳性
产物确已在默认分支 → 可退役。

### 阴性（不可弱）
存在**已批准但未进默认分支**的产物 → **不得退役**，并指名是哪一单、缺什么。
回归夹具用真实历史样本：`dev-fg-81dbb77434fa`（应判「未进默认分支」）、
`dev-fg-b0ea914caf0e`（应判「仅部分命中 2/8」）。
变异：把「产物在默认分支」判定改成「只要 dd record terminal=complete 就算在分支」→
必须有用例转红。

## 交付约束

- 只改 `src/fleet_graph/scheduler/daemon.py`（line done 处置加待验收门）、
  必要的 `src/fleet_graph/state/` 或 `graphs/goal_line.py` 接线，与
  `tests/test_scheduler_daemon.py` / `tests/test_goal_line.py` 及必要 fixtures；
- 待验收核产物在默认分支的机械法放监督面执行路径，本线只做「门」与「产物是否在默认分支」
  的机械判定实现；两笔回归样本作 fixture 硬编码其结论，不假想；
- 不触 harvest/allowlist；不改 done 的持久化语义（checkpoint 仍权威）；不部署不重启。

```dd-acceptance
uv sync --frozen
make verify
```
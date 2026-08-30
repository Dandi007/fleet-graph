# M3 收割反应器——harvest ReAct 子图 + allowlist 先行 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在独立 worktree）。
- 归属：goal.md M3「收割反应器」。依赖 M1（:7494 read-model）+ M2（E5 approved_unharvested 事件）。
- 类别：纯增量（新增 harvest 子图 + allowlist 配置 + conformance 扩展），不改 E1–E7 词表语义、不改判据。

## 交付 A：allowlist 先行（M3 顺序不可倒）

1. 新增独立配置（preauth 式，参照 supervise/preauth.py 语义）：可写 repo / 分支 / 部署脚本白名单，字段含 repo_path + 允许分支(或 ref 前缀) + 允许执行的部署脚本/命令列表。
2. 越界写拒绝并留痕：写目标不在白名单 -> 拒绝执行 + 记录 evidence，绝不静默放行。
3. 未合入 allowlist 前 harvest 子图不得获得任何写权限：默认 deny-all；写权限唯一来源 = 命中白名单条目。

## 交付 B：harvest ReAct 子图（supervisor 进程内）

1. 输入：E5 approved_unharvested 事件（payload: development_id / head_commit / stage）。
2. SOP：fetch dd ref -> cherry 判重（产品 commit 是否已 cherry 等价进默认分支）-> 独立 worktree cherry-pick 产品 commit -> 冲突消解(即兴) -> 全量套件(make verify) -> PR -> squash merge -> ff-only pull -> 部署 -> 真机 verify -> evidence note 挂卡。
3. 后置条件代码核验（不采信子图自述）：PR merged + verify 命令零退出 + evidence note 存在，三缺任一 = 失败/升报。
4. 生成-验证分离：写动作必须落在 allowlist 圈定目标。

## 交付 C：conformance 扩展

- scripts/check_supervisor_conformance.py 扩展为：harvest 子图只能写 allowlist 内目标，越界写即诊断。

## 交付 D：测试

1. allowlist 拒绝路径：非白名单 repo/分支/部署脚本 -> 拒绝 + 留痕，不执行任何写。
2. harvest 子图单测：E5 事件 -> 编排步骤齐全；后置条件三要素缺一即失败。
3. make verify 通过；E1–E7 词表/负例零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- allowlist 未合入前 harvest 子图无任何写权限（M3 顺序不可倒）。
- 收割后置条件只认代码证据，不采信子图自述。
- 一切改动走 PR，不直改 main；生产主 checkout 仅 ff-only pull。判据只有用户能改。
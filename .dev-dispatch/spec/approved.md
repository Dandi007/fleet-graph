# 第五复签条件——收割只打产品补丁（写入文件集合 == net_product_files 构造成立）

来源：监督面直写 goal.md 2026-09-03 03:1x「🟢🔴 三闸验收通过 + 第二个缺陷」
的第五条复签条件；与 2026-09-03 00:4x「监督面交界裁决」一致——本单只做
**收割写入面**的改造（merge 写什么由本单钉死），不触反应器接线位置（归
wf-8d9737）、不改 allowlist 四条判据语义、不改 E5/E6/E7 事件词表、不判签/
不改 `harvest-allowlist.json`、不部署不重启。

## 缺陷机械事实（实现据此，取证锚点，不重证）

上一单 `dev-fg-4f4f4dac23f2` 落地三闸后，反应器第一次真实收割
`dev-fg-7b1085a7b2e0`（PR #157）：三闸全部按设计生效——`approved_head
e31ca135` == 放行 head 且是 `harvested_head 787cee51` 的祖先；
`net_product_files` 正确列出 4 个真产品文件（`fleet_sentinel/exporter.py` 等）；
回执三头齐；outcome=harvested、非空 404 行。**但 `pr_squash_merge` 走的是
`worktree_cherry_pick` 产出的 `git merge --no-ff <approved_head>` 整个分支 diff，
把 11 个协议文件（`.dev-dispatch/` + `.dd-evidence/`）一并 squash 进了 master**，
而该仓此前协议文件数为 0。

**数的和合的不是一回事**：`net_product_files` 用 `:(exclude).dev-dispatch
:(exclude).dd-evidence` 排除协议目录算出了正确的产品文件清单，但 merge 写盘的
是**整个分支 diff**——协议子树照样进了默认分支。

**后果不是洁癖**：`dd/bootstrap.py:113-122 _refuse_if_edited_since_bootstrap`
拿 `git log --diff-filter=A -- .dev-dispatch/development.json` 最老的那个添加
提交当锚，比对锚上身份 vs HEAD 身份，不等则 `raise IdentityChanged`。master
一旦跟踪身份区，下一张派到该仓的 dd 单会在 bootstrap 处**确定性崩掉**（同
2026-08-28 `dev-fg-721ccba7a59e` 七连瞬退的坑）。

## 要什么（第五条复签条件）

> 收割实际写入的文件集合必须等于回执里的 `net_product_files`。多写一个文件
> 就是越权写入，必须 escalate，并且要有一条会红的用例守着。

实现方向（监督面给定，必须照此做）：**收割不再 merge 整个分支**，而是只打产品补丁

```
git diff <base>..<approved_head> -- . ':(exclude).dev-dispatch' ':(exclude).dd-evidence'
```

落成**一个新提交**（父系 = 默认分支 tip，不是 approved_head 的 merge），推到
`harvest/<development_id>`。这样「实际写入集合 == 数过的集合」是构造上成立的，
不需要额外断言去兜——但**仍需一条机器可读的收尾断言**把「越权多写」钉死为红。

## 判据（两方向都要能红）

- ① 阳性：正常产品补丁 → 照常绿。收割后产出 `harvest_tip`（产品补丁提交），
  `git diff <base>..<harvest_tip> --name-only`（**不带**任何 exclude）逐字等于
  `net_product_files`；`.dev-dispatch/` 与 `.dd-evidence/` 两条前缀零出现。
- ② 阴性（冒名超集，不可弱）：若写盘集合出现**多写一个文件**（例如把整个分支
  合进来从而夹带协议文件，或任何 `net_product_files` 之外的路径）→ **必须
  escalate**，且有一条例行会红的用例守着——把「只打产品补丁」变异回「整个分支
  merge」或「多写一个文件」，该用例必须转红。

## 与既有三闸的守恒（不许回退，本单改的是写入形态）

上一单四条判据**一条都不能弱**：

1. 收割绑定被放行的 exact head：`approved_head` 仍取 E5 `head_commit`；产品补丁
   的**来源必须锚定 `approved_head`**（`git diff <base>..<approved_head>`），
   不得「分支 head 是什么就收什么」。
2. 净产品 diff 为空必须 escalate，不得记「harvested」。
3. 回执三头对账：回执仍记 `approved_head` / `harvested_head` /
   `net_product_files`。
4. 净 diff 为空不得触发部署（writes_skipped 覆盖）。

⚠️ **必须注意的守恒点（本单的核心难点，写错等于回退判据①/③）**：
现有 `harvested_head 必须是 approved_head 后代`（`is_ancestor`）的判定，是
**merge 语义**下成立的——`git merge --no-ff <approved_head>` 的 merge commit
父系含 approved_head。改成「产品补丁新提交」后，`harvest_tip` 的父系是默认分支
tip，**approved_head 不再是它的祖先**。实现必须**同步改写**这条判定为等价的
「产品补丁等价」契约，而不是留着 `is_ancestor` 让合法收割被误杀、也不是删掉
判定让非祖先头放行。建议等价形态（方向自决，但判据不能弱）：

- `harvested_head` 相对 `base` 的产品内容必须与 `approved_head` 相对 `base` 的
  产品内容**逐字节等价**（`git diff base..approved_head` 与
  `git diff base..harvested_head`，均按同样的协议排除口径，内容一致）；且
- `harvested_head` 相对 `base` 的**不带排除口径**的文件清单必须**恰好等于**
  `net_product_files`（这就是第五条：多写一个文件即红）。

`approved_head` 是 `harvest_tip` 祖先这一条只在「merge 形态」下成立，改为补丁
形态后应被上面的等价契约**取代**，而不是并行共存造成自相矛盾。

## 交付约束

- 只改 `src/fleet_graph/supervise/harvest.py`（worktree/pr_merge/postconditions/
  receipt 及对账）、`src/fleet_graph/supervise/harvest_ops.py`（产品补丁构建 +
  等价对账读口）与 `tests/test_harvest.py` 及必要 fixture；必要时给
  `HarvestOps` 协议增补/替换机械读口（先行更新 fake 实现，模拟器与真机行为一致）。
- 协议子树**只在 diff 计算里排除**（`:(exclude)` pathspec），不得再落「exclude
  提交」（这是 09-03 01:0x 事故的根因之一）。
- 产品补丁**不是** cherry-pick——cherry-pick 会打断祖先后代关系；形态是
  `git diff` + 落在默认分支 tip 上的一个新提交。
- 不触反应器接线位置；不改 allowlist 四判据语义；写动作仍全部被 allowlist gate
  包住（Guard D 不变）。

```dd-acceptance
uv sync --frozen
make verify
```
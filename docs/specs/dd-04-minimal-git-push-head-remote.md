# minimal git 闸：交接机械核对（已 push / HEAD==remote tip / 干净 / spec 存在）

# DD-104 · minimal git 闸：agent 交接的机械核对

## 背景（自含）

本仓 `release/loopx-minimal` 正在按 `docs/specs/minimal/` 重建最小系统。其中一条总原则是 golden-order GO-25：

> 「能机械化确定的都尽量机械化去确定，脚本化或者靠外层的框架调度一些机械化节点去处理，那些自由裁量的才给 agent。」

落到 git 上，定稿的规则是（**以 golden-order 为准，protocol §0.10 的 commit 核对写法已被覆盖**，见 `docs/specs/minimal/README.md` 第 3 条）：

- GO-28：所有开发以 PR 为单位；**每次 agent 之间交接，强制核对「已 push 到 remote」且「本地 HEAD == remote 分支 tip」**；因此 agent 的输入里**不带 commit sha**，只带 branch + PR。
- GO-36：开 DD 时引擎对 Goal Agent 的准备工作做**机械五条核对**：① 分支在 remote 上存在；② worktree 确实在该分支上；③ HEAD == remote tip；④ 工作树干净；⑤ spec 文件（`docs/specs/N-XX.md`）在该 commit 里存在。核不过就按 GO-25 打回。
- GO-32：每个 repo 必须有 remote。
- 一张 DD 可跨多个 repo（GO-36），所以核对要能对一组 repo 逐个做并汇总。

必读：`docs/specs/minimal/golden-order.md` GO-25 / GO-28 / GO-29 / GO-35 / GO-36、`docs/specs/minimal/context.md` 第 8–9 行、`docs/specs/minimal/protocol.md` §0.10（读它的意图，但字段以 GO 为准）。

本单只做**核对与查询**这一层纯逻辑，不做任何写操作。

## 要改什么

在包 `src/fleet_graph/minimal/`（若同批其它 DD 尚未落地则自行建 `__init__.py`，**不要 import 同批其它 DD 的模块**）新建 `gitgate.py`：

1. **`GitRunner` 协议 + `SubprocessGitRunner` 默认实现**。所有 git 调用集中在这一层：`run(args: list[str], *, cwd: str) -> CompletedResult(exit_code, stdout, stderr)`。必须带 timeout，**禁止 `shell=True`**，参数以 list 传。测试用 fake runner 完全不碰真实 git。

2. **`WorktreeStatus` 查询**：给定 worktree 路径，返回 `branch`（当前分支名，detached 时为 None）、`head`（40 位 sha）、`clean`（`git status --porcelain` 为空）、`untracked`（是否有未跟踪文件，单独给出，便于错误信息更准）。

3. **`remote_tip(worktree, remote, branch) -> str | None`**：先 `git fetch <remote> <branch>`（可由参数关掉 fetch 便于测试），再解析远端 tip；分支在 remote 上不存在时返回 None（这是 GO-36 第①条的判据，不是异常）。

4. **`file_exists_at(worktree, commit, relpath) -> bool`**：用 `git cat-file -e <commit>:<relpath>` 之类的方式判断某个 commit 里是否存在该文件（**不看工作树**，GO-36 明确要求「spec 文件在该 commit 里存在」）。

5. **`check_handoff(repos, *, runner) -> GateResult`**：GO-28 的交接闸。输入是一组条目（每项含 worktree 路径、remote、branch），逐个核「已 push、HEAD == remote tip、工作树干净」，汇总成 `GateResult(ok: bool, failures: list[GateFailure])`；每条 failure 带 repo 标识、失败判据代号（如 `not_pushed` / `head_behind_remote` / `dirty_worktree` / `branch_missing_on_remote` / `detached_head`）与人读 detail。**不要在这里抛异常做控制流**——失败是正常返回值，因为它要被写进 event 并打回给 agent。

6. **`check_dd_ready(repos, *, runner) -> GateResult`**：GO-36 的开 DD 五条核对。输入每项在上面的基础上再加 `spec_path`（仓内相对路径，如 `docs/specs/101-foo.md`）。除 handoff 的三条外，另加「分支在 remote 存在」与「spec 文件在 HEAD 这个 commit 里存在」（失败代号 `spec_missing`）。

7. **失败代号做成常量枚举**并导出，供后续 DD 写进 event payload 与打回消息时引用（不要让下游靠字符串字面量硬编码）。

## 不改什么

- **不做任何写操作**：不 commit、不 push、不建分支、不开/删 worktree、不 merge、不 rebase、不开 PR。那些是后续 DD 的范围（GO-35 的 PR/worktree 生命周期、GO-15 的 Merge Agent）。
- 不碰 `src/fleet_graph/dd/git*.py` 等旧模块，也不 import 它们；新代码只在 `src/fleet_graph/minimal/` 下。
- 不碰 GitHub API / `gh` CLI（开 PR 是后续批次）。
- 不引入新运行时依赖；不改 Makefile；不改 `docs/specs/minimal/`。

## 怎么验收

`make verify` 绿（ruff line-length=100，写完 `make fmt`）。

另建 `tests/test_minimal_gitgate.py`，用 fake GitRunner（按 argv 匹配返回预设结果），至少覆盖：
- `check_handoff` 全绿的双 repo 情形 → ok=True；
- 工作树脏 → ok=False 且 failure 代号 `dirty_worktree`；
- 本地 HEAD 领先 remote（未 push）→ `not_pushed`；本地落后 remote → `head_behind_remote`；
- 远端无该分支 → `branch_missing_on_remote`；
- detached HEAD → `detached_head`；
- 多 repo 中只有一个坏 → ok=False 且 failures 恰好一条、点名那个 repo；
- `check_dd_ready`：spec 文件在 commit 里不存在 → `spec_missing`；五条全过 → ok=True；
- `file_exists_at` 对 `git cat-file -e` 的非零退出正确判 False，不抛异常；
- 断言 fake runner 收到的 argv 里**没有** `shell=True` 式的拼接、且所有调用都带 `cwd`。

可选（不强制）：若愿意，可加一个用 `tmp_path` 真起 `git init` 的集成测试，但必须能在无网络环境下跑（只用本地裸仓做 remote）。

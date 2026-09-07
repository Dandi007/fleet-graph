# prlifecycle.py：DD 的 PR 与 worktree 生命周期（GO-35）

## 背景

GO-35（golden-order.md:208-211）：每张 DD 一个 PR；PR 过 review 并 merge 后引擎机械关 PR、删 worktree、删远端 dd 分支；DD 以 failed 结束时同样关 PR（不合并）并清 worktree。GO-36（:213-217）把建分支/worktree 移给 Goal Agent，引擎核过五条后**开 PR 到 release 分支**、跑基线验收、起 Impl。context.md:9 另定：Goal approve 后先看平台 mergeable，能合就合，冲突才 Merge Agent。

现状缺口：完全没有实现。`gitgate.py` 只做只读核对（status / remote_tip / cat-file），不做任何写操作。

## 要改什么

新文件 `src/fleet_graph/minimal/prlifecycle.py`。风格与 `gitgate.py` 严格对齐：**每个命令都是 `list[str]`、`shell=False`、runner 注入、失败是数据不是异常**。只 import 标准库（可 import `gitgate` 复用 `GitRunner` / `CompletedResult` / `_GUARDS` 的同形本地副本——若复用请只复用类型 Protocol，argv guard 在本文件自含，注释说明原因）。

### 1. 安全边界
- token 白名单：remote / branch 用 `^[A-Za-z0-9._/-]+$`，且不得以 `-` 开头（防被当成 flag）；PR number 必须是 `int` 且 > 0。违反即 `ValueError`，在构造 argv 之前。
- 所有 git 调用带三条 guard（`core.fsmonitor=false`、`core.hooksPath=/dev/null`、`protocol.ext.allow=never`）并置于 `-C <path>` 之前，理由同 `gitgate.py:232-239`。

### 2. 数据类型
- `@dataclass(frozen=True) class PrRef`: `number: int`、`url: str`。
- `@dataclass(frozen=True) class StepResult`: `step: str`、`ok: bool`、`argv: list[str]`、`detail: str | None`。
- `@dataclass(frozen=True) class CleanupResult`: `ok: bool`、`steps: list[StepResult]`。

### 3. 操作
- `open_pr(worktree, *, head_branch, base_branch, title, body_file, runner) -> PrRef`：argv `["gh","pr","create","--head",H,"--base",B,"--title",T,"--body-file",F]`，cwd=worktree。stdout 里抓 `https://github.com/…/pull/<n>` 解析出 number 与 url。**幂等**：gh 报「already exists」时改跑 `["gh","pr","view",head_branch,"--json","number,url"]` 复用现有 PR，不报错（重放安全，对齐 protocol.md:291 的幂等要求）。都失败则抛 `PrError`（带 argv 与 stderr）。
- `pr_mergeable(worktree, number, *, runner) -> str`：`gh pr view <n> --json mergeable` → 返回 `"MERGEABLE" | "CONFLICTING" | "UNKNOWN"`，无法解析时返回 `"UNKNOWN"`（不猜、不抛）。这是 context.md:9「approve 后先看平台 mergeable」的机械输入。
- `close_pr(worktree, number, *, runner, comment: str | None = None) -> StepResult`：`gh pr close <n>`（带 comment 时加 `--comment <text>`）。PR 已 closed/merged 视为成功（幂等）。
- `remove_worktree(repo_path, worktree, *, runner, force: bool = False) -> StepResult`：`git … -C repo_path worktree remove [--force] <worktree>`，随后 `git … -C repo_path worktree prune`。worktree 已不存在视为成功。
- `delete_remote_branch(worktree, remote, branch, *, runner) -> StepResult`：`git … -C worktree push <remote> --delete <branch>`；远端已无该分支视为成功。
- `cleanup_dd(*, outcome, repos, runner, comment=None) -> CleanupResult`：`outcome` ∈ `merged|failed`（其它值 ValueError）。`repos` 是每 repo 一条 `(repo_path, worktree, remote, branch, pr_number)` 的记录序列。按 GO-35 编排：先 `close_pr`（merged 时 PR 已随 merge 关闭，仍调一次取幂等）、再 `remove_worktree`、再 `delete_remote_branch`。**逐步收集失败但不抛、不早退**——清理失败只该落 event，不该让 DD 卡死；`CleanupResult.ok` 为全部 step 的与。

### 4. 明确不做
- **不做 merge**。合并归 Merge Agent（GO-15 / design.md:13「引擎自己不做任何 merge」）。本模块没有 `merge_pr`。
- 不写 event（调用方负责）、不建分支、不建 worktree（GO-36 归 Goal Agent）、不跑验收命令。

### 5. `__init__.py`
照现有风格在 docstring 里列上模块名，不 import。

## 不改什么
`gitgate.py`、`acceptance.py`、`events.py`、`ddflow.py`、`control.py`、`enroll.py`、`protocol.py` 一律不动。不改 protocol.md / design.md。不引入新依赖（gh 与 git 都是外部可执行文件，经注入 runner 调用）。

## 验收
`make verify` 全绿，外加下面命令。测试全部用录制式 fake runner（照 tests/test_minimal_gitgate.py 现有写法），断言**argv 逐个 token**，不起真进程、不碰网络：`open_pr` 正常解析 number/url；`open_pr` 在 already-exists 分支回落到 `gh pr view` 并复用；`pr_mergeable` 三种返回值与不可解析→UNKNOWN；`close_pr` / `remove_worktree` / `delete_remote_branch` 的幂等分支各一例；`cleanup_dd` 在中间一步失败时仍跑完后续步骤且 `ok is False`、`steps` 完整；outcome 非法与 number/branch/remote 非法 token（含以 `-` 开头、含空格）在生成 argv 前即抛；每条 git argv 的三条 guard 都在 `-C` 之前。

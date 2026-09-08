# mergegate.py：approve 后先看 mergeable（能合就合，冲突才 Merge Agent）+ release→target 收尾合并

## 背景

GO-15 原本定「所有合并都交 Merge Agent，引擎不做 merge」。GO-36 的回复（已落进 `docs/specs/minimal/context.md` 的「已定」段末句）改为：**Goal approve 后先看平台 mergeable，能合就合，冲突才 Merge Agent**；protocol.md §0.10 末条同义。README.md 明写「凡与 design/protocol 冲突之处以 golden-order 最新段落为准」，所以按 GO-36 实现。

仓里已有件（读源码确认签名）：

- `prlifecycle.py`：`pr_mergeable` / `PrRef` / `PrError` / `StepResult` / `close_pr`
- `gitgate.py`：`GitRunner` / `SubprocessGitRunner` / `remote_tip` / `worktree_status` / `check_handoff` / `RepoRef` / `FailureCode`（argv 一律 list、`shell=False`、token 白名单 —— **照这个风格写**）
- `enroll.py`：`RepoSpec`（每 repo 的 `target_branch` 可不同，GO-31）
- `runroot.py`：`release_branch`

## 要做

新增 `src/fleet_graph/minimal/mergegate.py` 与 `tests/test_minimal_mergegate.py`。全部纯函数 + 注入 IO，模块内零全局 `subprocess`。

**1. `MergeRoute` 常量 + `MergeDecision`（frozen dataclass）**：`route` ∈ `platform_merge` / `merge_agent`，`reason`（机器可读的短码 + 人读 detail）。

**2. `decide(pr, *, mergeable_fn) -> MergeDecision`**：`mergeable_fn` 返回 True → `platform_merge`；返回 False（冲突）或 unknown/None（平台还没算完、查询失败）→ `merge_agent`，带 reason。**不猜**：unknown 一律走 agent，不当作可合。

**3. `platform_merge(pr, *, gh_runner) -> tuple[str, dict]`**：走平台合并（argv 为 `list[str]`、`shell=False`、所有插值 token 过白名单正则，风格照 `gitgate._git`）。成功返回 `("merged", {"merged_commit": ..., ...})`；失败返回 `("failed", {"detail": ...})`。**失败是数据不是异常**（照 `agentrun.AgentResult` 的纪律），不抛。

**4. `verify_merge_output(obj, repos, *, git_runner) -> list[str]`**：protocol §0.10 表里 Merge Agent 的 Stop 后机械核对，返回错误列表（空 = 通过）：
- `merged`：`merged_commit` == 目标分支 tip，且该 commit 包含 `source_head`；
- `rebased`：`new_head` 是源分支 tip，且目标分支 tip 未变；
- `failed`：源、目标两分支 tip 均未变。

**5. `final_merge_plan(enroll, *, release_branch) -> list[MergePlan]`**：线 done 时的收尾合并计划——release 分支 → **每个 repo 各自的** `target_branch`（GO-31：target 按 repo 可不同）。`MergePlan` 含 repo id、source（release）、target、remote。

## 不改什么

- 不实现任何 LangGraph 图、不调 `stagerunner`、不渲染 prompt（Merge Agent 的实际调用由 `ddgraph` / `goalgraph` 的 `merge_fn` seam 接，那是别的 DD）。
- 不改 `prlifecycle` / `gitgate` / `enroll` / `runroot` 任何一个字节。
- **不要改 `src/fleet_graph/minimal/__init__.py`**（连续五张 DD 的 rebase 冲突源）。
- 不 import `ddgraph` / `goalgraph`（同批并行）。
- 不改 `pyproject.toml` / `Makefile` / 不加依赖。

## 验收

`tests/test_minimal_mergegate.py`：
1. `decide` 三态路由：True → platform_merge；False → merge_agent；None/unknown → merge_agent（断言 reason 里点明 unknown 不当可合）；
2. `platform_merge` 成功返回 merged + merged_commit，失败返回 failed + detail 且**不抛**；
3. `verify_merge_output` 三种 stop 各一条通过用例，以及每条核对项各一条不过用例（merged_commit 不是 tip / 不含 source_head / rebased 时目标 tip 动了 / failed 时源 tip 动了）；
4. `final_merge_plan` 在两个 repo、target 分别为 main 与 develop 时给出两条正确计划；
5. 断言所有对外 argv 都是 `list[str]`，且没有任何一处 `shell=True`（可用 fake runner 捕获全部调用后断言）。

`make verify` 全绿，ruff 干净。

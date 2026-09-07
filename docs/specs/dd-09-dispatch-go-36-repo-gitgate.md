# dispatch 启动协议校验：GO-36 多 repo 字段 + 转 gitgate.DDRepoRef

## 背景

GO-36（golden-order.md:213-217）定：Goal Agent `stop: dispatch` 的输出对象**就是** DD 启动协议，一张 DD 可跨多个 repo，协议里写每个 repo 的分支、worktree 路径、spec 相对路径；Goal Agent 在 Stop 前自己建 dd 分支、开 worktree、把 spec 写成仓内 `docs/specs/N-XX.md` 并 push；引擎随后机械核五条（分支在 remote、worktree 在该分支、HEAD==remote tip、工作树干净、spec 文件存在）。

现状缺口：
- `src/fleet_graph/minimal/protocol.py:79` 的 `_check_goal_turn` 对 `stop==dispatch` 只校验 `dispatch` 是 dict。`spec_text`/`repos[]` 一个字段都没查，等于 GO-36 的启动协议没有校验节点。
- `src/fleet_graph/minimal/gitgate.py:337` 的 `check_dd_ready(repos: list[DDRepoRef], *, runner)` 五条核对已实现，`DDRepoRef(worktree, remote, branch, label, spec_path)` 也在（gitgate.py:191-208，`spec_path` 空串在构造期即拒）。但没有任何东西能把一个 dispatch 对象变成 `list[DDRepoRef]`。

## 要改什么

### 1. `protocol.py`：补 dispatch 字段级校验
新增模块内私有 `_check_dispatch(dispatch: dict, schema: str) -> list[str]`，由 `_check_goal_turn` 在 `stop == "dispatch"` 且 dispatch 是 dict 时调用，错误合并进同一个 errors 列表（沿用现有 `f"{schema}: ..."` 文案风格）。规则，全部机械、不猜：
- `spec_text`：非空 str（protocol.md:119「非空是硬校验」）。
- `repos`：必须是 list 且非空（一张 DD 至少一个 repo）。
- `repos[i]` 每项：必须是 dict，且 `path` / `remote` / `branch` / `spec_path` 四个键都是非空 str。错误文案带下标，如 `goal.turn/1: dispatch.repos[1].branch must be a non-empty string`。
- `repos[i].path`：必须是绝对路径（`path.startswith("/")`）——worktree 路径由 Goal Agent 给，相对路径无法核对。
- `repos[i].spec_path`：必须是**仓内相对**路径（不以 `/` 开头、不含 `..` 段），对齐 protocol.md:11「文件路径一律相对 workspace 根」。
- `repos[i].branch`：非空、不以 `-` 开头、不含空白字符、不含 `..`、不以 `/` 结尾（与 `enroll.py:68` 的 `is_valid_git_branch_name` 同规则）。
- `repos[i].path` 在 repos 内不得重复；`(path, branch)` 组合亦不得重复。
- `acceptance_extra`（可选）：给了就必须是 list[str]，每条非空。
- 未列出的键忽略（protocol.md:14 §0.6）。

**不要**在 protocol.py 里 import 任何其它 `fleet_graph.minimal` 模块——它是最底层，`enroll.py` / `gitgate.py` 都不能被它引用。分支名规则在本文件内自含实现（几行，不要跨模块共享）。

### 2. 新文件 `src/fleet_graph/minimal/dispatch.py`
纯转换层，零 IO、不跑 git、不调 agent。只 import `protocol` 与 `gitgate`（以及 `acceptance` 用于合并验收命令）：
- `SCHEMA = "goal.turn/1"` 相关不重复定义，直接用 protocol 常量。
- `dd_repo_refs(dispatch: dict) -> list[gitgate.DDRepoRef]`：把 `dispatch["repos"]` 逐项转成 `DDRepoRef(worktree=path, remote=remote, branch=branch, spec_path=spec_path, label=<repo path 的 basename>)`。调用前先跑 `protocol.validate(<整个 goal.turn/1 对象>, protocol.SCHEMA_GOAL_TURN)`？不——本函数只接 dispatch 子对象，进入即假设已过校验；若字段仍不合法（防御），抛 `ValueError` 并带下标，不返回半成品。
- `dd_acceptance(dispatch: dict, goal_acceptance: list[str]) -> list[str]`：用 `acceptance.combine_acceptance(goal_acceptance, dispatch.get("acceptance_extra"))`（acceptance.py:75 已有）产出该 DD 的完整验收命令序列。
- `check_dispatch_ready(dispatch, *, goal_acceptance, runner) -> gitgate.GateResult`：`dd_repo_refs` 之后直接调 `gitgate.check_dd_ready(refs, runner=runner)` 返回结果；不写 event、不开 PR、不做任何副作用。

### 3. `__init__.py`
按现有风格把 dispatch.py 的公开名加进 docstring 的模块列举；**不要** import dispatch（现有约定：不同 DD 的模块互不 import，__init__ 只导出 protocol，见 `__init__.py:1-7`）。

## 不改什么
- 不动 `gitgate.py`（`check_dd_ready` / `DDRepoRef` 已够用）、`ddflow.py`、`events.py`、`control.py`、`enroll.py`、`agentrun.py`。
- 不实现 dd_id 生成、不开 PR、不建 worktree、不建分支（分别属于别的 DD 与 Goal Agent）。
- 不改 protocol.md / design.md（文档回写是另一件事，用户还有三项待拍板）。
- 不引入新依赖。

## 验收
`make verify` 全绿，外加下面的额外命令。新测试要覆盖：dispatch 缺 spec_text / repos 空 / repos 非 list / 某项缺 branch / spec_path 绝对路径 / spec_path 含 `..` / path 相对 / path 重复 / branch 带空格 / acceptance_extra 非法，各自被 `protocol.validate` 判 invalid 且错误文案带下标；合法多 repo 对象过校验；`dd_repo_refs` 转出的 DDRepoRef 字段逐个对上；`dd_acceptance` 正确叠加；`check_dispatch_ready` 用假 runner（照 tests/test_minimal_gitgate.py 的现有 fake runner 写法）覆盖五条核对里至少「spec 文件不存在」与「全过」两条。

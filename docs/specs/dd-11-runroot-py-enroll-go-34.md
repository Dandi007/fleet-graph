# runroot.py：引擎状态根布局 + enroll 通过后的程序化准备（GO-34）

## 背景

GO-34（golden-order.md:201-206）纠正了 GO-19 的落法：运行时状态（events.jsonl、control.jsonl、sessions、worktrees、goal.enroll.json）**不放 WF**，由引擎在自己的根目录维护；WF 只放人读的 goal / design / progress / findings。context.md:7 写明形如 `/data/fleet/goals/<goal_id>/`。GO-33 要求列清「校验通过后引擎在起 Goal Agent 之前做的程序化准备」，context.md:6 已定为：建引擎根（不在 WF）、写 enroll 原样与首条 event、每 repo fetch 并在 release 分支缺失时从 target 切出 push、spawn。

现状缺口：`events.EventLog(goal_run_root)`（events.py:134）与 `control.ControlLog(goal_run_root)`（control.py:107）各自接一个 `goal_run_root` 字符串，但**没有任何模块定义这个根长什么样**，也没有 enroll 通过后的准备。`enroll.py` 明确只做校验（enroll.py:5-6：不写状态、不 spawn、不建分支）。

## 要改什么

新文件 `src/fleet_graph/minimal/runroot.py`。只 import 标准库与（可选）`fleet_graph.minimal.enroll` 的常量；不 import events/control/gitgate。

### 1. 布局
- `DEFAULT_ENGINE_ROOT = "/data/fleet/goals"`（可被参数覆盖，绝不在别处硬编码）。
- `@dataclass(frozen=True) class GoalRunRoot`：`goal_id`、`root: Path`，以 property 给出 `events_path`（root/events.jsonl）、`control_path`（root/control.jsonl）、`sessions_dir`、`worktrees_dir`、`dd_dir`、`enroll_path`（root/goal.enroll.json）、`observations_path`（root/observations.jsonl）。
- `goal_run_root(goal_id: str, *, engine_root: str | os.PathLike[str] = DEFAULT_ENGINE_ROOT) -> GoalRunRoot`：校验 goal_id 形状（`^g-[0-9a-f]{6}$`，与 enroll.py:39-40 的 `_GOAL_ID_PREFIX` / `_GOAL_ID_HEX_LEN` 一致），非法即 ValueError——goal_id 会进路径，不能带 `/` 或 `..`。

### 2. 分支与 worktree 命名（机械化，GO-25 / protocol.md §0.10 :33）
- `dd_branch(goal_id: str, dd_id: str) -> str` = `f"dd/{goal_id}/{dd_id}"`；`dd_id` 走白名单 `^[A-Za-z0-9._-]+$`。
- `worktree_path(run_root: GoalRunRoot, dd_id: str) -> Path` = `run_root.worktrees_dir / dd_id`。
- `release_branch(enroll_obj: dict) -> str`：**取 `enroll_obj["source_branch"]`**，不要用 protocol.md §0.10 里写的 `release/<goal_id>`。理由写进 docstring：GO-30/31（:181-189）定 release 分支名是 goal 级 enroll 字段 `source_branch`，只写一次、所有 repo 同名，这覆盖了 protocol.md 的旧写法。缺字段即 ValueError。

### 3. 建根与落 enroll
- `create_run_root(run_root: GoalRunRoot) -> None`：幂等 `mkdir(parents=True, exist_ok=True)` 建 root / sessions_dir / worktrees_dir / dd_dir。
- `write_enroll(run_root: GoalRunRoot, enroll_obj: dict) -> None`：把 enroll 对象**原样**写 `goal.enroll.json`（`ensure_ascii=False`, indent=2，write + flush + `os.fsync`，与 events.py:169-172 同纪律）。文件已存在且内容不同 → 抛 `RunRootConflict`（同一 goal_id 不重复 enroll，enroll.py 的校验清单里那条在这里兑现）；内容相同 → 静默通过（重放安全）。
- `read_enroll(run_root) -> dict`。

### 4. 准备计划（纯数据，不执行）
- `@dataclass(frozen=True) class RepoPrep`：`path`、`remote`、`target_branch`、`release_branch`、`needs_release_push: bool`。
- `@dataclass(frozen=True) class PreparePlan`：`run_root`、`release_branch`、`repos: tuple[RepoPrep, ...]`。
- `prepare_plan(enroll_obj: dict, goal_id: str, *, engine_root=DEFAULT_ENGINE_ROOT, release_exists: Callable[[str, str, str], bool]) -> PreparePlan`：每个 `enroll_obj["repos"]` 一条 RepoPrep；`release_exists(path, remote, release_branch)` 是**注入的**探测（照 `control.py` 的 `alive_probe` / `enroll.py` 的 probe 注入风格），返回 False 时 `needs_release_push=True`（表示要从该 repo 的 `target_branch` 切出 release 分支并 push）。本函数**不跑 git、不 push、不 spawn**，只产计划对象供后续引擎节点执行。
- `git_argv_for(prep: RepoPrep) -> list[list[str]]`：产该 repo 要执行的 guarded argv 列表（`git … fetch <remote>`；`needs_release_push` 时 `git … push <remote> <remote>/<target_branch>:refs/heads/<release_branch>`）。三条 `-c` guard 与 `enroll.py:48-55` 的 `_GIT_GUARDS` 同形（本文件内自含复制，注释说明为何不跨模块共享）。返回 argv，不执行。

## 不改什么
- 不改 `events.py` / `control.py` 的签名（把 `GoalRunRoot.root` 传给它们即可，它们已接 `str | PathLike`）。
- 不改 `enroll.py` 的校验逻辑。
- 不 spawn 进程、不发信号、不真的跑 git、不碰 work folder MCP。
- 不改 protocol.md / design.md。

## 验收
`make verify` 全绿，外加下面命令。测试用 `tmp_path` 作 engine_root，覆盖：goal_id 非法（含 `/`、`..`、大写、长度不对）被拒；八个路径 property 正确；`create_run_root` 幂等（连调两次）；`write_enroll` 首次写入后 `read_enroll` 逐字节还原（含非 ASCII 标题），同内容重写通过、异内容重写抛 `RunRootConflict`；`release_branch` 取 `source_branch` 且缺字段抛错；`prepare_plan` 在 probe 全 True / 全 False / 混合三种下 `needs_release_push` 正确；`git_argv_for` 的 argv 里 guard 在 `-C` 之前、且是 `list[str]` 无 shell 串；`dd_branch` / `worktree_path` 对非法 dd_id 抛错。

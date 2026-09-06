# minimal enroll 校验节点：goal.enroll/2 六字段 + 机械检查清单

# DD-102 · minimal enroll 校验节点

## 背景（自含）

本仓 `release/loopx-minimal` 正在按 `docs/specs/minimal/` 重建最小系统。线的入口是 MCP 收到的一个 goal enroll 请求，先过一个**纯程序化的校验节点**（golden-order GO-3.1：「程序化校验节点——确认输入的请求符合协议」），不过就拒绝、不 spawn 引擎。

必读：`docs/specs/minimal/golden-order.md` 第 **26–34** 段（enroll 的定稿全在这里）、`docs/specs/minimal/context.md`「已定」段、以及 `docs/specs/minimal/protocol.md` §1（**注意 §1 写的是过时的 `goal.enroll/1`，已被 GO-26~33 覆盖，以 golden-order 为准**）。

定稿后的 `goal.enroll/2` 是**六字段**（context.md 第 5 行）：

```json
{
  "schema": "goal.enroll/2",
  "work_folder": "wf-ab12cd",
  "title": "…",
  "goal_text": "…",
  "source_branch": "release/loopx-minimal",
  "repos": [ { "path": "/data/…（worktree 路径，GO-27）", "remote": "git@…（必有，GO-32）", "target_branch": "main", "acceptance": ["make verify"] } ]
}
```

关键约束（逐条来自 golden-order）：
- `source_branch` 是 **goal 级**的 release 分支名，**只写一次**，所有 repo 同名（GO-30、GO-31）。
- `target_branch` 在**每个 repo** 里，可各不相同（GO-31）。
- 每个 repo **必须有 remote**，没有 remote 的不接受 enroll（GO-32）。
- `repos[].path` 是 **worktree 的路径**，不是原始 repo 的 checkout（GO-27）。
- **去掉** `goal_path` / `sessions` / `warn` / 单数 `repo` / PR / worktree 字段（GO-27、GO-29；PR 与 worktree 下沉到 DD 级）。
- 一个 goal 可涉及**多个 repo**，且可能中途才加 repo（GO-26）。
- `work_folder` 是一等公民（GO-19）：为 null 时由 MCP 新建；本单只校验「给了就得是形如 `wf-` 的非空 id」，**不要真的去调 work-folder MCP**。

## 要改什么

在包 `src/fleet_graph/minimal/`（若同批 DD-101 尚未落地则自行 `mkdir` 并建 `__init__.py`，内容留空即可，**不要 import DD-101 的模块**以免互相阻塞）新建 `enroll.py`：

1. `EnrollRequest` 与 `RepoSpec` 两个 dataclass（或 TypedDict），字段如上。
2. **`validate_enroll(payload: dict, *, git_probe=None) -> EnrollValidation`**，返回 `ok: bool` 与 `errors: list[str]`（字段级消息，指明是哪个 repo 的哪个字段，如 `repos[1].remote: missing`）。全部检查项：
   - `schema == "goal.enroll/2"`；未知顶层键要报错（less is more，防旧字段回流）；显式拒绝 `goal_path` / `sessions` / `warn` / `repo` 这四个已废字段，错误信息里说明它们被哪一段 GO 移除。
   - `title` 非空字符串；`goal_text` 非空字符串。
   - `source_branch` 非空、是合法 git 分支名（不含空格、不以 `-` 开头、不含 `..`、不以 `/` 结尾、不含 ASCII 控制字符）。
   - `repos` 是**非空**数组；每项：`path` 非空且（经 git_probe）存在且是一个 git 工作树；`remote` 非空；`target_branch` 非空且合法分支名且（经 git_probe）在该仓存在；`acceptance` 非空数组，每条是非空字符串且能通过 `bash -n` 式的语法 dry-run 检查（protocol §1）。
   - `repos[].path` 去重：同一路径不得出现两次。
   - `work_folder`：为 None 合法（表示待建）；给了则必须是非空字符串。
3. **依赖注入的 git 探针**。所有需要碰真实文件系统/git 的检查（路径存在、是 git 工作树、target_branch 是否存在、`bash -n`）都走一个 `git_probe` 协议对象（Protocol 或简单 duck-typing），默认实现 `SubprocessProbe` 用 `subprocess` 跑 `git -C <path> rev-parse --is-inside-work-tree` / `git -C <path> rev-parse --verify <branch>` / `bash -n -c <cmd>`。**测试必须能传入 fake probe，完全不碰真实 git**。默认实现里所有 subprocess 调用必须带 timeout，且不得用 `shell=True` 拼接用户输入。
4. **`normalize_enroll(payload, goal_id=None) -> dict`**：校验通过后产出规范化对象（补上 `goal_id`，缺省生成形如 `g-` + 6 位十六进制；字段顺序固定；原样保留未来会被落盘的内容）。**本单不做落盘、不 spawn 任何进程、不建分支**——GO-34 已定「状态归引擎」，落盘与 spawn 是引擎的事，后续 DD 做。

## 不改什么

- 不碰 `src/fleet_graph/goal_enroll/` 这个旧包，也不碰其它任何旧模块；新代码全在 `src/fleet_graph/minimal/` 下。
- 不调用 katana-work-folder MCP、不建 work folder、不 fetch、不建分支、不 spawn 进程。
- 不实现 MCP 工具面（`goal_enroll` 等 8/9 个工具是后续批次）。
- 不引入新的运行时依赖。
- 不修改 `docs/specs/minimal/` 下任何文件，不改 Makefile。

## 怎么验收

`make verify` 绿（ruff line-length=100，写完跑 `make fmt`）。

另建 `tests/test_minimal_enroll.py`，全部用 fake git_probe，至少覆盖：
- 一个完整合法的双 repo 请求（两个 repo 的 target_branch 不同）→ ok=True；
- 缺 remote 的 repo → ok=False 且错误里点名 `repos[i].remote`（GO-32）；
- `repos` 为空数组 → ok=False；
- 出现已废字段 `goal_path` / `sessions` / `warn` / `repo` → 各自 ok=False；
- `source_branch` 出现在 repo 项里（旧形态）→ 被未知键检查拒绝；
- path 不是 git 工作树（fake probe 返回 False）→ ok=False；
- target_branch 在仓里不存在 → ok=False；
- acceptance 为空数组 → ok=False；某条 acceptance 语法不合法（fake probe 报错）→ ok=False；
- 两个 repo 用同一 path → ok=False；
- `normalize_enroll` 在未给 goal_id 时生成稳定形态的 id，且不改动 repos 的语义内容。

# H8 占用门作用域收敛（H-A/H-B/H-C）

## Goal

收割反应器（`supervise/harvest.py` 的 M3 ReAct 子图）的 H8 动树前 occupancy 门
目前被 **3 个缺 `record.json` 的目录** 彻底瘫痪：任何一次收割都在 intake 阶段
即被判 `HARVEST_TREE_OCCUPIED_BY_INFLIGHT` 而 escalate，产品 commit 永远落不进默认
分支。根因是并发门作用域错误——占用扫描枚举整个 `<dd_root>` 并把「任何一处
`record.json` 不可读」当成 **全局** 的「无法判定」，于是无关目录也拦住本次要动的树。

本轮修三件事，判据双向可红，**只做其一不收**：

- **H-A**　占用扫描作用域收敛到本次要动的树，不扫全库。
- **H-B**　结果终态即答案：`result.json` 为终态 = 该单不持有树，不进「无法判定」。
- **H-C**　判不定时必须给机器可读理由：`detail` 与 `repo_path` 均不得为空。

## Current Defect

`DefaultHarvestOps.detect_inflight_binding(tree_path, dd_root, ...)` 现行为：

1. 枚举 `<dd_root>` 下**所有** `*/` 子目录（`sorted(dd_root.iterdir())`）；
2. 对每个子目录读 `record.json`；读不到 / JSON 坏档 / 顶层非 object 时，把该目录
   append 到一个全局 `indeterminate` 列表（fail-closed）；
3. `repo_path` 为空的 record 直接 `continue`；
4. 命中绑定（record `repo_path` 经 `_binding_matches` 解析到 `tree_path`）才读
   `status.json` 的 `terminal` 字段判在飞/终态；
5. 循环结束后，只要 `indeterminate` 非空就返回 `in_flight=True` 且
   `bound_development_id=None`。

现网三个目录 `dev-fg-046c106083fb` / `dev-fg-229c68119576` /
`dev-fg-5ea26f3da7ce` 都没有 `record.json`（也无 `status.json`），但都带着终态的
`result.json`（`terminal` 分别为 `fault` / `failed` / `fault`）。它们与本次要收割的
树无关，却让上述第 5 步的 `indeterminate` 恒非空 → 每一次 `detect_inflight_binding`
都返回 `in_flight=True, bound_development_id=None` → `_detect_occupied_tree` 因
`bound_development_id != 本单 id` 而 escalate → 收割链在 `intake` 早退进 `receipt`，
任何写步骤都不再执行。

## Contract

### H-A — 占用扫描作用域收敛到本次要动的树（不扫全库）

`detect_inflight_binding(tree_path, ...)` 的**判定对象只有 `tree_path` 这一棵树**。

- 枚举 `<dd_root>` 子目录是可接受的读口，但一个子目录只有在「能证明自己绑定到
  `tree_path`」时才进入判定范围（in scope）。
- 缺 `record.json`、`record.json` JSON 坏档、或其顶层非 JSON object 的子目录，**只
  读不到任何 `repo_path`，因而无法构成对 `tree_path` 的绑定** → 必须跳过（out of
  scope），**绝不** append 到任何会导致「阻断所有树」的全局 `indeterminate` 聚合。
- 一个跨全库的「无法判定」结论不得再向上传导为「本树被占用」：只有对 `tree_path`
  确有绑定关系的 record 才可能产生 occupancy 判定。
- 本单自身绑定（`current_development_id`）不构成外来占用，跳过并继续扫描，语义保持
  不变（rc-3d12fbbe 回归不回归）。

### H-B — 结果终态即答案（result.json 为终态 = 不持有）

对「确实绑定到 `tree_path`」的 record，判定其终态时以**权威结果**为准：

1. 先读 `<dev_dir>/result.json`（`RESULT_FILE`，g1 即 `<dd_root>/<dev>/result.json`）。
   顶层为 JSON object 且 `terminal` 为非空字符串 → 该单已终态 → **不持有**
   （`in_flight=False`）。
2. `result.json` 缺失/坏档/无 `terminal` 时，退回读 `status.json` 的 `terminal`
   （`status.json` 只是可重建缓存，不是第二事实源）。
3. 两者都拿不到非空 terminal → 才按在飞处理（fail-closed）。

关键：**不得**因为 `status.json` 缺失/不可读就 fall back 成「无法判定」——只要有
终态的 `result.json`，答案就是「不持有」，绝不再判红。

### H-C — 判不定/判占用必须给机器可读理由

`detect_inflight_binding` 返回 `in_flight=True` 时（无论是命中真实的外来在飞绑定，
还是确属无法判定的 fail-closed 情形），返回体必须：

- 携带非空 `detail`：字符串里**同时**包含具体 `development_id` 与具体 `tree_path`
  （规范化后的树路径）；
- 携带非空 `repo_path`：即本次判定的树的规范化路径；
- 命中具体绑定单时 `bound_development_id` 非空。

`_detect_occupied_tree` 与 `harvest.py::intake` 必须把 `repo_path`、`detail`、
`bound_development_id` 原样落进 intake step 与 receipt，`detail` 与 `repo_path` 均
不得为空。占用 escalation 保持既有 `escalate == HARVEST_TREE_OCCUPIED_BY_INFLIGHT`
与 `writes_skipped == WRITE_STEPS`、零写动作的语义不变。

## Bidirectional Acceptance Criteria

两条都必须可红（未实现即测试失败），**只做①=拆掉 H8 保护，不收**。

### ① —— 无关单缺 record.json 且 result 终态 → 收割照常进行（H-A + H-B）

构造 `dd_root`，其中含一个**与本次收割的树无关**的发展目录，且该目录 **缺
`record.json`**、携带终态 `result.json`（`terminal` 非空）。再以真实 git 合成仓构造
本次要收割的目标树（如 h8 现有 `_h8_target_fixture` 的 linked worktree `<target>`）。

断言：

- `DefaultHarvestOps().detect_inflight_binding(tree, dd_root)` 返回
  `in_flight is False`、`bound_development_id is None`；
- 编排层 `run_harvest` **照常进行**，`outcome` 不是 `escalated`，且不以
  `HARVEST_TREE_OCCUPIED_BY_INFLIGHT` 拒绝；intake step `ok` 为 True。

未修复时（现网缺陷）该 fixture 必红：`detect_inflight_binding` 因缺 record.json 的
无关目录返回 `in_flight=True` → 收割被误 escalate。

### ② —— 本次要动的树确被另一在飞单绑定 → refuse+escalate 且 detail 含该单 id 与树路径（H-C）

构造 `dd_root`，其中另一发展（如 `dev-fg-OTHER`）的 `record.json` 的 `repo_path`
绑定到本次要动的树，且其终态为**在飞**（`status.json` 无 `terminal` / 空，且无终态
`result.json`）。

断言：

- `detect_inflight_binding` 返回 `in_flight=True`、`bound_development_id ==
  "dev-fg-OTHER"`；
- `run_harvest` `outcome == escalated`，intake step：
  `escalate == "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"`；
- `detail` 非空且 **同时包含 `dev-fg-OTHER` 与 `<target>` 树路径**；
- `repo_path` 字段非空；
- 写步骤一个没跑（`writes_skipped >= WRITE_STEPS`，无 worktree_cherry_pick /
  pr_squash_merge / ff_only_pull 的 ok:true），`<target>` 一字未动（HEAD / porcelain /
  哨兵字节 / 目录仍在）。

未修复时（若有人为了放行①而直接删掉占用判定）该 fixture 必红：真正的外来在飞占用
被漏检，树被去写。这是①不被滥用的护栏。

## Minimal Implementation Scope

1. 改 `DefaultHarvestOps._terminal_of`（或等价读口）使其先读 `result.json` 的
   `terminal`（H-B），`status.json` 退居缓存兜底；返回一个「是否终态 / 是否可判定」的
   机器可读结构。
2. 改 `DefaultHarvestOps.detect_inflight_binding`：
   - 缺/坏 `record.json` 的子目录 out of scope，跳过（H-A），不再进全局
     `indeterminate`；
   - 对绑定命中的 record 先走 H-B 的终态判定，再决定在飞；
   - 返回体在 `in_flight=True` 时必带非空 `detail`（含 dev id + tree path）与非空
     `repo_path`（H-C）。
3. 改 `harvest.py::_detect_occupied_tree` 与 `intake` 把 `repo_path`/`detail`/
   `bound_development_id` 完整落进 step 与 receipt（H-C），不丢字段。
4. 在 `tests/test_harvest.py` 增补两条可红判据（① 与 ②），含真实 git 合成仓与缺
   record.json + 终态 result.json 的目录形态；保留既有 H8 occupancy 用例（含
   rc-3d12fbbe 排序遮蔽、只读判据、本单在飞不阻断）全部不回归。

不改变：占用 escalation 的零写动作语义、`HARVEST_TREE_OCCUPIED_BY_INFLIGHT` 码、
allowlist 写门、H7 写前闸、M3 分支占用拒绝、以及 `detect_inflight_binding` 只读
（零 rmtree / worktree remove / reset / checkout / clean，不另造所有权账本）。

## Executable Acceptance

```dd-acceptance
uv sync --frozen
make verify
```

Acceptance 完成仅当 `make verify`（lint + 全量 pytest + conformance）含上述两条新增
判据且绿色，且所有既有 harvest / dd 测试不回归。本开发不部署、不重启、不触碰任何
生产 checkout。

## Delivery Constraints

所有业务码编写与 review 一律交 dev-dispatch。Git 检查、H0 构造与验收执行仅在
`/data/worktrees/` 下独立 worktree 进行；生产主 checkout
`/data/code/self/fleet-graph` 严禁 checkout/switch/reset/建分支。在飞的两张闸卡
`dev-fg-3369ceda52d5`（wiki-trigger v2）与 `dev-fg-29ba21ec70cf`
（roster-retired-set）本轮不得 rebase、不得在其 worktree 上做任何改动，留待监督面
审闸、等反应器自收割。
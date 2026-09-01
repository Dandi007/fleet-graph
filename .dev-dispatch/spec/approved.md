# H8 收割并发安全门——动树前确认目标树未被别的在飞单绑定，否则拒绝+escalate 且一字不动

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree，隔绝不碰生产主 checkout）。
- 归属：收割侧（`supervise/harvest.py` + `harvest_ops.py`），不驳回。理由照录：dd 侧的 `_refuse_foreign_binding`（`dd/control_plane.py`，读 worktree HEAD 里 `.dev-dispatch/development.json` 的 `development_id`）与 `record.json.repo_path`、`status.json.terminal` 已经是「工单↔worktree 绑定 + 在飞/终态」的权威事实；缺的是收割这条会动树的链在动树前根本不读它。所以修在收割侧，且只能用 dd 既有事实，不另造第二套所有权记账。
- 类别：缺陷修复（收割链并发安全缺口，数据丢失）。与 H1/H2/H3/H5/H6/H7 同族，但后果最重——前面是假绿/污染，这个是直接销毁别人未提交的产品改动（goal.md 顶部 2026-09-01 23:5x 🔴 立案：`dev-fg-cfe509fa9c23` 的在飞隔离工作树被并发 harvest 中途劫持，`.dev-dispatch/**` 被整棵删、分支从 main 切到 `harvest/cfe509-batch-a`、未提交的 10 个 spec 被 reset 抹掉）。

## 根因（已实读，非推断）

收割链对「什么树能动」没有互斥/归属检查。现有危险写原语全部落在 `supervise/harvest_ops.py`：

1. `worktree_cherry_pick`（L321-325）先 `shutil.rmtree(worktree_root, ignore_errors=True)` 再 `mkdir` 再 `git worktree add --detach <worktree_root> <default_branch>`——若 `worktree_root` 撞上（或被解析成）一张别的在飞单的隔离工作树，整棵树连同未提交改动被 `rmtree` 物理抹掉、再 checkout 换分支。
2. `build_harvest_tip`（L405-409）同款 `rmtree` + `worktree add --detach`。
3. `remove_worktree`（L439-441）`git worktree remove --force <worktree_root>`，失败兜底 `shutil.rmtree` + `git worktree prune`——同样是「无视归属直接删树/清注册」。
4. 编排层 `resolve_canonical_repo` 把 record 的 `repo_path`（每单一次的 linked worktree）解析成 canonical 后，`ff_only_pull`/`deploy`/`verify_real` 全作用在 canonical 上，但全程没有任何一步去核对「这条链要动的树是不是正被另一张在飞单占用」。

关键事实：dd 侧已有一等公民的绑定事实可用——`<dd_root>/<development_id>/record.json` 的 `repo_path`（=该工的 worktree 绑定，`worktree_path` 同源于此）、`<dd_root>/<development_id>/status.json` 的 `terminal` 字段（空/缺失=非终态=在飞，非空=终态 complete/failed/refused/bounds/fault），以及 worktree 自身 HEAD 里提交的 `.dev-dispatch/development.json:development_id`（`_refuse_foreign_binding` 用的就是它）。收割只需读这些，不发明新账本。

## 交付 A：ops 层 occupancy 只读口（机械层，Guard D 豁免）

`src/fleet_graph/supervise/harvest_ops.py`（`HarvestOps` 协议 + `DefaultHarvestOps`）：

新增纯读口 `detect_inflight_binding(self, tree_path: Path, dd_root: Path) -> dict[str, Any]`，返回机器可读 `{"bound_development_id": str|None, "in_flight": bool, "detail": str}`（字段名可微调但语义必须闭合）：

1. 规范化 `tree_path` 为绝对路径（`Path(...).resolve()`，复用 `_resolved` 思路）。
2. 枚举 `<dd_root>/` 下每个 `*/record.json`（列表读，只读 JSON）。对每条 record，读 `repo_path`（=worktree 绑定），规范化后与 `tree_path` 比对：
   - 相等 → 命中绑定；
   - 不等时，若 record `repo_path` 是 linked worktree，用 `git rev-parse --git-common-dir` 解析其 canonical，判其 canonical 是否等于 `tree_path`（覆盖 record 直接指向 canonical 与 worktree→canonical 两种情况）；
   - 任一条 record 不可读/坏档 → 记为「无法判定」，不静默跳过。
3. 命中绑定（含自身归属解析命中）时，读该 development 的 `<dd_root>/<id>/status.json` 的 `terminal` 字段：空/缺失/falsy → `in_flight=true`（created/running/awaiting_gate/interrupted 都算在飞）；非空 → `in_flight=false`（终态，含 complete，这是正常收割目标，不拒绝）。status.json 缺失/不可读 → 保守按 `in_flight=true` 处理（fail-closed）。
4. 任何非本单的 development 命中且 `in_flight=true` → 返回 `{"bound_development_id": <该id>, "in_flight": True, "detail": ...}`。没有任何在飞单绑定 → `{"bound_development_id": None, "in_flight": False, "detail": ""}`（含「只被终态单绑定」也在 False 侧）。
5. 本方法只读，零 `rmtree`/`worktree remove`/`reset`/`checkout`/`clean`；绝不落任何新账本文件——所有输入都来自既有 `record.json`/`status.json`/git 读口。

「不另造所有权记账」的硬判据：本交付只 `open/read` dd 既有文件 + `git rev-parse --git-common-dir`，不写、不建、不登记任何 ownership 文件。

## 交付 B：编排层动树前先查 occupancy，命中即拒绝+escalate

`src/fleet_graph/supervise/harvest.py`：

1. 在 `intake` 解析出 `repo`（canonical）的同时，把 record 的 `repo_path`（工作树路径）保留进 `HarvestState`（如 `record_worktree: str`，由 `_resolve_repo` 一并返回，或新增一个并列读口）。这是该链要消费的那棵树的归属锚点，不能丢。
2. 在 `intake` 内（`_resolve_repo` 成功之后、进入 `gate` 之前）调用 `deps.ops.detect_inflight_binding(Path(record_worktree), deps.dd_root)`；同时对解析出的 canonical `repo` 也调用一次 `detect_inflight_binding(repo, deps.dd_root)` 与 `detect_inflight_binding(worktree_root, deps.dd_root)`（`worktree_root = deps.thread_dir(...)/worktree`）——凡是要去 `rmtree`/`worktree add`/`worktree remove`/`pull`/`deploy` 的树，动之前都过一遍。
3. 任一调用返回 `in_flight=True` 且 `bound_development_id != 当前 development_id`：立即
   - `intake` step `ok:false` + `escalate="HARVEST_TREE_OCCUPIED_BY_INFLIGHT"` + 附带 `bound_development_id`/`detail`（机器可读）;
   - `outcome = OUTCOME_ESCALATED`，经既有 `after_intake`（intake 早退 → receipt）直接收尾，不进入 gate 及其后任何写节点。
4. 命中 `in_flight=False`（仅终态绑定）或 `bound_development_id is None`（无绑定）→ 放行，走既有链，行为与今天零回归。
5. 禁止备份-恢复绕过：交付物里不允许出现「动树前先 `cp -a`/`tar` 备份、动完再恢复」的补丁路径。唯一合法形态是「读 dd 事实 → 拒绝 → 不动」。写前闸（H7）与本门不互斥：本门在更早节点拦，拦不到时 H7 仍在位。

## 交付 C：测试（tests/test_harvest.py；真 git 合成 fixture，禁触真网/生产 checkout）

1. 阴性 fixture（关键，goal.md 判据，不可省略）：
   - 真实 git 合成仓，建一棵「目标工作树」`<target>`（linked worktree 或独立 init 皆可），在其中放一个未提交的哨兵文件（内容已知字节串），记录 `git rev-parse HEAD`、`git status --porcelain` 与哨兵文件字节快照。
   - 构造 `dd_root`：`<dd_root>/dev-fg-OTHER/record.json`（`repo_path = <target> 或 <target 的 canonical>`，`remote_url` 等按 record 必需字段补齐）+ `dev-fg-OTHER/status.json`（`state="running"`、`terminal=""`，即在飞）；另有 harness 自己的 `dev-fg-SUBJECT/record.json`（`repo_path` 指向 `<target>`，作为本次 E5 要收割的单）。
   - 跑 harvest（fake ops 注入 `detect_inflight_binding` 真读 dd_root，或直接注入 `DefaultHarvestOps` 读合成 dd_root）→ 断言：
     a. `outcome == OUTCOME_ESCALATED`；
     b. 存在 step `ok:false` 且 `escalate=="HARVEST_TREE_OCCUPIED_BY_INFLIGHT"`（或等价机器可读 escalate 码）、`bound_development_id=="dev-fg-OTHER"`；
     c. `writes_skipped` 含 pr_squash_merge/ff_only_pull/deploy（写步骤一个没跑），且 steps 里没有任何 `worktree_cherry_pick`/`pr_squash_merge`/`ff_only_pull` ok:true；
     d. `<target>` 的 HEAD 与工作区一字未动：`git rev-parse HEAD` 不变、`git status --porcelain` 逐字节相同、哨兵文件字节逐字节相同、未被 `rmtree`/`worktree remove`（目录仍在、文件仍在）。
2. 终态绑定即放行（区分在飞）：同一 fixture，把 `dev-fg-OTHER/status.json` 的 `terminal` 改为 `"complete"` → 断言 `detect_inflight_binding` 返回 `in_flight=False`，harvest 照常放行走既有链。
3. 无绑定即放行：`dd_root` 无任何外部 record 指向 `<target>` → `in_flight=False`，正常收割。
4. 只读判据（不另造账本）：fake ops 记录 `detect_inflight_binding` 触发的调用面，断言只发生了 `open/read` 类读操作与 `git rev-parse`，没有任何写文件/建目录/登记动作。
5. 既有用例零回归（`make verify` 全绿；H1-H7 相关 test 一个不改语义）。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py` + `harvest_ops.py`（+ 如必需 `tests/test_harvest.py`）；不触 `/data/fleet-graph/supervisor/harvest-allowlist.json`、不改 deny-all、不改判据、不动 SOP_STEPS 枚举、不改 E5/E6/E7 词表。
- 不另造所有权记账：occupancy 唯一数据源 = dd 既有 `record.json.repo_path` + `status.json.terminal` + worktree HEAD 的 `.dev-dispatch/development.json`；禁止新建任何 ownership/占位文件、禁止写 dd_root。
- 禁止备份-恢复绕过：不许「`cp`/`tar` 备份→动树→恢复」；唯一合法形态是「动前读事实→拒绝→一字不动」。
- 失败/无法判定一律保守 escalate（fail-closed），绝不静默放行、绝不带病继续。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读，禁 checkout/switch/reset/detach。
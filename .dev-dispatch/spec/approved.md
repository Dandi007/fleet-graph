# H9 收割反应器 verify_real 真机验证指令按目标仓解析（与 run_verify 共用同一机械口）

## Goal

收割反应器 deploy 后的 `verify_real` 节点仍硬编码 argv `["make","verify"]`，而
`run_verify`（交付 A.1）早已按目标仓解析 verify 指令。真机触因：fleet-sentinel 是
uv 管仓（有 `pyproject.toml` + `uv.lock`，无 `Makefile`），收割整链 intake→verify→
PR merge→deploy 全绿，但 `verify_real` 用 legacy `make verify` 在真机 deploy 后退出 2
（make 无 `verify` 目标），postconditions 判红 → outcome=escalated，序② DoD
「verify_real 退出码 0」一项落空，缺一不算真收割。

根因（已实读 `src/fleet_graph/supervise/harvest.py`，非推断）：`verify()`（run_verify）
节点已通过 `deps.ops.resolve_verify_argv(worktree)` 按目标仓解析指令，并对显式
`deps.verify_argv` 覆盖做区分；但 `verify_real()` 节点直接
`deps.ops.verify_real(deps.verify_real_argv, repo, merged_head)`，其中
`deps.verify_real_argv` 默认 `DEFAULT_VERIFY_ARGV = ["make","verify"]`，从不解析。

## Contract

### 共用机械口（唯一事实源）

`verify_real` 与 `run_verify` 必须共用 `HarvestOps.resolve_verify_argv(path)` 这一
机械口（实现见 `DefaultHarvestOps.resolve_verify_argv` → `_resolve_verify_argv`），
解析规则一字不改：

1. 目标仓根目录 `Makefile` 含 `verify` 目标 → `["make","verify"]`；
2. 否则目标仓根目录存在 `pyproject.toml` / `uv.lock` → `["uv","run","pytest","-q"]`
   （repo-canonical 全量套件）；
3. 否则 → `(None, "no resolvable verify command")`。

### 编排层改动（仅 `supervise/harvest.py`，`verify_real` 节点）

1. `verify_real` 节点在跑真机验证前，解析指令：
   - 若显式 `deps.verify_real_argv` 且不等于 legacy 默认 `DEFAULT_VERIFY_ARGV` →
     直接采用（测试/运维注入覆盖，行为不变）；
   - 否则调 `deps.ops.resolve_verify_argv(repo)`（`repo` = state 的 canonical 目标仓，
     pull 之后已位于 merged head；纯读，不执行命令）。返回 `argv` 就用它；返回
     `(None, detail)` → 该步如实 `ok:false` + 机器可读 `detail`（`no resolvable
     verify command`）→ `outcome=escalated`，**绝不硬跑 `make verify` 制造误导性 2**。
2. step 记录 `argv` 字段 = 最终实际采用的 argv（不再恒等于 `["make","verify"]`）。
3. `verify_real` 剩余语义不变：`EXIT_HEAD_MISMATCH` 时记「HEAD 与已合并 commit 不一致」
   detail、沉淀 `verify_real_exit_code`、postconditions 仍以 exit 0 为通关。

## Bidirectional Acceptance Criteria（双向可红）

### 阴性（修复前必红）

合成一个 uv 管仓（无 `Makefile`，有 `pyproject.toml` + `uv.lock`）作为目标仓：

- 未修复：`verify_real` 恒用 `["make","verify"]` → 退出 2 → escalate（DoD 缺失项）；
- 修复后：`resolve_verify_argv(repo)` 返回 `["uv","run","pytest","-q"]`，`verify_real`
  step `argv == ["uv","run","pytest","-q"]`（**非 make verify**），fake
  `verify_real` 退出 0 → harvested；或解析失败路径 → escalated 且 `detail` 非空、argv
  非 `["make","verify"]`。

### 反向不抖动（Makefile 仓行为不变）

目标仓根目录 `Makefile` 含 `verify` 目标 → `resolve_verify_argv` 仍返回
`["make","verify"]`，`verify_real` still exit 0（行为与现状一致，无回归）。

### 显式覆盖不抖动

`deps.verify_real_argv` 显式配置为非默认值 → 仍直接采用（覆盖优先，同 `verify`
节点的 `deps.verify_argv` 语义）。

## Minimal Implementation Scope

1. 只改 `src/fleet_graph/supervise/harvest.py`（`verify_real` 节点，约 ≤30 行，镜像
   `verify()` 节点的 argv 解析分支）、`harvest_ops.py`(如需，不加新机械口——复用
   `resolve_verify_argv`) 与 `tests/test_harvest.py`。
2. 测试新增于 `tests/test_harvest.py`（编排层）：
   - 阴性：fake ops `resolve_verify_argv` 返回 uv pytest → `verify_real` step argv 为
     uv pytest 且 exit 0（harvested）；fake `verify_real` 不使用 make。
   - 解析失败：`resolve_verify_argv` 返回 `(None, "no resolvable verify command")` →
     `verify_real` step ok:false + detail + `outcome=escalated`，且不产生误导性 exit。
   - 反向不抖动：Makefile verify 目标（或 fake 返回 make verify）→ still `["make","verify"]`
     exit 0。
   - 显式覆盖：`verify_real_argv` 非默认 → 直接采用。
3. 不复用/不新增第二解析，不碰 `harvest-allowlist.json`，不造收割单，不改 E5/E6/E7
   词表，不改 H7/H8/M3 分支占用语义。

## 可复现验收

```dd-acceptance
uv sync --frozen
make verify
```

Acceptance 完成仅当 `make verify`（lint + 全量 pytest + conformance）含上述新增判据
且绿色，所有既有 harvest 测试零回归。本开发不部署、不重启、不触碰任何生产 checkout。

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py`（+ 如需 `harvest_ops.py`）与
  `tests/test_harvest.py`。
- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁
  checkout/switch/reset/detach/建分支/验证。
- 不触 `harvest-allowlist.json`、不改判据、不自造真实收割单、不重派已 complete 的单
  （dev-fg-3369ceda52d5 / dev-fg-29ba21ec70cf / dev-fg-49bcbc00b4df）。
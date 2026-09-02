# Spec M2（wf-8d9737）· 裁决即唤醒 + dd gate 上裁决接口 —— r2 换号重派单

> 状态：**已授权换号重派（r2）**。r1 `dev-fg-36c2d76baca7`（base 69faa8859，head a3a81dcd）全链路过闸五事件绿、六项取证全绿（board seq2498），但其产品补丁与 main `878ffb6e`（PR #250，wf-525fd4 两单先合）在 `src/fleet_graph/decision_mcp.py` **真冲突**（plain `git apply --check` 报 `patch failed: decision_mcp.py:56`；三路 apply 落 4 处冲突标记：约 :159/:473/:864/:897，#250 的 DdOwnerSource 常量区与 M2 的 DD_ROOT/投递路径互改）——收割方打不了干净补丁。r2 以 `878ffb6e0ce62d4a463786437dc46887f4071c4f`（2026-09-03 本轮 ls-remote 亲读）为新 target_base 重落同一行为，等价 rebase；闸内容已审过、不重批。**判据与 dd-acceptance 两条命令与 r1 逐字一致，零改动。**实现可把 r1 dd 分支（dd/dev-fg-36c2d76baca7 @a3a81dcd）当起点搬运，也可重做；**decision_mcp.py 四处冲突的解决与全部代码编写/评审归 implement/review 阶段**，spec 只钉行为与判据。与 design.md 冲突以 design.md 为准。

## 换号说明（为什么是 r2）

- 上一单 `dev-fg-36c2d76baca7`（target_base=`69faa8859`，head `a3a81dcd`）已过全链五事件 + 六项取证（三方验收逐字相等 / diff 未越 spec / 零测试删除 / 亲跑验收 9 passed + make verify 2604 passed 全绿 / 变异枪两发双红即复原 / S9 增量 +9 零绿→红），但补丁与 main 在 `decision_mcp.py` 真冲突不能干净收割（机械事实见上，探针 worktree 逐字回显已落 progress）。
- r2 与 r1 的行为、判据、dd-acceptance 完全一致；唯一差异是 base 换到 878ffb6e 并吸收 #250 的 decision_mcp.py 改动（wf-525fd4 的 MCP 面工作，注意与其「对账视图可复用本单新路径事实」的交界，不重复造）。
- 下节挂接点行号是 69faa8859 基准；878ffb6e 上行号会漂，**实现请按符号定位（函数/常量名），不要按行号**。

## M2 挂接点（对齐 M1 r2 实产，基线 commit `0234b0b`，2026-09-03 亲核）

M1 r2 已把以下实体落为实产；M2 在这些点上接线，**不另起炉灶**：

- **裁决投递面**：`src/fleet_graph/decision_mcp.py:533` `decision_deliver(line, decision, reason)`——同步投递的现产入口，M2 把 target 扩到 dd 单（`dev-fg-<id>` / target.kind=development）。
- **驻停快照（「投递即清驻停」的对象）**：`.scheduler/<wf>.json` stall-state 的 `parked_*` 快照（`daemon.py:118-129` `_EMPTY_PARK_FIELDS` 族：parked_run_id / parked_at / parked_goal_revision / parked_inbox_available / parked_dd_development_id，M1 增末项）；`_wake`（`daemon.py:1321`）清快照时**保留** `park_considered_run_id` 的防吞语义不得破坏。
- **唤醒事实的现产形态**：
  - dd 驻停：`_dd_check_wake`（`daemon.py:1276` 附近）现产只认 `self.dd.dd_fact(dev_id)` 的事实——`src/fleet_graph/scheduler/wake.py` 的 `classify_dd_fact` 把 `<dd_root>/<dev_id>/status.json`（`DEFAULT_DD_ROOT=/data/fleet-graph/dd`，`daemon.py:109`）投影为 `"awaiting_gate" | "terminal" | None`。
  - 决策驻停：`_check_wake`（`daemon.py:1166`）**第一优先已消费「wake fact 4」**——stall 态的 `dispatched_decision_consumed_at` 字段（裁决送达即写、调度器纯读），命中 → `woken:decision_consumed`；漏送达有 `decision_wake_stall` 红标注（阈值 `decision_wake_stall_ticks=3`，`daemon.py:334`）。
  - M2 的「投递即唤醒事实」必须落成**调度器下一 tick 能机械消费的事实**：可复用 consumed-at 字段形态、可扩 `classify_dd_fact` 词表、也可走「裁决→单据 resume/终态→dd_terminal 自然点火」的自然路径——**形态归实现**，判据只认「投递后下一 tick 点火 + 零吞」。
- **dd 锚**：terminal record 的 `dd_development_id`（`graphs/goal_line.py:291` Verdict 字段；`daemon.py:568-570` 读取）；`Verdict.waiting_on ∈ {decision, external, dd, none}`。
- **decided_by 校验对象**：该单 `record.json` 的 `dispatched_by`（本线单 = `wf-8d9737`）。
- **状态词投影**：`state/run_artifacts.py` `derive_line_state`（terminal+waiting_on → 六词表）。

## 要交付的行为（全部在 fleet-graph 仓）

1. **target 扩到 dd 单**：`decision_mcp.py` 的 `decision_deliver` 接受 dd 单目标（`dev-fg-<id>` 形态或 target.kind=development），对 `awaiting_gate` 的单投 `APPROVE|REJECT`：
   - 单据走既有 gate 释放路径 resume（APPROVE → merge 段继续；REJECT → 单据拒绝终态）；
   - **投递即清驻停**：清 `.scheduler/<派单线>.json` 的 `parked_*` 驻停快照（见挂接点节，防吞语义保留）；
   - **投递即唤醒事实**：落成派单线下一 tick 可机械消费的事实（形态归实现，判据见下）。
2. **decided_by 校验（principal）**：对 dd 单的投递，调用方 principal 必须等于该单 `record.json.dispatched_by`；不等 → 拒绝码 `NOT_DISPATCHING_LINE`，单据状态不变。人/监督面对其余目标（线、升报问题）的投递路径不受影响。
3. **MAYBE 沿用现有阴性**：`decision ∉ {APPROVE, REJECT}` 在调用点报错（现状 `_validate` 已拒，保持并有用例守着）。
4. **零吞**：新路径（target=dd 单）的投递要么 consumed/refused 当场可见，要么登记失败即报错；`/v1/decisions`（或其 MCP 视图）里不得出现 state=swallowed 的新增条目；与 M1 的 `decision_wake_stall` 漏送达红标注衔接（不拆除、不绕过）。

## 判据（goal.md §二 M2 原文内联）

- 阳性：对一张 `awaiting_gate` 的单投递 → 单据 resume + 派单线下一 tick 点火；新路径零 `swallowed`。
- 阴性：用非派单线身份投递 → 拒绝码 `NOT_DISPATCHING_LINE`，单据状态不变；投 `MAYBE` → 调用点报错。
- design.md §8「送达即唤醒」「越权投递被拒」两行由此具备变绿条件（「DD 闸不经人」整行绿归 M3，本单不做线自判取证义务）。

## 测试与验收

- 新增 `tests/test_m2_dd_gate_delivery.py`：覆盖阳性（resume+点火+零吞）、两发阴性（越权拒 + MAYBE 报错）、principal==dispatched_by 校验。**零测试删除**。
- 既有 decision_mcp 用例更新到新语义不算删除。
- 全量绿基线：自本单 base（878ffb6e）起跑，判据「不把绿的打红」；S6 代理陷阱照旧——验收在 dd unit 环境无 SOCKS env。

## 边界

- 只动 fleet-graph 仓（decision_mcp.py、scheduler 唤醒事实接线、相关 state 写入）；不改 decision-bridge 本体（M8 退役项）、不做 M3 的线自判五项取证、不做 line_message（M4）、不动 dd 引擎 gate 内部判定逻辑本身（本单只接「裁决 → 释放 + 唤醒」的通道）。
- 与 wf-525fd4「M4 MCP 面可用性判定口 + 裁决口径改对账」不重叠：其对账视图可复用本单新路径的事实。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m2_dd_gate_delivery.py'
bash -lc 'make verify'
```
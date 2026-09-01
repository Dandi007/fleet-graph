# M5 线复活机制——`terminal_done` 的一等公民合法重开语义 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree，隔绝不碰生产主 checkout）。
- 归属：**`scheduler/`**。goal.md 监督面裁决已显式把此件从 `supervise/` 划给本线定死；本线**接受归属不驳回**，理由照录：本线是唯一在 fleet-graph 编排核心还活着的线、且 M3 五轮证明了判据纪律。若实现方研判需改动越过 scheduler/ 边界（如必须动 `graphs/goal_line.py`、`graphs/runner.py` 的 coord 输入透传），属本 spec 明示范围的一部分，不另开单。
- 类别：纯增量（新增 scheduler/ 内的一等公民复活语义 + 复活因由透传 + 审计留痕），**不改 E3 语义**（checkpoint 仍是终态裁决源、`terminal.json` 仍是派生 fallback，绝不反向压过 checkpoint）、不改 `decide()` 现有 judge 顺序中 `done→TERMINAL_DONE` 之外任何分支、不改 parking/wake 语义、不改 harvest/allowlist。
- 交付：代码与评审全委 dev-dispatch；worker 只写 spec 与取证。
- 前置事实：goal.md 真机对照实验里 `wf-66300e`(terminal_done) 被 goal.md 直写 → `ignited:false, refusal:"terminal_done"`，同动作对 `wf-e6560a`(parked)→`ignited:true`。根因在同文件 `scheduler/ignition.py::decide` 第 136 行 `status.terminal == "done"` 直接拒 `Refusal.TERMINAL_DONE`，且 `scheduler/daemon.py::_next_generation` 对 `done` 刻意不 bump generation（`return current`）——两道闩合起来就是「闭卷=单向门」，parking 那套 wake 源（inbox/goal_revision/清 parked 字段）只覆盖 `blocked+waiting_on=decision`，永不覆盖 `done`。

## E3 现状（本 spec 必须守住的对偶，先钉边界）

`checkpoint_terminal.py` 里 checkpoint 是终态裁决源；`terminal.json` 是派生兼容视图 + 故障 fallback。`daemon.py::terminal_record` 在 checkpoint 配置时会以 `get_state` 为准：absent/stale/conflicting 的 `terminal.json` 不能改答案。已测（`tests/test_terminal_derived_view.py::TestCheckpointIsAuthoritative`）钉死：伪造/陈旧 terminal.json 推不翻 checkpoint。

M5 要补的是它的**对偶**：一个「从外部合法推翻 `done` 终态」的一等公民入口。且必须证明这个入口**没有**打穿 E3 的保护（阴性面）。

## 交付 A：一等公民复活入口（scheduler/，复用 seat_override + bump_line_generation 既有纪律，不发明第二套）

1. 新增 per-line 复活/吊销记录面，落在调度器自己的持久区 `<run_root>/.scheduler/`（与 `seat-overrides.json`、per-line stall-state 同区）。**审计字段 C1 同款、缺一拒写**：`who`（谁推翻）、`basis`（依据——goal.md 裁决块 id / board 决策 id / 消息引用，非 prose）、`generation`（推翻的是哪一代终态，机械数字）、`when`、`reason`（可选 prose 但 alone 不够）。字段缺失/空白 → 写前拒，绝不落盘（复用 `validate` 拒绝不修的模式）。
2. CLI 一级入口（`fleet-graph line revive …`，形如 `set-seat`）：`--who`/`--basis`/`--generation`（或 `--run-id`）必填；**写前预检**——目标线当前 checkpoint 权威终态必须真是 `done` 且 `generation`/`run_id` 与 checkpoint 记录匹配，否则拒（`refused: target not terminal_done` / `refused: generation mismatch`）。预检过了才写 revoke 记录 + bump generation（新代冷启动，fresh thread）。
3. daemon 每 tick 的**聚合路径**：在把 `done` 喂给 `decide` 进退化为 `TERMINAL_DONE` 之前，先看有没有「匹配当前 checkpoint 记录的 `done` 终态」的合法 revoke 记录。匹配 → 清除 `done` 闩 + 强制 generation bump（`_next_generation` 对此情形必须 bump，不能再 `return current`），本 tick 即可重新 ignite；不匹配（stale / 指向别的代 / checkpoint 里没有 done）→ **inert**，保持 `done`。revoke 生效后该记录清掉或标记 `consumed`，不能一遍遍重复触发。

**不是 hack 的机械判据（负向自我校验）**：
- 复活信息**只**存在于 `.scheduler/` 新面，**绝不写** `terminal.json`、**绝不**逆向 checkpoint 内部结构手改 thread 状态——复活=新 generation 冷启动（复用既有 `bump_line_generation`），旧 `done` thread 原样保留可审计。
- `terminal.json` 仍不能反压 checkpoint：任何「往 terminal.json 写 not-done / 写 revived」的动作都不构成复活源。

## 交付 B：复活留痕且可审计（被叫醒的线下一轮 rehydrate 能读到因由）

1. 复活因由透传到线进程：launcher 把 valid revoke 的 `who/basis/generation/reason` 传下去，runner/CLI 侧新增一个 `revival` 字段注入 round-1 coordinator input（与既有 `prior_terminal` 同 envelope，`prior_terminal` 仍携带旧 `done` 终态让线知道「推翻的是什么」）。线上一条线从 done 叫回来，round-1 的 coordinator 能机械读到「是谁、凭什么、推翻哪一代」。
2. 调度器侧观测面：每代/每 tick 的 observe log / tick result 明示 revoke 事件（`revoked:<who>:g<gen>`），调度器持久区存 consumed 的 revoke 记录不删（追加式历史，与 stall-state 同区，审计可回溯）。bump 后的 generation 持久化到 stall-state（既有 `bump_line_generation`/`generation_of` 已覆盖）。

## 交付 C：阴性面（缺这半不收）

1. **伪造/陈旧 terminal.json 推不翻 checkpoint 依旧成立**：对 checkpoint 权威为 `done` 的线，把 terminal.json 伪造成 `blocked`/`not done`/`revived`，断言 `terminal_of` 仍是 `done`、`tick` 仍 `Refusal.TERMINAL_DONE`，复活**不会**被触发。
2. **伪造/陈旧 revoke 记录 inert**：revoke 记录 `generation` 与 checkpoint 实际 done 代不一致、或 `basis` 为空、或线当前 checkpoint 非 `done`，断言不 ignite、不 bump、不留「已复活」痕迹。
3. **revoke 记录缺 C1 审计字段拒写**：缺 `who`/`basis`/`generation` 任一 → 写前拒绝，盘上无该记录。
4. E3 既有 `TestCheckpointIsAuthoritative` 零回归：本次改动后全红即打穿，全绿才算守住。

## 交付 D：测试（合成 fixture / fake reader，禁触真网）

1. 正向：fake checkpoint `done` + 合法 revoke（匹配 generation）→ `decide` 不返回 `TERMINAL_DONE`、generation bump、line 可 re-ignite；round-1 coord input 里 `revival` 与 `prior_terminal` 都在场。
2. 负向：见交付 C 全列；`make verify` 下 `test_terminal_derived_view.py` / `test_parking.py` / `test_scheduler_daemon.py` / `test_ignition`（如存在）零回归。
3. 单测覆盖 `decide` 顺序不变（除 done 分支新增 revoke 放行外，其余 refusal 顺序绝不动）。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据（goal.md M5，四项硬约束全纳）

1. 归属 `scheduler/`（本 spec 接受不驳回；若实现方认为必须改判归属，须在 review 里书面说明该归谁及理由，不许默默不做）。
2. 不 hack：`terminal.json` 不反压 checkpoint；不逆向 checkpoint 手改 thread 状态；复活=新 generation 冷启动的一等公民语义。
3. 复活留痕可审计：谁/依据/哪一代终态全部机械入档，被叫醒线下一轮 rehydrate 可读到因由（round-1 coord input `revival` + `prior_terminal`）。
4. 阴性面：伪造/陈旧 terminal.json 仍推不翻 checkpoint、伪造/陈旧 revoke 记录 inert、C1 缺字段拒写——E3 保护不被本次改动打穿。

## 铁律

- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only，禁 checkout/switch/reset/detach。
- `terminal.json` 语义、checkpoint-authoritative 语义、`decide()` 非 done 分支、parking/wake、harvest/allowlist 一律不改；判据（goal.md 验收断言）只有用户能改。
- 复活入口必须默认拒绝（目标非 `done` 或 generation 不匹配 → 拒），绝无「未命中即静默放行」路径。
- 复活这一写动作的 gate 纪律与 seat_override C1 同款：缺审计字段拒写，绝不补造。
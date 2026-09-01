# G1 驻停基线竞态——goal 直写裁决被静默吞掉（parked_goal_revision 应记「线真正消费过的」revision，而非驻停登记那一刻的当前值）

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`scheduler/`（goal.md 顶部 2026-09-02 00:5x 🔴 立案）。裁决=独立成件，不并入 M5，理由照录：M5 spec 已冻结且在飞（dev-fg-bb5e555ecd12），只覆盖 `terminal_done` 一等公民复活语义，不含 `parked_goal_revision` 竞态；并件须改冻结 spec = §5e 末策重派。本件与 M5 同模块但不同缺陷面，工作树零重叠（M5 改复活链路，本件改 parking 基线 + 终态落盘字段）。
- 类别：缺陷修复（调度器 parking 唤醒基线语义缺陷，导致唯一可靠裁决信道静默失效），不改 checkpoint-authoritative、不改 decide() 顺序、不改 E3、不改 harvest/allowlist、不改 parking fail-open 纪律。

## 根因（已实读源码）

`src/fleet_graph/scheduler/daemon.py`：
1. `_establish_park`（L838/L845）在驻停登记那一刻读 `revision = self.wake.goal_revision(line.folder_id)`（`wake.py::goal_revision` = `fs_stat goal.md` 的 `content_revision`），并 `state["parked_goal_revision"] = revision`。
2. `_check_wake`（L892）每 tick 比较 `self.wake.goal_revision(...) != state["parked_goal_revision"]`，不等才 `_wake(..., "woken:goal_revision")`。

竞态窗口：`_establish_park` 发生在「线最后一次消费 goal 之后」。真机现场 wf-a6cfea：线最后读 goal=00:17 → 发 question note=00:22:24 → 监督面写 goal=00:24 → 调度器登记 parked_at 并把 `parked_goal_revision` 记成写完之后的哈希=00:28。于是「00:17→00:24 的改动」被吸进驻停基线，`_check_wake` 恒见 current==baseline 永不点火，线循环报 `refusal: parked_awaiting_decision` 无任何异常——裁决被静默吞掉。

即：`parked_goal_revision` 语义错在「登记时刻当前值」而非「线真正消费过的值」，二者窗口内任何 goal 改动都会被吞。

## 交付 A：终态落盘补「线消费过的 goal revision」机械字段（graphs/goal_line.py + state/run_artifacts.py）

1. 线在 coordinator 轮真正消费 goal.md 时（拿 `content_revision` 的那一次）把该 revision 随 `LineState` 流传；`finalise`（goal_line.py L770-779 调 `deps.artifacts.write_terminal(...)`）写终态时一并落进 `terminal.json`（新字段建议名 `goal_revision`，纯机械 hash 非 prose）。
2. `state/run_artifacts.py::write_terminal` 与 terminal 落盘 schema 同步补此可选字段；缺失不报错（向后兼容旧终态）。
3. 字段只随「线消费的 revision」更新：线没读到的新 goal 改动绝不回填进终态（line-consumed，不是 anytime-current）。

## 交付 B：scheduler 驻停基线改用「线消费过的 revision」（scheduler/daemon.py）

1. `_establish_park` 不再在登记时刻用 live `self.wake.goal_revision(...)` 当基线，改从 `terminal_record`（已读 run_id/waiting_on/at/pump_fault，同族补读 `goal_revision`）取「线消费过的 revision」作 `parked_goal_revision`。
2. 唤醒判定不变（L892）：`current != parked_goal_revision` → `woken:goal_revision`。基线改对后，窗口内 goal 改动（current 变、consumed 不变）自然点火。
3. 向后兼容 + 不破坏 fail-open（铁律）：终态缺 `goal_revision`（旧终态/异常）时不得静默锁线——降级为「无可靠基线→不 park，走普通 backoff」或等价保守路径，并记机器可读 `not_parked:no_consumed_revision`（或等价）。写明并测试该分支。
4. `_wake` 清基线语义（L898-913 清 `parked_goal_revision=None`）不变。

## 交付 C：阴性测试（必须能红）+ 反向不抖动（tests/ 合成 fixture，禁触真网/生产 checkout）

1. 阴性（本件判据，不可省略）：构造「线最后消费 rev=R0（终态 goal_revision=R0 且 at 早于写入）→ goal.md 写成 rev=R1 → 调度器在 R1 之后才 _establish_park」的 fixture。断言：下一 tick 必须 `woken:goal_revision`（current R1 != baseline R0），绝不能永醒不了。（对照：未修复时基线记成 R1 → 恒 parked_awaiting_decision，测试红。）
2. 反向不抖动：线唤醒后真正消费 R1、再 block 且终态 goal_revision=R1；current==R1==baseline → 断言不得重复唤醒（parked holds，无 woken:goal_revision）。
3. fail-open 回退：终态缺 goal_revision（或坏档）→ 断言不静默锁线（not_parked 或等价，backoff 可点火）。
4. 既有用例零回归：make verify 全绿；test_parking/test_scheduler_daemon/test_wake/test_ignition 语义不变；decide() 非 done 分支顺序绝不动；E3 TestCheckpointIsAuthoritative 零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据（goal.md 顶部立案四要素全纳）

1. 归属 scheduler/、独立成件不并入 M5（不驳回；若实现方认为应并件/另归他处，须在 review 书面说明理由，不得默默不做）。
2. 基线语义 =「线真正消费过的 revision」（line-consumed），非「登记时刻当前值」；窗口内 goal 改动不再被吞。
3. 阴性必须能红（心跳早于写入、驻停晚于写入→下一轮必点火）；反向（同一 revision 已消费→不重复唤醒，不抖动）。
4. 明确禁止把「再写一次 goal 让哈希变化」当修复；禁止任何「写两遍」绕过。

## 铁律

- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only，禁 checkout/switch/reset/detach。
- 只改 scheduler/daemon.py（+如必需 scheduler/wake.py）+ graphs/goal_line.py::finalise + state/run_artifacts.py::write_terminal + tests/；不触 decide() 非 done 分支、不触 E3、不触 harvest/allowlist、不动 parking fail-open 纪律。
- 新增字段纯机械 hash，禁 prose；判据只有用户能改；旧终态缺字段/坏档→fail-open 绝不静默锁线。
# fleet-graph scheduler：等待外部裁决(parked)的 run 不计入 no_progress streak spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：A 类调度缺陷（no_progress 退避不认裁决——裁决已落地、线仍被退避窗口拖住不点火）。监督面 2026-09-02 17:1x 立案。

## 1. 现象与真因（读源码坐实）

- 现象：线驻停 `blocked + waiting_on=decision` 等裁决；裁决落地 wake 后，线**下一 tick 不立即点火**，仍被 no_progress 退避窗口拖住（`ended N run(s) in a row without advancing a round; backing off ...`）。
- 真因（坐实）：
  - `scheduler/daemon.py::account_last_run`（streak 折叠，~805 行）：`advanced = rounds>0`、`finished = terminal=="done"`，`streak = 0 if (advanced or finished) else streak+1`。**`terminal="blocked"` 且 `waiting_on="decision"` 的 run 因 rounds 可能为 0 且非 done，被计为 zero-progress → streak+1**。
  - `scheduler/ignition.py`：`parked`（`terminal blocked + waiting_on=decision`）分支在 backoff 分支**之前**，驻停期每 tick 返回 `PARKED_AWAITING_DECISION`（正确）；但 streak 在 `account_last_run` 已先被 +1。裁决 wake 清 parked 字段后，下一 decide 落入 backoff 分支，带着被驻停 run 撑高的 streak → `Refusal.NO_PROGRESS` 退避。
  - 字段（坐实）：terminal record 含 `terminal`/`waiting_on`/`rounds`/`run_id`（`daemon.py::_terminal_json_record` 与 `checkpoint_terminal.to_record`，`waiting_on` 为机读枚举 decision|external|none）；stall-state `.scheduler/<wf>.json` 含 `board_question_note_id`（线是否真问过板）。

## 2. 修复方向（契约；方向自决，约束+判据如下）

1. streak 折叠对「等待外部裁决」的 run 复位：当 terminal 为 `blocked` 且 `waiting_on == "decision"`，视同 advanced/finished——`streak = 0`（不 +1）。语义：合法的人类等待不是 stall，不该喂出退避。
2. 判据两向：
   - **阳性**：线驻停等裁决（`blocked + waiting_on=decision`，问过板）→ 收到裁决 wake（清 parked 字段）→ **下一 decide tick 即 launch 点火、不落 NO_PROGRESS、无退避等待**。须真机回显（scheduler tick 日志 / `Refusal` 序列），**不得只靠单测断言**。
   - **阴性（防空转护栏不瞎）**：既未挂 question（`waiting_on != decision`）也未推进 round（`rounds == 0`）、`terminal != done` 的线**连续 3 次仍必须累计 streak 并退避**。删此阴性（把豁免放宽到「所有 blocked」或「所有 fault」）＝把防空转护栏做成瞎子，必红。

## 3. 判据（两向能红）

1. 阳性：造 `blocked+waiting_on=decision` 且问过板的驻停线 → 注入裁决 wake → 下一 tick 无 `NO_PROGRESS`/backoff、直接 launch。变异：去掉该豁免（恒 `advanced or finished` 才复位）→ 阳性用例红。
2. 阴性：连续 3 次 `rounds=0` 且 `waiting_on != decision` 的非 done terminal → streak=3 仍退避。变异：把豁免改成「所有 blocked 都复位」→ 阴性用例红。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_scheduler_daemon.py tests/test_parking.py
make verify
```

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree add 到 `/data/worktrees`；生产主 checkout 只读、仅 `git pull --ff-only`。
- 只改 streak 折叠的复位条件，不碰 ignition 的 parked/backoff 顺序语义。
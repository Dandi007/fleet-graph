# Spec M3（wf-8d9737）· 线自判闸 = 全舰默认路径（六项取证 + 回归基线必答字段）

> 状态：**在跑单 dev-fg-d9cd78e40c45 冻结的是折入 S10 前的谱（sha256:3cc92467…）；2026-09-03 09:1x 折入 S10 三条后为本谱，其终局后按 §5e 以本谱换号重派**。派单序钉在 M1、M2 合入之后（依赖 M1 的 `dd_awaiting_gate` 唤醒与 M2 的 `decision_deliver` dd 单路径；base 取派单时 main 头）。判据锚：design.md §6.2/§6.3/§6.4（D5：闸归派单线）、§8 行「DD 闸不经人」；goal.md §二 M3、S8（五步取证）、**S9（第六条，不可弱化）**、**S10（裁决送达落地三条，不可弱化，2026-09-03 08:1x 补）**、S7（wf-216dc3 G1 让位：闸权通用形态归本线，收割反应器改接 merge 段之后）。与 design.md/golden-order 冲突以后两者为准。

## 要交付的行为（全部在 fleet-graph 仓）

1. **线自判路径成引擎默认**：goal 线收到 `dd_awaiting_gate(dev_id)` 唤醒（M1）后，自动履行下列**六项取证义务**，全部通过后经 M2 的 `decision_deliver(dd 单, APPROVE|REJECT)` 投递；`decided_by` = 线 principal 且被校验等于该单 `record.json.dispatched_by`。**吸收 wf-216dc3 已收割的「线自判闸首批」既有形态（main `5a39d03`，PR #245），接着做、不另起炉灶**；该路径上线后为全舰默认：监督面不再逐单批闸，人只在 goal 级验收与升报回答出现（design §6.3）。
2. **六项取证义务 = gate 的必答字段**（引擎侧机械履行；缺任一项 → 投递被拒或被标记，用例必须能红）：
   1. **三方验收逐字相等**：spec 冻结验收 argv == record.json `acceptance_commands` == 阶段回执 command（机器比对）。
   2. **产品 diff 未越 spec 边界**：产品文件改动逐一对得上 spec 声明的交付面； `.dev-dispatch/`、`.dd-evidence/` 机器件除外。
   3. **零测试删除**：base..head 的 `--diff-filter=D` 为空（既有测试更新断言不算删除）。
   4. **亲跑验收**：线在 gate 侧亲自复跑冻结验收命令并留回显。
   5. **变异枪两发**：对产品做两处变异，冻结验收必须红；射后字节复原（sha/mode 校验）。
   6. **全量回归与放行前基线对比（S9，不可弱化；2026-09-03 加考题收紧）**：
      - **基线锚定该单冻结的 `record.json.target_base_commit`**（不是 gate 时刻的 main 头）——main 在单据运行期会漂移（实例：M1 的 daemon.py:93、M2 期间 #250 触 decision_mcp.py），按漂移 main 比对会把「收割冲突」误算成「回归」；base 漂移属收割次序/rebase 换号问题，不得混入回归增量语义。
      - **基线快照形态（机器可比对）**：在未打补丁的冻结 base 上、S6 清代理环境下跑 `uv run pytest -q` 全量，落「通过/失败/跳过计数 + 失败测试集合」两元组（gate 阶段现取或 create 时记录均可）。
      - **判据 = 红项集合不得扩大，且任一「绿→红翻转」即拒绝投递**（不是「现在是否全绿」；基线本身红不是本单的错）。原稿「红→绿翻转即拒绝」为笔误，红→绿是改善不是回归，2026-09-03 更正。
      - **flake 归因子规则**：红增量若为「净 base 上间歇红」的测试（实例：69faa8859 上 tests/test_supervisor_graph.py::TestKillRestartReAdopt::test_killed_supervisor_re_adopts_its_audit_run，2 次全量 1 红、隔离 4 跑 1 红），必须在净 base 上隔离复跑归因后才可放行，且归因证据（复跑结果）入载荷；不得静默忽略、也不得直接记本单回归。
3. **⚠️ 回归对比不得塞进 dd-acceptance**：回归是**闸的义务**，不是每张单验收命令的内容——判据冻结、只有用户能改，把全量测试塞进 dd-acceptance 会让判据随主干漂移而失去冻结意义（S9 原文警告，照办）。
4. **取证与结论落档**：六项结果模板化落 progress 与板卡 evidence note（可含各 digest），作为 `decision_deliver` 的 rationale 载荷。
5. **收割反应器改接 merge 段之后**（S7 分工）：闸 APPROVE → merge 段完成 → 收割；allowlist 语义改为「圈 `release/<line-id>` 可写仓」（release 分支本体是 M5，本单只把收割触发点从「闸后」改到「merge 后」，M5 未上线前保持现有 dd/<id> 分支语义不变）。
6. **裁决送达必须落地（S10 三条，不可弱；监督面 2026-09-03 08:1x 以 M2 成品真机抓洞后签入 goal.md §七 S10，三条逐字折入）**。事实背景（真机链条，每环已核）：对 `awaiting_gate` 单投 REJECT → `decision_mcp` 调 `dd.gate(resume=True)` 返回 `resume: True` → 回执 `status=delivered, outcome=consumed` 写「dd single resumed through its gate」→ unit 889ms 后 `75/TEMPFAIL` 退出（其 `--workspace` 路径不存在）→ 单据退回 `human_gate/awaiting_gate`，`gate_refused=None`、`events.jsonl` 无新事件——「`resume: True` 的含义是『我起了一个 unit』，不是『这条裁决被消费了』」，中间隔着一个会 889ms 就死的进程，而它死了之后没有任何人记一笔。监督面的整套闸纪律建立在信这个回执上：它成功一次、闸却没关，就是收割欠账与幽灵闸的生产机制；且 workspace 路径错的单不可能靠再投一次自愈。自判闸必须满足：
   1. **裁决的成功判据是「被消费」，不是「起了 unit」。** resume 之后必须回读单据状态，确认它**离开了 `awaiting_gate`**；没离开就报 `REFUSED`（带 unit 退出码），绝不报 `delivered/consumed`。
   2. **resume 失败必须在单据上留痕。** `gate_refused` 要写入拒绝原因与 unit 退出码，并追一条 `events.jsonl`。现在的形态是「进程死了，单据一个字没变」——事后只能靠翻 systemd journal 反推，我这次就是这么查出来的，这在全舰规模上不可行。
   3. **resume 前先校验 `workspace` 存在。** 路径不存在应当在启动 unit **之前**就报 `REFUSED`，而不是起一个注定 889ms 死掉的进程。顺带查一下这个日期后缀是怎么丢的（`development_create` 记的 workspace 与实际建的 worktree 名不一致），那是本缺陷的上游。

## 判据（goal.md §二 M3 + design.md §8 原文内联）

- 阳性：一个班次内本线派出的 dd 单，`decided_by` 100% 是本线 principal（design §8「DD 闸不经人」行）。
- 阴性：线漏掉任一项取证义务就投递 → 投递被拒或被标记，用例能红；第 6 条的阴性含四款：缺基线/增量字段、**绿→红翻转**（把绿的打红）、红项集合扩大（含基线本身红时再添新红）、以及**用漂移后的 main 当基线**（必须仍按冻结 target_base 比对）。
- 用例素材（真实失效事件，写进测试）：S9 实例——wf-525fd4 `dev-fg-cd44b133614e` 摘除 dev/gate/pump 三族工具但三族测试未动：基线 106 passed 全绿 → 打补丁 31 failed，而其冻结验收只跑自己的 12 条测试仍绿；五项旧义务全过、第六项必拦。
- 阴性（S10 红靶，真机单据不另造 fixture）：`dev-fg-36c2d76baca7`（M2 r1）此刻仍卡 `awaiting_gate`、workspace 路径已错——M3 落地后对它投一次裁决**必须**得到带原因的 `REFUSED`（而不是 `delivered/consumed`），并且单据上要能看见这次拒绝（`gate_refused` 有值 + `events.jsonl` 追加）。这就是这三条的验收现场。

## 测试与验收

- 新增 `tests/test_m3_line_selfgate.py`：六项义务逐项（含第 6 条四款：缺基线字段拒、绿→红翻转拒、基线红但红集未扩通过、**flake 唯一红增量→净 base 隔离复跑归因后放行且载荷留证**、**gate 时 main 已漂移仍按冻结 target_base 比对**）、漏项投递被拒、阳性路径（自判 APPROVE → merge 后收割触发）、principal 校验。**零测试删除**。
- S10 三条的阴性用例（红靶素材 = 真机失效事件 `dev-fg-36c2d76baca7`，2026-09-03 08:08 实测形态，不另造 fixture）：①错路径 workspace → resume 必须**在起 unit 之前**报带原因 `REFUSED`（不得触发起 unit、不得报 `delivered/consumed`）；②unit 起了但退出（实测 889ms `75/TEMPFAIL`）而单据仍在 `awaiting_gate` → 回读判据不满足，必须报 `REFUSED` 且**带 unit 退出码**；③每次 `REFUSED` → `gate_refused` 写入拒绝原因与退出码 + `events.jsonl` 追加一条（实测旧形态是「进程死了，单据一个字没变」——`gate_refused=None`、`events.jsonl` 无新事件，该旧形态的回执 `status=delivered, outcome=consumed` 属假绿，用例必须能红）。M3 落地后对真机 `dev-fg-36c2d76baca7` 补投一次裁决实测，作为三条的验收现场。
- 派单时 base 取 main 头；M1/M2 已合入（依赖其唤醒事实与 dd 单裁决路径）。

## 边界

- 只动 fleet-graph 仓（dd 引擎 gate 侧、goal_line 取证编排、scheduler/收割接线）；不做 line_message（M4）、不做 release 分支模型本体（M5）、不改 decision-bridge（M8）、不动 verify-lim.sh 判据（第 11 项会因本单变绿）。
- 本线自己的单在 M3 上线后走自判（goal §五）；上线前仍监督面批（S8）。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m3_line_selfgate.py'
bash -lc 'make verify'
```

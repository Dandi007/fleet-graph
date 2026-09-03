# Spec M3（wf-8d9737）· 线自判闸 = 全舰默认路径（六项取证 + 回归基线必答字段）

> 状态：**谱系：d9cd78e40c45（3cc92467…，无 S10/S11/S12）→ e75dd8d777b4（e9626381…，无 S11/S12）→ 9c8d4759e344（2488b1c4…，无 S12）→ a0734081b8b0（d9b9a01f…，S10+S11+S12 全谱，**只交付线侧**：self_gate/runner.py 接线 + enumerate_mutation_targets/verify_mutation_receipt 函数；**未改 dd 引擎 final_review 阶段本身**——rf review.json 无逐靶子红/绿回执、无必填「已核验项」字段，接线维缺口在 review 阶段复发）→ 2026-09-03 12:4x 折入引擎侧收束（S12 §8.3/§8.5 余件 + D8 等价断言 + 三条验收判据）→ dev-fg-c18ba1ac025f（g2 9bd0379，内容侧六项义务已全过；2026-09-04 因 base=d679cf58 属 main 谱系违反 D8 且谱系被 d90fb60ce56e 六缺陷收束单取代而 REJECT）→ **2026-09-04 按 §5e 换号重派为本单：base 改冻结为 origin/release/wf-8d9737 头 99adc38671（release 谱系，含 M3.1 六缺陷引擎修复与 M3 线侧收割），消解两个 REJECT 根因；S12 余件（final_review 执行变异/六项义务表/review prompt 措辞）为交付面，基底已含的 M3.1 修复面不重做**。S12 系用户拍板（正本 design.md §6.3 表+§6.3.1，commit `76de20f`/`0f68885`）**。派单序钉在 M1、M2 合入之后（依赖 M1 的 `dd_awaiting_gate` 唤醒与 M2 的 `decision_deliver` dd 单路径）。判据锚：design.md §6.2/§6.3/§6.4（D5：闸归派单线）、§8 行「DD 闸不经人」；goal.md §二 M3、S8（五步取证）、**S9（第六条，不可弱化）**、**S10（裁决送达落地三条，不可弱化，2026-09-03 08:1x 补）**、**S11（dd 投递双路径合一三条，不可弱、优先级在自判闸本体之前，2026-09-03 09:2x 补）**、**S12（变异测试改由 final_review 执行、靶子从 diff 机械枚举，2026-09-03 10:5x 用户拍板）**、S7（wf-216dc3 G1 让位：闸权通用形态归本线，收割反应器改接 merge 段之后）。与 design.md/golden-order 冲突以后两者为准。

## 要交付的行为（全部在 fleet-graph 仓）

1. **线自判路径成引擎默认**：goal 线收到 `dd_awaiting_gate(dev_id)` 唤醒（M1）后，自动履行下列**六项取证义务**，全部通过后经 M2 的 `decision_deliver(dd 单, APPROVE|REJECT)` 投递；`decided_by` = 线 principal 且被校验等于该单 `record.json.dispatched_by`。**吸收 wf-216dc3 已收割的「线自判闸首批」既有形态（main `5a39d03`，PR #245），接着做、不另起炉灶**；该路径上线后为全舰默认：监督面不再逐单批闸，人只在 goal 级验收与升报回答出现（design §6.3）。
2. **六项取证义务 = gate 的必答字段**（引擎侧机械履行；缺任一项 → 投递被拒或被标记，用例必须能红）：
   1. **三方验收逐字相等**：spec 冻结验收 argv == record.json `acceptance_commands` == 阶段回执 command（机器比对）。
   2. **产品 diff 未越 spec 边界**：产品文件改动逐一对得上 spec 声明的交付面； `.dev-dispatch/`、`.dd-evidence/` 机器件除外。
   3. **零测试删除**：base..head 的 `--diff-filter=D` 为空（既有测试更新断言不算删除）。
   4. **亲跑验收**：线在 gate 侧亲自复跑冻结验收命令并留回显。
   5. **变异测试（靶子不是自选的；S12 修订二，2026-09-03 用户拍板，替换原「变异枪两发·自选靶子」形态）**：从 `base..head` 的产品 diff 里**机械枚举本单新增的每一个生产侧调用点**。**逐个删除，冻结验收命令必须变红。** 有任一删了不变红 → 该调用点无测试覆盖 → `REJECT`。**执行者 = final_review 阶段**（在一次性副本里做），回执含逐个靶子的位置与红/绿结果。**gate 不重跑，只核验回执**：靶子集合是否等于机械枚举出来的集合、是否全部落红。对不上就拒绝投递。判据用**冻结验收命令**、不用全量套件——便宜（跑一个文件而非 2669 条），而且**自我纠正**：验收覆盖面太窄盖不住新接的线时这条检查就会失败，逼着要么把测试写进被覆盖的文件、要么放宽验收命令，两条路都把洞堵上。执行者必须是 final_review 而非本线：**M3 之后闸权归本线自判，本线自选靶子与实现方是同一种偏差——没有人会往自己的盲区开枪**，这不是态度问题，是「自选靶子」的必然结果；把打枪的人和写代码的人分开，同时保留 D5「线端到端负责」的形态（本线仍是投递裁决的那一方，只是不再自己给自己打枪）。
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
7. **dd 投递双路径合一（S11 三条，不可弱，优先级在自判闸本体之前；监督面 2026-09-03 09:2x 在部署 release `20260903-080440-d9c04295a3cd` 上逐形态实测后签入 goal.md §七 S11，三条逐字折入）**。事实背景（真机实测，每环已核）：`deliver_decision()` 里 `kind = _normalize_target_kind(target_kind)` 之后——**形态 A**（`kind == TARGET_KIND_DD`，wf-525fd4 早先加的路径）先 return `deliver_decision_dd(target_id=…)`，**不接收 principal，也不检查**；**形态 B**（dd 号放 `line`、`target_kind` 用默认，M2 加的路径）才走 `_deliver_dd(…, principal=principal, …)`，`NOT_DISPATCHING_LINE` 只在 B 里。逐形态实测（用不存在的单号 + FakeDd，不碰真实单据）：形态 A + principal `""` → `refused DD_NOT_FOUND`——**只查单据存不存在，从不查身份**；形态 B + `""` → `refused NOT_DISPATCHING_LINE`；形态 B + `"wf-8d9737"` → `delivered`。⇒ **形态 A 下，任何调用方都能对任何线的任何闸投裁决，身份校验完全不生效**。为什么编进 M3 且优先：①D5 之后闸权全面下放线自判，**授权模型是自判闸唯一的安全边界**，边界有个谁都能过的口子，「线自判」就等于「谁都能替线判」；②这正是本次重构要消灭的病——**两份真相并存，先到的那份赢**。M3 必须交付：
   1. **两条路径合一。** 形态 A 要么删掉、要么内部转调 `_deliver_dd` 并同样校验 principal。**不允许存在一条绕过授权的 dd 投递路径。**
   2. **阴性判据（不可弱）**：以非派单方身份、用**形态 A** 投递一张真实 `awaiting_gate` 单 → 必须 `NOT_DISPATCHING_LINE`。这条用例现在必然是红的，修完才绿——**这就是变异枪的靶子，不用另造**。
   3. 顺手把 `verify-lim.sh` 第 12 条按上面说的修对：那条探针用了不存在的单号走形态 B，先撞 `DEVELOPMENT_NOT_FOUND` 就返回了，**永远到不了身份校验分支**——既没证明校验在、也没证明校验不在；**这条判据本身要修**（用真实存在、但非本方派单的单号，断言拿到 `NOT_DISPATCHING_LINE`，属加考题、可自决）。
   - ⚠️ 与 S10 合看：**回执说成功不等于事情发生了（S10），回执被接受也不等于你有权做（S11）。**这两条都是「机制看起来在，实际没生效」。
8. **S12 收束件（用户拍板 2026-09-03 10:5x；正本 design.md §6.3 表+§6.3.1，commit `76de20f`/`0f68885`）**：
   1. **只读约束按修订一改措辞**：**subject workspace 只读**；验证实验（变异测试等）在**一次性副本**（另开 worktree / 临时目录）里做，做完丢弃。写 subject workspace 仍然作废结论。（引擎两个 review prompt 第 36–37 行的同一句一起改。）
   2. **实现机械枚举**：从 `base..head` 产品 diff 提取新增生产侧调用点，作为变异靶子集合（义务 2.5 的执行件）。
   3. **final_review 阶段接入变异测试（引擎侧执行，非线侧函数；2026-09-03 12:4x 收紧）**：dd 引擎的 final_review **阶段**必须在**一次性副本**里机械执行变异测试——从 base..head 产品 diff 机械枚举本单新增生产侧调用点，逐个删除后在该副本上跑冻结验收命令，产出**回执工件**（落 review.json 或其旁挂工件）：含**每个靶子的位置（文件:行/调用点）+ 红/绿结果**；任一靶子删后不红 → 无测试覆盖 → REJECT。**gate 侧只核验回执**（靶子集合==机械枚举集合且全落红），不重跑。
   4. **补上这次漏掉的那条用例**：`runner.py` 里 `result = deliver_self_gate_decision(...)` 这一行删掉后，冻结验收必须变红。**这就是修订二在本单上的实例，直接拿它当验收现场。**
   5. **review 回执 schema 增加必填「已核验项」清单字段（引擎侧 schema 变更，非可选建议）**：rc/rf review.json 均必填 checked/verified_items——即便 findings=0 也要列明检查了什么；缺该字段 → 回执无效（用例必须能红）。
   6. **按 D8 补 release 分支上可机械验证的等价断言（2026-09-03 12:4x 新增）**：须在 release 分支上补**可机械验证的等价断言**：断言 **final_review 执行入口在生产模块调用图中可达**（静态调用图/导入链可达性检查，纳入冻结验收可跑的测试），**而非断言某进程在跑**。断言不可达 → 红。

## 判据（goal.md §二 M3 + design.md §8 原文内联）

- 阳性：一个班次内本线派出的 dd 单，`decided_by` 100% 是本线 principal（design §8「DD 闸不经人」行）。
- 阴性：线漏掉任一项取证义务就投递 → 投递被拒或被标记，用例能红；第 6 条的阴性含四款：缺基线/增量字段、**绿→红翻转**（把绿的打红）、红项集合扩大（含基线本身红时再添新红）、以及**用漂移后的 main 当基线**（必须仍按冻结 target_base 比对）。
- 用例素材（真实失效事件，写进测试）：S9 实例——wf-525fd4 `dev-fg-cd44b133614e` 摘除 dev/gate/pump 三族工具但三族测试未动：基线 106 passed 全绿 → 打补丁 31 failed，而其冻结验收只跑自己的 12 条测试仍绿；五项旧义务全过、第六项必拦。
- 阴性（S10 红靶，真机单据不另造 fixture）：以真机在卡 `awaiting_gate` 的单据为素材（原例 dev-fg-36c2d76baca7 若已不在该状态，以届时任一真实 awaiting_gate 单等价替换并在回执注明单号）——M3 落地后对它投一次裁决**必须**得到带原因的 `REFUSED`（而不是 `delivered/consumed`），并且单据上要能看见这次拒绝（`gate_refused` 有值 + `events.jsonl` 追加）。
- 阴性（S11，不可弱）：以非派单方身份、用**形态 A**（`target_kind="dd"` + `target_id`）投递一张真实 `awaiting_gate` 单 → 必须 `NOT_DISPATCHING_LINE`。且不允许代码里残留任何绕过 principal 校验的 dd 投递路径（合一后形态 A 与形态 B 同判）。
- 判据（S12，不可弱）：机械枚举出的靶子集合中，任一「本单新增生产侧调用点」被删除后**冻结验收命令不红** → 该调用点无测试覆盖 → `REJECT`；final_review 回执的靶子集合 ≠ 机械枚举集合、或有靶子未落红、或 gate 侧重跑打枪而未核验回执 → 拒绝投递。实例判据（本单验收现场）：**删 `runner.py` 的 `result = deliver_self_gate_decision(...)` 行后冻结验收必须变红**。变异实验必须在一次性副本做——写 subject workspace 仍然作废结论。
- 判据（引擎侧收束三条，冻结命令 + 新用例须真机可复现）：①**删 `tests/test_m3_line_selfgate.py` 里 runner.py 那行 `result = deliver_self_gate_decision(...)` 的实例靶行后，冻结验收必须红**（本单验收现场）；②**review.json 工件必须含逐个靶子位置+红/绿结果与必填「已核验项」清单，缺任一字段即红**；③**final_review 执行入口在生产 review 模块调用图可达**（静态可达性断言，非进程在跑断言），不可达即红。

## 测试与验收

- 新增 `tests/test_m3_line_selfgate.py`：六项义务逐项（含第 6 条四款：缺基线字段拒、绿→红翻转拒、基线红但红集未扩通过、**flake 唯一红增量→净 base 隔离复跑归因后放行且载荷留证**、**gate 时 main 已漂移仍按冻结 target_base 比对**）、漏项投递被拒、阳性路径（自判 APPROVE → merge 后收割触发）、principal 校验。**零测试删除**。
- S10 三条的阴性用例：①错路径 workspace → resume 必须**在起 unit 之前**报带原因 `REFUSED`（不得触发起 unit、不得报 `delivered/consumed`）；②unit 起了但退出而单据仍在 `awaiting_gate` → 回读判据不满足，必须报 `REFUSED` 且**带 unit 退出码**；③每次 `REFUSED` → `gate_refused` 写入拒绝原因与退出码 + `events.jsonl` 追加一条（旧形态的回执 `status=delivered, outcome=consumed` 属假绿，用例必须能红）。
- S11 的阴性用例（**现在必红、修完才绿——变异枪靶子，不用另造**）：以非派单方 principal（含空串）用**形态 A**投递一张真实 `awaiting_gate` 单 → 断言必须 `NOT_DISPATCHING_LINE` 且单据状态不变；合一的实现断言：代码中不存在任何绕过 principal 校验的 dd 投递路径（形态 A 删除或转调 `_deliver_dd` 同判）。
- S12 的用例（靶子=机械枚举，不是自选）：①机械枚举靶子集合 == `base..head` 产品 diff 新增生产侧调用点全集（漏枚举/多枚举 → gate 拒绝投递的用例能红）；②final_review 回执含逐靶子位置与红/绿结果，gate 只核验回执不重跑（gate 侧重跑即拒）；③**删 `runner.py` 的 `result = deliver_self_gate_decision(...)` 行 → 冻结验收必须变红**（现在必红修完才绿）；④变异实验在一次性副本做、subject workspace 只读（对 subject workspace 的任何写入作废结论的用例能红）；⑤review 回执必填「已核验项」清单（缺清单 → 拒绝的用例能红）。
- 引擎侧收束三条的用例（对应判据节「引擎侧收束三条」，冻结命令可跑、真机可复现）：①**实例靶行删除红**；②**回执 schema 断言**：构造/读取 review.json 工件，断言含逐个靶子位置+红/绿结果与必填 checked/verified_items——缺任一字段即红；③**调用图可达性断言**：静态断言 final_review 执行入口在生产 review 模块调用图中可达（import 链/调用边检查），不可达即红——而非断言某进程在跑。
- 派单 base：**origin/release/wf-8d9737 头（本单冻结 99adc38671，release 谱系；§5e 换号重派修正，替代原「main 头」口径）**；M1/M2 已合入（依赖其唤醒事实与 dd 单裁决路径）；基底已含 M3.1（d90fb60ce56e）六缺陷引擎修复——已修复面不重做，交付面收窄到 S12 余件与上述判据。

## 边界

- 只动 fleet-graph 仓（dd 引擎 gate 侧、goal_line 取证编排、scheduler/收割接线）；不做 line_message（M4）、不做 release 分支模型本体（M5）、不改 decision-bridge（M8）。**例外（S11 第 3 条明令、属加考题可自决）：verify-lim.sh 第 12 条探针要修对。**
- 本线自己的单在 M3 上线后走自判（goal §五）。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m3_line_selfgate.py'
bash -lc 'make verify'
```
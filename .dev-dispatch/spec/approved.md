# Spec R6（wf-4601c8）· 删除 —— §7.1 九项与 §7.2 十三项合并执行，新路顶上（M7+M8）

> 状态：**定稿**（2026-09-06 派单前重校后定稿；round12 等待期产出草稿＋重校增补）。
> **派单前重校**（2026-09-06T01:2x–01:3xZ，§5b 独立 worktree /data/worktrees/fleet-graph-wf-4601c8-r6-preflight @88d56c2708c42b618270de8c44e0cbac7c136e7a，fetch＋ls-remote 亲证当刻 origin/release/wf-4601c8 头＝88d56c2＝R5 线级合并头，与预期一致）：dd-acceptance 三条 argv 形参与实际判据面全部受支持——`verify-rebuild.sh --check` 仅接受 01–21（01/02/21 合法），单查模式恰打一行 `NN name VERDICT — …`（em dash 与 CHK grep 模式逐字一致），全量恰 21 行 `NN [a-z0-9-]+ (PASS|FAIL) — `；`--env test` 与 `--root` 形参受支持（up rc=0、down rc=0、down 后 prod_references=0，本轮亲测）。基线活体取证（原文逐字，两面各一）——**--env test 面**（/tmp/r6-preflight-testenv 全新环境）：01 `PASS — agent-bus-* 已加载单元共 2 个，名集合 ⊆ {agent-bus-server, agent-bus-mcp}（探针: /tmp/r6-preflight-testenv/bin/systemctl-stub --user list-units --plain --no-legend）`（testenv stub 面，非本单判据项，交付后须维持）；02 `PASS — 协议注册表（http://127.0.0.1:27490/v1/protocols）中原 dead 协议 coord.* 出现次数为 0`（testenv 从零起无死协议，交付后须维持）；21 `FAIL — §7.1 gone=8/9 §7.2 gone=10/13，仍在对象（探针出错或对象存在，样例）: §7.1.7 dd-mcp NOT_SUPPORTED 五工具 §7.2.3 work.card.v1/board:work-index §7.2.4 dd status.json//v1/lines.parked §7.2.8 /data/ronin 引用或 token 新路径(/tmp/r6-preflight-testenv/secrets)（明细: systemctl rc_units=0 rc_uf=0，bus 可核=1，dd-mcp tools 可核=1，skill 可读=1）`（本单义务项：四残留在 testenv 全部可由仓内改动消除——§7.1.7 五 stub 工具在 src/fleet_graph/dd/service.py tools/list 亲证在列；§7.2.3 引擎 bus/board.py 的 WORK_INDEX/board:work-index 与 work.card.v1 信道仍由引擎/调度器创建（src/fleet_graph/bus/board.py:23,26 亲证）；§7.2.4 dd control_plane.py STATUS_FILE="status.json" 仍写、fleet_state.py /v1/lines "parked" 字段仍出（源码亲证+生产面 status.json 存在、/v1/lines parked 键=True 亲证）；§7.2.8 仓内 config/ronin-lines.json _provenance 文本两处字面含 /data/ronin（testenv current/ 快照直拷仓内 config，grep 命中亲证）——改写 provenance 文本即可，不涉 /data/ronin 实体）；18/19 当刻已绿（`PASS — …调度器唤醒路径 0 处「读 terminal.json/.scheduler 内容当 dd 终态事件」分支`、`PASS — testenv 删 checkpoint 重建探针通过: rebuild ok deleted=0 rebuilt=2 dups=0 重建=ok`，本单义务＝21 由红转绿且不回退 18/19 与全部已绿项）。**默认（生产读数）面**：01 `PASS — agent-bus-* 已加载单元共 2 个，名集合 ⊆ {agent-bus-server, agent-bus-mcp}（探针: systemctl --user list-units --plain --no-legend）`；02 `FAIL — 协议注册表响应中子串 coord.* 出现 20 次，命中样例: "kind": "coord.notice.v1" "kind": "coord.notice.v2" "kind": "coord.slot.v1"`；21 `FAIL — §7.1 gone=3/9 §7.2 gone=3/13，仍在对象（探针出错或对象存在，样例）: §7.1.2 wf-observe.service §7.1.3 loop-engine-*/loop-mcp/ronin-auto-gate/ronin-babysitter/ronin-pump-* §7.1.4 测试看板频道族 §7.1.5 死协议族(coord.*/dd.plan.*/probe.reqtype.v1/research.smoke.v1/agent.run.*) §7.1.7 dd-mcp NOT_SUPPORTED 五工具 §7.1.9 Tempo §7.2.1 decision-bridge/e7_* §7.2.2 ronin-mcp 门面 §7.2.3 work.card.v1/board:work-index §7.2.4 dd status.json//v1/lines.parked §7.2.6 :7494 调用面 §7.2.7 line revive/set-seat/supervisor reset/fleet-maint 调用面 §7.2.8 /data/ronin 引用或 token 新路径(/data/fleet-graph/secrets) §7.2.9 A2 arbiter timer §7.2.11 goal.md 直写捎话/line set-seat §7.2.13 逐单批闸 SOP`——生产面 02/21 的 gone 提升大半属仓外 systemd/bus 运行时/部署态（B-3 执行后），不作本单硬判据，依据行如实记录。**与 R2/R3 已删项去重**（不重复删）：R2 已删「terminal.json/.scheduler 读作事件」分支与 status.json/parked 的**双轨读路径**（本单删的是**剩余的写出面**：control_plane.py 的 status.json 落盘与 fleet_state /v1/lines parked 字段发射——R2 spec「写允许、读作事件即违宪」给写的暂留期到本单结束）；R3 已删 decision_deliver 的 dd 目标路径与 decision-bridge 在 dd gate 上的消费（本单不重复；仓内 decision_bridge/ 目录与 deploy/systemd/fleet-graph-decision-bridge.service 的**整件退役**属 §7.2.1 仓外 systemd 面与仓内残余清理，按开放点 1 归属表定，钉最后批）。R0 基线（2026-09-05 首跑 §7.1 3/9、§7.2 3/13）与本次重校（--env test 8/9、10/13；默认面 3/9、3/13）的差异属 R2–R5 合流后的正常推进，判据面本身零弱化。
> 判据锚：goal.md §二 R6 与 §四纪律、§四·一 B-3（仓外删除升报）；design.md §1（R6 ↔ 宪法第十条 主链路自足观测走旁路）、§3 自决「M7/M8 合并为 R6 直接删，D8 之后不存在生产上的中间态，双轨失去理由」、§2 改形（spec-m7 的清单 SSoT+逐项验证+exit=残留数已被 R0 的 v2 第 21 项吸收为本线后继；spec-m8 的「条件未满足不得删」与顺序护栏保留，decision-bridge 钉最后批）、§4 验收 v2 第 1、2、21 项；R0 实测基线（2026-09-05 首跑：21 项 §7.1 gone=3/9、§7.2 gone=3/13，仍在对象清单见 R0 spec 21 节）。与正本冲突以正本为准。

## 范围（一句话）

§7.1 九项与 §7.2 十三项直接删、新路顶上：**仓内**删除随本单代码落地；**仓外**删除（systemd unit、bus 频道与协议、部署件、/data/ronin）本线出清单与验证脚本，执行归监督面或 wf-3ffd90（B-3 升报一次，批准后执行）。

## 交付物

1. 引擎仓内删除（改）：剩余仓内对象——ronin-mcp 门面残余、dd-mcp 五个 NOT_SUPPORTED 工具 stub（deployment_create/deployment_status/development_control/development_relock/development_steer，tools/list 亲证在列）、`--stage-model` cmdline 覆盖键、CLI `line revive`/`line set-seat`/`supervisor reset` 的实现残余（R5 降级后；`fleet-maint.sh` 仓内已不存在，勿凭空造删除——重校亲证仓内零命中，属 §7.2.7 的 skill/persona 调用面与仓外部署态）、goal.md 直写捎话信道（supervise/e7_* 面）、Monitor/fleet-l0.py 唤醒路（仓内已无 fleet-l0.py 文件，删除面＝调用/引用残余，重校亲证）、`/v1/lines.parked` 字段与 `dd/<dev>/status.json` **写出面残余**（R2 删了双轨读路径，本单删写出面：control_plane.py STATUS_FILE 落盘与 fleet_state parked 字段发射）、仓内 `config/ronin-lines.json` _provenance 文本中的 `/data/ronin` 字面引用（两处，testenv current/ 快照 grep 命中亲证；改写文本，不碰 /data/ronin 实体）、`work.card.v1`/`board:work-index` 的引擎侧创建路（src/fleet_graph/bus/board.py WORK_INDEX/CARD_KIND 与调度器/引擎建卡路径）。触点由实现方按「§7.1/§7.2 逐项归属表」（开放点 1）探索定。
2. 新增 `tests/test_r6_legacy_removal.py`：逐对象「确实没了」机械断言 + import/引用零命中 + 前置条件核验器 + 顺序护栏用例。
3. 新增 `scripts/decommission-outer-list.md`（仓外删除清单 SSoT）与 `scripts/decommission-outer-verify.sh`（仓外逐项验证：systemctl 查询、bus API 只读探针、部署存在性——**只读**，不执行删除）；二者是 B-3 升报包的附件。
4. 既有测试零删除；绑定被删对象的用例随对象改写或移除（design §2 点名的除外），覆盖净数不降——逐条「删/改」对照表落交付。

## 行为契约（硬性）

### 1. 条件未满足不得删（spec-m8 保留纪律）

- 每个仓内删除对象绑定**前置条件**＝替代路已在分支上（对应 R2–R5 的交付）；逐项条件核验器先跑，任一未满足 → 该项删除拒绝并留痕（不整单失败，逐项分级）。
- 顺序护栏：`decision-bridge` 与 `/data/ronin` 引用清理钉在**最后一批**；乱序执行 → ERROR（spec-m8 原语义）。

### 2. 仓内删除的机械判据

- 删除后：仓内 `grep` 被删对象名/导入路径＝0（生产模块 import 面）；skill 与 persona 引用面＝0（第 8 项 grep 对象，本仓不拥有但断言读数）。
- dd-mcp 五工具从 `tools/list` 消失；`--stage-model` 键从所有 launch 路径消失（座位单一来源只剩 record.seats/registry）。

### 3. 仓外清单与验证（B-3 附件，本单只造不出刀）

- 清单逐项含：对象名、类别（systemd unit / bus 频道 / bus 协议 / 部署件 / 目录）、验证命令、执行命令（供监督面复核后执行）、风险注记。§7.1 第 1/2/3/4/5/9 项与 §7.2 第 8（/data/ronin 引用与 token 新路径断言，**不删不改**）项全部在此。
- 验证脚本只读：对生产执行仅查询（systemctl list/show、GET :7490/v1/protocols、部署存在性），exit=残留项数；与 verify-rebuild 21 项同判据不同执行面（21 项探分支与测试环境，本脚本探生产现状供升报）。

### 4. 验收口径

- **--env test**：测试环境从零起、无历史残留，21 项在仓内删除后须 `§7.1 gone=9/9 §7.2 gone=13/13`（对象不存在或代码引用已删）；第 1 项（systemctl stub 只应答 testenv 面）与第 2 项（新 bus 无死协议）PASS。
- **默认（生产读数）**：21 项 gone 计数较基线（3/9、3/13）提升，满额依赖 B-3 仓外执行——不作本单硬判据，依据行如实记录「仓外待执行清单+升报状态」；第 1/2 项生产转绿同属 B-3 后。

## 阴性用例与变异红靶（成对：红锚+注入翻转）

1. **恒 DELETED 红**：向 21 项探针/本单测试注入「恒 gone」 → 对坏 fixture（伪造残留对象）红。
2. **静默漏项红**：从清单抽掉一项 → 计数与对象总数不符 → 用例红（清单 SSoT：测试对象由清单枚举生成，不由实现方自选——S12）。
3. **条件未满足强删红**：注入跳过前置核验器 → 用例红。
4. **乱序删除红**：先删 decision-bridge 后删依赖它的面 → 顺序护栏 ERROR 用例红。
5. **import 残留红**：删除后任一被删对象仍被生产模块 import（注入一行 import）→ grep 断言红。
6. 元：dd-mcp tools/list 无五工具；launch 路径无 `--stage-model`；删/改对照表计数；make verify 全绿。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r6_legacy_removal.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r6-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r6-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; CHK(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check $1" | grep -cE "^$1 [a-z0-9-]+ PASS — "; }; c01=$( [ "$uprc" -eq 0 ] && CHK 01 ); c02=$( [ "$uprc" -eq 0 ] && CHK 02 ); c21=$( [ "$uprc" -eq 0 ] && CHK 21 ); c18=$( [ "$uprc" -eq 0 ] && CHK 18 ); c19=$( [ "$uprc" -eq 0 ] && CHK 19 ); lines=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " ); bash scripts/testenv.sh down --root "$R" >/tmp/r6-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r6-te-down.out | head -1); echo "up=$uprc lines=$lines c01=$c01 c02=$c02 c21=$c21 c18=$c18 c19=$c19 down=$drc $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$c01" -eq 1 -a "$c02" -eq 1 -a "$c21" -eq 1 -a "$c18" -eq 1 -a "$c19" -eq 1 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：21 行读数齐；**1/2/21 转 PASS**——21 在 --env test 须 `§7.1 gone=9/9 §7.2 gone=13/13`；18/19 维持（R2 成果不回退）；干净回收零生产引用。生产默认面的 gone 提升与 1/2 生产转绿属 B-3 执行后，如实记录不硬判。）

## 边界

- 仓内只动 fleet-graph 仓；**不执行任何仓外删除**（systemd/bus/部署//data/ronin 一律不出刀——B-3：本单交付清单+验证脚本后升报一次，批准后由监督面或 wf-3ffd90 执行）。
- /data/ronin 不删不改（v2 21 项之 7.2.8 原文：断言只查「不再被引用」与 token 新路径存在性）。
- 验收全部在 testenv 与离线单测；对生产只读（验证脚本、21 项默认读数）。

## 开放点（实现方回执强制作答）

1. **§7.1/§7.2 逐项归属表**（22 项 → 仓内代码 / 仓外 systemd / 仓外 bus 运行时 / 部署态 / 引用断言-only），含与 R2–R5 已删项的重叠去重清单——本单实际删除集的冻结清单。
2. B-3 升报包形状：清单+验证脚本之外还需什么（回滚预案？执行顺序批次？）；`scripts/decommission-outer-verify.sh` 的只读边界自证（无任何写路径命令）。
3. 绑定被删对象的既有测试逐条处置对照表（删/改/移并），净覆盖不减的量化口径。
4. `--stage-model` 覆盖键删除后座位注入的唯一路径确认（record.seats/registry → launch 的派生链路图）。

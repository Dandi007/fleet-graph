# Spec R5（wf-4601c8）· 外门收敛 —— 运行时状态与运维动作上 MCP，:7494 与 CLI 降为实现（D2）

> 状态：**定稿**（2026-09-05 派单前重校后定稿；round11 等待期产出草稿＋重校增补）。
> **派单前重校**（2026-09-05T11:2xZ，§5b 独立 worktree @a16584fbbb8d…，fetch＋ls-remote 亲证当刻 origin/release/wf-4601c8 头＝a16584f＝X-4 线级合并头，与预期一致）：dd-acceptance 三条 argv 形参与实际判据面全部受支持——`verify-rebuild.sh --check` 仅接受 01–21（09/10/18/19 合法），单查模式恰打一行 `NN name VERDICT — …`，全量恰 21 行 `NN [a-z0-9-]+ (PASS|FAIL) — `；`--env test` 与 `--root` 形参受支持（up rc=0、down rc=0、down 后 prod_references=0）。基线活体取证（--env test，原文逐字）：09 `FAIL — 零上下文一次调用拿不到六项：state 面 /v1/takeover http=404；decision/dd MCP tools/list 无 state_takeover（decision: decision_list decision_deliver；dd: deployment_create deployment_status development_list development_get development_events development_）。缺失项：名册/线状态/等拍板/待上线/授权模式/当前 release 的单一接管面未上线`（本单义务项，由红转绿）；10/18/19 当刻已绿（10 `PASS — 五个面 tools/list + 只读真调用全部成功：bus:25608=bus_agent_list:ok; dd:25610=development_list:ok; goal:25611=goal_list:ok; decision:25614=decision_list:ok; state=http://127.0.0.1:27494=read:/v1/lines ok`、18 `PASS — …调度器唤醒路径 0 处「读 terminal.json/.scheduler 内容当 dd 终态事件」分支（机械口径: 同行命中读内容模式）`、19 `PASS — testenv 删 checkpoint 重建探针通过: rebuild ok deleted=0 rebuilt=2 dups=0 重建=ok`，本单义务＝09 由红转绿且不回退已绿项）；08 当刻在 testenv 样例 skill/persona 上亦绿（`PASS — 监督面 skill 与线 persona 中裸 HTTP(:7490/:7494)/fleet-graph line/fleet-maint 入口命中 0 条`——注意 08 的 grep 对象是监督面 skill 与线 persona 资产，testenv 内为样例文件；生产资产不在本仓，08 允许仍红不构成本单判据）。R0 首跑（2026-09-05，生产面）红原文与本重校（testenv 面）的差异属检索对象不同，判据面本身零弱化。全量当刻恰 21 行（7 PASS/7 FAIL——04/06/15/16/20/21 属 R0 预期红区与 R6 边界，非本单判据）。
> 判据锚：goal.md §二 R5 与 §四纪律；design.md §1（R5 ↔ 宪法第十三条 通信协议化对外一个入口、第九条 失败必须现形；L1 约束第 2 条；D2、D15）、§2 改形复用（wf-8d9737 spec-m6-state-mcp-takeover.md 为底稿：四个运行时动作上 MCP、state_takeover 六项合成缺项显式标注、红侧 A 恒 present / B 无鉴权 / C 缓存合成；S7 边界＝复用 wf-525fd4 只读视图不重做）、§4 验收 v2 第 8、9、10 项；R0 实测红基线（2026-09-05 verify-rebuild 首跑：08 项 skill/persona 裸调用面 7 命中；09 项 /v1/takeover 404、decision/dd 面 tools/list 无 state_takeover、六项不可得；10 项 decision 面无只读工具致五面不全绿）。与正本冲突以正本为准。

## 范围（一句话）

监督面与线对引擎的 public interface 只剩 MCP：九个运行时工具上 MCP 面，`:7494` 与 CLI（`fleet-graph line`、`fleet-maint`）降为实现细节，对调用面不再暴露。

## 交付物

1. 引擎源码（改）：MCP 工具面九工具——读四件 `state_lines / state_line / state_decisions / state_takeover`，写四件 `line_revive / line_set_seat / maintenance_set / maintenance_clear`，挂 note 一件 `note_publish`；decision 面补只读工具（如 `decision_list / decision_get`，满足 10 项五面全绿）；`:7494` HTTP 与 CLI 降级为内部实现（保留只读 GET 供探针与 R0 判据 03/05 等既有读数，写面从调用面语义里移除）。触点由实现方按行为契约探索定。
2. 新增 `tests/test_r5_outer_gate_mcp.py`；既有测试零删除（绑定 CLI/HTTP 调用面的用例改写到 MCP 路径，覆盖净数不减）。
3. 不碰 verify-rebuild.sh（8/9/10 判据已冻结）、Makefile、监督面 skill 与线 persona（非本仓资产，见开放点 4）。

## 行为契约（硬性）

### 1. 九工具与鉴权

- 读四件：`state_lines()`（名册+线状态总览，含 R4 的 release_behind/deploy_behind）、`state_line(line_id)`（单线详情）、`state_decisions(window)`（裁决台账）、`state_takeover()`（零上下文接手：**六项**＝名册、线状态、等拍板、待上线、授权模式、当前 release，一次调用齐）。
- 写四件：`line_revive / line_set_seat / maintenance_set / maintenance_clear`——**监督者 principal 专属**（非监督者调用稳定拒绝+留痕，拒绝码写进回执；与 R2 的 development_create 外门同族鉴权）。
- `note_publish(card, note, note_type, refs)`：监督者与**卡主本人**（dispatched_by 线对自己卡）可用；refs 语义与 bus `work.note.v1` 对齐（MCP 是门，bus 是载体——工具落 bus，不另起信道）。
- `state_takeover` 六项中不可得项**显式标注**（`unavailable`+原因），不得省略键、不得以旧缓存冒充现算（每项带 computed_at；缓存须标注）。

### 2. 调用面收敛

- `:7494` 与 CLI 降为实现：HTTP 保留**只读 GET**（既有探针判据 03/05 与 R0 骨架依赖），写面与管理动作从 HTTP/CLI 语义里移除（写经 MCP）。
- 上游死地址必须告警（10 项阴性半边）：任一 MCP 面的上游依赖不可达时 `tools/list` 仍应答但相关工具报 `upstream_unavailable`，禁静默空转或假数据。
- S7 边界：只读视图复用 wf-525fd4 的既有投影，不重做。

### 3. 与 R2 外门语义衔接

- R2 已定：`development_create` 降内部函数、MCP 同名工具只留监督者手动起单。本单把「外门」补齐为完整工具面：读四件对监督面与线均开放，写四件+起单监督者专属——**外门＝监督者操作面 + 全体只读面**；线的派单/批 gate 仍走图内路径（R2/R3），不经此门。
- 一切新工具遵循宪法第九条：失败现形（拒绝码、unavailable 标注、告警），无静默成功。

## 阴性用例（成对红锚+注入翻转；红侧三形源自 spec-m6）

1. **A 恒 present 红**：takeaway 缺项省略键（注入）→ 用例红；正确行为＝缺项显式标注且不算「齐」。
2. **B 无鉴权红**：以线 principal 调写四件 → 稳定拒绝+留痕；监督者 principal → 成功。变异：去掉鉴权 → 红。
3. **C 缓存合成红**：把 takeover 某项换成过期缓存未标注 → 用例红；正确行为＝computed_at 标注且过期即 unavailable。
4. **死地址告警**：把某面上游指到死地址 → 该面工具报 upstream_unavailable 且 10 项探针红（非静默过）。变异：吞错误返空数据 → 红。
5. **note 越权**：非卡主线对他卡 note_publish → 拒绝；refs 缺失 → 拒绝（协议 refs_required）。
6. 元：五面 tools/list 全含各自只读工具；HTTP 写面移除后旧 CLI 用例改写计数不减；make verify 全绿。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r5_outer_gate_mcp.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r5-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r5-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; CHK(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check $1" | grep -cE "^$1 [a-z0-9-]+ PASS — "; }; c09=$( [ "$uprc" -eq 0 ] && CHK 09 ); c10=$( [ "$uprc" -eq 0 ] && CHK 10 ); c18=$( [ "$uprc" -eq 0 ] && CHK 18 ); c19=$( [ "$uprc" -eq 0 ] && CHK 19 ); lines=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " ); bash scripts/testenv.sh down --root "$R" >/tmp/r5-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r5-te-down.out | head -1); echo "up=$uprc lines=$lines c09=$c09 c10=$c10 c18=$c18 c19=$c19 down=$drc $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$c09" -eq 1 -a "$c10" -eq 1 -a "$c18" -eq 1 -a "$c19" -eq 1 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：21 行读数齐；**9 转 PASS**——零上下文一次调用六项齐（缺项显式标注不算齐）；**10 维持绿**——五面 tools/list+只读真调用全绿且死地址告警（本重校当刻 10 已绿于 testenv，交付不得使其回退，且死地址告警阴性必须可注入实证）；08 项 grep 对象为监督面 skill 与线 persona（非本仓资产），testenv 内当刻绿、生产侧是否红不构成本单判据——本单交付工具面后由监督面切调用面，依据行须显示「工具面已上、调用面未切」的诚实半绿；18/19 维持；干净回收零生产引用。）

## 边界

- 只动 fleet-graph 仓；不部署、不碰生产名册与 /data/ronin；验收全部在 testenv 与离线单测。
- skill/persona 调用面切换（8 项完全转绿的另一半）不属本单——context.md 明言本线不改监督面 skill；归属监督面或另单（开放点 4 记录归属建议）。
- 与 R4 分界：release_behind/deploy_behind 的计算归 R4，本单只把它们作为 state_line/state_takeover 的一等字段透出。

## 开放点（实现方回执强制作答）

1. 九工具落在哪个（些）MCP 面：并入现有 state 面 vs 新起外门面；与 10 项「五面」清单的对齐表（tool→face 映射冻结进测试）。
2. `:7494` 只读保留范围清单（哪些 GET 保留给探针、哪些随写面一起降级）；CLI 各子命令的 MCP 对应物映射表。
3. `note_publish` 与 bus `work.note.v1`/`work.decision.v1` 的边界：note 经工具落 bus 的 payload/refs 逐字段映射；**不得**为 decision 提供第二条投递路（S11——decision 仍只走 bus 裁决路径）。
4. skill/persona 调用面切换的归属建议（监督面动作清单化，供 coordinator 升报或另派单）；8 项半绿的依据行文案固定化。

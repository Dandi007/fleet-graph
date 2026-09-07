# Spec R4（wf-4601c8）· 一线一分支 —— 三层分支模型、configure 首步 rebase、merger 推线分支（D6、D10）

> 状态：**定稿**（2026-09-05 派单前重校后定稿；round10 等待期产出草稿＋重校增补）。
> **派单前重校**（2026-09-05T04:5xZ，§5b 独立 worktree @c6c96923803…，fetch＋ls-remote 亲证当刻 origin/release/wf-4601c8 头＝c6c9692＝R3 合流头，与预期一致）：dd-acceptance 三条 argv 形参与实际判据面全部受支持——`verify-rebuild.sh --check` 仅接受 01–21（13/14/18/19 合法），单查模式恰打一行 `NN name VERDICT — …`，全量恰 21 行 `NN [a-z0-9-]+ (PASS|FAIL) — `；testenv up/down/status/mkrepo/rebuild 形参受支持（up rc=0、down rc=0、down 后 prod_references=0）。基线活体取证（--env test，原文逐字）：13 `FAIL — 窗口内 1 张单，1 张 remote_ref 非 refs/heads/release/<line-id> 或 target_base_commit 非全量非零 commit（样例: dev-fg-5e69f191d95b:remote_ref=refs/heads/dd/dev-fg-5e69f191d95b）`；14 `FAIL — 窗口内 1 张单的 events.jsonl/dd.log configure 段无 rebase 到 release/<line-id> 记录（样例 configure 事件: <空>），release_behind 读数=0（state 面字段在否: false）`；18/19 当刻已绿（R2 成果，本单义务＝实现 13/14 由红转绿且不回退已绿项）：18 `PASS — /tmp/r4-baseline-testenv/current/src 调度器唤醒路径 0 处「读 terminal.json/.scheduler 内容当 dd 终态事件」分支（机械口径: 同行命中读内容模式）`、19 `PASS — testenv 删 checkpoint 重建探针通过: rebuild ok deleted=0 rebuilt=1 dups=0 重建=ok`；全量当刻恰 21 行。样本 dev-fg-5e69f191d95b 系 testenv 引擎级 fixture 经真实图路径产出（R3 成果），非手写 record。
> 判据锚：goal.md §二 R4 与 §四纪律；design.md §1（R4 ↔ 宪法第十一条 一线一部署面；L1 约束第 4 条；D6、D10）、§2 改形复用（wf-8d9737 spec-m5-release-branch.md 为底稿：三层分支模型、configure 首步 rebase、base 冻结为 rebase 后线分支头、merger 推线分支、release_behind、越分支拒绝/rebase 缺失红/落后告警三组阴性）、§4 验收 v2 第 13、14 项；R0 实测红基线（2026-09-05 verify-rebuild 首跑：13 项窗口内 11/11 单违规，样例 remote_ref=refs/heads/dd/dev-…、base 为派单时点旧头；14 项 configure 段无 rebase 记录、state 面无 release_behind 字段）；findings【D8 冻结代价】（执行位落后分支时返工链断代——release_behind 语义必须把执行位与分支两头都照见）。与正本冲突以正本为准。

## 范围（一句话）

每线每仓一条 `release/<line-id>`：派单必从线分支头起（configure 首步 rebase，base 冻结为 rebase 后的新头），gate 后 merger 把剥离机器件的合并结果推线分支，`release_behind` 进状态面。

## 交付物

1. 引擎源码（改）：configure 段首步 rebase、`target_base_commit` 冻结语义、merger 推送目标、record 字段、state 面 `release_behind`——触点由实现方按行为契约探索定。
2. 新增 `tests/test_r4_release_branch.py`；既有测试零删除（绑定旧推送目标的用例改写，覆盖净数不减）。
3. 不碰 verify-rebuild.sh（13/14 判据已冻结）、Makefile、名册。

## 行为契约（硬性）

### 1. 一线一仓一条 release

- 改动单的目标分支必须是派单线的 `release/<line-id>`；派单意图指定其他线分支或其他仓分支 → 准入拒绝（b1-scope 同族，拒绝留痕点名冲突 ref）。
- 线内不再向 main / 其他 release 直推；`dd/dev-*` 单私有审计分支降级为可选字段（见开放点 1）。

### 2. configure 首步 rebase；base 冻结为 rebase 后头

- configure 第一步：fetch 后把 bootstrap/spec 材料变基到**派单当刻** `origin/release/<line-id>` 头。派单请求可携带期望头（旧头），若远端已前进，rebase 后以**新头**冻结 `target_base_commit`，并在 configure 回执记录 `{requested_head, actual_head, rebased: true}`；无前进时 `rebased: false`、头不变。
- rebase 冲突 → configure 失败分类为「spec 与分支前进不相容」（非环境错误），事件留痕冲突文件清单；禁静默强推或跳过 rebase。
- 冻结后的 base 贯穿 record/bootstrap/H0，验收三条命令在该头上执行。

### 3. merger 推线分支

- gate APPROVE 后，merger 把合并结果（沿用机器件剥离：`.dev-dispatch/`、`.dd-evidence/` 不进线分支）推 `refs/heads/release/<line-id>`；推送前复核远端头仍等于冻结 base（被他人前进则失败留痕，交线重走 configure——不覆盖他人提交）。
- record 对 13 项判据对齐：`remote_ref` 记 merger 实际推送的 `refs/heads/release/<line-id>`（13 项探针机械查法冻结为读 record 的 target_base_commit 与 remote_ref）；单私有审计分支若保留另立字段（如 `audit_ref`），不占用 `remote_ref`。

### 4. release_behind 进状态面

- `state_line.release_behind`：**线分支头落后 `origin/release/<line-id>` 的提交数**（派单侧视角，14 项探针读它）；另设（或复用现有部署读数）`deploy_behind`：执行位部署 commit 落后线分支头的提交数（D8 冻结代价的照见，监督面可见）。
- 两读数在 state 面 `/v1/lines` 与 state MCP `state_line` 中均为一等字段；无样本/不可得时显式标注而非缺省 0。

## 阴性用例（三组，源自 spec-m5，成对红锚+注入翻转）

1. **越分支拒绝**：以 wf-4601c8 身份派单指定 `release/wf-8d9737` 或 main → 准入拒绝+留痕点名。变异：去掉拒绝 → 红。
2. **rebase 缺失红**：删掉 configure 首步 rebase（注入跳过）→ 14 项单测与探针红（人为前进线分支一提交后派单，configure 回执必须出现 rebased:true 与新头）。变异元照 S12。
3. **落后告警**：人为前进线分支一提交 → `release_behind>0`；派单 configure rebase 后回 0。变异：读数恒 0 → 红。
4. 元：`target_base_commit`==rebase 后头逐单断言；merger 推送目标逐单==`refs/heads/release/<line-id>`；测试函数总数不减；make verify 全绿。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r4_release_branch.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r4-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r4-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; CHK(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check $1" | grep -cE "^$1 [a-z0-9-]+ PASS — "; }; c13=$( [ "$uprc" -eq 0 ] && CHK 13 ); c14=$( [ "$uprc" -eq 0 ] && CHK 14 ); c18=$( [ "$uprc" -eq 0 ] && CHK 18 ); c19=$( [ "$uprc" -eq 0 ] && CHK 19 ); lines=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " ); bash scripts/testenv.sh down --root "$R" >/tmp/r4-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r4-te-down.out | head -1); echo "up=$uprc lines=$lines c13=$c13 c14=$c14 c18=$c18 c19=$c19 down=$drc $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$c13" -eq 1 -a "$c14" -eq 1 -a "$c18" -eq 1 -a "$c19" -eq 1 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：21 行读数齐；**13/14 转 PASS**——13 base==派单时线分支头且 merger 推线分支、14 rebase 记录存在且 release_behind 回 0；18/19 维持（R2 成果不回退）；干净回收零生产引用。）

## 边界

- 只动 fleet-graph 仓引擎与测试；不部署、不碰生产名册与 /data/ronin；验收全部在 testenv 与离线单测。
- 过渡期说明：本单合流前，线侧仍按 A-3 手动合流（现状）；合流后新起改动单即走新纪律，旧单（含 dev-fg-5af16702b3c4 挂起单）不追溯。
- 与 R3 分界：gate 释放路径与 actions 信封归 R3；本单只改「从哪起、往哪合、落后多少可见」。

## 开放点（实现方回执强制作答）

1. `remote_ref` 语义迁移：13 项判据要求 record.remote_ref==`refs/heads/release/<line-id>`；单私有审计分支（现 `dd/dev-*`）保留与否、若保留放哪个字段、收割与追溯如何不受影响——给字段级方案。
2. testenv 内 13/14 样本的确定性产出（与 R3 spec 开放点 1 同族，要求共享解法）：不依赖外部网关、可重复、样本必须经真实 configure/merger 路径产生（禁手写 record 伪造）。
3. rebase 冲突的失败分类与事件 schema（spec 不相容 vs 环境错误，各自 exit/attempt 语义）；merger 推送遇远端头前进的失败重走路径。
4. `release_behind`/`deploy_behind` 的计算来源与缓存策略（读远端 or 本地跟踪引用；与 state 面刷新频率的一致性）。

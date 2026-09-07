# Spec R7a（wf-4601c8）· verify-rebuild check04 探针自足化 —— 单文件收窄版（dsv4 第四次重派 r4）

> 状态：定稿（2026-09-07 01:2x CST，R7a 第四次重派产出；两条监督者例外条叠加：① X-9 在 GLM 两档坐实同型零产出 → implement 禁用 GLM 任一档；② goal §四·一「review 座位延伸条」2026-09-07 00:5x 补签 → 本单 review 两座一并改 deepseek-v4-pro，且显式传三段 timeouts=9000s，因 review 缺省 3600s 栅栏是前单死亡的直接原因）。
> 前身单：dev-fg-95fdee440f92、dev-fg-63be35a0a2f4（X-9 零 commit 终态 failed）、dev-fg-de66bc2e5ab2（implement 成功产物 a7f7b254ec412761a73195ba05c666ff7240affa 完好在远端 refs/heads/dd/dev-fg-de66bc2e5ab2；continuous_review 两试均 X-7 型死亡：run c85eafc3 conformance-retry resume 回执已写会话库仍判死，run 477436d4 撞 3600s 缺省栅栏 re-adopt 后被 bounded-retry 耗尽杀掉）——三次均为环境/参数类终态 failed，实现者与 review 无过。本 spec 沿袭 specs/r7a-check04-dsv4.md（spec_digest sha256:6498fc6f49d0d378b567535411f82bc33a92f834c6ff6e57f64951bef79d2363）的全部行为契约、阴性红靶四组与开放点，仅新增「implement 产物复用条」并改座位/栅栏参数。
> 派单前重校：release/wf-4601c8 头=47e57c14577d718827bd031694174e3cf8c40bb7（R6 线级合并头，前置探测 ls-remote 亲证）；前置探测六次全通（ls-remote×3 + push --dry-run×3，间隔≥20s）后派。
> 判据锚：goal §二 R7（端到端验收与交付：verify-rebuild 全部通过）、§三 DoD 1（测试环境与已部署 release 双侧 exit 0）；R7 第一段双腿实测（wf-4601c8/progress.md 2026-09-06 16:2x 段，gate-r7a-evidence/ 六件原始读数）。

## 背景（实测事实，逐字在卷，沿袭原 spec）

R6 合流后 @47e57c1 双腿实测：腿A（release 检出腿）**21/21 PASS**；腿B（testenv 内部署位快照腿 /tmp/r7a-te/current）**20/21**，唯一红＝check 04（LINE_NOT_PARKED）。机理亲证：

1. `scripts/testenv.sh` up 把合成靶 `vrb-selftest-wake` 以 stall 快照（`runs/.scheduler/vrb-selftest-wake.json` 的 `parked_run_id`/`parked_at`）+ `terminal.json`（blocked/waiting_on=decision）驻停（testenv.sh 约 765–790 行 fixture）。
2. check04 投递裁决 → 唤醒成功；唤醒按设计清除 stall 快照的 `parked_run_id`/`parked_at`（`src/fleet_graph/state/run_artifacts.py` `parked_decision_state`：快照活＝驻停唯一权威）。
3. 合成靶没有真调度器循环再驻停它 → **同一 testenv 的第二次全量跑（第二腿）check04 必拒 LINE_NOT_PARKED**。复挂 fixture 后单查 04 即 PASS（consumed 记录=2）——判据本身（S10 送达即唤醒＋消费证据）已被证明，缺口在 fixture 一次性消耗，不在机制。

前单 dev-fg-de66bc2e5ab2 的 implementer 已按 specs/r7a-check04-dsv4.md 完成上述自足化改造并 commit 为 a7f7b254ec412761a73195ba05c666ff7240affa（其父链：5e5aafd「R7a: verify-rebuild check04 synthetic-target self-sufficient fixture reset」，改动面恰 `M scripts/verify-rebuild.sh` 一行产品路径）。该产物完好且在远端，本单不重做。

## 范围（一句话 · 单文件收窄 + 产物复用）

**本单只改一个文件：`scripts/verify-rebuild.sh`（且仅其 check04 探针面＋check04 头注释）。** 明文不动 `scripts/testenv.sh`；明文不改 check01–03、05–21 的任何判据文本与阈值（零弱化，B-4）。tests/ 侧断言（阳性双腿可重复、阴性不越界不弱化）**不在本单改动面**——由 gate 轮亲跑验收（cmd1 既有结构测试＋cmd3 双腿探针）承担验证义务；若实现中发现必须动 tests/ 才能达标，如实报红走返工，不许折算。

## implement 产物复用条（本单新增 · 硬性第一步）

1. **implementer 首步**：在独立 dev worktree 内 `git fetch origin dd/dev-fg-de66bc2e5ab2`，随后将远端产物 **a7f7b254ec412761a73195ba05c666ff7240affa** 取用为本单 implement 起始点（`git cherry-pick a7f7b254…` 或 `git merge a7f7b254…` 均可；取用后确认 `scripts/verify-rebuild.sh` 已含 check04 自足复位实现与头注释更新，改动面恰 `M scripts/verify-rebuild.sh` 一行产品路径）。**不重做 implement 段工作**——前单该段已按本 spec 契约完成，重做视为浪费与越界风险。
2. 取用后 implementer 的剩余义务：按工作方式约束自检改动面；跑自验（cmd1/cmd2 离线面 + cmd3 双腿探针，三条 dd-acceptance argv 逐字）；出结构化回执（verification_record 三条 argv 计数齐备）。
3. 若 fetch/cherry-pick/merge 失败或取用后自验报红，如实报红并给出最小修复，不许静默重做整段 implement。

## 工作方式约束（硬性 · X-9 直击触发器）

- **实现中大文件只许 grep / sed -n 取片段，禁整读**：`tests/test_r1_testenv.py`（659 行）、`scripts/testenv.sh`（1074 行）、`src/` 生产模块（如 src/fleet_graph/state/run_artifacts.py）等大文件，一律以 `grep -n`/`sed -n 'N,Mp'` 取所需片段；`scripts/verify-rebuild.sh` 本身按 check 函数局部读写（vrb_check_04 块约 478 行起），禁全文件整读整贴。X-9 已证：大文件 read→推理失控→单请求 32k 输出撞顶零产出（GLM 两档六次同型）。
- 改动面自检：实现收尾时 `git diff --name-only`（相对 47e57c1）必须恰一行 `scripts/verify-rebuild.sh`；多一行即越界，自查自纠后再交。

## 行为契约（硬性 · 沿袭原 spec）

- 判据零弱化：check04 的 PASS 线不变（送达 + consumed 记录/outcome=consumed 消费证据 + 下一代 unit，S10 原文不动）；只允许「投递前 fixture 复位」这一个前置动作（已由 a7f7b254 实现）。
- 只动合成靶：复位对象硬编码 `vrb-selftest-wake`（与 testenv fixture 同名）；任何其他 line id 出现在复位路径 → 视为红。
- 幂等：复位对已驻停的靶是 no-op（不重写已有 card/question note idempotency_key 冲突）。
- testenv up 的 fixture 原逻辑不动（首次驻停仍由 up 提供；check04 复位覆盖后续消耗）——本单明文不改 testenv.sh。

## 阴性用例与变异红靶（成对 · 沿袭原 spec）

1. **恒复位红**：注入「复位后跳过投递」→ check04 必须仍执行投递并核消费证据（复位≠通过）。
2. **复位越界红**：把复位目标改成其他 line id 注入 → 必须红（只许 vrb-selftest-wake）。
3. **判据弱化红**：把 check04 PASS 条件里的 consumed 证据删掉 → 既有变异红靶测试（tests/test_r0_verify_rebuild.py 结构测试）必须红。
4. **生产写路径红**：注入复位写 /data/ 路径 → 拒绝清单/用例红。

（本单 tests/ 不改动；红靶 1/2/4 由实现方在交付回执中以自测命令输出逐条举证，红靶 3 由既有结构测试承担——gate 轮核验。）

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r0_verify_rebuild.py tests/test_r1_testenv.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r7a-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r7a-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; A(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ PASS — "; }; B(){ bash -c "$V bash $R/current/scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ PASS — "; }; a1=$( [ "$uprc" -eq 0 ] && A ); b1=$( [ "$uprc" -eq 0 ] && B ); bash scripts/testenv.sh down --root "$R" >/tmp/r7a-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r7a-te-down.out | head -1); echo "up=$uprc legA_pass=$a1 legB_pass=$b1 down=$drc $refs"; test "$uprc" -eq 0 -a "$a1" -eq 21 -a "$b1" -eq 21 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据与前单 spec 逐字节一致：腿A、腿B 各 21 行全 PASS——同一 testenv 先跑 release 检出腿再跑部署位快照腿，两腿顺序跑全部 21 PASS 即 fixture 自足成立；干净回收零生产引用。）

## 座位与栅栏（本单派单参数 · 两例外条叠加）

- `stage_models`：implement=deepseek-v4-pro、continuous_review=deepseek-v4-pro、final_review=deepseek-v4-pro（goal §四·一 implementer 座位例外条 ＋ review 座位延伸条，监督者补签）。
- `timeouts`：implement=9000s、continuous_review=9000s、final_review=9000s（review 缺省 3600s 栅栏是前单 run 477436d4 死亡直接原因；X-7 附加建议 ④「review 段栅栏对重推理 review 偏短，派单时按 spec 显式传」）。

## 边界（单文件硬界）

- 仓内**只动** `scripts/verify-rebuild.sh` 的 check04 面＋头注释；不碰 `scripts/testenv.sh`、不碰 `tests/`、不碰 `src/` 生产模块、不改 check01–03/05–21 判据。
- 不做生产面动作（B-1/B-3 不在本单）；验收全部在 /tmp/r7a-accept-testenv 与离线单测。
- 既有验收断言零弱化（B-4）。
- 旧单资产勿动：dev-fg-5af16702b3c4（X-3 废单）勿查勿动；dev-fg-95fdee440f92 / dev-fg-63be35a0a2f4 / dev-fg-de66bc2e5ab2 三旧单的 worktree 与 dd 数据为引擎资产，只读不改（fetch 远端分支除外）。

## 开放点（实现方回执强制作答 · 沿袭原 spec，追加第 4 点）

1. fixture 复位写 stall 快照时 generation 字段的处理（递增 vs 保持）——与决策台账 consumed 记录的可解释性（腿B consumed=2 先例在卷）。
2. check15/16 共用同一合成靶（只读 inbox/驻停面，不消耗 park）——确认复位顺序对 15/16 无副作用，逐条回执。
3. 若发现 check04 复位仍无法覆盖某边序（如单查模式后紧接全量跑），如实报红并给出最小补法，不许折算。
4. 产物复用条执行情况：取用方式（cherry-pick / merge）、取用后 base..HEAD 改动面自检结果（须恰 `M scripts/verify-rebuild.sh`）、是否零重做——逐项回执。
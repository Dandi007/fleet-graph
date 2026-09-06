# Spec R7a（wf-4601c8）· verify-rebuild check04 探针自足化 —— 双腿 21/21 收口

> 状态：定稿（2026-09-06 16:4x CST，R7 第一段双腿实测后产出）。
> 派单前重校：release/wf-4601c8 头=47e57c14577d718827bd031694174e3cf8c40bb7（R6 线级合并头，ls-remote 亲证×3）；前置探测六次全通（ls-remote×3 + push --dry-run×3，间隔≥20s，2026-09-06 16:51–16:53 CST）。
> 判据锚：goal §二 R7（端到端验收与交付：verify-rebuild 全部通过）、§三 DoD 1（测试环境与已部署 release 双侧 exit 0）；R7 第一段双腿实测（wf-4601c8/progress.md 2026-09-06 16:2x 段，gate-r7a-evidence/ 六件原始读数）。

## 背景（实测事实，逐字在卷）

R6 合流后 @47e57c1 双腿实测：腿A（release 检出腿）**21/21 PASS**；腿B（testenv 内部署位快照腿 /tmp/r7a-te/current）**20/21**，唯一红＝check 04（LINE_NOT_PARKED）。机理亲证：

1. `scripts/testenv.sh` up 把合成靶 `vrb-selftest-wake` 以 stall 快照（`runs/.scheduler/vrb-selftest-wake.json` 的 `parked_run_id`/`parked_at`）+ `terminal.json`（blocked/waiting_on=decision）驻停（testenv.sh 约 765–790 行 fixture）。
2. check04 投递裁决 → 唤醒成功；唤醒按设计清除 stall 快照的 `parked_run_id`/`parked_at`（`src/fleet_graph/state/run_artifacts.py` `parked_decision_state`：快照活＝驻停唯一权威）。
3. 合成靶没有真调度器循环再驻停它 → **同一 testenv 的第二次全量跑（第二腿）check04 必拒 LINE_NOT_PARKED**。复挂 fixture 后单查 04 即 PASS（consumed 记录=2）——判据本身（S10 送达即唤醒＋消费证据）已被证明，缺口在 fixture 一次性消耗，不在机制。

## 范围（一句话）

verify-rebuild check04 探针自足化：探针在投递前把合成靶驻停 fixture 复位（与 testenv up 同法），使 check04 在同一 testenv 内可重复执行（任意次全量跑、单查、双腿顺序跑均不依赖外部一次性状态）。

## 交付物

1. `scripts/verify-rebuild.sh` check04 改（且仅此面）：投递前若合成靶未被驻停（stall 快照 parked_run_id/parked_at 缺失或不一致），按 testenv up 同一写法复位 fixture（写 stall 快照 parked_run_id/parked_at + terminal.json blocked/waiting_on=decision；board card/question note 若缺一并补，idempotency_key 语义照旧），再投递。复位只允许落在 `$VRB_TEST_ROOT/runs/`（--env test 面）；非 --env test 面 check04 保持现状（不造生产状态）。fixture 复位动作打一行探针日志（stderr 或依据行内注明「fixture reset」），可审计。
2. `tests/` 机械断言（新增或并入既有判据面测试）：
   - 阳性：check04 连续两次对同一 testenv 全量跑均 PASS（模拟腿A→腿B 顺序，20 项其余不回归）；
   - 阴性：对未驻停靶投递必须拒 LINE_NOT_PARKED（不因复位逻辑把拒绝面吃掉——复位仅限 vrb-selftest-wake 合成靶，对其他任意靶名不得复位、不得投递成功）；
   - 阴性：复位逻辑不得写 `$VRB_TEST_ROOT` 之外任何路径（生产路径零写入断言，与既有 testenv 拒绝清单一致）。
3. 文档一处：`scripts/verify-rebuild.sh` check04 头注释更新（fixture 一次性语义 → 自足复位语义）；不改 check01–03、05–21 的任何判据文本与阈值（B-4 零弱化）。

## 行为契约（硬性）

- 判据零弱化：check04 的 PASS 线不变（送达 + consumed 记录/outcome=consumed 消费证据 + 下一代 unit，S10 原文不动）；只允许新增「投递前 fixture 复位」这一个前置动作。
- 只动合成靶：复位对象硬编码 `vrb-selftest-wake`（与 testenv fixture 同名）；任何其他 line id 出现在复位路径 → 用例红。
- 幂等：复位对已驻停的靶是 no-op（不重写已有 card/question note idempotency_key 冲突）。
- testenv up 的 fixture 原逻辑不动（首次驻停仍由 up 提供；check04 复位覆盖后续消耗）。

## 阴性用例与变异红靶（成对）

1. **恒复位红**：注入「复位后跳过投递」→ check04 必须仍执行投递并核消费证据（复位≠通过）。
2. **复位越界红**：把复位目标改成其他 line id 注入 → 用例红（只许 vrb-selftest-wake）。
3. **判据弱化红**：把 check04 PASS 条件里的 consumed 证据删掉 → 既有变异红靶测试（tests/test_r0_verify_rebuild.py 结构测试）必须红。
4. **生产写路径红**：注入复位写 /data/ 路径 → 拒绝清单/用例红。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r0_verify_rebuild.py tests/test_r1_testenv.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r7a-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r7a-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; A(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ PASS — "; }; B(){ bash -c "$V bash $R/current/scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ PASS — "; }; a1=$( [ "$uprc" -eq 0 ] && A ); b1=$( [ "$uprc" -eq 0 ] && B ); bash scripts/testenv.sh down --root "$R" >/tmp/r7a-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r7a-te-down.out | head -1); echo "up=$uprc legA_pass=$a1 legB_pass=$b1 down=$drc $refs"; test "$uprc" -eq 0 -a "$a1" -eq 21 -a "$b1" -eq 21 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：腿A、腿B 各 21 行全 PASS——同一 testenv 先跑 release 检出腿再跑部署位快照腿，两腿顺序跑全部 21 PASS 即 fixture 自足成立；干净回收零生产引用。）

## 边界

- 仓内只动 fleet-graph 仓 scripts/verify-rebuild.sh 的 check04 面 + tests/ 对应断言；不碰 src/ 生产模块、不改 testenv.sh 的 up/down 语义、不改 check01–03/05–21 判据。
- 不做生产面动作（B-1/B-3 不在本单）；验收全部在 /tmp/r7a-accept-testenv 与离线单测。
- 既有验收断言零弱化（B-4）。

## 开放点（实现方回执强制作答）

1. fixture 复位写 stall 快照时 generation 字段的处理（递增 vs 保持）——与决策台账 consumed 记录的可解释性（腿B consumed=2 先例在卷）。
2. check15/16 共用同一合成靶（只读 inbox/驻停面，不消耗 park）——确认复位顺序对 15/16 无副作用，逐条回执。
3. 若发现 check04 复位仍无法覆盖某边序（如单查模式后紧接全量跑），如实报红并给出最小补法，不许折算。

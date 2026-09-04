# Spec R1-fix（wf-4601c8）· testenv.sh up 失败路径完全回收 + 负例密闭化

> 状态：定稿（2026-09-05，worker 复现后撰写）。前单 dev-fg-ed22cba56b2c（target_base 99103be，head bc56545）终态 refused（ACCEPTANCE_FAILED）：cmd1/cmd2 同败于 tests/test_r1_testenv.py:291。判据锚：goal.md §四·一 A-1/A-6（加断言/加阴性可自决）、§四「弱化验收＝B-4 升报线，禁止」；specs/r1-isolated-test-env.md（原 R1 spec，语义正本）。
> 本单不改 R1 spec 的任何既有语义，只修两件事：up 失败路径的完全回收（代码缺陷）与该负例用例的密闭化（测试缺陷）。

## 交付物（恰好两个文件，其余零改动）

1. `scripts/testenv.sh`（修改：up 失败路径完全回收）
2. `tests/test_r1_testenv.py`（修改：仅 `test_denies_production_port_mutation` 密闭化与断言强化；其余用例一行不动）

零改动清单：`scripts/verify-rebuild.sh`、`tests/test_r0_verify_rebuild.py`、`Makefile`、其余一切文件。零测试删除。

## 一、缺陷事实（已双重复核＋真机复现，行号指 bc56545）

- 机理：`te_launch`（scripts/testenv.sh 行 199–204）先写 `TEST_ROOT/pids/<face>.pid` 再 exec；`cmd_up` 流程 refuse_checks→write_layout→te_spawn_faces→await_ready；就绪超时（行 381–385）→ `kill_all`（行 347–368，只杀进程、从不删 pid 文件）→ die 4。故「已拉起面之后」的失败退出确定性残留 pid 文件。
- 真机复现（2026-09-05 04:4xZ，一次性 worktree @ bc56545）：七默认面端口全空闲时 up（bus 面就绪必败配置）exit 4 ＋ pids 残留 7 个 pid 文件 ＋ 七 pid 全 dead；残留 pid 文件使再 up 落「部分存活（0/7）」exit 3——失败后不能干净重来的次生伤害。
- 负例非密闭三穴：㈠ TEST_ROOT 用 pytest tmp_path——basetemp 落生产根（如座位 TMPDIR 在 /data/fleet-graph 之下）时 check_root_deny 提前 die 2、未建 pids 目录 → 空真绿；㈡ `FGT_PORT_BUS_HTTP=17590` 固定——机器当刻占用该口时 bind 探测提前 die 2 → 空真绿；㈢ 六个 `PORT_KNOBS` 的 free_port() 释放-再占竞争（次要）。

## 二、修复硬性要求

### 1. scripts/testenv.sh——「已拉起面之后」的失败退出必须完全回收

- 就绪等待超时（die 4）路径：击杀并核验全灭（与 down 同一核验口径：kill -0 不可达或仅剩 Z 视为灭）之后、die 之前，删除全部面 pid 文件。若核验不灭（理论 D 态）：如实点名未灭面、保留 pid 文件取证、仍 die 4——宁留证据，不假报回收。
- 判据（验收锚）：可杀场景下 exit 4 的终态＝无存活面进程 且 `TEST_ROOT/pids` 目录为空（或不存在）；据此失败后的下一次 up 不再触发「部分存活 0/N → exit 3」。
- 语义不变面（不许动）：成功 up 的布局/摘要 `up=1 surfaces=a/b root=…`；幂等判定（全部 pid 存活 → 只打摘要 exit 0；部分存活 → exit 3，该分支在 refuse_checks 之前、由预置 pidfile 驱动，`test_up_idempotent_and_partial_refuses` 必须原样通过）；down 的击杀→核验→rm pid 文件→`down=1 prod_references=<n>`；status 四 token 行与 prod_write_fds 算法；mkrepo 幂等；拒绝清单三条与「拒绝零副作用」。
- 边界：纯 TEST_ROOT 内回收；不新增任何对生产路径/端口的读写；不建 systemd unit。

### 2. tests/test_r1_testenv.py——`test_denies_production_port_mutation` 密闭化（只改这一个用例）

- TEST_ROOT：mkdtemp 于真实 /tmp（照 `tmp_root()` 同法），不得用 pytest tmp_path——修复后该用例在任意 basetemp/TMPDIR 放置下结果一致。
- 端口：七个 `FGT_PORT_*` 全部由测试自选空闲端口；不得固定任何生产端口集成员（含 17590）或机器默认端口。生产端口集判定仍须被验证为「摘除后红」：以 `FGT_DENY_PORTS` 测试后门把测试自选的一个端口注入测试自设 deny 集（该后门是原 R1 spec §一·3 明文的测试替换面）。
- 断言（只加强，不得弱化既有任何断言——B-4 红线）：
  - 未变异红锚：同 env 下跑真 `scripts/testenv.sh` up → 非零退出、stderr 点名「属生产端口集」、TEST_ROOT/pids 未建（拒绝零副作用）；
  - 变异侧（注入方式不变：把 `if port_in_deny_list "$port"; then` 替换为永假条件）：显式断言 `returncode == 4`；stderr 不含「属生产端口集」；保留原断言原文语义「变异副本的就绪失败路径必须已把进程全部击杀回收」＝ TEST_ROOT/pids 目录不存在或为空；
  - docstring 如实描述密闭机制；`test_zero_test_deletion_r0_file_unchanged` 与其余既有用例一行不动。

## 三、验收判据（阳/阴，与前单红因一一对应）

- a) 七面端口全空闲时变异副本场景 exit 4 且 TEST_ROOT/pids 为空（失败路径完整回收：进程与 pid 文件皆无）。
- b) 该负例密闭：七个 FGT_PORT_* 全由测试自选空闲端口，显式断言退出码 4 与 stderr 不再点名生产端口集。
- c) `uv run pytest -q tests/test_r1_testenv.py` 连跑 ≥3 次与 `make verify` 连跑 ≥3 次全绿、零测试删除。
- d) testenv.sh 正常 up/status/down 语义不变：幂等 up=1、部分存活 exit 3、down 后 pids 清空、prod_write_fds=0、prod_references=0（既有用例＋cmd3 生命周期命令守护）。

## 四、验证环境注意

- 验收在 dd 隔离 unit 跑（其 TMPDIR 为真实 /tmp）；worker 已实测：单测试文件 ≈12s、`make verify`（2968 用例）≈105s——新验收总量 ≈8 分钟，默认 3600s 栅栏充裕。
- 测试全部离线自足：tmp + 测试后门（FGT_DENY_PORTS / FGT_READY_TIMEOUT / FGT_AGENT_BUS_ROOT 指不存在处）；不打生产端口、不写生产路径；变异副本的 REPO_ROOT 指 tmp（无 venv），任何「摘除守卫后继续走」的路径都在拒绝或缩短就绪等待处确定失败。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && for i in 1 2 3; do uv run pytest -q tests/test_r1_testenv.py || exit 9; done'
bash -lc 'for i in 1 2 3; do env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify || exit 8; done'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r1-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r1-te-up.out 2>&1; uprc=$?; env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash scripts/verify-rebuild.sh --env test --root "$R" >/tmp/r1-vrb-test.out 2>&1; vrc=$?; bash scripts/testenv.sh status --root "$R" >/tmp/r1-te-status.out 2>&1; bash scripts/testenv.sh down --root "$R" >/tmp/r1-te-down.out 2>&1; drc=$?; lines=$(grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " /tmp/r1-vrb-test.out); pass102=$(grep -cE "^(01|02|10) [a-z0-9-]+ PASS — " /tmp/r1-vrb-test.out); wfds=$(grep -oE "prod_write_fds=[0-9]+" /tmp/r1-te-status.out | head -1); refs=$(grep -oE "prod_references=[0-9]+" /tmp/r1-te-down.out | head -1); echo "up=$uprc lines=$lines pass010=$pass102 vrb_exit=$vrc down=$drc $wfds $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$pass102" -eq 3 -a "$drc" -eq 0 -a "$wfds" = "prod_write_fds=0" -a "$refs" = "prod_references=0"'\'''
```

（cmd1/cmd2 相比前单各加强为连跑三遍（判据 c）；cmd3 与前单逐字相同（判据 d 生命周期守护，未弱化）。）

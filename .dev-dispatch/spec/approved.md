# Spec R1（wf-4601c8）· scripts/testenv.sh —— 与生产隔离的测试环境（M9 / D8）

> 判据锚：wf-4601c8 goal.md §二 R1 与 §四纪律、§四·一 A-4（测试环境不是生产）；design.md §1（R1 ↔ 宪法第十一条、第十二条；L1 约束第 4 条；D8/§1.3、M9）、§3 自决「R1 先于 R2」、§4 验收标准 v2 第 19/20 项；findings.md【D8 冻结代价】。与正本冲突以正本为准。

## 交付物（恰好三个文件，其余零改动）

1. `scripts/testenv.sh`（新增，可执行 bash，chmod +x）
2. `scripts/verify-rebuild.sh`（修改：**只**新增 `--env test` 模式，见「改动边界」）
3. `tests/test_r1_testenv.py`（新增）

前置依赖：R0 已合流（`scripts/verify-rebuild.sh` 二十一项与 `VRB_*` knob 已存在于目标分支）。

## 一、testenv.sh 行为契约（硬性）

### 1. 子命令与总则

- `scripts/testenv.sh up|down|status|mkrepo <name> [--root PATH]`；`--root` 缺省 `$FGT_ROOT`，再缺省 `/tmp/fleet-graph-testenv`（下称 TEST_ROOT）。
- 纯 bash；`set -u` + `set -o pipefail`，无全局 `set -e`；开头 `unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy no_proxy NO_PROXY 2>/dev/null || true`（回环卫生，S6）；回环 curl 一律 `--noproxy '*'`。
- 代码源＝脚本所在仓（`${BASH_SOURCE[0]}/..` 自定位，只读使用）：直接 `uv run` 该仓的引擎与各面，**不**翻 symlink、**不**碰 /data/apps、**不**部署。
- 拉起方式＝普通子进程（setsid/nohup）+ TEST_ROOT/pids/*.pid + TEST_ROOT/logs/*.log；**禁止创建或修改任何 systemd unit**（生产 unit 名与 `testenv-` transient unit 都不建）；`systemctl` 只允许出现在被替换的 stub 里。
- 一切数据（run root、dd root、goal queue、名册、token、workfolders、repos、stub、日志）只落 TEST_ROOT；除 /tmp 外不得写 TEST_ROOT 之外任何路径。

### 2. TEST_ROOT 布局（up 创建）

```
TEST_ROOT/
  env/knobs.sh          # verify-rebuild.sh --env test 的唯一输入（见二）
  pids/  logs/  bin/systemctl-stub  stubs/ledger*（05 用）
  runs/   dd/   scheduler/   workfolders/   repos/   secrets/   config/ronin-lines.json   personas/
```

- 独立 run root=TEST_ROOT/runs、dd root=TEST_ROOT/dd、goal queue=TEST_ROOT 下独立队列、独立名册 config/ronin-lines.json（至少一条 `testenv-sample` 线，enabled=false 或等价不参与调度的安全位）、独立 alias token 落 TEST_ROOT/secrets（**绝不** touch /data/ronin）、work folder 能力＝workfolder 服务数据根 TEST_ROOT/workfolders（复用 katana-work-folder 代码只读运行，端口独立；若引擎支持以数据根参数直指 TEST_ROOT/workfolders 则可不另起进程——实现二选一，依据写进 status）。
- `mkrepo <name>`：创建 TEST_ROOT/repos/name.git（bare）+ TEST_ROOT/repos/name 工作克隆（remote=本地 bare），打印两路径；供测试环境内派单的「可造目标仓」。幂等：已存在则只打印路径。

### 3. 端口与拒绝清单（阴性核心）

- 固定端口映射（可用 `FGT_PORT_<面>` 覆盖）：bus HTTP 27490、bus MCP 25608、dd MCP 25610、goal MCP 25611、decision MCP 25614、state HTTP 27494、workfolder 25618。
- **up 与 `--env test` 双侧都执行同一拒绝判定**，任一命中即拒绝（退出非零、报错点名冲突项，零副作用——拒绝时不得已启动任何进程）：
  1. 目标端口 ∈ 生产端口集 {5608,5610,5611,5614,7490,7491,7493,7494,17590,9090,15722}；
  2. 目标端口在 up 当刻已被监听（bind 探测）；
  3. TEST_ROOT 等于或位于任一生产根之下，或 symlink 解析后位于其下：/data/fleet-graph、/data/apps、/data/ronin、/data/agent-bus、/data/code/self、/data（整个 /data）；
  4. knobs.sh 任一 VRB_* 值指向上述生产根或生产端口（对 `verify-rebuild.sh --env test` 侧的要求，见二）。
- 拒绝清单默认内置；`FGT_DENY_PATHS` / `FGT_DENY_PORTS` 仅供测试替换 fixture 用（文档注明这是测试后门，生产不设此二环境变量）。

### 4. up / status / down 语义

- **up**：布局检查 → 逐面拉起（引擎 + state + dd/goal/decision MCP + agent-bus server 与 gateway + workfolder）→ 就绪等待（每面 TCP+一次 tools/list 或等价 GET，总计 ≤60s；超时则全部击杀、exit 4、点名未就绪面）→ 写 env/knobs.sh → 产出生产基线（见 5）→ 打印一行摘要 `up=1 surfaces=<alive>/<total> root=<TEST_ROOT>`。幂等：全部 pid 存活时重复 up → 只打印摘要 exit 0；部分存活 → exit 3 报「先 down」，不得静默自愈。
- **status**：输出稳定 token 行（机器可查）：`up=<0|1>`、`pids=<n>`、`surfaces=<alive>/<total>`、`prod_write_fds=<n>`。`prod_write_fds` 的算法：对每个存活 pid，逐 /proc/`<pid>`/fd 读 fdinfo flags，凡带写位（O_WRONLY/O_RDWR）的 fd readlink 目标落在生产根（3 条之集合）即计数；>0 时逐条列出 pid+路径。这是「测试环境没写生产」的因果证明，不受生产自身写入抖动影响。
- **down**：对全部 pid SIGTERM（宽限 10s）→ SIGKILL → 核验全灭 → 输出 `down=1 prod_references=<n>`。`prod_references`：在 /data/fleet-graph 的引擎写入面（runs/*.json、runs/*/*.jsonl、dd/*/record.json、dd/*/events.jsonl，只读、限时）grep 字面 TEST_ROOT 路径串，命中数必须为 0。数据默认保留（取证），`--purge` 才删 TEST_ROOT。已 down 时 down → exit 0。
- **基线留痕**：up 时把 `GET 127.0.0.1:7494/v1/lines`（jq 排序快照）与 /data/fleet-graph/runs 树清单（路径+mtime）存 TEST_ROOT/prod-baseline/；down 时重取并 diff，差异行并入 down 输出（信息性——生产自身在跑会自然演进，故不作硬断言；硬断言是 prod_write_fds=0 与 prod_references=0 两个因果证明）。

### 5. 05 号检查的 stub

testenv 为 `VRB_LLM_LEDGER` 提供 stub（静态空 request_events 响应）：`file://TEST_ROOT/stubs/ledger.json`（若 R0 探针支持 file）或 PORT_BASE 内一个极简 python http server 固定 JSON，二选一；要求 `--env test --check 05` 产出有依据的读数而非崩溃。

## 二、verify-rebuild.sh 的 `--env test` 模式（改动边界，硬性）

- 新增参数 `--env test [--root PATH]`：在 unset 代理之后、任何探针之前，source `TEST_ROOT/env/knobs.sh`，然后按既有 01–21 主循环运行。
- **fail-closed（最硬的一条）**：knobs.sh 缺失、TEST_ROOT 不存在、或任一 VRB_* 值命中拒绝清单（一·3 之 3/4 条）→ 立即 exit 2、stderr 点名缺失/越界的 knob，**绝不回退生产默认值**、不输出任何 `NN … PASS|FAIL` 行。
- knobs.sh 由 testenv.sh up 生成：纯 `VRB_*=值` 赋值（无命令替换、无 source 链），至少覆盖 R0 spec 第 6 条全部 knob（VRB_SYSTEMCTL=TEST_ROOT/bin/systemctl-stub、VRB_CURRENT 指向仓自身或 TEST_ROOT 内快照、VRB_BUS_BASE/VRB_STATE_BASE/四个 VRB_MCP_*、VRB_RUNS_ROOT/VRB_SCHED_DIR/VRB_DD_ROOT、VRB_ROSTER=TEST_ROOT/config/ronin-lines.json、VRB_SKILL_FILE/VRB_PERSONA_FILES 指 TEST_ROOT 内文件、VRB_LLM_LEDGER=stub）。
- systemctl-stub 由 testenv 生成：能应答 `--user list-units 'agent-bus-*' --plain --no-legend`（按 TEST_ROOT pids 应答 testenv 的面）与 `--user show <unit> -p MainPID --value`。
- **除上述外零改动**：01–21 判定逻辑、`vrb_check_NN` 函数名、输出格式、退出码语义、默认（无 --env）行为逐字节等价；既有 tests/test_r0_verify_rebuild.py 一行不动且全绿（零测试删除）。
- R1 的阳性口径（goal §二 R1 原文机械化）：`--env test` 跑出恰好 21 行、每行依据非空（「逐项有读数」，不要求全 PASS——R2–R6 未做，红是诚实起点）；机制只依赖测试环境自身的项 **01/02/10 应 PASS**（stub systemctl 无试验实例、新 bus 无 coord. 协议、五面 tools/list 全应答）。

## 三、变异红靶（tests/test_r1_testenv.py；成对：红锚 + 注入翻转，照 R0 spec 的 S12 精神）

1. `test_denies_test_root_under_production`：`--root /data/fleet-graph/x`（及 /data/ronin/t、/data/apps）→ up 拒绝非零、报错含路径、无进程无目录副作用。变异元：删除该判定后同用例红。
2. `test_denies_production_and_occupied_port`：`FGT_PORT_BUS_HTTP=7490` → 拒绝点名 7490；对任一目标端口先占住（python 监听）→ 拒绝。变异元同上。
3. `test_env_test_fail_closed_no_knobs`：`--env test --root /nonexistent` → exit 2、stderr 点名、stdout 无任何 `NN … PASS|FAIL` 行（证明没有偷偷打生产）。变异元：把「缺 knobs 回退默认」注入后用例红。
4. `test_env_test_rejects_knob_pointing_at_production`：手写 knobs.sh 令 VRB_RUNS_ROOT=/data/fleet-graph/runs（另测 VRB_BUS_BASE=:7490）→ exit 2 点名 knob。变异元：去掉 knobs 校验后红。
5. `test_status_prod_write_fds`：以 sleep/持有写 fd 的 python 假进程 + 假 pidfile 驱动 status（FGT_DENY_PATHS 指向 tmp 假生产根）：无写 fd → `prod_write_fds=0`；持写 fd 于假生产根 → ≥1 且列出 pid。变异元：短路 fd 扫描后红侧用例红。
6. `test_up_idempotent_and_partial_refuses` + `test_down_idempotent`：全活重复 up exit 0；残缺 exit 3；down 后 pid 文件清、重复 down exit 0。
7. `test_verify_rebuild_default_mode_unchanged`：`bash -n` 两脚本；grep 01–21 函数名仍在；`--check 99` 仍非零报错；`--env` 缺参数报错。
8. 元/结构：testenv.sh 可执行位、up 摘要与 status/down 的 token 行格式、mkrepo 幂等、零测试删除断言（R0 测试文件哈希不变）。

## 四、边界

- 只动 fleet-graph 仓上述三文件；agent-bus 与 katana-work-folder 代码只读复用，不改动那两个仓。
- 不部署、不翻 symlink、不建 systemd unit、不碰 /data/ronin、不碰生产名册；对 /data/fleet-graph 与 :7494 只读（基线快照、prod_references grep）。
- 测试全部离线自足（tmp + 假进程 + 假 deny 清单），不依赖生产端口可达、不依赖真实 daemon 常驻。
- 派单 acceptance 里允许真 up 一回（见下），必须在 fenced 命令内完成击杀回收。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r1_testenv.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r1-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r1-te-up.out 2>&1; uprc=$?; env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash scripts/verify-rebuild.sh --env test --root "$R" >/tmp/r1-vrb-test.out 2>&1; vrc=$?; bash scripts/testenv.sh status --root "$R" >/tmp/r1-te-status.out 2>&1; bash scripts/testenv.sh down --root "$R" >/tmp/r1-te-down.out 2>&1; drc=$?; lines=$(grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " /tmp/r1-vrb-test.out); pass102=$(grep -cE "^(01|02|10) [a-z0-9-]+ PASS — " /tmp/r1-vrb-test.out); wfds=$(grep -oE "prod_write_fds=[0-9]+" /tmp/r1-te-status.out | head -1); refs=$(grep -oE "prod_references=[0-9]+" /tmp/r1-te-down.out | head -1); echo "up=$uprc lines=$lines pass010=$pass102 vrb_exit=$vrc down=$drc $wfds $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$pass102" -eq 3 -a "$drc" -eq 0 -a "$wfds" = "prod_write_fds=0" -a "$refs" = "prod_references=0"'\'''
```

（第三条不要求 vrb_exit=0：R2–R6 未做，红是诚实起点；判据＝21 行读数 + 01/02/10 绿 + 双因果零写证明 + 干净回收。）

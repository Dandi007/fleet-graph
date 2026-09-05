#!/usr/bin/env bash
#
# testenv.sh — wf-4601c8 R1：与生产隔离的测试环境（M9 / D8）。
#
# 判据锚：wf-4601c8 goal.md §二 R1 与 §四纪律、§四·一 A-4（测试环境不是生产）；
#         design.md §1（R1 ↔ 宪法第十一条、第十二条；L1 约束第 4 条；D8/§1.3、M9）。
#
# 职责：直接 `uv run`（或用已就绪的 .venv）本仓的引擎与各面，在本仓代码上拉起一套
#        一切数据只落 TEST_ROOT 的完整测试栈（引擎 + state + dd/goal/decision MCP +
#        agent-bus server 与 gateway；workfolder 能力由 goal serve 的
#        --work-folder-root 数据根直指 TEST_ROOT/workfolders 承担，不另起进程，
#        依据见 status 输出的 workfolder= 行）。agent-bus 代码自其仓只读复用，
#        不翻 symlink、不碰 /data/apps、不部署、不建任何 systemd unit——
#        拉起方式是普通 setsid 子进程 + TEST_ROOT/pids/*.pid + TEST_ROOT/logs/*.log，
#        `systemctl` 只以本脚本生成的 stub（TEST_ROOT/bin/systemctl-stub）出现，
#        那是 verify-rebuild 01 项探针的被替换物，不是进程管理器。
#
# 用法：scripts/testenv.sh up|down|status|mkrepo <name>|rebuild [--root PATH] [--purge]
#   --root 缺省 $FGT_ROOT，再缺省 /tmp/fleet-graph-testenv（下称 TEST_ROOT）。
#
#   up        布局与拒绝清单检查（零副作用）→ 幂等判定 → 逐面拉起 → 就绪等待
#             （每面 TCP+tools/list 或等价 GET，总计 ≤60s；超时全部击杀并核验
#             全灭、删除面 pid 文件——核验不灭则保留 pid 文件取证——exit 4、
#             点名未就绪面）→ 写 env/knobs.sh → 生产基线留痕 →
#             打印 `up=1 surfaces=<alive>/<total> root=<TEST_ROOT>`。
#             幂等：全部 pid 存活时重复 up → 只打印摘要 exit 0；部分存活 →
#             exit 3 报「先 down」，不得静默自愈。
#   status    稳定 token 行：`up=<0|1>`、`pids=<n>`、`surfaces=<alive>/<total>`、
#             `prod_write_fds=<n>`（>0 时逐条列出 pid+路径），另附 workfolder=
#             依据行。prod_write_fds 算法：对每个存活 pid 逐 /proc/<pid>/fd 读
#             fdinfo flags，凡带写位（O_WRONLY/O_RDWR）且 readlink 目标落在生产根
#             （一·3 条 3 之集合）的 fd 即计数——「测试环境没写生产」的因果证明。
#   down      对全部 pid SIGTERM（宽限 10s）→ SIGKILL → 核验全灭 →
#             `down=1 prod_references=<n>`。prod_references：在 /data/fleet-graph
#             的引擎写入面（runs/*.json、runs/*/*.jsonl、dd/*/record.json、
#             dd/*/events.jsonl；只读、限时 15s）grep 字面 TEST_ROOT 路径串。
#             数据默认保留（取证），`--purge` 才删 TEST_ROOT；已 down 时 down → exit 0。
#   mkrepo    创建 TEST_ROOT/repos/<name>.git（bare）+ TEST_ROOT/repos/<name>
#             工作克隆（remote=本地 bare），打印两路径；幂等：已存在只打印。
#   rebuild   R2 图合一 A 方案探针：checkpoint 库（sqlite）退为可删缓存——
#             枚举并删除 TEST_ROOT/runs/*/checkpoint.sqlite3，再对每张
#             dd/<dev>/record.json 从权威件（record.json + 当代 result.json）
#             重建图状态投影（重建输入只有持久权威件，绝不借 checkpoint 或
#             .scheduler 补状态），并校验同 (repo_path, spec_digest) 无重复
#             派单目录。末行打印
#             `rebuild ok deleted=<n> rebuilt=<n> dups=<n> 重建=ok`；rc=0。
#
# 拒绝清单（阴性核心；up 与 verify-rebuild --env test 双侧同判，任一命中即拒绝、
# 退出非零、报错点名冲突项、零副作用）：
#   1. 目标端口 ∈ 生产端口集 {5608,5610,5611,5614,7490,7491,7493,7494,17590,9090,15722}；
#   2. 目标端口在 up 当刻已被监听（bind 探测）；
#   3. TEST_ROOT 等于或位于任一生产根之下（symlink 解析后同判）：
#      /data/fleet-graph、/data/apps、/data/ronin、/data/agent-bus、/data/code/self、/data。
# 拒绝清单默认内置；FGT_DENY_PATHS（冒号分隔）/ FGT_DENY_PORTS（空白分隔）仅供
# 测试替换 fixture 用——这是测试后门，生产不设此二环境变量。同类的测试后门还有：
# FGT_READY_TIMEOUT（就绪等待上限，默认 60s）、FGT_PROD_GREP_ROOT（prod_references
# 的 grep 根与生产基线 runs 树快照根，默认 /data/fleet-graph，对生产只读）、
# FGT_AGENT_BUS_ROOT（agent-bus 代码源，默认 /data/code/self/agent-bus）。生产一律不设。
#
# 固定端口映射（可用 FGT_PORT_<面> 覆盖）：bus HTTP 27490、bus MCP 25608、
# dd MCP 25610、goal MCP 25611、decision MCP 25614、state HTTP 27494、workfolder 25618。
#
# 代理卫生（S6）：开头 unset 全部代理变量；回环 curl 一律 --noproxy '*'。
set -u
set -o pipefail

unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy no_proxy NO_PROXY 2>/dev/null || true

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------- 拒绝清单（默认内置；FGT_* 仅测试 fixture 替换用） ----------------
PROD_DENY_PATHS="${FGT_DENY_PATHS:-/data/fleet-graph:/data/apps:/data/ronin:/data/agent-bus:/data/code/self:/data}"
PROD_DENY_PORTS="${FGT_DENY_PORTS:-5608 5610 5611 5614 7490 7491 7493 7494 17590 9090 15722}"

# ---------------- 固定端口映射（FGT_PORT_<面> 可覆盖） ----------------
P_BUS_HTTP="${FGT_PORT_BUS_HTTP:-27490}"
P_BUS_MCP="${FGT_PORT_BUS_MCP:-25608}"
P_DD_MCP="${FGT_PORT_DD_MCP:-25610}"
P_GOAL_MCP="${FGT_PORT_GOAL_MCP:-25611}"
P_DECISION_MCP="${FGT_PORT_DECISION_MCP:-25614}"
P_STATE_HTTP="${FGT_PORT_STATE_HTTP:-27494}"
P_WORKFOLDER="${FGT_PORT_WORKFOLDER:-25618}"

FACE_PORTS="bus-http:$P_BUS_HTTP bus-mcp:$P_BUS_MCP dd-mcp:$P_DD_MCP goal-mcp:$P_GOAL_MCP decision-mcp:$P_DECISION_MCP state-http:$P_STATE_HTTP workfolder:$P_WORKFOLDER"

# agent-bus 代码源（只读复用，绝不写入）。
AGENT_BUS_ROOT="${FGT_AGENT_BUS_ROOT:-/data/code/self/agent-bus}"

USAGE="用法: scripts/testenv.sh up|down|status|mkrepo <name> [--root PATH] [--purge]"

die() { printf 'testenv: %s\n' "$1" >&2; exit "${2:-2}"; }

canonical() { readlink -m "$1" 2>/dev/null || printf '%s' "$1"; }

# ---------------- 参数解析 ----------------
CMD=""
MKREPO_NAME=""
ROOT=""
PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        up|down|status|mkrepo|rebuild)
            [ -z "$CMD" ] || die "一个调用只接受一个子命令（先给 $CMD 又给 $1）"
            CMD="$1"; shift ;;
        --root)
            [ $# -ge 2 ] || die "--root 需要一个参数"
            ROOT="$2"; shift 2 ;;
        --purge)
            PURGE=1; shift ;;
        -*)
            die "未知参数: $1（$USAGE）" ;;
        *)
            [ "$CMD" = "mkrepo" ] && [ -z "$MKREPO_NAME" ] || die "多余参数: $1（$USAGE）"
            MKREPO_NAME="$1"; shift ;;
    esac
done
[ -n "$CMD" ] || die "$USAGE"
if [ "$CMD" = "mkrepo" ]; then
    [ -n "$MKREPO_NAME" ] || die "mkrepo 需要 <name>（$USAGE）"
fi

TEST_ROOT="${ROOT:-${FGT_ROOT:-/tmp/fleet-graph-testenv}}"
TEST_ROOT="$(canonical "$TEST_ROOT")"

# ---------------- 拒绝判定（up 的布局检查核心；零副作用） ----------------

# 条 3：TEST_ROOT 等于或位于任一生产根之下（symlink 解析后同判）。
check_root_deny() {
    local cr dr
    cr="$(canonical "$TEST_ROOT")"
    local IFS=':'
    for dr in $PROD_DENY_PATHS; do
        [ -n "$dr" ] || continue
        dr="$(canonical "$dr")"
        if [ "$cr" = "$dr" ] || [ "${cr#"$dr"/}" != "$cr" ]; then
            die "拒绝：TEST_ROOT $cr 位于生产根 $dr 之下（测试环境不是生产，§四·一 A-4）" 2
        fi
    done
}

# 条 1+2：目标端口不得是生产端口，也不得已被监听（bind 探测）。
port_in_deny_list() {
    local port="$1" p
    for p in $PROD_DENY_PORTS; do
        [ "$p" = "$port" ] && return 0
    done
    return 1
}

port_is_free() {
    # SO_REUSEADDR：与真实服务同判——TIME_WAIT 残留不算「在监听」，
    # 只有活跃 listener（EADDRINUSE 且非 TIME_WAIT 场景）才算占用。
    python3 - "$1" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PYEOF
}

check_ports() {
    local face port
    for entry in $FACE_PORTS; do
        face="${entry%%:*}"; port="${entry##*:}"
        case "$port" in ''|*[!0-9]*) die "拒绝：面 $face 的端口值非法: $port" 2 ;; esac
        if port_in_deny_list "$port"; then
            die "拒绝：面 $face 的目标端口 $port 属生产端口集（命中拒绝清单条 1）" 2
        fi
        if ! port_is_free "$port"; then
            die "拒绝：面 $face 的目标端口 $port 在 up 当刻已被监听（命中拒绝清单条 2）" 2
        fi
    done
}

refuse_checks() {
    check_root_deny
    check_ports
}

# ---------------- 共用小件 ----------------
pid_alive() {
    # 存活 = kill -0 可达且非僵尸（Z：进程已死、 fd 全放，仅剩未被收尸的空壳；
    # 下核验全灭与 prod_write_fds 时僵尸一律按死计）。
    [ -n "$1" ] || return 1
    kill -0 "$1" 2>/dev/null || return 1
    local st
    st="$(sed -n 's/^State:[[:space:]]*\([A-Za-z]\).*/\1/p' "/proc/$1/status" 2>/dev/null)"
    [ "$st" != "Z" ]
}

pid_files() { ls "$TEST_ROOT"/pids/*.pid 2>/dev/null || true; }

alive_of_pids() {
    local f n=0 total=0
    for f in $(pid_files); do
        total=$(( total + 1 ))
        pid_alive "$(cat "$f" 2>/dev/null)" && n=$(( n + 1 ))
    done
    printf '%s %s\n' "$n" "$total"
}

te_launch() {
    local face="$1" log="$2"; shift 2
    : > "$log"
    setsid bash -c 'printf "%s\n" "$$" > "$1"; shift; exec "$@"' \
        _ "$TEST_ROOT/pids/$face.pid" "$@" >>"$log" 2>&1 &
}

te_spawn_faces() {
    # dd 面的 plugin-binding：TEST_ROOT 内自生成（§四边界——对 /data/fleet-graph
    # 的只读面只有基线快照与 prod_references grep 两处，binding 不再成为第三处）。
    # 最小绑定只承载 dd 启动检查的 fail-closed 语义（binding 仅在真派单 launch
    # 时被读）；测试环境内真派单由派单流自带绑定，不经本脚本。
    local bind="$TEST_ROOT/config/plugin-binding.json"
    [ -f "$bind" ] || printf '%s\n' '{"plugin_producer": {}}' > "$bind"

    local fg
    if [ -x "$REPO_ROOT/.venv/bin/fleet-graph" ]; then
        fg="$REPO_ROOT/.venv/bin/fleet-graph"
    else
        # 仓的 uv 环境（只读使用仓本身；uv 不改 lock：--frozen）
        fg="__uv__"
    fi

    local abserver=(env
        AGENT_BUS_CONFIG="$TEST_ROOT/config/agent-bus.yaml"
        BUS_ADMIN_TOKEN="$(cat "$TEST_ROOT/secrets/fleet-graph.token" 2>/dev/null)"
        BUS_GATEWAY_TOKEN="$(cat "$TEST_ROOT/secrets/gateway.token" 2>/dev/null)")
    if [ -x "$AGENT_BUS_ROOT/.venv/bin/agent-bus-server" ]; then
        abserver+=("$AGENT_BUS_ROOT/.venv/bin/agent-bus-server")
    else
        abserver+=(uv run --project "$AGENT_BUS_ROOT" agent-bus-server)
    fi
    local abmcp=(env
        AGENT_BUS_CONFIG="$TEST_ROOT/config/agent-bus.yaml"
        BUS_GATEWAY_TOKEN="$(cat "$TEST_ROOT/secrets/gateway.token" 2>/dev/null)")
    if [ -x "$AGENT_BUS_ROOT/.venv/bin/agent-bus-mcp" ]; then
        abmcp+=("$AGENT_BUS_ROOT/.venv/bin/agent-bus-mcp")
    else
        abmcp+=(uv run --project "$AGENT_BUS_ROOT" agent-bus-mcp)
    fi

    if [ "$fg" = "__uv__" ]; then
        te_launch engine "$TEST_ROOT/logs/engine.log" \
            env FLEET_GRAPH_GATEWAY_BASE_URL="http://127.0.0.1:$P_BUS_HTTP" \
            uv run --frozen --project "$REPO_ROOT" fleet-graph scheduler run \
            --config "$TEST_ROOT/config/ronin-lines.json"
        te_launch dd-mcp "$TEST_ROOT/logs/dd-mcp.log" \
            uv run --frozen --project "$REPO_ROOT" fleet-graph dd serve \
            --host 127.0.0.1 --port "$P_DD_MCP" \
            --root "$TEST_ROOT/dd" --plugin-binding "$bind" \
            --working-directory "$REPO_ROOT" \
            --executable "$REPO_ROOT/.venv/bin/fleet-graph"
        te_launch goal-mcp "$TEST_ROOT/logs/goal-mcp.log" \
            uv run --frozen --project "$REPO_ROOT" fleet-graph goal serve \
            --host 127.0.0.1 --port "$P_GOAL_MCP" \
            --work-folder-root "$TEST_ROOT/workfolders" \
            --goal-queue-home "$TEST_ROOT/scheduler"
        te_launch decision-mcp "$TEST_ROOT/logs/decision-mcp.log" \
            uv run --frozen --project "$REPO_ROOT" fleet-graph decision serve \
            --host 127.0.0.1 --port "$P_DECISION_MCP" \
            --run-root "$TEST_ROOT/runs" \
            --lines-config "$TEST_ROOT/config/ronin-lines.json" \
            --state-dir "$TEST_ROOT/decision-mcp"
        te_launch state-http "$TEST_ROOT/logs/state-http.log" \
            env FLEET_GRAPH_BUS_TOKEN_FILE="$TEST_ROOT/secrets/fleet-graph.token" \
            uv run --frozen --project "$REPO_ROOT" fleet-graph state serve \
            --host 127.0.0.1 --port "$P_STATE_HTTP" \
            --run-root "$TEST_ROOT/runs" --dd-root "$TEST_ROOT/dd" \
            --lines-config "$TEST_ROOT/config/ronin-lines.json" \
            --bridge-state-dir "$TEST_ROOT/bridge" \
            --bus-url "http://127.0.0.1:$P_BUS_HTTP" \
            --enroll-queue "$TEST_ROOT/scheduler/enroll-queue.jsonl" \
            --llm-ledger-file "$TEST_ROOT/stubs/ledger.json"
    else
        te_launch engine "$TEST_ROOT/logs/engine.log" \
            env FLEET_GRAPH_GATEWAY_BASE_URL="http://127.0.0.1:$P_BUS_HTTP" \
            "$fg" scheduler run --config "$TEST_ROOT/config/ronin-lines.json"
        te_launch dd-mcp "$TEST_ROOT/logs/dd-mcp.log" \
            "$fg" dd serve \
            --host 127.0.0.1 --port "$P_DD_MCP" \
            --root "$TEST_ROOT/dd" --plugin-binding "$bind" \
            --working-directory "$REPO_ROOT" \
            --executable "$fg"
        te_launch goal-mcp "$TEST_ROOT/logs/goal-mcp.log" \
            "$fg" goal serve \
            --host 127.0.0.1 --port "$P_GOAL_MCP" \
            --work-folder-root "$TEST_ROOT/workfolders" \
            --goal-queue-home "$TEST_ROOT/scheduler"
        te_launch decision-mcp "$TEST_ROOT/logs/decision-mcp.log" \
            "$fg" decision serve \
            --host 127.0.0.1 --port "$P_DECISION_MCP" \
            --run-root "$TEST_ROOT/runs" \
            --lines-config "$TEST_ROOT/config/ronin-lines.json" \
            --state-dir "$TEST_ROOT/decision-mcp"
        te_launch state-http "$TEST_ROOT/logs/state-http.log" \
            env FLEET_GRAPH_BUS_TOKEN_FILE="$TEST_ROOT/secrets/fleet-graph.token" \
            "$fg" state serve \
            --host 127.0.0.1 --port "$P_STATE_HTTP" \
            --run-root "$TEST_ROOT/runs" --dd-root "$TEST_ROOT/dd" \
            --lines-config "$TEST_ROOT/config/ronin-lines.json" \
            --bridge-state-dir "$TEST_ROOT/bridge" \
            --bus-url "http://127.0.0.1:$P_BUS_HTTP" \
            --enroll-queue "$TEST_ROOT/scheduler/enroll-queue.jsonl" \
            --llm-ledger-file "$TEST_ROOT/stubs/ledger.json"
    fi

    # 注意：不设 FLEET_GRAPH_BUS_TOKEN/FILE 于引擎与 dd/goal/decision 面——
    # BusClient 无凭证即构造失败降级（板升级为可选），绝不指向生产 :7490。
    te_launch bus-server "$TEST_ROOT/logs/bus-server.log" \
        "${abserver[@]}"
    te_launch bus-mcp "$TEST_ROOT/logs/bus-mcp.log" \
        "${abmcp[@]}"
}

# ---------------- 就绪等待（每面 TCP + tools/list 或等价 GET；总计 ≤60s） ----------------
http_code() { curl -s --noproxy '*' -m 3 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }

mcp_probe() {
    # 一次 initialize + tools/list；有应答（任意工具行）即可。
    local port="$1" sid body
    sid="$(curl -s --noproxy '*' -m 3 -D - -o /dev/null "http://127.0.0.1:$port/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"testenv","version":"1"}}}' 2>/dev/null \
        | tr -d '\r' | sed -n 's/^[Mm][Cc][Pp]-[Ss][Ee][Ss][Ss][Ii][Oo][Nn]-[Ii][Dd]:[[:space:]]*//Ip')"
    [ -z "$sid" ] && return 1
    body="$(curl -s --noproxy '*' -m 3 "http://127.0.0.1:$port/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -H "Mcp-Session-Id: $sid" \
        -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' 2>/dev/null \
        | sed -n 's/^data: //p' | tr -d '\r')"
    printf '%s' "$body" | jq -e '.result.tools | length >= 0' >/dev/null 2>&1
}

face_ready() {
    case "$1" in
        bus-http)      [ "$(http_code "http://127.0.0.1:$P_BUS_HTTP/readyz")" = "200" ] ;;
        bus-mcp)       mcp_probe "$P_BUS_MCP" ;;
        dd-mcp)        mcp_probe "$P_DD_MCP" ;;
        goal-mcp)      mcp_probe "$P_GOAL_MCP" ;;
        decision-mcp)  mcp_probe "$P_DECISION_MCP" ;;
        state-http)    [ "$(http_code "http://127.0.0.1:$P_STATE_HTTP/v1/lines")" = "200" ] ;;
        engine)        pid_alive "$(cat "$TEST_ROOT/pids/engine.pid" 2>/dev/null)" ;;
        *)             return 1 ;;
    esac
}

KILL_ALL_FACES="bus-mcp bus-server state-http decision-mcp goal-mcp dd-mcp engine"

kill_all() {
    # SIGTERM → 宽限 5s → SIGKILL → 等到全灭（击杀回收必须确定完成）。
    local f p i remaining=0
    for f in $KILL_ALL_FACES; do
        p="$(cat "$TEST_ROOT/pids/$f.pid" 2>/dev/null)"
        [ -n "$p" ] && kill "$p" 2>/dev/null
    done
    for i in 1 2 3 4 5; do
        remaining=0
        for f in $KILL_ALL_FACES; do
            p="$(cat "$TEST_ROOT/pids/$f.pid" 2>/dev/null)"
            pid_alive "$p" && remaining=$(( remaining + 1 ))
        done
        [ "$remaining" -eq 0 ] && return 0
        sleep 1
    done
    for f in $KILL_ALL_FACES; do
        p="$(cat "$TEST_ROOT/pids/$f.pid" 2>/dev/null)"
        [ -n "$p" ] && kill -9 "$p" 2>/dev/null
    done
    sleep 1
}

reclaim_after_kill() {
    # up 失败路径完全回收（R1-fix）：击杀后核验全灭（与 down 同一核验口径：
    # pid_alive 假 = kill -0 不可达或仅剩 Z）。
    #   全灭 → 删除全部面 pid 文件：exit 4 终态＝无存活面进程 且 TEST_ROOT/pids
    #          为空，失败后的下一次 up 不再触发「部分存活 0/N → exit 3」；
    #   有未灭（理论 D 态）→ 如实点名未灭面、保留 pid 文件取证——宁留证据，
    #          不假报回收。返回 0=已完全回收；1=有未灭面。
    local f p pf unverified=""
    for f in $KILL_ALL_FACES; do
        p="$(cat "$TEST_ROOT/pids/$f.pid" 2>/dev/null)"
        pid_alive "$p" && unverified="$unverified $f"
    done
    if [ -n "$unverified" ]; then
        printf '%s\n' "testenv: 击杀后核验未全灭，未灭面:${unverified}（pid 文件保留取证）" >&2
        return 1
    fi
    pf="$(pid_files)"
    [ -n "$pf" ] && rm -f $pf
    return 0
}

await_ready() {
    # 就绪上限 §一·4 默认 60s；FGT_READY_TIMEOUT 仅供测试缩短（同 FGT_DENY_* 的
    # 测试后门定位，生产不设）。
    local deadline=$(( $(date +%s) + ${FGT_READY_TIMEOUT:-60} )) pending faces f
    faces="bus-http bus-mcp dd-mcp goal-mcp decision-mcp state-http engine"
    while :; do
        pending=""
        for f in $faces; do
            face_ready "$f" || pending="$pending $f"
        done
        [ -z "$pending" ] && return 0
        [ "$(date +%s)" -ge "$deadline" ] && {
            kill_all
            sleep 1
            if reclaim_after_kill; then
                die "就绪等待超时（≤60s）：未就绪面:${pending}；已全部击杀并回收 pid 文件" 4
            fi
            die "就绪等待超时（≤60s）：未就绪面:${pending}；击杀后核验未全灭，pid 文件保留取证" 4
        }
        sleep 1
    done
}

# ---------------- 布局与配置生成 ----------------
write_layout() {
    local d
    for d in env pids logs bin stubs runs dd scheduler workfolders repos secrets \
             config personas supervisor prod-baseline agent-bus decision-mcp bridge \
             current; do
        mkdir -p "$TEST_ROOT/$d"
    done
}

snapshot_current() {
    # VRB_CURRENT 的 TEST_ROOT 内快照（§二：「VRB_CURRENT 指向仓自身或 TEST_ROOT
    # 内快照」）。取快照而非指仓自身：仓位于 /data/worktrees 之下，落在拒绝清单
    # 「整个 /data」里——快照让 --env test 侧的 knob 越界校验无需例外、纯机械成立。
    local item
    mkdir -p "$TEST_ROOT/current"
    for item in scripts src config deploy; do
        [ -e "$REPO_ROOT/$item" ] && cp -a "$REPO_ROOT/$item" "$TEST_ROOT/current/" 2>/dev/null
    done
}

write_roster() {
    cat > "$TEST_ROOT/config/ronin-lines.json" <<EOF
{
  "_comment": "testenv 独立名册：只落 TEST_ROOT，与生产名册零共享（§一·2）。",
  "run_root": "$TEST_ROOT/runs",
  "dd_root": "$TEST_ROOT/dd",
  "probe_via_runtime": false,
  "supervisor_events": false,
  "lines": [
    {
      "folder_id": "wf-testenv-sample",
      "seat": "testenv-sample",
      "alias": "testenv-sample",
      "max_rounds": 1,
      "enabled": false,
      "_provenance": "testenv 安全样例线：enabled=false 等价不参与调度（§一·2）"
    }
  ]
}
EOF
}

write_bus_config() {
    cat > "$TEST_ROOT/config/agent-bus.yaml" <<EOF
runtime_root: $TEST_ROOT/agent-bus
schema_version: 1
server:
  host: 127.0.0.1
  port: $P_BUS_HTTP
mcp:
  host: 127.0.0.1
  port: $P_BUS_MCP
EOF
}

gen_token() {
    # ≥32 字符 url-safe 随机串（/dev/urandom，无外部依赖）。
    head -c 48 /dev/urandom | base64 | tr '/+' '_-' | tr -d '=\n'
}

write_secrets_and_files() {
    local admin gateway
    admin="$(gen_token)"
    gateway="$(gen_token)"
    while [ "$gateway" = "$admin" ]; do gateway="$(gen_token)"; done
    printf '%s\n' "$admin" > "$TEST_ROOT/secrets/fleet-graph.token"
    chmod 600 "$TEST_ROOT/secrets/fleet-graph.token"
    printf '%s\n' "$gateway" > "$TEST_ROOT/secrets/gateway.token"
    chmod 600 "$TEST_ROOT/secrets/gateway.token"

    # 05 号检查的 VRB_LLM_LEDGER stub：静态空 request_events 投影文件，由
    # state-http 面的 /v1/llm-ledger 查询面按 --llm-ledger-file 服务（R2）。
    # 05 号探针是 curl 探针、要求 http 200——file:// 形参 curl 只报 000，
    # 故账本面必须真经 HTTP（查询面合成，与生产 knob 指向灵智账本消费面同构）。
    printf '%s\n' '{"request_events": [], "events": [], "total": 0}' \
        > "$TEST_ROOT/stubs/ledger.json"

    # 监督面 skill 与线 persona：指向 TEST_ROOT 内的干净文件（08/21 的 grep 面）。
    cat > "$TEST_ROOT/config/supervisor-SKILL.md" <<'EOF'
# fleet-supervisor SKILL（testenv 快照）

一切操作只走 MCP 面；读模型走 state 读模型。本文件是 testenv 的 TEST_ROOT 内快照。
EOF
    cat > "$TEST_ROOT/personas/wf-testenv-sample.md" <<'EOF'
# persona: wf-testenv-sample（testenv 样例线）

一切操作只走 MCP 面。enabled=false，不参与调度。
EOF
}

write_systemctl_stub() {
    # 能应答 `--user list-units 'agent-bus-*' --plain --no-legend`（按 TEST_ROOT pids
    # 应答 testenv 的面）与 `--user show <unit> -p MainPID --value`。
    cat > "$TEST_ROOT/bin/systemctl-stub" <<EOF
#!/usr/bin/env bash
# testenv systemctl stub（R1 §二）：只应答探针语法；绝不管理任何真实 unit。
set -u
PIDS_DIR="$TEST_ROOT/pids"
face_pid() {
    case "\$1" in
        agent-bus-server*) [ -r "\$PIDS_DIR/bus-server.pid" ] && cat "\$PIDS_DIR/bus-server.pid" ;;
        agent-bus-mcp*)    [ -r "\$PIDS_DIR/bus-mcp.pid" ] && cat "\$PIDS_DIR/bus-mcp.pid" ;;
        fleet-graph-dd-mcp*) [ -r "\$PIDS_DIR/dd-mcp.pid" ] && cat "\$PIDS_DIR/dd-mcp.pid" ;;
        *) printf '0' ;;
    esac
    return 0
}
case "\$*" in
    *" list-unit-files "*)
        # testenv 世界没有 unit 文件（systemd 不在环内）：如实应答空表。
        exit 0 ;;
    *" list-units "*)
        # 依 TEST_ROOT pids 应答 agent-bus-* 面；其余模式（fleet-graph-*）无 unit。
        for entry in "agent-bus-server.service bus-server" "agent-bus-mcp.service bus-mcp"; do
            unit="\${entry%% *}"; face="\${entry##* }"
            pid="\$(cat "\$PIDS_DIR/\$face.pid" 2>/dev/null || true)"
            if [ -n "\$pid" ] && kill -0 "\$pid" 2>/dev/null; then
                printf '%s loaded active running testenv\n' "\$unit"
            fi
        done
        exit 0 ;;
    *" show "*)
        unit=""; prop=""; prev=""
        for a in "\$@"; do
            case "\$prev" in show) unit="\$a" ;; -p) prop="\$a" ;; esac
            prev="\$a"
        done
        if [ "\$prop" = "MainPID" ]; then
            face=""
            case "\$unit" in
                agent-bus-server*) face="bus-server" ;;
                agent-bus-mcp*) face="bus-mcp" ;;
                fleet-graph-dd-mcp*) face="dd-mcp" ;;
            esac
            if [ -n "\$face" ]; then
                pid="\$(cat "\$PIDS_DIR/\$face.pid" 2>/dev/null || true)"
                if [ -n "\$pid" ] && kill -0 "\$pid" 2>/dev/null; then
                    printf '%s\n' "\$pid"; exit 0
                fi
            fi
        fi
        printf '0\n'; exit 0 ;;
    *)
        printf 'testenv systemctl stub: unsupported args: %s\n' "\$*" >&2
        exit 3 ;;
esac
EOF
    chmod +x "$TEST_ROOT/bin/systemctl-stub"
}

write_waiting_dd_sample() {
    # R2 交付物（spec 交付物 3②）：waiting_dd 判据样本。M1 状态词表里
    # waiting_dd = 线停在它派出的 development 上（waiting_on=dd 的机械投影，
    # run_artifacts.LINE_STATE_WAITING_DD）。样本落 TEST_ROOT/runs/.scheduler 下
    # 的 wf-*.json，让 verify-rebuild 05 号「等待零消耗」判据有样本可核：
    # 该线 alias 在账本 request_events 的计数必须为 0（stub 静态空账本）。
    # 这是测试判据样本，不是引擎状态——enabled=false 的样例线不参与调度。
    local dir="$TEST_ROOT/runs/.scheduler"
    mkdir -p "$dir"
    local stamp
    stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' "{\"folder_id\": \"wf-testenv-sample\", \"line_state\": \"waiting_dd\", \"status\": \"waiting_dd\", \"parked_dd_development_id\": \"dev-testenv-sample\", \"parked_at\": \"$stamp\"}" \
        > "$dir/wf-testenv-sample.json"
}

write_knobs() {
    # 纯 VRB_*=值 赋值（无命令替换、无 source 链）；verify-rebuild.sh --env test
    # 的唯一输入。至少覆盖 R0 spec 第 6 条全部 knob。VRB_CURRENT 指 TEST_ROOT 内
    # 快照（snapshot_current，见其注）。
    cat > "$TEST_ROOT/env/knobs.sh" <<EOF
VRB_SYSTEMCTL=$TEST_ROOT/bin/systemctl-stub
VRB_CURRENT=$TEST_ROOT/current
VRB_BUS_BASE=http://127.0.0.1:$P_BUS_HTTP
VRB_BUS_TOKEN_FILE=$TEST_ROOT/secrets/fleet-graph.token
VRB_STATE_BASE=http://127.0.0.1:$P_STATE_HTTP
VRB_MCP_BUS=$P_BUS_MCP
VRB_MCP_DD=$P_DD_MCP
VRB_MCP_GOAL=$P_GOAL_MCP
VRB_MCP_DECISION=$P_DECISION_MCP
VRB_RUNS_ROOT=$TEST_ROOT/runs
VRB_SCHED_DIR=$TEST_ROOT/runs/.scheduler
VRB_DD_ROOT=$TEST_ROOT/dd
VRB_ROSTER=$TEST_ROOT/config/ronin-lines.json
VRB_SKILL_FILE=$TEST_ROOT/config/supervisor-SKILL.md
VRB_PERSONA_FILES=$TEST_ROOT/personas/wf-testenv-sample.md
VRB_SUPERVISOR_ROOT=$TEST_ROOT/supervisor
VRB_SECRETS_DIR=$TEST_ROOT/secrets
VRB_LLM_LEDGER=http://127.0.0.1:$P_STATE_HTTP/v1/llm-ledger
EOF
}

prod_baseline_snapshot() {
    # 基线留痕（信息性，不作硬断言）：GET 生产 :7494/v1/lines（jq 排序快照）与
    # 生产 runs 树清单（路径+mtime）。对生产只读；失败留注记不阻塞。runs 树根
    # = FGT_PROD_GREP_ROOT（默认 /data/fleet-graph，与 prod_references 同根——
    # 测试 fixture 指 tmp 时基线侧同样离线自足）。
    local dir="$TEST_ROOT/prod-baseline" proot="${FGT_PROD_GREP_ROOT:-/data/fleet-graph}"
    mkdir -p "$dir"
    if [ "$(http_code "http://127.0.0.1:7494/v1/lines")" = "200" ]; then
        curl -s --noproxy '*' -m 5 "http://127.0.0.1:7494/v1/lines" 2>/dev/null \
            | jq -S . > "$dir/lines.json" 2>/dev/null || printf 'snapshot-failed\n' > "$dir/lines.json"
    else
        printf 'state-read-model-unreachable\n' > "$dir/lines.json"
    fi
    timeout 10 find "$proot/runs" -maxdepth 2 \( -type f -o -type d \) \
        -printf '%p %T@\n' 2>/dev/null | sort > "$dir/runs-tree.txt" \
        || printf 'runs-tree-listing-failed\n' > "$dir/runs-tree.txt"
}

# ---------------- up ----------------
cmd_up() {
    # 幂等判定在拒绝清单之前：全部 pid 存活时重复 up → 只打印摘要 exit 0
    # （此刻各目标端口正被自己的面监听，bind 探测必命中「已监听」，
    #  幂等语义必须优先，§一·4）；部分存活 → exit 3 报「先 down」。
    local pids n_total
    pids="$(pid_files)"
    if [ -n "$pids" ]; then
        read -r n_alive n_total <<EOF
$(alive_of_pids)
EOF
        if [ "$n_alive" -eq "$n_total" ]; then
            printf 'up=1 surfaces=%s/%s root=%s\n' "$n_alive" "$n_total" "$TEST_ROOT"
            return 0
        fi
        printf 'testenv: 部分存活（%s/%s）：先 down 再 up，不静默自愈\n' "$n_alive" "$n_total" >&2
        exit 3
    fi

    refuse_checks
    write_layout

    write_roster
    write_bus_config
    write_secrets_and_files
    write_systemctl_stub
    write_waiting_dd_sample
    snapshot_current

    te_spawn_faces
    await_ready

    write_knobs
    prod_baseline_snapshot

    # R3 验收样本（specs/r3-stop-response-dispatch.md 开放点 1 的实现方作答）：
    # 引擎级 fixture 在 testenv 内驱动真实图路径产出 11/17 所需的派单+gate
    # 样本（零外部网关、零真实模型调用、幂等、fail-closed）。样本失败的 up
    # 即失败——缺样本的环境不得变绿。
    r3_sample_driver "$REPO_ROOT" "$TEST_ROOT" "$P_BUS_HTTP"

    read -r n_alive n_total <<EOF
$(alive_of_pids)
EOF
    printf 'up=1 surfaces=%s/%s root=%s\n' "$n_alive" "$n_total" "$TEST_ROOT"
}

r3_sample_driver() {
    local repo_root="$1" test_root="$2" bus_port="$3"
    local driver="$repo_root/scripts/testenv_r3_sample.py"
    if [ ! -f "$driver" ]; then
        printf 'testenv: R3 样本驱动缺失: %s\n' "$driver" >&2
        exit 5
    fi
    local py=""
    if [ -x "$repo_root/.venv/bin/python" ]; then
        py="$repo_root/.venv/bin/python"
    else
        py="uv run --frozen --project $repo_root python"
    fi
    if ! $py "$driver" --root "$test_root" --bus-port "$bus_port"; then
        printf 'testenv: R3 样本驱动失败（判据样本不可缺，fail-closed）\n' >&2
        kill_all
        reclaim_after_kill || true
        exit 5
    fi
}

# ---------------- status ----------------
cmd_status() {
    local n_alive=0 n_total=0 f p count=0
    read -r n_alive n_total <<EOF
$(alive_of_pids)
EOF
    if [ "$n_total" -gt 0 ] && [ "$n_alive" -eq "$n_total" ]; then
        printf 'up=1\n'
    else
        printf 'up=0\n'
    fi
    printf 'pids=%s\n' "$n_alive"
    printf 'surfaces=%s/%s\n' "$n_alive" "$n_total"
    count=0
    for f in $(pid_files); do
        p="$(cat "$f" 2>/dev/null)"
        pid_alive "$p" || continue
        count=$(( count + $(count_prod_write_fds "$p") ))
    done
    printf 'prod_write_fds=%s\n' "$count"
    if [ "$count" -gt 0 ]; then
        for f in $(pid_files); do
            p="$(cat "$f" 2>/dev/null)"
            pid_alive "$p" || continue
            list_prod_write_fds "$p"
        done
    fi
    # workfolder 能力的实现依据（§一·2 二选一）：引擎 goal serve 的
    # --work-folder-root 数据根直指 TEST_ROOT/workfolders，不另起 katana 进程。
    printf 'workfolder=engine-data-root\n'
}

count_prod_write_fds() {
    list_prod_write_fds "$1" | wc -l | tr -d ' '
}

list_prod_write_fds() {
    # 对 /proc/<pid>/fd 逐 fd：fdinfo flags 带写位（O_WRONLY/O_RDWR）且
    # readlink 目标落在生产根（条 3 集合，FGT_DENY_PATHS 可按 fixture 替换）。
    python3 - "$1" "$PROD_DENY_PATHS" <<'PYEOF' 2>/dev/null
import os, sys

pid, deny = sys.argv[1], [p for p in sys.argv[2].split(":") if p]
deny_real = []
for root in deny:
    try:
        deny_real.append(os.path.realpath(root))
    except OSError:
        deny_real.append(root)

fd_dir = f"/proc/{pid}/fd"
try:
    fds = os.listdir(fd_dir)
except OSError:
    sys.exit(0)
for fd in fds:
    try:
        with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as fh:
            flags = None
            for line in fh:
                if line.startswith("flags:"):
                    flags = int(line.split()[1], 8)
                    break
        if flags is None or (flags & 3) == 0:  # O_ACCMODE: 1=O_WRONLY 2=O_RDWR
            continue
        target = os.readlink(f"{fd_dir}/{fd}")
        target_real = target
        if target.startswith("/"):
            target_real = os.path.realpath(target)
        if any(target_real == r or target_real.startswith(r + "/") for r in deny_real):
            print(f"pid={pid} fd={fd} path={target}")
    except (OSError, ValueError):
        continue
PYEOF
}

# ---------------- down ----------------
prod_references() {
    # 生产引擎写入面 grep 字面 TEST_ROOT（只读、限时 15s）。默认根
    # /data/fleet-graph；FGT_PROD_GREP_ROOT 仅供测试替换（测试后门，生产不设）。
    local n=0 out proot="${FGT_PROD_GREP_ROOT:-/data/fleet-graph}"
    out="$(timeout 15 bash -c "
        grep -rF --include='*.json' -l '$TEST_ROOT' '$proot/runs' 2>/dev/null | head -100
        find '$proot/runs' -maxdepth 2 -name '*.jsonl' -type f 2>/dev/null | head -400 | xargs -r grep -lF '$TEST_ROOT' 2>/dev/null | head -100
        find '$proot/dd' -maxdepth 2 \\( -name 'record.json' -o -name 'events.jsonl' \\) -type f 2>/dev/null | head -400 | xargs -r grep -lF '$TEST_ROOT' 2>/dev/null | head -100
    ")" || out=""
    if [ -n "$out" ]; then
        n="$(printf '%s\n' "$out" | grep -c .)"
    fi
    printf '%s' "$n"
}

cmd_down() {
    local pids f p i killed remaining
    pids="$(pid_files)"
    if [ -n "$pids" ]; then
        for f in $pids; do
            p="$(cat "$f" 2>/dev/null)"
            [ -n "$p" ] && kill "$p" 2>/dev/null
        done
        # 宽限 10s
        for i in 1 2 3 4 5 6 7 8 9 10; do
            remaining=0
            for f in $pids; do
                p="$(cat "$f" 2>/dev/null)"
                pid_alive "$p" && remaining=$(( remaining + 1 ))
            done
            [ "$remaining" -eq 0 ] && break
            sleep 1
        done
        for f in $pids; do
            p="$(cat "$f" 2>/dev/null)"
            if pid_alive "$p"; then
                kill -9 "$p" 2>/dev/null
            fi
        done
        sleep 1
        # 核验全灭
        for f in $pids; do
            p="$(cat "$f" 2>/dev/null)"
            if pid_alive "$p"; then
                die "down 失败：pid $p（$f）击杀后仍存活" 1
            fi
        done
        rm -f $pids
    fi
    local refs
    refs="$(prod_references)"
    printf 'down=1 prod_references=%s\n' "$refs"
    # 基线 diff（信息性——生产自身在跑会自然演进，不作硬断言）。两个基线工件
    # （lines.json 与 runs-tree.txt）down 时都重取并 diff 进输出（§一·4 基线留痕）。
    local base="$TEST_ROOT/prod-baseline" proot="${FGT_PROD_GREP_ROOT:-/data/fleet-graph}" now
    if [ -f "$base/lines.json" ] && [ "$(http_code "http://127.0.0.1:7494/v1/lines")" = "200" ]; then
        now="$(curl -s --noproxy '*' -m 5 "http://127.0.0.1:7494/v1/lines" 2>/dev/null | jq -S . 2>/dev/null)"
        [ -n "$now" ] && diff "$base/lines.json" <(printf '%s\n' "$now") \
            | sed 's/^/baseline-diff: /' || true
    fi
    if [ -f "$base/runs-tree.txt" ]; then
        if timeout 10 find "$proot/runs" -maxdepth 2 \( -type f -o -type d \) \
            -printf '%p %T@\n' 2>/dev/null | sort > "$base/runs-tree-now.txt"; then
            diff "$base/runs-tree.txt" "$base/runs-tree-now.txt" \
                | sed 's/^/runs-tree-diff: /' || true
        else
            printf 'runs-tree-diff: runs 树重取失败（信息性，不阻塞）\n'
        fi
        rm -f "$base/runs-tree-now.txt"
    fi
    if [ "$PURGE" = "1" ]; then
        rm -rf "$TEST_ROOT"
    fi
    return 0
}

# ---------------- mkrepo ----------------
cmd_mkrepo() {
    refuse_checks
    write_layout
    local bare="$TEST_ROOT/repos/$MKREPO_NAME.git" clone="$TEST_ROOT/repos/$MKREPO_NAME"
    if [ ! -d "$bare" ]; then
        git init --bare -q "$bare" || die "mkrepo: bare 仓创建失败: $bare" 1
    fi
    if [ ! -d "$clone" ]; then
        git clone -q "$bare" "$clone" 2>/dev/null || die "mkrepo: 工作克隆创建失败: $clone" 1
    fi
    printf '%s\n%s\n' "$bare" "$clone"
}

# ---------------- rebuild（R2 图合一 A 方案探针） ----------------
cmd_rebuild() {
    # 判据锚：R2 spec §行为契约 3（checkpoint A 方案）。checkpoint 库是可删缓存：
    # 删除后图状态仍可从持久权威件（work folder + dd record.json/result.json）
    # 完全重建——本探针机械核对三件事：
    #   1) 删 TEST_ROOT/runs/*/checkpoint.sqlite3（缓存库，可删）；
    #   2) 逐 dd/<dev>/record.json 用 jq 从 record.json + 当代 result.json 重建
    #      状态投影（state/terminal/head_commit/generation）——重建输入只有
    #      两权威件，绝不读 checkpoint 或 .scheduler；
    #   3) 同 (repo_path, spec_digest) 只允许一张单（已派单事实 + 幂等键判重，
    #      重建绝不产生重复派单目录）。
    local deleted=0 rebuilt=0 dups=0 db
    for db in "$TEST_ROOT"/runs/*/checkpoint.sqlite3; do
        [ -e "$db" ] || continue
        rm -f "$db" && deleted=$(( deleted + 1 ))
    done
    local dev rec gen result_path state terminal head_commit key
    local -A seen_keys=()
    if [ -d "$TEST_ROOT/dd" ]; then
        for dev in "$TEST_ROOT"/dd/*/; do
            [ -d "$dev" ] || continue
            rec="$dev/record.json"
            [ -f "$rec" ] || continue
            gen="$(jq -r '.generation // 1' "$rec" 2>/dev/null)"
            case "$gen" in ''|*[!0-9]*) gen=1 ;; esac
            if [ "$gen" -le 1 ]; then
                result_path="$dev/result.json"
            else
                result_path="$dev/g$gen/result.json"
            fi
            terminal="$(jq -r '.terminal // ""' "$result_path" 2>/dev/null)"
            head_commit="$(jq -r '.head_commit // ""' "$result_path" 2>/dev/null)"
            awaiting="$(jq -r '(.awaiting // empty) != empty' "$result_path" 2>/dev/null)"
            if [ "$awaiting" = "true" ]; then
                state="awaiting_gate"
            elif [ -n "$terminal" ] && [ "$terminal" != "null" ]; then
                state="$terminal"
            else
                state="created"
            fi
            key="$(jq -r '[.repo_path // "", .spec_digest // ""] | @tsv' "$rec" 2>/dev/null)"
            if [ -n "${seen_keys[$key]:-}" ]; then
                dups=$(( dups + 1 ))
            fi
            seen_keys[$key]=1
            printf 'rebuilt dev=%s state=%s head=%s gen=%s\n' \
                "$(basename "$dev")" "$state" "$head_commit" "$gen"
            rebuilt=$(( rebuilt + 1 ))
        done
    fi
    if [ "$dups" -gt 0 ]; then
        die "rebuild 校验失败：同 (repo_path, spec_digest) 存在 $dups 个重复派单目录" 1
    fi
    printf 'rebuild ok deleted=%d rebuilt=%d dups=%d 重建=ok\n' "$deleted" "$rebuilt" "$dups"
}

case "$CMD" in
    up)      cmd_up ;;
    status)  cmd_status ;;
    down)    cmd_down ;;
    mkrepo)  cmd_mkrepo ;;
    rebuild) cmd_rebuild ;;
esac

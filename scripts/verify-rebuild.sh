#!/usr/bin/env bash
#
# verify-rebuild.sh — wf-4601c8（ronin-rebuild）R0 验收判据脚本：验收标准 v2 二十一项的诚实报红。
#
# 骨架复制自 verify-lim.sh（M0A），检查项按 wf-4601c8 design.md §4 重写。
# 判据锚：wf-4601c8 goal.md §二 R0 与 §四纪律；design.md §4「验收标准 v2」二十一项（编号稳定）；
#         21 的对象清单源自 wf-8d9737 design.md §7.1/§7.2。
#
# 职责：逐条机械实现验收标准 v2 的 21 项检查，每项独立探测已部署的生产事实，
#        输出「NN <id> PASS|FAIL — <依据>」恰好一行，整体退出码 = FAIL 项数（0–21）。
#        起点大面积报红是正常起点：waiting_dd、state_takeover、testenv 等机制尚未落地时
#        本脚本如实报红并写明缺什么，不折算 PASS、不「跳过即过」。
#
# 断言对象是已部署的生产事实，不是本工作树源码（监督面 S5 裁决沿用）：
#   - systemd user unit 与 /proc/<pid>/cmdline
#   - agent-bus :7490（Bearer token 取 /data/agent-bus/tokens/fleet-graph.token）
#   - state :7494、bus MCP :5608、dd MCP :5610、goal MCP :5611、decision MCP :5614
#   - /data/fleet-graph/runs/（coord/rounds.jsonl、.scheduler/）、/data/fleet-graph/dd/
#   - 部署 current /data/apps/fleet-graph/current、名册 config/ronin-lines.json
#   - 监督面 /data/fleet-graph/supervisor、灵智账本（见 VRB_LLM_LEDGER 依据注释）
#
# 代理卫生（S6）：脚本开头 unset 全部代理变量，回环 curl/jq 探测加 --noproxy '*' 不走代理。
#
# 用法：
#   bash scripts/verify-rebuild.sh                 # 跑全部 21 项
#   bash scripts/verify-rebuild.sh --check 03      # 只跑指定项（01–21；其他值报错退出非零）
#   bash scripts/verify-rebuild.sh --window-seconds 3600   # 覆盖时间窗（03/05/07/11/13/14）
#   bash scripts/verify-rebuild.sh --env test --root TEST_ROOT   # R1：source
#                    TEST_ROOT/env/knobs.sh 后按既有 01–21 主循环跑测试环境
#                    （fail-closed：knobs/TEST_ROOT 缺失或 knob 越界 → exit 2，
#                    绝不回退生产默认值；缺 --root 时依 $FGT_ROOT，再缺省
#                    /tmp/fleet-graph-testenv）
#
# 退出码：等于 FAIL 项数（0–21，全绿为 0）。
#        单项探针出错（curl 非零 / jq 解析失败 / 文件缺失 / 超时）→ 该项 FAIL 并带错误原文，
#        脚本本身不崩溃（无全局 set -e）；单项探针超时上限 15s，整脚本目标 < 3 分钟。
#
# 检查函数命名约定（变异红靶的机械注入点，硬性）：每项一个 bash 函数 vrb_check_NN（NN 两位数字），
# 函数体首行不得是 return；判定一律经 vrb_emit NN id VERDICT evidence 发出；主循环按 01–21 顺序调用。
#
# 一切「主动打」的检查（04/06/12/14/15/16）只对现场合成的一次性 vrb-selftest- 靶进行，跑完即清；
# 合成不了的（所需机制尚未落地）按第 8 条 FAIL 带依据，绝不动真线、真单、真频道；只读检查不写任何生产路径。
#
# VRB_* 覆盖键（每个外部依赖都可指fixture；覆盖只改探针指向，不改判据）：
#   VRB_SYSTEMCTL      默认 systemctl（stub 须能应答 --user list-units … --plain --no-legend
#                      与 --user show <unit> -p <PROP> --value）
#   VRB_CURRENT        默认 /data/apps/fleet-graph/current
#   VRB_BUS_BASE       默认 http://127.0.0.1:7490
#   VRB_BUS_TOKEN_FILE 默认 /data/agent-bus/tokens/fleet-graph.token
#   VRB_STATE_BASE     默认 http://127.0.0.1:7494
#   VRB_MCP_BUS / VRB_MCP_DD / VRB_MCP_GOAL / VRB_MCP_DECISION  默认 5608 / 5610 / 5611 / 5614
#   VRB_RUNS_ROOT      默认 /data/fleet-graph/runs
#   VRB_SCHED_DIR      默认 $VRB_RUNS_ROOT/.scheduler
#   VRB_DD_ROOT        默认 /data/fleet-graph/dd
#   VRB_ROSTER         默认 $VRB_CURRENT/config/ronin-lines.json
#   VRB_SKILL_FILE     默认 /data/code/self/agent-skills/plugins/agent-skills/skills/fleet-supervisor/SKILL.md
#   VRB_PERSONA_FILES  冒号分隔的线 persona 文件集；默认从 $VRB_ROSTER 里 enabled 线的
#                      persona/seat 路径字段取，取不到则空集并在依据里注明
#   VRB_SUPERVISOR_ROOT 默认 /data/fleet-graph/supervisor（06 的 contract_changed 留痕检索根）
#   VRB_SECRETS_DIR    默认 /data/fleet-graph/secrets（21 §7.2.8 的 alias token 新路径存在性）
#   VRB_LLM_LEDGER     灵智账本查询面 URL（05 用）。依据：灵智账本消费面 = quota-api 额度聚合服务
#                      （127.0.0.1:8101，unit 描述「…Lingzhi…Token 曲线」，本机实测在听）；
#                      其 request_events 子面路径按 new-api 网关惯例先写 /api/request_events
#                      （R0 实测该子面 404 → 05 如实 FAIL 注明账本查询面未确认，落账面归
#                      wf-386b2f 成本可观测线收口）；账本不可达/响应不可解析一律按第 8 条 FAIL。
set -u
set -o pipefail

unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy no_proxy NO_PROXY 2>/dev/null || true

# ---------------- R1：--env test 模式预扫描（fail-closed；先于任何 VRB_ 默认值） ----------------
# 新增参数 --env test [--root PATH]：在 unset 代理之后、任何探针之前 source
# TEST_ROOT/env/knobs.sh（testenv.sh up 的唯一输出）。knobs.sh 缺失、TEST_ROOT
# 不存在、或任一 VRB_* 值命中拒绝清单（一·3 之 3/4 条）→ 立即 exit 2、stderr
# 点名缺失/越界的 knob，绝不回退生产默认值、不输出任何 `NN … PASS|FAIL` 行。
ENV_TEST=""
TEST_ROOT_ARG=""
_vrb_ps=("$@")
if [ "${#_vrb_ps[@]}" -gt 0 ]; then
    _vrb_i=0
    while [ "$_vrb_i" -lt "${#_vrb_ps[@]}" ]; do
        case "${_vrb_ps[$_vrb_i]}" in
            --env)
                if [ "$((_vrb_i + 1))" -ge "${#_vrb_ps[@]}" ]; then
                    printf 'verify-rebuild: --env 需要一个参数\n' >&2; exit 2
                fi
                ENV_TEST="${_vrb_ps[((_vrb_i + 1))]}"; _vrb_i=$(( _vrb_i + 2 )) ;;
            --root)
                if [ "$((_vrb_i + 1))" -ge "${#_vrb_ps[@]}" ]; then
                    printf 'verify-rebuild: --root 需要一个参数\n' >&2; exit 2
                fi
                TEST_ROOT_ARG="${_vrb_ps[((_vrb_i + 1))]}"; _vrb_i=$(( _vrb_i + 2 )) ;;
            --check|--window-seconds)
                _vrb_i=$(( _vrb_i + 2 )) ;;
            *)
                _vrb_i=$(( _vrb_i + 1 )) ;;
        esac
    done
fi
if [ -n "$ENV_TEST" ] && [ "$ENV_TEST" != "test" ]; then
    printf 'verify-rebuild: --env 仅接受 test，得到: %s\n' "$ENV_TEST" >&2
    exit 2
fi
if [ "$ENV_TEST" = "test" ]; then
    VRB_TEST_ROOT="${TEST_ROOT_ARG:-${FGT_ROOT:-/tmp/fleet-graph-testenv}}"
    VRB_TEST_ROOT="$(readlink -m "$VRB_TEST_ROOT" 2>/dev/null || printf '%s' "$VRB_TEST_ROOT")"
    if [ ! -d "$VRB_TEST_ROOT" ]; then
        printf 'verify-rebuild --env test: TEST_ROOT 不存在: %s\n' "$VRB_TEST_ROOT" >&2
        exit 2
    fi
    if [ ! -r "$VRB_TEST_ROOT/env/knobs.sh" ]; then
        printf 'verify-rebuild --env test: knobs.sh 缺失: %s/env/knobs.sh\n' "$VRB_TEST_ROOT" >&2
        exit 2
    fi
    # 唯一输入：source knobs.sh（纯 VRB_*=值 赋值）。source 之后下方既有
    # `${VRB_*:-默认}` 全部成为 no-op——test 模式绝不回退生产默认值。
    . "$VRB_TEST_ROOT/env/knobs.sh"
    # check 19/20 会在探针内调用 $VRB_CURRENT/scripts/testenv.sh（不带 --root），
    # 导出 FGT_ROOT 令其指向同一 TEST_ROOT（幂等摘要 / 拒绝，零副作用）。
    export FGT_ROOT="$VRB_TEST_ROOT"
    # 拒绝清单（一·3 条 3/4；默认内置；FGT_DENY_PATHS/FGT_DENY_PORTS 仅测试 fixture 用）。
    _vrb_deny_paths="${FGT_DENY_PATHS:-/data/fleet-graph:/data/apps:/data/ronin:/data/agent-bus:/data/code/self:/data}"
    _vrb_deny_ports="${FGT_DENY_PORTS:-5608 5610 5611 5614 7490 7491 7493 7494 17590 9090 15722}"
    _vrb_fail=""
    for _vrb_k in VRB_SYSTEMCTL VRB_CURRENT VRB_BUS_BASE VRB_BUS_TOKEN_FILE \
                  VRB_STATE_BASE VRB_MCP_BUS VRB_MCP_DD VRB_MCP_GOAL VRB_MCP_DECISION \
                  VRB_RUNS_ROOT VRB_SCHED_DIR VRB_DD_ROOT VRB_ROSTER VRB_SKILL_FILE \
                  VRB_PERSONA_FILES VRB_SUPERVISOR_ROOT VRB_SECRETS_DIR VRB_LLM_LEDGER; do
        # 缺 knob（未赋值）即退；空串值（如 VRB_PERSONA_FILES=""）是合法形态。
        eval "_vrb_set=\${$_vrb_k+set}"
        if [ -z "$_vrb_set" ]; then
            printf 'verify-rebuild --env test: knobs.sh 缺失 knob: %s\n' "$_vrb_k" >&2
            _vrb_fail=1
        fi
    done
    _vrb_path_under_deny() {
        # $1=knob 名 $2=路径：等于或位于任一生产根之下（readlink -m 解析 symlink）→ 报并记。
        local knob="$1" path="$2" dr pr
        [ -n "$path" ] || return 1
        pr="$(readlink -m "$path" 2>/dev/null || printf '%s' "$path")"
        local IFS=':'
        for dr in $_vrb_deny_paths; do
            [ -n "$dr" ] || continue
            dr="$(readlink -m "$dr" 2>/dev/null || printf '%s' "$dr")"
            if [ "$pr" = "$dr" ] || [ "${pr#"$dr"/}" != "$pr" ]; then
                printf 'verify-rebuild --env test: knob %s 越界指向生产根: %s (→ %s)\n' "$knob" "$path" "$dr" >&2
                return 0
            fi
        done
        return 1
    }
    _vrb_port_in_deny() {
        local p
        for p in $_vrb_deny_ports; do
            [ "$p" = "$1" ] && return 0
        done
        return 1
    }
    for _vrb_k in VRB_SYSTEMCTL VRB_CURRENT VRB_RUNS_ROOT VRB_SCHED_DIR VRB_DD_ROOT \
                  VRB_ROSTER VRB_SKILL_FILE VRB_SUPERVISOR_ROOT VRB_SECRETS_DIR \
                  VRB_BUS_TOKEN_FILE; do
        eval "_vrb_v=\"\${$_vrb_k:-}\""
        _vrb_path_under_deny "$_vrb_k" "$_vrb_v" && _vrb_fail=1
    done
    # VRB_PERSONA_FILES：冒号分隔的路径集，逐个同判。
    eval "_vrb_v=\"\${VRB_PERSONA_FILES:-}\""
    _vrb_ifs_save="$IFS"
    IFS=':'
    for _vrb_p in $_vrb_v; do
        [ -n "$_vrb_p" ] || continue
        _vrb_path_under_deny VRB_PERSONA_FILES "$_vrb_p" && _vrb_fail=1
    done
    IFS="$_vrb_ifs_save"
    # VRB_LLM_LEDGER：file:// 取路径部分同判；http(s):// 取端口判生产端口。
    eval "_vrb_v=\"\${VRB_LLM_LEDGER:-}\""
    case "$_vrb_v" in
        file://*)
            _vrb_path_under_deny VRB_LLM_LEDGER "${_vrb_v#file://}" && _vrb_fail=1 ;;
        http://*|https://*)
            _vrb_port="${_vrb_v#*://}"; _vrb_port="${_vrb_port%%/*}"; _vrb_port="${_vrb_port##*:}"
            case "$_vrb_port" in ''|*[!0-9]*) : ;;
                *) _vrb_port_in_deny "$_vrb_port" && {
                       printf 'verify-rebuild --env test: knob VRB_LLM_LEDGER 越界指向生产端口: %s\n' "$_vrb_port" >&2
                       _vrb_fail=1
                   } ;;
            esac ;;
        *) : ;;
    esac
    # 端口类 knob：四个 VRB_MCP_*（纯端口）与 VRB_BUS_BASE/VRB_STATE_BASE（URL 内端口）。
    for _vrb_k in VRB_MCP_BUS VRB_MCP_DD VRB_MCP_GOAL VRB_MCP_DECISION; do
        eval "_vrb_v=\"\${$_vrb_k:-}\""
        [ -n "$_vrb_v" ] || continue
        case "$_vrb_v" in
            ''|*[!0-9]*)
                printf 'verify-rebuild --env test: knob %s 非纯端口值: %s\n' "$_vrb_k" "$_vrb_v" >&2
                _vrb_fail=1 ;;
            *)  _vrb_port_in_deny "$_vrb_v" && {
                    printf 'verify-rebuild --env test: knob %s 越界指向生产端口: %s\n' "$_vrb_k" "$_vrb_v" >&2
                    _vrb_fail=1
                } ;;
        esac
    done
    for _vrb_k in VRB_BUS_BASE VRB_STATE_BASE; do
        eval "_vrb_v=\"\${$_vrb_k:-}\""
        [ -n "$_vrb_v" ] || continue
        _vrb_port="${_vrb_v#*://}"; _vrb_port="${_vrb_port%%/*}"; _vrb_port="${_vrb_port##*:}"
        case "$_vrb_port" in
            ''|*[!0-9]*)
                printf 'verify-rebuild --env test: knob %s 无法解析端口: %s\n' "$_vrb_k" "$_vrb_v" >&2
                _vrb_fail=1 ;;
            *)  _vrb_port_in_deny "$_vrb_port" && {
                    printf 'verify-rebuild --env test: knob %s 越界指向生产端口: %s\n' "$_vrb_k" "$_vrb_port" >&2
                    _vrb_fail=1
                } ;;
        esac
    done
    [ -n "$_vrb_fail" ] && exit 2
fi

# ---------------- VRB_* 覆盖键 ----------------
VRB_SYSTEMCTL="${VRB_SYSTEMCTL:-systemctl}"
VRB_CURRENT="${VRB_CURRENT:-/data/apps/fleet-graph/current}"
VRB_BUS_BASE="${VRB_BUS_BASE:-http://127.0.0.1:7490}"
VRB_BUS_TOKEN_FILE="${VRB_BUS_TOKEN_FILE:-/data/agent-bus/tokens/fleet-graph.token}"
VRB_STATE_BASE="${VRB_STATE_BASE:-http://127.0.0.1:7494}"
VRB_MCP_BUS="${VRB_MCP_BUS:-5608}"
VRB_MCP_DD="${VRB_MCP_DD:-5610}"
VRB_MCP_GOAL="${VRB_MCP_GOAL:-5611}"
VRB_MCP_DECISION="${VRB_MCP_DECISION:-5614}"
VRB_RUNS_ROOT="${VRB_RUNS_ROOT:-/data/fleet-graph/runs}"
VRB_SCHED_DIR="${VRB_SCHED_DIR:-$VRB_RUNS_ROOT/.scheduler}"
VRB_DD_ROOT="${VRB_DD_ROOT:-/data/fleet-graph/dd}"
VRB_ROSTER="${VRB_ROSTER:-$VRB_CURRENT/config/ronin-lines.json}"
VRB_SKILL_FILE="${VRB_SKILL_FILE:-/data/code/self/agent-skills/plugins/agent-skills/skills/fleet-supervisor/SKILL.md}"
VRB_SUPERVISOR_ROOT="${VRB_SUPERVISOR_ROOT:-/data/fleet-graph/supervisor}"
VRB_SECRETS_DIR="${VRB_SECRETS_DIR:-/data/fleet-graph/secrets}"
VRB_LLM_LEDGER="${VRB_LLM_LEDGER:-http://127.0.0.1:8101/api/request_events}"

BUS_TOKEN="$(cat "$VRB_BUS_TOKEN_FILE" 2>/dev/null)"

# VRB_PERSONA_FILES 默认派生：enabled 线的 persona/seat 路径字段（persona_file/persona/seat_file），
# 取不到则空集（08/21 的依据行注明派生来源与结果）。
PERSONA_NOTE=""
if [ -z "${VRB_PERSONA_FILES:-}" ]; then
    derived=""
    if [ -r "$VRB_ROSTER" ]; then
        while IFS= read -r p; do
            [ -n "$p" ] && derived="$derived:$p"
        done <<EOF
$(jq -r '.lines[]? | select(.enabled == true) | (.persona_file // .persona // .seat_file // empty)' "$VRB_ROSTER" 2>/dev/null)
EOF
        PERSONA_NOTE="persona 集:从 $VRB_ROSTER enabled 线 persona/seat 路径字段派生"
    else
        PERSONA_NOTE="persona 集:名册不可读($VRB_ROSTER)"
    fi
    VRB_PERSONA_FILES="${derived#:}"
else
    PERSONA_NOTE="persona 集:外部显式指定"
fi
[ -z "$VRB_PERSONA_FILES" ] && PERSONA_NOTE="$PERSONA_NOTE 取不到→空集"

WINDOW_SECONDS=86400
ONLY_CHECK=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check)
            [ $# -ge 2 ] || { printf 'verify-rebuild: --check 需要一个参数\n' >&2; exit 2; }
            ONLY_CHECK="$2"; shift 2 ;;
        --window-seconds)
            [ $# -ge 2 ] || { printf 'verify-rebuild: --window-seconds 需要一个参数\n' >&2; exit 2; }
            WINDOW_SECONDS="$2"; shift 2 ;;
        --env)
            # R1：值与语义已在顶部预扫描（fail-closed source knobs.sh）完成；
            # 此处仅接受该参数，保持未知参数报错语义不变。
            [ $# -ge 2 ] || { printf 'verify-rebuild: --env 需要一个参数\n' >&2; exit 2; }
            [ "$2" = "test" ] || { printf 'verify-rebuild: --env 仅接受 test，得到: %s\n' "$2" >&2; exit 2; }
            shift 2 ;;
        --root)
            # R1：TEST_ROOT 已在顶部预扫描消费；此处仅接受该参数。
            [ $# -ge 2 ] || { printf 'verify-rebuild: --root 需要一个参数\n' >&2; exit 2; }
            shift 2 ;;
        *)
            printf 'verify-rebuild: 未知参数: %s\n' "$1" >&2; exit 2 ;;
    esac
done

case "$ONLY_CHECK" in
    "") : ;;
    01|02|03|04|05|06|07|08|09|10|11|12|13|14|15|16|17|18|19|20|21) : ;;
    *) printf 'verify-rebuild: --check 仅接受 01–21，得到: %s\n' "$ONLY_CHECK" >&2; exit 2 ;;
esac
case "$WINDOW_SECONDS" in
    ''|*[!0-9]*) printf 'verify-rebuild: --window-seconds 仅接受非负整数秒，得到: %s\n' "$WINDOW_SECONDS" >&2; exit 2 ;;
esac

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

# ---------------- M0A 骨架（复制自 verify-lim.sh，clientInfo 名 verify-rebuild） ----------------
sanitize() {
    tr '\n\r\t' ' ' | tr -s ' ' | sed -e 's/^ *//' -e 's/ *$//'
}

vrb_emit() {
    local nn="$1" id="$2" verdict="$3"
    shift 3
    local ev
    ev="$(printf '%s' "$*" | sanitize)"
    printf '%s %s %s — %s\n' "$nn" "$id" "$verdict" "$ev" >> "$LOG"
}

json_get() {
    curl -s --noproxy '*' -m 15 "$1" 2>/dev/null
}

mcp_init() {
    curl -s -m 10 --noproxy '*' -D - -o /dev/null "http://127.0.0.1:$1/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify-rebuild","version":"1"}}}' 2>/dev/null \
        | tr -d '\r' | sed -n 's/^[Mm][Cc][Pp]-[Ss][Ee][Ss][Ss][Ii][Oo][Nn]-[Ii][Dd]:[[:space:]]*//Ip'
}

mcp_json() {
    local port="$1" method="$2" params="$3" sid
    sid="$(mcp_init "$port")"
    [ -z "$sid" ] && return 1
    curl -s -m 10 --noproxy '*' "http://127.0.0.1:$port/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -H "Mcp-Session-Id: $sid" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"$method\",\"params\":$params}" 2>/dev/null \
        | sed -n 's/^data: //p' | tr -d '\r'
}

mcp_tool_names() {
    local body
    body="$(mcp_json "$1" 'tools/list' '{}')" || return 1
    printf '%s\n' "$body" | jq -r '.result.tools[]?.name // empty' 2>/dev/null
}

needs_check() {
    [ -z "$ONLY_CHECK" ] && return 0
    [ "$ONLY_CHECK" = "$1" ] && return 0
    return 1
}

now="$(date +%s)"
window_start=$(( now - WINDOW_SECONDS ))
window_start_iso="$(date -u -d "@$window_start" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u '+%Y-%m-%dT%H:%M:%SZ')"

# 窗口内 dd 单枚举：$1=回调名，逐个传入 record.json 全路径。
vrb_each_window_record() {
    local cb="$1" d rf mt
    for d in "$VRB_DD_ROOT"/*/; do
        [ -d "$d" ] || continue
        rf="$d/record.json"
        [ -r "$rf" ] || continue
        mt="$(stat -c %Y "$rf" 2>/dev/null)"
        [ -n "$mt" ] && [ "$mt" -ge "$window_start" ] || continue
        "$cb" "$rf"
    done
}

persona_files_for_grep() {
    local p out="$VRB_SKILL_FILE"
    IFS=':' read -ra _vrb_personas <<< "$VRB_PERSONA_FILES"
    for p in "${_vrb_personas[@]:-}"; do
        [ -n "$p" ] && [ -r "$p" ] && out="$out $p"
    done
    printf '%s' "$out"
}

# ---------------- 01 trial-instances-stopped 试验实例已停 ----------------
vrb_check_01() {
    units="$("$VRB_SYSTEMCTL" --user list-units 'agent-bus-*' --plain --no-legend 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        vrb_emit 01 trial-instances-stopped FAIL "systemctl 探针失败 rc=$rc: $(printf '%s' "$units" | head -c 200)"
        return 0
    fi
    names="$(printf '%s\n' "$units" | awk 'NF{print $1}' | sed 's/\.service$//')"
    n_units="$(printf '%s\n' "$names" | grep -c .)"
    residual="$(printf '%s\n' "$names" | grep -vxE 'agent-bus-server|agent-bus-mcp')"
    if [ -z "$residual" ]; then
        vrb_emit 01 trial-instances-stopped PASS "agent-bus-* 已加载单元共 ${n_units} 个，名集合 ⊆ {agent-bus-server, agent-bus-mcp}（探针: $VRB_SYSTEMCTL --user list-units --plain --no-legend）"
    else
        vrb_emit 01 trial-instances-stopped FAIL "残留试验实例单元: $(printf '%s' "$residual" | tr '\n' ' ' | head -c 200)；允许集合 {agent-bus-server, agent-bus-mcp}，实测共 ${n_units} 个"
    fi
}

# ---------------- 02 dead-protocols-deregistered 死协议已注销 ----------------
vrb_check_02() {
    if [ -z "$BUS_TOKEN" ]; then
        vrb_emit 02 dead-protocols-deregistered FAIL "token 文件不可读: $VRB_BUS_TOKEN_FILE"
        return 0
    fi
    body="$(curl -sS --noproxy '*' -m 15 -H "Authorization: Bearer $BUS_TOKEN" "$VRB_BUS_BASE/v1/protocols" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$body" ]; then
        vrb_emit 02 dead-protocols-deregistered FAIL "agent-bus $VRB_BUS_BASE/v1/protocols 不可达（curl rc=$rc）: $(printf '%s' "$body" | head -c 160)"
        return 0
    fi
    if ! printf '%s' "$body" | jq -e . >/dev/null 2>&1; then
        vrb_emit 02 dead-protocols-deregistered FAIL "/v1/protocols 响应非合法 JSON: $(printf '%s' "$body" | head -c 160)"
        return 0
    fi
    count="$(printf '%s' "$body" | grep -oF 'coord.' | wc -l | tr -d ' ')"
    if [ "$count" = "0" ]; then
        vrb_emit 02 dead-protocols-deregistered PASS "协议注册表（$VRB_BUS_BASE/v1/protocols）中原 dead 协议 coord.* 出现次数为 0"
    else
        sample="$(printf '%s' "$body" | grep -oE '"kind"[[:space:]]*:[[:space:]]*"[^"]*coord[^"]*"' | head -3 | tr '\n' ' ')"
        vrb_emit 02 dead-protocols-deregistered FAIL "协议注册表响应中子串 coord.* 出现 ${count} 次，命中样例: ${sample:-<无 kind 字段样例>}"
    fi
}

# ---------------- 03 decisions-zero-swallowed 裁决零吞 ----------------
vrb_check_03() {
    body="$(json_get "$VRB_STATE_BASE/v1/decisions")"
    if [ -z "$body" ]; then
        vrb_emit 03 decisions-zero-swallowed FAIL "state $VRB_STATE_BASE /v1/decisions 不可达（空响应/连接失败）"
        return 0
    fi
    if ! printf '%s' "$body" | jq -e . >/dev/null 2>&1; then
        vrb_emit 03 decisions-zero-swallowed FAIL "/v1/decisions 响应非合法 JSON: $(printf '%s' "$body" | head -c 160)"
        return 0
    fi
    total="$(printf '%s' "$body" | jq '[.decisions[]?] | length' 2>/dev/null)"
    swallowed_all="$(printf '%s' "$body" | jq '[.decisions[]? | select((.state // "") == "swallowed")] | length' 2>/dev/null)"
    # 窗口过滤只在决策记录带可解析时间字段时启用；识别不出时间字段（过滤后计数为 0 而全量非 0）
    # 时退回全量计数并在依据注明，绝不让「过滤把一切滤空」伪装成 swallowed=0。
    swallowed_win="$(printf '%s' "$body" | jq -r --arg ws "$window_start_iso" \
        '[.decisions[]?
          | select((((.decided_at // .ts // .timestamp // .created_at // "") | tostring) != "")
                   and (((.decided_at // .ts // .timestamp // .created_at // "") | tostring) >= $ws))
          | select((.state // "") == "swallowed")] | length' 2>/dev/null)"
    win_dated="$(printf '%s' "$body" | jq -r --arg ws "$window_start_iso" \
        '[.decisions[]?
          | select((((.decided_at // .ts // .timestamp // .created_at // "") | tostring) != "")) ] | length' 2>/dev/null)"
    states="$(printf '%s' "$body" | jq -r '[.decisions[]?.state] | group_by(.) | map((.[0] // "null") + "=" + (length | tostring)) | join(" ")' 2>/dev/null)"
    if [ -z "${total:-}" ] || [ "${total:-null}" = "null" ]; then
        vrb_emit 03 decisions-zero-swallowed FAIL "/v1/decisions 解析失败（原文: $(printf '%s' "$body" | head -c 160)）"
        return 0
    fi
    if [ "$total" = "0" ]; then
        vrb_emit 03 decisions-zero-swallowed FAIL "窗口（${WINDOW_SECONDS}s）内裁决样本为空（total=0，状态分布: ${states:-空}），判据无可核读数"
        return 0
    fi
    if [ -n "${swallowed_win:-}" ] && [ "${swallowed_win}" != "null" ] && [ "${win_dated:-0}" != "0" ]; then
        scope="窗口（${WINDOW_SECONDS}s，自 ${window_start_iso}）内"
        swallowed="$swallowed_win"
    else
        scope="全量（决策记录无可识别时间字段，窗口过滤不可得，注明后全量计数）"
        swallowed="${swallowed_all:-?}"
    fi
    if [ "$swallowed" = "0" ]; then
        vrb_emit 03 decisions-zero-swallowed PASS "总 ${total} 条裁决中 swallowed=0（${scope}；分状态: ${states}）"
    else
        vrb_emit 03 decisions-zero-swallowed FAIL "总 ${total} 条裁决中 swallowed=${swallowed}（${scope}；分状态: ${states}），非零，裁决零吞未成立"
    fi
}

# ---------------- 04 external-decision-wakes-line 外部裁决送达即唤醒（合成靶） ----------------
# 现场合成的只是 vrb-selftest- 命名空间的一次性靶 id（不落任何盘面文件）：对 decision MCP
# 投一次裁决、再查消费证据（state /v1/decisions 的 consumed 记录 + 下一代 unit）。
# 合成 blocked 靶线本身所需机制（调度器 wake 事实）R0 未落地 → 拒绝/无消费证据如实 FAIL。
vrb_check_04() {
    target="vrb-selftest-wake-$(date +%s)-$$"
    tools="$(mcp_tool_names "$VRB_MCP_DECISION")"
    if [ -z "$tools" ]; then
        vrb_emit 04 external-decision-wakes-line FAIL "decision MCP :$VRB_MCP_DECISION tools/list 不可达，无法对合成靶（$target）投裁决；送达即唤醒机制不可核"
        return 0
    fi
    deliver_tool="$(printf '%s\n' "$tools" | grep -E '^(decision_deliver|deliver_decision|decision_publish)$' | head -1)"
    if [ -z "$deliver_tool" ]; then
        vrb_emit 04 external-decision-wakes-line FAIL "decision MCP :$VRB_MCP_DECISION 无 deliver 工具（tools/list: $(printf '%s' "$tools" | tr '\n' ' ' | head -c 160)），合成靶裁决无法投递，机制未落地"
        return 0
    fi
    res="$(mcp_json "$VRB_MCP_DECISION" 'tools/call' "{\"name\":\"$deliver_tool\",\"arguments\":{\"decision\":\"APPROVE\",\"reason\":\"verify-rebuild check04 synthetic wake probe\",\"target_kind\":\"line\",\"line\":\"$target\",\"principal\":\"vrb-selftest-probe\"}}")"
    if [ -z "$res" ]; then
        vrb_emit 04 external-decision-wakes-line FAIL "decision MCP :$VRB_MCP_DECISION tools/call 空响应（探针失败），合成靶（$target）裁决未送达"
        return 0
    fi
    text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
    code="$(printf '%s' "$text" | jq -r '.code // empty' 2>/dev/null)"
    status="$(printf '%s' "$text" | jq -r '.status // empty' 2>/dev/null)"
    dec="$(json_get "$VRB_STATE_BASE/v1/decisions")"
    consumed="$(printf '%s' "$dec" | jq -r --arg t "$target" '[.decisions[]? | select((.state // "") == "consumed" and ((.owner.id // .owner // "") | tostring | contains($t)))] | length' 2>/dev/null)"
    new_units="$("$VRB_SYSTEMCTL" --user list-units "fleet-graph-line-${target}-*" --plain --no-legend 2>/dev/null | grep -c .)"
    if [ "$status" = "accepted" ] || printf '%s' "$text" | grep -qi 'accepted'; then
        if [ "${consumed:-0}" != "0" ] && [ "${new_units:-0}" != "0" ]; then
            vrb_emit 04 external-decision-wakes-line PASS "合成靶（$target）裁决送达且被消费（consumed 记录=${consumed}，下一代 unit=${new_units}，S10 消费证据成立）；探针靶 id 一次性，无真实线被触碰"
        else
            vrb_emit 04 external-decision-wakes-line FAIL "合成靶（$target）裁决受理但消费证据缺失：consumed 记录=${consumed:-解析失败}，下一代 unit=${new_units:-0}（非仅起 unit 的 S10 证据不成立）"
        fi
    else
        vrb_emit 04 external-decision-wakes-line FAIL "合成靶（$target）投递被拒（code=${code:-无}, status=${status:-无}: $(printf '%s' "$text" | head -c 160)）——外部裁决对合成 blocked 靶线不可送达，送达即唤醒机制未覆盖（R0 预期红）"
    fi
}

# ---------------- 05 waiting-zero-consumption 等待零消耗 ----------------
vrb_check_05() {
    n_sched=0
    waiting=""
    for f in "$VRB_SCHED_DIR"/wf-*.json; do
        [ -e "$f" ] || continue
        n_sched=$(( n_sched + 1 ))
        st="$(jq -r '.status // .state // empty' "$f" 2>/dev/null)"
        if [ "$st" = "waiting_dd" ] || grep -q 'waiting_dd' "$f" 2>/dev/null; then
            waiting="$waiting $(basename "$f" .json)"
        fi
    done
    if [ -z "$waiting" ]; then
        vrb_emit 05 waiting-zero-consumption FAIL "无 waiting_dd 样本（$VRB_SCHED_DIR 下 wf-*.json 共 ${n_sched} 个，均非 waiting_dd）——等待零消耗判据无样本可核；灵智账本 $VRB_LLM_LEDGER 未查询。缺：waiting_dd 状态词表（机制未落地）"
        return 0
    fi
    ledger="$(curl -sS --noproxy '*' -m 15 -w '\n%{http_code}' "$VRB_LLM_LEDGER?window_seconds=$WINDOW_SECONDS" 2>&1)"
    lrc=$?
    lhttp="${ledger##*$'\n'}"
    lbody="${ledger%$'\n'*}"
    if [ "$lrc" -ne 0 ] || [ "$lhttp" != "200" ]; then
        vrb_emit 05 waiting-zero-consumption FAIL "waiting_dd 线（${waiting# }）存在，但灵智账本 $VRB_LLM_LEDGER 不可达（http=${lhttp:-无} curl rc=$lrc），request_events 计数不可得。缺：账本查询面"
        return 0
    fi
    if ! printf '%s' "$lbody" | jq -e . >/dev/null 2>&1; then
        vrb_emit 05 waiting-zero-consumption FAIL "灵智账本 $VRB_LLM_LEDGER 响应非合法 JSON，request_events 计数不可得: $(printf '%s' "$lbody" | head -c 160)"
        return 0
    fi
    nonzero=""
    for ln in $waiting; do
        alias="${ln#wf-}"
        cnt="$(printf '%s' "$lbody" | jq -r --arg a "$alias" '[.. | objects | select(((.alias // .line // "") | tostring) == $a)] | length' 2>/dev/null)"
        [ -n "${cnt:-}" ] && [ "$cnt" != "0" ] && nonzero="$nonzero ${alias}=${cnt}"
    done
    if [ -z "$nonzero" ]; then
        vrb_emit 05 waiting-zero-consumption PASS "waiting_dd 线（${waiting# }）在账本 $VRB_LLM_LEDGER 的 request_events 计数全为 0（窗口 ${WINDOW_SECONDS}s）"
    else
        vrb_emit 05 waiting-zero-consumption FAIL "waiting_dd 线等待期间仍有模型消耗（$VRB_LLM_LEDGER request_events）:${nonzero}"
    fi
}

# ---------------- 06 acceptance-supervisor-only 验收标准只有监督者能改 ----------------
# 执行方向：goal MCP 上不得存在执行方可用的 acceptance/contract 写入口（有则对合成靶试改须被拒）；
# 监督者向：只读检索 $VRB_SUPERVISOR_ROOT 的 contract_changed 留痕（绝不主动改真契约）。
vrb_check_06() {
    missing=""
    tools="$(mcp_tool_names "$VRB_MCP_GOAL")"
    if [ -z "$tools" ]; then
        missing="$missing goal-MCP tools/list 失败(:$VRB_MCP_GOAL 不可达，执行方写入入口不可核)"
    else
        write_entries="$(printf '%s\n' "$tools" | grep -iE 'acceptance|contract' | tr '\n' ' ')"
        if [ -n "$write_entries" ]; then
            target="vrb-selftest-acc-$(date +%s)-$$"
            res="$(mcp_json "$VRB_MCP_GOAL" 'tools/call' "{\"name\":\"$(printf '%s' "$write_entries" | awk '{print $1}')\",\"arguments\":{\"folder_id\":\"$target\",\"acceptance\":[[\"echo\",\"mutation-probe\"]]}}")"
            text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
            if printf '%s' "$text" | grep -qi 'accepted\|ok\|updated'; then
                missing="$missing 执行方对合成靶（$target）改 acceptance 被接受（应为拒绝+留痕）"
            fi
        fi
        # 无写入口 = 拒绝面为「入口不存在」（D16 的执行方向拒绝语义在名册 PR 面成立）
    fi
    # 只读检索 contract_changed 留痕：限定小文本文件（跳过 sqlite/大二进制、限深限量、
    # timeout 10s），监督面 state 树可能很大，探针不允许无界扫描。
    traces="$(timeout 10 bash -c "find '$VRB_SUPERVISOR_ROOT' -maxdepth 3 -type f ! -name '*.sqlite3*' ! -name '*.sqlite3-*' -size -5M 2>/dev/null | head -400 | xargs -r grep -l 'contract_changed' 2>/dev/null | head -3")"
    trc=$?
    if [ "$trc" -ne 0 ]; then
        missing="$missing contract_changed 留痕检索受限（timeout/遍历失败 rc=$trc，$VRB_SUPERVISOR_ROOT）"
    elif [ -z "$traces" ]; then
        missing="$missing contract_changed 留痕未找到（$VRB_SUPERVISOR_ROOT 有界检索无命中，监督者向留痕不可核）"
    fi
    if [ -z "$missing" ]; then
        vrb_emit 06 acceptance-supervisor-only PASS "执行方向无 acceptance/contract 写入口（goal MCP tools: $(printf '%s' "$tools" | tr '\n' ' ' | head -c 120)），监督者向 contract_changed 留痕存在（$VRB_SUPERVISOR_ROOT: $(printf '%s' "$traces" | tr '\n' ' ')）"
    else
        vrb_emit 06 acceptance-supervisor-only FAIL "缺失:${missing}"
    fi
}

# ---------------- 07 seats-single-source 座位单一来源 ----------------
vrb_check_07() {
    pid="$("$VRB_SYSTEMCTL" --user show fleet-graph-dd-mcp -p MainPID --value 2>/dev/null)"
    rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$pid" ] || [ "$pid" = "0" ]; then
        vrb_emit 07 seats-single-source FAIL "dd-mcp（fleet-graph-dd-mcp）MainPID 不可得（rc=$rc 值=${pid:-空}），无法读 /proc cmdline 核 --stage-model 覆盖键"
        return 0
    fi
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    if [ -z "$cmdline" ]; then
        vrb_emit 07 seats-single-source FAIL "/proc/$pid/cmdline 不可读"
        return 0
    fi
    seat_bad=0
    seat_note=""
    vrb_07_cb() {
        rf="$1"
        src="$(jq -r '.seats_source // empty' "$rf" 2>/dev/null)"
        has_seats="$(jq -r 'has("seats")' "$rf" 2>/dev/null)"
        [ "$has_seats" = "true" ] || return 0
        vrb_07_n=$(( vrb_07_n + 1 ))
        bad="$(printf '%s' "$src" | jq -r 'to_entries[]? | select((.value | tostring | test("cmdline|stage-model|override"))) | .key' 2>/dev/null | tr '\n' ' ')"
        if [ -n "$bad" ]; then
            seat_bad=$(( seat_bad + 1 ))
            [ -z "$seat_note" ] && seat_note="$(basename "$(dirname "$rf")"):seats_source=$src"
        fi
    }
    vrb_07_n=0
    vrb_each_window_record vrb_07_cb
    if printf '%s' "$cmdline" | grep -q -- '--stage-model'; then
        vrb_emit 07 seats-single-source FAIL "dd-mcp cmdline 含 --stage-model 覆盖键: $(printf '%s' "$cmdline" | head -c 200)；窗口内带 seats 的单 ${vrb_07_n} 张${seat_note:+，违规座位来源: ${seat_note}}"
    elif [ "$seat_bad" -gt 0 ]; then
        vrb_emit 07 seats-single-source FAIL "dd-mcp cmdline 无 --stage-model，但窗口内 ${seat_bad}/${vrb_07_n} 张单座位来源非派单请求/role registry（样例: ${seat_note}）"
    else
        vrb_emit 07 seats-single-source PASS "dd-mcp cmdline 无 --stage-model: $(printf '%s' "$cmdline" | head -c 160)；窗口内带 seats 的单 ${vrb_07_n} 张座位来源均合规（无 cmdline/override 痕迹）"
    fi
}

# ---------------- 08 public-interface-mcp-only public interface 只有 MCP ----------------
vrb_check_08() {
    if [ ! -r "$VRB_SKILL_FILE" ]; then
        vrb_emit 08 public-interface-mcp-only FAIL "SKILL.md 不可读: $VRB_SKILL_FILE（${PERSONA_NOTE}）"
        return 0
    fi
    grep_files="$(persona_files_for_grep)"
    hits="$(grep -rEn 'curl .*:(7490|7494)|fleet-graph line |fleet-maint' $grep_files 2>/dev/null)"
    n_hits="$(printf '%s\n' "$hits" | grep -c .)"
    if [ "$n_hits" = "0" ]; then
        vrb_emit 08 public-interface-mcp-only PASS "监督面 skill 与线 persona 中裸 HTTP(:7490/:7494)/fleet-graph line/fleet-maint 入口命中 0 条（$PERSONA_NOTE，检索面: ${grep_files}）"
    else
        vrb_emit 08 public-interface-mcp-only FAIL "命中 ${n_hits} 条裸 HTTP(:7490/:7494)/fleet-graph line/fleet-maint 入口（${PERSONA_NOTE}），样例: $(printf '%s' "$hits" | head -3 | tr '\n' ' ' | head -c 200)"
    fi
}

# ---------------- 09 takeover-one-call 接手一次调用 ----------------
vrb_check_09() {
    body="$(curl -s --noproxy '*' -m 15 -w $'\n%{http_code}' "$VRB_STATE_BASE/v1/takeover" 2>/dev/null)"
    http="${body##*$'\n'}"
    body="${body%$'\n'*}"
    if [ "$http" = "200" ] && printf '%s' "$body" | jq -e . >/dev/null 2>&1; then
        missing=""
        for k in roster line_states awaiting_decisions pending_releases auth_mode current_release; do
            printf '%s' "$body" | jq -e --arg k "$k" 'has($k)' >/dev/null 2>&1 || missing="$missing $k"
        done
        if [ -z "$missing" ]; then
            vrb_emit 09 takeover-one-call PASS "state 面 /v1/takeover 一次零上下文调用返回六项（roster/line_states/awaiting_decisions/pending_releases/auth_mode/current_release 全齐）"
        else
            vrb_emit 09 takeover-one-call FAIL "/v1/takeover 可达但缺项:${missing}（实测顶层键: $(printf '%s' "$body" | jq -r 'keys | join(",")' 2>/dev/null | head -c 160)）"
        fi
        return 0
    fi
    tools="$(mcp_tool_names "$VRB_MCP_DECISION")"
    state_tools="$(mcp_tool_names "$VRB_MCP_DD")"
    takeover_tool="$(printf '%s\n' "$tools" "$state_tools" | grep -x 'state_takeover' | head -1)"
    if [ -n "$takeover_tool" ]; then
        vrb_emit 09 takeover-one-call FAIL "state 面 /v1/takeover 不可达（http=${http:-无}），state_takeover 工具在 MCP 面存在但六项读模型不可核（无一次调用面）"
    else
        vrb_emit 09 takeover-one-call FAIL "零上下文一次调用拿不到六项：state 面 /v1/takeover http=${http:-无}；decision/dd MCP tools/list 无 state_takeover（decision: $(printf '%s' "$tools" | tr '\n' ' ' | head -c 100)；dd: $(printf '%s' "$state_tools" | tr '\n' ' ' | head -c 100)）。缺失项：名册/线状态/等拍板/待上线/授权模式/当前 release 的单一接管面未上线"
    fi
}

# ---------------- 10 mcp-function-probes 功能探针 ----------------
vrb_check_10() {
    results=""
    all_ok=1
    probe_face() {
        local label="$1" port="$2" tool="$3"
        local names call bok
        names="$(mcp_tool_names "$port")"
        if [ -z "$names" ]; then
            results="${results}${label}:${port}=tools/list失败; "
            all_ok=0
            return 0
        fi
        if ! printf '%s\n' "$names" | grep -qx "$tool"; then
            results="${results}${label}:${port}=无只读工具${tool}(tools: $(printf '%s' "$names" | tr '\n' ' ' | head -c 80)); "
            all_ok=0
            return 0
        fi
        call="$(mcp_json "$port" 'tools/call' "{\"name\":\"$tool\",\"arguments\":{}}")"
        bok="$(printf '%s' "$call" | jq -r 'if .result then "ok" else (.error.message // "err") end' 2>/dev/null)"
        results="${results}${label}:${port}=${tool}:${bok}; "
        [ "$bok" = "ok" ] || all_ok=0
    }
    probe_face bus "$VRB_MCP_BUS" bus_agent_list
    probe_face dd "$VRB_MCP_DD" development_list
    probe_face goal "$VRB_MCP_GOAL" goal_list
    probe_face decision "$VRB_MCP_DECISION" decision_list
    st_code="$(curl -s --noproxy '*' -m 10 -o /dev/null -w '%{http_code}' "$VRB_STATE_BASE/v1/lines" 2>/dev/null)"
    if [ "$st_code" = "200" ]; then
        results="${results}state=$VRB_STATE_BASE=read:/v1/lines ok"
    else
        results="${results}state=$VRB_STATE_BASE=read:/v1/lines http=${st_code:-无响应}"
        all_ok=0
    fi
    if [ "$all_ok" = "1" ]; then
        vrb_emit 10 mcp-function-probes PASS "五个面 tools/list + 只读真调用全部成功：${results}"
    else
        vrb_emit 10 mcp-function-probes FAIL "五个面逐面结果：${results}"
    fi
}

# ---------------- 11 gate-decided-by-dispatcher gate 由派单线自判 ----------------
vrb_check_11() {
    n_window=0
    n_gate=0
    n_mismatch=0
    sample=""
    vrb_11_cb() {
        rf="$1"
        n_window=$(( n_window + 1 ))
        dispatched_by="$(jq -r '.dispatched_by // empty' "$rf" 2>/dev/null)"
        repo_path="$(jq -r '.repo_path // empty' "$rf" 2>/dev/null)"
        generation="$(jq -r '.generation // empty' "$rf" 2>/dev/null)"
        case "$generation" in ''|*[!0-9]*) generation=1 ;; esac
        decided_by=""
        gf="$repo_path/.dev-dispatch/gate/decision-g${generation}.json"
        if [ -n "$repo_path" ] && [ -r "$gf" ]; then
            decided_by="$(jq -r '.decided_by // empty' "$gf" 2>/dev/null)"
        fi
        [ -z "$decided_by" ] && return 0
        n_gate=$(( n_gate + 1 ))
        # 归一：取首个空白分隔 token 再去掉尾部非 id 字符（真机存在「wf-xxx（线内 D5 自判…）」
        # 这类全角括号紧贴的署名写法；verify-lim check 11 同款归一先例）。
        decided_tok="$(printf '%s' "$decided_by" | awk '{print $1}' | sed 's/[^A-Za-z0-9_.-]*$//')"
        if [ "$decided_tok" != "$dispatched_by" ]; then
            n_mismatch=$(( n_mismatch + 1 ))
            [ -z "$sample" ] && sample="$(basename "$(dirname "$rf")"):decided_by=${decided_by}≠dispatched_by=${dispatched_by:-空}"
        fi
    }
    vrb_each_window_record vrb_11_cb
    if [ "$n_window" = "0" ]; then
        vrb_emit 11 gate-decided-by-dispatcher FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核（$VRB_DD_ROOT 无带 record.json 的单）"
    elif [ "$n_gate" = "0" ]; then
        vrb_emit 11 gate-decided-by-dispatcher FAIL "窗口内 ${n_window} 张 dd 单，0 张有闸裁决署名（.dev-dispatch/gate/decision-g<N>.json 均缺），decided_by==dispatched_by 无从比对"
    elif [ "$n_mismatch" -gt 0 ]; then
        vrb_emit 11 gate-decided-by-dispatcher FAIL "窗口内 ${n_window} 张单、${n_gate} 张过闸，其中 ${n_mismatch} 张 decided_by≠dispatched_by（样例: ${sample}）"
    else
        vrb_emit 11 gate-decided-by-dispatcher PASS "窗口内 ${n_window} 张 dd 单、${n_gate} 张过闸，decided_by 与 dispatched_by 100% 相等（逐单 gate 裁决文件比对）"
    fi
}

# ---------------- 12 gate-unforgeable-outside-line 线外无法批 gate（合成靶，跑完即清） ----------------
# 照 verify-lim.sh check 12 先例：现场合成一张 vrb-selftest- awaiting_gate 靶单
# （record.json+status.json 落 $VRB_DD_ROOT 一次性目录），以非派单身份经 MCP 与 HTTP 双路
# 尝试释放；靶单状态须不变；跑完即清，真实 dev-fg-* 单零触碰。
vrb_check_12() {
    target="vrb-selftest-gate-$(date +%s)-$$"
    probe_dir="$VRB_DD_ROOT/$target"
    foreign="vrb-selftest-foreign-$$"
    dispatcher="vrb-selftest-owner-$$"
    mkdir -p "$probe_dir" 2>/dev/null
    printf '%s' "{\"development_id\":\"$target\",\"repo_path\":\"/tmp/vrb-selftest-no-repo\",\"remote_ref\":\"refs/heads/release/vrb-selftest\",\"target_base_commit\":\"0000000000000000000000000000000000000000\",\"dispatched_by\":\"$dispatcher\",\"generation\":1,\"seats\":{\"implement\":\"vrb-selftest\"},\"seats_source\":{\"implement\":\"line-explicit\"}}" > "$probe_dir/record.json" 2>/dev/null
    printf '%s' "{\"development_id\":\"$target\",\"state\":\"awaiting_gate\",\"generation\":1,\"dispatched_by\":\"$dispatcher\"}" > "$probe_dir/status.json" 2>/dev/null
    before="$(cat "$probe_dir/status.json" 2>/dev/null)"
    findings=""
    # MCP 路：dd / decision 面上的 gate/release 入口，逐一以非派单身份试放
    dd_tools="$(mcp_tool_names "$VRB_MCP_DD")"
    dec_tools="$(mcp_tool_names "$VRB_MCP_DECISION")"
    gate_tools="$(printf '%s\n' "$dd_tools" "$dec_tools" | grep -iE 'gate|release' | grep -v '^$' | head -3)"
    if [ -z "$gate_tools" ]; then
        findings="${findings}MCP 路无 gate/release 入口（dd tools: $(printf '%s' "$dd_tools" | tr '\n' ' ' | head -c 100)；decision tools: $(printf '%s' "$dec_tools" | tr '\n' ' ' | head -c 100)）；"
    else
        for gt in $gate_tools; do
            port="$VRB_MCP_DD"
            printf '%s\n' "$dec_tools" | grep -qx "$gt" && port="$VRB_MCP_DECISION"
            res="$(mcp_json "$port" 'tools/call' "{\"name\":\"$gt\",\"arguments\":{\"development_id\":\"$target\",\"decision\":\"RELEASE\",\"principal\":\"$foreign\"}}")"
            text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
            if printf '%s' "$text" | grep -qi 'accepted\|"ok":true\|released'; then
                findings="${findings}MCP 路以非派单身份（$foreign）经 $gt 释放合成靶被接受: $(printf '%s' "$text" | head -c 120)；"
            else
                findings="${findings}MCP 路 $gt 拒绝/无此入口（$(printf '%s' "$text" | head -c 80)）；"
            fi
        done
    fi
    # HTTP 路：state 面的 gate/release 端点
    http_out="$(curl -sS --noproxy '*' -m 15 -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' -d "{\"development_id\":\"$target\",\"decision\":\"RELEASE\",\"principal\":\"$foreign\"}" "$VRB_STATE_BASE/v1/gate/release" 2>&1)"
    hrc=$?
    hhttp="${http_out##*$'\n'}"
    hbody="${http_out%$'\n'*}"
    if [ "$hrc" -ne 0 ]; then
        findings="${findings}HTTP 路不可达（rc=$hrc）；"
    elif [ "$hhttp" = "2xx" ] || [ "${hhttp#2}" != "$hhttp" ]; then
        findings="${findings}HTTP 路以非派单身份释放被接受（http=$hhttp: $(printf '%s' "$hbody" | head -c 120)）；"
    else
        findings="${findings}HTTP 路拒绝/无此入口（http=$hhttp）；"
    fi
    after="$(cat "$probe_dir/status.json" 2>/dev/null)"
    status_ok=1
    [ "$before" = "$after" ] && [ -n "$before" ] || status_ok=0
    rm -rf "$probe_dir" 2>/dev/null
    cleanup="清理完成"
    [ -e "$probe_dir" ] && cleanup="清理失败（$probe_dir 仍在）"
    if [ "$hrc" -ne 0 ] && [ -z "$gate_tools" ]; then
        vrb_emit 12 gate-unforgeable-outside-line FAIL "双路释放尝试均无法执行（MCP 面不可达+HTTP 路不可达），线外无法批 gate 判据不可核；${findings}合成靶（$target）${cleanup}"
    elif [ "$status_ok" = "1" ] && ! printf '%s' "$findings" | grep -q '被接受'; then
        vrb_emit 12 gate-unforgeable-outside-line PASS "线外（$foreign）对合成 awaiting_gate 靶单（$target）MCP+HTTP 双路释放均拒绝/无此入口，靶单状态不变；${findings}合成靶已清理（${cleanup}，真实单零触碰）"
    else
        vrb_emit 12 gate-unforgeable-outside-line FAIL "线外释放存在可 forging 入口或靶单状态被改（status_ok=$status_ok）；${findings}合成靶（$target）${cleanup}"
    fi
}

# ---------------- 13 dd-touches-line-branch-only DD 只碰线分支 ----------------
vrb_check_13() {
    n_window=0
    bad=0
    sample=""
    vrb_13_cb() {
        rf="$1"
        n_window=$(( n_window + 1 ))
        ref="$(jq -r '.remote_ref // empty' "$rf" 2>/dev/null)"
        base="$(jq -r '.target_base_commit // empty' "$rf" 2>/dev/null)"
        if ! printf '%s' "$ref" | grep -q '^refs/heads/release/'; then
            bad=$(( bad + 1 ))
            [ -z "$sample" ] && sample="$(basename "$(dirname "$rf")"):remote_ref=${ref:-空}"
        elif [ -z "$base" ] || printf '%s' "$base" | grep -qE '^0+$' || [ "${#base}" -ne 40 ]; then
            bad=$(( bad + 1 ))
            [ -z "$sample" ] && sample="$(basename "$(dirname "$rf")"):target_base_commit=${base:-空}"
        fi
    }
    vrb_each_window_record vrb_13_cb
    if [ "$n_window" = "0" ]; then
        vrb_emit 13 dd-touches-line-branch-only FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核"
    elif [ "$bad" -gt 0 ]; then
        vrb_emit 13 dd-touches-line-branch-only FAIL "窗口内 ${n_window} 张单，${bad} 张 remote_ref 非 refs/heads/release/<line-id> 或 target_base_commit 非全量非零 commit（样例: ${sample}）"
    else
        vrb_emit 13 dd-touches-line-branch-only PASS "窗口内 ${n_window} 张单 remote_ref 均为 refs/heads/release/<line-id> 且 target_base_commit 均为全量非零 commit"
    fi
}

# ---------------- 14 rebase-before-dispatch 派单前 rebase ----------------
vrb_check_14() {
    n_window=0
    found_rebase=0
    sample=""
    vrb_14_cb() {
        rf="$1"
        d="$(dirname "$rf")"
        n_window=$(( n_window + 1 ))
        if { [ -r "$d/events.jsonl" ] && grep -qE 'rebase.*release/' "$d/events.jsonl" 2>/dev/null; } \
            || { [ -r "$d/dd.log" ] && grep -qE 'rebase.*release/' "$d/dd.log" 2>/dev/null; }; then
            found_rebase=$(( found_rebase + 1 ))
        fi
        [ -z "$sample" ] && [ -r "$d/events.jsonl" ] && sample="$(grep -m1 '"stage"[[:space:]]*:[[:space:]]*"configure"' "$d/events.jsonl" 2>/dev/null | head -c 160)"
        return 0
    }
    vrb_each_window_record vrb_14_cb
    lines_body="$(json_get "$VRB_STATE_BASE/v1/lines")"
    behind_zero="$(printf '%s' "$lines_body" | jq -r '[.lines[]? | select((.release_behind // -1) == 0)] | length' 2>/dev/null)"
    behind_field="$(printf '%s' "$lines_body" | jq -r '[.lines[]? | has("release_behind")] | any' 2>/dev/null)"
    if [ "$n_window" = "0" ]; then
        vrb_emit 14 rebase-before-dispatch FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核，configure 段 rebase 记录无从查找"
    elif [ "$found_rebase" = "0" ]; then
        vrb_emit 14 rebase-before-dispatch FAIL "窗口内 ${n_window} 张单的 events.jsonl/dd.log configure 段无 rebase 到 release/<line-id> 记录（样例 configure 事件: ${sample:-<空>}），release_behind 读数=${behind_zero:-无}（state 面字段在否: ${behind_field:-不可得}）"
    elif [ "${behind_zero:-}" = "" ] || [ "${behind_zero}" = "null" ] || [ "$behind_zero" = "0" ]; then
        vrb_emit 14 rebase-before-dispatch FAIL "窗口内 ${found_rebase}/${n_window} 张单有 rebase 记录，但 state 面无 release_behind==0 读数（字段在否: ${behind_field:-不可得}，零读数张数=${behind_zero:-无}）——rebase 后回 0 的读数面未上线"
    else
        vrb_emit 14 rebase-before-dispatch PASS "窗口内 ${found_rebase}/${n_window} 张单有 configure rebase 记录，state 面 release_behind==0 读数 ${behind_zero} 条"
    fi
}

# ---------------- 15 message-delivered-and-acked 消息必达必回（合成靶） ----------------
vrb_check_15() {
    tools="$(mcp_tool_names "$VRB_MCP_GOAL")"
    if ! printf '%s\n' "$tools" | grep -qx 'line_message'; then
        vrb_emit 15 message-delivered-and-acked FAIL "goal MCP :$VRB_MCP_GOAL tools/list 无 line_message 工具（监督者消息工具缺失，实测: $(printf '%s' "$tools" | tr '\n' ' ' | head -c 120)）"
        return 0
    fi
    target="vrb-selftest-msg-$(date +%s)-$$"
    res="$(mcp_json "$VRB_MCP_GOAL" 'tools/call' "{\"name\":\"line_message\",\"arguments\":{\"line\":\"$target\",\"text\":\"verify-rebuild check15 ack probe\",\"kind\":\"instruction\",\"sent_by\":\"vrb-selftest-probe\"}}")"
    text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
    code="$(printf '%s' "$text" | jq -r '.code // empty' 2>/dev/null)"
    msg_id="$(printf '%s' "$text" | jq -r '.message_id // empty' 2>/dev/null)"
    if [ -z "$msg_id" ]; then
        vrb_emit 15 message-delivered-and-acked FAIL "对合成靶线（$target）的 line_message 未产出 message_id（code=${code:-无}: $(printf '%s' "$text" | head -c 160)）——合成靶投递被拒/无回执，必达必回无从核（R0 预期红）"
        return 0
    fi
    lines_body="$(json_get "$VRB_STATE_BASE/v1/lines")"
    ack_row="$(printf '%s' "$lines_body" | jq -r --arg m "$msg_id" '[.lines[]?.wake_facts.line_message_acks[]? | select(((.message_id // .id // "") | tostring) == $m)] | length' 2>/dev/null)"
    next_input="$(grep -l "$msg_id" "$VRB_RUNS_ROOT/$target"/coord/rounds.jsonl 2>/dev/null | head -1)"
    if [ "${ack_row:-0}" != "0" ] && [ -n "$next_input" ]; then
        vrb_emit 15 message-delivered-and-acked PASS "合成靶（$target）消息（$msg_id）已入下一代输入（$next_input）且 ack 台账有行（${ack_row}）"
    else
        vrb_emit 15 message-delivered-and-acked FAIL "合成靶（$target）消息（$msg_id）送达/回执证据缺失：ack 台账行=${ack_row:-0}，下一代输入含该消息=${next_input:-无}（机制未覆盖合成靶，R0 预期红）"
    fi
}

# ---------------- 16 message-not-a-decision 消息不能冒充裁决（合成靶） ----------------
vrb_check_16() {
    tools="$(mcp_tool_names "$VRB_MCP_GOAL")"
    if ! printf '%s\n' "$tools" | grep -qx 'line_message'; then
        vrb_emit 16 message-not-a-decision FAIL "先决不满足：goal MCP :$VRB_MCP_GOAL tools/list 无 line_message 工具（实测: $(printf '%s' "$tools" | tr '\n' ' ' | head -c 120)），无法验证『仅消息不解除 waiting_decision 驻停』"
        return 0
    fi
    target="vrb-selftest-dec-$(date +%s)-$$"
    res="$(mcp_json "$VRB_MCP_GOAL" 'tools/call' "{\"name\":\"line_message\",\"arguments\":{\"line\":\"$target\",\"text\":\"APPROVE\",\"kind\":\"info\",\"sent_by\":\"vrb-selftest-probe\"}}")"
    text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
    code="$(printf '%s' "$text" | jq -r '.code // empty' 2>/dev/null)"
    msg_id="$(printf '%s' "$text" | jq -r '.message_id // empty' 2>/dev/null)"
    lines_body="$(json_get "$VRB_STATE_BASE/v1/lines")"
    parked="$(printf '%s' "$lines_body" | jq -r --arg t "$target" '[.lines[]? | select((.id // .line // .folder_id // "") == $t)] | length' 2>/dev/null)"
    if [ -z "$msg_id" ]; then
        vrb_emit 16 message-not-a-decision FAIL "对合成 waiting_decision 靶线（$target）只发 line_message(\"APPROVE\") 被拒/无回执（code=${code:-无}: $(printf '%s' "$text" | head -c 160)）——合成 waiting_decision 靶线机制未落地，驻停不解除判据不可核（R0 预期红）"
        return 0
    fi
    if [ "${parked:-0}" = "0" ]; then
        vrb_emit 16 message-not-a-decision FAIL "合成靶线（$target）消息（$msg_id）被受理，但 state 面无该线 waiting_decision 驻停事实可对照（驻停不解除无从核，回执: $(printf '%s' "$text" | head -c 120)）"
    else
        vrb_emit 16 message-not-a-decision PASS "合成靶线（$target）驻停未解除且回执写明消息不是裁决（msg=$msg_id）"
    fi
}

# ---------------- 17 dispatch-gate-via-stop-response 派单与批 gate 走 Stop Response ----------------
vrb_check_17() {
    dispatch_seen=0
    gate_seen=0
    dd_calls=0
    n_rounds=0
    sample=""
    for f in "$VRB_RUNS_ROOT"/*/coord/rounds.jsonl; do
        [ -r "$f" ] || continue
        n_rounds=$(( n_rounds + 1 ))
        grep -q 'dd.dispatch.v1' "$f" 2>/dev/null && dispatch_seen=1
        grep -q 'dd.gate_release.v1' "$f" 2>/dev/null && gate_seen=1
        if grep -qE '"(development_gate|development_create|development_start)"|dd[-_]mcp' "$f" 2>/dev/null; then
            dd_calls=$(( dd_calls + 1 ))
        fi
        [ -z "$sample" ] && sample="$(grep -m1 'dd.dispatch.v1' "$f" 2>/dev/null | head -c 160)"
    done
    if [ "$n_rounds" = "0" ]; then
        vrb_emit 17 dispatch-gate-via-stop-response FAIL "无 rounds.jsonl 样本（$VRB_RUNS_ROOT/*/coord/ 下 0 个），派单/gate 轮 actions 无从核"
    elif [ "$dd_calls" -gt 0 ]; then
        vrb_emit 17 dispatch-gate-via-stop-response FAIL "线内发现 dd-mcp 入口调用记录（${dd_calls}/${n_rounds} 个 rounds.jsonl 命中 development_gate/create/start 或 dd-mcp），Stop Response 面未收口"
    elif [ "$dispatch_seen" = "0" ] || [ "$gate_seen" = "0" ]; then
        vrb_emit 17 dispatch-gate-via-stop-response FAIL "${n_rounds} 个 rounds.jsonl 中派单轮 dd.dispatch.v1 命中=${dispatch_seen}、gate 释放轮 dd.gate_release.v1 命中=${gate_seen}（样例: ${sample:-<无>}）——Stop Response action 化未上线"
    else
        vrb_emit 17 dispatch-gate-via-stop-response PASS "${n_rounds} 个 rounds.jsonl 均核：派单轮 actions 含 dd.dispatch.v1、gate 释放轮含 dd.gate_release.v1，线内无 dd-mcp 工具调用记录"
    fi
}

# ---------------- 18 disk-not-a-channel 磁盘不当信道 ----------------
vrb_check_18() {
    if [ ! -d "$VRB_CURRENT/src" ]; then
        vrb_emit 18 disk-not-a-channel FAIL "部署源码目录不可读: $VRB_CURRENT/src，调度器唤醒路径代码核无从做起"
        return 0
    fi
    # 机械口径：命中 terminal.json / .scheduler 且同一行存在「读文件内容当事件」的模式
    # （read_text / open( / json.load / json.loads / read_bytes / cat ），即调度器唤醒路径
    # 把盘面文件内容当 dd 终态事件消费的分支；纯路径字符串引用（如写指针文件）不计。
    hits="$(grep -rnE 'terminal\.json|\.scheduler' "$VRB_CURRENT/src" 2>/dev/null \
        | grep -E 'read_text|read_bytes|open\(|json\.load|json\.loads|cat ')"
    n_hits="$(printf '%s\n' "$hits" | grep -c .)"
    if [ "$n_hits" = "0" ]; then
        vrb_emit 18 disk-not-a-channel PASS "$VRB_CURRENT/src 调度器唤醒路径 0 处「读 terminal.json/.scheduler 内容当 dd 终态事件」分支（机械口径: 同行命中读内容模式）"
    else
        vrb_emit 18 disk-not-a-channel FAIL "$VRB_CURRENT/src 命中 ${n_hits} 处读盘面文件内容当事件的分支，样例: $(printf '%s' "$hits" | head -3 | tr '\n' ' ' | head -c 200)"
    fi
}

# ---------------- 19 graph-state-rebuildable 图状态可重建 ----------------
vrb_check_19() {
    testenv="$VRB_CURRENT/scripts/testenv.sh"
    if [ ! -x "$testenv" ]; then
        vrb_emit 19 graph-state-rebuildable FAIL "测试环境不可得：$testenv 不存在或不可执行（R1 交付物未落地），删 parked 线 checkpoint 库重建判据无环境可验（不变量四 R0 预期红）"
        return 0
    fi
    if ! grep -q 'rebuild' "$testenv" 2>/dev/null; then
        vrb_emit 19 graph-state-rebuildable FAIL "testenv 存在（$testenv）但无 rebuild 子命令/探针，删库重建判据仍不可验"
        return 0
    fi
    out="$(timeout 15 bash "$testenv" rebuild 2>&1)"
    rc=$?
    if [ "$rc" = "0" ] && printf '%s' "$out" | grep -qi 'rebuild.*ok\|重建'; then
        vrb_emit 19 graph-state-rebuildable PASS "testenv 删 checkpoint 重建探针通过: $(printf '%s' "$out" | tail -1 | head -c 160)"
    else
        vrb_emit 19 graph-state-rebuildable FAIL "testenv rebuild 探针失败（rc=$rc）: $(printf '%s' "$out" | tail -1 | head -c 160)"
    fi
}

# ---------------- 20 testenv-e2e 测试环境端到端 ----------------
vrb_check_20() {
    testenv="$VRB_CURRENT/scripts/testenv.sh"
    if [ ! -x "$testenv" ]; then
        vrb_emit 20 testenv-e2e FAIL "scripts/testenv.sh 不存在（$testenv，R1 交付物），R0 无测试环境端到端可验（目标架构页 Ⅴ 五步回显无从跑起）"
        return 0
    fi
    out="$(timeout 15 bash "$testenv" up 2>&1)"
    rc=$?
    steps=0
    for s in 入编 派单 gate 合并 验收; do
        printf '%s' "$out" | grep -q "$s" && steps=$(( steps + 1 ))
    done
    if [ "$rc" = "0" ] && [ "$steps" = "5" ]; then
        vrb_emit 20 testenv-e2e PASS "testenv up 五步回显齐（入编/派单/gate/合并/验收，rc=0）"
    else
        vrb_emit 20 testenv-e2e FAIL "testenv up rc=$rc，五步回显仅 ${steps}/5（入编/派单/gate/合并/验收）: $(printf '%s' "$out" | tail -1 | head -c 160)"
    fi
}

# ---------------- 21 deletion-list-assertions 删除清单存在性 ----------------
# §7.1 九项 + §7.2 十三项逐对象机械断言「确实没了」；探针全部走 VRB_* knob。
vrb_check_21() {
    s71_missing=0
    s72_missing=0
    survivors=""
    note_missing() {
        survivors="$survivors $1"
    }
    # —— 探针读数（一次性取齐，探针出错按该项「未确认=还在」计） ——
    units_agentbus="$("$VRB_SYSTEMCTL" --user list-units 'agent-bus-*' --plain --no-legend 2>/dev/null)"
    rc_units=$?
    unitfiles="$("$VRB_SYSTEMCTL" --user list-unit-files --plain --no-legend 2>/dev/null)"
    rc_uf=$?
    proto_ok=1
    if [ -n "$BUS_TOKEN" ]; then
        proto_body="$(curl -s --noproxy '*' -m 15 -H "Authorization: Bearer $BUS_TOKEN" "$VRB_BUS_BASE/v1/protocols" 2>/dev/null)"
        chan_body="$(curl -s --noproxy '*' -m 15 -H "Authorization: Bearer $BUS_TOKEN" "$VRB_BUS_BASE/v1/channels?limit=1000" 2>/dev/null)"
        printf '%s' "$proto_body" | jq -e . >/dev/null 2>&1 || proto_ok=0
        printf '%s' "$chan_body" | jq -e . >/dev/null 2>&1 || proto_ok=0
    else
        proto_ok=0
        proto_body=""
        chan_body=""
    fi
    dd_tools="$(mcp_tool_names "$VRB_MCP_DD")"
    goal_tools="$(mcp_tool_names "$VRB_MCP_GOAL")"
    bus_tools="$(mcp_tool_names "$VRB_MCP_BUS")"
    dec_tools="$(mcp_tool_names "$VRB_MCP_DECISION")"
    all_tools="$(printf '%s\n' "$dd_tools" "$goal_tools" "$bus_tools" "$dec_tools" | grep -v '^$')"
    lines_body="$(json_get "$VRB_STATE_BASE/v1/lines")"
    skill_ok=1
    [ -r "$VRB_SKILL_FILE" ] || skill_ok=0
    grep_files="$(persona_files_for_grep)"
    pid="$("$VRB_SYSTEMCTL" --user show fleet-graph-dd-mcp -p MainPID --value 2>/dev/null)"
    cmdline=""
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    fi

    # —— §7.1 九项 ——
    # 1. agent-bus 三试验实例
    if [ "$rc_units" -ne 0 ] || printf '%s\n' "$units_agentbus" | grep -qE 'agent-bus-(test|staging|autodev-test)'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.1 agent-bus-test/staging/autodev-test"
    fi
    # 2. wf-observe.service
    if [ "$rc_uf" -ne 0 ] || printf '%s\n' "$unitfiles" | grep -q 'wf-observe'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.2 wf-observe.service"
    fi
    # 3. 退役 unit 文件族
    if [ "$rc_uf" -ne 0 ] || printf '%s\n' "$unitfiles" | grep -qE '^(loop-engine-|loop-mcp\.|loop-mcp\.service|ronin-auto-gate|ronin-babysitter|ronin-pump-)'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.3 loop-engine-*/loop-mcp/ronin-auto-gate/ronin-babysitter/ronin-pump-*"
    fi
    # 4. 看板频道族
    if [ "$proto_ok" = "0" ] || printf '%s' "$chan_body" | grep -qE 'gd:e2e-gdrun-|chat:testroom|chatgroup:livetest-|coord:observability-successors-|board:dd-talk-staging-|board:agent-runtime-profile-schema-'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.4 测试看板频道族"
    fi
    # 5. 死协议族
    if [ "$proto_ok" = "0" ] || printf '%s' "$proto_body" | grep -qE 'coord\.|dd\.plan\.|coordination\.dispatch-request\.v1|probe\.reqtype\.v1|research\.smoke\.v1|agent\.run\.(started|exited)\.v[12]'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.5 死协议族(coord.*/dd.plan.*/probe.reqtype.v1/research.smoke.v1/agent.run.*)"
    fi
    # 6. ronin-mcp dev/gate 13 + pump 3 死工具
    if printf '%s\n' "$all_tools" | grep -qE '^ronin_(dev|gate|pump)_'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.6 ronin-mcp dev/gate/pump 死工具"
    fi
    # 7. dd-mcp 5 个 NOT_SUPPORTED 工具
    if [ -z "$dd_tools" ] || printf '%s\n' "$dd_tools" | grep -qE '^(development_steer|development_relock|development_control|deployment_create|deployment_status)$'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.7 dd-mcp NOT_SUPPORTED 五工具"
    fi
    # 8. dd-mcp unit --stage-model 覆盖键（unit 不在跑 = 覆盖键不复存在）
    if printf '%s' "$cmdline" | grep -q -- '--stage-model'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.8 dd-mcp --stage-model 覆盖键"
    fi
    # 9. Tempo
    if [ "$rc_uf" -ne 0 ] || printf '%s\n' "$unitfiles" | grep -qi 'tempo'; then
        s71_missing=$(( s71_missing + 1 )); note_missing "§7.1.9 Tempo"
    fi

    # —— §7.2 十三项 ——
    # 1. decision-bridge + goal.md 直写信道（supervise/e7_*）
    if [ "$rc_uf" -ne 0 ] || printf '%s\n' "$unitfiles" | grep -q 'decision-bridge' || printf '%s\n' "$all_tools" | grep -qE '^(e7_|supervise_)'; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.1 decision-bridge/e7_*"
    fi
    # 2. ronin-mcp 整个门面
    if [ "$rc_uf" -ne 0 ] || printf '%s\n' "$unitfiles" | grep -qE '^(ronin-mcp|loop-mcp)' || printf '%s\n' "$all_tools" | grep -q '^ronin_'; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.2 ronin-mcp 门面"
    fi
    # 3. work.card.v1 与 board:work-index
    if [ "$proto_ok" = "0" ] || printf '%s' "$proto_body" | grep -q 'work.card.v1' || printf '%s' "$chan_body" | grep -q 'board:work-index'; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.3 work.card.v1/board:work-index"
    fi
    # 4. dd/<dev>/status.json 与 /v1/lines.parked 字段
    if [ -n "$(find "$VRB_DD_ROOT" -maxdepth 2 -name 'status.json' 2>/dev/null | head -1)" ] \
        || ! printf '%s' "$lines_body" | jq -e '[.. | objects | select(has("parked"))] | length == 0' >/dev/null 2>&1; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.4 dd status.json//v1/lines.parked"
    fi
    # 5. fleet-l0.py + Monitor 唤醒路
    if [ -e "$VRB_CURRENT/scripts/fleet-l0.py" ] || printf '%s\n' "$unitfiles" | grep -qi 'fleet-l0'; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.5 fleet-l0.py+Monitor"
    fi
    # 6. :7494 作为调用面（skill/persona）
    if [ "$skill_ok" = "0" ] || grep -rEq ':7494' $grep_files 2>/dev/null; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.6 :7494 调用面"
    fi
    # 7. CLI line revive / set-seat / supervisor reset / fleet-maint.sh 调用面
    if [ "$skill_ok" = "0" ] || grep -rEq 'line revive|line set-seat|supervisor reset|fleet-maint' $grep_files 2>/dev/null; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.7 line revive/set-seat/supervisor reset/fleet-maint 调用面"
    fi
    # 8. /data/ronin 不再被引用 + alias token 新路径存在
    if grep -rq '/data/ronin' "$VRB_CURRENT/config" "$VRB_CURRENT/deploy" 2>/dev/null || [ ! -e "$VRB_SECRETS_DIR" ]; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.8 /data/ronin 引用或 token 新路径($VRB_SECRETS_DIR)"
    fi
    # 9. A2 arbiter timer
    if [ "$rc_uf" -ne 0 ] || printf '%s\n' "$unitfiles" | grep -qi 'arbiter'; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.9 A2 arbiter timer"
    fi
    # 10. 线的 dd 轮询（源内轮询分支）
    if [ ! -d "$VRB_CURRENT/src" ] || grep -rnEi 'poll[_-]dd|dd[_-]poll|polling.{0,24}development_(list|get)|development_(list|get).{0,24}poll' "$VRB_CURRENT/src" >/dev/null 2>&1; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.10 线 dd 轮询分支"
    fi
    # 11. goal.md 直写捎话 + line set-seat CLI
    if [ "$skill_ok" = "0" ] || grep -rEiq 'goal\.md.{0,12}(直写|append|>>)|line set-seat' $grep_files 2>/dev/null; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.11 goal.md 直写捎话/line set-seat"
    fi
    # 12. 监督面待办 T-2b / T-2c
    if [ "$skill_ok" = "0" ] || grep -rEq 'T-2b|T-2c' $grep_files 2>/dev/null; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.12 T-2b/T-2c"
    fi
    # 13. 监督面逐单批闸 SOP
    if [ "$skill_ok" = "0" ] || grep -rEq '逐单批闸|手工收割' $grep_files 2>/dev/null; then
        s72_missing=$(( s72_missing + 1 )); note_missing "§7.2.13 逐单批闸 SOP"
    fi

    gone71=$(( 9 - s71_missing ))
    gone72=$(( 13 - s72_missing ))
    if [ "$s71_missing" = "0" ] && [ "$s72_missing" = "0" ]; then
        vrb_emit 21 deletion-list-assertions PASS "§7.1 gone=9/9 §7.2 gone=13/13（探针: systemctl list-units/list-unit-files、bus protocols/channels、四 MCP tools/list、state /v1/lines、skill/persona grep、$VRB_CURRENT 源码 grep）"
    else
        vrb_emit 21 deletion-list-assertions FAIL "§7.1 gone=${gone71}/9 §7.2 gone=${gone72}/13，仍在对象（探针出错或对象存在，样例）:${survivors}（明细: systemctl rc_units=$rc_units rc_uf=$rc_uf，bus 可核=$proto_ok，dd-mcp tools 可核=$([ -n "$dd_tools" ] && echo 1 || echo 0)，skill 可读=$skill_ok）"
    fi
}

# ---------------- 主循环（01–21 顺序，needs_check 过滤） ----------------
for nn in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21; do
    if needs_check "$nn"; then
        "vrb_check_$nn"
    fi
done

cat "$LOG"

fail=$(grep -cE '^[0-9]{2} [a-z0-9-]+ FAIL' "$LOG")
if [ -z "$ONLY_CHECK" ]; then
    pass=$(grep -cE '^[0-9]{2} [a-z0-9-]+ PASS' "$LOG")
    printf 'TOTAL pass=%d fail=%d\n' "$pass" "$fail"
fi
exit "$fail"

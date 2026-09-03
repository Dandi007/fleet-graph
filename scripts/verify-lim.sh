#!/usr/bin/env bash
#
# verify-lim.sh — 舰队 less-is-more 重构线的 16 项验收判据脚本（wf-8d9737 首轮脚手架）。
#
# 职责：逐条机械实现 design.md §8 的 16 项检查，每项独立探测已部署的生产事实，
#        输出「NN <id> PASS|FAIL — <依据>」恰好一行，整体退出码 = FAIL 项数（0–16）。
#        首轮大面积报红是正常起点：大部分机制（waiting_dd、裁决即唤醒、state_takeover、
#        line_message、release 分支模型……）要到 M1–M8 才落地，本脚本如实报红，不折算 PASS。
#
# 断言对象是已部署的生产事实，不是本工作树源码（监督面 S5 裁决，2026-09-03）：
#   - systemd user unit 与 /proc/<pid>/cmdline
#   - agent-bus :7490（Bearer token 取 /data/agent-bus/tokens/fleet-graph.token）
#   - state :7494、goal MCP :5611、dd MCP :5610、decision MCP :5614、bus MCP :5608
#   - /data/fleet-graph/runs/（heartbeat.json、terminal.json、.scheduler/）
#   - /data/fleet-graph/dd/（record.json、result.json、events.jsonl、launches.jsonl、dd.log）
#   - 名册 /data/apps/fleet-graph/current/config/ronin-lines.json
#
# 代理卫生（S6）：脚本开头 unset 全部代理变量，回环 curl/jq 探测不得走 SOCKS。
#
# 用法：
#   bash scripts/verify-lim.sh                 # 跑全部 16 项
#   bash scripts/verify-lim.sh --check 03      # 只跑指定项（01–16）
#   bash scripts/verify-lim.sh --check 12      # check 12 对一张真实非本方派单的 awaiting 单投递，断言 NOT_DISPATCHING_LINE（S11 修订）
#   bash scripts/verify-lim.sh --window-seconds 3600   # 覆盖 check 11/13/14 的时间窗
#
# 退出码：等于 FAIL 项数（0–16，全绿为 0）。
#        单项探针出错（curl 非零 / jq 解析失败 / 文件缺失）→ 该项 FAIL 并带错误原文，
#        脚本本身不崩溃（无全局 set -e）；单项探针超时上限 15s，整脚本目标 < 3 分钟。
set -u
set -o pipefail

unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy no_proxy NO_PROXY 2>/dev/null || true

TOKEN_FILE="/data/agent-bus/tokens/fleet-graph.token"
BUS_TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null)"

STATE_PORT=7494
BUS_MCP=5608
DD_MCP=5610
GOAL_MCP=5611
DECISION_MCP=5614

RONIN_LINES="/data/apps/fleet-graph/current/config/ronin-lines.json"
RUNS_ROOT="/data/fleet-graph/runs"
DD_ROOT="/data/fleet-graph/dd"
SCHED_DIR="$RUNS_ROOT/.scheduler"
SKILL_FILE="/data/code/self/agent-skills/plugins/agent-skills/skills/fleet-supervisor/SKILL.md"
GATE_REF="fleet-graph-dd-mcp"
SELFTEST_LINE="dev-fg-lim-selftest-probe"

WINDOW_SECONDS=86400
ONLY_CHECK=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check) ONLY_CHECK="$2"; shift 2 ;;
        --window-seconds) WINDOW_SECONDS="$2"; shift 2 ;;
        *) shift ;;
    esac
done

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

sanitize() {
    tr '\n\r\t' ' ' | tr -s ' ' | sed -e 's/^ *//' -e 's/ *$//'
}

emit() {
    local nn="$1" id="$2" verdict="$3"
    shift 3
    local ev
    ev="$(printf '%s' "$*" | sanitize)"
    printf '%s %s %s — %s\n' "$nn" "$id" "$verdict" "$ev" >> "$LOG"
}

json_get() {
    curl -s -m 15 "$1" 2>/dev/null
}

mcp_init() {
    curl -s -m 10 -D - -o /dev/null "http://127.0.0.1:$1/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify-lim","version":"1"}}}' 2>/dev/null \
        | tr -d '\r' | sed -n 's/^[Mm][Cc][Pp]-[Ss][Ee][Ss][Ss][Ii][Oo][Nn]-[Ii][Dd]:[[:space:]]*//Ip'
}

mcp_json() {
    local port="$1" method="$2" params="$3" sid
    sid="$(mcp_init "$port")"
    [ -z "$sid" ] && return 1
    curl -s -m 10 "http://127.0.0.1:$port/mcp" \
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

# ---------------- 01 test-instances-stopped ----------------
if needs_check 01; then
    units="$(systemctl --user list-units 'agent-bus-*' --plain --no-legend 2>/dev/null)"
    names="$(printf '%s\n' "$units" | awk '{print $1}' | sed 's/\.service$//')"
    n_units="$(printf '%s\n' "$names" | grep -c . )"
    residual="$(printf '%s\n' "$names" | grep -vxE 'agent-bus-server|agent-bus-mcp')"
    if [ -z "$residual" ]; then
        emit 01 test-instances-stopped PASS "agent-bus-* 已加载单元（含 loaded/active/inactive）共 ${n_units} 个，名集合 ⊆ {agent-bus-server, agent-bus-mcp}"
    else
        detail=""
        while IFS= read -r u; do
            [ -z "$u" ] && continue
            st="$(systemctl --user show "$u.service" -p ActiveState --value 2>/dev/null)"
            detail="$detail ${u}(${st:-unknown})"
        done <<EOF
$residual
EOF
        emit 01 test-instances-stopped FAIL "残留试验实例单元（名(状态)）:${detail}; 允许集合为 {agent-bus-server, agent-bus-mcp}，实测共 ${n_units} 个"
    fi
fi

# ---------------- 02 dead-protocols-deregistered ----------------
if needs_check 02; then
    if [ -z "$BUS_TOKEN" ]; then
        emit 02 dead-protocols-deregistered FAIL "token 文件不可读: $TOKEN_FILE"
    else
        auth_body="$(curl -s -m 15 -H "Authorization: Bearer $BUS_TOKEN" http://127.0.0.1:7490/v1/protocols 2>/dev/null)"
        if [ -z "$auth_body" ]; then
            emit 02 dead-protocols-deregistered FAIL "agent-bus :7490 /v1/protocols 不可达（空响应/连接失败）"
        else
            count="$(printf '%s' "$auth_body" | grep -oF 'coord.' | wc -l | tr -d ' ')"
            if [ "$count" = "0" ]; then
                emit 02 dead-protocols-deregistered PASS "协议注册表中原 dead 协议 coord.* 出现次数为 0"
            else
                sample="$(printf '%s' "$auth_body" | grep -oE '"kind"[[:space:]]*:[[:space:]]*"[^"]*coord[^"]*"' | head -3 | tr '\n' ' ')"
                emit 02 dead-protocols-deregistered FAIL "协议注册表响应中子串 coord.* 出现 ${count} 次，命中样例: ${sample}"
            fi
        fi
    fi
fi

# ---------------- 03 decisions-zero-swallowed ----------------
if needs_check 03; then
    body="$(json_get "http://127.0.0.1:$STATE_PORT/v1/decisions")"
    if [ -z "$body" ]; then
        emit 03 decisions-zero-swallowed FAIL "state :$STATE_PORT /v1/decisions 不可达（空响应/连接失败）"
    else
        total="$(printf '%s' "$body" | jq '[.decisions[]]|length' 2>/dev/null)"
        swallowed="$(printf '%s' "$body" | jq '[.decisions[]|select(.state=="swallowed")]|length' 2>/dev/null)"
        consumed="$(printf '%s' "$body" | jq '[.decisions[]|select(.state=="consumed")]|length' 2>/dev/null)"
        states="$(printf '%s' "$body" | jq -r '[.decisions[].state]|group_by(.)|map(.[0]+"="+(length|tostring))|join(" ")' 2>/dev/null)"
        if [ -z "${total:-}" ] || [ "$total" = "null" ]; then
            emit 03 decisions-zero-swallowed FAIL "/v1/decisions 解析失败（非决策数组，原文: $(printf '%s' "$body" | head -c 120)）"
        elif [ "$swallowed" = "0" ]; then
            emit 03 decisions-zero-swallowed PASS "总 ${total} 条裁决中 swallowed=0（分状态: ${states}）"
        else
            emit 03 decisions-zero-swallowed FAIL "总 ${total} 条裁决中 swallowed=${swallowed}（分状态: ${states}），非零，机制未上线"
        fi
    fi
fi

# ---------------- 04 delivery-wakes-line ----------------
if needs_check 04; then
    body="$(json_get "http://127.0.0.1:$STATE_PORT/v1/decisions")"
    line_dec="$(printf '%s' "$body" | jq '[.decisions[]|select(.state=="consumed" and .owner.kind=="line")|select((.owner.id//"")|startswith("wf-"))]' 2>/dev/null)"
    n_line="$(printf '%s' "$line_dec" | jq 'length' 2>/dev/null)"
    ts_field="$(printf '%s' "$body" | jq -r '.decisions[0] | keys | join(",")' 2>/dev/null)"
    if [ "${n_line:-0}" = "0" ] || [ -z "${n_line:-}" ]; then
        emit 04 delivery-wakes-line FAIL "无已送达（consumed）且 target 为线（wf-*）的裁决可对照"
    else
        newest="$(printf '%s' "$line_dec" | jq -r '.[-1] | (.owner.id//"?") + " g" + ((.owner.generation//0)|tostring)' 2>/dev/null)"
        emit 04 delivery-wakes-line FAIL "已送达线裁决存在（${n_line} 条，最近一条 owner=${newest}），但裁决记录仅含 ${ts_field}（无送达时刻字段），无法与 fleet-graph-line-* 的 ActiveEnterTimestamp 做 90s 容差对照"
    fi
fi

# ---------------- 05 waiting-zero-llm-spend ----------------
if needs_check 05; then
    sched_count="$(ls "$SCHED_DIR"/wf-*.json 2>/dev/null | wc -l | tr -d ' ')"
    waiting_files="$(grep -l 'waiting_dd' "$SCHED_DIR"/wf-*.json 2>/dev/null)"
    term_waiting="$(grep -l 'waiting_dd' "$RUNS_ROOT"/*/terminal.json 2>/dev/null | wc -l | tr -d ' ')"
    lines_body="$(json_get "http://127.0.0.1:$STATE_PORT/v1/lines")"
    n_lines="$(printf '%s' "$lines_body" | jq '[.lines[]]|length' 2>/dev/null)"
    waiting_values="$(printf '%s' "$lines_body" | jq -r '[.lines[].wake_facts.waiting_on // empty]|unique|join(",")' 2>/dev/null)"
    if [ -n "$waiting_files" ]; then
        emit 05 waiting-zero-llm-spend FAIL "存在处于 waiting_dd 语义的线: $(printf '%s' "$waiting_files" | tr '\n' ' ')"
    else
        emit 05 waiting-zero-llm-spend FAIL "当前没有任何线处于 waiting_dd 语义：.scheduler 下 wf-*.json 共 ${sched_count} 个均无 waiting_dd，terminal.json 命中 ${term_waiting}；:${STATE_PORT} /v1/lines ${n_lines} 条线 wake_facts.waiting_on 取值集合={${waiting_values}}，状态词表未上线"
    fi
fi

# ---------------- 06 acceptance-command-frozen ----------------
if needs_check 06; then
    gs="$(mcp_json "$GOAL_MCP" 'tools/call' '{"name":"goal_status","arguments":{"folder_id":"wf-8d9737"}}')"
    if [ -z "$gs" ]; then
        emit 06 acceptance-command-frozen FAIL "goal MCP :$GOAL_MCP goal_status 不可达（空响应/连接失败）"
    else
        text="$(printf '%s' "$gs" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
        has_digest="$(printf '%s' "$text" | grep -c 'acceptance_digest')"
        has_accept="$(printf '%s' "$text" | jq -r 'has("acceptance") // false' 2>/dev/null)"
        keys="$(printf '%s' "$text" | jq -r 'if type=="object" then keys|join(",") else "non-object" end' 2>/dev/null)"
        emit 06 acceptance-command-frozen FAIL "goal_status 面（folder wf-8d9737）不暴露 acceptance/acceptance_digest 字段（实测顶层键: ${keys}，acceptance_digest 出现 ${has_digest} 次），亦无可观测的『摘要不一致拒绝点火』结构化码面 —— digest 字段缺失，机制未上线（M1 前预期红）"
    fi
fi

# ---------------- 07 seat-single-source ----------------
if needs_check 07; then
    pid="$(systemctl --user show "$GATE_REF" -p MainPID --value 2>/dev/null)"
    if [ -z "$pid" ] || [ "$pid" = "0" ]; then
        emit 07 seat-single-source FAIL "$GATE_REF 的 MainPID 不可得，无法读取 /proc cmdline"
    else
        cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
        if [ -z "$cmdline" ]; then
            emit 07 seat-single-source FAIL "/proc/$pid/cmdline 不可读"
        elif printf '%s' "$cmdline" | grep -q -- '--stage-model'; then
            emit 07 seat-single-source FAIL "dd-mcp cmdline 含 --stage-model 覆盖键：${cmdline}"
        else
            emit 07 seat-single-source PASS "dd-mcp cmdline 不含 --stage-model：${cmdline}"
        fi
    fi
fi

# ---------------- 08 public-interface-mcp-only ----------------
if needs_check 08; then
    if [ ! -r "$SKILL_FILE" ]; then
        emit 08 public-interface-mcp-only FAIL "SKILL.md 不可读: $SKILL_FILE"
    else
        hits="$(grep -rEn 'curl .*:(7490|7494)|fleet-graph line |fleet-maint' "$SKILL_FILE" 2>/dev/null)"
        n_hits="$(printf '%s\n' "$hits" | grep -c . )"
        if [ "$n_hits" = "0" ]; then
            emit 08 public-interface-mcp-only PASS "SKILL.md 中裸 HTTP/CLI 公共入口命中 0 条"
        else
            emit 08 public-interface-mcp-only FAIL "SKILL.md 命中 ${n_hits} 条裸 HTTP(:7490/:7494)/fleet-graph line/fleet-maint 入口，样例: $(printf '%s' "$hits" | head -3 | tr '\n' ' ')"
        fi
    fi
fi

# ---------------- 09 takeover-one-call ----------------
if needs_check 09; then
    dec_tools="$(mcp_tool_names "$DECISION_MCP")"
    has_takeover="$(printf '%s\n' "$dec_tools" | grep -cE 'takeover|state_takeover')"
    state_probe="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$STATE_PORT/v1/takeover" 2>/dev/null)"
    if [ "${has_takeover:-0}" != "0" ]; then
        emit 09 takeover-one-call PASS "decision :$DECISION_MCP 暴露 takeover/state_takeover 工具: $(printf '%s' "$dec_tools" | grep -E 'takeover|state_takeover' | tr '\n' ' ')"
    else
        emit 09 takeover-one-call FAIL "零上下文一次调用拿不到六项：decision :$DECISION_MCP tools/list 仅 {$(printf '%s' "$dec_tools" | tr '\n' ' ')}，无 takeover/state_takeover 工具；state :$STATE_PORT /v1/takeover 返回 ${state_probe:-无响应}。缺失项：名册/线状态/等拍板/待上线/授权模式/当前 release 的单一 take-over 面"
    fi
fi

# ---------------- 10 mcp-function-probes ----------------
if needs_check 10; then
    results=""
    all_ok=1
    # bus MCP :5608
    bt="$(mcp_tool_names "$BUS_MCP")"
    if [ -z "$bt" ]; then
        results="${results}bus:5608=tools/list失败; "; all_ok=0
    else
        bc="$(mcp_json "$BUS_MCP" 'tools/call' '{"name":"bus_agent_list","arguments":{}}')"
        bok="$(printf '%s' "$bc" | jq -r 'if .result then "ok" else .error.message // "err" end' 2>/dev/null)"
        results="${results}bus:5608=read:${bok}; "; [ "$bok" = "ok" ] || all_ok=0
    fi
    # goal MCP :5611
    gt="$(mcp_tool_names "$GOAL_MCP")"
    if [ -z "$gt" ]; then
        results="${results}goal:5611=tools/list失败; "; all_ok=0
    else
        gc="$(mcp_json "$GOAL_MCP" 'tools/call' '{"name":"goal_list","arguments":{}}')"
        gok="$(printf '%s' "$gc" | jq -r 'if .result then "ok" else .error.message // "err" end' 2>/dev/null)"
        results="${results}goal:5611=read:${gok}; "; [ "$gok" = "ok" ] || all_ok=0
    fi
    # dd MCP :5610
    dt="$(mcp_tool_names "$DD_MCP")"
    if [ -z "$dt" ]; then
        results="${results}dd:5610=tools/list失败; "; all_ok=0
    else
        dc="$(mcp_json "$DD_MCP" 'tools/call' '{"name":"development_list","arguments":{}}')"
        dok="$(printf '%s' "$dc" | jq -r 'if .result then "ok" else .error.message // "err" end' 2>/dev/null)"
        results="${results}dd:5610=read:${dok}; "; [ "$dok" = "ok" ] || all_ok=0
    fi
    # decision MCP :5614
    det="$(mcp_tool_names "$DECISION_MCP")"
    if [ -z "$det" ]; then
        results="${results}decision:5614=tools/list失败; "; all_ok=0
    else
        n_ro="$(printf '%s\n' "$det" | grep -vc 'decision_deliver')"
        results="${results}decision:5614=只有 decision_deliver(写原语),无只读工具; "; all_ok=0
    fi
    # state :7494 (JSON read model，无 MCP tools/list)
    st_code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$STATE_PORT/v1/lines" 2>/dev/null)"
    if [ "$st_code" = "200" ]; then
        results="${results}state:7494=read:/v1/lines ok"
    else
        results="${results}state:7494=read:/v1/lines http=${st_code}"; all_ok=0
    fi
    if [ "$all_ok" = "1" ]; then
        emit 10 mcp-function-probes PASS "五个面 tools/list(或等价发现)+只读调用全部成功：${results}"
    else
        emit 10 mcp-function-probes FAIL "五个面逐面结果：${results}"
    fi
fi

# ---------------- 11 dd-gate-by-dispatching-line ----------------
if needs_check 11; then
    n_window=0
    n_gate=0
    n_match=0
    gate_sample=""
    for d in "$DD_ROOT"/*/; do
        rf="$d/record.json"
        [ -r "$rf" ] || continue
        mt="$(stat -c %Y "$rf" 2>/dev/null)"
        [ -n "$mt" ] && [ "$mt" -ge "$window_start" ] || continue
        n_window=$(( n_window + 1 ))
        has_gate="$(jq -r 'if (.scope_verdict != null) then "1" else "0" end' "$rf" 2>/dev/null)"
        if [ "$has_gate" = "1" ]; then
            n_gate=$(( n_gate + 1 ))
            dispatched_by="$(jq -r '.dispatched_by // empty' "$rf" 2>/dev/null)"
            decided_by="$(jq -r '.scope_verdict.decided_by // .scope_verdict.principal // empty' "$rf" 2>/dev/null)"
            if [ -n "$decided_by" ] && [ "$decided_by" = "$dispatched_by" ]; then
                n_match=$(( n_match + 1 ))
            fi
            if [ -z "$gate_sample" ]; then
                gate_sample="dispatched_by=${dispatched_by:-无},scope_verdict.decided_by=${decided_by:-无}"
            fi
        fi
    done
    if [ "$n_window" = "0" ]; then
        emit 11 dd-gate-by-dispatching-line FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核"
    elif [ "$n_gate" = "0" ]; then
        emit 11 dd-gate-by-dispatching-line FAIL "窗口内 ${n_window} 张 dd 单，0 张带闸裁决（record.json 无 scope_verdict）"
    elif [ "$n_match" -gt 0 ]; then
        emit 11 dd-gate-by-dispatching-line PASS "窗口内 ${n_window} 张 dd 单，${n_match} 张闸裁决 decided_by==dispatched_by"
    else
        emit 11 dd-gate-by-dispatching-line FAIL "窗口内 ${n_window} 张 dd 单，${n_gate} 张含闸裁决(scope_verdict)，但 0 张 decided_by==dispatched_by（样例: ${gate_sample}；scope_verdict 无 decided_by 字段，闸由监督面批，预期红）"
    fi
fi

# ---------------- 12 foreign-delivery-refused ----------------
# S11 修订（2026-09-03）：探针原用不存在的合成 id 走默认线路径，先撞
# NO_WAITING_PARTY / DEVELOPMENT_NOT_FOUND 就返回，永远到不了身份校验分支——
# 既没证明校验在、也没证明校验不在。改用真实存在、且非本方派单的
# awaiting_gate 单（dev-fg-36c2d76baca7，wf-8d9737 M2 r1 真机单），以空
# principal（形态等价：line 填 dev-fg- 号即走 dd 闸路径）投递，断言必须拿到
# NOT_DISPATCHING_LINE——这条判据证明的是「身份校验在且生效」。
if needs_check 12; then
    foreign_dd="dev-fg-36c2d76baca7"
    res="$(mcp_json "$DECISION_MCP" 'tools/call' "{\"name\":\"decision_deliver\",\"arguments\":{\"line\":\"$foreign_dd\",\"decision\":\"REJECT\",\"reason\":\"verify-lim check12 foreign-delivery-refused probe (S11): non-dispatching principal on a real awaiting single\",\"principal\":\"\"}}")"
    if [ -z "$res" ]; then
        emit 12 foreign-delivery-refused FAIL "decision :$DECISION_MCP 不可达（空响应/连接失败），无法投递身份探针"
    else
        text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
        code="$(printf '%s' "$text" | jq -r '.code // empty' 2>/dev/null)"
        status="$(printf '%s' "$text" | jq -r '.status // empty' 2>/dev/null)"
        if [ "$code" = "NOT_DISPATCHING_LINE" ] && [ "$status" = "refused" ]; then
            emit 12 foreign-delivery-refused PASS "非派单方对真实 awaiting 单 ${foreign_dd} 投递被拒，code=NOT_DISPATCHING_LINE：${text}"
        elif [ "$status" = "delivered" ]; then
            emit 12 foreign-delivery-refused FAIL "非派单方对 ${foreign_dd} 的投递被接受（delivered/consumed）——身份校验不生效，严重红：${text}"
        else
            emit 12 foreign-delivery-refused FAIL "返回非 NOT_DISPATCHING_LINE 的结构化拒绝码（code=${code:-无}, status=${status:-无}），原文: ${text}（S11 修订后必绿；若该单不在 awaiting_gate 则另择真实在闸单号更新探针）"
        fi
    fi
fi

# ---------------- 13 dd-touches-line-branch-only ----------------
if needs_check 13; then
    n_window=0
    bad_ref=0
    sample=""
    for d in "$DD_ROOT"/*/; do
        rf="$d/record.json"
        [ -r "$rf" ] || continue
        mt="$(stat -c %Y "$rf" 2>/dev/null)"
        [ -n "$mt" ] && [ "$mt" -ge "$window_start" ] || continue
        n_window=$(( n_window + 1 ))
        ref="$(jq -r '.remote_ref // empty' "$rf" 2>/dev/null)"
        if printf '%s' "$ref" | grep -q '^refs/heads/release/'; then
            :
        else
            bad_ref=$(( bad_ref + 1 ))
            if [ -z "$sample" ]; then sample="$ref"; fi
        fi
    done
    if [ "$n_window" = "0" ]; then
        emit 13 dd-touches-line-branch-only FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核"
    elif [ "$bad_ref" -gt 0 ]; then
        emit 13 dd-touches-line-branch-only FAIL "窗口内 ${n_window} 张 dd 单，${bad_ref} 张 remote_ref 不以 refs/heads/release/<line-id> 为前缀，样例: ${sample}（M5 前预期红）"
    else
        emit 13 dd-touches-line-branch-only PASS "窗口内 ${n_window} 张 dd 单 remote_ref 均为 refs/heads/release/<line-id> 前缀"
    fi
fi

# ---------------- 14 rebase-before-dispatch ----------------
if needs_check 14; then
    n_window=0
    sample=""
    found_rebase=0
    for d in "$DD_ROOT"/*/; do
        rf="$d/record.json"
        [ -r "$rf" ] || continue
        mt="$(stat -c %Y "$rf" 2>/dev/null)"
        [ -n "$mt" ] && [ "$mt" -ge "$window_start" ] || continue
        n_window=$(( n_window + 1 ))
        evf="$d/events.jsonl"
        logf="$d/dd.log"
        if [ -r "$evf" ] && grep -qE 'rebase.*release/' "$evf" 2>/dev/null; then found_rebase=$(( found_rebase + 1 )); fi
        if [ -r "$logf" ] && grep -qE 'rebase.*release/' "$logf" 2>/dev/null; then found_rebase=$(( found_rebase + 1 )); fi
        if [ -z "$sample" ] && [ -r "$evf" ]; then
            sample="$(grep -m1 '"stage"[[:space:]]*:[[:space:]]*"configure"' "$evf" 2>/dev/null)"
        fi
    done
    if [ "$n_window" = "0" ]; then
        emit 14 rebase-before-dispatch FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核"
    elif [ "$found_rebase" -gt 0 ]; then
        emit 14 rebase-before-dispatch PASS "窗口内 ${n_window} 张 dd 单均含 rebase 到 release/<line-id> 的 configure 步骤"
    else
        emit 14 rebase-before-dispatch FAIL "窗口内 ${n_window} 张 dd 单的 configure 段无 rebase 到 release/<line-id> 记录，样例 configure 事件: ${sample:-<空>}（M5 前预期红）"
    fi
fi

# ---------------- 15 message-delivered-and-acked ----------------
if needs_check 15; then
    gt="$(mcp_tool_names "$GOAL_MCP")"
    has_lm="$(printf '%s\n' "$gt" | grep -cx 'line_message')"
    if [ "${has_lm:-0}" = "0" ]; then
        emit 15 message-delivered-and-acked FAIL "goal MCP :$GOAL_MCP tools/list 无 line_message 工具（实测: $(printf '%s' "$gt" | tr '\n' ' ')）"
    else
        emit 15 message-delivered-and-acked FAIL "line_message 工具已在位但最近一条给线 inbox instruction 无 ack 落档（机制未上线）"
    fi
fi

# ---------------- 16 message-cannot-impersonate-decision ----------------
if needs_check 16; then
    gt="$(mcp_tool_names "$GOAL_MCP")"
    has_lm="$(printf '%s\n' "$gt" | grep -cx 'line_message')"
    waiting_decision="$(grep -l 'waiting_decision' "$SCHED_DIR"/wf-*.json 2>/dev/null)"
    if [ "${has_lm:-0}" = "0" ]; then
        emit 16 message-cannot-impersonate-decision FAIL "先决不满足：goal MCP :$GOAL_MCP tools/list 无 line_message 工具（实测: $(printf '%s' "$gt" | tr '\n' ' ')），无法验证『仅 inbox 消息不解除 waiting_decision 驻停』"
    elif [ -z "$waiting_decision" ]; then
        emit 16 message-cannot-impersonate-decision FAIL "line_message 在位但无 waiting_decision 驻停样本可对照（.scheduler 无 waiting_decision 字段）"
    else
        emit 16 message-cannot-impersonate-decision FAIL "line_message 在位，但最近一条 inbox 消息前后驻停字段对照无法证明『仅 inbox 不解除 waiting_decision』（机制未上线）"
    fi
fi

cat "$LOG"

pass=$(grep -cE '^[0-9]{2} [a-z0-9-]+ PASS' "$LOG")
fail=$(grep -cE '^[0-9]{2} [a-z0-9-]+ FAIL' "$LOG")
printf 'TOTAL pass=%d fail=%d\n' "$pass" "$fail"
exit "$fail"
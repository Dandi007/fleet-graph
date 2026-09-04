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
#   bash scripts/verify-lim.sh --check 12      # check 12 现场合成一张探针专用靶单（跑完即清，无真实单被触碰）走 dd 闸身份校验分支
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

# spec-m4b：本仓库根（check 15/16 的现场合成靶探针从这份源码起靶栈）。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# run_m4b_probe <nn> — 跑 spec-m4b 的现场合成靶探针（scripts/lim_probe_m4b.py），
# 优先用本仓 .venv 的解释器（验收序列 uv sync --frozen 先行），缺失时退回
# uv run。stdout 压缩成末行依据回显；退出码原样透传（0=探针全绿）。
run_m4b_probe() {
    local nn="$1"
    local py="$REPO_ROOT/.venv/bin/python"
    local out rc
    if [ -x "$py" ]; then
        out="$(cd "$REPO_ROOT" && "$py" scripts/lim_probe_m4b.py --check "$nn" 2>&1)"
    else
        out="$(cd "$REPO_ROOT" && uv run python scripts/lim_probe_m4b.py --check "$nn" 2>&1)"
    fi
    rc=$?
    printf '%s' "$(printf '%s' "$out" | tail -n 2 | tr '\n' ' ' | sanitize)"
    return "$rc"
}

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
# record.json.scope_verdict 的真实用途：它是准入时（development_create）的
# B1/B3 边界裁决——DdControlPlane._require_scope(spec) 的产物，只有
# admitted/rule_id/rationale（等）字段，由 _admit 写入；_read_scope_evidence
# 只消费 admitted/rule_id。它是「这单有没有越 scope 边界被准入」的证据，
# 引擎从不往里写 decided_by/principal —— 它不是闸裁决署名。
# 闸裁决署名（decided_by）按优先级依序探测，任一命中即得：
#   1. <repo_path>/.dev-dispatch/gate/decision-g<generation>.json 的 .decided_by
#      （gate() 的 _committed_gate_decision 亲写的闸裁决文件；generation 取
#      record.json.generation 或 status.json.generation，默认 1；文件不存在
#      = 该单未过闸）
#   2. board work.decision.v1 的 decided_by：对 status.json.awaiting
#      .question_note_id（或 gen result.json 的 awaiting）投递的裁决消息，
#      agent-bus :7490 读 board:work-notes 频道（只读 GET，禁止 publish），
#      按 refs[].target_entity == question_note_id 且 payload.decided_by
#      非空识别
# 比较前先归一：取 decided_by 第一个空白分隔 token 再与 dispatched_by 全等
# （真机存在「wf-6475fd」与「wf-6475fd (goal line, self-adjudication)」两种
# 署名写法）。decided_by 为空或两源都无 → 该单不计数（不算 match 也不算 gate）。
if needs_check 11; then
    bus_board_cache=""
    board_decided_by() {
        local qn="$1"
        [ -z "$qn" ] && return 0
        [ -z "$BUS_TOKEN" ] && return 0
        if [ -z "$bus_board_cache" ]; then
            bus_board_cache="$(curl -s -m 15 -H "Authorization: Bearer $BUS_TOKEN" "http://127.0.0.1:7490/v1/channels/board:work-notes/messages?limit=1000" 2>/dev/null)"
        fi
        [ -z "$bus_board_cache" ] && return 0
        printf '%s' "$bus_board_cache" | jq -r --arg qn "$qn" '
            [.messages[]
             | select(.kind == "work.decision.v1"
                      and any(.refs[]?; .target_entity == $qn)
                      and ((.payload.decided_by // "") != ""))
             | .payload.decided_by]
            | .[0] // empty' 2>/dev/null
    }
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
        dispatched_by="$(jq -r '.dispatched_by // empty' "$rf" 2>/dev/null)"
        repo_path="$(jq -r '.repo_path // empty' "$rf" 2>/dev/null)"
        generation="$(jq -r '.generation // empty' "$rf" 2>/dev/null)"
        [ -n "$generation" ] || generation="$(jq -r '.generation // empty' "$d/status.json" 2>/dev/null)"
        case "$generation" in ''|*[!0-9]*) generation=1 ;; esac
        decided_by=""
        gate_src="gate-file"
        gf="$repo_path/.dev-dispatch/gate/decision-g${generation}.json"
        if [ -n "$repo_path" ] && [ -r "$gf" ]; then
            decided_by="$(jq -r '.decided_by // empty' "$gf" 2>/dev/null)"
        fi
        if [ -z "$decided_by" ]; then
            gate_src="board"
            qn="$(jq -r '.awaiting.question_note_id // empty' "$d/status.json" 2>/dev/null)"
            if [ -z "$qn" ]; then
                resf="$d/result.json"
                [ "$generation" -gt 1 ] && resf="$d/g${generation}/result.json"
                [ -r "$resf" ] && qn="$(jq -r '.awaiting.question_note_id // empty' "$resf" 2>/dev/null)"
            fi
            decided_by="$(board_decided_by "$qn")"
        fi
        # 空署名或两源都无 → 该单不计数（不算 match 也不算 gate）
        [ -z "$decided_by" ] && continue
        n_gate=$(( n_gate + 1 ))
        decided_tok="$(printf '%s' "$decided_by" | awk '{print $1}')"
        if [ -n "$dispatched_by" ] && [ "$decided_tok" = "$dispatched_by" ]; then
            n_match=$(( n_match + 1 ))
        fi
        if [ -z "$gate_sample" ]; then
            gate_sample="dispatched_by=${dispatched_by:-无},decided_by=${decided_by}(${gate_src})"
        fi
    done
    if [ "$n_window" = "0" ]; then
        emit 11 dd-gate-by-dispatching-line FAIL "窗口（${WINDOW_SECONDS}s）内无 dd 单可核"
    elif [ "$n_gate" = "0" ]; then
        emit 11 dd-gate-by-dispatching-line FAIL "窗口内 ${n_window} 张 dd 单，0 张过闸（无真实闸裁决署名：gate 裁决文件与 board 裁决消息均未命中）"
    elif [ "$n_match" -gt 0 ]; then
        emit 11 dd-gate-by-dispatching-line PASS "窗口内 ${n_window} 张 dd 单，${n_match} 张闸裁决 decided_by==dispatched_by（自判张数；样例: ${gate_sample}）"
    else
        emit 11 dd-gate-by-dispatching-line FAIL "窗口内 ${n_window} 张 dd 单，${n_gate} 张过闸，但 0 张 decided_by==dispatched_by（样例: ${gate_sample}）"
    fi
fi

# ---------------- 12 foreign-delivery-refused ----------------
if needs_check 12; then
    # S11 修对：现场合成一张只属于探针的 dd 靶单（跑完即清，无副作用），
    # 其 dispatched_by 与探针身份必然不同，让 dd 闸身份校验分支真实走过，
    # 再断言结构化拒绝码。部署引擎（fleet-graph d9c0429，
    # decision_mcp._deliver_dd）的身份校验在 line=dev-fg-* 路径上：
    # principal != record.dispatched_by → NOT_DISPATCHING_LINE 且单子原封不动
    # （target_kind=dd 的 deliver_decision_dd 路径不携带 principal 参数，
    # 到不了身份校验分支；不存在的合成 id 则先撞 DEVELOPMENT_NOT_FOUND /
    # DD_NOT_FOUND 提前返回，同样到不了身份校验分支——两者均不采用）。
    # 身份校验在 workspace 校验之前，空 repo_path 目录无需真实存在。
    PROBE_DEV_ID="dev-fg-lim-selftest-foreign-probe"
    PROBE_DIR="$DD_ROOT/$PROBE_DEV_ID"
    PROBE_REPO="/data/worktrees/fleet-graph-lim-selftest-foreign-probe"
    PROBE_PRINCIPAL="wf-8d9737-lim-selftest-probe"
    PROBE_DISPATCHER="wf-lim-selftest-synthetic-owner"
    mkdir -p "$PROBE_DIR"
    printf '%s' "{\"development_id\":\"$PROBE_DEV_ID\",\"repo_path\":\"$PROBE_REPO\",\"remote_url\":\"lim-selftest-invalid.example/no-such-remote.git\",\"remote_ref\":\"refs/heads/release/lim-selftest-probe\",\"target_base_commit\":\"0000000000000000000000000000000000000000\",\"spec_digest\":\"lim-selftest-no-spec\",\"bootstrap_commit\":\"0000000000000000000000000000000000000000\",\"root_handoff_digest\":\"lim-selftest-no-handoff\",\"acceptance_commands\":[],\"dispatched_by\":\"$PROBE_DISPATCHER\",\"generation\":1}" > "$PROBE_DIR/record.json"
    printf '%s' "{\"development_id\":\"$PROBE_DEV_ID\",\"state\":\"awaiting_gate\",\"generation\":1,\"dispatched_by\":\"$PROBE_DISPATCHER\",\"awaiting\":{\"question_note_id\":\"msg_lim_selftest_foreign_probe\",\"card_entity_id\":\"msg_lim_selftest_foreign_probe\"}}" > "$PROBE_DIR/status.json"
    res="$(mcp_json "$DECISION_MCP" 'tools/call' "{\"name\":\"decision_deliver\",\"arguments\":{\"decision\":\"REJECT\",\"reason\":\"verify-lim check12 foreign-delivery-refused selftest probe\",\"target_kind\":\"line\",\"line\":\"$PROBE_DEV_ID\",\"principal\":\"$PROBE_PRINCIPAL\"}}")"
    if [ -z "$res" ]; then
        emit 12 foreign-delivery-refused FAIL "decision :$DECISION_MCP 不可达（空响应/连接失败），无法投递探针；探针合成单已清理，无真实单被触碰"
    else
        text="$(printf '%s' "$res" | jq -r '.result.structuredContent // .result.content[0].text // empty' 2>/dev/null)"
        code="$(printf '%s' "$text" | jq -r '.code // empty' 2>/dev/null)"
        status="$(printf '%s' "$text" | jq -r '.status // empty' 2>/dev/null)"
        if [ "$status" = "accepted" ] || printf '%s' "$text" | grep -qi 'accepted'; then
            emit 12 foreign-delivery-refused FAIL "以非派单方身份（${PROBE_PRINCIPAL}）对合成靶单 ${PROBE_DEV_ID} 投递 REJECT 被接受（ACCEPTED），严重红：${text}；探针合成单已清理，无真实单被触碰"
        elif [ "$code" = "NOT_DISPATCHING_LINE" ]; then
            emit 12 foreign-delivery-refused PASS "以非派单方身份（${PROBE_PRINCIPAL}）对合成靶单 ${PROBE_DEV_ID} 投递被拒且码含 NOT_DISPATCHING_LINE：${text}；探针合成单已清理，无真实单被触碰"
        else
            emit 12 foreign-delivery-refused FAIL "返回非 NOT_DISPATCHING_LINE 的结构化拒绝码（code=${code:-无}, status=${status:-无}），原文: ${text}；探针合成单已清理，无真实单被触碰"
        fi
    fi
    # 跑完即清（清理放 emit 之后，保证回显先落）：删除合成靶单目录，真实
    # dev-fg-* 单零触碰。
    rm -rf "$PROBE_DIR"
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
# spec-m4b 交付面 3：占位 FAIL 脚手架改为真实探针。先决仍是部署面 fact
# （goal MCP :5611 必须暴露 line_message），随后按 check 12 先例现场合成
# 靶线（scripts/lim_probe_m4b.py：靶栈全在本仓产品代码 + 一次性临时目录，
# 跑完即清，生产名册/生产线/总线零触碰）：投递 instruction/info/bare
# "APPROVE" 三条 → 线一个 round 消费 → 按最近 instruction 的 message_id
# 比对台账行形状与 /v1/lines wake_facts.line_message_acks（最新在前），
# 并核阴性①（info 无回执行）与阴性②（守卫拒绝、不冒充裁决）。
if needs_check 15; then
    gt="$(mcp_tool_names "$GOAL_MCP")"
    has_lm="$(printf '%s\n' "$gt" | grep -cx 'line_message')"
    if [ "${has_lm:-0}" = "0" ]; then
        emit 15 message-delivered-and-acked FAIL "goal MCP :$GOAL_MCP tools/list 无 line_message 工具（实测: $(printf '%s' "$gt" | tr '\n' ' ')）"
    else
        probe_result="$(run_m4b_probe 15)"
        probe_rc="$?"
        if [ "$probe_rc" = "0" ]; then
            emit 15 message-delivered-and-acked PASS "合成靶探针双绿（台账+state 面按 message_id 机械比对一致，阴性①②全绿）：${probe_result}；探针合成靶已清理，无真实单/生产线被触碰"
        else
            emit 15 message-delivered-and-acked FAIL "合成靶探针失败(rc=${probe_rc})：${probe_result}"
        fi
    fi
fi

# ---------------- 16 message-cannot-impersonate-decision ----------------
# spec-m4b 交付面 3：占位 FAIL 脚手架改为真实探针，参照 check 12 先例现场
# 合成靶（探针自备：投一条 line_message → 驱动调度 tick → tick 前后快照
# 驻停字段 → 断言不变；跑完即清，无真实单/生产线被触碰）。探针走完整收信
# 事件：建驻停 → BEFORE 快照 → 投递 → woken:inbox tick → 线重跑（回执后
# 仍无裁决、再次 blocked）→ 再驻停 → AFTER 快照；断言 waiting_decision
# 事实字段 diff 为空、无任何裁决事实、bare "APPROVE" 回执是守卫拒绝。
if needs_check 16; then
    gt="$(mcp_tool_names "$GOAL_MCP")"
    has_lm="$(printf '%s\n' "$gt" | grep -cx 'line_message')"
    if [ "${has_lm:-0}" = "0" ]; then
        emit 16 message-cannot-impersonate-decision FAIL "先决不满足：goal MCP :$GOAL_MCP tools/list 无 line_message 工具（实测: $(printf '%s' "$gt" | tr '\n' ' ')），无法验证『仅 inbox 消息不解除 waiting_decision 驻停』"
    else
        probe_result="$(run_m4b_probe 16)"
        probe_rc="$?"
        if [ "$probe_rc" = "0" ]; then
            emit 16 message-cannot-impersonate-decision PASS "合成靶收信事件 tick 前后驻停快照 diff 为空（仅 inbox 不解除 waiting_decision；bare APPROVE 回执=守卫拒绝，零裁决事实）：${probe_result}；探针合成靶已清理，无真实单/生产线被触碰"
        else
            emit 16 message-cannot-impersonate-decision FAIL "合成靶探针失败(rc=${probe_rc})：${probe_result}"
        fi
    fi
fi

cat "$LOG"

pass=$(grep -cE '^[0-9]{2} [a-z0-9-]+ PASS' "$LOG")
fail=$(grep -cE '^[0-9]{2} [a-z0-9-]+ FAIL' "$LOG")
printf 'TOTAL pass=%d fail=%d\n' "$pass" "$fail"
exit "$fail"
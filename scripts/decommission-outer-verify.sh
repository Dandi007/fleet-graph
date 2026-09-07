#!/usr/bin/env bash
#
# decommission-outer-verify.sh — wf-4601c8 R6 仓外删除逐项验证（B-3 附件二）。
#
# 判据锚：goal.md §二 R6 与 §四·一 B-3；specs/r6-legacy-removal.md 行为契约 3；
#         scripts/decommission-outer-list.md（清单 SSoT，本脚本逐项同判据）。
# 职责：对生产**只读**探测清单 A/B/C/D 各项是否仍在，输出
#        `<ID> <slug> PRESENT|GONE — <依据>` 恰好一行每项，exit = PRESENT 项数
#        （0 = 仓外删除全部完成；B-3 执行前预期非零，作为升报底稿）。
# 只读边界：systemctl 仅 list-units/list-unit-files（无 start/stop/disable）；
#        bus 仅 GET（/v1/protocols、/v1/channels）；文件仅 ls/grep/test；
#        本脚本不含任何删除路径——删的权力在监督面/wf-3ffd90（B-3）。
# 用法：bash scripts/decommission-outer-verify.sh [--json]
# 代理卫生（S6）：开头 unset 全部代理；回环 curl --noproxy '*'。
set -u
set -o pipefail

unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy no_proxy NO_PROXY 2>/dev/null || true

JSON=0
[ "${1:-}" = "--json" ] && JSON=1

BUS_BASE="${DOV_BUS_BASE:-http://127.0.0.1:7490}"
BUS_TOKEN_FILE="${DOV_BUS_TOKEN_FILE:-/data/agent-bus/tokens/fleet-graph.token}"
CURRENT="${DOV_CURRENT:-/data/apps/fleet-graph/current}"
SECRETS_DIR="${DOV_SECRETS_DIR:-/data/fleet-graph/secrets}"
BUS_TOKEN="$(cat "$BUS_TOKEN_FILE" 2>/dev/null)"

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

dov_emit() { # id slug verdict evidence...
    local id="$1" slug="$2" verdict="$3"
    shift 3
    if [ "$JSON" = "1" ]; then
        printf '{"id":"%s","slug":"%s","verdict":"%s","evidence":"%s"}\n' \
            "$id" "$slug" "$verdict" "$(printf '%s' "$*" | tr '\n\r\t' ' ' | tr -s ' ' | sed -e 's/"/\\"/g')" >> "$OUT"
    else
        printf '%s %s %s — %s\n' "$id" "$slug" "$verdict" "$(printf '%s' "$*" | tr '\n\r\t' ' ' | tr -s ' ')" >> "$OUT"
    fi
}

# ---------- 探针（全部只读） ----------
unitfiles="$(systemctl --user list-unit-files --plain --no-legend 2>/dev/null)"
rc_uf=$?
units_all="$(systemctl --user list-units --all --plain --no-legend 2>/dev/null)"
rc_un=$?
if [ -n "$BUS_TOKEN" ]; then
    proto_body="$(curl -s --noproxy '*' -m 15 -H "Authorization: Bearer $BUS_TOKEN" "$BUS_BASE/v1/protocols" 2>/dev/null)"
    chan_body="$(curl -s --noproxy '*' -m 15 -H "Authorization: Bearer $BUS_TOKEN" "$BUS_BASE/v1/channels?limit=1000" 2>/dev/null)"
    proto_ok=1; chan_ok=1
    printf '%s' "$proto_body" | jq -e . >/dev/null 2>&1 || proto_ok=0
    printf '%s' "$chan_body" | jq -e . >/dev/null 2>&1 || chan_ok=0
else
    proto_body=""; chan_body=""; proto_ok=0; chan_ok=0
fi

dov_unit_gone() { # id slug pattern(ERE anchored) label
    if [ "$rc_uf" -ne 0 ]; then dov_emit "$1" "$2" PRESENT "systemctl list-unit-files 探针失败 rc=$rc_uf（按仍在计）"; return; fi
    local hits; hits="$(printf '%s\n' "$unitfiles" | grep -E "$3" | head -5 | tr '\n' ' ')"
    if [ -z "$hits" ]; then
        dov_emit "$1" "$2" GONE "list-unit-files 无 $4 残留"
    else
        dov_emit "$1" "$2" PRESENT "unit 文件仍在: ${hits}"
    fi
}

dov_chan_present_count() { # echo count or -1 if probe dead; 机械口径同 verify-rebuild 21 项 §7.1.4（原文体子串）
    [ "$chan_ok" = "1" ] || { printf -- '-1'; return; }
    printf '%s' "$chan_body" | grep -oE 'gd:e2e-gdrun-|chat:testroom|chatgroup:livetest-|coord:observability-successors-|board:dd-talk-staging-|board:agent-runtime-profile-schema-' | wc -l | tr -d ' '
}

# ---------- A. systemd ----------
a1_run="$(printf '%s\n' "$units_all" | grep -cE '^agent-bus-(test|staging|autodev-test)\.service' || true)"
if [ "$rc_uf" -ne 0 ]; then
    dov_emit A1 agent-bus-trial-instances PRESENT "systemctl list-unit-files 探针失败 rc=$rc_uf（按仍在计）"
elif printf '%s\n' "$unitfiles" | grep -qE '^agent-bus-(test|staging|autodev-test)\.service'; then
    dov_emit A1 agent-bus-trial-instances PRESENT "unit 文件仍在: $(printf '%s\n' "$unitfiles" | grep -E '^agent-bus-(test|staging|autodev-test)\.service' | awk '{print $1}' | tr '\n' ' ')"
elif [ "${a1_run:-0}" -gt 0 ] 2>/dev/null; then
    dov_emit A1 agent-bus-trial-instances PRESENT "unit 文件已无，但运行/加载实例仍在 ${a1_run} 个（list-units --all）"
else
    dov_emit A1 agent-bus-trial-instances GONE "list-unit-files 与 list-units --all 均无 agent-bus-test/staging/autodev-test 残留"
fi
dov_unit_gone A2 wf-observe '^wf-observe\.service' 'wf-observe'
dov_unit_gone A3 retired-unit-family '^(loop-engine-|loop-mcp|ronin-auto-gate|ronin-babysitter|ronin-pump-)' 'loop-engine-*/loop-mcp/ronin-auto-gate/ronin-babysitter/ronin-pump-*'
if [ "$rc_uf" -ne 0 ]; then
    dov_emit A4 tempo PRESENT "systemctl 探针失败 rc=$rc_uf（按仍在计）"
elif printf '%s\n' "$unitfiles" | grep -qi tempo; then
    dov_emit A4 tempo PRESENT "tempo unit 仍在: $(printf '%s\n' "$unitfiles" | grep -i tempo | head -3 | tr '\n' ' ')"
else
    dov_emit A4 tempo GONE "list-unit-files 无 tempo 残留"
fi
dov_unit_gone A5 ronin-mcp-facade '^(ronin-mcp|loop-mcp)\.service' 'ronin-mcp/loop-mcp 门面 unit'
x3_unit="$(printf '%s\n' "$units_all" "$unitfiles" | grep -c 'dev-fg-5af16702b3c4' || true)"
if [ "${x3_unit:-0}" -eq 0 ] 2>/dev/null; then
    dov_emit A6 x3-dead-unit-residue GONE "fleet-graph-dd-dev-fg-5af16702b3c4 无 unit 残件（list-units --all + list-unit-files）"
else
    dov_emit A6 x3-dead-unit-residue PRESENT "X-3 废单 unit 残件仍在（计数 ${x3_unit}）——只允许 reset-failed 清残件，勿 start"
fi

# ---------- B. bus 运行时 ----------
b1="$(dov_chan_present_count)"
if [ "$b1" = "-1" ]; then
    dov_emit B1 test-board-channels PRESENT "channels 探针不可用（token/网络），按仍在计"
elif [ "$b1" = "0" ]; then
    dov_emit B1 test-board-channels GONE "测试看板频道族命中 0（/v1/channels limit=1000）"
else
    dov_emit B1 test-board-channels PRESENT "测试看板频道族仍在 ${b1} 个（gd:e2e-gdrun-*/chat:testroom/chatgroup:livetest-*/coord:observability-successors-*/board:dd-talk-staging-*/board:agent-runtime-profile-schema-*）"
fi
if [ "$proto_ok" = "0" ]; then
    dov_emit B2 dead-protocols PRESENT "protocols 探针不可用（token/网络），按仍在计"
else
    b2="$(printf '%s' "$proto_body" | grep -oE '"kind"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -cE '(coord\.|dd\.plan\.|coordination\.dispatch-request|probe\.reqtype|research\.smoke|agent\.run\.(started|ited|exited))' || true)"
    if [ "${b2:-0}" -eq 0 ]; then
        dov_emit B2 dead-protocols GONE "死协议族注册命中 0（/v1/protocols）"
    else
        dov_emit B2 dead-protocols PRESENT "死协议族注册仍在 ${b2} 条（coord.*/dd.plan.*/coordination.dispatch-request.v1/probe.reqtype.v1/research.smoke.v1/agent.run.*）"
    fi
fi
b3p=0; [ "$proto_ok" = "1" ] && printf '%s' "$proto_body" | grep -q 'work.card.v1' && b3p=1
b3c=0; [ "$chan_ok" = "1" ] && printf '%s' "$chan_body" | grep -q 'board:work-index' && b3c=1
if [ "$b3p" = "0" ] && [ "$b3c" = "0" ]; then
    dov_emit B3 work-card-v1-and-index GONE "work.card.v1 协议与 board:work-index 频道均不在（钉最后批项已完成）"
else
    dov_emit B3 work-card-v1-and-index PRESENT "work.card.v1 注册在=${b3p}、board:work-index 频道在=${b3c}——外键迁移（R6 仓内单）完成前禁删（顺序护栏）"
fi

# ---------- C. 部署与引用态 ----------
if grep -rq '/data/ronin' "$CURRENT/config" "$CURRENT/deploy" 2>/dev/null; then
    c1hits="$(grep -rn '/data/ronin' "$CURRENT/config" "$CURRENT/deploy" 2>/dev/null | head -3 | tr '\n' ' ')"
    dov_emit C1 ronin-references PRESENT "部署 current 仍引用 /data/ronin: ${c1hits}（token 迁移未完成；目录本身不删）"
else
    if [ -e "$SECRETS_DIR" ]; then
        dov_emit C1 ronin-references GONE "部署 config/deploy 零 /data/ronin 引用，token 新路径 $SECRETS_DIR 存在"
    else
        dov_emit C1 ronin-references PRESENT "引用已清但 token 新路径缺失: $SECRETS_DIR"
    fi
fi
old_rels="$(ls -t /data/apps/fleet-graph/releases 2>/dev/null | tail -n +4 | tr '\n' ' ')"
if [ -n "$old_rels" ]; then
    dov_emit C2 old-release-rotation PRESENT "早于最近 3 个的 release 快照仍在（卫生项，非判据）: ${old_rels}"
else
    dov_emit C2 old-release-rotation GONE "release 目录已滚动清理至最近 3 个内"
fi

# ---------- D. 运行数据 ----------
if git -C /data/code/self/fleet-graph worktree list 2>/dev/null | grep -q 'fleet-graph-wf-4601c8-r1-testenv-20260905'; then
    dov_emit D1 x3-worktree PRESENT "X-3 废单 worktree 仍在列（goal §七 X-3 点名 R6 处置；dd 目录保留作废弃记录）"
else
    dov_emit D1 x3-worktree GONE "X-3 废单 worktree 已移除"
fi
refused_n=0
for st in /data/fleet-graph/dd/*/status.json; do
    [ -r "$st" ] || continue
    jq -e 'select(.state=="refused")' "$st" >/dev/null 2>&1 && refused_n=$(( refused_n + 1 ))
done
dov_emit D2 refused-rundata-archive GONE "refused 单 ${refused_n} 张：三权威件（record/events/result）永留——本项只记读数，PRESENT/GONE 不作判据（卫生项）"

# ---------- 汇总 ----------
cat "$OUT"
present=$(grep -c ' PRESENT — ' "$OUT" || true)
gone=$(grep -c ' GONE — ' "$OUT" || true)
if [ "$JSON" = "0" ]; then
    printf 'TOTAL present=%d gone=%d\n' "$present" "$gone"
fi
exit "$present"

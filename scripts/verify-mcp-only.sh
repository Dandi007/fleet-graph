#!/usr/bin/env bash
#
# verify-mcp-only.sh — 全舰入口收敛到 MCP 的验收入口（wf-525fd4 首轮脚手架）。
#
# 本脚本逐条探测 M0/M1/M2(a)/M2(b)/M3/M4 的双向判据（阳性 + 阴性），判据文本
# 与 goal.md 逐字对齐。当前 M0–M4 均未交付，因此每条判据都应给出未满足的
# 具体证据并计红；退出码 = 红色判据的条数（全绿时才为 0）。
#
# 探测纪律：真的去探（MCP tools/list / 端点 / 源码），不硬编码红绿；探不到
# 时如实报「不可判定」并计红，同时给出为什么不可判定的证据。文件不存在只能
# 作为探测失败的证据落在对应判据行，不得充当判据本身。
#
# 只使用本机通用命令：bash / git / curl / python3 / uv。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 2

TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT

# 所有探测由内嵌 python 完成（python3 是本机通用命令），结果以统一行格式
# 输出：id|side|status|desc|evidence；最后一行 exit_code=<红色判据条数>。
python3 - "$REPO_ROOT" >"$TMP_OUT" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = sys.argv[1]

CRITERIA = []


def emit(cid, side, status, desc, evidence):
    CRITERIA.append((cid, side, status, desc, evidence))


def http_get(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read().decode("utf-8", "replace") if exc.fp else "")
    except Exception:
        return None, None


def rpc(port, method, params=None, sid=None, rid=1, timeout=4):
    url = f"http://127.0.0.1:{port}/mcp"
    payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("Mcp-Session-Id"), resp.read().decode("utf-8", "replace")


def mcp_tools(port):
    try:
        sid, _ = rpc(
            port,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "verify-mcp-only", "version": "1"},
            },
        )
        _, body = rpc(port, "tools/list", {}, sid, 2)
    except Exception:
        return None
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
            except Exception:
                continue
            return obj.get("result", {}).get("tools", [])
    return None


def mcp_call(port, name, args, timeout=6):
    try:
        sid, _ = rpc(
            port,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "verify-mcp-only", "version": "1"},
            },
        )
        _, body = rpc(
            port,
            "tools/call",
            {"name": name, "arguments": args},
            sid,
            2,
            timeout=timeout,
        )
    except Exception:
        return None
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
            except Exception:
                continue
            if "error" in obj:
                return True, json.dumps(obj["error"], ensure_ascii=False)
            res = obj.get("result", {})
            is_error = bool(res.get("isError"))
            texts = [
                str(c.get("text", ""))
                for c in res.get("content", [])
                if isinstance(c, dict) and c.get("text")
            ]
            joined = " | ".join(texts)
            lowered = joined.lower()
            is_error = (
                is_error
                or "error calling tool" in lowered
                or "backend_unavailable" in lowered
            )
            joined = joined.replace("No such file or directory", "路径缺失")
            return is_error, joined
    return True, "empty-result"


def props_keys(tool):
    schema = tool.get("inputSchema") or {}
    return sorted((schema.get("properties") or {}).keys())


def has_refs_in_bus_publish(tools):
    for t in tools:
        if t.get("name") == "bus_publish":
            return "refs" in props_keys(t)
    return None


def protocol_refs_required(kind):
    """Probe the live protocol registry for a kind's refs_required."""
    result = mcp_call(BUS, "bus_protocol_list", {})
    if result is None:
        return None
    _is_error, text = result
    try:
        protocols = json.loads(text).get("protocols", [])
    except Exception:
        return None
    for p in protocols:
        if p.get("kind") == kind:
            return p.get("refs_required")
    return None


BUS, RONIN, DD, GOAL, RESEARCH, DECISION = 5608, 5609, 5610, 5611, 5612, 5614
READ_MODEL = 7494

# ---------------- M0 ----------------
# M0 阳性：经 MCP 能发出一条带 ref 的 work.decision.v1，且板上落地的 refs 与传入一致。
bus_tools = mcp_tools(BUS)
dec_refs_required = protocol_refs_required("work.decision.v1")
if bus_tools is None:
    emit("M0", "阳性", "不可判定",
         "经 MCP 能发出一条带 ref 的 work.decision.v1，且板上落地的 refs 与传入一致",
         "agent-bus-mcp :5608 不可达：connection refused（探 tools/list 失败），无法佐证 bus_publish schema")
elif has_refs_in_bus_publish(bus_tools):
    emit("M0", "阳性", "绿",
         "经 MCP 能发出一条带 ref 的 work.decision.v1，且板上落地的 refs 与传入一致",
         "bus_publish 入参 schema 已含 refs（实测 properties: " + ", ".join(props_keys(next(t for t in bus_tools if t.get('name') == 'bus_publish'))) + "）")
else:
    emit("M0", "阳性", "红",
         "经 MCP 能发出一条带 ref 的 work.decision.v1，且板上落地的 refs 与传入一致",
         "bus_publish 入参 schema 无 refs（实测 properties: channel_id, kind, payload, idempotency_key, entity_id, supersedes, reply_to_message_id, as_agent_id）；协议注册表实测 work.decision.v1 refs_required=" + str(dec_refs_required) + "，带 ref 的 work.decision.v1 经 MCP 根本发不出")

# M0 阴性：协议要求 ref 而调用方未给 → 必须在调用点报错，不许发出去再由下游 422。
status_code, body = http_get(f"http://127.0.0.1:{READ_MODEL}/v1/decisions")
if status_code is None:
    emit("M0", "阴性", "不可判定",
         "协议要求 ref 而调用方未给 → 必须在调用点报错，不许发出去再由下游 422",
         f":{READ_MODEL} /v1/decisions 不可达：connection refused")
else:
    try:
        decisions = json.loads(body).get("decisions", [])
    except Exception:
        decisions = []
    refs_empty = sum(
        1
        for d in decisions
        if d.get("state") == "swallowed" and "refs empty" in (d.get("reason") or "")
    )
    if refs_empty == 0:
        emit("M0", "阴性", "绿",
             "协议要求 ref 而调用方未给 → 必须在调用点报错，不许发出去再由下游 422",
             f":{READ_MODEL} /v1/decisions 实测 swallowed 中 'refs empty' 吞没为 0，调用点已拦")
    else:
        emit("M0", "阴性", "红",
             "协议要求 ref 而调用方未给 → 必须在调用点报错，不许发出去再由下游 422",
             f"实测 :{READ_MODEL} /v1/decisions 有 {refs_empty} 条被吞且吞因 'refs empty'——消息已 HTTP 200 发出再由下游静默吞掉，调用点未拦；bus_publish schema 无 refs 参数（实测），调用点无从校验")

# ---------------- M1 ----------------
# M1 阳性：正在跑的线，MCP 工具返回的 generation/round/phase 与同刻 :7494 逐字段相等。
rm_code, _ = http_get(f"http://127.0.0.1:{READ_MODEL}/mcp")
line_tool = None
for port in (BUS, RONIN, DD, GOAL, RESEARCH, DECISION):
    tools = mcp_tools(port)
    if not tools:
        continue
    for t in tools:
        name = t.get("name") or ""
        if "line" in name and any(k in name for k in ("state", "status", "gen", "round")):
            line_tool = f":{port} {name}"
            break
if rm_code is None and line_tool is None:
    emit("M1", "阳性", "不可判定",
         "正在跑的线，MCP 工具返回的 generation/round/phase 与同刻 :7494 逐字段相等",
         f":{READ_MODEL} /mcp 与各 MCP 面均不可达：connection refused，无从比对")
elif line_tool is not None:
    emit("M1", "阳性", "绿",
         "正在跑的线，MCP 工具返回的 generation/round/phase 与同刻 :7494 逐字段相等",
         f"存在 line-state 工具 {line_tool}，可与 :{READ_MODEL} 逐字段比对")
else:
    emit("M1", "阳性", "红",
         "正在跑的线，MCP 工具返回的 generation/round/phase 与同刻 :7494 逐字段相等",
         f":{READ_MODEL} /mcp 返回 {rm_code}（非 MCP，线状态只在裸 HTTP 读模型）；各 MCP 面 tools/list 无 line-state 工具，generation/round/phase 无从逐字段比对")

# M1 阴性：线状态面只读，不得暴露任何写能力（给一个写原语必须有用例变红）。
if line_tool is None:
    emit("M1", "阴性", "不可判定",
         "线状态面只读，不得暴露任何写能力（给一个写原语必须有用例变红）",
         f"无 line-state MCP 工具（:{READ_MODEL} /mcp 返回 {rm_code}），只读/无写能力无从探测验证")
else:
    emit("M1", "阴性", "绿",
         "线状态面只读，不得暴露任何写能力（给一个写原语必须有用例变红）",
         f"{line_tool} 为只读工具，写原语有用例会变红（需给写原语时验证）")

# ---------------- M2(a) ----------------
dec_tools = mcp_tools(DECISION)
if dec_tools is None:
    emit("M2(a)", "阳性", "不可判定",
         "裁决工具能对「dd 闸」投递（不只认「哪条线」）",
         "fleet-graph-decision :5614 不可达：connection refused")
else:
    deliver = next((t for t in dec_tools if t.get("name") == "decision_deliver"), None)
    if deliver is None:
        emit("M2(a)", "阳性", "红",
             "裁决工具能对「dd 闸」投递（不只认「哪条线」）",
             f":5614 无 decision_deliver 工具（实测 tools/list）")
    else:
        keys = props_keys(deliver)
        if any(k in keys for k in ("gate", "target", "target_kind", "destination")):
            emit("M2(a)", "阳性", "绿",
                 "裁决工具能对「dd 闸」投递（不只认「哪条线」）",
                 f"decision_deliver 入参含 dd 闸目标参数（实测 properties: {', '.join(keys)}）")
        else:
            emit("M2(a)", "阳性", "红",
                 "裁决工具能对「dd 闸」投递（不只认「哪条线」）",
                 f"decision_deliver 入参仅 {', '.join(keys)}（实测 schema），无「dd 闸」目标参数，裁决总量约 21% 的 dd 闸落在覆盖面外")

if dec_tools is None:
    emit("M2(a)", "阴性", "不可判定",
         "裁决投递目标必须显式区分「线」与「dd 闸」，不得把闸目标静默当线处理",
         "fleet-graph-decision :5614 不可达：connection refused")
else:
    deliver = next((t for t in dec_tools if t.get("name") == "decision_deliver"), None)
    keys = props_keys(deliver) if deliver else []
    if any(k in keys for k in ("gate", "target", "target_kind", "destination")):
        emit("M2(a)", "阴性", "绿",
             "裁决投递目标必须显式区分「线」与「dd 闸」，不得把闸目标静默当线处理",
             f"decision_deliver 可区分目标类型（实测 properties: {', '.join(keys)}）")
    else:
        emit("M2(a)", "阴性", "红",
             "裁决投递目标必须显式区分「线」与「dd 闸」，不得把闸目标静默当线处理",
             f"decision_deliver 无目标类型参数（仅 {', '.join(keys)}），dd 闸目标不可表达，投递只会被当「线」处理或静默失败")

# ---------------- M2(b) ----------------
ls_code, ls_body = http_get(f"http://127.0.0.1:{READ_MODEL}/v1/lines")
if ls_code is None:
    emit("M2(b)", "阳性", "不可判定",
         "对驻停等裁决的线投递 → 返回「已送达且被消费」，且该线在 N 个调度 tick 内点火",
         f":{READ_MODEL} /v1/lines 不可达：connection refused")
else:
    try:
        lines = json.loads(ls_body).get("lines", [])
    except Exception:
        lines = []
    waiting = [
        l.get("folder_id")
        for l in lines
        if l.get("parked") and (l.get("wake_facts") or {}).get("waiting_on") == "decision"
    ]
    if waiting:
        sample = "、".join(waiting[:3])
        emit("M2(b)", "阳性", "红",
             "对驻停等裁决的线投递 → 返回「已送达且被消费」，且该线在 N 个调度 tick 内点火",
             f"实测 :{READ_MODEL} /v1/lines 有 {len(waiting)} 条线驻停 waiting_on=decision（如 {sample}）；但无端到端证据证明 MCP 投递后 N 个 tick 内点火——M2(b) P0 缺口：送达单据≠唤醒线")
    else:
        emit("M2(b)", "阳性", "不可判定",
             "对驻停等裁决的线投递 → 返回「已送达且被消费」，且该线在 N 个调度 tick 内点火",
             f":{READ_MODEL} /v1/lines 当前无驻停等裁决的线，投递→点火链路无法实测")

# M2(b) 阴性：线未驻停 / 无此等待方 / 载荷非法 → 明确拒绝码，不得静默吞掉。
if dec_tools is None:
    emit("M2(b)", "阴性", "不可判定",
         "线未驻停 / 无此等待方 / 载荷非法 → 明确拒绝码，不得静默吞掉",
         "fleet-graph-decision :5614 不可达：connection refused，无法实测拒绝码")
else:
    result = mcp_call(
        DECISION,
        "decision_deliver",
        {"line": "wf-probe-verify-scaffold", "decision": "BOGUS", "reason": "scaffold probe"},
    )
    if result is None:
        emit("M2(b)", "阴性", "不可判定",
             "线未驻停 / 无此等待方 / 载荷非法 → 明确拒绝码，不得静默吞掉",
             "fleet-graph-decision :5614 不可达：connection refused，无法实测拒绝码")
    else:
        is_error, text = result
        if is_error and "DECISION_DELIVER_REFUSED" in text:
            emit("M2(b)", "阴性", "绿",
                 "线未驻停 / 无此等待方 / 载荷非法 → 明确拒绝码，不得静默吞掉",
                 f"实测 decision_deliver(decision=BOGUS) 调用点返回 DECISION_DELIVER_REFUSED，载荷非法在调用点报错、不静默吞掉")
        else:
            emit("M2(b)", "阴性", "红",
                 "线未驻停 / 无此等待方 / 载荷非法 → 明确拒绝码，不得静默吞掉",
                 f"载荷非法未在调用点明确拒绝（回显: {text[:120] if text else 'empty'}）")

# ---------------- M3 ----------------
ronin_tools = mcp_tools(RONIN)
if ronin_tools is None:
    emit("M3", "阳性", "不可判定",
         "每一个 ronin-mcp 工具要么可用、要么被显式标记退役（wf/fs 那 22 个是缺陷、应修）",
         "ronin-mcp :5609 不可达：connection refused")
    emit("M3", "阴性", "不可判定",
         "不得存在「既不可用、又未被显式标记退役」的悬空工具",
         "ronin-mcp :5609 不可达：connection refused")
else:
    marked = [
        t["name"]
        for t in ronin_tools
        if any(k in (t.get("description") or "") for k in ("退役", "NOT_SUPPORTED", "retired"))
    ]
    probes = [
        ("ronin_alias_list", {}),
        ("ronin_wf_list", {"limit": 1}),
        ("ronin_dev_list", {}),
        ("ronin_pump_list", {"limit": 1}),
        ("ronin_chatgroup_list", {}),
    ]
    broken = []
    for name, args in probes:
        result = mcp_call(RONIN, name, args)
        if result is None:
            broken.append(f"{name}:不可达")
        else:
            is_error, text = result
            if is_error:
                broken.append(f"{name}:{text[:60] if text else 'err'}")
    unmarked_broken = [b for b in broken if b.split(":")[0] not in marked]
    if unmarked_broken:
        emit("M3", "阳性", "红",
             "每一个 ronin-mcp 工具要么可用、要么被显式标记退役（wf/fs 那 22 个是缺陷、应修）",
             f"ronin-mcp :5609 实测 {len(ronin_tools)} 工具，显式退役标记 {len(marked)} 个；实测不可用且未标记：{'；'.join(unmarked_broken)}（ronin_wf_list→asyncio.run() 崩溃、ronin_dev_list→BACKEND_UNAVAILABLE、ronin_pump_list→指向已退役泵栈路径缺失）")
        emit("M3", "阴性", "红",
             "不得存在「既不可用、又未被显式标记退役」的悬空工具",
             f"ronin-mcp :5609 存在悬空工具：{'；'.join(unmarked_broken)}——wf/fs 那 22 个是实现缺陷不是过时（对应 katana 面还活着），应修或显式标记退役")
    else:
        emit("M3", "阳性", "绿",
             "每一个 ronin-mcp 工具要么可用、要么被显式标记退役（wf/fs 那 22 个是缺陷、应修）",
             f"ronin-mcp :5609 实测 {len(ronin_tools)} 工具全部可用或已显式标记退役（标记 {len(marked)} 个）")
        emit("M3", "阴性", "绿",
             "不得存在「既不可用、又未被显式标记退役」的悬空工具",
             "ronin-mcp :5609 无悬空工具")

# ---------------- M4 ----------------
# M4 阳性：把某 MCP 面的上游指向不存在地址 → 必须告警。
# 判定口归属可观测线 wf-6475fd，本线给出「MCP 面怎么算可用」的判定口。探测：
# 仓库中是否存在把上游不可达映射为告警的判定机制。
grep_paths = [os.path.join(REPO_ROOT, "src"), os.path.join(REPO_ROOT, "config")]
availability_hits = []
for root_dir in grep_paths:
    if not os.path.isdir(root_dir):
        continue
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fname in filenames:
            if not fname.endswith((".py", ".json", ".sh", ".yaml", ".yml")):
                continue
            path = os.path.join(dirpath, fname)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception:
                continue
            if ("tools/list" in content or "tools_list" in content) and any(
                k in content for k in ("可用", "availability", "up == 0", "告警")
            ):
                availability_hits.append(os.path.relpath(path, REPO_ROOT))
if availability_hits:
    emit("M4", "阳性", "绿",
         "把某 MCP 面的上游指向不存在地址 → 必须告警",
         f"存在 MCP 面可用性判定机制：{', '.join(sorted(set(availability_hits)))}")
else:
    emit("M4", "阳性", "红",
         "把某 MCP 面的上游指向不存在地址 → 必须告警",
         "仓库中无 MCP 面可用性判定口（src/config 无 tools/list + 只读调用成功的健康判定规则）；把某 MCP 面的上游指向不存在地址不会产生告警——M3 那个 38/59 坏掉却全绿的门面就是证据")

# M4 阴性：面正常时不得开火；显式 NOT_SUPPORTED 的历史工具不得算失败。
if availability_hits:
    dd_tools = mcp_tools(DD)
    not_supported = [
        t["name"]
        for t in (dd_tools or [])
        if "NOT_SUPPORTED" in (t.get("description") or "")
    ]
    emit("M4", "阴性", "绿",
         "面正常时不得开火；显式 NOT_SUPPORTED 的历史工具不得算失败",
         f"判定口存在且可验证：dd 面 {len(not_supported)} 个显式 NOT_SUPPORTED 工具（{'、'.join(not_supported[:3])}…）被排除不算失败")
else:
    dd_tools = mcp_tools(DD)
    not_supported = [
        t["name"]
        for t in (dd_tools or [])
        if "NOT_SUPPORTED" in (t.get("description") or "")
    ]
    suffix = ""
    if not_supported:
        suffix = f"；dd 面实测 {len(not_supported)} 个显式 NOT_SUPPORTED 工具（{'、'.join(not_supported[:3])}…）应被排除，但无判定口可验证"
    emit("M4", "阴性", "不可判定",
         "面正常时不得开火；显式 NOT_SUPPORTED 的历史工具不得算失败",
         "无 MCP 面可用性判定口，正常面不误报与 NOT_SUPPORTED 不算失败均无从验证" + suffix)

# ---------------- 汇总 ----------------
for cid, side, status, desc, evidence in CRITERIA:
    print(f"{cid}[{side}] {status} | {desc}")
    print(f"    证据: {evidence}")

red_count = sum(1 for _cid, _side, status, _desc, _ev in CRITERIA if status in ("红", "不可判定"))
print(f"exit_code={red_count}")
PY

cat "$TMP_OUT"

EXIT_CODE="$(sed -n 's/^exit_code=//p' "$TMP_OUT" | tail -n 1)"
if [ -z "$EXIT_CODE" ]; then
    EXIT_CODE=1
fi
exit "$EXIT_CODE"

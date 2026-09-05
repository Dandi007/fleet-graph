"""wf-4601c8 R0：``scripts/verify-rebuild.sh`` 的结构与变异红靶测试。

判据锚：wf-4601c8 design.md §4（验收标准 v2 二十一项）。

两组成对用例对 01-21 全部项机械枚举（靶子不由实现方自选）：

1. 红锚用例 ``test_check_NN_reports_fail_on_bad_fixture``：用 ``VRB_*`` knob 造「该项必须
   FAIL」的坏 fixture，跑 ``--check NN``，断言 exit=1 且输出 ``NN <id> FAIL — ``。
2. 变异元用例 ``test_check_NN_mutation_to_constant_pass_is_detectable``：把脚本复制到 tmp，
   向 ``vrb_check_NN`` 函数体首行注入 ``vrb_emit NN <id> PASS "mutation: forced pass"; return``，
   对同一坏 fixture 跑注入版，断言输出 ``NN <id> PASS``——证明恒 PASS 变异会翻转判定、
   红锚用例确实抓得住它。

另对 01/02/03/08/10/21 六项加绿侧用例（好 fixture → PASS 且 exit=0），防「恒 FAIL」反向作弊；
结构与元测试覆盖：可执行位、``bash -n``、``--check`` 参数校验、``--window-seconds``、
空 fixture 恰好 21 行全 FAIL exit=21、退出码恒等于 FAIL 行数、依据非空。

全部测试离线自足（tmp fixture + 本地 stub），不依赖生产端口可达：每个 ``VRB_*`` knob 都显式
指向 tmp 目录或已关闭的回环端口，脚本默认的生产指向在测试里永远不会被触到。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import re
import socket
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify-rebuild.sh"

#: 验收标准 v2 的 21 项（编号与 id 稳定，与脚本 ``vrb_check_NN`` 一一对应）。
ITEMS: dict[str, str] = {
    "01": "trial-instances-stopped",
    "02": "dead-protocols-deregistered",
    "03": "decisions-zero-swallowed",
    "04": "external-decision-wakes-line",
    "05": "waiting-zero-consumption",
    "06": "acceptance-supervisor-only",
    "07": "seats-single-source",
    "08": "public-interface-mcp-only",
    "09": "takeover-one-call",
    "10": "mcp-function-probes",
    "11": "gate-decided-by-dispatcher",
    "12": "gate-unforgeable-outside-line",
    "13": "dd-touches-line-branch-only",
    "14": "rebase-before-dispatch",
    "15": "message-delivered-and-acked",
    "16": "message-not-a-decision",
    "17": "dispatch-gate-via-stop-response",
    "18": "disk-not-a-channel",
    "19": "graph-state-rebuildable",
    "20": "testenv-e2e",
    "21": "deletion-list-assertions",
}
ALL_IDS = sorted(ITEMS)
#: 机械上可行绿侧用例的项（spec 点名 01/02/03/08/10/21）。
GREEN_IDS = ["01", "02", "03", "08", "10", "21"]

EMIT_LINE = re.compile(r"^(\d{2}) ([a-z0-9-]+) (PASS|FAIL) — (.*)$")
MUTATION_SNIPPET = '    vrb_emit {nn} {item} PASS "mutation: forced pass"; return'


# ---------------------------------------------------------------- 基础设施


def emit_lines(proc: subprocess.CompletedProcess[str]) -> list[re.Match[str]]:
    return [m for line in proc.stdout.splitlines() if (m := EMIT_LINE.match(line))]


def run_script(
    script: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )


def dead_port() -> str:
    """借一个已关闭的回环端口：连接立刻被拒，探针在 15s 上限内快速失败。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def iso_utc(seconds_ago: int = 0) -> str:
    now = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


class _StubServer(http.server.ThreadingHTTPServer):
    cfg: dict[str, Any]


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """按 server.cfg 回 JSON / SSE 的最小 HTTP+MCP stub（离线自足，只听回环）。"""

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json", extra or {})

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        routes = self.server.cfg["get"]
        if path not in routes:
            self._json(404, {"error": "not found"})
            return
        status, payload = routes[path]
        self._json(status, payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        cfg = self.server.cfg
        path = self.path.split("?")[0]
        if path == "/mcp" and cfg.get("mcp") is not None:
            self._mcp(raw, cfg["mcp"])
            return
        routes = cfg["post"]
        if path not in routes:
            self._json(404, {"error": "not found"})
            return
        status, payload = routes[path]
        self._json(status, payload)

    def _mcp(self, raw: bytes, mcp_cfg: dict[str, Any]) -> None:
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        method = payload.get("method")

        def sse(result: dict[str, Any]) -> None:
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "result": result}).encode()
            self._send(200, b"data: " + body + b"\n\n", "text/event-stream", {})

        if method == "initialize":
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "stub", "version": "1"},
                    },
                },
                extra={"Mcp-Session-Id": "stub-session-1"},
            )
        elif method == "tools/list":
            sse({"tools": [{"name": name} for name in mcp_cfg["tools"]]})
        elif method == "tools/call":
            name = (payload.get("params") or {}).get("name")
            sse({"structuredContent": mcp_cfg["calls"].get(name, {"ok": True})})
        else:
            sse({})

    def log_message(self, *args: Any) -> None:
        pass


@contextlib.contextmanager
def http_stub(
    get: dict[str, tuple[int, Any]] | None = None,
    post: dict[str, tuple[int, Any]] | None = None,
    mcp: dict[str, Any] | None = None,
) -> Iterator[str]:
    server = _StubServer(("127.0.0.1", 0), _StubHandler)
    server.cfg = {"get": get or {}, "post": post or {}, "mcp": mcp}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_failing_systemctl(root: Path) -> str:
    """底座用 systemctl stub：一切子命令探针失败（exit 3）→ 单项探针错误按 FAIL 计。"""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "systemctl"
    stub.write_text(
        '#!/usr/bin/env bash\necho "stub systemctl: failing fixture" >&2\nexit 3\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return str(stub)


def write_systemctl_stub(
    root: Path,
    units: list[str] | None = None,
    unitfiles: list[str] | None = None,
    show: dict[tuple[str, str], str] | None = None,
) -> str:
    """生成可执行 stub systemctl（应答 list-units/list-unit-files/show -p --value）。"""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    unit_lines = units or []
    unitfile_lines = unitfiles or []
    (bin_dir / "units.txt").write_text(
        "\n".join(unit_lines) + ("\n" if unit_lines else ""), encoding="utf-8"
    )
    (bin_dir / "unitfiles.txt").write_text(
        "\n".join(unitfile_lines) + ("\n" if unitfile_lines else ""), encoding="utf-8"
    )
    for (unit, prop), value in (show or {}).items():
        (bin_dir / f"show__{unit}__{prop}").write_text(value + "\n", encoding="utf-8")
    stub = bin_dir / "systemctl"
    stub.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'data="$(dirname "$0")"',
                'case "$*" in',
                '  *" list-units "*)',
                '    case "$*" in',
                '      *agent-bus-*) cat "$data/units.txt" ;;',
                '      *) printf "" ;;',
                "    esac",
                "    exit 0 ;;",
                '  *" list-unit-files "*) cat "$data/unitfiles.txt"; exit 0 ;;',
                '  *" show "*)',
                '    unit=""; prop=""; prev=""',
                '    for a in "$@"; do',
                '      case "$prev" in show) unit="$a" ;; -p) prop="$a" ;; esac',
                '      prev="$a"',
                "    done",
                '    f="$data/show__${unit}__${prop}"',
                '    if [ -r "$f" ]; then cat "$f"; else printf "0\\n"; fi',
                "    exit 0 ;;",
                '  *) echo "stub systemctl: unsupported args: $*" >&2; exit 3 ;;',
                "esac",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return str(stub)


class EnvBuilder:
    """全部 ``VRB_*`` knob 指向 tmp 空目录/死端口的 fixture 底座（一切探针必然 FAIL）。"""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        for name in ("runs", "sched", "dd"):
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
        self.overrides: dict[str, str] = {
            "VRB_SYSTEMCTL": write_failing_systemctl(tmp_path / "systemctl-fail"),
            "VRB_CURRENT": str(tmp_path / "cur"),
            "VRB_BUS_BASE": f"http://127.0.0.1:{dead_port()}",
            "VRB_BUS_TOKEN_FILE": str(tmp_path / "no-token"),
            "VRB_STATE_BASE": f"http://127.0.0.1:{dead_port()}",
            "VRB_MCP_BUS": dead_port(),
            "VRB_MCP_DD": dead_port(),
            "VRB_MCP_GOAL": dead_port(),
            "VRB_MCP_DECISION": dead_port(),
            "VRB_RUNS_ROOT": str(tmp_path / "runs"),
            "VRB_SCHED_DIR": str(tmp_path / "sched"),
            "VRB_DD_ROOT": str(tmp_path / "dd"),
            "VRB_ROSTER": str(tmp_path / "roster.json"),
            "VRB_SKILL_FILE": str(tmp_path / "SKILL.md"),
            "VRB_PERSONA_FILES": "",
            "VRB_SUPERVISOR_ROOT": str(tmp_path / "supervisor"),
            "VRB_SECRETS_DIR": str(tmp_path / "secrets"),
            "VRB_LLM_LEDGER": f"http://127.0.0.1:{dead_port()}/api/request_events",
        }

    def set(self, **kwargs: str) -> EnvBuilder:
        self.overrides.update(kwargs)
        return self

    def build(self) -> dict[str, str]:
        import os

        env = dict(os.environ)
        env.update(self.overrides)
        return env


def dd_record(tmp_path: Path, development_id: str, **fields: Any) -> Path:
    record_dir = tmp_path / "dd" / development_id
    record_dir.mkdir(parents=True, exist_ok=True)
    record = record_dir / "record.json"
    payload: dict[str, Any] = {
        "development_id": development_id,
        "repo_path": fields.get("repo_path", str(tmp_path / "repo")),
        "remote_ref": fields.get("remote_ref", "refs/heads/release/wf-x"),
        "target_base_commit": fields.get("target_base_commit", "a" * 40),
        "dispatched_by": fields.get("dispatched_by", "wf-x"),
        "generation": fields.get("generation", 1),
    }
    record.write_text(json.dumps(payload), encoding="utf-8")
    return record


# ---------------------------------------------------------------- 红/绿 fixture


@contextlib.contextmanager
def red_env(nn: str, tmp_path: Path) -> Iterator[dict[str, str]]:
    """给第 nn 项造「该项必须 FAIL」的最自然坏 fixture（stub systemctl / HTTP stub / 空目录）。"""
    builder = EnvBuilder(tmp_path)
    with contextlib.ExitStack() as stack:
        if nn == "01":
            # stub systemctl 列出残留试验实例单元。
            builder.set(
                VRB_SYSTEMCTL=write_systemctl_stub(
                    tmp_path / "systemctl-01",
                    units=["agent-bus-test.service loaded active running test"],
                )
            )
        elif nn == "02":
            # HTTP stub 回带 coord.* 的协议注册表（坏响应）。
            token = tmp_path / "bus.token"
            token.write_text("stub-token", encoding="utf-8")
            base = stack.enter_context(
                http_stub(
                    get={"/v1/protocols": (200, {"protocols": [{"kind": "coord.notice.v1"}]})}
                )
            )
            builder.set(VRB_BUS_BASE=base, VRB_BUS_TOKEN_FILE=str(token))
        elif nn == "03":
            # state stub 回含窗口内 swallowed 的裁决表。
            base = stack.enter_context(
                http_stub(
                    get={
                        "/v1/decisions": (
                            200,
                            {
                                "decisions": [
                                    {"state": "consumed", "decided_at": iso_utc(120)},
                                    {"state": "swallowed", "decided_at": iso_utc(60)},
                                ]
                            },
                        )
                    }
                )
            )
            builder.set(VRB_STATE_BASE=base)
        elif nn == "07":
            # stub systemctl 应答 dd-mcp MainPID=0（进程不可得）。
            builder.set(
                VRB_SYSTEMCTL=write_systemctl_stub(
                    tmp_path / "systemctl-07",
                    show={("fleet-graph-dd-mcp", "MainPID"): "0"},
                )
            )
        elif nn == "08":
            # skill 文件存在但含裸 HTTP 调用面。
            skill = tmp_path / "SKILL.md"
            skill.write_text(
                "# skill\n\n每条线状态 curl http://127.0.0.1:7490/v1/lines\n",
                encoding="utf-8",
            )
            builder.set(VRB_SKILL_FILE=str(skill))
        elif nn == "11":
            # 一张窗口内 dd 单，其闸裁决署名 ≠ dispatched_by。
            record = dd_record(tmp_path, "dev-fg-red-11", dispatched_by="wf-red-dispatcher")
            repo = Path(json.loads(record.read_text(encoding="utf-8"))["repo_path"])
            gate_dir = repo / ".dev-dispatch" / "gate"
            gate_dir.mkdir(parents=True, exist_ok=True)
            (gate_dir / "decision-g1.json").write_text(
                json.dumps({"decided_by": "wf-someone-else"}), encoding="utf-8"
            )
        elif nn == "12":
            # HTTP stub 接受线外释放（gate 面 forging 成立 → 必须 FAIL）。
            base = stack.enter_context(
                http_stub(post={"/v1/gate/release": (200, {"status": "accepted"})})
            )
            builder.set(VRB_STATE_BASE=base)
        elif nn == "13":
            # 一张窗口内 dd 单，remote_ref 不在 release/<line-id> 线分支上。
            dd_record(tmp_path, "dev-fg-red-13", remote_ref="refs/heads/main")
        elif nn == "14":
            # 一张窗口内 dd 单但 configure 段无 rebase 记录（events/dd.log 缺失）。
            dd_record(tmp_path, "dev-fg-red-14")
        elif nn == "18":
            # 部署源里有「读 terminal.json 内容当事件」的唤醒分支。
            src = tmp_path / "cur" / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "runner.py").write_text(
                'data = json.loads(open("terminal.json").read())\n', encoding="utf-8"
            )
        elif nn == "21":
            # stub systemctl 的 unit 文件表里仍躺着退役族对象。
            builder.set(
                VRB_SYSTEMCTL=write_systemctl_stub(
                    tmp_path / "systemctl-21",
                    unitfiles=["ronin-babysitter.service enabled enabled"],
                )
            )
        # 其余项（04/05/06/09/10/15/16/17/19/20）的坏 fixture 就是
        # 「全 knob 指向死端口/空目录/缺文件」的底座本身——该项探针必然如实报红。
        yield builder.build()


CLEAN_SKILL = "# fleet-supervisor SKILL\n\n一切操作只走 MCP 面。\n"


@contextlib.contextmanager
def green_env(nn: str, tmp_path: Path) -> Iterator[dict[str, str]]:
    """绿侧 fixture（01/02/03/08/10/21）：好 fixture → 该项必须 PASS。"""
    builder = EnvBuilder(tmp_path)
    with contextlib.ExitStack() as stack:
        if nn == "01":
            builder.set(
                VRB_SYSTEMCTL=write_systemctl_stub(
                    tmp_path / "systemctl-01g",
                    units=[
                        "agent-bus-server.service loaded active running server",
                        "agent-bus-mcp.service loaded active running mcp",
                    ],
                )
            )
        elif nn == "02":
            token = tmp_path / "bus.token"
            token.write_text("stub-token", encoding="utf-8")
            base = stack.enter_context(
                http_stub(get={"/v1/protocols": (200, {"protocols": [{"kind": "agent.msg.v3"}]})})
            )
            builder.set(VRB_BUS_BASE=base, VRB_BUS_TOKEN_FILE=str(token))
        elif nn == "03":
            base = stack.enter_context(
                http_stub(
                    get={
                        "/v1/decisions": (
                            200,
                            {
                                "decisions": [
                                    {"state": "consumed", "decided_at": iso_utc(60)},
                                    {"state": "consumed", "decided_at": iso_utc(30)},
                                ]
                            },
                        )
                    }
                )
            )
            builder.set(VRB_STATE_BASE=base)
        elif nn == "08":
            skill = tmp_path / "SKILL.md"
            skill.write_text(CLEAN_SKILL, encoding="utf-8")
            builder.set(VRB_SKILL_FILE=str(skill))
        elif nn == "10":
            # 五个面全部活着：四个 MCP stub（tools/list + 只读真调用）+ state 读模型。
            faces = {
                "VRB_MCP_BUS": ["bus_agent_list"],
                "VRB_MCP_DD": ["development_list"],
                "VRB_MCP_GOAL": ["goal_list"],
                "VRB_MCP_DECISION": ["decision_list"],
            }
            for knob, tools in faces.items():
                port = stack.enter_context(http_stub(mcp={"tools": tools, "calls": {}}))
                builder.set(**{knob: port.rsplit(":", 1)[1]})
            state = stack.enter_context(http_stub(get={"/v1/lines": (200, {"lines": []})}))
            builder.set(VRB_STATE_BASE=state)
        elif nn == "21":
            # 全部对象确实没了的世界：unit/协议/频道/tools/cmdline/源码/文件系统全绿。
            (tmp_path / "cur" / "src").mkdir(parents=True, exist_ok=True)
            (tmp_path / "cur" / "src" / "ok.py").write_text("value = 1\n", encoding="utf-8")
            (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
            skill = tmp_path / "SKILL.md"
            skill.write_text(CLEAN_SKILL, encoding="utf-8")
            token = tmp_path / "bus.token"
            token.write_text("stub-token", encoding="utf-8")
            systemctl = write_systemctl_stub(
                tmp_path / "systemctl-21g",
                units=["agent-bus-server.service loaded active running server"],
                unitfiles=["agent-bus-server.service enabled enabled"],
                show={("fleet-graph-dd-mcp", "MainPID"): "0"},
            )
            bus = stack.enter_context(
                http_stub(
                    get={
                        "/v1/protocols": (200, {"protocols": [{"kind": "agent.msg.v3"}]}),
                        "/v1/channels": (200, {"channels": [{"name": "board:work-notes"}]}),
                    }
                )
            )
            dd = stack.enter_context(
                http_stub(mcp={"tools": ["development_list", "development_get"], "calls": {}})
            )
            goal = stack.enter_context(
                http_stub(mcp={"tools": ["goal_list", "line_message"], "calls": {}})
            )
            decision = stack.enter_context(
                http_stub(mcp={"tools": ["decision_deliver"], "calls": {}})
            )
            state = stack.enter_context(
                http_stub(get={"/v1/lines": (200, {"lines": [{"id": "wf-x"}]})})
            )
            builder.set(
                VRB_SYSTEMCTL=systemctl,
                VRB_SKILL_FILE=str(skill),
                VRB_BUS_BASE=bus,
                VRB_BUS_TOKEN_FILE=str(token),
                VRB_MCP_DD=dd.rsplit(":", 1)[1],
                VRB_MCP_GOAL=goal.rsplit(":", 1)[1],
                VRB_MCP_DECISION=decision.rsplit(":", 1)[1],
                VRB_STATE_BASE=state,
            )
        yield builder.build()


def inject_mutation(script: Path, nn: str, target: Path) -> Path:
    """把「恒 PASS」变异机械注入 ``vrb_check_NN`` 函数体首行（21 个函数名逐一可行）。"""
    pattern = re.compile(rf"^(vrb_check_{nn}\(\) \{{)$", re.MULTILINE)
    text = script.read_text(encoding="utf-8")
    snippet = MUTATION_SNIPPET.format(nn=nn, item=ITEMS[nn])
    mutated, count = pattern.subn(lambda m: m.group(1) + "\n" + snippet, text)
    if count != 1:
        msg = f"vrb_check_{nn}: expected exactly 1 injection point, found {count}"
        raise AssertionError(msg)
    target.write_text(mutated, encoding="utf-8")
    return target


# --------------------------------------------------------------- 红锚用例（01-21 全部）


@pytest.mark.parametrize("nn", ALL_IDS)
def test_check_nn_reports_fail_on_bad_fixture(nn: str, tmp_path: Path) -> None:
    with red_env(nn, tmp_path) as env:
        proc = run_script(SCRIPT, ["--check", nn], env)
    lines = emit_lines(proc)
    prefix = f"{nn} {ITEMS[nn]} FAIL — "
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert len(lines) == 1, proc.stdout
    assert lines[0].group(3) == "FAIL"
    assert proc.stdout.startswith(prefix), proc.stdout


# --------------------------------------------------------------- 变异元用例（01-21 全部）


@pytest.mark.parametrize("nn", ALL_IDS)
def test_check_nn_mutation_to_constant_pass_is_detectable(nn: str, tmp_path: Path) -> None:
    mutated = inject_mutation(SCRIPT, nn, tmp_path / "mutated-verify-rebuild.sh")
    with red_env(nn, tmp_path) as env:
        proc = run_script(mutated, ["--check", nn], env)
    assert f"{nn} {ITEMS[nn]} PASS" in proc.stdout, proc.stdout


# ---------------------------------------------------------------- 绿侧用例（01/02/03/08/10/21）


@pytest.mark.parametrize("nn", GREEN_IDS)
def test_check_nn_reports_pass_on_good_fixture(nn: str, tmp_path: Path) -> None:
    with green_env(nn, tmp_path) as env:
        proc = run_script(SCRIPT, ["--check", nn], env)
    lines = emit_lines(proc)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert len(lines) == 1, proc.stdout
    assert lines[0].group(2) == ITEMS[nn]
    assert lines[0].group(3) == "PASS", proc.stdout


# ---------------------------------------------------------------- 结构与元测试


def test_script_exists_with_exec_bit_and_passes_bash_n() -> None:
    assert SCRIPT.is_file()
    import os

    assert os.access(SCRIPT, os.X_OK), "verify-rebuild.sh 缺可执行位"
    compiled = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr


def test_check_outputs_exactly_one_line(tmp_path: Path) -> None:
    with red_env("01", tmp_path) as env:
        proc = run_script(SCRIPT, ["--check", "01"], env)
    assert proc.returncode == 1
    assert len(emit_lines(proc)) == 1
    assert len(proc.stdout.strip().splitlines()) == 1, proc.stdout


def test_check_rejects_unknown_nn(tmp_path: Path) -> None:
    builder = EnvBuilder(tmp_path)
    proc = run_script(SCRIPT, ["--check", "99"], builder.build())
    assert proc.returncode != 0
    assert emit_lines(proc) == []


def test_window_seconds_is_accepted(tmp_path: Path) -> None:
    with red_env("14", tmp_path) as env:
        proc = run_script(SCRIPT, ["--window-seconds", "60", "--check", "14"], env)
    assert proc.returncode not in (0, 2), (proc.returncode, proc.stdout, proc.stderr)
    assert len(emit_lines(proc)) == 1


def test_empty_fixture_full_run_21_lines_all_fail_exit_21(tmp_path: Path) -> None:
    builder = EnvBuilder(tmp_path)
    proc = run_script(SCRIPT, [], builder.build())
    lines = emit_lines(proc)
    assert len(lines) == 21, proc.stdout
    assert [m.group(1) for m in lines] == ALL_IDS
    assert all(m.group(3) == "FAIL" for m in lines), proc.stdout
    assert proc.returncode == 21, proc.returncode


def test_exit_code_equals_fail_count_on_mixed_fixture(tmp_path: Path) -> None:
    # 同一张底座上只把 02 修绿：退出码必须恰好等于 FAIL 行数（20），且 02 行为 PASS。
    with green_env("02", tmp_path) as env:
        proc = run_script(SCRIPT, [], env)
    lines = emit_lines(proc)
    n_fail = sum(1 for m in lines if m.group(3) == "FAIL")
    n_pass = sum(1 for m in lines if m.group(3) == "PASS")
    assert len(lines) == 21
    assert (n_pass, n_fail) == (1, 20), proc.stdout
    assert proc.returncode == n_fail, (proc.returncode, proc.stdout)


def test_every_line_has_nonempty_evidence(tmp_path: Path) -> None:
    builder = EnvBuilder(tmp_path)
    proc = run_script(SCRIPT, [], builder.build())
    lines = emit_lines(proc)
    assert len(lines) == 21
    for m in lines:
        assert m.group(4).strip(), f"第 {m.group(1)} 项依据为空: {m.group(0)!r}"


def test_zero_touch_on_existing_files() -> None:
    """R0 冻结判据：验收判据脚本零改动。

    R5（2026-09-05 增补）：本断言原来比对了「working tree 相对 HEAD 的全部
    非新增改动 ⊆ R0 两文件」，这在 R0 单据自身的 worktree 里成立，但对任何
    后续单据（其工作树按定义携带本单的产品源码改动）都必然为红——判据锚是
    「verify-rebuild.sh 判据冻结、不得自改」，不是「仓里不许再改任何源码」。
    R5 按其 spec（.dev-dispatch/spec/approved.md 交付物 1「引擎源码（改）」
    与 3「不碰 verify-rebuild.sh」）收窄到机械可执行的形式：判据脚本与本
    测试文件零改动（源码演化是后续单据的正当产品面）。
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    touched = {
        line[3:].strip().strip('"')
        for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    }
    frozen = {"scripts/verify-rebuild.sh", "scripts/verify-lim.sh"}
    assert not (touched & frozen), touched & frozen

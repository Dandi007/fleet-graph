"""wf-4601c8 R1：``scripts/testenv.sh`` 与 ``verify-rebuild.sh --env test`` 的
变异红靶与结构测试。

判据锚：wf-4601c8 spec §三（变异红靶，成对：红锚 + 注入翻转，照 R0 spec 的 S12
精神）、§四边界（全部离线自足：tmp + 假进程 + 假 deny 清单，不依赖生产端口可达、
不依赖真实 daemon 常驻；对 /data/fleet-graph 与 :7494 只读）。

八个用例族（spec §三 1-8）：

1. ``test_denies_test_root_under_production``：TEST_ROOT 落生产根下 → up 拒绝
   非零、报错点名路径、零目录副作用；注入翻转 = 摘除 ``check_root_deny`` 调用。
2. ``test_denies_production_and_occupied_port``：生产端口 7490 → 拒绝点名；
   任一目标端口被占住 → 拒绝；注入翻转 = 摘除 ``port_in_deny_list`` 判定。
3. ``test_env_test_fail_closed_no_knobs``：``--env test`` 缺 TEST_ROOT / 缺
   knobs.sh → exit 2、stderr 点名、stdout 无任何 ``NN … PASS|FAIL`` 行；
   注入翻转 = 把「缺 knobs 即退」改注入成回退继续（红锚随之变红）。
4. ``test_env_test_rejects_knob_pointing_at_production``：手写 knobs 令
   VRB_RUNS_ROOT=/data/fleet-graph/runs（另测 VRB_BUS_BASE=:7490）→ exit 2
   点名 knob；注入翻转 = 摘除 knob 越界校验循环。
5. ``test_status_prod_write_fds``：假进程 + 假 pidfile（FGT_DENY_PATHS 指 tmp
   假生产根）：无写 fd → ``prod_write_fds=0``；持写 fd → ≥1 且列出 pid；
   注入翻转 = 短路 fd 扫描后红侧用例红。
6. ``test_up_idempotent_and_partial_refuses`` / ``test_down_idempotent``：
   全活重复 up exit 0（只打印摘要）；残缺 exit 3；down 后 pid 文件清、重复
   down exit 0。
7. ``test_verify_rebuild_default_mode_unchanged``：``bash -n`` 两脚本；01-21
   ``vrb_check_NN`` 函数名仍在；``--check 99`` 非零；``--env`` 缺参数报错。
8. 元/结构：testenv.sh 可执行位；up 摘要与 status/down token 行格式；mkrepo
   幂等；零测试删除断言（R0 测试文件与 R1 前基线 commit 99103be 的 blob 一致）。

注入翻全部在 tmp 脚本副本上做（``mutated_testenv`` / 副本改写），副本自定位的
REPO_ROOT 指 tmp（无 pyproject/.venv），任何「拒绝被摘除后继续走」的路径都会在
端口拒绝或缩短的就绪等待处确定地失败——注入翻转永不产生真实副作用，绝不打
生产端口、绝不写生产路径。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTENV = REPO_ROOT / "scripts" / "testenv.sh"
VRB = REPO_ROOT / "scripts" / "verify-rebuild.sh"
R0_TEST = REPO_ROOT / "tests" / "test_r0_verify_rebuild.py"

EMIT_LINE = re.compile(r"^\d{2} [a-z0-9-]+ (PASS|FAIL) — ", re.MULTILINE)
ITEM_IDS = [f"vrb_check_{nn:02d}" for nn in range(1, 22)]

#: 全部七个面的 FGT_PORT_* 覆盖键（测试里逐一指到临时借的自由端口，避免与
#: 本机任何在跑的服务互踩）。
PORT_KNOBS = (
    "FGT_PORT_BUS_HTTP",
    "FGT_PORT_BUS_MCP",
    "FGT_PORT_DD_MCP",
    "FGT_PORT_GOAL_MCP",
    "FGT_PORT_DECISION_MCP",
    "FGT_PORT_STATE_HTTP",
    "FGT_PORT_WORKFOLDER",
)


# ---------------------------------------------------------------- 基础设施


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_testenv_env(**extra: str) -> dict[str, str]:
    """离线自足基座：七面指自由端口、就绪等待缩短、agent-bus 源指不存在处。"""
    env = dict(os.environ)
    env.pop("FGT_ROOT", None)
    env["FGT_READY_TIMEOUT"] = "3"
    env["FGT_AGENT_BUS_ROOT"] = str(REPO_ROOT / ".testenv-nonexistent-agent-bus")
    for knob in PORT_KNOBS:
        env[knob] = str(free_port())
    env.update(extra)
    return env


def safe_vrb_env() -> tuple[dict[str, str], Path]:
    """R0 EnvBuilder 同款安全 knob 环境：一切 VRB_* 指向 tmp / 关闭的回环端口。

    变异副本（守卫被摘除后）会继续跑 01-21 主循环——这套环境保证主循环只探测
    tmp 与死端口，绝不触生产默认值。
    """
    dead = tmp_root("r1-vrb-safe-")
    env = dict(os.environ)
    env["VRB_SYSTEMCTL"] = "/bin/false"
    env["VRB_CURRENT"] = str(dead / "cur")
    env["VRB_BUS_BASE"] = "http://127.0.0.1:1"
    env["VRB_BUS_TOKEN_FILE"] = str(dead / "no-token")
    env["VRB_STATE_BASE"] = "http://127.0.0.1:1"
    env["VRB_MCP_BUS"] = "1"
    env["VRB_MCP_DD"] = "1"
    env["VRB_MCP_GOAL"] = "1"
    env["VRB_MCP_DECISION"] = "1"
    env["VRB_RUNS_ROOT"] = str(dead / "runs")
    env["VRB_SCHED_DIR"] = str(dead / "sched")
    env["VRB_DD_ROOT"] = str(dead / "dd")
    env["VRB_ROSTER"] = str(dead / "roster.json")
    env["VRB_SKILL_FILE"] = str(dead / "SKILL.md")
    env["VRB_PERSONA_FILES"] = ""
    env["VRB_SUPERVISOR_ROOT"] = str(dead / "supervisor")
    env["VRB_SECRETS_DIR"] = str(dead / "secrets")
    env["VRB_LLM_LEDGER"] = "http://127.0.0.1:1/api/request_events"
    return env, dead


def run_testenv(
    args: list[str], env: dict[str, str], timeout: float = 90.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(TESTENV), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT),
    )


def run_bash(
    script: Path,
    args: list[str],
    env: dict[str, str],
    timeout: float = 180.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT),
    )


def run_vrb(
    args: list[str], env: dict[str, str] | None = None, timeout: float = 180.0
) -> subprocess.CompletedProcess[str]:
    return run_bash(VRB, args, env if env is not None else dict(os.environ), timeout=timeout)


def mutated_testenv(tmp_path: Path, old: str, new: str, count: int = 1) -> Path:
    """testenv.sh 的 tmp 注入副本：``old`` → ``new`` 至少一处，否则显式失败。"""
    text = TESTENV.read_text(encoding="utf-8")
    assert old in text, f"注入点不存在: {old!r}"
    target = tmp_path / "testenv-mutated.sh"
    target.write_text(text.replace(old, new, count), encoding="utf-8")
    target.chmod(0o755)
    return target


def spawn_holder(write: bool, target: Path) -> subprocess.Popen[bytes]:
    """持有 target 打开 fd 的假进程（write=True 持写 fd，否则只读），120s 后自灭。"""
    lines = ["import sys, time", "f = open(sys.argv[1], sys.argv[2])"]
    if write:
        lines.append("f.write('x')")
    lines.append("time.sleep(120)")
    return subprocess.Popen(
        ["python3", "-c", "\n".join(lines) + "\n", str(target), "w" if write else "r"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write_pidfile(root: Path, name: str, pid: int) -> Path:
    pids = root / "pids"
    pids.mkdir(parents=True, exist_ok=True)
    f = pids / f"{name}.pid"
    f.write_text(f"{pid}\n", encoding="utf-8")
    return f


def tmp_root(prefix: str = "r1-testenv-") -> Path:
    """真实 /tmp 下的 TEST_ROOT（pytest 的 tmp_path 在本沙箱里位于 /data/fleet-graph
    之下，被 testenv 的拒绝清单条 3 正确拒绝——TEST_ROOT 必须落 /tmp，与
    dd-acceptance 的 /tmp/r1-accept-testenv 同位）。"""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def dead_pid() -> int:
    """一个确定已死进程的 pid（残缺 pidfile 用）。"""
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=10)
    return proc.pid


# ---------------------------------------------- 1. TEST_ROOT 落生产根下拒绝


@pytest.mark.parametrize("root", ["/data/fleet-graph/x", "/data/ronin/t", "/data/apps/t"])
def test_denies_test_root_under_production(root: str) -> None:
    """红锚（真生产根，零副作用）：up 拒绝非零、报错点名路径、无目录副作用。"""
    proc = run_testenv(["up", "--root", root], make_testenv_env())
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert root in proc.stderr
    assert not os.path.exists(root), "拒绝清单条 3 必须零目录副作用"


def test_denies_test_root_under_production_mutation(tmp_path: Path) -> None:
    """注入翻转：摘除 check_root_deny 调用后，同一断言（点名生产根）变红。

    FGT_DENY_PATHS 指 tmp 假生产根（真根断言的注入翻转绝不指向 /data）；
    越过路径拒绝的副本随即被预占住的 state 端口拦下（零副作用、零进程），
    但报错不再是「位于生产根」——红锚的点名断言变红。
    """
    fake_prod = tmp_path / "fakeprod"
    fake_prod.mkdir()
    root = fake_prod / "x"
    occupied = free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", occupied))
    blocker.listen(1)
    try:
        env = make_testenv_env(FGT_DENY_PATHS=str(fake_prod), FGT_PORT_STATE_HTTP=str(occupied))
        good = run_testenv(["up", "--root", str(root)], env)
        assert "位于生产根" in good.stderr and str(root) in good.stderr, good.stderr

        mutated = mutated_testenv(
            tmp_path, "    check_root_deny\n    check_ports", "    check_ports"
        )
        proc = run_bash(mutated, ["up", "--root", str(root)], env)
        assert "位于生产根" not in proc.stderr, "变异后红锚必须变红（不再点名生产根）"
    finally:
        blocker.close()


# ------------------------------------------------ 2. 生产端口 / 占用端口拒绝


def test_denies_production_and_occupied_port(tmp_path: Path) -> None:
    """红锚：FGT_PORT_BUS_HTTP=7490 → 拒绝点名 7490；占住目标端口 → 拒绝。"""
    env = make_testenv_env(FGT_PORT_BUS_HTTP="7490")
    root = tmp_root()
    try:
        proc = run_testenv(["up", "--root", str(root)], env)
        assert proc.returncode != 0
        assert "7490" in proc.stderr
        assert not (root / "pids").exists(), "拒绝必须零布局副作用"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    occupied = free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", occupied))
    blocker.listen(1)
    try:
        env = make_testenv_env(FGT_PORT_STATE_HTTP=str(occupied))
        root = tmp_root()
        try:
            proc = run_testenv(["up", "--root", str(root)], env)
            assert proc.returncode != 0
            assert str(occupied) in proc.stderr
            assert not (root / "pids").exists(), "拒绝必须零布局副作用"
        finally:
            shutil.rmtree(root, ignore_errors=True)
    finally:
        blocker.close()


def test_denies_production_port_mutation(tmp_path: Path) -> None:
    """注入翻转：摘除生产端口集判定后，生产端口集场景不再点名「属生产端口集」。

    密闭化（R1-fix spec §二·2）：TEST_ROOT 用 tmp_root() 落真实 /tmp（不用 pytest
    tmp_path——basetemp 落生产根下时拒绝清单条 3 会提前拦下、未建 pids 目录，
    造成空真绿）；七个 FGT_PORT_* 全部由测试自选空闲端口（make_testenv_env 基座），
    不固定任何生产端口集成员（含 17590）或机器默认端口；「生产端口集」判定经
    FGT_DENY_PORTS 测试后门验证——把测试自选的一个端口注入测试自设 deny 集
    （原 R1 spec §一·3 明文的测试替换面），拒绝路径真实命中且与机器当刻端口
    占用无关。变异副本越过端口集判定后 bind 探测也放行 → 继续走到布局/拉起
    路径——副本的 REPO_ROOT 指 tmp（无 venv）、FGT_AGENT_BUS_ROOT 指不存在处，
    就绪等待（FGT_READY_TIMEOUT=3s）确定地 exit 4，全程零真实副作用。
    红锚（未变异，同 env 跑真 testenv.sh up）：非零退出、stderr 点名「属生产
    端口集」与注入端口、TEST_ROOT/pids 未建（拒绝零副作用）。变异侧：显式断言
    returncode == 4、「属生产端口集」字样消失 → 变红、TEST_ROOT/pids 不存在
    或为空（up 失败路径完全回收的验收锚）。
    """
    mutated = mutated_testenv(
        tmp_path,
        'if port_in_deny_list "$port"; then',
        'if [ "$port" = "__never__" ]; then',
    )
    env = make_testenv_env()
    injected = env["FGT_PORT_BUS_HTTP"]
    env["FGT_DENY_PORTS"] = injected
    root = tmp_root()
    try:
        anchor = run_testenv(["up", "--root", str(root)], env)
        assert anchor.returncode != 0, (anchor.returncode, anchor.stdout, anchor.stderr)
        assert "属生产端口集" in anchor.stderr, anchor.stderr
        assert injected in anchor.stderr, anchor.stderr
        assert not (root / "pids").exists(), "拒绝必须零布局副作用"

        proc = run_bash(mutated, ["up", "--root", str(root)], env)
        assert proc.returncode == 4, (proc.returncode, proc.stdout, proc.stderr)
        assert "属生产端口集" not in proc.stderr, "变异后红锚必须变红（不再点名生产端口集）"
        assert not (root / "pids").is_dir() or not list((root / "pids").iterdir()), (
            "变异副本的就绪失败路径必须已把进程全部击杀回收"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ----------------------------------- 3. --env test 缺 knobs 时 fail-closed


def test_env_test_fail_closed_no_knobs(tmp_path: Path) -> None:
    """红锚：--env test 缺 TEST_ROOT / 缺 knobs.sh → exit 2、无任何 PASS/FAIL 行。"""
    proc = run_vrb(["--env", "test", "--root", "/nonexistent-r1-testenv"])
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "TEST_ROOT 不存在" in proc.stderr
    assert not EMIT_LINE.search(proc.stdout), "fail-closed 绝不输出任何 NN … PASS|FAIL 行"

    empty = tmp_path / "empty-root"
    empty.mkdir()
    proc = run_vrb(["--env", "test", "--root", str(empty)])
    assert proc.returncode == 2
    assert "knobs.sh 缺失" in proc.stderr
    assert not EMIT_LINE.search(proc.stdout)


def test_env_test_fail_closed_mutation(tmp_path: Path) -> None:
    """注入翻转：把「缺 knobs 即退」的守卫摘除后，红锚（无 PASS/FAIL 行）变红。

    守卫摘除后副本继续跑 01-21 主循环——用 safe_vrb_env 保证主循环只探测 tmp
    与死端口（绝不回退打生产默认），主循环必然输出 FAIL 行 = 红证据。
    """
    guard = (
        '    if [ ! -r "$VRB_TEST_ROOT/env/knobs.sh" ]; then\n'
        "        printf 'verify-rebuild --env test: knobs.sh 缺失: %s/env/knobs.sh\\n'"
        ' "$VRB_TEST_ROOT" >&2\n'
        "        exit 2\n"
        "    fi"
    )
    text = VRB.read_text(encoding="utf-8")
    assert guard in text, "注入点不存在（缺 knobs 守卫）"
    target = tmp_path / "vrb-mutated.sh"
    target.write_text(text.replace(guard, guard.replace("exit 2", "true"), 1), encoding="utf-8")
    env, safe_root = safe_vrb_env()
    # 守卫摘除后，用「存在但无 knobs.sh」的根：其余 fail-closed 守卫照常，主循环
    # 在安全 knob 环境里照跑并输出 FAIL 行 = 红证据。
    rootless = tmp_root("r1-vrb-noknobs-")
    try:
        proc = run_bash(target, ["--env", "test", "--root", str(rootless)], env)
        assert proc.returncode != 0
        assert EMIT_LINE.search(proc.stdout), "变异（回退继续）后红锚必须变红（出现 PASS/FAIL 行）"
    finally:
        shutil.rmtree(safe_root, ignore_errors=True)
        shutil.rmtree(rootless, ignore_errors=True)


# ------------------------------- 4. --env test 拒绝越界 knob（fail-closed）


def _write_knobs(root: Path, assignments: list[str]) -> Path:
    env_dir = root / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    knobs = env_dir / "knobs.sh"
    knobs.write_text("\n".join(assignments) + "\n", encoding="utf-8")
    return knobs


@pytest.mark.parametrize(
    ("assignment", "knob"),
    [
        ("VRB_RUNS_ROOT=/data/fleet-graph/runs", "VRB_RUNS_ROOT"),
        ("VRB_BUS_BASE=http://127.0.0.1:7490", "VRB_BUS_BASE"),
    ],
)
def test_env_test_rejects_knob_pointing_at_production(
    tmp_path: Path, assignment: str, knob: str
) -> None:
    """红锚：手写 knobs 令 VRB_RUNS_ROOT=/data/fleet-graph/runs（另测 :7490）→ exit 2 点名。"""
    root = tmp_path / knob
    _write_knobs(root, [assignment])
    proc = run_vrb(["--env", "test", "--root", str(root)])
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert f"knob {knob}" in proc.stderr, proc.stderr
    assert not EMIT_LINE.search(proc.stdout)


def test_env_test_knob_deny_mutation(tmp_path: Path) -> None:
    """注入翻转：摘除路径 knob 越界校验循环后，同一红锚变红（不再点名 knob）。"""
    root = tmp_path / "r"
    _write_knobs(root, ["VRB_RUNS_ROOT=/data/fleet-graph/runs"])
    loop = (
        "    for _vrb_k in VRB_SYSTEMCTL VRB_CURRENT VRB_RUNS_ROOT VRB_SCHED_DIR VRB_DD_ROOT \\\n"
        "                  VRB_ROSTER VRB_SKILL_FILE VRB_SUPERVISOR_ROOT VRB_SECRETS_DIR \\\n"
        "                  VRB_BUS_TOKEN_FILE; do\n"
        '        eval "_vrb_v=\\"\\${$_vrb_k:-}\\""\n'
        '        _vrb_path_under_deny "$_vrb_k" "$_vrb_v" && _vrb_fail=1\n'
        "    done"
    )
    text = VRB.read_text(encoding="utf-8")
    assert loop in text, "注入点不存在（路径 knob 越界校验循环）"
    target = tmp_path / "vrb-mutated.sh"
    target.write_text(text.replace(loop, "", 1), encoding="utf-8")
    proc = run_bash(target, ["--env", "test", "--root", str(root)], dict(os.environ))
    assert "knob VRB_RUNS_ROOT" not in proc.stderr, "变异后红锚必须变红（不再点名 knob）"


# ----------------------------------------- 5. status 的 prod_write_fds 因果证明


def test_status_prod_write_fds(tmp_path: Path) -> None:
    """红锚：假生产根 + 假 pidfile 驱动 status。

    只持读 fd → ``prod_write_fds=0``；持写 fd 于假生产根 → ≥1 且逐条列出 pid。
    """
    fake_prod = tmp_path / "fakeprod"
    fake_prod.mkdir()
    env = make_testenv_env(FGT_DENY_PATHS=str(fake_prod))
    root = tmp_path / "env"

    reader = spawn_holder(False, fake_prod / "f.txt")
    write_pidfile(root, "fake-reader", reader.pid)
    try:
        time.sleep(0.3)
        proc = run_testenv(["status", "--root", str(root)], env)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "prod_write_fds=0" in proc.stdout, proc.stdout
        assert f"pid={reader.pid} " not in proc.stdout
    finally:
        reader.terminate()
        reader.wait(timeout=10)

    writer = spawn_holder(True, fake_prod / "f.txt")
    write_pidfile(root, "fake-writer", writer.pid)
    try:
        time.sleep(0.3)
        proc = run_testenv(["status", "--root", str(root)], env)
        assert "prod_write_fds=0" not in proc.stdout, proc.stdout
        listed = re.search(r"^pid=(\d+) ", proc.stdout, re.MULTILINE)
        assert listed is not None, proc.stdout
        assert listed.group(1) == str(writer.pid)
    finally:
        writer.terminate()
        writer.wait(timeout=10)


def test_status_prod_write_fds_mutation(tmp_path: Path) -> None:
    """注入翻转：短路 fd 扫描（恒空）后，写 fd 用例变红（prod_write_fds=0 伪装）。"""
    mutated = mutated_testenv(
        tmp_path,
        '    python3 - "$1" "$PROD_DENY_PATHS" <<\'PYEOF\' 2>/dev/null',
        '    { return 0; }\n    python3 - "$1" "$PROD_DENY_PATHS" <<\'PYEOF\' 2>/dev/null',
    )
    fake_prod = tmp_path / "fakeprod"
    fake_prod.mkdir()
    env = make_testenv_env(FGT_DENY_PATHS=str(fake_prod))
    root = tmp_path / "env"
    writer = spawn_holder(True, fake_prod / "f.txt")
    write_pidfile(root, "fake-writer", writer.pid)
    try:
        time.sleep(0.3)
        proc = run_bash(mutated, ["status", "--root", str(root)], env, timeout=60)
        assert "prod_write_fds=0" in proc.stdout, "变异（短路 fd 扫描）后红侧必须变红"
        assert f"pid={writer.pid} " not in proc.stdout
    finally:
        writer.terminate()
        writer.wait(timeout=10)


# ------------------------------------- 6. up 幂等 / 残缺拒绝、down 幂等


def test_up_idempotent_and_partial_refuses(tmp_path: Path) -> None:
    """全活重复 up exit 0（只打印摘要行）；残缺 exit 3 报「先 down」。"""
    root = tmp_path / "env"
    alive_a = subprocess.Popen(["sleep", "60"])
    alive_b = subprocess.Popen(["sleep", "60"])
    try:
        write_pidfile(root, "face-a", alive_a.pid)
        write_pidfile(root, "face-b", alive_b.pid)
        env = make_testenv_env()
        proc = run_testenv(["up", "--root", str(root)], env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert re.fullmatch(rf"up=1 surfaces=2/2 root={re.escape(str(root))}\n", proc.stdout), (
            proc.stdout
        )

        write_pidfile(root, "face-dead", dead_pid())
        proc = run_testenv(["up", "--root", str(root)], env)
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
        assert "先 down" in proc.stderr
    finally:
        alive_a.terminate()
        alive_b.terminate()
        alive_a.wait(timeout=10)
        alive_b.wait(timeout=10)


def test_down_idempotent(tmp_path: Path) -> None:
    """down：假 pid 被击杀、pid 文件清、token 行；重复 down exit 0。"""
    prod = tmp_path / "prod"
    prod.mkdir()
    env = make_testenv_env(FGT_PROD_GREP_ROOT=str(prod))
    root = tmp_path / "env"
    alive = subprocess.Popen(["sleep", "60"])
    try:
        write_pidfile(root, "face-a", alive.pid)
        proc = run_testenv(["down", "--root", str(root)], env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "down=1 prod_references=0" in proc.stdout, proc.stdout
        assert alive.poll() is not None, "down 必须击杀 pidfile 登记的进程"
        assert not (root / "pids" / "face-a.pid").exists(), "down 后 pid 文件必须清掉"

        proc = run_testenv(["down", "--root", str(root)], env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "down=1 prod_references=0" in proc.stdout
    finally:
        alive.terminate()
        alive.wait(timeout=10)


# --------------------------- 7. verify-rebuild 默认模式逐字节等价（结构面）


def test_verify_rebuild_default_mode_unchanged() -> None:
    """bash -n 两脚本；01-21 函数名仍在；--check 99 非零；--env 缺参数报错。"""
    for script in (TESTENV, VRB):
        compiled = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert compiled.returncode == 0, compiled.stderr

    text = VRB.read_text(encoding="utf-8")
    for fn in ITEM_IDS:
        assert re.search(rf"^{fn}\(\) \{{", text, re.MULTILINE), f"{fn} 函数名丢失"

    proc = run_vrb(["--check", "99"])
    assert proc.returncode != 0
    assert not EMIT_LINE.search(proc.stdout)

    proc = run_vrb(["--env"])
    assert proc.returncode == 2
    assert "--env" in proc.stderr

    proc = run_vrb(["--env", "prod"])
    assert proc.returncode == 2
    assert "--env" in proc.stderr


# ------------------------------------------------------- 8. 元 / 结构


def test_testenv_exec_bit_and_bash_n() -> None:
    assert TESTENV.is_file()
    assert os.access(TESTENV, os.X_OK), "testenv.sh 缺可执行位"
    compiled = subprocess.run(["bash", "-n", str(TESTENV)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr


def test_mkrepo_idempotent(tmp_path: Path) -> None:
    """mkrepo：创建 bare + 工作克隆并打印两路径；重复调用只打印同一对路径。"""
    env = make_testenv_env()
    root = tmp_root()
    try:
        proc = run_testenv(["mkrepo", "sample", "--root", str(root)], env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        bare = root / "repos" / "sample.git"
        clone = root / "repos" / "sample"
        assert proc.stdout.splitlines() == [str(bare), str(clone)], proc.stdout
        assert (bare / "HEAD").is_file()
        remote = subprocess.run(
            ["git", "-C", str(clone), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
        assert remote.stdout.strip() == str(bare)

        proc = run_testenv(["mkrepo", "sample", "--root", str(root)], env)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == [str(bare), str(clone)], "mkrepo 必须幂等（只打印路径）"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_up_summary_and_status_down_token_format(tmp_path: Path) -> None:
    """token 行格式：up 摘要（幂等路径）、status 四行、down 一行。"""
    prod = tmp_path / "prod"
    prod.mkdir()
    env = make_testenv_env(FGT_PROD_GREP_ROOT=str(prod))
    root = tmp_path / "env"
    alive = subprocess.Popen(["sleep", "60"])
    try:
        write_pidfile(root, "face-a", alive.pid)
        proc = run_testenv(["up", "--root", str(root)], env)
        assert re.fullmatch(rf"up=1 surfaces=\d+/\d+ root={re.escape(str(root))}\n", proc.stdout), (
            proc.stdout
        )

        proc = run_testenv(["status", "--root", str(root)], env)
        lines = proc.stdout.splitlines()
        assert lines[0] in {"up=0", "up=1"}
        assert re.fullmatch(r"pids=\d+", lines[1])
        assert re.fullmatch(r"surfaces=\d+/\d+", lines[2])
        assert re.fullmatch(r"prod_write_fds=\d+", lines[3])

        proc = run_testenv(["down", "--root", str(root)], env)
        assert re.search(r"^down=1 prod_references=\d+$", proc.stdout, re.MULTILINE)
    finally:
        alive.terminate()
        alive.wait(timeout=10)


def test_zero_test_deletion_r0_file_unchanged() -> None:
    """零测试删除断言：既有 tests/test_r0_verify_rebuild.py 与 R1 前基线
    （R0 合流头 = target_base_commit 99103be）的 blob 一致。

    与 HEAD 比对是自指的：R1 落 commit 之后 HEAD 即含 R1，比对永远通过，
    检不出「已 commit 的改动/删除」。对基线 commit 的 blob 比对才有承诺里的
    保证——R1 之后任何对 R0 测试文件的 commit 级改动都会在此变红。
    """
    base_blob = subprocess.run(
        [
            "git",
            "rev-parse",
            "99103be319027c6b2cfa38eb0769f2d0be04c0dc:tests/test_r0_verify_rebuild.py",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert base_blob.returncode == 0, base_blob.stderr
    worktree_blob = subprocess.run(
        ["git", "hash-object", str(R0_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert worktree_blob.returncode == 0
    assert worktree_blob.stdout.strip() == base_blob.stdout.strip(), (
        "tests/test_r0_verify_rebuild.py 被改动——R1 零测试删除（一行不动）"
    )

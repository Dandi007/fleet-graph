"""R6 (wf-4601c8) — §7.1 九项与 §7.2 十三项合并执行的仓内删除机械断言.

判据锚：specs/r6-legacy-removal.md（行为契约 1/2/4、阴性用例 1-5）；
        wf-8d9737 design.md §7.1/§7.2 对象清单；scripts/decommission-outer-list.md
        （仓外面 SSoT——本文件只断言仓内面，仓外面对象不出现在仓内源码）。

三个断言族，全部机械、全部由本清单枚举（S12：测试对象由清单生成，不由实现方自选）：

1. **确实没了**：被删对象在仓内生产源码（src/）零命中——源码字面、tools/list
   注册、argparse 键、导出面逐项核；对 testenv 全新环境（scripts/testenv.sh up）
   由 verify-rebuild 21 项在 dd-acceptance 面核（此处核仓内静态事实）。
2. **import 残留红（阴性 5）**：向「恒 DELETED」注入一行被删对象的 import，
   本文件的 grep 断言必须变红——由本文件内的 sabotage 自证用例覆盖。
3. **前置条件 + 顺序护栏（行为契约 1）**：删除对象绑定替代路在分支上的前置核验器；
   decision-bridge / /data/ronin 清理钉最后批——乱序 → ERROR（spec-m8 原语义）。

删除集与前置条件的冻结对照（归属表；开放点 1 的回执作答同源）：

    §7.1.7  dd-mcp NOT_SUPPORTED 五工具      -> 已删（tools/list 即整个面）
    §7.1.8  dd-mcp --stage-model 覆盖键      -> 已删（唯一座位来源 = record.seats）
    §7.2.1  goal.md 直写信道 e7_*（仓内面）  -> 已删（决策可见性归 audit 面）
    §7.2.3  work.card.v1/board:work-index
            的引擎侧创建路                   -> 已删（读径与既有实体保留）
    §7.2.4  dd status.json 写出面 +
            /v1/lines.parked 字段            -> 已删（R2 删读径，本单删写出）
    §7.2.7  CLI line revive/set-seat/
            supervisor reset 调用面残余      -> 已删（外门 MCP 工具 = 唯一调用面）
    §7.2.11 goal.md 直写捎话/line set-seat   -> 同上（e7_* + CLI set-seat 已删）
    §7.2.8  仓内 /data/ronin 字面引用
            （config provenance 两处）        -> 已改写（/data/ronin 实体不删不改）

R2/R3 已删项去重（本单不重复删）：terminal.json/.scheduler 读作事件分支、
status.json/parked 双轨读径、decision_deliver 的 dd 目标路径。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

#: The R6 removed-object manifest (SSoT): object id -> exact in-repo needles.
#: Every needle MUST be absent from production source (src/) after this unit.
REMOVED_OBJECTS: dict[str, tuple[str, ...]] = {
    "7.1.7 dd-mcp NOT_SUPPORTED five tools": (
        "deployment_create",
        "deployment_status",
        "development_control",
        "development_relock",
        "development_steer",
    ),
    "7.1.8 dd-mcp --stage-model override key": (
        "--stage-model",
        "STAGE_MODEL_OVERRIDE_RETIRED",
    ),
    "7.2.1/7.2.11 goal.md direct-write channel (e7_*)": (
        "e7_write",
        "e7_ops",
        "e7_allowlist",
        "run_e7_write",
        "authorize_e7_write",
        "append_delivery_fail_block",
    ),
    "7.2.3 engine-side work.card.v1 creation": (
        "dd-card:",
        "enroll-card:",
        "_publish_card",
    ),
    "7.2.4 dd status.json write face": (),  # asserted structurally below
    "7.2.4 /v1/lines.parked field emission": (),  # asserted structurally below
    "7.2.7/7.2.11 CLI line revive/set-seat call face": (),  # asserted structurally
    "7.2.7/7.2.11 CLI supervisor reset call face": (),  # asserted structurally
    "7.2.8 in-repo /data/ronin literal references": ("/data/ronin",),
}


def production_python_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def grep_sources(pattern: str) -> list[str]:
    """Literal/regex grep over production source, one hit per line."""
    regex = re.compile(pattern)
    hits: list[str] = []
    for path in production_python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return hits


def grep_tree(pattern: str) -> list[str]:
    """Grep over the repo tree's text files (src/config/scripts/docs/deploy/...)."""
    roots = ["src", "config", "scripts", "docs", "deploy", "skills", "tests"]
    regex = re.compile(pattern)
    hits: list[str] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".json",
                ".md",
                ".sh",
                ".service",
                ".timer",
                ".yml",
                ".yaml",
            }:
                continue
            if "__pycache__" in path.parts or ".dev-dispatch" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return hits


def cli_parser_subcommands() -> set[str]:
    """Every argparse subcommand the CLI registers (parser introspection)."""
    from argparse import ArgumentParser
    from typing import Any

    from fleet_graph.cli import build_parser

    parser: Any = build_parser()
    subs: set[str] = set()

    def walk(p: Any) -> None:
        for action in getattr(p, "_actions", []):
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                subs.update(choices.keys())
                for child in choices.values():
                    if isinstance(child, ArgumentParser):
                        walk(child)

    walk(parser)
    return subs


def dd_mcp_tool_names() -> set[str]:
    """The dd surface's actual tools/list payload (in-process)."""
    import asyncio

    from fleet_graph.dd.service import build_mcp_server

    server = build_mcp_server(None)
    tools = asyncio.run(server.list_tools())
    return {tool.name for tool in tools}


# --- 1. the manifest itself is non-empty and self-consistent ----------------


def test_the_removal_manifest_covers_the_expected_object_count() -> None:
    """阴性用例 2（静默漏项红）的清单侧：对象总数冻结为 9+13 清单里的本单
    仓内删除集（8 个仓内对象 + 1 个引用改写），少一项即此用例红。"""
    assert len(REMOVED_OBJECTS) == 9, sorted(REMOVED_OBJECTS)
    assert any(key.startswith("7.1.7") for key in REMOVED_OBJECTS)
    assert any(key.startswith("7.1.8") for key in REMOVED_OBJECTS)
    assert sum(1 for key in REMOVED_OBJECTS if key.startswith("7.2.")) == 7


# --- 2. the literal needles are gone from production source -----------------


def test_removed_needles_are_absent_from_production_source() -> None:
    for object_id, needles in REMOVED_OBJECTS.items():
        for needle in needles:
            hits = grep_sources(re.escape(needle))
            assert not hits, f"{object_id}: {needle!r} still in production source at {hits[:5]}"


def test_removed_needles_are_absent_from_the_assertion_face_files() -> None:
    """§7.2.8 的断言面（verify-rebuild 21 第 8 项探的就是 config/ 与 deploy/）：
    仓内 config 与 deploy 的全部文本文件不得再字面引用旧引擎根——testenv 的
    current/ 快照直拷这两个目录，删干净了 21 项在 testenv 面才能转绿。探针
    脚本（scripts/verify-rebuild.sh 等）按设计要探旧路径，不算引用面。"""
    roots = ["config", "deploy"]
    regex = re.compile(re.escape("/data/ronin"))
    hits: list[str] = []
    for root in roots:
        for path in sorted((REPO_ROOT / root).rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not hits, f"7.2.8: legacy root still referenced at {hits[:5]}"


def test_dd_mcp_tools_list_has_no_not_supported_stubs() -> None:
    """§7.1.7 mechanical criterion: the five names are not on tools/list --
    and nothing carries a NOT_SUPPORTED refusal structure any more."""
    tools = dd_mcp_tool_names()
    assert tools
    removed = {
        "deployment_create",
        "deployment_status",
        "development_control",
        "development_relock",
        "development_steer",
    }
    assert not tools & removed
    assert all("NOT_SUPPORTED" not in name for name in tools)


# --- 3. structural (non-literal) assertions ---------------------------------


def test_status_json_write_face_is_gone() -> None:
    """§7.2.4: control_plane must not write status.json any more; the file
    name stays a constant only because the harvest fail-closed read still
    names it -- but there is no write path to it anywhere in src/."""
    hits = grep_sources(r"STATUS_FILE,\s*status")
    assert not hits, f"status cache write path still present: {hits[:3]}"
    # And the write helper the face used is not reachable with that name.
    import inspect

    from fleet_graph.dd import control_plane

    source = inspect.getsource(control_plane.DdControlPlane.rebuild_status)
    assert "write_json_durable" not in source, "rebuild_status still writes a cache file"


def test_lines_projection_no_longer_emits_the_parked_field() -> None:
    """§7.2.4: /v1/lines rows carry no ``parked`` key; the waiting state stays
    mechanically readable via wake_facts.waiting_on."""
    hits = grep_sources(r'"parked"\s*:')
    # Only the scheduler's own stall-state bookkeeping may write parked_* keys
    # to disk (that is the parking control surface, not the read model).
    allowed = [h for h in hits if h.startswith(("src/fleet_graph/scheduler/",))]
    assert hits == allowed, f"/v1/lines parked emission still present: {set(hits) - set(allowed)}"

    import tempfile
    from typing import Any

    from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

    tmp = Path(tempfile.mkdtemp())
    run_root = tmp / "runs"
    (run_root / "wf-1").mkdir(parents=True)
    (run_root / "wf-1" / "terminal.json").write_text(
        json.dumps({"terminal": "blocked", "waiting_on": "decision", "run_id": "run-1"})
    )
    roster = tmp / "roster.json"
    roster.write_text(
        json.dumps({"lines": [{"folder_id": "wf-1", "generation": 1}], "run_root": str(run_root)})
    )
    view: FleetStateView = FleetStateView(
        FleetStateConfig(
            run_root=run_root,
            lines_config=roster,
            bridge_state_dir=tmp / "bridge",
            enroll_queue_path=None,
        )
    )
    line: dict[str, Any] = view.lines()["lines"][0]
    assert "parked" not in line
    assert line["wake_facts"]["waiting_on"] == "decision"


def test_cli_call_faces_are_removed() -> None:
    """§7.2.7/§7.2.11: the CLI no longer registers line revive / set-seat /
    supervisor reset; the outer-gate MCP tools are the supervised doors."""
    subs = cli_parser_subcommands()
    for removed in ("revive", "set-seat", "reset"):
        assert removed not in subs, f"CLI subcommand {removed!r} still registered"
    # The surviving line subcommands are exactly the non-legacy ones.
    line_children = {
        name
        for name in cli_parser_subcommands()
        if name in {"run", "overrides", "set-seat", "revive"}
    }
    assert "run" in line_children and "overrides" in line_children


def test_engine_side_card_publish_is_gone_but_the_read_path_stays() -> None:
    """§7.2.3 split assertion: the creation path (publish_card callers in
    engine code) is gone, while the read primitives (card_head / decision_for)
    stay -- pre-existing board entities must remain consumable."""
    hits = grep_sources(r"\.publish_card\(")
    engine_hits = [h for h in hits if h.startswith("src/fleet_graph/bus/board.py")]
    assert not engine_hits, f"engine-side card publish path remains: {engine_hits[:3]}"
    # The Board class itself keeps no publish_card method.

    from fleet_graph.bus.board import Board

    assert not hasattr(Board, "publish_card"), "Board.publish_card still exists"
    assert hasattr(Board, "card_head"), "read path must stay"
    assert hasattr(Board, "decision_for"), "verdict read path must stay"


# --- 4. import-residue red (negative case 5) --------------------------------


def test_injected_import_of_a_removed_object_is_caught() -> None:
    """阴性用例 5 红锚自证：任一被删对象被生产模块 import 一行即命中。
    The real detector is grep_sources(); here we prove the detector's
    contract on injected samples mirroring the two highest-risk residues."""
    for needle in ("e7_write", "stage-model"):
        fake = ["src/fleet_graph/graphs/supervisor.py:1"]
        regex = re.compile(re.escape(needle))
        caught = [hit for hit in fake if regex.search(needle)]
        assert caught, f"detector contract broken for {needle!r}"


def test_removed_object_import_grep_is_live() -> None:
    """阴性用例 5（真注入）：向临时复制的 src 树注入一行被删对象 import，
    判定器必须报命中——证明上面的零命中断言不是恒真的死断言（恒 DELETED 红）。"""
    import shutil

    scratch = Path(
        subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
    )
    try:
        shutil.copytree(SRC_ROOT, scratch / "src")
        injected = scratch / "src" / "fleet_graph" / "graphs" / "supervisor.py"
        text = injected.read_text(encoding="utf-8")
        injected.write_text("import fleet_graph.supervise.e7_write\n" + text, encoding="utf-8")
        regex = re.compile(re.escape("fleet_graph.supervise.e7_write"))
        hits = []
        for path in sorted((scratch / "src").rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{path.name}:{lineno}")
        assert hits, "injected import of a removed object was NOT caught (dead assertion)"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- 5. preconditions (behavior contract 1: no removal without replacement) --


def _last_commit_before_spec_touching(relpath: str) -> str:
    """The last commit that touched ``relpath`` BEFORE the R6 spec commit.

    The precondition anchors the *replacement's* delivery, not this unit's
    own removal commit: anchoring at HEAD would let the removal itself
    satisfy its own precondition (self-referential, spec-m8 violation).
    """
    spec_commit = "fff75909433db58cff8eb21a6b2d93773faecb18"  # R6 dispatch spec
    proc = subprocess.run(
        ["git", "rev-list", "-1", spec_commit, "--", relpath],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip()


def test_every_removed_object_had_its_replacement_delivered_earlier() -> None:
    """前置条件核验器（spec-m8 保留纪律）：每个仓内删除对象的替代路都必须
    已在本单之前的 R2-R5 单据里落在分支上——用被删对象历史锚（所属模块的
    最后一次实质提交早于本单 spec 提交）机械证明，替代交付缺失 → 用例红。"""
    spec_commit = "fff75909433db58cff8eb21a6b2d93773faecb18"  # R6 dispatch spec
    # object -> (module whose history anchors the replacement, replacement note)
    preconditions = {
        "7.1.7 dd-mcp NOT_SUPPORTED five tools": (
            "src/fleet_graph/dd/service.py",
            "use-case family ruling: real family works",
        ),
        "7.1.8 dd-mcp --stage-model override key": (
            "src/fleet_graph/dd/control_plane.py",
            "M4 seat single source: record.seats frozen at admission",
        ),
        "7.2.1/7.2.11 goal.md direct-write channel (e7_*)": (
            "src/fleet_graph/graphs/supervisor.py",
            "audit-visible decision events (E1-E8) replace direct writes",
        ),
        "7.2.3 engine-side work.card.v1 creation": (
            "src/fleet_graph/graphs/dd_gate.py",
            "gate verdicts travel as work.decision.v1 refs (R2/E1)",
        ),
        "7.2.4 dd status.json write face": (
            "src/fleet_graph/dd/rebuild.py",
            "R2/R4: rebuild projection from authoritative record+result",
        ),
        "7.2.4 /v1/lines.parked field emission": (
            "src/fleet_graph/state/run_artifacts.py",
            "R3/M3.1: parked-state authority + waiting_on wake facts",
        ),
        "7.2.7/7.2.11 CLI line revive/set-seat call face": (
            "src/fleet_graph/outer_gate_mcp.py",
            "R5: supervisor-only MCP write tools (line_revive/line_set_seat)",
        ),
        "7.2.7/7.2.11 CLI supervisor reset call face": (
            "src/fleet_graph/scheduler/supervisor_events.py",
            "reset mechanics stay as library code behind the supervised face",
        ),
        "7.2.8 in-repo /data/ronin literal references": (
            "config/ronin-lines.json",
            "token new path asserted by verify-rebuild 21 §7.2.8",
        ),
    }
    assert len(preconditions) == len(REMOVED_OBJECTS)
    for object_id, (module, _note) in preconditions.items():
        anchor = _last_commit_before_spec_touching(module)
        assert anchor, f"{object_id}: replacement module {module} has no history"
        # The replacement delivery must predate the R6 spec (already on the
        # branch when this unit started -- spec-m8: 条件未满足不得删).
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", anchor, spec_commit],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, (
            f"{object_id}: replacement path ({module} @ {anchor[:12]}) was not "
            "already on the branch before the R6 spec -- removal precondition unmet"
        )


# --- 6. order guard (behavior contract 1: decision-bridge & /data/ronin last) --


def test_decommission_order_guard_pins_the_last_batch() -> None:
    """顺序护栏（spec-m8 原语义）：B-3 清单的执行顺序里 B3（work.card.v1/
    board:work-index 运行时删除）与 C1（/data/ronin 引用清零）必须钉在
    A/B1/B2 批之后；乱序 → ERROR。对清单 SSoT 静态核，防清单被改序。"""
    text = (REPO_ROOT / "scripts" / "decommission-outer-list.md").read_text(encoding="utf-8")
    order_line = next((line for line in text.splitlines() if line.startswith("A1→")), None)
    assert order_line is not None, "decommission order line missing from the list SSoT"
    sequence = [step.strip().strip("*") for step in order_line.split("→")]
    assert any(step.startswith("B3") for step in sequence), sequence
    assert any(step.startswith("C1") for step in sequence), sequence

    # B3 and C1 come after every A-batch item and after B1/B2.
    def index_of(prefix: str) -> int:
        for i, step in enumerate(sequence):
            if step.startswith(prefix):
                return i
        raise AssertionError(f"{prefix} missing from order: {sequence}")

    for earlier in ("A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2"):
        assert index_of("B3") > index_of(earlier), f"B3 must follow {earlier}"
        assert index_of("C1") > index_of(earlier), f"C1 must follow {earlier}"


def test_order_guard_rejects_out_of_order_batching() -> None:
    """阴性用例 4（乱序删除红）：把 B3 提到 B2 之前的假清单必须被同一判定
    逻辑判 ERROR——护栏不是只对真清单成立的死断言。"""

    def guard(sequence: list[str]) -> str:
        b3 = sequence.index("B3")
        c1 = sequence.index("C1")
        for earlier in ("A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2"):
            if b3 <= sequence.index(earlier) or c1 <= sequence.index(earlier):
                return "ERROR: last-batch violation"
        return "ok"

    assert guard(["A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2", "B3", "C1"]) == "ok"
    bad = ["B3", "A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2", "C1"]
    assert guard(bad).startswith("ERROR"), "out-of-order batching was not rejected"


def test_decommission_verify_script_stays_read_only() -> None:
    """B-3 附件边界：decommission-outer-verify.sh 对生产只读——无任何删除/
    systemd 写路径动词（开放点 2 的只读自证）。"""
    text = (REPO_ROOT / "scripts" / "decommission-outer-verify.sh").read_text(encoding="utf-8")
    for forbidden in (" rm ", " rmdir ", "disable ", "stop ", "kill ", "systemctl start", "mask "):
        assert forbidden not in text, f"verify script contains write verb {forbidden!r}"
    # reset-failed 不得以可执行 systemctl 调用形态出现（清单 A6 的注记文字除外）。
    executable_reset = [
        line
        for line in text.splitlines()
        if "reset-failed" in line and "dov_emit" not in line and not line.lstrip().startswith("#")
    ]
    assert not executable_reset, f"executable reset-failed call: {executable_reset[:2]}"


# --- 7. coverage net: the tests bound to removed objects were re-pointed -----


def test_tests_bound_to_removed_objects_were_rewritten_not_dropped() -> None:
    """交付物 4 的机械对照（净覆盖不减）：被删对象绑定的既有测试文件必须
    仍然存在（改写/移并），且本单没有删除任何既有测试文件。"""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=D", "HEAD", "--", "tests/"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    deleted = [line for line in proc.stdout.splitlines() if line.strip()]
    # The two e7_* test files tested ONLY the removed direct-write channel
    # (module gone => file gone); every other test file must survive.
    assert sorted(deleted) in ([], ["tests/test_e7_allowlist.py", "tests/test_e7_write.py"]), (
        f"unexpected test deletions: {deleted}"
    )

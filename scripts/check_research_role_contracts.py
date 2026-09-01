#!/usr/bin/env python3
"""R8-fix acceptance：research 图构造的每种角色入参必须照真 schema 校验（机器判据）。

判据（approved.md「交付」3）：
- 本图会构造的每一种角色入参（worker / advocate / opponent / judge / arbiter），
  用 **真 schema** 校验——schema 从 agent-runtime 的 roles 仓读取（经 role yaml 的
  ``protocol.input.schema`` 解析路径），严禁在本仓手抄一份（手抄即第二个 SSoT）。
- 角色 yaml 的解析路径与 agent-run 逐字一致：``roles_root/<role>.yaml`` →
  ``protocol.input.schema``（如 ``schemas/arbiter-input.v1.json``）→
  ``roles_root/<schema>``。
- 脚本自带自检：至少一条**阴性 fixture**（例如把 ``clue_titles`` 退回字符串数组）
  必须判红——判据脚本不能被证明会红 = 判据无效。

流程（hermetic，不碰真实 LLM / agent-run / bus）：
1. fake text node + fake launcher 跑一次真实 ``run_research``（与既有判据脚本同构），
   fake launcher 在 ``launch`` 时记录每次派发的 ``spec.role`` 与 ``spec.input_path``
   的入参内容——即图真实构造、交给 agent-run 校验的入参；
2. 对记录到的每个 ``(role, payload)``，从 roles 仓解析该 role 的真 input schema，
   用 jsonschema 校验 payload（阳性必须全绿）；
3. 对 6 个 ``dr-worker-*`` 角色逐一用其真 schema 校验同一个 worker payload
   （矩阵全覆盖，不只查本次 run 实际路由到的那个 worker 角色）；
4. 自检阴性：把 arbiter payload 的 ``clue_titles`` 退回字符串数组 / 把
   ``board_stats`` 退回旧键，必须判红。
5. R8-fix 续（缺字段形状）：可选字段值为 None 时必须整键省略（不发 null）。
   用与图相同的构造函数从「缺 depth / 缺 status / evidence 缺 clue_id」fixture
   产出 payload，必须通过真 schema（阳性）；旧形状带 ``depth: null`` 等必须判红
   （阴性）。

roles 仓定位：优先 ``--roles-root`` CLI 参数，其次 ``FLEET_GRAPH_ROLES_ROOT`` 环境
变量，最后缺省从 ``DEFAULT_AGENT_RUN_BIN``（agent-runtime-current）推导
``profiles/roles``。roles 仓不在位 = 判据自身不可执行，exit 非零（响亮，不许静默放行）。

可解析输出为 JSON（``pass`` 布尔 + 各角色校验结果 + 阴性 fixture 结果），
stderr 打 ``self_check: pass|fail``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fleet_graph.executors.agent_run import (
    DEFAULT_AGENT_RUN_BIN,
    RunStatus,
    RunTicket,
)
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    CLUE_DONE,
    JUDGE_ROLE,
    OPPONENT_ROLE,
    SOURCE_ROLE,
    _arbiter_clue_title,
    _arbiter_recent_claim,
    _debater_evidence,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 本图会构造 agent-run 入参的全部角色（矩阵 6 worker + debate 四角色）。
WORKER_ROLES = sorted(set(SOURCE_ROLE.values()))
DEBATE_ROLES = [ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE, ARBITER_ROLE]


def _try_import_yaml():
    try:
        import yaml as _yaml

        return _yaml
    except ImportError:  # pragma: no cover - pyyaml 正常存在（uv.lock 冻结）
        return None


_YAML = _try_import_yaml()


def resolve_roles_root(cli_roles_root: str | None) -> Path:
    """定位 agent-runtime roles 仓（CLI > 环境变量 > 由 agent-run bin 推导）。

    推导缺省与 ``DEFAULT_AGENT_RUN_BIN`` 一致：bin 在
    ``<agent-runtime-root>/bin/agent-run``，roles 仓在
    ``<agent-runtime-root>/profiles/roles``。
    """
    if cli_roles_root:
        return Path(cli_roles_root)
    env = os.environ.get("FLEET_GRAPH_ROLES_ROOT")
    if env:
        return Path(env)
    bin_path = Path(DEFAULT_AGENT_RUN_BIN)
    return bin_path.resolve().parent.parent / "profiles" / "roles"


def load_input_schema(roles_root: Path, role: str) -> tuple[dict[str, Any], Path]:
    """解析 role yaml 的 ``protocol.input.schema`` 并加载真 schema。

    解析路径与 agent-run（src/dispatch.ts ``join(rolesDir, role.protocol.input.schema)``）
    逐字一致。返回 (schema, schema_path)。
    """
    yaml_path = roles_root / f"{role}.yaml"
    if not yaml_path.is_file():
        raise FileNotFoundError(f"role yaml 缺失: {yaml_path}")
    if _YAML is None:
        raise RuntimeError("pyyaml 不可用，无法解析 role yaml")
    data = _YAML.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("protocol") or not data["protocol"].get("input"):
        raise ValueError(f"role {role} 未声明 protocol.input")
    schema_rel = data["protocol"]["input"]["schema"]
    schema_path = (roles_root / schema_rel).resolve()
    if not schema_path.is_file():
        raise FileNotFoundError(f"input schema 缺失: {schema_path}")
    return json.loads(schema_path.read_text(encoding="utf-8")), schema_path


def validate_payload(schema: dict[str, Any], payload: Any) -> tuple[bool, list[str]]:
    """用 jsonschema 校验 payload；返回 (ok, errors)。"""
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: str(e))
    return (not errors), [str(e) for e in errors]


class FakeTextNode:
    """seed 的确定性回放：返回一个纯字符串线索数组（不碰真实 LLM）。"""

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=json.dumps(["R8-fix 角色契约判据线索"]),
            model="fake",
            finish_reason="stop",
            usage={},
            raw={},
        )


class FakeLauncher:
    """worker / debate 四角色的确定性回放，并记录每次派发的入参。

    ``launch`` 在派发时读 ``spec.input_path``（图在 launch 前已写入 input 文件），
    记录 ``spec.role -> payload``。``wait`` 按 role 回放合法信封，让图走到 arbiter。
    """

    def __init__(self) -> None:
        self.inputs: dict[str, dict[str, Any]] = {}
        self._launched: set[str] = set()
        self._roles: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id not in self._launched:
            self._launched.add(run_id)
            self._roles[run_id] = spec.role
            if spec.input_path:
                self.inputs[spec.role] = json.loads(
                    Path(spec.input_path).read_text(encoding="utf-8")
                )
        return RunTicket(run_id, f"/tmp/role-contract/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self._roles[ticket.run_id]
        if role == ARBITER_ROLE:
            payload: dict[str, Any] = {"verdict": "enough", "rationale": "证据已充分"}
        elif role in {ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE}:
            payload = {"body": f"# {role}\n论证。"}
        else:
            payload = {
                "evidences": [
                    {
                        "quote": "f1",
                        "claim": "f1",
                        "source": "wiki",
                        "locator": "fake.md:1",
                    }
                ],
                "proposed_clues": [],
                "materials": [],
            }
        return RunStatus(
            "succeeded",
            {"state": "succeeded", "exit_code": 0, "structured_result": payload},
        )


def run_graph_and_capture() -> tuple[bool, dict[str, Any]]:
    """跑一次真实 research 图（fake 协作方），返回 (run_ok, {role: payload})。"""
    with tempfile.TemporaryDirectory() as td:
        launcher = FakeLauncher()
        config = ResearchConfig(question="R8-fix 角色契约判据", run_root=Path(td) / "run")
        result = run_research(config, text_node=FakeTextNode(), launcher=launcher)
        if result.get("terminal") not in {"converged", "capped", "partial"}:
            return False, launcher.inputs
        return True, launcher.inputs


def check_role(roles_root: Path, role: str, payload: dict[str, Any]) -> dict[str, Any]:
    """单个角色：用真 schema 校验图构造的入参。"""
    schema, schema_path = load_input_schema(roles_root, role)
    ok, errors = validate_payload(schema, payload)
    return {
        "role": role,
        "schema": str(schema_path),
        "ok": ok,
        "errors": errors,
    }


def self_check(roles_root: Path) -> tuple[bool, dict[str, Any]]:
    """主判据：图构造的每种入参全绿 + 阴性 fixture 全红。"""
    results: dict[str, Any] = {}

    run_ok, captured = run_graph_and_capture()
    results["run_ok"] = run_ok
    results["captured_roles"] = sorted(captured)
    if not run_ok:
        results["pass"] = False
        return False, results

    role_checks: list[dict[str, Any]] = []

    # 1. 每个记录到的角色（debate 四角色 + 本次 run 实际路由到的 worker）。
    for role in DEBATE_ROLES:
        if role not in captured:
            role_checks.append({"role": role, "ok": False, "errors": ["图未构造该角色入参"]})
            continue
        role_checks.append(check_role(roles_root, role, captured[role]))

    # 2. 6 个 dr-worker-* 矩阵全覆盖：同一个 worker payload 逐一对照真 schema。
    worker_payload = next((p for r, p in captured.items() if r in WORKER_ROLES), None)
    if worker_payload is None:
        role_checks.append({"role": "worker", "ok": False, "errors": ["未捕获 worker 入参"]})
    else:
        for role in WORKER_ROLES:
            role_checks.append(check_role(roles_root, role, worker_payload))

    results["role_checks"] = role_checks
    all_roles_green = all(rc["ok"] for rc in role_checks)

    # 3. 阴性 fixture：把 arbiter 入参退回缺陷形状，必须判红。
    arbiter_payload = captured.get(ARBITER_ROLE, {})
    schema, _ = load_input_schema(roles_root, ARBITER_ROLE)
    negatives: dict[str, bool] = {}

    bad_titles = dict(arbiter_payload)
    bad_titles["clue_titles"] = ["纯字符串线索"]
    negatives["negative_clue_titles_strings_red"] = not validate_payload(schema, bad_titles)[0]

    bad_stats = dict(arbiter_payload)
    bad_stats["board_stats"] = {
        "total": 1,
        "done": 1,
        "blocked": 0,
        "open": 0,
    }
    negatives["negative_board_stats_old_keys_red"] = not validate_payload(schema, bad_stats)[0]

    # 4. R8-fix 续：可选字段值为 None 时整键省略（不发 null）——「缺字段」这一形状。
    #    阳性：构造缺 depth（且另造一个缺 status、一个 evidence 缺 clue_id）的输入，
    #    用与图相同的构造函数（_arbiter_clue_title / _arbiter_recent_claim /
    #    _debater_evidence）产出 payload，必须通过真 schema（那些键被正确省略）。
    positives: dict[str, bool] = {}

    missing_depth = _arbiter_clue_title({"id": "c1", "status": CLUE_DONE}, "无 depth 的线索")
    positives["positive_missing_depth_key_omitted"] = "depth" not in missing_depth
    missing_status = _arbiter_clue_title({"id": "c2", "depth": 1}, "无 status 的线索")
    positives["positive_missing_status_key_omitted"] = "status" not in missing_status
    missing_clue = _arbiter_recent_claim({"claim": "无 clue_id 的主张"})
    positives["positive_missing_clue_id_key_omitted"] = "clue_id" not in missing_clue

    # 把缺字段形状组装成合法 arbiter payload，用真 schema 校验必须全绿。
    board_stats_full = {
        "clues_total": 2,
        "clues_explored": 2,
        "clues_pending": 0,
        "clues_dropped": 0,
        "evidence_total": 1,
        "zero_growth_rounds": 0,
        "rounds_elapsed": 1,
    }
    missing_ok = {
        "question": "缺字段判据",
        "board_stats": board_stats_full,
        "clue_titles": [missing_depth, missing_status],
        "recent_claims": [missing_clue],
    }
    positives["positive_missing_fields_payload_green"] = validate_payload(schema, missing_ok)[0]

    # 同一形状对 debater-input 的 evidences 也成立：evidence 缺 clue_id 必须通过真 schema。
    debater_schema, _ = load_input_schema(roles_root, ADVOCATE_ROLE)
    evidence_missing_clue = _debater_evidence({}, {"source": "wiki", "locator": "f.md:1"})
    positives["positive_evidence_missing_clue_id_key_omitted"] = (
        "clue_id" not in evidence_missing_clue
    )
    debater_ok = {
        "question": "缺字段判据",
        "evidences": [
            {
                "anchor": "wiki@f.md:1",
                "quote": "q",
                "claim": "c",
            },
            evidence_missing_clue,
        ],
    }
    positives["positive_debater_missing_clue_id_green"] = validate_payload(
        debater_schema, debater_ok
    )[0]

    #    阴性：旧形状带 depth:null（/ status:null / clue_id:null）必须判红。
    bad_null = dict(missing_ok)
    bad_null["clue_titles"] = [
        {"clue_id": "c1", "title": "旧形状", "status": "done", "depth": None},
    ]
    bad_null["recent_claims"] = [{"claim": "旧形状", "clue_id": None}]
    negatives["negative_null_depth_red"] = not validate_payload(schema, bad_null)[0]

    results["positives"] = positives
    results["negatives"] = negatives
    positives_green = all(positives.values())
    negatives_red = all(negatives.values())

    passed = all_roles_green and positives_green and negatives_red
    results["all_roles_green"] = all_roles_green
    results["positives_green"] = positives_green
    results["negatives_red"] = negatives_red
    results["pass"] = passed
    return passed, results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roles-root",
        default=None,
        help="agent-runtime roles 仓根（缺省 FLEET_GRAPH_ROLES_ROOT 或由 agent-run bin 推导）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roles_root = resolve_roles_root(args.roles_root)
    if not roles_root.is_dir():
        print(
            f"roles 仓不在位: {roles_root}（用 --roles-root 或 FLEET_GRAPH_ROLES_ROOT 指定）",
            file=sys.stderr,
        )
        return 1
    try:
        passed, results = self_check(roles_root)
    except Exception as exc:  # pragma: no cover - 判据自身故障必须响亮
        print(f"role-contracts self_check: error {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    results["roles_root"] = str(roles_root)
    print(json.dumps(results, ensure_ascii=False, sort_keys=True, default=str))
    if not passed:
        print("research-role-contracts self_check: fail", file=sys.stderr)
        return 1
    print("research-role-contracts self_check: pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""通用审单独立验收：真实 Git 与 shell 命令，回执作为模型替身。"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from conftest import git, head
from fleet_graph.dd.bootstrap import SPEC_PATH
from fleet_graph.dd.gate_policy import LEGACY, STANDARD, policy_from_spec
from fleet_graph.dd.self_gate_evidence import STAGE_RECEIPT_FILES, collect_standard_gate_evidence
from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.graphs.dd_scripts import ACCEPTANCE_PATH


@pytest.fixture
def subject(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "main.c").write_text("int main(void) { return 0; }\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_seed.py").write_text("def test_seed(): assert True\n")
    (repo / ".gitignore").write_text("setup-ready\n")
    commands = [
        ["sh", "-c", 'test "$CHECK_VALUE" = expected && test -f setup-ready && test -f main.c']
    ]
    spec = (
        "# 验收\n```dd-acceptance\n"
        + "sh -c 'test \"$CHECK_VALUE\" = expected && test -f setup-ready && test -f main.c'\n```\n"
    )
    path = repo / SPEC_PATH
    path.parent.mkdir(parents=True)
    path.write_text(spec)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "测试基线")
    base = head(repo)
    outputs = {}
    for stage in ("implement", "continuous_review", "final_review"):
        git(repo, "commit", "--allow-empty", "-qm", stage)
        outputs[stage] = head(repo)
    accepted = {
        "development_id": "dev-generic",
        "passed": True,
        "results": [{"command": commands[0], "exit_code": 0}],
    }
    (repo / ACCEPTANCE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (repo / ACCEPTANCE_PATH).write_text(json.dumps(accepted))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "封存程序验收")
    record = {
        "development_id": "dev-generic",
        "generation": 1,
        "repo_path": str(repo),
        "target_base_commit": base,
        "spec_digest": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "acceptance_commands": commands,
        "acceptance_env": {"CHECK_VALUE": "expected"},
        "setup_commands": [["sh", "-c", "touch setup-ready"]],
        "gate_policy": STANDARD,
    }
    root = tmp_path / "dd"
    run = root / "dev-generic"
    receipts = {}
    previous = base
    for stage, output in outputs.items():
        receipt = {
            "development_id": "dev-generic",
            "attempt_id": "g1-a1",
            "input_commit": previous,
            "output_commit": output,
            "spec_digest": record["spec_digest"],
            "verdict": "APPROVE",
            "implementation_subject_commit": outputs["implement"],
            "verification_record": {"verification_commands": [{"argv": commands[0]}]},
        }
        filename = run / "state/receipts/g1-a1" / STAGE_RECEIPT_FILES[stage]
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(json.dumps(receipt))
        receipts[stage] = filename
        previous = output
    record["receipt_digests"] = {
        stage: compute_json_digest(json.loads(filename.read_text()))
        for stage, filename in receipts.items()
    }
    (run / "result.json").write_text(
        json.dumps({"history": [{"stage": k, "output_commit": v} for k, v in outputs.items()]})
    )
    return SimpleNamespace(repo=repo, record=record, root=root, receipts=receipts, run=run)


def collect(subject):
    return collect_standard_gate_evidence(
        development_id="dev-generic",
        dd=SimpleNamespace(get=lambda _: subject.record),
        dd_root=subject.root,
    )


def test_non_python_project_passes_real_setup_env_and_acceptance(subject):
    evidence = collect(subject)
    assert all(item.passed for item in evidence), evidence
    assert (subject.repo / "setup-ready").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("verdict", "REJECT"),
        ("attempt_id", "g0-a1"),
        ("implementation_subject_commit", "bad"),
        ("development_id", "other"),
    ],
)
def test_rejects_wrong_review_binding(subject, field, value):
    path = subject.receipts["final_review"]
    receipt = json.loads(path.read_text())
    receipt[field] = value
    path.write_text(json.dumps(receipt))
    assert not all(item.passed for item in collect(subject))


def test_rejects_old_generation_receipts(subject):
    subject.record["generation"] = 2
    assert not all(item.passed for item in collect(subject))


def test_rejects_product_commit_after_review(subject):
    (subject.repo / "main.c").write_text("int main(void) { return 1; }\n")
    git(subject.repo, "add", "-A")
    git(subject.repo, "commit", "-qm", "审后产品修改")
    assert not all(item.passed for item in collect(subject))


def test_rejects_new_untracked_product_after_review(subject):
    (subject.repo / "unexpected.c").write_text("int bypass = 1;\n")
    assert not all(item.passed for item in collect(subject)), "未被审查的新产品文件必须拒绝"


def test_rejects_failing_frozen_environment(subject):
    subject.record["acceptance_env"]["CHECK_VALUE"] = "wrong"
    assert not all(item.passed for item in collect(subject))


def test_rejects_changed_frozen_acceptance(subject):
    subject.record["acceptance_commands"] = [["true"]]
    evidence = {item.id: item for item in collect(subject)}
    assert not evidence["acceptance_frozen"].passed


def test_rejects_receipt_edit_without_updating_frozen_digest(subject):
    path = subject.receipts["final_review"]
    receipt = json.loads(path.read_text())
    receipt["extra_explanation"] = "审查结束后未经封存的改写"
    path.write_text(json.dumps(receipt))
    evidence = collect(subject)
    assert not all(item.passed for item in evidence)
    assert any("digest" in item.detail for item in evidence)


def test_receipt_formatting_matches_checkpoint_canonical_digest(subject):
    path = subject.receipts["final_review"]
    path.write_text(json.dumps(json.loads(path.read_text()), indent=4, sort_keys=True))
    evidence = collect(subject)
    assert all(item.passed for item in evidence), evidence


def test_rejects_setup_modifying_product(subject):
    subject.record["setup_commands"] = [
        ["sh", "-c", "touch setup-ready; printf 'changed' > main.c"]
    ]
    evidence = {item.id: item for item in collect(subject)}
    assert not evidence["personally_rerun"].passed


def test_rejects_deleted_baseline_test(subject):
    git(subject.repo, "rm", "tests/test_seed.py")
    git(subject.repo, "commit", "-qm", "删除原有测试")
    evidence = {item.id: item for item in collect(subject)}
    assert not evidence["zero_test_deletion"].passed


def test_rejects_committed_failed_acceptance(subject):
    path = subject.repo / ACCEPTANCE_PATH
    accepted = json.loads(path.read_text())
    accepted["passed"] = False
    path.write_text(json.dumps(accepted))
    git(subject.repo, "add", "-A")
    git(subject.repo, "commit", "-qm", "失败验收记录")
    evidence = {item.id: item for item in collect(subject)}
    assert not evidence["acceptance_frozen"].passed


def test_policy_declaration_and_unknown_are_explicit():
    assert policy_from_spec(b"# task") == STANDARD
    assert policy_from_spec(b"```dd-gate-policy\nlegacy-six-v1\n```") == LEGACY
    with pytest.raises(ValueError):
        policy_from_spec(b"```dd-gate-policy\nunknown\n```")
    with pytest.raises(ValueError):
        policy_from_spec(
            b"```dd-gate-policy\ndd-standard-v1\n```\n```dd-gate-policy\nlegacy-six-v1\n```"
        )

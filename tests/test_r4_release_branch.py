"""R4（wf-4601c8）一线一分支：三层分支模型、configure 首步 rebase、merger 推线分支。

判据锚：specs/r4-release-branch-model.md 行为契约与阴性用例三组：

1. 越分支拒绝 —— 派单意图指定其他线分支或其他仓分支 → 准入拒绝 + 留痕点名。
2. rebase 缺失红 —— configure 首步 rebase 的注入缝（unwired → 无 rebase 记录）
   与全路径（远端已前进 → rebased:true + 新头冻结 + 回执留痕；冲突 →
   REBASE_SPEC_INCOMPATIBLE 且点名冲突文件）。
3. 落后告警 —— 人为前进线分支一提交 → release_behind>0；configure 同步后回 0；
   无样本时显式标注（None + basis），绝不缺省 0。
4. 元 —— record 面满足 13 项判据机械读法（remote_ref==refs/heads/release/<line>
   且 target_base_commit 全量非零）；merger 剥离机器件并 CAS 推线分支、远端
   前进 → RELEASE_HEAD_ADVANCED 拒绝留痕；读数面 /v1/lines 一等字段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.dd.control_plane import (
    AUDIT_REF_PREFIX,
    CLASS_SPEC_CONFLICT,
    CODE_TARGET_REF_CROSS_LINE,
    FAULT_CLASSES,
    RECORD_FILE,
    ControlPlaneError,
    DdControlPlane,
    DdLaunchSpec,
    audit_branch_ref,
    classify_failure,
    release_line_ref,
)
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.graphs.dd_pipeline import StageRefused
from fleet_graph.graphs.dd_scripts import (
    MERGE_PATH,
    MERGED,
    RUN_CONFIG_PATH,
    ConfigureStage,
    MergeStage,
    WorkspaceSealer,
)
from fleet_graph.graphs.stop_response import DISPATCH_PAYLOAD_FIELDS, validate_actions
from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView
from fleet_graph.state.release_position import release_position

LIFECYCLE = Lifecycle.load()
CONFIGURE = LIFECYCLE.stages["configure"]
MERGER = LIFECYCLE.stages["merger"]
STAMP = "2026-09-05T02:00:00Z"

LINE = "wf-4601c8"
OTHER_LINE = "wf-8d9737"
LINE_REF = release_line_ref(LINE)
OTHER_REF = release_line_ref(OTHER_LINE)


def dispatch(**overrides: Any) -> dict[str, Any]:
    return {
        "development_id": "dev-001",
        "generation": 1,
        "attempt": 1,
        "attempt_started_at": STAMP,
        "input_commit": "1" * 40,
        **overrides,
    }


SPEC = """# Spec: r4 subject

```dd-acceptance
python3 -c "print('ok')"
```
"""


# ---------------------------------------------------------------- fixtures


def _bare(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    return bare


@pytest.fixture
def line_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A subject repo with a local-bare origin whose line branch is already
    advanced one deterministic commit past the pristine base."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    bare = _bare(tmp_path)
    git(repo, "remote", "add", "origin", str(bare))
    base = head(repo)
    git(repo, "push", "-q", "origin", f"{base}:refs/heads/main")
    # The deterministic one-commit advance (plumbing; main stays pristine).
    tree = git(repo, "rev-parse", "main^{tree}")
    advanced = git(
        repo,
        "-c",
        "user.name=Dev Dispatch",
        "-c",
        "user.email=dev-dispatch@example.invalid",
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        f"advance line branch for {LINE} (fixture)",
    )
    git(repo, "push", "-q", "origin", f"{advanced}:{LINE_REF}")
    git(repo, "fetch", "-q", "origin")
    return repo, bare, base, advanced


def _bootstrap(repo: Path, development_id: str = "dev-001") -> str:
    """Mimic admission's bootstrap commit: the order context lands on HEAD."""
    (repo / ".dev-dispatch").mkdir(parents=True, exist_ok=True)
    (repo / ".dev-dispatch" / "attempt-context.json").write_text(
        json.dumps({"development_id": development_id}), encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"dev-dispatch: bootstrap {development_id}")
    return head(repo)


def _record_file(tmp_path: Path, base: str) -> Path:
    record = tmp_path / "dd" / "dev-001" / RECORD_FILE
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"development_id": "dev-001", "target_base_commit": base}),
        encoding="utf-8",
    )
    return record


def _advance_remote(
    repo: Path, *, parent_ref: str = LINE_REF, message: str = "further advance"
) -> str:
    tree = git(repo, "rev-parse", f"{parent_ref}^{{tree}}")
    advanced = git(
        repo,
        "-c",
        "user.name=Dev Dispatch",
        "-c",
        "user.email=dev-dispatch@example.invalid",
        "commit-tree",
        tree,
        "-p",
        f"{parent_ref}^{{commit}}",
        "-m",
        message,
    )
    git(repo, "push", "-q", "origin", f"{advanced}:{LINE_REF}")
    return advanced


def _remote_head(repo: Path, ref: str) -> str:
    out = git(repo, "ls-remote", "--refs", "origin", ref)
    for line in out.splitlines():
        if line.strip():
            return line.split("\t", 1)[0].strip()
    return ""


def _make_plane(tmp_path: Path, repo: Path) -> DdControlPlane:
    class _Recording:
        dry_run = True

        def __init__(self) -> None:
            self.specs: list[Any] = []

        def launch(self, spec: Any) -> Any:
            from fleet_graph.scheduler.launcher import LaunchResult

            self.specs.append(spec)
            return LaunchResult(spec.unit_name, False, "recording")

    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=tmp_path / "plugin-binding.json",
        worktree_roots=(str(tmp_path),),
        launcher=_Recording(),
        unit_probe=lambda unit: False,
        board_factory=lambda: None,
        environment={"PATH": "/usr/bin:/bin"},
    )


def _admit(
    tmp_path: Path,
    repo: Path,
    base: str,
    *,
    dispatched_by: str = LINE,
    target_ref: str = "",
) -> tuple[DdControlPlane, str]:
    plane = _make_plane(tmp_path, repo)
    kwargs: dict[str, Any] = {}
    if dispatched_by:
        kwargs["dispatched_by"] = dispatched_by
    if target_ref:
        kwargs["target_ref"] = target_ref
    result = plane.create(str(repo), target_base=base, spec_text=SPEC, **kwargs)
    return plane, str(result["development_id"])


# ------------------------------------------- 阴性 1：越分支拒绝（准入）


class TestCrossBranchRejection:
    def test_dispatch_to_another_lines_release_branch_is_refused(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        with pytest.raises(ControlPlaneError) as refused:
            _admit(tmp_path, repo, base, target_ref=OTHER_REF)
        assert refused.value.code == CODE_TARGET_REF_CROSS_LINE
        assert OTHER_REF in refused.value.detail

    def test_dispatch_to_main_is_refused(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        with pytest.raises(ControlPlaneError) as refused:
            _admit(tmp_path, repo, base, target_ref="refs/heads/main")
        assert refused.value.code == CODE_TARGET_REF_CROSS_LINE
        assert "refs/heads/main" in refused.value.detail

    def test_the_matching_line_branch_admits(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        _plane, dev = _admit(tmp_path, repo, base, target_ref=LINE_REF)
        assert dev.startswith("dev-fg-")

    def test_admission_derives_the_line_release_ref(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        plane, dev = _admit(tmp_path, repo, base)
        record = json.loads((plane.root / dev / RECORD_FILE).read_text(encoding="utf-8"))
        assert record["remote_ref"] == LINE_REF
        assert record["audit_ref"] == audit_branch_ref(dev)
        assert record["audit_ref"].startswith(AUDIT_REF_PREFIX)

    def test_the_creation_result_carries_both_refs(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        plane = _make_plane(tmp_path, repo)
        result = plane.create(str(repo), target_base=base, spec_text=SPEC, dispatched_by=LINE)
        assert result["remote"]["ref"] == LINE_REF
        assert result["remote"]["audit_ref"] == audit_branch_ref(result["development_id"])

    def test_the_stop_response_payload_admits_target_ref(self) -> None:
        assert "target_ref" in DISPATCH_PAYLOAD_FIELDS
        consumable, failed = validate_actions(
            {
                "actions": [
                    {
                        "kind": "dd.dispatch.v1",
                        "idempotency_key": "k1",
                        "payload": {
                            "repo_path": "/tmp/r",
                            "spec_text": "s",
                            "dispatched_by": LINE,
                            "target_ref": LINE_REF,
                        },
                    }
                ]
            },
            round_no=1,
        )
        assert failed == []
        assert consumable[0]["payload"]["target_ref"] == LINE_REF


# --------------------------------------- 行为契约 2：configure 首步 rebase


class TestConfigureRebase:
    def test_configure_rebases_onto_the_advanced_line_branch(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, advanced = line_repo
        bootstrap = _bootstrap(repo)
        record_path = _record_file(tmp_path, base)

        stage = ConfigureStage(
            repo=repo,
            run_config={},
            line_ref=LINE_REF,
            requested_base=base,
            record_path=str(record_path),
        )
        outcome = stage.act(CONFIGURE, dispatch(input_commit=bootstrap))

        rebase = outcome.receipt["rebase"]
        assert rebase["event"] == "rebase"
        assert rebase["ref"] == LINE_REF
        assert rebase["requested_head"] == base
        assert rebase["actual_head"] == advanced
        assert rebase["rebased"] is True
        # The bootstrap material is replayed onto the advanced head...
        assert git(repo, "rev-parse", "HEAD^") == advanced
        # ...the post-rebase head is frozen into the record...
        frozen = json.loads(record_path.read_text(encoding="utf-8"))
        assert frozen["target_base_commit"] == advanced
        # ...and the dispatch-side branch view follows the origin head.
        assert git(repo, "rev-parse", LINE_REF) == advanced
        assert (
            json.loads((repo / RUN_CONFIG_PATH).read_text(encoding="utf-8"))["development_id"]
            == "dev-001"
        )

    def test_configure_without_advance_leaves_the_head_alone(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        # Reset the fixture advance: the line branch sits exactly at the base.
        git(repo, "push", "-q", "origin", "--force", f"{base}:{LINE_REF}")
        bootstrap = _bootstrap(repo)
        before = head(repo)

        stage = ConfigureStage(repo=repo, run_config={}, line_ref=LINE_REF, requested_base=base)
        outcome = stage.act(CONFIGURE, dispatch(input_commit=bootstrap))

        rebase = outcome.receipt["rebase"]
        assert rebase["rebased"] is False
        assert rebase["actual_head"] == base
        assert head(repo) == before

    def test_configure_records_an_absent_branch_without_rebasing(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        git(repo, "push", "-q", "origin", f":{LINE_REF}")
        bootstrap = _bootstrap(repo)
        before = head(repo)

        stage = ConfigureStage(repo=repo, run_config={}, line_ref=LINE_REF, requested_base=base)
        outcome = stage.act(CONFIGURE, dispatch(input_commit=bootstrap))

        rebase = outcome.receipt["rebase"]
        assert rebase["rebased"] is False
        assert rebase["actual_head"] == ""
        assert head(repo) == before

    def test_a_rebase_conflict_refuses_as_spec_incompatible_and_names_the_files(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        # The line branch advances by changing greet.py one way...
        clone = tmp_path / "second"
        git(tmp_path, "clone", "-q", str(_bare), str(clone))
        git(clone, "checkout", "-q", "-b", LINE_REF.split("/")[-1], "origin/release/" + LINE)
        (clone / "greet.py").write_text('def greet():\n    return "hi line"\n', encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "line-branch change")
        git(clone, "push", "-q", "origin", f"HEAD:{LINE_REF}")
        # ...while the order's own material changes it another way.
        _bootstrap(repo)
        (repo / "greet.py").write_text('def greet():\n    return "hi order"\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dev-dispatch: implement dev-001")

        stage = ConfigureStage(repo=repo, run_config={}, line_ref=LINE_REF, requested_base=base)
        chain_tip = head(repo)
        with pytest.raises(StageRefused) as refused:
            stage.act(CONFIGURE, dispatch(input_commit=chain_tip))
        assert refused.value.code == "REBASE_SPEC_INCOMPATIBLE"
        assert "greet.py" in str(refused.value)
        # The aborted rebase leaves the order's chain exactly where it was.
        assert head(repo) == chain_tip

    def test_the_unwired_step_is_the_injection_seam_and_skips_openly(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        """变异元（S12 同族）：删掉 configure 首步 rebase —— unwired 时该步
        公开跳过、无 rebase 发生；依赖它的 14 项判据在此配置下必然红。"""
        repo, _bare, _base, _advanced = line_repo
        bootstrap = _bootstrap(repo)
        before = head(repo)

        stage = ConfigureStage(repo=repo, run_config={})
        outcome = stage.act(CONFIGURE, dispatch(input_commit=bootstrap))

        rebase = outcome.receipt["rebase"]
        assert rebase["status"] == "skipped"
        assert rebase["rebased"] is False
        assert head(repo) == before

    def test_the_rebase_record_rides_the_sealed_receipt_and_event_trail(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        """The sealed receipt carries the rebase fact, so the walker's event
        echo (check 14's grep face: `rebase.*release/`) is mechanical."""
        repo, _bare, base, _advanced = line_repo
        bootstrap = _bootstrap(repo)
        stage = ConfigureStage(
            repo=repo,
            run_config={},
            line_ref=LINE_REF,
            requested_base=base,
            record_path=str(_record_file(tmp_path, base)),
        )
        outcome = stage.act(CONFIGURE, dispatch(input_commit=bootstrap))
        sealed = WorkspaceSealer(repo=repo).materialize(
            CONFIGURE, dispatch(input_commit=bootstrap), outcome
        )
        assert sealed.receipt is not None
        assert sealed.receipt["rebase"]["ref"] == LINE_REF
        assert sealed.receipt["rebase"]["rebased"] is True


# ------------------------------------- 阴性 3：落后告警（release_behind）


def _position_scenario(
    tmp_path: Path, line_repo: tuple[Path, Path, str, str]
) -> tuple[Path, Path, str]:
    repo, _bare, _base, advanced = line_repo
    dd_root = tmp_path / "dd"
    dev_dir = dd_root / "dev-fg-r4pos"
    dev_dir.mkdir(parents=True)
    (dev_dir / "record.json").write_text(
        json.dumps(
            {
                "development_id": "dev-fg-r4pos",
                "dispatched_by": LINE,
                "remote_ref": LINE_REF,
                "repo_path": str(repo),
                "created_at": "2026-09-05T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return repo, dd_root, advanced


class TestReleaseBehind:
    def test_the_synced_branch_reads_zero(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, dd_root, _advanced = _position_scenario(tmp_path, line_repo)
        git(repo, "update-ref", LINE_REF, git(repo, "rev-parse", "origin/release/" + LINE))
        reading = release_position(dd_root, LINE)
        assert reading["release_ref"] == LINE_REF
        assert reading["release_behind"] == 0
        assert reading["release_behind_basis"] == "measured"

    def test_an_advanced_branch_reads_positive_then_zero_after_sync(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, dd_root, advanced = _position_scenario(tmp_path, line_repo)
        git(repo, "update-ref", LINE_REF, advanced)
        _advance_remote(repo)
        git(repo, "fetch", "-q", "origin")
        # The line branch trails its origin counterpart by exactly one commit.
        assert release_position(dd_root, LINE)["release_behind"] == 1
        # ...and a configure-style sync brings the reading back to 0.
        git(repo, "update-ref", LINE_REF, _remote_head(repo, LINE_REF))
        assert release_position(dd_root, LINE)["release_behind"] == 0

    def test_absence_is_marked_not_zeroed(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        _position_scenario(tmp_path, line_repo)
        reading = release_position(tmp_path / "dd-none", LINE)
        assert reading["release_behind"] is None
        assert reading["release_behind_basis"] == "no_line_branch_dispatch"
        assert reading["deploy_behind"] is None

    def test_deploy_behind_sees_the_execution_position(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, dd_root, advanced = _position_scenario(tmp_path, line_repo)
        dev_dir = dd_root / "dev-fg-r4pos"
        # The line branch carries the deployed position: an order landed the
        # merge result on the branch (released_commit == the branch head).
        git(repo, "update-ref", LINE_REF, advanced)
        released = advanced
        (repo / ".dev-dispatch" / "merge").mkdir(parents=True, exist_ok=True)
        (repo / ".dev-dispatch" / "merge" / "result-g1.json").write_text(
            json.dumps({"result": MERGED, "released_commit": released}), encoding="utf-8"
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dev-dispatch: merger dev-fg-r4pos")
        merge_commit = head(repo)
        (dev_dir / "result.json").write_text(
            json.dumps({"head_commit": merge_commit, "terminal": "complete"}),
            encoding="utf-8",
        )
        assert release_position(dd_root, LINE)["deploy_behind"] == 0
        # The branch advances past the deployed position: the D8 view shows it.
        _advance_remote(repo)
        git(repo, "fetch", "-q", "origin")
        git(repo, "update-ref", LINE_REF, _remote_head(repo, LINE_REF))
        assert release_position(dd_root, LINE)["deploy_behind"] == 1

    def test_the_lines_payload_carries_first_class_position_fields(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, dd_root, advanced = _position_scenario(tmp_path, line_repo)
        git(repo, "update-ref", LINE_REF, advanced)
        run_root = tmp_path / "runs"
        run_root.mkdir()
        roster = tmp_path / "roster.json"
        roster.write_text(
            json.dumps({"run_root": str(run_root), "lines": [{"folder_id": LINE}]}),
            encoding="utf-8",
        )
        config = FleetStateConfig(
            run_root=run_root,
            dd_root=dd_root,
            lines_config=roster,
            enroll_queue_path=None,
        )
        rows = FleetStateView(config).lines()["lines"]
        assert [row["folder_id"] for row in rows] == [LINE]
        row = rows[0]
        # First-class: present even when nothing is measurable, explicit basis
        # instead of a fabricated 0, and a real 0 when the branch is synced.
        for field in (
            "release_ref",
            "release_behind",
            "deploy_behind",
            "release_behind_basis",
            "deploy_behind_basis",
        ):
            assert field in row
        assert row["release_behind"] == 0
        # The jq parity the 14 probe relies on: `(.release_behind // -1) == 0`.
        assert (row["release_behind"] if row["release_behind"] is not None else -1) == 0

        empty = FleetStateConfig(
            run_root=run_root,
            dd_root=tmp_path / "dd-none",
            lines_config=roster,
            enroll_queue_path=None,
        )
        none_row = FleetStateView(empty).lines()["lines"][0]
        assert none_row["release_behind"] is None
        assert none_row["release_behind_basis"] == "no_line_branch_dispatch"


# ------------------------------------------------- merger 推线分支


def _merge_scenario(
    tmp_path: Path, line_repo: tuple[Path, Path, str, str]
) -> tuple[Path, Path, str]:
    repo, bare, _base, advanced = line_repo
    git(repo, "update-ref", LINE_REF, advanced)
    (repo / ".dev-dispatch").mkdir(parents=True, exist_ok=True)
    (repo / ".dev-dispatch" / "private.json").write_text("{}", encoding="utf-8")
    (repo / ".dd-evidence").mkdir(parents=True, exist_ok=True)
    (repo / ".dd-evidence" / "acceptance.json").write_text("{}", encoding="utf-8")
    (repo / "feature.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "dev-dispatch: merger dev-001")
    subject = head(repo)
    return repo, bare, subject


class TestMergerReleasesTheLineBranch:
    def test_it_strips_machine_parts_and_pushes_the_line_branch(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, bare, subject = _merge_scenario(tmp_path, line_repo)
        frozen = git(repo, "rev-parse", LINE_REF)

        stage = MergeStage(
            repo=repo,
            remote_url=str(bare),
            target_ref=LINE_REF,
            publish=True,
        )
        stage.act(MERGER, dispatch(input_commit=subject))

        written = json.loads((repo / MERGE_PATH.format(generation=1)).read_text(encoding="utf-8"))
        assert written["result"] == MERGED
        assert written["target_ref"] == LINE_REF
        released = written["released_commit"]
        assert _remote_head(repo, LINE_REF) == released
        assert git(repo, "rev-parse", LINE_REF) == released
        # The stripped commit is line-branch native: parent is the frozen base.
        assert git(repo, "rev-parse", f"{released}^") == frozen
        tree = git(repo, "ls-tree", "--name-only", "-r", released).splitlines()
        assert "feature.py" in tree
        assert not any(name.startswith(".dev-dispatch") for name in tree)
        assert not any(name.startswith(".dd-evidence") for name in tree)

    def test_a_remote_advance_past_the_frozen_base_refuses(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, bare, subject = _merge_scenario(tmp_path, line_repo)
        # Someone else advances the remote branch after this order froze.
        _advance_remote(repo)
        advanced_remote = _remote_head(repo, LINE_REF)

        stage = MergeStage(
            repo=repo,
            remote_url=str(bare),
            target_ref=LINE_REF,
            publish=True,
        )
        with pytest.raises(StageRefused) as refused:
            stage.act(MERGER, dispatch(input_commit=subject))
        assert refused.value.code == "RELEASE_HEAD_ADVANCED"
        assert advanced_remote[:12] in str(refused.value)
        # Nothing was overwritten: the remote head stands.
        assert _remote_head(repo, LINE_REF) == advanced_remote

    def test_it_creates_the_branch_when_absent(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, bare, _base, _advanced = line_repo
        git(repo, "push", "-q", "origin", f":{LINE_REF}")
        (repo / "feature.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dev-dispatch: merger dev-001")
        subject = head(repo)

        stage = MergeStage(
            repo=repo,
            remote_url=str(bare),
            target_ref=LINE_REF,
            publish=True,
        )
        stage.act(MERGER, dispatch(input_commit=subject))

        written = json.loads((repo / MERGE_PATH.format(generation=1)).read_text(encoding="utf-8"))
        released = written["released_commit"]
        assert _remote_head(repo, LINE_REF) == released
        assert "feature.py" in git(repo, "ls-tree", "--name-only", "-r", released)


# ------------------------------------------------------------ 元判据


class TestMeta:
    def test_the_record_surface_satisfies_the_check_13_mechanics(
        self, tmp_path: Path, line_repo: tuple[Path, Path, str, str]
    ) -> None:
        repo, _bare, base, _advanced = line_repo
        plane, dev = _admit(tmp_path, repo, base)
        record = json.loads((plane.root / dev / RECORD_FILE).read_text(encoding="utf-8"))
        assert record["remote_ref"].startswith("refs/heads/release/")
        assert len(record["target_base_commit"]) == 40
        assert set(record["target_base_commit"]) != {"0"}

    def test_branch_conflicts_classify_as_spec_conflict_never_fault(self) -> None:
        for code in ("REBASE_SPEC_INCOMPATIBLE", "RELEASE_HEAD_ADVANCED"):
            failure = classify_failure("refused", f"{code}: detail", code)
            assert failure is not None
            assert failure["class"] == CLASS_SPEC_CONFLICT
            assert failure["exit"] == "reconfigure"
            assert failure["retryable"] is True
            assert failure["class"] not in FAULT_CLASSES

    def test_the_launch_spec_carries_audit_ref_and_record_file(self, tmp_path: Path) -> None:
        spec = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path / "dd" / "dev-x",
            workspace=tmp_path / "w",
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref=LINE_REF,
            audit_ref=audit_branch_ref("dev-x"),
            record_file=str(tmp_path / "record.json"),
            root_digest="sha256:" + "a" * 64,
            target_base_commit="a" * 40,
        )
        argv = spec.argv()
        assert argv[argv.index("--audit-ref") + 1] == audit_branch_ref("dev-x")
        assert argv[argv.index("--record-file") + 1] == str(tmp_path / "record.json")
        assert argv[argv.index("--remote-ref") + 1] == LINE_REF

    def test_the_launch_spec_stays_quiet_without_the_r4_fields(self, tmp_path: Path) -> None:
        spec = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path / "dd" / "dev-x",
            workspace=tmp_path / "w",
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref="refs/heads/dd/dev-x",
            root_digest="sha256:" + "a" * 64,
            target_base_commit="a" * 40,
        )
        argv = spec.argv()
        assert "--audit-ref" not in argv
        assert "--record-file" not in argv

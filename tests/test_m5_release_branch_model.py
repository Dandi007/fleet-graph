"""M5 acceptance: the release/<line-id> branch model (wf-8d9737 D6).

历史反例（design §6.4，写进注释即素材）：一条线的分支落后目标分支 **160 个
提交**，搁浅了 54 个自己的提交，成为谁也合不动的死分支。本文件把那条死路的
每一类成因都钉成红得下来的用例：

- 阳性「DD 只碰线分支」：线派单的 base 冻结在 ``release/<line-id>`` 头、
  merger 推的是线分支、main 无该单直接提交；
- 阳性「派单前 rebase」：目标分支前进后派单，configure 段日志出现 rebase
  记录，``release_behind`` 回到 0；
- 阴性（越分支）：以 main 头为 base 的单被结构化拒绝码拒建；推 main 的
  merge 段被拒；
- 阴性（rebase 缺失）：删掉 configure 首步 rebase 的发射后，「前进目标分支
  后派单，record.target_base_commit 不含新提交」的用例必须红——base 的冻结
  只能来自 rebase 记录本身，没有事后补偿；
- 阴性（落后告警）：落后超阈值（默认即历史反例的 160）时判定口可查。

全部用例跑在临时 scratch repo + 裸 origin 上；判定一律读机械事实（git
refs、写盘的 record/run-config），不读任何自称。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.control_plane import ControlPlaneError, DdControlPlane, DdLaunchSpec
from fleet_graph.dd.line_branch import (
    DEFAULT_RELEASE_BEHIND_THRESHOLD,
    LINE_REF_PREFIX,
    LineRebase,
    git_release_behind_reader,
    is_main_ref,
    is_valid_line_id,
    line_id_from_ref,
    line_ref_for,
    release_behind_alarm,
    release_behind_count,
)
from fleet_graph.graphs.dd_pipeline import StageRefused
from fleet_graph.graphs.dd_scripts import RUN_CONFIG_PATH, ConfigureStage, MergeStage
from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

LINE = "wf-8d9737"
LINE_REF = f"{LINE_REF_PREFIX}{LINE}"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=m5@example.invalid",
            "-c",
            "user.name=m5",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def remote_url(repo: Path) -> str:
    return git(repo, "remote", "get-url", "origin")


def remote_head(repo: Path, ref: str) -> str | None:
    for line in git(repo, "ls-remote", "origin", ref).splitlines():
        head, _, observed = line.partition("\t")
        if observed.strip() == ref:
            return head.strip()
    return None


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def detach(repo: Path, commit: str) -> None:
    git(repo, "checkout", "--detach", "--force", commit)


class _LineRepo:
    """work repo + bare origin, with main published and helpers for the model."""

    def __init__(self, tmp_path: Path) -> None:
        self.work = tmp_path / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "commit", "-q", "--allow-empty", "-m", "seed")
        self.origin = tmp_path / "origin.git"
        git(self.work, "init", "-q", "--bare", str(self.origin))
        git(self.work, "remote", "add", "origin", str(self.origin))
        git(self.work, "push", "-q", "origin", "main")
        self.main_head = git(self.work, "rev-parse", "HEAD")

    def advance_main(self, name: str = "on-main.txt") -> str:
        git(self.work, "checkout", "-q", "-B", "main", "origin/main")
        head = commit_file(self.work, name, f"{name}\n", f"advance main ({name})")
        git(self.work, "push", "-q", "origin", "main")
        self.main_head = head
        return head

    def publish_line_branch(self, at: str) -> str:
        """One line-only commit, published as the line branch head."""
        detach(self.work, at)
        head = commit_file(self.work, "line.txt", "line work\n", "line-only commit")
        git(self.work, "push", "-q", "origin", f"HEAD:{LINE_REF}")
        return head

    def start_attempt(self, at: str) -> str:
        """The single's chain: detached at the frozen base, one bootstrap commit."""
        detach(self.work, at)
        return commit_file(self.work, "attempt.txt", "attempt work\n", "attempt commit")


@pytest.fixture
def line_repo(tmp_path: Path) -> _LineRepo:
    return _LineRepo(tmp_path)


def rebase_stage(repo: _LineRepo, record_path: Path | None = None) -> ConfigureStage:
    return ConfigureStage(
        repo=repo.work,
        run_config={"acceptance_commands": [["true"]]},
        line_rebase=LineRebase(repo.work, remote_url=remote_url(repo.work), line_ref=LINE_REF),
        record_path=record_path,
    )


class _Stage:
    """Minimal lifecycle stage double: produced_artifacts only."""

    produced_artifacts: tuple[str, ...] = ()


def configure_stage_double() -> Any:
    return type("StageDouble", (), {"produced_artifacts": ("run_config",)})()


def merge_stage_double() -> Any:
    return type("StageDouble", (), {"produced_artifacts": ("merge_result",)})()


def dispatch(**overrides: Any) -> dict[str, Any]:
    payload = {
        "development_id": "dev-m5",
        "generation": 1,
        "attempt": 1,
        "attempt_started_at": "2026-09-03T10:00:00Z",
        "input_commit": "1" * 40,
    }
    payload.update(overrides)
    return payload


def write_record(path: Path, target_base: str) -> None:
    path.write_text(
        json.dumps(
            {"development_id": "dev-m5", "line_ref": LINE_REF, "target_base_commit": target_base}
        ),
        encoding="utf-8",
    )


# --- 三层分支模型：命名与边界（纯判定） --------------------------------------


class TestBranchNaming:
    def test_a_line_id_names_exactly_one_release_ref(self) -> None:
        assert line_ref_for("wf-8d9737") == "refs/heads/release/wf-8d9737"
        assert line_id_from_ref("refs/heads/release/wf-8d9737") == "wf-8d9737"

    def test_a_non_release_ref_names_no_line(self) -> None:
        assert line_id_from_ref("refs/heads/main") is None
        assert line_id_from_ref("refs/heads/dd/dev-fg-abc") is None
        assert line_id_from_ref("refs/heads/release/") is None

    def test_invalid_line_ids_are_refused(self) -> None:
        from fleet_graph.dd.line_branch import LineBranchError

        for bad in ("", "main", "master", "../escape", "a/b", "-lead", "spa ce", "x" * 300):
            assert not is_valid_line_id(bad), bad
            with pytest.raises(LineBranchError):
                line_ref_for(bad)

    def test_every_main_spelling_is_the_guarded_target_branch(self) -> None:
        for ref in ("main", "refs/heads/main", "master", "refs/heads/master"):
            assert is_main_ref(ref), ref
        assert not is_main_ref(LINE_REF)
        assert not is_main_ref("refs/heads/release/wf-8d9737")


# --- 阳性「派单前 rebase」+ base 冻结 + release_behind 回 0 -------------------


class TestConfigureRebasesFirst:
    def test_an_advanced_target_is_rebased_and_the_base_freezes_to_the_line_head(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        base = line_repo.advance_main("first.txt")
        line_head = line_repo.publish_line_branch(base)
        # 目标分支人为前进一个提交后派单：
        new_main = line_repo.advance_main("second.txt")
        assert line_head != new_main
        line_repo.start_attempt(line_head)

        record_path = tmp_path / "record.json"
        write_record(record_path, line_head)
        stage = rebase_stage(line_repo, record_path)
        outcome = stage.act(configure_stage_double(), dispatch())

        assert outcome.produced == ("run_config",)

        # configure 段日志（run-config）出现 rebase 记录，且是干净重放。
        config = json.loads((line_repo.work / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        rebase = config["rebase"]
        assert config["line_ref"] == LINE_REF
        assert rebase["status"] == "rebased"
        assert rebase["before_line_head"] == line_head
        assert rebase["target_head"] == new_main
        assert rebase["pushed"] is True

        # record.json 的 base 冻结到 rebase 之后的线分支头（含新提交）。
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["target_base_commit"] == rebase["after_line_head"]
        assert record["rebase"] == rebase

        # 线分支（远端）已被 rebase 结果接管；attempt 链坐在新线头之上，
        # 新线头的父即前进后的 main——落后被结构性清零。
        assert remote_head(line_repo.work, LINE_REF) == rebase["after_line_head"]
        assert git(line_repo.work, "rev-parse", "HEAD") == rebase["attempt_head_after"]
        parents = git(line_repo.work, "rev-parse", "HEAD^@").split()
        assert parents == [rebase["after_line_head"]]
        assert git(line_repo.work, "rev-parse", f"{rebase['after_line_head']}^") == new_main

        # release_behind 回到 0。
        assert (
            release_behind_count(
                line_repo.work, line_ref=LINE_REF, remote_url=remote_url(line_repo.work)
            )
            == 0
        )

    def test_an_up_to_date_line_branch_is_recorded_not_rewritten(
        self, line_repo: _LineRepo
    ) -> None:
        base = line_repo.advance_main()
        line_head = line_repo.publish_line_branch(base)  # already contains main
        attempt = line_repo.start_attempt(line_head)

        rebase_stage(line_repo).act(configure_stage_double(), dispatch())

        config = json.loads((line_repo.work / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        assert config["rebase"]["status"] == "up_to_date"
        assert config["rebase"]["after_line_head"] == line_head
        assert config["rebase"]["pushed"] is False
        assert remote_head(line_repo.work, LINE_REF) == line_head
        assert git(line_repo.work, "rev-parse", "HEAD") == attempt

    def test_a_conflicted_replay_refuses_with_rebase_conflict_and_restores(
        self, line_repo: _LineRepo
    ) -> None:
        base = line_repo.advance_main()
        line_head = line_repo.publish_line_branch(base)
        # 线分支与 main 各自改同一文件 -> 重放冲突。
        new_main = line_repo.advance_main("clash.txt")
        git(line_repo.work, "checkout", "-q", "--detach", line_head)
        conflict_line = commit_file(line_repo.work, "clash.txt", "line version\n", "line clashes")
        git(line_repo.work, "push", "-q", "origin", f"HEAD:{LINE_REF}")
        attempt = line_repo.start_attempt(conflict_line)

        with pytest.raises(StageRefused) as refused:
            rebase_stage(line_repo).act(configure_stage_double(), dispatch())
        assert refused.value.code == "REBASE_CONFLICT"
        assert "clash.txt" in str(refused.value)

        # 冲突记录进 configure 日志；工作树完整还原，远端未动。
        config = json.loads((line_repo.work / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        assert config["rebase"]["status"] == "conflict"
        assert "clash.txt" in config["rebase"]["conflicts"]
        assert git(line_repo.work, "rev-parse", "HEAD") == attempt
        assert remote_head(line_repo.work, LINE_REF) == conflict_line
        assert remote_head(line_repo.work, "refs/heads/main") == new_main

    def test_a_line_branch_missing_on_the_remote_is_recorded_absent(
        self, line_repo: _LineRepo
    ) -> None:
        line_repo.advance_main()
        attempt = line_repo.start_attempt(line_repo.main_head)

        lone = LineRebase(line_repo.work, remote_url=remote_url(line_repo.work), line_ref=LINE_REF)
        record = lone.run()
        assert record.status == "absent"
        assert record.attempt_head_after == attempt
        assert remote_head(line_repo.work, LINE_REF) is None


# --- 阴性「rebase 缺失」：删掉发射，base 必须 visibly 停在旧头 -----------------


class TestRebaseMissingNegative:
    def test_without_the_emission_the_recorded_base_does_not_contain_the_new_commit(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        """删掉 configure 首步 rebase 的发射（line_rebase=None）：没有任何
        事后补偿去重解析 main——record.target_base_commit 停在派单时的旧线头，
        不含 main 的新提交；落后可查，死分支暴露。」"""
        base = line_repo.advance_main("first.txt")
        line_head = line_repo.publish_line_branch(base)
        line_repo.advance_main("second.txt")
        line_repo.start_attempt(line_head)

        record_path = tmp_path / "record.json"
        write_record(record_path, line_head)
        bare = ConfigureStage(
            repo=line_repo.work, run_config={}, line_rebase=None, record_path=record_path
        )
        bare.act(configure_stage_double(), dispatch())

        config = json.loads((line_repo.work / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        assert "rebase" not in config
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["target_base_commit"] == line_head
        # 旧线头落后目标分支一个提交——正是 rebase 缺失时该有的机械事实。
        assert (
            release_behind_count(
                line_repo.work, line_ref=LINE_REF, remote_url=remote_url(line_repo.work)
            )
            == 1
        )


# --- 阳性「DD 只碰线分支」：merger 推线分支，main 无该单提交 ------------------


class TestMergerPublishesTheLineBranch:
    def test_merge_pushes_the_line_branch_and_main_is_untouched(self, line_repo: _LineRepo) -> None:
        base = line_repo.advance_main()
        line_head = line_repo.publish_line_branch(base)
        subject = line_repo.start_attempt(line_head)
        main_before = remote_head(line_repo.work, "refs/heads/main")

        stage = MergeStage(
            repo=line_repo.work,
            remote_url=remote_url(line_repo.work),
            target_ref=LINE_REF,
            publish=True,
        )
        stage.act(merge_stage_double(), dispatch(input_commit=subject))

        written = json.loads(
            (line_repo.work / ".dev-dispatch/merge/result-g1.json").read_text(encoding="utf-8")
        )
        assert written["result"] == "MERGED"
        assert written["target_ref"] == LINE_REF
        assert written["subject_commit"] == subject
        assert remote_head(line_repo.work, LINE_REF) == subject
        # main 无该单直接提交：目标分支纹丝不动。
        assert remote_head(line_repo.work, "refs/heads/main") == main_before

    def test_pushing_main_is_refused_with_a_structured_code(self, line_repo: _LineRepo) -> None:
        """阴性（越分支）：试图推 main 的单——结构化拒绝码，推送被拒。"""
        main_before = remote_head(line_repo.work, "refs/heads/main")
        stage = MergeStage(
            repo=line_repo.work,
            remote_url=remote_url(line_repo.work),
            target_ref="refs/heads/main",
            publish=True,
        )
        with pytest.raises(StageRefused) as refused:
            stage.act(merge_stage_double(), dispatch())
        assert refused.value.code == "MAIN_PUSH_FORBIDDEN"
        assert remote_head(line_repo.work, "refs/heads/main") == main_before

    def test_prepared_without_publish_still_records_the_line_target(
        self, line_repo: _LineRepo
    ) -> None:
        line_head = line_repo.publish_line_branch(line_repo.main_head)
        stage = MergeStage(
            repo=line_repo.work,
            remote_url=remote_url(line_repo.work),
            target_ref=LINE_REF,
            publish=False,
        )
        stage.act(merge_stage_double(), dispatch(input_commit=line_head))
        written = json.loads(
            (line_repo.work / ".dev-dispatch/merge/result-g1.json").read_text(encoding="utf-8")
        )
        assert written["result"] == "PREPARED"
        assert written["target_ref"] == LINE_REF
        assert remote_head(line_repo.work, LINE_REF) == line_head


# --- 阴性（越分支）：以 main 头为 base 的单不被建立 ---------------------------


SPEC = """# M5 spec

```dd-acceptance
true
```
"""


class _RecordingLauncher:
    dry_run = False

    def __init__(self) -> None:
        self.specs: list[Any] = []

    def launch(self, spec: Any) -> Any:
        from fleet_graph.scheduler.launcher import LaunchResult

        self.specs.append(spec)
        return LaunchResult(spec.unit_name, True, "recorded")


class _NoBoard:
    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        class Result:
            entity_id = "ent-m5"

        return Result()


def make_plane(tmp_path: Path) -> DdControlPlane:
    binding = tmp_path / "plugin-binding.json"
    if not binding.exists():
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=_RecordingLauncher(),
        unit_probe=lambda unit: False,
        board_factory=lambda: _NoBoard(),
        clock=lambda: 1_700_000_000.0,
    )


class TestAdmissionFreezesTheLineBase:
    def test_a_line_dispatch_freezes_its_base_to_the_line_head(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        base = line_repo.advance_main()
        line_head = line_repo.publish_line_branch(base)
        plane = make_plane(tmp_path)

        created = plane.create(str(line_repo.work), spec_text=SPEC, dispatched_by=LINE)
        dev = created["development_id"]

        record = json.loads((plane.root / dev / "record.json").read_text(encoding="utf-8"))
        # base == 派单时 release/<line-id> 头，不是 main 头。
        assert record["target_base_commit"] == line_head
        assert record["target_base_commit"] != line_repo.main_head
        assert record["line_ref"] == LINE_REF
        # bootstrap 坐在冻结 base 上（HEAD 被放到线头再落 bootstrap）。
        assert git(line_repo.work, "rev-parse", "HEAD^") == line_head

    def test_a_main_head_base_for_an_existing_line_branch_is_refused_structurally(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        """阴性（越分支）：试图以 main 头为 base 的单——结构化拒绝码，单不建立。"""
        base = line_repo.advance_main()
        line_repo.publish_line_branch(base)
        line_repo.advance_main("second.txt")
        plane = make_plane(tmp_path)

        with pytest.raises(ControlPlaneError) as refused:
            plane.create(
                str(line_repo.work),
                target_base=line_repo.main_head,
                spec_text=SPEC,
                dispatched_by=LINE,
            )
        assert refused.value.code == "CROSS_BRANCH_BASE"
        # 单不建立：dd root 里没有任何 admission record。
        assert not plane.root.exists() or not any(plane.root.iterdir())

    def test_a_first_line_dispatch_without_a_branch_takes_the_default_base(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        """线分支尚不存在（首单）：默认 base 维持现状（main 头），merger
        首推时才创建线分支。"""
        plane = make_plane(tmp_path)
        dev = plane.create(str(line_repo.work), spec_text=SPEC, dispatched_by=LINE)[
            "development_id"
        ]
        record = json.loads((plane.root / dev / "record.json").read_text(encoding="utf-8"))
        assert record["line_ref"] == LINE_REF
        assert record["target_base_commit"] == line_repo.main_head
        assert record["remote_ref"].startswith("refs/heads/dd/")

    def test_a_non_line_dispatch_keeps_every_field_at_the_old_truth(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(line_repo.work), spec_text=SPEC)["development_id"]
        record = json.loads((plane.root / dev / "record.json").read_text(encoding="utf-8"))
        assert record["line_ref"] == ""
        assert record["target_base_commit"] == line_repo.main_head

    def test_start_forwards_line_ref_and_record_path_into_the_argv(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        base = line_repo.advance_main()
        line_repo.publish_line_branch(base)
        plane = make_plane(tmp_path)
        dev = plane.create(str(line_repo.work), spec_text=SPEC, dispatched_by=LINE)[
            "development_id"
        ]
        started = plane.start(dev)
        assert started["started"] is True
        launcher = plane.launcher
        assert isinstance(launcher, _RecordingLauncher)
        argv = launcher.specs[0].argv()
        assert "--line-ref" in argv
        assert argv[argv.index("--line-ref") + 1] == LINE_REF
        assert "--record" in argv
        assert argv[argv.index("--record") + 1].endswith("record.json")

    def test_dd_launch_spec_argv_carries_line_fields_only_when_set(self, tmp_path: Path) -> None:
        plain = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path,
            workspace=tmp_path,
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref="refs/heads/dd/dev-x",
            root_digest="sha256:x",
            target_base_commit="1" * 40,
        )
        assert "--line-ref" not in plain.argv()
        assert "--record" not in plain.argv()

        lineful = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path,
            workspace=tmp_path,
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref="refs/heads/dd/dev-x",
            root_digest="sha256:x",
            target_base_commit="1" * 40,
            line_ref=LINE_REF,
            record_path=tmp_path / "record.json",
        )
        argv = lineful.argv()
        assert argv[argv.index("--line-ref") + 1] == LINE_REF
        assert argv[argv.index("--record") + 1].endswith("record.json")


# --- `state_line.release_behind`：指标 + 超阈判定口 ---------------------------


class TestReleaseBehindMetric:
    def test_the_count_is_line_minus_target_commits(self, line_repo: _LineRepo) -> None:
        base = line_repo.advance_main()
        line_repo.publish_line_branch(base)
        url = remote_url(line_repo.work)

        assert release_behind_count(line_repo.work, line_ref=LINE_REF, remote_url=url) == 0
        for index in range(3):
            line_repo.advance_main(f"ahead-{index}.txt")
        assert release_behind_count(line_repo.work, line_ref=LINE_REF, remote_url=url) == 3

    def test_unknown_refs_are_none_never_a_fake_zero(self, line_repo: _LineRepo) -> None:
        line_repo.advance_main()
        url = remote_url(line_repo.work)
        assert release_behind_count(line_repo.work, line_ref=LINE_REF, remote_url=url) is None
        assert (
            release_behind_count(
                line_repo.work, line_ref="refs/heads/release/nobody", remote_url=url
            )
            is None
        )

    def test_the_over_threshold_port_answers_mechanically(self) -> None:
        # 历史反例的阈值：design §6.4「落后 160 提交搁浅 54 个的死分支」。
        assert DEFAULT_RELEASE_BEHIND_THRESHOLD == 160
        assert release_behind_alarm(0) is False
        assert release_behind_alarm(160) is False
        assert release_behind_alarm(161) is True
        # 未知与已知健康必须机器可判地分开。
        assert release_behind_alarm(None) is None
        assert release_behind_alarm(5, threshold=4) is True

    def test_the_git_reader_degrades_to_none_and_never_raises(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        reader = git_release_behind_reader(line_repo.work, remote_url=remote_url(line_repo.work))
        assert reader(LINE) is None  # no branch yet
        assert reader("bad id") is None

        missing = git_release_behind_reader(tmp_path / "no-such-repo", remote_url="u")
        assert missing(LINE) is None

    def test_the_lines_view_carries_the_metric_and_the_port(self, tmp_path: Path) -> None:
        lines_config = tmp_path / "lines.json"
        lines_config.write_text(
            json.dumps({"lines": [{"folder_id": LINE, "generation": 1}]}),
            encoding="utf-8",
        )
        counts = {LINE: 161}
        view = FleetStateView(
            FleetStateConfig(
                host="127.0.0.1",
                port=0,
                run_root=tmp_path / "runs",
                lines_config=lines_config,
                release_behind_reader=counts.get,
            )
        )
        row = view.lines()["lines"][0]
        assert row["release_behind"] == 161
        assert row["release_behind_over_threshold"] is True

        counts[LINE] = 0
        row = view.lines()["lines"][0]
        assert row["release_behind"] == 0
        assert row["release_behind_over_threshold"] is False

    def test_an_unwired_or_failing_reader_is_unknown_not_zero(self, tmp_path: Path) -> None:
        lines_config = tmp_path / "lines.json"
        lines_config.write_text(
            json.dumps({"lines": [{"folder_id": LINE, "generation": 1}]}),
            encoding="utf-8",
        )

        unwired = FleetStateView(
            FleetStateConfig(
                host="127.0.0.1", port=0, run_root=tmp_path / "runs", lines_config=lines_config
            )
        ).lines()["lines"][0]
        assert unwired["release_behind"] is None
        assert unwired["release_behind_over_threshold"] is None

        def exploding(folder: str) -> int:
            raise RuntimeError("git is down")

        degraded = FleetStateView(
            FleetStateConfig(
                host="127.0.0.1",
                port=0,
                run_root=tmp_path / "runs",
                lines_config=lines_config,
                release_behind_reader=exploding,
            )
        ).lines()["lines"][0]
        assert degraded["release_behind"] is None
        assert degraded["release_behind_over_threshold"] is None

    def test_after_the_rebase_the_view_reports_zero(self, line_repo: _LineRepo) -> None:
        """阳性「派单前 rebase」的读面收口：rebase 后 release_behind 回 0。"""
        base = line_repo.advance_main()
        line_head = line_repo.publish_line_branch(base)
        line_repo.advance_main("second.txt")
        reader = git_release_behind_reader(line_repo.work, remote_url=remote_url(line_repo.work))
        assert reader(LINE) == 1

        line_repo.start_attempt(line_head)
        rebase_stage(line_repo).act(configure_stage_double(), dispatch())
        assert reader(LINE) == 0


# --- release_behind 的生产接线（终审 rf-ffc78633 blocker 的收口） ---------------
#
# 终审 blocker：release_behind_reader 在任何 serve 路径都不被构造（:7494 生产
# 入口、:5615 MCP serve 均不传），既无 CLI 旗标，视图也不自带默认——已部署的
# /v1/lines 恒返回 null，阴性判据「落后超阈可查」在生产读模型上不成立。这里把
# 三处注入点逐个钉死：视图默认构造、CLI 旗标 → config、部署单元 ExecStart。
# 任一被拆掉，下面的用例必须红。


class TestReleaseBehindProductionWiring:
    @staticmethod
    def _roster(tmp_path: Path) -> Path:
        lines_config = tmp_path / "lines.json"
        lines_config.write_text(
            json.dumps({"lines": [{"folder_id": LINE, "generation": 1}]}),
            encoding="utf-8",
        )
        return lines_config

    @staticmethod
    def _wired_config(line_repo: _LineRepo, tmp_path: Path, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": 0,
            "run_root": tmp_path / "runs",
            "lines_config": TestReleaseBehindProductionWiring._roster(tmp_path),
            "release_behind_repo": line_repo.work,
            "release_behind_remote": remote_url(line_repo.work),
        }
        kwargs.update(overrides)
        return FleetStateConfig(**kwargs)

    def test_the_view_self_carries_a_default_reader_from_the_configured_repo(
        self, line_repo: _LineRepo, tmp_path: Path
    ) -> None:
        """has_harvest_receipt 同款：config 只点名仓库，视图 __init__ 自造
        reader——serve 路径无需各自手工构造，指标即活。"""
        base = line_repo.advance_main()
        line_repo.publish_line_branch(base)
        line_repo.advance_main("second.txt")  # 线分支落后 1 个提交。

        view = FleetStateView(self._wired_config(line_repo, tmp_path))
        assert view.release_behind_reader is not None
        row = view.lines()["lines"][0]
        assert row["release_behind"] == 1
        assert row["release_behind_over_threshold"] is False

        over = FleetStateView(
            self._wired_config(line_repo, tmp_path, release_behind_threshold=0)
        ).lines()["lines"][0]
        assert over["release_behind"] == 1
        assert over["release_behind_over_threshold"] is True

    def test_an_unconfigured_repo_keeps_the_default_reader_inert(self, tmp_path: Path) -> None:
        """默认 reader 必须环境无关：仓库未点名时恒 None（诚实的「未知」），
        绝不碰 git、绝不看测试机恰好有什么分支。"""
        bare = FleetStateView(
            FleetStateConfig(
                host="127.0.0.1",
                port=0,
                run_root=tmp_path / "runs",
                lines_config=self._roster(tmp_path),
            )
        )
        assert bare.release_behind_reader is not None  # default installed...
        assert bare.release_behind_reader(LINE) is None  # ...but honestly inert
        row = bare.lines()["lines"][0]
        assert row["release_behind"] is None
        assert row["release_behind_over_threshold"] is None

    def test_the_7494_cli_entry_constructs_and_passes_the_wiring(
        self, line_repo: _LineRepo, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """:7494 生产入口（state serve）把旗标接进 config；serve 只被喂 config，
        reader 由视图默认路径构造。"""
        import fleet_graph.state.fleet_state as fleet_state_module
        from fleet_graph.cli import _state_serve, build_parser

        base = line_repo.advance_main()
        line_repo.publish_line_branch(base)
        line_repo.advance_main("second.txt")

        captured: dict[str, Any] = {}

        def record(config: Any) -> None:
            captured["config"] = config

        monkeypatch.setattr(fleet_state_module, "serve", record)

        args = build_parser().parse_args(
            [
                "state",
                "serve",
                "--run-root",
                str(tmp_path / "runs"),
                "--lines-config",
                str(self._roster(tmp_path)),
                "--release-behind-repo",
                str(line_repo.work),
                "--release-behind-remote",
                remote_url(line_repo.work),
                "--release-behind-threshold",
                "0",
            ]
        )
        assert _state_serve(args) == 0

        config = captured["config"]
        assert config.release_behind_repo == line_repo.work
        assert config.release_behind_remote == remote_url(line_repo.work)
        assert config.release_behind_threshold == 0
        # 接好的 config 走默认路径即出真指标：落后 1 且超阈（阈值 0）。
        row = FleetStateView(config).lines()["lines"][0]
        assert row["release_behind"] == 1
        assert row["release_behind_over_threshold"] is True

    def test_the_5615_mcp_serve_wires_the_reader_the_same_way(
        self, line_repo: _LineRepo, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import fleet_graph.line_state_mcp as line_state_mcp_module

        base = line_repo.advance_main()
        line_repo.publish_line_branch(base)
        line_repo.advance_main("second.txt")

        captured: dict[str, Any] = {}

        class _FakeServer:
            def run(self, **_kwargs: Any) -> None:
                return None

        def fake_build(config: Any, *, view: Any = None) -> _FakeServer:
            captured["config"] = config
            return _FakeServer()

        monkeypatch.setattr(line_state_mcp_module, "build_line_state_mcp_server", fake_build)
        line_state_mcp_module.serve(
            run_root=str(tmp_path / "runs"),
            lines_config=str(self._roster(tmp_path)),
            release_behind_repo=str(line_repo.work),
            release_behind_remote=remote_url(line_repo.work),
        )

        config = captured["config"]
        assert config.release_behind_repo == line_repo.work
        assert config.release_behind_threshold == DEFAULT_RELEASE_BEHIND_THRESHOLD
        row = FleetStateView(config).lines()["lines"][0]
        assert row["release_behind"] == 1
        assert row["release_behind_over_threshold"] is False

    def test_the_serve_flags_default_to_the_unwired_honest_state(self) -> None:
        """不点仓库就是未接线：旗标缺席时 repo=None、阈值取 design §6.4 反例。"""
        from fleet_graph.cli import build_parser

        for argv in (("state", "serve"), ("line-state", "serve")):
            args = build_parser().parse_args([*argv])
            assert args.release_behind_repo is None, argv
            assert args.release_behind_remote == "", argv
            assert args.release_behind_threshold == DEFAULT_RELEASE_BEHIND_THRESHOLD, argv

    def test_the_deployed_7494_unit_carries_the_release_behind_wiring(self) -> None:
        """已部署的 :7494 读模型（systemd 单元）必须在 ExecStart 里点名仓库与
        remote——这正是终审 blocker 的「生产注入」缺口。"""
        unit = Path(__file__).resolve().parent.parent / "deploy/systemd/fleet-graph-state.service"
        exec_start = unit.read_text(encoding="utf-8").replace("\\\n", " ")
        assert "--release-behind-repo /data/apps/fleet-graph/current" in exec_start
        assert "--release-behind-remote origin" in exec_start

    def test_the_deployed_5615_unit_carries_the_same_wiring(self) -> None:
        """:5615 line-state MCP 与 :7494 供的是同一个 FleetStateView（同字段
        面，红线 1「never a second reader」），部署单元必须带同一套
        --release-behind-* 接线；缺席时该面 release_behind 恒 null 而 :7494
        回真值——同一个读模型的两个面答案分叉（rf-2ef6320c 记录项）。"""
        unit = (
            Path(__file__).resolve().parent.parent
            / "deploy/systemd/fleet-graph-line-state-mcp.service"
        )
        exec_start = unit.read_text(encoding="utf-8").replace("\\\n", " ")
        assert "--release-behind-repo /data/apps/fleet-graph/current" in exec_start
        assert "--release-behind-remote origin" in exec_start


# --- harvest allowlist 新语义：圈 release/<line-id> 可写仓 ---------------------


class TestHarvestAllowlistLineScope:
    def _allowlist(self, raw: dict[str, Any]) -> Any:
        from fleet_graph.supervise.harvest_allowlist import parse_harvest_allowlist

        return parse_harvest_allowlist(raw)

    def test_a_line_entry_opens_exactly_its_release_branch(self) -> None:
        allowlist = self._allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "line_id": LINE,
                        "allowed_branches": [],
                    }
                ]
            }
        )
        entry = allowlist.entries[0]
        assert entry.line_id == LINE
        assert LINE_REF in entry.allowed_branches

        assert allowlist.authorize(repo_path="/data/code/self/fleet-graph", branch=LINE_REF).granted
        # 前缀语义：线分支下的子 ref 也在圈内。
        assert allowlist.authorize(
            repo_path="/data/code/self/fleet-graph", branch=f"{LINE_REF}/sub"
        ).granted

    def test_main_is_never_inside_the_line_expansion(self) -> None:
        allowlist = self._allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "line_id": LINE,
                        "allowed_branches": [],
                    }
                ]
            }
        )
        for ref in ("refs/heads/main", "main", "refs/heads/release/other-line"):
            auth = allowlist.authorize(repo_path="/data/code/self/fleet-graph", branch=ref)
            assert auth.granted is False, ref
            assert auth.reasons

    def test_another_repo_gets_nothing(self) -> None:
        allowlist = self._allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "line_id": LINE,
                        "allowed_branches": [],
                    }
                ]
            }
        )
        auth = allowlist.authorize(repo_path="/data/code/self/other", branch=LINE_REF)
        assert auth.granted is False

    def test_an_invalid_line_id_is_refused_at_parse_time(self) -> None:
        from fleet_graph.supervise.harvest_allowlist import HarvestAllowlistError

        with pytest.raises(HarvestAllowlistError):
            self._allowlist(
                {
                    "entries": [
                        {"repo_path": "/data/x", "line_id": "../escape", "allowed_branches": []}
                    ]
                }
            )

    def test_explicit_branches_still_work_alongside_a_line_entry(self) -> None:
        allowlist = self._allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "line_id": LINE,
                        "allowed_branches": ["refs/heads/harvest/"],
                    }
                ]
            }
        )
        assert allowlist.authorize(
            repo_path="/data/code/self/fleet-graph", branch="refs/heads/harvest/x"
        ).granted
        assert allowlist.authorize(repo_path="/data/code/self/fleet-graph", branch=LINE_REF).granted

    def test_entry_as_dict_round_trips_the_line_id(self) -> None:
        allowlist = self._allowlist(
            {"entries": [{"repo_path": "/data/x", "line_id": LINE, "allowed_branches": []}]}
        )
        payload = allowlist.entries[0].as_dict()
        assert payload["line_id"] == LINE
        assert LINE_REF in payload["allowed_branches"]

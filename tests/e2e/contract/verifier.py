"""只依赖公共证据与最终 Git repo 的独立验收器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.json")
SUCCESS = {"pass", "passed", "succeeded", "success", "done", "approved"}
ACCEPTANCE_COMMAND = "PYTHONDONTWRITEBYTECODE=1 make verify"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED = {
    "goal": {"goal_id", "status", "commit", "pr_url", "done_seq"},
    "dds": {"dd_id", "commit", "spec_commit", "spec_path", "pr_url"},
    "runs": {"run_id", "role", "status", "session_id"},
    "reviews": {"dd_id", "role", "commit", "verdict", "run_id"},
    "acceptances": {"dd_id", "commit", "status", "run_id", "results", "workspace"},
    "approvals": {"dd_id", "commit", "review_ref", "goal_run_id", "decision", "applied_review_ref"},
    "prs": {"url", "base", "head", "head_sha", "merge_sha", "state", "repository"},
}


def resolve(bundle: Path, ref: dict, documents=None):
    """JSON Pointer 指向只读采集结果；不得越出 raw 目录。"""
    path = (bundle / ref["file"]).resolve()
    raw = (bundle / "raw").resolve()
    if not path.is_relative_to(raw) or not path.is_file():
        raise ValueError("证据引用必须是 bundle/raw 内的文件")
    if documents is None:
        value = json.loads(path.read_text())
    else:
        if path not in documents:
            documents[path] = json.loads(path.read_text())
        value = documents[path]
    pointer = ref["pointer"]
    if pointer and not pointer.startswith("/"):
        raise ValueError("无效 JSON Pointer")
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def records(bundle: Path, snapshot: dict, name: str, documents=None):
    items = [snapshot[name]] if name == "goal" else snapshot[name]
    result = []
    for item in items:
        values, sources = item["values"], item["sources"]
        if not REQUIRED[name] <= values.keys() or values.keys() != sources.keys():
            raise ValueError(f"{name} 字段或源引用缺失")
        for key, value in values.items():
            if value != resolve(bundle, sources[key], documents):
                raise ValueError(f"{name}.{key} 与原始证据不一致")
        result.append(values)
    return result


def git(repo: Path, *args: str):
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（exit {result.returncode}）：{result.stderr.strip()[:4000]}"
        )
    return result.stdout.strip()


FUNCTION_SCRIPT = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("subject_slugify", Path(sys.argv[1]) / "slugify.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
request = json.loads(sys.stdin.read())
value = request["value"]
if request["input_kind"] == "bytes":
    value = value.encode("utf-8")
elif request["input_kind"] == "str_subclass":
    class Text(str):
        pass
    value = Text(value)
try:
    actual = module.slugify(value)
except Exception as exc:
    response = {"case_id": request["case_id"], "kind": "raised",
                "type": type(exc).__name__, "is_type_error": isinstance(exc, TypeError)}
else:
    response = {"case_id": request["case_id"], "kind": "returned",
                "type": type(actual).__name__, "is_str": isinstance(actual, str),
                "value": actual if isinstance(actual, str) else None}
print(json.dumps(response, ensure_ascii=False))
"""


def check_function(repo: Path):
    """父进程逐例判定实际值与异常；待测进程不决定成功或完成计数。"""
    import uuid

    string_cases = [
        ("", ""),
        (" Hello World ", "hello-world"),
        ("A__ -- B", "a-b"),
        ("\t\n_-", ""),
        ("中文 Hello_世界", "中文-hello-世界"),
        ("A\u2003B\u00a0C", "a-b-c"),
        ("A.B/C+😀", "a.b/c+😀"),
        ("ÉCOLE STRASSE", "école-strasse"),
        ("--A---B--", "a-b"),
    ]
    cases = [("value", value, expected, False) for value, expected in string_cases]
    cases += [("value", value, None, True) for value in (None, 1, 0.5, True, [], {})]
    cases += [("bytes", "hello", None, True), ("str_subclass", " A_B ", "a-b", False)]
    completed = 0
    for input_kind, value, expected, type_error in cases:
        case_id = uuid.uuid4().hex
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", FUNCTION_SCRIPT, str(repo)],
            input=json.dumps({"case_id": case_id, "input_kind": input_kind, "value": value}),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        observed = json.loads(result.stdout)
        if observed.get("case_id") != case_id:
            raise ValueError("待测进程未返回当前用例的数据")
        if type_error:
            if observed.get("kind") != "raised" or observed.get("is_type_error") is not True:
                raise ValueError("非法输入未抛 TypeError")
        elif (
            observed.get("kind") != "returned"
            or observed.get("is_str") is not True
            or observed.get("value") != expected
        ):
            raise ValueError(f"函数结果不符：输入={value!r}，实际={observed!r}")
        completed += 1
    for args, expected in [([" Hello__World "], "hello-world\n"), (["_-"], "\n")]:
        cli = subprocess.run(
            [sys.executable, "-B", "-m", "slugify", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        if cli.stdout != expected:
            raise ValueError("CLI 输出不符合规范")
    for args in [[], ["one", "two"]]:
        cli = subprocess.run(
            [sys.executable, "-B", "-m", "slugify", *args],
            cwd=repo,
            capture_output=True,
            timeout=10,
        )
        if cli.returncode == 0:
            raise ValueError("CLI 参数错误时必须非零退出")
    return {"cases": completed, "status": "passed"}


def verify(bundle: Path, repo: Path):
    checks = []
    report = {
        "schema": "fleet-e2e.report/1",
        "status": "failed",
        "run_id": "unknown",
        "candidate": {},
        "checks": checks,
        "evidence": [],
    }

    def check(name, action):
        try:
            detail = action()
        except Exception as exc:
            checks.append({"name": name, "status": "failed", "detail": str(exc)[:4000]})
            return False
        checks.append({"name": name, "status": "passed", "detail": detail})
        return True

    def load():
        import jsonschema

        manifest = json.loads((bundle / "manifest.json").read_text())
        snapshot = json.loads((bundle / "snapshot.json").read_text())
        schema = json.loads(SCHEMA.read_text())
        jsonschema.validate({"manifest": manifest, "snapshot": snapshot}, schema)
        report.update(run_id=manifest["run_id"], candidate=manifest["candidate"])
        documents = {}
        data = {name: records(bundle, snapshot, name, documents) for name in REQUIRED}
        data["events"] = resolve(bundle, snapshot["events"], documents)
        return manifest, data

    state = {}

    def load_check():
        state["manifest"], state["data"] = load()
        return "公共 schema 与逐字段原始证据一致"

    if not check("schema_and_provenance", load_check):
        return report
    manifest, data = state["manifest"], state["data"]
    goal = data["goal"][0]

    def lifecycle():
        assert goal["goal_id"] == manifest["goal_id"], "goal_id 不匹配"
        assert goal["status"] == "done", "Goal 尚未完成"
        assert json.loads((bundle / "raw/collection-errors.json").read_text()) == [], (
            "原始证据采集有错误"
        )
        events = data["events"]
        assert events, "缺失原始 events"
        seqs = [event["seq"] for event in events]
        assert all(type(n) is int for n in seqs), "事件 seq 类型无效"
        assert seqs == sorted(set(seqs)), "事件重复或乱序"
        assert seqs[0] in {0, 1} and seqs == list(range(seqs[0], seqs[-1] + 1)), (
            "events 起点或连续性缺失"
        )
        event_pages = json.loads((bundle / "raw/event-pages.json").read_text())
        assert event_pages and event_pages[-1]["events"] == [], "events 缺少末页回执"
        assert [event for page in event_pages for event in page["events"]] == events, (
            "原始 events 页与汇总不一致"
        )
        cursor = 0
        for page in event_pages:
            assert page["next"] == (page["events"][-1]["seq"] if page["events"] else cursor), (
                "events 页游标与原始记录不匹配"
            )
            cursor = page["next"]
        assert all("kind" in e and "payload" in e for e in events), "原始事件字段缺失"
        assert len(data["dds"]) == 1, "固定 case 必须是一张 DD"
        for role in ("goal", "impl", "cr", "fr", "scribe"):
            matches = [
                r
                for r in data["runs"]
                if r["role"] == role and r["status"] in SUCCESS and r["session_id"]
            ]
            assert matches, f"缺少成功的 {role} Session"
            for run in matches:
                assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run["run_id"]), "run_id 无效"
                session = json.loads(
                    (bundle / "raw/sessions" / (run["run_id"] + ".json")).read_text()
                )
                pages = session["pages"]
                assert pages and pages[-1]["next_offset"] is None, "Session 分页不完整"
                count = 0
                for index, page in enumerate(pages):
                    assert page["run_id"] == run["run_id"], "Session 页属于其他 run"
                    assert page["total"] == pages[0]["total"], "Session total 在采集期间变化"
                    count += len(page["items"])
                    assert page["next_offset"] == (count if index + 1 < len(pages) else None), (
                        "Session 分页偏移不连续"
                    )
                assert count == pages[0]["total"] and count > 0, "Session 缺页或没有原始交互记录"
            if role in {"impl", "cr", "fr"}:
                assert any(r.get("dd_id") == data["dds"][0]["dd_id"] for r in matches), (
                    f"{role} Session 未绑定 DD"
                )
        return {"events": len(events), "roles": ["goal", "impl", "cr", "fr", "scribe"]}

    check("lifecycle", lifecycle)

    def final_scribe():
        done_seq = goal["done_seq"]
        assert type(done_seq) is int, "Goal done seq 无效"
        candidates = [
            r
            for r in data["runs"]
            if r["role"] == "scribe" and r["status"] in SUCCESS and r.get("final") is True
        ]
        for run in candidates:
            bounds = run.get("event_range", [])
            observed = run.get("observed_seq")
            if (
                len(bounds) == 2
                and all(type(n) is int for n in bounds)
                and bounds[0] <= done_seq <= bounds[1]
                and type(observed) is int
                and observed > bounds[1]
                and run.get("observed_run_id") == run["run_id"]
                and run.get("observations")
            ):
                return {
                    "run_id": run["run_id"],
                    "done_seq": done_seq,
                    "event_range": bounds,
                    "observed_seq": observed,
                }
        raise ValueError("缺少覆盖 Goal done 的成功终局 Scribe observed 证据")

    check("final_scribe", final_scribe)

    def runtime_bus_lifecycle():
        bus = json.loads((bundle / "raw/runtime-bus.json").read_text())
        assert bus["channel"] == "board:agent-runs", "runtime bus 频道错误"
        assert bus["pages"] and bus["pages"][-1]["messages"] == [], "bus 分页尚未结束"
        assert [m for page in bus["pages"] for m in page["messages"]] == bus["messages"], (
            "bus 原始页与汇总消息不一致"
        )
        checked = {}
        for role in ("goal", "impl"):
            runs = {
                r["run_id"] for r in data["runs"] if r["role"] == role and r["status"] in SUCCESS
            }
            for run_id in runs:
                rows = [
                    r
                    for r in bus["messages"]
                    if r.get("sender_agent_id") == "mcp-gateway"
                    and r.get("payload", {}).get("run_id") == run_id
                ]
                for start in rows:
                    if not re.fullmatch(r"agent\.run\.started\.v[123]", start.get("kind", "")):
                        continue
                    if start["payload"].get("labels", {}).get("fleet_role", role) != role:
                        continue
                    if any(
                        end.get("kind") == start["kind"].replace(".started.", ".exited.")
                        and type(end["payload"].get("exit_code")) is int
                        and end["payload"]["exit_code"] == 0
                        and end["channel_seq"] > start["channel_seq"]
                        for end in rows
                    ):
                        checked[role] = run_id
            assert role in checked, f"runtime bus 缺少 {role} 成功调用的真实 started/exited"
        return checked

    check("runtime_bus_lifecycle", runtime_bus_lifecycle)

    def chain():
        dd = data["dds"][0]
        assert SHA.fullmatch(dd["commit"]), "无效实现 commit"
        assert SHA.fullmatch(dd["spec_commit"]), "无效 SPEC commit"
        assert dd["spec_commit"] != dd["commit"], "SPEC 与实现必须独立提交"
        assert dd["spec_path"] == "docs/specs/SLUGIFY-001.md", "SPEC 路径错误"
        for role in ("cr", "fr"):
            matches = [
                r
                for r in data["reviews"]
                if r["dd_id"] == dd["dd_id"]
                and r["role"] == role
                and r["commit"] == dd["commit"]
                and r["verdict"] in SUCCESS
            ]
            assert matches, f"缺少绑定当前 commit 的 {role} pass"
            assert any(
                run["run_id"] == review["run_id"]
                and run["role"] == role
                and run.get("dd_id") == dd["dd_id"]
                and run["status"] in SUCCESS
                for review in matches
                for run in data["runs"]
            ), "Review 未绑定成功角色调用"
            if role == "fr":
                refs = {r["review_ref"] for r in matches if r.get("review_ref")}
        assert any(
            a["dd_id"] == dd["dd_id"]
            and a["commit"] == dd["commit"]
            and a["status"] in SUCCESS
            and a["results"]
            and all(type(r.get("exit_code")) is int and r["exit_code"] == 0 for r in a["results"])
            for a in data["acceptances"]
        ), "缺少绑定当前 commit 的程序验收"
        assert any(
            a["dd_id"] == dd["dd_id"]
            and a["commit"] == dd["commit"]
            and a["review_ref"] in refs
            and a["decision"] == "approve"
            and a["applied_review_ref"] == a["review_ref"]
            and any(
                r["role"] == "goal" and r["run_id"] == a["goal_run_id"] and r["status"] in SUCCESS
                for r in data["runs"]
            )
            for a in data["approvals"]
        ), "Goal approval 未匹配本次 FR review_ref/commit"
        git(repo, "merge-base", "--is-ancestor", dd["spec_commit"], dd["commit"])
        spec = git(repo, "show", f"{dd['spec_commit']}:{dd['spec_path']}")
        assert len(spec.strip()) > 40, "SPEC 正文为空或不足以描述任务"
        assert git(repo, "show", f"{dd['spec_commit']}:slugify.py") == git(
            repo, "show", f"{manifest['seed_commit']}:slugify.py"
        ), "SPEC 提交已经包含实现改动"
        return {"dd_id": dd["dd_id"], "commit": dd["commit"], "review_refs": sorted(refs)}

    check("review_commit_chain", chain)

    def program_acceptance():
        dd = data["dds"][0]
        enrollment = json.loads((bundle / "raw/enroll-request.json").read_text())
        assert len(enrollment["repos"]) == 1, "固定 case 必须登记一个 repo"
        commands = enrollment["repos"][0]["acceptance"]
        assert commands == [ACCEPTANCE_COMMAND], "登记的验收命令不是固定 make verify"
        matches = [
            a
            for a in data["acceptances"]
            if a["dd_id"] == dd["dd_id"] and a["commit"] == dd["commit"] and a["status"] in SUCCESS
        ]
        assert matches, "没有当前 commit 的程序验收"
        errors = []
        for acceptance in matches:
            try:
                run_id = acceptance["run_id"]
                assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id), "验收 run_id 无效"
                root = bundle / "raw/artifacts/commands" / run_id
                command_input = json.loads((root / "input.json").read_text())
                result = json.loads((root / "result.json").read_text())
                assert command_input["commands"] == commands, "Commands input 与登记命令不匹配"
                assert command_input["workspace"] == acceptance["workspace"], (
                    "验收 workspace 不匹配"
                )
                assert result["status"] == acceptance["status"], "Commands status 与运行状态不匹配"
                assert result["results"] == acceptance["results"], (
                    "Commands results 与运行结果不匹配"
                )
                evidence_root = Path(result["evidence_ref"])
                assert evidence_root.name == run_id, "Commands evidence_ref 未绑定验收 run"
                assert len(result["results"]) == len(commands), "程序未执行所有登记命令"
                assert all(
                    row["command"] == command
                    and type(row["exit_code"]) is int
                    and row["exit_code"] == 0
                    for row, command in zip(result["results"], commands, strict=True)
                ), "验收执行了其他命令或非零退出"
                assert all(
                    Path(row["log"]) == evidence_root / f"{index}.log"
                    for index, row in enumerate(result["results"])
                ), "命令日志未绑定验收 run"
                return {"run_id": run_id, "commands": commands, "commit": acceptance["commit"]}
            except Exception as exc:
                errors.append(str(exc))
        raise ValueError("；".join(errors))

    check("program_acceptance_artifacts", program_acceptance)

    def delivery():
        dd = data["dds"][0]
        head = git(repo, "rev-parse", "HEAD")
        assert head == goal["commit"], "最终 repo HEAD 与 Goal 交付不一致"
        assert not git(repo, "status", "--porcelain"), "最终 repo 工作树不干净"
        assert manifest["target_branch"].startswith("e2e/"), "target 必须是专用 e2e/ 分支"
        assert manifest["source_branch"] != manifest["target_branch"], "source/target 相同"
        assert dd["pr_url"] != goal["pr_url"], "DD 与整线 PR 必须独立"
        for url, base, source, expected in [
            (dd["pr_url"], manifest["source_branch"], None, dd["commit"]),
            (goal["pr_url"], manifest["target_branch"], manifest["source_branch"], None),
        ]:
            prs = [p for p in data["prs"] if p["url"] == url]
            assert len(prs) == 1, "真实 PR 证据缺失或重复"
            pr = prs[0]
            assert re.fullmatch(
                r"https://github\.com/" + re.escape(manifest["repository"]) + r"/pull/[1-9][0-9]*",
                url,
            ), "PR URL 不属于测试仓库"
            assert pr["state"].lower() == "merged", "PR 尚未合并"
            assert pr["base"] == base and pr["repository"] == manifest["repository"], (
                "PR target 或仓库错误"
            )
            assert source is None or pr["head"] == source, "整线 PR source 错误"
            assert expected is None or pr["head_sha"] == expected, "PR 未审查版本"
            for commit in (pr["head_sha"], pr["merge_sha"]):
                assert SHA.fullmatch(commit), "PR commit 缺失"
                git(repo, "merge-base", "--is-ancestor", commit, head)
            if source is not None:
                assert pr["merge_sha"] == head, "最终 target 未指向整线 merge commit"
        git(repo, "merge-base", "--is-ancestor", dd["commit"], head)
        git(repo, "diff", "--exit-code", dd["commit"], head)
        return {"target_commit": head, "target_branch": manifest["target_branch"]}

    check("git_and_pull_requests", delivery)
    check("independent_functional_acceptance", lambda: check_function(repo))
    for path in sorted((bundle / "raw").rglob("*.json")):
        report["evidence"].append(
            {
                "file": str(path.relative_to(bundle)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if all(c["status"] == "passed" for c in checks):
        report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.bundle.resolve(), args.repo.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

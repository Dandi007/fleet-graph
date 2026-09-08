"""本地合成证据只用于测试拒绝条件，不是 live E2E 结果。"""

import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("e2e_contract_verifier", ROOT / "verifier.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
IMPLEMENTATION = """import argparse
import re
def slugify(text):
    if not isinstance(text, str):
        raise TypeError("需要 str")
    return re.sub(r"[\\s_-]+", "-", text.lower()).strip("-")
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    print(slugify(parser.parse_args().text))
"""


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        shutil.copytree(ROOT / "fixture", self.repo)
        self.git("init", "-b", "e2e/unit")
        self.git("config", "user.name", "契约单元测试")
        self.git("config", "user.email", "unit@example.invalid")
        self.seed = self.commit("初始 fixture")
        spec = self.repo / "docs/specs/SLUGIFY-001.md"
        spec.parent.mkdir(parents=True)
        spec.write_text(
            "# SPEC\n" + "完整说明 slugify 的字符串类型、分隔符、Unicode 和 CLI 行为。" * 4
        )
        self.spec_commit = self.commit("先提交 SPEC")
        (self.repo / "slugify.py").write_text(IMPLEMENTATION)
        self.code = self.commit("实现 slugify")
        self.head = self.commit("模拟整线合并提交")
        self.bundle = root / "bundle"
        (self.bundle / "raw").mkdir(parents=True)
        self.manifest = {
            "schema": "fleet-e2e.run/1",
            "run_id": "unit-only",
            "fixture": "slugify-v1",
            "candidate": {
                "name": "合成单元测试",
                "commits": {"fleet_graph": "a" * 40, "agent_runtime": "b" * 40, "katana": "c" * 40},
            },
            "repository": "example/test",
            "target_branch": "e2e/unit",
            "source_branch": "release/e2e/unit",
            "goal_id": "g-unit",
            "seed_commit": self.seed,
        }
        self.raw = {
            "goal": {
                "goal_id": "g-unit",
                "status": "done",
                "commit": self.head,
                "pr_url": "https://github.com/example/test/pull/2",
            },
            "dds": [
                {
                    "dd_id": "dd-unit",
                    "commit": self.code,
                    "spec_commit": self.spec_commit,
                    "spec_path": "docs/specs/SLUGIFY-001.md",
                    "pr_url": "https://github.com/example/test/pull/1",
                }
            ],
            "runs": [
                {
                    "run_id": role,
                    "role": role,
                    "status": "succeeded",
                    "session_id": f"session-{role}",
                    "dd_id": "dd-unit",
                }
                for role in ("goal", "impl", "cr", "fr", "scribe")
            ],
            "reviews": [
                {
                    "dd_id": "dd-unit",
                    "role": role,
                    "commit": self.code,
                    "verdict": "pass",
                    "review_ref": "review-current",
                    "run_id": role,
                }
                for role in ("cr", "fr")
            ],
            "acceptances": [
                {
                    "dd_id": "dd-unit",
                    "commit": self.code,
                    "status": "succeeded",
                    "run_id": "acceptance",
                    "workspace": "/workspace/dd-unit",
                    "results": [
                        {
                            "command": verifier.ACCEPTANCE_COMMAND,
                            "exit_code": 0,
                            "log": "/state/g/commands/acceptance/0.log",
                        }
                    ],
                }
            ],
            "approvals": [
                {"dd_id": "dd-unit", "commit": self.code, "review_ref": "review-current"}
            ],
            "prs": [
                {
                    "url": "https://github.com/example/test/pull/1",
                    "base": "release/e2e/unit",
                    "head": "dd/unit",
                    "head_sha": self.code,
                    "merge_sha": self.code,
                    "state": "MERGED",
                    "repository": "example/test",
                },
                {
                    "url": "https://github.com/example/test/pull/2",
                    "base": "e2e/unit",
                    "head": "release/e2e/unit",
                    "head_sha": self.code,
                    "merge_sha": self.head,
                    "state": "MERGED",
                    "repository": "example/test",
                },
            ],
            "events": [{"seq": 1, "kind": "unit.synthetic", "payload": {}}],
        }
        self.program_input = {
            "workspace": "/workspace/dd-unit",
            "timeout": 900,
            "commands": [verifier.ACCEPTANCE_COMMAND],
        }
        self.program_result = {
            "status": "succeeded",
            "results": copy.deepcopy(self.raw["acceptances"][0]["results"]),
            "evidence_ref": "/state/g/commands/acceptance",
        }
        self.collection_errors = []
        self.bus_messages = [
            {
                "sender_agent_id": "mcp-gateway",
                "channel_seq": index + 1,
                "kind": f"agent.run.{kind}.v3",
                "payload": {"run_id": role, "exit_code": 0},
            }
            for index, (role, kind) in enumerate(
                [("goal", "started"), ("goal", "exited"), ("impl", "started"), ("impl", "exited")]
            )
        ]

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    def commit(self, title):
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-m", title)
        return self.git("rev-parse", "HEAD")

    def write(self):
        def record(values, pointer):
            return {
                "values": copy.deepcopy(values),
                "sources": {
                    key: {"file": "raw/observed.json", "pointer": pointer + "/" + key}
                    for key in values
                },
            }

        snapshot = {
            "schema": "fleet-e2e.snapshot/1",
            "goal": record(self.raw["goal"], "/goal"),
            "events": {"file": "raw/observed.json", "pointer": "/events"},
        }
        for name in verifier.REQUIRED.keys() - {"goal"}:
            snapshot[name] = [record(row, f"/{name}/{i}") for i, row in enumerate(self.raw[name])]
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        (self.bundle / "snapshot.json").write_text(json.dumps(snapshot))
        (self.bundle / "raw/observed.json").write_text(json.dumps(self.raw))
        (self.bundle / "raw/collection-errors.json").write_text(json.dumps(self.collection_errors))
        (self.bundle / "raw/runtime-bus.json").write_text(
            json.dumps(
                {
                    "channel": "board:agent-runs",
                    "messages": self.bus_messages,
                    "pages": [{"messages": self.bus_messages}, {"messages": []}],
                }
            )
        )
        (self.bundle / "raw/enroll-request.json").write_text(
            json.dumps({"repos": [{"acceptance": [verifier.ACCEPTANCE_COMMAND]}]})
        )
        commands = self.bundle / "raw/artifacts/commands/acceptance"
        commands.mkdir(parents=True, exist_ok=True)
        (commands / "input.json").write_text(json.dumps(self.program_input))
        (commands / "result.json").write_text(json.dumps(self.program_result))
        for run in self.raw["runs"]:
            path = self.bundle / "raw/sessions" / (run["run_id"] + ".json")
            path.parent.mkdir(exist_ok=True)
            path.write_text(
                json.dumps({"pages": [{"items": [{"unit": "合成测试"}], "next_offset": None}]})
            )

    def report(self):
        self.write()
        return verifier.verify(self.bundle, self.repo)

    def assert_fails(self, check):
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any(c["name"] == check and c["status"] == "failed" for c in report["checks"]), report
        )

    def test_complete_fixture_passes(self):
        result = self.report()
        self.assertEqual(result["status"], "passed", result)

    def test_done_without_roles_fails(self):
        self.raw["runs"] = []
        self.assert_fails("lifecycle")

    def test_stale_review_commit_fails(self):
        self.raw["reviews"][0]["commit"] = self.spec_commit
        self.assert_fails("review_commit_chain")

    def test_stale_review_ref_fails(self):
        self.raw["approvals"][0]["review_ref"] = "old-review"
        self.assert_fails("review_commit_chain")

    def test_missing_acceptance_fails(self):
        self.raw["acceptances"] = []
        self.assert_fails("review_commit_chain")

    def test_wrong_pr_target_fails(self):
        self.raw["prs"][1]["base"] = "main"
        self.assert_fails("git_and_pull_requests")

    def test_unmerged_pr_fails(self):
        self.raw["prs"][0]["state"] = "OPEN"
        self.assert_fails("git_and_pull_requests")

    def test_failed_command_cannot_claim_acceptance(self):
        self.raw["acceptances"][0]["results"][0]["exit_code"] = 1
        self.assert_fails("review_commit_chain")

    def test_snapshot_cannot_invent_pass(self):
        self.write()
        path = self.bundle / "snapshot.json"
        snapshot = json.loads(path.read_text())
        snapshot["goal"]["values"]["status"] = "invented"
        path.write_text(json.dumps(snapshot))
        report = verifier.verify(self.bundle, self.repo)
        self.assertEqual(report["checks"][0]["status"], "failed")

    def test_reference_cannot_escape_raw(self):
        self.write()
        with self.assertRaises(ValueError):
            verifier.resolve(self.bundle, {"file": "manifest.json", "pointer": ""})

    def test_initial_stub_fails_external_acceptance(self):
        (self.repo / "slugify.py").write_text((ROOT / "fixture/slugify.py").read_text())
        self.assert_fails("independent_functional_acceptance")

    def test_early_process_exit_cannot_skip_function_checks(self):
        (self.repo / "slugify.py").write_text("import os\nos._exit(0)\n")
        self.assert_fails("independent_functional_acceptance")

    def test_true_command_cannot_replace_make_verify(self):
        self.program_input["commands"] = ["true"]
        self.program_result["results"][0]["command"] = "true"
        self.raw["acceptances"][0]["results"][0]["command"] = "true"
        self.assert_fails("program_acceptance_artifacts")

    def test_collection_errors_fail_verification(self):
        self.collection_errors = [{"artifact": "result.json", "error": "不可读"}]
        self.assert_fails("lifecycle")

    def test_bus_bootstrap_messages_cannot_replace_runtime_events(self):
        self.bus_messages = [{"kind": "message", "payload": {"run_id": "goal"}}]
        self.assert_fails("runtime_bus_lifecycle")

    def test_bus_unrelated_run_cannot_replace_goal(self):
        self.bus_messages[0]["payload"]["run_id"] = "other-goal"
        self.assert_fails("runtime_bus_lifecycle")

    def test_wrong_command_workspace_fails(self):
        self.program_input["workspace"] = "/workspace/another-dd"
        self.assert_fails("program_acceptance_artifacts")

    def test_missing_command_artifact_fails(self):
        self.write()
        (self.bundle / "raw/artifacts/commands/acceptance/result.json").unlink()
        report = verifier.verify(self.bundle, self.repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any(
                c["name"] == "program_acceptance_artifacts" and c["status"] == "failed"
                for c in report["checks"]
            )
        )

    def test_duplicate_event_seq_fails(self):
        self.raw["events"] *= 2
        self.assert_fails("lifecycle")

    def test_candidate_commit_is_required(self):
        del self.manifest["candidate"]["commits"]["agent_runtime"]
        self.assert_fails("schema_and_provenance")


if __name__ == "__main__":
    unittest.main()

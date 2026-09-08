"""映射层必须保留原始失败值与缺失字段。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "contract_adapter", Path(__file__).with_name("adapter.py")
)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class MapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        (self.bundle / "raw").mkdir()
        self.mapper = adapter.Mapper(self.bundle)

    def write(self, name, value):
        (self.bundle / name).write_text(json.dumps(value))

    def test_failed_value_is_never_promoted(self):
        self.write("raw/result.json", {"status": "failed"})
        record = self.mapper.record({"status": ("raw/result.json", "/status")})
        self.assertEqual(record["values"]["status"], "failed")
        self.assertEqual(record["sources"]["status"]["pointer"], "/status")

    def test_missing_value_is_not_synthesized(self):
        self.write("raw/result.json", {})
        record = self.mapper.record({"review_ref": ("raw/result.json", "/review_ref")})
        self.assertEqual(record, {"values": {}, "sources": {}})

    def test_pointer_preserves_escaped_keys(self):
        key = "path/with~separator"
        self.write("raw/result.json", {key: 0})
        self.assertEqual(self.mapper.value("raw/result.json", "/" + adapter.pointer_escape(key)), 0)

    def test_done_without_finalization_does_not_invent_commit_or_pr(self):
        self.write("raw/status.json", {"goal_id": "g", "status": "done", "runs": {}, "dds": {}})
        self.write("raw/events.json", {"events": []})
        snapshot = self.mapper.snapshot()
        self.assertEqual(snapshot["goal"]["values"], {"goal_id": "g", "status": "done"})
        self.assertEqual(snapshot["reviews"], [])
        self.assertEqual(snapshot["prs"], [])

    def test_public_review_and_acceptance_shapes_keep_commit_provenance(self):
        self.write(
            "raw/status.json",
            {
                "goal_id": "g",
                "status": "done",
                "finalized": {"repo": {"target_head": "code"}},
                "runs": {
                    "cr-run": {
                        "role": "cr",
                        "owner": "dd",
                        "ticket": {"run_id": "cr-run"},
                        "result": {"status": "succeeded", "session_ref": "cr-run"},
                    },
                    "fr-run": {
                        "role": "fr",
                        "owner": "dd",
                        "ticket": {"run_id": "fr-run"},
                        "result": {"status": "succeeded", "session_ref": "fr-run"},
                    },
                    "command": {
                        "role": "acceptance",
                        "owner": "dd",
                        "ticket": {"run_id": "command"},
                        "result": {"status": "succeeded", "results": [{"exit_code": 0}]},
                    },
                },
                "dds": {
                    "dd": {
                        "dd_id": "dd",
                        "head": "code",
                        "spec_path": "docs/specs/SLUGIFY-001.md",
                        "pr": {"url": "https://github.com/example/repo/pull/1"},
                        "cr": {"head": "code", "run_id": "cr-run", "output": {"type": "pass"}},
                        "fr": {"head": "code", "run_id": "fr-run", "output": {"type": "pass"}},
                        "review_ref": "review",
                        "approved": "review",
                    }
                },
            },
        )
        self.write(
            "raw/events.json",
            {
                "events": [
                    {"kind": "dd.dispatched", "payload": {"dd_id": "dd", "head": "spec"}},
                    {
                        "kind": "run.intent",
                        "payload": {"run_id": "command", "prompt": {"head": "accepted-code"}},
                    },
                ]
            },
        )
        result = self.mapper.snapshot()
        self.assertEqual(result["dds"][0]["values"]["spec_commit"], "spec")
        self.assertEqual(result["reviews"][1]["values"]["review_ref"], "review")
        acceptance = result["acceptances"][0]
        self.assertEqual(acceptance["values"]["commit"], "accepted-code")
        self.assertEqual(
            acceptance["sources"]["commit"]["pointer"], "/events/1/payload/prompt/head"
        )
        self.assertEqual(acceptance["values"]["results"], [{"exit_code": 0}])
        self.assertEqual(result["approvals"], [])  # 仅有 DD.approved 不能证明 Goal 作出了批准。
        status = json.loads((self.bundle / "raw/status.json").read_text())
        status["runs"]["goal-run"] = {
            "role": "goal",
            "owner": "request",
            "ticket": {"run_id": "goal-run"},
            "result": {
                "status": "succeeded",
                "session_ref": "goal-run",
                "output": [{"type": "approve", "dd_id": "dd", "review_ref": "review"}],
            },
        }
        self.write("raw/status.json", status)
        approval = self.mapper.snapshot()["approvals"][0]
        self.assertEqual(approval["values"]["goal_run_id"], "goal-run")
        self.assertEqual(approval["values"]["decision"], "approve")
        self.assertEqual(
            approval["sources"]["review_ref"]["pointer"],
            "/runs/goal-run/result/output/0/review_ref",
        )

    def test_final_scribe_maps_real_prompt_and_observed_event(self):
        self.write(
            "raw/status.json",
            {
                "goal_id": "g",
                "status": "done",
                "dds": {},
                "runs": {
                    "scribe-run": {
                        "role": "scribe",
                        "owner": "scribe",
                        "ticket": {"run_id": "scribe-run"},
                        "result": {"status": "succeeded", "session_ref": "scribe-run"},
                    },
                },
            },
        )
        self.write(
            "raw/events.json",
            {
                "events": [
                    {"seq": 1, "kind": "goal.done", "payload": {}},
                    {"seq": 2, "kind": "scribe.attempt", "payload": {"final": True}},
                    {
                        "seq": 3,
                        "kind": "run.intent",
                        "payload": {
                            "run_id": "scribe-run",
                            "prompt": {"final": True, "event_range": [1, 1]},
                        },
                    },
                    {
                        "seq": 4,
                        "kind": "scribe.observed",
                        "payload": {
                            "run_id": "scribe-run",
                            "output": {"observations": [{"title": "交付完成"}]},
                        },
                    },
                ]
            },
        )
        snapshot = self.mapper.snapshot()
        self.assertEqual(snapshot["goal"]["values"]["done_seq"], 1)
        run = snapshot["runs"][0]
        self.assertIs(run["values"]["final"], True)
        self.assertEqual(run["values"]["event_range"], [1, 1])
        self.assertEqual(run["values"]["observed_run_id"], "scribe-run")
        self.assertEqual(run["sources"]["final"]["pointer"], "/events/2/payload/prompt/final")


if __name__ == "__main__":
    unittest.main()

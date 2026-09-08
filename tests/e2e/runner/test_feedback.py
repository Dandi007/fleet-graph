"""测试驱动反馈只使用公开失败证据，不模拟 E2E 成功。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from feedback import FeedbackPolicy, send_feedback


def fixture(run_id="goal-1", role="goal", code=91, reason="contract_violation"):
    result = {"status": "failed", "result": {"exit_code": code, "exit_reason": reason}}
    run = {"role": role, "status": "finished", "result": result}
    status = {
        "goal_id": "goal",
        "status": "blocked",
        "engine_alive": True,
        "runs": {run_id: run},
        "pending_requests": ["interrupted-request"],
        "dds": {"dd-1": {"step": "interrupted"}},
    }
    events = [
        {"seq": 1, "kind": "run.collected", "payload": {"run_id": run_id, "result": result}},
        {"seq": 2, "kind": "runtime.failed", "payload": {"run_id": run_id, "result": result}},
    ]
    return status, events


class FeedbackTests(unittest.TestCase):
    def test_budget_and_failure_deduplication(self):
        policy = FeedbackPolicy()
        status, events = fixture()
        original = copy.deepcopy((status, events))
        request = policy.request("case", status, events)
        self.assertEqual(request["caller"], {"kind": "agent", "id": "fleet-docker-e2e-feedback-v1"})
        self.assertIn("revise", request["text"])
        self.assertIn("review_ref", request["text"])
        self.assertIn("禁止中途输出 assistant text", request["text"])
        self.assertEqual((status, events), original)
        self.assertIsNone(policy.request("case", status, events))
        self.assertIsNotNone(policy.request("case", *fixture("goal-2")))
        self.assertIsNone(policy.request("case", *fixture("goal-3")))
        self.assertEqual(policy.attempts, 2)

    def test_inflight_uncertain_absent_and_nonblocked_never_feedback(self):
        for phase in ["running", "launching", "collected", "uncertain", "paused"]:
            status, events = fixture()
            status["runs"]["review"] = {"role": "fr", "status": phase}
            self.assertIsNone(FeedbackPolicy().request("case", status, events))
        for field, value in [
            ("status", "active"),
            ("status", "done"),
            ("engine_alive", False),
            ("engine_alive", None),
        ]:
            status, events = fixture()
            status[field] = value
            self.assertIsNone(FeedbackPolicy().request("case", status, events))

    def test_other_failure_and_impl_native_handoff_are_not_retried(self):
        for kwargs in [
            {"code": 1},
            {"reason": "config_error"},
            {"role": "impl"},
            {"role": "cr"},
            {"role": "scribe"},
        ]:
            self.assertIsNone(FeedbackPolicy().request("case", *fixture(**kwargs)))

    def test_old_contract_failure_cannot_mask_new_success_or_other_failure(self):
        for new_result in [
            {"status": "succeeded"},
            {"status": "failed", "result": {"exit_code": 1, "exit_reason": "config_error"}},
        ]:
            status, events = fixture()
            status["runs"]["goal-new"] = {
                "role": "goal",
                "status": "finished",
                "result": new_result,
            }
            events.append(
                {
                    "seq": 3,
                    "kind": "run.collected",
                    "payload": {"run_id": "goal-new", "result": new_result},
                }
            )
            self.assertIsNone(FeedbackPolicy().request("case", status, events))

    def test_event_gap_or_inconsistent_failure_is_rejected(self):
        status, events = fixture()
        events[1]["seq"] = 3
        with self.assertRaisesRegex(ValueError, "完整公开事件"):
            FeedbackPolicy().request("case", status, events)
        status, events = fixture()
        events = copy.deepcopy(events)
        events[1]["payload"]["result"]["status"] = "succeeded"
        self.assertIsNone(FeedbackPolicy().request("case", status, events))


class FeedbackAPITests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, response=None, changed=False, error=False):
        status, events = fixture()
        calls = []
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        def write(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))

        async def call(client, name, request):
            calls.append((name, request))
            if name == "goal_events":
                return {"events": events if request["after"] == 0 else [], "next": 2}
            if name == "goal_status":
                return {**status, "status": "active"} if changed else status
            self.assertTrue((root / "raw/feedback/01/intent.json").exists())
            if error:
                raise RuntimeError("public API failure")
            return response or {
                "request_id": request["request_id"],
                "queued": True,
                "requires_resume": False,
            }

        policy = FeedbackPolicy()
        result = await send_feedback(policy, None, call, write, root, "case", status)
        return result, policy, calls, root

    async def test_public_request_and_complete_evidence_are_recorded(self):
        result, policy, calls, root = await self.exercise()
        self.assertEqual(result, "sent")
        self.assertEqual(
            [name for name, _ in calls],
            ["goal_events", "goal_events", "goal_status", "goal_message"],
        )
        intent = json.loads((root / "raw/feedback/01/intent.json").read_text())
        self.assertEqual(intent["trigger_run_ids"], ["goal-1"])
        self.assertEqual(intent["interrupted_dd_ids"], ["dd-1"])
        self.assertTrue((root / "raw/feedback/01/response.json").exists())
        self.assertEqual(policy.attempts, 1)

    async def test_race_to_active_sends_nothing_and_keeps_observing(self):
        result, policy, calls, _ = await self.exercise(changed=True)
        self.assertEqual(result, "changed")
        self.assertEqual(policy.attempts, 0)
        self.assertNotIn("goal_message", [name for name, _ in calls])

    async def test_requires_resume_or_api_error_stops_without_retry(self):
        for options in [
            {"error": True},
            {
                "response": {
                    "request_id": "e2e-format-feedback-v1:case:goal-1",
                    "queued": True,
                    "requires_resume": True,
                }
            },
        ]:
            result, policy, calls, root = await self.exercise(**options)
            self.assertEqual(result, "stop")
            self.assertEqual(policy.attempts, 1)
            self.assertEqual([name for name, _ in calls].count("goal_message"), 1)
            self.assertTrue((root / "raw/feedback/01/error.json").exists())

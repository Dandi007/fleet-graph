import copy
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("e2e_monitor", Path(__file__).with_name("monitor.py"))
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = monitor.BlockedIdleMonitor()
        self.status = {
            "status": "blocked",
            "engine_alive": True,
            "pending_requests": [],
            "runs": {"goal": {"status": "finished"}, "scribe": {"status": "finished"}},
        }

    def test_requires_two_unchanged_blocked_idle_observations(self):
        self.assertFalse(self.monitor.observe(self.status))
        self.assertTrue(self.monitor.observe(copy.deepcopy(self.status)))

    def test_running_review_never_requests_stop(self):
        self.status["runs"]["cr"] = {"status": "running"}
        for _ in range(3):
            self.assertFalse(self.monitor.observe(self.status))

    def test_uncertain_collected_launching_and_paused_are_not_finished(self):
        for state in ("uncertain", "collected", "launching", "paused", "unknown"):
            self.status["runs"]["cr"] = {"status": state}
            self.assertFalse(self.monitor.observe(self.status))
            self.assertFalse(self.monitor.observe(self.status))

    def test_intervening_active_status_resets_stability(self):
        self.assertFalse(self.monitor.observe(self.status))
        active = {**self.status, "status": "active"}
        self.assertFalse(self.monitor.observe(active))
        self.assertFalse(self.monitor.observe(self.status))
        self.assertTrue(self.monitor.observe(self.status))

    def test_new_request_resets_stability(self):
        self.assertFalse(self.monitor.observe(self.status))
        self.status["pending_requests"] = ["new-message"]
        self.assertFalse(self.monitor.observe(self.status))
        self.assertTrue(self.monitor.observe(self.status))


if __name__ == "__main__":
    unittest.main()

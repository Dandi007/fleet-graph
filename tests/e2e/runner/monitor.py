"""根据连续公开状态识别无法继续推进的运行；不推导成功。"""

import json


class BlockedIdleMonitor:
    def __init__(self):
        self.previous = None
        self.consecutive = 0

    def observe(self, status):
        runs = status.get("runs")
        idle = (
            status.get("status") == "blocked"
            and isinstance(runs, dict)
            and all(run.get("status") == "finished" for run in runs.values())
        )
        if not idle:
            self.previous, self.consecutive = None, 0
            return False
        current = json.dumps(status, sort_keys=True)
        self.consecutive = self.consecutive + 1 if current == self.previous else 1
        self.previous = current
        return self.consecutive >= 2


class EngineAbsentMonitor:
    def __init__(self):
        self.previous = None
        self.consecutive = 0

    def observe(self, status):
        if status.get("engine_alive") is not False or status.get("status") == "done":
            self.previous, self.consecutive = None, 0
            return False
        current = json.dumps(status, sort_keys=True)
        self.consecutive = self.consecutive + 1 if current == self.previous else 1
        self.previous = current
        return self.consecutive >= 2

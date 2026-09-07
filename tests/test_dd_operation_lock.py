"""跨控制进程的取消/启动竞争必须被同一把开发单锁阻断。"""

import os
import subprocess
import sys
from pathlib import Path

from fleet_graph.dd.operation_lock import operation_lock


def test_nested_owner_and_competing_process(tmp_path: Path):
    path = tmp_path / "operation.lock"
    code = """
import sys
from pathlib import Path
from fleet_graph.dd.operation_lock import operation_lock
try:
    with operation_lock(Path(sys.argv[1]), timeout=0.1):
        print('entered')
except TimeoutError:
    print('busy')
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}

    def competitor():
        return subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()

    with operation_lock(path), operation_lock(path):
        assert competitor() == "busy"
    assert competitor() == "entered"

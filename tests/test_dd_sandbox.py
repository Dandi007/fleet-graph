"""在真实 Linux namespace 中验证 DD 无法误写范围外文件或操作父进程。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fleet_graph.executors.sandbox import sandbox_argv


@pytest.mark.skipif(not shutil.which("bwrap"), reason="需要部署依赖 bubblewrap")
def test_real_sandbox_confines_files_and_processes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / "maintenance-stop"
    protected.write_text("unchanged")
    script = """
import json, os
from pathlib import Path
workspace, protected, parent = __import__('sys').argv[1:]
Path(workspace, 'output').write_text('done')
try:
    Path(protected).write_text('damaged')
    protected_blocked = False
except OSError:
    protected_blocked = True
try:
    os.kill(int(parent), 0)
    parent_hidden = False
except ProcessLookupError:
    parent_hidden = True
print(json.dumps({'protected_blocked': protected_blocked, 'parent_hidden': parent_hidden}))
"""
    result = subprocess.run(
        sandbox_argv(
            [sys.executable, "-c", script, str(workspace), str(protected), str(os.getpid())],
            writable=[workspace],
            readable=[protected],
        ),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {"protected_blocked": True, "parent_hidden": True}
    assert protected.read_text() == "unchanged"
    assert (workspace / "output").read_text() == "done"


def test_missing_sandbox_refuses_instead_of_host_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="拒绝无隔离"):
        sandbox_argv(["true"], writable=[])

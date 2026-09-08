"""初始化本次容器数据仓，然后启动未经修改的真实 Work Folder MCP。"""

import os
import subprocess
from pathlib import Path

from katana_work_folder_mcp import server
from katana_work_folder_mcp.reindex import render_index
from work_folder_search import running_search

REMOTE = "git://git-remote:9418/work-folder.git"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def initialize(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        if any(root.iterdir()):
            raise RuntimeError("拒绝覆盖非空的未初始化 Work Folder 数据卷")
        subprocess.run(["git", "clone", REMOTE, str(root)], check=True)
    if git(root, "remote", "get-url", "origin") != REMOTE:
        raise RuntimeError("Work Folder origin 必须指向本次内部 git-remote")
    git(root, "config", "user.name", "Docker E2E")
    git(root, "config", "user.email", "docker-e2e@example.invalid")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
    )
    if head.returncode == 0:
        return
    if any(entry.name != ".git" for entry in root.iterdir()):
        raise RuntimeError("拒绝重写已有数据但尚无 HEAD 的数据仓")
    (root / ".gitignore").write_text("/.katana/runtime/\n", encoding="utf-8")
    (root / "INDEX.md").write_text(render_index([]), encoding="utf-8")
    controls = root / ".katana"
    controls.mkdir()
    for filename, content in {
        "tombstones.json": '{"tombstones": []}\n',
        "flat-layout.json": '{"layout": "flat-id-v1", "schema_version": 1}\n',
        "legacy-manifest-inventory.json": '{"manifests": [], "schema_version": 1}\n',
    }.items():
        (controls / filename).write_text(content, encoding="utf-8")
    git(root, "add", ".gitignore", "INDEX.md", ".katana")
    git(root, "commit", "-m", "初始化 Docker E2E Work Folder 数据仓")
    git(root, "push", "-u", "origin", "HEAD:main")


if __name__ == "__main__":
    root = Path(os.environ.get("E2E_WORK_FOLDER_ROOT", "/data/work-folder"))
    initialize(root)
    server.configure(str(root))
    with running_search(root):
        server.mcp.run(transport="streamable-http", host="0.0.0.0", port=5602)

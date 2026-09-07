"""DD 子进程的文件写入与进程隔离，失败时不退回宿主执行。"""

import shutil
from pathlib import Path


def sandbox_argv(
    command: list[str], *, writable: list[Path], readable: list[Path] | None = None
) -> list[str]:
    binary = shutil.which("bwrap")
    if binary is None:
        raise FileNotFoundError("DD 隔离需要 bubblewrap（bwrap），拒绝无隔离执行")
    argv = [
        binary,
        "--ro-bind",
        "/",
        "/",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--unsetenv",
        "DBUS_SESSION_BUS_ADDRESS",
        "--unsetenv",
        "SSH_AUTH_SOCK",
        "--unsetenv",
        "XDG_RUNTIME_DIR",
        "--cap-drop",
        "ALL",
    ]
    for path in sorted({p.resolve() for p in (readable or [])}, key=lambda p: len(p.parts)):
        argv += ["--ro-bind", str(path), str(path)]
    for path in sorted({p.resolve() for p in writable}, key=lambda p: len(p.parts)):
        if path == Path("/"):
            raise ValueError("不能将根目录作为 DD 可写范围")
        argv += ["--bind", str(path), str(path)]
    return [*argv, "--", *command]


def git_write_paths(workspace: Path) -> list[Path]:
    """Git worktree 的共享对象库需要写入，产品目录仍只有本单工作区可写。"""
    paths = [workspace.resolve()]
    from fleet_graph.dd.git import run_git

    result = run_git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode == 0:
        paths.append(Path(result.stdout.strip()).resolve())
    return paths

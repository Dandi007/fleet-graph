"""Write-gate compatibility: reconcile adoption contends on the governed lock.

The correcting fact this closes: ``GitWorkFolderSource.adopt()`` used to perform
its byte CAS, writes, ``git add``, and ``git commit`` without the cross-process
mutation lock the canonical governed work-folder writer holds. That let a
concurrent MCP governed mutation interleave between those steps and silently
overwrite a winner, stage another process's bytes, or leave unattributable
residue.

These tests use a real disposable repository with ``wf-governed/`` as a
subdirectory and real OS processes. A governed-writer process acquires the
canonical ``<git-common-dir>/katana-governed.lock`` from the repository root
while reconciliation adopts through the production ``GitWorkFolderSource.adopt``
path resolved from the subdirectory. At least one assertion proves the two
participants resolve to the *same* lock inode, so a false test using two
unrelated lock files fails rather than passing.
"""

from __future__ import annotations

import fcntl
import json
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fleet_graph.dd.reconcile import ReconcileError
from fleet_graph.dd.work_folder_store import GitWorkFolderSource

PROGRESS = b"# Progress\n- first line\n"
APPEND = b"- resolved: adopt the residue\n"

#: How long the governed writer holds the lock at a minimum. Long enough for the
#: adoption (if it bypassed the lock) to land a commit before the writer's own
#: head-count probe, so a bypass is observed deterministically instead of racing.
HOLD_SECONDS = 1.0

#: How long the loser test confirms adoption is blocked on the held lock before
#: it orders the competing commit. Bounded, and far above the ~milliseconds
#: adoption takes to reach the lock, so a buggy pre-lock CAS has already read the
#: pre-winner bytes before the winner lands.
BLOCK_CONFIRM_SECONDS = 1.0

#: A standalone governed-writer process. ``argv``:
#: 1 cwd used to resolve the common dir (the repository root),
#: 2 hold seconds,
#: 3 mode: "hold" or "commit-competing",
#: 4 (commit-competing only) repository-relative file to rewrite,
#: 5 (commit-competing only) the exact new content (utf-8).
#:
#: ``commit-competing`` acquires the lock and then blocks on a single stdin line
#: before it writes, stages, and commits the competing state. That lets the test
#: hold the lock while adoption is provably blocked on it, and only then order
#: the competing commit -- so adoption must contend and must re-read under lock.
_GOVERNED_WRITER = r"""
import fcntl
import subprocess
import sys
import time
from pathlib import Path

cwd = Path(sys.argv[1])
hold = float(sys.argv[2])
mode = sys.argv[3]

proc = subprocess.run(
    ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
    capture_output=True,
    text=True,
)
assert proc.returncode == 0, proc.stderr
common = Path(proc.stdout.strip())
if not common.is_absolute():
    common = cwd / common
lock = common.resolve() / "katana-governed.lock"

handle = open(lock, "a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("locked " + str(lock), flush=True)

if mode == "commit-competing":
    sys.stdin.readline()
    rel = Path(sys.argv[4])
    target = cwd / rel
    target.write_bytes(sys.argv[5].encode("utf-8"))
    subprocess.run(
        ["git", "-C", str(cwd), "add", "--", str(rel)], check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=governed",
            "-c", "user.email=governed@example.invalid",
            "commit", "-q", "-m", "governed writer wins",
        ],
        check=True,
    )
elif mode == "hold":
    time.sleep(0.4)
    count = subprocess.run(
        ["git", "-C", str(cwd), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    print("head-count " + count.stdout.strip(), flush=True)
    time.sleep(max(0.0, hold - 0.4))

fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
handle.close()
print("unlocked", flush=True)
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _monorepo_governed_repo(tmp_path: Path, working: bytes) -> tuple[Path, Path]:
    """A real single repository with ``wf-governed/`` as a subdirectory."""
    root = tmp_path / "monorepo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.invalid")
    governed = root / "wf-governed"
    governed.mkdir()
    (governed / "progress.md").write_bytes(PROGRESS)
    _git(root, "add", "--", "wf-governed/progress.md")
    _git(root, "commit", "-q", "-m", "base")
    (governed / "progress.md").write_bytes(working)
    return root, governed


def _source_for(subdir: Path) -> GitWorkFolderSource:
    def resolve(folder_id: str) -> Path | None:
        if folder_id != "wf-governed":
            return None
        return subdir

    return GitWorkFolderSource(resolve)


def _git_blob(root: Path, rev_path: str) -> bytes:
    """The exact committed bytes of ``rev_path``, without whitespace trimming."""
    proc = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", rev_path],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout


def _common_dir_lock(cwd: Path) -> Path:
    """The canonical governed lock as resolved from ``cwd``."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = cwd / common
    return common.resolve() / "katana-governed.lock"


def _readline(stream: Any, timeout: float) -> str | None:
    ready, _, _ = select.select([stream], [], [], timeout)
    if stream not in ready:
        return None
    return stream.readline().rstrip("\n")


def _spawn_writer(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _GOVERNED_WRITER, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _lock_is_held(lock: Path) -> bool:
    """True while some process holds the exclusive governed lock, false otherwise."""
    with open(lock, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


class TestWriteGateSerialization:
    def test_adopt_waits_for_cross_process_governed_lock(self, tmp_path: Path) -> None:
        root, governed = _monorepo_governed_repo(tmp_path, working=PROGRESS + APPEND)
        source = _source_for(governed)
        entries = (("progress.md", PROGRESS, APPEND),)

        # Repo-root and folder-subdirectory participants must resolve the same
        # lock inode, or a false test using two unrelated lock files would pass.
        root_lock = _common_dir_lock(root)
        subdir_lock = _common_dir_lock(governed)
        assert root_lock == subdir_lock

        writer = _spawn_writer(str(root), str(HOLD_SECONDS), "hold")
        assert writer.stdout is not None and writer.stderr is not None
        locked_line = _readline(writer.stdout, 10.0)
        assert locked_line is not None, "governed writer never acquired the lock"
        # The writer (repo-root) and adoption (subdirectory) agree on the lock.
        assert locked_line == f"locked {root_lock}"

        start = time.monotonic()
        result = source.adopt("wf-governed", entries)
        elapsed = time.monotonic() - start

        # Adoption waited for the held lock rather than bypassing it.
        assert elapsed >= HOLD_SECONDS - 0.2, (
            f"adoption returned in {elapsed:.3f}s while the governed writer "
            f"held the lock for {HOLD_SECONDS}s; the write gate was bypassed"
        )
        head_count_line = _readline(writer.stdout, 10.0)
        unlocked_line = _readline(writer.stdout, 10.0)
        assert head_count_line is not None and head_count_line == "head-count 1", (
            "adoption committed inside the governed writer's hold window"
        )
        assert unlocked_line == "unlocked"
        assert writer.wait(timeout=10.0) == 0, writer.stderr.read()

        # Commits exactly the append, returns only logical fields, stays clean.
        assert result == {"store": "git", "committed_files": ["progress.md"]}
        assert json.dumps(result, sort_keys=True).find(str(root)) == -1
        assert _git_blob(root, "HEAD:wf-governed/progress.md") == PROGRESS + APPEND
        assert _git(root, "rev-list", "--count", "HEAD") == "2"
        assert _git(root, "status", "--porcelain") == ""

    def test_concurrent_governed_winner_is_never_overwritten_or_left_as_residue(
        self, tmp_path: Path
    ) -> None:
        root, governed = _monorepo_governed_repo(tmp_path, working=PROGRESS + APPEND)
        source = _source_for(governed)
        entries = (("progress.md", PROGRESS, APPEND),)

        root_lock = _common_dir_lock(root)
        assert root_lock == _common_dir_lock(governed)

        winner_bytes = PROGRESS + b"- governed winner\n"
        writer = _spawn_writer(
            str(root),
            "0.0",
            "commit-competing",
            "wf-governed/progress.md",
            winner_bytes.decode("utf-8"),
        )
        assert writer.stdout is not None and writer.stdin is not None
        assert writer.stderr is not None

        # The writer acquires the canonical lock and then waits for the go signal
        # before committing, so adoption overlaps with the held lock rather than
        # running after a joined, already-finished writer.
        locked_line = _readline(writer.stdout, 10.0)
        assert locked_line == f"locked {root_lock}", (
            "governed writer never acquired the canonical lock"
        )

        # Adoption must now start while the lock is held, so it contends.
        outcomes: list[tuple[str, Any]] = []

        def run_adopt() -> None:
            try:
                outcomes.append(("ok", source.adopt("wf-governed", entries)))
            except BaseException as exc:  # surface any adoption failure to the test
                outcomes.append(("err", exc))

        thread = threading.Thread(target=run_adopt, daemon=True)
        thread.start()

        # Prove adoption is blocked on the writer's lock before the writer commits:
        # the lock is still held (a non-blocking acquisition from here fails) and
        # adoption has not completed, so its only remaining path is to wait on it.
        deadline = time.monotonic() + BLOCK_CONFIRM_SECONDS
        while time.monotonic() < deadline and not outcomes:
            assert _lock_is_held(root_lock), (
                "the governed writer released the lock before adoption contended"
            )
            time.sleep(0.05)
        assert not outcomes, "adoption finished while the governed lock was held"
        assert _lock_is_held(root_lock)

        # Order the competing commit while adoption is still blocked, then release.
        writer.stdin.write("go\n")
        writer.stdin.flush()
        assert writer.wait(timeout=10.0) == 0, writer.stderr.read()

        thread.join(timeout=10.0)
        assert not thread.is_alive(), "adoption never unblocked after the release"
        assert len(outcomes) == 1 and outcomes[0][0] == "err", (
            f"adoption did not refuse closed: {outcomes!r}"
        )
        assert isinstance(outcomes[0][1], ReconcileError)
        assert "does not match the current bytes" in str(outcomes[0][1])

        # The winner's HEAD and bytes remain exact; no residue and no extra commit.
        assert _git(root, "log", "-1", "--format=%s") == "governed writer wins"
        assert _git_blob(root, "HEAD:wf-governed/progress.md") == winner_bytes
        assert _git(root, "rev-list", "--count", "HEAD") == "2"
        assert _git(root, "status", "--porcelain") == ""
        assert (governed / "progress.md").read_bytes() == winner_bytes

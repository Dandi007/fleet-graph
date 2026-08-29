"""Concrete production ``ReconcileSource``: the governed git work-folder store.

The correcting fact this closes is ``RECONCILE_SOURCE_UNBOUND``: ``serve()``
built its MCP server without a concrete ``ReconcileSource``, so the already
registered ``wf_reconcile`` tool refused every real call. This module is the
concrete source, bound in the production ``serve()`` path (see
``fleet_graph.dd.service``), backed by the governed work-folder repository the
production deployment owns.

The source is deterministic and fail-closed. It reads ``folder_id`` as an
opaque token: the physical repository stays behind an injectable ``resolve``
seam, so no public payload or error can ever disclose where the data lives.

- ``inspect(folder_id)`` is the read side. It walks the repository's *governed*
  tree (``git ls-tree HEAD``) for the committed base of each logical filename
  and reads the working tree for the current bytes, and it lists untracked
  residue (``git ls-files --others``) as ``tracked=False``. A deleted file has
  ``current=None``; a governed file whose blob cannot be read has ``base=None``
  so the reconciler classifies it ``ambiguous`` and refuses, not guesses.
- ``adopt(folder_id, entries)`` is the write side, invoked exactly once per
  confirmation. It verifies the working bytes still equal ``base + appended``
  for every entry (the append-only CAS binding, re-checked at the store), then
  writes the exact bytes and commits them atomically, returning a receipt
  fragment that names logical files and never a path.

Write-gate compatibility: the governed work-folder writer serializes every
mutation behind an exclusive ``fcntl.flock`` on
``<git-common-dir>/katana-governed.lock``. ``adopt`` holds that *same* lock
across its entire critical section (filename validation, byte CAS, writes,
staging, commit, receipt), resolving the common directory via
``git rev-parse --git-common-dir`` so a folder-subdirectory resolver and a
repository-root MCP process contend on one inode. Sharing the lock -- rather
than adopting unlocked -- is what lets reconciliation's byte CAS stay visible to
a concurrent governed mutation: without it, an MCP governed write could land
between the reconcile byte check, its writes, and its commit, silently
overwriting a winner or staging another process's bytes. Lock resolution/open/
acquisition failures refuse closed with ``ReconcileError`` and never fall back
to an unlocked adoption or disclose the physical path.

Any git failure, unresolvable folder, or a folder id that does not look like an
opaque token raises ``ReconcileError`` (``RECONCILE_REFUSED`` over the wire)
without mutating anything.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import git_argv
from fleet_graph.dd.reconcile import AppendItem, InspectedFile, ReconcileError
from fleet_graph.dd.vendor import git_ops

#: An opaque ``folder_id`` is a safe token. Anything carrying a filesystem
#: fragment (separator, parent traversal, absolute path) is refused before it
#: is ever joined onto a path -- the physical store stays behind the seam.
_SAFE_FOLDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The commit identity the reconciler uses when it adopts residue. The governed
#: store commits are written by this mechanism, never by a caller's config.
_RECONCILE_COMMIT_USER = "WorkFolderReconciler"
_RECONCILE_COMMIT_EMAIL = "work-folder-reconciler@example.invalid"

#: The canonical cross-process mutation lock the governed work-folder writer
#: serializes every mutation with. The reconciler must contend on the exact same
#: inode, resolved from the same ``git rev-parse --git-common-dir``, so a
#: folder-subdirectory resolver and a repository-root MCP process cannot bypass
#: each other's write gate.
_GOVERNED_LOCK_FILENAME = "katana-governed.lock"

#: Bounded wait for the governed mutation lock. A blocked acquisition polls with
#: a non-blocking flock and gives up after this budget, so a wedged holder fails
#: the adoption closed instead of hanging a reconcile call forever.
_GOVERNED_LOCK_TIMEOUT_SECONDS = 30.0
_GOVERNED_LOCK_POLL_SECONDS = 0.05

#: A physical repository resolver. ``None`` means "cannot resolve" and refuses.
FolderResolver = Callable[[str], Path | None]


def _is_safe_folder_id(folder_id: str) -> bool:
    return bool(_SAFE_FOLDER_ID.match(folder_id))


class GitWorkFolderSource:
    """A ``ReconcileSource`` over a governed git work-folder repository.

    The resolver maps the opaque ``folder_id`` to the repository the governor
    owns; tests and the acceptance harness point it at disposable repositories,
    while production points it at the governed store. The directory itself
    never crosses the seam back out.
    """

    def __init__(self, resolve: FolderResolver) -> None:
        self._resolve = resolve

    # --- seam -------------------------------------------------------------

    def _repo(self, folder_id: str) -> Path:
        if not _is_safe_folder_id(folder_id):
            raise ReconcileError(
                f"work folder {folder_id!r} is not a valid opaque folder id; refusing closed"
            )
        try:
            repo = self._resolve(folder_id)
        except ReconcileError:
            raise
        except Exception as exc:
            raise ReconcileError(
                f"governed work-folder store could not resolve folder {folder_id!r}; "
                "refusing closed"
            ) from exc
        if repo is None:
            raise ReconcileError(
                f"no governed work-folder store serves folder {folder_id!r}; refusing closed"
            )
        return repo

    # --- read side --------------------------------------------------------

    def inspect(self, folder_id: str) -> tuple[InspectedFile, ...]:
        repo = self._repo(folder_id)
        governed = self._git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0")
        untracked = self._git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        result: list[InspectedFile] = []
        for name in governed:
            if not name:
                continue
            filename = name.decode("utf-8", "surrogateescape")
            base = self._governed_base(repo, filename)
            current = self._working_bytes(repo, filename)
            result.append(
                InspectedFile(filename=filename, base=base, current=current, tracked=True)
            )
        for name in untracked:
            if not name:
                continue
            filename = name.decode("utf-8", "surrogateescape")
            current = self._working_bytes(repo, filename)
            if current is None:
                continue
            result.append(
                InspectedFile(filename=filename, base=None, current=current, tracked=False)
            )
        return tuple(result)

    def _governed_base(self, repo: Path, filename: str) -> bytes | None:
        # ``repo`` is the resolver-returned cwd. ``HEAD:./{filename}`` addresses
        # the blob relative to that cwd, so a governed folder that is a monorepo
        # subdirectory resolves to ``HEAD:<subdir>/<filename>`` instead of the
        # repository-root path the logical filename alone would name.
        proc = subprocess.run(
            git_argv(repo, "show", f"HEAD:./{filename}"),
            capture_output=True,
            env=git_ops.safe_git_environment(),
            check=False,
        )
        if proc.returncode != 0:
            # A governed file whose blob we cannot read is ambiguous, so the
            # reconciler refuses rather than guessing at a base.
            return None
        return proc.stdout

    @staticmethod
    def _working_bytes(repo: Path, filename: str) -> bytes | None:
        rel = Path(filename)
        if rel.is_absolute() or ".." in rel.parts:
            return None
        try:
            return (repo / rel).read_bytes()
        except OSError:
            return None

    # --- write side -------------------------------------------------------

    def adopt(self, folder_id: str, entries: tuple[AppendItem, ...]) -> dict[str, Any]:
        repo = self._repo(folder_id)
        lock_path = self._governed_lock_path(repo)
        with self._exclusive_governed_lock(lock_path):
            return self._adopt_under_lock(repo, entries)

    def _adopt_under_lock(
        self, repo: Path, entries: tuple[AppendItem, ...]
    ) -> dict[str, Any]:
        # Everything from the filename validation through the receipt builds in
        # one exclusive governed-lock critical section: no byte read is taken
        # before the lock and later trusted, and a concurrent governed writer
        # cannot interleave between any of these steps.
        for filename, base, appended in entries:
            if not self._is_safe_relpath(filename):
                raise ReconcileError(f"refusing to adopt unsafe filename {filename!r}")
            target = base + appended
            current = self._working_bytes(repo, filename)
            if current != target:
                raise ReconcileError(
                    f"append-only confirmation does not match the current bytes of "
                    f"{filename!r}; refusing without mutation"
                )
        for filename, base, appended in entries:
            (repo / filename).write_bytes(base + appended)
        self._git(repo, "add", "--", *(filename for filename, _, _ in entries))
        self._git(
            repo,
            "-c",
            f"user.name={_RECONCILE_COMMIT_USER}",
            "-c",
            f"user.email={_RECONCILE_COMMIT_EMAIL}",
            "commit",
            "-q",
            "-m",
            "reconcile: adopt append-only residue",
        )
        return {
            "store": "git",
            "committed_files": [filename for filename, _, _ in entries],
        }

    @staticmethod
    def _is_safe_relpath(filename: str) -> bool:
        rel = Path(filename)
        return not rel.is_absolute() and ".." not in rel.parts

    # --- governed mutation lock -------------------------------------------

    def _governed_lock_path(self, repo: Path) -> Path:
        """Resolve the canonical cross-process mutation lock for ``repo``.

        ``repo`` is the resolver-returned cwd, which may be a monorepo
        subdirectory. ``git rev-parse --git-common-dir`` names the shared
        ``.git`` directory for that repository regardless of the cwd, so a
        folder-subdirectory resolver and a repository-root MCP process resolve
        the same lock inode. Any resolution failure refuses closed with a
        ReconcileError that never discloses the physical path.
        """
        proc = subprocess.run(
            git_argv(repo, "rev-parse", "--git-common-dir"),
            capture_output=True,
            env=git_ops.safe_git_environment(),
            check=False,
        )
        common_bytes = proc.stdout.strip() if proc.returncode == 0 else b""
        if not common_bytes:
            raise ReconcileError(
                "governed work-folder store could not resolve the repository "
                "mutation lock; refusing closed without mutation"
            )
        common = Path(os.fsdecode(common_bytes))
        if not common.is_absolute():
            common = repo / common
        return common.resolve() / _GOVERNED_LOCK_FILENAME

    @contextmanager
    def _exclusive_governed_lock(self, lock_path: Path) -> Iterator[None]:
        """Acquire the exclusive governed mutation lock, bounded, never unlocked.

        A failed open, an acquisition that runs past the budget, or any refusal
        raises ``ReconcileError`` without mutating anything and without naming
        the physical lock path. There is deliberately no fallback to an
        unlocked adoption.
        """
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise ReconcileError(
                "governed work-folder store could not open the repository "
                "mutation lock; refusing closed without mutation"
            ) from exc
        try:
            deadline = time.monotonic() + _GOVERNED_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ReconcileError(
                            "governed work-folder store timed out acquiring the "
                            "repository mutation lock; refusing closed without mutation"
                        ) from None
                    time.sleep(_GOVERNED_LOCK_POLL_SECONDS)
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    # --- git plumbing -----------------------------------------------------

    def _git(self, repo: Path, *args: str) -> bytes:
        proc = subprocess.run(
            git_argv(repo, *args),
            capture_output=True,
            env=git_ops.safe_git_environment(),
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise ReconcileError(
                f"governed work-folder store refused ({args[0]} failed"
                + (f": {detail}" if detail else "")
                + "); no data mutated"
            )
        return proc.stdout


def governed_work_folder_store(root: str | os.PathLike[str] | None) -> GitWorkFolderSource:
    """The production construction: a concrete source resolving ``folder_id``.

    ``root`` is the directory that owns one governed repository per work
    folder. When it is absent the returned source is still concrete (never
    ``None``, never ``RECONCILE_SOURCE_UNBOUND``): each ``inspect``/``adopt``
    refuses closed with a configuration refusal, so a real call can never be
    mistaken for a working route that silently does nothing.
    """
    if root in (None, ""):

        def refuse(folder_id: str) -> Path | None:
            raise ReconcileError(
                f"no governed work-folder store is configured for this server; "
                f"cannot reconcile folder {folder_id!r} (refusing closed)"
            )

        return GitWorkFolderSource(refuse)

    base = Path(root)

    def resolve(folder_id: str) -> Path | None:
        if not _is_safe_folder_id(folder_id):
            raise ReconcileError(
                f"work folder {folder_id!r} is not a valid opaque folder id; refusing closed"
            )
        return base / folder_id

    return GitWorkFolderSource(resolve)


__all__ = [
    "GitWorkFolderSource",
    "governed_work_folder_store",
]

"""The concrete goal-folder source: one goal line per directory under a root.

Mirrors the ``governed_work_folder_store`` pattern in the dev-dispatch plane:
the root owns one folder per ``folder_id``, an opaque token, and a missing root
still yields a *concrete* source that refuses closed per-folder rather than a
server without a route. The physical path never crosses the seam back out --
only ``folder_id`` and the logical filenames the validator names (``goal.md``,
``golden-order.md``) do.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: An opaque ``folder_id`` is a safe token. Anything carrying a filesystem
#: fragment (separator, parent traversal, absolute path) is refused before it
#: is ever joined onto a path.
_SAFE_FOLDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _is_safe_folder_id(folder_id: str) -> bool:
    return bool(_SAFE_FOLDER_ID.match(folder_id))


class DirectoryGoalFolderSource:
    """A ``GoalFolderSource`` over a directory of goal-line folders.

    ``folder_id`` resolves to ``root / folder_id``. Safe-token validation and
    a closed ``_refuse`` seam keep the physical layout behind the boundary.
    """

    def __init__(self, resolve: Any) -> None:
        self._resolve = resolve

    def exists(self, folder_id: str) -> bool:
        folder = self._folder(folder_id)
        return folder is not None and folder.is_dir()

    def read(self, folder_id: str, filename: str) -> str | None:
        folder = self._folder(folder_id)
        if folder is None:
            return None
        rel = Path(filename)
        if rel.is_absolute() or ".." in rel.parts:
            return None
        try:
            return (folder / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _folder(self, folder_id: str) -> Path | None:
        if not _is_safe_folder_id(folder_id):
            return None
        try:
            folder = self._resolve(folder_id)
        except Exception:
            return None
        if folder is None:
            return None
        return Path(folder)


def governed_goal_folder_store(root: str | None) -> DirectoryGoalFolderSource:
    """The production construction: a concrete source resolving ``folder_id``.

    ``root`` is the directory that owns one goal-line folder per folder id.
    When it is absent the returned source is still concrete (never ``None``):
    each read/exists refuses closed, so a real call can never be mistaken for a
    working route that silently does nothing.
    """
    if root in (None, ""):
        return DirectoryGoalFolderSource(lambda folder_id: None)
    base = Path(root)

    def resolve(folder_id: str) -> Path | None:
        if not _is_safe_folder_id(folder_id):
            return None
        return base / folder_id

    return DirectoryGoalFolderSource(resolve)


__all__ = ["DirectoryGoalFolderSource", "governed_goal_folder_store"]

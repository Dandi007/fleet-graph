"""Reading a module's code without its prose.

Several tests assert that something is absent from a module -- a stage name,
a message kind. Searching the raw file only proves the docstrings are shy, so
comments and string literals come out first.
"""

from __future__ import annotations

import tokenize
from pathlib import Path


def executable_source(path: Path) -> str:
    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


__all__ = ["executable_source"]

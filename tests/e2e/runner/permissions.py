"""终局 checkout 的只读访问准备，不修改文件内容或 Git executable bit。"""

import os
import stat
from pathlib import Path


def prepare_readable(repo: Path):
    changes = []
    for directory, dirs, files in os.walk(repo, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in dirs + files)]:
            before = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                continue
            required = 0o555 if path.is_dir() else 0o444
            after = before | required
            if before != after:
                path.chmod(after)
                changes.append(
                    {
                        "path": str(path.relative_to(repo)),
                        "before": oct(before),
                        "after": oct(after),
                    }
                )
    return {"operation": "prepare_readable", "content_modified": False, "changes": changes}

"""R2 图合一 checkpoint A 方案：图状态从权威件完全重建，checkpointer 是可删缓存。

判据锚：R2 spec §行为契约 3（design §3 自决 A 方案）。删库重建后三不变：

- **不重复派发**：准入幂等键 (repo, spec, base) → 同一 development_id；已派单
  事实在 record.json，重建据此判重，绝不建第二张单；
- **不丢结果**：终态权威是 result.json，重建从这里重放终态；
- **线继续运转**：图状态 = work folder 持久件 + dd 两权威件的纯函数投影，
  重建输入里没有 checkpoint、没有 status.json / terminal.json / .scheduler
  （那三类盘面文件自 R2 起根本不是状态源）。

本模块是引擎侧重建面（单测红靶 ``test_checkpoint_rebuild_no_dup_dispatch_no_loss``
锚在这里）；``scripts/testenv.sh rebuild`` 的 bash 探针是其同口径投影。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: The dd authority artifacts (single state source per R2).
RECORD_FILE = "record.json"
RESULT_FILE = "result.json"

#: The deletable cache: the LangGraph sqlite checkpointer. Deleting it must
#: lose nothing — the rebuild answers come from the authorities alone.
CHECKPOINT_FILES = ("checkpoint.sqlite3", "checkpoint.sqlite3-wal", "checkpoint.sqlite3-shm")

#: A rebuilt development's state vocabulary mirrors the control plane's
#: authority projection (awaiting question -> gate; terminal -> terminal;
#: else created).
STATE_AWAITING_GATE = "awaiting_gate"
STATE_CREATED = "created"
STATE_IN_FLIGHT = "in_flight"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _generation(record: dict[str, Any]) -> int:
    try:
        return max(1, int(record.get("generation") or 1))
    except (TypeError, ValueError):
        return 1


def result_path_for(dd_root: Path, development_id: str, generation: int) -> Path:
    """The generation's terminal authority (generation 1 sits at the root)."""
    dev_root = dd_root / development_id
    return dev_root / RESULT_FILE if generation <= 1 else dev_root / f"g{generation}" / RESULT_FILE


def rebuild_development(dd_root: Path, development_id: str) -> dict[str, Any]:
    """One development's state, rebuilt from the two authority files ONLY.

    The projection is the same precedence the control plane enforces: a
    pending gate question means the single sits at the gate; otherwise any
    terminal terminalises; neither present means created (admitted, not
    started). Nothing here reads a checkpoint, a status cache, or any
    scheduler file.
    """
    record = _read_json(dd_root / development_id / RECORD_FILE)
    if record is None:
        raise ValueError(f"admission record unreadable: {dd_root / development_id / RECORD_FILE}")
    generation = _generation(record)
    result = _read_json(result_path_for(dd_root, development_id, generation))
    awaiting = result.get("awaiting") if isinstance(result, dict) else None
    terminal = str((result or {}).get("terminal") or "")
    if awaiting:
        state: str = STATE_AWAITING_GATE
    elif terminal:
        state = terminal
    else:
        state = STATE_CREATED
    return {
        "development_id": development_id,
        "generation": generation,
        "repo_path": str(record.get("repo_path") or ""),
        "spec_digest": str(record.get("spec_digest") or ""),
        "dispatched_by": str(record.get("dispatched_by") or ""),
        "state": state,
        "terminal": terminal,
        "terminal_reason": str((result or {}).get("terminal_reason") or ""),
        "output_commit": str((result or {}).get("head_commit") or ""),
        "stage": str((result or {}).get("stage") or ""),
        #: Honest provenance: the rebuild read exactly these inputs.
        "rebuilt_from": [RECORD_FILE, RESULT_FILE],
    }


def duplicate_dispatches(dd_root: Path) -> list[list[str]]:
    """Development directories that share one admission idempotency key.

    The key is (repo_path, spec_digest) — the same identity
    ``DdControlPlane.create`` is idempotent on. A rebuild (or any buggy
    double-dispatch) must never produce two directories with one key; a
    non-empty answer here IS a duplicate dispatch.
    """
    keys: dict[tuple[str, str], list[str]] = {}
    if not dd_root.is_dir():
        return []
    for entry in sorted(dd_root.iterdir()):
        record = _read_json(entry / RECORD_FILE) if entry.is_dir() else None
        if record is None:
            continue
        key = (str(record.get("repo_path") or ""), str(record.get("spec_digest") or ""))
        keys.setdefault(key, []).append(entry.name)
    return [names for names in keys.values() if len(names) > 1]


def rebuild_line_state(
    work_folder: Path,
    dd_root: Path,
    line_folder: str,
) -> dict[str, Any]:
    """One line's graph state, rebuilt from persistence only.

    Inputs: the work folder's durable files (progress/findings/INDEX — read
    as presence facts, never parsed as state) plus the dd authorities for the
    developments this line dispatched (record.json ``dispatched_by`` is the
    dispatch fact, result.json the terminal). No checkpoint, no
    terminal.json, no .scheduler file, no board.
    """
    developments: list[dict[str, Any]] = []
    if dd_root.is_dir():
        for entry in sorted(dd_root.iterdir()):
            record = _read_json(entry / RECORD_FILE) if entry.is_dir() else None
            if record is None:
                continue
            if str(record.get("dispatched_by") or "") != line_folder:
                continue
            developments.append(rebuild_development(dd_root, entry.name))
    work_files = (
        sorted(item.name for item in work_folder.iterdir() if item.is_file())
        if work_folder.is_dir()
        else []
    )
    return {
        "line_folder": line_folder,
        "work_folder_files": work_files,
        "dispatched_developments": developments,
        "rebuilt_from": ["work_folder", RECORD_FILE, RESULT_FILE],
    }


def delete_checkpointer_cache(runs_root: Path, dd_root: Path | None = None) -> list[Path]:
    """Delete the sqlite checkpointer caches; the authorities stay untouched."""
    deleted: list[Path] = []
    roots = [runs_root] + ([dd_root] if dd_root is not None else [])
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.rglob("checkpoint.sqlite3*")):
            if entry.name in CHECKPOINT_FILES:
                try:
                    entry.unlink()
                    deleted.append(entry)
                except OSError:
                    continue
    return deleted


def rebuild_all(runs_root: Path, dd_root: Path) -> dict[str, Any]:
    """The whole-fleet rebuild answer: delete caches, rebuild, assert no dups."""
    deleted = delete_checkpointer_cache(runs_root, dd_root)
    developments: list[dict[str, Any]] = []
    if dd_root.is_dir():
        for entry in sorted(dd_root.iterdir()):
            if entry.is_dir() and (entry / RECORD_FILE).is_file():
                developments.append(rebuild_development(dd_root, entry.name))
    return {
        "deleted_checkpoints": [str(path) for path in deleted],
        "developments": developments,
        "duplicate_dispatches": duplicate_dispatches(dd_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R2 checkpoint-A rebuild probe")
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--dd-root", type=Path, required=True)
    args = parser.parse_args(argv)
    answer = rebuild_all(args.runs_root, args.dd_root)
    print(
        json.dumps(
            {
                "rebuild": "ok",
                "重建": "ok",
                "deleted": len(answer["deleted_checkpoints"]),
                "rebuilt": len(answer["developments"]),
                "dups": len(answer["duplicate_dispatches"]),
                "detail": answer,
            },
            ensure_ascii=False,
        )
    )
    return 1 if answer["duplicate_dispatches"] else 0


if __name__ == "__main__":
    sys.exit(main())

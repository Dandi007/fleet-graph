"""workfolder.py — WF 回写：goal 级 progress 与书记员 warn/high findings 的 MCP 追加层。

protocol §1「WF 与运行时状态的分工」：WF 只放人读的 ``goal.md`` / ``spec.md`` /
``progress.md`` / ``findings.md`` 正本；引擎在每个 goal 级 event（turn 结束、DD 结束、
done / blocked）后经 work-folder MCP 追加一行 progress；protocol §12「存放」段同时要求
``severity ∈ {warn, high}`` 的 observation 追加进 WF ``findings.md``，让 findings 天然
是 L1 的高严重度子集。

本模块只定义「怎么把文本送进 WF」，零 graph、零事件、零 agent：

- :class:`WorkFolderWriter`：协议，两个方法（``append_progress`` / ``append_findings``）；
  调用方注入，测试换假实现。
- :class:`McpWorkFolderWriter`：默认实现——经 work-folder MCP 追加。进程调用 seam 与
  :mod:`fleet_graph.minimal.prlifecycle` 调 ``gh`` 保持一致：argv 白名单（folder id 强
  校验、永不经过 shell）、``shell=False``、超时（注入的 runner 已带），失败抛
  :class:`WorkFolderError`、绝不直写 WF 目录（协议只说「经 work-folder MCP」）。
- :class:`NullWorkFolderWriter`：no-op，用于测试与「WF 不可达」的降级。
- :func:`progress_line`：把一个 goal 级 event 渲染成一行人读文本，零 IO。

WF 写入永不阻塞主流程：本模块只「抛错」；把异常吞掉、落 ``goal.warning`` 并让 goal
循环继续是调用方（:mod:`fleet_graph.minimal.goalgraph`）的职责。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from fleet_graph.minimal.gitgate import GitRunner

# 与 enroll._is_valid_work_folder_id 同一形状（GO-19 id：``wf-`` + 小写字母数字）；进程
# 调用前先过它，保证 work_folder 永远不可能被解析成 argv 里的 flag。
_WORK_FOLDER_ID_RE = re.compile(r"^wf-[0-9a-z]+$")

# katana work-folder MCP 的 loopback 地址（同旧 state/work_folder.py 的默认值）。
DEFAULT_WORK_FOLDER_MCP_URL = "http://127.0.0.1:5602/mcp/"

# 进程调用 seam：经 fastmcp 的 ``call`` 子命令调 MCP 工具（argv 白名单、shell=False、
# --timeout 有界连接），与 prlifecycle 调 gh 的「注入 runner + list argv」一致。
WORK_FOLDER_MCP_CLI = "fastmcp"

# katana work-folder MCP 的两个追加工具：progress 走 ``wf_append_progress``，findings 走
# ``wf_save(findings_addition=...)``（store.save 把 findings_addition 原样 append 进
# findings.md）。名字在此成常量，部署换 MCP 面只改这里。
PROGRESS_TOOL = "wf_append_progress"
FINDINGS_TOOL = "wf_save"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _blurb(payload: dict[str, Any]) -> str:
    """payload 里的第一句人读结论（summary → detail → impl_summary）。"""
    for key in ("summary", "detail", "impl_summary"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def progress_line(kind: str, payload: dict[str, Any], *, ts: str | None = None) -> str:
    """把一个 goal 级 event 渲染成一行人读文本；零 IO，单独可测。

    ``kind`` 是四个 goal 级边界之一：``goal.turn.finished`` / ``dd.merged`` /
    ``dd.failed`` / ``goal.done`` / ``goal.blocked``。``payload`` 提供 turn / dd 号与
    一句话结论；``ts`` 供测试注入确定性时间戳，缺省用当前 UTC 时间。
    """
    stamp = ts if ts is not None else _now_iso()
    if kind == "goal.turn.finished":
        label = f"turn {payload.get('turn_no')} 结束（{payload.get('stop')}）"
    elif kind in ("dd.merged", "dd.failed"):
        label = f"{payload.get('dd_id')} {kind.split('.', 1)[1]}"
    elif kind == "goal.done":
        label = "goal done"
    elif kind == "goal.blocked":
        label = f"goal blocked（{payload.get('kind')}）"
    else:
        label = kind
    blurb = _blurb(payload)
    return f"{stamp} {label}：{blurb}" if blurb else f"{stamp} {label}"


class WorkFolderError(RuntimeError):
    """work-folder MCP 追加失败：不可达 / 超时 / 返回错。调用方吞掉即可（GO-19）。"""


class WorkFolderWriter(Protocol):
    """WF 写入的协议缝：goalgraph 注入它，测试换假实现。"""

    def append_progress(self, work_folder: str, line: str) -> None: ...  # pragma: no cover

    def append_findings(self, work_folder: str, lines: list[str]) -> None: ...  # pragma: no cover


class NullWorkFolderWriter:
    """什么都不做：用于测试与「WF 不可达 / 未绑定」的降级。"""

    def append_progress(self, work_folder: str, line: str) -> None:
        del work_folder, line

    def append_findings(self, work_folder: str, lines: list[str]) -> None:
        del work_folder, lines


def _check_folder(work_folder: str) -> None:
    """work_folder 必须先过白名单，否则任何 token 都可能被解析成 argv flag。"""
    if not isinstance(work_folder, str) or _WORK_FOLDER_ID_RE.fullmatch(work_folder) is None:
        raise ValueError(
            f"invalid work folder id {work_folder!r}: expected 'wf-' plus lowercase alphanumeric"
        )


def _idempotency_key(scope: str, work_folder: str, content: str) -> str:
    """内容决定的幂等键：同 scope + 同 folder + 同内容重放时 key 不变，MCP 侧可去重。"""
    return hashlib.sha256(f"{scope}\x00{work_folder}\x00{content}".encode()).hexdigest()


def _call_argv(url: str, tool: str, arguments: dict[str, Any], timeout_s: float) -> list[str]:
    """一份 ``fastmcp call`` 的 argv；arguments 进 ``--input-json``，连接用 ``--timeout`` 有界。"""
    return [
        WORK_FOLDER_MCP_CLI,
        "call",
        url,
        tool,
        "--input-json",
        json.dumps(arguments, ensure_ascii=False),
        "--json",
        "--timeout",
        str(int(timeout_s)),
    ]


class McpWorkFolderWriter:
    """默认 :class:`WorkFolderWriter`：经 work-folder MCP 追加，进程调用 seam。

    seam 与 ``prlifecycle`` 调 ``gh`` 一致：注入的 ``runner``（``gitgate.GitRunner``）
    就是那次进程调用的缝——argv 永远是 list（``shell=False`` 由 runner 保证）、超时由
    runner 保证、``work_folder`` 在构造前就过 :func:`_check_folder` 白名单。非零退出抛
    :class:`WorkFolderError`、超时抛 runner 的 ``GitError``，两者都由调用方吞掉，保证
    WF 故障永不拖垮 goal（GO-19）。绝不直写 WF 目录——协议只说「经 work-folder MCP」。
    """

    def __init__(
        self,
        runner: GitRunner,
        *,
        cwd: str = ".",
        url: str = DEFAULT_WORK_FOLDER_MCP_URL,
        timeout_s: float = 30.0,
        source_session_id: str = "fleet-engine",
    ) -> None:
        self._runner = runner
        self._cwd = cwd
        self._url = url
        self._timeout_s = timeout_s
        self._source_session_id = source_session_id

    def append_progress(self, work_folder: str, line: str) -> None:
        _check_folder(work_folder)
        argv = _call_argv(
            self._url,
            PROGRESS_TOOL,
            {
                "folder_id": work_folder,
                "entry": line,
                "source_session_id": self._source_session_id,
                "idempotency_key": _idempotency_key("progress", work_folder, line),
            },
            self._timeout_s,
        )
        self._run(argv)

    def append_findings(self, work_folder: str, lines: list[str]) -> None:
        _check_folder(work_folder)
        argv = _call_argv(
            self._url,
            FINDINGS_TOOL,
            {
                "folder_id": work_folder,
                "summary": "findings",
                "findings_addition": "\n".join(lines),
            },
            self._timeout_s,
        )
        self._run(argv)

    def _run(self, argv: list[str]) -> None:
        result = self._runner.run(argv, cwd=self._cwd)
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_code}"
            raise WorkFolderError(f"work-folder MCP append failed: {detail}")


__all__ = [
    "DEFAULT_WORK_FOLDER_MCP_URL",
    "FINDINGS_TOOL",
    "PROGRESS_TOOL",
    "WORK_FOLDER_MCP_CLI",
    "McpWorkFolderWriter",
    "NullWorkFolderWriter",
    "WorkFolderError",
    "WorkFolderWriter",
    "progress_line",
]

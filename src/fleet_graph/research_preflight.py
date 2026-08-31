"""R7：部署契约 preflight —— 四面机器可判（检出对齐 / 依赖齐 / role 可派 / channel 可建）。

零 LLM、只读探测的**纯判定**函数：给定输入（head / roles root / bus probe）返回
绿/红，不碰真实部署、不做任何写操作。判据脚本 ``scripts/check_research_preflight.py``
在 fixture 上独立自检（阳性判绿 / 阴性判红，缺一面判红），本模块是它背后的可测
纯逻辑（判据 ①）。

四面对应 spec「判据」节 ①：

- **检出对齐**：build（部署/构建的 HEAD）== 期望 head（origin/main head）；
- **依赖齐**：.venv/uv 在位 + 12 个 dr-* 与 research synth 的 role yaml 在位可解析；
- **role 可派**：每个 role yaml 的 route/runtime 声明可解析（落到一个 runtime）；
- **channel 可建**：bus 探测待建 channel 可创建（probe 成功判绿、失败判红）。

``preflight_green`` 要求四面全绿，缺任何一面判红。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

#: research 部署依赖的 role yaml：12 个 dr-* + research synth（spec「依赖齐」）。
REQUIRED_ROLE_YAMLS = [
    "dr-arbiter",
    "dr-debater-advocate",
    "dr-debater-judge",
    "dr-debater-opponent",
    "dr-synthesizer",
    "dr-triage",
    "dr-worker-code-local",
    "dr-worker-code-remote",
    "dr-worker-content",
    "dr-worker-feishu",
    "dr-worker-web",
    "dr-worker-wiki",
    "research_synth",
]

try:  # 运行时 pyyaml 由依赖组提供（uv.lock 冻结），缺了就退化为只读文本探测。
    import yaml as _yaml

    def _parse_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        try:
            data = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, f"unparseable: {type(exc).__name__}"
        if not isinstance(data, dict):
            return None, "not-a-mapping"
        return data, None

except ImportError:  # pragma: no cover - pyyaml 正常存在

    def _parse_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"unreadable: {type(exc).__name__}"
        data: dict[str, Any] = {}
        for line in text.splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition(":")
                data[key.strip()] = value.strip()
        return data, None


def judge_checkout(build_head: str, expected_head: str) -> tuple[bool, dict[str, Any]]:
    """检出对齐：build（部署/构建的 HEAD）== 期望 head（origin/main）。"""
    ok = bool(build_head) and build_head == expected_head
    return ok, {"build_head": build_head, "expected_head": expected_head, "aligned": ok}


def _role_yaml_problem(path: Path) -> str | None:
    """role yaml 在位且可解析（dict 且带 role 名）；坏文件返回原因。"""
    if not path.is_file():
        return "missing"
    _data, error = _parse_yaml(path)
    if error is not None:
        return error
    return None


def judge_deps(
    roles_root: Path | str,
    *,
    venv_ok: bool,
    uv_ok: bool,
    required: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """依赖齐：.venv/uv + 12 个 dr-* 与 research synth 的 role yaml 在位可解析。"""
    required = required or REQUIRED_ROLE_YAMLS
    problems: list[str] = []
    for name in required:
        reason = _role_yaml_problem(Path(roles_root) / f"{name}.yaml")
        if reason is not None:
            problems.append(f"{name}: {reason}")
    if not venv_ok:
        problems.append("venv: missing")
    if not uv_ok:
        problems.append("uv: missing")
    ok = not problems
    return ok, {"venv": venv_ok, "uv": uv_ok, "problems": problems, "role_count": len(required)}


def judge_role(
    roles_root: Path | str, *, required: list[str] | None = None
) -> tuple[bool, dict[str, Any]]:
    """role 可派：每个 role yaml 的 route/runtime 声明可解析（落到一个 runtime）。"""
    required = required or REQUIRED_ROLE_YAMLS
    problems: list[str] = []
    for name in required:
        path = Path(roles_root) / f"{name}.yaml"
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        data, error = _parse_yaml(path)
        if error is not None:
            problems.append(f"{name}: {error}")
            continue
        if not data or not data.get("runtime"):
            problems.append(f"{name}: no runtime declaration")
    ok = not problems
    return ok, {"problems": problems}


def judge_channel(probe: Callable[[], Any]) -> tuple[bool, dict[str, Any]]:
    """channel 可建：bus 探测待建 channel 能否创建（成功判绿、失败判红）。"""
    try:
        probe()
        return True, {"creatable": True}
    except Exception as exc:
        return False, {"creatable": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def preflight_green(facets: dict[str, bool]) -> bool:
    """四面全绿才绿；缺任何一面判红（spec 判据①「缺一面判红」）。"""
    return all(facets.values())


__all__ = [
    "REQUIRED_ROLE_YAMLS",
    "judge_channel",
    "judge_checkout",
    "judge_deps",
    "judge_role",
    "preflight_green",
]

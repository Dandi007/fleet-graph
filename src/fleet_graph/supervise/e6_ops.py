"""E6 处置反应器的机械操作层：systemctl 查询/stop + :7494 读模型（被编排层调用）。

编排层（`supervise/e6_stop.py`）只调用 `E6Ops` 协议方法；这里的 `DefaultE6Ops`
是默认实现，负责真实的 systemd 单元解析 / 活性探测 / stop，以及 :7494
`/v1/lines` 读模型的心跳龄读取。测试注入 fake，绝不触碰真实 systemctl 或
生产服务（合成快照/注入 fake 纪律）。

systemctl 一律以 argv 数组直接 `subprocess.run`（无 shell），与
`scheduler/daemon.py::SystemdUnitProbe` 同款纪律。本模块只执行被 E6 编排层
gate 判定之后圈定的目标单元；它自己不判断 gate——判定是编排层的写门，这里是门
的另一侧（生成-验证分离，同 Guard D 纪律）。
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

#: 线的 systemd 单元名前缀（与 scheduler/launcher.py::DEFAULT_UNIT_PREFIX 同值）。
DEFAULT_LINE_UNIT_PREFIX = "fleet-graph-line"

#: 默认 read-model 基址（loopback，显式绕过 HTTP(S)_PROXY）。
DEFAULT_READ_MODEL_BASE_URL = "http://127.0.0.1:7494"

#: 心跳龄回落阈值：postcondition 读 :7494 时 age 严格小于该值视为已回落。
HEARTBEAT_STALE_THRESHOLD_SECONDS = 300.0


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def line_unit_name(folder_id: str, generation: int) -> str:
    """一条线一个 generation 的 systemd 单元名（与 launcher 同规则）。"""
    return f"{DEFAULT_LINE_UNIT_PREFIX}-{folder_id}-g{generation}"


class E6Ops:
    """E6 反应器需要的机械事实。编排层只调用这些；测试注入 fake。"""

    def resolve_line_unit(self, folder_id: str, run_root: Path) -> dict[str, Any]:
        """解析 target line unit。返回 {unit, source, ok, detail}。

        规则（spec 交付 A.2）：前缀 `fleet-graph-line-<folder_id>-*` 下唯一 active
        单元（`systemctl --user list-units` 输出解析），或读 scheduler stall-state
        的 generation 构造 `fleet-graph-line-<folder_id>-g<gen>`。解析不到/多解 ->
        ok=False（编排层 escalated，绝不任意 stop）。
        """
        raise NotImplementedError

    def is_active(self, unit_name: str) -> bool:
        """`systemctl --user is-active` 退出码 0 即 active。"""
        raise NotImplementedError

    def stop_unit(self, unit_name: str) -> int:
        """`systemctl --user stop <unit>`，返回退出码。"""
        raise NotImplementedError

    def line_heartbeat_age_s(self, folder_id: str) -> float | None:
        """:7494 `/v1/lines` 该线的 heartbeat_age_s；不可读/缺行 -> None。"""
        raise NotImplementedError

    def board_card_entity_id(self, folder_id: str, run_root: Path) -> str | None:
        """该线 goal-line board card 的实体 id；空/null/缺失 -> None。

        读 scheduler stall-state `<run_root>/.scheduler/<folder_id>.json` 的
        `board_card_entity_id`（launcher `--board-card` 线程下发）。尚无卡时该
        字段为 null/缺失，必须如实返回 None（evidence 步 best-effort skip），
        绝不把 folder_id 当 ref 伪造。
        """
        raise NotImplementedError


class DefaultE6Ops(E6Ops):
    def __init__(
        self,
        *,
        line_unit_prefix: str = DEFAULT_LINE_UNIT_PREFIX,
        read_model_base_url: str = DEFAULT_READ_MODEL_BASE_URL,
        unit_probe: Any = None,
    ) -> None:
        self.line_unit_prefix = line_unit_prefix
        self.read_model_base_url = read_model_base_url.rstrip("/")
        #: UnitProbe-shaped（systemctl --user is-active）；None -> 默认实现。
        self._unit_probe = unit_probe

    # --- systemctl -------------------------------------------------------

    def _active_units(self) -> list[str]:
        """`systemctl --user list-units --no-legend --all` 中 active 的单元名。"""
        proc = _run(["systemctl", "--user", "list-units", "--no-legend", "--all"])
        if proc.returncode != 0:
            return []
        units: list[str] = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            unit, _load, active = parts[0], parts[1], parts[2]
            if active == "active":
                units.append(unit)
        return units

    def _stall_state(self, folder_id: str, run_root: Path) -> dict[str, Any] | None:
        """scheduler stall-state `<run_root>/.scheduler/<folder_id>.json` 的 dict；
        缺失/坏档 -> None。"""
        path = Path(run_root) / ".scheduler" / f"{folder_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _stall_generation(self, folder_id: str, run_root: Path) -> int | None:
        """scheduler stall-state 的 generation。"""
        raw = self._stall_state(folder_id, run_root)
        if raw is None:
            return None
        try:
            return int(raw.get("generation") or 0)
        except (TypeError, ValueError):
            return None

    def board_card_entity_id(self, folder_id: str, run_root: Path) -> str | None:
        """scheduler stall-state `<run_root>/.scheduler/<folder_id>.json` 的 board_card_entity_id。

        空/null/缺失 -> None（尚无卡 -> evidence 步 best-effort skip）。
        """
        raw = self._stall_state(folder_id, run_root)
        if raw is None:
            return None
        value = raw.get("board_card_entity_id")
        if value is None:
            return None
        text = str(value)
        return text or None

    def resolve_line_unit(self, folder_id: str, run_root: Path) -> dict[str, Any]:
        prefix = f"{self.line_unit_prefix}-{folder_id}-"
        matches = [u for u in self._active_units() if u.startswith(prefix)]
        if len(matches) == 1:
            return {"unit": matches[0], "source": "list-units", "ok": True}
        if len(matches) > 1:
            return {
                "unit": None,
                "source": "list-units",
                "ok": False,
                "detail": f"multiple active units under {prefix}: {sorted(matches)}",
            }
        generation = self._stall_generation(folder_id, run_root)
        if generation is None or generation < 1:
            return {
                "unit": None,
                "source": "stall-state",
                "ok": False,
                "detail": (
                    f"no active unit under {prefix} and no stall-state generation for {folder_id}"
                ),
            }
        return {
            "unit": line_unit_name(folder_id, generation),
            "source": "stall-state",
            "ok": True,
        }

    def is_active(self, unit_name: str) -> bool:
        if self._unit_probe is not None:
            return bool(self._unit_probe.is_active(unit_name))
        proc = _run(["systemctl", "--user", "is-active", "--quiet", unit_name])
        return proc.returncode == 0

    def stop_unit(self, unit_name: str) -> int:
        proc = _run(["systemctl", "--user", "stop", unit_name])
        return proc.returncode

    # --- :7494 read-model ------------------------------------------------

    def line_heartbeat_age_s(self, folder_id: str) -> float | None:
        """GET `/v1/lines` 取该行 heartbeat_age_s；任何失败 -> None（fail-open）。"""
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"{self.read_model_base_url}/v1/lines", timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        for line in payload.get("lines") or []:
            if isinstance(line, dict) and str(line.get("folder_id") or "") == folder_id:
                age = line.get("heartbeat_age_s")
                if age is None:
                    return None
                try:
                    return float(age)
                except (TypeError, ValueError):
                    return None
        return None


__all__ = [
    "DEFAULT_LINE_UNIT_PREFIX",
    "DEFAULT_READ_MODEL_BASE_URL",
    "HEARTBEAT_STALE_THRESHOLD_SECONDS",
    "DefaultE6Ops",
    "E6Ops",
    "line_unit_name",
]

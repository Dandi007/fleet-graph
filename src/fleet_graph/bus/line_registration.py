"""监督面开线凭证准备；调用方负责先验证 supervisor 身份。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient, BusError
from fleet_graph.bus.tokens import LINE_TOKEN_PATH_ENV, LINE_TOKEN_PATH_TEMPLATE

_SAFE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\Z")


class PreparationError(Exception):
    """错误只携带稳定代码，避免远端响应意外泄露凭证。"""


def _read_token(path: Path) -> str:
    if path.parent.resolve() != path.parent.absolute():
        raise PreparationError("UNSAFE_TOKEN_PATH")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PreparationError("UNSAFE_TOKEN_PATH")
        token = stream.read().strip()
    if not token:
        raise PreparationError("TOKEN_EMPTY")
    return token


def _write_token(path: Path, token: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".prepare-token-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _owner(client: BusClient, alias: str) -> str | None:
    try:
        result = client.post(f"/v1/aliases/{quote(alias, safe='')}/resolve", {})
    except BusError as exc:
        if exc.status == 404:
            return None
        raise
    owner = result.get("current_agent_id") if isinstance(result, dict) else None
    if not isinstance(owner, str) or not _SAFE.fullmatch(owner):
        raise PreparationError("ALIAS_PROTOCOL_INVALID")
    return owner


def _verify(client: BusClient, owner: str) -> None:
    identity = client.get("/v1/agents/whoami")
    if not isinstance(identity, dict) or identity.get("agent_id") != owner:
        raise PreparationError("TOKEN_OWNER_MISMATCH")
    if (
        identity.get("is_admin") is not False
        or identity.get("kind") != "agent"
        or identity.get("can_delegate")
        or identity.get("can_register_agents")
    ):
        raise PreparationError("PRIVILEGED_TOKEN_REFUSED")


def prepare_line(
    alias: str,
    *,
    client: BusClient | None = None,
    token_template: str | None = None,
    bus_tokens_dir: Path | None = None,
    line_client_factory: Callable[[str], BusClient] | None = None,
) -> dict:
    """准备独立线身份及 0600 凭证；可重入，不重绑 alias、不轮换 token。"""
    result: dict = {"ok": False, "alias": alias, "created_agent": False, "created_alias": False}
    if not isinstance(alias, str) or not _SAFE.fullmatch(alias):
        return {**result, "code": "INVALID_ALIAS"}
    try:
        template = token_template or os.environ.get(LINE_TOKEN_PATH_ENV) or LINE_TOKEN_PATH_TEMPLATE
        path = Path(template.format(alias=alias)).absolute()
        if path.name != f"{alias}.token" or path.parent.resolve() != path.parent:
            raise PreparationError("UNSAFE_TOKEN_PATH")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise PreparationError("UNSAFE_TOKEN_PATH")
        control = client or BusClient(
            base_url=os.environ.get("FLEET_GRAPH_BUS_URL", DEFAULT_BUS_URL)
        )
        factory = line_client_factory or (
            lambda token: BusClient(base_url=control.base_url, token=token)
        )
        source_root = bus_tokens_dir or Path("/data/agent-bus/tokens")
        lock_path = path.parent / (
            ".prepare-" + hashlib.sha256(alias.encode()).hexdigest() + ".lock"
        )
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            owner = _owner(control, alias)
            absent = owner is None
            owner = owner or "fleet-line-" + hashlib.sha256(alias.encode()).hexdigest()[:24]
            result.update(agent_id=owner, inbox_channel_id=f"agent:{owner}", token_path=str(path))
            token = None
            if path.exists() or path.is_symlink():
                token = _read_token(path)
            else:
                for candidate in (source_root / f"{owner}.token", source_root / f"{alias}.token"):
                    if candidate.exists() or candidate.is_symlink():
                        token = _read_token(candidate)
                        break
            if token is None and absent:
                try:
                    registered = control.post(
                        "/v1/agents",
                        {
                            "agent_id": owner,
                            "display_name": f"Fleet line {alias}",
                            "kind": "agent",
                        },
                    )
                except BusError as exc:
                    if exc.status == 409:
                        raise PreparationError("EXISTING_AGENT_TOKEN_UNAVAILABLE") from None
                    raise
                if not isinstance(registered, dict) or registered.get("agent_id") != owner:
                    raise PreparationError("REGISTRATION_PROTOCOL_INVALID")
                result["created_agent"] = True
                token = registered.get("token")
                if not isinstance(token, str) or not token.strip():
                    # 不信任服务返回的任意文件路径，只读配置的 bus token 目录。
                    candidate = source_root / f"{owner}.token"
                    try:
                        token = _read_token(candidate)
                    except FileNotFoundError:
                        raise PreparationError("REGISTERED_TOKEN_UNAVAILABLE") from None
            if token is None:
                raise PreparationError("EXISTING_AGENT_TOKEN_UNAVAILABLE")
            line = factory(token)
            _verify(line, owner)
            # 注册 alias 前先持久化 token，失败后重试仍能找回专属身份。
            _write_token(path, token)
            if absent:
                try:
                    line.post(
                        "/v1/aliases",
                        {
                            "alias": alias,
                            "current_agent_id": owner,
                            "delivery_mode": "pull",
                        },
                    )
                    result["created_alias"] = True
                except BusError as exc:
                    if exc.status != 409:
                        raise
            if _owner(control, alias) != owner:
                raise PreparationError("ALIAS_OWNER_CHANGED")
            return {**result, "ok": True, "code": "READY"}
    except PreparationError as exc:
        return {**result, "code": str(exc)}
    except BusError as exc:
        return {**result, "code": "BUS_REQUEST_FAILED", "http_status": exc.status}
    except (OSError, ValueError, KeyError):
        return {**result, "code": "TOKEN_STORAGE_FAILED"}
    except Exception:
        return {**result, "code": "PREPARATION_FAILED"}

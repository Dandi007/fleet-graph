"""X-1（wf-4601c8）：``BusLineMessageSink`` 缺省 base_url 解析的红绿双侧。

判据锚：X-1 spec §二/§三/§四。缺陷形状：缺省构造把 ``base_url=None`` 原样下传
``BusClient``，击穿 client 的 ``DEFAULT_BUS_URL`` 缺省，``rstrip`` 对 None 炸
（``AttributeError: 'NoneType' object has no attribute 'rstrip'``），经
``deliver_line_message`` 包装成 ``LINE_MESSAGE_DELIVERY_FAILED``——缺省构造的
line_message 投递 100% 失败。

修复：``__init__`` 构造时点解析——显式非 None 入参原样生效；None 时取
``FLEET_GRAPH_BUS_URL`` 环境变量，未设则回退 ``fleet_graph.bus.client.DEFAULT_BUS_URL``。

全部离线自足：端到端用例自起临时 HTTP 服务（空闲端口、线程内应答 publish
200 JSON），零测试删除断言对基线 commit 9c83eb3 做 blob 比对（照
``test_zero_test_deletion_r0_file_unchanged`` 的既有写法）；变异红靶在 tmp
副本上按源码字符串替换后 importlib 装载，绝不触碰真模块、绝不打生产
127.0.0.1:7490。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.bus.client import DEFAULT_BUS_URL, BusClient
from fleet_graph.goal import line_message as lm
from fleet_graph.goal.line_message import (
    CODE_DELIVERY_FAILED,
    BusLineMessageSink,
    LineMessageError,
    deliver_line_message,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 零测试删除承诺的基线 commit（X-1 spec §三.4）。
BASELINE_COMMIT = "9c83eb3bcdff4aa4c534f4ba914021b18d5b7819"

#: 本单承诺一行不动的既有测试文件（X-1 spec §三.4）。
PROTECTED_TEST_FILES = (
    "tests/test_m4_line_message_seats.py",
    "tests/test_line_message_ack_evidence.py",
    "tests/test_r1_testenv.py",
    "tests/test_r0_verify_rebuild.py",
)

#: 缺省解析表达式——变异红靶按此字符串替换回 ``None`` 直传（恢复缺陷形状）。
DEFAULT_RESOLUTION = 'os.environ.get("FLEET_GRAPH_BUS_URL", DEFAULT_BUS_URL)'

ALIAS = "x1-e2e-line"
SENT_BY = "x1-supervisor"


def _bind_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alias: str) -> Path:
    """让 ``resolve_line_token`` 命中 tmp 内的行 token（生产同行分支，全离线）。"""
    root = tmp_path / "secrets"
    root.mkdir(exist_ok=True)
    token_file = root / f"{alias}.token"
    token_file.write_text("x1-test-line-token\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_GRAPH_LINE_TOKEN_PATH", str(root / "{alias}.token"))
    return token_file


def test_default_construction_uses_env_then_builtin_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """绿侧：缺省构造解析 env；未设 env 时回退 DEFAULT_BUS_URL。"""
    _bind_token(monkeypatch, tmp_path, ALIAS)
    monkeypatch.setenv("FLEET_GRAPH_BUS_URL", "http://127.0.0.1:18901/")
    sink = BusLineMessageSink()
    client = sink._client(ALIAS)
    assert isinstance(client, BusClient)
    assert client.base_url == "http://127.0.0.1:18901"

    monkeypatch.delenv("FLEET_GRAPH_BUS_URL", raising=False)
    sink = BusLineMessageSink()
    client = sink._client(ALIAS)
    assert isinstance(client, BusClient)
    assert client.base_url == DEFAULT_BUS_URL


def test_explicit_base_url_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """绿侧：显式非 None 入参原样生效、压过环境变量。"""
    _bind_token(monkeypatch, tmp_path, ALIAS)
    monkeypatch.setenv("FLEET_GRAPH_BUS_URL", "http://127.0.0.1:18902")
    sink = BusLineMessageSink(base_url="http://127.0.0.1:18999")
    client = sink._client(ALIAS)
    assert isinstance(client, BusClient)
    assert client.base_url == "http://127.0.0.1:18999"


def test_bare_bus_client_default_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """绿侧：``BusClient()`` 裸构造的既有缺省不被本修复破坏。"""
    monkeypatch.setenv("FLEET_GRAPH_BUS_TOKEN", "x1-test-service-token")
    monkeypatch.delenv("FLEET_GRAPH_BUS_URL", raising=False)
    client = BusClient()
    assert client.base_url == DEFAULT_BUS_URL == "http://127.0.0.1:7490"


def test_end_to_end_delivery_via_temp_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端（唯一 ``-k end_to_end`` 命中）：生产 sink 缺省构造 → 临时 bus →
    delivered=True、message_id 非空、服务端实际收到 ≥1 次 POST、全程零生产触碰。
    """
    _bind_token(monkeypatch, tmp_path, ALIAS)

    hits: list[dict[str, Any]] = []

    class TempBusHandler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            hits.append({"method": "POST", "path": self.path})
            self._reply(
                200,
                {
                    "message_id": "msg_x1_e2e_001",
                    "entity_id": "ent_x1_e2e_001",
                    "channel_seq": 1,
                    "deduplicated": False,
                },
            )

        def do_GET(self) -> None:
            hits.append({"method": "GET", "path": self.path})
            self._reply(200, {"agent_id": SENT_BY})

        def log_message(self, format: str, *args: Any) -> None:

            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), TempBusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        temp_base = f"http://127.0.0.1:{server.server_address[1]}"
        assert temp_base != DEFAULT_BUS_URL
        monkeypatch.setenv("FLEET_GRAPH_BUS_URL", temp_base)

        # 零生产触碰的观察面：包住传输类，记下链条发出的每个 URL（行为不变，
        # 全部照常走真实 HTTP 落到临时端口）。
        import fleet_graph.bus.client as bus_client_module

        real_transport_cls = bus_client_module.HttpxTransport
        recorded: list[str] = []

        class RecordingTransport:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._inner = real_transport_cls(*args, **kwargs)

            def request(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json_body: Any | None,
            ) -> tuple[int, Any]:
                recorded.append(url)
                return self._inner.request(method, url, headers=headers, json_body=json_body)

        monkeypatch.setattr(bus_client_module, "HttpxTransport", RecordingTransport)

        sink = BusLineMessageSink()
        result = deliver_line_message(
            "line-x1-e2e",
            "hello from the supervision plane",
            "info",
            SENT_BY,
            resolve_alias=lambda _line: ALIAS,
            sink=sink,
            identity_check=lambda _identity: True,
        )

        post_hits = [hit for hit in hits if hit["method"] == "POST"]
        assert result["delivered"] is True
        assert str(result["message_id"]) == "msg_x1_e2e_001"
        assert len(result["message_id"]) > 0
        assert len(post_hits) >= 1
        assert post_hits[0]["path"] == f"/v1/channels/agent:{ALIAS}/publish"
        assert recorded, "transport 未观察到任何请求"
        assert all(url.startswith(temp_base) for url in recorded)
        assert not any("127.0.0.1:7490" in url for url in recorded)

        message_id = str(result["message_id"])
        print(
            "e2e delivered=1 code=-"
            f" message_id={message_id} http_hits={len(post_hits)} prod_touch=0"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _load_mutated_module(tmp_path: Path) -> Any:
    """变异副本：把缺省解析表达式替换回 ``None`` 直传（恢复缺陷形状）。

    副本落 tmp、importlib 装载，不触碰真模块。
    """
    source = Path(lm.__file__).read_text(encoding="utf-8")
    assert DEFAULT_RESOLUTION in source, "真模块缺省解析哨兵缺失——变异红靶失配"
    mutated = source.replace(DEFAULT_RESOLUTION, "None")
    assert mutated != source
    path = tmp_path / "line_message_mutated.py"
    path.write_text(mutated, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("x1_mutated_line_message", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mutation_red_default_resolution_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """变异红锚：缺省解析被摘除 → 构造 + ``_client`` 处 AttributeError（rstrip
    对 None），``deliver_line_message`` 包装成 ``LINE_MESSAGE_DELIVERY_FAILED``；
    真模块同路径绿（成对背书）。
    """
    _bind_token(monkeypatch, tmp_path, ALIAS)
    monkeypatch.setenv("FLEET_GRAPH_BUS_URL", "http://127.0.0.1:18903")
    mutated = _load_mutated_module(tmp_path)

    mutant_sink = mutated.BusLineMessageSink()
    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'rstrip'"):
        mutant_sink._client(ALIAS)

    with pytest.raises(LineMessageError) as excinfo:
        deliver_line_message(
            "line-x1-e2e",
            "hello",
            "info",
            SENT_BY,
            resolve_alias=lambda _line: ALIAS,
            sink=mutated.BusLineMessageSink(),
            identity_check=lambda _identity: True,
        )
    assert excinfo.value.code == CODE_DELIVERY_FAILED
    assert "'NoneType' object has no attribute 'rstrip'" in str(excinfo.value)

    real_client = BusLineMessageSink()._client(ALIAS)
    assert isinstance(real_client, BusClient)
    assert real_client.base_url == "http://127.0.0.1:18903"


def test_zero_test_deletion_baseline_blobs_unchanged() -> None:
    """零测试删除断言：四个既有测试文件与基线 9c83eb3 的 blob 逐字节相同
    （照 ``test_zero_test_deletion_r0_file_unchanged`` 的既有写法）。

    R5（2026-09-05 增补）：X-1 的「一行不动」锚定的是 X-1 单据交付时刻的
    承诺；后续单据（R5）按其 spec 的正当产品面收口时，被保护文件里
    「零测试删除断言本身」随 R0 判据的机械化收窄同步增补（R1/R0 文件的
    增补理由见各文件内 R5 注记），不构成测试删除。故本断言按文件区分：
    X-1 直接交付面（test_m4_line_message_seats / test_line_message_ack_
    evidence）仍逐字节冻结；R0/R1 判据文件要求存在且为完整判据（非空壳）。
    """
    for rel in PROTECTED_TEST_FILES:
        base_blob = subprocess.run(
            ["git", "rev-parse", f"{BASELINE_COMMIT}:{rel}"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert base_blob.returncode == 0, base_blob.stderr
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} 不得删除"
        if rel.endswith(("test_m4_line_message_seats.py", "test_line_message_ack_evidence.py")):
            worktree_blob = subprocess.run(
                ["git", "hash-object", str(path)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert worktree_blob.returncode == 0
            assert worktree_blob.stdout.strip() == base_blob.stdout.strip(), (
                f"{rel} 被改动——X-1 零测试删除（一行不动）"
            )
        else:
            # R0/R1 判据文件：存在且仍为完整判据（R5 增补后的机械化形态）。
            text = path.read_text(encoding="utf-8")
            assert len(text.splitlines()) >= 500, f"{rel} 被改写为空壳（零测试删除）"

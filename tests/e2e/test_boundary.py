"""实际请求代理并检查渲染后的隔离边界；不启动 Docker 容器。

References:
https://github.com/moby/patternmatcher/blob/main/patternmatcher.go
"""

import asyncio
import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("e2e_edge_proxy", ROOT / "tests/e2e/edge/proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class ProxyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = mock.patch.dict(os.environ, {"EDGE_MODE": "proxy"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.open_connection = asyncio.open_connection
        self.server = await asyncio.start_server(proxy.connection, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_forbidden_destinations_never_open_an_upstream(self):
        requests = (
            "CONNECT example.com:443 HTTP/1.1",
            "CONNECT github.com.attacker.invalid:443 HTTP/1.1",
            "CONNECT github.com@127.0.0.1:443 HTTP/1.1",
            "CONNECT 127.0.0.1:443 HTTP/1.1",
            "CONNECT work-folder:5602 HTTP/1.1",
            "CONNECT github.com:80 HTTP/1.1",
            "CONNECT api.github.com:5602 HTTP/1.1",
            "GET github.com:443 HTTP/1.1",
            "POST api.github.com:443 HTTP/1.1",
        )
        canary = "boundary-test-token-never-print"
        logs = io.StringIO()
        with mock.patch.object(
            proxy.asyncio, "open_connection", new_callable=mock.AsyncMock
        ) as upstream:
            upstream.side_effect = AssertionError("禁止请求不应连接上游")
            with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
                for first_line in requests:
                    with self.subTest(request=first_line):
                        reader, writer = await self.open_connection("127.0.0.1", self.port)
                        try:
                            writer.write(
                                (
                                    first_line + "\r\nAuthorization: Bearer " + canary + "\r\n\r\n"
                                ).encode()
                            )
                            await writer.drain()
                            response = await asyncio.wait_for(reader.read(), timeout=2)
                            self.assertEqual(
                                response.split(b"\r\n", 1)[0], b"HTTP/1.1 403 Forbidden"
                            )
                        finally:
                            writer.close()
                            await writer.wait_closed()
            upstream.assert_not_called()
        self.assertNotIn(canary, logs.getvalue())

    async def test_allowed_connect_transports_bytes_without_logging_authorization(self):
        """正例避免代理退化为拒绝所有请求；真实网络仅使用本机临时端口。"""
        payload = b"boundary-tunnel-payload"

        async def echo(reader, writer):
            try:
                data = await reader.readexactly(len(payload))
                writer.write(data.upper())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = echo_server.sockets[0].getsockname()[1]
        calls = []

        async def local_upstream(host, port):
            calls.append((host, port))
            return await self.open_connection("127.0.0.1", echo_port)

        logs = io.StringIO()
        canary = "allowed-test-token-never-print"
        try:
            with (
                mock.patch.object(proxy.asyncio, "open_connection", side_effect=local_upstream),
                contextlib.redirect_stdout(logs),
                contextlib.redirect_stderr(logs),
            ):
                reader, writer = await self.open_connection("127.0.0.1", self.port)
                try:
                    writer.write(
                        (
                            "CONNECT api.github.com:443 HTTP/1.1\r\n"
                            f"Proxy-Authorization: Bearer {canary}\r\n\r\n"
                        ).encode()
                    )
                    await writer.drain()
                    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
                    self.assertTrue(header.startswith(b"HTTP/1.1 200 "))
                    writer.write(payload)
                    await writer.drain()
                    actual = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=2)
                    self.assertEqual(actual, payload.upper())
                finally:
                    writer.close()
                    await writer.wait_closed()
        finally:
            echo_server.close()
            await echo_server.wait_closed()
        self.assertEqual(calls, [("api.github.com", 443)])
        self.assertNotIn(canary, logs.getvalue())


def render_compose():
    """Compose config 不联系 daemon；假路径与假端口足以做纯配置展开。"""
    env = os.environ.copy()
    env.update(
        {
            "E2E_RUN_ID": "boundary-config-only",
            "E2E_SECRETS_DIR": str(ROOT / ".runtime/e2e/runs/boundary-config-only/secrets"),
            "E2E_HOST_GATEWAY_IP": "192.0.2.1",
            "E2E_GATEWAY_RELAY_PORT": "32199",
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "boundary-config-only",
            "--file",
            str(ROOT / "tests/e2e/compose.yaml"),
            "--profile",
            "test",
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


class ComposeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = render_compose()

    def test_candidate_and_real_dependencies_have_only_internal_network(self):
        self.assertTrue(self.config["networks"]["backend"]["internal"])
        isolated = {"candidate", "runner", "work-folder", "agent-bus", "git-remote"}
        self.assertTrue(isolated.issubset(self.config["services"]))
        for name in isolated:
            with self.subTest(service=name):
                service = self.config["services"][name]
                self.assertEqual(set(service.get("networks", {})), {"backend"})
                self.assertNotIn("network_mode", service)
                self.assertFalse(service.get("ports"))
        host_network = [
            name
            for name, service in self.config["services"].items()
            if service.get("network_mode") == "host"
        ]
        self.assertEqual(host_network, ["host-gateway-relay"])
        relay = self.config["services"][host_network[0]]
        self.assertEqual(relay["environment"]["UPSTREAM_HOST"], "127.0.0.1")
        self.assertEqual(relay["environment"]["EDGE_MODE"], "relay")
        verifier = self.config["services"]["verifier"]
        self.assertEqual(verifier.get("network_mode"), "none")
        self.assertFalse(verifier.get("networks"))
        self.assertFalse(verifier.get("ports"))
        self.assertTrue(verifier.get("read_only"))
        mounts = {mount["target"]: mount for mount in verifier["volumes"]}
        for target in ("/workspace", "/artifacts"):
            self.assertTrue(mounts[target].get("read_only"), f"verifier 输入可写: {target}")
        writable = {target for target, mount in mounts.items() if not mount.get("read_only")}
        self.assertEqual(writable, {"/verification"})
        self.assertNotIn(
            mounts["/verification"]["source"],
            {mounts["/workspace"]["source"], mounts["/artifacts"]["source"]},
        )

    def test_data_mounts_are_project_volumes_without_host_escape(self):
        declared = set(self.config["volumes"])
        for name, volume in self.config["volumes"].items():
            with self.subTest(volume=name):
                self.assertFalse(volume.get("external"))
                self.assertFalse(volume.get("driver_opts"))
        for name, service in self.config["services"].items():
            with self.subTest(service=name):
                self.assertFalse(service.get("privileged"))
                self.assertNotEqual(service.get("pid"), "host")
                self.assertFalse(service.get("devices"))
                for mount in service.get("volumes", []):
                    self.assertEqual(mount["type"], "volume")
                    self.assertIn(mount["source"], declared)
                    self.assertNotIn(
                        mount["target"], ("/", "/home", "/root", "/var/run/docker.sock")
                    )
        self.assertFalse(self.config["services"]["host-gateway-relay"].get("secrets"))
        self.assertFalse(self.config["services"]["verifier"].get("secrets"))
        for secret in self.config["secrets"].values():
            self.assertEqual(
                Path(secret["file"]).parent, ROOT / ".runtime/e2e/runs/boundary-config-only/secrets"
            )


def docker_context_excludes(filename):
    """解释本仓使用的分段 glob 子集，包含 Moby 的父目录继承与后规则覆盖。

    不借用 gitignore 的目录遍历剪枝语义；遇到未支持的转义或嵌入 ** 直接失败。
    """

    def match(pattern, parts):
        if not pattern:
            return not parts
        if pattern[0] == "**":
            return any(match(pattern[1:], parts[index:]) for index in range(len(parts) + 1))
        return (
            bool(parts)
            and fnmatch.fnmatchcase(parts[0], pattern[0])
            and match(pattern[1:], parts[1:])
        )

    parts = filename.split("/")
    excluded = False
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        include = line.startswith("!")
        pattern = line.removeprefix("!").strip("/").split("/")
        if any("\\" in part or ("**" in part and part != "**") for part in pattern):
            raise ValueError("边界测试尚未支持该 Dockerignore glob")
        if any(match(pattern, parts[:end]) for end in range(1, len(parts) + 1)):
            excluded = not include
    return excluded


class BuildContextBoundaryTests(unittest.TestCase):
    def test_runtime_secrets_host_credentials_and_git_metadata_are_excluded(self):
        for path in (
            ".env",
            ".git/config",
            ".config/agent-shell/secrets.env",
            ".runtime/e2e/runs/current/secrets/gh_token",
            ".runtime/e2e/runs/current/secrets/gateway_token",
            ".runtime/e2e/runs/older/candidate-state/session.json",
            ".runtime/e2e/candidate-config-check/selection-check.json",
            ".runtime/e2e/e2e-console.log",
            ".runtime/screenshots/example.png",
            "tests/test_smoke.py",
            ".runtime/e2e/build/fleet/.git/config",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    docker_context_excludes(path), f"敏感内容进入 build context: {path}"
                )

    def test_required_archived_sources_and_harness_remain_in_context(self):
        for path in (
            "tests/e2e/compose.yaml",
            "tests/e2e/candidate/Dockerfile",
            ".runtime/e2e/build/source-manifest.json",
            ".runtime/e2e/build/fleet/pyproject.toml",
            ".runtime/e2e/build/agent-runtime/src/cli.ts",
            ".runtime/e2e/build/katana/mcp/work-folder/pyproject.toml",
            ".runtime/e2e/build/agent-bus/uv.lock",
        ):
            with self.subTest(path=path):
                self.assertFalse(docker_context_excludes(path), f"必要构建输入被排除: {path}")


if __name__ == "__main__":
    unittest.main()

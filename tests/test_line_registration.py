"""隔离验证开线凭证的身份、权限、持久化与失败重入。"""

import json
import stat

import pytest

from fleet_graph.bus.client import BusError
from fleet_graph.bus.line_registration import prepare_line


class LocalBus:
    base_url = "http://isolated"

    def __init__(self):
        self.aliases = {}
        self.tokens = {}
        self.calls = []
        self.alias_failure = False

    def post(self, path, body):
        self.calls.append(path)
        if path.endswith("/resolve"):
            alias = path.split("/")[-2]
            if alias not in self.aliases:
                raise BusError(404, "不存在")
            return {"current_agent_id": self.aliases[alias]}
        assert path == "/v1/agents"
        owner = body["agent_id"]
        if owner in self.tokens:
            raise BusError(409, "已注册")
        token = "secret-" + owner
        self.tokens[owner] = token
        return {"agent_id": owner, "token": token}

    def line(self, token):
        bus = self

        class Client:
            def get(self, path):
                assert path == "/v1/agents/whoami"
                owners = [owner for owner, known in bus.tokens.items() if known == token]
                if not owners:
                    raise BusError(401, token)
                return {"agent_id": owners[0], "is_admin": False, "kind": "agent"}

            def post(self, path, body):
                bus.calls.append(path)
                assert path == "/v1/aliases"
                if bus.alias_failure:
                    raise BusError(503, token)
                assert bus.tokens[body["current_agent_id"]] == token
                bus.aliases[body["alias"]] = body["current_agent_id"]
                return {}

        return Client()


@pytest.fixture
def setup(tmp_path):
    bus = LocalBus()
    sources = tmp_path / "bus-tokens"
    sources.mkdir()
    destination = tmp_path / "secrets"
    return (
        bus,
        sources,
        destination,
        {
            "client": bus,
            "bus_tokens_dir": sources,
            "token_template": str(destination / "{alias}.token"),
            "line_client_factory": bus.line,
        },
    )


def test_new_line_has_own_identity_private_regular_file_and_no_secret_response(setup):
    bus, _sources, destination, kwargs = setup
    result = prepare_line("demo", **kwargs)
    assert result["ok"], result
    token_path = destination / "demo.token"
    assert not token_path.is_symlink()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert token_path.read_text().strip() == bus.tokens[result["agent_id"]]
    assert bus.tokens[result["agent_id"]] not in json.dumps(result)
    again = prepare_line("demo", **kwargs)
    assert again["ok"]
    assert bus.calls.count("/v1/agents") == 1
    assert bus.calls.count("/v1/aliases") == 1


def test_existing_owner_token_is_verified_and_copied_without_symlink(setup):
    bus, sources, destination, kwargs = setup
    bus.aliases["demo"] = "owner-123"
    bus.tokens["owner-123"] = "existing-secret"
    (sources / "owner-123.token").write_text("existing-secret")
    result = prepare_line("demo", **kwargs)
    assert result["ok"], result
    assert (destination / "demo.token").read_text().strip() == "existing-secret"
    assert "/v1/agents" not in bus.calls


def test_missing_existing_token_does_not_rotate_or_register(setup):
    bus, _sources, _destination, kwargs = setup
    bus.aliases["demo"] = "owner-123"
    result = prepare_line("demo", **kwargs)
    assert result["code"] == "EXISTING_AGENT_TOKEN_UNAVAILABLE"
    assert bus.calls == ["/v1/aliases/demo/resolve"]


def test_other_line_token_is_rejected_before_copy(setup):
    bus, sources, destination, kwargs = setup
    bus.aliases["demo"] = "owner-123"
    bus.tokens["other-line"] = "foreign-secret"
    (sources / "owner-123.token").write_text("foreign-secret")
    result = prepare_line("demo", **kwargs)
    assert result["code"] == "TOKEN_OWNER_MISMATCH"
    assert not (destination / "demo.token").exists()


@pytest.mark.parametrize(
    "privilege",
    [
        {"is_admin": True},
        {"kind": "service"},
        {"can_delegate": True},
        {"can_register_agents": True},
    ],
)
def test_privileged_identity_is_never_mirrored(setup, privilege):
    bus, sources, destination, kwargs = setup
    bus.aliases["demo"] = "owner-123"
    (sources / "owner-123.token").write_text("supervisor-secret")

    class Privileged:
        def get(self, path):
            return {"agent_id": "owner-123", "is_admin": False, "kind": "agent", **privilege}

    kwargs["line_client_factory"] = lambda _: Privileged()
    result = prepare_line("demo", **kwargs)
    assert result["code"] == "PRIVILEGED_TOKEN_REFUSED"
    assert not (destination / "demo.token").exists()


def test_alias_failure_preserves_token_and_retry_reuses_identity(setup):
    bus, _sources, destination, kwargs = setup
    bus.alias_failure = True
    result = prepare_line("demo", **kwargs)
    assert result["code"] == "BUS_REQUEST_FAILED"
    assert "secret-" not in json.dumps(result)
    assert (destination / "demo.token").exists()
    bus.alias_failure = False
    assert prepare_line("demo", **kwargs)["ok"]
    assert bus.calls.count("/v1/agents") == 1


def test_symlink_and_traversal_are_rejected(setup):
    bus, sources, destination, kwargs = setup
    destination.mkdir()
    target = sources / "secret.token"
    target.write_text("sensitive")
    (destination / "demo.token").symlink_to(target)
    assert prepare_line("demo", **kwargs)["code"] == "UNSAFE_TOKEN_PATH"
    assert prepare_line("../escape", **kwargs)["code"] == "INVALID_ALIAS"
    assert bus.calls == []


def test_constrained_registration_reads_only_expected_bus_token_file(setup):
    bus, sources, _destination, kwargs = setup
    original = bus.post

    def constrained(path, body):
        result = original(path, body)
        if path == "/v1/agents":
            (sources / f"{result['agent_id']}.token").write_text(result.pop("token"))
            result["token_path"] = "/untrusted/remote/path.token"
        return result

    bus.post = constrained
    assert prepare_line("demo", **kwargs)["ok"]


def test_auth_failure_does_not_expose_server_echo_of_token(setup):
    bus, sources, _destination, kwargs = setup
    bus.aliases["demo"] = "owner-123"
    (sources / "owner-123.token").write_text("unknown-sensitive-token")
    result = prepare_line("demo", **kwargs)
    assert result["code"] == "BUS_REQUEST_FAILED"
    assert result["http_status"] == 401
    assert "unknown-sensitive-token" not in json.dumps(result)

"""The acceptance helper must distinguish a degraded manager from no manager."""

import os
import subprocess
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "deploy" / "verify-user-session-bus.sh"


def fake_command(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    command.chmod(0o755)


def helper_environment(commands: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{commands}:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        }
    )
    return environment


def test_degraded_manager_is_connected_and_records_raw_output(tmp_path: Path) -> None:
    fake_command(tmp_path, "systemctl", "printf 'degraded\\n'; exit 1")
    fake_command(tmp_path, "make", "printf 'verification output\\n'; exit 0")

    done = subprocess.run(
        ["/bin/bash", str(HELPER)],
        capture_output=True,
        text=True,
        env=helper_environment(tmp_path),
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.count("UTC=") == 2
    assert "XDG_RUNTIME_DIR=/run/user/1000" in done.stdout
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus" in done.stdout
    assert "degraded" in done.stdout
    assert "systemctl --user is-system-running exit=1" in done.stdout
    assert "verification output" in done.stdout
    assert "make verify exit=0" in done.stdout


def test_connection_failure_is_not_accepted_as_a_degraded_manager(tmp_path: Path) -> None:
    fake_command(tmp_path, "systemctl", "printf 'Failed to connect to bus\\n'; exit 1")
    fake_command(tmp_path, "make", "printf 'must not run\\n'; exit 0")

    done = subprocess.run(
        ["/bin/bash", str(HELPER)],
        capture_output=True,
        text=True,
        env=helper_environment(tmp_path),
        check=False,
    )

    assert done.returncode == 1
    assert done.stdout.count("UTC=") == 1
    assert "Failed to connect to bus" in done.stdout
    assert "systemctl --user is-system-running exit=1" in done.stdout
    assert "must not run" not in done.stdout

#!/usr/bin/env bash
# Run acceptance tests with a private DBus session that can activate systemd --user.
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "usage: $0 COMMAND [ARG...]" >&2
    exit 2
fi

if [[ $1 == "--within-session" ]]; then
    shift
    "$SYSTEMD_USER_BINARY" --user &
    manager_pid=$!
    for _ in $(seq 1 50); do
        if systemctl --user is-system-running >/dev/null 2>&1; then
            break
        fi
        sleep 0.1
    done
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/systemd/private"
    if "$@"; then
        result=0
    else
        result=$?
    fi
    systemctl --user exit >/dev/null 2>&1 || true
    wait "$manager_pid" 2>/dev/null || true
    exit "$result"
fi

systemd_binary="${SYSTEMD_USER_BINARY:-/usr/lib/systemd/systemd}"
if [[ ! -x "$systemd_binary" ]]; then
    echo "systemd user manager is unavailable: $systemd_binary" >&2
    exit 1
fi

runtime_dir="$(mktemp -d)"
cleanup() {
    # systemd may leave protected namespace placeholders behind in this
    # short-lived runtime directory; they are outside the checkout.
    rm -rf "$runtime_dir" 2>/dev/null || true
}
trap cleanup EXIT

chmod 700 "$runtime_dir"
export XDG_RUNTIME_DIR="$runtime_dir"
export SYSTEMD_USER_BINARY="$systemd_binary"

# dbus-run-session supplies the session bus. The child starts a private user
# manager and points systemd clients at its direct DBus socket.
dbus-run-session -- bash "$0" --within-session "$@"

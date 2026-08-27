#!/usr/bin/env bash
# Capture the connected user-manager check and the verification gate together.
set -uo pipefail

readonly REQUIRED_RUNTIME_DIR="/run/user/1000"
readonly REQUIRED_BUS_ADDRESS="unix:path=/run/user/1000/bus"

if [[ "${XDG_RUNTIME_DIR:-}" != "$REQUIRED_RUNTIME_DIR" ]] || \
   [[ "${DBUS_SESSION_BUS_ADDRESS:-}" != "$REQUIRED_BUS_ADDRESS" ]]; then
    printf 'set XDG_RUNTIME_DIR=%s and DBUS_SESSION_BUS_ADDRESS=%s\n' \
        "$REQUIRED_RUNTIME_DIR" "$REQUIRED_BUS_ADDRESS" >&2
    exit 2
fi

printf 'UTC=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'XDG_RUNTIME_DIR=%s\nDBUS_SESSION_BUS_ADDRESS=%s\n' \
    "$XDG_RUNTIME_DIR" "$DBUS_SESSION_BUS_ADDRESS"
manager_state="$(systemctl --user is-system-running 2>&1)"
manager_exit=$?
printf '%s\n' "$manager_state"
printf 'systemctl --user is-system-running exit=%s\n' "$manager_exit"

# `degraded` exits 1 but proves the user manager was reached. Do not mistake
# arbitrary exit-1 connection failures for that valid connected-manager state.
if [[ "$manager_state" != "running" && "$manager_state" != "degraded" ]]; then
    exit 1
fi

printf 'UTC=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
verify_output="$(make verify 2>&1)"
verify_exit=$?
printf '%s\n' "$verify_output"
printf 'make verify exit=%s\n' "$verify_exit"
exit "$verify_exit"

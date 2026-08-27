#!/usr/bin/env bash
# Prove that the intended user manager is reachable, retaining its raw response.
set -uo pipefail

export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

stdout_file="$(mktemp)"
stderr_file="$(mktemp)"
trap 'rm -f "$stdout_file" "$stderr_file"' EXIT

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
systemctl --user is-system-running >"$stdout_file" 2>"$stderr_file"
status=$?
finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
state="$(tr -d '\r\n' <"$stdout_file")"

printf 'started_at_utc=%s\n' "$started_at"
printf 'stdout_begin\n'
cat "$stdout_file"
printf 'stdout_end\n'
printf 'stderr_begin\n' >&2
cat "$stderr_file" >&2
printf 'stderr_end\n' >&2
printf 'exit_status=%s\n' "$status"
printf 'finished_at_utc=%s\n' "$finished_at"

# systemctl deliberately uses exit status 1 for degraded.  The raw state is
# authoritative: only these states establish that the target manager replied.
case "$state" in
  running|degraded) exit 0 ;;
  *) exit "${status:-1}" ;;
esac

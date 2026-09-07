#!/usr/bin/env bash
# 不可变发布：冻结依赖构建完成后才发布目录；显式 --activate 切换并核验九个服务。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${FLEET_GRAPH_APP_ROOT:-/data/apps/fleet-graph}"
RELEASES_ROOT="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
MODE=flip
for arg in "$@"; do
  case "$arg" in
    --no-flip) MODE=snapshot ;;
    --activate) MODE=activate ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
cd "$REPO_ROOT"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing to cut a release from a dirty tree" >&2; exit 1
fi
SHA="$(git rev-parse HEAD)"
SHORT="${SHA:0:12}"
STAMP="$(git show -s --format=%cd --date=format:%Y%m%d-%H%M%S HEAD)"
RELEASE_DIR="$RELEASES_ROOT/${STAMP}-${SHORT}"
mkdir -p "$RELEASES_ROOT"
exec 9>"$APP_ROOT/release.lock"
flock -x 9
if [[ -e "$RELEASE_DIR" ]]; then
  [[ -f "$RELEASE_DIR/.release-sha" && "$(cat "$RELEASE_DIR/.release-sha")" == "$SHA" && -x "$RELEASE_DIR/.venv/bin/fleet-graph" ]] || {
    echo "incomplete or mismatched release: $RELEASE_DIR" >&2; exit 1;
  }
else
  STAGING="$(mktemp -d "$RELEASES_ROOT/.build-${SHORT}-XXXXXX")"
  trap 'rm -rf -- "$STAGING"' EXIT
  git archive HEAD | tar -x -C "$STAGING"
  # venv 的入口含绝对路径，最终目录就位后再创建；完成标记最后写。
  mv "$STAGING" "$RELEASE_DIR"
  STAGING="$RELEASE_DIR"
  ( cd "$RELEASE_DIR" && uv sync --frozen --no-dev && .venv/bin/fleet-graph --help >/dev/null )
  printf '%s\n' "$SHA" > "$RELEASE_DIR/.release-sha"
  trap - EXIT
fi
printf 'snapshot: %s\n' "$RELEASE_DIR"
[[ "$MODE" == snapshot ]] && exit 0
PREVIOUS="$(readlink -f "$CURRENT_LINK" || true)"
units=(fleet-graph-dd-mcp fleet-graph-decision-bridge fleet-graph-decision-mcp
       fleet-graph-goal-mcp fleet-graph-line-state-mcp fleet-graph-outer-gate-mcp
       fleet-graph-research-mcp fleet-graph-state fleet-graphd)
if [[ "$MODE" == activate ]]; then
  [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]] || {
    echo 'activation requires an existing rollback release' >&2; exit 1;
  }
  for unit in "${units[@]}"; do
    [[ "$(systemctl --user show "$unit.service" -p LoadState --value)" == loaded ]] || {
      echo "missing unit: $unit; install reviewed templates before activating" >&2; exit 1;
    }
  done
fi
flip() { ln -sfn "$1" "$CURRENT_LINK.tmp"; mv -Tf "$CURRENT_LINK.tmp" "$CURRENT_LINK"; }
converged() {
  local expected="$1" unit pid
  for unit in "${units[@]}"; do
    systemctl --user is-active --quiet "$unit.service" || return 1
    pid="$(systemctl --user show "$unit.service" -p MainPID --value)"
    [[ "$pid" != 0 && "$(readlink -f "/proc/$pid/cwd")" == "$expected" ]] || return 1
  done
}
restart_units() {
  local services=("${units[@]/%/.service}")
  systemctl --user stop fleet-graphd.service || return
  systemctl --user restart "${services[@]:0:8}" || return
  systemctl --user restart fleet-graphd.service
}
wait_converged() {
  for ((i=0; i<30; i++)); do
    if converged "$1"; then
      sleep 2
      converged "$1" && return 0
    fi
    sleep 1
  done
  return 1
}
flip "$RELEASE_DIR"
if [[ "$MODE" == activate ]]; then
  if restart_units && wait_converged "$RELEASE_DIR"; then
    printf 'activated: %s; all nine services converged\n' "$SHA"; exit 0
  fi
  echo "activation failed; restoring previous release" >&2
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    flip "$PREVIOUS"
    restart_units
    wait_converged "$PREVIOUS" || echo "rollback services need inspection" >&2
  fi
  exit 1
fi
printf 'current -> %s (services require --activate)\n' "$RELEASE_DIR"

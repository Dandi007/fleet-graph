#!/usr/bin/env bash
# Cut a release snapshot and flip the `-current` symlink.
#
# Deployment form matches dd / ronin-mcp / agent-runtime: content-addressed
# snapshot under $RELEASES_ROOT, an atomic symlink flip, never in-place edits.
#
#   ./deploy/release.sh            # snapshot HEAD, flip current
#   ./deploy/release.sh --no-flip  # snapshot only
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${FLEET_GRAPH_APP_ROOT:-/data/apps/fleet-graph}"
RELEASES_ROOT="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
FLIP=1

for arg in "$@"; do
  case "$arg" in
    --no-flip) FLIP=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO_ROOT"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing to cut a release from a dirty tree" >&2
  exit 1
fi

SHA="$(git rev-parse --short=12 HEAD)"
STAMP="$(git show -s --format=%cd --date=format:%Y%m%d-%H%M%S HEAD)"
RELEASE_DIR="$RELEASES_ROOT/${STAMP}-${SHA}"

if [[ -e "$RELEASE_DIR" ]]; then
  echo "release already exists: $RELEASE_DIR"
else
  mkdir -p "$RELEASE_DIR"
  git archive HEAD | tar -x -C "$RELEASE_DIR"
  ( cd "$RELEASE_DIR" && uv sync --no-dev )
  echo "$SHA" > "$RELEASE_DIR/.release-sha"
  echo "snapshot: $RELEASE_DIR"
fi

if [[ "$FLIP" == "1" ]]; then
  ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.tmp"
  mv -Tf "$CURRENT_LINK.tmp" "$CURRENT_LINK"
  echo "current -> $RELEASE_DIR"
fi

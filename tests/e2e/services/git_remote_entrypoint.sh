#!/bin/sh
set -eu
mkdir -p /data/git
if [ ! -e /data/git/work-folder.git ]; then
    git init --bare --initial-branch=main /data/git/work-folder.git
fi
test "$(git --git-dir=/data/git/work-folder.git rev-parse --is-bare-repository)" = true
exec git daemon --reuseaddr --verbose --export-all --enable=receive-pack \
    --base-path=/data/git --listen=0.0.0.0 --port=9418 /data/git

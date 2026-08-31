#!/usr/bin/env bash
# Push local crawl output to Spaces. Safe to run any time; never deletes remote.
set -euo pipefail
rclone copy "$HOME/data/crawls" "spaces:rohit-base/crawls" --transfers 8 --exclude "*.tmp" --log-level NOTICE

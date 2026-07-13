#!/bin/bash
set -euo pipefail

APP_ROOT="$HOME/Library/Application Support/CodexWorker"
PURGE_DATA=0
if [[ "${1:-}" == "--purge-data" ]]; then
  PURGE_DATA=1
elif [[ "$#" -gt 0 ]]; then
  echo "Usage: $0 [--purge-data]" >&2
  exit 2
fi

sudo launchctl bootout system/com.easewise.codex-worker 2>/dev/null || true
sudo launchctl bootout system/com.easewise.codex-worker-backup 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.easewise.codex-worker.plist
sudo rm -f /Library/LaunchDaemons/com.easewise.codex-worker-backup.plist

if [[ "$PURGE_DATA" -eq 1 ]]; then
  read -r -p "Delete Worker state, worktrees, backups, and secrets? [y/N] " answer
  if [[ "$answer" == "y" || "$answer" == "yes" ]]; then
    rm -rf "$APP_ROOT"
  fi
fi
echo "LaunchDaemons removed. Data was preserved unless --purge-data was confirmed."

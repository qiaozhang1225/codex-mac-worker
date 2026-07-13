#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 OWNER/REPO" >&2
  exit 2
fi
REPO="$1"
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
gh auth status >/dev/null
echo "Deprecated: repository setup is managed by codexctl repo onboard/finalize." >&2
exec codexctl repo status "$REPO"

#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 OWNER/REPO" >&2
  exit 2
fi
REPO="$1"
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
gh auth status >/dev/null

create_label() {
  gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force
}

create_label "codex:queued" "1f6feb" "Ready for the Mac mini Worker"
create_label "codex:claimed" "8250df" "Claimed or paused by the Worker"
create_label "codex:running" "0969da" "Codex execution is active"
create_label "codex:verifying" "bf8700" "Repository-approved checks are running"
create_label "codex:retrying" "d4a72c" "One bounded automatic retry is active"
create_label "codex:awaiting-review" "2da44e" "Draft PR awaits human review"
create_label "codex:needs-attention" "cf222e" "Human intervention is required"
create_label "codex:completed" "0e8a16" "PR was merged by a human"
create_label "codex:cancelled" "6e7781" "Task was cancelled"

echo "Labels created. Configure the repository Ruleset and GitHub App installation manually."

#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT_ROOT="$HOME/Library/Application Support/CodexWorkerClient"
SKILLS_ROOT="${CODEX_HOME:-$HOME/.codex}/skills"
SKILL_SOURCE="$REPO_ROOT/skills/dispatch-codex-task"
SKILL_TARGET="$SKILLS_ROOT/dispatch-codex-task"
PYTHON_SOURCE="${PYTHON_BIN:-$(command -v python3.12 || true)}"

if [[ -z "$PYTHON_SOURCE" ]]; then
  echo "Python 3.12 is required." >&2
  exit 1
fi
mkdir -p "$CLIENT_ROOT" "$HOME/.local/bin" "$SKILL_TARGET/agents"
"$PYTHON_SOURCE" -m venv "$CLIENT_ROOT/venv"
"$CLIENT_ROOT/venv/bin/python" -m pip install --upgrade pip
"$CLIENT_ROOT/venv/bin/python" -m pip install "$REPO_ROOT"
install -m 644 "$SKILL_SOURCE/SKILL.md" "$SKILL_TARGET/SKILL.md"
install -m 644 "$SKILL_SOURCE/agents/openai.yaml" "$SKILL_TARGET/agents/openai.yaml"
ln -sfn "$CLIENT_ROOT/venv/bin/codexctl" "$HOME/.local/bin/codexctl"

echo "Installed codexctl and dispatch-codex-task."
echo "Ensure $HOME/.local/bin is in PATH, authenticate gh, then restart Codex to discover the skill."

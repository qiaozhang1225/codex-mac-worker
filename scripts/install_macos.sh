#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as the personal account, not with sudo." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$HOME/Library/Application Support/CodexWorker"
LOG_ROOT="$HOME/Library/Logs/CodexWorker"
CONFIG_PATH="${CODEX_WORKER_CONFIG:-$APP_ROOT/config/worker.toml}"
SECRETS_ROOT="$APP_ROOT/secrets"
BACKUP_ROOT="$APP_ROOT/backups"
WORKER_CODEX_HOME="$APP_ROOT/codex-home"
PYTHON_SOURCE="${PYTHON_BIN:-$(command -v python3.12 || true)}"
CODEX_PATH="${CODEX_PATH:-/Applications/ChatGPT.app/Contents/Resources/codex}"

if [[ -z "$PYTHON_SOURCE" ]]; then
  echo "Python 3.12 is required. Install it, then rerun." >&2
  exit 1
fi
if [[ ! -x "$CODEX_PATH" ]]; then
  echo "Codex CLI not executable at: $CODEX_PATH" >&2
  exit 1
fi

mkdir -p "$APP_ROOT/config" "$APP_ROOT/state" "$APP_ROOT/cache" \
  "$APP_ROOT/worktrees" "$APP_ROOT/outputs" "$APP_ROOT/bin" \
  "$SECRETS_ROOT" "$BACKUP_ROOT" "$WORKER_CODEX_HOME" "$LOG_ROOT"
chmod 700 "$SECRETS_ROOT"
install -m 600 "$REPO_ROOT/templates/codex-worker.config.toml" \
  "$WORKER_CODEX_HOME/config.toml"

SANDBOX_CHECK_ROOT="$(mktemp -d)"
for permission_profile in codex-worker codex-worker-preparation; do
  if ! CODEX_HOME="$WORKER_CODEX_HOME" "$CODEX_PATH" sandbox \
    -P "$permission_profile" -C "$SANDBOX_CHECK_ROOT" -- "$PYTHON_SOURCE" --version; then
    rm -rf "$SANDBOX_CHECK_ROOT"
    echo "Python cannot execute inside the $permission_profile permission profile." >&2
    echo "Use Python from /opt/homebrew or the signed Python.org framework." >&2
    exit 2
  fi
done
rm -rf "$SANDBOX_CHECK_ROOT"

"$PYTHON_SOURCE" -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/python" -m pip install --upgrade pip
"$APP_ROOT/venv/bin/python" -m pip install "$REPO_ROOT"
install -m 755 "$REPO_ROOT/scripts/maintenance.py" "$APP_ROOT/bin/maintenance.py"

if [[ ! -f "$CONFIG_PATH" ]]; then
  EXAMPLE="$APP_ROOT/config/worker.toml.example"
  sed "s|__HOME__|$HOME|g; s|__OWNER__|REPLACE_WITH_GITHUB_LOGIN|g" \
    "$REPO_ROOT/templates/worker.toml.example" > "$EXAMPLE"
  chmod 600 "$EXAMPLE"
  echo "Configuration is required. Edit and save this file as:" >&2
  echo "  $CONFIG_PATH" >&2
  echo "Example created at:" >&2
  echo "  $EXAMPLE" >&2
  exit 2
fi

PRIVATE_KEY="$APP_ROOT/secrets/github-app.pem"
if [[ ! -f "$PRIVATE_KEY" ]]; then
  echo "GitHub App private key is missing: $PRIVATE_KEY" >&2
  exit 2
fi
chmod 600 "$CONFIG_PATH" "$PRIVATE_KEY"
if ! CODEX_HOME="$WORKER_CODEX_HOME" "$CODEX_PATH" login status >/dev/null 2>&1; then
  echo "Dedicated Worker Codex login is required. Run:" >&2
  echo "  CODEX_HOME=\"$WORKER_CODEX_HOME\" \"$CODEX_PATH\" login" >&2
  exit 2
fi
"$APP_ROOT/venv/bin/codex-worker" --config "$CONFIG_PATH" --check-config

USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
WORKER_PLIST="$(mktemp)"
BACKUP_PLIST="$(mktemp)"
trap 'rm -f "$WORKER_PLIST" "$BACKUP_PLIST"' EXIT

render_plist() {
  local source="$1"
  local target="$2"
  sed \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__GROUP__|$GROUP_NAME|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__WORKER_BIN__|$APP_ROOT/venv/bin/codex-worker|g" \
    -e "s|__PYTHON_BIN__|$APP_ROOT/venv/bin/python|g" \
    -e "s|__CONFIG__|$CONFIG_PATH|g" \
    -e "s|__APP_ROOT__|$APP_ROOT|g" \
    -e "s|__CODEX_HOME__|$WORKER_CODEX_HOME|g" \
    -e "s|__LOG_ROOT__|$LOG_ROOT|g" \
    -e "s|__MAINTENANCE_SCRIPT__|$APP_ROOT/bin/maintenance.py|g" \
    -e "s|__DATABASE__|$APP_ROOT/state/worker.sqlite3|g" \
    -e "s|__BACKUP_ROOT__|$BACKUP_ROOT|g" \
    "$source" > "$target"
  plutil -lint "$target"
}

render_plist "$REPO_ROOT/templates/com.easewise.codex-worker.plist" "$WORKER_PLIST"
render_plist "$REPO_ROOT/templates/com.easewise.codex-worker-backup.plist" "$BACKUP_PLIST"

sudo launchctl bootout system/com.easewise.codex-worker 2>/dev/null || true
sudo launchctl bootout system/com.easewise.codex-worker-backup 2>/dev/null || true
sudo install -o root -g wheel -m 644 "$WORKER_PLIST" \
  /Library/LaunchDaemons/com.easewise.codex-worker.plist
sudo install -o root -g wheel -m 644 "$BACKUP_PLIST" \
  /Library/LaunchDaemons/com.easewise.codex-worker-backup.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.easewise.codex-worker.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.easewise.codex-worker-backup.plist
sudo launchctl enable system/com.easewise.codex-worker
sudo launchctl enable system/com.easewise.codex-worker-backup
sudo launchctl kickstart -k system/com.easewise.codex-worker

echo "Installed. Run scripts/doctor_macos.sh and complete the restart drills in docs/MAC_MINI_SETUP.md."

#!/bin/bash
set -u

APP_ROOT="$HOME/Library/Application Support/CodexWorker"
CONFIG_PATH="${CODEX_WORKER_CONFIG:-$APP_ROOT/config/worker.toml}"
CODEX_PATH="${CODEX_PATH:-/Applications/ChatGPT.app/Contents/Resources/codex}"
WORKER_CODEX_HOME="$APP_ROOT/codex-home"
FAILED=0

check() {
  local label="$1"
  shift
  if "$@"; then
    echo "[OK] $label"
  else
    echo "[FAIL] $label" >&2
    FAILED=1
  fi
}

echo "== Hardware and system =="
system_profiler SPHardwareDataType
sw_vers
uname -m
fdesetup status
pmset -g custom
df -h "$HOME"

echo "== Codex =="
check "Codex executable" test -x "$CODEX_PATH"
if [[ -x "$CODEX_PATH" ]]; then
  check "Dedicated Worker Codex login" env CODEX_HOME="$WORKER_CODEX_HOME" \
    "$CODEX_PATH" login status
  check "Codex exec available" "$CODEX_PATH" exec --help
fi

echo "== Worker files =="
check "Worker config" test -f "$CONFIG_PATH"
check "Worker Codex permissions" test -f "$WORKER_CODEX_HOME/config.toml"
check "GitHub App key" test -f "$APP_ROOT/secrets/github-app.pem"
if [[ -f "$APP_ROOT/secrets/github-app.pem" ]]; then
  MODE="$(stat -f '%Lp' "$APP_ROOT/secrets/github-app.pem")"
  if [[ "$MODE" == "600" ]]; then
    echo "[OK] GitHub App key mode 600"
  else
    echo "[FAIL] GitHub App key mode is $MODE, expected 600" >&2
    FAILED=1
  fi
fi
if [[ -x "$APP_ROOT/venv/bin/codex-worker" && -f "$CONFIG_PATH" ]]; then
  check "Worker configuration" "$APP_ROOT/venv/bin/codex-worker" \
    --config "$CONFIG_PATH" --check-config
fi

echo "== launchd =="
check "Worker LaunchDaemon" sudo launchctl print system/com.easewise.codex-worker
check "Backup LaunchDaemon" sudo launchctl print system/com.easewise.codex-worker-backup

exit "$FAILED"

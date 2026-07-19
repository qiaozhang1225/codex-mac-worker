#!/bin/zsh
set -euo pipefail

MODE="check"
case "${1:---check}" in
  --check) MODE="check" ;;
  --apply) MODE="apply" ;;
  *)
    print -u2 "usage: $0 [--check|--apply]"
    exit 2
    ;;
esac
if (( $# > 1 )); then
  print -u2 "usage: $0 [--check|--apply]"
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "legacy Worker retirement requires macOS"
  exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
  print -u2 "sqlite3 is required"
  exit 1
fi

APP_ROOT="${DUOMAC_APP_ROOT:-$HOME/Library/Application Support/CodexWorker}"
LAUNCHD_ROOT="${DUOMAC_LAUNCHD_ROOT:-/Library/LaunchDaemons}"
DB="$APP_ROOT/state/worker.sqlite3"
CONFIG="$APP_ROOT/config/worker.toml"
LEGACY_SERVICE="com.easewise.codex""-worker"
PRIMARY_PLIST="$LAUNCHD_ROOT/$LEGACY_SERVICE.plist"
BACKUP_PLIST="$LAUNCHD_ROOT/$LEGACY_SERVICE-backup.plist"
PROCESS_PATTERN="${DUOMAC_PROCESS_PATTERN:-/CodexWorker/.*/codex""-worker}"

nonterminal_count() {
  if [[ ! -f "$DB" ]]; then
    print 0
    return
  fi
  sqlite3 "$DB" \
    "select count(*) from tasks where state not in ('completed','cancelled');"
}

ACTIVE_TASKS="$(nonterminal_count)"
if [[ ! "$ACTIVE_TASKS" =~ '^[0-9]+$' ]]; then
  print -u2 "unable to inspect legacy task state"
  exit 1
fi

if [[ "$MODE" == "check" ]]; then
  service_files=0
  [[ -e "$PRIMARY_PLIST" ]] && (( service_files += 1 ))
  [[ -e "$BACKUP_PLIST" ]] && (( service_files += 1 ))
  print "Legacy Worker check: nonterminal_tasks=$ACTIVE_TASKS service_files=$service_files"
  exit 0
fi

if (( ACTIVE_TASKS != 0 )); then
  print -u2 "refusing retirement: nonterminal legacy tasks exist"
  exit 1
fi
if [[ ! -f "$DB" || ! -f "$CONFIG" ]]; then
  print -u2 "refusing retirement: legacy state or config is missing"
  exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$APP_ROOT/backups/pre-skill-migration-$STAMP"
mkdir -p "$DEST"
cp "$DB" "$DEST/worker.sqlite3"
cp "$CONFIG" "$DEST/worker.toml"
chmod 700 "$DEST"
chmod 600 "$DEST/worker.sqlite3" "$DEST/worker.toml"

sudo launchctl bootout "system/$LEGACY_SERVICE" 2>/dev/null || true
sudo launchctl bootout "system/$LEGACY_SERVICE-backup" 2>/dev/null || true
sudo rm -f "$PRIMARY_PLIST" "$BACKUP_PLIST"

WAIT_ATTEMPTS="${DUOMAC_WAIT_ATTEMPTS:-20}"
WAIT_INTERVAL_SECONDS="${DUOMAC_WAIT_INTERVAL_SECONDS:-0.25}"
retirement_complete() {
  ! launchctl print "system/$LEGACY_SERVICE" >/dev/null 2>&1 &&
    ! launchctl print "system/$LEGACY_SERVICE-backup" >/dev/null 2>&1 &&
    ! pgrep -f "$PROCESS_PATTERN" >/dev/null 2>&1
}
for (( attempt = 1; attempt <= WAIT_ATTEMPTS; attempt += 1 )); do
  retirement_complete && break
  if (( attempt == WAIT_ATTEMPTS )); then
    print -u2 "legacy Worker did not finish unloading before the verification deadline"
    exit 1
  fi
  sleep "$WAIT_INTERVAL_SECONDS"
done
if [[ -e "$PRIMARY_PLIST" || -e "$BACKUP_PLIST" ]]; then
  print -u2 "legacy service files are still installed"
  exit 1
fi

if [[ -d "$APP_ROOT/secrets" ]]; then
  LEGACY_SECRETS="$APP_ROOT/legacy-secrets-$STAMP"
  if [[ -e "$LEGACY_SECRETS" ]]; then
    print -u2 "unable to isolate legacy secrets"
    exit 1
  fi
  mv "$APP_ROOT/secrets" "$LEGACY_SECRETS"
  chmod 700 "$LEGACY_SECRETS"
fi

print "Legacy Worker retired; state and configuration backup created"

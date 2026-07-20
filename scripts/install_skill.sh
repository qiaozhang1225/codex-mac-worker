#!/bin/zsh
set -euo pipefail

REMOVE_LEGACY_CLIENT=0
if [[ ${1:-} == "--remove-legacy-client" ]]; then
  REMOVE_LEGACY_CLIENT=1
  shift
fi
if (( $# != 0 )); then
  print -u2 "usage: $0 [--remove-legacy-client]"
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "dual-Mac skill installation requires macOS"
  exit 1
fi
for command_name in python3.12 git gh; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    print -u2 "required command is missing: $command_name"
    exit 1
  fi
done
if [[ "$(python3.12 --version 2>&1)" != Python\ 3.12.* ]]; then
  print -u2 "Python 3.12 is required"
  exit 1
fi
if ! GH_PROMPT_DISABLED=1 gh auth status >/dev/null 2>&1; then
  print -u2 "gh must be authenticated before skill installation"
  exit 1
fi

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
SOURCE="$REPO_ROOT/skills/dual-mac-collaboration"
if [[ ! -f "$SOURCE/SKILL.md" ]]; then
  print -u2 "skill source is missing: $SOURCE"
  exit 1
fi
SOURCE_COMMIT="$(cd "$REPO_ROOT" && git rev-parse HEAD)"
if [[ ! "$SOURCE_COMMIT" =~ '^[0-9a-fA-F]{40}$' ]]; then
  print -u2 "unable to resolve an exact source commit"
  exit 1
fi

APP_ROOT="${DUOMAC_APP_ROOT:-$HOME/Library/Application Support/DualMacCollaboration}"
SKILLS_ROOT="${DUOMAC_SKILLS_ROOT:-${CODEX_HOME:-$HOME/.codex}/skills}"
BIN_ROOT="${DUOMAC_BIN_ROOT:-$HOME/.local/bin}"
VENV="$APP_ROOT/venv"
TARGET="$SKILLS_ROOT/dual-mac-collaboration"

typeset -A WRAPPERS
WRAPPERS=(
  duomac-config-validate config_validate.py
  duomac-issue-validate issue_validate.py
  duomac-issue-create issue_create.py
  duomac-issue-checkpoint issue_checkpoint.py
  duomac-issue-complete issue_complete.py
  duomac-git-preflight git_preflight.py
  duomac-git-deliver git_deliver.py
  duomac-scheduled-pick scheduled_pick.py
)

mkdir -p "$APP_ROOT" "$SKILLS_ROOT" "$BIN_ROOT"
chmod 700 "$APP_ROOT"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3.12 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install 'PyYAML>=6,<7'

STAGING="$(mktemp -d "$SKILLS_ROOT/.dual-mac-collaboration.XXXXXX")"
OLD_TARGET="$SKILLS_ROOT/.dual-mac-collaboration.previous.$$"
cleanup() {
  rm -rf "$STAGING" 2>/dev/null || true
}
trap cleanup EXIT
cp -R "$SOURCE/." "$STAGING/"
printf '%s\n' "$SOURCE_COMMIT" > "$STAGING/.source-commit"
VALIDATE_ARGS=(--skill-root "$STAGING")
for wrapper script_name in ${(kv)WRAPPERS}; do
  VALIDATE_ARGS+=(--wrapper-target "$script_name")
done
"$VENV/bin/python" "$REPO_ROOT/scripts/validate_skill.py" \
  "${VALIDATE_ARGS[@]}" >/dev/null
if [[ -e "$TARGET" ]]; then
  mv "$TARGET" "$OLD_TARGET"
fi
if ! mv "$STAGING" "$TARGET"; then
  if [[ -e "$OLD_TARGET" ]]; then
    mv "$OLD_TARGET" "$TARGET"
  fi
  print -u2 "unable to activate the staged skill"
  exit 1
fi
rm -rf "$OLD_TARGET"
trap - EXIT

for wrapper script_name in ${(kv)WRAPPERS}; do
  destination="$BIN_ROOT/$wrapper"
  temporary="$destination.tmp.$$"
  printf '#!/bin/zsh\nexec %q %q "$@"\n' \
    "$VENV/bin/python" "$TARGET/scripts/$script_name" > "$temporary"
  chmod 755 "$temporary"
  mv "$temporary" "$destination"
done

EXAMPLE_SOURCE="$TARGET/assets/repositories.toml.example"
EXAMPLE_TARGET="$APP_ROOT/repositories.toml.example"
if [[ ! -f "$EXAMPLE_TARGET" ]] || ! cmp -s "$EXAMPLE_SOURCE" "$EXAMPLE_TARGET"; then
  EXAMPLE_TEMPORARY="$EXAMPLE_TARGET.tmp.$$"
  cp "$EXAMPLE_SOURCE" "$EXAMPLE_TEMPORARY"
  chmod 600 "$EXAMPLE_TEMPORARY"
  mv "$EXAMPLE_TEMPORARY" "$EXAMPLE_TARGET"
fi

if (( REMOVE_LEGACY_CLIENT )); then
  rm -rf "$SKILLS_ROOT/dispatch-codex-task"
  LEGACY_CLI="codex""ctl"
  if [[ -L "$BIN_ROOT/$LEGACY_CLI" ]]; then
    rm "$BIN_ROOT/$LEGACY_CLI"
  fi
fi

print "Installed dual-mac-collaboration at commit $SOURCE_COMMIT"

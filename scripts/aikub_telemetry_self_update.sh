#!/usr/bin/env bash
# Update/repair the telemetry checkout only. Scheduling belongs to run_telemetry.sh.
# Preserve local config/runtime files; never print their contents.
set -euo pipefail

REPO_URL="${AIKUB_TELEMETRY_REPO_URL:-https://github.com/AIKUB-ai/AIKUB-hermes-telemetry-agent_Script.git}"
BRANCH="${AIKUB_TELEMETRY_REPO_BRANCH:-main}"
AGENT_DIR="${AIKUB_TELEMETRY_AGENT_DIR:-$HOME/.hermes/aikub_telemetry_agent}"
OLD_DIR="${AGENT_DIR}.old_$(date +%Y%m%d_%H%M%S)_$$"

export GIT_TERMINAL_PROMPT=0
git_public() {
  git -c credential.helper= -c http.extraHeader= \
    -c "http.https://github.com/.extraheader=" "$@"
}

# Offline fallback is allowed only if the scripts actually exist.
local_copy_available() {
  local item
  for item in run_telemetry.sh scripts/aikub_telemetry_logger.py \
    scripts/aikub_telemetry_logs_incremental.py scripts/aikub_telemetry_sessions_snapshot.py; do
    [ -f "$AGENT_DIR/$item" ] || return 1
  done
}
update_failed() {
  if local_copy_available; then
    echo "WARN: Git update failed; using existing local telemetry scripts" >&2
    exit 0
  fi
  echo "ERROR: Git update failed and no usable local copy exists" >&2
  exit 1
}

mkdir -p "$(dirname "$AGENT_DIR")"
if [ -d "$AGENT_DIR/.git" ]; then
  cd "$AGENT_DIR"
  git_public remote set-url origin "$REPO_URL"
  git_public fetch origin "$BRANCH" || update_failed
  git_public reset --hard "origin/$BRANCH" || update_failed
else
  # Clone first: a network failure must not move or destroy the working copy.
  STAGING_DIR="$(mktemp -d "${AGENT_DIR}.update_XXXXXX")"
  trap 'rm -rf -- "$STAGING_DIR"' EXIT
  git_public clone --branch "$BRANCH" "$REPO_URL" "$STAGING_DIR" || update_failed
  if [ -d "$AGENT_DIR" ]; then
    for item in .env logs state failed_chunks failed_log_entries.jsonl; do
      if [ -e "$AGENT_DIR/$item" ] && [ ! -e "$STAGING_DIR/$item" ]; then
        cp -a "$AGENT_DIR/$item" "$STAGING_DIR/$item"
      fi
    done
    mv "$AGENT_DIR" "$OLD_DIR"
  fi
  if ! mv "$STAGING_DIR" "$AGENT_DIR"; then
    [ ! -d "$OLD_DIR" ] || mv "$OLD_DIR" "$AGENT_DIR"
    exit 1
  fi
  cd "$AGENT_DIR"
fi
chmod +x run_telemetry.sh scripts/*.py scripts/*.sh 2>/dev/null || true
echo "commit=$(git log -1 --oneline)"
echo "ok=true"

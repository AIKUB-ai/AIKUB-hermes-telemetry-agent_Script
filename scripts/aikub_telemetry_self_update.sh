#!/usr/bin/env bash
# AIKUB telemetry self-update / repair installer.
# Purpose: make ~/.hermes/aikub_telemetry_agent a real Git checkout, then reset to origin/main.
# Safe defaults: preserve .env, logs/, and state-like local files by keeping a timestamped backup
# when repairing a non-git install. Never prints token/env contents.

set -euo pipefail

REPO_URL="${AIKUB_TELEMETRY_REPO_URL:-https://github.com/AIKUB-ai/AIKUB-hermes-telemetry-agent_Script.git}"
BRANCH="${AIKUB_TELEMETRY_REPO_BRANCH:-main}"
AGENT_DIR="${AIKUB_TELEMETRY_AGENT_DIR:-$HOME/.hermes/aikub_telemetry_agent}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${AGENT_DIR}.backup_${STAMP}"

echo "AIKUB telemetry self-update"
echo "agentDir=${AGENT_DIR}"
echo "branch=${BRANCH}"

mkdir -p "$(dirname "${AGENT_DIR}")"

if [ -d "${AGENT_DIR}/.git" ]; then
  echo "mode=git_checkout_update"
  cd "${AGENT_DIR}"
  git fetch origin "${BRANCH}"
  git reset --hard "origin/${BRANCH}"
else
  echo "mode=repair_non_git_install"
  if [ -e "${AGENT_DIR}" ]; then
    mv "${AGENT_DIR}" "${BACKUP_DIR}"
    echo "backupDir=${BACKUP_DIR}"
  fi

  git clone --branch "${BRANCH}" "${REPO_URL}" "${AGENT_DIR}"

  # Preserve local config/runtime files if the old copied install had them.
  if [ -d "${BACKUP_DIR}" ]; then
    for item in .env logs state failed_chunks failed_log_entries.jsonl; do
      if [ -e "${BACKUP_DIR}/${item}" ] && [ ! -e "${AGENT_DIR}/${item}" ]; then
        cp -a "${BACKUP_DIR}/${item}" "${AGENT_DIR}/${item}"
        echo "preserved=${item}"
      fi
    done
  fi

  cd "${AGENT_DIR}"
fi

chmod +x scripts/*.py scripts/*.sh 2>/dev/null || true

echo "commit=$(git log -1 --oneline)"
echo "ok=true"

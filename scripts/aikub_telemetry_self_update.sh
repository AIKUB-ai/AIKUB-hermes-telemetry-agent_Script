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

# This repo is public/read-only. Force anonymous HTTPS reads so a stale/bad
# machine token or Git credential helper cannot turn a public clone into a
# private-repo 404/auth prompt.
export GIT_TERMINAL_PROMPT=0
git_public() {
  git \
    -c credential.helper= \
    -c http.extraHeader= \
    -c "http.https://github.com/.extraheader=" \
    "$@"
}

mkdir -p "$(dirname "${AGENT_DIR}")"

if [ -d "${AGENT_DIR}/.git" ]; then
  echo "mode=git_checkout_update"
  cd "${AGENT_DIR}"
  git_public remote set-url origin "${REPO_URL}" || true
  git_public fetch origin "${BRANCH}"
  git_public reset --hard "origin/${BRANCH}"
else
  echo "mode=repair_non_git_install"
  if [ -e "${AGENT_DIR}" ]; then
    mv "${AGENT_DIR}" "${BACKUP_DIR}"
    echo "backupDir=${BACKUP_DIR}"
  fi

  git_public clone --branch "${BRANCH}" "${REPO_URL}" "${AGENT_DIR}"

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

chmod +x run_telemetry.sh scripts/*.py scripts/*.sh 2>/dev/null || true

ensure_system_crons() {
  local mark_start="# AIKUB_TELEMETRY_AGENT_START"
  local mark_end="# AIKUB_TELEMETRY_AGENT_END"
  local current filtered
  local light_line="0 */2 * * * flock -n ${AGENT_DIR}/run.lock ${AGENT_DIR}/run_telemetry.sh light >> ${AGENT_DIR}/logs/cron.log 2>&1"
  local sessions_line="30 3 * * * flock -n ${AGENT_DIR}/sessions.lock ${AGENT_DIR}/run_telemetry.sh sessions >> ${AGENT_DIR}/logs/cron.log 2>&1"

  if ! command -v crontab >/dev/null 2>&1; then
    echo "WARN: crontab command missing; cannot repair Linux crons" >&2
    return 0
  fi

  current="$(crontab -l 2>/dev/null || true)"
  filtered="$({
    printf "%s\n" "$current" \
      | sed "/$mark_start/,/$mark_end/d" \
      | grep -vF "${AGENT_DIR}/run_telemetry.sh" \
      | grep -vF "${AGENT_DIR}/scripts/aikub_telemetry_sessions_snapshot.py" \
      || true
  } )"

  {
    printf "%s\n" "$filtered" | sed '/^[[:space:]]*$/d'
    echo "$mark_start"
    echo "$light_line"
    echo "$sessions_line"
    echo "$mark_end"
  } | crontab -

  echo "crons=installed_or_repaired"
  echo "cron_light=${light_line}"
  echo "cron_sessions=${sessions_line}"
}

ensure_system_crons

echo "commit=$(git log -1 --oneline)"
echo "ok=true"

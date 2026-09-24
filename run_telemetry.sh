#!/usr/bin/env bash
# AIKUB Hermes telemetry runner.
# Modes:
#   light    -> inventory/model/plugins/crons + incremental logs; no sessions
#   sessions -> Hermes sessions/messages only; no inventory/logs
#   all      -> explicit diagnostic/full run only

set -euo pipefail

export AIKUB_TELEMETRY_RUNNER_VERSION="2026.09.23.1"
SCRIPT_PATH="${BASH_SOURCE[0]}"
INSTALL_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="${ENV_FILE:-$INSTALL_DIR/.env}"
MODE="${1:-light}"
case "$MODE" in
  light|--light|sessions|--sessions|session|--session|all|--all|install-cron) ;;
  *) echo "Usage: $0 [light|sessions|all|install-cron]" >&2; exit 2 ;;
esac

# Read a single KEY=value from .env without printing or sourcing secrets.
env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  awk -v key="$key" '
    /^[[:space:]]*(#|$)/ { next }
    {
      line=$0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      if (line !~ "^[[:space:]]*" key "[[:space:]]*=") next
      sub("^[[:space:]]*" key "[[:space:]]*=[[:space:]]*", "", line)
      sub(/[[:space:]]+#.*$/, "", line)
      sub(/^[[:space:]]+/, "", line)
      sub(/[[:space:]]+$/, "", line)
      if ((substr(line, 1, 1) == "\"" && substr(line, length(line), 1) == "\"") || (substr(line, 1, 1) == "'"'"'" && substr(line, length(line), 1) == "'"'"'")) {
        line=substr(line, 2, length(line) - 2)
      }
      value=line
    }
    END { if (value != "") print value }
  ' "$ENV_FILE"
}

export AIKUB_TELEMETRY_URL="${AIKUB_TELEMETRY_URL:-$(env_value AIKUB_TELEMETRY_URL)}"
export AIKUB_TELEMETRY_URL="${AIKUB_TELEMETRY_URL:-$(env_value AIKUB_TELEMETRY_BASE_URL)}"
export AIKUB_TELEMETRY_BASE_URL="${AIKUB_TELEMETRY_BASE_URL:-$AIKUB_TELEMETRY_URL}"
export AIKUB_TELEMETRY_BOTOPS_TOKEN="${AIKUB_TELEMETRY_BOTOPS_TOKEN:-$(env_value AIKUB_TELEMETRY_BOTOPS_TOKEN)}"
export AIKUB_TELEMETRY_BOTOPS_TOKEN="${AIKUB_TELEMETRY_BOTOPS_TOKEN:-$(env_value AIKUB_TELEMETRY_API_KEY)}"
export AIKUB_TELEMETRY_API_KEY="${AIKUB_TELEMETRY_API_KEY:-$AIKUB_TELEMETRY_BOTOPS_TOKEN}"
export AIKUB_LOG_TIMEZONE="${AIKUB_LOG_TIMEZONE:-$(env_value AIKUB_LOG_TIMEZONE)}"
export HERMES_HOME="${HERMES_HOME:-$(env_value HERMES_HOME)}"
export HERMES_REAL_HOME="${HERMES_REAL_HOME:-$(env_value HERMES_REAL_HOME)}"
export OPENCLAW_HOME="${OPENCLAW_HOME:-$(env_value OPENCLAW_HOME)}"
HERMES_HOME="${HERMES_HOME:-${HERMES_REAL_HOME:-$HOME/.hermes}}"
export HERMES_HOME

if [ "$MODE" != "install-cron" ] && [ -z "${AIKUB_TELEMETRY_BASE_URL:-}" ] && [ -z "${AIKUB_TELEMETRY_URL:-}" ]; then
  echo "ERROR: AIKUB_TELEMETRY_BASE_URL manquant dans $ENV_FILE." >&2
  exit 1
fi

if [ "$MODE" != "install-cron" ] && [ -z "${AIKUB_TELEMETRY_BOTOPS_TOKEN:-}" ] && [ -z "${AIKUB_TELEMETRY_API_KEY:-}" ]; then
  echo "ERROR: AIKUB_TELEMETRY_BOTOPS_TOKEN manquant dans $ENV_FILE." >&2
  exit 1
fi

self_update() {
  if [ -x "$INSTALL_DIR/scripts/aikub_telemetry_self_update.sh" ]; then
    if ! AIKUB_TELEMETRY_AGENT_DIR="$INSTALL_DIR" bash "$INSTALL_DIR/scripts/aikub_telemetry_self_update.sh"; then
      echo "WARN: self-update failed; continuing with current local copy" >&2
    fi
  else
    echo "WARN: self-update helper missing; continuing with current local copy" >&2
  fi
}

ensure_local_cron_lock() {
  local mark_start="# AIKUB_TELEMETRY_AGENT_START"
  local mark_end="# AIKUB_TELEMETRY_AGENT_END"
  local light_line="0 */2 * * * flock -n $INSTALL_DIR/run.lock $INSTALL_DIR/run_telemetry.sh light >> $INSTALL_DIR/logs/cron.log 2>&1"
  local sessions_line="30 3 * * * flock -n $INSTALL_DIR/sessions.lock $INSTALL_DIR/run_telemetry.sh sessions >> $INSTALL_DIR/logs/cron.log 2>&1"
  local current filtered

  if ! command -v crontab >/dev/null 2>&1 || ! command -v flock >/dev/null 2>&1; then
    echo "WARN: crontab/flock missing; Linux scheduling not installed" >&2
    [ "$MODE" != "install-cron" ]
    return
  fi

  current="$(crontab -l 2>/dev/null || true)"
  if printf "%s\n" "$current" | grep -Fq "$light_line" && printf "%s\n" "$current" | grep -Fq "$sessions_line"; then
    return 0
  fi

  filtered="$({
    printf "%s\n" "$current" \
      | sed "/$mark_start/,/$mark_end/d" \
      | grep -vF "$INSTALL_DIR/run_telemetry.sh" \
      | grep -vF "$INSTALL_DIR/scripts/aikub_telemetry_sessions_snapshot.py" \
      || true
  } )"

  (
    printf "%s\n" "$filtered" | sed '/^[[:space:]]*$/d'
    echo "$mark_start"
    echo "$light_line"
    echo "$sessions_line"
    echo "$mark_end"
  ) | crontab -
  echo "AIKUB Telemetry: Linux crons repaired: light aux 2h + sessions daily"
}

run_light() {
  echo "AIKUB Telemetry: sending inventory snapshot (identity + model + crons + plugins)"
  python3 aikub_telemetry_logger.py

  echo "AIKUB Telemetry: sending logs snapshot"
  if ! python3 aikub_telemetry_logs_incremental.py --first-run-days 3 --chunk-size 250; then
    echo "WARN: logs snapshot failed; continuing light telemetry" >&2
  fi
}

run_sessions() {
  echo "AIKUB Telemetry: sending sessions snapshot"
  python3 aikub_telemetry_sessions_snapshot.py --chunk-size 50
}

mkdir -p "$INSTALL_DIR/logs"
if [ "$MODE" = "install-cron" ]; then
  ensure_local_cron_lock
  exit 0
fi

echo "AIKUB Telemetry: self-update agent scripts"
self_update
for script in aikub_telemetry_logger.py aikub_telemetry_logs_incremental.py aikub_telemetry_sessions_snapshot.py; do
  if [ ! -f "$INSTALL_DIR/scripts/$script" ]; then
    echo "ERROR: incomplete local telemetry copy: $script" >&2
    exit 1
  fi
done
cd "$INSTALL_DIR/scripts"

case "$MODE" in
  light|--light)
    run_light
    ;;
  sessions|--sessions|session|--session)
    run_sessions
    ;;
  all|--all)
    run_light
    run_sessions
    ;;
  *)
    echo "Usage: $0 [light|sessions|all|install-cron]" >&2
    exit 2
    ;;
esac

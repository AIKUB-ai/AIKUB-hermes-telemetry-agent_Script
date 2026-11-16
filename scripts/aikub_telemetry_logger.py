#!/usr/bin/env python3
"""
Aikub Telemetry logger for Hermes bots.

Goal:
  The bot receives ONLY its telemetry write config, then self-documents its
  existing Hermes inventory into Aikub Telemetry.

Required env per bot:
  AIKUB_TELEMETRY_URL=https://aikubtelemetry-production.up.railway.app
  AIKUB_TELEMETRY_API_KEY=[REDACTED]

Optional compatibility env:
  AIKUB_TELEMETRY_BASE_URL=https://aikubtelemetry-production.up.railway.app
  AIKUB_TELEMETRY_BOTOPS_TOKEN=[REDACTED]  # legacy alias for AIKUB_TELEMETRY_API_KEY
  AIKUB_TELEMETRY_BOT_ID=<technical slug, e.g. chopchop>
  AIKUB_TELEMETRY_SOURCE=<runtime/source, e.g. hermes>

The script auto-discovers from the local Hermes installation/dashboard data:
  - identity from AIKUB_TELEMETRY_BOT_ID when present; otherwise API resolves bot from token
  - model from ~/.hermes/config.yaml
  - skills from ~/.hermes/skills and ~/.hermes/hermes-agent/skills
  - crons from ~/.hermes/cron/jobs.json, sanitized
  - enabled Hermes dashboard plugins only

Usage:
  python3 aikub_telemetry_logger.py
  python3 aikub_telemetry_logger.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_PATH = "/v1/telemetry/events"
OPENAI_ME_URL = "https://api.openai.com/v1/me"
LOGGER_VERSION = "2026.11.16.1"
EXPECTED_SCRIPT_VERSION = "2026.11.16.1"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise SystemExit(f"Missing required env: {name}")
    return value.strip()


def require_base_url() -> str:
    value = optional_env("AIKUB_TELEMETRY_URL") or optional_env("AIKUB_TELEMETRY_BASE_URL")
    if not value:
        raise SystemExit("Missing required env: AIKUB_TELEMETRY_URL")
    return value


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or os.getenv("HERMES_REAL_HOME") or "~/.hermes").expanduser()


def openclaw_home() -> Path:
    return Path(os.getenv("OPENCLAW_HOME") or "~/.openclaw").expanduser()


def detect_runtime(home: Path) -> str:
    """Detect the local agent runtime without requiring an env var."""
    if (home / "config.yaml").exists() or (home / "hermes-agent").exists():
        return "hermes"
    if openclaw_home().exists():
        return "openclaw"
    return "unknown"


def build_endpoint(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    if base_url.endswith(EVENT_PATH):
        return base_url
    return base_url + EVENT_PATH


def read_model(home: Path) -> dict[str, Any]:
    """Small YAML reader for the top-level `model:` block in Hermes config."""
    config = home / "config.yaml"
    model: dict[str, Any] = {"provider": None, "name": None}
    if not config.exists():
        return model

    in_model = False
    for raw in config.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("model:"):
            in_model = True
            continue
        if in_model and raw and not raw.startswith((" ", "\t")):
            break
        if not in_model:
            continue
        match = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(.*?)\s*$", raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip().strip('"').strip("'")
        if key == "provider":
            model["provider"] = value or None
        elif key in {"default", "name", "model"}:
            model["name"] = value or None
        elif key == "context_length":
            try:
                model["contextLength"] = int(value)
            except ValueError:
                model["contextLength"] = value
    return model


def _json_key(data: Any, key: str) -> str:
    if isinstance(data, dict):
        value = data.get(key)
        return value if isinstance(value, str) and value.strip() else ""
    return ""


def _find_access_token(data: Any) -> str:
    if isinstance(data, dict):
        token = _json_key(data, "access_token")
        if token:
            return token
        for value in data.values():
            token = _find_access_token(value)
            if token:
                return token
    elif isinstance(data, list):
        for value in data:
            token = _find_access_token(value)
            if token:
                return token
    return ""


def _find_first_string(data: Any, keys: set[str]) -> str:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _find_first_string(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_first_string(value, keys)
            if found:
                return found
    return ""


def codex_auth_candidates(home: Path) -> list[Path]:
    candidates = [
        home / "auth.json",
        home / ".codex" / "auth.json",
        home / "codex-home" / "auth.json",
        home / "hermes-agent" / ".codex" / "auth.json",
        home / "hermes-agent" / "codex-home" / "auth.json",
        Path(os.getenv("CODEX_HOME", "")).expanduser() / "auth.json" if os.getenv("CODEX_HOME") else None,
        Path("~/.codex/auth.json").expanduser(),
        openclaw_home() / "agents" / "main" / "agent" / "codex-home" / "auth.json",
    ]
    for path in openclaw_home().glob("agents/*/agent/codex-home/auth.json"):
        candidates.append(path)

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def read_codex_oauth_account(home: Path) -> dict[str, Any] | None:
    """Read Codex auth locally and keep only non-secret account identity fields."""
    for auth_file in codex_auth_candidates(home):
        account = read_codex_oauth_account_from_file(auth_file)
        if account:
            return account
    return None


def codex_oauth_account_from_auth_data(auth_data: Any) -> dict[str, Any] | None:
    email = _find_first_string(auth_data, {"email", "account_email", "login_email"})
    name = _find_first_string(auth_data, {"name", "display_name", "account_name"})
    user_id = _find_first_string(auth_data, {"user_id", "account_id", "id"})
    if not any([email, name, user_id]):
        return None
    return {
        "provider": "openai-codex",
        "email": email or None,
        "name": name or None,
        "userId": user_id or None,
    }


def read_codex_oauth_account_from_file(auth_file: Path) -> dict[str, Any] | None:
    if not auth_file.exists():
        return None
    try:
        auth_data = json.loads(auth_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    embedded_account = codex_oauth_account_from_auth_data(auth_data)
    if embedded_account and embedded_account.get("email"):
        return embedded_account

    access_token = _find_access_token(auth_data)
    if not access_token:
        return embedded_account

    try:
        request = urllib.request.Request(
            optional_env("AIKUB_TELEMETRY_OPENAI_ME_URL", OPENAI_ME_URL),
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    email = _json_key(data, "email")
    name = _json_key(data, "name")
    user_id = _json_key(data, "id")
    if not any([email, name, user_id]):
        return embedded_account
    return {
        "provider": "openai-codex",
        "email": email or None,
        "name": name or None,
        "userId": user_id or None,
    }


def parse_skill_md(path: Path) -> dict[str, Any]:
    """Extract lightweight skill metadata without executing anything."""
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
    description = ""

    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            for line in frontmatter.splitlines():
                if line.lower().startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'") or name
                elif line.lower().startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"').strip("'")

    if not description:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("---", "#")) and ":" not in line[:25]:
                description = line[:240]
                break

    return {
        "name": name,
        "description": description,
        "path": str(path),
        "installed": True,
        "enabled": True,
        "readinessStatus": "available",
        "reason": None,
        "requiredPlatforms": [],
        "currentPlatform": current_platform(),
        "missingCommands": [],
        "prerequisitesSatisfied": True,
        "compatibleWithPlatform": True,
        "usableByHermes": True,
    }


def current_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system.startswith("linux"):
        return "linux"
    if system.startswith(("windows", "mingw", "msys", "cygwin")):
        return "windows"
    return system or "unknown"


def normalize_required_platform(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    if text.startswith("linux"):
        return "linux"
    if text.startswith(("win", "windows")):
        return "windows"
    return text


def missing_values(missing: Any, key: str) -> list[str]:
    if not isinstance(missing, dict):
        return []
    values = missing.get(key)
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value)]


def readiness_from_openclaw_skill(item: dict[str, Any]) -> tuple[str, str | None]:
    missing = item.get("missing") if isinstance(item.get("missing"), dict) else {}
    required_platforms = [normalize_required_platform(value) for value in missing_values(missing, "os")]
    missing_commands = missing_values(missing, "bins")
    missing_any_commands = missing_values(missing, "anyBins")
    missing_env = missing_values(missing, "env")
    missing_config = missing_values(missing, "config")
    platform_mismatch = bool(required_platforms and current_platform() not in required_platforms)
    missing_prereqs = bool(missing_commands or missing_any_commands or missing_env or missing_config)

    if platform_mismatch:
        return "unsupported", "platform_mismatch"
    if missing_prereqs:
        return "blocked", "missing_prerequisites"
    if item.get("disabled") is True:
        return "disabled", "disabled"
    if item.get("eligible") is False:
        return "disabled", "not_eligible"
    return "active", None


def normalize_openclaw_skill(item: dict[str, Any]) -> dict[str, Any]:
    missing = item.get("missing") if isinstance(item.get("missing"), dict) else {}
    required_platforms = [normalize_required_platform(value) for value in missing_values(missing, "os")]
    missing_commands = [*missing_values(missing, "bins"), *missing_values(missing, "anyBins")]
    readiness_status, reason = readiness_from_openclaw_skill(item)
    compatible = not required_platforms or current_platform() in required_platforms
    prerequisites_satisfied = not any(
        [
            missing_commands,
            missing_values(missing, "env"),
            missing_values(missing, "config"),
        ]
    )
    usable = bool(item.get("eligible") and not item.get("disabled") and item.get("modelVisible") and compatible and prerequisites_satisfied)
    return {
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        "category": "openclaw",
        "source": item.get("source", "openclaw"),
        "installed": True,
        "enabled": bool(not item.get("disabled") and item.get("eligible")),
        "readinessStatus": readiness_status,
        "reason": reason,
        "requiredPlatforms": required_platforms,
        "currentPlatform": current_platform(),
        "missingCommands": missing_commands,
        "prerequisitesSatisfied": prerequisites_satisfied,
        "compatibleWithPlatform": compatible,
        "usableByHermes": usable,
        "eligible": item.get("eligible"),
        "modelVisible": item.get("modelVisible"),
        "userInvocable": item.get("userInvocable"),
        "commandVisible": item.get("commandVisible"),
    }


def unique_existing_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    existing: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        key = str(expanded)
        if key in seen or not expanded.exists():
            continue
        seen.add(key)
        existing.append(expanded)
    return existing


def openclaw_bin() -> str | None:
    if optional_env("AIKUB_TELEMETRY_DISABLE_OPENCLAW_CLI").lower() in {"1", "true", "yes"}:
        return None
    configured = optional_env("OPENCLAW_BIN")
    candidates = [configured] if configured else []
    candidates.extend([str(Path("~/.npm-global/bin/openclaw").expanduser()), "openclaw"])
    for candidate in candidates:
        if not candidate:
            continue
        if "/" in candidate and Path(candidate).exists():
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def run_openclaw_json(args: list[str], cwd: Path) -> dict[str, Any] | None:
    binary = openclaw_bin()
    if not binary:
        return None
    try:
        output = subprocess.check_output(
            [binary, *args],
            cwd=str(cwd) if cwd.exists() else None,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=12,
        )
        data = json.loads(output)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def discover_openclaw_skills(home: Path) -> list[dict[str, Any]]:
    oc_home = openclaw_home()
    data = run_openclaw_json(["skills", "list", "--json"], oc_home / "workspace")
    raw_skills = data.get("skills", []) if data else []
    skills: list[dict[str, Any]] = []
    for item in raw_skills:
        if not isinstance(item, dict):
            continue
        skills.append(normalize_openclaw_skill(item))
    return sorted(skills, key=lambda item: (item.get("category", ""), item["name"]))


def discover_skills(home: Path) -> list[dict[str, Any]]:
    """Read modern Hermes/OpenClaw skill layouts without importing the agent.

    Keep discovery passive: runtime helpers can initialize Hermes or mutate
    sys.path, and are not safe inside a cron inventory reader.
    """

    cli_skills = discover_openclaw_skills(home)
    by_name: dict[str, dict[str, Any]] = {skill["name"]: skill for skill in cli_skills if skill.get("name")}

    oc_home = openclaw_home()
    roots = unique_existing_paths(
        [
            home / "skills",
            home / "hermes-agent" / "skills",
            oc_home / "workspace" / "skills",
            oc_home / "plugin-skills",
            oc_home / "agents" / "main" / "agent" / "codex-home" / "skills",
            *oc_home.glob("agents/*/agent/codex-home/skills"),
        ]
    )
    for root in roots:
        for skill_file in root.rglob("SKILL.md"):
            if "optional-skills" in skill_file.parts:
                continue
            skill = parse_skill_md(skill_file)
            name = skill["name"]
            try:
                rel_parent = skill_file.parent.relative_to(root)
                category = str(rel_parent.parent) if str(rel_parent.parent) != "." else ""
            except ValueError:
                category = ""
            skill["category"] = category
            if name not in by_name or str(skill_file).startswith(str(home / "skills")):
                by_name[name] = skill

    return sorted(by_name.values(), key=lambda item: (item.get("category", ""), item["name"]))


def read_cron_jobs_file(jobs_file: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []

    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return [job for job in jobs if isinstance(job, dict)]


def nested_value(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def discover_openclaw_crons(home: Path) -> list[dict[str, Any]]:
    oc_home = openclaw_home()
    data = run_openclaw_json(["cron", "list", "--all", "--json", "--timeout", "8000"], oc_home / "workspace")
    raw_jobs = data.get("jobs", []) if data else []
    return [job for job in raw_jobs if isinstance(job, dict)]


def discover_crons(home: Path) -> list[dict[str, Any]]:
    """Read Hermes/OpenClaw cron definitions and remove private/noisy fields.

    We intentionally do not log prompt text, delivery origin/chat metadata,
    fire claims, or delivery errors. This is an inventory, not a transcript.
    """
    oc_home = openclaw_home()
    jobs_files = unique_existing_paths(
        [
            home / "cron" / "jobs.json",
            oc_home / "cron" / "jobs.json",
            oc_home / "cron" / "jobs.json.migrated",
            oc_home / "cron" / "jobs.json.bak",
        ]
    )
    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for job in discover_openclaw_crons(home):
        job_id = str(job.get("id") or "")
        if job_id and job_id in seen_ids:
            continue
        if job_id:
            seen_ids.add(job_id)
        jobs.append(job)

    for jobs_file in jobs_files:
        for job in read_cron_jobs_file(jobs_file):
            job_id = str(job.get("id") or "")
            if job_id and job_id in seen_ids:
                continue
            if job_id:
                seen_ids.add(job_id)
            jobs.append(job)

    safe_jobs: list[dict[str, Any]] = []
    for job in jobs:
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        repeat = job.get("repeat") if isinstance(job.get("repeat"), dict) else {}
        safe_jobs.append(
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "enabled": job.get("enabled"),
                "state": job.get("state"),
                "schedule": {
                    "kind": schedule.get("kind"),
                    "display": job.get("schedule_display") or schedule.get("display") or schedule.get("expr"),
                    "expr": schedule.get("expr"),
                    "tz": schedule.get("tz"),
                },
                "repeat": {
                    "times": repeat.get("times"),
                    "completed": repeat.get("completed"),
                },
                "script": job.get("script"),
                "noAgent": job.get("no_agent"),
                "deliver": job.get("deliver"),
                "workdir": job.get("workdir"),
                "skills": job.get("skills") or ([] if not job.get("skill") else [job.get("skill")]),
                "model": job.get("model") or job.get("model_snapshot"),
                "provider": job.get("provider") or job.get("provider_snapshot"),
                "createdAt": job.get("created_at") or job.get("createdAt") or job.get("createdAtMs"),
                "updatedAt": job.get("updated_at") or job.get("updatedAt") or job.get("updatedAtMs"),
                "nextRunAt": job.get("next_run_at") or job.get("nextRunAt") or job.get("nextRunAtMs") or nested_value(job, "state", "nextRunAtMs"),
                "lastRunAt": job.get("last_run_at") or job.get("lastRunAt") or job.get("lastRunAtMs") or nested_value(job, "state", "lastRunAtMs"),
                "lastStatus": job.get("last_status") or job.get("lastStatus") or job.get("status") or nested_value(job, "state", "lastStatus"),
            }
        )
    return safe_jobs


def _dashboard_plugins_url() -> str:
    """Local Hermes dashboard endpoint that exposes plugins with dashboard tabs.

    This is intentionally local-only and does not need dashboard credentials for
    the public plugin catalog route. These are the plugin records the Hermes
    dashboard actually surfaces as enabled dashboard plugins, not the full
    bundled catalog of 80 inactive plugins.
    """
    port = os.getenv("HERMES_DASHBOARD_PORT") or "9119"
    return f"http://127.0.0.1:{port}/api/dashboard/plugins"


def discover_plugins(home: Path) -> dict[str, Any]:
    """Discover enabled/dashboard-active Hermes plugins only.

    Do NOT log every bundled plugin shown as "not enabled" by
    `hermes plugins list`. For AIKUB BotOps we only want the plugins that are
    actually active/visible in the Hermes dashboard.
    """
    items: list[dict[str, Any]] = []
    try:
        request = urllib.request.Request(_dashboard_plugins_url(), method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        if isinstance(data, list):
            for plugin in data:
                if not isinstance(plugin, dict):
                    continue
                tab = plugin.get("tab") if isinstance(plugin.get("tab"), dict) else {}
                items.append(
                    {
                        "name": plugin.get("name"),
                        "label": plugin.get("label") or plugin.get("name"),
                        "version": plugin.get("version"),
                        "source": plugin.get("source"),
                        "enabled": True,
                        "status": "active",
                        "description": plugin.get("description"),
                        "dashboardTab": tab.get("path"),
                        "hasApi": bool(plugin.get("has_api")),
                    }
                )
    except Exception:
        items = []

    return {
        "enabledCount": len(items),
        "pluginCount": len(items),
        "items": sorted(items, key=lambda item: item.get("name") or ""),
        "source": "hermes_dashboard_plugins_api",
        "includesInactiveBundledPlugins": False,
    }


def optional_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    return default


def require_token() -> str:
    """Return the bot telemetry token. Prefer the env var shown in BotOps Vault."""
    token = optional_env("AIKUB_TELEMETRY_BOTOPS_TOKEN") or optional_env("AIKUB_TELEMETRY_API_KEY")
    if not token:
        raise SystemExit("Missing required env: AIKUB_TELEMETRY_API_KEY")
    return token


def telemetry_script_metadata() -> dict[str, Any]:
    runner_version = optional_env("AIKUB_TELEMETRY_RUNNER_VERSION")
    expected_version = optional_env("AIKUB_TELEMETRY_SCRIPT_EXPECTED_VERSION", EXPECTED_SCRIPT_VERSION)
    version = runner_version or LOGGER_VERSION
    return {
        "name": "aikub-telemetry-agent",
        "version": version,
        "runnerVersion": runner_version or None,
        "loggerVersion": LOGGER_VERSION,
        "expectedVersion": expected_version,
        "upToDate": bool(version and expected_version and version == expected_version),
        "selfUpdating": True,
        "installDir": "aikub_telemetry_agent",
        "expectedCronExpression": "0 */2 * * *",
    }


def build_payload(bot_id: str, home: Path) -> dict[str, Any]:
    source = optional_env("AIKUB_TELEMETRY_SOURCE", "hermes")
    runtime = detect_runtime(home)
    skills = discover_skills(home)
    crons = discover_crons(home)
    plugins = discover_plugins(home)
    # File inventory intentionally disabled: too heavy for ERP/BotOps hourly telemetry.
    model = read_model(home)
    oauth_account = read_codex_oauth_account(home)
    telemetry_script = telemetry_script_metadata()
    occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    session_id = f"bot-inventory-{bot_id}-{occurred_at}"

    # AIKUB_Telemetry only accepts these root fields:
    # botId, eventType, severity, source, traceId, sessionId, payload, occurredAt.
    # Everything discovered about the bot must live under payload.
    return {
        "botId": bot_id,
        "eventType": "bot_inventory_snapshot",
        "severity": "INFO",
        "source": source,
        "traceId": session_id,
        "sessionId": session_id,
        "occurredAt": occurred_at,
        "payload": {
            "identity": {
                "name": bot_id,
                "technicalSlug": bot_id,
                "displayName": bot_id,
                "kind": "Hermes Agent" if runtime == "hermes" else runtime,
            },
            "model": {
                "provider": model.get("provider"),
                "name": model.get("name"),
                "context": model.get("contextLength"),
            },
            "oauthAccount": oauth_account,
            "telemetryScriptVersion": telemetry_script["version"],
            "telemetryScript": telemetry_script,
            "skillCount": len(skills),
            "skills": skills,
            "crons": {
                "cronCount": len(crons),
                "cronNames": [cron.get("name") for cron in crons if cron.get("name")],
                "items": crons,
            },
            "plugins": plugins,
            "inventoryDiscovery": {
                "runtime": runtime,
                "hermesHome": str(home),
                "openclawHome": str(openclaw_home()),
                "usedOpenClawCli": openclaw_bin() is not None,
            },
        },
    }


def post_event(endpoint: str, api_key: str, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-aikub-botops-token": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(response_body) if response_body else None
            relay = parsed.get("erpImport") if isinstance(parsed, dict) else None
            relay_ok = not isinstance(relay, dict) or (relay.get("ok") is True and not relay.get("skipped"))
            return {
                "ok": 200 <= response.status < 300 and relay_ok,
                "status": response.status,
                "body": parsed,
            }
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed_body: Any = json.loads(response_body)
        except json.JSONDecodeError:
            parsed_body = response_body
        return {"ok": False, "status": exc.code, "body": parsed_body}
    except urllib.error.URLError as exc:
        return {"ok": False, "status": None, "body": str(exc)}
    except TimeoutError as exc:
        return {"ok": False, "status": None, "body": f"timeout: {exc}"}


def redacted_config(endpoint: str, bot_id: str, home: Path) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "botId": bot_id,
        "hermesHome": str(home),
        "headers": {
            "content-type": "application/json",
            "x-aikub-botops-token": "[REDACTED]",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-document Hermes bot identity, model, skills, crons, and enabled plugins to Aikub Telemetry.")
    parser.add_argument("--dry-run", action="store_true", help="Print discovered payload/config without sending.")
    args = parser.parse_args()

    api_key = require_token()
    configured_bot_id = optional_env("AIKUB_TELEMETRY_BOT_ID")
    bot_id = configured_bot_id or "unknown"
    endpoint = build_endpoint(require_base_url())
    home = hermes_home()
    payload = build_payload(bot_id, home)
    if not configured_bot_id:
        payload.pop("botId", None)

    if args.dry_run:
        print(json.dumps({"dryRun": True, "config": redacted_config(endpoint, bot_id, home), "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    result = post_event(endpoint, api_key, bot_id, payload)
    print(json.dumps({
        "config": redacted_config(endpoint, bot_id, home),
        "inventory": {
            "skillCount": len(payload["payload"].get("skills", [])),
            "cronCount": len(payload["payload"].get("crons", {}).get("items", [])),
            "pluginCount": payload["payload"].get("plugins", {}).get("pluginCount", 0),
        },
        "result": result,
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

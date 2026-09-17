#!/usr/bin/env python3
"""
Aikub Telemetry logger for Hermes bots.

Goal:
  The bot receives ONLY its telemetry write config, then self-documents its
  existing Hermes inventory into Aikub Telemetry.

Required env per bot:
  AIKUB_TELEMETRY_BASE_URL=https://aikubtelemetry-production.up.railway.app
  AIKUB_TELEMETRY_BOTOPS_TOKEN=[REDACTED]

Optional compatibility env:
  AIKUB_TELEMETRY_API_KEY=[REDACTED]  # legacy alias for AIKUB_TELEMETRY_BOTOPS_TOKEN
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
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_PATH = "/v1/telemetry/events"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise SystemExit(f"Missing required env: {name}")
    return value.strip()


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or os.getenv("HERMES_REAL_HOME") or "~/.hermes").expanduser()


def detect_runtime(home: Path) -> str:
    """Detect the local agent runtime without requiring an env var."""
    if (home / "config.yaml").exists() or (home / "hermes-agent").exists():
        return "hermes"
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
    model: dict[str, Any] = {"provider": "unknown", "name": "unknown"}
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
            model["provider"] = value or "unknown"
        elif key in {"default", "name", "model"}:
            model["name"] = value or "unknown"
        elif key == "context_length":
            try:
                model["contextLength"] = int(value)
            except ValueError:
                model["contextLength"] = value
    return model


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

    return {"name": name, "description": description, "path": str(path)}


def discover_skills(home: Path) -> list[dict[str, Any]]:
    """Discover the exact skills Hermes exposes to the agent.

    Preferred path: import Hermes' own skill discovery helper, which is what the
    dashboard/agent tooling uses. Fallback path: read SKILL.md files locally.
    """
    hermes_agent = home / "hermes-agent"
    if hermes_agent.exists():
        sys.path.insert(0, str(hermes_agent))
        try:
            from tools.skills_tool import _find_all_skills  # type: ignore

            found = _find_all_skills(skip_disabled=True)
            skills = []
            for item in found:
                skills.append(
                    {
                        "name": item.get("name", ""),
                        "description": item.get("description", ""),
                        "category": item.get("category") or "",
                        "source": item.get("source", ""),
                    }
                )
            return sorted(skills, key=lambda item: (item.get("category", ""), item["name"]))
        except Exception:
            pass

    roots = [home / "skills", home / "hermes-agent" / "skills"]
    by_name: dict[str, dict[str, Any]] = {}
    for root in roots:
        if not root.exists():
            continue
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


def discover_crons(home: Path) -> list[dict[str, Any]]:
    """Read Hermes cron definitions and remove private/noisy fields.

    We intentionally do not log prompt text, delivery origin/chat metadata,
    fire claims, or delivery errors. This is an inventory, not a transcript.
    """
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.exists():
        return []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []

    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    safe_jobs: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
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
                "createdAt": job.get("created_at"),
                "nextRunAt": job.get("next_run_at"),
                "lastRunAt": job.get("last_run_at"),
                "lastStatus": job.get("last_status"),
            }
        )
    return safe_jobs


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode non-secret JWT claims only. Never return the raw token."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _visible_email(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 254:
        return ""
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
        return value
    return ""


def _credential_email(credential: dict[str, Any]) -> tuple[str, str]:
    """Return (email, source) only when an email is explicitly visible/provable."""
    for key in ("email", "account_email", "accountEmail", "username", "user", "login"):
        email = _visible_email(credential.get(key))
        if email:
            return email, f"auth_json.{key}"

    for token_key in ("id_token", "access_token"):
        token = credential.get(token_key)
        if not isinstance(token, str) or not token:
            continue
        claims = _decode_jwt_payload(token)
        for claim in ("email", "preferred_username", "upn"):
            email = _visible_email(claims.get(claim))
            if email:
                return email, f"{token_key}.jwt.{claim}"

    override = _visible_email(os.getenv("AIKUB_CODEX_ACCOUNT_EMAIL")) or _visible_email(os.getenv("OPENAI_CODEX_EMAIL"))
    if override:
        return override, "env_override"
    return "", "not_exposed_by_oauth_credential"


def _credential_fingerprint(provider: str, credential: dict[str, Any]) -> tuple[str, str]:
    """Return a stable, non-reversible account fingerprint from OAuth subject claims.

    The raw OAuth `sub` identifies the account but should not be sent to ERP.
    We hash provider + issuer + subject so BotOps can group bots using the same
    Codex account without knowing the email or the opaque provider subject.
    """
    for token_key in ("id_token", "access_token"):
        token = credential.get(token_key)
        if not isinstance(token, str) or not token:
            continue
        claims = _decode_jwt_payload(token)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            continue
        issuer = claims.get("iss") if isinstance(claims.get("iss"), str) else ""
        material = f"{provider.strip().lower()}|{issuer.strip()}|{subject.strip()}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), f"{token_key}.jwt.sub_sha256"
    return "", "not_exposed_by_oauth_credential"


def discover_accounts(home: Path) -> dict[str, Any]:
    """Discover Hermes auth/account inventory without exposing tokens/secrets.

    Hermes stores openai-codex device-code OAuth credentials in auth.json. On
    current builds, that credential often has access/refresh tokens but no email
    claim, so we report the credential as connected with emailStatus=unknown
    instead of guessing from the Nous account email.
    """
    auth_file = home / "auth.json"
    if not auth_file.exists():
        return {"providerCount": 0, "providers": [], "source": str(auth_file), "error": "auth_json_missing"}
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {"providerCount": 0, "providers": [], "source": str(auth_file), "error": f"auth_json_unreadable:{type(exc).__name__}"}

    pool = data.get("credential_pool") if isinstance(data, dict) else None
    if not isinstance(pool, dict):
        pool = {}

    providers: list[dict[str, Any]] = []
    for provider, credentials in sorted(pool.items()):
        if not isinstance(credentials, list):
            continue
        safe_credentials: list[dict[str, Any]] = []
        for index, credential in enumerate(credentials, start=1):
            if not isinstance(credential, dict):
                continue
            email, email_source = _credential_email(credential)
            fingerprint, fingerprint_source = _credential_fingerprint(str(provider), credential)
            safe_credentials.append(
                {
                    "index": index,
                    "id": credential.get("id"),
                    "label": credential.get("label"),
                    "authType": credential.get("auth_type"),
                    "source": credential.get("source"),
                    "priority": credential.get("priority"),
                    "lastStatus": credential.get("last_status"),
                    "baseUrl": credential.get("base_url"),
                    "email": email or None,
                    "emailStatus": "proven" if email else "unknown",
                    "emailSource": email_source,
                    "accountFingerprint": fingerprint or None,
                    "accountFingerprintShort": fingerprint[:16] if fingerprint else None,
                    "fingerprintSource": fingerprint_source,
                }
            )
        providers.append(
            {
                "name": provider,
                "credentialCount": len(safe_credentials),
                "connected": len(safe_credentials) > 0,
                "credentials": safe_credentials,
            }
        )

    return {
        "providerCount": len(providers),
        "activeProvider": data.get("active_provider") if isinstance(data, dict) else None,
        "providers": providers,
        "source": str(auth_file),
        "secretsIncluded": False,
    }


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
    """Return the bot telemetry token. Prefer the explicit BotOps token env name; keep the
    old API_KEY name as a compatibility alias for already-installed bots."""
    token = optional_env("AIKUB_TELEMETRY_BOTOPS_TOKEN") or optional_env("AIKUB_TELEMETRY_API_KEY")
    if not token:
        raise SystemExit("Missing required env: AIKUB_TELEMETRY_BOTOPS_TOKEN")
    return token


def build_payload(bot_id: str, home: Path) -> dict[str, Any]:
    source = optional_env("AIKUB_TELEMETRY_SOURCE", "hermes")
    runtime = detect_runtime(home)
    skills = discover_skills(home)
    crons = discover_crons(home)
    plugins = discover_plugins(home)
    accounts = discover_accounts(home)
    # File inventory intentionally disabled: too heavy for ERP/BotOps hourly telemetry.
    model = read_model(home)
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
            "skillCount": len(skills),
            "skills": skills,
            "crons": {
                "cronCount": len(crons),
                "cronNames": [cron.get("name") for cron in crons if cron.get("name")],
                "items": crons,
            },
            "plugins": plugins,
            "accounts": accounts,
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
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "body": json.loads(response_body) if response_body else None,
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
    endpoint = build_endpoint(require_env("AIKUB_TELEMETRY_BASE_URL"))
    home = hermes_home()
    payload = build_payload(bot_id, home)
    if not configured_bot_id:
        payload.pop("botId", None)

    if args.dry_run:
        print(json.dumps({"dryRun": True, "config": redacted_config(endpoint, bot_id, home), "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    result = post_event(endpoint, api_key, bot_id, payload)
    print(json.dumps({"config": redacted_config(endpoint, bot_id, home), "result": result}, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

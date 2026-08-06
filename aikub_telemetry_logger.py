#!/usr/bin/env python3
"""
Aikub Telemetry logger for Hermes bots.

Goal:
  The bot receives ONLY its telemetry write config, then self-documents its
  existing Hermes inventory into Aikub Telemetry.

Required env per bot:
  AIKUB_TELEMETRY_API_KEY=<personal bot API key>
  AIKUB_TELEMETRY_BOT_ID=<technical slug, e.g. chopchop>
  AIKUB_TELEMETRY_SOURCE=<runtime/source, e.g. hermes>
  AIKUB_TELEMETRY_BASE_URL=https://aikubtelemetry-production.up.railway.app

The script auto-discovers from the local Hermes installation/dashboard data:
  - identity from AIKUB_TELEMETRY_BOT_ID
  - model from ~/.hermes/config.yaml
  - skills from ~/.hermes/skills and ~/.hermes/hermes-agent/skills

Usage:
  python3 aikub_telemetry_logger.py
  python3 aikub_telemetry_logger.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
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


def build_payload(bot_id: str, home: Path) -> dict[str, Any]:
    source = require_env("AIKUB_TELEMETRY_SOURCE")
    runtime = detect_runtime(home)
    skills = discover_skills(home)
    return {
        "eventType": "bot_inventory_snapshot",
        "botId": bot_id,
        "source": source,
        "identity": {
            "name": bot_id,
            "technicalSlug": bot_id,
            "displayName": bot_id,
            "runtime": runtime,
            "hermesHome": str(home),
        },
        "model": read_model(home),
        "skillCount": len(skills),
        "skills": skills,
    }


def post_event(endpoint: str, api_key: str, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "x-aikub-bot-id": bot_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
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


def redacted_config(endpoint: str, bot_id: str, home: Path) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "botId": bot_id,
        "hermesHome": str(home),
        "headers": {
            "content-type": "application/json",
            "x-api-key": "[REDACTED]",
            "x-aikub-bot-id": bot_id,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-document Hermes bot identity, model, and skills to Aikub Telemetry.")
    parser.add_argument("--dry-run", action="store_true", help="Print discovered payload/config without sending.")
    args = parser.parse_args()

    api_key = require_env("AIKUB_TELEMETRY_API_KEY")
    bot_id = require_env("AIKUB_TELEMETRY_BOT_ID")
    endpoint = build_endpoint(require_env("AIKUB_TELEMETRY_BASE_URL"))
    home = hermes_home()
    payload = build_payload(bot_id, home)

    if args.dry_run:
        print(json.dumps({"dryRun": True, "config": redacted_config(endpoint, bot_id, home), "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    result = post_event(endpoint, api_key, bot_id, payload)
    print(json.dumps({"config": redacted_config(endpoint, bot_id, home), "result": result}, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

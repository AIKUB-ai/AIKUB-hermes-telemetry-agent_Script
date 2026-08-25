#!/usr/bin/env python3
"""Hermes sessions/messages telemetry snapshot sender for AIKUB.

Final recipe validated with AIKUB ERP Sessions:
- Reads local Hermes ~/.hermes/state.db.
- Sends visible sessions that have at least one active message with content.
- Sends active non-empty messages only; compacted/inactive/empty messages are skipped.
- Chunks transport by message count so the API/ERP can merge all chunks by batchId.
- Redacts secret-like values and removes invalid control characters before sending.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_PATH = "/v1/telemetry/events"
DEFAULT_CHUNK_SIZE = 100
DEFAULT_MAX_CONTENT_CHARS = 4000

SECRET_PATTERNS = [
    (re.compile(r"(?i)(x-api-key|api[_-]?key|token|password|passwd|secret|authorization|cookie)\s*[:=]\s*([^\s,;]+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+"), r"\1[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|gho_[A-Za-z0-9_]+"), "[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@"), r"\1[REDACTED]:[REDACTED]@"),
]


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or os.getenv("HERMES_REAL_HOME") or "~/.hermes").expanduser()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise SystemExit(f"Missing required env: {name}")
    return value.strip()


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


def build_endpoint(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    if base_url.endswith(EVENT_PATH):
        return base_url
    return base_url + EVENT_PATH


def redact(text: Any) -> str:
    if text is None:
        return ""
    safe = str(text)
    # Postgres JSON rejects NUL bytes; other control chars make ERP rendering noisy.
    safe = safe.replace("\x00", "")
    safe = "".join(ch if (ch in "\n\r\t" or ord(ch) >= 32) else " " for ch in safe)
    for pattern, repl in SECRET_PATTERNS:
        safe = pattern.sub(repl, safe)
    return safe


def iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def read_model_config(model_config: str | None) -> dict[str, str]:
    model = {"provider": "unknown", "name": "unknown"}
    if not model_config:
        return model
    try:
        data = json.loads(model_config)
    except Exception:
        return model
    if isinstance(data, dict):
        model["provider"] = data.get("provider") or data.get("model_provider") or model["provider"]
        model["name"] = data.get("model") or data.get("name") or data.get("default") or model["name"]
    return model


def connect_db(home: Path) -> sqlite3.Connection:
    db_path = home / "state.db"
    if not db_path.exists():
        raise SystemExit(f"Hermes state DB not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def collect_sessions_and_messages(home: Path, max_content_chars: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    con = connect_db(home)
    cur = con.cursor()

    raw_total_sessions = cur.execute("SELECT count(*) FROM sessions").fetchone()[0]
    raw_total_messages = cur.execute("SELECT count(*) FROM messages").fetchone()[0]
    active_messages = cur.execute("SELECT count(*) FROM messages WHERE active = 1").fetchone()[0]
    visible_messages = cur.execute("SELECT count(*) FROM messages WHERE active = 1 AND coalesce(content, '') != ''").fetchone()[0]

    session_rows = cur.execute(
        """
        SELECT
            s.*,
            coalesce(max(m.timestamp), s.started_at) AS last_message_at,
            count(m.id) AS visible_message_count
        FROM sessions s
        JOIN messages m ON m.session_id = s.id
            AND m.active = 1
            AND coalesce(m.content, '') != ''
        GROUP BY s.id
        HAVING visible_message_count > 0
        ORDER BY last_message_at DESC
        """
    ).fetchall()

    visible_session_ids = {row["id"] for row in session_rows}
    platforms = []
    for row in cur.execute(
        """
        SELECT s.source, count(distinct s.id) AS c
        FROM sessions s
        JOIN messages m ON m.session_id = s.id
            AND m.active = 1
            AND coalesce(m.content, '') != ''
        GROUP BY s.source
        ORDER BY c DESC
        """
    ):
        platforms.append({"source": row["source"], "sessionCount": row["c"]})

    last_activity = cur.execute(
        """
        SELECT max(timestamp)
        FROM messages
        WHERE active = 1 AND coalesce(content, '') != ''
        """
    ).fetchone()[0]

    sessions: list[dict[str, Any]] = []
    for row in session_rows:
        model_config = read_model_config(row["model_config"])
        provider = model_config["provider"] if model_config["provider"] != "unknown" else (row["billing_provider"] or "unknown")
        model_name = model_config["name"] if model_config["name"] != "unknown" else (row["model"] or "unknown")
        status = "Active" if (row["ended_at"] is None and not row["archived"]) else ("Archived" if row["archived"] else "Ended")
        sessions.append(
            {
                "sessionId": row["id"],
                "title": redact(row["title"] or "Untitled session")[:180],
                "source": row["source"],
                "platform": row["source"],
                "chatId": redact(row["chat_id"] or ""),
                "chatType": row["chat_type"],
                "model": model_name,
                "provider": provider,
                "messageCount": row["visible_message_count"],
                "toolCallCount": row["tool_call_count"],
                "apiCallCount": row["api_call_count"],
                "inputTokens": row["input_tokens"],
                "outputTokens": row["output_tokens"],
                "startedAt": iso(row["started_at"]),
                "endedAt": iso(row["ended_at"]),
                "lastMessageAt": iso(row["last_message_at"]),
                "archived": bool(row["archived"]),
                "status": status,
            }
        )

    messages: list[dict[str, Any]] = []
    for row in cur.execute(
        """
        SELECT id, session_id, role, content, tool_name, timestamp, token_count,
               platform_message_id, active, compacted
        FROM messages
        WHERE active = 1 AND coalesce(content, '') != ''
        ORDER BY timestamp ASC
        """
    ):
        if row["session_id"] not in visible_session_ids:
            continue
        content = redact(row["content"])
        truncated = False
        if max_content_chars > 0 and len(content) > max_content_chars:
            content = content[:max_content_chars]
            truncated = True
        messages.append(
            {
                "messageId": row["id"],
                "sessionId": row["session_id"],
                "timestamp": iso(row["timestamp"]),
                "role": row["role"],
                "content": content,
                "contentPreview": content[:300],
                "contentTruncated": truncated,
                "toolName": row["tool_name"],
                "tokenCount": row["token_count"],
                "platformMessageId": redact(row["platform_message_id"] or ""),
                "active": bool(row["active"]),
                "compacted": bool(row["compacted"]),
            }
        )

    overview = {
        "totalSessions": len(sessions),
        "visibleSessions": len(sessions),
        "visibleMessages": len(messages),
        "totalMessages": len(messages),
        "activeInStore": len([s for s in sessions if not s["archived"]]),
        "archivedSessions": len([s for s in sessions if s["archived"]]),
        "platforms": platforms,
        "lastActivityAt": iso(last_activity),
        "rawStore": {
            "totalSessions": raw_total_sessions,
            "totalMessages": raw_total_messages,
            "activeMessages": active_messages,
            "visibleMessages": visible_messages,
            "note": "ERP-visible snapshot skips empty, inactive, and compacted messages.",
        },
    }
    return overview, sessions, messages


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)] or [[]]


def build_payload(
    bot_id: str,
    source: str,
    batch_id: str,
    occurred_at: str,
    overview: dict[str, Any],
    sessions: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    chunk_index: int,
    chunk_count: int,
) -> dict[str, Any]:
    return {
        "botId": bot_id,
        "eventType": "bot_sessions_snapshot",
        "severity": "INFO",
        "source": source,
        "traceId": f"{batch_id}-chunk-{chunk_index:03d}",
        "sessionId": batch_id,
        "occurredAt": occurred_at,
        "payload": {
            "identity": {
                "name": bot_id,
                "technicalSlug": bot_id,
                "displayName": bot_id,
                "kind": "Hermes Agent",
            },
            "sessions": {
                "source": "hermes_state_db",
                "batchId": batch_id,
                "chunkIndex": chunk_index,
                "chunkCount": chunk_count,
                "transportChunkingOnly": chunk_count > 1,
                "window": {"type": "full_snapshot_active_non_empty_messages"},
                "overview": overview,
                "sessionsReturned": len(sessions),
                "messagesReturned": len(messages),
                "dedupe": {
                    "sessionKey": "botId + sessionId",
                    "messageKey": "botId + sessionId + messageId",
                },
                "items": sessions,
                "messages": messages,
                "redactionApplied": True,
            },
            "telemetryTest": {
                "section": "sessions",
                "mode": "full_snapshot_visible_messages_chunked",
                "batchId": batch_id,
            },
        },
    }


def send_payload(payload: dict[str, Any], endpoint: str, api_key: str, bot_id: str, timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("x-aikub-botops-token", api_key)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:500]}
        return {"status": resp.status, "bytes": len(body), "response": parsed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Hermes Sessions telemetry to AIKUB.")
    parser.add_argument("--dry-run", action="store_true", help="Build payload summary without POSTing.")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Messages per transport chunk.")
    parser.add_argument("--max-content-chars", type=int, default=DEFAULT_MAX_CONTENT_CHARS, help="Max chars per message content; 0 disables truncation.")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds per chunk.")
    args = parser.parse_args()

    home = hermes_home()
    configured_bot_id = optional_env("AIKUB_TELEMETRY_BOT_ID")
    bot_id = configured_bot_id or "unknown"
    source = optional_env("AIKUB_TELEMETRY_SOURCE", "hermes")
    now = datetime.now(timezone.utc)
    occurred_at = now.isoformat().replace("+00:00", "Z")
    batch_id = f"hermes-sessions-full-{bot_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"

    overview, sessions, messages = collect_sessions_and_messages(home, args.max_content_chars)
    message_chunks = chunks(messages, args.chunk_size)
    payloads = [
        build_payload(bot_id, source, batch_id, occurred_at, overview, sessions, chunk, idx, len(message_chunks))
        for idx, chunk in enumerate(message_chunks, 1)
    ]
    if not configured_bot_id:
        for payload in payloads:
            payload.pop("botId", None)
    total_bytes = sum(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) for payload in payloads)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dryRun": True,
                    "batchId": batch_id,
                    "eventType": "bot_sessions_snapshot",
                    "sessions": len(sessions),
                    "messages": len(messages),
                    "chunkCount": len(message_chunks),
                    "chunkSize": args.chunk_size,
                    "bytesTotal": total_bytes,
                    "mbTotal": round(total_bytes / 1024 / 1024, 2),
                    "overview": overview,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    endpoint = build_endpoint(require_env("AIKUB_TELEMETRY_BASE_URL"))
    api_key = require_token()
    results: list[dict[str, Any]] = []
    for payload in payloads:
        session_payload = payload["payload"]["sessions"]
        try:
            result = send_payload(payload, endpoint, api_key, bot_id, args.timeout)
            results.append(
                {
                    "ok": 200 <= result["status"] < 300,
                    "chunkIndex": session_payload["chunkIndex"],
                    "messagesInChunk": len(session_payload["messages"]),
                    **result,
                }
            )
        except urllib.error.HTTPError as exc:
            results.append(
                {
                    "ok": False,
                    "chunkIndex": session_payload["chunkIndex"],
                    "status": exc.code,
                    "body": exc.read().decode("utf-8", "replace")[:1000],
                }
            )
            break

    print(
        json.dumps(
            {
                "ok": all(result.get("ok") for result in results),
                "batchId": batch_id,
                "eventType": "bot_sessions_snapshot",
                "sessions": len(sessions),
                "messages": len(messages),
                "chunkCount": len(message_chunks),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(result.get("ok") for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Incremental Hermes sessions/messages telemetry sender for AIKUB.

Final recipe validated with AIKUB ERP Sessions:
- Reads local Hermes ~/.hermes/state.db.
- Sends visible sessions that have at least one new active user/assistant message with content.
- Sends active non-empty user/assistant messages only; tool/system/developer, compacted, inactive and empty messages are skipped.
- Chunks transport by message count so the API/ERP can merge all chunks by batchId.
- Saves a local cursor only after every chunk is accepted, preventing duplicates on later crons.
- Redacts secret-like values and removes invalid control characters before sending.
"""

from __future__ import annotations

import argparse
import math
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_PATH = "/v1/telemetry/events"
DEFAULT_CHUNK_SIZE = 100
DEFAULT_MAX_CONTENT_CHARS = 4000
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
STATE_DIR_NAME = "aikub_telemetry_state"
STATE_FILE_NAME = "sessions_messages.json"
SAFE_MESSAGE_ROLES = {"user", "assistant"}

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


def require_base_url() -> str:
    value = optional_env("AIKUB_TELEMETRY_URL") or optional_env("AIKUB_TELEMETRY_BASE_URL")
    if not value:
        raise SystemExit("Missing required env: AIKUB_TELEMETRY_URL")
    return value


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
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    con.row_factory = sqlite3.Row
    return con


def state_path(home: Path) -> Path:
    return home / STATE_DIR_NAME / STATE_FILE_NAME


def read_state(home: Path) -> dict[str, Any] | None:
    path = state_path(home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cursor must be an object")
        timestamp = data.get("lastMessageTimestamp")
        ids = data.get("messageIdsAtLastTimestamp")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or not isinstance(ids, list):
            raise ValueError("cursor timestamp/IDs missing or invalid")
        return data
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Invalid sessions cursor; refusing replay: {path}: {exc}") from exc


def write_state(home: Path, state: dict[str, Any]) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def table_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    return {str(row[1]) for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}


def row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def message_visible_where(columns: set[str], alias: str = "m") -> str:
    prefix = f"{alias}." if alias else ""
    filters = []
    if "role" in columns:
        allowed_roles = ",".join(f"'{role}'" for role in sorted(SAFE_MESSAGE_ROLES))
        filters.append(f"lower(coalesce({prefix}role, '')) IN ({allowed_roles})")
    if "active" in columns:
        filters.append(f"{prefix}active = 1")
    if "content" in columns:
        filters.append(f"coalesce({prefix}content, '') != ''")
    if "compacted" in columns:
        filters.append(f"coalesce({prefix}compacted, 0) = 0")
    return " AND ".join(filters) or "1 = 1"


def select_expr(columns: set[str], column: str, fallback: str, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{column}" if column in columns else f"{fallback} AS {column}"


def message_cursor_where(message_columns: set[str], state: dict[str, Any] | None, first_run_days: int, alias: str = "m") -> tuple[str, list[Any], str]:
    prefix = f"{alias}." if alias else ""
    if "timestamp" not in message_columns:
        raise SystemExit("Messages timestamp missing; refusing full-snapshot fallback")

    last_ts = state.get("lastMessageTimestamp") if state else None
    last_ids = state.get("messageIdsAtLastTimestamp") if state else None
    if isinstance(last_ts, (int, float)) and isinstance(last_ids, list):
        placeholders = ",".join("?" for _ in last_ids) or "NULL"
        return (
            f"({prefix}timestamp > ? OR ({prefix}timestamp = ? AND {prefix}id NOT IN ({placeholders})))",
            [last_ts, last_ts, *[str(item) for item in last_ids]],
            "incremental_since_message_cursor",
        )

    cutoff_ts = (time.time() - max(0, first_run_days) * 86400)
    return f"{prefix}timestamp >= ?", [cutoff_ts], f"first_run_last_{max(0, first_run_days)}_days"


def next_state_from_messages(previous: dict[str, Any] | None, messages: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    if not messages:
        return {
            **(previous or {}),
            "mode": mode,
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "messagesInLastRun": 0,
        }
    sortable = [
        (float(message["_cursorTimestamp"]), str(message["messageId"]))
        for message in messages
        if isinstance(message.get("_cursorTimestamp"), (int, float))
    ]
    if not sortable:
        return {
            **(previous or {}),
            "mode": mode,
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "messagesInLastRun": len(messages),
        }
    last_ts = max(item[0] for item in sortable)
    acknowledged_ids = {message_id for ts, message_id in sortable if ts == last_ts}
    # New messages may share the previous high-water timestamp. Keep earlier
    # ACKed IDs at that timestamp or the next cron will replay them forever.
    if previous and previous.get("lastMessageTimestamp") == last_ts:
        previous_ids = previous.get("messageIdsAtLastTimestamp")
        if isinstance(previous_ids, list):
            acknowledged_ids.update(str(message_id) for message_id in previous_ids)
    return {
        "mode": mode,
        "lastMessageTimestamp": last_ts,
        "lastMessageAt": iso(last_ts),
        "messageIdsAtLastTimestamp": sorted(acknowledged_ids),
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "messagesInLastRun": len(messages),
    }


def collect_sessions_and_messages(home: Path, max_content_chars: int, first_run_days: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    con = connect_db(home)
    cur = con.cursor()
    session_columns = table_columns(cur, "sessions")
    message_columns = table_columns(cur, "messages")
    previous_state = read_state(home)
    # No historical export on installation/migration. Persist this baseline even
    # for an empty run, so messages arriving before the next cron are retained.
    if previous_state is None and first_run_days <= 0:
        previous_state = {"lastMessageTimestamp": time.time(), "messageIdsAtLastTimestamp": []}
    visible_where_m = message_visible_where(message_columns, "m")
    visible_where_plain = message_visible_where(message_columns, "")
    cursor_where_m, cursor_params_m, mode = message_cursor_where(message_columns, previous_state, first_run_days, "m")
    cursor_where_plain, cursor_params_plain, _ = message_cursor_where(message_columns, previous_state, first_run_days, "")

    raw_total_sessions = cur.execute("SELECT count(*) FROM sessions").fetchone()[0]
    raw_total_messages = cur.execute("SELECT count(*) FROM messages").fetchone()[0]
    active_messages = cur.execute("SELECT count(*) FROM messages WHERE active = 1").fetchone()[0] if "active" in message_columns else raw_total_messages
    visible_messages = cur.execute(f"SELECT count(*) FROM messages WHERE {visible_where_plain}").fetchone()[0]

    session_rows = cur.execute(
        f"""
        SELECT
            s.*,
            coalesce(max(m.timestamp), s.started_at) AS last_message_at,
            count(m.id) AS visible_message_count
        FROM sessions s
        JOIN messages m ON m.session_id = s.id AND {visible_where_m} AND {cursor_where_m}
        GROUP BY s.id
        HAVING visible_message_count > 0
        ORDER BY last_message_at DESC
        """,
        cursor_params_m,
    ).fetchall()

    visible_session_ids = {row["id"] for row in session_rows}
    platforms = []
    for row in cur.execute(
        f"""
        SELECT s.source, count(distinct s.id) AS c
        FROM sessions s
        JOIN messages m ON m.session_id = s.id AND {visible_where_m}
        GROUP BY s.source
        ORDER BY c DESC
        """
    ):
        platforms.append({"source": row["source"], "sessionCount": row["c"]})

    last_activity = cur.execute(
        f"""
        SELECT max(timestamp)
        FROM messages
        WHERE {visible_where_plain}
        """
    ).fetchone()[0]

    sessions: list[dict[str, Any]] = []
    for row in session_rows:
        archived = bool(row_value(row, "archived", False))
        model_config = read_model_config(row_value(row, "model_config"))
        provider = model_config["provider"] if model_config["provider"] != "unknown" else (row_value(row, "billing_provider") or "unknown")
        model_name = model_config["name"] if model_config["name"] != "unknown" else (row_value(row, "model") or "unknown")
        status = "Active" if (row_value(row, "ended_at") is None and not archived) else ("Archived" if archived else "Ended")
        sessions.append(
            {
                "sessionId": row["id"],
                "title": redact(row_value(row, "title") or "Untitled session")[:180],
                "source": row_value(row, "source", "hermes"),
                "platform": row_value(row, "source", "hermes"),
                "chatId": redact(row_value(row, "chat_id", "")),
                "chatType": row_value(row, "chat_type"),
                "model": model_name,
                "provider": provider,
                "messageCount": row["visible_message_count"],
                "toolCallCount": row_value(row, "tool_call_count"),
                "apiCallCount": row_value(row, "api_call_count"),
                "inputTokens": row_value(row, "input_tokens"),
                "outputTokens": row_value(row, "output_tokens"),
                "startedAt": iso(row_value(row, "started_at")),
                "endedAt": iso(row_value(row, "ended_at")),
                "lastMessageAt": iso(row["last_message_at"]),
                "archived": archived,
                "status": status,
            }
        )

    messages: list[dict[str, Any]] = []
    for row in cur.execute(
        f"""
        SELECT id, session_id,
               {select_expr(message_columns, "role", "'unknown'")},
               {select_expr(message_columns, "content", "''")},
               {select_expr(message_columns, "tool_name", "NULL")},
               {select_expr(message_columns, "timestamp", "NULL")},
               {select_expr(message_columns, "token_count", "NULL")},
               {select_expr(message_columns, "platform_message_id", "NULL")},
               {select_expr(message_columns, "active", "1")},
               {select_expr(message_columns, "compacted", "0")}
        FROM messages
        WHERE {visible_where_plain} AND {cursor_where_plain}
        ORDER BY timestamp ASC
        """,
        cursor_params_plain,
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
                "role": row["role"] or "unknown",
                "content": content,
                "contentPreview": content[:300],
                "contentTruncated": truncated,
                "toolName": row["tool_name"],
                "tokenCount": row["token_count"],
                "platformMessageId": redact(row["platform_message_id"] or ""),
                "active": bool(row["active"]),
                "compacted": bool(row["compacted"]),
                "_cursorTimestamp": row["timestamp"],
            }
        )

    state_to_save = next_state_from_messages(previous_state, messages, mode)
    for message in messages:
        message.pop("_cursorTimestamp", None)

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
            "note": "ERP-visible snapshot skips empty messages, inactive messages when tracked, and compacted messages when tracked.",
            "schemaCompatibility": {
                "missingSessionColumns": sorted({"chat_id", "chat_type"} - session_columns),
                "missingMessageColumns": sorted({"compacted"} - message_columns),
            },
        },
        "cursor": {
            "mode": mode,
            "previousStateFound": bool(previous_state),
            "statePath": str(state_path(home)),
            "dedupeStrategy": "persistent_message_timestamp_and_ids",
        },
    }
    con.close()
    return overview, sessions, messages, state_to_save


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)] or [[]]


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def json_bytes(value: Any) -> bytes:
    return json.dumps(json_safe(value), ensure_ascii=False, allow_nan=False).encode("utf-8")


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
                "window": {"type": "incremental_active_non_empty_messages"},
                "overview": overview,
                "sessionsReturned": len(sessions),
                "messagesReturned": len(messages),
                "dedupe": {
                    "sessionKey": "botId + sessionId",
                    "messageKey": "botId + sessionId + messageId",
                    "cursor": "message timestamp + ids at last timestamp",
                },
                "items": sessions,
                "messages": messages,
                "redactionApplied": True,
            },
            "telemetryTest": {
                "section": "sessions",
                "mode": "incremental_no_duplicates",
                "batchId": batch_id,
            },
        },
    }


def send_payload_once(payload: dict[str, Any], endpoint: str, api_key: str, bot_id: str, timeout: int) -> dict[str, Any]:
    body = json_bytes(payload)
    request = urllib.request.Request(endpoint, data=body, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("x-aikub-botops-token", api_key)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:500]}
        relay = parsed.get("erpImport") if isinstance(parsed, dict) else None
        chunk = payload.get("payload", {}).get("sessions", {})
        index, count = chunk.get("chunkIndex"), chunk.get("chunkCount")
        nonfinal = type(index) is int and type(count) is int and 1 <= index < count
        delivery = parsed.get("delivery") if isinstance(parsed, dict) else None
        if resp.status == 202 or (resp.status == 200 and isinstance(delivery, dict) and delivery.get("status") == "DELIVERED"):
            # A durable queue owns retries; this does not claim ERP import.
            accepted = (
                isinstance(parsed, dict)
                and parsed.get("stored") is True
                and isinstance(delivery, dict)
                and delivery.get("durable") is True
                and (delivery.get("status") in ("PENDING", "DELIVERED")
                     or (nonfinal and delivery.get("status") == "WAITING_CHUNKS"))
            )
            return {"ok": accepted, "status": resp.status, "bytes": len(body), "response": parsed}
        waiting = isinstance(relay, dict) and relay.get("skipped") and relay.get("reason") == "SESSION_BATCH_INCOMPLETE"
        legacy = (isinstance(parsed, dict) and "delivery" not in parsed
                  and ("stored" not in parsed or parsed["stored"] is True))
        relay_ok = legacy and isinstance(relay, dict) and relay.get("ok") is True and (not relay.get("skipped") or waiting)
        batch = parsed.get("batch") if isinstance(parsed, dict) else None
        final = chunk.get("chunkIndex") == chunk.get("chunkCount")
        complete = not final or (not waiting and (not isinstance(batch, dict) or batch.get("status") == "COMPLETE"))
        return {"ok": 200 <= resp.status < 300 and relay_ok and complete,
                "status": resp.status, "bytes": len(body), "response": parsed}


def send_payload(payload: dict[str, Any], endpoint: str, api_key: str, bot_id: str, timeout: int, retries: int = 3) -> dict[str, Any]:
    last_error: dict[str, Any] | None = None
    for attempt in range(1, retries + 2):
        try:
            result = send_payload_once(payload, endpoint, api_key, bot_id, timeout)
            if result["ok"] or attempt > retries:
                return {**result, "attempts": attempt}
            last_error = {**result, "attempts": attempt}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:1000]
            last_error = {"status": exc.code, "body": body, "attempts": attempt}
            if exc.code not in RETRYABLE_HTTP_STATUSES or attempt > retries:
                return last_error
        except OSError as exc:
            last_error = {"status": None, "body": str(exc)[:1000], "attempts": attempt}
            if attempt > retries:
                return last_error

        time.sleep(min(2 ** (attempt - 1), 10))

    return last_error or {"status": None, "body": "unknown send failure", "attempts": retries + 1}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Hermes Sessions telemetry to AIKUB.")
    parser.add_argument("--dry-run", action="store_true", help="Build payload summary without POSTing.")
    parser.add_argument("--first-run-days", type=int, default=0, help="Explicit initial lookback; default 0 baselines future messages only.")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Messages per transport chunk.")
    parser.add_argument("--max-content-chars", type=int, default=DEFAULT_MAX_CONTENT_CHARS, help="Max chars per message content; 0 disables truncation.")
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout seconds per chunk (includes ERP relay acknowledgement).")
    parser.add_argument("--retries", type=int, default=3, help="Retries per chunk for transient HTTP/API failures.")
    args = parser.parse_args()

    home = hermes_home()
    configured_bot_id = optional_env("AIKUB_TELEMETRY_BOT_ID")
    bot_id = configured_bot_id or "unknown"
    batch_bot_label = configured_bot_id or "token-auth"
    source = optional_env("AIKUB_TELEMETRY_SOURCE", "hermes")
    now = datetime.now(timezone.utc)
    occurred_at = now.isoformat().replace("+00:00", "Z")
    batch_id = f"hermes-sessions-incremental-{batch_bot_label}-{now.strftime('%Y%m%dT%H%M%SZ')}"

    overview, sessions, messages, state_to_save = collect_sessions_and_messages(home, args.max_content_chars, args.first_run_days)
    message_chunks = chunks(messages, args.chunk_size)
    payloads = [
        build_payload(bot_id, source, batch_id, occurred_at, overview, sessions if idx == 1 else [], chunk, idx, len(message_chunks))
        for idx, chunk in enumerate(message_chunks, 1)
    ]
    if not configured_bot_id:
        for payload in payloads:
            payload.pop("botId", None)
    total_bytes = sum(len(json_bytes(payload)) for payload in payloads)

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
                    "mode": overview["cursor"]["mode"],
                    "bytesTotal": total_bytes,
                    "mbTotal": round(total_bytes / 1024 / 1024, 2),
                    "overview": overview,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    endpoint = build_endpoint(require_base_url())
    api_key = require_token()
    results: list[dict[str, Any]] = []
    if not messages:
        write_state(home, state_to_save)
        print(
            json.dumps(
                {
                    "ok": True,
                    "batchId": batch_id,
                    "eventType": "bot_sessions_snapshot",
                    "sessions": 0,
                    "messages": 0,
                    "chunkCount": 0,
                    "mode": overview["cursor"]["mode"],
                    "result": "no_new_messages",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    for payload in payloads:
        session_payload = payload["payload"]["sessions"]
        try:
            result = send_payload(payload, endpoint, api_key, bot_id, args.timeout, args.retries)
            results.append(
                {
                    "ok": result.get("ok", False),
                    "chunkIndex": session_payload["chunkIndex"],
                    "messagesInChunk": len(session_payload["messages"]),
                    "retryable": result["status"] in RETRYABLE_HTTP_STATUSES if result["status"] is not None else True,
                    **result,
                }
            )
            if not result.get("ok"):
                break
        except Exception as exc:
            results.append({"ok": False, "chunkIndex": session_payload["chunkIndex"], "status": None, "body": str(exc)[:1000]})
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
                "mode": overview["cursor"]["mode"],
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    all_ok = all(result.get("ok") for result in results)
    if all_ok:
        write_state(home, state_to_save)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

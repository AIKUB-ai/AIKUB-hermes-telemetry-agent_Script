#!/usr/bin/env python3
"""Incremental Hermes agent.log telemetry sender for AIKUB tests.

Behavior:
- First run: sends logs from the last N days plus lines whose time is unknown.
- Later runs: sends only bytes appended since the saved cursor.
- Splits transport into chunks to avoid oversized API payloads.
- If a chunk is rejected, splits it smaller to isolate the bad log entry instead of freezing the whole bot.
- Quarantines a small number of single bad log entries locally, then advances the cursor for the rest.
- Chunks are NOT duplicates; they are pages from the same logical snapshot.
- Redacts secret-like values before sending.
- Removes invalid control characters before sending so one NUL byte cannot poison the API/DB payload.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER_PATH = Path(__file__).with_name("aikub_telemetry_logger.py")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
LOG_PATH = HERMES_HOME / "logs" / "agent.log"
STATE_PATH = HERMES_HOME / "aikub_telemetry_state" / "logs_agent_log.json"
QUARANTINE_PATH = HERMES_HOME / "aikub_telemetry_state" / "failed_log_entries.jsonl"

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:[,.]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"\s+(?P<level>[A-Z]+)\s+(?P<rest>.*)$"
)
SECRET_PATTERNS = [
    (re.compile(r"(?i)(x-api-key|api[_-]?key|token|password|passwd|secret|authorization|cookie)=([^\s,;]+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@"), r"\1[REDACTED]:[REDACTED]@"),
]


def load_base_logger():
    spec = importlib.util.spec_from_file_location("aikub_logger", LOGGER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {LOGGER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sanitize_control_chars(text: str) -> str:
    """Remove characters that break JSON/DB ingestion while preserving readable logs."""
    text = str(text).replace("\x00", "")
    return "".join(ch if (ch in "\n\r\t" or ord(ch) >= 32) else " " for ch in text)


def redact(text: str) -> str:
    # Postgres JSON rejects NUL bytes; sanitize before redaction so one bad log
    # line cannot poison the whole incremental telemetry cursor forever.
    text = sanitize_control_chars(text)
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def normalize_timestamp(stamp: str | None) -> dict[str, Any]:
    """Resolve only evidenced offsets or an operator-verified logger IANA zone.

    The collector's TZ, /etc/localtime and Hermes scheduling timezone are not
    evidence of the timezone used by the process that wrote historical logs.
    """
    zone_name = os.environ.get("AIKUB_LOG_TIMEZONE", "").strip()
    try:
        if zone_name in {"localtime", "posixrules"} or zone_name.startswith(("posix/", "right/")):
            raise ValueError("Not an IANA zone")
        zone = ZoneInfo(zone_name) if zone_name else None
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("AIKUB_LOG_TIMEZONE must name an installed IANA timezone (e.g. UTC or America/Toronto)") from None
    result = {
        "timestamp": None,
        "sourceTimezone": zone_name or None,
        "timezoneSource": "AIKUB_LOG_TIMEZONE" if zone else "unknown",
        "timestampStatus": "missing" if stamp is None else "unknown_timezone",
    }
    if stamp is None:
        return result
    try:
        dt = datetime.fromisoformat(stamp.replace(",", "."))
    except ValueError:
        result["timestampStatus"] = "invalid_timestamp"
        return result
    if dt.tzinfo is not None:
        offset = dt.strftime("%z")
        result.update(sourceTimezone="UTC" if dt.utcoffset() == timedelta(0) else offset[:3] + ":" + offset[3:],
                      timezoneSource="explicit_offset")
        dt = dt.astimezone(timezone.utc)
    elif zone:
        # Round-trip both folds: gaps have no candidates; overlaps have two.
        candidates = set()
        for fold in (0, 1):
            candidate = dt.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if candidate.astimezone(zone).replace(tzinfo=None) == dt:
                candidates.add(candidate)
        if len(candidates) != 1:
            result["timestampStatus"] = "ambiguous_local_time" if candidates else "nonexistent_local_time"
            return result
        dt = candidates.pop()
    else:
        return result
    precision = "microseconds" if dt.microsecond % 1000 else "milliseconds"
    result.update(timestamp=dt.isoformat(timespec=precision).replace("+00:00", "Z"),
                  timestampStatus="normalized")
    return result


def parse_ts(line: str) -> datetime | None:
    match = LINE_RE.match(line)
    if not match:
        return None
    stamp = normalize_timestamp(match.group("ts"))["timestamp"]
    return datetime.fromisoformat(stamp) if stamp else None


def parse_entry(line_no: int, raw_line: str, parent: dict[str, Any] | None = None) -> dict[str, Any]:
    safe = redact(raw_line.rstrip("\n"))
    match = LINE_RE.match(safe)
    level = match.group("level") if match else None
    component = None
    message = safe
    is_continuation = match is None
    parent_line = None
    parent_timestamp = None
    parent_level = None
    parent_component = None
    if match:
        rest = match.group("rest")
        rest = re.sub(r"^\[[^\]]+\]\s+", "", rest)
        if ": " in rest:
            component, message = rest.split(": ", 1)
        else:
            message = rest
    elif parent:
        parent_line = parent.get("line")
        parent_timestamp = parent.get("timestamp")
        parent_level = parent.get("level")
        parent_component = parent.get("component")
    return {
        "line": line_no,
        **normalize_timestamp(match.group("ts") if match else None),
        "level": level,
        "component": component,
        "message": message,
        "raw": safe,
        "parsed": match is not None,
        "isContinuation": is_continuation,
        "parentLine": parent_line,
        "parentTimestamp": parent_timestamp,
        "parentSourceTimezone": parent.get("sourceTimezone") if is_continuation and parent else None,
        "parentTimezoneSource": parent.get("timezoneSource") if is_continuation and parent else None,
        "parentTimestampStatus": parent.get("timestampStatus") if is_continuation and parent else None,
        "parentLevel": parent_level,
        "parentComponent": parent_component,
    }


def read_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_lines(first_run_days: int, baseline_line: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Validate even for empty files, before sending or changing a cursor.
    configured_timezone = normalize_timestamp(None)
    stat = LOG_PATH.stat()
    state = read_state()
    file_size = stat.st_size
    inode = stat.st_ino

    if state and state.get("inode") == inode and 0 <= int(state.get("offset", 0)) <= file_size:
        start_offset = int(state.get("offset", 0))
        mode = "incremental_since_cursor"
        baseline_line = int(state.get("lastLine", 0))
    elif baseline_line is not None:
        start_offset = 0
        mode = "manual_baseline_line_no_duplicates"
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=first_run_days)
        start_offset = 0
        mode = f"first_run_last_{first_run_days}_days"

    entries: list[dict[str, Any]] = []
    # lastLine remains the global high-water mark across inode changes/truncation.
    # Legacy states already contain it; offsets still refer to the current file.
    line_base = int(state.get("lastLine", 0)) if state else 0
    last_line_no = line_base
    levels: dict[str, int] = {}
    # Orphan lines have no reliable event time: keep them rather than guess.
    included_started = True

    parent_entry: dict[str, Any] | None = None
    if mode == "incremental_since_cursor" and state:
        parent_entry = state.get("lastParent")
    with LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
        if mode == "incremental_since_cursor":
            # Resume exactly at the saved byte cursor. If the cursor somehow lands in the
            # middle of a line after a crash/rotation race, drop only that partial line.
            # Do NOT blindly readline() after seek: when the cursor was saved at EOF,
            # the next appended log line starts exactly at start_offset and must be sent.
            resume_line_no = baseline_line + 1
            if start_offset > 0:
                handle.seek(start_offset - 1)
                previous_char = handle.read(1)
                handle.seek(start_offset)
                if previous_char != "\n":
                    handle.readline()
                    resume_line_no += 1
            else:
                handle.seek(start_offset)
        # readline keeps tell() available, unlike TextIOWrapper's iterator.
        for line_no, line in enumerate(iter(handle.readline, ""), line_base + 1 if mode != "incremental_since_cursor" else resume_line_no):
            last_line_no = line_no
            entry = parse_entry(line_no, line, parent_entry)
            if entry.get("parsed"):
                parent_entry = entry
            if mode.startswith("first_run_last_"):
                if entry["parsed"]:
                    ts = datetime.fromisoformat(entry["timestamp"]) if entry["timestamp"] else None
                    # Unknown/ambiguous times cannot safely be aged out.
                    included_started = ts is None or ts >= cutoff
                if not included_started:
                    continue
            elif mode == "manual_baseline_line_no_duplicates":
                if line_no - line_base <= baseline_line:
                    continue
            entries.append(entry)
            if entry.get("level"):
                levels[entry["level"]] = levels.get(entry["level"], 0) + 1
        # The log may have grown since stat(): persist only the bytes consumed.
        end_offset = handle.tell()

    new_state = {
        "logName": "agent.log",
        "path": str(LOG_PATH),
        "inode": inode,
        "offset": end_offset,
        "lastLine": max(last_line_no, baseline_line or 0),
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "previousStateFound": bool(state),
        "levelsInThisRun": levels,
        "entriesInThisRun": len(entries),
        "sourceTimezone": configured_timezone["sourceTimezone"],
        "timezoneSource": configured_timezone["timezoneSource"],
        # Keep only redacted metadata, never persist the parent's raw/message.
        "lastParent": {key: parent_entry.get(key) for key in (
            "line", "timestamp", "level", "component", "sourceTimezone",
            "timezoneSource", "timestampStatus",
        )} if parent_entry else None,
    }
    return entries, new_state


def append_quarantine(batch_id: str, entry: dict[str, Any], result: dict[str, Any]) -> None:
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "quarantinedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "batchId": batch_id,
        "reason": "single_log_entry_rejected_by_api",
        "status": result.get("status"),
        "body": result.get("body"),
        "entry": entry,
    }
    with QUARANTINE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_log_payload(
    base_payload: dict[str, Any],
    batch_id: str,
    bot_id: str,
    configured_bot_id: str | None,
    source: str,
    state: dict[str, Any],
    chunk: list[dict[str, Any]],
    chunk_index: int,
    chunk_count: int,
) -> dict[str, Any]:
    return {
        **({"botId": bot_id} if configured_bot_id else {}),
        "eventType": "bot_inventory_snapshot",
        "severity": "INFO",
        "source": source,
        "traceId": f"{batch_id}-chunk-{chunk_index:03d}",
        "sessionId": batch_id,
        "occurredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {
            "identity": base_payload["payload"].get("identity"),
            "model": base_payload["payload"].get("model"),
            "logs": {
                "source": "hermes_agent_log_file",
                "logName": "agent.log",
                "path": str(LOG_PATH),
                "batchId": batch_id,
                "chunkIndex": chunk_index,
                "chunkCount": chunk_count,
                "transportChunkingOnly": True,
                "dedupeStrategy": "persistent_file_cursor_offset_inode",
                "mode": state.get("mode"),
                "sourceTimezone": state.get("sourceTimezone"),
                "timezoneSource": state.get("timezoneSource", "unknown"),
                "timestampFormat": "UTC_ISO8601_Z_or_null",
                "lineStart": chunk[0]["line"] if chunk else None,
                "lineEnd": chunk[-1]["line"] if chunk else None,
                "returnedCount": len(chunk),
                "totalReturnedAcrossChunks": state.get("entriesInThisRun"),
                "redactionApplied": True,
                "controlCharSanitizationApplied": True,
                "items": chunk,
            },
            "telemetryTest": {
                "section": "logs",
                "mode": "incremental_no_duplicates",
                "batchId": batch_id,
                "firstRunDays": 3,
            },
        },
    }


def send(entries: list[dict[str, Any]], state: dict[str, Any], chunk_size: int, dry_run: bool, max_quarantine: int) -> dict[str, Any]:
    base = load_base_logger()
    home = HERMES_HOME
    base_payload = base.build_payload(base.optional_env("AIKUB_TELEMETRY_BOT_ID", "unknown"), home)
    endpoint = base.build_endpoint(base.require_env("AIKUB_TELEMETRY_BASE_URL"))
    api_key = base.require_token()
    configured_bot_id = base.optional_env("AIKUB_TELEMETRY_BOT_ID")
    bot_id = configured_bot_id or "unknown"
    source = base.optional_env("AIKUB_TELEMETRY_SOURCE", "hermes")
    batch_id = f"agent-log-incremental-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    planned_chunks = [entries[i:i + chunk_size] for i in range(0, len(entries), chunk_size)] or [[]]
    results: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    abort_reason = None

    def post_chunk(chunk: list[dict[str, Any]], label: str, chunk_index: int, chunk_count: int) -> dict[str, Any]:
        payload = build_log_payload(base_payload, batch_id, bot_id, configured_bot_id, source, state, chunk, chunk_index, chunk_count)
        if dry_run:
            result = {"ok": True, "status": "dry_run", "body": None}
        else:
            result = base.post_event(endpoint, api_key, bot_id, payload)
        results.append({
            "chunkIndex": chunk_index,
            "label": label,
            "ok": result.get("ok"),
            "status": result.get("status"),
            "body": result.get("body"),
            "lineStart": payload["payload"]["logs"]["lineStart"],
            "lineEnd": payload["payload"]["logs"]["lineEnd"],
            "returnedCount": len(chunk),
        })
        if not dry_run:
            time.sleep(0.05)
        return result

    def send_resilient(chunk: list[dict[str, Any]], label: str, chunk_index: int, chunk_count: int) -> bool:
        nonlocal abort_reason
        result = post_chunk(chunk, label, chunk_index, chunk_count)
        if result.get("ok"):
            return True
        if len(chunk) <= 1:
            if len(quarantined) >= max_quarantine:
                abort_reason = f"max_quarantine_exceeded_{max_quarantine}"
                return False
            entry = chunk[0] if chunk else {"line": None}
            quarantined.append({"line": entry.get("line"), "status": result.get("status")})
            if not dry_run and chunk:
                append_quarantine(batch_id, entry, result)
            return True
        mid = max(1, len(chunk) // 2)
        return send_resilient(chunk[:mid], f"{label}.split-a", chunk_index, chunk_count) and send_resilient(chunk[mid:], f"{label}.split-b", chunk_index, chunk_count)

    for idx, chunk in enumerate(planned_chunks, 1):
        if not send_resilient(chunk, f"chunk-{idx:03d}", idx, len(planned_chunks)):
            break

    completed = abort_reason is None
    return {
        "batchId": batch_id,
        "chunkSize": chunk_size,
        "chunksAttempted": len(results),
        "chunksTotal": len(planned_chunks),
        "allOk": completed,
        "entries": len(entries),
        "quarantinedCount": len(quarantined),
        "quarantinePath": str(QUARANTINE_PATH) if quarantined else None,
        "abortReason": abort_reason,
        "stateToSave": state,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-run-days", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=250)
    parser.add_argument("--baseline-line", type=int, default=None, help="Testing only: skip lines <= this line when no cursor exists.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-quarantine", type=int, default=25, help="Max single rejected log lines to quarantine before stopping without advancing cursor.")
    args = parser.parse_args()

    entries, state = collect_lines(args.first_run_days, args.baseline_line)
    result = send(entries, state, args.chunk_size, args.dry_run, args.max_quarantine)
    if result["allOk"] and not args.dry_run:
        write_state(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["allOk"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

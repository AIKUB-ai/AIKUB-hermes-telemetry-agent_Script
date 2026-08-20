#!/usr/bin/env python3
"""Incremental Hermes agent.log telemetry sender for AIKUB tests.

Behavior:
- First run: sends log lines from the last N days.
- Later runs: sends only bytes appended since the saved cursor.
- Splits transport into chunks to avoid oversized API payloads.
- Chunks are NOT duplicates; they are pages from the same logical snapshot.
- Redacts secret-like values before sending.
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

LOGGER_PATH = Path("/home/chopchop/aikub_telemetry_logger.py")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
LOG_PATH = HERMES_HOME / "logs" / "agent.log"
STATE_PATH = HERMES_HOME / "aikub_telemetry_state" / "logs_agent_log.json"

LINE_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(?P<level>[A-Z]+)\s+(?P<rest>.*)$")
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


def redact(text: str) -> str:
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def parse_ts(line: str) -> datetime | None:
    match = LINE_RE.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_entry(line_no: int, raw_line: str, parent: dict[str, Any] | None = None) -> dict[str, Any]:
    safe = redact(raw_line.rstrip("\n"))
    match = LINE_RE.match(safe)
    level = match.group("level") if match else None
    component = None
    message = safe
    timestamp = None
    is_continuation = match is None
    parent_line = None
    parent_timestamp = None
    parent_level = None
    parent_component = None
    if match:
        timestamp = match.group("ts")
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
        "timestamp": timestamp,
        "level": level,
        "component": component,
        "message": message,
        "raw": safe,
        "parsed": match is not None,
        "isContinuation": is_continuation,
        "parentLine": parent_line,
        "parentTimestamp": parent_timestamp,
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
    last_line_no = 0
    levels: dict[str, int] = {}
    included_started = False

    parent_entry: dict[str, Any] | None = None
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
        for line_no, line in enumerate(handle, 1 if mode != "incremental_since_cursor" else resume_line_no):
            last_line_no = line_no
            if mode.startswith("first_run_last_"):
                ts = parse_ts(line)
                if ts is not None and ts < cutoff:
                    continue
                included_started = True
            elif mode == "manual_baseline_line_no_duplicates":
                if line_no <= baseline_line:
                    continue
            entry = parse_entry(line_no, line, parent_entry)
            entries.append(entry)
            if entry.get("parsed"):
                parent_entry = entry
            if entry.get("level"):
                levels[entry["level"]] = levels.get(entry["level"], 0) + 1

    new_state = {
        "logName": "agent.log",
        "path": str(LOG_PATH),
        "inode": inode,
        "offset": file_size,
        "lastLine": max([e["line"] for e in entries], default=(baseline_line or last_line_no)),
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "previousStateFound": bool(state),
        "levelsInThisRun": levels,
        "entriesInThisRun": len(entries),
    }
    return entries, new_state


def send(entries: list[dict[str, Any]], state: dict[str, Any], chunk_size: int, dry_run: bool) -> dict[str, Any]:
    base = load_base_logger()
    home = HERMES_HOME
    base_payload = base.build_payload(base.require_env("AIKUB_TELEMETRY_BOT_ID"), home)
    endpoint = base.build_endpoint(base.require_env("AIKUB_TELEMETRY_BASE_URL"))
    api_key = base.require_env("AIKUB_TELEMETRY_API_KEY")
    bot_id = base.require_env("AIKUB_TELEMETRY_BOT_ID")
    source = base.require_env("AIKUB_TELEMETRY_SOURCE")
    batch_id = f"agent-log-incremental-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    chunks = [entries[i:i + chunk_size] for i in range(0, len(entries), chunk_size)] or [[]]
    results = []
    for idx, chunk in enumerate(chunks, 1):
        payload = {
            "botId": bot_id,
            "eventType": "bot_inventory_snapshot",
            "severity": "INFO",
            "source": source,
            "traceId": f"{batch_id}-chunk-{idx:03d}",
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
                    "chunkIndex": idx,
                    "chunkCount": len(chunks),
                    "transportChunkingOnly": True,
                    "dedupeStrategy": "persistent_file_cursor_offset_inode",
                    "mode": state.get("mode"),
                    "lineStart": chunk[0]["line"] if chunk else None,
                    "lineEnd": chunk[-1]["line"] if chunk else None,
                    "returnedCount": len(chunk),
                    "totalReturnedAcrossChunks": len(entries),
                    "redactionApplied": True,
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
        if dry_run:
            result = {"ok": True, "status": "dry_run", "body": None}
        else:
            result = base.post_event(endpoint, api_key, bot_id, payload)
        results.append({
            "chunkIndex": idx,
            "ok": result.get("ok"),
            "status": result.get("status"),
            "body": result.get("body"),
            "lineStart": payload["payload"]["logs"]["lineStart"],
            "lineEnd": payload["payload"]["logs"]["lineEnd"],
            "returnedCount": len(chunk),
        })
        if not result.get("ok"):
            break
        if not dry_run:
            time.sleep(0.05)
    return {
        "batchId": batch_id,
        "chunkSize": chunk_size,
        "chunksAttempted": len(results),
        "chunksTotal": len(chunks),
        "allOk": all(r["ok"] for r in results) and len(results) == len(chunks),
        "entries": len(entries),
        "stateToSave": state,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-run-days", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=250)
    parser.add_argument("--baseline-line", type=int, default=None, help="Testing only: skip lines <= this line when no cursor exists.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    entries, state = collect_lines(args.first_run_days, args.baseline_line)
    result = send(entries, state, args.chunk_size, args.dry_run)
    if result["allOk"] and not args.dry_run:
        write_state(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["allOk"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

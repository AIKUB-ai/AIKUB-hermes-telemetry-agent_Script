#!/usr/bin/env python3
"""Create and upload a local Hermes bot backup archive to AIKUB ERP/BotOps.

This is intentionally simple and dependency-free so every Hermes bot can run it.
For now it can include .env files in the archive when AIKUB_BACKUP_INCLUDE_ENV=1.
The archive contents are never printed to stdout.

Required env:
  AIKUB_TELEMETRY_BASE_URL=https://...
  AIKUB_TELEMETRY_BOTOPS_TOKEN=[REDACTED]

Optional env:
  AIKUB_TELEMETRY_API_KEY=[REDACTED]   # legacy alias for token
  AIKUB_BACKUP_BOT_SLUG=<bot-slug>     # defaults to AIKUB_TELEMETRY_BOT_ID or hostname
  AIKUB_BACKUP_PATHS=~/.hermes,/opt/my-bot/data  # optional override; otherwise auto-discovery
  AIKUB_BACKUP_DISCOVERY_ROOTS=~,.,/opt,/srv     # optional safe scan roots
  AIKUB_BACKUP_DISCOVERY_MAX_DEPTH=4
  AIKUB_BACKUP_ENV_FILES=.env,~/.hermes/.env     # optional override; otherwise auto-discovery
  AIKUB_BACKUP_INCLUDE_ENV=1           # default: 1 for the current temporary phase
  AIKUB_BACKUP_TYPE=daily              # daily|weekly|monthly
  AIKUB_BACKUP_EXCLUDES=.git,node_modules,.venv,__pycache__,tmp,cache
  AIKUB_BACKUP_CHUNK_BYTES=700000

Usage:
  python3 scripts/aikub_bot_backup_upload.py --dry-run
  python3 scripts/aikub_bot_backup_upload.py
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import io
import json
import os
import platform
import re
import socket
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_EXCLUDES = [
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".next",
    "dist",
    "build",
    "tmp",
    "cache",
    "logs/*.log",
    ".env",
    "*.env",
    ".env.*",
]
ALLOWED_TYPES = {"daily", "weekly", "monthly"}
START_PATH = "/v1/backups/uploads/start"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_dotenv(path: Path) -> None:
    """Tiny .env loader: populate missing env vars without printing secrets."""
    if not path.exists() or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def bootstrap_env() -> None:
    # Common locations for Hermes bots; no logging of values.
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path.home() / ".env")
    load_dotenv(hermes_home() / ".env")


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or os.getenv("HERMES_REAL_HOME") or "~/.hermes").expanduser()


def optional_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def require_env(name: str) -> str:
    value = optional_env(name)
    if not value:
        raise SystemExit(f"Missing required env: {name}")
    return value


def require_token() -> str:
    return require_env("AIKUB_TELEMETRY_BOTOPS_TOKEN") if optional_env("AIKUB_TELEMETRY_BOTOPS_TOKEN") else require_env("AIKUB_TELEMETRY_API_KEY")


def build_url(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    # Accept either the service root or a full /v1/... path.
    if "/v1/" in base:
        base = base.split("/v1/", 1)[0]
    return base + path


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._")
    return value or "unknown-bot"


def configured_bot_slug() -> str:
    return slugify(optional_env("AIKUB_BACKUP_BOT_SLUG") or optional_env("AIKUB_TELEMETRY_BOT_ID") or socket.gethostname())


def csv_paths(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(item.strip()).expanduser() for item in value.split(",") if item.strip()]


def unique_existing(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for raw in paths:
        path = raw.expanduser()
        if not path.exists():
            continue
        try:
            key = path.resolve()
        except OSError:
            key = path.absolute()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def discovery_roots() -> list[Path]:
    configured = csv_paths(optional_env("AIKUB_BACKUP_DISCOVERY_ROOTS"))
    candidates = configured or [Path.home(), Path.cwd(), Path("/opt"), Path("/srv")]
    safe: list[Path] = []
    for path in candidates:
        path = path.expanduser()
        try:
            resolved = path.resolve() if path.exists() else path.absolute()
        except OSError:
            resolved = path.absolute()
        # Keep discovery bounded. Never walk the filesystem root by accident.
        if str(resolved) == "/":
            continue
        if path.exists() and path.is_dir():
            safe.append(path)
    return unique_existing(safe)


def max_discovery_depth() -> int:
    try:
        return max(0, min(8, int(optional_env("AIKUB_BACKUP_DISCOVERY_MAX_DEPTH", "4") or "4")))
    except ValueError:
        return 4


def looks_like_hermes_dir(path: Path) -> bool:
    names = {"config.yaml", "config.yml", "state.db", "skills", "cron", "memories", "plugins", "profiles", "logs"}
    try:
        existing = {child.name for child in path.iterdir()}
    except OSError:
        return False
    if path.name in {".hermes", "hermes"} and existing.intersection(names):
        return True
    score = len(existing.intersection({"skills", "cron", "memories", "plugins"}))
    return score >= 2 or "state.db" in existing


def walk_candidate_dirs(roots: list[Path]) -> Iterable[Path]:
    skip_names = set(DEFAULT_EXCLUDES + [".cache", "Downloads", "Music", "Pictures", "Videos"])
    max_depth = max_discovery_depth()
    for root in roots:
        root = root.expanduser()
        root_depth = len(root.parts)
        for current, dirs, _files in os.walk(root):
            path = Path(current)
            depth = len(path.parts) - root_depth
            dirs[:] = [d for d in dirs if (d == ".hermes" or (d not in skip_names and not d.startswith(".")))]
            if depth > max_depth:
                dirs[:] = []
                continue
            yield path


def discover_hermes_dirs() -> list[Path]:
    candidates: list[Path] = []
    explicit: list[Path | None] = [
        hermes_home(),
        Path(os.getenv("HERMES_PROFILE_DIR", "")).expanduser() if os.getenv("HERMES_PROFILE_DIR") else None,
        Path(os.getenv("HERMES_CONFIG_DIR", "")).expanduser() if os.getenv("HERMES_CONFIG_DIR") else None,
        Path.cwd() / ".hermes",
    ]
    if not (os.getenv("HERMES_HOME") or os.getenv("HERMES_REAL_HOME")):
        explicit.append(Path.home() / ".hermes")
    candidates.extend([p for p in explicit if p is not None])
    roots = discovery_roots()
    for path in walk_candidate_dirs(roots):
        if looks_like_hermes_dir(path):
            candidates.append(path)
        # Hermes profiles may store personal memory/skills under profiles/<name>/...
        if path.name == "profiles" and path.is_dir():
            try:
                candidates.extend(child for child in path.iterdir() if child.is_dir())
            except OSError:
                pass
    return unique_existing(candidates)


def discover_app_dirs() -> list[Path]:
    markers = {"package.json", "pyproject.toml", "requirements.txt", "Dockerfile", "docker-compose.yml", "compose.yml", "bot.py", "main.py", "app.py"}
    candidates: list[Path] = []
    for root in discovery_roots():
        for path in walk_candidate_dirs([root]):
            try:
                names = {child.name for child in path.iterdir()}
            except OSError:
                continue
            if names.intersection(markers):
                candidates.append(path)
    return unique_existing(candidates)[:20]


def default_backup_paths() -> list[Path]:
    configured = csv_paths(optional_env("AIKUB_BACKUP_PATHS"))
    if configured:
        return configured

    paths: list[Path] = []
    for home in discover_hermes_dirs():
        paths.append(home)
        profile_root = home / "profiles"
        if profile_root.exists():
            try:
                paths.extend(child for child in profile_root.iterdir() if child.is_dir())
            except OSError:
                pass
    paths.extend(discover_app_dirs())
    return unique_existing(paths)


def env_files() -> list[Path]:
    configured = csv_paths(optional_env("AIKUB_BACKUP_ENV_FILES"))
    if configured:
        return configured
    candidates: list[Path] = [Path.cwd() / ".env", hermes_home() / ".env"]
    roots = discovery_roots()
    home_env = Path.home() / ".env"
    if not optional_env("AIKUB_BACKUP_DISCOVERY_ROOTS") or any(root == Path.home() for root in roots):
        candidates.append(home_env)
    for root in default_backup_paths() + roots:
        env_path = root / ".env"
        if env_path.exists() and env_path.is_file():
            candidates.append(env_path)
    # Bounded extra search for env files near likely bot/Hermes roots.
    for root in discovery_roots():
        for path in walk_candidate_dirs([root]):
            env_path = path / ".env"
            if env_path.exists() and env_path.is_file():
                candidates.append(env_path)
    return unique_existing(candidates)


def exclude_patterns() -> list[str]:
    return [p.strip() for p in (optional_env("AIKUB_BACKUP_EXCLUDES") or ",".join(DEFAULT_EXCLUDES)).split(",") if p.strip()]


def should_exclude(path: Path, root: Path, patterns: list[str]) -> bool:
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = path.name
    rel = rel.replace(os.sep, "/")
    parts = rel.split("/")
    for pattern in patterns:
        normalized = pattern.replace(os.sep, "/")
        if fnmatch.fnmatch(rel, normalized) or any(fnmatch.fnmatch(part, normalized) for part in parts):
            return True
    return False


def safe_label(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    raw = str(resolved).strip("/").replace("/", "__")
    return slugify(raw)[-140:] or slugify(path.name)


def add_path_to_tar(tar: tarfile.TarFile, path: Path, arc_prefix: str, excludes: list[str]) -> tuple[int, int, list[str]]:
    count = 0
    total = 0
    skipped: list[str] = []
    path = path.expanduser()
    if not path.exists():
        skipped.append(f"missing:{path}")
        return count, total, skipped
    root = path if path.is_dir() else path.parent
    max_file_bytes = int(optional_env("AIKUB_BACKUP_MAX_FILE_BYTES", str(50 * 1024 * 1024)) or str(50 * 1024 * 1024))

    candidates: Iterable[Path]
    if path.is_dir():
        candidates = path.rglob("*")
    else:
        candidates = [path]

    for item in candidates:
        try:
            if item.is_symlink():
                skipped.append(f"symlink:{item}")
                continue
            if should_exclude(item, root, excludes):
                continue
            if item.is_dir():
                continue
            if not item.is_file():
                skipped.append(f"not-file:{item}")
                continue
            size = item.stat().st_size
            if size > max_file_bytes:
                skipped.append(f"too-large:{item}")
                continue
            rel = item.relative_to(root) if path.is_dir() else Path(item.name)
            arcname = f"{arc_prefix}/{safe_label(path)}/{rel.as_posix()}"
            tar.add(item, arcname=arcname, recursive=False)
            count += 1
            total += size
        except OSError as exc:
            skipped.append(f"error:{item}:{exc.__class__.__name__}")
    return count, total, skipped


def build_archive(output_dir: Path) -> dict[str, Any]:
    bot_slug = configured_bot_slug()
    backup_type = (optional_env("AIKUB_BACKUP_TYPE", "daily") or "daily").lower()
    if backup_type not in ALLOWED_TYPES:
        raise SystemExit(f"Invalid AIKUB_BACKUP_TYPE={backup_type}; expected one of {sorted(ALLOWED_TYPES)}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_key = optional_env("AIKUB_BACKUP_KEY") or f"{bot_slug}-{backup_type}-{timestamp}"
    filename = f"{backup_key}.tar.gz"
    archive_path = output_dir / filename
    excludes = exclude_patterns()
    include_env = (optional_env("AIKUB_BACKUP_INCLUDE_ENV", "1") or "1").lower() not in {"0", "false", "no", "off"}

    included_files = 0
    included_bytes = 0
    skipped: list[str] = []
    roots = default_backup_paths()
    envs = env_files() if include_env else []
    manifest = {
        "schemaVersion": "aikub.bot.local-backup.v1",
        "createdAt": utc_now(),
        "botSlug": bot_slug,
        "type": backup_type,
        "backupKey": backup_key,
        "format": "tar.gz",
        "includesEnv": include_env,
        "envMode": "plain-temporary",
        "roots": [str(p) for p in roots],
        "envFiles": [str(p) for p in envs],
        "discovery": {
            "mode": "explicit" if optional_env("AIKUB_BACKUP_PATHS") else "auto",
            "rootsScanned": [str(p) for p in discovery_roots()],
            "maxDepth": max_discovery_depth(),
            "hermesDirs": [str(p) for p in discover_hermes_dirs()],
        },
        "excludes": excludes,
        "host": {"hostname": socket.gethostname(), "platform": platform.platform()},
        "warning": ".env files are included in plain text for the temporary AIKUB rollout; do not expose downloaded archives.",
    }

    with tarfile.open(archive_path, "w:gz") as tar:
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
        included_files += 1
        included_bytes += len(data)

        for root in roots:
            c, b, s = add_path_to_tar(tar, root, "paths", excludes)
            included_files += c
            included_bytes += b
            skipped.extend(s)

        for env_file in envs:
            c, b, s = add_path_to_tar(tar, env_file, "env", [])
            included_files += c
            included_bytes += b
            skipped.extend(s)

    payload = archive_path.read_bytes()
    sha256 = hashlib.sha256(payload).hexdigest()
    chunk_bytes = int(optional_env("AIKUB_BACKUP_CHUNK_BYTES", "700000") or "700000")
    chunks = [payload[i:i + chunk_bytes] for i in range(0, len(payload), chunk_bytes)] or [b""]
    return {
        "path": archive_path,
        "filename": filename,
        "botSlug": bot_slug,
        "type": backup_type,
        "backupKey": backup_key,
        "sizeBytes": len(payload),
        "sha256": sha256,
        "chunkBytes": chunk_bytes,
        "chunkCount": len(chunks),
        "includedFiles": included_files,
        "includedSourceBytes": included_bytes,
        "skipped": skipped[:200],
        "chunks": chunks,
        "manifest": manifest,
    }


def post_json(url: str, token: str, payload: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json", "x-aikub-botops-token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 300, "status": response.status, "body": json.loads(raw) if raw else None}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        return {"ok": False, "status": exc.code, "body": body}
    except Exception as exc:
        return {"ok": False, "status": None, "body": f"{exc.__class__.__name__}: {exc}"}


def extract_run_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("runId", "id", "uploadId"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    run = body.get("run")
    if isinstance(run, dict):
        value = run.get("id") or run.get("runId")
        if isinstance(value, str) and value:
            return value
    return None


def upload_archive(archive: dict[str, Any]) -> dict[str, Any]:
    base_url = require_env("AIKUB_TELEMETRY_BASE_URL")
    token = require_token()
    start_url = build_url(base_url, START_PATH)
    start_payload = {
        "botSlug": archive["botSlug"],
        "type": archive["type"],
        "backupKey": archive["backupKey"],
        "filename": archive["filename"],
        "mimeType": "application/gzip",
        "sizeBytes": archive["sizeBytes"],
        "sha256": archive["sha256"],
        "chunkCount": archive["chunkCount"],
        "metadata": {
            "schemaVersion": archive["manifest"]["schemaVersion"],
            "format": "tar.gz",
            "includesEnv": archive["manifest"]["includesEnv"],
            "envMode": archive["manifest"]["envMode"],
            "includedFiles": archive["includedFiles"],
            "includedSourceBytes": archive["includedSourceBytes"],
            "source": "scripts/aikub_bot_backup_upload.py",
        },
    }
    started = post_json(start_url, token, start_payload)
    if not started.get("ok"):
        return {"ok": False, "stage": "start", "startUrl": start_url, "result": started}
    run_id = extract_run_id(started.get("body"))
    if not run_id:
        return {"ok": False, "stage": "start", "startUrl": start_url, "result": started, "error": "Missing runId/id/uploadId in start response"}

    chunk_results = []
    for index, chunk in enumerate(archive["chunks"], 1):
        chunk_url = build_url(base_url, f"/v1/backups/uploads/{run_id}/chunk")
        chunk_payload = {
            "runId": run_id,
            "index": index,
            "chunkIndex": index,
            "total": archive["chunkCount"],
            "chunkCount": archive["chunkCount"],
            "dataBase64": base64.b64encode(chunk).decode("ascii"),
            "sha256": hashlib.sha256(chunk).hexdigest(),
        }
        result = post_json(chunk_url, token, chunk_payload)
        chunk_results.append({"index": index, "ok": result.get("ok"), "status": result.get("status"), "body": result.get("body")})
        if not result.get("ok"):
            return {"ok": False, "stage": "chunk", "runId": run_id, "failedChunk": index, "chunks": chunk_results}

    complete_url = build_url(base_url, f"/v1/backups/uploads/{run_id}/complete")
    completed = post_json(complete_url, token, {"runId": run_id, "sha256": archive["sha256"], "sizeBytes": archive["sizeBytes"], "chunkCount": archive["chunkCount"]})
    return {"ok": bool(completed.get("ok")), "stage": "complete", "runId": run_id, "complete": completed, "chunks": chunk_results}


def public_summary(archive: dict[str, Any]) -> dict[str, Any]:
    return {
        "botSlug": archive["botSlug"],
        "type": archive["type"],
        "backupKey": archive["backupKey"],
        "filename": archive["filename"],
        "sizeBytes": archive["sizeBytes"],
        "sha256": archive["sha256"],
        "chunkCount": archive["chunkCount"],
        "includedFiles": archive["includedFiles"],
        "includedSourceBytes": archive["includedSourceBytes"],
        "includesEnv": archive["manifest"].get("includesEnv"),
        "envMode": archive["manifest"].get("envMode"),
        "roots": archive["manifest"].get("roots"),
        "discoveryMode": archive["manifest"].get("discovery", {}).get("mode"),
        "discoveredHermesDirCount": len(archive["manifest"].get("discovery", {}).get("hermesDirs", [])),
        "envFiles": ["[REDACTED_PATH]" for _ in archive["manifest"].get("envFiles", [])],
        "skippedCount": len(archive.get("skipped", [])),
        "skippedPreview": archive.get("skipped", [])[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and upload a local Hermes bot backup archive to AIKUB ERP.")
    parser.add_argument("--dry-run", action="store_true", help="Create the archive and print metadata only; do not upload.")
    parser.add_argument("--keep-archive", action="store_true", help="Keep the generated .tar.gz locally after upload/dry-run.")
    parser.add_argument("--output-dir", default=None, help="Directory for temporary archive output.")
    args = parser.parse_args()

    bootstrap_env()
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else Path(tempfile.mkdtemp(prefix="aikub-bot-backup-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = build_archive(output_dir)

    if args.dry_run:
        print(json.dumps({"dryRun": True, "archive": public_summary(archive), "headers": {"x-aikub-botops-token": "[REDACTED]"}}, ensure_ascii=False, indent=2))
        return 0

    result = upload_archive(archive)
    print(json.dumps({"archive": public_summary(archive), "upload": result}, ensure_ascii=False, indent=2))

    if not args.keep_archive:
        try:
            Path(archive["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Runtime snapshot helpers for the admin area."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import current_app

from constants import APP_VERSION, get_git_sha_info
from schema_migrations import get_schema_history, get_schema_state, SCHEMA_VERSION
from startup import BOOTSTRAP_VERSION, get_bootstrap_history, get_bootstrap_state
from time_utils import format_container_time


def _mask_database_uri(uri):
    if not uri or uri.startswith("sqlite:"):
        return uri
    parsed = urlsplit(uri)
    if not parsed.scheme or not parsed.hostname:
        return uri
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password is not None:
            auth += ":***"
        auth += "@"
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def _read_pid1_cmdline():
    try:
        return Path("/proc/1/cmdline").read_text().replace("\x00", " ").strip()
    except OSError:
        return None


def _path_status(path):
    if not path:
        return {"path": None, "exists": False, "writable": False}
    candidate = Path(path)
    target = candidate if candidate.exists() else candidate.parent
    return {
        "path": str(candidate),
        "exists": candidate.exists(),
        "writable": os.access(target, os.W_OK),
    }


def build_runtime_details() -> dict:
    """Build the runtime diagnostics payload."""
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    sqlite_file = db_uri.removeprefix("sqlite:////") if db_uri.startswith("sqlite:////") else None
    data_dir = os.environ.get("DATA_DIR", "/data")
    log_dir = os.environ.get("LOG_DIR", "/data/logs")
    git_sha, git_sha_source = get_git_sha_info()
    return {
        "app_version": APP_VERSION,
        "git_sha": git_sha,
        "git_sha_source": git_sha_source,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "home": os.environ.get("HOME", ""),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "database_uri": _mask_database_uri(db_uri),
        "database_file": sqlite_file,
        "log_dir": log_dir,
        "data_dir": data_dir,
        "log_level": os.environ.get("LOG_LEVEL", "INFO"),
        "tz": os.environ.get("TZ", "UTC"),
        "flask_env": os.environ.get("FLASK_ENV", "production"),
        "puid": os.environ.get("PUID", "0"),
        "pgid": os.environ.get("PGID", "0"),
        "pid1_cmdline": _read_pid1_cmdline(),
        "schema_state": get_schema_state(),
        "schema_history": [
            {**entry, "formatted_applied_at": format_container_time(entry.get("applied_at"))}
            for entry in get_schema_history()
        ],
        "schema_version": SCHEMA_VERSION,
        "bootstrap_state": get_bootstrap_state(),
        "bootstrap_history": [
            {**entry, "formatted_applied_at": format_container_time(entry.get("applied_at"))}
            for entry in get_bootstrap_history()
        ],
        "bootstrap_version": BOOTSTRAP_VERSION,
        "path_checks": [
            {"label": "Data Directory", **_path_status(data_dir)},
            {"label": "Log Directory", **_path_status(log_dir)},
            {"label": "SQLite File", **_path_status(sqlite_file)} if sqlite_file else None,
        ],
    }

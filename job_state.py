"""Shared helpers for persisted background-job state files."""

from __future__ import annotations

import json
import os
from threading import Lock


def read_json_file(path: str, default: dict) -> dict:
    """Read a JSON file, returning `default` if it is missing or invalid."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(default)


def write_json_file(path: str, data: dict, *, lock: Lock | None = None) -> None:
    """Atomically write a JSON file, optionally under a caller-provided lock."""

    def _write() -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)

    if lock is None:
        _write()
        return
    with lock:
        _write()


def reset_json_file(path: str, default: dict, *, lock: Lock | None = None) -> None:
    """Rewrite a JSON state file back to its default payload."""
    write_json_file(path, default, lock=lock)

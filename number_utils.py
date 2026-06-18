"""Shared helpers for parsing simple numeric form inputs."""

from __future__ import annotations

import re


def parse_nonnegative_whole_number(
    raw_value: str, label: str
) -> tuple[int | None, str | None]:
    """Parse a non-negative whole number, treating blank/zero-like inputs as empty."""
    cleaned = (raw_value or "").strip()
    if not cleaned or cleaned.lower() in {"0", "none", "n/a", "-"}:
        return None, None
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned), None
    return None, f"{label} must be a whole number."


def parse_positive_whole_number(raw_value: str) -> tuple[int | None, str | None]:
    """Parse a positive whole number, defaulting blanks to 1."""
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return 1, None
    if re.fullmatch(r"[1-9]\d*", cleaned):
        return int(cleaned), None
    return None, "Quantity must be a positive whole number."

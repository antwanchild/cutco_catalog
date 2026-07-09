"""Helpers for parsing and formatting container-local timestamps."""

import os
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _MountainTimezone(tzinfo):
    """Mountain Time with U.S. DST rules for Windows fallback."""

    def __init__(self, name: str) -> None:
        self.key = name

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return timedelta(hours=-6) if self.dst(dt) else timedelta(hours=-7)

    def dst(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return timedelta(0)
        naive = dt.replace(tzinfo=None)
        year = naive.year
        dst_start = self._second_sunday(year, 3)
        dst_end = self._first_sunday(year, 11)
        return timedelta(hours=1) if dst_start <= naive < dst_end else timedelta(0)

    def tzname(self, dt: datetime | None) -> str | None:
        return "MDT" if self.dst(dt) else "MST"

    @staticmethod
    def _first_sunday(year: int, month: int) -> datetime:
        first_day = datetime(year, month, 1)
        days_until_sunday = (6 - first_day.weekday()) % 7
        return first_day + timedelta(days=days_until_sunday, hours=2)

    @classmethod
    def _second_sunday(cls, year: int, month: int) -> datetime:
        return cls._first_sunday(year, month) + timedelta(days=7)


def container_timezone() -> tuple[tzinfo | ZoneInfo, str]:
    """Return the configured container timezone and its display name."""
    tz_name = os.environ.get("TZ", "UTC").strip() or "UTC"
    if tz_name in {"America/Boise", "America/Denver"}:
        return _MountainTimezone(tz_name), tz_name
    try:
        return ZoneInfo(tz_name), tz_name
    except ZoneInfoNotFoundError:
        return timezone.utc, "UTC"


def format_container_time(value: str | None) -> str:
    """Format an ISO timestamp in the container timezone."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz, tz_name = container_timezone()
    dt = dt.astimezone(tz)
    date_part = dt.strftime("%b %d, %Y").replace(" 0", " ")
    time_part = dt.strftime("%I:%M %p").lstrip("0")
    return f"{date_part}, {time_part} {dt.strftime('%Z') or tz_name}"

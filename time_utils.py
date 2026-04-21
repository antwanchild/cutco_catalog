from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def container_timezone() -> tuple[timezone | ZoneInfo, str]:
    tz_name = os.environ.get("TZ", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(tz_name), tz_name
    except ZoneInfoNotFoundError:
        return timezone.utc, "UTC"


def format_container_time(value: str | None) -> str:
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

"""Shared helpers for sharpening and cookware log views."""

from __future__ import annotations

from datetime import date

from constants import (
    COOKWARE_CATEGORIES, SHARPENING_PAGE_EXCLUDED_CATEGORIES,
    SHARPENING_PAGE_EXCLUDED_NAME_KEYWORDS, SHARPENING_PAGE_INCLUDED_NAME_KEYWORDS,
)
from models import CookwareSession, Item, SharpeningLog


def _safe_parse_iso_date(raw: str) -> date | None:
    """Return parsed ISO date or None when invalid."""
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _is_sharpening_page_item(item: Item) -> bool:
    """Return whether an item should appear on the sharpening page."""
    category = item.category or ""
    name = (item.name or "").lower()
    if any(keyword in name for keyword in SHARPENING_PAGE_INCLUDED_NAME_KEYWORDS):
        return True
    if category in COOKWARE_CATEGORIES or category in SHARPENING_PAGE_EXCLUDED_CATEGORIES:
        return False
    return not any(keyword in name for keyword in SHARPENING_PAGE_EXCLUDED_NAME_KEYWORDS)


def _build_sharpening_rows(
    all_entries: list[SharpeningLog],
    *,
    today: date,
    threshold_days: int,
) -> tuple[list[dict], dict[int, int]]:
    """Build tracked sharpening rows and per-item counts."""
    last_by_item: dict[int, str] = {}
    count_by_item: dict[int, int] = {}
    for entry in all_entries:
        count_by_item[entry.item_id] = count_by_item.get(entry.item_id, 0) + 1
        if entry.item_id not in last_by_item:
            last_by_item[entry.item_id] = entry.sharpened_on

    tracked: list[dict] = []
    for item_id, last_str in last_by_item.items():
        parsed_last = _safe_parse_iso_date(last_str)
        if not parsed_last:
            continue
        days_since = (today - parsed_last).days
        tracked.append(dict(
            item_id=item_id,
            last_date=last_str,
            days_since=days_since,
            overdue=days_since > threshold_days,
            event_count=count_by_item[item_id],
        ))

    tracked.sort(key=lambda row: (0 if row["overdue"] else 1, -row["days_since"]))
    return tracked, count_by_item


def _build_cookware_rows(
    sessions: list[CookwareSession],
    *,
    today: date,
    threshold_days: int,
) -> tuple[list[dict], dict[int, int], dict[int, list[int]], set[int]]:
    """Build tracked cookware rows and per-item aggregates."""
    last_by_item: dict[int, str] = {}
    count_by_item: dict[int, int] = {}
    rating_by_item: dict[int, list[int]] = {}
    for cookware_session in sessions:
        item_id = cookware_session.item_id
        count_by_item[item_id] = count_by_item.get(item_id, 0) + 1
        if item_id not in last_by_item:
            last_by_item[item_id] = cookware_session.used_on
        if cookware_session.rating is not None:
            rating_by_item.setdefault(item_id, []).append(cookware_session.rating)

    tracked: list[dict] = []
    for item_id, last_str in last_by_item.items():
        parsed_last = _safe_parse_iso_date(last_str)
        if not parsed_last:
            continue
        days_since = (today - parsed_last).days
        ratings = rating_by_item.get(item_id, [])
        tracked.append(dict(
            item_id=item_id,
            last_date=last_str,
            days_since=days_since,
            stale=days_since > threshold_days,
            session_count=count_by_item[item_id],
            avg_rating=round(sum(ratings) / len(ratings), 1) if ratings else None,
        ))

    tracked.sort(key=lambda row: (0 if row["stale"] else 1, -row["days_since"]))
    return tracked, count_by_item, rating_by_item, set(last_by_item)


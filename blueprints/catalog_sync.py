"""Catalog sync helpers for the catalog blueprint."""

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from flask import Blueprint, current_app
from sqlalchemy.orm import selectinload

from constants import (
    DATA_DIR,
    SYNC_BLOCKED_CATEGORIES,
    UNKNOWN_COLOR,
)
from extensions import db
from job_state import read_json_file, reset_json_file, write_json_file
from models import (
    Item,
    ItemSetMember,
    Set,
    parse_alternate_skus,
    record_activity,
    reconcile_unknown_variant,
)
from scraping import (
    _member_hover_title,
    scrape_catalog,
    scrape_item_specs,
    scrape_item_variant_colors,
    scrape_sets,
)

catalog_bp = Blueprint("catalog", __name__)
logger = logging.getLogger(__name__)
UNCATEGORIZED_FILTER = "__uncategorized__"
_CATALOG_SYNC_JOB_FILE = os.path.join(DATA_DIR, "catalog_sync_job.json")
_catalog_sync_job_lock = threading.Lock()


class ResolvedMember(TypedDict):
    """A set member resolved to a catalog item with an aggregate quantity."""

    item: Item
    quantity: int


def _read_catalog_sync_job() -> dict:
    return read_json_file(
        _CATALOG_SYNC_JOB_FILE,
        {
            "status": "idle",
            "progress": [],
            "results": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
            "preview": None,
            "heartbeat_at": None,
        },
    )


def _write_catalog_sync_job(data: dict) -> None:
    write_json_file(_CATALOG_SYNC_JOB_FILE, data, lock=_catalog_sync_job_lock)


def _reset_catalog_sync_job() -> None:
    reset_json_file(
        _CATALOG_SYNC_JOB_FILE,
        {
            "status": "idle",
            "progress": [],
            "results": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
            "preview": None,
            "heartbeat_at": None,
        },
        lock=_catalog_sync_job_lock,
    )


def _build_catalog_sync_preview(scraped: list[dict], scraped_sets: list[dict]) -> dict:
    existing_skus = {
        item.sku for item in Item.query.filter(Item.sku.isnot(None)).all() if item.sku
    }
    new_items = [
        scraped_item
        for scraped_item in scraped
        if scraped_item["sku"] not in existing_skus
    ]

    _grouped_unsorted: dict = {}
    for item in new_items:
        _grouped_unsorted.setdefault(item["category"], []).append(item)

    def _sku_sort_key(item):
        sku = item.get("sku") or ""
        sku_num_match = re.match(r"(\d+)", sku)
        return (0, int(sku_num_match.group(1)), sku) if sku_num_match else (1, 0, sku)

    grouped = {
        cat: sorted(items, key=_sku_sort_key)
        for cat, items in sorted(
            _grouped_unsorted.items(), key=lambda kv: kv[0].lower()
        )
    }

    if scraped:
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        _details_map: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            _future_map: dict[Any, tuple[str, str]] = {}
            for item_data in new_items:
                sku = str(item_data["sku"])
                url = str(item_data["url"])
                _future_map[pool.submit(scrape_item_specs, url)] = ("specs", sku)
                _future_map[pool.submit(scrape_item_variant_colors, url)] = (
                    "variant_colors",
                    sku,
                )
            for future in _as_completed(_future_map):
                kind, sku = _future_map[future]
                _details_map.setdefault(sku, {})[kind] = future.result()
        for item in scraped:
            details = _details_map.get(item["sku"], {})
            raw_variant_colors = details.get("variant_colors", ())
            item["variant_colors"] = [
                color
                for color in raw_variant_colors
                if color and color != UNKNOWN_COLOR
            ]
        for item in new_items:
            details = _details_map.get(item["sku"], {})
            specs = details.get("specs", {})
            item["edge_type"] = specs.get("edge_type", "Unknown")
            item["msrp"] = specs.get("msrp")
            item["blade_length"] = specs.get("blade_length")
            item["overall_length"] = specs.get("overall_length")
            item["weight"] = specs.get("weight")

    existing_sets = {item_set.name.lower() for item_set in Set.query.all()}
    new_sets = sorted(
        (
            scraped_set
            for scraped_set in scraped_sets
            if scraped_set["name"].lower() not in existing_sets
        ),
        key=_sku_sort_key,
    )
    existing_sets_data = [
        scraped_set
        for scraped_set in scraped_sets
        if scraped_set["name"].lower() in existing_sets
    ]

    existing_set_lookup = {
        item_set.name.lower(): item_set
        for item_set in Set.query.options(
            selectinload(Set.members).selectinload(ItemSetMember.item)
        ).all()
    }

    catalog_sku_lookup = {
        sku: item
        for item in Item.query.filter(Item.sku.isnot(None)).all()
        if (sku := _normalize_member_sku(item.sku))
    }
    preview_name_lookup = _build_member_name_lookup(
        [
            *Item.query.filter(Item.sku.isnot(None)).all(),
            *new_items,
        ]
    )
    scraped_sku_lookup = {
        sku for item in new_items if (sku := _normalize_member_sku(item.get("sku")))
    }
    for item_set in (*new_sets, *existing_sets_data):
        member_entries = item_set.get("member_entries") or []
        member_snapshot_rows, not_in_catalog_skus = _build_member_status_rows(
            member_entries,
            catalog_sku_lookup,
            preview_name_lookup,
            set_sku=_normalize_member_sku(item_set.get("sku")),
            found_skus=scraped_sku_lookup,
        )
        item_set["member_snapshot_rows"] = member_snapshot_rows
        item_set["not_in_catalog_skus"] = not_in_catalog_skus
        item_set["member_data_json"] = json.dumps(member_entries, ensure_ascii=False)
        if item_set in existing_sets_data:
            current_set = existing_set_lookup.get(item_set["name"].lower())
            item_set["membership_preview"] = (
                _build_set_membership_preview(
                    current_set,
                    member_entries,
                    catalog_sku_lookup,
                    preview_name_lookup,
                )
                if current_set
                else {
                    "has_changes": False,
                    "summary": "Unable to load current set state.",
                    "current_rows": [],
                    "incoming_rows": member_snapshot_rows,
                    "change_rows": [],
                    "added": 0,
                    "removed": 0,
                    "quantity_changed": 0,
                    "current_count": 0,
                    "incoming_count": len(member_snapshot_rows),
                }
            )

    changed_existing_sets_data = [
        item_set
        for item_set in existing_sets_data
        if item_set.get("membership_preview", {}).get("has_changes")
    ]
    has_missing_set_members = any(
        item_set.get("not_in_catalog_skus")
        for item_set in (*new_sets, *existing_sets_data)
    )

    return {
        "grouped": grouped,
        "scraped_items": scraped,
        "new_items": new_items,
        "scraped_total": len(scraped),
        "new_sets": new_sets,
        "existing_sets_data": existing_sets_data,
        "changed_existing_sets_data": changed_existing_sets_data,
        "scraped_sets_total": len(scraped_sets),
        "has_missing_set_members": has_missing_set_members,
        "blocked_categories": sorted(SYNC_BLOCKED_CATEGORIES),
    }


def _run_catalog_sync_job(app) -> None:
    with app.app_context():
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        progress: list[str] = []

        def log(message: str) -> None:
            progress.append(message)
            _write_catalog_sync_job(
                {
                    "status": "running",
                    "progress": list(progress),
                    "results": None,
                    "error": None,
                    "started_at": started_at,
                    "finished_at": None,
                    "preview": None,
                    "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )

        try:
            log("Scraping live catalog…")
            scraped, set_candidates = scrape_catalog(progress_cb=log)
            log(f"Found {len(scraped)} items on cutco.com")
            log("Scraping set pages…")
            scraped_sets = scrape_sets(extra_candidates=set_candidates)
            log(f"Found {len(scraped_sets)} sets on cutco.com")
            if not scraped and not scraped_sets:
                raise RuntimeError(
                    "Cutco.com could not be reached or returned no catalog data."
                )
            preview = _build_catalog_sync_preview(scraped, scraped_sets)
            finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            results = {
                "scraped_total": preview["scraped_total"],
                "new_items": len(preview["new_items"]),
                "new_sets": len(preview["new_sets"]),
                "existing_sets": len(preview["existing_sets_data"]),
            }
            _write_catalog_sync_job(
                {
                    "status": "done",
                    "progress": progress,
                    "results": results,
                    "error": None,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "preview": preview,
                    "heartbeat_at": finished_at,
                }
            )
            record_activity(
                "catalog_sync",
                "Catalog sync complete",
                f"Scraped {preview['scraped_total']} items and {preview['scraped_sets_total']} sets.",
                occurred_at=finished_at,
            )
        except Exception as exc:
            logger.exception("Catalog sync failed")
            finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            _write_catalog_sync_job(
                {
                    "status": "error",
                    "progress": progress,
                    "results": None,
                    "error": str(exc),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "preview": None,
                    "heartbeat_at": finished_at,
                }
            )
            record_activity(
                "catalog_sync",
                "Catalog sync failed",
                str(exc),
                occurred_at=finished_at,
            )
            db.session.commit()


def _start_catalog_sync_background_job(app) -> threading.Thread:
    """Launch the catalog sync job in a background thread."""
    thread = threading.Thread(target=_run_catalog_sync_job, args=(app,), daemon=True)
    thread.start()
    return thread


def _safe_redirect_target(target: str | None) -> str | None:
    if not target:
        return None
    target = target.strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _delete_attachment_files(item: Item) -> None:
    """Remove stored attachment files for an item."""
    attachment_root = Path(current_app.config["ATTACHMENTS_DIR"]).expanduser()
    item_dir = attachment_root / str(item.id)
    for attachment in item.attachments:
        file_path = item_dir / attachment.stored_filename
        if file_path.exists():
            file_path.unlink()


def _item_alternate_skus_text(item: Item | None) -> str:
    if not item or not item.alternate_skus:
        return ""
    return ", ".join(parse_alternate_skus(item.alternate_skus))


def _normalize_member_sku(value: object) -> str | None:
    sku = str(value).strip().upper() if value is not None else ""
    return sku or None


def _normalize_member_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _coerce_quantity(value: object, default: int = 1) -> int:
    """Convert scraped quantity values to a positive integer."""
    if not isinstance(value, str | float | int):
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _get_item_field(item: object, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _build_member_name_lookup(items: list[object]) -> dict[str, object]:
    lookup: dict[str, object] = {}
    ambiguous: set[str] = set()
    for item in items:
        name = _normalize_member_name(_get_item_field(item, "name"))
        if not name:
            continue
        if name in ambiguous:
            continue
        if name in lookup:
            lookup.pop(name, None)
            ambiguous.add(name)
            continue
        lookup[name] = item
    return lookup


def _get_item_identity(item: object) -> tuple[str | None, str | None]:
    sku = _normalize_member_sku(_get_item_field(item, "sku"))
    name = _get_item_field(item, "name")
    return sku, _normalize_member_name(name)


def _resolve_member_item(
    entry: dict[str, Any],
    catalog_sku_lookup: dict[str, Item],
    catalog_name_lookup: dict[str, object],
    *,
    set_sku: str | None = None,
) -> object | None:
    sku = _normalize_member_sku(entry.get("sku"))
    name_key = _normalize_member_name(entry.get("name"))
    sku_item = catalog_sku_lookup.get(sku) if sku else None
    name_item = catalog_name_lookup.get(name_key) if name_key else None

    if sku_item and set_sku:
        item_sku, _ = _get_item_identity(sku_item)
        if item_sku and item_sku == set_sku and name_item is not None:
            sku_item = name_item
    if sku_item and name_item:
        sku_name = _normalize_member_name(_get_item_field(sku_item, "name"))
        name_sku, _ = _get_item_identity(name_item)
        if sku_name != name_key and name_sku != sku:
            sku_item = name_item
    return sku_item or name_item


def _catalog_category_options() -> list[str]:
    return [
        row[0]
        for row in db.session.query(Item.category)
        .filter(Item.category.isnot(None), Item.category != "")
        .distinct()
        .order_by(Item.category)
        .all()
    ]


def _set_name_options() -> list[str]:
    return [
        row[0]
        for row in db.session.query(Set.name)
        .filter(Set.name.isnot(None), Set.name != "")
        .distinct()
        .order_by(Set.name)
        .all()
    ]


def _set_sku_options() -> list[str]:
    return [
        row[0]
        for row in db.session.query(Set.sku)
        .filter(Set.sku.isnot(None), Set.sku != "")
        .distinct()
        .order_by(Set.sku)
        .all()
    ]


def _load_member_snapshot(raw_member_data: str | None) -> list[dict[str, Any]]:
    if not raw_member_data:
        return []
    try:
        payload = json.loads(raw_member_data)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("members") or payload.get("member_entries") or []
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        sku = _normalize_member_sku(entry.get("sku"))
        name = str(entry.get("name") or "").strip() or None
        try:
            quantity = max(1, int(entry.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        if sku:
            existing = seen.get(sku)
            if existing is None:
                seen[sku] = {"sku": sku, "name": name, "quantity": quantity}
            else:
                existing["quantity"] = (
                    max(1, int(existing.get("quantity") or 1)) + quantity
                )
                if not existing.get("name") and name:
                    existing["name"] = name
            continue
        rows.append({"sku": sku, "name": name, "quantity": quantity})
    rows[:0] = list(seen.values())
    return rows


def _build_member_status_rows(
    member_entries: list[dict[str, Any]],
    catalog_sku_lookup: dict[str, Item],
    catalog_name_lookup: dict[str, object] | None = None,
    *,
    set_sku: str | None = None,
    found_skus: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    missing_skus: list[str] = []
    found_skus = {sku for sku in (found_skus or set()) if sku}
    catalog_name_lookup = catalog_name_lookup or {}
    for index, entry in enumerate(member_entries, start=1):
        sku = _normalize_member_sku(entry.get("sku"))
        item = _resolve_member_item(
            entry, catalog_sku_lookup, catalog_name_lookup, set_sku=set_sku
        )
        quantity = _coerce_quantity(entry.get("quantity"))
        if item is not None:
            status = "present"
            status_label = "In catalog"
            resolution_note = "Will link to existing catalog item."
        elif sku and sku in found_skus:
            status = "found"
            status_label = "Found in scrape"
            resolution_note = "Will create a placeholder if that option is enabled."
        elif sku:
            status = "missing"
            status_label = "Missing from catalog"
            missing_skus.append(sku)
            resolution_note = "Will be skipped unless placeholder creation is enabled."
        else:
            status = "no_sku"
            status_label = "No item number"
            resolution_note = "Will be skipped."
        rows.append(
            {
                "index": index,
                "sku": sku,
                "name": entry.get("name") or None,
                "hover_title": _member_hover_title(
                    _get_item_field(item, "name")
                    if item is not None
                    else entry.get("name")
                ),
                "quantity": quantity,
                "status": status,
                "status_label": status_label,
                "resolution_note": resolution_note,
                "matched_item_id": _get_item_field(item, "id")
                if item is not None
                else None,
                "matched_item_name": _get_item_field(item, "name")
                if item is not None
                else None,
            }
        )
    rows.sort(key=_member_preview_sort_key)
    return rows, missing_skus


def _create_missing_set_member_item(member: dict[str, Any], set_name: str) -> Item:
    sku = _normalize_member_sku(member.get("sku"))
    if not sku:
        raise ValueError("Missing member SKU")
    name = str(member.get("name") or "").strip() or f"Set Member {sku}"
    item = Item(
        name=name,
        sku=sku,
        category=None,
        edge_type="Unknown",
        availability="non-catalog",
        in_catalog=False,
        set_only=True,
        cutco_url=None,
        msrp=None,
        blade_length=None,
        overall_length=None,
        weight=None,
        notes=f"Placeholder imported from set {set_name}.",
    )
    db.session.add(item)
    db.session.flush()
    reconcile_unknown_variant(item)
    return item


def _aggregate_resolved_members(
    member_entries: list[dict[str, Any]],
    sku_to_item: dict[str, Item],
    name_to_item: dict[str, object],
    *,
    set_sku: str | None = None,
    create_missing: bool = False,
    set_name: str | None = None,
) -> tuple[OrderedDict[int, ResolvedMember], int]:
    resolved_members: OrderedDict[int, ResolvedMember] = OrderedDict()
    created_missing_items = 0
    for member in member_entries:
        resolved_item = _resolve_member_item(
            member, sku_to_item, name_to_item, set_sku=set_sku
        )
        member_sku = _normalize_member_sku(
            getattr(resolved_item, "sku", None) if resolved_item else member.get("sku")
        )
        if not member_sku:
            continue
        item = sku_to_item.get(member_sku.upper())
        if resolved_item is not None and getattr(resolved_item, "sku", None):
            item = cast(Item, resolved_item)
        if not item and create_missing and set_name:
            item = _create_missing_set_member_item(member, set_name)
            sku_to_item[item.sku.upper()] = item
            name_to_item[_normalize_member_name(item.name)] = item
            created_missing_items += 1
        if not item:
            continue
        quantity = _coerce_quantity(member.get("quantity"))
        aggregated = resolved_members.get(item.id)
        if aggregated is None:
            resolved_members[item.id] = {"item": item, "quantity": quantity}
        else:
            aggregated["quantity"] = (
                max(1, int(aggregated.get("quantity") or 1)) + quantity
            )
    return resolved_members, created_missing_items


def _member_preview_key(sku: str | None, name: str | None) -> str | None:
    """Return a stable key for comparing set members."""
    normalized_name = _normalize_member_name(name)
    if normalized_name:
        return f"name:{normalized_name}"
    normalized_sku = _normalize_member_sku(sku)
    if normalized_sku:
        return f"sku:{normalized_sku}"
    return None


def _member_preview_name(name: str | None, quantity: int | None) -> str | None:
    """Return a display name with redundant trailing quantity removed."""
    normalized_name = str(name or "").strip() or None
    if not normalized_name or quantity is None:
        return normalized_name
    qty_value = max(1, int(quantity))
    qty_text = str(qty_value)
    qty_words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
        11: "eleven",
        12: "twelve",
        13: "thirteen",
        14: "fourteen",
        15: "fifteen",
        16: "sixteen",
        17: "seventeen",
        18: "eighteen",
        19: "nineteen",
        20: "twenty",
    }
    suffixes = [f" ({qty_text})", f" {qty_text}"]
    word_form = qty_words.get(qty_value)
    if word_form:
        suffixes.extend([f" ({word_form})", f" {word_form}"])
    for suffix in suffixes:
        if normalized_name.endswith(suffix):
            stripped = normalized_name[: -len(suffix)].rstrip()
            return stripped or normalized_name
    return normalized_name


def _member_preview_sort_key(member: dict[str, object]) -> tuple[int, int, str]:
    """Sort set members by numeric SKU, then by SKU text, then by name."""
    sku = str(member.get("sku") or "")
    sku_match = re.match(r"^(\d+)", sku)
    if sku_match:
        return (0, int(sku_match.group(1)), sku)
    name = str(member.get("name") or "")
    return (1, 0, sku or name)


def _aggregate_member_preview_rows(
    member_entries: list[dict[str, Any]],
) -> OrderedDict[str, dict[str, object]]:
    """Aggregate member rows by stable preview key."""
    rows: OrderedDict[str, dict[str, object]] = OrderedDict()
    for entry in member_entries:
        sku = _normalize_member_sku(entry.get("sku"))
        quantity = _coerce_quantity(entry.get("quantity"))
        name = _member_preview_name(entry.get("name"), quantity)
        key = _member_preview_key(sku, name)
        if not key:
            continue
        current = rows.get(key)
        if current is None:
            rows[key] = {
                "sku": sku,
                "name": name,
                "quantity": quantity,
            }
        else:
            current["quantity"] = _coerce_quantity(current.get("quantity")) + quantity
            if not current.get("sku") and sku:
                current["sku"] = sku
            if not current.get("name") and name:
                current["name"] = name
    return rows


def _build_set_membership_preview(
    item_set: Set,
    member_entries: list[dict[str, Any]],
    catalog_sku_lookup: dict[str, Item] | None = None,
    catalog_name_lookup: dict[str, object] | None = None,
) -> dict:
    """Build a before/after snapshot for an existing set."""
    catalog_sku_lookup = catalog_sku_lookup or {}
    catalog_name_lookup = catalog_name_lookup or {}
    set_sku = _normalize_member_sku(item_set.sku)
    current_rows = OrderedDict()
    current_compare_rows = OrderedDict()
    for membership in item_set.members:
        item = membership.item
        sku = _normalize_member_sku(item.sku if item else None)
        name = _member_preview_name(item.name if item else None, membership.quantity)
        key = _member_preview_key(sku, name)
        compare_key = f"item:{item.id}" if item is not None else key
        if not key:
            continue
        current_rows[key] = {
            "item_id": item.id if item is not None else None,
            "sku": sku,
            "display_sku": sku,
            "name": name,
            "quantity": max(1, int(membership.quantity or 1)),
        }
        current_compare_rows[compare_key] = current_rows[key]

    incoming_rows = _aggregate_member_preview_rows(member_entries)
    incoming_compare_rows = OrderedDict()
    current_rows_list = sorted(current_rows.values(), key=_member_preview_sort_key)
    incoming_rows_list = sorted(incoming_rows.values(), key=_member_preview_sort_key)
    incoming_row_notes: dict[str, str] = {}
    incoming_row_notes_by_compare: dict[str, str] = {}
    for key, incoming in incoming_rows.items():
        resolved_item = _resolve_member_item(
            incoming,
            catalog_sku_lookup,
            catalog_name_lookup,
            set_sku=set_sku,
        )
        matched_item = cast(Item, resolved_item) if resolved_item is not None else None
        compare_key = f"item:{matched_item.id}" if matched_item is not None else key
        incoming["item_id"] = matched_item.id if matched_item is not None else None
        incoming["source_sku"] = incoming.get("sku")
        incoming["display_sku"] = _normalize_member_sku(
            getattr(matched_item, "sku", None)
        ) or incoming.get("sku")
        incoming_compare_rows[compare_key] = incoming
        if resolved_item is not None:
            incoming_row_notes[key] = "Will link to existing catalog item."
        elif incoming.get("sku"):
            incoming_row_notes[key] = (
                "Will create a placeholder if that option is enabled."
            )
        else:
            incoming_row_notes[key] = "Will be skipped."
        incoming_row_notes_by_compare[compare_key] = incoming_row_notes[key]
    for row in incoming_rows_list:
        row_sku = _normalize_member_sku(row.get("sku"))
        row_name = str(row.get("name")) if row.get("name") else None
        row_key = _member_preview_key(row_sku, row_name)
        if row_key:
            row["resolution_note"] = incoming_row_notes.get(row_key)

    change_rows: list[dict[str, object]] = []
    added = 0
    removed = 0
    quantity_changed = 0

    for key, incoming in incoming_compare_rows.items():
        current = current_compare_rows.get(key)
        if current is None:
            added += 1
            change_rows.append(
                {
                    "action": "added",
                    "sku": incoming.get("display_sku") or incoming.get("sku") or "—",
                    "name": incoming.get("name") or "—",
                    "current_quantity": None,
                    "incoming_quantity": incoming.get("quantity"),
                    "resolution_note": incoming_row_notes_by_compare.get(key),
                    "source_sku": incoming.get("source_sku"),
                }
            )
            continue
        current_qty = int(current.get("quantity") or 1)
        incoming_qty = int(incoming.get("quantity") or 1)
        if current_qty != incoming_qty:
            quantity_changed += 1
            change_rows.append(
                {
                    "action": "quantity",
                    "sku": incoming.get("display_sku")
                    or incoming.get("sku")
                    or current.get("sku")
                    or "—",
                    "name": incoming.get("name") or current.get("name") or "—",
                    "current_quantity": current_qty,
                    "incoming_quantity": incoming_qty,
                    "resolution_note": "Will update the quantity on the linked item.",
                    "source_sku": incoming.get("source_sku"),
                }
            )

    for key, current in current_compare_rows.items():
        if key in incoming_compare_rows:
            continue
        removed += 1
        change_rows.append(
            {
                "action": "removed",
                "sku": current.get("display_sku") or current.get("sku") or "—",
                "name": current.get("name") or "—",
                "current_quantity": current.get("quantity"),
                "incoming_quantity": None,
                "resolution_note": "Will be removed from the set.",
            }
        )

    summary_parts = []
    if added:
        summary_parts.append(f"{added} added")
    if removed:
        summary_parts.append(f"{removed} removed")
    if quantity_changed:
        summary_parts.append(f"{quantity_changed} quantity changed")

    return {
        "has_changes": bool(change_rows),
        "summary": ", ".join(summary_parts)
        if summary_parts
        else "No membership changes detected.",
        "current_rows": current_rows_list,
        "incoming_rows": incoming_rows_list,
        "change_rows": change_rows,
        "added": added,
        "removed": removed,
        "quantity_changed": quantity_changed,
        "current_count": len(current_rows),
        "incoming_count": len(incoming_rows),
    }

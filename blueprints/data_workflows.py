"""Shared workflows for import, completion, and variant sync routes."""

import logging
import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import flash
from sqlalchemy.orm import selectinload

from blueprints.import_shared import (
    _build_item_name_lookup,
    _build_item_sku_lookup,
    _build_import_header_report,  # noqa: F401
    _build_set_sku_lookup,
    _import_row_label,  # noqa: F401
    _normalize_variant_lookup_name,
    _read_completion_rows,  # noqa: F401
    _safe_csv_filename,  # noqa: F401
)
from constants import (
    UNKNOWN_COLOR,
    VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES,
)
from extensions import db
from models import (
    Item,
    ItemSetMember,
    ItemVariant,
    Ownership,
    Person,
    Set,
    normalize_sku_value,
)
from number_utils import parse_nonnegative_whole_number, parse_positive_whole_number
from scraping import (
    discover_cutco_item_page_url,
    scrape_item_variant_colors,
    scrape_purple_campaign_variants,
)

logger = logging.getLogger(__name__)


def sync_variant_sync_helpers(
    scrape_item_variant_colors_fn, scrape_purple_campaign_variants_fn
) -> None:
    """Keep the workflow helpers pointed at the patchable route-level scrapers."""
    global scrape_item_variant_colors, scrape_purple_campaign_variants
    scrape_item_variant_colors = scrape_item_variant_colors_fn
    scrape_purple_campaign_variants = scrape_purple_campaign_variants_fn


def _parse_quantity_fields(row: dict) -> tuple[int | None, int | None, list[str]]:
    """Parse ownership quantity fields as whole numbers."""
    errors: list[str] = []
    quantity_purchased = None
    quantity_given_away = None
    for key, label in [
        ("quantity_purchased", "Quantity Purchased"),
        ("quantity_given_away", "Quantity Given Away"),
    ]:
        value = row.get(key, "").strip()
        if not value or value.lower() in {"0", "none", "n/a", "-"}:
            continue
        parsed_value, error = parse_nonnegative_whole_number(value, label)
        if error:
            errors.append(error)
            continue
        if parsed_value is not None:
            if key == "quantity_purchased":
                quantity_purchased = parsed_value
            else:
                quantity_given_away = parsed_value
    return quantity_purchased, quantity_given_away, errors


def _build_completion_preview(
    parsed_rows: list[dict],
    *,
    person_override: str | None = None,
) -> dict:
    """Resolve completion rows into expanded, rolled-up, and unresolved preview data."""
    existing_items = _build_item_sku_lookup(
        Item.query.options(selectinload(Item.variants)).all()
    )
    existing_sets = _build_set_sku_lookup(
        Set.query.options(
            selectinload(Set.members).selectinload(ItemSetMember.item)
        ).all()
    )
    existing_persons = {person.name.lower(): person for person in Person.query.all()}
    existing_ownerships = {}
    for ownership in Ownership.query.options(
        selectinload(Ownership.person),
        selectinload(Ownership.variant).selectinload(ItemVariant.item),
    ).all():
        existing_ownerships[
            (
                ownership.person.name.lower(),
                ownership.variant.item_id,
                ownership.variant.color.lower(),
            )
        ] = ownership

    unresolved_rows: list[dict] = []
    expanded_rows: list[dict] = []
    set_rows_expanded = 0

    for row in parsed_rows:
        row_num = row.get("row_num")
        person = (person_override or row.get("person", "") or "").strip()
        sku = normalize_sku_value(row.get("sku", ""))
        note = row.get("note", "").strip() or None
        quantity, qty_error = parse_positive_whole_number(row.get("quantity", ""))

        if qty_error:
            unresolved_rows.append(
                {
                    "row": row_num,
                    "person": person or "—",
                    "sku": sku or "—",
                    "quantity": row.get("quantity", "") or "—",
                    "note": note or "—",
                    "reason": qty_error,
                }
            )
            continue
        if quantity is None:
            unresolved_rows.append(
                {
                    "row": row_num,
                    "person": person or "—",
                    "sku": sku or "—",
                    "quantity": "—",
                    "note": note or "—",
                    "reason": "Quantity is required.",
                }
            )
            continue
        if not person:
            unresolved_rows.append(
                {
                    "row": row_num,
                    "person": "—",
                    "sku": sku or "—",
                    "quantity": quantity,
                    "note": note or "—",
                    "reason": "Missing person.",
                }
            )
            continue
        if not sku:
            unresolved_rows.append(
                {
                    "row": row_num,
                    "person": person,
                    "sku": "—",
                    "quantity": quantity,
                    "note": note or "—",
                    "reason": "Missing sku.",
                }
            )
            continue

        matched_set = existing_sets.get(sku)
        matched_item = None if matched_set else existing_items.get(sku)

        if matched_set:
            set_rows_expanded += 1
            if not matched_set.members:
                unresolved_rows.append(
                    {
                        "row": row_num,
                        "person": person,
                        "sku": sku,
                        "quantity": quantity,
                        "note": note or "—",
                        "reason": "Set SKU not found in member data.",
                    }
                )
                continue
            for membership in matched_set.members:
                member_item = membership.item
                if not member_item or not member_item.sku:
                    unresolved_rows.append(
                        {
                            "row": row_num,
                            "person": person,
                            "sku": sku,
                            "quantity": quantity,
                            "note": note or "—",
                            "reason": f"Set member missing from catalog: {member_item.name if member_item else 'unknown item'}.",
                        }
                    )
                    continue
                member_qty = quantity * (membership.quantity or 1)
                expanded_rows.append(
                    {
                        "input_row": row_num,
                        "person": person,
                        "person_key": person.lower(),
                        "original_sku": sku,
                        "resolved_sku": member_item.sku,
                        "item_id": member_item.id,
                        "item_name": member_item.name,
                        "color": UNKNOWN_COLOR,
                        "display_color": "—",
                        "quantity": member_qty,
                        "source": "Set member",
                        "note": note,
                        "source_rows": [row_num],
                    }
                )
            continue

        if matched_item:
            expanded_rows.append(
                {
                    "input_row": row_num,
                    "person": person,
                    "person_key": person.lower(),
                    "original_sku": sku,
                    "resolved_sku": matched_item.sku or sku,
                    "item_id": matched_item.id,
                    "item_name": matched_item.name,
                    "color": UNKNOWN_COLOR,
                    "display_color": "—",
                    "quantity": quantity,
                    "source": "Item",
                    "note": note,
                    "source_rows": [row_num],
                }
            )
            continue

        unresolved_rows.append(
            {
                "row": row_num,
                "person": person,
                "sku": sku,
                "quantity": quantity,
                "note": note or "—",
                "reason": (
                    "Item SKU not found."
                    if sku not in existing_sets
                    else "Set SKU not found."
                ),
            }
        )

    rolled_map: dict[tuple[str, int, str], dict] = {}
    for expanded in expanded_rows:
        key = (expanded["person_key"], expanded["item_id"], expanded["color"].lower())
        bucket = rolled_map.setdefault(
            key,
            {
                "person": expanded["person"],
                "person_key": expanded["person_key"],
                "item_id": expanded["item_id"],
                "item_name": expanded["item_name"],
                "sku": expanded["resolved_sku"],
                "color": expanded["color"],
                "display_color": expanded["display_color"],
                "quantity": 0,
                "notes": [],
                "source_rows": [],
                "source_count": 0,
                "action": "",
                "is_new_variant": False,
            },
        )
        bucket["quantity"] += expanded["quantity"]
        bucket["source_count"] += 1
        bucket["source_rows"].extend(expanded["source_rows"])
        if expanded["note"]:
            bucket["notes"].append(expanded["note"])

    rolled_rows: list[dict] = []
    new_ownership_rows = 0
    updated_ownership_rows = 0
    for bucket in rolled_map.values():
        item = db.session.get(Item, bucket["item_id"])
        if not item:
            unresolved_rows.append(
                {
                    "row": (
                        min(bucket["source_rows"]) if bucket["source_rows"] else None
                    ),
                    "person": bucket["person"],
                    "sku": bucket["sku"],
                    "quantity": bucket["quantity"],
                    "note": "—",
                    "reason": "Matched item vanished before preview.",
                }
            )
            continue
        target_color = (
            UNKNOWN_COLOR
            if (item.category or "") in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES
            else bucket["color"]
        )
        variant = next(
            (
                variant
                for variant in item.variants
                if variant.color.lower() == target_color.lower()
            ),
            None,
        )
        person_obj = existing_persons.get(bucket["person_key"])
        existing_o = None
        if person_obj and variant:
            existing_o = existing_ownerships.get(
                (bucket["person_key"], item.id, variant.color.lower())
            )
        bucket["display_color"] = "—" if target_color == UNKNOWN_COLOR else target_color
        bucket["action"] = "Update ownership" if existing_o else "Create ownership"
        bucket["is_new_variant"] = variant is None
        bucket["notes_text"] = "; ".join(dict.fromkeys(bucket["notes"])) or None
        if existing_o:
            updated_ownership_rows += 1
        else:
            new_ownership_rows += 1
        rolled_rows.append(bucket)

    merged_rows = [row for row in rolled_rows if row["source_count"] > 1]

    return {
        "unresolved_rows": unresolved_rows,
        "expanded_rows": expanded_rows,
        "rolled_rows": rolled_rows,
        "merged_rows": merged_rows,
        "summary": {
            "rows_received": len(parsed_rows),
            "rows_resolved": len(expanded_rows),
            "set_rows_expanded": set_rows_expanded,
            "duplicate_rows_merged": max(len(expanded_rows) - len(rolled_rows), 0),
            "new_ownership_rows": new_ownership_rows,
            "existing_ownership_rows_updated": updated_ownership_rows,
            "unresolved_skus": len(unresolved_rows),
        },
    }


def _build_completion_missing_rows(person_names: list[str]) -> list[dict]:
    """Build per-person missing public catalog item rows for completion reporting."""
    if not person_names:
        return []

    target_items = (
        Item.query.filter_by(set_only=False, in_catalog=True)
        .order_by(Item.category, Item.sku, Item.name)
        .all()
    )
    people = {
        person.name.lower(): person
        for person in Person.query.filter(Person.name.in_(person_names)).all()
    }

    missing_rows: list[dict] = []
    for person_name in sorted(
        {name.strip() for name in person_names if name.strip()}, key=str.lower
    ):
        person = people.get(person_name.lower())
        if not person:
            continue
        owned_item_ids = set(
            db.session.execute(
                db.select(ItemVariant.item_id)
                .join(Ownership, Ownership.variant_id == ItemVariant.id)
                .where(
                    Ownership.person_id == person.id,
                    Ownership.status == "Owned",
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        for item in target_items:
            if item.id in owned_item_ids:
                continue
            missing_rows.append(
                {
                    "person": person.name,
                    "missing_sku": item.sku,
                    "item": item.name,
                    "category": item.category or "—",
                }
            )

    return missing_rows


def _build_completion_missing_csv(missing_rows: list[dict]) -> str:
    """Serialize completion gaps rows to CSV text."""
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["person", "missing_sku", "item", "category"])
    for row in missing_rows:
        writer.writerow(
            [
                row["person"],
                row["missing_sku"],
                row["item"],
                row["category"],
            ]
        )
    return csv_buffer.getvalue()


def _parse_variant_sync_selected_skus(raw_value: str) -> list[str]:
    """Parse pasted SKU text for the variant sync scope picker."""
    seen: set[str] = set()
    skus: list[str] = []
    for part in re.split(r"[\n,;]+", raw_value or ""):
        sku = normalize_sku_value(part)
        if not sku or sku in seen:
            continue
        seen.add(sku)
        skus.append(sku)
    return skus


def _resolve_variant_sync_items(
    scope: str, category: str | None, selected_skus: list[str]
) -> tuple[list[Item], str | None]:
    """Resolve the item set to scan for variant sync."""
    items = (
        Item.query.options(selectinload(Item.variants))
        .filter(Item.cutco_url.isnot(None) | Item.set_only.is_(True))
        .all()
    )
    if scope == "all":
        return (
            sorted(
                items,
                key=lambda item: (
                    (item.category or "").lower(),
                    (item.sku or "").lower(),
                    (item.name or "").lower(),
                ),
            ),
            None,
        )

    if scope == "category":
        if not category:
            return [], "Please choose a category."
        filtered = [item for item in items if (item.category or "") == category]
        if not filtered:
            return [], f'No variant-sync eligible items were found in "{category}".'
        return (
            sorted(
                filtered,
                key=lambda item: ((item.sku or "").lower(), (item.name or "").lower()),
            ),
            None,
        )

    if scope == "selected":
        if not selected_skus:
            return [], "Please enter one or more item SKUs."
        lookup = _build_item_sku_lookup(items)
        selected_items: list[Item] = []
        selected_ids: set[int] = set()
        missing: list[str] = []
        for sku in selected_skus:
            item = lookup.get(sku)
            if not item:
                missing.append(sku)
                continue
            if item.id in selected_ids:
                continue
            selected_ids.add(item.id)
            selected_items.append(item)
        selected_items.sort(
            key=lambda item: (
                (item.category or "").lower(),
                (item.sku or "").lower(),
                (item.name or "").lower(),
            )
        )
        if missing and selected_items:
            flash(
                f"Some selected SKUs were not found and were skipped: {', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}",
                "warning",
            )
        if not selected_items:
            return [], "None of the selected SKUs matched a variant-sync eligible item."
        return selected_items, None

    return [], "Please choose a valid scan scope."


def _variant_sync_candidate_urls(item: Item) -> list[str]:
    """Return product URLs to try for a variant-sync item, in priority order."""
    candidates: list[str] = []
    if item.set_only:
        sku = normalize_sku_value(item.sku or "")
        name_slug = re.sub(r"[^a-z0-9]+", "-", (item.name or "").lower()).strip("-")
        if sku and name_slug:
            candidates.append(f"https://www.cutco.com/p/{name_slug}/{sku}&view=product")
    if item.cutco_url:
        candidates.append(item.cutco_url)
    if item.set_only:
        sku = normalize_sku_value(item.sku or "")
        if sku:
            candidates.extend(
                (
                    f"https://www.cutco.com/p/{sku}&view=product",
                    f"https://www.cutco.com/p/{sku}",
                )
            )
    return list(dict.fromkeys(candidates))


def _scrape_variant_sync_item(
    item: Item, discovered_url: str | None = None
) -> tuple[tuple[str, ...], str | None]:
    """Try an item's viable product URLs until one exposes clear variants."""
    for url in _variant_sync_candidate_urls(item):
        colors = scrape_item_variant_colors(url)
        if colors:
            return colors, url
    if item.set_only and discovered_url:
        colors = scrape_item_variant_colors(discovered_url)
        if colors:
            return colors, discovered_url
    return (), None


def _build_variant_sync_preview(items: list[Item]) -> dict:
    """Scrape variant colors for a set of items and build preview rows."""
    preview_items: list[dict] = []
    scanned_items = 0
    scraped_variant_total = 0
    variants_to_create = 0
    variants_retained = 0
    items_with_no_clear_variants = 0
    purple_variant_count = 0

    fetched_variants: dict[int, tuple[str, ...]] = {}
    fetched_urls: dict[int, str] = {}
    discovered_urls = {
        item.id: discover_cutco_item_page_url(item.sku)
        for item in items
        if item.set_only
        and (item.category or "") not in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_map = {
            pool.submit(
                _scrape_variant_sync_item,
                item,
                discovered_urls.get(item.id),
            ): item.id
            for item in items
            if _variant_sync_candidate_urls(item)
            and (item.category or "") not in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES
        }
        for future in as_completed(future_map):
            item_id = future_map[future]
            colors, scraped_url = future.result()
            fetched_variants[item_id] = colors
            if scraped_url:
                fetched_urls[item_id] = scraped_url

    for item in items:
        scanned_items += 1
        if not _variant_sync_candidate_urls(item):
            preview_items.append(
                {
                    "item_id": item.id,
                    "item_name": item.name,
                    "sku": item.sku or "—",
                    "category": item.category or "—",
                    "status": "skipped",
                    "skip_reason": "No Cutco URL stored.",
                    "variant_rows": [],
                    "create_colors": [],
                    "retained_colors": [],
                    "existing_count": 0,
                    "create_count": 0,
                    "retained_count": 0,
                    "has_unknown_variant": any(
                        variant.color == UNKNOWN_COLOR for variant in item.variants
                    ),
                    "no_clear_variants": True,
                    "has_purple_variant": False,
                }
            )
            items_with_no_clear_variants += 1
            continue
        if (item.category or "") in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES:
            skip_reason = "These items use a single fallback variant."
            if (item.category or "") == "Cutting Boards":
                skip_reason = (
                    "Cutting board items are treated as a single fallback variant."
                )
            preview_items.append(
                {
                    "item_id": item.id,
                    "item_name": item.name,
                    "sku": item.sku or "—",
                    "category": item.category or "—",
                    "status": "skipped",
                    "skip_reason": skip_reason,
                    "variant_rows": [],
                    "create_colors": [],
                    "retained_colors": [],
                    "existing_count": 0,
                    "create_count": 0,
                    "retained_count": 0,
                    "has_unknown_variant": any(
                        variant.color == UNKNOWN_COLOR for variant in item.variants
                    ),
                    "no_clear_variants": True,
                    "has_purple_variant": False,
                }
            )
            items_with_no_clear_variants += 1
            continue

        scraped_colors = list(fetched_variants.get(item.id, ()))
        scraped_variant_total += len(scraped_colors)
        has_purple_variant = any(color.lower() == "purple" for color in scraped_colors)
        if has_purple_variant:
            purple_variant_count += 1
        existing_real_variants = [
            variant for variant in item.variants if variant.color != UNKNOWN_COLOR
        ]
        existing_lookup = {
            variant.color.lower(): variant for variant in existing_real_variants
        }
        scraped_lookup = {color.lower() for color in scraped_colors}

        variant_rows: list[dict] = []
        create_colors: list[str] = []
        retained_colors: list[str] = []
        existing_count = 0
        create_count = 0

        for color in scraped_colors:
            status = "existing" if color.lower() in existing_lookup else "create"
            if status == "existing":
                existing_count += 1
            else:
                create_count += 1
                create_colors.append(color)
            variant_rows.append({"color": color, "status": status})

        for variant in existing_real_variants:
            if variant.color.lower() in scraped_lookup:
                continue
            retained_colors.append(variant.color)
            variant_rows.append({"color": variant.color, "status": "not seen in sync"})

        retained_count = len(retained_colors)
        variants_to_create += create_count
        variants_retained += retained_count
        has_unknown_variant = any(
            variant.color == UNKNOWN_COLOR for variant in item.variants
        )
        no_clear_variants = not scraped_colors
        if no_clear_variants:
            items_with_no_clear_variants += 1
        variant_rows.sort(key=lambda row: (row["color"].lower(), row["status"]))
        preview_items.append(
            {
                "item_id": item.id,
                "item_name": item.name,
                "sku": item.sku or "—",
                "category": item.category or "—",
                "status": "ready" if scraped_colors else "skipped",
                "skip_reason": (
                    None if scraped_colors else "No clear color variants were detected."
                ),
                "variant_rows": variant_rows,
                "create_colors": create_colors,
                "retained_colors": retained_colors,
                "existing_count": existing_count,
                "create_count": create_count,
                "retained_count": retained_count,
                "has_unknown_variant": has_unknown_variant,
                "no_clear_variants": no_clear_variants,
                "has_purple_variant": has_purple_variant,
                "scraped_url": fetched_urls.get(item.id),
                "scraped_variant_count": len(scraped_colors),
                "swatch_count": len(scraped_colors),
            }
        )

    grouped_items: dict[str, list[dict]] = {}
    for item in preview_items:
        grouped_items.setdefault(item["category"], []).append(item)
    grouped_preview = [
        {
            "category": category,
            "items": sorted(
                category_items,
                key=lambda item: (
                    (item["sku"] or "").lower(),
                    (item["item_name"] or "").lower(),
                ),
            ),
        }
        for category, category_items in sorted(
            grouped_items.items(), key=lambda kv: kv[0].lower()
        )
    ]

    return {
        "items": preview_items,
        "grouped_items": grouped_preview,
        "summary": {
            "items_scanned": scanned_items,
            "variants_found": scraped_variant_total,
            "variants_to_create": variants_to_create,
            "variants_retained": variants_retained,
            "items_with_no_clear_variants": items_with_no_clear_variants,
            "purple_variant_count": purple_variant_count,
            "has_purple_variants": purple_variant_count > 0,
        },
    }


def _build_purple_campaign_variant_preview() -> dict:
    """Scrape the purple campaign page and map promo variants to catalog items."""
    promo_entries = list(scrape_purple_campaign_variants())
    suppressed_promo_names = {"gift set", "package"}
    if not promo_entries:
        return {
            "items": [],
            "summary": {
                "items_scanned": 0,
                "variants_found": 0,
                "variants_to_create": 0,
                "variants_retained": 0,
                "items_with_no_clear_variants": 0,
                "purple_variant_count": 0,
                "has_purple_variants": False,
            },
        }

    catalog_items = (
        Item.query.options(selectinload(Item.variants))
        .filter(Item.sku.isnot(None))
        .all()
    )
    sku_lookup = _build_item_sku_lookup(catalog_items)
    name_lookup = _build_item_name_lookup(catalog_items)

    preview_items: list[dict] = []
    items_scanned = 0
    variants_found = 0
    variants_to_create = 0
    variants_retained = 0
    grouped_items: dict[int, dict] = {}

    def add_preview_target(item: Item, entry: dict[str, str]) -> None:
        nonlocal variants_to_create, variants_retained
        group = grouped_items.setdefault(
            item.id,
            {
                "item_id": item.id,
                "item_name": item.name,
                "sku": item.sku or entry.get("sku_hint") or "—",
                "category": item.category or "—",
                "status": "ready",
                "skip_reason": None,
                "variant_rows": [],
                "create_colors": [],
                "retained_colors": [],
                "existing_count": 0,
                "create_count": 0,
                "retained_count": 0,
                "has_unknown_variant": any(
                    variant.color == UNKNOWN_COLOR for variant in item.variants
                ),
                "no_clear_variants": False,
                "has_purple_variant": True,
                "scraped_variant_count": 0,
                "swatch_count": 0,
                "promo_codes": [],
                "source_label": "Purple Products",
            },
        )
        group["promo_codes"].append(entry.get("promo_code"))
        existing_colors = {variant.color.lower() for variant in item.variants}
        color_name = "Purple"
        color_key = color_name.lower()
        if color_key not in {row["color"].lower() for row in group["variant_rows"]}:
            status = "existing" if color_key in existing_colors else "create"
            group["variant_rows"].append(
                {
                    "color": color_name,
                    "status": status,
                    "promo_code": entry.get("promo_code"),
                }
            )
            group["scraped_variant_count"] += 1
            group["swatch_count"] += 1
            if status == "existing":
                variants_retained += 1
                group["retained_colors"].append(color_name)
            else:
                variants_to_create += 1
                group["create_colors"].append(color_name)
        group["create_count"] = len(group["create_colors"])
        group["retained_count"] = len(group["retained_colors"])
        group["existing_count"] = len(group["retained_colors"])

    for entry in promo_entries:
        items_scanned += 1
        promo_name = entry.get("name") or "Purple Promo Item"
        promo_name_key = _normalize_variant_lookup_name(promo_name)
        sku_hint = normalize_sku_value(entry.get("sku_hint"))
        if promo_name_key in suppressed_promo_names:
            preview_items.append(
                {
                    "item_id": None,
                    "item_name": promo_name,
                    "sku": sku_hint or "—",
                    "category": "Purple Promo",
                    "status": "skipped",
                    "skip_reason": "Suppressed because this is a campaign bundle item, not a standalone catalog product.",
                    "variant_rows": [],
                    "create_colors": [],
                    "retained_colors": [],
                    "existing_count": 0,
                    "create_count": 0,
                    "retained_count": 0,
                    "has_unknown_variant": False,
                    "no_clear_variants": True,
                    "has_purple_variant": True,
                    "promo_code": entry.get("promo_code"),
                    "source_label": "Purple Products",
                }
            )
            continue

        base_name = re.sub(r"^purple\s+", "", promo_name, flags=re.I).strip()
        base_name_key = _normalize_variant_lookup_name(base_name)
        knife_item = sku_lookup.get(sku_hint) if sku_hint else None
        if not knife_item:
            knife_item = name_lookup.get(base_name_key) or name_lookup.get(
                promo_name_key
            )

        resolved_items: list[Item] = []
        if knife_item:
            resolved_items.append(knife_item)

        if "sheath" in promo_name_key:
            sheath_base = re.sub(
                r"\s+with\s+sheath\s*$", "", base_name, flags=re.I
            ).strip()
            sheath_candidates = (
                f"{sheath_base} Sheath",
                f"{sheath_base} Knife Sheath",
            )
            sheath_item = None
            for candidate in sheath_candidates:
                sheath_item = name_lookup.get(_normalize_variant_lookup_name(candidate))
                if sheath_item:
                    break
            if sheath_item and (not knife_item or sheath_item.id != knife_item.id):
                resolved_items.append(sheath_item)

        if not resolved_items:
            preview_items.append(
                {
                    "item_id": None,
                    "item_name": promo_name,
                    "sku": sku_hint or "—",
                    "category": "Purple Promo",
                    "status": "skipped",
                    "skip_reason": f"No matching catalog item was found for purple promo code {entry.get('promo_code', '—')}.",
                    "variant_rows": [],
                    "create_colors": [],
                    "retained_colors": [],
                    "existing_count": 0,
                    "create_count": 0,
                    "retained_count": 0,
                    "has_unknown_variant": False,
                    "no_clear_variants": True,
                    "has_purple_variant": True,
                    "promo_code": entry.get("promo_code"),
                    "source_label": "Purple Products",
                }
            )
            continue

        for item in resolved_items:
            add_preview_target(item, entry)

    for group in grouped_items.values():
        variants_found += 1
        group["promo_code"] = (
            ", ".join([code for code in group.pop("promo_codes", []) if code]) or "—"
        )
        preview_items.append(group)

    preview_items.sort(
        key=lambda item: (
            (item["sku"] or "").lower(),
            (item["item_name"] or "").lower(),
        )
    )
    return {
        "items": preview_items,
        "summary": {
            "items_scanned": items_scanned,
            "variants_found": variants_found,
            "variants_to_create": variants_to_create,
            "variants_retained": variants_retained,
            "items_with_no_clear_variants": 0,
            "purple_variant_count": variants_found,
            "has_purple_variants": bool(variants_found),
        },
    }


def _resolve_completion_gap_people(
    selected_person_id: str, people: list[Person]
) -> tuple[list[Person], str | int, str | None]:
    """Resolve a completion gaps collector selection."""
    if selected_person_id == "all":
        return people, "all", None
    try:
        person_id = int(selected_person_id)
    except ValueError:
        return [], "all", "Please choose a valid collector."
    person = db.session.get(Person, person_id)
    if not person:
        return [], "all", "Please choose a valid collector."
    return [person], person.id, None


def _read_confirm_quantity_field(
    raw_value: str, label: str
) -> tuple[int | None, str | None]:
    """Parse a posted ownership quantity field."""
    cleaned = (raw_value or "").strip()
    if not cleaned or cleaned.lower() in {"0", "none", "n/a", "-"}:
        return None, None
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned), None
    return None, f"{label} must be a whole number."


def _apply_import_ownership_copy_details(
    ownership: Ownership,
    *,
    copy_type: str | None = None,
    engraving_text: str | None = None,
    engraving_notes: str | None = None,
    engraving_signature: str | None = None,
) -> None:
    """Apply copy-level metadata to an ownership row."""
    if copy_type is not None:
        ownership.copy_type = copy_type
    if engraving_text is not None:
        ownership.engraving_text = engraving_text
    if engraving_notes is not None:
        ownership.engraving_notes = engraving_notes
    if engraving_signature is not None:
        ownership.engraving_signature = engraving_signature


def _merge_import_ownership(
    ownership: Ownership,
    *,
    status: str,
    notes: str | None = None,
    quantity_purchased: int | None = None,
    quantity_given_away: int | None = None,
    copy_type: str | None = None,
    engraving_text: str | None = None,
    engraving_notes: str | None = None,
    engraving_signature: str | None = None,
) -> None:
    """Update an existing ownership row from an import row."""
    ownership.status = status
    if notes is not None:
        ownership.notes = notes
    if quantity_purchased is not None:
        ownership.quantity_purchased = quantity_purchased
    if quantity_given_away is not None:
        ownership.quantity_given_away = quantity_given_away
    _apply_import_ownership_copy_details(
        ownership,
        copy_type=copy_type,
        engraving_text=engraving_text,
        engraving_notes=engraving_notes,
        engraving_signature=engraving_signature,
    )


def _add_import_ownership_quantities(
    ownership: Ownership,
    *,
    status: str,
    notes: str | None = None,
    quantity_purchased: int | None = None,
    quantity_given_away: int | None = None,
    copy_type: str | None = None,
    engraving_text: str | None = None,
    engraving_notes: str | None = None,
    engraving_signature: str | None = None,
) -> None:
    """Add imported quantities onto an existing ownership row."""
    ownership.status = status
    if notes is not None:
        ownership.notes = notes
    if quantity_purchased is not None:
        ownership.quantity_purchased = (
            ownership.quantity_purchased or 0
        ) + quantity_purchased
    if quantity_given_away is not None:
        ownership.quantity_given_away = (
            ownership.quantity_given_away or 0
        ) + quantity_given_away
    _apply_import_ownership_copy_details(
        ownership,
        copy_type=copy_type,
        engraving_text=engraving_text,
        engraving_notes=engraving_notes,
        engraving_signature=engraving_signature,
    )


def _find_import_variant(item: Item, color: str) -> ItemVariant | None:
    """Return an existing variant for an item/color pair if one exists."""
    normalized_color = (color or "").strip().lower()
    if not normalized_color:
        return None
    return (
        ItemVariant.query.filter_by(item_id=item.id)
        .filter(db.func.lower(ItemVariant.color) == normalized_color)
        .first()
    )

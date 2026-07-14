"""Variant-sync routes for the data blueprint."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload

from blueprints.data import data_bp
from blueprints.data_workflows import (
    _build_purple_campaign_variant_preview,
    _build_set_variant_sync_preview,
    _build_variant_sync_preview,
    _merge_variant_sync_previews,
    _parse_variant_sync_selected_skus,
    _resolve_variant_sync_items,
    _resolve_variant_sync_sets,
    sync_variant_sync_helpers,
)
from constants import (
    SET_VARIANT_PROPAGATION_EXCLUDED_CATEGORIES,
    UNKNOWN_COLOR,
)
from extensions import db
from helpers import db_commit
from models import (
    Item,
    ItemVariant,
    Set,
    SetVariant,
    record_activity,
    reconcile_unknown_variant,
)
from scraping import discover_cutco_item_page_url
from scraping import set_handle_color_applies_to_member
import blueprints.data as data_module

logger = logging.getLogger(__name__)


@data_bp.route("/variant-sync", methods=["GET", "POST"])
def variant_sync_page():
    """Render the variant sync preview page."""
    sync_variant_sync_helpers(
        data_module.scrape_item_variant_colors,
        data_module.scrape_purple_campaign_variants,
        data_module.scrape_set_variant_options,
    )
    all_items = (
        Item.query.options(selectinload(Item.variants))
        .filter(Item.cutco_url.isnot(None) | Item.set_only.is_(True))
        .all()
    )
    categories = sorted(
        {item.category for item in all_items if item.category},
        key=lambda value: value.lower(),
    )

    if request.method == "GET":
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope="all",
            category="",
            selected_skus_text="",
        )

    scope = (request.form.get("scope") or "all").strip().lower()
    category = (request.form.get("category") or "").strip()
    selected_skus_text = request.form.get("selected_skus", "").strip()
    selected_skus = _parse_variant_sync_selected_skus(selected_skus_text)

    # Variant color pages can change over time, so always start with a fresh scrape.
    cast(Any, data_module.scrape_item_variant_colors).cache_clear()
    cast(Any, data_module.scrape_set_variant_options).cache_clear()
    cast(Any, data_module.scrape_purple_campaign_variants).cache_clear()
    cast(Any, discover_cutco_item_page_url).cache_clear()
    items, selection_error = _resolve_variant_sync_items(scope, category, selected_skus)
    item_sets = _resolve_variant_sync_sets(scope, category, selected_skus)
    if selection_error and not item_sets:
        flash(selection_error, "error")
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope=scope or "all",
            category=category,
            selected_skus_text=selected_skus_text,
        )
    if not items and not item_sets:
        flash(
            "No variant-sync eligible catalog items were found for that scope.",
            "warning",
        )
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope=scope or "all",
            category=category,
            selected_skus_text=selected_skus_text,
        )

    item_preview = _build_variant_sync_preview(items)
    pending_item_colors = {
        preview_item["item_id"]: {
            str(color).strip().lower()
            for color in preview_item.get("create_colors", [])
            if str(color).strip()
        }
        for preview_item in item_preview.get("items", [])
        if preview_item.get("item_id")
    }
    preview = _merge_variant_sync_previews(
        item_preview,
        _build_set_variant_sync_preview(item_sets, pending_item_colors),
    )
    promo_preview = _build_purple_campaign_variant_preview()
    preview["promo_items"] = promo_preview.get("items", [])
    preview["promo_summary"] = promo_preview.get("summary", {})
    preview["scope"] = scope
    preview["scope_label"] = {
        "all": "Entire catalog",
        "category": f"Category: {category}",
        "selected": "Selected SKUs",
    }.get(scope, "Entire catalog")
    preview["category"] = category
    preview["selected_skus_text"] = selected_skus_text
    preview_json = json.dumps(preview, ensure_ascii=False)
    return render_template(
        "variant_sync_preview.html",
        preview=preview,
        preview_json=preview_json,
        categories=categories,
        scope=scope,
        category=category,
        selected_skus_text=selected_skus_text,
    )


@data_bp.route("/variant-sync/confirm", methods=["POST"])
def variant_sync_confirm():
    """Apply the variant sync preview."""
    preview_raw = request.form.get("preview_json", "")
    if not preview_raw:
        flash("Variant sync preview data was missing.", "error")
        return redirect(url_for("data.variant_sync_page"))

    try:
        preview = json.loads(preview_raw)
    except json.JSONDecodeError:
        flash("Variant sync preview data could not be read.", "error")
        return redirect(url_for("data.variant_sync_page"))

    created_variants = 0
    retained_variants = 0
    skipped_items = 0
    touched_items = 0
    mark_purple_as_unicorn = request.form.get("mark_purple_variants_unicorn") == "on"
    confirm_target = (request.form.get("confirm_target") or "all").strip()
    set_selection_enabled = request.form.get("set_selection_enabled") == "1"
    selected_set_ids = {
        int(raw_set_id)
        for raw_set_id in request.form.getlist("selected_set_ids")
        if raw_set_id.isdigit()
    }
    skipped_details: list[dict] = []

    try:
        promo_summary = preview.get("promo_summary", {})

        def should_process(preview_item: dict, *, section: str) -> bool:
            is_set = preview_item.get("entity_type") == "set"
            set_id = preview_item.get("set_id")
            if set_selection_enabled and is_set and set_id not in selected_set_ids:
                return False
            if confirm_target == "all":
                return True
            if confirm_target == "promo":
                return section == "promo"
            if confirm_target == "selected_sets":
                return section == "category" and is_set and set_id in selected_set_ids
            if confirm_target.startswith("category:"):
                return (
                    section == "category"
                    and confirm_target == f"category:{preview_item.get('category', '')}"
                )
            return True

        def apply_item_preview(
            preview_item: dict, *, allow_purple_unicorn: bool = False
        ) -> None:
            nonlocal created_variants, retained_variants, skipped_items, touched_items
            if preview_item.get("entity_type") == "set":
                set_id = preview_item.get("set_id")
                item_set = db.session.get(Set, set_id) if set_id else None
                if not item_set:
                    skipped_items += 1
                    return
                if preview_item.get("status") == "skipped":
                    skipped_items += 1
                    skipped_details.append(
                        {
                            "item": item_set.name,
                            "sku": item_set.sku or "—",
                            "reason": preview_item.get("skip_reason")
                            or "No clear set variants were detected.",
                        }
                    )
                    return
                scraped_url = (preview_item.get("scraped_url") or "").strip()
                if scraped_url:
                    item_set.cutco_url = scraped_url
                existing_set_options = {
                    (variant.kind, variant.color.lower())
                    for variant in item_set.variants
                }
                existing_set_by_color = {
                    variant.color.lower(): variant for variant in item_set.variants
                }
                for option in preview_item.get("remove_options", []):
                    variant_id = option.get("variant_id")
                    variant = (
                        db.session.get(SetVariant, variant_id) if variant_id else None
                    )
                    if variant and variant.set_id == item_set.id:
                        db.session.delete(variant)
                for option in preview_item.get("reclassify_options", []):
                    color_value = (option.get("color") or "").strip()
                    kind = option.get("kind") or "handle"
                    existing_variant = existing_set_by_color.get(color_value.lower())
                    if existing_variant and existing_variant.kind != kind:
                        existing_variant.kind = kind
                        created_variants += 1
                for option in preview_item.get("create_options", []):
                    color_value = (option.get("color") or "").strip()
                    kind = option.get("kind") or "handle"
                    if (
                        color_value
                        and (kind, color_value.lower()) not in existing_set_options
                    ):
                        db.session.add(
                            SetVariant(
                                set=item_set,
                                color=color_value,
                                kind=kind,
                                source="variant_sync",
                            )
                        )
                        existing_set_options.add((kind, color_value.lower()))
                        created_variants += 1
                for membership in item_set.members:
                    member = membership.item
                    if (
                        not member
                        or (member.category or "")
                        in SET_VARIANT_PROPAGATION_EXCLUDED_CATEGORIES
                    ):
                        continue
                    existing_member_colors = {
                        variant.color.lower()
                        for variant in member.variants
                        if variant.color != UNKNOWN_COLOR
                    }
                    for color in preview_item.get("propagate_colors", []):
                        color_value = (color or "").strip()
                        if (
                            not color_value
                            or not set_handle_color_applies_to_member(
                                member.sku,
                                color_value,
                                preview_item.get("propagate_color_member_skus"),
                            )
                            or color_value.lower() in existing_member_colors
                        ):
                            continue
                        db.session.add(
                            ItemVariant(
                                item=member,
                                color=color_value,
                                source="set_variant_sync",
                            )
                        )
                        existing_member_colors.add(color_value.lower())
                        created_variants += 1
                    db.session.flush()
                    reconcile_unknown_variant(member)
                retained_variants += len(preview_item.get("retained_colors", []))
                touched_items += 1
                return
            item_id = preview_item.get("item_id")
            if not item_id:
                return
            item = db.session.get(Item, item_id)
            if not item:
                skipped_items += 1
                skipped_details.append(
                    {
                        "item": preview_item.get("item_name", "Unknown item"),
                        "sku": preview_item.get("sku", "—"),
                        "reason": "Item was not found during confirmation.",
                    }
                )
                return

            if preview_item.get("status") == "skipped":
                skipped_items += 1
                skipped_details.append(
                    {
                        "item": item.name,
                        "sku": item.sku or "—",
                        "reason": preview_item.get("skip_reason")
                        or "No clear variants were detected.",
                    }
                )
                return

            scraped_url = (preview_item.get("scraped_url") or "").strip()
            url_changed = bool(scraped_url and item.cutco_url != scraped_url)
            if url_changed:
                item.cutco_url = scraped_url

            existing_real = {
                variant.color.lower()
                for variant in item.variants
                if variant.color != UNKNOWN_COLOR
            }
            create_colors = []
            for color in preview_item.get("create_colors", []):
                color_value = (color or "").strip()
                if not color_value:
                    continue
                if color_value.lower() in existing_real:
                    retained_variants += 1
                    continue
                variant = ItemVariant(
                    item=item, color=color_value, source="variant_sync"
                )
                if (
                    allow_purple_unicorn
                    and mark_purple_as_unicorn
                    and color_value.lower().startswith("purple")
                ):
                    variant.is_unicorn = True
                db.session.add(variant)
                create_colors.append(color_value)
                created_variants += 1
            if allow_purple_unicorn:
                for variant in item.variants:
                    if variant.color.lower().startswith("purple"):
                        if mark_purple_as_unicorn:
                            variant.is_unicorn = True
            retained_variants += len(preview_item.get("retained_colors", []))
            if create_colors or preview_item.get("retained_colors") or url_changed:
                touched_items += 1
                db.session.flush()
                reconcile_unknown_variant(item)

        for preview_item in preview.get("items", []):
            if should_process(preview_item, section="category"):
                apply_item_preview(preview_item)
        for preview_item in preview.get("promo_items", []):
            if should_process(preview_item, section="promo"):
                apply_item_preview(preview_item, allow_purple_unicorn=True)

        combined_summary = dict(preview.get("summary", {}))
        for key in (
            "items_scanned",
            "variants_found",
            "variants_to_create",
            "variants_retained",
            "items_with_no_clear_variants",
            "purple_variant_count",
        ):
            combined_summary[key] = combined_summary.get(key, 0) + promo_summary.get(
                key, 0
            )
        combined_summary["has_purple_variants"] = (
            combined_summary.get("purple_variant_count", 0) > 0
        )

        record_activity(
            "sync",
            "Variant sync complete",
            (
                f"Items scanned {combined_summary.get('items_scanned', 0)}, "
                f"variants created {created_variants}, retained {retained_variants}, "
                f"skipped {skipped_items}."
            ),
        )
        if db_commit(db.session):
            return render_template(
                "variant_sync_result.html",
                summary=combined_summary,
                created_variants=created_variants,
                retained_variants=retained_variants,
                skipped_items=skipped_items,
                touched_items=touched_items,
                skipped_details=skipped_details,
                scope_label=preview.get("scope_label", "Entire catalog"),
            )
    except Exception as exc:
        db.session.rollback()
        logger.error("Variant sync failed: %s", exc)
        flash("Variant sync failed — no changes were saved.", "error")
        return redirect(url_for("data.variant_sync_page"))

    return redirect(url_for("data.variant_sync_page"))

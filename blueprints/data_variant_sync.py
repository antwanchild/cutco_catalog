"""Variant-sync routes for the data blueprint."""

from __future__ import annotations

import json
import logging

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload

from blueprints.data import data_bp
from blueprints.data_workflows import (
    _build_purple_campaign_variant_preview,
    _build_variant_sync_preview,
    _parse_variant_sync_selected_skus,
    _resolve_variant_sync_items,
    sync_variant_sync_helpers,
)
from constants import UNKNOWN_COLOR
from extensions import db
from helpers import db_commit
from models import Item, ItemVariant, record_activity, reconcile_unknown_variant
import blueprints.data as data_module

logger = logging.getLogger(__name__)


@data_bp.route("/variant-sync", methods=["GET", "POST"])
def variant_sync_page():
    """Render the variant sync preview page."""
    sync_variant_sync_helpers(
        data_module.scrape_item_variant_colors,
        data_module.scrape_purple_campaign_variants,
    )
    all_items = (
        Item.query.options(selectinload(Item.variants))
        .filter(Item.cutco_url.isnot(None))
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
    data_module.scrape_item_variant_colors.cache_clear()
    data_module.scrape_purple_campaign_variants.cache_clear()
    items, selection_error = _resolve_variant_sync_items(scope, category, selected_skus)
    if selection_error:
        flash(selection_error, "error")
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope=scope or "all",
            category=category,
            selected_skus_text=selected_skus_text,
        )
    if not items:
        flash("No catalog items with URLs were found for that scope.", "warning")
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope=scope or "all",
            category=category,
            selected_skus_text=selected_skus_text,
        )

    preview = _build_variant_sync_preview(items)
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
    skipped_details: list[dict] = []

    try:
        promo_summary = preview.get("promo_summary", {})

        def should_process(item_data: dict, *, section: str) -> bool:
            if confirm_target == "all":
                return True
            if confirm_target == "promo":
                return section == "promo"
            if confirm_target.startswith("category:"):
                return (
                    section == "category"
                    and confirm_target == f"category:{item_data.get('category', '')}"
                )
            return True

        def apply_item_preview(
            item_data: dict, *, allow_purple_unicorn: bool = False
        ) -> None:
            nonlocal created_variants, retained_variants, skipped_items, touched_items
            item_id = item_data.get("item_id")
            if not item_id:
                return
            item = db.session.get(Item, item_id)
            if not item:
                skipped_items += 1
                skipped_details.append(
                    {
                        "item": item_data.get("item_name", "Unknown item"),
                        "sku": item_data.get("sku", "—"),
                        "reason": "Item was not found during confirmation.",
                    }
                )
                return

            if item_data.get("status") == "skipped":
                skipped_items += 1
                skipped_details.append(
                    {
                        "item": item.name,
                        "sku": item.sku or "—",
                        "reason": item_data.get("skip_reason")
                        or "No clear variants were detected.",
                    }
                )
                return

            existing_real = {
                variant.color.lower()
                for variant in item.variants
                if variant.color != UNKNOWN_COLOR
            }
            create_colors = []
            for color in item_data.get("create_colors", []):
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
            retained_variants += len(item_data.get("retained_colors", []))
            if create_colors or item_data.get("retained_colors"):
                touched_items += 1
                db.session.flush()
                reconcile_unknown_variant(item)

        for item_data in preview.get("items", []):
            if should_process(item_data, section="category"):
                apply_item_preview(item_data)
        for item_data in preview.get("promo_items", []):
            if should_process(item_data, section="promo"):
                apply_item_preview(item_data, allow_purple_unicorn=True)

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

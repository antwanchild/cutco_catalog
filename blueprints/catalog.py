"""Catalog, item, set, and sync routes."""

import json
import logging
from datetime import UTC, datetime
from typing import Any, cast

from flask import (
    abort,
    Flask,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from constants import (
    AVAILABILITY_CHOICES,
    EDGELESS_CATEGORIES,
    COOKWARE_CATEGORIES,
    EDGE_TYPES,
    SYNC_BLOCKED_CATEGORIES,
    UNKNOWN_COLOR,
    canonicalize_category,
    canonicalize_availability,
    normalize_edge_for_category,
    VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES,
)
from extensions import db
from helpers import admin_required, db_commit, is_admin, is_authenticated_user
from models import (
    Item,
    ItemAttachment,
    ItemSetMember,
    ItemVariant,
    KnifeTask,
    Ownership,
    Set,
    normalize_sku_value,
    parse_alternate_skus,
    record_audit_event,
    get_or_create_set,
    record_activity,
    reconcile_unknown_variant,
)
from scraping import (
    scrape_catalog,
    scrape_item_specs,
    scrape_item_variant_colors,
    scrape_item_uses,
    scrape_sets,
)
import blueprints.catalog_sync as catalog_sync_module

from blueprints.catalog_sync import (
    catalog_bp,
    _aggregate_resolved_members,
    _build_member_name_lookup,
    _build_set_membership_preview,  # noqa: F401 - re-exported for tests/backwards compatibility
    _build_member_status_rows,
    _catalog_category_options,
    _coerce_quantity,
    _delete_attachment_files,
    _item_alternate_skus_text,
    _load_member_snapshot,
    _normalize_member_sku,
    _safe_redirect_target,
    _set_name_options,
    _set_sku_options,
)

logger = logging.getLogger(__name__)
UNCATEGORIZED_FILTER = "__uncategorized__"
_CATALOG_SYNC_JOB_FILE = catalog_sync_module._CATALOG_SYNC_JOB_FILE


def _current_flask_app() -> Flask:
    """Return the real Flask app object instead of the request-local proxy."""
    return cast(Flask, cast(Any, current_app)._get_current_object())


def _sync_catalog_sync_helpers() -> None:
    """Keep the helper module pointed at the patchable route-level scrapers."""
    catalog_sync_module._CATALOG_SYNC_JOB_FILE = _CATALOG_SYNC_JOB_FILE
    catalog_sync_module.scrape_catalog = scrape_catalog
    catalog_sync_module.scrape_item_specs = scrape_item_specs
    catalog_sync_module.scrape_item_variant_colors = scrape_item_variant_colors
    catalog_sync_module.scrape_sets = scrape_sets


def _read_catalog_sync_job() -> dict:
    _sync_catalog_sync_helpers()
    return catalog_sync_module._read_catalog_sync_job()


def _write_catalog_sync_job(data: dict) -> None:
    _sync_catalog_sync_helpers()
    catalog_sync_module._write_catalog_sync_job(data)


def _reset_catalog_sync_job() -> None:
    _sync_catalog_sync_helpers()
    catalog_sync_module._reset_catalog_sync_job()


def _start_catalog_sync_background_job(app) -> None:
    _sync_catalog_sync_helpers()
    catalog_sync_module._start_catalog_sync_background_job(app)


def _run_catalog_sync_job(app) -> None:
    """Run the catalog sync job with the route-level scraper bindings."""
    _sync_catalog_sync_helpers()
    catalog_sync_module._run_catalog_sync_job(app)


@catalog_bp.route("/catalog")
def catalog():
    """Render the catalog browser."""
    search_query = request.args.get("q", "").strip()
    cat_filter = request.args.get("category", "")
    unicorn_f = request.args.get("unicorn", "")
    status_f = request.args.get("status", "")
    availability_f = request.args.get("availability", "")
    sort = request.args.get("sort", "name")
    direction = request.args.get("dir", "asc")

    query = Item.query
    if search_query:
        query = query.filter(
            db.or_(
                Item.name.ilike(f"%{search_query}%"),
                Item.sku.ilike(f"%{search_query}%"),
            )
        )
    if cat_filter:
        if cat_filter == UNCATEGORIZED_FILTER:
            query = query.filter(db.or_(Item.category.is_(None), Item.category == ""))
        else:
            query = query.filter(Item.category == cat_filter)
    if unicorn_f == "1":
        query = query.filter(
            db.or_(
                Item.is_unicorn,
                Item.edge_is_unicorn,
                Item.variants.any(ItemVariant.is_unicorn == True),  # noqa: E712
            )
        )
    if status_f == "set_only":
        query = query.filter(Item.set_only.is_(True))
    elif status_f == "off_catalog":
        query = query.filter(Item.set_only.is_(False), Item.in_catalog.is_(False))
    elif status_f == "non_catalog":
        query = query.filter(Item.in_catalog.is_(False))
    if availability_f:
        query = query.filter(Item.availability == availability_f)

    sort_cols = {
        "name": Item.name,
        "sku": Item.sku,
        "category": Item.category,
        "edge_type": Item.edge_type,
    }
    col = sort_cols.get(sort, Item.name)
    if sort == "category":
        category_col = db.func.lower(db.func.coalesce(Item.category, ""))
        name_col = db.func.lower(db.func.coalesce(Item.name, ""))
        items = (
            query.options(selectinload(Item.variants), selectinload(Item.sets))
            .order_by(
                category_col.desc() if direction == "desc" else category_col,
                name_col.desc() if direction == "desc" else name_col,
            )
            .all()
        )
    elif sort == "edge_type":
        edge_col = db.func.lower(db.func.coalesce(Item.edge_type, ""))
        name_col = db.func.lower(db.func.coalesce(Item.name, ""))
        items = (
            query.options(selectinload(Item.variants), selectinload(Item.sets))
            .order_by(
                edge_col.desc() if direction == "desc" else edge_col,
                name_col.desc() if direction == "desc" else name_col,
            )
            .all()
        )
    elif sort == "variants":
        variant_count = db.func.count(ItemVariant.id)
        name_col = db.func.lower(db.func.coalesce(Item.name, ""))
        items = (
            query.outerjoin(Item.variants)
            .options(selectinload(Item.variants), selectinload(Item.sets))
            .group_by(Item.id)
            .order_by(
                variant_count.desc() if direction == "desc" else variant_count,
                name_col.desc() if direction == "desc" else name_col,
            )
            .all()
        )
    else:
        items = (
            query.options(selectinload(Item.variants), selectinload(Item.sets))
            .order_by(col.desc() if direction == "desc" else col)
            .all()
        )

    categories = [
        row[0]
        for row in db.session.query(Item.category)
        .filter(Item.category.isnot(None))
        .distinct()
        .order_by(Item.category)
        .all()
    ]
    referenced_item_ids = {
        ownership.variant.item_id for ownership in Ownership.query.all()
    }
    unreferenced_count = (
        Item.query.filter(~Item.id.in_(referenced_item_ids)).count()
        if referenced_item_ids
        else Item.query.count()
    )
    all_item_count = Item.query.count()
    set_count = Set.query.count()

    return render_template(
        "catalog.html",
        items=items,
        categories=categories,
        q=search_query,
        cat_filter=cat_filter,
        unicorn_f=unicorn_f,
        status_f=status_f,
        availability_f=availability_f,
        sort=sort,
        direction=direction,
        availability_choices=AVAILABILITY_CHOICES,
        edge_types=EDGE_TYPES,
        UNCATEGORIZED_FILTER=UNCATEGORIZED_FILTER,
        SINGLE_VARIANT_CATEGORIES=VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES,
        edgeless_categories=EDGELESS_CATEGORIES,
        UNKNOWN_COLOR=UNKNOWN_COLOR,
        unreferenced_count=unreferenced_count,
        all_item_count=all_item_count,
        set_count=set_count,
    )


@catalog_bp.route("/catalog/add", methods=["GET", "POST"])
@admin_required
def catalog_add():
    """Create a catalog item."""
    next_target = (
        request.form.get("next", "")
        if request.method == "POST"
        else request.args.get("next", "")
    )
    if request.method == "POST":
        availability = canonicalize_availability(
            request.form.get("availability", "public")
        )
        item = Item(
            name=request.form["name"].strip(),
            sku=normalize_sku_value(request.form.get("sku", "")),
            alternate_skus=", ".join(
                parse_alternate_skus(request.form.get("alternate_skus", ""))
            )
            or None,
            category=canonicalize_category(request.form.get("category", "")),
            availability=availability,
            edge_type=normalize_edge_for_category(
                canonicalize_category(request.form.get("category", "")),
                request.form.get("edge_type", "Unknown"),
            )[0],
            is_unicorn=request.form.get("is_unicorn") == "on",
            edge_is_unicorn=normalize_edge_for_category(
                canonicalize_category(request.form.get("category", "")),
                request.form.get("edge_type", "Unknown"),
                request.form.get("edge_is_unicorn") == "on",
            )[1],
            set_only=request.form.get("set_only") == "on",
            in_catalog=availability == "public"
            and request.form.get("set_only") != "on",
            cutco_url=request.form.get("cutco_url", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(item)
        db.session.flush()
        colors = [
            raw_color.strip()
            for raw_color in request.form.get("colors", "").split(",")
            if raw_color.strip()
        ]
        for color in colors:
            if (
                color != UNKNOWN_COLOR
                and (item.category or "") not in COOKWARE_CATEGORIES
            ):
                db.session.add(
                    ItemVariant(item_id=item.id, color=color, source="manual")
                )
        db.session.flush()
        reconcile_unknown_variant(item)
        if db_commit(db.session):
            logger.info("Item added: %s (SKU: %s)", item.name, item.sku or "none")
            flash(f'Added "{item.name}" to catalog.', "success")
        return redirect(
            _safe_redirect_target(next_target) or url_for("catalog.catalog")
        )

    return render_template(
        "item_form.html",
        item=None,
        edge_types=EDGE_TYPES,
        action="Add",
        UNKNOWN_COLOR=UNKNOWN_COLOR,
        all_sets=Set.query.order_by(Set.name).all(),
        categories=_catalog_category_options(),
        availability_choices=AVAILABILITY_CHOICES,
        alternate_skus_text="",
        next_target=_safe_redirect_target(next_target),
        edgeless_categories=EDGELESS_CATEGORIES,
    )


@catalog_bp.route("/catalog/<int:item_id>/edit", methods=["GET", "POST"])
@admin_required
def catalog_edit(item_id):
    """Edit an existing catalog item."""
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    next_target = (
        request.form.get("next", "")
        if request.method == "POST"
        else request.args.get("next", "")
    )
    if request.method == "POST":
        availability = canonicalize_availability(
            request.form.get("availability", "public")
        )
        item.name = request.form["name"].strip()
        item.sku = normalize_sku_value(request.form.get("sku", ""))
        item.alternate_skus = (
            ", ".join(parse_alternate_skus(request.form.get("alternate_skus", "")))
            or None
        )
        item.category = canonicalize_category(request.form.get("category", ""))
        item.availability = availability
        item.edge_type, item.edge_is_unicorn = normalize_edge_for_category(
            item.category,
            request.form.get("edge_type", "Unknown"),
            request.form.get("edge_is_unicorn") == "on",
        )
        item.is_unicorn = request.form.get("is_unicorn") == "on"
        item.set_only = request.form.get("set_only") == "on"
        item.in_catalog = (
            availability == "public" and request.form.get("set_only") != "on"
        )
        item.cutco_url = request.form.get("cutco_url", "").strip() or None
        item.notes = request.form.get("notes", "").strip() or None

        selected_set_ids: set[int] = set()
        invalid_set_id_seen = False
        for set_id_str in request.form.getlist("set_ids"):
            try:
                selected_set_ids.add(int(set_id_str))
            except (TypeError, ValueError):
                invalid_set_id_seen = True
        if invalid_set_id_seen:
            flash("Some set selections were invalid and were ignored.", "warning")
        current_memberships = {
            membership.set_id: membership for membership in item.set_memberships
        }
        selected_sets = (
            {
                selected_set.id: selected_set
                for selected_set in Set.query.filter(Set.id.in_(selected_set_ids)).all()
            }
            if selected_set_ids
            else {}
        )

        for existing_set_id, existing_member in list(current_memberships.items()):
            if existing_set_id not in selected_sets:
                db.session.delete(existing_member)

        for set_id in selected_sets:
            if set_id not in current_memberships:
                db.session.add(
                    ItemSetMember(item_id=item.id, set_id=set_id, quantity=1)
                )

        if db_commit(db.session):
            logger.info("Item updated: %s (SKU: %s)", item.name, item.sku or "none")
            flash(f'Updated "{item.name}".', "success")
        return redirect(
            _safe_redirect_target(next_target) or url_for("catalog.catalog")
        )

    return render_template(
        "item_form.html",
        item=item,
        edge_types=EDGE_TYPES,
        action="Edit",
        UNKNOWN_COLOR=UNKNOWN_COLOR,
        all_sets=Set.query.order_by(Set.name).all(),
        categories=_catalog_category_options(),
        availability_choices=AVAILABILITY_CHOICES,
        alternate_skus_text=_item_alternate_skus_text(item),
        next_target=_safe_redirect_target(next_target),
        edgeless_categories=EDGELESS_CATEGORIES,
    )


@catalog_bp.route("/catalog/purge-unreferenced", methods=["POST"])
def catalog_purge_unreferenced():
    """Delete catalog items that have no ownership records."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    referenced_item_ids = {
        ownership.variant.item_id for ownership in Ownership.query.all()
    }
    items = (
        Item.query.filter(~Item.id.in_(referenced_item_ids)).all()
        if referenced_item_ids
        else Item.query.all()
    )
    count = len(items)
    for item in items:
        _delete_attachment_files(item)
        db.session.delete(item)
    if db_commit(db.session):
        logger.info("Purged %d unreferenced catalog items", count)
        record_audit_event(
            kind="audit",
            title="Purged unreferenced catalog items",
            action="delete",
            entity_type="Item",
            entity_name="Unreferenced catalog items",
            payload={"items_deleted": count},
        )
        flash(
            f"Removed {count} item{'s' if count != 1 else ''} with no ownership records.",
            "info",
        )
    return redirect(url_for("catalog.catalog"))


@catalog_bp.route("/catalog/purge-all", methods=["POST"])
def catalog_purge_all():
    """Delete the entire catalog including sets, ownership, logs, and variants."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    count = Item.query.count()
    set_count = Set.query.count()
    items = Item.query.all()
    for item in items:
        _delete_attachment_files(item)
    ItemAttachment.query.delete()
    Item.query.delete()
    Set.query.delete()
    if db_commit(db.session):
        logger.info(
            "Full catalog purge: %d items deleted, %d sets deleted", count, set_count
        )
        record_audit_event(
            kind="audit",
            title="Purged full catalog",
            action="delete",
            entity_type="Catalog",
            entity_name="Catalog and sets",
            payload={"items_deleted": count, "sets_deleted": set_count},
        )
        flash(
            f"Catalog purged — {count} item{'s' if count != 1 else ''} and "
            f"{set_count} set{'s' if set_count != 1 else ''} deleted, plus related records.",
            "info",
        )
    return redirect(url_for("catalog.catalog"))


@catalog_bp.route("/catalog/<int:item_id>/delete", methods=["POST"])
def catalog_delete(item_id):
    """Delete a catalog item."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    name = item.name
    _delete_attachment_files(item)
    db.session.delete(item)
    if db_commit(db.session):
        logger.info("Item deleted: %s", name)
        flash(f'Deleted "{name}".', "info")
    return redirect(url_for("catalog.catalog"))


# ── Variants ──────────────────────────────────────────────────────────────────


@catalog_bp.route("/variants")
def variants_browse():
    """Browse variants across the catalog with optional color filtering."""
    q = request.args.get("q", "").strip()
    color = request.args.get("color", "").strip()
    include_unknown = request.args.get("unknown") == "1"

    query = ItemVariant.query.join(Item).options(selectinload(ItemVariant.item))
    if not include_unknown:
        query = query.filter(ItemVariant.color != UNKNOWN_COLOR)
    if color:
        query = query.filter(func.lower(ItemVariant.color) == color.lower())
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Item.name.ilike(like),
                Item.sku.ilike(like),
                Item.category.ilike(like),
                ItemVariant.color.ilike(like),
                ItemVariant.notes.ilike(like),
            )
        )

    variants = query.order_by(
        ItemVariant.color.asc(), Item.name.asc(), Item.sku.asc()
    ).all()
    color_counts = db.session.query(ItemVariant.color, func.count(ItemVariant.id)).join(
        Item
    )
    if not include_unknown:
        color_counts = color_counts.filter(ItemVariant.color != UNKNOWN_COLOR)
    if color:
        color_counts = color_counts.filter(
            func.lower(ItemVariant.color) == color.lower()
        )
    if q:
        like = f"%{q}%"
        color_counts = color_counts.filter(
            or_(
                Item.name.ilike(like),
                Item.sku.ilike(like),
                Item.category.ilike(like),
                ItemVariant.color.ilike(like),
                ItemVariant.notes.ilike(like),
            )
        )
    color_counts = [
        {"color": color_name, "count": count}
        for color_name, count in (
            color_counts.group_by(ItemVariant.color)
            .order_by(func.count(ItemVariant.id).desc(), ItemVariant.color.asc())
            .all()
        )
    ]
    item_count = len({variant.item_id for variant in variants})
    return render_template(
        "variants_browse.html",
        variants=variants,
        color_counts=color_counts,
        item_count=item_count,
        q=q,
        color=color,
        include_unknown=include_unknown,
        UNKNOWN_COLOR=UNKNOWN_COLOR,
    )


@catalog_bp.route("/catalog/<int:item_id>/variants")
def variants(item_id):
    """Render the variants view for an item."""
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    is_single_variant = (item.category or "") in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES
    return render_template(
        "variants.html",
        item=item,
        UNKNOWN_COLOR=UNKNOWN_COLOR,
        is_single_variant=is_single_variant,
    )


@catalog_bp.route("/catalog/<int:item_id>/variants/add", methods=["POST"])
@admin_required
def variant_add(item_id):
    """Add a variant to an item."""
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    color = request.form.get("color", "").strip()
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    if (
        item.category or ""
    ) in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES and color != UNKNOWN_COLOR:
        flash(
            "These items use a single Unknown variant; color variants are not supported.",
            "warning",
        )
        return redirect(url_for("catalog.variants", item_id=item_id))
    if any(
        existing_variant.color.lower() == color.lower()
        for existing_variant in item.variants
    ):
        flash(f'"{color}" already exists for this item.', "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    db.session.add(
        ItemVariant(
            item_id=item_id,
            color=color,
            source="manual",
            notes=request.form.get("notes", "").strip() or None,
        )
    )
    db.session.flush()
    reconcile_unknown_variant(item)
    if db_commit(db.session):
        logger.info("Variant added: %s → %s", item.name, color)
        flash(f'Added variant "{color}".', "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/edit", methods=["POST"])
@admin_required
def variant_edit(vid):
    """Edit an item variant."""
    variant = db.session.get(ItemVariant, vid)
    if not variant:
        abort(404)
    item_id = variant.item_id
    color = request.form.get("color", "").strip()
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    item = variant.item
    if item is None:
        abort(404)
    if (
        item.category or ""
    ) in VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES and color != UNKNOWN_COLOR:
        flash(
            "These items use a single Unknown variant; color variants are not supported.",
            "warning",
        )
        return redirect(url_for("catalog.variants", item_id=item_id))
    variant.color = color
    variant.notes = request.form.get("notes", "").strip() or None
    variant.is_unicorn = request.form.get("is_unicorn") == "on"
    db.session.flush()
    reconcile_unknown_variant(item)
    if db_commit(db.session):
        logger.info("Variant updated: item %d → %s", item_id, color)
        flash(f'Updated to "{color}".', "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/reset-unknown", methods=["POST"])
@admin_required
def variant_reset_unknown(vid):
    """Reset a single bad variant back to the Unknown fallback."""
    variant = db.session.get(ItemVariant, vid)
    if not variant:
        abort(404)
    item = variant.item
    if item is None:
        abort(404)
    item_id = variant.item_id
    if len(item.variants) != 1:
        flash(
            "Reset to Unknown is only available when this is the only variant.",
            "warning",
        )
        return redirect(url_for("catalog.variants", item_id=item_id))
    if variant.color == UNKNOWN_COLOR:
        flash("This variant is already Unknown.", "info")
        return redirect(url_for("catalog.variants", item_id=item_id))

    variant.color = UNKNOWN_COLOR
    variant.notes = None
    variant.is_unicorn = False
    variant.source = "fallback_unknown"
    db.session.flush()
    reconcile_unknown_variant(item)
    if db_commit(db.session):
        logger.info("Variant reset to Unknown: item %d", item_id)
        flash("Variant reset to Unknown.", "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/delete", methods=["POST"])
@admin_required
def variant_delete(vid):
    """Delete an item variant."""
    variant = db.session.get(ItemVariant, vid)
    if not variant:
        abort(404)
    item = variant.item
    if item is None:
        abort(404)
    if len(item.variants) == 1:
        flash("Cannot delete the only variant. Add another first.", "error")
        return redirect(url_for("catalog.variants", item_id=variant.item_id))
    item_id = variant.item_id
    db.session.delete(variant)
    db.session.flush()
    reconcile_unknown_variant(item)
    if db_commit(db.session):
        logger.info("Variant deleted: item %d", item_id)
        flash("Variant removed.", "info")
    return redirect(url_for("catalog.variants", item_id=item_id))


# ── Sets ──────────────────────────────────────────────────────────────────────


@catalog_bp.route("/sets")
def sets_list():
    """Render the set list page."""
    from models import Ownership, Person

    all_sets = Set.query.order_by(Set.name).all()
    all_persons = Person.query.order_by(Person.name).all()
    person_id = request.args.get("person", type=int)
    not_in_catalog_f = request.args.get("missing", "")
    incomplete_f = request.args.get("incomplete", "")

    # Completion relative to selected person, or globally if none selected
    owned_q = Ownership.query.filter_by(status="Owned")
    if person_id:
        owned_q = owned_q.filter_by(person_id=person_id)
    owned_ownerships = [
        ownership
        for ownership in owned_q.all()
        if ownership.variant is not None and ownership.variant.item is not None
    ]
    owned_item_ids = {ownership.variant.item_id for ownership in owned_ownerships}

    catalog_sku_lookup = {
        sku: item
        for item in Item.query.filter(Item.sku.isnot(None)).all()
        if (sku := _normalize_member_sku(item.sku))
    }

    completion = {}
    not_in_catalog_counts: dict[int, int] = {}
    filtered_sets = []
    for item_set in all_sets:
        total = len(item_set.items)
        owned = sum(1 for item in item_set.items if item.id in owned_item_ids)
        pct = round(100 * owned / total) if total else 0
        _, not_in_catalog_skus = _build_member_status_rows(
            _load_member_snapshot(item_set.member_data),
            catalog_sku_lookup,
        )
        not_in_catalog_counts[item_set.id] = len(not_in_catalog_skus)
        if not_in_catalog_f == "1" and not not_in_catalog_skus:
            continue
        if incomplete_f == "1" and pct == 100:
            continue
        completion[item_set.id] = dict(total=total, owned=owned, pct=pct)
        filtered_sets.append(item_set)

    return render_template(
        "sets.html",
        sets=filtered_sets,
        completion=completion,
        not_in_catalog_counts=not_in_catalog_counts,
        all_persons=all_persons,
        person_id=person_id,
        not_in_catalog_f=not_in_catalog_f,
        incomplete_f=incomplete_f,
    )


@catalog_bp.route("/sets/add", methods=["GET", "POST"])
@admin_required
def set_add():
    """Create a set."""
    next_target = (
        request.form.get("next", "")
        if request.method == "POST"
        else request.args.get("next", "")
    )
    if request.method == "POST":
        name = request.form["name"].strip()
        if Set.query.filter(db.func.lower(Set.name) == name.lower()).first():
            flash(f'Set "{name}" already exists.', "error")
            return redirect(url_for("catalog.set_add"))
        item_set = Set(
            name=name,
            sku=request.form.get("sku", "").strip().upper() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(item_set)
        if db_commit(db.session):
            logger.info("Set created: %s", name)
            flash(f'Created set "{name}".', "success")
        return redirect(
            _safe_redirect_target(next_target) or url_for("catalog.sets_list")
        )
    return render_template(
        "set_form.html",
        set=None,
        action="Add",
        all_items=[],
        member_qty_map={},
        set_name_options=_set_name_options(),
        set_sku_options=_set_sku_options(),
        next_target=_safe_redirect_target(next_target),
    )


@catalog_bp.route("/sets/<int:set_id>/edit", methods=["GET", "POST"])
@catalog_bp.route("/sets/<int:sid>/edit", methods=["GET", "POST"])
@admin_required
def set_edit(set_id=None, sid=None):
    """Edit a set."""
    set_id = set_id if set_id is not None else sid
    item_set = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    next_target = (
        request.form.get("next", "")
        if request.method == "POST"
        else request.args.get("next", "")
    )
    all_items = Item.query.order_by(Item.name).all()
    member_qty_map = {member.item_id: member.quantity for member in item_set.members}

    if request.method == "POST":
        item_set.name = request.form["name"].strip()
        item_set.sku = request.form.get("sku", "").strip().upper() or None
        item_set.notes = request.form.get("notes", "").strip() or None

        selected_item_ids: set[int] = set()
        for raw_item_id in request.form.getlist("member_item_ids"):
            try:
                selected_item_ids.add(int(raw_item_id))
            except (TypeError, ValueError):
                continue

        valid_item_ids = (
            {
                item_id
                for (item_id,) in db.session.query(Item.id)
                .filter(Item.id.in_(selected_item_ids))
                .all()
            }
            if selected_item_ids
            else set()
        )

        existing_members = {member.item_id: member for member in item_set.members}

        # Remove memberships that are no longer selected.
        for existing_item_id, existing_member in list(existing_members.items()):
            if existing_item_id not in valid_item_ids:
                db.session.delete(existing_member)

        # Add new memberships and update quantities on selected members.
        for item_id in valid_item_ids:
            try:
                qty = int(request.form.get(f"member_qty_{item_id}", "1"))
            except (TypeError, ValueError):
                qty = 1
            qty = max(1, qty)

            if item_id in existing_members:
                existing_members[item_id].quantity = qty
            else:
                db.session.add(
                    ItemSetMember(set_id=item_set.id, item_id=item_id, quantity=qty)
                )

        if db_commit(db.session):
            logger.info("Set updated: %s", item_set.name)
            flash(f'Updated set "{item_set.name}".', "success")
        return redirect(
            _safe_redirect_target(next_target) or url_for("catalog.sets_list")
        )
    return render_template(
        "set_form.html",
        set=item_set,
        action="Edit",
        all_items=all_items,
        member_qty_map=member_qty_map,
        set_name_options=_set_name_options(),
        set_sku_options=_set_sku_options(),
        next_target=_safe_redirect_target(next_target),
    )


def _restore_set_memberships_from_snapshot(item_set: Set) -> tuple[int, int, bool]:
    return _reconcile_set_memberships_from_entries(
        item_set,
        _load_member_snapshot(item_set.member_data),
        replace_memberships=False,
    )


def _reconcile_set_memberships_from_entries(
    item_set: Set,
    member_entries: list[dict[str, Any]],
    *,
    replace_memberships: bool,
) -> tuple[int, int, bool]:
    if not member_entries:
        return 0, 0, False

    items = Item.query.filter(Item.sku.isnot(None)).all()
    sku_to_item = {item.sku.upper(): item for item in items if item.sku}
    name_to_item = _build_member_name_lookup(items)
    resolved_members, _created_now = _aggregate_resolved_members(
        member_entries,
        sku_to_item,
        name_to_item,
        create_missing=False,
        set_name=item_set.name,
    )
    if not resolved_members:
        return 0, 0, False

    existing_members = {member.item_id: member for member in item_set.members}
    restored = 0
    updated = 0
    incoming_member_ids: set[int] = set()
    for item_id, resolved in resolved_members.items():
        item = resolved["item"]
        qty = _coerce_quantity(resolved.get("quantity"))
        membership = existing_members.get(item_id)
        if membership is None:
            db.session.add(
                ItemSetMember(set_id=item_set.id, item_id=item.id, quantity=qty)
            )
            restored += 1
        elif membership.quantity != qty:
            membership.quantity = qty
            updated += 1
        incoming_member_ids.add(item_id)

    if replace_memberships and incoming_member_ids:
        for membership in list(item_set.members):
            if membership.item_id not in incoming_member_ids:
                db.session.delete(membership)

    return restored, updated, True


@catalog_bp.route("/sets/<int:set_id>/restore-memberships", methods=["POST"])
@catalog_bp.route("/sets/<int:sid>/restore-memberships", methods=["POST"])
@admin_required
def set_restore_memberships(set_id=None, sid=None):
    """Restore set memberships from the stored snapshot."""
    set_id = set_id if set_id is not None else sid
    item_set = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    restored, updated, matched = _restore_set_memberships_from_snapshot(item_set)
    if not matched:
        flash(f'No catalog items could be matched for "{item_set.name}".', "warning")
        return redirect(url_for("catalog.set_detail", set_id=item_set.id))

    if db_commit(db.session):
        logger.info(
            "Set memberships restored: %s (+%d memberships, %d updated)",
            item_set.name,
            restored,
            updated,
        )
        flash(f'Restored {restored} membership(s) for "{item_set.name}".', "success")
    return redirect(url_for("catalog.set_detail", set_id=item_set.id))


@catalog_bp.route("/sets/bulk-resync-memberships", methods=["POST"])
@admin_required
def bulk_resync_set_memberships():
    """Resync memberships for selected sets from scrape data."""
    selected_set_ids: set[int] = set()
    invalid_seen = False
    for raw_set_id in request.form.getlist("set_ids"):
        try:
            selected_set_ids.add(int(raw_set_id))
        except (TypeError, ValueError):
            invalid_seen = True

    if invalid_seen:
        flash("Some set selections were invalid and were ignored.", "warning")
    if not selected_set_ids:
        flash("Choose one or more sets to resync.", "warning")
        return redirect(url_for("catalog.sets_list"))

    item_sets = {
        item_set.id: item_set
        for item_set in Set.query.filter(Set.id.in_(selected_set_ids)).all()
    }
    if not item_sets:
        flash("No valid sets were selected.", "warning")
        return redirect(url_for("catalog.sets_list"))

    try:
        scraped_sets = scrape_sets()
    except Exception as exc:
        logger.error("Bulk set resync failed: %s", exc)
        flash("Could not re-scrape Cutco sets right now.", "error")
        return redirect(url_for("catalog.sets_list"))

    scraped_lookup = {
        name.lower(): scraped_set
        for scraped_set in scraped_sets
        if isinstance(name := scraped_set.get("name"), str)
    }
    restored_sets = 0
    restored_memberships = 0
    updated_memberships = 0
    skipped_sets = 0
    for item_set in item_sets.values():
        scraped_set = scraped_lookup.get(item_set.name.lower())
        if not scraped_set:
            skipped_sets += 1
            continue
        raw_member_entries = scraped_set.get("member_entries")
        member_entries = (
            raw_member_entries if isinstance(raw_member_entries, list) else []
        )
        if member_entries:
            item_set.member_data = json.dumps(member_entries, ensure_ascii=False)
        restored, updated, matched = _reconcile_set_memberships_from_entries(
            item_set,
            member_entries,
            replace_memberships=True,
        )
        if not matched:
            skipped_sets += 1
            continue
        restored_sets += 1
        restored_memberships += restored
        updated_memberships += updated

    if db_commit(db.session):
        logger.info(
            "Bulk set resync: %d set(s), %d memberships restored, %d updated, %d skipped",
            restored_sets,
            restored_memberships,
            updated_memberships,
            skipped_sets,
        )
        flash(
            f"Resynced memberships for {restored_sets} set{'s' if restored_sets != 1 else ''}.",
            "success",
        )
        if skipped_sets:
            flash(
                f"Skipped {skipped_sets} set{'s' if skipped_sets != 1 else ''} that could not be matched from Cutco.",
                "warning",
            )
    return redirect(
        _safe_redirect_target(request.form.get("next")) or url_for("catalog.sets_list")
    )


@catalog_bp.route("/sets/bulk-restore-memberships", methods=["POST"])
@admin_required
def bulk_restore_set_memberships():
    """Restore memberships for selected sets from snapshots."""
    selected_set_ids: set[int] = set()
    invalid_seen = False
    for raw_set_id in request.form.getlist("set_ids"):
        try:
            selected_set_ids.add(int(raw_set_id))
        except (TypeError, ValueError):
            invalid_seen = True

    if invalid_seen:
        flash("Some set selections were invalid and were ignored.", "warning")
    if not selected_set_ids:
        flash("Choose one or more sets to restore.", "warning")
        return redirect(url_for("catalog.sets_list"))

    item_sets = {
        item_set.id: item_set
        for item_set in Set.query.filter(Set.id.in_(selected_set_ids)).all()
    }
    if not item_sets:
        flash("No valid sets were selected.", "warning")
        return redirect(url_for("catalog.sets_list"))

    restored_sets = 0
    restored_memberships = 0
    updated_memberships = 0
    skipped_sets = 0
    for item_set in item_sets.values():
        restored, updated, matched = _restore_set_memberships_from_snapshot(item_set)
        if not matched:
            skipped_sets += 1
            continue
        restored_sets += 1
        restored_memberships += restored
        updated_memberships += updated

    if db_commit(db.session):
        logger.info(
            "Bulk set membership restore: %d set(s), %d memberships restored, %d updated, %d skipped",
            restored_sets,
            restored_memberships,
            updated_memberships,
            skipped_sets,
        )
        flash(
            f"Restored memberships for {restored_sets} set{'s' if restored_sets != 1 else ''}.",
            "success",
        )
        if skipped_sets:
            flash(
                f"Skipped {skipped_sets} set{'s' if skipped_sets != 1 else ''} with no imported member snapshot.",
                "warning",
            )
    return redirect(
        _safe_redirect_target(request.form.get("next")) or url_for("catalog.sets_list")
    )


@catalog_bp.route("/sets/<int:set_id>/delete", methods=["POST"])
@catalog_bp.route("/sets/<int:sid>/delete", methods=["POST"])
def set_delete(set_id=None, sid=None):
    """Delete a set."""
    set_id = set_id if set_id is not None else sid
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.sets_list"))
    item_set = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    name = item_set.name
    db.session.delete(item_set)
    if db_commit(db.session):
        logger.info("Set deleted: %s", name)
        flash(f'Deleted set "{name}".', "info")
    return redirect(url_for("catalog.sets_list"))


@catalog_bp.route("/sets/<int:set_id>")
@catalog_bp.route("/sets/<int:sid>")
def set_detail(set_id=None, sid=None):
    """Render a set detail page."""
    set_id = set_id if set_id is not None else sid
    from models import Ownership, Person

    item_set = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    private_view = is_authenticated_user()
    all_persons = Person.query.order_by(Person.name).all() if private_view else []
    person_id = request.args.get("person", type=int) if private_view else None
    person = db.session.get(Person, person_id) if person_id else None
    sort_field = (request.args.get("sort", "name") or "name").strip().lower()
    direction = (request.args.get("dir", "asc") or "asc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "asc"
    if sort_field not in {"name", "sku", "category", "edge", "msrp", "wishlist"}:
        sort_field = "name"

    # Split items into owned vs. missing for the selected person (or globally)
    owned_ownerships = []
    owned_item_ids: set[int] = set()
    if private_view:
        owned_q = Ownership.query.filter_by(status="Owned")
        if person_id:
            owned_q = owned_q.filter_by(person_id=person_id)
        owned_ownerships = [
            ownership
            for ownership in owned_q.all()
            if ownership.variant is not None and ownership.variant.item is not None
        ]
        owned_item_ids = {ownership.variant.item_id for ownership in owned_ownerships}

    wishlisted_item_ids: set[int] = set()
    if private_view and person_id:
        wishlisted_item_ids = {
            ownership.variant.item_id
            for ownership in Ownership.query.filter_by(
                person_id=person_id, status="Wishlist"
            ).all()
            if ownership.variant is not None and ownership.variant.item is not None
        }

    catalog_sku_lookup = {
        sku: item
        for item in Item.query.filter(Item.sku.isnot(None)).all()
        if (sku := _normalize_member_sku(item.sku))
    }
    member_snapshot_rows, not_in_catalog_skus = _build_member_status_rows(
        _load_member_snapshot(item_set.member_data),
        catalog_sku_lookup,
    )

    top_colors = []
    if private_view:
        color_counts: dict[str, int] = {}
        for ownership in owned_ownerships:
            color = ownership.variant.color or UNKNOWN_COLOR
            if color == UNKNOWN_COLOR:
                continue
            color_counts[color] = color_counts.get(color, 0) + 1
        top_colors = [
            {"color": color, "count": count}
            for color, count in sorted(
                color_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())
            )[:8]
        ]

    def _sort_items(items: list[Item]) -> list[Item]:
        if sort_field == "msrp":
            if direction == "desc":
                return sorted(
                    items, key=lambda item: (item.msrp is None, -(item.msrp or 0))
                )
            return sorted(items, key=lambda item: (item.msrp is None, item.msrp or 0))
        if sort_field == "wishlist":
            return sorted(
                items,
                key=lambda item: item.id in wishlisted_item_ids,
                reverse=(direction == "desc"),
            )

        key_map = {
            "name": lambda item: (item.name or "").lower(),
            "sku": lambda item: (item.sku or "").lower(),
            "category": lambda item: (item.category or "").lower(),
            "edge": lambda item: (item.edge_type or "").lower(),
        }
        key_fn = key_map.get(sort_field, key_map["name"])
        return sorted(items, key=key_fn, reverse=(direction == "desc"))

    owned_items = (
        _sort_items([item for item in item_set.items if item.id in owned_item_ids])
        if private_view
        else []
    )
    missing_items = (
        _sort_items([item for item in item_set.items if item.id not in owned_item_ids])
        if private_view
        else []
    )

    total = len(item_set.items)
    owned_count = len(owned_items)
    pct = round(100 * owned_count / total) if total else 0

    qty_map = {
        membership.item_id: membership.quantity for membership in item_set.members
    }
    next_target = _safe_redirect_target(request.args.get("next"))

    return render_template(
        "set_detail.html",
        set=item_set,
        owned_items=owned_items,
        missing_items=missing_items,
        owned_count=owned_count,
        total=total,
        pct=pct,
        all_persons=all_persons,
        person_id=person_id,
        person=person,
        sort=sort_field,
        direction=direction,
        wishlisted_item_ids=wishlisted_item_ids,
        qty_map=qty_map,
        member_snapshot_rows=member_snapshot_rows,
        not_in_catalog_skus=not_in_catalog_skus,
        top_colors=top_colors,
        next_target=next_target,
        can_restore_memberships=bool(item_set.member_data) and private_view,
        SINGLE_VARIANT_CATEGORIES=VARIANT_SYNC_SINGLE_VARIANT_CATEGORIES,
        edgeless_categories=EDGELESS_CATEGORIES,
        UNKNOWN_COLOR=UNKNOWN_COLOR,
    )


# ── Uses Sync ─────────────────────────────────────────────────────────────────


@catalog_bp.route("/catalog/sync-uses", methods=["POST"])
@admin_required
def catalog_sync_uses():
    """Scrape Cutco.com uses for every cataloged item and populate item_tasks."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    items_with_url = Item.query.filter(Item.cutco_url.isnot(None)).all()
    if not items_with_url:
        flash("No catalog items have a Cutco URL — run a catalog sync first.", "info")
        return redirect(url_for("logs.tasks_manage"))

    # Fetch uses pages in parallel (no DB work in threads)
    item_uses: dict[int, list[str]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_map = {
            pool.submit(scrape_item_uses, item.cutco_url): item.id
            for item in items_with_url
        }
        for future in as_completed(future_map):
            uses = future.result()
            if uses:
                item_uses[future_map[future]] = uses

    # Apply results in main thread
    item_lookup = {item.id: item for item in items_with_url}
    tasks_added = 0
    links_added = 0

    for item_id, uses in item_uses.items():
        item = item_lookup[item_id]
        existing_task_ids = {task.id for task in item.suggested_tasks}
        for use_text in uses:
            task = KnifeTask.query.filter(
                db.func.lower(KnifeTask.name) == use_text.lower()
            ).first()
            if not task:
                task = KnifeTask(name=use_text, is_preset=False)
                db.session.add(task)
                db.session.flush()
                tasks_added += 1
            if task.id not in existing_task_ids:
                item.suggested_tasks.append(task)
                existing_task_ids.add(task.id)
                links_added += 1

    db_commit(db.session)
    logger.info(
        "Uses sync: %d items, %d new tasks, %d links",
        len(item_uses),
        tasks_added,
        links_added,
    )
    flash(
        f"Uses sync complete — {len(item_uses)} items processed, "
        f"{tasks_added} new task{'s' if tasks_added != 1 else ''}, "
        f"{links_added} link{'s' if links_added != 1 else ''} added.",
        "success",
    )
    return redirect(url_for("logs.tasks_manage"))


# ── Catalog Sync ──────────────────────────────────────────────────────────────


@catalog_bp.route("/catalog/sync")
@admin_required
def catalog_sync():
    """Render the catalog sync preview page."""
    _sync_catalog_sync_helpers()
    start_requested = request.args.get("run") == "1"
    job = _read_catalog_sync_job()

    if start_requested:
        if job.get("status") != "running":
            _reset_catalog_sync_job()
            _write_catalog_sync_job(
                {
                    "status": "running",
                    "progress": ["Preparing catalog sync…"],
                    "results": None,
                    "error": None,
                    "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "finished_at": None,
                    "preview": None,
                    "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            _start_catalog_sync_background_job(_current_flask_app())
        job = _read_catalog_sync_job()
        if job.get("status") == "done" and job.get("preview"):
            preview = job["preview"]
            return render_template(
                "sync_preview.html",
                job=job,
                edgeless_categories=EDGELESS_CATEGORIES,
                **preview,
            )
        if job.get("status") == "error":
            flash(
                f"Catalog sync failed: {job.get('error') or 'Unknown error'}", "error"
            )
        return render_template(
            "sync_preview.html",
            job=job,
            grouped={},
            scraped_items=[],
            new_items=[],
            scraped_total=0,
            new_sets=[],
            existing_sets_data=[],
            changed_existing_sets_data=[],
            scraped_sets_total=0,
            has_missing_set_members=False,
            blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES),
            edgeless_categories=EDGELESS_CATEGORIES,
        )

    if job.get("status") == "done" and job.get("preview"):
        return render_template(
            "sync_preview.html",
            job=job,
            edgeless_categories=EDGELESS_CATEGORIES,
            **job["preview"],
        )
    if job.get("status") == "running":
        return render_template(
            "sync_preview.html",
            job=job,
            grouped={},
            scraped_items=[],
            new_items=[],
            scraped_total=0,
            new_sets=[],
            existing_sets_data=[],
            changed_existing_sets_data=[],
            scraped_sets_total=0,
            has_missing_set_members=False,
            blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES),
            edgeless_categories=EDGELESS_CATEGORIES,
        )
    if job.get("status") == "error":
        flash(f"Catalog sync failed: {job.get('error') or 'Unknown error'}", "error")
        return render_template(
            "sync_preview.html",
            job=job,
            grouped={},
            scraped_items=[],
            new_items=[],
            scraped_total=0,
            new_sets=[],
            existing_sets_data=[],
            changed_existing_sets_data=[],
            scraped_sets_total=0,
            has_missing_set_members=False,
            blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES),
            edgeless_categories=EDGELESS_CATEGORIES,
        )

    return render_template(
        "sync_preview.html",
        job=job,
        grouped={},
        scraped_items=[],
        new_items=[],
        scraped_total=0,
        new_sets=[],
        existing_sets_data=[],
        changed_existing_sets_data=[],
        scraped_sets_total=0,
        has_missing_set_members=False,
        blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES),
        edgeless_categories=EDGELESS_CATEGORIES,
    )


@catalog_bp.route("/catalog/sync/status")
@admin_required
def catalog_sync_status():
    """Return the current catalog sync job state."""
    return jsonify(_read_catalog_sync_job())


@catalog_bp.route("/catalog/sync/confirm", methods=["POST"])
@admin_required
def catalog_sync_confirm():
    """Apply a catalog sync preview."""
    selected = set(request.form.getlist("selected_skus"))
    item_data = {}
    for key, val in request.form.items():
        for prefix in (
            "name_",
            "category_",
            "url_",
            "edge_type_",
            "msrp_",
            "blade_length_",
            "overall_length_",
            "weight_",
            "variant_colors_",
            "item_unicorn_",
        ):
            if key.startswith(prefix):
                sku = key[len(prefix) :]
                item_data.setdefault(sku, {})[prefix.rstrip("_")] = val

    added_items = 0
    detected_variant_color_total = 0
    reconciled_item_variants = 0

    def _add_catalog_sync_variants(
        item: Item, raw_variant_colors: object
    ) -> tuple[int, int]:
        variant_colors: list[str] = []
        if isinstance(raw_variant_colors, str) and raw_variant_colors:
            try:
                parsed_colors = json.loads(raw_variant_colors)
            except json.JSONDecodeError:
                parsed_colors = []
            if isinstance(parsed_colors, list):
                variant_colors = [
                    str(color).strip()
                    for color in parsed_colors
                    if str(color).strip() and str(color).strip() != UNKNOWN_COLOR
                ]

        detected = len(variant_colors)
        if not variant_colors:
            return 0, 0

        existing_colors = {
            existing_variant.color.lower()
            for existing_variant in item.variants
            if existing_variant.color != UNKNOWN_COLOR
        }
        seen_colors: set[str] = set()
        created = 0
        for color in variant_colors:
            color_key = color.lower()
            if color_key in seen_colors or color_key in existing_colors:
                continue
            seen_colors.add(color_key)
            db.session.add(ItemVariant(item=item, color=color, source="catalog_sync"))
            created += 1

        if created:
            db.session.flush()
            reconcile_unknown_variant(item)
        return detected, created

    for sku in selected:
        if Item.query.filter_by(sku=sku).first():
            continue
        data = item_data.get(sku, {})
        try:
            msrp = float(data["msrp"]) if data.get("msrp") else None
        except ValueError:
            msrp = None
        is_limited_edition = data.get("item_unicorn") == "on"
        availability = "non-catalog" if is_limited_edition else "public"
        item = Item(
            name=data.get("name", sku),
            sku=sku,
            category=canonicalize_category(data.get("category")),
            cutco_url=data.get("url"),
            availability=availability,
            in_catalog=availability == "public",
            set_only=False,
            is_unicorn=is_limited_edition,
            edge_is_unicorn=normalize_edge_for_category(
                canonicalize_category(data.get("category")),
                data.get("edge_type"),
                False,
            )[1],
            edge_type=normalize_edge_for_category(
                canonicalize_category(data.get("category")),
                data.get("edge_type"),
            )[0],
            msrp=msrp,
            blade_length=data.get("blade_length") or None,
            overall_length=data.get("overall_length") or None,
            weight=data.get("weight") or None,
        )
        db.session.add(item)
        db.session.flush()
        detected, created = _add_catalog_sync_variants(item, data.get("variant_colors"))
        detected_variant_color_total += detected
        reconciled_item_variants += created
        added_items += 1

    db.session.flush()

    for sku, data in item_data.items():
        if sku in selected:
            continue
        item = Item.query.filter_by(sku=sku).first()
        if not item:
            continue
        raw_variant_colors = data.get("variant_colors")
        if isinstance(raw_variant_colors, str) and raw_variant_colors:
            try:
                parsed_colors = json.loads(raw_variant_colors)
            except json.JSONDecodeError:
                parsed_colors = []
            if isinstance(parsed_colors, list):
                detected_variant_color_total += sum(
                    1
                    for color in parsed_colors
                    if str(color).strip() and str(color).strip() != UNKNOWN_COLOR
                )

    selected_sets = set(request.form.getlist("selected_sets"))
    added_sets = 0
    linked_items = 0
    created_missing_items = 0
    create_missing_set_members = request.form.get("create_missing_set_members") == "on"

    sku_to_item = {
        item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()
    }
    name_to_item = _build_member_name_lookup(
        Item.query.filter(Item.sku.isnot(None)).all()
    )

    set_count = int(request.form.get("set_count", 0))
    for index in range(set_count):
        set_name = request.form.get(f"set_name_{index}", "").strip()
        if not set_name or set_name not in selected_sets:
            continue
        member_entries_raw = request.form.get(f"set_member_entries_{index}", "").strip()
        member_entries = (
            _load_member_snapshot(member_entries_raw) if member_entries_raw else []
        )
        if not member_entries:
            legacy_member_skus = [
                raw.strip()
                for raw in request.form.get(f"set_members_{index}", "").split("|")
                if raw.strip()
            ]
            legacy_member_qtys = {}
            for raw_pair in request.form.get(f"set_member_qtys_{index}", "").split("|"):
                if ":" in raw_pair:
                    sku_part, qty_part = raw_pair.split(":", 1)
                    try:
                        legacy_member_qtys[sku_part.strip()] = int(qty_part.strip())
                    except ValueError:
                        pass
            member_entries = [
                {"sku": sku, "quantity": legacy_member_qtys.get(sku, 1), "name": None}
                for sku in legacy_member_skus
            ]
        set_sku = request.form.get(f"set_sku_{index}", "").strip() or None

        pre_existing_set = Set.query.filter(
            db.func.lower(Set.name) == set_name.lower()
        ).first()
        item_set = get_or_create_set(set_name)
        if pre_existing_set is None:
            added_sets += 1
        if set_sku and not item_set.sku:
            item_set.sku = set_sku
        if member_entries_raw:
            item_set.member_data = json.dumps(member_entries, ensure_ascii=False)

        existing_members = {
            membership.item_id: membership for membership in item_set.members
        }
        incoming_member_ids: set[int] = set()
        resolved_members, created_now = _aggregate_resolved_members(
            member_entries,
            sku_to_item,
            name_to_item,
            set_sku=set_sku,
            create_missing=create_missing_set_members,
            set_name=set_name if create_missing_set_members else None,
        )
        created_missing_items += created_now
        for item_id, resolved in resolved_members.items():
            item = resolved["item"]
            qty = _coerce_quantity(resolved.get("quantity"))
            if item_id not in existing_members:
                membership = ItemSetMember(
                    set_id=item_set.id, item_id=item.id, quantity=qty
                )
                db.session.add(membership)
                existing_members[item_id] = membership
                linked_items += 1
            else:
                existing_members[item_id].quantity = qty
            incoming_member_ids.add(item_id)
        if incoming_member_ids:
            for membership in list(item_set.members):
                if membership.item_id not in incoming_member_ids:
                    db.session.delete(membership)
        elif member_entries_raw:
            logger.warning(
                "Skipping set membership reconciliation for %s because no members were resolved",
                set_name,
            )

    # Update quantities on existing sets (no new rows, just qty backfill)
    existing_set_count = int(request.form.get("existing_set_count", 0))
    qty_updates = 0
    created_existing_missing_items = 0
    for index in range(existing_set_count):
        set_name = request.form.get(f"existing_set_name_{index}", "").strip()
        if not set_name:
            continue
        item_set = Set.query.filter(db.func.lower(Set.name) == set_name.lower()).first()
        if not item_set:
            continue
        member_entries_raw = request.form.get(
            f"existing_set_member_entries_{index}", ""
        ).strip()
        member_entries = (
            _load_member_snapshot(member_entries_raw) if member_entries_raw else []
        )
        if not member_entries:
            legacy_member_skus = [
                raw.strip()
                for raw in request.form.get(
                    f"existing_set_member_skus_{index}", ""
                ).split("|")
                if raw.strip()
            ]
            legacy_member_qtys = {}
            for raw_pair in request.form.get(
                f"existing_set_member_qtys_{index}", ""
            ).split("|"):
                if ":" in raw_pair:
                    sku_part, qty_part = raw_pair.split(":", 1)
                    try:
                        legacy_member_qtys[sku_part.strip()] = int(qty_part.strip())
                    except ValueError:
                        pass
            member_entries = [
                {"sku": sku, "quantity": legacy_member_qtys.get(sku, 1), "name": None}
                for sku in legacy_member_skus
            ]
        if member_entries_raw:
            item_set.member_data = json.dumps(member_entries, ensure_ascii=False)

        set_sku = _normalize_member_sku(item_set.sku)
        existing_member_ids = {member.item_id for member in item_set.members}
        incoming_member_ids: set[int] = set()
        resolved_members, created_now = _aggregate_resolved_members(
            member_entries,
            sku_to_item,
            name_to_item,
            set_sku=set_sku,
            create_missing=create_missing_set_members,
            set_name=set_name if create_missing_set_members else None,
        )
        created_existing_missing_items += created_now
        for item_id, resolved in resolved_members.items():
            item = resolved["item"]
            qty = _coerce_quantity(resolved.get("quantity"))
            if item_id not in existing_member_ids:
                db.session.add(
                    ItemSetMember(set_id=item_set.id, item_id=item.id, quantity=qty)
                )
                existing_member_ids.add(item_id)
            else:
                membership = next(
                    (
                        member
                        for member in item_set.members
                        if member.item_id == item_id
                    ),
                    None,
                )
                if membership and membership.quantity != qty:
                    membership.quantity = qty
                    qty_updates += 1
            incoming_member_ids.add(item_id)
        if incoming_member_ids:
            for membership in list(item_set.members):
                if membership.item_id not in incoming_member_ids:
                    db.session.delete(membership)
        elif member_entries_raw:
            logger.warning(
                "Skipping existing set membership reconciliation for %s because no members were resolved",
                set_name,
            )

    db_commit(db.session)
    logger.info(
        "Sync complete: %d items, %d sets, %d memberships, %d qty updates, %d placeholders, %d variants",
        added_items,
        added_sets,
        linked_items,
        qty_updates,
        created_missing_items + created_existing_missing_items,
        reconciled_item_variants,
    )
    record_activity(
        "sync",
        "Catalog sync complete",
        f"Added {added_items} items, {added_sets} sets, {linked_items} memberships, {qty_updates} quantity updates, {created_missing_items + created_existing_missing_items} placeholder items, {reconciled_item_variants} variant updates.",
    )
    db.session.commit()

    parts = []
    if added_items:
        parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
    if added_sets:
        parts.append(f"{added_sets} set{'s' if added_sets != 1 else ''}")
    if linked_items:
        parts.append(f"{linked_items} set membership{'s' if linked_items != 1 else ''}")
    placeholder_items = created_missing_items + created_existing_missing_items
    if placeholder_items:
        parts.append(
            f"{placeholder_items} placeholder item{'s' if placeholder_items != 1 else ''}"
        )
    flash(
        "Sync complete — added " + (", ".join(parts) if parts else "nothing new") + ".",
        "success",
    )
    if detected_variant_color_total:
        flash(
            f"Detected {detected_variant_color_total} variant color{'s' if detected_variant_color_total != 1 else ''} in the catalog scrape.",
            "info",
        )
    return redirect(url_for("catalog.catalog"))

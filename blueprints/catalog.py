import json
import logging
import re
from collections import OrderedDict
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from constants import (
    AVAILABILITY_CHOICES, COOKWARE_CATEGORIES, EDGE_TYPES,
    SYNC_BLOCKED_CATEGORIES, UNKNOWN_COLOR, canonicalize_category,
    canonicalize_availability,
)
from extensions import db
from helpers import admin_required, db_commit, is_admin
from models import (
    Item,
    ItemSetMember,
    ItemVariant,
    KnifeTask,
    Ownership,
    Set,
    normalize_sku_value,
    parse_alternate_skus,
    get_or_create_set,
    record_activity,
    reconcile_unknown_variant,
)
from scraping import _member_hover_title, scrape_catalog, scrape_item_specs, scrape_item_uses, scrape_sets

catalog_bp = Blueprint("catalog", __name__)
logger = logging.getLogger(__name__)
UNCATEGORIZED_FILTER = "__uncategorized__"


def _safe_redirect_target(target: str | None) -> str | None:
    if not target:
        return None
    target = target.strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _item_alternate_skus_text(item: Item | None) -> str:
    if not item or not item.alternate_skus:
        return ""
    return ", ".join(parse_alternate_skus(item.alternate_skus))


def _normalize_member_sku(value: str | None) -> str | None:
    sku = (str(value).strip().upper() if value is not None else "")
    return sku or None


def _normalize_member_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _get_item_field(item: object, field: str) -> object | None:
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
    entry: dict,
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


def _load_member_snapshot(raw_member_data: str | None) -> list[dict]:
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
    rows: list[dict] = []
    seen: OrderedDict[str, dict] = OrderedDict()
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
                existing["quantity"] = max(1, int(existing.get("quantity") or 1)) + quantity
                if not existing.get("name") and name:
                    existing["name"] = name
            continue
        rows.append({"sku": sku, "name": name, "quantity": quantity})
    rows[:0] = list(seen.values())
    return rows


def _build_member_status_rows(
    member_entries: list[dict],
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
        item = _resolve_member_item(entry, catalog_sku_lookup, catalog_name_lookup, set_sku=set_sku)
        quantity = entry.get("quantity", 1)
        try:
            quantity = max(1, int(quantity))
        except (TypeError, ValueError):
            quantity = 1
        if item is not None:
            status = "present"
            status_label = "In catalog"
        elif sku and sku in found_skus:
            status = "found"
            status_label = "Found in scrape"
        elif sku:
            status = "missing"
            status_label = "Missing from catalog"
            missing_skus.append(sku)
        else:
            status = "no_sku"
            status_label = "No item number"
        rows.append({
            "index": index,
            "sku": sku,
            "name": entry.get("name") or None,
            "hover_title": _member_hover_title(
                _get_item_field(item, "name") if item is not None else entry.get("name")
            ),
            "quantity": quantity,
            "status": status,
            "status_label": status_label,
            "matched_item": item,
        })
    return rows, missing_skus


def _create_missing_set_member_item(member: dict, set_name: str) -> Item:
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
    member_entries: list[dict],
    sku_to_item: dict[str, Item],
    name_to_item: dict[str, object],
    *,
    set_sku: str | None = None,
    create_missing: bool = False,
    set_name: str | None = None,
) -> tuple[OrderedDict[int, dict[str, object]], int]:
    resolved_members: OrderedDict[int, dict[str, object]] = OrderedDict()
    created_missing_items = 0
    for member in member_entries:
        resolved_item = _resolve_member_item(member, sku_to_item, name_to_item, set_sku=set_sku)
        member_sku = _normalize_member_sku(getattr(resolved_item, "sku", None) if resolved_item else member.get("sku"))
        if not member_sku:
            continue
        item = sku_to_item.get(member_sku.upper())
        if resolved_item is not None and getattr(resolved_item, "sku", None):
            item = resolved_item
        if not item and create_missing and set_name:
            item = _create_missing_set_member_item(member, set_name)
            sku_to_item[item.sku.upper()] = item
            name_to_item[_normalize_member_name(item.name)] = item
            created_missing_items += 1
        if not item:
            continue
        try:
            quantity = max(1, int(member.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        aggregated = resolved_members.get(item.id)
        if aggregated is None:
            resolved_members[item.id] = {"item": item, "quantity": quantity}
        else:
            aggregated["quantity"] = max(1, int(aggregated.get("quantity") or 1)) + quantity
    return resolved_members, created_missing_items


@catalog_bp.route("/catalog")
def catalog():
    search_query = request.args.get("q", "").strip()
    cat_filter = request.args.get("category", "")
    unicorn_f  = request.args.get("unicorn", "")
    status_f   = request.args.get("status", "")
    sort       = request.args.get("sort", "name")
    direction  = request.args.get("dir", "asc")

    query = Item.query
    if search_query:
        query = query.filter(
            db.or_(Item.name.ilike(f"%{search_query}%"), Item.sku.ilike(f"%{search_query}%")))
    if cat_filter:
        if cat_filter == UNCATEGORIZED_FILTER:
            query = query.filter(db.or_(Item.category.is_(None), Item.category == ""))
        else:
            query = query.filter(Item.category == cat_filter)
    if unicorn_f == "1":
        query = query.filter(db.or_(
            Item.is_unicorn,
            Item.edge_is_unicorn,
            Item.variants.any(ItemVariant.is_unicorn == True)  # noqa: E712
        ))
    if status_f == "set_only":
        query = query.filter(Item.set_only.is_(True))
    elif status_f == "off_catalog":
        query = query.filter(Item.set_only.is_(False), Item.in_catalog.is_(False))
    elif status_f == "non_catalog":
        query = query.filter(Item.in_catalog.is_(False))

    from sqlalchemy.orm import selectinload
    sort_cols = {"name": Item.name, "sku": Item.sku, "category": Item.category, "edge_type": Item.edge_type}
    col = sort_cols.get(sort, Item.name)
    if sort == "category":
        category_col = db.func.lower(db.func.coalesce(Item.category, ""))
        name_col = db.func.lower(db.func.coalesce(Item.name, ""))
        items = (query
                 .options(selectinload(Item.variants), selectinload(Item.sets))
                 .order_by(
                     category_col.desc() if direction == "desc" else category_col,
                     name_col.desc() if direction == "desc" else name_col,
                 )
                 .all())
    elif sort == "edge_type":
        edge_col = db.func.lower(db.func.coalesce(Item.edge_type, ""))
        name_col = db.func.lower(db.func.coalesce(Item.name, ""))
        items = (query
                 .options(selectinload(Item.variants), selectinload(Item.sets))
                 .order_by(
                     edge_col.desc() if direction == "desc" else edge_col,
                     name_col.desc() if direction == "desc" else name_col,
                 )
                 .all())
    elif sort == "variants":
        variant_count = db.func.count(ItemVariant.id)
        name_col = db.func.lower(db.func.coalesce(Item.name, ""))
        items = (query
                 .outerjoin(Item.variants)
                 .options(selectinload(Item.variants), selectinload(Item.sets))
                 .group_by(Item.id)
                 .order_by(
                     variant_count.desc() if direction == "desc" else variant_count,
                     name_col.desc() if direction == "desc" else name_col,
                 )
                 .all())
    else:
        items = (query
                 .options(selectinload(Item.variants), selectinload(Item.sets))
                 .order_by(col.desc() if direction == "desc" else col)
                 .all())

    categories = [row[0] for row in
                  db.session.query(Item.category)
                  .filter(Item.category.isnot(None))
                  .distinct().order_by(Item.category).all()]
    referenced_item_ids = {ownership.variant.item_id for ownership in Ownership.query.all()}
    unreferenced_count = Item.query.filter(~Item.id.in_(referenced_item_ids)).count() if referenced_item_ids else Item.query.count()
    all_item_count = Item.query.count()
    set_count = Set.query.count()

    return render_template("catalog.html", items=items, categories=categories,
                           q=search_query, cat_filter=cat_filter, unicorn_f=unicorn_f,
                           status_f=status_f,
                           sort=sort, direction=direction,
                           edge_types=EDGE_TYPES,
                           UNCATEGORIZED_FILTER=UNCATEGORIZED_FILTER,
                           COOKWARE_CATEGORIES=COOKWARE_CATEGORIES,
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           unreferenced_count=unreferenced_count,
                           all_item_count=all_item_count,
                           set_count=set_count)


@catalog_bp.route("/catalog/add", methods=["GET", "POST"])
@admin_required
def catalog_add():
    next_target = request.form.get("next", "") if request.method == "POST" else request.args.get("next", "")
    if request.method == "POST":
        availability = canonicalize_availability(request.form.get("availability", "public"))
        item = Item(
            name       = request.form["name"].strip(),
            sku        = normalize_sku_value(request.form.get("sku", "")),
            alternate_skus = ", ".join(parse_alternate_skus(request.form.get("alternate_skus", ""))) or None,
            category   = canonicalize_category(request.form.get("category", "")),
            availability = availability,
            edge_type  = request.form.get("edge_type", "Unknown"),
            is_unicorn = request.form.get("is_unicorn") == "on",
            edge_is_unicorn = request.form.get("edge_is_unicorn") == "on",
            set_only   = request.form.get("set_only") == "on",
            in_catalog = availability == "public" and request.form.get("set_only") != "on",
            cutco_url  = request.form.get("cutco_url", "").strip() or None,
            notes      = request.form.get("notes", "").strip() or None,
        )
        db.session.add(item)
        db.session.flush()
        colors = [raw_color.strip() for raw_color in request.form.get("colors", "").split(",") if raw_color.strip()]
        for color in colors:
            if color != UNKNOWN_COLOR and (item.category or "") not in COOKWARE_CATEGORIES:
                db.session.add(ItemVariant(item_id=item.id, color=color))
        db.session.flush()
        reconcile_unknown_variant(item)
        if db_commit(db.session):
            logger.info("Item added: %s (SKU: %s)", item.name, item.sku or "none")
            flash(f'Added "{item.name}" to catalog.', "success")
        return redirect(_safe_redirect_target(next_target) or url_for("catalog.catalog"))

    return render_template("item_form.html", item=None,
                           edge_types=EDGE_TYPES, action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all(),
                           categories=_catalog_category_options(),
                           availability_choices=AVAILABILITY_CHOICES,
                           alternate_skus_text="",
                           next_target=_safe_redirect_target(next_target))


@catalog_bp.route("/catalog/<int:item_id>/edit", methods=["GET", "POST"])
@admin_required
def catalog_edit(item_id):
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    next_target = request.form.get("next", "") if request.method == "POST" else request.args.get("next", "")
    if request.method == "POST":
        availability = canonicalize_availability(request.form.get("availability", "public"))
        item.name       = request.form["name"].strip()
        item.sku        = normalize_sku_value(request.form.get("sku", ""))
        item.alternate_skus = ", ".join(parse_alternate_skus(request.form.get("alternate_skus", ""))) or None
        item.category   = canonicalize_category(request.form.get("category", ""))
        item.availability = availability
        item.edge_type  = request.form.get("edge_type", "Unknown")
        item.is_unicorn = request.form.get("is_unicorn") == "on"
        item.edge_is_unicorn = request.form.get("edge_is_unicorn") == "on"
        item.set_only   = request.form.get("set_only") == "on"
        item.in_catalog = availability == "public" and request.form.get("set_only") != "on"
        item.cutco_url  = request.form.get("cutco_url", "").strip() or None
        item.notes      = request.form.get("notes", "").strip() or None

        selected_set_ids: set[int] = set()
        invalid_set_id_seen = False
        for set_id_str in request.form.getlist("set_ids"):
            try:
                selected_set_ids.add(int(set_id_str))
            except (TypeError, ValueError):
                invalid_set_id_seen = True
        if invalid_set_id_seen:
            flash("Some set selections were invalid and were ignored.", "warning")
        current_memberships = {membership.set_id: membership for membership in item.set_memberships}
        selected_sets = {
            selected_set.id: selected_set
            for selected_set in Set.query.filter(Set.id.in_(selected_set_ids)).all()
        } if selected_set_ids else {}

        for existing_set_id, existing_member in list(current_memberships.items()):
            if existing_set_id not in selected_sets:
                db.session.delete(existing_member)

        for set_id in selected_sets:
            if set_id not in current_memberships:
                db.session.add(ItemSetMember(item_id=item.id, set_id=set_id, quantity=1))

        if db_commit(db.session):
            logger.info("Item updated: %s (SKU: %s)", item.name, item.sku or "none")
            flash(f'Updated "{item.name}".', "success")
        return redirect(_safe_redirect_target(next_target) or url_for("catalog.catalog"))

    return render_template("item_form.html", item=item,
                           edge_types=EDGE_TYPES, action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all(),
                           categories=_catalog_category_options(),
                           availability_choices=AVAILABILITY_CHOICES,
                           alternate_skus_text=_item_alternate_skus_text(item),
                           next_target=_safe_redirect_target(next_target))


@catalog_bp.route("/catalog/purge-unreferenced", methods=["POST"])
def catalog_purge_unreferenced():
    """Delete catalog items that have no ownership records."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    referenced_item_ids = {ownership.variant.item_id for ownership in Ownership.query.all()}
    items = Item.query.filter(~Item.id.in_(referenced_item_ids)).all() if referenced_item_ids else Item.query.all()
    count = len(items)
    for item in items:
        db.session.delete(item)
    if db_commit(db.session):
        logger.info("Purged %d unreferenced catalog items", count)
        flash(f"Removed {count} item{'s' if count != 1 else ''} with no ownership records.", "info")
    return redirect(url_for("catalog.catalog"))


@catalog_bp.route("/catalog/purge-all", methods=["POST"])
def catalog_purge_all():
    """Delete the entire catalog including sets, ownership, logs, and variants."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    count = Item.query.count()
    set_count = Set.query.count()
    Item.query.delete()
    Set.query.delete()
    if db_commit(db.session):
        logger.info("Full catalog purge: %d items deleted, %d sets deleted", count, set_count)
        flash(
            f"Catalog purged — {count} item{'s' if count != 1 else ''} and "
            f"{set_count} set{'s' if set_count != 1 else ''} deleted, plus related records.",
            "info",
        )
    return redirect(url_for("catalog.catalog"))


@catalog_bp.route("/catalog/<int:item_id>/delete", methods=["POST"])
def catalog_delete(item_id):
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    name = item.name
    db.session.delete(item)
    if db_commit(db.session):
        logger.info("Item deleted: %s", name)
        flash(f'Deleted "{name}".', "info")
    return redirect(url_for("catalog.catalog"))


# ── Variants ──────────────────────────────────────────────────────────────────

@catalog_bp.route("/catalog/<int:item_id>/variants")
def variants(item_id):
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    is_cookware = (item.category or "") in COOKWARE_CATEGORIES
    return render_template("variants.html", item=item, UNKNOWN_COLOR=UNKNOWN_COLOR, is_cookware=is_cookware)


@catalog_bp.route("/catalog/<int:item_id>/variants/add", methods=["POST"])
@admin_required
def variant_add(item_id):
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    color = request.form.get("color", "").strip()
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    if (item.category or "") in COOKWARE_CATEGORIES and color != UNKNOWN_COLOR:
        flash("Cookware items use a single Unknown variant; color variants are not supported.", "warning")
        return redirect(url_for("catalog.variants", item_id=item_id))
    if any(existing_variant.color.lower() == color.lower() for existing_variant in item.variants):
        flash(f'"{color}" already exists for this item.', "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    db.session.add(ItemVariant(item_id=item_id, color=color,
                               notes=request.form.get("notes", "").strip() or None))
    db.session.flush()
    reconcile_unknown_variant(item)
    if db_commit(db.session):
        logger.info("Variant added: %s → %s", item.name, color)
        flash(f'Added variant "{color}".', "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/edit", methods=["POST"])
@admin_required
def variant_edit(vid):
    variant = db.session.get(ItemVariant, vid)
    if not variant:
        abort(404)
    item_id     = variant.item_id
    color   = request.form.get("color", "").strip()
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    if (variant.item.category or "") in COOKWARE_CATEGORIES and color != UNKNOWN_COLOR:
        flash("Cookware items use a single Unknown variant; color variants are not supported.", "warning")
        return redirect(url_for("catalog.variants", item_id=item_id))
    variant.color      = color
    variant.notes      = request.form.get("notes", "").strip() or None
    variant.is_unicorn = request.form.get("is_unicorn") == "on"
    db.session.flush()
    reconcile_unknown_variant(variant.item)
    if db_commit(db.session):
        logger.info("Variant updated: item %d → %s", item_id, color)
        flash(f'Updated to "{color}".', "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/delete", methods=["POST"])
@admin_required
def variant_delete(vid):
    variant = db.session.get(ItemVariant, vid)
    if not variant:
        abort(404)
    if len(variant.item.variants) == 1:
        flash("Cannot delete the only variant. Add another first.", "error")
        return redirect(url_for("catalog.variants", item_id=variant.item_id))
    item_id = variant.item_id
    item = variant.item
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
    from models import Ownership, Person
    all_sets    = Set.query.order_by(Set.name).all()
    all_persons = Person.query.order_by(Person.name).all()
    person_id   = request.args.get("person", type=int)
    not_in_catalog_f = request.args.get("missing", "")
    incomplete_f = request.args.get("incomplete", "")

    # Completion relative to selected person, or globally if none selected
    owned_q = Ownership.query.filter_by(status="Owned")
    if person_id:
        owned_q = owned_q.filter_by(person_id=person_id)
    owned_item_ids = {
        ownership.variant.item_id
        for ownership in owned_q.all()
        if ownership.variant is not None and ownership.variant.item is not None
    }

    catalog_sku_lookup = {
        _normalize_member_sku(item.sku): item
        for item in Item.query.filter(Item.sku.isnot(None)).all()
        if _normalize_member_sku(item.sku)
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

    return render_template("sets.html", sets=filtered_sets, completion=completion,
                           not_in_catalog_counts=not_in_catalog_counts,
                           all_persons=all_persons, person_id=person_id,
                           not_in_catalog_f=not_in_catalog_f, incomplete_f=incomplete_f)


@catalog_bp.route("/sets/add", methods=["GET", "POST"])
@admin_required
def set_add():
    next_target = request.form.get("next", "") if request.method == "POST" else request.args.get("next", "")
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
        return redirect(_safe_redirect_target(next_target) or url_for("catalog.sets_list"))
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
    set_id = set_id if set_id is not None else sid
    item_set = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    next_target = request.form.get("next", "") if request.method == "POST" else request.args.get("next", "")
    all_items = Item.query.order_by(Item.name).all()
    member_qty_map = {member.item_id: member.quantity for member in item_set.members}

    if request.method == "POST":
        item_set.name  = request.form["name"].strip()
        item_set.sku   = request.form.get("sku", "").strip().upper() or None
        item_set.notes = request.form.get("notes", "").strip() or None

        selected_item_ids: set[int] = set()
        for raw_item_id in request.form.getlist("member_item_ids"):
            try:
                selected_item_ids.add(int(raw_item_id))
            except (TypeError, ValueError):
                continue

        valid_item_ids = {
            item_id for (item_id,) in db.session.query(Item.id).filter(Item.id.in_(selected_item_ids)).all()
        } if selected_item_ids else set()

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
                db.session.add(ItemSetMember(set_id=item_set.id, item_id=item_id, quantity=qty))

        if db_commit(db.session):
            logger.info("Set updated: %s", item_set.name)
            flash(f'Updated set "{item_set.name}".', "success")
        return redirect(_safe_redirect_target(next_target) or url_for("catalog.sets_list"))
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
    member_entries: list[dict],
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
        qty = max(1, int(resolved.get("quantity") or 1))
        membership = existing_members.get(item_id)
        if membership is None:
            db.session.add(ItemSetMember(set_id=item_set.id, item_id=item.id, quantity=qty))
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
    set_id = set_id if set_id is not None else sid
    item_set = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    restored, updated, matched = _restore_set_memberships_from_snapshot(item_set)
    if not matched:
        flash(f'No catalog items could be matched for "{item_set.name}".', "warning")
        return redirect(url_for("catalog.set_detail", set_id=item_set.id))

    if db_commit(db.session):
        logger.info("Set memberships restored: %s (+%d memberships, %d updated)", item_set.name, restored, updated)
        flash(f'Restored {restored} membership(s) for "{item_set.name}".', "success")
    return redirect(url_for("catalog.set_detail", set_id=item_set.id))


@catalog_bp.route("/sets/bulk-resync-memberships", methods=["POST"])
@admin_required
def bulk_resync_set_memberships():
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

    scraped_lookup = {scraped_set["name"].lower(): scraped_set for scraped_set in scraped_sets if scraped_set.get("name")}
    restored_sets = 0
    restored_memberships = 0
    updated_memberships = 0
    skipped_sets = 0
    for item_set in item_sets.values():
        scraped_set = scraped_lookup.get(item_set.name.lower())
        if not scraped_set:
            skipped_sets += 1
            continue
        member_entries = scraped_set.get("member_entries") or []
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
    return redirect(_safe_redirect_target(request.form.get("next")) or url_for("catalog.sets_list"))


@catalog_bp.route("/sets/bulk-restore-memberships", methods=["POST"])
@admin_required
def bulk_restore_set_memberships():
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
    return redirect(_safe_redirect_target(request.form.get("next")) or url_for("catalog.sets_list"))


@catalog_bp.route("/sets/<int:set_id>/delete", methods=["POST"])
@catalog_bp.route("/sets/<int:sid>/delete", methods=["POST"])
def set_delete(set_id=None, sid=None):
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
    set_id = set_id if set_id is not None else sid
    from models import Ownership, Person
    item_set    = db.session.get(Set, set_id)
    if not item_set:
        abort(404)
    all_persons = Person.query.order_by(Person.name).all()
    person_id   = request.args.get("person", type=int)
    person      = db.session.get(Person, person_id) if person_id else None
    sort_field  = (request.args.get("sort", "name") or "name").strip().lower()
    direction   = (request.args.get("dir", "asc") or "asc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "asc"
    if sort_field not in {"name", "sku", "category", "edge", "msrp", "wishlist"}:
        sort_field = "name"

    # Split items into owned vs. missing for the selected person (or globally)
    owned_q = Ownership.query.filter_by(status="Owned")
    if person_id:
        owned_q = owned_q.filter_by(person_id=person_id)
    owned_item_ids = {
        ownership.variant.item_id
        for ownership in owned_q.all()
        if ownership.variant is not None and ownership.variant.item is not None
    }

    wishlisted_item_ids: set[int] = set()
    if person_id:
        wishlisted_item_ids = {
            ownership.variant.item_id
            for ownership in Ownership.query.filter_by(person_id=person_id, status="Wishlist").all()
            if ownership.variant is not None and ownership.variant.item is not None
        }

    catalog_sku_lookup = {
        _normalize_member_sku(item.sku): item
        for item in Item.query.filter(Item.sku.isnot(None)).all()
        if _normalize_member_sku(item.sku)
    }
    member_snapshot_rows, not_in_catalog_skus = _build_member_status_rows(
        _load_member_snapshot(item_set.member_data),
        catalog_sku_lookup,
    )

    def _sort_items(items: list[Item]) -> list[Item]:
        if sort_field == "msrp":
            if direction == "desc":
                return sorted(items, key=lambda item: (item.msrp is None, -(item.msrp or 0)))
            return sorted(items, key=lambda item: (item.msrp is None, item.msrp or 0))
        if sort_field == "wishlist":
            return sorted(
                items,
                key=lambda item: (item.id in wishlisted_item_ids),
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

    owned_items   = _sort_items([item for item in item_set.items if item.id in owned_item_ids])
    missing_items = _sort_items([item for item in item_set.items if item.id not in owned_item_ids])

    total = len(item_set.items)
    owned_count = len(owned_items)
    pct = round(100 * owned_count / total) if total else 0

    qty_map = {membership.item_id: membership.quantity for membership in item_set.members}
    next_target = _safe_redirect_target(request.args.get("next"))

    return render_template("set_detail.html",
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
                           next_target=next_target,
                           can_restore_memberships=bool(item_set.member_data),
                           COOKWARE_CATEGORIES=COOKWARE_CATEGORIES,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


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
    item_lookup  = {item.id: item for item in items_with_url}
    tasks_added  = 0
    links_added  = 0

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
    logger.info("Uses sync: %d items, %d new tasks, %d links", len(item_uses), tasks_added, links_added)
    flash(
        f"Uses sync complete — {len(item_uses)} items processed, "
        f"{tasks_added} new task{'s' if tasks_added != 1 else ''}, "
        f"{links_added} link{'s' if links_added != 1 else ''} added.",
        "success",
    )
    return redirect(url_for("logs.tasks_manage"))


# ── Catalog Sync ──────────────────────────────────────────────────────────────

@catalog_bp.route("/catalog/sync")
def catalog_sync():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))

    try:
        scraped, set_candidates = scrape_catalog()
    except Exception as exc:
        logger.error("Catalog scrape failed: %s", exc)
        flash("Could not reach cutco.com — try again later.", "error")
        return redirect(url_for("catalog.catalog"))

    existing_skus = {item.sku for item in Item.query.filter(Item.sku.isnot(None)).all()}
    new_items = [scraped_item for scraped_item in scraped if scraped_item["sku"] not in existing_skus]

    _grouped_unsorted: dict = {}
    for item in new_items:
        _grouped_unsorted.setdefault(item["category"], []).append(item)

    def _sku_sort_key(item):
        sku = item.get("sku") or ""
        sku_num_match = re.match(r"(\d+)", sku)
        return (0, int(sku_num_match.group(1)), sku) if sku_num_match else (1, 0, sku)

    grouped = OrderedDict(
        (cat, sorted(items, key=_sku_sort_key))
        for cat, items in sorted(_grouped_unsorted.items(), key=lambda kv: kv[0].lower())
    )

    try:
        scraped_sets = scrape_sets(extra_candidates=set_candidates)
    except Exception as exc:
        logger.error("Sets scrape failed: %s", exc)
        scraped_sets = []

    # Fetch specs (edge, msrp, lengths, weight) for new items in parallel
    if new_items:
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        _specs_map: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            _future_map = {pool.submit(scrape_item_specs, item_data["url"]): item_data["sku"] for item_data in new_items}
            for future in _as_completed(_future_map):
                _specs_map[_future_map[future]] = future.result()
        for item in new_items:
            specs = _specs_map.get(item["sku"], {})
            item["edge_type"]      = specs.get("edge_type", "Unknown")
            item["msrp"]           = specs.get("msrp")
            item["blade_length"]   = specs.get("blade_length")
            item["overall_length"] = specs.get("overall_length")
            item["weight"]         = specs.get("weight")

    existing_sets = {item_set.name.lower() for item_set in Set.query.all()}
    new_sets      = sorted(
        (scraped_set for scraped_set in scraped_sets if scraped_set["name"].lower() not in existing_sets),
        key=_sku_sort_key,
    )
    # Pass existing sets too so confirm can update member quantities
    existing_sets_data = [scraped_set for scraped_set in scraped_sets if scraped_set["name"].lower() in existing_sets]

    catalog_sku_lookup = {
        _normalize_member_sku(item.sku): item
        for item in Item.query.filter(Item.sku.isnot(None)).all()
        if _normalize_member_sku(item.sku)
    }
    preview_name_lookup = _build_member_name_lookup([
        *Item.query.filter(Item.sku.isnot(None)).all(),
        *new_items,
    ])
    scraped_sku_lookup = {
        _normalize_member_sku(item["sku"])
        for item in new_items
        if _normalize_member_sku(item.get("sku"))
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

    return render_template("sync_preview.html",
                           grouped=grouped,
                           new_items=new_items,
                           scraped_total=len(scraped),
                           new_sets=new_sets,
                           existing_sets_data=existing_sets_data,
                           scraped_sets_total=len(scraped_sets),
                           blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES))


@catalog_bp.route("/catalog/sync/confirm", methods=["POST"])
def catalog_sync_confirm():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))

    selected = set(request.form.getlist("selected_skus"))
    item_data = {}
    for key, val in request.form.items():
        for prefix in ("name_", "category_", "url_", "edge_type_",
                       "msrp_", "blade_length_", "overall_length_", "weight_"):
            if key.startswith(prefix):
                sku = key[len(prefix):]
                item_data.setdefault(sku, {})[prefix.rstrip("_")] = val

    added_items = 0
    for sku in selected:
        if Item.query.filter_by(sku=sku).first():
            continue
        data = item_data.get(sku, {})
        try:
            msrp = float(data["msrp"]) if data.get("msrp") else None
        except ValueError:
            msrp = None
        item = Item(name=data.get("name", sku), sku=sku,
                    category=canonicalize_category(data.get("category")), cutco_url=data.get("url"),
                    availability="public", in_catalog=True, set_only=False, is_unicorn=False, edge_is_unicorn=False,
                    edge_type=data.get("edge_type") or "Unknown",
                    msrp=msrp,
                    blade_length=data.get("blade_length") or None,
                    overall_length=data.get("overall_length") or None,
                    weight=data.get("weight") or None)
        db.session.add(item)
        db.session.flush()
        reconcile_unknown_variant(item)
        added_items += 1

    db.session.flush()

    selected_sets = set(request.form.getlist("selected_sets"))
    added_sets    = 0
    linked_items  = 0
    created_missing_items = 0
    create_missing_set_members = request.form.get("create_missing_set_members") == "on"

    sku_to_item = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}
    name_to_item = _build_member_name_lookup(Item.query.filter(Item.sku.isnot(None)).all())

    set_count = int(request.form.get("set_count", 0))
    for index in range(set_count):
        set_name = request.form.get(f"set_name_{index}", "").strip()
        if not set_name or set_name not in selected_sets:
            continue
        member_entries_raw = request.form.get(f"set_member_entries_{index}", "").strip()
        member_entries = _load_member_snapshot(member_entries_raw) if member_entries_raw else []
        if not member_entries:
            legacy_member_skus = [raw.strip() for raw in
                                  request.form.get(f"set_members_{index}", "").split("|") if raw.strip()]
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

        pre_existing_set = Set.query.filter(db.func.lower(Set.name) == set_name.lower()).first()
        item_set = get_or_create_set(set_name)
        if pre_existing_set is None:
            added_sets += 1
        if set_sku and not item_set.sku:
            item_set.sku = set_sku
        if member_entries_raw:
            item_set.member_data = json.dumps(member_entries, ensure_ascii=False)

        existing_members = {membership.item_id: membership for membership in item_set.members}
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
            qty = max(1, int(resolved.get("quantity") or 1))
            if item_id not in existing_members:
                membership = ItemSetMember(set_id=item_set.id, item_id=item.id, quantity=qty)
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
            logger.warning("Skipping set membership reconciliation for %s because no members were resolved",
                           set_name)

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
        member_entries_raw = request.form.get(f"existing_set_member_entries_{index}", "").strip()
        member_entries = _load_member_snapshot(member_entries_raw) if member_entries_raw else []
        if not member_entries:
            legacy_member_skus = [raw.strip() for raw in
                                  request.form.get(f"existing_set_member_skus_{index}", "").split("|") if raw.strip()]
            legacy_member_qtys = {}
            for raw_pair in request.form.get(f"existing_set_member_qtys_{index}", "").split("|"):
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

        member_qtys = {
            _normalize_member_sku(member.get("sku")): max(1, int(member.get("quantity") or 1))
            for member in member_entries
            if _normalize_member_sku(member.get("sku"))
        }
        incoming_member_ids: set[int] = set()
        for member in item_set.members:
            item = db.session.get(Item, member.item_id)
            if item and item.sku:
                new_qty = member_qtys.get(item.sku.upper(), 1)
                if member.quantity != new_qty:
                    member.quantity = new_qty
                    qty_updates += 1
        if create_missing_set_members and member_entries:
            existing_member_ids = {member.item_id for member in item_set.members}
            resolved_members, created_now = _aggregate_resolved_members(
                member_entries,
                sku_to_item,
                name_to_item,
                set_sku=set_sku,
                create_missing=True,
                set_name=set_name,
            )
            created_existing_missing_items += created_now
            for item_id, resolved in resolved_members.items():
                item = resolved["item"]
                qty = max(1, int(resolved.get("quantity") or 1))
                if item_id not in existing_member_ids:
                    db.session.add(ItemSetMember(set_id=item_set.id, item_id=item.id, quantity=qty))
                    existing_member_ids.add(item_id)
                else:
                    membership = next((member for member in item_set.members if member.item_id == item_id), None)
                    if membership and membership.quantity != qty:
                        membership.quantity = qty
                        qty_updates += 1
                incoming_member_ids.add(item_id)
        if incoming_member_ids:
            for membership in list(item_set.members):
                if membership.item_id not in incoming_member_ids:
                    db.session.delete(membership)
        elif member_entries_raw:
            logger.warning("Skipping existing set membership reconciliation for %s because no members were resolved",
                           set_name)

    db_commit(db.session)
    logger.info("Sync complete: %d items, %d sets, %d memberships, %d qty updates, %d placeholders",
                added_items, added_sets, linked_items, qty_updates, created_missing_items + created_existing_missing_items)
    record_activity(
        "sync",
        "Catalog sync complete",
        f"Added {added_items} items, {added_sets} sets, {linked_items} memberships, {qty_updates} quantity updates, {created_missing_items + created_existing_missing_items} placeholder items.",
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
        parts.append(f"{placeholder_items} placeholder item{'s' if placeholder_items != 1 else ''}")
    flash("Sync complete — added " + (", ".join(parts) if parts else "nothing new") + ".", "success")
    return redirect(url_for("catalog.catalog"))

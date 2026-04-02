import re
from collections import OrderedDict

from flask import Blueprint, flash, redirect, render_template, request, url_for

from constants import EDGE_TYPES, SYNC_BLOCKED_CATEGORIES, UNKNOWN_COLOR
from extensions import db
from helpers import is_admin
from models import Item, ItemVariant, Set, ensure_unknown_variant, get_or_create_set
from scraping import scrape_catalog, scrape_sets

catalog_bp = Blueprint("catalog", __name__)


@catalog_bp.route("/catalog")
def catalog():
    search_query = request.args.get("q", "").strip()
    cat_filter = request.args.get("category", "")
    unicorn_f  = request.args.get("unicorn", "")
    sort       = request.args.get("sort", "name")
    direction  = request.args.get("dir", "asc")

    query = Item.query
    if search_query:
        query = query.filter(
            db.or_(Item.name.ilike(f"%{search_query}%"), Item.sku.ilike(f"%{search_query}%")))
    if cat_filter:
        query = query.filter(Item.category == cat_filter)
    if unicorn_f == "1":
        query = query.filter(db.or_(
            Item.is_unicorn,
            Item.variants.any(ItemVariant.is_unicorn == True)  # noqa: E712
        ))

    from sqlalchemy.orm import selectinload
    col   = getattr(Item, sort, Item.name)
    items = (query
             .options(selectinload(Item.variants), selectinload(Item.sets))
             .order_by(col.desc() if direction == "desc" else col)
             .all())

    categories = [row[0] for row in
                  db.session.query(Item.category)
                  .filter(Item.category.isnot(None))
                  .distinct().order_by(Item.category).all()]

    return render_template("catalog.html", items=items, categories=categories,
                           q=search_query, cat_filter=cat_filter, unicorn_f=unicorn_f,
                           sort=sort, direction=direction,
                           edge_types=EDGE_TYPES,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@catalog_bp.route("/catalog/add", methods=["GET", "POST"])
def catalog_add():
    if request.method == "POST":
        item = Item(
            name       = request.form["name"].strip(),
            sku        = request.form.get("sku", "").strip().upper() or None,
            category   = request.form.get("category", "").strip() or None,
            edge_type  = request.form.get("edge_type", "Unknown"),
            is_unicorn = request.form.get("is_unicorn") == "on",
            in_catalog = request.form.get("in_catalog") == "on",
            cutco_url  = request.form.get("cutco_url", "").strip() or None,
            notes      = request.form.get("notes", "").strip() or None,
        )
        db.session.add(item)
        db.session.flush()
        ensure_unknown_variant(item)
        colors = [c.strip() for c in request.form.get("colors", "").split(",") if c.strip()]
        for color in colors:
            if color != UNKNOWN_COLOR:
                db.session.add(ItemVariant(item_id=item.id, color=color))
        db.session.commit()
        flash(f'Added "{item.name}" to catalog.', "success")
        return redirect(url_for("catalog.catalog"))

    return render_template("item_form.html", item=None,
                           edge_types=EDGE_TYPES, action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all())


@catalog_bp.route("/catalog/<int:iid>/edit", methods=["GET", "POST"])
def catalog_edit(iid):
    item = Item.query.get_or_404(iid)
    if request.method == "POST":
        item.name       = request.form["name"].strip()
        item.sku        = request.form.get("sku", "").strip().upper() or None
        item.category   = request.form.get("category", "").strip() or None
        item.edge_type  = request.form.get("edge_type", "Unknown")
        item.is_unicorn = request.form.get("is_unicorn") == "on"
        item.in_catalog = request.form.get("in_catalog") == "on"
        item.cutco_url  = request.form.get("cutco_url", "").strip() or None
        item.notes      = request.form.get("notes", "").strip() or None

        selected_set_ids = set(int(set_id_str) for set_id_str in request.form.getlist("set_ids"))
        item.sets = Set.query.filter(Set.id.in_(selected_set_ids)).all()

        db.session.commit()
        flash(f'Updated "{item.name}".', "success")
        return redirect(url_for("catalog.catalog"))

    return render_template("item_form.html", item=item,
                           edge_types=EDGE_TYPES, action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all())


@catalog_bp.route("/catalog/<int:iid>/delete", methods=["POST"])
def catalog_delete(iid):
    item = Item.query.get_or_404(iid)
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f'Deleted "{name}".', "info")
    return redirect(url_for("catalog.catalog"))


# ── Variants ──────────────────────────────────────────────────────────────────

@catalog_bp.route("/catalog/<int:iid>/variants")
def variants(iid):
    item = Item.query.get_or_404(iid)
    return render_template("variants.html", item=item, UNKNOWN_COLOR=UNKNOWN_COLOR)


@catalog_bp.route("/catalog/<int:iid>/variants/add", methods=["POST"])
def variant_add(iid):
    item = Item.query.get_or_404(iid)
    color = request.form.get("color", "").strip()
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("catalog.variants", iid=iid))
    if any(v.color.lower() == color.lower() for v in item.variants):
        flash(f'"{color}" already exists for this item.', "error")
        return redirect(url_for("catalog.variants", iid=iid))
    db.session.add(ItemVariant(item_id=iid, color=color,
                               notes=request.form.get("notes", "").strip() or None))
    db.session.commit()
    flash(f'Added variant "{color}".', "success")
    return redirect(url_for("catalog.variants", iid=iid))


@catalog_bp.route("/variants/<int:vid>/edit", methods=["POST"])
def variant_edit(vid):
    variant = ItemVariant.query.get_or_404(vid)
    iid     = variant.item_id
    color   = request.form.get("color", "").strip()
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("catalog.variants", iid=iid))
    variant.color      = color
    variant.notes      = request.form.get("notes", "").strip() or None
    variant.is_unicorn = request.form.get("is_unicorn") == "on"
    db.session.commit()
    flash(f'Updated to "{color}".', "success")
    return redirect(url_for("catalog.variants", iid=iid))


@catalog_bp.route("/variants/<int:vid>/delete", methods=["POST"])
def variant_delete(vid):
    variant = ItemVariant.query.get_or_404(vid)
    if len(variant.item.variants) == 1:
        flash("Cannot delete the only variant. Add another first.", "error")
        return redirect(url_for("catalog.variants", iid=variant.item_id))
    iid = variant.item_id
    db.session.delete(variant)
    db.session.commit()
    flash("Variant removed.", "info")
    return redirect(url_for("catalog.variants", iid=iid))


# ── Sets ──────────────────────────────────────────────────────────────────────

@catalog_bp.route("/sets")
def sets_list():
    all_sets = Set.query.order_by(Set.name).all()
    return render_template("sets.html", sets=all_sets)


@catalog_bp.route("/sets/add", methods=["GET", "POST"])
def set_add():
    if request.method == "POST":
        name = request.form["name"].strip()
        if Set.query.filter(db.func.lower(Set.name) == name.lower()).first():
            flash(f'Set "{name}" already exists.', "error")
            return redirect(url_for("catalog.set_add"))
        item_set = Set(name=name, notes=request.form.get("notes", "").strip() or None)
        db.session.add(item_set)
        db.session.commit()
        flash(f'Created set "{name}".', "success")
        return redirect(url_for("catalog.sets_list"))
    return render_template("set_form.html", set=None, action="Add")


@catalog_bp.route("/sets/<int:sid>/edit", methods=["GET", "POST"])
def set_edit(sid):
    item_set = Set.query.get_or_404(sid)
    if request.method == "POST":
        item_set.name  = request.form["name"].strip()
        item_set.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash(f'Updated set "{item_set.name}".', "success")
        return redirect(url_for("catalog.sets_list"))
    return render_template("set_form.html", set=item_set, action="Edit")


@catalog_bp.route("/sets/<int:sid>/delete", methods=["POST"])
def set_delete(sid):
    item_set = Set.query.get_or_404(sid)
    name = item_set.name
    db.session.delete(item_set)
    db.session.commit()
    flash(f'Deleted set "{name}".', "info")
    return redirect(url_for("catalog.sets_list"))


@catalog_bp.route("/sets/<int:sid>")
def set_detail(sid):
    item_set = Set.query.get_or_404(sid)
    return render_template("set_detail.html", set=item_set, UNKNOWN_COLOR=UNKNOWN_COLOR)


# ── Catalog Sync ──────────────────────────────────────────────────────────────

@catalog_bp.route("/catalog/sync")
def catalog_sync():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))

    scraped, set_candidates = scrape_catalog()
    existing_skus = {item.sku for item in Item.query.filter(Item.sku.isnot(None)).all()}
    new_items = [i for i in scraped if i["sku"] not in existing_skus]

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

    scraped_sets  = scrape_sets(extra_candidates=set_candidates)
    existing_sets = {s.name.lower() for s in Set.query.all()}
    new_sets      = sorted(
        (s for s in scraped_sets if s["name"].lower() not in existing_sets),
        key=_sku_sort_key,
    )

    return render_template("sync_preview.html",
                           grouped=grouped,
                           new_items=new_items,
                           scraped_total=len(scraped),
                           new_sets=new_sets,
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
        for prefix in ("name_", "category_", "url_"):
            if key.startswith(prefix):
                sku = key[len(prefix):]
                item_data.setdefault(sku, {})[prefix.rstrip("_")] = val

    added_items = 0
    for sku in selected:
        if Item.query.filter_by(sku=sku).first():
            continue
        data = item_data.get(sku, {})
        item = Item(name=data.get("name", sku), sku=sku,
                    category=data.get("category"), cutco_url=data.get("url"),
                    in_catalog=True, is_unicorn=False, edge_type="Unknown")
        db.session.add(item)
        db.session.flush()
        ensure_unknown_variant(item)
        added_items += 1

    db.session.flush()

    selected_sets = set(request.form.getlist("selected_sets"))
    added_sets    = 0
    linked_items  = 0

    sku_to_item = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}

    set_count = int(request.form.get("set_count", 0))
    for i in range(set_count):
        set_name = request.form.get(f"set_name_{i}", "").strip()
        if not set_name or set_name not in selected_sets:
            continue
        member_skus = [raw.strip() for raw in
                       request.form.get(f"set_members_{i}", "").split("|") if raw.strip()]
        set_sku = request.form.get(f"set_sku_{i}", "").strip() or None

        item_set = get_or_create_set(set_name)
        if item_set.id is None:
            added_sets += 1
        if set_sku and not item_set.sku:
            item_set.sku = set_sku

        for msku in member_skus:
            item = sku_to_item.get(msku.upper())
            if item and item not in item_set.items:
                item_set.items.append(item)
                linked_items += 1

    db.session.commit()

    parts = []
    if added_items:
        parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
    if added_sets:
        parts.append(f"{added_sets} set{'s' if added_sets != 1 else ''}")
    if linked_items:
        parts.append(f"{linked_items} set membership{'s' if linked_items != 1 else ''}")
    flash("Sync complete — added " + (", ".join(parts) if parts else "nothing new") + ".", "success")
    return redirect(url_for("catalog.catalog"))

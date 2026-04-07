import logging
import re
from collections import OrderedDict

from flask import Blueprint, flash, redirect, render_template, request, url_for

from constants import EDGE_TYPES, SYNC_BLOCKED_CATEGORIES, UNKNOWN_COLOR
from extensions import db
from helpers import admin_required, db_commit, is_admin
from models import Item, ItemSetMember, ItemVariant, KnifeTask, Ownership, Set, ensure_unknown_variant, get_or_create_set
from scraping import scrape_catalog, scrape_item_specs, scrape_item_uses, scrape_sets

catalog_bp = Blueprint("catalog", __name__)
logger = logging.getLogger(__name__)


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
    sort_cols = {"name": Item.name, "sku": Item.sku, "category": Item.category, "edge_type": Item.edge_type}
    col   = sort_cols.get(sort, Item.name)
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
@admin_required
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
        if db_commit(db.session):
            logger.info("Item added: %s (SKU: %s)", item.name, item.sku or "none")
            flash(f'Added "{item.name}" to catalog.', "success")
        return redirect(url_for("catalog.catalog"))

    return render_template("item_form.html", item=None,
                           edge_types=EDGE_TYPES, action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all())


@catalog_bp.route("/catalog/<int:item_id>/edit", methods=["GET", "POST"])
@admin_required
def catalog_edit(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == "POST":
        item.name       = request.form["name"].strip()
        item.sku        = request.form.get("sku", "").strip().upper() or None
        item.category   = request.form.get("category", "").strip() or None
        item.edge_type  = request.form.get("edge_type", "Unknown")
        item.is_unicorn = request.form.get("is_unicorn") == "on"
        item.in_catalog = request.form.get("in_catalog") == "on"
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
        item.sets = Set.query.filter(Set.id.in_(selected_set_ids)).all() if selected_set_ids else []

        if db_commit(db.session):
            logger.info("Item updated: %s (SKU: %s)", item.name, item.sku or "none")
            flash(f'Updated "{item.name}".', "success")
        return redirect(url_for("catalog.catalog"))

    return render_template("item_form.html", item=item,
                           edge_types=EDGE_TYPES, action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all())


@catalog_bp.route("/catalog/purge-unreferenced", methods=["POST"])
def catalog_purge_unreferenced():
    """Delete catalog items that have no ownership records."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.catalog"))
    referenced_item_ids = {o.variant.item_id for o in Ownership.query.all()}
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
    item = Item.query.get_or_404(item_id)
    name = item.name
    db.session.delete(item)
    if db_commit(db.session):
        logger.info("Item deleted: %s", name)
        flash(f'Deleted "{name}".', "info")
    return redirect(url_for("catalog.catalog"))


# ── Variants ──────────────────────────────────────────────────────────────────

@catalog_bp.route("/catalog/<int:item_id>/variants")
def variants(item_id):
    item = Item.query.get_or_404(item_id)
    return render_template("variants.html", item=item, UNKNOWN_COLOR=UNKNOWN_COLOR)


@catalog_bp.route("/catalog/<int:item_id>/variants/add", methods=["POST"])
@admin_required
def variant_add(item_id):
    item = Item.query.get_or_404(item_id)
    color = request.form.get("color", "").strip()
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    if any(v.color.lower() == color.lower() for v in item.variants):
        flash(f'"{color}" already exists for this item.', "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    db.session.add(ItemVariant(item_id=item_id, color=color,
                               notes=request.form.get("notes", "").strip() or None))
    if db_commit(db.session):
        logger.info("Variant added: %s → %s", item.name, color)
        flash(f'Added variant "{color}".', "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/edit", methods=["POST"])
@admin_required
def variant_edit(vid):
    variant = ItemVariant.query.get_or_404(vid)
    item_id     = variant.item_id
    color   = request.form.get("color", "").strip()
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("catalog.variants", item_id=item_id))
    variant.color      = color
    variant.notes      = request.form.get("notes", "").strip() or None
    variant.is_unicorn = request.form.get("is_unicorn") == "on"
    if db_commit(db.session):
        logger.info("Variant updated: item %d → %s", item_id, color)
        flash(f'Updated to "{color}".', "success")
    return redirect(url_for("catalog.variants", item_id=item_id))


@catalog_bp.route("/variants/<int:vid>/delete", methods=["POST"])
@admin_required
def variant_delete(vid):
    variant = ItemVariant.query.get_or_404(vid)
    if len(variant.item.variants) == 1:
        flash("Cannot delete the only variant. Add another first.", "error")
        return redirect(url_for("catalog.variants", item_id=variant.item_id))
    item_id = variant.item_id
    db.session.delete(variant)
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

    # Completion relative to selected person, or globally if none selected
    owned_q = Ownership.query.filter_by(status="Owned")
    if person_id:
        owned_q = owned_q.filter_by(person_id=person_id)
    owned_item_ids = {
        o.variant.item_id
        for o in owned_q.all()
        if o.variant is not None and o.variant.item is not None
    }

    completion = {}
    for s in all_sets:
        total = len(s.items)
        owned = sum(1 for item in s.items if item.id in owned_item_ids)
        completion[s.id] = dict(total=total, owned=owned,
                                pct=round(100 * owned / total) if total else 0)

    return render_template("sets.html", sets=all_sets, completion=completion,
                           all_persons=all_persons, person_id=person_id)


@catalog_bp.route("/sets/add", methods=["GET", "POST"])
@admin_required
def set_add():
    if request.method == "POST":
        name = request.form["name"].strip()
        if Set.query.filter(db.func.lower(Set.name) == name.lower()).first():
            flash(f'Set "{name}" already exists.', "error")
            return redirect(url_for("catalog.set_add"))
        item_set = Set(name=name, notes=request.form.get("notes", "").strip() or None)
        db.session.add(item_set)
        if db_commit(db.session):
            logger.info("Set created: %s", name)
            flash(f'Created set "{name}".', "success")
        return redirect(url_for("catalog.sets_list"))
    return render_template("set_form.html", set=None, action="Add")


@catalog_bp.route("/sets/<int:set_id>/edit", methods=["GET", "POST"])
@catalog_bp.route("/sets/<int:sid>/edit", methods=["GET", "POST"])
@admin_required
def set_edit(set_id=None, sid=None):
    set_id = set_id if set_id is not None else sid
    item_set = Set.query.get_or_404(set_id)
    if request.method == "POST":
        item_set.name  = request.form["name"].strip()
        item_set.notes = request.form.get("notes", "").strip() or None
        if db_commit(db.session):
            logger.info("Set updated: %s", item_set.name)
            flash(f'Updated set "{item_set.name}".', "success")
        return redirect(url_for("catalog.sets_list"))
    return render_template("set_form.html", set=item_set, action="Edit")


@catalog_bp.route("/sets/<int:set_id>/delete", methods=["POST"])
@catalog_bp.route("/sets/<int:sid>/delete", methods=["POST"])
def set_delete(set_id=None, sid=None):
    set_id = set_id if set_id is not None else sid
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog.sets_list"))
    item_set = Set.query.get_or_404(set_id)
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
    item_set    = Set.query.get_or_404(set_id)
    all_persons = Person.query.order_by(Person.name).all()
    person_id   = request.args.get("person", type=int)
    person      = Person.query.get(person_id) if person_id else None

    # Split items into owned vs. missing for the selected person (or globally)
    owned_q = Ownership.query.filter_by(status="Owned")
    if person_id:
        owned_q = owned_q.filter_by(person_id=person_id)
    owned_item_ids = {
        o.variant.item_id
        for o in owned_q.all()
        if o.variant is not None and o.variant.item is not None
    }

    owned_items   = sorted([i for i in item_set.items if i.id in owned_item_ids],
                           key=lambda i: i.name)
    missing_items = sorted([i for i in item_set.items if i.id not in owned_item_ids],
                           key=lambda i: i.name)

    total = len(item_set.items)
    owned_count = len(owned_items)
    pct = round(100 * owned_count / total) if total else 0

    qty_map = {m.item_id: m.quantity for m in item_set.members}

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
                           qty_map=qty_map,
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
        existing_task_ids = {t.id for t in item.suggested_tasks}
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
            _future_map = {pool.submit(scrape_item_specs, i["url"]): i["sku"] for i in new_items}
            for _fut in _as_completed(_future_map):
                _specs_map[_future_map[_fut]] = _fut.result()
        for item in new_items:
            specs = _specs_map.get(item["sku"], {})
            item["edge_type"]      = specs.get("edge_type", "Unknown")
            item["msrp"]           = specs.get("msrp")
            item["blade_length"]   = specs.get("blade_length")
            item["overall_length"] = specs.get("overall_length")
            item["weight"]         = specs.get("weight")

    existing_sets = {s.name.lower() for s in Set.query.all()}
    new_sets      = sorted(
        (s for s in scraped_sets if s["name"].lower() not in existing_sets),
        key=_sku_sort_key,
    )
    # Pass existing sets too so confirm can update member quantities
    existing_sets_data = [s for s in scraped_sets if s["name"].lower() in existing_sets]

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
                    category=data.get("category"), cutco_url=data.get("url"),
                    in_catalog=True, is_unicorn=False,
                    edge_type=data.get("edge_type") or "Unknown",
                    msrp=msrp,
                    blade_length=data.get("blade_length") or None,
                    overall_length=data.get("overall_length") or None,
                    weight=data.get("weight") or None)
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
        member_qtys = {}
        for raw_pair in request.form.get(f"set_member_qtys_{i}", "").split("|"):
            if ":" in raw_pair:
                sku_part, qty_part = raw_pair.split(":", 1)
                try:
                    member_qtys[sku_part.strip()] = int(qty_part.strip())
                except ValueError:
                    pass
        set_sku = request.form.get(f"set_sku_{i}", "").strip() or None

        pre_existing_set = Set.query.filter(db.func.lower(Set.name) == set_name.lower()).first()
        item_set = get_or_create_set(set_name)
        if pre_existing_set is None:
            added_sets += 1
        if set_sku and not item_set.sku:
            item_set.sku = set_sku

        existing_members = {m.item_id: m for m in item_set.members}
        for msku in member_skus:
            item = sku_to_item.get(msku.upper())
            if not item:
                continue
            qty = member_qtys.get(msku, 1)
            if item.id not in existing_members:
                db.session.add(ItemSetMember(set_id=item_set.id, item_id=item.id, quantity=qty))
                linked_items += 1
            else:
                # Update quantity if it changed
                existing_members[item.id].quantity = qty

    # Update quantities on existing sets (no new rows, just qty backfill)
    existing_set_count = int(request.form.get("existing_set_count", 0))
    qty_updates = 0
    for i in range(existing_set_count):
        set_name = request.form.get(f"existing_set_name_{i}", "").strip()
        if not set_name:
            continue
        item_set = Set.query.filter(db.func.lower(Set.name) == set_name.lower()).first()
        if not item_set:
            continue
        member_qtys = {}
        for raw_pair in request.form.get(f"existing_set_member_qtys_{i}", "").split("|"):
            if ":" in raw_pair:
                sku_part, qty_part = raw_pair.split(":", 1)
                try:
                    member_qtys[sku_part.strip()] = int(qty_part.strip())
                except ValueError:
                    pass
        for member in item_set.members:
            item = db.session.get(Item, member.item_id)
            if item and item.sku:
                new_qty = member_qtys.get(item.sku.upper(), 1)
                if member.quantity != new_qty:
                    member.quantity = new_qty
                    qty_updates += 1

    db_commit(db.session)
    logger.info("Sync complete: %d items, %d sets, %d memberships, %d qty updates",
                added_items, added_sets, linked_items, qty_updates)

    parts = []
    if added_items:
        parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
    if added_sets:
        parts.append(f"{added_sets} set{'s' if added_sets != 1 else ''}")
    if linked_items:
        parts.append(f"{linked_items} set membership{'s' if linked_items != 1 else ''}")
    flash("Sync complete — added " + (", ".join(parts) if parts else "nothing new") + ".", "success")
    return redirect(url_for("catalog.catalog"))

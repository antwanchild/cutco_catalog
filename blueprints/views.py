from flask import Blueprint, abort, render_template, request

from constants import STATUS_RANK, UNKNOWN_COLOR
from extensions import db
from helpers import (_collection_token, _gift_token,
                     _verify_collection_token, _verify_gift_token)
from models import Item, ItemVariant, KnifeTaskLog, Ownership, Person, Set, SharpeningLog

views_bp = Blueprint("views", __name__)


@views_bp.route("/views/item/<int:item_id>")
def item_owners(item_id):
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)

    entries = (Ownership.query
               .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
               .filter(ItemVariant.item_id == item_id)
               .order_by(Ownership.status).all())
    owner_ids      = {entry.person_id for entry in entries}
    people_without = (Person.query
                      .filter(~Person.id.in_(owner_ids))
                      .order_by(Person.name).all())

    sharpening = (SharpeningLog.query
                  .filter_by(item_id=item_id)
                  .order_by(SharpeningLog.sharpened_on.desc())
                  .all())

    task_log = (KnifeTaskLog.query
                .filter_by(item_id=item_id)
                .order_by(KnifeTaskLog.logged_on.desc())
                .all())

    # Task usage summary: task name → count
    task_counts: dict[str, int] = {}
    for entry in task_log:
        task_counts[entry.task.name] = task_counts.get(entry.task.name, 0) + 1
    top_tasks = sorted(task_counts.items(), key=lambda kv: kv[1], reverse=True)

    return render_template("item_owners.html", item=item,
                           entries=entries, people_without=people_without,
                           sharpening=sharpening, task_log=task_log,
                           top_tasks=top_tasks,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@views_bp.route("/views/matrix")
def matrix():
    people_list = Person.query.order_by(Person.name).all()
    items_list  = Item.query.order_by(Item.name).all()

    item_lookup = {}
    for ownership in Ownership.query.all():
        key     = (ownership.person_id, ownership.variant.item_id)
        current = item_lookup.get(key)
        if current is None or STATUS_RANK.get(ownership.status, 9) < STATUS_RANK.get(current.status, 9):
            item_lookup[key] = ownership

    variant_lookup = {(ownership.person_id, ownership.variant_id): ownership
                      for ownership in Ownership.query.all()}

    variants_by_item = {
        item.id: [variant for variant in item.variants if variant.color != UNKNOWN_COLOR] or item.variants
        for item in items_list
    }

    return render_template("matrix.html",
                           people=people_list,
                           items=items_list,
                           item_lookup=item_lookup,
                           variant_lookup=variant_lookup,
                           variants_by_item=variants_by_item,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@views_bp.route("/stats")
def stats():
    person_id   = request.args.get("person", type=int)
    people_list = Person.query.order_by(Person.name).all()

    owned_q = (
        db.session.query(Ownership)
        .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
        .join(Item, ItemVariant.item_id == Item.id)
        .filter(Ownership.status == "Owned")
    )
    if person_id:
        owned_q = owned_q.filter(Ownership.person_id == person_id)
    owned = owned_q.all()

    owned_item_map: dict[int, Item] = {}
    for ownership in owned:
        item = ownership.variant.item
        if item.id not in owned_item_map:
            owned_item_map[item.id] = item
    owned_items = list(owned_item_map.values())

    cat_counts: dict[str, int]   = {}
    cat_values: dict[str, float] = {}
    cat_catalog: dict[str, int]  = {}

    for item in Item.query.all():
        cat = item.category or "Uncategorized"
        cat_catalog[cat] = cat_catalog.get(cat, 0) + 1

    for item in owned_items:
        cat = item.category or "Uncategorized"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if item.msrp:
            cat_values[cat] = cat_values.get(cat, 0.0) + item.msrp

    color_counts: dict[str, int] = {}
    for ownership in owned:
        color = ownership.variant.color
        if color == UNKNOWN_COLOR:
            color = "Unknown"
        color_counts[color] = color_counts.get(color, 0) + 1

    edge_counts: dict[str, int] = {}
    for item in owned_items:
        edge = item.edge_type or "Unknown"
        edge_counts[edge] = edge_counts.get(edge, 0) + 1

    collector_rows = []
    for person in people_list:
        person_owned = Ownership.query.filter_by(person_id=person.id, status="Owned").all()
        person_item_ids = {ownership.variant.item_id for ownership in person_owned}
        person_items = Item.query.filter(Item.id.in_(person_item_ids)).all() if person_item_ids else []
        person_value = sum(item.msrp for item in person_items if item.msrp)
        collector_rows.append(dict(
            id=person.id, name=person.name,
            count=len(person_item_ids), value=person_value,
        ))
    collector_rows.sort(key=lambda row: row["count"], reverse=True)

    total_value  = sum(item.msrp for item in owned_items if item.msrp)
    priced_count = sum(1 for item in owned_items if item.msrp)
    catalog_total = Item.query.count()

    summary = dict(
        owned_items   = len(owned_items),
        owned_entries = len(owned),
        total_value   = total_value,
        priced_count  = priced_count,
        catalog_total = catalog_total,
        coverage_pct  = round(100 * len(owned_items) / catalog_total, 1) if catalog_total else 0,
    )

    cat_data   = sorted(cat_counts.items(),  key=lambda kv: kv[1], reverse=True)
    val_data   = sorted(cat_values.items(),  key=lambda kv: kv[1], reverse=True)
    color_data = sorted(color_counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
    edge_data  = sorted(edge_counts.items(), key=lambda kv: kv[1], reverse=True)

    cov_cats   = sorted(cat_catalog.keys())
    cov_owned  = [cat_counts.get(cat, 0)   for cat in cov_cats]
    cov_gap    = [cat_catalog.get(cat, 0) - cat_counts.get(cat, 0) for cat in cov_cats]

    return render_template(
        "stats.html",
        people=people_list,
        person_id=person_id,
        summary=summary,
        cat_data=cat_data,
        val_data=val_data,
        color_data=color_data,
        edge_data=edge_data,
        collector_rows=collector_rows,
        cov_cats=cov_cats,
        cov_owned=cov_owned,
        cov_gap=cov_gap,
    )


# ── Gift list ─────────────────────────────────────────────────────────────────

@views_bp.route("/sets/<int:set_id>/gift-token")
@views_bp.route("/sets/<int:sid>/gift-token")
def gift_token(set_id=None, sid=None):
    """Generate a shareable gift list token for a set + person combination."""
    set_id = set_id if set_id is not None else sid
    person_id = request.args.get("person", type=int)
    if not person_id:
        abort(400)
    # Validate both exist
    if not db.session.get(Set, set_id):
        abort(404)
    if not db.session.get(Person, person_id):
        abort(404)
    token = _gift_token(set_id, person_id)
    gift_url = request.host_url.rstrip("/") + f"/gifts/{token}"
    return render_template("gift_share.html", gift_url=gift_url,
                           set_id=set_id, person_id=person_id)


@views_bp.route("/gifts/<token>")
def gift_list(token):
    """Public read-only gift list page — no login required."""
    ids = _verify_gift_token(token)
    if ids is None:
        abort(404)
    set_id, person_id = ids
    item_set = db.session.get(Set, set_id)
    person   = db.session.get(Person, person_id)
    if not item_set or not person:
        abort(404)

    owned_item_ids = {
        ownership.variant.item_id
        for ownership in Ownership.query.filter_by(person_id=person_id, status="Owned").all()
    }
    missing_items = sorted(
        [item for item in item_set.items if item.id not in owned_item_ids],
        key=lambda item: item.name,
    )
    owned_count = len(item_set.items) - len(missing_items)
    total       = len(item_set.items)
    pct         = round(100 * owned_count / total) if total else 0

    return render_template("gift_list.html",
                           item_set=item_set, person=person,
                           missing_items=missing_items,
                           owned_count=owned_count, total=total, pct=pct)


# ── Collection card ───────────────────────────────────────────────────────────

@views_bp.route("/people/<int:person_id>/collection-token")
def collection_token(person_id):
    """Generate a shareable collection card token for a person."""
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    token  = _collection_token(person_id)
    card_url = request.host_url.rstrip("/") + f"/collection-card/{token}"
    return render_template("collection_card_share.html", person=person,
                           card_url=card_url, person_id=person_id)


@views_bp.route("/collection-card/<token>")
def collection_card(token):
    """Public read-only collection card — no login required."""
    person_id = _verify_collection_token(token)
    if person_id is None:
        abort(404)
    person     = db.session.get(Person, person_id)
    if not person:
        abort(404)
    ownerships = (Ownership.query
                  .filter_by(person_id=person_id, status="Owned")
                  .order_by(Ownership.id).all())

    # Group by category
    by_category: dict[str, list] = {}
    total_value = 0.0
    priced      = 0
    seen_items: set[int] = set()
    for ownership in ownerships:
        item = ownership.variant.item
        if item.id in seen_items:
            continue
        seen_items.add(item.id)
        cat = item.category or "Uncategorized"
        by_category.setdefault(cat, []).append(item)
        if item.msrp:
            total_value += item.msrp
            priced += 1

    by_category = dict(sorted(by_category.items()))
    catalog_total = Item.query.count()
    owned_count   = len(seen_items)
    coverage_pct  = round(100 * owned_count / catalog_total) if catalog_total else 0

    return render_template("collection_card.html",
                           person=person,
                           by_category=by_category,
                           owned_count=owned_count,
                           catalog_total=catalog_total,
                           coverage_pct=coverage_pct,
                           total_value=total_value,
                           priced=priced)

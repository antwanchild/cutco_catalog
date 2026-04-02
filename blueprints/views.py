from flask import Blueprint, render_template, request

from constants import STATUS_RANK, UNKNOWN_COLOR
from extensions import db
from models import Item, ItemVariant, Ownership, Person

views_bp = Blueprint("views", __name__)


@views_bp.route("/views/item/<int:iid>")
def item_owners(iid):
    item = Item.query.get_or_404(iid)
    entries = (Ownership.query
               .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
               .filter(ItemVariant.item_id == iid)
               .order_by(Ownership.status).all())
    owner_ids      = {e.person_id for e in entries}
    people_without = (Person.query
                      .filter(~Person.id.in_(owner_ids))
                      .order_by(Person.name).all())
    return render_template("item_owners.html", item=item,
                           entries=entries, people_without=people_without,
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
        p_owned = Ownership.query.filter_by(person_id=person.id, status="Owned").all()
        p_item_ids = {o.variant.item_id for o in p_owned}
        p_items    = Item.query.filter(Item.id.in_(p_item_ids)).all() if p_item_ids else []
        p_value    = sum(i.msrp for i in p_items if i.msrp)
        collector_rows.append(dict(
            id=person.id, name=person.name,
            count=len(p_item_ids), value=p_value,
        ))
    collector_rows.sort(key=lambda row: row["count"], reverse=True)

    total_value  = sum(i.msrp for i in owned_items if i.msrp)
    priced_count = sum(1 for i in owned_items if i.msrp)
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

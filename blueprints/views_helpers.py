"""Shared view context helpers."""

from __future__ import annotations

from constants import COOKWARE_CATEGORIES, STATUS_RANK, UNKNOWN_COLOR
from extensions import db
from models import Item, ItemVariant, KnifeTaskLog, Ownership, Person, Set, SharpeningLog


def _build_item_owners_context(item_id: int, *, private_view: bool) -> dict:
    """Build the item ownership page context."""
    item = db.session.get(Item, item_id)
    if not item:
        return {"item": None}

    entries = []
    people_without = []
    if private_view:
        entries = (
            Ownership.query
            .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
            .filter(ItemVariant.item_id == item_id)
            .order_by(Ownership.status)
            .all()
        )
        owner_ids = {entry.person_id for entry in entries}
        people_without = (
            Person.query
            .filter(~Person.id.in_(owner_ids))
            .order_by(Person.name)
            .all()
        )

    sharpening = []
    if private_view and item.category not in COOKWARE_CATEGORIES:
        sharpening = (
            SharpeningLog.query
            .filter_by(item_id=item_id)
            .order_by(SharpeningLog.sharpened_on.desc())
            .all()
        )

    task_log = (
        KnifeTaskLog.query
        .filter_by(item_id=item_id)
        .order_by(KnifeTaskLog.logged_on.desc())
        .all()
    )
    if not private_view:
        task_log = []

    task_counts: dict[str, int] = {}
    for entry in task_log:
        task_counts[entry.task.name] = task_counts.get(entry.task.name, 0) + 1
    top_tasks = sorted(task_counts.items(), key=lambda kv: kv[1], reverse=True)

    color_counts: dict[str, int] = {}
    for entry in entries:
        color = entry.variant.color or UNKNOWN_COLOR
        if color == UNKNOWN_COLOR:
            continue
        color_counts[color] = color_counts.get(color, 0) + 1
    top_colors = [
        {"color": color, "count": count}
        for color, count in sorted(color_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]
    if not private_view:
        top_colors = []

    return {
        "item": item,
        "entries": entries,
        "people_without": people_without,
        "sharpening": sharpening,
        "task_log": task_log,
        "top_tasks": top_tasks,
        "top_colors": top_colors,
        "attachments": item.attachments,
    }


def _build_matrix_context(sort_field: str, sort_dir: str) -> dict:
    """Build the matrix view context."""
    people_list = Person.query.order_by(Person.name).all()
    items_list = Item.query.order_by(Item.name).all()
    if sort_field == "sku":
        items_list = sorted(
            items_list,
            key=lambda item: ((item.sku or "").lower(), (item.name or "").lower()),
            reverse=(sort_dir == "desc"),
        )
    else:
        items_list = sorted(
            items_list,
            key=lambda item: ((item.name or "").lower(), (item.sku or "").lower()),
            reverse=(sort_dir == "desc"),
        )

    item_lookup = {}
    for ownership in Ownership.query.all():
        key = (ownership.person_id, ownership.variant.item_id)
        current = item_lookup.get(key)
        if current is None or STATUS_RANK.get(ownership.status, 9) < STATUS_RANK.get(current.status, 9):
            item_lookup[key] = ownership

    variant_lookup = {
        (ownership.person_id, ownership.variant_id): ownership
        for ownership in Ownership.query.all()
    }
    variants_by_item = {
        item.id: [variant for variant in item.variants if variant.color != UNKNOWN_COLOR] or item.variants
        for item in items_list
    }

    return {
        "people": people_list,
        "items": items_list,
        "item_lookup": item_lookup,
        "variant_lookup": variant_lookup,
        "variants_by_item": variants_by_item,
    }


def _build_stats_context(person_id: int | None, *, private_view: bool) -> dict:
    """Build the stats page context."""
    people_list = Person.query.order_by(Person.name).all() if private_view else []

    owned = []
    if private_view:
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

    cat_counts: dict[str, int] = {}
    cat_values: dict[str, float] = {}
    cat_catalog: dict[str, int] = {}
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
    if private_view:
        for person in people_list:
            person_owned = Ownership.query.filter_by(person_id=person.id, status="Owned").all()
            person_item_ids = {ownership.variant.item_id for ownership in person_owned}
            person_items = Item.query.filter(Item.id.in_(person_item_ids)).all() if person_item_ids else []
            person_value = sum(item.msrp for item in person_items if item.msrp)
            collector_rows.append(dict(
                id=person.id,
                name=person.name,
                count=len(person_item_ids),
                value=person_value,
            ))
        collector_rows.sort(key=lambda row: row["count"], reverse=True)

    total_value = sum(item.msrp for item in owned_items if item.msrp)
    priced_count = sum(1 for item in owned_items if item.msrp)
    catalog_total = Item.query.count()
    public_summary = dict(
        item_count=Item.query.count(),
        variants=ItemVariant.query.filter(ItemVariant.color != UNKNOWN_COLOR).count(),
        sets=Set.query.count(),
        unicorns=Item.query.filter(
            db.or_(
                Item.is_unicorn,
                Item.edge_is_unicorn,
                Item.variants.any(ItemVariant.is_unicorn == True),  # noqa: E712
            )
        ).count(),
    )

    summary = dict(
        owned_items=len(owned_items),
        owned_entries=len(owned),
        total_value=total_value,
        priced_count=priced_count,
        catalog_total=catalog_total,
        coverage_pct=round(100 * len(owned_items) / catalog_total, 1) if catalog_total else 0,
    )

    cat_data = sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)
    val_data = sorted(cat_values.items(), key=lambda kv: kv[1], reverse=True)
    color_data = sorted(color_counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
    edge_data = sorted(edge_counts.items(), key=lambda kv: kv[1], reverse=True)
    top_colors = [
        {"color": color, "count": count}
        for color, count in sorted(color_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:8]
    ]

    cov_cats = sorted(cat_catalog.keys()) if private_view else []
    cov_owned = [cat_counts.get(cat, 0) for cat in cov_cats]
    cov_gap = [cat_catalog.get(cat, 0) - cat_counts.get(cat, 0) for cat in cov_cats]

    return {
        "people": people_list,
        "summary": summary,
        "public_summary": public_summary,
        "cat_data": cat_data,
        "val_data": val_data,
        "color_data": color_data,
        "top_colors": top_colors,
        "edge_data": edge_data,
        "collector_rows": collector_rows,
        "cov_cats": cov_cats,
        "cov_owned": cov_owned,
        "cov_gap": cov_gap,
    }


def _build_gift_list_context(set_id: int, person_id: int) -> dict | None:
    """Build the public gift list context."""
    item_set = db.session.get(Set, set_id)
    person = db.session.get(Person, person_id)
    if not item_set or not person:
        return None

    owned_item_ids = {
        ownership.variant.item_id
        for ownership in Ownership.query.filter_by(person_id=person_id, status="Owned").all()
    }
    missing_items = sorted(
        [item for item in item_set.items if item.id not in owned_item_ids],
        key=lambda item: item.name,
    )
    owned_count = len(item_set.items) - len(missing_items)
    total = len(item_set.items)
    pct = round(100 * owned_count / total) if total else 0
    return {
        "item_set": item_set,
        "person": person,
        "missing_items": missing_items,
        "owned_count": owned_count,
        "total": total,
        "pct": pct,
    }


def _build_collection_card_context(person_id: int) -> dict | None:
    """Build the public collection card context."""
    person = db.session.get(Person, person_id)
    if not person:
        return None
    ownerships = (
        Ownership.query
        .filter_by(person_id=person_id, status="Owned")
        .order_by(Ownership.id)
        .all()
    )

    by_category: dict[str, list] = {}
    total_value = 0.0
    priced = 0
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
    owned_count = len(seen_items)
    coverage_pct = round(100 * owned_count / catalog_total) if catalog_total else 0

    return {
        "person": person,
        "by_category": by_category,
        "owned_count": owned_count,
        "catalog_total": catalog_total,
        "coverage_pct": coverage_pct,
        "total_value": total_value,
        "priced": priced,
    }


def _build_person_collection_context(person_id: int, *, session: dict) -> dict:
    """Build the collection page context for a person."""
    person = db.session.get(Person, person_id)
    if not person:
        return {"person": None}

    session["last_person_id"] = person_id
    ownerships = (
        Ownership.query.filter_by(person_id=person_id)
        .order_by(Ownership.status)
        .all()
    )

    owned_item_ids = {o.variant.item_id for o in ownerships if o.status == "Owned"}
    all_items = Item.query.order_by(Item.name).all()
    item_gaps = [item for item in all_items if item.id not in owned_item_ids]

    variant_gaps = []
    for item in all_items:
        real_variants = [variant for variant in item.variants if variant.color != UNKNOWN_COLOR]
        if not real_variants:
            continue
        owned_variant_ids = {
            ownership.variant_id
            for ownership in ownerships
            if ownership.variant.item_id == item.id and ownership.status == "Owned"
        }
        missing = [variant for variant in real_variants if variant.id not in owned_variant_ids]
        if missing:
            variant_gaps.append((item, missing))

    color_counts: dict[str, int] = {}
    for ownership in ownerships:
        color = ownership.variant.color or UNKNOWN_COLOR
        if color == UNKNOWN_COLOR:
            continue
        color_counts[color] = color_counts.get(color, 0) + 1
    top_colors = [
        {"color": color, "count": count}
        for color, count in sorted(color_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:8]
    ]

    return {
        "person": person,
        "ownerships": ownerships,
        "item_gaps": item_gaps,
        "variant_gaps": variant_gaps,
        "top_colors": top_colors,
    }


def _build_wishlist_rows(person_id: int | None, sort_field: str, sort_dir: str) -> tuple[list[dict], list[Person]]:
    """Build sorted wishlist rows for the wishlist page."""
    people_list = Person.query.order_by(Person.name).all()

    wl_q = Ownership.query.filter_by(status="Wishlist")
    if person_id:
        wl_q = wl_q.filter_by(person_id=person_id)
    entries = wl_q.all()

    rows = []
    for entry in entries:
        msrp = entry.variant.item.msrp
        target = entry.target_price
        hit = msrp is not None and target is not None and msrp <= target
        delta = (msrp - target) if (msrp is not None and target is not None) else None
        rows.append(
            dict(
                ownership=entry,
                msrp=msrp,
                target=target,
                hit=hit,
                delta=delta,
            )
        )

    if sort_field == "name":
        rows.sort(
            key=lambda row: (
                (row["ownership"].variant.item.name or "").lower(),
                (row["ownership"].variant.item.sku or "").lower(),
                row["ownership"].person.name.lower(),
            ),
            reverse=(sort_dir == "desc"),
        )
    elif sort_field == "sku":
        rows.sort(
            key=lambda row: (
                (row["ownership"].variant.item.sku or "").lower(),
                (row["ownership"].variant.item.name or "").lower(),
                row["ownership"].person.name.lower(),
            ),
            reverse=(sort_dir == "desc"),
        )
    else:
        rows.sort(
            key=lambda row: (
                0 if row["hit"] else (1 if row["delta"] is not None else 2),
                row["delta"] if row["delta"] is not None else float("inf"),
            )
        )

    return rows, people_list

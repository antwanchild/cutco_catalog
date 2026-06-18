"""Helper functions for people, collection, and wishlist views."""

from __future__ import annotations

from extensions import db
from models import Item, Ownership, Person
from constants import UNKNOWN_COLOR


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

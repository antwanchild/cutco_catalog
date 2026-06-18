"""Item-focused view context helpers."""

from __future__ import annotations

from constants import COOKWARE_CATEGORIES, STATUS_RANK, UNKNOWN_COLOR
from extensions import db
from models import Item, ItemVariant, KnifeTaskLog, Ownership, Person, SharpeningLog


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

"""Read-only views and public share links."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    render_template,
    request,
    redirect,
    send_from_directory,
    url_for,
)

from constants import COOKWARE_CATEGORIES, STATUS_RANK, UNKNOWN_COLOR
from extensions import db
from helpers import (
    admin_required,
    db_commit,
    is_authenticated_user,
    top_count_rows,
    user_required,
)
from helpers import (
    _collection_token,
    _gift_token,
    _verify_collection_token,
    _verify_gift_token,
)
from models import (
    Item,
    ItemAttachment,
    ItemVariant,
    KnifeTaskLog,
    Ownership,
    Person,
    Set,
    SharpeningLog,
)

_ALLOWED_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _attachments_root() -> Path:
    """Return the base upload directory for item attachments."""
    return Path(current_app.config["ATTACHMENTS_DIR"]).expanduser()


def _attachment_dir(item_id: int) -> Path:
    """Return the storage directory for one item's attachments."""
    return _attachments_root() / str(item_id)


def _allowed_attachment(filename: str | None) -> bool:
    """Return whether a filename looks like a supported image upload."""
    if not filename:
        return False
    return Path(filename).suffix.lower() in _ALLOWED_ATTACHMENT_EXTENSIONS


def _store_attachment(
    item: Item, uploaded_file, caption: str | None
) -> ItemAttachment | None:
    """Persist an uploaded attachment and return the database row."""
    original_filename = Path(uploaded_file.filename or "").name
    if not _allowed_attachment(original_filename):
        return None
    if uploaded_file.mimetype and not uploaded_file.mimetype.startswith("image/"):
        return None

    storage_dir = _attachment_dir(item.id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_filename).suffix.lower()
    stored_filename = f"{uuid4().hex}{suffix}"
    uploaded_file.save(storage_dir / stored_filename)

    attachment = ItemAttachment(
        item=item,
        original_filename=original_filename,
        stored_filename=stored_filename,
        content_type=uploaded_file.mimetype or None,
        caption=(caption or "").strip() or None,
    )
    db.session.add(attachment)
    if not db_commit(db.session, error_msg="Could not save attachment."):
        stored_path = storage_dir / stored_filename
        if stored_path.exists():
            stored_path.unlink()
        return None
    return attachment


def _build_item_owners_context(item_id: int, *, private_view: bool) -> dict:
    """Build the item ownership page context."""
    item = db.session.get(Item, item_id)
    if not item:
        return {"item": None}

    entries = []
    people_without = []
    if private_view:
        entries = (
            Ownership.query.join(ItemVariant, Ownership.variant_id == ItemVariant.id)
            .filter(ItemVariant.item_id == item_id)
            .order_by(Ownership.status)
            .all()
        )
        owner_ids = {entry.person_id for entry in entries}
        people_without = (
            Person.query.filter(~Person.id.in_(owner_ids)).order_by(Person.name).all()
        )

    sharpening = []
    if private_view and item.category not in COOKWARE_CATEGORIES:
        sharpening = (
            SharpeningLog.query.filter_by(item_id=item_id)
            .order_by(SharpeningLog.sharpened_on.desc())
            .all()
        )

    task_log = (
        KnifeTaskLog.query.filter_by(item_id=item_id)
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
    top_colors = top_count_rows(color_counts)
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
        if current is None or STATUS_RANK.get(ownership.status, 9) < STATUS_RANK.get(
            current.status, 9
        ):
            item_lookup[key] = ownership

    variant_lookup = {
        (ownership.person_id, ownership.variant_id): ownership
        for ownership in Ownership.query.all()
    }
    variants_by_item = {
        item.id: [
            variant for variant in item.variants if variant.color != UNKNOWN_COLOR
        ]
        or item.variants
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
        if ownership.variant is None or ownership.variant.item is None:
            continue
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
        if ownership.variant is None:
            continue
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
            person_owned = Ownership.query.filter_by(
                person_id=person.id, status="Owned"
            ).all()
            person_item_ids = {ownership.variant.item_id for ownership in person_owned}
            person_items = (
                Item.query.filter(Item.id.in_(person_item_ids)).all()
                if person_item_ids
                else []
            )
            person_value = sum(item.msrp for item in person_items if item.msrp)
            collector_rows.append(
                dict(
                    id=person.id,
                    name=person.name,
                    count=len(person_item_ids),
                    value=person_value,
                )
            )
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
        coverage_pct=(
            round(100 * len(owned_items) / catalog_total, 1) if catalog_total else 0
        ),
    )

    cat_data = sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)
    val_data = sorted(cat_values.items(), key=lambda kv: kv[1], reverse=True)
    color_data = sorted(color_counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
    edge_data = sorted(edge_counts.items(), key=lambda kv: kv[1], reverse=True)
    top_colors = top_count_rows(color_counts, sort_by_name=True)

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
        for ownership in Ownership.query.filter_by(
            person_id=person_id, status="Owned"
        ).all()
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
        Ownership.query.filter_by(person_id=person_id, status="Owned")
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
        Ownership.query.filter_by(person_id=person_id).order_by(Ownership.status).all()
    )

    owned_item_ids = {o.variant.item_id for o in ownerships if o.status == "Owned"}
    all_items = Item.query.order_by(Item.name).all()
    item_gaps = [item for item in all_items if item.id not in owned_item_ids]

    variant_gaps = []
    for item in all_items:
        real_variants = [
            variant for variant in item.variants if variant.color != UNKNOWN_COLOR
        ]
        if not real_variants:
            continue
        owned_variant_ids = {
            ownership.variant_id
            for ownership in ownerships
            if ownership.variant.item_id == item.id and ownership.status == "Owned"
        }
        missing = [
            variant for variant in real_variants if variant.id not in owned_variant_ids
        ]
        if missing:
            variant_gaps.append((item, missing))

    color_counts: dict[str, int] = {}
    for ownership in ownerships:
        color = ownership.variant.color or UNKNOWN_COLOR
        if color == UNKNOWN_COLOR:
            continue
        color_counts[color] = color_counts.get(color, 0) + 1
    top_colors = top_count_rows(color_counts, sort_by_name=True)

    return {
        "person": person,
        "ownerships": ownerships,
        "item_gaps": item_gaps,
        "variant_gaps": variant_gaps,
        "top_colors": top_colors,
    }


def _build_wishlist_rows(
    person_id: int | None, sort_field: str, sort_dir: str
) -> tuple[list[dict], list[Person]]:
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


views_bp = Blueprint("views", __name__)


@views_bp.route("/attachments/<int:attachment_id>")
def attachment_file(attachment_id):
    """Serve a stored attachment file."""
    attachment = db.session.get(ItemAttachment, attachment_id)
    if not attachment:
        abort(404)
    storage_dir = _attachment_dir(attachment.item_id)
    file_path = storage_dir / attachment.stored_filename
    if not file_path.exists():
        abort(404)
    return send_from_directory(
        storage_dir, attachment.stored_filename, as_attachment=False
    )


@views_bp.route("/views/item/<int:item_id>")
def item_owners(item_id):
    """Render the ownership view for a single item."""
    context = _build_item_owners_context(item_id, private_view=is_authenticated_user())
    item = context["item"]
    if not item:
        abort(404)
    return render_template("item_owners.html", **context)


@views_bp.route("/views/item/<int:item_id>/attachments", methods=["POST"])
@admin_required
def item_attachment_upload(item_id):
    """Upload one image attachment for an item."""
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)

    uploaded_file = request.files.get("attachment")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose an image to upload.", "warning")
        return redirect(url_for("views.item_owners", item_id=item_id))

    attachment = _store_attachment(item, uploaded_file, request.form.get("caption"))
    if attachment is None:
        flash(
            "That file type is not supported. Please upload a JPG, PNG, GIF, or WEBP image.",
            "error",
        )
        return redirect(url_for("views.item_owners", item_id=item_id))

    flash(f'Added attachment "{attachment.original_filename}".', "success")
    return redirect(url_for("views.item_owners", item_id=item_id))


@views_bp.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
@admin_required
def attachment_delete(attachment_id):
    """Delete a stored attachment and its backing file."""
    attachment = db.session.get(ItemAttachment, attachment_id)
    if not attachment:
        abort(404)

    item_id = attachment.item_id
    file_path = _attachment_dir(item_id) / attachment.stored_filename
    db.session.delete(attachment)
    if not db_commit(db.session, error_msg="Could not delete attachment."):
        return redirect(url_for("views.item_owners", item_id=item_id))
    if file_path.exists():
        file_path.unlink()
    flash("Attachment deleted.", "info")
    return redirect(url_for("views.item_owners", item_id=item_id))


@views_bp.route("/views/matrix")
@user_required
def matrix():
    """Render the ownership matrix view."""
    sort_field = (request.args.get("sort", "name") or "name").strip().lower()
    if sort_field not in {"name", "sku"}:
        sort_field = "name"
    sort_dir = (request.args.get("dir", "asc") or "asc").strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"
    context = _build_matrix_context(sort_field, sort_dir)
    return render_template(
        "matrix.html",
        people=context["people"],
        items=context["items"],
        sort_field=sort_field,
        sort_dir=sort_dir,
        item_lookup=context["item_lookup"],
        variant_lookup=context["variant_lookup"],
        variants_by_item=context["variants_by_item"],
        UNKNOWN_COLOR=UNKNOWN_COLOR,
    )


@views_bp.route("/stats", strict_slashes=False)
@user_required
def stats():
    """Render collection summary statistics."""
    person_id = request.args.get("person", type=int)
    private_view = is_authenticated_user()
    context = _build_stats_context(person_id, private_view=private_view)
    return render_template(
        "stats.html",
        people=context["people"],
        person_id=person_id,
        summary=context["summary"],
        public_summary=context["public_summary"],
        cat_data=context["cat_data"],
        val_data=context["val_data"],
        color_data=context["color_data"],
        top_colors=context["top_colors"],
        edge_data=context["edge_data"],
        collector_rows=context["collector_rows"],
        cov_cats=context["cov_cats"],
        cov_owned=context["cov_owned"],
        cov_gap=context["cov_gap"],
    )


# ── Gift list ─────────────────────────────────────────────────────────────────


@views_bp.route("/sets/<int:set_id>/gift-token")
@views_bp.route("/sets/<int:sid>/gift-token")
def gift_token(set_id=None, sid=None):
    """Generate a shareable gift list token."""
    set_id = set_id if set_id is not None else sid
    person_id = request.args.get("person", type=int)
    if set_id is None or not person_id:
        abort(400)
    # Validate both exist
    if not db.session.get(Set, set_id):
        abort(404)
    if not db.session.get(Person, person_id):
        abort(404)
    token = _gift_token(set_id, person_id)
    gift_url = request.host_url.rstrip("/") + f"/gifts/{token}"
    return render_template(
        "gift_share.html", gift_url=gift_url, set_id=set_id, person_id=person_id
    )


@views_bp.route("/gifts/<token>")
def gift_list(token):
    """Render a public gift list page."""
    ids = _verify_gift_token(token)
    if ids is None:
        abort(404)
    set_id, person_id = ids
    context = _build_gift_list_context(set_id, person_id)
    if context is None:
        abort(404)
    return render_template(
        "gift_list.html",
        item_set=context["item_set"],
        person=context["person"],
        missing_items=context["missing_items"],
        owned_count=context["owned_count"],
        total=context["total"],
        pct=context["pct"],
    )


# ── Collection card ───────────────────────────────────────────────────────────


@views_bp.route("/people/<int:person_id>/collection-token")
def collection_token(person_id):
    """Generate a shareable collection token."""
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    token = _collection_token(person_id)
    card_url = request.host_url.rstrip("/") + f"/collection-card/{token}"
    return render_template(
        "collection_card_share.html",
        person=person,
        card_url=card_url,
        person_id=person_id,
    )


@views_bp.route("/collection-card/<token>")
def collection_card(token):
    """Render a public collection card page."""
    person_id = _verify_collection_token(token)
    if person_id is None:
        abort(404)
    context = _build_collection_card_context(person_id)
    if context is None:
        abort(404)
    return render_template(
        "collection_card.html",
        person=context["person"],
        by_category=context["by_category"],
        owned_count=context["owned_count"],
        catalog_total=context["catalog_total"],
        coverage_pct=context["coverage_pct"],
        total_value=context["total_value"],
        priced=context["priced"],
    )

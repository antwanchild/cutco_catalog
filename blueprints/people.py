import logging
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from constants import DISCORD_WEBHOOK_URL, STATUS_OPTIONS, UNKNOWN_COLOR
from extensions import db
from helpers import _notify_discord, admin_required, check_wishlist_targets, db_commit
from models import Item, Ownership, Person

people_bp = Blueprint("people", __name__)
logger = logging.getLogger(__name__)


def _parse_optional_whole_number(raw_value: str, label: str) -> tuple[int | None, str | None]:
    cleaned = (raw_value or "").strip()
    if not cleaned or cleaned.lower() in {"0", "none", "n/a", "-"}:
        return None, None
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned), None
    return None, f"{label} must be a whole number."


@people_bp.route("/people")
def people():
    people = Person.query.order_by(Person.name).all()
    counts = {person.id: Ownership.query.filter_by(person_id=person.id, status="Owned").count()
              for person in people}
    return render_template("people.html", persons=people, counts=counts)


@people_bp.route("/people/add", methods=["GET", "POST"])
@admin_required
def people_add():
    if request.method == "POST":
        person = Person(name=request.form["name"].strip(),
                        notes=request.form.get("notes", "").strip() or None)
        db.session.add(person)
        if db_commit(db.session):
            logger.info("Person added: %s", person.name)
            flash(f"Added {person.name}.", "success")
        return redirect(url_for("people.people"))
    return render_template("person_form.html", person=None, action="Add")


@people_bp.route("/people/<int:person_id>/edit", methods=["GET", "POST"])
@admin_required
def people_edit(person_id):
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    if request.method == "POST":
        person.name  = request.form["name"].strip()
        person.notes = request.form.get("notes", "").strip() or None
        if db_commit(db.session):
            logger.info("Person updated: %s", person.name)
            flash(f"Updated {person.name}.", "success")
        return redirect(url_for("people.people"))
    return render_template("person_form.html", person=person, action="Edit")


@people_bp.route("/people/<int:person_id>/delete", methods=["POST"])
@admin_required
def people_delete(person_id):
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    name   = person.name
    db.session.delete(person)
    if db_commit(db.session):
        logger.info("Person deleted: %s", name)
        flash(f"Removed {name}.", "info")
    return redirect(url_for("people.people"))


@people_bp.route("/people/<int:person_id>/purge-collection", methods=["POST"])
@admin_required
def purge_collection(person_id):
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    count  = Ownership.query.filter_by(person_id=person_id).count()
    Ownership.query.filter_by(person_id=person_id).delete()
    if db_commit(db.session):
        logger.info("Collection purged: %s (%d entries)", person.name, count)
        flash(f"Removed all {count} ownership entr{'ies' if count != 1 else 'y'} for {person.name}.", "info")
    return redirect(url_for("people.person_collection", person_id=person_id))


@people_bp.route("/people/<int:person_id>/collection")
def person_collection(person_id):
    person     = db.session.get(Person, person_id)
    if not person:
        abort(404)
    ownerships = (Ownership.query.filter_by(person_id=person_id)
                  .order_by(Ownership.status).all())

    owned_item_ids = {o.variant.item_id for o in ownerships if o.status == "Owned"}
    all_items      = Item.query.order_by(Item.name).all()
    item_gaps      = [item for item in all_items if item.id not in owned_item_ids]

    variant_gaps = []
    for item in all_items:
        real_variants = [variant for variant in item.variants if variant.color != UNKNOWN_COLOR]
        if not real_variants:
            continue
        owned_variant_ids = {ownership.variant_id for ownership in ownerships
                             if ownership.variant.item_id == item.id and ownership.status == "Owned"}
        missing = [variant for variant in real_variants if variant.id not in owned_variant_ids]
        if missing:
            variant_gaps.append((item, missing))

    return render_template("collection.html", person=person,
                           ownerships=ownerships,
                           item_gaps=item_gaps,
                           variant_gaps=variant_gaps,
                           status_options=STATUS_OPTIONS,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


# ── Ownership CRUD ────────────────────────────────────────────────────────────

@people_bp.route("/ownership/add", methods=["GET", "POST"])
@admin_required
def ownership_add():
    person_id   = request.args.get("person_id", type=int)
    item_id     = request.args.get("item_id", type=int)
    variant_id  = request.args.get("variant_id", type=int)
    sel_status  = request.args.get("status", "Owned")

    if request.method == "POST":
        person_id  = int(request.form["person_id"])
        variant_id = int(request.form["variant_id"])
        if Ownership.query.filter_by(person_id=person_id, variant_id=variant_id).first():
            flash("That person already has an entry for that variant.", "error")
            return redirect(url_for("people.person_collection", person_id=person_id))
        raw_target = request.form.get("target_price", "").strip()
        try:
            target_price = float(raw_target) if raw_target else None
        except ValueError:
            target_price = None
        quantity_purchased, qty_error = _parse_optional_whole_number(
            request.form.get("quantity_purchased", ""),
            "Quantity Purchased",
        )
        if qty_error:
            flash(qty_error, "error")
            return redirect(url_for(
                "people.ownership_add",
                person_id=person_id,
                item_id=request.form.get("item_id", type=int),
                variant_id=variant_id,
                status=request.form.get("status", "Owned"),
            ))
        quantity_given_away, qty_error = _parse_optional_whole_number(
            request.form.get("quantity_given_away", ""),
            "Quantity Given Away",
        )
        if qty_error:
            flash(qty_error, "error")
            return redirect(url_for(
                "people.ownership_add",
                person_id=person_id,
                item_id=request.form.get("item_id", type=int),
                variant_id=variant_id,
                status=request.form.get("status", "Owned"),
            ))
        db.session.add(Ownership(
            person_id    = person_id,
            variant_id   = variant_id,
            status       = request.form.get("status", "Owned"),
            target_price = target_price,
            notes        = request.form.get("notes", "").strip() or None,
            quantity_purchased=quantity_purchased,
            quantity_given_away=quantity_given_away,
        ))
        if db_commit(db.session):
            logger.info("Ownership added: person %d, variant %d", person_id, variant_id)
            flash("Entry logged.", "success")
        return redirect(url_for("people.person_collection", person_id=person_id))

    selected_item = db.session.get(Item, item_id) if item_id else None
    return render_template("ownership_form.html", ownership=None,
                           people_list=Person.query.order_by(Person.name).all(),
                           items_list=Item.query.order_by(Item.name).all(),
                           status_options=STATUS_OPTIONS,
                           sel_person_id=person_id,
                           sel_item_id=item_id,
                           sel_variant_id=variant_id,
                           sel_item=selected_item,
                           sel_status=sel_status,
                           action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@people_bp.route("/ownership/<int:ownership_id>/edit", methods=["GET", "POST"])
@admin_required
def ownership_edit(ownership_id):
    ownership = db.session.get(Ownership, ownership_id)
    if not ownership:
        abort(404)
    if request.method == "POST":
        ownership.status = request.form.get("status", "Owned")
        raw_target = request.form.get("target_price", "").strip()
        try:
            ownership.target_price = float(raw_target) if raw_target else None
        except ValueError:
            ownership.target_price = None
        ownership.notes  = request.form.get("notes", "").strip() or None
        quantity_purchased, qty_error = _parse_optional_whole_number(
            request.form.get("quantity_purchased", ""),
            "Quantity Purchased",
        )
        if qty_error:
            flash(qty_error, "error")
            return redirect(url_for("people.ownership_edit", ownership_id=ownership_id))
        quantity_given_away, qty_error = _parse_optional_whole_number(
            request.form.get("quantity_given_away", ""),
            "Quantity Given Away",
        )
        if qty_error:
            flash(qty_error, "error")
            return redirect(url_for("people.ownership_edit", ownership_id=ownership_id))
        ownership.quantity_purchased = quantity_purchased
        ownership.quantity_given_away = quantity_given_away
        if db_commit(db.session):
            logger.info("Ownership updated: id %d → %s", ownership_id, ownership.status)
            flash("Updated.", "success")
        return redirect(url_for("people.person_collection", person_id=ownership.person_id))

    return render_template("ownership_form.html", ownership=ownership,
                           people_list=Person.query.order_by(Person.name).all(),
                           items_list=Item.query.order_by(Item.name).all(),
                           status_options=STATUS_OPTIONS,
                           sel_person_id=ownership.person_id,
                           sel_item_id=ownership.variant.item_id,
                           sel_variant_id=ownership.variant_id,
                           sel_item=ownership.variant.item,
                           action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@people_bp.route("/ownership/<int:ownership_id>/delete", methods=["POST"])
@admin_required
def ownership_delete(ownership_id):
    ownership = db.session.get(Ownership, ownership_id)
    if not ownership:
        abort(404)
    person_id       = ownership.person_id
    db.session.delete(ownership)
    if db_commit(db.session):
        logger.info("Ownership deleted: id %d", ownership_id)
        flash("Entry removed.", "info")
    return redirect(url_for("people.person_collection", person_id=person_id))


# ── Bulk status update ────────────────────────────────────────────────────────

@people_bp.route("/people/<int:person_id>/bulk-status", methods=["POST"])
@admin_required
def bulk_status_update(person_id):
    if not db.session.get(Person, person_id):
        abort(404)
    selected = request.form.getlist("ownership_ids", type=int)
    bulk_action = request.form.get("bulk_action", "status").strip()
    if not selected:
        flash("Select at least one entry first.", "error")
        return redirect(url_for("people.person_collection", person_id=person_id))

    updated = (Ownership.query
               .filter(Ownership.id.in_(selected), Ownership.person_id == person_id)
               .all())
    if bulk_action == "status":
        new_status = request.form.get("bulk_status", "").strip()
        if new_status not in STATUS_OPTIONS:
            flash("Select at least one entry and a valid status.", "error")
            return redirect(url_for("people.person_collection", person_id=person_id))
        for ownership in updated:
            ownership.status = new_status
        summary = f"Updated {len(updated)} entr{'y' if len(updated) == 1 else 'ies'} to {new_status}."
        log_message = f"Bulk status update: person {person_id}, {len(updated)} entries → {new_status}"
    elif bulk_action == "target":
        raw_target = request.form.get("bulk_target_price", "").strip()
        try:
            target_price = float(raw_target) if raw_target else None
        except ValueError:
            flash("Enter a valid target price or leave it blank to clear.", "error")
            return redirect(url_for("people.person_collection", person_id=person_id))
        for ownership in updated:
            ownership.target_price = target_price
            if target_price is not None and ownership.status != "Wishlist":
                ownership.status = "Wishlist"
        if target_price is None:
            summary = f"Cleared target price for {len(updated)} entr{'y' if len(updated) == 1 else 'ies'}."
            log_message = f"Bulk target clear: person {person_id}, {len(updated)} entries"
        else:
            summary = f"Set target price to ${target_price:.2f} for {len(updated)} entr{'y' if len(updated) == 1 else 'ies'}."
            log_message = f"Bulk target update: person {person_id}, {len(updated)} entries → {target_price:.2f}"
    elif bulk_action == "delete":
        for ownership in updated:
            db.session.delete(ownership)
        summary = f"Deleted {len(updated)} entr{'y' if len(updated) == 1 else 'ies'}."
        log_message = f"Bulk ownership delete: person {person_id}, {len(updated)} entries"
    else:
        flash("Choose a valid bulk action.", "error")
        return redirect(url_for("people.person_collection", person_id=person_id))

    if db_commit(db.session):
        logger.info(log_message)
        flash(summary, "success")
    return redirect(url_for("people.person_collection", person_id=person_id))


# ── Wishlist ──────────────────────────────────────────────────────────────────

@people_bp.route("/wishlist")
def wishlist():
    person_id   = request.args.get("person", type=int)
    people_list = Person.query.order_by(Person.name).all()

    wl_q = Ownership.query.filter_by(status="Wishlist")
    if person_id:
        wl_q = wl_q.filter_by(person_id=person_id)
    entries = wl_q.all()

    rows = []
    for entry in entries:
        msrp   = entry.variant.item.msrp
        target = entry.target_price
        hit    = (msrp is not None and target is not None and msrp <= target)
        delta  = (msrp - target) if (msrp is not None and target is not None) else None
        rows.append(dict(
            ownership = entry,
            msrp      = msrp,
            target    = target,
            hit       = hit,
            delta     = delta,
        ))

    rows.sort(key=lambda row: (
        0 if row["hit"] else (1 if row["delta"] is not None else 2),
        row["delta"] if row["delta"] is not None else float("inf"),
    ))

    return render_template(
        "wishlist.html",
        rows        = rows,
        people      = people_list,
        person_id   = person_id,
        has_discord = bool(DISCORD_WEBHOOK_URL),
        hit_count   = sum(1 for row in rows if row["hit"]),
    )


@people_bp.route("/wishlist/check", methods=["POST"])
@admin_required
def wishlist_check():
    hits = check_wishlist_targets()
    if not hits:
        flash("No wishlist targets met at current MSRP prices.", "info")
        return redirect(url_for("people.wishlist"))
    if DISCORD_WEBHOOK_URL:
        lines = ["**🎯 Cutco Wishlist — Price Targets Met**"]
        for hit in hits:
            lines.append(
                f"• **{hit['person']}** — {hit['item']} (#{hit['sku']}): "
                f"MSRP ${hit['msrp']:.2f} ≤ target ${hit['target']:.2f} "
                f"(saves ${hit['savings']:.2f})"
            )
        _notify_discord("\n".join(lines))
        flash(f"Sent {len(hits)} price alert(s) to Discord.", "success")
    else:
        flash(
            f"{len(hits)} target(s) met — set DISCORD_WEBHOOK_URL to enable notifications.",
            "info",
        )
    return redirect(url_for("people.wishlist"))

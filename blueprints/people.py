"""People, ownership, and wishlist routes."""

import logging

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from constants import DISCORD_WEBHOOK_URL, STATUS_OPTIONS, UNKNOWN_COLOR
from extensions import db
from number_utils import parse_nonnegative_whole_number
from helpers import _notify_discord, check_wishlist_targets, db_commit, user_required
from blueprints.views_summary_helpers import _build_person_collection_context, _build_wishlist_rows
from models import Item, Ownership, Person, record_audit_event

people_bp = Blueprint("people", __name__)
logger = logging.getLogger(__name__)


@people_bp.route("/people")
@user_required
def people():
    """Render the people list."""
    people = Person.query.order_by(Person.name).all()
    counts = {person.id: Ownership.query.filter_by(person_id=person.id, status="Owned").count()
              for person in people}
    return render_template("people.html", persons=people, counts=counts)


@people_bp.route("/people/add", methods=["GET", "POST"])
@user_required
def people_add():
    """Create a person record."""
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
@user_required
def people_edit(person_id):
    """Edit an existing person record."""
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
@user_required
def people_delete(person_id):
    """Delete a person record."""
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
@user_required
def purge_collection(person_id):
    """Delete all ownership records for a person."""
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    count  = Ownership.query.filter_by(person_id=person_id).count()
    Ownership.query.filter_by(person_id=person_id).delete()
    if db_commit(db.session):
        logger.info("Collection purged: %s (%d entries)", person.name, count)
        record_audit_event(
            kind="audit",
            title="Purged collector ownerships",
            action="delete",
            entity_type="Ownership",
            entity_id=person.id,
            entity_name=person.name,
            payload={"person": person.name, "ownerships_deleted": count},
        )
        flash(f"Removed all {count} ownership entr{'ies' if count != 1 else 'y'} for {person.name}.", "info")
    return redirect(url_for("people.person_collection", person_id=person_id))


@people_bp.route("/people/<int:person_id>/collection")
@user_required
def person_collection(person_id):
    """Render a person's collection detail page."""
    context = _build_person_collection_context(person_id, session=session)
    person = context["person"]
    if not person:
        abort(404)
    return render_template("collection.html", person=person,
                           ownerships=context["ownerships"],
                           item_gaps=context["item_gaps"],
                           variant_gaps=context["variant_gaps"],
                           top_colors=context["top_colors"],
                           status_options=STATUS_OPTIONS,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


# ── Ownership CRUD ────────────────────────────────────────────────────────────

@people_bp.route("/ownership/add", methods=["GET", "POST"])
@user_required
def ownership_add():
    """Create an ownership record."""
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
        quantity_purchased, qty_error = parse_nonnegative_whole_number(
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
        quantity_given_away, qty_error = parse_nonnegative_whole_number(
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
@user_required
def ownership_edit(ownership_id):
    """Edit an ownership record."""
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
        quantity_purchased, qty_error = parse_nonnegative_whole_number(
            request.form.get("quantity_purchased", ""),
            "Quantity Purchased",
        )
        if qty_error:
            flash(qty_error, "error")
            return redirect(url_for("people.ownership_edit", ownership_id=ownership_id))
        quantity_given_away, qty_error = parse_nonnegative_whole_number(
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
@user_required
def ownership_delete(ownership_id):
    """Delete an ownership record."""
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
@user_required
def bulk_status_update(person_id):
    """Update the status of multiple ownership records."""
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
@user_required
def wishlist():
    """Render the wishlist page."""
    person_id   = request.args.get("person", type=int)
    sort_field  = (request.args.get("sort", "target") or "target").strip().lower()
    if sort_field not in {"target", "name", "sku"}:
        sort_field = "target"
    sort_dir = (request.args.get("dir", "asc") or "asc").strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"
    rows, people_list = _build_wishlist_rows(person_id, sort_field, sort_dir)

    return render_template(
        "wishlist.html",
        rows        = rows,
        people      = people_list,
        person_id   = person_id,
        sort_field  = sort_field,
        sort_dir    = sort_dir,
        has_discord = bool(DISCORD_WEBHOOK_URL),
        hit_count   = sum(1 for row in rows if row["hit"]),
    )


@people_bp.route("/wishlist/check", methods=["POST"])
@user_required
def wishlist_check():
    """Check wishlist targets and notify when items hit their price."""
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

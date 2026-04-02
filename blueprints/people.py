from flask import Blueprint, flash, redirect, render_template, request, url_for

from constants import DISCORD_WEBHOOK_URL, STATUS_OPTIONS, UNKNOWN_COLOR
from extensions import db
from helpers import _notify_discord, check_wishlist_targets, is_admin
from models import Item, Ownership, Person

people_bp = Blueprint("people", __name__)


@people_bp.route("/people")
def people():
    persons = Person.query.order_by(Person.name).all()
    counts  = {person.id: Ownership.query.filter_by(person_id=person.id, status="Owned").count()
               for person in persons}
    return render_template("people.html", persons=persons, counts=counts)


@people_bp.route("/people/add", methods=["GET", "POST"])
def people_add():
    if request.method == "POST":
        person = Person(name=request.form["name"].strip(),
                        notes=request.form.get("notes", "").strip() or None)
        db.session.add(person)
        db.session.commit()
        flash(f"Added {person.name}.", "success")
        return redirect(url_for("people.people"))
    return render_template("person_form.html", person=None, action="Add")


@people_bp.route("/people/<int:pid>/edit", methods=["GET", "POST"])
def people_edit(pid):
    person = Person.query.get_or_404(pid)
    if request.method == "POST":
        person.name  = request.form["name"].strip()
        person.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash(f"Updated {person.name}.", "success")
        return redirect(url_for("people.people"))
    return render_template("person_form.html", person=person, action="Edit")


@people_bp.route("/people/<int:pid>/delete", methods=["POST"])
def people_delete(pid):
    person = Person.query.get_or_404(pid)
    name   = person.name
    db.session.delete(person)
    db.session.commit()
    flash(f"Removed {name}.", "info")
    return redirect(url_for("people.people"))


@people_bp.route("/people/<int:pid>/collection")
def person_collection(pid):
    person     = Person.query.get_or_404(pid)
    ownerships = (Ownership.query.filter_by(person_id=pid)
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
def ownership_add():
    person_id  = request.args.get("person_id", type=int)
    item_id    = request.args.get("item_id", type=int)
    variant_id = request.args.get("variant_id", type=int)

    if request.method == "POST":
        person_id  = int(request.form["person_id"])
        variant_id = int(request.form["variant_id"])
        if Ownership.query.filter_by(person_id=person_id, variant_id=variant_id).first():
            flash("That person already has an entry for that variant.", "error")
            return redirect(url_for("people.person_collection", pid=person_id))
        raw_target = request.form.get("target_price", "").strip()
        try:
            target_price = float(raw_target) if raw_target else None
        except ValueError:
            target_price = None
        db.session.add(Ownership(
            person_id    = person_id,
            variant_id   = variant_id,
            status       = request.form.get("status", "Owned"),
            target_price = target_price,
            notes        = request.form.get("notes", "").strip() or None,
        ))
        db.session.commit()
        flash("Entry logged.", "success")
        return redirect(url_for("people.person_collection", pid=person_id))

    sel_item = Item.query.get(item_id) if item_id else None
    return render_template("ownership_form.html", ownership=None,
                           people_list=Person.query.order_by(Person.name).all(),
                           items_list=Item.query.order_by(Item.name).all(),
                           status_options=STATUS_OPTIONS,
                           sel_person_id=person_id,
                           sel_item_id=item_id,
                           sel_variant_id=variant_id,
                           sel_item=sel_item,
                           action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@people_bp.route("/ownership/<int:oid>/edit", methods=["GET", "POST"])
def ownership_edit(oid):
    ownership = Ownership.query.get_or_404(oid)
    if request.method == "POST":
        ownership.status = request.form.get("status", "Owned")
        raw_target = request.form.get("target_price", "").strip()
        try:
            ownership.target_price = float(raw_target) if raw_target else None
        except ValueError:
            ownership.target_price = None
        ownership.notes  = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash("Updated.", "success")
        return redirect(url_for("people.person_collection", pid=ownership.person_id))

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


@people_bp.route("/ownership/<int:oid>/delete", methods=["POST"])
def ownership_delete(oid):
    ownership = Ownership.query.get_or_404(oid)
    pid       = ownership.person_id
    db.session.delete(ownership)
    db.session.commit()
    flash("Entry removed.", "info")
    return redirect(url_for("people.person_collection", pid=pid))


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
def wishlist_check():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("people.wishlist"))
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

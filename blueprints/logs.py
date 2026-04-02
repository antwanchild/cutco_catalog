from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from constants import (
    BAKEWARE_CATEGORIES, BAKEWARE_THRESHOLD_DAYS,
    DISCORD_WEBHOOK_URL, SHARPEN_METHODS, SHARPEN_THRESHOLD_DAYS,
)
from extensions import db
from helpers import _notify_discord, is_admin
from models import BakewareSession, Item, SharpeningLog

logs_bp = Blueprint("logs", __name__)


# ── Sharpening Log ────────────────────────────────────────────────────────────

@logs_bp.route("/sharpening")
def sharpening():
    today       = date.today()
    all_entries = (SharpeningLog.query
                   .order_by(SharpeningLog.sharpened_on.desc())
                   .all())

    last_by_item: dict[int, str] = {}
    count_by_item: dict[int, int] = {}
    for entry in all_entries:
        count_by_item[entry.item_id] = count_by_item.get(entry.item_id, 0) + 1
        if entry.item_id not in last_by_item:
            last_by_item[entry.item_id] = entry.sharpened_on

    tracked: list[dict] = []
    for item_id, last_str in last_by_item.items():
        item = Item.query.get(item_id)
        if not item:
            continue
        days_since = (today - date.fromisoformat(last_str)).days
        tracked.append(dict(
            item       = item,
            last_date  = last_str,
            days_since = days_since,
            overdue    = days_since > SHARPEN_THRESHOLD_DAYS,
            event_count= count_by_item[item_id],
        ))

    tracked.sort(key=lambda row: (0 if row["overdue"] else 1, -row["days_since"]))

    return render_template(
        "sharpening.html",
        tracked         = tracked,
        recent_entries  = all_entries[:25],
        overdue_count   = sum(1 for row in tracked if row["overdue"]),
        threshold_days  = SHARPEN_THRESHOLD_DAYS,
        today           = today.isoformat(),
        items_list      = Item.query.order_by(Item.name).all(),
        methods         = SHARPEN_METHODS,
        has_discord     = bool(DISCORD_WEBHOOK_URL),
    )


@logs_bp.route("/sharpening/add", methods=["POST"])
def sharpening_add():
    item_id      = request.form.get("item_id", type=int)
    sharpened_on = request.form.get("sharpened_on", "").strip()
    method       = request.form.get("method", "Home Sharpener").strip()
    notes        = request.form.get("notes", "").strip() or None

    if not item_id or not sharpened_on:
        flash("Item and date are required.", "error")
        return redirect(url_for("logs.sharpening"))

    if not Item.query.get(item_id):
        flash("Item not found.", "error")
        return redirect(url_for("logs.sharpening"))

    db.session.add(SharpeningLog(
        item_id      = item_id,
        sharpened_on = sharpened_on,
        method       = method,
        notes        = notes,
    ))
    db.session.commit()
    flash("Sharpening event logged.", "success")
    return redirect(url_for("logs.sharpening"))


@logs_bp.route("/sharpening/<int:lid>/edit", methods=["GET", "POST"])
def sharpening_edit(lid):
    entry = SharpeningLog.query.get_or_404(lid)
    if request.method == "POST":
        entry.sharpened_on = request.form.get("sharpened_on", entry.sharpened_on).strip()
        entry.method       = request.form.get("method", entry.method).strip()
        entry.notes        = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash("Event updated.", "success")
        return redirect(url_for("logs.sharpening"))
    return render_template("sharpening_edit.html", entry=entry, methods=SHARPEN_METHODS)


@logs_bp.route("/sharpening/<int:lid>/delete", methods=["POST"])
def sharpening_delete(lid):
    entry = SharpeningLog.query.get_or_404(lid)
    db.session.delete(entry)
    db.session.commit()
    flash("Event removed.", "info")
    return redirect(url_for("logs.sharpening"))


@logs_bp.route("/sharpening/notify", methods=["POST"])
def sharpening_notify():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("logs.sharpening"))

    today       = date.today()
    all_entries = SharpeningLog.query.order_by(SharpeningLog.sharpened_on.desc()).all()
    last_by_item: dict[int, str] = {}
    for entry in all_entries:
        if entry.item_id not in last_by_item:
            last_by_item[entry.item_id] = entry.sharpened_on

    overdue = []
    for item_id, last_str in last_by_item.items():
        days_since = (today - date.fromisoformat(last_str)).days
        if days_since > SHARPEN_THRESHOLD_DAYS:
            item = Item.query.get(item_id)
            if item:
                overdue.append((item, days_since))
    overdue.sort(key=lambda pair: pair[1], reverse=True)

    if not overdue:
        flash(f"No knives overdue for sharpening (threshold: {SHARPEN_THRESHOLD_DAYS} days).", "info")
        return redirect(url_for("logs.sharpening"))

    if DISCORD_WEBHOOK_URL:
        lines = [f"**🔪 Cutco Sharpening Reminder — {len(overdue)} overdue**"]
        for item, days in overdue:
            lines.append(f"• {item.name} — {days} days since last sharpening")
        _notify_discord("\n".join(lines))
        flash(f"Sent reminder for {len(overdue)} overdue knife(s) to Discord.", "success")
    else:
        flash(
            f"{len(overdue)} overdue — set DISCORD_WEBHOOK_URL to enable notifications.",
            "info",
        )
    return redirect(url_for("logs.sharpening"))


# ── Bakeware ──────────────────────────────────────────────────────────────────

@logs_bp.route("/bakeware")
def bakeware():
    today       = date.today()
    all_sessions = (BakewareSession.query
                    .order_by(BakewareSession.baked_on.desc())
                    .all())

    last_by_item:   dict[int, str]   = {}
    count_by_item:  dict[int, int]   = {}
    rating_by_item: dict[int, list]  = {}
    for session in all_sessions:
        iid = session.item_id
        count_by_item[iid] = count_by_item.get(iid, 0) + 1
        if iid not in last_by_item:
            last_by_item[iid] = session.baked_on
        if session.rating is not None:
            rating_by_item.setdefault(iid, []).append(session.rating)

    tracked: list[dict] = []
    for iid, last_str in last_by_item.items():
        item = Item.query.get(iid)
        if not item:
            continue
        days_since = (today - date.fromisoformat(last_str)).days
        ratings    = rating_by_item.get(iid, [])
        tracked.append(dict(
            item       = item,
            last_date  = last_str,
            days_since = days_since,
            stale      = days_since > BAKEWARE_THRESHOLD_DAYS,
            session_count = count_by_item[iid],
            avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None,
        ))

    tracked.sort(key=lambda row: (0 if row["stale"] else 1, -row["days_since"]))

    used_ids = set(last_by_item.keys())
    never_used = (Item.query
                  .filter(Item.category.in_(BAKEWARE_CATEGORIES))
                  .filter(Item.id.notin_(used_ids))
                  .order_by(Item.name)
                  .all()) if BAKEWARE_CATEGORIES else []

    bakeware_items = (Item.query
                      .filter(Item.category.in_(BAKEWARE_CATEGORIES))
                      .order_by(Item.name).all()) if BAKEWARE_CATEGORIES else []
    other_items    = (Item.query
                      .filter(Item.category.notin_(BAKEWARE_CATEGORIES))
                      .order_by(Item.name).all()) if BAKEWARE_CATEGORIES else Item.query.order_by(Item.name).all()

    return render_template(
        "bakeware.html",
        tracked          = tracked,
        recent_sessions  = all_sessions[:25],
        stale_count      = sum(1 for row in tracked if row["stale"]),
        never_used       = never_used,
        threshold_days   = BAKEWARE_THRESHOLD_DAYS,
        today            = today.isoformat(),
        bakeware_items   = bakeware_items,
        other_items      = other_items,
        has_discord      = bool(DISCORD_WEBHOOK_URL),
    )


@logs_bp.route("/bakeware/add", methods=["POST"])
def bakeware_add():
    item_id   = request.form.get("item_id", type=int)
    baked_on  = request.form.get("baked_on", "").strip()
    what_made = request.form.get("what_made", "").strip()
    raw_rating = request.form.get("rating", "").strip()
    notes     = request.form.get("notes", "").strip() or None

    if not item_id or not baked_on or not what_made:
        flash("Item, date, and what you made are required.", "error")
        return redirect(url_for("logs.bakeware"))
    if not Item.query.get(item_id):
        flash("Item not found.", "error")
        return redirect(url_for("logs.bakeware"))

    try:
        rating = int(raw_rating) if raw_rating else None
        if rating is not None and not (1 <= rating <= 5):
            rating = None
    except ValueError:
        rating = None

    db.session.add(BakewareSession(
        item_id  = item_id,
        baked_on = baked_on,
        what_made = what_made,
        rating   = rating,
        notes    = notes,
    ))
    db.session.commit()
    flash("Baking session logged.", "success")
    return redirect(url_for("logs.bakeware"))


@logs_bp.route("/bakeware/<int:sid>/edit", methods=["GET", "POST"])
def bakeware_edit(sid):
    session = BakewareSession.query.get_or_404(sid)
    if request.method == "POST":
        session.baked_on  = request.form.get("baked_on", session.baked_on).strip()
        session.what_made = request.form.get("what_made", "").strip() or session.what_made
        session.notes     = request.form.get("notes", "").strip() or None
        raw_rating = request.form.get("rating", "").strip()
        try:
            rating = int(raw_rating) if raw_rating else None
            session.rating = rating if (rating is None or 1 <= rating <= 5) else session.rating
        except ValueError:
            pass
        db.session.commit()
        flash("Session updated.", "success")
        return redirect(url_for("logs.bakeware"))
    return render_template("bakeware_edit.html", session=session)


@logs_bp.route("/bakeware/<int:sid>/delete", methods=["POST"])
def bakeware_delete(sid):
    session = BakewareSession.query.get_or_404(sid)
    db.session.delete(session)
    db.session.commit()
    flash("Session removed.", "info")
    return redirect(url_for("logs.bakeware"))


@logs_bp.route("/bakeware/notify", methods=["POST"])
def bakeware_notify():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("logs.bakeware"))

    today        = date.today()
    all_sessions = BakewareSession.query.order_by(BakewareSession.baked_on.desc()).all()
    last_by_item: dict[int, str] = {}
    for session in all_sessions:
        if session.item_id not in last_by_item:
            last_by_item[session.item_id] = session.baked_on

    stale = []
    for iid, last_str in last_by_item.items():
        days_since = (today - date.fromisoformat(last_str)).days
        if days_since > BAKEWARE_THRESHOLD_DAYS:
            item = Item.query.get(iid)
            if item:
                stale.append((item, days_since))
    stale.sort(key=lambda pair: pair[1], reverse=True)

    if not stale:
        flash(f"No bakeware unused for >{BAKEWARE_THRESHOLD_DAYS} days.", "info")
        return redirect(url_for("logs.bakeware"))

    if DISCORD_WEBHOOK_URL:
        lines = [f"**🍰 Bakeware Reminder — {len(stale)} item(s) unused**"]
        for item, days in stale:
            lines.append(f"• {item.name} — {days} days since last use")
        _notify_discord("\n".join(lines))
        flash(f"Sent reminder for {len(stale)} idle bakeware item(s) to Discord.", "success")
    else:
        flash(
            f"{len(stale)} item(s) idle — set DISCORD_WEBHOOK_URL to enable notifications.",
            "info",
        )
    return redirect(url_for("logs.bakeware"))

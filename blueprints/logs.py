import logging
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from constants import (
    BAKEWARE_CATEGORIES, BAKEWARE_THRESHOLD_DAYS,
    DISCORD_WEBHOOK_URL, SHARPEN_METHODS, SHARPEN_THRESHOLD_DAYS,
)
from extensions import db
from helpers import _notify_discord, admin_required, db_commit
from models import BakewareSession, Item, KnifeTask, KnifeTaskLog, Ownership, SharpeningLog

logs_bp = Blueprint("logs", __name__)
logger = logging.getLogger(__name__)


def _safe_parse_iso_date(raw: str) -> date | None:
    """Return parsed ISO date or None when invalid."""
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


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
        parsed_last = _safe_parse_iso_date(last_str)
        if not parsed_last:
            logger.warning("Skipping invalid sharpening date for item_id=%s: %r", item_id, last_str)
            continue
        days_since = (today - parsed_last).days
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
@admin_required
def sharpening_add():
    item_id      = request.form.get("item_id", type=int)
    sharpened_on = request.form.get("sharpened_on", "").strip()
    method       = request.form.get("method", "Home Sharpener").strip()
    notes        = request.form.get("notes", "").strip() or None

    if not item_id or not sharpened_on:
        flash("Item and date are required.", "error")
        return redirect(url_for("logs.sharpening"))
    if not _safe_parse_iso_date(sharpened_on):
        flash("Date must be valid YYYY-MM-DD.", "error")
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
    if db_commit(db.session):
        logger.info("Sharpening logged: item %d on %s (%s)", item_id, sharpened_on, method)
        flash("Sharpening event logged.", "success")
    return redirect(url_for("logs.sharpening"))


@logs_bp.route("/sharpening/<int:lid>/edit", methods=["GET", "POST"])
@admin_required
def sharpening_edit(lid):
    entry = SharpeningLog.query.get_or_404(lid)
    if request.method == "POST":
        new_date = request.form.get("sharpened_on", entry.sharpened_on).strip()
        if not _safe_parse_iso_date(new_date):
            flash("Date must be valid YYYY-MM-DD.", "error")
            return redirect(url_for("logs.sharpening"))
        entry.sharpened_on = new_date
        entry.method       = request.form.get("method", entry.method).strip()
        entry.notes        = request.form.get("notes", "").strip() or None
        if db_commit(db.session):
            logger.info("Sharpening entry %d updated", lid)
            flash("Event updated.", "success")
        return redirect(url_for("logs.sharpening"))
    return render_template("sharpening_edit.html", entry=entry, methods=SHARPEN_METHODS)


@logs_bp.route("/sharpening/<int:lid>/delete", methods=["POST"])
@admin_required
def sharpening_delete(lid):
    entry = SharpeningLog.query.get_or_404(lid)
    db.session.delete(entry)
    if db_commit(db.session):
        logger.info("Sharpening entry %d deleted", lid)
        flash("Event removed.", "info")
    return redirect(url_for("logs.sharpening"))


@logs_bp.route("/sharpening/item/<int:iid>/purge", methods=["POST"])
@admin_required
def sharpening_purge_item(iid):
    item = Item.query.get_or_404(iid)
    count = SharpeningLog.query.filter_by(item_id=iid).count()
    SharpeningLog.query.filter_by(item_id=iid).delete()
    if db_commit(db.session):
        logger.info("Sharpening logs purged for item %d (%d entries)", iid, count)
        flash(f"Removed all {count} sharpening event{'s' if count != 1 else ''} for {item.name}.", "info")
    return redirect(url_for("logs.sharpening"))


@logs_bp.route("/sharpening/purge-all", methods=["POST"])
@admin_required
def sharpening_purge_all():
    count = SharpeningLog.query.count()
    SharpeningLog.query.delete()
    if db_commit(db.session):
        logger.info("All sharpening logs purged (%d entries)", count)
        flash(f"Removed all {count} sharpening event{'s' if count != 1 else ''}.", "info")
    return redirect(url_for("logs.sharpening"))


@logs_bp.route("/sharpening/notify", methods=["POST"])
@admin_required
def sharpening_notify():
    today       = date.today()
    all_entries = SharpeningLog.query.order_by(SharpeningLog.sharpened_on.desc()).all()
    last_by_item: dict[int, str] = {}
    for entry in all_entries:
        if entry.item_id not in last_by_item:
            last_by_item[entry.item_id] = entry.sharpened_on

    overdue = []
    for item_id, last_str in last_by_item.items():
        parsed_last = _safe_parse_iso_date(last_str)
        if not parsed_last:
            logger.warning("Skipping invalid sharpening date for item_id=%s: %r", item_id, last_str)
            continue
        days_since = (today - parsed_last).days
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

@logs_bp.route("/cookware", endpoint="cookware")
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
        parsed_last = _safe_parse_iso_date(last_str)
        if not parsed_last:
            logger.warning("Skipping invalid bakeware date for item_id=%s: %r", iid, last_str)
            continue
        days_since = (today - parsed_last).days
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
        "cookware.html",
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
@admin_required
def bakeware_add():
    item_id   = request.form.get("item_id", type=int)
    baked_on  = request.form.get("baked_on", "").strip()
    what_made = request.form.get("what_made", "").strip()
    raw_rating = request.form.get("rating", "").strip()
    notes     = request.form.get("notes", "").strip() or None

    if not item_id or not baked_on or not what_made:
        flash("Item, date, and what you made are required.", "error")
        return redirect(url_for("logs.cookware"))
    if not _safe_parse_iso_date(baked_on):
        flash("Date must be valid YYYY-MM-DD.", "error")
        return redirect(url_for("logs.cookware"))
    if not Item.query.get(item_id):
        flash("Item not found.", "error")
        return redirect(url_for("logs.cookware"))

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
    if db_commit(db.session):
        logger.info("Cookware session logged: item %d on %s — %s", item_id, baked_on, what_made)
        flash("Baking session logged.", "success")
    return redirect(url_for("logs.cookware"))


@logs_bp.route("/bakeware/<int:sid>/edit", methods=["GET", "POST"])
@admin_required
def bakeware_edit(sid):
    session = BakewareSession.query.get_or_404(sid)
    if request.method == "POST":
        new_date = request.form.get("baked_on", session.baked_on).strip()
        if not _safe_parse_iso_date(new_date):
            flash("Date must be valid YYYY-MM-DD.", "error")
            return redirect(url_for("logs.cookware"))
        session.baked_on  = new_date
        session.what_made = request.form.get("what_made", "").strip() or session.what_made
        session.notes     = request.form.get("notes", "").strip() or None
        raw_rating = request.form.get("rating", "").strip()
        try:
            rating = int(raw_rating) if raw_rating else None
            session.rating = rating if (rating is None or 1 <= rating <= 5) else session.rating
        except ValueError:
            pass
        if db_commit(db.session):
            logger.info("Cookware session %d updated", sid)
            flash("Session updated.", "success")
        return redirect(url_for("logs.cookware"))
    return render_template("cookware_edit.html", session=session)


@logs_bp.route("/bakeware/<int:sid>/delete", methods=["POST"])
@admin_required
def bakeware_delete(sid):
    session = BakewareSession.query.get_or_404(sid)
    db.session.delete(session)
    if db_commit(db.session):
        logger.info("Cookware session %d deleted", sid)
        flash("Session removed.", "info")
    return redirect(url_for("logs.cookware"))


@logs_bp.route("/bakeware/item/<int:iid>/purge", methods=["POST"])
@admin_required
def bakeware_purge_item(iid):
    item = Item.query.get_or_404(iid)
    count = BakewareSession.query.filter_by(item_id=iid).count()
    BakewareSession.query.filter_by(item_id=iid).delete()
    if db_commit(db.session):
        logger.info("Cookware sessions purged for item %d (%d entries)", iid, count)
        flash(f"Removed all {count} cookware session{'s' if count != 1 else ''} for {item.name}.", "info")
    return redirect(url_for("logs.cookware"))


@logs_bp.route("/bakeware/purge-all", methods=["POST"])
@admin_required
def bakeware_purge_all():
    count = BakewareSession.query.count()
    BakewareSession.query.delete()
    if db_commit(db.session):
        logger.info("All cookware sessions purged (%d entries)", count)
        flash(f"Removed all {count} cookware session{'s' if count != 1 else ''}.", "info")
    return redirect(url_for("logs.cookware"))


@logs_bp.route("/bakeware/notify", methods=["POST"])
@admin_required
def bakeware_notify():
    today        = date.today()
    all_sessions = BakewareSession.query.order_by(BakewareSession.baked_on.desc()).all()
    last_by_item: dict[int, str] = {}
    for session in all_sessions:
        if session.item_id not in last_by_item:
            last_by_item[session.item_id] = session.baked_on

    stale = []
    for iid, last_str in last_by_item.items():
        parsed_last = _safe_parse_iso_date(last_str)
        if not parsed_last:
            logger.warning("Skipping invalid bakeware date for item_id=%s: %r", iid, last_str)
            continue
        days_since = (today - parsed_last).days
        if days_since > BAKEWARE_THRESHOLD_DAYS:
            item = Item.query.get(iid)
            if item:
                stale.append((item, days_since))
    stale.sort(key=lambda pair: pair[1], reverse=True)

    if not stale:
        flash(f"No cookware unused for >{BAKEWARE_THRESHOLD_DAYS} days.", "info")
        return redirect(url_for("logs.cookware"))

    if DISCORD_WEBHOOK_URL:
        lines = [f"**🍳 Cookware Reminder — {len(stale)} item(s) unused**"]
        for item, days in stale:
            lines.append(f"• {item.name} — {days} days since last use")
        _notify_discord("\n".join(lines))
        flash(f"Sent reminder for {len(stale)} idle cookware item(s) to Discord.", "success")
    else:
        flash(
            f"{len(stale)} item(s) idle — set DISCORD_WEBHOOK_URL to enable notifications.",
            "info",
        )
    return redirect(url_for("logs.cookware"))


# ── Knife Task Pairing ────────────────────────────────────────────────────────

@logs_bp.route("/tasks")
def tasks():
    today = date.today()

    # Only items that are owned by anyone
    owned_item_ids = {
        o.variant.item_id
        for o in Ownership.query.filter_by(status="Owned").all()
    }
    owned_items = (Item.query
                   .filter(Item.id.in_(owned_item_ids))
                   .order_by(Item.name).all()) if owned_item_ids else []

    all_tasks = KnifeTask.query.order_by(KnifeTask.name).all()
    recent_entries = (KnifeTaskLog.query
                      .order_by(KnifeTaskLog.logged_on.desc(), KnifeTaskLog.id.desc())
                      .limit(50).all())

    # Build per-item task usage summary: item_id → task_id → count
    all_log = KnifeTaskLog.query.all()
    usage: dict[int, dict[int, int]] = {}
    for entry in all_log:
        usage.setdefault(entry.item_id, {})
        usage[entry.item_id][entry.task_id] = usage[entry.item_id].get(entry.task_id, 0) + 1

    # Top task per owned item
    item_top_task: dict[int, str] = {}
    for iid, task_counts in usage.items():
        top_tid = max(task_counts, key=task_counts.get)
        top_task = KnifeTask.query.get(top_tid)
        if top_task:
            item_top_task[iid] = top_task.name

    # Suggested task IDs per item (from Cutco uses sync) — used for JS filtering
    item_tasks_map: dict[int, list[int]] = {
        item.id: [t.id for t in item.suggested_tasks]
        for item in owned_items
        if item.suggested_tasks
    }

    return render_template(
        "tasks.html",
        owned_items    = owned_items,
        all_tasks      = all_tasks,
        recent_entries = recent_entries,
        item_top_task  = item_top_task,
        item_tasks_map = item_tasks_map,
        today          = today.isoformat(),
    )


@logs_bp.route("/tasks/add", methods=["POST"])
@admin_required
def task_log_add():
    item_id   = request.form.get("item_id", type=int)
    task_id   = request.form.get("task_id", type=int)
    logged_on = request.form.get("logged_on", "").strip()
    notes     = request.form.get("notes", "").strip() or None

    if not item_id or not task_id or not logged_on:
        flash("Item, task, and date are required.", "error")
        return redirect(url_for("logs.tasks"))
    if not _safe_parse_iso_date(logged_on):
        flash("Date must be valid YYYY-MM-DD.", "error")
        return redirect(url_for("logs.tasks"))
    if not Item.query.get(item_id):
        flash("Item not found.", "error")
        return redirect(url_for("logs.tasks"))
    if not KnifeTask.query.get(task_id):
        flash("Task not found.", "error")
        return redirect(url_for("logs.tasks"))

    db.session.add(KnifeTaskLog(
        item_id   = item_id,
        task_id   = task_id,
        logged_on = logged_on,
        notes     = notes,
    ))
    if db_commit(db.session):
        item = Item.query.get(item_id)
        task = KnifeTask.query.get(task_id)
        logger.info("Task logged: %s → %s on %s", item.name, task.name, logged_on)
        flash("Usage logged.", "success")
    return redirect(url_for("logs.tasks"))


@logs_bp.route("/tasks/log/<int:lid>/delete", methods=["POST"])
@admin_required
def task_log_delete(lid):
    entry = KnifeTaskLog.query.get_or_404(lid)
    db.session.delete(entry)
    if db_commit(db.session):
        logger.info("Task log entry %d deleted", lid)
        flash("Entry removed.", "info")
    return redirect(url_for("logs.tasks"))


@logs_bp.route("/tasks/item/<int:iid>/purge", methods=["POST"])
@admin_required
def task_log_purge_item(iid):
    item = Item.query.get_or_404(iid)
    count = KnifeTaskLog.query.filter_by(item_id=iid).count()
    KnifeTaskLog.query.filter_by(item_id=iid).delete()
    if db_commit(db.session):
        logger.info("Task logs purged for item %d (%d entries)", iid, count)
        flash(f"Removed all {count} task log entr{'ies' if count != 1 else 'y'} for {item.name}.", "info")
    return redirect(url_for("logs.tasks"))


@logs_bp.route("/tasks/purge-all", methods=["POST"])
@admin_required
def task_log_purge_all():
    count = KnifeTaskLog.query.count()
    KnifeTaskLog.query.delete()
    if db_commit(db.session):
        logger.info("All task logs purged (%d entries)", count)
        flash(f"Removed all {count} task log entr{'ies' if count != 1 else 'y'}.", "info")
    return redirect(url_for("logs.tasks"))


@logs_bp.route("/tasks/manage")
def tasks_manage():
    all_tasks = KnifeTask.query.order_by(KnifeTask.is_preset.desc(), KnifeTask.name).all()
    return render_template("tasks_manage.html", all_tasks=all_tasks)


@logs_bp.route("/tasks/manage/<int:tid>")
def task_detail(tid):
    task = KnifeTask.query.get_or_404(tid)
    # Items that have this task as a Cutco-sourced suggested use
    suggested_items = sorted(task.suggested_items, key=lambda i: i.name)
    # Log count per item for this task
    log_counts: dict[int, int] = {}
    for entry in task.log_entries:
        log_counts[entry.item_id] = log_counts.get(entry.item_id, 0) + 1
    return render_template(
        "task_detail.html",
        task            = task,
        suggested_items = suggested_items,
        log_counts      = log_counts,
        total_logs      = len(task.log_entries),
    )


@logs_bp.route("/tasks/manage/add", methods=["POST"])
@admin_required
def task_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Task name is required.", "error")
        return redirect(url_for("logs.tasks_manage"))
    if KnifeTask.query.filter(db.func.lower(KnifeTask.name) == name.lower()).first():
        flash(f'Task "{name}" already exists.', "error")
        return redirect(url_for("logs.tasks_manage"))
    db.session.add(KnifeTask(name=name, is_preset=False))
    if db_commit(db.session):
        logger.info("Knife task added: %s", name)
        flash(f'Task "{name}" added.', "success")
    return redirect(url_for("logs.tasks_manage"))


@logs_bp.route("/tasks/manage/<int:tid>/delete", methods=["POST"])
@admin_required
def task_delete(tid):
    task = KnifeTask.query.get_or_404(tid)
    if task.log_entries:
        flash(f'Cannot delete "{task.name}" — it has logged usage. Remove logs first.', "error")
        return redirect(url_for("logs.tasks_manage"))
    name = task.name
    db.session.delete(task)
    if db_commit(db.session):
        logger.info("Knife task deleted: %s", name)
        flash(f'Task "{name}" deleted.', "info")
    return redirect(url_for("logs.tasks_manage"))

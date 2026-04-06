import logging
import threading
from datetime import date

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from constants import ADMIN_SESSION_SECONDS, ADMIN_TOKEN
from extensions import limiter
from helpers import is_admin
from models import Item
from msrp_helpers import (
    _read_msrp_job, _run_msrp_diff_job, _write_msrp_job,
    _read_specs_job, _run_specs_backfill_job, _write_specs_job,
)

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)


@admin_bp.route("/admin/msrp-diff")
def msrp_diff_page():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    return render_template("msrp_diff_ui.html", job=_read_msrp_job())


@admin_bp.route("/admin/msrp-diff/run", methods=["POST"])
def msrp_diff_run():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    job = _read_msrp_job()
    if job["status"] == "running":
        flash("A diff is already running.", "warning")
        return redirect(url_for("admin.msrp_diff_page"))
    update_db = request.form.get("update_db") == "on"
    _write_msrp_job({"status": "running", "progress": [], "results": None,
                     "error": None, "update_db": update_db,
                     "started_at": date.today().isoformat(), "finished_at": None})
    from flask import current_app
    app = current_app._get_current_object()
    logger.info("MSRP diff job started (update_db=%s)", update_db)
    threading.Thread(target=_run_msrp_diff_job, args=(app, update_db,), daemon=True).start()
    return redirect(url_for("admin.msrp_diff_page"))


@admin_bp.route("/admin/msrp-diff/status")
def msrp_diff_status():
    if not is_admin():
        return jsonify(error="Unauthorized"), 403
    return jsonify(_read_msrp_job())


@admin_bp.route("/admin/specs-backfill")
def specs_backfill_page():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    return render_template("specs_backfill_ui.html", job=_read_specs_job())


@admin_bp.route("/admin/specs-backfill/run", methods=["POST"])
def specs_backfill_run():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    job = _read_specs_job()
    if job["status"] == "running":
        flash("A backfill is already running.", "warning")
        return redirect(url_for("admin.specs_backfill_page"))
    _write_specs_job({"status": "running", "progress": [], "results": None,
                      "error": None, "started_at": None, "finished_at": None})
    from flask import current_app
    app = current_app._get_current_object()
    logger.info("Specs backfill job started")
    threading.Thread(target=_run_specs_backfill_job, args=(app,), daemon=True).start()
    return redirect(url_for("admin.specs_backfill_page"))


@admin_bp.route("/admin/specs-backfill/status")
def specs_backfill_status():
    if not is_admin():
        return jsonify(error="Unauthorized"), 403
    return jsonify(_read_specs_job())


@admin_bp.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour")
def admin_login():
    if request.method == "POST":
        if request.form.get("token") == ADMIN_TOKEN:
            resp = redirect(url_for("catalog.catalog"))
            resp.set_cookie("admin_token", ADMIN_TOKEN, httponly=True, samesite="Lax",
                            max_age=ADMIN_SESSION_SECONDS)
            logger.info("Admin login successful")
            flash("Admin access granted.", "success")
            return resp
        logger.warning("Admin login failed — wrong token")
        flash("Wrong token.", "error")
    return render_template("admin_login.html")


@admin_bp.route("/admin/logout")
def admin_logout():
    logger.info("Admin logged out")
    resp = redirect(url_for("index"))
    resp.delete_cookie("admin_token")
    return resp


@admin_bp.route("/api/variants/<int:iid>")
def api_variants(iid):
    item = Item.query.get_or_404(iid)
    return jsonify([{"id": v.id, "color": v.color} for v in item.variants])

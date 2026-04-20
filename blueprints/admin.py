import logging
import os
import platform
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from datetime import date

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from constants import ADMIN_SESSION_SECONDS, ADMIN_TOKEN, APP_VERSION, get_git_sha
from extensions import db
from extensions import limiter
from helpers import is_admin
from models import Item
from schema_migrations import get_schema_history, get_schema_state, SCHEMA_VERSION
from startup import BOOTSTRAP_VERSION, get_bootstrap_history, get_bootstrap_state
from msrp_helpers import (
    _read_msrp_job, _run_msrp_diff_job, _write_msrp_job,
    _read_specs_job, _run_specs_backfill_job, _write_specs_job,
)

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)


def _mask_database_uri(uri):
    if not uri or uri.startswith("sqlite:"):
        return uri
    parsed = urlsplit(uri)
    if not parsed.scheme or not parsed.hostname:
        return uri
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password is not None:
            auth += ":***"
        auth += "@"
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def _read_pid1_cmdline():
    try:
        return Path("/proc/1/cmdline").read_text().replace("\x00", " ").strip()
    except OSError:
        return None


def _path_status(path):
    if not path:
        return {"path": None, "exists": False, "writable": False}
    candidate = Path(path)
    target = candidate if candidate.exists() else candidate.parent
    return {
        "path": str(candidate),
        "exists": candidate.exists(),
        "writable": os.access(target, os.W_OK),
    }


def _runtime_details():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    sqlite_file = db_uri.removeprefix("sqlite:////") if db_uri.startswith("sqlite:////") else None
    data_dir = os.environ.get("DATA_DIR", "/data")
    log_dir = os.environ.get("LOG_DIR", "/data/logs")
    return {
        "app_version": APP_VERSION,
        "git_sha": get_git_sha(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "home": os.environ.get("HOME", ""),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "database_uri": _mask_database_uri(db_uri),
        "database_file": sqlite_file,
        "log_dir": log_dir,
        "data_dir": data_dir,
        "log_level": os.environ.get("LOG_LEVEL", "INFO"),
        "tz": os.environ.get("TZ", "UTC"),
        "flask_env": os.environ.get("FLASK_ENV", "production"),
        "puid": os.environ.get("PUID", "0"),
        "pgid": os.environ.get("PGID", "0"),
        "pid1_cmdline": _read_pid1_cmdline(),
        "schema_state": get_schema_state(),
        "schema_history": get_schema_history(),
        "schema_version": SCHEMA_VERSION,
        "bootstrap_state": get_bootstrap_state(),
        "bootstrap_history": get_bootstrap_history(),
        "bootstrap_version": BOOTSTRAP_VERSION,
        "path_checks": [
            {"label": "Data Directory", **_path_status(data_dir)},
            {"label": "Log Directory", **_path_status(log_dir)},
            {"label": "SQLite File", **_path_status(sqlite_file)} if sqlite_file else None,
        ],
    }


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


@admin_bp.route("/admin/diagnostics")
def diagnostics_page():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    return render_template(
        "admin_diagnostics.html",
        details=_runtime_details(),
        msrp_job=_read_msrp_job(),
        specs_job=_read_specs_job(),
    )


@admin_bp.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour")
def admin_login():
    if request.method == "POST":
        if request.form.get("token") == ADMIN_TOKEN:
            session["is_admin"] = True
            session.permanent = ADMIN_SESSION_SECONDS > 0
            resp = redirect(url_for("catalog.catalog"))
            logger.info("Admin login successful")
            flash("Admin access granted.", "success")
            return resp
        logger.warning("Admin login failed — wrong token")
        flash("Wrong token.", "error")
    return render_template("admin_login.html")


@admin_bp.route("/admin/logout")
def admin_logout():
    logger.info("Admin logged out")
    session.pop("is_admin", None)
    return redirect(url_for("index"))


@admin_bp.route("/api/variants/<int:item_id>")
def api_variants(item_id):
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    return jsonify([{"id": v.id, "color": v.color} for v in item.variants])

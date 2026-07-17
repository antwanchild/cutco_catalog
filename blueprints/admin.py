"""Admin-only routes and runtime diagnostics."""

import logging
import os
import platform
import threading
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.exc import SQLAlchemyError

from constants import ADMIN_SESSION_SECONDS, APP_VERSION, get_git_sha_info
from extensions import db
from extensions import limiter
from helpers import (
    admin_token_matches,
    admin_required,
    authenticate_local_user,
    clear_auth_session,
    current_identity,
    current_user,
    db_commit,
    establish_token_admin_session,
    establish_user_session,
    is_admin,
    local_auth_enabled,
    proxy_auth_enabled,
    proxy_auth_failure,
    user_required,
    users_exist,
)
from models import (
    ActivityEvent,
    AuthSetupState,
    Item,
    USER_ROLE_ADMIN,
    User,
    get_recent_audit_events,
    record_audit_event,
)
from schema_migrations import get_schema_history, get_schema_state, SCHEMA_VERSION
from startup import BOOTSTRAP_VERSION, get_bootstrap_history, get_bootstrap_state
from time_utils import format_container_time
from msrp_jobs import (
    _read_msrp_job,
    _reset_msrp_job,
    _run_msrp_diff_job,
    _write_msrp_job,
    _read_specs_job,
    _run_specs_backfill_job,
    _write_specs_job,
)

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)
AUTH_SETUP_STATE_ID = 1


def _current_flask_app() -> Flask:
    """Return the real Flask app object instead of the request-local proxy."""
    return cast(Flask, cast(Any, current_app)._get_current_object())


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
    return urlunsplit(
        (parsed.scheme, f"{auth}{host}", parsed.path, parsed.query, parsed.fragment)
    )


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


def build_runtime_details() -> dict:
    """Build the runtime diagnostics payload."""
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    sqlite_file = (
        db_uri.removeprefix("sqlite:////") if db_uri.startswith("sqlite:////") else None
    )
    data_dir = os.environ.get("DATA_DIR", "/data")
    log_dir = os.environ.get("LOG_DIR", "/data/logs")
    git_sha, git_sha_source = get_git_sha_info()
    return {
        "app_version": APP_VERSION,
        "git_sha": git_sha,
        "git_sha_source": git_sha_source,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "home": os.environ.get("HOME", ""),
        "uid": getattr(os, "getuid", lambda: None)(),
        "gid": getattr(os, "getgid", lambda: None)(),
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
        "schema_history": [
            {
                **entry,
                "formatted_applied_at": format_container_time(entry.get("applied_at")),
            }
            for entry in get_schema_history()
        ],
        "schema_version": SCHEMA_VERSION,
        "bootstrap_state": get_bootstrap_state(),
        "bootstrap_history": [
            {
                **entry,
                "formatted_applied_at": format_container_time(entry.get("applied_at")),
            }
            for entry in get_bootstrap_history()
        ],
        "bootstrap_version": BOOTSTRAP_VERSION,
        "path_checks": [
            {"label": "Data Directory", **_path_status(data_dir)},
            {"label": "Log Directory", **_path_status(log_dir)},
            (
                {"label": "SQLite File", **_path_status(sqlite_file)}
                if sqlite_file
                else None
            ),
        ],
    }


@admin_bp.route("/admin/msrp-diff")
def msrp_diff_page():
    """Render the MSRP diff admin page."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    return render_template(
        "msrp_diff_ui.html",
        job=_read_msrp_job(),
        format_container_time=format_container_time,
    )


@admin_bp.route("/admin/msrp-diff/run", methods=["POST"])
def msrp_diff_run():
    """Start the MSRP diff background job."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    job = _read_msrp_job()
    if job["status"] == "running":
        flash("A diff is already running.", "warning")
        return redirect(url_for("admin.msrp_diff_page"))
    update_db = request.form.get("update_db") == "on"
    _write_msrp_job(
        {
            "status": "running",
            "progress": [],
            "results": None,
            "error": None,
            "update_db": update_db,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": None,
        }
    )
    app = _current_flask_app()
    logger.info("MSRP diff job started (update_db=%s)", update_db)
    threading.Thread(
        target=_run_msrp_diff_job,
        args=(
            app,
            update_db,
        ),
        daemon=True,
    ).start()
    return redirect(url_for("admin.msrp_diff_page"))


@admin_bp.route("/admin/msrp-diff/status")
def msrp_diff_status():
    """Return the current MSRP diff job status."""
    if not is_admin():
        return jsonify(error="Unauthorized"), 403
    return jsonify(_read_msrp_job())


@admin_bp.route("/admin/msrp-diff/reset", methods=["POST"])
def msrp_diff_reset():
    """Clear the persisted MSRP diff job state."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    _reset_msrp_job()
    flash("MSRP diff job state cleared.", "success")
    return redirect(url_for("admin.msrp_diff_page"))


@admin_bp.route("/admin/specs-backfill")
def specs_backfill_page():
    """Render the specs backfill admin page."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    return render_template(
        "specs_backfill_ui.html",
        job=_read_specs_job(),
        format_container_time=format_container_time,
    )


@admin_bp.route("/admin/specs-backfill/run", methods=["POST"])
def specs_backfill_run():
    """Start the specs backfill background job."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    job = _read_specs_job()
    if job["status"] == "running":
        flash("A backfill is already running.", "warning")
        return redirect(url_for("admin.specs_backfill_page"))
    _write_specs_job(
        {
            "status": "running",
            "progress": [],
            "results": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }
    )
    app = _current_flask_app()
    logger.info("Specs backfill job started")
    threading.Thread(target=_run_specs_backfill_job, args=(app,), daemon=True).start()
    return redirect(url_for("admin.specs_backfill_page"))


@admin_bp.route("/admin/specs-backfill/status")
def specs_backfill_status():
    """Return the current specs backfill job status."""
    if not is_admin():
        return jsonify(error="Unauthorized"), 403
    return jsonify(_read_specs_job())


@admin_bp.route("/admin/diagnostics")
def diagnostics_page():
    """Render the runtime diagnostics page."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))
    return render_template(
        "admin_diagnostics.html",
        details=build_runtime_details(),
        msrp_job=_read_msrp_job(),
        specs_job=_read_specs_job(),
    )


@admin_bp.route("/admin/audit")
def audit_page():
    """Render the audit history page."""
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("index"))

    action = (request.args.get("action", "") or "").strip().lower() or None
    entity_type = (request.args.get("entity_type", "") or "").strip() or None
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(25, min(limit, 250))

    entity_types = [
        row[0]
        for row in (
            db.session.execute(
                db.select(ActivityEvent.entity_type)
                .where(
                    ActivityEvent.kind == "audit", ActivityEvent.entity_type.isnot(None)
                )
                .distinct()
                .order_by(ActivityEvent.entity_type)
            ).all()
        )
    ]
    events = get_recent_audit_events(
        limit=limit, action=action, entity_type=entity_type
    )
    return render_template(
        "admin_audit.html",
        events=events,
        limit=limit,
        action=action or "",
        entity_type=entity_type or "",
        available_actions=["create", "update", "delete"],
        entity_types=entity_types,
    )


@admin_bp.context_processor
def inject_admin_status_strip():
    """Expose the admin status strip to admin templates."""
    if (
        not is_admin()
        or not request.endpoint
        or not request.endpoint.startswith("admin.")
    ):
        return {}
    git_sha, git_sha_source = get_git_sha_info()
    return {
        "admin_status_strip": {
            "app_version": APP_VERSION,
            "git_sha": git_sha,
            "git_sha_source": git_sha_source,
            "tz": os.environ.get("TZ", "UTC"),
            "msrp_status": _read_msrp_job().get("status", "unknown"),
            "specs_status": _read_specs_job().get("status", "unknown"),
        }
    }


@admin_bp.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour")
def admin_login():
    """Handle local, token-bootstrap, and trusted-proxy login requests."""
    identity = current_identity()
    if identity is not None:
        return redirect(
            url_for("admin.diagnostics_page") if identity.is_admin else url_for("index")
        )
    if request.method == "POST":
        token_attempt = (
            request.form.get("login_type") == "token" or "token" in request.form
        )
        if token_attempt:
            if not users_exist() and admin_token_matches(request.form.get("token", "")):
                establish_token_admin_session()
                session.permanent = (
                    int(
                        current_app.config.get("SESSION_SECONDS", ADMIN_SESSION_SECONDS)
                    )
                    > 0
                )
                logger.info("Admin token bootstrap login successful")
                flash("Admin access granted. Create a named admin account.", "success")
                return redirect(url_for("catalog.catalog"))
            logger.warning("Admin token bootstrap login failed")
            flash("Token login is unavailable or the token is invalid.", "error")
        else:
            if not local_auth_enabled():
                flash("Local password login is disabled in proxy-only mode.", "error")
                return redirect(url_for("admin.admin_login"))
            user = authenticate_local_user(
                request.form.get("username", ""),
                request.form.get("password", ""),
            )
            if user is not None:
                user.last_login_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                if db_commit(
                    db.session,
                    error_msg="Could not start your session — please try again.",
                ):
                    establish_user_session(user)
                    session.permanent = (
                        int(
                            current_app.config.get(
                                "SESSION_SECONDS", ADMIN_SESSION_SECONDS
                            )
                        )
                        > 0
                    )
                    logger.info("Local login successful for user_id=%s", user.id)
                    flash("Signed in.", "success")
                    if user.must_change_password:
                        return redirect(url_for("admin.account_password"))
                    return redirect(url_for("index"))
            logger.warning("Local login failed")
            flash("Invalid username or password.", "error")
    return render_template(
        "admin_login.html",
        setup_available=not users_exist() and local_auth_enabled(),
        token_login_available=not users_exist(),
        local_login_available=local_auth_enabled(),
        proxy_login_enabled=proxy_auth_enabled(),
        proxy_error=proxy_auth_failure(),
    )


@admin_bp.route("/setup", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def initial_setup():
    """Create the first named administrator using the bootstrap token."""
    if not local_auth_enabled():
        flash(
            "Local setup is disabled in proxy-only mode. Use bootstrap token "
            "access or the user CLI to provision a proxy administrator.",
            "error",
        )
        return redirect(url_for("admin.admin_login"))
    setup_claim = db.session.get(AuthSetupState, AUTH_SETUP_STATE_ID)
    if users_exist() or setup_claim is not None:
        flash("Initial account setup is already complete.", "info")
        return redirect(url_for("admin.admin_login"))

    if request.method == "POST":
        username = request.form.get("username", "")
        display_name = request.form.get("display_name", "").strip() or None
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        if not admin_token_matches(request.form.get("token", "")):
            flash("The setup token is invalid.", "error")
        elif password != password_confirm:
            flash("Passwords do not match.", "error")
        elif display_name and len(display_name) > 160:
            flash("Display name must be 160 characters or fewer.", "error")
        else:
            try:
                user = User(
                    username=username,
                    display_name=display_name,
                    role=USER_ROLE_ADMIN,
                )
                user.set_password(password)
                db.session.add(user)
                db.session.flush()
                completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                db.session.add(
                    AuthSetupState(
                        id=AUTH_SETUP_STATE_ID,
                        user_id=user.id,
                        completed_at=completed_at,
                    )
                )
                record_audit_event(
                    title="Created initial administrator",
                    actor=user.username,
                    actor_user_id=user.id,
                    action="create",
                    entity_type="User",
                    entity_id=user.id,
                    entity_name=user.label,
                    payload={
                        "role": user.role,
                        "auth_source": user.auth_source,
                    },
                )
                db.session.commit()
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            except SQLAlchemyError as exc:
                db.session.rollback()
                logger.warning("Initial account setup conflict: %s", exc)
                flash(
                    "Initial setup could not be completed. It may already be claimed.",
                    "error",
                )
            else:
                establish_user_session(user)
                session.permanent = (
                    int(
                        current_app.config.get("SESSION_SECONDS", ADMIN_SESSION_SECONDS)
                    )
                    > 0
                )
                logger.info("Initial named administrator created user_id=%s", user.id)
                flash("Administrator account created.", "success")
                return redirect(url_for("admin.diagnostics_page"))

    return render_template("initial_setup.html")


@admin_bp.route("/account/password", methods=["GET", "POST"])
@user_required
def account_password():
    """Allow a local named user to change their password."""
    user = current_user()
    if user is None or not user.password_hash:
        flash("Password changes are available only for local accounts.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        password_confirm = request.form.get("password_confirm", "")
        if not user.check_password(current_password):
            flash("Current password is incorrect.", "error")
        elif new_password != password_confirm:
            flash("New passwords do not match.", "error")
        elif user.check_password(new_password):
            flash("Choose a password different from your current password.", "error")
        else:
            try:
                user.set_password(new_password)
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                user.revoke_sessions()
                record_audit_event(
                    title="Changed account password",
                    actor=user.username,
                    actor_user_id=user.id,
                    action="update",
                    entity_type="User",
                    entity_id=user.id,
                    entity_name=user.label,
                    payload={"session_version": user.session_version},
                )
                if db_commit(
                    db.session,
                    error_msg="Could not change your password — please try again.",
                ):
                    establish_user_session(user)
                    session.permanent = (
                        int(
                            current_app.config.get(
                                "SESSION_SECONDS", ADMIN_SESSION_SECONDS
                            )
                        )
                        > 0
                    )
                    flash("Password changed and other sessions revoked.", "success")
                    return redirect(url_for("index"))

    return render_template("account_password.html", user=user)


@admin_bp.route("/admin/logout", methods=["POST"])
def admin_logout():
    """Log the admin user out."""
    logger.info("Admin logged out")
    clear_auth_session()
    return redirect(url_for("index"))


@admin_bp.route("/admin")
def admin_root():
    """Send admin traffic to a useful landing page."""
    if is_admin():
        return redirect(url_for("admin.diagnostics_page"))
    return redirect(url_for("admin.admin_login"))


@admin_bp.route("/api/variants/<int:item_id>")
@admin_required
def api_variants(item_id):
    """Return item variants as JSON for admin tooling."""
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)
    return jsonify([{"id": v.id, "color": v.color} for v in item.variants])

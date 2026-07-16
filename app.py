"""Flask application factory, logging setup, and global route wiring."""

import logging
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func

from constants import (
    ADMIN_SESSION_SECONDS,
    ADMIN_TOKEN,
    APP_VERSION,
    GIT_SHA,
    DATA_DIR,
    UNKNOWN_COLOR,
    get_git_sha_info,
)
from extensions import db, limiter
from helpers import (
    _csrf_token,
    current_identity,
    current_user,
    is_admin,
    is_authenticated_user,
    validate_csrf,
)
from models import Item, KnifeTask, Ownership, Person, Set, get_latest_activity
from schema_migrations import get_schema_history, get_schema_state
from startup import get_bootstrap_history, get_bootstrap_state
from time_utils import format_container_time
from startup import initialize_database

logger = logging.getLogger(__name__)
_LOG_FORMAT = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)
_LOGGING_READY = False
_FILE_LOG_PATHS: set[str] = set()
CSRF_EXEMPT: set[str] = set()


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.lower() in {"1", "true", "yes", "on"}


def _setup_logging(log_dir: str, log_level: str) -> None:
    global _LOGGING_READY

    root_logger = logging.getLogger()
    resolved_level = getattr(logging, log_level.upper(), logging.INFO)

    if not _LOGGING_READY:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(_LOG_FORMAT)
        logging.basicConfig(level=resolved_level, handlers=[console_handler])
        _LOGGING_READY = True
    else:
        root_logger.setLevel(resolved_level)

    if log_dir in _FILE_LOG_PATHS:
        return

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "cutco.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setFormatter(_LOG_FORMAT)
        root_logger.addHandler(file_handler)
        _FILE_LOG_PATHS.add(log_dir)
        logger.info("File logging active: %s", log_dir)
    except OSError as exc:
        logger.warning("Could not set up file logging (%s) - console only", exc)


def _teardown_logging(log_dir: str) -> None:
    root_logger = logging.getLogger()
    log_path = os.path.join(log_dir, "cutco.log")

    for handler in list(root_logger.handlers):
        if (
            isinstance(handler, RotatingFileHandler)
            and handler.baseFilename == log_path
        ):
            root_logger.removeHandler(handler)
            handler.close()
            _FILE_LOG_PATHS.discard(log_dir)
            logger.info("File logging stopped: %s", log_dir)


def _register_blueprints(app: Flask) -> None:
    from blueprints.admin import admin_bp
    from blueprints.catalog import catalog_bp
    from blueprints.data import data_bp
    from blueprints.logs import logs_bp
    from blueprints.people import people_bp
    from blueprints.user_admin import user_admin_bp
    from blueprints.views import views_bp

    app.register_blueprint(catalog_bp)
    app.register_blueprint(people_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(user_admin_bp)


def _register_cli(app: Flask) -> None:
    from user_cli import register_user_cli

    register_user_cli(app)


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def csrf_protect():
        if (
            request.method == "POST"
            and not request.path.startswith("/api/")
            and request.path not in CSRF_EXEMPT
        ):
            validate_csrf()

    @app.before_request
    def enforce_required_password_change():
        user = current_user()
        if user is None or not user.must_change_password:
            return None
        allowed_endpoints = {
            "admin.account_password",
            "admin.admin_logout",
            "static",
        }
        if request.endpoint not in allowed_endpoints:
            flash("Change your temporary password before continuing.", "warning")
            return redirect(url_for("admin.account_password"))
        return None

    @app.after_request
    def set_security_headers(response):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    @app.context_processor
    def inject_globals():
        return dict(
            app_version=APP_VERSION,
            current_year=datetime.now().year,
            current_identity=current_identity,
            current_user=current_user,
            is_admin=is_admin,
            is_private_user=is_authenticated_user,
            UNKNOWN_COLOR=UNKNOWN_COLOR,
            csrf_token=_csrf_token,
        )


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def err_403(error):
        return (
            render_template(
                "error.html", code=403, icon="🚫", message="Access denied."
            ),
            403,
        )

    @app.errorhandler(404)
    def err_404(error):
        return (
            render_template(
                "error.html", code=404, icon="🔍", message="Page not found."
            ),
            404,
        )

    @app.errorhandler(429)
    def err_429(error):
        return (
            render_template(
                "error.html",
                code=429,
                icon="⏱️",
                message="Too many requests - slow down and try again shortly.",
            ),
            429,
        )

    @app.errorhandler(413)
    def err_413(error):
        return (
            render_template(
                "error.html",
                code=413,
                icon="📦",
                message="Request too large - the upload or submitted form data exceeded the 10 MB limit.",
            ),
            413,
        )

    @app.errorhandler(500)
    def err_500(error):
        db.session.rollback()
        logger.error("Unhandled 500: %s", error)
        return (
            render_template(
                "error.html",
                code=500,
                icon="💥",
                message="Something went wrong on our end. Try again or check the logs.",
            ),
            500,
        )


def _register_routes(app: Flask) -> None:
    def _recent_activity() -> list[dict]:
        activity_rows: list[dict] = []

        def _event_row(label: str, kind: str, empty_text: str) -> dict:
            event = get_latest_activity(kind)
            if event:
                url = None
                if kind == "import":
                    url = url_for("data.import_page")
                elif kind == "sync":
                    url = url_for("catalog.catalog_sync")
                elif kind == "msrp_diff":
                    url = url_for("admin.msrp_diff_page")
                elif kind in {"schema", "bootstrap"}:
                    url = url_for("admin.diagnostics_page")
                return {
                    "label": label,
                    "title": event["title"],
                    "details": event.get("details"),
                    "time": format_container_time(event.get("occurred_at")),
                    "url": url,
                }
            return {
                "label": label,
                "title": empty_text,
                "details": None,
                "time": "—",
                "url": None,
            }

        activity_rows.append(_event_row("Last Import", "import", "No imports yet."))
        activity_rows.append(
            _event_row("Last Catalog Sync", "sync", "No catalog sync yet.")
        )
        activity_rows.append(
            _event_row("Last MSRP Diff", "msrp_diff", "No MSRP diff yet.")
        )

        schema_history = get_schema_history(limit=1)
        schema_state = get_schema_state()
        if schema_history:
            latest_schema = schema_history[0]
            activity_rows.append(
                {
                    "label": "Schema Update",
                    "title": f"v{schema_state['version']}",
                    "details": latest_schema["name"],
                    "time": format_container_time(latest_schema["applied_at"]),
                }
            )
        else:
            activity_rows.append(
                {
                    "label": "Schema Update",
                    "title": "No schema updates yet.",
                    "details": None,
                    "time": "—",
                }
            )

        bootstrap_history = get_bootstrap_history(limit=1)
        bootstrap_state = get_bootstrap_state()
        if bootstrap_history:
            latest_bootstrap = bootstrap_history[0]
            activity_rows.append(
                {
                    "label": "Bootstrap Update",
                    "title": f"v{bootstrap_state['version']}",
                    "details": latest_bootstrap["name"],
                    "time": format_container_time(latest_bootstrap["applied_at"]),
                }
            )
        else:
            activity_rows.append(
                {
                    "label": "Bootstrap Update",
                    "title": "No bootstrap updates yet.",
                    "details": None,
                    "time": "—",
                }
            )

        return activity_rows

    def _release_snapshot() -> dict:
        git_sha, git_sha_source = get_git_sha_info()
        return {
            "app_version": APP_VERSION,
            "git_sha": git_sha,
            "git_sha_source": git_sha_source,
            "schema_version": get_schema_state()["version"],
            "bootstrap_version": get_bootstrap_state()["version"],
        }

    def _recent_changes() -> dict:
        return {
            "items": Item.query.order_by(Item.id.desc()).limit(5).all(),
            "people": Person.query.order_by(Person.id.desc()).limit(5).all(),
            "entries": (Ownership.query.order_by(Ownership.id.desc()).limit(5).all()),
        }

    @app.route("/")
    def index():
        from models import ItemVariant, Ownership, Person, Set

        private_view = is_authenticated_user()

        stats = dict(
            item_count=Item.query.count(),
            unicorns=Item.query.filter(
                db.or_(
                    Item.is_unicorn,
                    Item.edge_is_unicorn,
                    Item.variants.any(ItemVariant.is_unicorn == True),  # noqa: E712
                )
            ).count(),
            people=Person.query.count(),
            owned=Ownership.query.filter_by(status="Owned").count(),
            wishlist=Ownership.query.filter_by(status="Wishlist").count(),
            variants=ItemVariant.query.filter(
                ItemVariant.color != UNKNOWN_COLOR
            ).count(),
            sets=Set.query.count(),
        )
        people = Person.query.order_by(Person.name).all() if private_view else []
        recent = (
            Ownership.query.order_by(Ownership.id.desc()).limit(10).all()
            if private_view
            else []
        )
        top_colors = [
            {"color": color, "count": count}
            for color, count in (
                db.session.query(ItemVariant.color, func.count(ItemVariant.id))
                .filter(ItemVariant.color != UNKNOWN_COLOR)
                .group_by(ItemVariant.color)
                .order_by(func.count(ItemVariant.id).desc(), ItemVariant.color.asc())
                .limit(8)
                .all()
            )
        ]
        recent_activity = _recent_activity() if private_view else []
        recent_changes = (
            _recent_changes()
            if private_view
            else {"items": [], "people": [], "entries": []}
        )
        return render_template(
            "index.html",
            stats=stats,
            people=people,
            recent=recent,
            recent_activity=recent_activity,
            recent_changes=recent_changes,
            release_snapshot=_release_snapshot(),
            top_colors=top_colors,
        )

    @app.route("/search")
    def search():
        query = request.args.get("q", "").strip()
        cat_filter = request.args.get("category", "").strip()
        private_view = is_authenticated_user()
        categories = [
            row[0]
            for row in db.session.query(Item.category)
            .filter(Item.category.isnot(None), Item.category != "")
            .distinct()
            .order_by(Item.category)
            .all()
        ]
        if not query:
            shortcuts = [
                {
                    "label": "Catalog",
                    "url": url_for("catalog.catalog"),
                    "description": "Browse and filter items.",
                },
                {
                    "label": "Sets",
                    "url": url_for("catalog.sets_list"),
                    "description": "Browse product sets.",
                },
                {
                    "label": "Stats",
                    "url": url_for("views.stats"),
                    "description": "View catalog summaries.",
                },
            ]
            if private_view:
                shortcuts.extend(
                    [
                        {
                            "label": "People",
                            "url": url_for("people.people"),
                            "description": "Open collector records.",
                        },
                        {
                            "label": "Tasks",
                            "url": url_for("logs.tasks_manage"),
                            "description": "Manage knife tasks.",
                        },
                        {
                            "label": "Diagnostics",
                            "url": url_for("admin.diagnostics_page"),
                            "description": "Check runtime health.",
                        },
                    ]
                )
            return render_template(
                "search.html",
                query="",
                results={},
                shortcuts=shortcuts,
                categories=categories,
                cat_filter=cat_filter,
                UNCATEGORIZED_FILTER="__uncategorized__",
            )

        like = f"%{query}%"
        item_query = Item.query.filter(
            db.or_(
                Item.name.ilike(like),
                Item.sku.ilike(like),
                Item.category.ilike(like),
                Item.notes.ilike(like),
            )
        )
        if cat_filter == "__uncategorized__":
            item_query = item_query.filter(
                db.or_(Item.category.is_(None), Item.category == "")
            )
        elif cat_filter:
            item_query = item_query.filter(Item.category == cat_filter)
        item_results = item_query.order_by(Item.name).limit(20).all()

        set_results = (
            Set.query.filter(
                db.or_(
                    Set.name.ilike(like),
                    Set.sku.ilike(like),
                    Set.notes.ilike(like),
                )
            )
            .order_by(Set.name)
            .limit(20)
            .all()
        )

        person_results = []
        task_results = []
        if private_view:
            person_results = (
                Person.query.filter(
                    db.or_(
                        Person.name.ilike(like),
                        Person.notes.ilike(like),
                    )
                )
                .order_by(Person.name)
                .limit(20)
                .all()
            )

            task_results = (
                KnifeTask.query.filter(KnifeTask.name.ilike(like))
                .order_by(KnifeTask.name)
                .limit(20)
                .all()
            )

        return render_template(
            "search.html",
            query=query,
            results={
                "items": item_results,
                "people": person_results,
                "sets": set_results,
                "tasks": task_results,
            },
            shortcuts=[],
            categories=categories,
            cat_filter=cat_filter,
            UNCATEGORIZED_FILTER="__uncategorized__",
        )

    @app.route("/health")
    def health():
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify(status="ok", version=APP_VERSION, git_sha=GIT_SHA), 200
        except Exception as exc:
            logger.error("Health check failed: %s", exc)
            return jsonify(status="error"), 500

    @app.route("/robots.txt")
    def robots_txt():
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.route("/version")
    def version():
        return jsonify(version=APP_VERSION, git_sha=GIT_SHA)


def create_app(test_config: dict | None = None) -> Flask:
    """Build and configure the Flask application."""
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "cutco-vault-dev-key"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:////data/cutco.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE"),
        SESSION_REFRESH_EACH_REQUEST=False,
        LOG_DIR=os.environ.get("LOG_DIR", "/data/logs"),
        ATTACHMENTS_DIR=os.environ.get(
            "ATTACHMENTS_DIR", os.path.join(DATA_DIR, "uploads", "items")
        ),
        LOG_LEVEL=os.environ.get("LOG_LEVEL", "INFO").upper(),
        TESTING=False,
    )
    app.url_map.strict_slashes = False
    if test_config:
        app.config.update(test_config)

    app.secret_key = app.config["SECRET_KEY"]
    app.permanent_session_lifetime = timedelta(seconds=ADMIN_SESSION_SECONDS)

    _is_prod = os.environ.get("FLASK_ENV", "production").lower() == "production"
    _allow_insecure = _env_flag("ALLOW_INSECURE_DEFAULTS")
    if _is_prod and not _allow_insecure and not app.testing:
        if app.secret_key == "cutco-vault-dev-key" or ADMIN_TOKEN == "admin":
            raise RuntimeError(
                "Refusing to start in production with default SECRET_KEY or ADMIN_TOKEN. "
                "Set strong values, or set ALLOW_INSECURE_DEFAULTS=1 to bypass."
            )

    _setup_logging(app.config["LOG_DIR"], app.config["LOG_LEVEL"])
    db.init_app(app)
    limiter.init_app(app)

    _register_blueprints(app)
    _register_cli(app)
    _register_hooks(app)
    _register_error_handlers(app)
    _register_routes(app)

    with app.app_context():
        initialize_database()

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8080, debug=False)

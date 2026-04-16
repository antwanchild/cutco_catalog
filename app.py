import logging
import os
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, jsonify, render_template, request

from constants import (
    ADMIN_SESSION_SECONDS,
    ADMIN_TOKEN,
    APP_VERSION,
    GIT_SHA,
    KNIFE_TASK_PRESETS,
    UNKNOWN_COLOR,
    canonicalize_category,
)
from extensions import db, limiter
from helpers import _csrf_token, is_admin, validate_csrf
from models import Item, KnifeTask, ensure_unknown_variant

logger = logging.getLogger(__name__)
_LOG_FORMAT = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)
_LOGGING_READY = False
_FILE_LOG_PATHS: set[str] = set()
CSRF_EXEMPT = {"/admin/login", "/admin/logout"}


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


def _register_blueprints(app: Flask) -> None:
    from blueprints.admin import admin_bp
    from blueprints.catalog import catalog_bp
    from blueprints.data import data_bp
    from blueprints.logs import logs_bp
    from blueprints.people import people_bp
    from blueprints.views import views_bp

    app.register_blueprint(catalog_bp)
    app.register_blueprint(people_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(admin_bp)


def _initialize_database() -> None:
    from sqlalchemy import inspect as sa_inspect

    db.Model.metadata.create_all(db.engine, checkfirst=True)

    inspector = sa_inspect(db.engine)
    column_migrations = [
        ("sets", "sku", "ALTER TABLE sets ADD COLUMN sku VARCHAR(20)"),
        (
            "item_variants",
            "is_unicorn",
            "ALTER TABLE item_variants ADD COLUMN is_unicorn BOOLEAN NOT NULL DEFAULT 0",
        ),
        ("items", "msrp", "ALTER TABLE items ADD COLUMN msrp REAL"),
        ("ownership", "target_price", "ALTER TABLE ownership ADD COLUMN target_price REAL"),
        ("item_sets", "quantity", "ALTER TABLE item_sets ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"),
        ("items", "blade_length", "ALTER TABLE items ADD COLUMN blade_length VARCHAR(20)"),
        ("items", "overall_length", "ALTER TABLE items ADD COLUMN overall_length VARCHAR(20)"),
        ("items", "weight", "ALTER TABLE items ADD COLUMN weight VARCHAR(20)"),
        (
            "items",
            "edge_is_unicorn",
            "ALTER TABLE items ADD COLUMN edge_is_unicorn BOOLEAN NOT NULL DEFAULT 0",
        ),
    ]
    with db.engine.connect() as conn:
        for table_name, column_name, statement in column_migrations:
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            if column_name not in existing:
                conn.execute(db.text(statement))
                conn.commit()
                logger.info("Schema migration: added %s.%s", table_name, column_name)

    existing_task_names = {task.name for task in KnifeTask.query.all()}
    for preset in KNIFE_TASK_PRESETS:
        if preset not in existing_task_names:
            db.session.add(KnifeTask(name=preset, is_preset=True))
    db.session.flush()

    invalid_items = Item.query.filter(Item.sku.op("GLOB")("[0-9]")).all()
    for item in invalid_items:
        logger.info("Removing item with invalid single-digit SKU: %s (sku=%s)", item.name, item.sku)
        db.session.delete(item)

    renamed_categories = 0
    for item in Item.query.all():
        canonical_category = canonicalize_category(item.category)
        if canonical_category != item.category:
            item.category = canonical_category
            renamed_categories += 1
    if renamed_categories:
        logger.info("Category normalization: updated %d item category value(s)", renamed_categories)

    for item in Item.query.all():
        ensure_unknown_variant(item)

    db.session.commit()
    logger.info("Database ready")


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def csrf_protect():
        if request.method == "POST" and not request.path.startswith("/api/") and request.path not in CSRF_EXEMPT:
            validate_csrf()

    @app.after_request
    def set_security_headers(response):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.context_processor
    def inject_globals():
        return dict(
            app_version=APP_VERSION,
            is_admin=is_admin,
            UNKNOWN_COLOR=UNKNOWN_COLOR,
            csrf_token=_csrf_token,
        )


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def err_403(error):
        return render_template("error.html", code=403, icon="🚫", message="Access denied."), 403

    @app.errorhandler(404)
    def err_404(error):
        return render_template("error.html", code=404, icon="🔍", message="Page not found."), 404

    @app.errorhandler(429)
    def err_429(error):
        return render_template(
            "error.html",
            code=429,
            icon="⏱️",
            message="Too many requests - slow down and try again shortly.",
        ), 429

    @app.errorhandler(413)
    def err_413(error):
        return render_template(
            "error.html",
            code=413,
            icon="📦",
            message="File too large - maximum upload size is 10 MB.",
        ), 413

    @app.errorhandler(500)
    def err_500(error):
        db.session.rollback()
        logger.error("Unhandled 500: %s", error)
        return render_template(
            "error.html",
            code=500,
            icon="💥",
            message="Something went wrong on our end. Try again or check the logs.",
        ), 500


def _register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        from models import ItemVariant, Ownership, Person, Set

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
            variants=ItemVariant.query.filter(ItemVariant.color != UNKNOWN_COLOR).count(),
            sets=Set.query.count(),
        )
        people = Person.query.order_by(Person.name).all()
        recent = Ownership.query.order_by(Ownership.id.desc()).limit(10).all()
        return render_template("index.html", stats=stats, people=people, recent=recent)

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
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "cutco-vault-dev-key"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:////data/cutco.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        },
        LOG_DIR=os.environ.get("LOG_DIR", "/data/logs"),
        LOG_LEVEL=os.environ.get("LOG_LEVEL", "INFO").upper(),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    app.secret_key = app.config["SECRET_KEY"]
    app.permanent_session_lifetime = timedelta(seconds=ADMIN_SESSION_SECONDS)

    _is_prod = os.environ.get("FLASK_ENV", "production").lower() == "production"
    _allow_insecure = os.environ.get("ALLOW_INSECURE_DEFAULTS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
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
    _register_hooks(app)
    _register_error_handlers(app)
    _register_routes(app)

    with app.app_context():
        _initialize_database()

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8080, debug=False)

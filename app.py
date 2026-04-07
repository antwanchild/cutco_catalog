import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import timedelta

from flask import Flask, jsonify, render_template, request

from constants import ADMIN_TOKEN, ADMIN_SESSION_SECONDS, APP_VERSION, KNIFE_TASK_PRESETS, UNKNOWN_COLOR
from extensions import db, limiter
from helpers import _csrf_token, is_admin, validate_csrf
from models import Item, KnifeTask, ensure_unknown_variant

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR     = os.environ.get("LOG_DIR", "/data/logs")
_log_level  = getattr(logging, LOG_LEVEL, logging.INFO)
_log_format = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_format)

logging.basicConfig(level=_log_level, handlers=[_console_handler])
logger = logging.getLogger(__name__)

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "cutco.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
    )
    _file_handler.setFormatter(_log_format)
    logging.getLogger().addHandler(_file_handler)
    logger.info("File logging active: %s", LOG_DIR)
except OSError as exc:
    logger.warning("Could not set up file logging (%s) — console only", exc)

# ── App / DB ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cutco-vault-dev-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:////data/cutco.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {
    "1", "true", "yes", "on"
}
app.permanent_session_lifetime = timedelta(seconds=ADMIN_SESSION_SECONDS)

_is_prod = os.environ.get("FLASK_ENV", "production").lower() == "production"
_allow_insecure = os.environ.get("ALLOW_INSECURE_DEFAULTS", "").lower() in {
    "1", "true", "yes", "on"
}
if _is_prod and not _allow_insecure:
    if app.secret_key == "cutco-vault-dev-key" or ADMIN_TOKEN == "admin":
        raise RuntimeError(
            "Refusing to start in production with default SECRET_KEY or ADMIN_TOKEN. "
            "Set strong values, or set ALLOW_INSECURE_DEFAULTS=1 to bypass."
        )

db.init_app(app)
limiter.init_app(app)

# ── Blueprints ────────────────────────────────────────────────────────────────

from blueprints.catalog import catalog_bp  # noqa: E402
from blueprints.people  import people_bp   # noqa: E402
from blueprints.logs    import logs_bp     # noqa: E402
from blueprints.views   import views_bp    # noqa: E402
from blueprints.data    import data_bp     # noqa: E402
from blueprints.admin   import admin_bp    # noqa: E402

app.register_blueprint(catalog_bp)
app.register_blueprint(people_bp)
app.register_blueprint(logs_bp)
app.register_blueprint(views_bp)
app.register_blueprint(data_bp)
app.register_blueprint(admin_bp)

# ── DB init ───────────────────────────────────────────────────────────────────

with app.app_context():
    from sqlalchemy import inspect as _sa_inspect
    db.Model.metadata.create_all(db.engine, checkfirst=True)

    # Backfill columns added after initial schema deployment
    _inspector = _sa_inspect(db.engine)
    _col_migrations = [
        ("sets",          "sku",          "ALTER TABLE sets ADD COLUMN sku VARCHAR(20)"),
        ("item_variants", "is_unicorn",   "ALTER TABLE item_variants ADD COLUMN is_unicorn BOOLEAN NOT NULL DEFAULT 0"),
        ("items",         "msrp",         "ALTER TABLE items ADD COLUMN msrp REAL"),
        ("ownership",     "target_price", "ALTER TABLE ownership ADD COLUMN target_price REAL"),
        ("item_sets",     "quantity",       "ALTER TABLE item_sets ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"),
        ("items",         "blade_length",   "ALTER TABLE items ADD COLUMN blade_length VARCHAR(20)"),
        ("items",         "overall_length", "ALTER TABLE items ADD COLUMN overall_length VARCHAR(20)"),
        ("items",         "weight",         "ALTER TABLE items ADD COLUMN weight VARCHAR(20)"),
    ]
    with db.engine.connect() as _conn:
        for _table, _col, _stmt in _col_migrations:
            _existing = {c["name"] for c in _inspector.get_columns(_table)}
            if _col not in _existing:
                _conn.execute(db.text(_stmt))
                _conn.commit()
                logger.info("Schema migration: added %s.%s", _table, _col)
    existing_task_names = {t.name for t in KnifeTask.query.all()}
    for preset in KNIFE_TASK_PRESETS:
        if preset not in existing_task_names:
            db.session.add(KnifeTask(name=preset, is_preset=True))
    db.session.flush()

    _bad = Item.query.filter(Item.sku.op("GLOB")("[0-9]")).all()
    for _item in _bad:
        logger.info("Removing item with invalid single-digit SKU: %s (sku=%s)", _item.name, _item.sku)
        db.session.delete(_item)
    for item in Item.query.all():
        ensure_unknown_variant(item)
    db.session.commit()
    logger.info("Database ready")

# ── Template context ──────────────────────────────────────────────────────────

# ── CSRF validation ───────────────────────────────────────────────────────────

CSRF_EXEMPT = {"/admin/login", "/admin/logout"}

@app.before_request
def csrf_protect():
    if request.method == "POST" and not request.path.startswith("/api/") \
            and request.path not in CSRF_EXEMPT:
        validate_csrf()


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"]           = "noindex, nofollow"
    return response


@app.route("/robots.txt")
def robots_txt():
    from flask import Response
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.context_processor
def inject_globals():
    return dict(app_version=APP_VERSION, is_admin=is_admin,
                UNKNOWN_COLOR=UNKNOWN_COLOR, csrf_token=_csrf_token)


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(403)
def err_403(e):
    return render_template("error.html", code=403,
                           icon="🚫", message="Access denied."), 403

@app.errorhandler(404)
def err_404(e):
    return render_template("error.html", code=404,
                           icon="🔍", message="Page not found."), 404

@app.errorhandler(429)
def err_429(e):
    return render_template("error.html", code=429,
                           icon="⏱️", message="Too many requests — slow down and try again shortly."), 429

@app.errorhandler(413)
def err_413(e):
    return render_template("error.html", code=413,
                           icon="📦", message="File too large — maximum upload size is 10 MB."), 413

@app.errorhandler(500)
def err_500(e):
    db.session.rollback()
    logger.error("Unhandled 500: %s", e)
    return render_template("error.html", code=500,
                           icon="💥", message="Something went wrong on our end. Try again or check the logs."), 500


# ── Core routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    from models import Item, ItemVariant, Ownership, Person, Set
    stats = dict(
        item_count = Item.query.count(),
        unicorns = Item.query.filter(db.or_(
                       Item.is_unicorn,
                       Item.variants.any(ItemVariant.is_unicorn == True)  # noqa: E712
                   )).count(),
        people   = Person.query.count(),
        owned    = Ownership.query.filter_by(status="Owned").count(),
        wishlist = Ownership.query.filter_by(status="Wishlist").count(),
        variants = ItemVariant.query.filter(ItemVariant.color != UNKNOWN_COLOR).count(),
        sets     = Set.query.count(),
    )
    people = Person.query.order_by(Person.name).all()
    recent = Ownership.query.order_by(Ownership.id.desc()).limit(10).all()
    return render_template("index.html", stats=stats, people=people, recent=recent)


@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify(status="ok", version=APP_VERSION), 200
    except Exception as exc:
        logger.error("Health check failed: %s", exc)
        return jsonify(status="error"), 500


@app.route("/version")
def version():
    return jsonify(version=APP_VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

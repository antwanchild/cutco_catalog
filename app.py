import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, render_template

from constants import APP_VERSION, KNIFE_TASK_PRESETS, UNKNOWN_COLOR
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
    db.Model.metadata.create_all(db.engine, checkfirst=True)
    with db.engine.connect() as _conn:
        for _stmt in [
            "ALTER TABLE sets ADD COLUMN sku VARCHAR(20)",
            "ALTER TABLE item_variants ADD COLUMN is_unicorn BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE items ADD COLUMN msrp REAL",
            "ALTER TABLE ownership ADD COLUMN target_price REAL",
            "CREATE TABLE IF NOT EXISTS knife_tasks (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE, is_preset BOOLEAN NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS knife_task_log (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), task_id INTEGER NOT NULL REFERENCES knife_tasks(id), notes TEXT, logged_on VARCHAR(10) NOT NULL)",
        ]:
            try:
                _conn.execute(db.text(_stmt))
                _conn.commit()
            except Exception:
                pass
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

@app.before_request
def csrf_protect():
    if request.method == "POST" and not request.path.startswith("/api/"):
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
        return jsonify(status="error", detail=str(exc)), 500


@app.route("/version")
def version():
    return jsonify(version=APP_VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

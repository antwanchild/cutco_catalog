"""Cutco Catalog — Flask web application for tracking Cutco knife collections.

Provides catalog browsing, collector management, ownership tracking, spreadsheet
import/export, and an optional admin mode for catalog sync from cutco.com.
"""
import csv
import io
import logging
import os
import time

import openpyxl
import requests
from bs4 import BeautifulSoup
from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, Response, url_for)
from flask_sqlalchemy import SQLAlchemy

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── App / DB ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cutco-vault-dev-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:////data/cutco.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ── Constants ─────────────────────────────────────────────────────────────────

EDGE_TYPES = ["Straight", "Double-D", "Serrated", "Micro-D", "Tec Edge", "Unknown"]
# Lookup map for case-insensitive normalization of imported edge type values
EDGE_TYPE_LOOKUP = {et.lower(): et for et in EDGE_TYPES}
STATUS_OPTIONS = ["Owned", "Wishlist", "Sold", "Traded"]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin")
UNKNOWN_COLOR = "Unknown / Unspecified"
APP_VERSION = os.environ.get("APP_VERSION", "dev")

SCRAPE_CATEGORIES = [
    ("Kitchen Knives", "https://www.cutco.com/shop/kitchen-knives"),
    ("Utility Knives",  "https://www.cutco.com/shop/utility-knives"),
    ("Chef Knives",     "https://www.cutco.com/shop/chef-knives"),
    ("Paring Knives",   "https://www.cutco.com/shop/paring-knives"),
    ("Outdoor Knives",  "https://www.cutco.com/shop/outdoor-knives"),
    ("Everyday Knives", "https://www.cutco.com/shop/everyday-knives"),
]
SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CutcoVaultBot/1.0)"}

SYNC_BLOCKED_CATEGORIES = {c.strip() for c in os.environ.get(
    "SYNC_BLOCKED_CATEGORIES", "Knife Sets,Accessories,Tableware").split(",") if c.strip()}

# Set column names as they appear in the spreadsheet
SPREADSHEET_SET_COLUMNS = [
    "Beast", "Fanatic", "SIGNATURE", "HOMEMAKER",
    "Accomplished Chef", "CUTCO Kitchen", "BEAST2", "HOMEMAKER2",
    "Accomplished Chef3", "CUTCO Kitchen4",
]

# ── Models ────────────────────────────────────────────────────────────────────

# Many-to-many join table: items <-> sets
item_sets = db.Table(
    "item_sets",
    db.Column("item_id", db.Integer, db.ForeignKey("items.id"), primary_key=True),
    db.Column("set_id",  db.Integer, db.ForeignKey("sets.id"),  primary_key=True),
)


class Item(db.Model):
    __tablename__ = "items"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(160), nullable=False)
    sku        = db.Column(db.String(60),  nullable=True, unique=True)
    category   = db.Column(db.String(80),  nullable=True)
    edge_type  = db.Column(db.String(40),  nullable=False, default="Unknown")
    is_unicorn = db.Column(db.Boolean,     nullable=False, default=False)
    in_catalog = db.Column(db.Boolean,     nullable=False, default=True)
    cutco_url  = db.Column(db.String(300), nullable=True)
    notes      = db.Column(db.Text,        nullable=True)

    variants = db.relationship("ItemVariant", backref="item",
                               lazy=True, cascade="all, delete-orphan",
                               order_by="ItemVariant.color")
    sets     = db.relationship("Set", secondary=item_sets,
                               back_populates="items", lazy="select")


class ItemVariant(db.Model):
    __tablename__ = "item_variants"

    id      = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    color   = db.Column(db.String(80), nullable=False, default=UNKNOWN_COLOR)
    notes   = db.Column(db.Text, nullable=True)

    ownerships = db.relationship("Ownership", backref="variant",
                                 lazy=True, cascade="all, delete-orphan")


class Set(db.Model):
    __tablename__ = "sets"

    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(120), nullable=False, unique=True)
    notes = db.Column(db.Text, nullable=True)

    items = db.relationship("Item", secondary=item_sets,
                            back_populates="sets", lazy="select")


class Person(db.Model):
    __tablename__ = "people"

    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text, nullable=True)

    ownerships = db.relationship("Ownership", backref="person",
                                 lazy=True, cascade="all, delete-orphan")


class Ownership(db.Model):
    __tablename__ = "ownership"

    id         = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("item_variants.id"), nullable=False)
    person_id  = db.Column(db.Integer, db.ForeignKey("people.id"),        nullable=False)
    status     = db.Column(db.String(20), nullable=False, default="Owned")
    notes      = db.Column(db.Text, nullable=True)

    __table_args__ = (db.UniqueConstraint("variant_id", "person_id",
                                          name="uq_variant_person"),)


# ── DB init ───────────────────────────────────────────────────────────────────

def ensure_unknown_variant(item):
    """Guarantee every Item has at least one 'Unknown / Unspecified' variant.

    Called after creating a new item so that ownership can always be recorded
    even when the exact handle color is not known.
    """
    if not any(v.color == UNKNOWN_COLOR for v in item.variants):
        db.session.add(ItemVariant(item_id=item.id, color=UNKNOWN_COLOR))
        db.session.flush()


with app.app_context():
    db.create_all()
    for it in Item.query.all():
        ensure_unknown_variant(it)
    db.session.commit()
    logger.info("Database ready")

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_text(value: str) -> str:
    """Strip whitespace and apply title case for consistent storage.

    Applied to item names, variant colors, person names, categories, and set
    names so that entries from different sources (manual form, CSV, XLSX) are
    stored with uniform casing and can be compared reliably.

    Examples:
        "classic brown"  → "Classic Brown"
        "PEARL WHITE"    → "Pearl White"
        "super shears"   → "Super Shears"
    """
    return value.strip().title() if value and value.strip() else ""


def is_admin():
    """Return True if the current request carries a valid admin cookie."""
    return request.cookies.get("admin_token") == ADMIN_TOKEN


def scrape_catalog():
    """Scrape CUTCO's public shop pages and return a list of product dicts.

    Each dict contains keys: name, sku, category, url.
    SKUs are extracted from the product page URL path (e.g. /p/1720) and
    must contain at least one digit to filter out non-product slugs.
    Categories in SYNC_BLOCKED_CATEGORIES are excluded by the caller.
    """
    results = []
    seen_skus = set()
    for cat_name, cat_url in SCRAPE_CATEGORIES:
        try:
            resp = requests.get(cat_url, headers=SCRAPE_HEADERS, timeout=12)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a[href*='/p/']"):
                href = a.get("href", "")
                parts = href.rstrip("/").split("/")
                candidate = parts[-1].split("?")[0].split("&")[0]
                if not candidate or len(candidate) > 10:
                    continue
                if not any(c.isdigit() for c in candidate):
                    continue
                sku = candidate.upper()
                if sku in seen_skus:
                    continue
                url = href if href.startswith("http") else f"https://www.cutco.com{href}"
                name_el = a.find(["h2", "h3"])
                if not name_el and a.parent:
                    name_el = a.parent.find(["h2", "h3"])
                name = name_el.get_text(strip=True) if name_el else None
                if not name:
                    continue
                seen_skus.add(sku)
                results.append(dict(name=name, sku=sku, category=cat_name, url=url))
            time.sleep(0.4)
        except Exception as exc:
            logger.warning("Scrape failed for %s: %s", cat_url, exc)
    return results


def get_or_create_set(name: str) -> "Set":
    """Return existing Set by name (case-insensitive) or create a new one."""
    existing_set = Set.query.filter(db.func.lower(Set.name) == name.lower()).first()
    if not existing_set:
        existing_set = Set(name=name)
        db.session.add(existing_set)
        db.session.flush()
    return existing_set

# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return dict(app_version=APP_VERSION, is_admin=is_admin)

# ── Version ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/version")
def version():
    return jsonify(version=APP_VERSION)

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    stats = dict(
        items    = Item.query.count(),
        unicorns = Item.query.filter_by(is_unicorn=True).count(),
        people   = Person.query.count(),
        owned    = Ownership.query.filter_by(status="Owned").count(),
        wishlist = Ownership.query.filter_by(status="Wishlist").count(),
        variants = ItemVariant.query.filter(ItemVariant.color != UNKNOWN_COLOR).count(),
        sets     = Set.query.count(),
    )
    people = Person.query.order_by(Person.name).all()
    recent = Ownership.query.order_by(Ownership.id.desc()).limit(10).all()
    return render_template("index.html", stats=stats, people=people, recent=recent)

# ── Catalog ───────────────────────────────────────────────────────────────────

@app.route("/catalog")
def catalog():
    search_query   = request.args.get("q", "").strip()
    cat_filter     = request.args.get("category", "")
    unicorn_filter = request.args.get("unicorn", "")
    sort_field     = request.args.get("sort", "name")
    direction      = request.args.get("dir", "asc")

    query = Item.query
    if search_query:
        query = query.filter(
            db.or_(Item.name.ilike(f"%{search_query}%"), Item.sku.ilike(f"%{search_query}%")))
    if cat_filter:
        query = query.filter(Item.category == cat_filter)
    if unicorn_filter == "1":
        query = query.filter(Item.is_unicorn)

    sort_column = getattr(Item, sort_field, Item.name)
    items = query.order_by(sort_column.desc() if direction == "desc" else sort_column).all()

    categories = [r[0] for r in
                  db.session.query(Item.category)
                  .filter(Item.category.isnot(None))
                  .distinct().order_by(Item.category).all()]

    return render_template("catalog.html", items=items, categories=categories,
                           q=search_query, cat_filter=cat_filter, unicorn_f=unicorn_filter,
                           sort=sort_field, direction=direction,
                           edge_types=EDGE_TYPES, is_admin=is_admin(),
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/add", methods=["GET", "POST"])
def catalog_add():
    if request.method == "POST":
        item = Item(
            name       = normalize_text(request.form["name"]),
            sku        = request.form.get("sku", "").strip().upper() or None,
            category   = normalize_text(request.form.get("category", "")) or None,
            edge_type  = request.form.get("edge_type", "Unknown"),
            is_unicorn = request.form.get("is_unicorn") == "on",
            in_catalog = request.form.get("in_catalog") == "on",
            cutco_url  = request.form.get("cutco_url", "").strip() or None,
            notes      = request.form.get("notes", "").strip() or None,
        )
        db.session.add(item)
        db.session.flush()
        ensure_unknown_variant(item)
        colors = [normalize_text(c) for c in request.form.get("colors", "").split(",") if c.strip()]
        for color in colors:
            if color != UNKNOWN_COLOR:
                db.session.add(ItemVariant(item_id=item.id, color=color))
        db.session.commit()
        flash(f'Added "{item.name}" to catalog.', "success")
        return redirect(url_for("catalog"))

    return render_template("item_form.html", item=None,
                           edge_types=EDGE_TYPES, action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all())


@app.route("/catalog/<int:iid>/edit", methods=["GET", "POST"])
def catalog_edit(iid):
    item = Item.query.get_or_404(iid)
    if request.method == "POST":
        item.name       = normalize_text(request.form["name"])
        item.sku        = request.form.get("sku", "").strip().upper() or None
        item.category   = normalize_text(request.form.get("category", "")) or None
        item.edge_type  = request.form.get("edge_type", "Unknown")
        item.is_unicorn = request.form.get("is_unicorn") == "on"
        item.in_catalog = request.form.get("in_catalog") == "on"
        item.cutco_url  = request.form.get("cutco_url", "").strip() or None
        item.notes      = request.form.get("notes", "").strip() or None

        # Update set memberships
        selected_set_ids = set(int(x) for x in request.form.getlist("set_ids"))
        item.sets = Set.query.filter(Set.id.in_(selected_set_ids)).all()

        db.session.commit()
        flash(f'Updated "{item.name}".', "success")
        return redirect(url_for("catalog"))

    return render_template("item_form.html", item=item,
                           edge_types=EDGE_TYPES, action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR,
                           all_sets=Set.query.order_by(Set.name).all())


@app.route("/catalog/<int:iid>/delete", methods=["POST"])
def catalog_delete(iid):
    item = Item.query.get_or_404(iid)
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f'Deleted "{name}".', "info")
    return redirect(url_for("catalog"))

# ── Variants ──────────────────────────────────────────────────────────────────

@app.route("/catalog/<int:iid>/variants")
def variants(iid):
    item = Item.query.get_or_404(iid)
    return render_template("variants.html", item=item, UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/<int:iid>/variants/add", methods=["POST"])
def variant_add(iid):
    item = Item.query.get_or_404(iid)
    color = normalize_text(request.form.get("color", ""))
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("variants", iid=iid))
    if any(v.color.lower() == color.lower() for v in item.variants):
        flash(f'"{color}" already exists for this item.', "error")
        return redirect(url_for("variants", iid=iid))
    db.session.add(ItemVariant(item_id=iid, color=color,
                               notes=request.form.get("notes", "").strip() or None))
    db.session.commit()
    flash(f'Added variant "{color}".', "success")
    return redirect(url_for("variants", iid=iid))


@app.route("/variants/<int:vid>/edit", methods=["POST"])
def variant_edit(vid):
    variant = ItemVariant.query.get_or_404(vid)
    iid = variant.item_id
    color = normalize_text(request.form.get("color", ""))
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("variants", iid=iid))
    variant.color = color
    variant.notes = request.form.get("notes", "").strip() or None
    db.session.commit()
    flash(f'Updated to "{color}".', "success")
    return redirect(url_for("variants", iid=iid))


@app.route("/variants/<int:vid>/delete", methods=["POST"])
def variant_delete(vid):
    variant = ItemVariant.query.get_or_404(vid)
    if len(variant.item.variants) == 1:
        flash("Cannot delete the only variant. Add another first.", "error")
        return redirect(url_for("variants", iid=variant.item_id))
    iid = variant.item_id
    db.session.delete(variant)
    db.session.commit()
    flash("Variant removed.", "info")
    return redirect(url_for("variants", iid=iid))

# ── Sets ──────────────────────────────────────────────────────────────────────

@app.route("/sets")
def sets_list():
    all_sets = Set.query.order_by(Set.name).all()
    return render_template("sets.html", sets=all_sets)


@app.route("/sets/add", methods=["GET", "POST"])
def set_add():
    if request.method == "POST":
        name = normalize_text(request.form["name"])
        if Set.query.filter(db.func.lower(Set.name) == name.lower()).first():
            flash(f'Set "{name}" already exists.', "error")
            return redirect(url_for("set_add"))
        new_set = Set(name=name, notes=request.form.get("notes", "").strip() or None)
        db.session.add(new_set)
        db.session.commit()
        flash(f'Created set "{name}".', "success")
        return redirect(url_for("sets_list"))
    return render_template("set_form.html", set=None, action="Add")


@app.route("/sets/<int:sid>/edit", methods=["GET", "POST"])
def set_edit(sid):
    item_set = Set.query.get_or_404(sid)
    if request.method == "POST":
        item_set.name  = normalize_text(request.form["name"])
        item_set.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash(f'Updated set "{item_set.name}".', "success")
        return redirect(url_for("sets_list"))
    return render_template("set_form.html", set=item_set, action="Edit")


@app.route("/sets/<int:sid>/delete", methods=["POST"])
def set_delete(sid):
    item_set = Set.query.get_or_404(sid)
    name = item_set.name
    db.session.delete(item_set)
    db.session.commit()
    flash(f'Deleted set "{name}".', "info")
    return redirect(url_for("sets_list"))


@app.route("/sets/<int:sid>")
def set_detail(sid):
    item_set = Set.query.get_or_404(sid)
    return render_template("set_detail.html", set=item_set, UNKNOWN_COLOR=UNKNOWN_COLOR)

# ── Catalog Sync ──────────────────────────────────────────────────────────────

@app.route("/catalog/sync")
def catalog_sync():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog"))

    scraped = scrape_catalog()
    existing_skus = {k.sku for k in Item.query.filter(Item.sku.isnot(None)).all()}
    allowed   = [i for i in scraped if i["category"] not in SYNC_BLOCKED_CATEGORIES]
    new_items = [i for i in allowed if i["sku"] not in existing_skus]

    from collections import OrderedDict
    grouped = OrderedDict()
    for item in new_items:
        grouped.setdefault(item["category"], []).append(item)

    logger.info("Sync: %d scraped, %d blocked, %d new",
                len(scraped), len(scraped) - len(allowed), len(new_items))

    return render_template("sync_preview.html",
                           grouped=grouped,
                           new_items=new_items,
                           scraped_total=len(scraped),
                           blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES))


@app.route("/catalog/sync/confirm", methods=["POST"])
def catalog_sync_confirm():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog"))

    selected = set(request.form.getlist("selected_skus"))
    items = {}
    for key, val in request.form.items():
        for prefix in ("name_", "category_", "url_"):
            if key.startswith(prefix):
                sku = key[len(prefix):]
                items.setdefault(sku, {})[prefix.rstrip("_")] = val

    added = 0
    for sku in selected:
        if Item.query.filter_by(sku=sku).first():
            continue
        data = items.get(sku, {})
        item = Item(name=data.get("name", sku), sku=sku,
                    category=data.get("category"), cutco_url=data.get("url"),
                    in_catalog=True, is_unicorn=False, edge_type="Unknown")
        db.session.add(item)
        db.session.flush()
        ensure_unknown_variant(item)
        added += 1

    db.session.commit()
    flash(f"Sync complete — added {added} new item{'s' if added != 1 else ''}.", "success")
    return redirect(url_for("catalog"))

# ── People ────────────────────────────────────────────────────────────────────

@app.route("/people")
def people():
    persons = Person.query.order_by(Person.name).all()
    counts  = {p.id: Ownership.query.filter_by(person_id=p.id, status="Owned").count()
               for p in persons}
    return render_template("people.html", persons=persons, counts=counts)


@app.route("/people/add", methods=["GET", "POST"])
def people_add():
    if request.method == "POST":
        person = Person(name=normalize_text(request.form["name"]),
                        notes=request.form.get("notes", "").strip() or None)
        db.session.add(person)
        db.session.commit()
        flash(f"Added {person.name}.", "success")
        return redirect(url_for("people"))
    return render_template("person_form.html", person=None, action="Add")


@app.route("/people/<int:pid>/edit", methods=["GET", "POST"])
def people_edit(pid):
    person = Person.query.get_or_404(pid)
    if request.method == "POST":
        person.name  = normalize_text(request.form["name"])
        person.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash(f"Updated {person.name}.", "success")
        return redirect(url_for("people"))
    return render_template("person_form.html", person=person, action="Edit")


@app.route("/people/<int:pid>/delete", methods=["POST"])
def people_delete(pid):
    person = Person.query.get_or_404(pid)
    name   = person.name
    db.session.delete(person)
    db.session.commit()
    flash(f"Removed {name}.", "info")
    return redirect(url_for("people"))


@app.route("/people/<int:pid>/collection")
def person_collection(pid):
    person     = Person.query.get_or_404(pid)
    ownerships = (Ownership.query.filter_by(person_id=pid)
                  .order_by(Ownership.status).all())

    owned_item_ids = {ownership.variant.item_id for ownership in ownerships if ownership.status == "Owned"}
    all_items      = Item.query.order_by(Item.name).all()
    item_gaps      = [item for item in all_items if item.id not in owned_item_ids]

    # Find items where the person owns at least one variant but is missing others
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

@app.route("/ownership/add", methods=["GET", "POST"])
def ownership_add():
    person_id  = request.args.get("person_id", type=int)
    item_id    = request.args.get("item_id", type=int)
    variant_id = request.args.get("variant_id", type=int)

    if request.method == "POST":
        pid = int(request.form["person_id"])
        vid = int(request.form["variant_id"])
        if Ownership.query.filter_by(person_id=pid, variant_id=vid).first():
            flash("That person already has an entry for that variant.", "error")
            return redirect(url_for("person_collection", pid=pid))
        db.session.add(Ownership(
            person_id  = pid,
            variant_id = vid,
            status     = request.form.get("status", "Owned"),
            notes      = request.form.get("notes", "").strip() or None,
        ))
        db.session.commit()
        flash("Entry logged.", "success")
        return redirect(url_for("person_collection", pid=pid))

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


@app.route("/ownership/<int:oid>/edit", methods=["GET", "POST"])
def ownership_edit(oid):
    ownership = Ownership.query.get_or_404(oid)
    if request.method == "POST":
        ownership.status = request.form.get("status", "Owned")
        ownership.notes  = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash("Updated.", "success")
        return redirect(url_for("person_collection", pid=ownership.person_id))

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


@app.route("/ownership/<int:oid>/delete", methods=["POST"])
def ownership_delete(oid):
    ownership = Ownership.query.get_or_404(oid)
    pid = ownership.person_id
    db.session.delete(ownership)
    db.session.commit()
    flash("Entry removed.", "info")
    return redirect(url_for("person_collection", pid=pid))

# ── Views ─────────────────────────────────────────────────────────────────────

@app.route("/views/item/<int:iid>")
def item_owners(iid):
    item = Item.query.get_or_404(iid)
    entries = (Ownership.query
               .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
               .filter(ItemVariant.item_id == iid)
               .order_by(Ownership.status).all())
    owner_ids      = {e.person_id for e in entries}
    people_without = (Person.query
                      .filter(~Person.id.in_(owner_ids))
                      .order_by(Person.name).all())
    return render_template("item_owners.html", item=item,
                           entries=entries, people_without=people_without,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/views/matrix")
def matrix():
    people_list = Person.query.order_by(Person.name).all()
    items_list  = Item.query.order_by(Item.name).all()
    STATUS_RANK = {"Owned": 0, "Wishlist": 1, "Traded": 2, "Sold": 3}

    # Build item_lookup: (person_id, item_id) → best Ownership record by STATUS_RANK
    item_lookup = {}
    for ownership_record in Ownership.query.all():
        key     = (ownership_record.person_id, ownership_record.variant.item_id)
        current = item_lookup.get(key)
        if current is None or STATUS_RANK.get(ownership_record.status, 9) < STATUS_RANK.get(current.status, 9):
            item_lookup[key] = ownership_record

    # Build variant_lookup: (person_id, variant_id) → Ownership record
    variant_lookup = {(rec.person_id, rec.variant_id): rec for rec in Ownership.query.all()}

    # For each item, prefer named variants; fall back to all variants if none are named
    variants_by_item = {
        item.id: [variant for variant in item.variants if variant.color != UNKNOWN_COLOR] or item.variants
        for item in items_list
    }

    return render_template("matrix.html",
                           people=people_list,
                           items=items_list,
                           item_lookup=item_lookup,
                           variant_lookup=variant_lookup,
                           variants_by_item=variants_by_item,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)

# ── Export ────────────────────────────────────────────────────────────────────

@app.route("/export/csv")
def export_csv():
    rows = (db.session.query(Ownership, ItemVariant, Item, Person)
            .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
            .join(Item,        ItemVariant.item_id   == Item.id)
            .join(Person,      Ownership.person_id   == Person.id)
            .order_by(Person.name, Item.name, ItemVariant.color).all())

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["person", "item_name", "sku", "category", "edge_type",
                     "color", "status", "is_unicorn", "notes"])
    for ownership, variant, item, person in rows:
        writer.writerow([person.name, item.name, item.sku or "", item.category or "",
                         item.edge_type, variant.color, ownership.status,
                         "yes" if item.is_unicorn else "no", ownership.notes or ""])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_collection.csv"})

# ── Import ────────────────────────────────────────────────────────────────────

TRUTHY = {"yes", "y", "true", "1", "x"}

# Column mapping from spreadsheet header → internal key
XLSX_COL_MAP = {
    # Core item fields
    "name":                  "name",
    "model #":               "sku",
    "model#":                "sku",
    "color":                 "color",
    "category":              "category",
    "edge":                  "edge_type",
    "unicorn?":              "is_unicorn",
    # Ownership
    "owned?":                "owned_raw",
    # Notes fields (merged into notes column)
    "price":                 "notes_price",
    "gift box":              "notes_gift_box",
    "sheath":                "notes_sheath",
    "quantity purchased":    "notes_qty",
    "given away":            "notes_given_away",
}
# Set membership columns — key = lowercase spreadsheet header, value = canonical set name
XLSX_SET_COLS = {s.lower(): s for s in SPREADSHEET_SET_COLUMNS}


def parse_owned_raw(owned_raw: str, default_person: str | None):
    """
    Parse 'Owned?' cell.
    - Truthy → status=Owned, person=default_person
    - Falsy  → status=Wishlist, person=default_person
    - Text that isn't yes/no → treat as person name, status=Owned
    Returns (status, person_name)
    """
    val = owned_raw.strip()
    if val.lower() in TRUTHY:
        return "Owned", default_person
    if val.lower() in {"no", "n", "false", "0", ""}:
        return "Wishlist", default_person
    # Non-boolean string: treated as assigned person
    return "Owned", val or default_person


def build_notes(row: dict) -> str | None:
    """Combine spreadsheet metadata columns into a single notes string.

    Fields like price, gift box, and sheath info are concatenated as
    'Label: value' pairs separated by '; '. Returns None if all fields
    are empty or contain placeholder values (0, none, n/a, -).
    """
    parts = []
    for key, label in [
        ("notes_price",     "Price"),
        ("notes_gift_box",  "Gift Box"),
        ("notes_sheath",    "Sheath"),
        ("notes_qty",       "Qty Purchased"),
        ("notes_given_away","Given Away"),
    ]:
        field_value = row.get(key, "").strip()
        if field_value and field_value not in ("0", "none", "n/a", "-"):
            parts.append(f"{label}: {field_value}")
    return "; ".join(parts) or None


@app.route("/import/template")
def import_template():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["name", "sku", "color", "edge_type", "is_unicorn",
                     "person", "status", "category", "notes"])
    writer.writerow(["2-3/4\" Paring Knife", "1720", "Classic Brown", "Double-D",
                     "no", "Anthony", "Owned", "Kitchen Knives", ""])
    writer.writerow(["Super Shears", "2137", "Pearl White", "Straight",
                     "no", "Anthony", "Owned", "Kitchen Knives", ""])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_import_template.csv"})


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    uploaded_file = request.files.get("csvfile")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose a file.", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    person_override = request.form.get("person_override", "").strip() or None
    file_extension = uploaded_file.filename.rsplit(".", 1)[-1].lower()

    try:
        if file_extension == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(uploaded_file.stream.read()), data_only=True)
            ws = wb.active
            raw_headers = [str(cell.value).strip() if cell.value is not None else ""
                           for cell in ws[1]]
            norm_rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                if all(v is None for v in row):
                    continue
                norm_rows.append({raw_headers[i]: str(v).strip() if v is not None else ""
                                  for i, v in enumerate(row)})
            # Keep original casing for set detection; build internal-key row
            parsed_rows = []
            for row in norm_rows:
                out_row = {}
                set_memberships = []
                for orig_key, val in row.items():
                    lk = orig_key.strip().lower()
                    if lk in XLSX_COL_MAP:
                        out_row[XLSX_COL_MAP[lk]] = val
                    elif lk in XLSX_SET_COLS:
                        if val.strip().lower() in TRUTHY:
                            set_memberships.append(XLSX_SET_COLS[lk])
                    else:
                        # Fallback: snake_case normalisation for CSV-style columns
                        out_row[lk.replace(" ", "_")] = val
                out_row["set_memberships"] = set_memberships
                parsed_rows.append(out_row)
        else:
            stream = io.StringIO(uploaded_file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            parsed_rows = []
            for row in reader:
                out_row = {k.strip().lower().replace(" ", "_"): v.strip()
                           for k, v in row.items()}
                out_row["set_memberships"] = []
                parsed_rows.append(out_row)

    except Exception as exc:
        flash(f"Could not parse file: {exc}", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    # Apply person override
    if person_override:
        for row in parsed_rows:
            row["owned_raw"] = row.get("owned_raw", "yes")
            row["person_override"] = person_override

    existing_items   = {k.sku.upper(): k for k in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {k.name.lower(): k for k in Item.query.all()}
    existing_persons = {p.name.lower(): p for p in Person.query.all()}

    already_in_catalog = []
    new_items_list     = []
    likely_unicorns    = []
    ownership_entries  = []
    conflicts          = []
    errors             = []
    seen_skus          = set()

    for i, row in enumerate(parsed_rows, start=2):
        name       = normalize_text(row.get("name", ""))
        sku        = (row.get("sku", "") or "").strip().upper() or None
        color      = normalize_text(row.get("color", "")) or UNKNOWN_COLOR
        edge_type  = EDGE_TYPE_LOOKUP.get(row.get("edge_type", "").strip().lower(), "Unknown")
        is_unicorn = row.get("is_unicorn", "").strip().lower() in TRUTHY
        category   = normalize_text(row.get("category", "")) or None
        notes      = build_notes(row) or row.get("notes", "").strip() or None
        set_names  = row.get("set_memberships", [])

        # Resolve person + status from 'Owned?' column
        owned_raw = row.get("owned_raw", row.get("status", "yes"))
        status, person_name = parse_owned_raw(owned_raw, row.get("person_override") or row.get("person", ""))

        if person_override:
            person_name = person_override
        if person_name:
            person_name = normalize_text(person_name)

        if not name:
            errors.append({"row": i, "reason": "Missing name", "data": row})
            continue

        if status not in STATUS_OPTIONS:
            status = "Owned"

        matched_item = None
        if sku and sku in existing_items:
            matched_item = existing_items[sku]
        elif name.lower() in existing_names:
            matched_item = existing_names[name.lower()]

        dedup_key = (sku or name.lower())

        if matched_item:
            already_in_catalog.append({"item": matched_item, "row": row,
                                       "color": color, "person": person_name,
                                       "status": status, "sets": set_names})
        elif dedup_key not in seen_skus:
            seen_skus.add(dedup_key)
            bucket = likely_unicorns if is_unicorn or not sku else new_items_list
            bucket.append({
                "name": name, "sku": sku, "color": color,
                "edge_type": edge_type, "is_unicorn": is_unicorn,
                "category": category, "notes": notes,
                "person": person_name, "status": status,
                "sets": set_names, "row": i,
            })

        if person_name and matched_item:
            person_obj = existing_persons.get(person_name.lower())
            if person_obj:
                variant = next((v for v in matched_item.variants
                                if v.color.lower() == color.lower()), None)
                if variant:
                    existing_o = Ownership.query.filter_by(
                        person_id=person_obj.id, variant_id=variant.id).first()
                    if existing_o:
                        if existing_o.status != status:
                            conflicts.append({
                                "person": person_name,
                                "item": matched_item.name,
                                "color": color,
                                "existing_status": existing_o.status,
                                "import_status": status,
                                "oid": existing_o.id,
                            })
                        continue
            ownership_entries.append({
                "person": person_name,
                "item_name": matched_item.name,
                "item_id":   matched_item.id,
                "color":     color,
                "status":    status,
                "notes":     notes,
                "is_new_person": person_name.lower() not in existing_persons,
            })

    return render_template("import_preview.html",
                           already_in_catalog=already_in_catalog,
                           new_items=new_items_list,
                           likely_unicorns=likely_unicorns,
                           ownership_entries=ownership_entries,
                           conflicts=conflicts,
                           errors=errors,
                           edge_types=EDGE_TYPES,
                           status_options=STATUS_OPTIONS,
                           person_override=person_override)


@app.route("/import/confirm", methods=["POST"])
def import_confirm():
    added_items     = 0
    added_ownership = 0
    added_persons   = 0

    existing_items   = {k.sku.upper(): k for k in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {k.name.lower(): k for k in Item.query.all()}
    existing_persons = {p.name.lower(): p for p in Person.query.all()}

    item_count = int(request.form.get("item_count", 0))
    for i in range(item_count):
        if request.form.get(f"item_accept_{i}") != "on":
            continue

        name        = normalize_text(request.form.get(f"item_name_{i}", ""))
        sku         = request.form.get(f"item_sku_{i}", "").strip().upper() or None
        color       = normalize_text(request.form.get(f"item_color_{i}", "")) or UNKNOWN_COLOR
        edge_type   = request.form.get(f"item_edge_{i}", "Unknown")
        is_unicorn  = request.form.get(f"item_unicorn_{i}") == "on"
        category    = normalize_text(request.form.get(f"item_category_{i}", "")) or None
        notes       = request.form.get(f"item_notes_{i}", "").strip() or None
        person_name = normalize_text(request.form.get(f"item_person_{i}", ""))
        status      = request.form.get(f"item_status_{i}", "Owned")
        set_names   = [s for s in request.form.get(f"item_sets_{i}", "").split("|") if s]

        if not name:
            continue

        item = None
        if sku and sku in existing_items:
            item = existing_items[sku]
        elif name.lower() in existing_names:
            item = existing_names[name.lower()]

        if not item:
            item = Item(name=name, sku=sku, category=category,
                        edge_type=edge_type, is_unicorn=is_unicorn,
                        in_catalog=not is_unicorn, notes=notes)
            db.session.add(item)
            db.session.flush()
            ensure_unknown_variant(item)
            if sku:
                existing_items[sku] = item
            existing_names[name.lower()] = item
            added_items += 1

        # Assign set memberships
        for set_name in set_names:
            item_set = get_or_create_set(set_name)
            if item_set not in item.sets:
                item.sets.append(item_set)

        if color and color != UNKNOWN_COLOR:
            if not any(v.color.lower() == color.lower() for v in item.variants):
                new_v = ItemVariant(item_id=item.id, color=color)
                db.session.add(new_v)
                db.session.flush()

        if person_name:
            person = existing_persons.get(person_name.lower())
            if not person:
                person = Person(name=person_name)
                db.session.add(person)
                db.session.flush()
                existing_persons[person_name.lower()] = person
                added_persons += 1
            variant = next((v for v in item.variants
                            if v.color.lower() == color.lower()), item.variants[0])
            if not Ownership.query.filter_by(person_id=person.id,
                                             variant_id=variant.id).first():
                db.session.add(Ownership(person_id=person.id,
                                         variant_id=variant.id, status=status))
                added_ownership += 1

    ownership_count = int(request.form.get("own_count", 0))
    for i in range(ownership_count):
        if request.form.get(f"own_accept_{i}") != "on":
            continue

        item_id     = int(request.form.get(f"own_item_id_{i}", 0))
        person_name = normalize_text(request.form.get(f"own_person_{i}", ""))
        color       = normalize_text(request.form.get(f"own_color_{i}", "")) or UNKNOWN_COLOR
        status      = request.form.get(f"own_status_{i}", "Owned")
        notes       = request.form.get(f"own_notes_{i}", "").strip() or None

        item = Item.query.get(item_id)
        if not item or not person_name:
            continue

        person = existing_persons.get(person_name.lower())
        if not person:
            person = Person(name=person_name)
            db.session.add(person)
            db.session.flush()
            existing_persons[person_name.lower()] = person
            added_persons += 1

        variant = next((v for v in item.variants
                        if v.color.lower() == color.lower()), None)
        if not variant:
            variant = ItemVariant(item_id=item.id, color=color)
            db.session.add(variant)
            db.session.flush()

        if not Ownership.query.filter_by(person_id=person.id,
                                          variant_id=variant.id).first():
            db.session.add(Ownership(person_id=person.id,
                                     variant_id=variant.id,
                                     status=status, notes=notes))
            added_ownership += 1

    db.session.commit()
    logger.info("Import: %d items, %d ownership, %d persons",
                added_items, added_ownership, added_persons)

    parts = []
    if added_items:
        parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
    if added_persons:
        parts.append(f"{added_persons} collector{'s' if added_persons != 1 else ''}")
    if added_ownership:
        parts.append(f"{added_ownership} ownership entr{'ies' if added_ownership != 1 else 'y'}")
    flash("Import complete — added " + (", ".join(parts) if parts else "nothing new") + ".", "success")
    return redirect(url_for("catalog"))

# ── Admin auth ────────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("token") == ADMIN_TOKEN:
            resp = redirect(url_for("catalog"))
            resp.set_cookie("admin_token", ADMIN_TOKEN, httponly=True, samesite="Lax")
            flash("Admin access granted.", "success")
            return resp
        flash("Wrong token.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    resp = redirect(url_for("index"))
    resp.delete_cookie("admin_token")
    return resp

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/variants/<int:iid>")
def api_variants(iid):
    item = Item.query.get_or_404(iid)
    return jsonify([{"id": variant.id, "color": variant.color} for variant in item.variants])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

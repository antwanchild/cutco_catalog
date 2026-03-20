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
EDGE_TYPES     = ["Straight", "Double-D", "Serrated", "Micro-D", "Tec Edge", "Unknown"]
STATUS_OPTIONS = ["Owned", "Wishlist", "Sold", "Traded"]
ADMIN_TOKEN    = os.environ.get("ADMIN_TOKEN", "admin")
UNKNOWN_COLOR  = "Unknown / Unspecified"
APP_VERSION    = os.environ.get("APP_VERSION", "dev")

SCRAPE_CATEGORIES = [
    ("Kitchen Knives",  "https://www.cutco.com/shop/kitchen-knives"),
    ("Utility Knives",  "https://www.cutco.com/shop/utility-knives"),
    ("Chef Knives",     "https://www.cutco.com/shop/chef-knives"),
    ("Paring Knives",   "https://www.cutco.com/shop/paring-knives"),
    ("Outdoor Knives",  "https://www.cutco.com/shop/outdoor-knives"),
    ("Everyday Knives", "https://www.cutco.com/shop/everyday-knives"),
]
SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CutcoVaultBot/1.0)"}

# Categories always excluded from sync. Override via env var (comma-separated):
#   SYNC_BLOCKED_CATEGORIES="Knife Sets,Tableware,Accessories"
_blocked_env = os.environ.get("SYNC_BLOCKED_CATEGORIES", "Knife Sets,Accessories,Tableware")
SYNC_BLOCKED_CATEGORIES = {c.strip() for c in _blocked_env.split(",") if c.strip()}


# ── Models ────────────────────────────────────────────────────────────────────
class Knife(db.Model):
    __tablename__ = "knives"
    id         = db.Column(db.Integer,     primary_key=True)
    name       = db.Column(db.String(160), nullable=False)
    sku        = db.Column(db.String(60),  nullable=True, unique=True)
    category   = db.Column(db.String(80),  nullable=True)
    edge_type  = db.Column(db.String(40),  nullable=False, default="Unknown")
    is_unicorn = db.Column(db.Boolean,     nullable=False, default=False)
    in_catalog = db.Column(db.Boolean,     nullable=False, default=True)
    cutco_url  = db.Column(db.String(300), nullable=True)
    notes      = db.Column(db.Text,        nullable=True)
    variants   = db.relationship("KnifeVariant", backref="knife",
                                 lazy=True, cascade="all, delete-orphan",
                                 order_by="KnifeVariant.color")


class KnifeVariant(db.Model):
    __tablename__ = "knife_variants"
    id         = db.Column(db.Integer,    primary_key=True)
    knife_id   = db.Column(db.Integer,    db.ForeignKey("knives.id"), nullable=False)
    color      = db.Column(db.String(80), nullable=False, default=UNKNOWN_COLOR)
    notes      = db.Column(db.Text,       nullable=True)
    ownerships = db.relationship("Ownership", backref="variant",
                                 lazy=True, cascade="all, delete-orphan")


class Person(db.Model):
    __tablename__ = "people"
    id         = db.Column(db.Integer,     primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    notes      = db.Column(db.Text,        nullable=True)
    ownerships = db.relationship("Ownership", backref="person",
                                 lazy=True, cascade="all, delete-orphan")


class Ownership(db.Model):
    __tablename__ = "ownership"
    id         = db.Column(db.Integer,    primary_key=True)
    variant_id = db.Column(db.Integer,    db.ForeignKey("knife_variants.id"), nullable=False)
    person_id  = db.Column(db.Integer,    db.ForeignKey("people.id"),         nullable=False)
    status     = db.Column(db.String(20), nullable=False, default="Owned")
    notes      = db.Column(db.Text,       nullable=True)
    __table_args__ = (db.UniqueConstraint("variant_id", "person_id",
                                          name="uq_variant_person"),)


def ensure_unknown_variant(knife):
    if not any(v.color == UNKNOWN_COLOR for v in knife.variants):
        db.session.add(KnifeVariant(knife_id=knife.id, color=UNKNOWN_COLOR))
        db.session.flush()


with app.app_context():
    db.create_all()
    for k in Knife.query.all():
        ensure_unknown_variant(k)
    db.session.commit()
    logger.info("Database ready")


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_admin():
    return request.cookies.get("admin_token") == ADMIN_TOKEN


def scrape_catalog():
    results   = []
    seen_skus = set()
    for cat_name, cat_url in SCRAPE_CATEGORIES:
        try:
            resp = requests.get(cat_url, headers=SCRAPE_HEADERS, timeout=12)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a[href*='/p/']"):
                href      = a.get("href", "")
                parts     = href.rstrip("/").split("/")
                candidate = parts[-1].split("?")[0].split("&")[0]
                if not candidate or len(candidate) > 10:
                    continue
                if not any(c.isdigit() for c in candidate):
                    continue
                sku = candidate.upper()
                if sku in seen_skus:
                    continue
                url     = href if href.startswith("http") else f"https://www.cutco.com{href}"
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


# ── Template context ──────────────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    return dict(app_version=APP_VERSION, is_admin=is_admin)


# ── Version route ──────────────────────────────────────────────────────────────
@app.route("/version")
def version():
    return jsonify(version=APP_VERSION)


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    stats = dict(
        knives   = Knife.query.count(),
        unicorns = Knife.query.filter_by(is_unicorn=True).count(),
        people   = Person.query.count(),
        owned    = Ownership.query.filter_by(status="Owned").count(),
        wishlist = Ownership.query.filter_by(status="Wishlist").count(),
        variants = KnifeVariant.query.filter(KnifeVariant.color != UNKNOWN_COLOR).count(),
    )
    people = Person.query.order_by(Person.name).all()
    recent = Ownership.query.order_by(Ownership.id.desc()).limit(10).all()
    return render_template("index.html", stats=stats, people=people, recent=recent)


# ── Catalog ───────────────────────────────────────────────────────────────────
@app.route("/catalog")
def catalog():
    q          = request.args.get("q", "").strip()
    cat_filter = request.args.get("category", "")
    unicorn_f  = request.args.get("unicorn", "")
    sort       = request.args.get("sort", "name")
    direction  = request.args.get("dir", "asc")

    query = Knife.query
    if q:
        query = query.filter(
            db.or_(Knife.name.ilike(f"%{q}%"), Knife.sku.ilike(f"%{q}%")))
    if cat_filter:
        query = query.filter(Knife.category == cat_filter)
    if unicorn_f == "1":
        query = query.filter(Knife.is_unicorn == True)

    col    = getattr(Knife, sort, Knife.name)
    knives = query.order_by(col.desc() if direction == "desc" else col).all()
    categories = [r[0] for r in
                  db.session.query(Knife.category)
                  .filter(Knife.category.isnot(None))
                  .distinct().order_by(Knife.category).all()]

    return render_template("catalog.html", knives=knives, categories=categories,
                           q=q, cat_filter=cat_filter, unicorn_f=unicorn_f,
                           sort=sort, direction=direction,
                           edge_types=EDGE_TYPES, is_admin=is_admin(),
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/add", methods=["GET", "POST"])
def catalog_add():
    if request.method == "POST":
        knife = Knife(
            name       = request.form["name"].strip(),
            sku        = request.form.get("sku","").strip().upper() or None,
            category   = request.form.get("category","").strip() or None,
            edge_type  = request.form.get("edge_type","Unknown"),
            is_unicorn = request.form.get("is_unicorn") == "on",
            in_catalog = request.form.get("in_catalog") == "on",
            cutco_url  = request.form.get("cutco_url","").strip() or None,
            notes      = request.form.get("notes","").strip() or None,
        )
        db.session.add(knife)
        db.session.flush()
        ensure_unknown_variant(knife)
        colors = [c.strip() for c in request.form.get("colors","").split(",") if c.strip()]
        for color in colors:
            if color != UNKNOWN_COLOR:
                db.session.add(KnifeVariant(knife_id=knife.id, color=color))
        db.session.commit()
        flash(f'Added "{knife.name}" to catalog.', "success")
        return redirect(url_for("catalog"))
    return render_template("knife_form.html", knife=None,
                           edge_types=EDGE_TYPES, action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/<int:kid>/edit", methods=["GET", "POST"])
def catalog_edit(kid):
    knife = Knife.query.get_or_404(kid)
    if request.method == "POST":
        knife.name       = request.form["name"].strip()
        knife.sku        = request.form.get("sku","").strip().upper() or None
        knife.category   = request.form.get("category","").strip() or None
        knife.edge_type  = request.form.get("edge_type","Unknown")
        knife.is_unicorn = request.form.get("is_unicorn") == "on"
        knife.in_catalog = request.form.get("in_catalog") == "on"
        knife.cutco_url  = request.form.get("cutco_url","").strip() or None
        knife.notes      = request.form.get("notes","").strip() or None
        db.session.commit()
        flash(f'Updated "{knife.name}".', "success")
        return redirect(url_for("catalog"))
    return render_template("knife_form.html", knife=knife,
                           edge_types=EDGE_TYPES, action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/<int:kid>/delete", methods=["POST"])
def catalog_delete(kid):
    knife = Knife.query.get_or_404(kid)
    name  = knife.name
    db.session.delete(knife)
    db.session.commit()
    flash(f'Deleted "{name}".', "info")
    return redirect(url_for("catalog"))


# ── Variants ──────────────────────────────────────────────────────────────────
@app.route("/catalog/<int:kid>/variants")
def variants(kid):
    knife = Knife.query.get_or_404(kid)
    return render_template("variants.html", knife=knife, UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/<int:kid>/variants/add", methods=["POST"])
def variant_add(kid):
    knife = Knife.query.get_or_404(kid)
    color = request.form.get("color","").strip()
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("variants", kid=kid))
    if any(v.color.lower() == color.lower() for v in knife.variants):
        flash(f'"{color}" already exists for this knife.', "error")
        return redirect(url_for("variants", kid=kid))
    db.session.add(KnifeVariant(knife_id=kid, color=color,
                                notes=request.form.get("notes","").strip() or None))
    db.session.commit()
    flash(f'Added variant "{color}".', "success")
    return redirect(url_for("variants", kid=kid))


@app.route("/variants/<int:vid>/edit", methods=["POST"])
def variant_edit(vid):
    v     = KnifeVariant.query.get_or_404(vid)
    kid   = v.knife_id
    color = request.form.get("color","").strip()
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("variants", kid=kid))
    v.color = color
    v.notes = request.form.get("notes","").strip() or None
    db.session.commit()
    flash(f'Updated to "{color}".', "success")
    return redirect(url_for("variants", kid=kid))


@app.route("/variants/<int:vid>/delete", methods=["POST"])
def variant_delete(vid):
    v = KnifeVariant.query.get_or_404(vid)
    if len(v.knife.variants) == 1:
        flash("Cannot delete the only variant. Add another first.", "error")
        return redirect(url_for("variants", kid=v.knife_id))
    kid = v.knife_id
    db.session.delete(v)
    db.session.commit()
    flash("Variant removed.", "info")
    return redirect(url_for("variants", kid=kid))


# ── Catalog Sync ──────────────────────────────────────────────────────────────
@app.route("/catalog/sync")
def catalog_sync():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog"))

    scraped       = scrape_catalog()
    existing_skus = {k.sku for k in Knife.query.filter(Knife.sku.isnot(None)).all()}

    # Pre-filter blocked categories
    allowed   = [i for i in scraped if i["category"] not in SYNC_BLOCKED_CATEGORIES]
    new_items = [i for i in allowed  if i["sku"] not in existing_skus]

    # Group new items by category for the preview UI
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
    items    = {}
    for key, val in request.form.items():
        for prefix in ("name_", "category_", "url_"):
            if key.startswith(prefix):
                sku = key[len(prefix):]
                items.setdefault(sku, {})[prefix.rstrip("_")] = val
    added = 0
    for sku in selected:
        if Knife.query.filter_by(sku=sku).first():
            continue
        data  = items.get(sku, {})
        knife = Knife(name=data.get("name", sku), sku=sku,
                      category=data.get("category"), cutco_url=data.get("url"),
                      in_catalog=True, is_unicorn=False, edge_type="Unknown")
        db.session.add(knife)
        db.session.flush()
        ensure_unknown_variant(knife)
        added += 1
    db.session.commit()
    flash(f"Sync complete — added {added} new knife{'s' if added != 1 else ''}.", "success")
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
        person = Person(name=request.form["name"].strip(),
                        notes=request.form.get("notes","").strip() or None)
        db.session.add(person)
        db.session.commit()
        flash(f"Added {person.name}.", "success")
        return redirect(url_for("people"))
    return render_template("person_form.html", person=None, action="Add")


@app.route("/people/<int:pid>/edit", methods=["GET", "POST"])
def people_edit(pid):
    person = Person.query.get_or_404(pid)
    if request.method == "POST":
        person.name  = request.form["name"].strip()
        person.notes = request.form.get("notes","").strip() or None
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

    owned_knife_ids = {o.variant.knife_id for o in ownerships if o.status == "Owned"}
    all_knives      = Knife.query.order_by(Knife.name).all()
    knife_gaps      = [k for k in all_knives if k.id not in owned_knife_ids]

    # Variant gaps: owns some but not all known non-Unknown variants
    variant_gaps = []
    for k in all_knives:
        real_variants = [v for v in k.variants if v.color != UNKNOWN_COLOR]
        if not real_variants:
            continue
        owned_variant_ids = {o.variant_id for o in ownerships
                             if o.variant.knife_id == k.id and o.status == "Owned"}
        missing = [v for v in real_variants if v.id not in owned_variant_ids]
        if missing:
            variant_gaps.append((k, missing))

    return render_template("collection.html", person=person,
                           ownerships=ownerships,
                           knife_gaps=knife_gaps,
                           variant_gaps=variant_gaps,
                           status_options=STATUS_OPTIONS,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


# ── Ownership CRUD ────────────────────────────────────────────────────────────
@app.route("/ownership/add", methods=["GET", "POST"])
def ownership_add():
    person_id  = request.args.get("person_id",  type=int)
    knife_id   = request.args.get("knife_id",   type=int)
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
            status     = request.form.get("status","Owned"),
            notes      = request.form.get("notes","").strip() or None,
        ))
        db.session.commit()
        flash("Entry logged.", "success")
        return redirect(url_for("person_collection", pid=pid))

    sel_knife = Knife.query.get(knife_id) if knife_id else None
    return render_template("ownership_form.html", ownership=None,
                           people_list=Person.query.order_by(Person.name).all(),
                           knives_list=Knife.query.order_by(Knife.name).all(),
                           status_options=STATUS_OPTIONS,
                           sel_person_id=person_id,
                           sel_knife_id=knife_id,
                           sel_variant_id=variant_id,
                           sel_knife=sel_knife,
                           action="Add",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/ownership/<int:oid>/edit", methods=["GET", "POST"])
def ownership_edit(oid):
    o = Ownership.query.get_or_404(oid)
    if request.method == "POST":
        o.status = request.form.get("status","Owned")
        o.notes  = request.form.get("notes","").strip() or None
        db.session.commit()
        flash("Updated.", "success")
        return redirect(url_for("person_collection", pid=o.person_id))
    return render_template("ownership_form.html", ownership=o,
                           people_list=Person.query.order_by(Person.name).all(),
                           knives_list=Knife.query.order_by(Knife.name).all(),
                           status_options=STATUS_OPTIONS,
                           sel_person_id=o.person_id,
                           sel_knife_id=o.variant.knife_id,
                           sel_variant_id=o.variant_id,
                           sel_knife=o.variant.knife,
                           action="Edit",
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/ownership/<int:oid>/delete", methods=["POST"])
def ownership_delete(oid):
    o   = Ownership.query.get_or_404(oid)
    pid = o.person_id
    db.session.delete(o)
    db.session.commit()
    flash("Entry removed.", "info")
    return redirect(url_for("person_collection", pid=pid))


# ── Views ─────────────────────────────────────────────────────────────────────
@app.route("/views/knife/<int:kid>")
def knife_owners(kid):
    knife   = Knife.query.get_or_404(kid)
    entries = (Ownership.query
               .join(KnifeVariant, Ownership.variant_id == KnifeVariant.id)
               .filter(KnifeVariant.knife_id == kid)
               .order_by(Ownership.status).all())
    owner_ids      = {e.person_id for e in entries}
    people_without = (Person.query
                      .filter(~Person.id.in_(owner_ids))
                      .order_by(Person.name).all())
    return render_template("knife_owners.html", knife=knife,
                           entries=entries, people_without=people_without,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/views/matrix")
def matrix():
    people_list = Person.query.order_by(Person.name).all()
    knives_list = Knife.query.order_by(Knife.name).all()
    STATUS_RANK = {"Owned": 0, "Wishlist": 1, "Traded": 2, "Sold": 3}

    # Knife-level: best status per (person, knife)
    knife_lookup = {}
    for o in Ownership.query.all():
        key     = (o.person_id, o.variant.knife_id)
        current = knife_lookup.get(key)
        if current is None or STATUS_RANK.get(o.status, 9) < STATUS_RANK.get(current.status, 9):
            knife_lookup[key] = o

    # Variant-level: direct lookup
    variant_lookup = {(o.person_id, o.variant_id): o for o in Ownership.query.all()}

    # Variants per knife for the variant matrix (prefer real colors; fall back to Unknown)
    variants_by_knife = {
        k.id: [v for v in k.variants if v.color != UNKNOWN_COLOR] or k.variants
        for k in knives_list
    }

    return render_template("matrix.html",
                           people=people_list,
                           knives=knives_list,
                           knife_lookup=knife_lookup,
                           variant_lookup=variant_lookup,
                           variants_by_knife=variants_by_knife,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


# ── Export ────────────────────────────────────────────────────────────────────
@app.route("/export/csv")
def export_csv():
    rows = (db.session.query(Ownership, KnifeVariant, Knife, Person)
            .join(KnifeVariant, Ownership.variant_id == KnifeVariant.id)
            .join(Knife,        KnifeVariant.knife_id == Knife.id)
            .join(Person,       Ownership.person_id   == Person.id)
            .order_by(Person.name, Knife.name, KnifeVariant.color).all())
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["person","knife_name","sku","category","edge_type",
                "color","status","is_unicorn","notes"])
    for o, v, k, p in rows:
        w.writerow([p.name, k.name, k.sku or "", k.category or "",
                    k.edge_type, v.color, o.status,
                    "yes" if k.is_unicorn else "no", o.notes or ""])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_collection.csv"})


# ── Import ────────────────────────────────────────────────────────────────────
IMPORT_COLUMNS = ["name", "sku", "color", "edge_type", "is_unicorn", "person", "status", "category", "notes"]
TRUTHY = {"yes", "y", "true", "1", "x"}

SAMPLE_CSV_ROWS = [
    ["name", "sku", "color", "edge_type", "is_unicorn", "person", "status", "category", "notes"],
    ["2-3/4\" Paring Knife", "1720", "Classic Brown", "Double-D", "no", "Anthony", "Owned", "Kitchen Knives", ""],
    ["Super Shears", "2137", "Pearl White", "Straight", "no", "Anthony", "Owned", "Kitchen Knives", ""],
    ["Vintage Bread Knife", "", "Harvest Gold", "Serrated", "yes", "Anthony", "Owned", "", "Discontinued 1998"],
    ["Trimmer", "1721", "", "Double-D", "no", "Friend Name", "Wishlist", "Kitchen Knives", ""],
    ["Trimmer", "1721", "Classic Brown", "Double-D", "no", "", "", "Kitchen Knives", "catalog-only row"],
]


@app.route("/import/template")
def import_template():
    """Download a sample CSV template."""
    out = io.StringIO()
    w   = csv.writer(out)
    for row in SAMPLE_CSV_ROWS:
        w.writerow(row)
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_import_template.csv"})


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    f = request.files.get("csvfile")
    if not f or not f.filename:
        flash("Please choose a file.", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    # Person override — if set, every row is attributed to this person
    person_override = request.form.get("person_override", "").strip() or None

    ext = f.filename.rsplit(".", 1)[-1].lower()

    try:
        if ext == "xlsx":
            wb       = openpyxl.load_workbook(io.BytesIO(f.stream.read()), data_only=True)
            ws       = wb.active
            headers  = [str(cell.value).strip().lower().replace(" ", "_")
                        if cell.value is not None else ""
                        for cell in ws[1]]
            raw_rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                if all(v is None for v in row):
                    continue
                raw_rows.append({headers[i]: str(v).strip() if v is not None else ""
                                 for i, v in enumerate(row)})
            norm_rows = raw_rows
        else:
            stream   = io.StringIO(f.stream.read().decode("utf-8-sig"))
            reader   = csv.DictReader(stream)
            raw_rows = list(reader)
            norm_rows = []
            for row in raw_rows:
                norm_rows.append({k.strip().lower().replace(" ", "_"): v.strip()
                                  for k, v in row.items()})
    except Exception as exc:
        flash(f"Could not parse file: {exc}", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    # Apply person override — replaces whatever is in the person column
    if person_override:
        for row in norm_rows:
            row["person"] = person_override

    # Existing data for comparison
    existing_knives  = {k.sku.upper(): k for k in Knife.query.filter(Knife.sku.isnot(None)).all()}
    existing_names   = {k.name.lower(): k for k in Knife.query.all()}
    existing_persons = {p.name.lower(): p for p in Person.query.all()}

    # Buckets
    already_in_catalog = []   # matched, no knife action needed
    new_knives         = []   # new to catalog
    likely_unicorns    = []   # no SKU match AND not on Cutco site (flagged)
    ownership_entries  = []   # person + knife pairings to log
    conflicts          = []   # person already has knife with different status
    errors             = []   # unparseable rows

    seen_skus = set()  # dedupe within the file

    for i, row in enumerate(norm_rows, start=2):
        name       = row.get("name", "").strip()
        sku        = (row.get("sku", "") or "").strip().upper() or None
        color      = row.get("color", "").strip() or UNKNOWN_COLOR
        edge_type  = row.get("edge_type", "").strip() or "Unknown"
        is_unicorn = row.get("is_unicorn", "").strip().lower() in TRUTHY
        person_name= row.get("person", "").strip()
        status     = row.get("status", "").strip().capitalize() or "Owned"
        category   = row.get("category", "").strip() or None
        notes      = row.get("notes", "").strip() or None

        if not name:
            errors.append({"row": i, "reason": "Missing name", "data": row})
            continue
        if status not in STATUS_OPTIONS:
            status = "Owned"

        # Match against existing catalog
        matched_knife = None
        if sku and sku in existing_knives:
            matched_knife = existing_knives[sku]
        elif name.lower() in existing_names:
            matched_knife = existing_names[name.lower()]

        dedup_key = (sku or name.lower())
        if matched_knife:
            already_in_catalog.append({"knife": matched_knife, "row": row,
                                        "color": color, "person": person_name,
                                        "status": status})
        elif dedup_key not in seen_skus:
            seen_skus.add(dedup_key)
            bucket = likely_unicorns if is_unicorn or not sku else new_knives
            bucket.append({
                "name": name, "sku": sku, "color": color,
                "edge_type": edge_type, "is_unicorn": is_unicorn,
                "category": category, "notes": notes,
                "person": person_name, "status": status,
                "row": i,
            })

        # Ownership side
        if person_name and matched_knife:
            person_obj = existing_persons.get(person_name.lower())
            if person_obj:
                # Find matching variant
                variant = next((v for v in matched_knife.variants
                                if v.color.lower() == color.lower()), None)
                if variant:
                    existing_o = Ownership.query.filter_by(
                        person_id=person_obj.id, variant_id=variant.id).first()
                    if existing_o:
                        if existing_o.status != status:
                            conflicts.append({
                                "person": person_name,
                                "knife": matched_knife.name,
                                "color": color,
                                "existing_status": existing_o.status,
                                "import_status": status,
                                "oid": existing_o.id,
                            })
                        # else already identical — silently skip
                        continue
                ownership_entries.append({
                    "person": person_name,
                    "knife_name": matched_knife.name,
                    "knife_id": matched_knife.id,
                    "color": color,
                    "status": status,
                    "notes": notes,
                    "is_new_person": person_name.lower() not in existing_persons,
                })
            else:
                ownership_entries.append({
                    "person": person_name,
                    "knife_name": matched_knife.name,
                    "knife_id": matched_knife.id,
                    "color": color,
                    "status": status,
                    "notes": notes,
                    "is_new_person": True,
                })

    return render_template("import_preview.html",
                           already_in_catalog=already_in_catalog,
                           new_knives=new_knives,
                           likely_unicorns=likely_unicorns,
                           ownership_entries=ownership_entries,
                           conflicts=conflicts,
                           errors=errors,
                           edge_types=EDGE_TYPES,
                           status_options=STATUS_OPTIONS,
                           person_override=person_override)


@app.route("/import/confirm", methods=["POST"])
def import_confirm():
    added_knives    = 0
    added_ownership = 0
    added_persons   = 0

    # Re-fetch live data
    existing_knives  = {k.sku.upper(): k for k in Knife.query.filter(Knife.sku.isnot(None)).all()}
    existing_names   = {k.name.lower(): k for k in Knife.query.all()}
    existing_persons = {p.name.lower(): p for p in Person.query.all()}

    # ── Accept selected new knives ─────────────────────────────────────────
    knife_count = int(request.form.get("knife_count", 0))
    for i in range(knife_count):
        if request.form.get(f"knife_accept_{i}") != "on":
            continue
        name       = request.form.get(f"knife_name_{i}", "").strip()
        sku        = request.form.get(f"knife_sku_{i}", "").strip().upper() or None
        color      = request.form.get(f"knife_color_{i}", "").strip() or UNKNOWN_COLOR
        edge_type  = request.form.get(f"knife_edge_{i}", "Unknown")
        is_unicorn = request.form.get(f"knife_unicorn_{i}") == "on"
        category   = request.form.get(f"knife_category_{i}", "").strip() or None
        notes      = request.form.get(f"knife_notes_{i}", "").strip() or None
        person_name= request.form.get(f"knife_person_{i}", "").strip()
        status     = request.form.get(f"knife_status_{i}", "Owned")

        if not name:
            continue

        # Check again in case previous iteration added it
        knife = None
        if sku and sku in existing_knives:
            knife = existing_knives[sku]
        elif name.lower() in existing_names:
            knife = existing_names[name.lower()]

        if not knife:
            knife = Knife(name=name, sku=sku, category=category,
                          edge_type=edge_type, is_unicorn=is_unicorn,
                          in_catalog=not is_unicorn, notes=notes)
            db.session.add(knife)
            db.session.flush()
            ensure_unknown_variant(knife)
            existing_knives[sku.upper() if sku else ""] = knife
            existing_names[name.lower()] = knife
            added_knives += 1

        # Add specific color variant if provided
        if color and color != UNKNOWN_COLOR:
            if not any(v.color.lower() == color.lower() for v in knife.variants):
                new_v = KnifeVariant(knife_id=knife.id, color=color)
                db.session.add(new_v)
                db.session.flush()

        # Log ownership if person given
        if person_name:
            person = existing_persons.get(person_name.lower())
            if not person:
                person = Person(name=person_name)
                db.session.add(person)
                db.session.flush()
                existing_persons[person_name.lower()] = person
                added_persons += 1

            variant = next((v for v in knife.variants
                            if v.color.lower() == color.lower()), knife.variants[0])
            if not Ownership.query.filter_by(person_id=person.id,
                                              variant_id=variant.id).first():
                db.session.add(Ownership(person_id=person.id,
                                         variant_id=variant.id, status=status))
                added_ownership += 1

    # ── Accept selected ownership entries ──────────────────────────────────
    own_count = int(request.form.get("own_count", 0))
    for i in range(own_count):
        if request.form.get(f"own_accept_{i}") != "on":
            continue
        knife_id    = int(request.form.get(f"own_knife_id_{i}", 0))
        person_name = request.form.get(f"own_person_{i}", "").strip()
        color       = request.form.get(f"own_color_{i}", "").strip() or UNKNOWN_COLOR
        status      = request.form.get(f"own_status_{i}", "Owned")
        notes       = request.form.get(f"own_notes_{i}", "").strip() or None

        knife = Knife.query.get(knife_id)
        if not knife or not person_name:
            continue

        person = existing_persons.get(person_name.lower())
        if not person:
            person = Person(name=person_name)
            db.session.add(person)
            db.session.flush()
            existing_persons[person_name.lower()] = person
            added_persons += 1

        variant = next((v for v in knife.variants
                        if v.color.lower() == color.lower()), None)
        if not variant:
            variant = KnifeVariant(knife_id=knife.id, color=color)
            db.session.add(variant)
            db.session.flush()

        if not Ownership.query.filter_by(person_id=person.id,
                                          variant_id=variant.id).first():
            db.session.add(Ownership(person_id=person.id,
                                     variant_id=variant.id,
                                     status=status, notes=notes))
            added_ownership += 1

    db.session.commit()
    logger.info("Import: %d knives, %d ownership, %d persons",
                added_knives, added_ownership, added_persons)

    parts = []
    if added_knives:    parts.append(f"{added_knives} knife{'s' if added_knives != 1 else ''}")
    if added_persons:   parts.append(f"{added_persons} collector{'s' if added_persons != 1 else ''}")
    if added_ownership: parts.append(f"{added_ownership} ownership entr{'ies' if added_ownership != 1 else 'y'}")
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
@app.route("/api/variants/<int:kid>")
def api_variants(kid):
    knife = Knife.query.get_or_404(kid)
    return jsonify([{"id": v.id, "color": v.color} for v in knife.variants])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

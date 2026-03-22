import csv
import io
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler

import openpyxl
import requests
from bs4 import BeautifulSoup
from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, Response, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import selectinload

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR     = os.environ.get("LOG_DIR", "/data/logs")
_log_level  = getattr(logging, LOG_LEVEL, logging.INFO)
_log_format = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)

# Console handler — always on
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_format)

logging.basicConfig(level=_log_level, handlers=[_console_handler])
logger = logging.getLogger(__name__)

# Rotating file handler — writes to /data/logs/cutco.log
# 5 MB per file, keep 5 rotated files (25 MB max)
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

db = SQLAlchemy(app)

# ── Constants ─────────────────────────────────────────────────────────────────

EDGE_TYPES = ["Straight", "Double-D", "Serrated", "Micro-D", "Tec Edge", "Unknown"]
STATUS_OPTIONS = ["Owned", "Wishlist", "Sold", "Traded"]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin")
UNKNOWN_COLOR = "Unknown / Unspecified"
APP_VERSION = os.environ.get("APP_VERSION", "dev")

SCRAPE_CATEGORIES = [
    ("Utility Knives",  "https://www.cutco.com/shop/utility-knives"),
    ("Chef Knives",     "https://www.cutco.com/shop/chef-knives"),
    ("Paring Knives",   "https://www.cutco.com/shop/paring-knives"),
    ("Outdoor Knives",  "https://www.cutco.com/shop/outdoor-knives"),
    ("Everyday Knives", "https://www.cutco.com/shop/everyday-knives"),
    ("Table Knives",    "https://www.cutco.com/shop/table-knives"),
    ("Steak Knives",    "https://www.cutco.com/shop/steak-knives"),
    ("Kitchen Tools",   "https://www.cutco.com/shop/kitchen-tools"),
    ("Gadgets",         "https://www.cutco.com/shop/gadgets"),
    ("Cutting Boards",  "https://www.cutco.com/shop/cutting-boards"),
    ("Accessories",     "https://www.cutco.com/shop/cooks-tools"),
    ("Flatware",         "https://www.cutco.com/shop/flatware"),
    ("Tableware",        "https://www.cutco.com/shop/tableware"),
    ("Cookware",         "https://www.cutco.com/shop/cookware"),
    ("Ake Cookware",     "https://www.cutco.com/shop/ake-cookware"),
    ("Storage",          "https://www.cutco.com/shop/storage"),
    ("Knife Sheaths",    "https://www.cutco.com/shop/kitchen-knife-sheaths"),
    ("Garden Tools",     "https://www.cutco.com/shop/garden-tools"),
    ("Kitchen Knives",   "https://www.cutco.com/shop/kitchen-knives"),
]

_BUNDLE_KEYWORDS = {"gift", "additional"}

# Words that indicate a product is a bundle/set, not a standalone catalog item.
# Knife blocks (e.g. "Gourmet Set Block") are excluded from this check.
_SET_NAME_PATTERN = re.compile(
    r"\b(set|pack|mates|classics|combo|collection|favorites|starters|bundle)\b",
    re.IGNORECASE,
)


def _is_set_product(name: str) -> bool:
    """Return True if the name suggests a bundle/set rather than a single item."""
    if not name or not _SET_NAME_PATTERN.search(name):
        return False
    # Knife blocks are standalone items even though their name contains "set"
    return "block" not in name.lower()


SCRAPE_SETS_URL = "https://www.cutco.com/shop/knife-sets"
SCRAPE_HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; CutcoVaultBot/1.0)"}
REQUEST_TIMEOUT = 12  # seconds for all outbound HTTP requests

# Default: nothing blocked — all categories shown in preview before import.
# Override via env: SYNC_BLOCKED_CATEGORIES="Tableware,Accessories"
_blocked_env = os.environ.get("SYNC_BLOCKED_CATEGORIES", "")
SYNC_BLOCKED_CATEGORIES = {c.strip() for c in _blocked_env.split(",") if c.strip()}

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
    """Guarantee every item has an 'Unknown / Unspecified' color variant.

    This catch-all variant lets ownership be recorded even when a specific
    handle color is not known.  Called after every new Item insert.
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

def is_admin():
    return request.cookies.get("admin_token") == ADMIN_TOKEN


def _extract_sku_from_href(href: str) -> str | None:
    """Pull a base SKU from a /p/ product URL.

    Strips accessory and color suffixes so that bundle SKUs (e.g. 4135CSH —
    the 4-inch vegetable knife with sheath) resolve to the base item SKU
    (4135) rather than being treated as a separate catalog entry.

    Suffix stripping order:
      1. Remove trailing 'SH' (sheath bundle indicator)
      2. Remove trailing color letter (C / W / R / B)

    Returns None if the URL is not a product link.
    """
    parts = href.rstrip("/").split("/")
    slug = parts[-1].split("?")[0].split("&")[0].upper()
    if not slug:
        return None
    # If slug starts with digits, extract the leading numeric+letter portion.
    # This handles "1720-PETITE-CHEF" → "1720" as well as "4135CSH" → "4135C".
    lead = re.match(r'^(\d+[A-Z]{0,3})', slug)
    if lead:
        candidate = lead.group(1)
    elif any(c.isdigit() for c in slug) and len(slug) <= 12:
        # Short slug with embedded digits (e.g. "ABC1234")
        candidate = slug
    else:
        return None
    # Strip sheath suffix, then optional color letter, to get the base SKU
    if candidate.endswith("SH"):
        candidate = candidate[:-2]
    if candidate and candidate[-1] in "CWRB" and len(candidate) > 2:
        candidate = candidate[:-1]
    return candidate or None


def _discover_categories() -> list[tuple[str, str]]:
    """Scrape the Cutco shop index to discover all category pages automatically.

    Returns a list of (category_name, url) tuples found in the shop navigation.
    Falls back to an empty list if the shop page cannot be reached.
    """
    # Try the homepage — it reliably has nav links to all /shop/ categories.
    # The /shop/ index itself 404s on cutco.com.
    discovery_urls = [
        "https://www.cutco.com/",
        "https://www.cutco.com/products/knives/",
    ]
    discovered = []
    seen_slugs  = set()
    try:
        resp = None
        for url in discovery_urls:
            try:
                resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    break
            except Exception:
                continue
        if resp is None or resp.status_code != 200:
            raise RuntimeError("No discovery URL returned 200")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href*='/shop/']"):
            href = a.get("href", "").rstrip("/")
            slug = href.split("/shop/")[-1].split("?")[0]
            # Skip empty slugs, set pages, and already-seen slugs
            if not slug or slug in seen_slugs or "knife-set" in slug or "set" == slug:
                continue
            url  = href if href.startswith("http") else f"https://www.cutco.com{href}"
            # Derive a human-readable name: title-case the slug
            name = slug.replace("-", " ").title()
            seen_slugs.add(slug)
            discovered.append((name, url))
        logger.info("Discovered %d categories from shop index", len(discovered))
    except Exception as exc:
        logger.warning("Category discovery failed: %s", exc)
    return discovered


def _build_category_list() -> list[tuple[str, str]]:
    """Merge auto-discovered categories with the hardcoded SCRAPE_CATEGORIES list.

    Hardcoded entries take precedence — their names override auto-discovered
    names for the same URL slug, and they are always included even if discovery
    fails.  Discovered categories not already in SCRAPE_CATEGORIES are appended.
    """
    # Build a slug→(name, url) map from hardcoded list (authoritative)
    def slug_of(url):
        return url.rstrip("/").split("/shop/")[-1].split("?")[0].lower()

    known = {slug_of(url): (name, url) for name, url in SCRAPE_CATEGORIES}

    # Add discovered categories that aren't already covered
    for name, url in _discover_categories():
        slug = slug_of(url)
        if slug not in known:
            known[slug] = (name, url)

    return list(known.values())


def _fetch_sku_from_page(url: str) -> tuple[str | None, str | None]:
    """Fetch a product page and return (sku, name) from on-page content.

    Used for pure-slug URLs like /p/fishermans-solution where no numeric SKU
    appears in the URL itself.  Tries several extraction strategies in order:
      1. JSON-LD structured data (<script type="application/ld+json">)
      2. Inline JS variable — "sku":"1769" in raw HTML
      3. Meta tag (product:retailer_item_id or similar)
      4. On-page visible text matching "#XXXX" (scripts/styles stripped first)
      5. Broader keyword context (Model/Item/SKU followed by a number)
    """
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.info("SKU fetch: HTTP %d for %s", resp.status_code, url)
            return None, None
        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "html.parser")
        sku = None

        # Strategy 1: JSON-LD structured data — checked before script tags stripped.
        for ld in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(ld.string or "")
                items = data if isinstance(data, list) else [data]
                for entry in items:
                    if isinstance(entry, dict) and entry.get("@type") == "Product":
                        sku_val = entry.get("sku") or entry.get("productID")
                        if sku_val:
                            sku = str(sku_val).strip().upper()
                            break
            except (json.JSONDecodeError, AttributeError):
                pass
            if sku:
                break

        # Strategy 2: inline JS variable — catches patterns like:
        #   "sku":"1769"  |  'sku': '1769'  |  sku: 1769
        if not sku:
            m = re.search(
                r"""["']?sku["']?\s*:\s*["']?(\d{2,4}[A-Z]{0,2})["']?""",
                raw_html, re.IGNORECASE)
            if m:
                sku = m.group(1).upper()

        # Strategy 2b: Cutco-specific JS page variables, e.g.:
        #   const prPageId = "1886BK";
        #   const defaultWebItemSingle = "1886BK";
        # Extract only the leading digits so we store the base model number.
        if not sku:
            for var in ("prPageId", "defaultWebItemSingle"):
                m = re.search(
                    rf"""(?:const|var|let)\s+{var}\s*=\s*["']([^"']+)["']""",
                    raw_html)
                if m:
                    digits = re.match(r'^(\d{2,})', m.group(1).strip())
                    if digits:
                        sku = digits.group(1)
                        break

        # Strategy 3: meta tags (Open Graph / Schema product SKU)
        if not sku:
            for attr in ("product:retailer_item_id", "product:sku"):
                tag = soup.find("meta", property=attr) or soup.find("meta", attrs={"name": attr})
                if tag and tag.get("content", "").strip():
                    sku = tag["content"].strip().upper()
                    break

        # Remove script/style blocks before text search so CSS hex colors
        # (e.g. brand blue #0073A4) don't get matched as SKUs.
        for noise in soup.find_all(["script", "style"]):
            noise.decompose()

        # Strategy 4: on-page visible text containing "#XXXX".
        # Cutco SKUs are 2–4 digits with an optional single color letter.
        # The word-boundary anchor prevents matching 6-char hex colors like 0073A4.
        if not sku:
            sku_text = soup.find(string=re.compile(r"#\d{2,4}[A-Z]?\b"))
            if sku_text:
                m = re.search(r"#(\d{2,4}[A-Z]?)\b", sku_text.strip(), re.IGNORECASE)
                if m:
                    sku = m.group(1).upper()

        # Strategy 5: keyword context — "Model 1769", "Item No. 1769", etc.
        if not sku:
            page_text = soup.get_text(" ", strip=True)
            m = re.search(
                r"(?:model|item|sku|product)\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{2,4}[A-Z]?)\b",
                page_text, re.IGNORECASE)
            if m:
                sku = m.group(1).upper()

        # Normalise: strip trailing color letter so we store the base SKU
        if sku and len(sku) > 2 and sku[-1] in "CWRB":
            sku = sku[:-1]

        # Reject CSS hex colors — 6 hex chars like "0073A4" are never a SKU
        if sku and re.fullmatch(r"[0-9A-F]{6}", sku, re.IGNORECASE):
            sku = None

        h1 = soup.find("h1")
        name = h1.get_text(strip=True) if h1 else None
        logger.debug("SKU fetch: %s → sku=%s name=%s", url, sku, name)
        return sku, name
    except Exception as exc:
        logger.warning("SKU fetch failed: %s — %s", url, exc)
        return None, None


def scrape_catalog():
    """Scrape all item categories and return a list of item dicts."""
    results    = []
    seen_skus  = set()
    # pure-slug items queued for a single parallel fetch pass at the end:
    # list of (prod_url, cat_name, name_from_category_page)
    slug_queue: list[tuple[str, str, str | None]] = []
    seen_slug_urls: set[str] = set()

    categories = _build_category_list()
    logger.info("Scraping %d categories", len(categories))
    for cat_name, cat_url in categories:
        if cat_name in SYNC_BLOCKED_CATEGORIES:
            continue
        try:
            resp = requests.get(cat_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Walk the DOM in order, collecting product links and stopping
            # once a bundle/gift section heading is encountered.
            # Only stop at sections that explicitly indicate gift bundles or
            # additional/accessory groupings — not generic headings like "set"
            # or "collection" which appear in normal product sections too.
            product_links = []
            for element in soup.descendants:
                if element.name in ("h2", "h3", "h4") and product_links:
                    if any(kw in element.get_text(strip=True).lower()
                           for kw in _BUNDLE_KEYWORDS):
                        logger.debug("Bundle section detected on %s: '%s'",
                                     cat_url, element.get_text(strip=True))
                        break
                if element.name == "a" and "/p/" in element.get("href", ""):
                    product_links.append(element)

            logger.debug("%s: found %d /p/ links — %s",
                         cat_name, len(product_links),
                         [a.get("href", "") for a in product_links[:10]])

            # Deduplicate: strip &view=product variants so each product is
            # processed at most once per category, but keep the full href
            # (including &view=product) for the actual fetch so the server
            # returns the full product page with JSON-LD.
            seen_hrefs: set[str] = set()
            unique_links = []
            for a in product_links:
                full_href = a.get("href", "")
                base_href = full_href.split("&")[0]
                if base_href not in seen_hrefs:
                    seen_hrefs.add(base_href)
                    unique_links.append((a, full_href))

            for a, href in unique_links:
                base_href = href.split("&")[0]
                sku = _extract_sku_from_href(base_href)
                prod_url = href if href.startswith("http") else f"https://www.cutco.com{href}"

                name_el = a.find(["h2", "h3"])
                if not name_el and a.parent:
                    name_el = a.parent.find(["h2", "h3"])
                name = name_el.get_text(strip=True) if name_el else None

                if not sku:
                    # Pure-slug URL — queue for parallel page fetch after this loop.
                    # Skip sheath pages: they report the parent knife's model number,
                    # not a distinct sheath SKU, so they'd create duplicate entries.
                    if "-sheath" in prod_url or cat_name == "Knife Sheaths":
                        continue
                    # Always fetch with &view=product so the server returns the full
                    # product page including JSON-LD structured data with the SKU.
                    if "&view=product" not in prod_url:
                        prod_url = prod_url + "&view=product"
                    if prod_url not in seen_slug_urls:
                        seen_slug_urls.add(prod_url)
                        slug_queue.append((prod_url, cat_name, name))
                    continue

                if sku in seen_skus or not name or _is_set_product(name):
                    continue
                seen_skus.add(sku)
                results.append(dict(name=name, sku=sku, category=cat_name, url=prod_url))
            time.sleep(0.4)
        except Exception as exc:
            logger.warning("Scrape failed for %s: %s", cat_url, exc)

    # Parallel fetch for all pure-slug product pages collected above.
    # Using a thread pool avoids serialising hundreds of HTTP round-trips.
    if slug_queue:
        logger.info("Fetching %d pure-slug product pages (parallel)", len(slug_queue))
        added_from_slugs = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {
                pool.submit(_fetch_sku_from_page, prod_url): (prod_url, cat_name, cat_name_hint)
                for prod_url, cat_name, cat_name_hint in slug_queue
            }
            for future in as_completed(future_map):
                prod_url, cat_name, cat_page_name = future_map[future]
                sku, page_name = future.result()
                name = cat_page_name or page_name
                if not sku or sku in seen_skus or not name or _is_set_product(name):
                    continue
                seen_skus.add(sku)
                results.append(dict(name=name, sku=sku, category=cat_name, url=prod_url))
                added_from_slugs += 1
        logger.info("Slug queue: %d pages fetched, %d items added", len(slug_queue), added_from_slugs)

    return results


def scrape_sets() -> list[dict]:
    """
    Scrape the sets listing page, then visit each set detail page to extract
    member item SKUs from the Set Pieces section image URLs.

    Returns a list of dicts:
      { name, sku, url, member_skus: [str, ...] }
    """
    results = []
    seen_set_skus = set()

    try:
        resp = requests.get(SCRAPE_SETS_URL, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        set_links = []
        for a in soup.select("a[href*='/p/']"):
            href = a.get("href", "")
            # Set URLs use slug-style paths like /p/ultimate-set, not numeric SKUs
            if not href:
                continue
            name_el = a.find(["h2", "h3"])
            if not name_el and a.parent:
                name_el = a.parent.find(["h2", "h3"])
            name = name_el.get_text(strip=True) if name_el else None
            if not name:
                continue
            url = href if href.startswith("http") else f"https://www.cutco.com{href}"
            # Dedupe by URL slug
            slug = href.rstrip("/").split("/")[-1].split("&")[0]
            if slug not in seen_set_skus:
                seen_set_skus.add(slug)
                set_links.append(dict(name=name, slug=slug, url=url))
    except Exception as exc:
        logger.warning("Scrape failed for sets listing: %s", exc)
        return results

    # Visit each set detail page and extract member SKUs
    # Image URLs look like: /products/rolo/1720C-h.jpg — SKU is the numeric prefix
    sku_pattern = re.compile(r"/rolo/([0-9]+[A-Z]?)-h\.", re.IGNORECASE)

    for set_link in set_links:
        time.sleep(0.4)
        try:
            resp = requests.get(set_link["url"], headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            detail = BeautifulSoup(resp.text, "html.parser")

            # Get set SKU from the page (e.g. #1813C)
            set_sku = None
            sku_el = detail.find(string=re.compile(r"^#\d"))
            if sku_el:
                set_sku = sku_el.strip().lstrip("#").upper()

            # Extract member SKUs from rolo image URLs, strip color suffix
            member_skus = []
            seen_member = set()
            for img in detail.select("img[src*='/rolo/']"):
                src = img.get("src", "")
                match = sku_pattern.search(src)
                if match:
                    raw = match.group(1).upper()
                    # Strip trailing color letter (C, W, R, etc.) to get base SKU
                    base_sku = raw.rstrip("CWRB") if len(raw) > 2 else raw
                    if base_sku not in seen_member:
                        seen_member.add(base_sku)
                        member_skus.append(base_sku)

            results.append(dict(
                name=set_link["name"],
                sku=set_sku or set_link["slug"].upper(),
                url=set_link["url"],
                member_skus=member_skus,
            ))
            logger.debug("Set '%s': %d members", set_link["name"], len(member_skus))

        except Exception as exc:
            logger.warning("Scrape failed for set %s: %s", set_link["url"], exc)

    logger.info("Sets scraped: %d", len(results))
    return results


def get_or_create_set(name: str) -> "Set":
    """Return existing Set by name or create a new one."""
    item_set = Set.query.filter(db.func.lower(Set.name) == name.lower()).first()
    if not item_set:
        item_set = Set(name=name)
        db.session.add(item_set)
        db.session.flush()
    return item_set

# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return dict(app_version=APP_VERSION, is_admin=is_admin)

# ── Health & Version ──────────────────────────────────────────────────────────

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

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    stats = dict(
        item_count = Item.query.count(),
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
    search_query = request.args.get("q", "").strip()
    cat_filter = request.args.get("category", "")
    unicorn_f  = request.args.get("unicorn", "")
    sort       = request.args.get("sort", "name")
    direction  = request.args.get("dir", "asc")

    query = Item.query
    if search_query:
        query = query.filter(
            db.or_(Item.name.ilike(f"%{search_query}%"), Item.sku.ilike(f"%{search_query}%")))
    if cat_filter:
        query = query.filter(Item.category == cat_filter)
    if unicorn_f == "1":
        query = query.filter(Item.is_unicorn)

    col   = getattr(Item, sort, Item.name)
    items = (query
             .options(selectinload(Item.variants), selectinload(Item.sets))
             .order_by(col.desc() if direction == "desc" else col)
             .all())

    categories = [r[0] for r in
                  db.session.query(Item.category)
                  .filter(Item.category.isnot(None))
                  .distinct().order_by(Item.category).all()]

    return render_template("catalog.html", items=items, categories=categories,
                           q=search_query, cat_filter=cat_filter, unicorn_f=unicorn_f,
                           sort=sort, direction=direction,
                           edge_types=EDGE_TYPES,
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@app.route("/catalog/add", methods=["GET", "POST"])
def catalog_add():
    if request.method == "POST":
        item = Item(
            name       = request.form["name"].strip(),
            sku        = request.form.get("sku", "").strip().upper() or None,
            category   = request.form.get("category", "").strip() or None,
            edge_type  = request.form.get("edge_type", "Unknown"),
            is_unicorn = request.form.get("is_unicorn") == "on",
            in_catalog = request.form.get("in_catalog") == "on",
            cutco_url  = request.form.get("cutco_url", "").strip() or None,
            notes      = request.form.get("notes", "").strip() or None,
        )
        db.session.add(item)
        db.session.flush()
        ensure_unknown_variant(item)
        colors = [c.strip() for c in request.form.get("colors", "").split(",") if c.strip()]
        for color in colors:
            if color != UNKNOWN_COLOR:
                db.session.add(ItemVariant(item_id=item.id, color=color))
        db.session.commit()
        logger.info("Item added: %s (SKU: %s)", item.name, item.sku or "none")
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
        item.name       = request.form["name"].strip()
        item.sku        = request.form.get("sku", "").strip().upper() or None
        item.category   = request.form.get("category", "").strip() or None
        item.edge_type  = request.form.get("edge_type", "Unknown")
        item.is_unicorn = request.form.get("is_unicorn") == "on"
        item.in_catalog = request.form.get("in_catalog") == "on"
        item.cutco_url  = request.form.get("cutco_url", "").strip() or None
        item.notes      = request.form.get("notes", "").strip() or None

        # Update set memberships
        selected_set_ids = set(int(x) for x in request.form.getlist("set_ids"))
        item.sets = Set.query.filter(Set.id.in_(selected_set_ids)).all()

        db.session.commit()
        logger.info("Item updated: %s (SKU: %s)", item.name, item.sku or "none")
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
    logger.info("Item deleted: %s", name)
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
    color = request.form.get("color", "").strip()
    if not color:
        flash("Color is required.", "error")
        return redirect(url_for("variants", iid=iid))
    if any(v.color.lower() == color.lower() for v in item.variants):
        flash(f'"{color}" already exists for this item.', "error")
        return redirect(url_for("variants", iid=iid))
    db.session.add(ItemVariant(item_id=iid, color=color,
                               notes=request.form.get("notes", "").strip() or None))
    db.session.commit()
    logger.info("Variant added: %s → %s", item.name, color)
    flash(f'Added variant "{color}".', "success")
    return redirect(url_for("variants", iid=iid))


@app.route("/variants/<int:vid>/edit", methods=["POST"])
def variant_edit(vid):
    variant = ItemVariant.query.get_or_404(vid)
    iid     = variant.item_id
    color   = request.form.get("color", "").strip()
    if not color:
        flash("Color cannot be empty.", "error")
        return redirect(url_for("variants", iid=iid))
    variant.color = color
    variant.notes = request.form.get("notes", "").strip() or None
    db.session.commit()
    logger.info("Variant updated: item %d → %s", iid, color)
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
    logger.info("Variant deleted: item %d", iid)
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
        name = request.form["name"].strip()
        if Set.query.filter(db.func.lower(Set.name) == name.lower()).first():
            flash(f'Set "{name}" already exists.', "error")
            return redirect(url_for("set_add"))
        item_set = Set(name=name, notes=request.form.get("notes", "").strip() or None)
        db.session.add(item_set)
        db.session.commit()
        logger.info("Set created: %s", name)
        flash(f'Created set "{name}".', "success")
        return redirect(url_for("sets_list"))
    return render_template("set_form.html", set=None, action="Add")


@app.route("/sets/<int:sid>/edit", methods=["GET", "POST"])
def set_edit(sid):
    item_set = Set.query.get_or_404(sid)
    if request.method == "POST":
        item_set.name  = request.form["name"].strip()
        item_set.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        logger.info("Set updated: %s", item_set.name)
        flash(f'Updated set "{item_set.name}".', "success")
        return redirect(url_for("sets_list"))
    return render_template("set_form.html", set=item_set, action="Edit")


@app.route("/sets/<int:sid>/delete", methods=["POST"])
def set_delete(sid):
    item_set = Set.query.get_or_404(sid)
    name = item_set.name
    db.session.delete(item_set)
    db.session.commit()
    logger.info("Set deleted: %s", name)
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
    existing_skus = {item.sku for item in Item.query.filter(Item.sku.isnot(None)).all()}
    new_items = [i for i in scraped if i["sku"] not in existing_skus]

    from collections import OrderedDict
    grouped = OrderedDict()
    for item in new_items:
        grouped.setdefault(item["category"], []).append(item)

    # Also scrape sets for preview
    scraped_sets  = scrape_sets()
    existing_sets = {existing_set.name.lower() for existing_set in Set.query.all()}
    new_sets      = [scraped_set for scraped_set in scraped_sets if scraped_set["name"].lower() not in existing_sets]

    logger.info("Sync: %d items scraped, %d new; %d sets scraped, %d new",
                len(scraped), len(new_items), len(scraped_sets), len(new_sets))

    return render_template("sync_preview.html",
                           grouped=grouped,
                           new_items=new_items,
                           scraped_total=len(scraped),
                           new_sets=new_sets,
                           scraped_sets_total=len(scraped_sets),
                           blocked_categories=sorted(SYNC_BLOCKED_CATEGORIES))


@app.route("/catalog/sync/confirm", methods=["POST"])
def catalog_sync_confirm():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect(url_for("catalog"))

    # ── Items ──────────────────────────────────────────────────────────────
    selected = set(request.form.getlist("selected_skus"))
    item_data = {}
    for key, val in request.form.items():
        for prefix in ("name_", "category_", "url_"):
            if key.startswith(prefix):
                sku = key[len(prefix):]
                item_data.setdefault(sku, {})[prefix.rstrip("_")] = val

    added_items = 0
    for sku in selected:
        if Item.query.filter_by(sku=sku).first():
            continue
        data = item_data.get(sku, {})
        item = Item(name=data.get("name", sku), sku=sku,
                    category=data.get("category"), cutco_url=data.get("url"),
                    in_catalog=True, is_unicorn=False, edge_type="Unknown")
        db.session.add(item)
        db.session.flush()
        ensure_unknown_variant(item)
        added_items += 1

    db.session.flush()  # ensure all new items have IDs before set linkage

    # ── Sets ───────────────────────────────────────────────────────────────
    selected_sets = set(request.form.getlist("selected_sets"))
    added_sets    = 0
    linked_items  = 0

    # Build fresh SKU→Item map including items just added above
    sku_to_item = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}

    set_count = int(request.form.get("set_count", 0))
    for i in range(set_count):
        set_name = request.form.get(f"set_name_{i}", "").strip()
        if not set_name or set_name not in selected_sets:
            continue
        member_skus = [raw.strip() for raw in
                       request.form.get(f"set_members_{i}", "").split("|") if raw.strip()]

        item_set = get_or_create_set(set_name)
        if item_set.id is None:
            added_sets += 1

        for msku in member_skus:
            item = sku_to_item.get(msku.upper())
            if item and item not in item_set.items:
                item_set.items.append(item)
                linked_items += 1

    db.session.commit()

    parts = []
    if added_items:
        parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
    if added_sets:
        parts.append(f"{added_sets} set{'s' if added_sets != 1 else ''}")
    if linked_items:
        parts.append(f"{linked_items} set membership{'s' if linked_items != 1 else ''}")
    flash("Sync complete — added " + (", ".join(parts) if parts else "nothing new") + ".", "success")
    return redirect(url_for("catalog"))

# ── People ────────────────────────────────────────────────────────────────────

@app.route("/people")
def people():
    persons = Person.query.order_by(Person.name).all()
    counts  = {person.id: Ownership.query.filter_by(person_id=person.id, status="Owned").count()
               for person in persons}
    return render_template("people.html", persons=persons, counts=counts)


@app.route("/people/add", methods=["GET", "POST"])
def people_add():
    if request.method == "POST":
        person = Person(name=request.form["name"].strip(),
                        notes=request.form.get("notes", "").strip() or None)
        db.session.add(person)
        db.session.commit()
        logger.info("Person added: %s", person.name)
        flash(f"Added {person.name}.", "success")
        return redirect(url_for("people"))
    return render_template("person_form.html", person=None, action="Add")


@app.route("/people/<int:pid>/edit", methods=["GET", "POST"])
def people_edit(pid):
    person = Person.query.get_or_404(pid)
    if request.method == "POST":
        person.name  = request.form["name"].strip()
        person.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        logger.info("Person updated: %s", person.name)
        flash(f"Updated {person.name}.", "success")
        return redirect(url_for("people"))
    return render_template("person_form.html", person=person, action="Edit")


@app.route("/people/<int:pid>/delete", methods=["POST"])
def people_delete(pid):
    person = Person.query.get_or_404(pid)
    name   = person.name
    db.session.delete(person)
    db.session.commit()
    logger.info("Person deleted: %s", name)
    flash(f"Removed {name}.", "info")
    return redirect(url_for("people"))


@app.route("/people/<int:pid>/collection")
def person_collection(pid):
    person     = Person.query.get_or_404(pid)
    ownerships = (Ownership.query.filter_by(person_id=pid)
                  .order_by(Ownership.status).all())

    owned_item_ids = {o.variant.item_id for o in ownerships if o.status == "Owned"}
    all_items      = Item.query.order_by(Item.name).all()
    item_gaps      = [item for item in all_items if item.id not in owned_item_ids]

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
        logger.info("Ownership added: person %d, variant %d", pid, vid)
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
        logger.info("Ownership updated: id %d → %s", oid, ownership.status)
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
    pid       = ownership.person_id
    db.session.delete(ownership)
    db.session.commit()
    logger.info("Ownership deleted: id %d", oid)
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

    # Build item_lookup: for each (person, item) pair keep the highest-priority ownership
    item_lookup = {}
    for ownership in Ownership.query.all():
        key     = (ownership.person_id, ownership.variant.item_id)
        current = item_lookup.get(key)
        if current is None or STATUS_RANK.get(ownership.status, 9) < STATUS_RANK.get(current.status, 9):
            item_lookup[key] = ownership

    variant_lookup = {(ownership.person_id, ownership.variant_id): ownership
                      for ownership in Ownership.query.all()}

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

    logger.info("CSV export requested: %d rows", len(rows))
    out    = io.StringIO()
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

# Priority order for resolving duplicate ownership rows in the matrix view.
# Lower number = higher priority (Owned wins over Wishlist, etc.)
STATUS_RANK = {"Owned": 0, "Wishlist": 1, "Traded": 2, "Sold": 3}

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
    "price":                 "_notes_price",
    "gift box":              "_notes_gift_box",
    "sheath":                "_notes_sheath",
    "quantity purchased":    "_notes_qty",
    "given away":            "_notes_given_away",
}
# Set membership columns — key = lowercase spreadsheet header, value = canonical set name
XLSX_SET_COLS = {s.lower(): s for s in SPREADSHEET_SET_COLUMNS}


def _parse_owned_raw(owned_raw: str, default_person: str | None):
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


def _build_notes(row: dict) -> str | None:
    """Combine spreadsheet auxiliary columns (price, gift box, etc.) into a single notes string.

    Ignores blank or placeholder values (0, none, n/a, -).
    Returns None if all columns are empty.
    """
    parts = []
    for key, label in [
        ("_notes_price",     "Price"),
        ("_notes_gift_box",  "Gift Box"),
        ("_notes_sheath",    "Sheath"),
        ("_notes_qty",       "Qty Purchased"),
        ("_notes_given_away","Given Away"),
    ]:
        value = row.get(key, "").strip()
        if value and value not in ("0", "none", "n/a", "-"):
            parts.append(f"{label}: {value}")
    return "; ".join(parts) or None


@app.route("/import/template")
def import_template():
    out    = io.StringIO()
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

    f = request.files.get("csvfile")
    if not f or not f.filename:
        flash("Please choose a file.", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    person_override = request.form.get("person_override", "").strip() or None
    ext = f.filename.rsplit(".", 1)[-1].lower()
    logger.info("Import file received: %s (person override: %s)", f.filename, person_override or "none")

    try:
        if ext == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(f.stream.read()), data_only=True)
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
                out_row["_sets"] = set_memberships
                parsed_rows.append(out_row)
        else:
            stream = io.StringIO(f.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            parsed_rows = []
            for row in reader:
                out_row = {k.strip().lower().replace(" ", "_"): v.strip()
                           for k, v in row.items()}
                out_row["_sets"] = []
                parsed_rows.append(out_row)

    except Exception as exc:
        flash(f"Could not parse file: {exc}", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    # Apply person override
    if person_override:
        for row in parsed_rows:
            row["owned_raw"] = row.get("owned_raw", "yes")
            row["_person_override"] = person_override

    existing_items   = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    already_in_catalog = []
    new_items_list     = []
    likely_unicorns    = []
    ownership_entries  = []
    conflicts          = []
    errors             = []
    seen_skus          = set()

    for i, row in enumerate(parsed_rows, start=2):
        name       = row.get("name", "").strip()
        sku        = (row.get("sku", "") or "").strip().upper() or None
        color      = row.get("color", "").strip() or UNKNOWN_COLOR
        edge_type  = row.get("edge_type", "").strip() or "Unknown"
        is_unicorn = row.get("is_unicorn", "").strip().lower() in TRUTHY
        category   = row.get("category", "").strip() or None
        notes      = _build_notes(row) or row.get("notes", "").strip() or None
        set_names  = row.get("_sets", [])

        # Resolve person + status from 'Owned?' column
        owned_raw = row.get("owned_raw", row.get("status", "yes"))
        status, person_name = _parse_owned_raw(owned_raw, row.get("_person_override") or row.get("person", ""))

        if person_override:
            person_name = person_override

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

        # Dedup by (sku, color) so the same item with different handle colors
        # each get their own row in the preview.  The commit creates the item
        # once (first row) and adds variants for subsequent rows.
        dedup_key = (sku or name.lower(), color.lower())

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

    existing_items   = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    item_count = int(request.form.get("item_count", 0))
    for i in range(item_count):
        if request.form.get(f"item_accept_{i}") != "on":
            continue

        name        = request.form.get(f"item_name_{i}", "").strip()
        sku         = request.form.get(f"item_sku_{i}", "").strip().upper() or None
        color       = request.form.get(f"item_color_{i}", "").strip() or UNKNOWN_COLOR
        edge_type   = request.form.get(f"item_edge_{i}", "Unknown")
        is_unicorn  = request.form.get(f"item_unicorn_{i}") == "on"
        category    = request.form.get(f"item_category_{i}", "").strip() or None
        notes       = request.form.get(f"item_notes_{i}", "").strip() or None
        person_name = request.form.get(f"item_person_{i}", "").strip()
        status      = request.form.get(f"item_status_{i}", "Owned")
        set_names   = [sname for sname in request.form.get(f"item_sets_{i}", "").split("|") if sname]

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
        for sname in set_names:
            item_set = get_or_create_set(sname)
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

    own_count = int(request.form.get("own_count", 0))
    for i in range(own_count):
        if request.form.get(f"own_accept_{i}") != "on":
            continue

        item_id     = int(request.form.get(f"own_item_id_{i}", 0))
        person_name = request.form.get(f"own_person_{i}", "").strip()
        color       = request.form.get(f"own_color_{i}", "").strip() or UNKNOWN_COLOR
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
            logger.info("Admin login successful")
            flash("Admin access granted.", "success")
            return resp
        logger.warning("Admin login failed — wrong token")
        flash("Wrong token.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    logger.info("Admin logged out")
    resp = redirect(url_for("index"))
    resp.delete_cookie("admin_token")
    return resp

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/variants/<int:iid>")
def api_variants(iid):
    item = Item.query.get_or_404(iid)
    return jsonify([{"id": v.id, "color": v.color} for v in item.variants])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

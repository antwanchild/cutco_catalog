import os
import re

EDGE_TYPES = ["Straight", "Double-D", "Micro Double-D", "Serrated", "Micro-D", "Tec Edge", "N/A", "Unknown"]
STATUS_OPTIONS = ["Owned", "Wishlist", "Sold", "Traded"]
STATUS_RANK = {"Owned": 0, "Wishlist": 1, "Traded": 2, "Sold": 3}

ADMIN_TOKEN             = os.environ.get("ADMIN_TOKEN", "admin")
if ADMIN_TOKEN == "admin":
    import warnings
    warnings.warn(
        "ADMIN_TOKEN is set to the default value 'admin'. "
        "Set the ADMIN_TOKEN environment variable to a strong secret before exposing this service.",
        stacklevel=2,
    )
ADMIN_SESSION_SECONDS   = int(os.environ.get("ADMIN_SESSION_SECONDS", str(2 * 60 * 60)))  # default 2 h
DATA_DIR                = os.environ.get("DATA_DIR", "/data")
DISCORD_WEBHOOK_URL     = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
SHARPEN_METHODS         = ["Home Sharpener", "Whetstone", "Cutco Service", "Professional", "Other"]
SHARPEN_THRESHOLD_DAYS  = int(os.environ.get("SHARPEN_THRESHOLD_DAYS", "180"))
_cookware_threshold_env = os.environ.get("COOKWARE_THRESHOLD_DAYS")
if _cookware_threshold_env is None:
    _cookware_threshold_env = os.environ.get("BAKEWARE_THRESHOLD_DAYS", "60")
COOKWARE_THRESHOLD_DAYS = int(_cookware_threshold_env)

_cookware_env = os.environ.get("COOKWARE_CATEGORIES")
if _cookware_env is None:
    _cookware_env = os.environ.get("BAKEWARE_CATEGORIES", "Cookware,Bakeware")
COOKWARE_CATEGORIES = {cat.strip() for cat in _cookware_env.split(",") if cat.strip()}

# Backward-compatible aliases.
BAKEWARE_THRESHOLD_DAYS = COOKWARE_THRESHOLD_DAYS
BAKEWARE_CATEGORIES     = COOKWARE_CATEGORIES
KNIFE_TASK_PRESETS = [
    "Slicing bread", "Dicing vegetables", "Mincing herbs", "Carving meat",
    "Peeling fruit", "Filleting fish", "Chopping nuts", "Slicing cheese",
    "Trimming fat", "General prep",
]
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
    ("Sheaths",    "https://www.cutco.com/shop/kitchen-knife-sheaths"),
    ("Garden Tools",     "https://www.cutco.com/shop/garden-tools"),
    ("Kitchen Knives",   "https://www.cutco.com/shop/kitchen-knives"),
]

_BUNDLE_KEYWORDS = {"gift", "additional"}

CATEGORY_OVERRIDES: dict[str, str] = {
    "79": "Sheaths",  # Shears Holster
}


def _resolve_category(sku: str, scraped_category: str, name: str = "") -> str:
    """Return the effective category for an item, applying overrides."""
    if sku in CATEGORY_OVERRIDES:
        return CATEGORY_OVERRIDES[sku]
    if re.search(r"-\d+$", sku):
        return "Sheaths"
    if "sheath" in name.lower() and "with sheath" not in name.lower():
        return "Sheaths"
    return scraped_category


_SET_NAME_PATTERN = re.compile(
    r"\b(set|setting|pack|mates|classics|combo|collection|favorites|starters|bundle|companions|gift\s+box)\b",
    re.IGNORECASE,
)


def _is_set_product(name: str) -> bool:
    """Return True if the name suggests a bundle/set rather than a single item."""
    if not name or not _SET_NAME_PATTERN.search(name):
        return False
    if "set block" in name.lower():
        return False
    return True


SCRAPE_SETS_URL = "https://www.cutco.com/shop/knife-sets"
SCRAPE_HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; CutcoVaultBot/1.0)"}
REQUEST_TIMEOUT = 12

_blocked_env = os.environ.get("SYNC_BLOCKED_CATEGORIES", "")
SYNC_BLOCKED_CATEGORIES = {cat_name.strip() for cat_name in _blocked_env.split(",") if cat_name.strip()}

SPREADSHEET_SET_COLUMNS = [
    "Beast", "Fanatic", "SIGNATURE", "HOMEMAKER",
    "Accomplished Chef", "CUTCO Kitchen", "BEAST2", "HOMEMAKER2",
    "Accomplished Chef3", "CUTCO Kitchen4",
]

TRUTHY = {"yes", "y", "true", "1", "x"}

XLSX_COL_MAP = {
    "name":                  "name",
    "model #":               "sku",
    "model#":                "sku",
    "color":                 "color",
    "category":              "category",
    "edge":                  "edge_type",
    "sku unicorn?":          "is_sku_unicorn",
    "variant unicorn?":      "is_variant_unicorn",
    "edge unicorn?":         "is_edge_unicorn",
    "is_sku_unicorn":        "is_sku_unicorn",
    "is_variant_unicorn":    "is_variant_unicorn",
    "is_edge_unicorn":       "is_edge_unicorn",
    "unicorn?":              "is_unicorn",
    "owned?":                "owned_raw",
    "price":                 "_notes_price",
    "gift box":              "_notes_gift_box",
    "sheath":                "_notes_sheath",
    "quantity purchased":    "_notes_qty",
    "given away":            "_notes_given_away",
}

XLSX_SET_COLS = {s.lower(): s for s in SPREADSHEET_SET_COLUMNS}

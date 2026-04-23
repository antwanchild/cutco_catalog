import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

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
COOKWARE_THRESHOLD_DAYS = int(os.environ.get("COOKWARE_THRESHOLD_DAYS", "60"))
_cookware_env = os.environ.get("COOKWARE_CATEGORIES", "Cookware")
COOKWARE_CATEGORIES = {cat.strip() for cat in _cookware_env.split(",") if cat.strip()}
KNIFE_TASK_PRESETS = [
    "Slicing bread", "Dicing vegetables", "Mincing herbs", "Carving meat",
    "Peeling fruit", "Filleting fish", "Chopping nuts", "Slicing cheese",
    "Trimming fat", "General prep",
]
UNKNOWN_COLOR = "Unknown / Unspecified"
APP_VERSION = os.environ.get("APP_VERSION", "dev")


def _read_git_sha_from_repo() -> str | None:
    repo_root = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha or None


@lru_cache(maxsize=1)
def get_git_sha_info() -> tuple[str, str]:
    sha = os.environ.get("GIT_SHA", "").strip()
    if sha:
        return sha, "image"
    repo_sha = _read_git_sha_from_repo()
    if repo_sha:
        return repo_sha, "repo"
    return "unknown", "unknown"


def get_git_sha() -> str:
    return get_git_sha_info()[0]


GIT_SHA = get_git_sha()

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

CANONICAL_CATEGORY_ALIASES = {
    "everyday knives": "Kitchen Knives",
}


def canonicalize_category(category: str | None) -> str | None:
    """Normalize category labels to canonical values."""
    if category is None:
        return None
    normalized = category.strip()
    if not normalized:
        return None
    return CANONICAL_CATEGORY_ALIASES.get(normalized.lower(), normalized)


def _resolve_category(sku: str, scraped_category: str, name: str = "") -> str:
    """Return the effective category for an item, applying overrides."""
    if sku in CATEGORY_OVERRIDES:
        return CATEGORY_OVERRIDES[sku]
    if re.search(r"-\d+$", sku):
        return "Sheaths"
    if "sheath" in name.lower() and "with sheath" not in name.lower():
        return "Sheaths"
    return canonicalize_category(scraped_category) or scraped_category


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

TRUTHY = {"yes", "y", "true", "1", "x"}

XLSX_COL_MAP = {
    "name":                  "name",
    "item_name":             "name",
    "model #":               "sku",
    "model#":                "sku",
    "owned":                 "owned_raw",
    "color":                 "color",
    "category":              "category",
    "edge":                  "edge_type",
    "sku unicorn?":          "is_sku_unicorn",
    "sku unicorn":           "is_sku_unicorn",
    "sku_unicorn":           "is_sku_unicorn",
    "variant unicorn?":      "is_variant_unicorn",
    "variant unicorn":       "is_variant_unicorn",
    "variant_unicorn":       "is_variant_unicorn",
    "color unicorn?":        "is_variant_unicorn",
    "is color unicorn":      "is_variant_unicorn",
    "is color unicorn?":     "is_variant_unicorn",
    "is_color_unicorn":      "is_variant_unicorn",
    "edge unicorn?":         "is_edge_unicorn",
    "edge unicorn":          "is_edge_unicorn",
    "edge_unicorn":          "is_edge_unicorn",
    "is_sku_unicorn":        "is_sku_unicorn",
    "is_variant_unicorn":    "is_variant_unicorn",
    "is_edge_unicorn":       "is_edge_unicorn",
    "owned?":                "owned_raw",
    "price":                 "_notes_price",
    "quantity purchased":    "quantity_purchased",
    "quantity given away":   "quantity_given_away",
    "given away":            "quantity_given_away",
}

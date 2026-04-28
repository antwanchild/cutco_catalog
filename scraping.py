import json
import logging
import re
import time
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from constants import (
    COOKWARE_CATEGORIES, REQUEST_TIMEOUT, SCRAPE_CATEGORIES, SCRAPE_HEADERS, SCRAPE_SETS_URL,
    SYNC_BLOCKED_CATEGORIES, _BUNDLE_KEYWORDS, _resolve_category, _is_set_product,
)

logger = logging.getLogger(__name__)


def _extract_sku_from_href(href: str, *, preserve_lettered_code: bool = False) -> str | None:
    """Pull a base SKU from a /p/ product URL."""
    parts = href.rstrip("/").split("/")
    slug = parts[-1].split("?")[0].split("&")[0].upper()
    if not slug:
        return None
    if preserve_lettered_code:
        lead = re.match(r'^(\d{3,}(?:[A-Z]{0,3}(?:-\d+)?)?)', slug)
    else:
        lead = re.match(r'^(\d{3,}[A-Z]{0,3})', slug)
    if lead:
        candidate = lead.group(1)
    elif any(char.isdigit() for char in slug) and len(slug) <= 12:
        candidate = slug
    else:
        return None
    if not preserve_lettered_code:
        if candidate.endswith("SH"):
            candidate = candidate[:-2]
        stripped = re.sub(r"[A-Z]+$", "", candidate)
        if stripped and stripped.isdigit() and len(stripped) >= 2:
            candidate = stripped
    return candidate or None


def _extract_sku_from_image_src(src: str | None) -> str | None:
    if not src:
        return None
    match = re.search(r"/rolo/([0-9]+(?:-[0-9]+)?[A-Z]{0,3})-h\.", src, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


def _discover_categories() -> list[tuple[str, str]]:
    """Scrape the Cutco shop index to discover all category pages automatically."""
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
        for anchor in soup.select("a[href*='/shop/']"):
            href = anchor.get("href", "").rstrip("/")
            slug = href.split("/shop/")[-1].split("?")[0]
            if not slug or slug in seen_slugs or "knife-set" in slug or "set" == slug:
                continue
            url  = href if href.startswith("http") else f"https://www.cutco.com{href}"
            name = slug.replace("-", " ").title()
            seen_slugs.add(slug)
            discovered.append((name, url))
        logger.info("Discovered %d categories from shop index", len(discovered))
    except Exception as exc:
        logger.warning("Category discovery failed: %s", exc)
    return discovered


def _build_category_list() -> list[tuple[str, str]]:
    """Merge auto-discovered categories with the hardcoded SCRAPE_CATEGORIES list."""
    def slug_of(url):
        return url.rstrip("/").split("/shop/")[-1].split("?")[0].lower()

    known = {slug_of(url): (name, url) for name, url in SCRAPE_CATEGORIES}

    for name, url in _discover_categories():
        slug = slug_of(url)
        if slug not in known:
            known[slug] = (name, url)

    return list(known.values())


def _product_link_name(anchor) -> str | None:
    name_el = anchor.find(["h2", "h3"])
    if name_el:
        title = name_el.get_text(" ", strip=True)
        if title:
            return title
    text = anchor.get_text(" ", strip=True)
    return text or None


def _dedupe_product_links(product_links) -> list[tuple[object, str, str | None]]:
    """Keep one /p/ anchor per base URL, preferring the anchor with a title."""
    unique_links: dict[str, tuple[object, str, str | None]] = {}
    for anchor in product_links:
        full_href = anchor.get("href", "")
        base_href = full_href.split("&")[0]
        name = _product_link_name(anchor)
        existing = unique_links.get(base_href)
        if existing is None:
            unique_links[base_href] = (anchor, full_href, name)
            continue
        existing_anchor, _existing_href, existing_name = existing
        if not existing_name and name:
            unique_links[base_href] = (anchor, full_href, name)
        elif existing_anchor is None:
            unique_links[base_href] = (anchor, full_href, name)
    return list(unique_links.values())


def _should_queue_slug(prod_url: str, cat_name: str, seen_slug_urls: set[str]) -> bool:
    return prod_url not in seen_slug_urls or cat_name == "Sheaths"


def _norm_member_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _build_set_member_entries(
    structured_members: list[dict[str, str | int | None]],
    visible_rows: list[dict],
    member_skus: list[str],
    member_quantities: dict[str, int],
) -> list[dict]:
    member_entries: list[dict] = []
    if structured_members:
        used_visible: set[int] = set()
        for structured in structured_members:
            structured_norm = _norm_member_name(str(structured.get("name") or ""))
            matched_visible = None
            matched_index = None
            for visible_index, visible_row in enumerate(visible_rows):
                if visible_index in used_visible:
                    continue
                visible_norm = _norm_member_name(str(visible_row.get("name") or ""))
                if visible_norm and structured_norm and visible_norm == structured_norm:
                    matched_visible = visible_row
                    matched_index = visible_index
                    break
            if matched_index is not None:
                used_visible.add(matched_index)
            visible_sku = _normalize_set_member_sku(matched_visible.get("sku")) if matched_visible else None
            structured_sku = _normalize_set_member_sku(structured.get("sku"))
            fallback_sku = _normalize_set_member_sku(
                member_skus[matched_index]
                if matched_index is not None and matched_index < len(member_skus)
                else structured.get("sku"),
            )
            chosen_sku = visible_sku or structured_sku or fallback_sku
            member_entries.append({
                "sku": chosen_sku,
                "name": matched_visible.get("name") if matched_visible and matched_visible.get("name") else structured.get("name") or None,
                "quantity": structured.get("quantity", 1),
                "is_set_only": matched_visible.get("is_set_only", False) if matched_visible else False,
            })

        # Preserve any remaining visible rows that have a fallback SKU from the structured list.
        seen_skus = {
            _norm_member_name(str(entry.get("sku") or ""))
            for entry in member_entries
            if entry.get("sku")
        }
        for visible_index, visible_row in enumerate(visible_rows):
            if visible_index in used_visible:
                continue
            visible_sku = _normalize_set_member_sku(visible_row.get("sku"))
            fallback_sku = _normalize_set_member_sku(member_skus[visible_index]) if visible_index < len(member_skus) else None
            chosen_sku = visible_sku or fallback_sku
            if not chosen_sku:
                continue
            normalized_sku = _norm_member_name(chosen_sku)
            if normalized_sku in seen_skus:
                continue
            member_entries.append({
                "sku": chosen_sku,
                "name": visible_row.get("name") or None,
                "quantity": member_quantities.get(chosen_sku, 1),
                "is_set_only": visible_row.get("is_set_only", False),
            })
    else:
        for idx, sku in enumerate(member_skus):
            member_entries.append({
                "sku": sku,
                "name": None,
                "quantity": member_quantities.get(sku, 1),
                "is_set_only": False,
            })
    return member_entries


def _member_hover_title(member_name: str | None) -> str | None:
    title = re.sub(r"\s+", " ", str(member_name or "").strip())
    if not title:
        return None
    for separator in (",", ";", " / "):
        if separator in title:
            title = title.split(separator, 1)[0].strip()
    if re.search(r"\s+[—–-]\s+", title):
        title = re.split(r"\s+[—–-]\s+", title, maxsplit=1)[0].strip()
    words = title.split()
    if len(words) > 4 and not any(separator in title for separator in (",", ";", " / ")) and not re.search(r"\s+[—–]\s+", title):
        title = " ".join(words[:2]).strip()
    return title or None


@lru_cache(maxsize=512)
def _infer_visible_member_sku(member_name: str | None, *, context_url: str | None = None) -> str | None:
    name = str(member_name or "").strip()
    if not name:
        return None
    lower = name.lower()
    if "gift box" not in lower and "sheath" not in lower:
        return None
    lower = re.sub(r'(\d[\d/-]*)["”]', r"\1 inch", lower)
    lower = re.sub(r"\b(inches?|in\.)\b", "inch", lower)
    slug = re.sub(r"[^a-z0-9]+", "-", lower)
    slug = slug.replace("inch", "inch")
    slug = re.sub(r"(^|-)p-c(-|$)", r"\1pc\2", slug)
    slug = re.sub(r"(^|-)pc(-|$)", r"\1pc\2", slug)
    slug = re.sub(r"(^|-)knife-and-sheath(-|$)", r"\1knife-sheath\2", slug)
    slug = re.sub(r"(^|-)knife-sheath-set(-|$)", r"\1knife-sheath-set\2", slug)
    slug = re.sub(r"(^|-)sheath(-|$)", r"\1sheath\2", slug)
    slug = slug.strip("-")
    if not slug:
        return None
    candidate_slugs = [slug]
    if "sheath" in lower:
        compact_slug = slug.replace("-inch-", "-")
        if compact_slug != slug:
            candidate_slugs.append(compact_slug)
    for candidate_slug in candidate_slugs:
        inferred_sku, _ = _fetch_sku_from_page(f"https://www.cutco.com/p/{candidate_slug}", preserve_lettered_code=True)
        if inferred_sku:
            return inferred_sku
    if context_url and "box" in lower:
        inferred_sku, _ = _fetch_sku_from_page(context_url, preserve_lettered_code=True)
        if inferred_sku:
            return inferred_sku
    return None


def _resolve_visible_member_sku(
    href: str | list[str] | tuple[str, ...] | None,
    member_name: str | None,
    *,
    context_url: str | None = None,
    set_sku: str | None = None,
) -> str | None:
    hrefs = [href] if isinstance(href, str) else list(href or ())
    set_sku_norm = _normalize_set_member_sku(set_sku)
    for candidate_href in hrefs:
        href_sku = _extract_sku_from_href(candidate_href)
        normalized_href_sku = _normalize_set_member_sku(href_sku)
        if normalized_href_sku and normalized_href_sku != set_sku_norm:
            return href_sku
    for candidate_href in hrefs:
        fetched_sku, _ = _fetch_sku_from_page(candidate_href, preserve_lettered_code=True)
        normalized_fetched_sku = _normalize_set_member_sku(fetched_sku)
        if normalized_fetched_sku and normalized_fetched_sku != set_sku_norm:
            return fetched_sku
    return None


def _collect_visible_set_piece_rows(pieces_list, *, context_url: str | None = None, set_sku: str | None = None) -> list[dict]:
    visible_rows: list[dict] = []
    for anchor in pieces_list.select("a.pdp-set-item-detail"):
        visible_name = ""
        name_tag = anchor.select_one(".pdp-use-detail")
        if name_tag is not None:
            visible_name = name_tag.get_text(" ", strip=True)
        if not visible_name:
            image = anchor.find("img", alt=True)
            visible_name = image.get("alt", "").strip() if image else ""
        if not visible_name:
            continue
        visible_sku = _normalize_set_member_sku(anchor.get("data-item-selected"))
        if not visible_sku:
            visible_sku = _normalize_set_member_sku(_extract_sku_from_image_src(anchor.find("img", src=True).get("src") if anchor.find("img", src=True) else None))
        if not visible_sku:
            href = anchor.get("href") or ""
            visible_sku = _resolve_visible_member_sku(
                [href] if "/p/" in href else None,
                visible_name,
                context_url=context_url,
                set_sku=set_sku,
            )
        visible_rows.append({
            "name": visible_name,
            "sku": visible_sku,
            "is_set_only": not visible_sku,
        })
    for li in pieces_list.select("li.pdp-piece-no-details"):
        visible_name = ""
        name_tag = li.select_one(".pdp-use-detail")
        if name_tag is not None:
            visible_name = name_tag.get_text(" ", strip=True)
        if not visible_name:
            image = li.find("img", alt=True)
            visible_name = image.get("alt", "").strip() if image else ""
        if not visible_name:
            continue
        visible_sku = _normalize_set_member_sku(_extract_sku_from_image_src(li.find("img", src=True).get("src") if li.find("img", src=True) else None))
        visible_rows.append({
            "name": visible_name,
            "sku": visible_sku,
            "is_set_only": not visible_sku,
        })
    return visible_rows


def _normalize_set_member_sku(raw_sku: str | None) -> str | None:
    sku = (str(raw_sku or "").upper().strip().split("/")[0] if raw_sku is not None else "")
    if not sku:
        return None
    sku = re.sub(r"[\s\-]+$", "", sku)
    if re.fullmatch(r"\d{3,}-\d+", sku):
        return sku
    if re.fullmatch(r"\d{3,}(?:CD|CSH|D)", sku):
        return sku
    variant_match = re.fullmatch(r"(\d{3,})(?:[A-Z]+)?-\d+", sku)
    if variant_match:
        base = variant_match.group(1)
        return None if re.fullmatch(r"20\d{2}", base) else base
    stripped = re.sub(r"[A-Z]+$", "", sku) if len(sku) > 2 else sku
    stripped = re.sub(r"[\s\-]+$", "", stripped)
    if re.fullmatch(r"20\d{2}", stripped):
        return None
    return stripped or None


@lru_cache(maxsize=1024)
def _fetch_sku_from_page(url: str, *, preserve_lettered_code: bool = False) -> tuple[str | None, str | None]:
    """Fetch a product page and return (sku, name) from on-page content."""
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.info("SKU fetch: HTTP %d for %s", resp.status_code, url)
            return None, None
        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "html.parser")
        sku = None
        strategy_log: list[str] = []

        # Strategy 0: SKU embedded in the URL slug
        url_slug_sku = _extract_sku_from_href(url, preserve_lettered_code=preserve_lettered_code)
        if url_slug_sku:
            sku = url_slug_sku
            strategy_log.append(f"slug={sku}")

        # Strategy 1: JSON-LD structured data
        if not sku:
            for ld in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(ld.string or "")
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        if isinstance(entry, dict) and entry.get("@type") == "Product":
                            sku_val = entry.get("sku") or entry.get("productID")
                            if sku_val:
                                sku = str(sku_val).strip().upper()
                                break
                except (json.JSONDecodeError, AttributeError):
                    pass
                if sku:
                    break
            if sku:
                strategy_log.append(f"json-ld={sku}")

        # Strategy 2: Cutco-specific JS page variables
        if not sku:
            for js_var_name in ("prPageId", "defaultWebItemSingle"):
                sku_match = re.search(
                    rf"""(?:const|var|let)\s+{js_var_name}\s*=\s*["']([^"']+)["']""",
                    raw_html)
                if sku_match:
                    digits = re.match(r'^(\d{2,}(?:-\d+)?)', sku_match.group(1).strip())
                    if digits:
                        sku = digits.group(1)
                        break
            if sku:
                strategy_log.append(f"prPageId={sku}")

        # Strategy 3: generic inline JS
        if not sku:
            sku_match = re.search(
                r"""["']?sku["']?\s*:\s*["']?(\d{2,4}(?:-\d+|[A-Z]{0,2})?)["']?""",
                raw_html, re.IGNORECASE)
            if sku_match:
                sku = sku_match.group(1).upper()
                strategy_log.append(f"inline-js={sku}")

        # Strategy 4: meta tags
        if not sku:
            for attr in ("product:retailer_item_id", "product:sku"):
                tag = soup.find("meta", property=attr) or soup.find("meta", attrs={"name": attr})
                if tag and tag.get("content", "").strip():
                    sku = tag["content"].strip().upper()
                    strategy_log.append(f"meta={sku}")
                    break

        for noise in soup.find_all(["script", "style"]):
            noise.decompose()

        # Strategy 5: on-page visible text containing "#XXXX"
        if not sku:
            sku_text = soup.find(string=re.compile(r"#\d{2,4}(?:-\d+|[A-Z]{0,2})?\b"))
            if sku_text:
                sku_match = re.search(r"#(\d{2,4}(?:-\d+|[A-Z]{0,2})?)\b", sku_text.strip(), re.IGNORECASE)
                if sku_match:
                    sku = sku_match.group(1).upper()
                    strategy_log.append(f"visible-text={sku}")

        # Strategy 6: keyword context
        if not sku:
            page_text = soup.get_text(" ", strip=True)
            sku_match = re.search(
                r"(?:model|item|sku|product)\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{2,4}(?:-\d+|[A-Z]{0,2})?)\b",
                page_text, re.IGNORECASE)
            if sku_match:
                sku = sku_match.group(1).upper()
                strategy_log.append(f"keyword={sku}")

        # Normalise: strip trailing color/variant letters unless explicitly
        # told to preserve a lettered SKU code (used for gift-box products).
        if not preserve_lettered_code:
            stripped = re.sub(r"[A-Z]+$", "", sku or "")
            if stripped and stripped.isdigit() and len(stripped) >= 2:
                sku = stripped

        # Reject CSS hex colors, but keep valid Cutco codes like 2130CD.
        if sku and re.fullmatch(r"[0-9A-F]{6}", sku, re.IGNORECASE) and not sku[0].isdigit():
            sku = None

        page_heading = soup.find("h1")
        name = page_heading.get_text(strip=True) if page_heading else None
        logger.info("SKU fetch: %s → sku=%s [%s] name=%s",
                    url, sku, ", ".join(strategy_log) or "none", name)
        return sku, name
    except Exception as exc:
        logger.warning("SKU fetch failed: %s — %s", url, exc)
        return None, None


def scrape_item_uses(url: str) -> list[str]:
    """Fetch a product page and return uses from the 'Uses+' accordion section."""
    clean_url = url.split("&view=")[0].split("?view=")[0]
    try:
        resp = requests.get(clean_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.debug("Uses fetch: HTTP %d for %s", resp.status_code, clean_url)
            return []
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find the heading that starts with "Uses" (e.g. "Uses+")
        uses_heading = None
        for tag in soup.find_all(["h2", "h3", "h4"]):
            if tag.get_text(strip=True).lower().startswith("uses"):
                uses_heading = tag
                break

        if not uses_heading:
            return []

        # The <ul> is usually a sibling; fall back to next <ul> anywhere after it
        uses_ul = uses_heading.find_next_sibling("ul")
        if not uses_ul:
            parent = uses_heading.parent
            uses_ul = parent.find_next_sibling("ul") if parent else None
        if not uses_ul:
            uses_ul = uses_heading.find_next("ul")
        if not uses_ul:
            return []

        uses = [li.get_text(strip=True) for li in uses_ul.find_all("li") if li.get_text(strip=True)]
        logger.debug("Uses fetch: %s → %d uses", clean_url, len(uses))
        return uses
    except Exception as exc:
        logger.warning("Uses scrape failed for %s: %s", url, exc)
        return []


_EDGE_NORMALIZE = {
    "double-d":           "Double-D",
    "double-d®":          "Double-D",
    "micro double-d™":    "Micro Double-D",
    "micro double-d":     "Micro Double-D",
    "straight":           "Straight",
    "serrated":           "Serrated",
    "micro-d":            "Micro-D",
    "micro-d®":           "Micro-D",
    "tec edge":           "Tec Edge",
    "tec-edge":           "Tec Edge",
}


def scrape_item_specs(url: str) -> dict:
    """Fetch a product page and return edge, price, and key measurements.

    edge_type: 'N/A' = no blade edge, 'Unknown' = fetch failure or ambiguous.
    All other keys are None when not found.
    """
    clean_url = url.split("&view=")[0].split("?view=")[0]
    result = {
        "edge_type":      "Unknown",
        "msrp":           None,
        "blade_length":   None,
        "overall_length": None,
        "weight":         None,
    }
    try:
        resp = requests.get(clean_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return result
        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "html.parser")

        # ── Edge type ────────────────────────────────────────────────────────
        item_class_m    = re.search(r'"itemClass"\s*:\s*"([^"]+)"', raw_html)
        item_subclass_m = re.search(r'"itemSubclass"\s*:\s*"([^"]+)"', raw_html)
        if (item_class_m and item_class_m.group(1) == "FLA"
                and item_subclass_m and item_subclass_m.group(1) == "STL"):
            result["edge_type"] = "N/A"
        else:
            edge_match = re.search(r'"specName"\s*:\s*"Edge"\s*,\s*"specValue"\s*:\s*"([^"]+)"', raw_html)
            if not edge_match:
                edge_match = re.search(r'"specValue"\s*:\s*"([^"]+)"\s*,\s*"specName"\s*:\s*"Edge"', raw_html)
            if edge_match:
                result["edge_type"] = _EDGE_NORMALIZE.get(edge_match.group(1).strip().lower(), "Unknown")
            else:
                result["edge_type"] = "N/A"

        # ── itemSpecs (blade length, overall length, weight) ─────────────────
        spec_map = {
            "length - blade":   "blade_length",
            "length - overall": "overall_length",
            "weight - knife only": "weight",
            "weight":           "weight",
        }
        for spec in re.finditer(
                r'"specName"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"specValue"\s*:\s*"((?:\\.|[^"\\])*)"',
                raw_html):
            try:
                key = json.loads(f'"{spec.group(1)}"').strip().lower()
                val = json.loads(f'"{spec.group(2)}"').strip()
            except json.JSONDecodeError:
                key = spec.group(1).strip().lower()
                val = spec.group(2).strip()
            field = spec_map.get(key)
            if field and result[field] is None:
                result[field] = val

        # ── MSRP (JSON-LD → og:price → itemprop) ────────────────────────────
        for ld_tag in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                data = _json.loads(ld_tag.string or "")
                entries = data if isinstance(data, list) else [data]
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("@type") != "Product":
                        continue
                    offers = entry.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price_val = offers.get("price") if isinstance(offers, dict) else None
                    if price_val is not None:
                        result["msrp"] = float(price_val)
                        break
            except (ValueError, AttributeError):
                pass
            if result["msrp"] is not None:
                break
        if result["msrp"] is None:
            # Cutco embeds fullRetail in webItemsMap JS
            retail_m = re.search(r'"fullRetail"\s*:\s*([\d.]+)', raw_html)
            if retail_m:
                try:
                    result["msrp"] = float(retail_m.group(1))
                except ValueError:
                    pass
        if result["msrp"] is None:
            og_tag = soup.find("meta", property="og:price:amount")
            if og_tag and og_tag.get("content", "").strip():
                try:
                    result["msrp"] = float(og_tag["content"].replace(",", ""))
                except ValueError:
                    pass

        logger.debug("Specs: %s → %s", clean_url, result)
    except Exception as exc:
        logger.warning("Spec scrape failed for %s: %s", url, exc)
    return result


_VARIANT_SKIP_LABELS = {
    "",
    "select",
    "variant",
    "variants",
    "color",
    "colors",
    "finish",
    "block finish",
    "handle color",
    "default",
    "choose",
    "choose a color",
    "choose color",
    "add gift wrap",
    "add personalization",
}


def _collect_variant_candidate(candidates: list[str], seen: set[str], value: str | None) -> None:
    candidate = _normalize_variant_label(value or "")
    if not candidate:
        return
    key = candidate.lower()
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate)


def _collect_variant_candidates_from_swatches(soup: BeautifulSoup) -> tuple[str, ...]:
    """Extract color-like choices from product swatch groups."""
    candidates: list[str] = []
    seen: set[str] = set()
    for fieldset in soup.select("fieldset.swatch-group"):
        group_text = " ".join(
            [
                " ".join(fieldset.get("class", [])),
                str(fieldset.get("data-type", "")),
                fieldset.get_text(" ", strip=True),
            ]
        ).lower()
        if not any(keyword in group_text for keyword in ("color", "finish")):
            continue
        for swatch in fieldset.select(".swatch.product-option"):
            swatch_classes = {cls.lower() for cls in swatch.get("class", [])}
            if swatch_classes & {"engraving-swatch", "design-button", "location-button", "font-swatch"}:
                continue
            swatch_sources = (
                swatch.get("data-option"),
                swatch.get("data-value"),
                swatch.get("aria-label"),
                swatch.get("title"),
                swatch.get("data-code"),
            )
            for source in swatch_sources:
                if _normalize_variant_label(source or ""):
                    _collect_variant_candidate(candidates, seen, source)
                    break
            else:
                reader_only = swatch.select_one(".reader-only")
                if reader_only:
                    _collect_variant_candidate(candidates, seen, reader_only.get_text(" ", strip=True))
                else:
                    _collect_variant_candidate(candidates, seen, swatch.get_text(" ", strip=True))
    return tuple(candidates)


def _normalize_variant_label(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", (value or "").strip()).strip(" \t\r\n:-|")
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in _VARIANT_SKIP_LABELS:
        return None
    if lowered.startswith("select "):
        return None
    if lowered.startswith("image "):
        return None
    if not any(char.isalpha() for char in cleaned):
        return None
    if len(cleaned) > 60:
        return None
    return cleaned.title()


@lru_cache(maxsize=1024)
def _extract_product_variant_colors(url: str) -> tuple[str, ...]:
    """Fetch a product page and return a tuple of candidate color/variant names."""
    clean_url = url.split("&view=")[0].split("?view=")[0]
    try:
        resp = requests.get(clean_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.debug("Variant fetch: HTTP %d for %s", resp.status_code, clean_url)
            return ()
        soup = BeautifulSoup(resp.text, "html.parser")
        for noise in soup.find_all(["script", "style"]):
            noise.decompose()
        raw_html = resp.text
        text = soup.get_text("\n", strip=True)

        candidates: list[str] = []
        seen: set[str] = set()
        swatch_candidates = _collect_variant_candidates_from_swatches(soup)
        for candidate in swatch_candidates:
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        if not swatch_candidates:
            patterns = [
                re.compile(r"Select\s+([A-Za-z0-9][A-Za-z0-9 /&'\"().,-]{0,60}?)\s+Image:", re.IGNORECASE),
                re.compile(r"(?:Color|Block Finish|Handle Color|Finish)\s*:\s*([A-Za-z0-9][A-Za-z0-9 /&'\"().,-]{0,60}?)\b", re.IGNORECASE),
            ]
            sources = [raw_html, text]
            for tag in soup.find_all(True):
                tag_text = tag.get_text(" ", strip=True)
                if tag_text:
                    sources.append(tag_text)
            for source in sources:
                for pattern in patterns:
                    for match in pattern.finditer(source):
                        candidate = _normalize_variant_label(match.group(1))
                        if not candidate:
                            continue
                        key = candidate.lower()
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates.append(candidate)
            for tag in soup.find_all(True):
                for attr in ("aria-label", "title", "alt", "value", "data-color", "data-variant", "data-name", "data-value"):
                    attr_value = tag.get(attr)
                    if attr_value:
                        _collect_variant_candidate(candidates, seen, str(attr_value))

        logger.debug("Variant fetch: %s → %d candidates", clean_url, len(candidates))
        return tuple(candidates)
    except Exception as exc:
        logger.warning("Variant scrape failed for %s: %s", url, exc)
        return ()


def scrape_item_variant_colors(url: str) -> tuple[str, ...]:
    """Public alias for the product-page variant color scraper."""
    return _extract_product_variant_colors(url)


# Preserve cache helpers on the public alias used by callers.
scrape_item_variant_colors.cache_clear = _extract_product_variant_colors.cache_clear  # type: ignore[attr-defined]
scrape_item_variant_colors.cache_info = _extract_product_variant_colors.cache_info  # type: ignore[attr-defined]


# Keep old name as alias so existing callers still work
def scrape_edge_type(url: str) -> str:
    return scrape_item_specs(url)["edge_type"]


def scrape_catalog() -> tuple[list[dict], list[tuple[str, str]]]:
    """Scrape all item categories.

    Returns (items, set_candidates).
    """
    results        = []
    set_candidates: list[tuple[str, str]] = []
    seen_skus      = set()
    seen_set_urls: set[str] = set()
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
                         [anchor.get("href", "") for anchor in product_links[:10]])

            for anchor, href, name_hint in _dedupe_product_links(product_links):
                base_href = href.split("&")[0]
                preserve_lettered_code = cat_name in COOKWARE_CATEGORIES or cat_name == "Sheaths"
                sku = _extract_sku_from_href(base_href, preserve_lettered_code=preserve_lettered_code)
                prod_url = href if href.startswith("http") else f"https://www.cutco.com{href}"

                name = name_hint

                if cat_name == "Sheaths" or (name and "sheath" in name.lower() and "with sheath" not in name.lower()):
                    sku = None

                if not sku:
                    if "&view=product" not in prod_url:
                        prod_url = prod_url + "&view=product"
                    if _should_queue_slug(prod_url, cat_name, seen_slug_urls):
                        seen_slug_urls.add(prod_url)
                        slug_queue.append((prod_url, cat_name, name))
                    continue

                if sku in seen_skus or not name:
                    continue
                if _is_set_product(name):
                    if prod_url not in seen_set_urls:
                        seen_set_urls.add(prod_url)
                        set_candidates.append((name, prod_url))
                    continue
                seen_skus.add(sku)
                results.append(dict(name=name, sku=sku,
                                    category=_resolve_category(sku, cat_name, name),
                                    url=prod_url))
            time.sleep(0.4)
        except Exception as exc:
            logger.warning("Scrape failed for %s: %s", cat_url, exc)

    if slug_queue:
        logger.info("Fetching %d pure-slug product pages (parallel)", len(slug_queue))
        added_from_slugs = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {
                pool.submit(
                    _fetch_sku_from_page,
                    prod_url,
                    preserve_lettered_code=cat_name in COOKWARE_CATEGORIES or cat_name == "Sheaths",
                ): (prod_url, cat_name, cat_name_hint)
                for prod_url, cat_name, cat_name_hint in slug_queue
            }
            for future in as_completed(future_map):
                prod_url, cat_name, cat_page_name = future_map[future]
                sku, page_name = future.result()
                name = cat_page_name or page_name
                if not sku or sku in seen_skus or not name:
                    continue
                if _is_set_product(name):
                    if prod_url not in seen_set_urls:
                        seen_set_urls.add(prod_url)
                        set_candidates.append((name, prod_url))
                    continue
                seen_skus.add(sku)
                results.append(dict(name=name, sku=sku,
                                    category=_resolve_category(sku, cat_name, name),
                                    url=prod_url))
                added_from_slugs += 1
        logger.info("Slug queue: %d pages fetched, %d items added", len(slug_queue), added_from_slugs)

    return results, set_candidates


def scrape_sets(
    extra_candidates: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Scrape the knife-sets listing page and each set detail page."""
    results = []
    seen_slugs: set[str] = set()
    set_links = []

    try:
        resp = requests.get(SCRAPE_SETS_URL, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for anchor in soup.select("a[href*='/p/']"):
            href = anchor.get("href", "")
            if not href:
                continue
            name_el = anchor.find(["h2", "h3"])
            if not name_el and anchor.parent:
                name_el = anchor.parent.find(["h2", "h3"])
            name = name_el.get_text(strip=True) if name_el else None
            if not name:
                continue
            url = href if href.startswith("http") else f"https://www.cutco.com{href}"
            slug = href.rstrip("/").split("/")[-1].split("&")[0]
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                set_links.append(dict(name=name, slug=slug, url=url))
    except Exception as exc:
        logger.warning("Scrape failed for sets listing: %s", exc)

    for name, url in (extra_candidates or []):
        slug = url.rstrip("/").split("/")[-1].split("&")[0]
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            set_links.append(dict(name=name, slug=slug, url=url))

    sku_pattern = re.compile(r"/rolo/([0-9]+[A-Z]?)-h\.", re.IGNORECASE)

    def _fetch_set_detail(set_link: dict) -> dict | None:
        fetch_url = set_link["url"]
        if "view=product" not in fetch_url:
            sep = "&" if "?" in fetch_url else "?"
            fetch_url += sep + "view=product"
        try:
            set_sku, _ = _fetch_sku_from_page(fetch_url)
            resp = requests.get(fetch_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            raw_html = resp.text
            detail = BeautifulSoup(raw_html, "html.parser")

            member_skus: list[str] = []
            member_names: list[str] = []
            member_quantities: dict[str, int] = {}
            member_is_set_only: list[bool] = []
            seen_member: set[str] = set()
            structured_members: list[dict[str, str | int | None]] = []

            # Strategy 1: parse itemSetList JSON embedded in page JS
            # Page has webItemsMap with one itemSetList per variant — search
            # from the URL's variant key so we get the right size set.
            _set_list_json = None
            _url_variant = fetch_url.split("/")[-1].split("?")[0].split("&")[0].upper()
            _search_from = 0
            if re.match(r'^\d+[A-Z]?$', _url_variant):
                # Find the webItemsMap entry specifically (key followed by {)
                _var_pos = raw_html.find(f'"{_url_variant}"' + ':{')
                if _var_pos >= 0:
                    _search_from = _var_pos
            _key_match = re.search(r'"itemSetList"\s*:\s*\[', raw_html[_search_from:])
            if _key_match:
                _start = _search_from + _key_match.end() - 1  # abs pos of opening [
                _depth, _end = 0, _start
                for _i, _ch in enumerate(raw_html[_start:], _start):
                    if _ch == '[':
                        _depth += 1
                    elif _ch == ']':
                        _depth -= 1
                        if _depth == 0:
                            _end = _i + 1
                            break
                _set_list_json = raw_html[_start:_end]

            if _set_list_json:
                try:
                    set_list = json.loads(_set_list_json)
                    for entry in set_list:
                        base_sku = _normalize_set_member_sku(entry.get("childItemNumber"))
                        if not base_sku:
                            continue
                        qty = int(entry.get("qty") or 1)
                        if base_sku not in seen_member:
                            seen_member.add(base_sku)
                            member_skus.append(base_sku)
                            member_names.append(str(entry.get("name") or entry.get("itemName") or "").strip())
                            member_quantities[base_sku] = qty
                            member_is_set_only.append(False)
                            structured_members.append({
                                "sku": base_sku,
                                "name": str(entry.get("name") or entry.get("itemName") or "").strip(),
                                "quantity": qty,
                            })
                    logger.debug("Set '%s': itemSetList → %d members", set_link["name"], len(member_skus))
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    logger.debug("itemSetList parse failed for %s: %s", fetch_url, exc)
                    member_skus.clear()
                    member_names.clear()
                    member_quantities.clear()
                    member_is_set_only.clear()
                    seen_member.clear()
                    structured_members.clear()

            # Strategy 2: fallback — image /rolo/ SKU extraction
            if not member_skus:
                for img in detail.select("img[src*='/rolo/']"):
                    match = sku_pattern.search(img.get("src", ""))
                    if match:
                        base_sku = _normalize_set_member_sku(match.group(1))
                        if not base_sku:
                            continue
                        if base_sku not in seen_member:
                            seen_member.add(base_sku)
                            member_skus.append(base_sku)
                            member_names.append("")
                            member_quantities[base_sku] = 1
                            member_is_set_only.append(False)
                            structured_members.append({
                                "sku": base_sku,
                                "name": "",
                                "quantity": 1,
                            })
                logger.debug("Set '%s': image fallback → %d members", set_link["name"], len(member_skus))

            # Strategy 3: visible Set Pieces labels, if present
            visible_rows: list[dict] = []
            heading = None
            for tag in detail.find_all(["h2", "h3", "h4"]):
                if tag.get_text(strip=True).lower().startswith("set pieces"):
                    heading = tag
                    break
            if heading:
                pieces_list = heading.find_next("ul")
                if pieces_list:
                    visible_rows.extend(_collect_visible_set_piece_rows(
                        pieces_list,
                        context_url=set_link["url"],
                        set_sku=set_sku,
                    ))
            if visible_rows:
                logger.debug("Set '%s': visible Set Pieces labels → %d names", set_link["name"], len(visible_rows))

            def _norm_member_name(value: str) -> str:
                return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

            member_entries = []
            if visible_rows:
                used_structured: set[int] = set()
                for idx, visible_row in enumerate(visible_rows):
                    visible_name = visible_row.get("name") or None
                    visible_norm = _norm_member_name(visible_name or "")
                    matched_structured = None
                    matched_index = None
                    for inner_idx, structured in enumerate(structured_members):
                        if inner_idx in used_structured:
                            continue
                        structured_name = _norm_member_name(str(structured.get("name") or ""))
                        if visible_norm and structured_name == visible_norm:
                            matched_structured = structured
                            matched_index = inner_idx
                            break
                    if matched_index is not None:
                        used_structured.add(matched_index)
                    visible_sku = _normalize_set_member_sku(visible_row.get("sku"))
                    structured_sku = _normalize_set_member_sku(matched_structured.get("sku")) if matched_structured else None
                    fallback_sku = _normalize_set_member_sku(member_skus[idx]) if idx < len(member_skus) else None
                    fallback_qty = member_quantities.get(
                        visible_sku or structured_sku or fallback_sku,
                        1,
                    )
                    chosen_sku = visible_sku or structured_sku or fallback_sku
                    if chosen_sku and set_sku and chosen_sku == set_sku:
                        chosen_sku = visible_sku or structured_sku or fallback_sku
                    member_entries.append(dict(
                        sku=chosen_sku,
                        name=visible_name,
                        quantity=matched_structured["quantity"] if matched_structured else fallback_qty,
                        is_set_only=visible_row["is_set_only"] if matched_structured is None else False,
                    ))
            else:
                for idx, sku in enumerate(member_skus):
                    member_entries.append(dict(
                        sku=sku,
                        name=member_names[idx] if idx < len(member_names) and member_names[idx] else None,
                        quantity=member_quantities.get(sku, 1),
                        is_set_only=member_is_set_only[idx] if idx < len(member_is_set_only) else False,
                    ))

            logger.debug("Set '%s': sku=%s, %d members", set_link["name"], set_sku, len(member_skus))
            return dict(
                name             = set_link["name"],
                sku              = set_sku,
                url              = set_link["url"],
                member_skus      = member_skus,
                member_quantities= member_quantities,
                member_entries   = member_entries,
            )
        except Exception as exc:
            logger.warning("Scrape failed for set %s: %s", set_link["url"], exc)
            return None

    logger.info("Fetching %d set detail pages (parallel)", len(set_links))
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(_fetch_set_detail, set_links):
            if result is not None:
                results.append(result)

    logger.info("Sets scraped: %d", len(results))
    return results

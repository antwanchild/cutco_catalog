"""Scraping helpers for Cutco catalog, product, and set data."""

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NotRequired, TypedDict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import PageElement, Tag

from constants import (
    COOKWARE_CATEGORIES,
    REQUEST_TIMEOUT,
    SCRAPE_CATEGORIES,
    SCRAPE_HEADERS,
    SCRAPE_SETS_URL,
    SYNC_BLOCKED_CATEGORIES,
    _BUNDLE_KEYWORDS,
    _resolve_category,
    _is_set_product,
)

logger = logging.getLogger(__name__)

_CUTTING_BOARD_URLS = {
    "124": "https://www.cutco.com/p/small-cutting-board",
    "125": "https://www.cutco.com/p/medium-cutting-board",
    "126": "https://www.cutco.com/p/large-cutting-board",
}


class StructuredSetMember(TypedDict):
    """Structured member data parsed from Cutco set JSON."""

    sku: str | None
    name: str | None
    quantity: int


class VisibleSetRow(TypedDict):
    """Visible set member row parsed from Cutco page markup."""

    name: str
    sku: str | None
    is_set_only: bool


class SetMemberEntry(TypedDict):
    """Normalized set member data returned to catalog sync."""

    sku: str | None
    name: str | None
    quantity: int
    is_set_only: bool


class SetVariantOptions(TypedDict):
    """Set options split by the product component they customize."""

    handle_colors: tuple[str, ...]
    block_finishes: tuple[str, ...]
    handle_colors_authoritative: NotRequired[bool]
    handle_color_member_skus: NotRequired[dict[str, tuple[str, ...]]]


def _tag_attr_text(tag: PageElement | None, *attrs: str) -> str | None:
    """Return the first non-empty string attribute from a BeautifulSoup tag."""
    if not isinstance(tag, Tag):
        return None
    for attr in attrs:
        value = tag.get(attr)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
        elif value is not None:
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    return None


def _tag_classes(tag: Tag) -> set[str]:
    """Return a normalized class set from a BeautifulSoup tag."""
    classes = tag.get("class")
    if isinstance(classes, str):
        return {classes.lower()}
    if isinstance(classes, (list, tuple, set)):
        return {str(cls).lower() for cls in classes}
    return set()


def _extract_cutco_canonical_url(
    raw_html: str, *, fallback_url: str | None = None
) -> str | None:
    """Return a canonical Cutco product URL from page metadata when present."""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in (
        soup.find("link", rel="canonical"),
        soup.find("meta", property="og:url"),
    ):
        if not tag:
            continue
        candidate = _tag_attr_text(tag, "href", "content")
        if candidate and "/p/" in candidate:
            return candidate
    return fallback_url


def _resolve_cutco_product_url(url: str) -> str:
    """Return a size-specific Cutco product URL when a family page is ambiguous."""
    sku_hint = _extract_sku_from_href(url, preserve_lettered_code=True)
    if sku_hint in _CUTTING_BOARD_URLS and "cutting-boards" in url:
        return _CUTTING_BOARD_URLS[sku_hint]
    return url


def _find_cutco_item_link(
    raw_html: str, item_name: str | None, sku: str | None = None
) -> str | None:
    """Return a matching product link from a family page when the item identity is known."""
    normalized_item = _normalize_text_for_match(item_name or "")
    normalized_sku = _normalize_text_for_match(sku or "")
    if not normalized_item:
        if not normalized_sku:
            return None

    soup = BeautifulSoup(raw_html, "html.parser")
    wants_sheath = "sheath" in normalized_item or "gift box" in normalized_item
    best_match: tuple[int, str] | None = None
    for anchor in soup.select("a[href*='/p/']"):
        href = _tag_attr_text(anchor, "href")
        if not href:
            continue
        texts = [
            anchor.get_text(" ", strip=True),
            _tag_attr_text(anchor, "aria-label") or "",
            _tag_attr_text(anchor, "title") or "",
            href,
        ]
        image = anchor.find("img", alt=True)
        if image:
            texts.append(_tag_attr_text(image, "alt") or "")
        candidate_text = " ".join(texts)
        normalized_text = _normalize_text_for_match(candidate_text)
        if not normalized_text:
            continue
        score = 0
        if normalized_item:
            if normalized_text == normalized_item:
                score += 100
            elif normalized_text.startswith(normalized_item):
                score += 80
            elif normalized_item in normalized_text:
                score += 60
            if (
                normalized_item.split()[:2]
                and " ".join(normalized_item.split()[:2]) in normalized_text
            ):
                score += 10
        if normalized_sku:
            if normalized_sku in normalized_text:
                score += 90
            if normalized_sku in _normalize_text_for_match(href):
                score += 80
        if re.search(
            r"\b(with\s+sheath|knife\s+and\s+sheath|knife\s+sheath|sheath\s+set|gift\s+box|bundle)\b",
            normalized_text,
        ):
            score -= 50 if not wants_sheath else 0
        if re.search(r"\b(set|kit)\b", normalized_text) and not wants_sheath:
            score -= 10
        if score <= 0:
            continue
        if best_match is None or score > best_match[0]:
            best_match = (score, urljoin("https://www.cutco.com", href))
    return best_match[1] if best_match else None


def _fetch_cutco_page(
    url: str,
    *,
    item_name: str | None = None,
    sku: str | None = None,
) -> tuple[str | None, str | None]:
    """Fetch a Cutco page and follow canonical or item-specific URLs when exposed."""
    try:
        resolved_request_url = _resolve_cutco_product_url(url)
        resp = requests.get(
            resolved_request_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            return None, None
        if item_name or sku:
            item_url = _find_cutco_item_link(resp.text, item_name, sku)
            if item_url and item_url != resp.url:
                try:
                    item_resp = requests.get(
                        item_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
                    )
                    if item_resp.status_code == 200:
                        return item_resp.url, item_resp.text
                except Exception:
                    pass
        canonical_url = _extract_cutco_canonical_url(resp.text, fallback_url=resp.url)
        if canonical_url and canonical_url != resp.url:
            try:
                canonical_resp = requests.get(
                    canonical_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
                )
                if canonical_resp.status_code == 200:
                    return canonical_resp.url, canonical_resp.text
            except Exception:
                pass
        return resp.url, resp.text
    except Exception:
        return None, None


def _resolve_cutco_item_page_url(url: str, *, item_name: str | None = None) -> str:
    """Return the best Cutco page URL for a specific item when possible."""
    resolved_url, _ = _fetch_cutco_page(url, item_name=item_name)
    return resolved_url or url


def _normalize_text_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _normalize_cutco_title(value: str) -> str:
    normalized = _normalize_text_for_match(value)
    normalized = re.sub(r"^cutco\s+", "", normalized)
    normalized = re.sub(r"^#?\s*\d{2,4}(?:[a-z]{0,3})?(?:\s+)?", "", normalized)
    return normalized.strip()


def _line_matches_item_identity(
    line: str, item_name: str | None, sku: str | None = None
) -> bool:
    """Return True when a visible line looks like the target product."""
    normalized_item = _normalize_cutco_title(item_name or "")
    normalized_line = _normalize_cutco_title(line)
    if not normalized_line:
        return False
    if sku:
        normalized_sku = _normalize_text_for_match(sku)
        if normalized_sku and re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_sku)}(?![a-z0-9])",
            _normalize_text_for_match(line),
        ):
            if (
                not normalized_item
                or normalized_item in normalized_line
                or normalized_line in normalized_item
            ):
                return True
            # Some Cutco pages expose a slightly different product title; the SKU
            # is still a reliable anchor for the main product block.
            if normalized_line:
                return True
    if not normalized_item:
        return False
    if normalized_line == normalized_item:
        return True
    if normalized_line.startswith(normalized_item) or normalized_item.startswith(
        normalized_line
    ):
        return True
    return normalized_item in normalized_line or normalized_line in normalized_item


def _extract_primary_visible_price(
    page_text: str,
    *,
    heading_text: str | None = None,
    candidate_text: str | None = None,
    sku: str | None = None,
) -> float | None:
    """Extract the main visible product price from Cutco page text.

    Cutco product pages often include extra prices later in the page for
    frequently-bought-together items or related accessories. The primary
    product price is usually positioned before the regular-shipping copy.
    """
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    if not lines:
        return None

    start_index = 0
    normalized_candidate = _normalize_text_for_match(candidate_text or "")
    normalized_heading = _normalize_text_for_match(heading_text or "")
    wants_sheath = (
        "sheath" in normalized_candidate or "gift box" in normalized_candidate
    )
    if normalized_candidate:
        for index, line in enumerate(lines):
            if _line_matches_item_identity(line, candidate_text, sku=sku):
                start_index = index
                break
        else:
            if sku:
                normalized_sku = _normalize_text_for_match(sku)
                for index, line in enumerate(lines):
                    if normalized_sku and re.search(
                        rf"(?<![a-z0-9]){re.escape(normalized_sku)}(?![a-z0-9])",
                        _normalize_text_for_match(line),
                    ):
                        start_index = index
                        break
    elif normalized_heading:
        for index, line in enumerate(lines):
            if normalized_heading in _normalize_text_for_match(line):
                start_index = index + 1
                break

    cut_markers = (
        "Add to Cart",
        "Frequently Bought Together",
        "Specifications",
        "Reviews & Questions",
        "Overview +",
        "Set Pieces +",
    )
    stop_markers = cut_markers + (
        "Regular shipping and handling included",
        "Regular shipping included",
        "Shipping and handling included",
    )

    truncated_lines = lines[start_index:]
    for stop_index, line in enumerate(truncated_lines):
        if any(marker.lower() in line.lower() for marker in stop_markers):
            truncated_lines = truncated_lines[:stop_index]
            break

    for line in truncated_lines:
        normalized_line = _normalize_text_for_match(line)
        if normalized_candidate and not wants_sheath:
            if re.search(
                r"\b(with\s+sheath|knife\s+and\s+sheath|knife\s+sheath|sheath\s+set|gift\s+box|bundle)\b",
                normalized_line,
            ):
                continue
        for dollar_match in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", line):
            try:
                price = float(dollar_match.group(1).replace(",", ""))
            except ValueError:
                continue
            if price > 0:
                if normalized_candidate:
                    return price
                return price
    return None


def _coerce_positive_price(raw_price: object) -> float | None:
    """Convert a scraped value to a positive price, rejecting placeholders."""
    if not isinstance(raw_price, str | float | int):
        return None
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _coerce_int(raw_value: object, default: int = 1) -> int:
    """Convert a scraped value to an int, falling back on invalid input."""
    if not isinstance(raw_value, str | float | int):
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _extract_cutco_price(
    raw_html: str,
    *,
    page_url: str | None = None,
    item_name: str | None = None,
    sku: str | None = None,
) -> float | None:
    """Return the most reliable product price found on a Cutco page.

    Cutco pages often expose multiple price sources. The page-local JS values
    tend to match the rendered product price better than JSON-LD, which can
    point at alternate offers or variants on pages like shears and cutting
    boards.
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    page_sku = None
    if page_url and "/p/" in page_url:
        page_sku = _extract_sku_from_href(page_url, preserve_lettered_code=True)

    # Prefer the rendered price the customer sees first.
    heading = soup.find("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else None
    for noise in soup.find_all(["script", "style"]):
        noise.decompose()
    page_text = soup.get_text("\n", strip=True)
    visible_price = _extract_primary_visible_price(
        page_text,
        heading_text=heading_text,
        candidate_text=item_name,
        sku=sku or page_sku,
    )
    if visible_price is not None:
        return visible_price

    # Fallback to the page's own product JS if the visible price is absent.
    for key in ("actualPrice", "fullRetail"):
        price_match = re.search(rf'"{key}"\s*:\s*([\d.]+)', raw_html)
        if price_match:
            price = _coerce_positive_price(price_match.group(1))
            if price is not None:
                return price

    # JSON-LD can contain multiple Product entries; if we know the SKU, try to
    # select the matching one instead of blindly taking the first offer.
    for ld_tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(str(ld_tag.string or ""))
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("@type") != "Product":
                    continue
                if page_sku:
                    entry_sku = (
                        str(entry.get("sku") or entry.get("productID") or "")
                        .strip()
                        .upper()
                    )
                    if (
                        entry_sku
                        and page_sku not in entry_sku
                        and entry_sku not in page_sku
                    ):
                        continue
                offers = entry.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price_val = offers.get("price") if isinstance(offers, dict) else None
                price = _coerce_positive_price(price_val)
                if price is not None:
                    return price
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

    og_tag = soup.find("meta", property="og:price:amount")
    og_content = _tag_attr_text(og_tag, "content")
    if og_content:
        price = _coerce_positive_price(og_content.replace(",", ""))
        if price is not None:
            return price

    price_el = soup.select_one('meta[itemprop="price"]')
    if price_el:
        raw = _tag_attr_text(price_el, "content") or price_el.get_text(strip=True)
        price_match = re.search(r"[\d,]+\.?\d*", raw or "")
        if price_match:
            price = _coerce_positive_price(price_match.group().replace(",", ""))
            if price is not None:
                return price

    return None


def _extract_sku_from_href(
    href: str, *, preserve_lettered_code: bool = False
) -> str | None:
    """Pull a base SKU from a /p/ product URL."""
    parts = href.rstrip("/").split("/")
    slug = parts[-1].split("?")[0].split("&")[0].upper()
    if not slug:
        return None
    if preserve_lettered_code:
        match = re.fullmatch(r"\d{3,}(?:[A-Z]{0,3}(?:-\d+)?)?", slug)
    else:
        match = re.fullmatch(r"\d{3,}[A-Z]{0,3}", slug)
    if not match:
        return None
    candidate = match.group(0)
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
    discovered: list[tuple[str, str]] = []
    seen_slugs: set[str] = set()
    try:
        resp = None
        for url in discovery_urls:
            try:
                resp = requests.get(
                    url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
                )
                if resp.status_code == 200:
                    break
            except Exception:
                continue
        if resp is None or resp.status_code != 200:
            raise RuntimeError("No discovery URL returned 200")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for anchor in soup.select("a[href*='/shop/']"):
            href = (_tag_attr_text(anchor, "href") or "").rstrip("/")
            slug = href.split("/shop/")[-1].split("?")[0]
            if not slug or slug in seen_slugs or "knife-set" in slug or "set" == slug:
                continue
            url = href if href.startswith("http") else f"https://www.cutco.com{href}"
            name = slug.replace("-", " ").title()
            seen_slugs.add(slug)
            discovered.append((name, url))
        logger.info("Discovered %d categories from shop index", len(discovered))
    except Exception as exc:
        logger.warning("Category discovery failed: %s", exc)
    return discovered


def _build_category_list() -> list[tuple[str, str]]:
    """Merge auto-discovered categories with the hardcoded SCRAPE_CATEGORIES list."""

    def slug_of(url: str) -> str:
        return url.rstrip("/").split("/shop/")[-1].split("?")[0].lower()

    known: dict[str, tuple[str, str]] = {
        slug_of(url): (name, url) for name, url in SCRAPE_CATEGORIES
    }

    for name, url in _discover_categories():
        slug = slug_of(url)
        if slug not in known:
            known[slug] = (name, url)

    return list(known.values())


@lru_cache(maxsize=1)
def _cutco_product_url_lookup() -> dict[str, str]:
    """Build a SKU-to-product-URL index from Cutco's category pages."""

    def _fetch_category(category_url: str) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            response = requests.get(
                category_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.select("a[href*='/p/']"):
                href = _tag_attr_text(anchor, "href") or ""
                sku = _extract_sku_from_href(href, preserve_lettered_code=True)
                if not sku:
                    continue
                found.setdefault(sku.upper(), urljoin("https://www.cutco.com", href))
        except Exception as exc:
            logger.debug("Product URL discovery failed for %s: %s", category_url, exc)
        return found

    lookup: dict[str, str] = {}
    category_urls = [url for _name, url in _build_category_list()]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(_fetch_category, category_urls):
            for sku, url in found.items():
                lookup.setdefault(sku, url)
                base_sku = re.sub(r"[A-Z]+$", "", sku)
                if base_sku and base_sku != sku:
                    lookup.setdefault(base_sku, url)
    logger.info("Indexed %d Cutco product URLs by SKU", len(lookup))
    return lookup


def discover_cutco_item_page_url(sku: str | None) -> str | None:
    """Find a Cutco product URL by exact or handle-neutral base SKU."""
    normalized_sku = (sku or "").strip().upper()
    if not normalized_sku:
        return None
    return _cutco_product_url_lookup().get(normalized_sku)


discover_cutco_item_page_url.cache_clear = _cutco_product_url_lookup.cache_clear  # type: ignore[attr-defined]


def _product_link_name(anchor: Tag | None) -> str | None:
    if anchor is None:
        return None
    name_el = anchor.find(["h2", "h3"])
    if name_el:
        title = name_el.get_text(" ", strip=True)
        if title:
            return title
    text = anchor.get_text(" ", strip=True)
    return text or None


def _dedupe_product_links(
    product_links: list[Tag],
) -> list[tuple[Tag, str, str | None]]:
    """Keep one /p/ anchor per base URL, preferring the anchor with a title."""
    unique_links: dict[str, tuple[Tag, str, str | None]] = {}
    for anchor in product_links:
        full_href = _tag_attr_text(anchor, "href") or ""
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
    structured_members: Sequence[Mapping[str, object]],
    visible_rows: Sequence[Mapping[str, object]],
    member_skus: list[str],
    member_quantities: dict[str, int],
) -> list[SetMemberEntry]:
    member_entries: list[SetMemberEntry] = []
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
            visible_sku = (
                _normalize_set_member_sku(matched_visible.get("sku"))
                if matched_visible
                else None
            )
            structured_sku = _normalize_set_member_sku(structured.get("sku"))
            fallback_sku = _normalize_set_member_sku(
                (
                    member_skus[matched_index]
                    if matched_index is not None and matched_index < len(member_skus)
                    else structured.get("sku")
                ),
            )
            chosen_sku = visible_sku or structured_sku or fallback_sku
            entry_name_value = (
                matched_visible.get("name")
                if matched_visible and matched_visible.get("name")
                else structured.get("name")
            )
            entry_quantity = _coerce_int(structured.get("quantity"))
            entry_is_set_only = (
                bool(matched_visible.get("is_set_only")) if matched_visible else False
            )
            member_entries.append(
                {
                    "sku": chosen_sku,
                    "name": str(entry_name_value) if entry_name_value else None,
                    "quantity": entry_quantity,
                    "is_set_only": entry_is_set_only,
                }
            )

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
            fallback_sku = (
                _normalize_set_member_sku(member_skus[visible_index])
                if visible_index < len(member_skus)
                else None
            )
            chosen_sku = visible_sku or fallback_sku
            if not chosen_sku:
                continue
            normalized_sku = _norm_member_name(chosen_sku)
            if normalized_sku in seen_skus:
                continue
            member_entries.append(
                {
                    "sku": chosen_sku,
                    "name": (
                        str(visible_row.get("name"))
                        if visible_row.get("name")
                        else None
                    ),
                    "quantity": member_quantities.get(chosen_sku, 1),
                    "is_set_only": bool(visible_row.get("is_set_only")),
                }
            )
    else:
        for idx, sku in enumerate(member_skus):
            member_entries.append(
                {
                    "sku": sku,
                    "name": None,
                    "quantity": member_quantities.get(sku, 1),
                    "is_set_only": False,
                }
            )
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
    if (
        len(words) > 4
        and not any(separator in title for separator in (",", ";", " / "))
        and not re.search(r"\s+[—–]\s+", title)
    ):
        title = " ".join(words[:2]).strip()
    return title or None


@lru_cache(maxsize=512)
def _infer_visible_member_sku(
    member_name: str | None, *, context_url: str | None = None
) -> str | None:
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
        inferred_sku, _ = _fetch_sku_from_page(
            f"https://www.cutco.com/p/{candidate_slug}", preserve_lettered_code=True
        )
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
        fetched_sku, _ = _fetch_sku_from_page(
            candidate_href, preserve_lettered_code=True
        )
        normalized_fetched_sku = _normalize_set_member_sku(fetched_sku)
        if normalized_fetched_sku and normalized_fetched_sku != set_sku_norm:
            return fetched_sku
    return None


def _collect_visible_set_piece_rows(
    pieces_list: Tag | None,
    *,
    context_url: str | None = None,
    set_sku: str | None = None,
) -> list[VisibleSetRow]:
    visible_rows: list[VisibleSetRow] = []
    if pieces_list is None:
        return visible_rows
    for anchor in pieces_list.select("a.pdp-set-item-detail"):
        visible_name = ""
        name_tag = anchor.select_one(".pdp-use-detail")
        if name_tag is not None:
            visible_name = name_tag.get_text(" ", strip=True)
        if not visible_name:
            image = anchor.find("img", alt=True)
            visible_name = _tag_attr_text(image, "alt") or ""
        if not visible_name:
            continue
        visible_sku = _normalize_set_member_sku(
            _tag_attr_text(anchor, "data-item-selected")
        )
        if not visible_sku:
            visible_sku = _normalize_set_member_sku(
                _extract_sku_from_image_src(
                    _tag_attr_text(anchor.find("img", src=True), "src")
                    if anchor.find("img", src=True)
                    else None
                )
            )
        if not visible_sku:
            href = _tag_attr_text(anchor, "href") or ""
            visible_sku = _resolve_visible_member_sku(
                [href] if "/p/" in href else None,
                visible_name,
                context_url=context_url,
                set_sku=set_sku,
            )
        visible_rows.append(
            {
                "name": visible_name,
                "sku": visible_sku,
                "is_set_only": not visible_sku,
            }
        )
    for li in pieces_list.select("li.pdp-piece-no-details"):
        visible_name = ""
        name_tag = li.select_one(".pdp-use-detail")
        if name_tag is not None:
            visible_name = name_tag.get_text(" ", strip=True)
        if not visible_name:
            image = li.find("img", alt=True)
            visible_name = _tag_attr_text(image, "alt") or ""
        if not visible_name:
            continue
        visible_sku = _normalize_set_member_sku(
            _extract_sku_from_image_src(
                _tag_attr_text(li.find("img", src=True), "src")
                if li.find("img", src=True)
                else None
            )
        )
        visible_rows.append(
            {
                "name": visible_name,
                "sku": visible_sku,
                "is_set_only": not visible_sku,
            }
        )
    return visible_rows


def _normalize_set_member_sku(raw_sku: object) -> str | None:
    sku = (
        str(raw_sku or "").upper().strip().split("/")[0] if raw_sku is not None else ""
    )
    if not sku:
        return None
    sku = re.sub(r"[\s\-]+$", "", sku)
    if re.fullmatch(r"\d{3,}-\d+", sku):
        return sku
    if re.fullmatch(r"\d{3}[A-Z]", sku):
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
def _fetch_sku_from_page(
    url: str, *, preserve_lettered_code: bool = False
) -> tuple[str | None, str | None]:
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
        url_slug_sku = _extract_sku_from_href(
            url, preserve_lettered_code=preserve_lettered_code
        )
        if url_slug_sku:
            sku = url_slug_sku
            strategy_log.append(f"slug={sku}")

        # Strategy 1: JSON-LD structured data
        if not sku:
            for ld in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(str(ld.string or ""))
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
                    raw_html,
                )
                if sku_match:
                    digits = re.match(r"^(\d{2,}(?:-\d+)?)", sku_match.group(1).strip())
                    if digits:
                        sku = digits.group(1)
                        break
            if sku:
                strategy_log.append(f"prPageId={sku}")

        # Strategy 3: generic inline JS
        if not sku:
            sku_match = re.search(
                r"""["']?sku["']?\s*:\s*["']?(\d{2,4}(?:-\d+|[A-Z]{0,2})?)["']?""",
                raw_html,
                re.IGNORECASE,
            )
            if sku_match:
                sku = sku_match.group(1).upper()
                strategy_log.append(f"inline-js={sku}")

        # Strategy 4: meta tags
        if not sku:
            for attr in ("product:retailer_item_id", "product:sku"):
                tag = soup.find("meta", property=attr) or soup.find(
                    "meta", attrs={"name": attr}
                )
                content = _tag_attr_text(tag, "content")
                if content:
                    sku = content.upper()
                    strategy_log.append(f"meta={sku}")
                    break

        for noise in soup.find_all(["script", "style"]):
            noise.decompose()

        # Strategy 5: on-page visible text containing "#XXXX"
        if not sku:
            sku_text = soup.find(string=re.compile(r"#\d{2,4}(?:-\d+|[A-Z]{0,2})?\b"))
            if isinstance(sku_text, str):
                sku_match = re.search(
                    r"#(\d{2,4}(?:-\d+|[A-Z]{0,2})?)\b", sku_text.strip(), re.IGNORECASE
                )
                if sku_match:
                    sku = sku_match.group(1).upper()
                    strategy_log.append(f"visible-text={sku}")

        # Strategy 6: keyword context
        if not sku:
            page_text = soup.get_text(" ", strip=True)
            sku_match = re.search(
                r"(?:model|item|sku|product)\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{2,4}(?:-\d+|[A-Z]{0,2})?)\b",
                page_text,
                re.IGNORECASE,
            )
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
        if (
            sku
            and re.fullmatch(r"[0-9A-F]{6}", sku, re.IGNORECASE)
            and not sku[0].isdigit()
        ):
            sku = None

        page_heading = soup.find("h1")
        name = page_heading.get_text(strip=True) if page_heading else None
        logger.info(
            "SKU fetch: %s → sku=%s [%s] name=%s",
            url,
            sku,
            ", ".join(strategy_log) or "none",
            name,
        )
        return sku, name
    except Exception as exc:
        logger.warning("SKU fetch failed: %s — %s", url, exc)
        return None, None


def scrape_item_uses(url: str) -> list[str]:
    """Fetch a product page and return uses from the 'Uses+' accordion section."""
    clean_url = url.split("&view=")[0].split("?view=")[0]
    try:
        resolved_url, raw_html = _fetch_cutco_page(clean_url)
        if not raw_html:
            logger.debug("Uses fetch failed for %s", clean_url)
            return []
        soup = BeautifulSoup(raw_html, "html.parser")

        # Find the heading that starts with "Uses" (e.g. "Uses+")
        uses_heading: Tag | None = None
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
            uses_ul = (
                parent.find_next_sibling("ul") if isinstance(parent, Tag) else None
            )
        if not uses_ul:
            uses_ul = uses_heading.find_next("ul")
        if not isinstance(uses_ul, Tag):
            return []

        uses = [
            li.get_text(strip=True)
            for li in uses_ul.find_all("li")
            if li.get_text(strip=True)
        ]
        logger.debug("Uses fetch: %s → %d uses", resolved_url or clean_url, len(uses))
        return uses
    except Exception as exc:
        logger.warning("Uses scrape failed for %s: %s", url, exc)
        return []


_EDGE_NORMALIZE = {
    "double-d": "Double-D",
    "double-d®": "Double-D",
    "micro double-d™": "Micro Double-D",
    "micro double-d": "Micro Double-D",
    "straight": "Straight",
    "serrated": "Serrated",
    "micro-d": "Micro-D",
    "micro-d®": "Micro-D",
    "tec edge": "Tec Edge",
    "tec-edge": "Tec Edge",
}


def scrape_item_specs(url: str) -> dict[str, str | float | None]:
    """Fetch a product page and return edge, price, and key measurements.

    edge_type: 'N/A' = no blade edge, 'Unknown' = fetch failure or ambiguous.
    All other keys are None when not found.
    """
    clean_url = url.split("&view=")[0].split("?view=")[0]
    result = {
        "edge_type": "Unknown",
        "msrp": None,
        "blade_length": None,
        "overall_length": None,
        "weight": None,
    }
    try:
        resolved_url, raw_html = _fetch_cutco_page(clean_url)
        if not raw_html:
            return result
        # ── Edge type ────────────────────────────────────────────────────────
        item_class_match = re.search(r'"itemClass"\s*:\s*"([^"]+)"', raw_html)
        item_subclass_match = re.search(r'"itemSubclass"\s*:\s*"([^"]+)"', raw_html)
        if (
            item_class_match
            and item_class_match.group(1) == "FLA"
            and item_subclass_match
            and item_subclass_match.group(1) == "STL"
        ):
            result["edge_type"] = "N/A"
        else:
            edge_match = re.search(
                r'"specName"\s*:\s*"Edge"\s*,\s*"specValue"\s*:\s*"([^"]+)"', raw_html
            )
            if not edge_match:
                edge_match = re.search(
                    r'"specValue"\s*:\s*"([^"]+)"\s*,\s*"specName"\s*:\s*"Edge"',
                    raw_html,
                )
            if edge_match:
                result["edge_type"] = _EDGE_NORMALIZE.get(
                    edge_match.group(1).strip().lower(), "Unknown"
                )
            else:
                result["edge_type"] = "N/A"

        # ── itemSpecs (blade length, overall length, weight) ─────────────────
        spec_map = {
            "length - blade": "blade_length",
            "length - overall": "overall_length",
            "weight - knife only": "weight",
            "weight": "weight",
        }
        for spec_match in re.finditer(
            r'"specName"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"specValue"\s*:\s*"((?:\\.|[^"\\])*)"',
            raw_html,
        ):
            try:
                spec_name = json.loads(f'"{spec_match.group(1)}"').strip().lower()
                spec_value = json.loads(f'"{spec_match.group(2)}"').strip()
            except json.JSONDecodeError:
                spec_name = spec_match.group(1).strip().lower()
                spec_value = spec_match.group(2).strip()
            field = spec_map.get(spec_name)
            if field and result[field] is None:
                result[field] = spec_value

        # ── MSRP ─────────────────────────────────────────────────────────────
        result["msrp"] = _extract_cutco_price(
            raw_html, page_url=resolved_url or clean_url
        )

        logger.debug("Specs: %s → %s", resolved_url or clean_url, result)
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
    "choose a finish",
    "choose color",
    "select a finish",
    "add gift wrap",
    "add personalization",
}

_VARIANT_SWATCH_CLASS_HINTS = {
    "color",
    "color-swatch",
    "finish",
    "finish-swatch",
    "block-finish",
    "block-finish-swatch",
    "handle-color",
    "handle-color-swatch",
    "handle-finish",
    "handle-finish-swatch",
}

_VARIANT_SWATCH_FALLBACK_BLOCKLIST = {
    "add",
    "american",
    "board",
    "box",
    "chat",
    "choose",
    "customer",
    "design",
    "gift",
    "guarantee",
    "home",
    "image",
    "knife",
    "live",
    "made",
    "monogram",
    "page",
    "personalization",
    "plate",
    "press",
    "product",
    "service",
    "set",
    "sheath",
    "shop",
    "style",
    "text",
}

_VARIANT_SELECT_HINTS = (
    "color",
    "finish",
    "style",
    "handle color",
    "handle finish",
)

_VARIANT_COLOR_WORDS = {
    "amber",
    "beige",
    "black",
    "blue",
    "bronze",
    "brown",
    "brass",
    "burgundy",
    "champagne",
    "charcoal",
    "cherry",
    "chrome",
    "classic",
    "clear",
    "copper",
    "coral",
    "cream",
    "dark",
    "gold",
    "gray",
    "grey",
    "green",
    "honey",
    "ivory",
    "light",
    "mahogany",
    "maple",
    "natural",
    "navy",
    "nickel",
    "onyx",
    "oak",
    "pearl",
    "pewter",
    "pink",
    "platinum",
    "purple",
    "red",
    "rose",
    "rosewood",
    "rust",
    "sand",
    "satin",
    "silver",
    "smoke",
    "stainless",
    "steel",
    "stone",
    "tan",
    "teal",
    "white",
    "walnut",
    "yellow",
}

_PAGE_COLOR_RE = re.compile(
    r"\bColor:\s*([A-Za-z][A-Za-z0-9'’&()./\-\s]{0,60})", re.IGNORECASE
)


def _collect_variant_candidate(
    candidates: list[str], seen: set[str], value: str | None
) -> None:
    candidate = _normalize_variant_label(value or "")
    if not candidate:
        return
    key = candidate.lower()
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate)


def _looks_like_variant_color(label: str | None) -> bool:
    candidate = re.sub(r"\s+", " ", (label or "").strip()).strip(" \t\r\n:-|")
    if not candidate:
        return False
    if any(char.isdigit() for char in candidate):
        return False
    if len(candidate) > 32:
        return False
    lowered = candidate.lower()
    if lowered in _VARIANT_SKIP_LABELS:
        return False
    if lowered.startswith(("select ", "choose ", "add ", "image ")):
        return False
    words = re.findall(r"[A-Za-z]+", candidate)
    if not words or len(words) > 3:
        return False
    word_set = {word.lower() for word in words}
    if word_set & _VARIANT_SWATCH_FALLBACK_BLOCKLIST:
        return False
    return all(word in _VARIANT_COLOR_WORDS for word in word_set)


def _collect_variant_candidates_from_swatches(
    soup: BeautifulSoup, *, option_kind: str | None = None
) -> tuple[str, ...]:
    """Extract color-like choices from product swatch groups."""
    candidates: list[str] = []
    seen: set[str] = set()
    for fieldset in soup.select("fieldset.swatch-group"):
        group_text = " ".join(
            [
                " ".join(_tag_classes(fieldset)),
                _tag_attr_text(fieldset, "data-type") or "",
                fieldset.get_text(" ", strip=True),
            ]
        ).lower()
        if not any(keyword in group_text for keyword in ("color", "finish")):
            continue
        group_kind = (
            "block_finish"
            if "block finish" in group_text or "block color" in group_text
            else "handle"
        )
        if option_kind and option_kind != group_kind:
            continue
        preferred_swatch_nodes: list[Tag] = []
        generic_swatch_nodes: list[Tag] = []
        for swatch in fieldset.select(".swatch.product-option"):
            swatch_classes = _tag_classes(swatch)
            if swatch_classes & {
                "engraving-swatch",
                "design-button",
                "location-button",
                "font-swatch",
            }:
                continue
            if swatch_classes & _VARIANT_SWATCH_CLASS_HINTS:
                preferred_swatch_nodes.append(swatch)
            else:
                generic_swatch_nodes.append(swatch)

        swatch_nodes = preferred_swatch_nodes or generic_swatch_nodes
        for swatch in swatch_nodes:
            swatch_sources = (
                _tag_attr_text(swatch, "data-option"),
                _tag_attr_text(swatch, "data-value"),
                _tag_attr_text(swatch, "aria-label"),
                _tag_attr_text(swatch, "title"),
                _tag_attr_text(swatch, "data-code"),
            )
            added = False
            for source in swatch_sources:
                if not source:
                    continue
                normalized = _normalize_variant_label(source)
                if not normalized:
                    continue
                if swatch in generic_swatch_nodes and not _looks_like_variant_color(
                    normalized
                ):
                    continue
                _collect_variant_candidate(candidates, seen, source)
                added = True
                break
            if added:
                continue
            reader_only = swatch.select_one(".reader-only")
            if reader_only:
                reader_text = reader_only.get_text(" ", strip=True)
                if swatch in generic_swatch_nodes and not _looks_like_variant_color(
                    reader_text
                ):
                    continue
                _collect_variant_candidate(candidates, seen, reader_text)
            else:
                swatch_text = swatch.get_text(" ", strip=True)
                if swatch in generic_swatch_nodes and not _looks_like_variant_color(
                    swatch_text
                ):
                    continue
                _collect_variant_candidate(candidates, seen, swatch_text)
    return tuple(candidates)


def _collect_variant_candidates_from_selects(
    soup: BeautifulSoup, *, option_kind: str | None = None
) -> tuple[str, ...]:
    """Extract color-like choices from select dropdowns."""
    candidates: list[str] = []
    seen: set[str] = set()
    for select in soup.select("select"):
        select_text = " ".join(
            [
                " ".join(_tag_classes(select)),
                _tag_attr_text(select, "data-type") or "",
                _tag_attr_text(select, "name") or "",
                _tag_attr_text(select, "id") or "",
                _tag_attr_text(select, "aria-label") or "",
                _tag_attr_text(select, "title") or "",
                select.get_text(" ", strip=True),
            ]
        ).lower()
        if not any(hint in select_text for hint in _VARIANT_SELECT_HINTS):
            continue
        select_kind = (
            "block_finish"
            if "block finish" in select_text or "block color" in select_text
            else "handle"
        )
        if option_kind and option_kind != select_kind:
            continue

        for option in select.find_all("option"):
            option_text = option.get_text(" ", strip=True)
            option_value = _tag_attr_text(option, "value") or ""
            normalized = _normalize_variant_label(option_text or option_value)
            if not normalized:
                continue
            if normalized.lower() == (option_value or "").strip().lower():
                if not _looks_like_variant_color(normalized):
                    continue
            _collect_variant_candidate(candidates, seen, normalized)
    return tuple(candidates)


def _extract_selected_page_color(soup: BeautifulSoup) -> str | None:
    """Return the currently selected color label when the page exposes one."""
    page_text = soup.get_text(" ", strip=True)
    match = _PAGE_COLOR_RE.search(page_text)
    if not match:
        return None
    label = re.split(r"\b(?:Select|Image)\b", match.group(1), maxsplit=1)[0].strip()
    return _normalize_variant_label(label)


def _page_product_supports_block_finish(soup: BeautifulSoup) -> bool:
    """Return whether the selected product identity includes block-style storage."""
    product_titles: list[str] = []
    metadata_titles: list[str] = []
    for meta in soup.select('meta[property="og:title"], meta[name="twitter:title"]'):
        title = _tag_attr_text(meta, "content")
        if title:
            metadata_titles.append(title)
    for heading in soup.select("h1"):
        title = heading.get_text(" ", strip=True)
        if title and not re.fullmatch(r"#?\s*[A-Za-z0-9-]+", title):
            product_titles.append(title)

    if not product_titles:
        product_titles = metadata_titles
    if not product_titles:
        # Some metadata-only responses do not expose a product heading. Preserve
        # their structured block options rather than guessing from no identity.
        return True
    product_identity = " ".join(product_titles).lower()
    return any(word in product_identity for word in ("block", "holder"))


def _web_items_map_supports_block_finish(
    raw_html: str, target_sku: str | None
) -> bool | None:
    """Return block-storage applicability from the exact SKU's structured options."""
    normalized_target = re.sub(r"[^A-Z0-9]", "", (target_sku or "").upper())
    if not normalized_target:
        return None

    decisions: list[bool] = []
    search_from = 0
    while True:
        marker = re.search(r"\bwebItemsMap\b", raw_html[search_from:])
        if not marker:
            break
        marker_index = search_from + marker.start()
        brace_index = raw_html.find("{", marker_index)
        if brace_index < 0:
            break
        block = _extract_balanced_braces(raw_html, brace_index)
        search_from = brace_index + 1
        if not block:
            continue
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, product in payload.items():
            normalized_key = re.sub(r"[^A-Z0-9]", "", str(key).upper())
            if not re.fullmatch(
                re.escape(normalized_target) + r"[A-Z]*", normalized_key
            ):
                continue
            if not isinstance(product, dict):
                continue
            identity = " ".join(
                str(product.get(field) or "") for field in ("itemName", "itemHeadline")
            ).lower()
            if "block" in identity or "holder" in identity:
                decisions.append(True)
            elif "tools only" in identity or "knives only" in identity:
                decisions.append(False)
            for option in product.get("itemOptions", []):
                if not isinstance(option, dict):
                    continue
                option_type = " ".join(
                    str(option.get(field) or "")
                    for field in ("optionType", "displayedType")
                ).lower()
                if "storage" not in option_type:
                    continue
                storage_value = " ".join(
                    str(option.get(field) or "")
                    for field in ("description", "optionCode")
                ).lower()
                if "block" in storage_value or "holder" in storage_value:
                    decisions.append(True)
                elif "only" in storage_value:
                    decisions.append(False)

    if not decisions:
        return None
    return any(decisions)


def _collect_campaign_variant_candidates(soup: BeautifulSoup) -> tuple[str, ...]:
    """Extract promo-page variant labels when a campaign page exposes them."""
    candidates: list[str] = []
    seen: set[str] = set()

    purple_inputs = soup.select('input[data-type*="Purple Products"]')
    if purple_inputs:
        _collect_variant_candidate(candidates, seen, "Purple")

    return tuple(candidates)


_WEB_ITEMS_MAP_LABEL_FIELDS = (
    "name",
    "itemName",
    "variantName",
    "webItemName",
    "displayName",
    "productName",
    "setName",
    "label",
    "optionName",
    "color",
    "finish",
    "style",
)


def _extract_balanced_braces(text: str, start_index: int) -> str | None:
    """Return the balanced brace block starting at ``start_index`` if present."""
    if start_index < 0 or start_index >= len(text) or text[start_index] != "{":
        return None

    depth = 0
    in_string = False
    quote_char = ""
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote_char:
                in_string = False
        else:
            if char in {'"', "'"}:
                in_string = True
                quote_char = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start_index : index + 1]
    return None


def _collect_variant_candidates_from_web_items_map(
    raw_html: str,
    *,
    target_sku: str | None = None,
    option_kind: str | None = None,
) -> tuple[str, ...]:
    """Extract variant labels from Cutco's ``webItemsMap`` product metadata."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _walk_payload(value: object) -> None:
        if isinstance(value, dict):
            option_context = " ".join(
                str(value.get(field) or "") for field in ("displayedType", "optionType")
            ).lower()
            context_kind = (
                "block_finish"
                if "block finish" in option_context or "block color" in option_context
                else "handle"
            )
            if any(hint in option_context for hint in _VARIANT_SELECT_HINTS) and (
                not option_kind or option_kind == context_kind
            ):
                for field in ("description", "optionCode", "label", "name"):
                    option_label = value.get(field)
                    if isinstance(option_label, str) and _looks_like_variant_color(
                        option_label
                    ):
                        _collect_variant_candidate(candidates, seen, option_label)
                        break
            for key, nested_value in value.items():
                if (
                    isinstance(nested_value, str)
                    and key in _WEB_ITEMS_MAP_LABEL_FIELDS
                    and _looks_like_variant_color(nested_value)
                    and option_kind != "block_finish"
                ):
                    _collect_variant_candidate(candidates, seen, nested_value)
                else:
                    _walk_payload(nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                _walk_payload(nested_value)

    search_from = 0
    while True:
        marker = re.search(r"\bwebItemsMap\b", raw_html[search_from:])
        if not marker:
            break
        marker_index = search_from + marker.start()
        brace_index = raw_html.find("{", marker_index)
        if brace_index < 0:
            break
        block = _extract_balanced_braces(raw_html, brace_index)
        search_from = brace_index + 1
        if not block:
            continue
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        payloads: list[object] = [payload]
        normalized_target = re.sub(r"[^A-Z0-9]", "", (target_sku or "").upper())
        if normalized_target:
            matching_payloads = [
                value
                for key, value in payload.items()
                if re.fullmatch(
                    re.escape(normalized_target) + r"[A-Z]*",
                    re.sub(r"[^A-Z0-9]", "", str(key).upper()),
                )
            ]
            payloads = matching_payloads
        for selected_payload in payloads:
            _walk_payload(selected_payload)

    return tuple(candidates)


def _collect_handle_color_member_skus_from_web_items_map(
    raw_html: str, target_sku: str | None
) -> dict[str, tuple[str, ...]]:
    """Map set handle colors to child SKUs that actually change by color."""
    normalized_target = re.sub(r"[^A-Z0-9]", "", (target_sku or "").upper())
    if not normalized_target:
        return {}

    configurations: list[tuple[str, dict[str, str]]] = []
    search_from = 0
    while True:
        marker = re.search(r"\bwebItemsMap\b", raw_html[search_from:])
        if not marker:
            break
        marker_index = search_from + marker.start()
        brace_index = raw_html.find("{", marker_index)
        if brace_index < 0:
            break
        block = _extract_balanced_braces(raw_html, brace_index)
        search_from = brace_index + 1
        if not block:
            continue
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, product in payload.items():
            normalized_key = re.sub(r"[^A-Z0-9]", "", str(key).upper())
            if not re.fullmatch(
                re.escape(normalized_target) + r"[A-Z]*", normalized_key
            ) or not isinstance(product, dict):
                continue
            color = None
            for option in product.get("itemOptions", []):
                if not isinstance(option, dict):
                    continue
                option_context = " ".join(
                    str(option.get(field) or "")
                    for field in ("displayedType", "optionType")
                ).lower()
                if "color" not in option_context:
                    continue
                candidate = _normalize_variant_label(
                    str(option.get("description") or option.get("optionCode") or "")
                )
                if candidate and _looks_like_variant_color(candidate):
                    color = candidate
                    break
            if not color:
                continue
            members: dict[str, str] = {}
            for member in product.get("itemSetList", []):
                if not isinstance(member, dict):
                    continue
                child_sku = re.sub(
                    r"[^A-Z0-9]",
                    "",
                    str(member.get("childItemNumber") or "").upper(),
                )
                member_name = _normalize_text_for_match(
                    str(member.get("itemName") or "")
                )
                if child_sku and member_name:
                    members[member_name] = child_sku
            configurations.append((color, members))

    colors = {color.lower() for color, _members in configurations}
    if len(colors) < 2:
        return {}
    member_skus: dict[str, set[str]] = {}
    member_names = {
        member_name for _color, members in configurations for member_name in members
    }
    for member_name in member_names:
        exact_skus = {
            members[member_name]
            for _color, members in configurations
            if member_name in members
        }
        if len(exact_skus) < 2:
            continue
        for color, members in configurations:
            child_sku = members.get(member_name)
            if child_sku:
                member_skus.setdefault(color, set()).add(child_sku)
    return {
        color: tuple(sorted(skus))
        for color, skus in sorted(
            member_skus.items(), key=lambda entry: entry[0].lower()
        )
    }


def set_handle_color_applies_to_member(
    member_sku: str | None,
    color: str,
    color_member_skus: Mapping[str, Sequence[str]] | None,
) -> bool:
    """Return whether an exact set configuration applies a color to one member."""
    if not color_member_skus:
        return True
    allowed_skus = next(
        (
            skus
            for mapped_color, skus in color_member_skus.items()
            if mapped_color.lower() == color.lower()
        ),
        (),
    )
    normalized_member = re.sub(r"[^A-Z0-9]", "", (member_sku or "").upper())
    if not normalized_member or not allowed_skus:
        return False
    return any(
        re.fullmatch(
            re.escape(normalized_member) + r"[A-Z]*",
            re.sub(r"[^A-Z0-9]", "", str(allowed_sku).upper()),
        )
        for allowed_sku in allowed_skus
    )


def _page_has_size_selector(soup: BeautifulSoup) -> bool:
    """Return True if the page exposes a size swatch group."""
    for fieldset in soup.select("fieldset.swatch-group"):
        group_text = " ".join(
            [
                " ".join(_tag_classes(fieldset)),
                _tag_attr_text(fieldset, "data-type") or "",
                fieldset.get_text(" ", strip=True),
            ]
        ).lower()
        if "size" in group_text:
            return True
    return False


def _normalize_variant_label(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", (value or "").strip()).strip(" \t\r\n:-|")
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in _VARIANT_SKIP_LABELS:
        return None
    if lowered.startswith("select "):
        return None
    if lowered.startswith("choose "):
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
    try:
        candidate_urls = [url]
        clean_url = url.split("&view=")[0].split("?view=")[0]
        if clean_url != url:
            candidate_urls.append(clean_url)

        for fetch_url in candidate_urls:
            resp = requests.get(
                fetch_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code != 200:
                logger.debug(
                    "Variant fetch: HTTP %d for %s", resp.status_code, fetch_url
                )
                continue
            raw_html = resp.text
            soup = BeautifulSoup(raw_html, "html.parser")
            for noise in soup.find_all(["script", "style"]):
                noise.decompose()

            candidates: list[str] = []
            seen: set[str] = set()
            campaign_candidates = _collect_campaign_variant_candidates(soup)
            for candidate in campaign_candidates:
                key = candidate.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
            web_items_candidates = _collect_variant_candidates_from_web_items_map(
                raw_html
            )
            for candidate in web_items_candidates:
                key = candidate.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
            swatch_candidates = _collect_variant_candidates_from_swatches(soup)
            select_candidates = _collect_variant_candidates_from_selects(soup)
            selected_color = _extract_selected_page_color(soup)
            if selected_color and _page_has_size_selector(soup):
                if selected_color in swatch_candidates:
                    swatch_candidates = (selected_color,)
            if not swatch_candidates and selected_color:
                swatch_candidates = (selected_color,)
            for candidate in swatch_candidates:
                key = candidate.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
            for candidate in select_candidates:
                key = candidate.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)

            logger.debug(
                "Variant fetch: %s → %d candidates", fetch_url, len(candidates)
            )
            return tuple(candidates)
        return ()
    except Exception as exc:
        logger.warning("Variant scrape failed for %s: %s", url, exc)
        return ()


def scrape_item_variant_colors(url: str) -> tuple[str, ...]:
    """Public alias for the product-page variant color scraper."""
    return _extract_product_variant_colors(url)


@lru_cache(maxsize=512)
def scrape_set_variant_options(url: str, sku: str | None = None) -> SetVariantOptions:
    """Return set-wide handle colors and block finishes for one exact set SKU."""
    empty: SetVariantOptions = {"handle_colors": (), "block_finishes": ()}
    try:
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return empty
        raw_html = response.text
        soup = BeautifulSoup(raw_html, "html.parser")
        visible_soup = BeautifulSoup(raw_html, "html.parser")
        for noise in visible_soup.find_all(["script", "style"]):
            noise.decompose()

        def collect(kind: str) -> tuple[str, ...]:
            candidates: list[str] = []
            seen: set[str] = set()
            web_items_candidates = _collect_variant_candidates_from_web_items_map(
                raw_html, target_sku=sku, option_kind=kind
            )
            sources = (
                web_items_candidates,
                _collect_variant_candidates_from_swatches(soup, option_kind=kind),
                _collect_variant_candidates_from_selects(soup, option_kind=kind),
            )
            if kind == "handle" and web_items_candidates:
                sources = (web_items_candidates,)
            for source in sources:
                for candidate in source:
                    if not _looks_like_variant_color(candidate):
                        continue
                    _collect_variant_candidate(candidates, seen, candidate)
            if kind == "handle":
                selected_color = _extract_selected_page_color(visible_soup)
                if selected_color and _looks_like_variant_color(selected_color):
                    _collect_variant_candidate(candidates, seen, selected_color)
            return tuple(candidates)

        structured_handle_colors = _collect_variant_candidates_from_web_items_map(
            raw_html, target_sku=sku, option_kind="handle"
        )
        block_finishes = collect("block_finish")
        supports_block_finish = _web_items_map_supports_block_finish(raw_html, sku)
        if supports_block_finish is None:
            supports_block_finish = _page_product_supports_block_finish(visible_soup)
        if block_finishes and not supports_block_finish:
            block_finishes = ()

        result: SetVariantOptions = {
            "handle_colors": collect("handle"),
            "block_finishes": block_finishes,
        }
        if structured_handle_colors:
            result["handle_colors_authoritative"] = True
        handle_color_member_skus = _collect_handle_color_member_skus_from_web_items_map(
            raw_html, sku
        )
        if handle_color_member_skus:
            result["handle_color_member_skus"] = handle_color_member_skus
        return result
    except Exception as exc:
        logger.warning("Set variant scrape failed for %s: %s", url, exc)
        return empty


# Preserve cache helpers on the public alias used by callers.
scrape_item_variant_colors.cache_clear = _extract_product_variant_colors.cache_clear  # type: ignore[attr-defined]
scrape_item_variant_colors.cache_info = _extract_product_variant_colors.cache_info  # type: ignore[attr-defined]


@lru_cache(maxsize=64)
def scrape_purple_campaign_variants() -> tuple[dict[str, str], ...]:
    """Fetch the Cutco Cares purple campaign page and return promo variant hints."""
    campaign_url = "https://www.cutco.com/p/cutco-cares-alzheimers/"
    try:
        resp = requests.get(
            campaign_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            logger.debug(
                "Purple campaign fetch: HTTP %d for %s", resp.status_code, campaign_url
            )
            return ()
        soup = BeautifulSoup(resp.text, "html.parser")
        candidates: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for promo_input in soup.select('input[data-type*="Purple Products"]'):
            promo_name = _normalize_variant_label(
                _tag_attr_text(promo_input, "value") or ""
            )
            promo_code = (_tag_attr_text(promo_input, "data-code") or "").upper()
            if not promo_name or not promo_code:
                continue
            key = (promo_name.lower(), promo_code)
            if key in seen:
                continue
            seen.add(key)
            match = re.match(r"^(\d+)", promo_code)
            sku_hint = match.group(1) if match else promo_code
            candidates.append(
                {
                    "name": promo_name,
                    "promo_code": promo_code,
                    "sku_hint": sku_hint,
                    "color": "Purple",
                }
            )

        # The promo campaign also includes two sheathed purple knife offers that
        # are easy to miss if the campaign page only exposes the generic purple
        # entry. Keep them explicit so the promo sync can surface them too.
        for fallback_name, fallback_code, fallback_sku_hint in (
            ('Purple 7" Santoku with Sheath', "1766LSH", "1766"),
            ("Purple Santoku-Style Trimmer with Sheath", "3721LSH", "3721"),
        ):
            key = (fallback_name.lower(), fallback_code)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "name": fallback_name,
                    "promo_code": fallback_code,
                    "sku_hint": fallback_sku_hint,
                    "color": "Purple",
                }
            )
        logger.debug(
            "Purple campaign fetch: %s → %d candidates", campaign_url, len(candidates)
        )
        return tuple(candidates)
    except Exception as exc:
        logger.warning("Purple campaign scrape failed for %s: %s", campaign_url, exc)
        return ()


# Keep old name as alias so existing callers still work
def scrape_edge_type(url: str) -> str:
    """Return the scraped edge type for a product page."""
    edge_type = scrape_item_specs(url)["edge_type"]
    return edge_type if isinstance(edge_type, str) else "Unknown"


def scrape_catalog(
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
    """Scrape all item categories and return items plus set candidates."""

    def _progress(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)

    results: list[dict[str, object]] = []
    set_candidates: list[tuple[str, str]] = []
    seen_skus: set[str] = set()
    seen_set_urls: set[str] = set()
    slug_queue: list[tuple[str, str, str | None]] = []
    seen_slug_urls: set[str] = set()

    categories = _build_category_list()
    logger.info("Scraping %d categories", len(categories))
    for cat_name, cat_url in categories:
        if cat_name in SYNC_BLOCKED_CATEGORIES:
            continue
        _progress(f"Scraping category: {cat_name}…")
        try:
            resp = requests.get(
                cat_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            product_links: list[Tag] = []
            for element in soup.descendants:
                if not isinstance(element, Tag):
                    continue
                if element.name in ("h2", "h3", "h4") and product_links:
                    if any(
                        kw in element.get_text(strip=True).lower()
                        for kw in _BUNDLE_KEYWORDS
                    ):
                        logger.debug(
                            "Bundle section detected on %s: '%s'",
                            cat_url,
                            element.get_text(strip=True),
                        )
                        break
                if element.name == "a" and "/p/" in (
                    _tag_attr_text(element, "href") or ""
                ):
                    product_links.append(element)

            logger.debug(
                "%s: found %d /p/ links — %s",
                cat_name,
                len(product_links),
                [_tag_attr_text(anchor, "href") or "" for anchor in product_links[:10]],
            )

            for anchor, href, name_hint in _dedupe_product_links(product_links):
                base_href = href.split("&")[0]
                preserve_lettered_code = (
                    cat_name in COOKWARE_CATEGORIES or cat_name == "Sheaths"
                )
                sku = _extract_sku_from_href(
                    base_href, preserve_lettered_code=preserve_lettered_code
                )
                prod_url = (
                    href if href.startswith("http") else f"https://www.cutco.com{href}"
                )
                prod_url = _resolve_cutco_product_url(prod_url)

                name = name_hint

                if cat_name == "Sheaths" or (
                    name
                    and "sheath" in name.lower()
                    and "with sheath" not in name.lower()
                ):
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
                results.append(
                    dict(
                        name=name,
                        sku=sku,
                        category=_resolve_category(sku, cat_name, name),
                        url=prod_url,
                    )
                )
            time.sleep(0.4)
            _progress(f"Finished category: {cat_name} ({len(results)} items so far)")
        except Exception as exc:
            logger.warning("Scrape failed for %s: %s", cat_url, exc)
            _progress(f"Category failed: {cat_name}")

    if slug_queue:
        logger.info("Fetching %d pure-slug product pages (parallel)", len(slug_queue))
        _progress(f"Fetching {len(slug_queue)} pure-slug product pages…")
        added_from_slugs = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {
                pool.submit(
                    _fetch_sku_from_page,
                    prod_url,
                    preserve_lettered_code=cat_name in COOKWARE_CATEGORIES
                    or cat_name == "Sheaths",
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
                results.append(
                    dict(
                        name=name,
                        sku=sku,
                        category=_resolve_category(sku, cat_name, name),
                        url=prod_url,
                    )
                )
                added_from_slugs += 1
        logger.info(
            "Slug queue: %d pages fetched, %d items added",
            len(slug_queue),
            added_from_slugs,
        )
        _progress(f"Finished slug pages: {added_from_slugs} items added")

    return results, set_candidates


def scrape_sets(
    extra_candidates: list[tuple[str, str]] | None = None,
) -> list[dict[str, object]]:
    """Scrape the knife-sets listing page and each set detail page."""
    results: list[dict[str, object]] = []
    seen_slugs: set[str] = set()
    set_links: list[dict[str, str]] = []

    try:
        resp = requests.get(
            SCRAPE_SETS_URL, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for anchor in soup.select("a[href*='/p/']"):
            href = _tag_attr_text(anchor, "href") or ""
            if not href:
                continue
            name_el = anchor.find(["h2", "h3"])
            parent = anchor.parent if isinstance(anchor.parent, Tag) else None
            if not name_el and parent is not None:
                name_el = parent.find(["h2", "h3"])
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

    for name, url in extra_candidates or []:
        slug = url.rstrip("/").split("/")[-1].split("&")[0]
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            set_links.append(dict(name=name, slug=slug, url=url))

    sku_pattern = re.compile(r"/rolo/([0-9]+[A-Z]?)-h\.", re.IGNORECASE)

    def _fetch_set_detail(set_link: dict[str, str]) -> dict[str, object] | None:
        fetch_url = set_link["url"]
        if "view=product" not in fetch_url:
            sep = "&" if "?" in fetch_url else "?"
            fetch_url += sep + "view=product"
        try:
            set_sku, _ = _fetch_sku_from_page(fetch_url)
            resp = requests.get(
                fetch_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            raw_html = resp.text
            detail = BeautifulSoup(raw_html, "html.parser")

            member_skus: list[str] = []
            member_names: list[str] = []
            member_quantities: dict[str, int] = {}
            member_is_set_only: list[bool] = []
            seen_member: set[str] = set()
            structured_members: list[StructuredSetMember] = []

            # Strategy 1: parse itemSetList JSON embedded in page JS
            # Page has webItemsMap with one itemSetList per variant — search
            # from the URL's variant key so we get the right size set.
            _set_list_json = None
            _url_variant = fetch_url.split("/")[-1].split("?")[0].split("&")[0].upper()
            _search_from = 0
            if re.match(r"^\d+[A-Z]?$", _url_variant):
                # Find the webItemsMap entry specifically (key followed by {)
                _var_pos = raw_html.find(f'"{_url_variant}"' + ":{")
                if _var_pos >= 0:
                    _search_from = _var_pos
            _key_match = re.search(r'"itemSetList"\s*:\s*\[', raw_html[_search_from:])
            if _key_match:
                _start = _search_from + _key_match.end() - 1  # abs pos of opening [
                _depth, _end = 0, _start
                for _i, _ch in enumerate(raw_html[_start:], _start):
                    if _ch == "[":
                        _depth += 1
                    elif _ch == "]":
                        _depth -= 1
                        if _depth == 0:
                            _end = _i + 1
                            break
                _set_list_json = raw_html[_start:_end]

            if _set_list_json:
                try:
                    set_list: list[dict[str, object]] = json.loads(_set_list_json)
                    for entry in set_list:
                        base_sku = _normalize_set_member_sku(
                            entry.get("childItemNumber")
                        )
                        if not base_sku:
                            continue
                        qty = _coerce_int(entry.get("qty"))
                        if base_sku not in seen_member:
                            seen_member.add(base_sku)
                            member_skus.append(base_sku)
                            member_names.append(
                                str(
                                    entry.get("name") or entry.get("itemName") or ""
                                ).strip()
                            )
                            member_quantities[base_sku] = qty
                            member_is_set_only.append(False)
                            structured_members.append(
                                {
                                    "sku": base_sku,
                                    "name": str(
                                        entry.get("name") or entry.get("itemName") or ""
                                    ).strip(),
                                    "quantity": qty,
                                }
                            )
                    logger.debug(
                        "Set '%s': itemSetList → %d members",
                        set_link["name"],
                        len(member_skus),
                    )
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
                    match = sku_pattern.search(_tag_attr_text(img, "src") or "")
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
                            structured_members.append(
                                {
                                    "sku": base_sku,
                                    "name": "",
                                    "quantity": 1,
                                }
                            )
                logger.debug(
                    "Set '%s': image fallback → %d members",
                    set_link["name"],
                    len(member_skus),
                )

            # Strategy 3: visible Set Pieces labels, if present
            visible_rows: list[VisibleSetRow] = []
            heading: Tag | None = None
            for tag in detail.find_all(["h2", "h3", "h4"]):
                if tag.get_text(strip=True).lower().startswith("set pieces"):
                    heading = tag
                    break
            if heading:
                pieces_list = heading.find_next("ul")
                if isinstance(pieces_list, Tag):
                    visible_rows.extend(
                        _collect_visible_set_piece_rows(
                            pieces_list,
                            context_url=set_link["url"],
                            set_sku=set_sku,
                        )
                    )
            if visible_rows:
                logger.debug(
                    "Set '%s': visible Set Pieces labels → %d names",
                    set_link["name"],
                    len(visible_rows),
                )

            def _norm_member_name(value: str) -> str:
                return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

            member_entries: list[SetMemberEntry] = []
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
                        structured_name = _norm_member_name(
                            str(structured.get("name") or "")
                        )
                        if visible_norm and structured_name == visible_norm:
                            matched_structured = structured
                            matched_index = inner_idx
                            break
                    if matched_index is not None:
                        used_structured.add(matched_index)
                    visible_sku = _normalize_set_member_sku(visible_row.get("sku"))
                    structured_sku = (
                        _normalize_set_member_sku(matched_structured.get("sku"))
                        if matched_structured
                        else None
                    )
                    fallback_sku = (
                        _normalize_set_member_sku(member_skus[idx])
                        if idx < len(member_skus)
                        else None
                    )
                    quantity_sku = visible_sku or structured_sku or fallback_sku
                    fallback_qty = (
                        member_quantities.get(quantity_sku, 1) if quantity_sku else 1
                    )
                    chosen_sku = visible_sku or structured_sku or fallback_sku
                    if chosen_sku and set_sku and chosen_sku == set_sku:
                        chosen_sku = visible_sku or structured_sku or fallback_sku
                    member_entries.append(
                        {
                            "sku": chosen_sku,
                            "name": visible_name,
                            "quantity": (
                                matched_structured["quantity"]
                                if matched_structured
                                else fallback_qty
                            ),
                            "is_set_only": (
                                visible_row["is_set_only"]
                                if matched_structured is None
                                else False
                            ),
                        }
                    )
            else:
                for idx, sku in enumerate(member_skus):
                    member_entries.append(
                        {
                            "sku": sku,
                            "name": (
                                member_names[idx]
                                if idx < len(member_names) and member_names[idx]
                                else None
                            ),
                            "quantity": member_quantities.get(sku, 1),
                            "is_set_only": (
                                member_is_set_only[idx]
                                if idx < len(member_is_set_only)
                                else False
                            ),
                        }
                    )

            logger.debug(
                "Set '%s': sku=%s, %d members",
                set_link["name"],
                set_sku,
                len(member_skus),
            )
            return dict(
                name=set_link["name"],
                sku=set_sku,
                url=set_link["url"],
                member_skus=member_skus,
                member_quantities=member_quantities,
                member_entries=member_entries,
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

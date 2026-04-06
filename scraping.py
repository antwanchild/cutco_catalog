import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from constants import (
    REQUEST_TIMEOUT, SCRAPE_CATEGORIES, SCRAPE_HEADERS, SCRAPE_SETS_URL,
    SYNC_BLOCKED_CATEGORIES, _BUNDLE_KEYWORDS, _resolve_category, _is_set_product,
)

logger = logging.getLogger(__name__)


def _extract_sku_from_href(href: str) -> str | None:
    """Pull a base SKU from a /p/ product URL."""
    parts = href.rstrip("/").split("/")
    slug = parts[-1].split("?")[0].split("&")[0].upper()
    if not slug:
        return None
    lead = re.match(r'^(\d{3,}[A-Z]{0,3})', slug)
    if lead:
        candidate = lead.group(1)
    elif any(char.isdigit() for char in slug) and len(slug) <= 12:
        candidate = slug
    else:
        return None
    if candidate.endswith("SH"):
        candidate = candidate[:-2]
    stripped = re.sub(r"[A-Z]+$", "", candidate)
    if stripped and stripped.isdigit() and len(stripped) >= 2:
        candidate = stripped
    return candidate or None


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


def _fetch_sku_from_page(url: str) -> tuple[str | None, str | None]:
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
        url_slug_sku = _extract_sku_from_href(url)
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
                r"""["']?sku["']?\s*:\s*["']?(\d{2,4}[A-Z]{0,2})["']?""",
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
            sku_text = soup.find(string=re.compile(r"#\d{2,4}[A-Z]?\b"))
            if sku_text:
                sku_match = re.search(r"#(\d{2,4}[A-Z]?)\b", sku_text.strip(), re.IGNORECASE)
                if sku_match:
                    sku = sku_match.group(1).upper()
                    strategy_log.append(f"visible-text={sku}")

        # Strategy 6: keyword context
        if not sku:
            page_text = soup.get_text(" ", strip=True)
            sku_match = re.search(
                r"(?:model|item|sku|product)\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{2,4}[A-Z]?)\b",
                page_text, re.IGNORECASE)
            if sku_match:
                sku = sku_match.group(1).upper()
                strategy_log.append(f"keyword={sku}")

        # Normalise: strip all trailing color/variant letters
        stripped = re.sub(r"[A-Z]+$", "", sku or "")
        if stripped and stripped.isdigit() and len(stripped) >= 2:
            sku = stripped

        # Reject CSS hex colors
        if sku and re.fullmatch(r"[0-9A-F]{6}", sku, re.IGNORECASE):
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
    """Fetch a product page and return a dict with edge_type, msrp,
    blade_length, overall_length, and weight.  One HTTP request covers all.

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
            m = re.search(r'"specName"\s*:\s*"Edge"\s*,\s*"specValue"\s*:\s*"([^"]+)"', raw_html)
            if not m:
                m = re.search(r'"specValue"\s*:\s*"([^"]+)"\s*,\s*"specName"\s*:\s*"Edge"', raw_html)
            if m:
                result["edge_type"] = _EDGE_NORMALIZE.get(m.group(1).strip().lower(), "Unknown")
            else:
                result["edge_type"] = "N/A"

        # ── itemSpecs (blade length, overall length, weight) ─────────────────
        _SPEC_MAP = {
            "length - blade":   "blade_length",
            "length - overall": "overall_length",
            "weight - knife only": "weight",
            "weight":           "weight",
        }
        for spec in re.finditer(
                r'"specName"\s*:\s*"([^"]+)"\s*,\s*"specValue"\s*:\s*"([^"]+)"', raw_html):
            key = spec.group(1).strip().lower()
            val = spec.group(2).strip()
            field = _SPEC_MAP.get(key)
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
                         [a.get("href", "") for a in product_links[:10]])

            seen_hrefs: set[str] = set()
            unique_links = []
            for anchor in product_links:
                full_href = anchor.get("href", "")
                base_href = full_href.split("&")[0]
                if base_href not in seen_hrefs:
                    seen_hrefs.add(base_href)
                    unique_links.append((anchor, full_href))

            for anchor, href in unique_links:
                base_href = href.split("&")[0]
                sku = _extract_sku_from_href(base_href)
                prod_url = href if href.startswith("http") else f"https://www.cutco.com{href}"

                name_el = anchor.find(["h2", "h3"])
                if not name_el and anchor.parent:
                    name_el = anchor.parent.find(["h2", "h3"])
                name = name_el.get_text(strip=True) if name_el else None

                if cat_name == "Sheaths" or (name and "sheath" in name.lower() and "with sheath" not in name.lower()):
                    sku = None

                if not sku:
                    if "&view=product" not in prod_url:
                        prod_url = prod_url + "&view=product"
                    if prod_url not in seen_slug_urls:
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
                pool.submit(_fetch_sku_from_page, prod_url): (prod_url, cat_name, cat_name_hint)
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
            member_quantities: dict[str, int] = {}
            seen_member: set[str] = set()

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
                        raw_sku = str(entry.get("childItemNumber") or "").upper().strip().split("/")[0]
                        if not raw_sku:
                            continue
                        base_sku = re.sub(r"[A-Z]+$", "", raw_sku) if len(raw_sku) > 2 else raw_sku
                        if re.fullmatch(r"20\d{2}", base_sku):
                            continue
                        qty = int(entry.get("qty") or 1)
                        if base_sku not in seen_member:
                            seen_member.add(base_sku)
                            member_skus.append(base_sku)
                            member_quantities[base_sku] = qty
                    logger.debug("Set '%s': itemSetList → %d members", set_link["name"], len(member_skus))
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    logger.debug("itemSetList parse failed for %s: %s", fetch_url, exc)
                    member_skus.clear()
                    member_quantities.clear()
                    seen_member.clear()

            # Strategy 2: fallback — image /rolo/ SKU extraction
            if not member_skus:
                for img in detail.select("img[src*='/rolo/']"):
                    match = sku_pattern.search(img.get("src", ""))
                    if match:
                        raw = match.group(1).upper()
                        base_sku = re.sub(r"[A-Z]+$", "", raw) if len(raw) > 2 else raw
                        if re.fullmatch(r"20\d{2}", base_sku):
                            continue
                        if base_sku not in seen_member:
                            seen_member.add(base_sku)
                            member_skus.append(base_sku)
                            member_quantities[base_sku] = 1
                logger.debug("Set '%s': image fallback → %d members", set_link["name"], len(member_skus))

            logger.debug("Set '%s': sku=%s, %d members", set_link["name"], set_sku, len(member_skus))
            return dict(
                name             = set_link["name"],
                sku              = set_sku,
                url              = set_link["url"],
                member_skus      = member_skus,
                member_quantities= member_quantities,
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

"""Scraping logic for MSRP background jobs."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import requests
from bs4 import BeautifulSoup

from constants import REQUEST_TIMEOUT, SCRAPE_HEADERS
from models import Item

_MSRP_PRICE_FETCH_TIMEOUT = timedelta(minutes=3)
_MSRP_PRICE_FETCH_WORKERS = 12

_CUTTING_BOARD_URLS = {
    "124": "https://www.cutco.com/p/small-cutting-board",
    "125": "https://www.cutco.com/p/medium-cutting-board",
    "126": "https://www.cutco.com/p/large-cutting-board",
}


def _normalize_price_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _is_exact_product_url(url: str | None) -> bool:
    return bool(url and "/p/" in url)


def _normalize_msrp_url(url: str, sku: str | None = None) -> str:
    if sku and sku in _CUTTING_BOARD_URLS and "cutting-boards" in url:
        return _CUTTING_BOARD_URLS[sku]
    return url


def _line_matches_target(line: str, item_name: str | None, sku: str | None = None) -> bool:
    normalized_line = _normalize_price_text(line)
    if not normalized_line:
        return False
    if sku:
        normalized_sku = _normalize_price_text(sku)
        if normalized_sku and re.search(rf"(?<![a-z0-9]){re.escape(normalized_sku)}(?![a-z0-9])", normalized_line):
            return True
    normalized_item = _normalize_price_text(item_name)
    if not normalized_item:
        return False
    return normalized_line == normalized_item or normalized_item in normalized_line or normalized_line in normalized_item


def _scrape_price_from_page(url: str, item_name: str | None = None, sku: str | None = None) -> float | None:
    """Return the visible MSRP from a single Cutco product page."""
    try:
        request_url = _normalize_msrp_url(url, sku)
        resp = requests.get(request_url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        page_url = resp.url or request_url
        if not _is_exact_product_url(page_url):
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        for noise in soup.find_all(["script", "style"]):
            noise.decompose()

        heading = soup.find("h1")
        heading_text = heading.get_text(" ", strip=True) if heading else None
        lines = [line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()]

        candidates = [item_name, heading_text]
        start_index = 0
        if lines:
            for candidate in candidates:
                if not candidate:
                    continue
                for index, line in enumerate(lines):
                    if _line_matches_target(line, candidate, sku=sku):
                        start_index = index
                        break
                else:
                    continue
                break

        stop_markers = (
            "Add to Cart",
            "Frequently Bought Together",
            "You May Also Like",
            "Specifications",
            "Reviews & Questions",
            "Regular shipping and handling included",
            "Regular shipping included",
            "Shipping and handling included",
        )

        if lines:
            truncated_lines = lines[start_index:]
            for stop_index, line in enumerate(truncated_lines):
                if any(marker.lower() in line.lower() for marker in stop_markers):
                    truncated_lines = truncated_lines[:stop_index]
                    break

            for line in truncated_lines:
                for dollar_match in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", line):
                    try:
                        price = float(dollar_match.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    if price > 0:
                        return price

        for key in ("actualPrice", "fullRetail"):
            price_match = re.search(rf'"{key}"\s*:\s*([\d.]+)', resp.text)
            if price_match:
                try:
                    price = float(price_match.group(1))
                except ValueError:
                    continue
                if price > 0:
                    return price
        return None
    except Exception:
        return None


def _build_msrp_price_targets(live_items: list[dict]) -> dict[str, dict]:
    """Build a SKU map that prefers stored DB URLs for known items."""
    db_url_by_sku = {
        item.sku: item.cutco_url
        for item in Item.query.filter(Item.sku.isnot(None), Item.cutco_url.isnot(None)).all()
        if item.sku and item.cutco_url
    }
    by_sku: dict[str, dict] = {}
    for live_item in live_items:
        sku = live_item.get("sku")
        if sku and sku not in by_sku:
            by_sku[sku] = {
                "name": live_item["name"],
                "url": db_url_by_sku.get(sku) or live_item["url"],
                "price": None,
            }
    return by_sku


def _build_msrp_price_targets_from_db(db_items: list[Item]) -> dict[str, dict]:
    """Build a SKU map from stored catalog rows."""
    by_sku: dict[str, dict] = {}
    for item in db_items:
        if not item.sku or not item.cutco_url or item.sku in by_sku:
            continue
        by_sku[item.sku] = {
            "name": item.name,
            "url": _normalize_msrp_url(item.cutco_url, item.sku),
            "price": None,
        }
    return by_sku


def _fetch_live_prices_by_sku(
    by_sku: dict[str, dict],
    *,
    workers: int = _MSRP_PRICE_FETCH_WORKERS,
    log_fn=None,
) -> tuple[int, int]:
    """Fetch live prices for the provided SKU map."""
    fetched = 0
    timed_out = 0
    completed: set = set()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        future_map = {
            executor.submit(_scrape_price_from_page, info["url"], info["name"], sku): sku
            for sku, info in by_sku.items()
            if info.get("url")
        }
        try:
            for future in as_completed(future_map, timeout=_MSRP_PRICE_FETCH_TIMEOUT.total_seconds()):
                sku = future_map[future]
                completed.add(future)
                try:
                    by_sku[sku]["price"] = future.result()
                except Exception as exc:
                    by_sku[sku]["price"] = None
                    if log_fn:
                        log_fn(f"  ! {sku} price fetch failed: {exc}")
                fetched += 1
                if fetched % 20 == 0 and log_fn:
                    log_fn(f"  …{fetched}/{len(future_map)} prices fetched")
        except TimeoutError:
            pass

        pending = [future for future in future_map if future not in completed]
        timed_out = len(pending)
        for future in pending:
            sku = future_map[future]
            by_sku[sku]["price"] = None
            future.cancel()
        if timed_out and log_fn:
            log_fn(
                f"Timed out waiting for {timed_out} price fetch(es); "
                "continuing without them."
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return fetched, timed_out

"""Background jobs for MSRP diffing and specs backfill."""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from constants import DATA_DIR, DISCORD_WEBHOOK_URL, REQUEST_TIMEOUT, SCRAPE_HEADERS
from extensions import db
from helpers import _notify_discord, check_wishlist_targets
from job_state import read_json_file, reset_json_file, write_json_file
from models import Item, record_activity

logger = logging.getLogger(__name__)

_MSRP_JOB_FILE   = os.path.join(DATA_DIR, "msrp_job.json")
_msrp_write_lock  = threading.Lock()
_MSRP_JOB_STALE_AFTER = timedelta(minutes=30)
_MSRP_PRICE_FETCH_TIMEOUT = timedelta(minutes=3)
_MSRP_PRICE_FETCH_WORKERS = int(os.environ.get("MSRP_PRICE_FETCH_WORKERS", "12"))

_CUTTING_BOARD_URLS = {
    "124": "https://www.cutco.com/p/small-cutting-board",
    "125": "https://www.cutco.com/p/medium-cutting-board",
    "126": "https://www.cutco.com/p/large-cutting-board",
}

_SPECS_JOB_FILE  = os.path.join(DATA_DIR, "specs_job.json")
_specs_write_lock = threading.Lock()


def _read_specs_job() -> dict:
    return read_json_file(_SPECS_JOB_FILE, {"status": "idle", "progress": [], "results": None, "error": None,
                "started_at": None, "finished_at": None})


def _write_specs_job(data: dict) -> None:
    write_json_file(_SPECS_JOB_FILE, data, lock=_specs_write_lock)


def _run_specs_backfill_job(app) -> None:
    """Backfill item specs from cutco_url pages.

    Updates blade_length, overall_length, weight, and msrp
    (only when msrp is not already set).
    """
    from scraping import scrape_item_specs

    with app.app_context():
        items = Item.query.filter(Item.cutco_url.isnot(None)).all()
        total = len(items)
        progress = []

        def _log(msg):
            progress.append(msg)
            _write_specs_job({"status": "running", "progress": list(progress),
                              "results": None, "error": None,
                              "started_at": started_at, "finished_at": None})

        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        _log(f"Starting specs backfill for {total} items…")

        updated = skipped = errors = 0

        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {pool.submit(scrape_item_specs, item.cutco_url): item
                          for item in items}
            done = 0
            for future in as_completed(future_map):
                item = future_map[future]
                done += 1
                try:
                    specs = future.result()
                    changed = False
                    if specs.get("blade_length") and not item.blade_length:
                        item.blade_length = specs["blade_length"]
                        changed = True
                    if specs.get("overall_length") and not item.overall_length:
                        item.overall_length = specs["overall_length"]
                        changed = True
                    if specs.get("weight") and not item.weight:
                        item.weight = specs["weight"]
                        changed = True
                    if specs.get("msrp") and not item.msrp:
                        item.msrp = specs["msrp"]
                        changed = True
                    if changed:
                        db.session.commit()
                        updated += 1
                        _log(f"[{done}/{total}] ✓ {item.name} ({item.sku})")
                    else:
                        skipped += 1
                except Exception as exc:
                    db.session.rollback()
                    errors += 1
                    _log(f"[{done}/{total}] ✗ {item.name}: {exc}")

        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        results = {"updated": updated, "skipped": skipped, "errors": errors, "total": total}
        _log(f"Done — {updated} updated, {skipped} already complete, {errors} errors.")
        _write_specs_job({"status": "done", "progress": progress, "results": results,
                          "error": None, "started_at": started_at, "finished_at": finished_at})
        record_activity(
            "specs_backfill",
            "Specs backfill complete",
            f"Updated {updated} items, skipped {skipped}, {errors} errors.",
            occurred_at=finished_at,
        )
        db.session.commit()


def _read_msrp_job() -> dict:
    job = read_json_file(_MSRP_JOB_FILE, {"status": "idle", "progress": [], "results": None,
                "error": None, "started_at": None, "finished_at": None,
                "update_db": True, "heartbeat_at": None})
    if job.get("status") != "running":
        return job
    timestamp_text = job.get("heartbeat_at") or job.get("started_at")
    stale = not timestamp_text
    if not stale:
        try:
            timestamp = datetime.fromisoformat(str(timestamp_text))
        except ValueError:
            stale = True
        else:
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            stale = datetime.now(UTC) - timestamp > _MSRP_JOB_STALE_AFTER
    if not stale:
        return job

    recovered = dict(job)
    recovered["status"] = "error"
    recovered["error"] = "Previous MSRP diff job became stale. Please rerun the diff."
    recovered["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    recovered["heartbeat_at"] = recovered["finished_at"]
    _write_msrp_job(recovered)
    return recovered


def _write_msrp_job(data: dict) -> None:
    write_json_file(_MSRP_JOB_FILE, data, lock=_msrp_write_lock)


def _reset_msrp_job() -> None:
    """Clear the persisted MSRP job state back to idle."""
    reset_json_file(_MSRP_JOB_FILE, {
        "status": "idle",
        "progress": [],
        "results": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "update_db": True,
        "heartbeat_at": None,
    }, lock=_msrp_write_lock)


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
    """Return the visible MSRP from a single Cutco product page.

    This path is intentionally strict: it only trusts exact product pages and a
    tiny explicit cutting-board URL mapping. If the page is ambiguous, it fails
    closed and returns None instead of guessing.
    """
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
    """Build a SKU map from stored catalog rows.

    This is the fast path for MSRP scans: we already know the stored DB item
    URLs, so we can fetch prices directly without rediscovering the catalog.
    """
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
    """Fetch live prices for the provided SKU map.

    Returns:
        (fetched_count, timed_out_count)

    """
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


def _build_msrp_diff(db_items: list, live: dict) -> dict:
    """Diff DB MSRP prices against live scraped prices."""
    db_by_sku = {item.sku: item for item in db_items if item.sku}
    live_skus  = set(live.keys())
    db_skus    = set(db_by_sku.keys())
    result: dict[str, list] = {
        "new": [], "removed": [], "increased": [],
        "decreased": [], "unchanged": [], "no_price": [],
    }
    for sku in sorted(live_skus - db_skus):
        info = live[sku]
        result["new"].append({"sku": sku, "name": info["name"],
                               "db_price": None, "live_price": info["price"]})
    for sku in sorted(db_skus - live_skus):
        item = db_by_sku[sku]
        result["removed"].append({"sku": sku, "name": item.name,
                                   "db_price": item.msrp, "live_price": None})
    for sku in sorted(db_skus & live_skus):
        item       = db_by_sku[sku]
        db_price   = item.msrp
        live_price = live[sku]["price"]
        if live_price is None:
            result["no_price"].append({"sku": sku, "name": item.name,
                                        "db_price": db_price, "live_price": None})
        elif db_price is None:
            result["unchanged"].append({"sku": sku, "name": item.name,
                                         "db_price": None, "live_price": live_price})
        elif live_price > db_price + 0.005:
            result["increased"].append({"sku": sku, "name": item.name,
                                         "db_price": db_price, "live_price": live_price,
                                         "delta": live_price - db_price})
        elif live_price < db_price - 0.005:
            result["decreased"].append({"sku": sku, "name": item.name,
                                         "db_price": db_price, "live_price": live_price,
                                         "delta": live_price - db_price})
        else:
            result["unchanged"].append({"sku": sku, "name": item.name,
                                         "db_price": db_price, "live_price": live_price})
    return result


def _run_msrp_diff_job(app, update_db: bool) -> None:
    """Background thread: scrape prices, diff against DB, optionally update."""

    def log(msg: str) -> None:
        job = _read_msrp_job()
        job["progress"].append(msg)
        job["heartbeat_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _write_msrp_job(job)

    with app.app_context():
        try:
            db_items = Item.query.filter(Item.sku.isnot(None)).all()
            log(f"Loaded {len(db_items)} DB items — using stored Cutco URLs…")
            by_sku = _build_msrp_price_targets_from_db(db_items)

            log(f"Fetching prices for {len(by_sku)} unique SKUs…")
            fetched, timed_out = _fetch_live_prices_by_sku(by_sku, log_fn=log)
            if timed_out:
                log(f"Continuing with {timed_out} missing live price(s).")

            priced = sum(1 for info in by_sku.values() if info["price"] is not None)
            log(f"Prices found: {priced}/{len(by_sku)}")
            if by_sku and priced == 0:
                raise RuntimeError(
                    "Cutco.com could not be reached or returned no MSRP prices."
                )

            log("Building diff from stored DB rows…")
            diff = _build_msrp_diff(db_items, by_sku)

            changes = len(diff["increased"]) + len(diff["decreased"])
            log(f"Done — {changes} price change(s), {len(diff['new'])} new, "
                f"{len(diff['removed'])} removed")

            if update_db:
                db_by_sku = {item.sku: item for item in db_items}
                updated = 0
                for sku, info in by_sku.items():
                    price = info["price"]
                    item = db_by_sku.get(sku)
                    if price is None or item is None:
                        continue
                    if item.msrp != price:
                        item.msrp = price
                        updated += 1
                db.session.commit()
                log(f"Updated {updated} MSRP prices in database")

                hits = check_wishlist_targets()
                if hits:
                    log(f"Wishlist targets met: {len(hits)}")
                    if DISCORD_WEBHOOK_URL:
                        lines = ["**🎯 Cutco Wishlist — Price Targets Met**"]
                        for hit in hits:
                            lines.append(
                                f"• **{hit['person']}** — {hit['item']} (#{hit['sku']}): "
                                f"MSRP ${hit['msrp']:.2f} ≤ target ${hit['target']:.2f}"
                            )
                        _notify_discord("\n".join(lines))

            job = _read_msrp_job()
            job.update({"status": "done", "results": diff,
                        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds")})
            _write_msrp_job(job)
            record_activity(
                "msrp_diff",
                "MSRP diff complete",
                f"{len(diff['increased']) + len(diff['decreased'])} changed prices, {len(diff['new'])} new, {len(diff['removed'])} removed.",
                occurred_at=job["finished_at"],
            )
            db.session.commit()

        except Exception as exc:
            logger.error("MSRP diff job failed: %s", exc)
            job = _read_msrp_job()
            job.update({"status": "error", "error": str(exc),
                        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds")})
            _write_msrp_job(job)
            record_activity(
                "msrp_diff",
                "MSRP diff failed",
                str(exc),
                occurred_at=job["finished_at"],
            )
            db.session.commit()

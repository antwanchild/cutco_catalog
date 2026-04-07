import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from constants import (
    DATA_DIR, DISCORD_WEBHOOK_URL, REQUEST_TIMEOUT, SCRAPE_HEADERS,
)
from extensions import db
from helpers import _notify_discord, check_wishlist_targets
from models import Item
from scraping import scrape_catalog

logger = logging.getLogger(__name__)

_MSRP_JOB_FILE   = os.path.join(DATA_DIR, "msrp_job.json")
_msrp_write_lock  = threading.Lock()

_SPECS_JOB_FILE  = os.path.join(DATA_DIR, "specs_job.json")
_specs_write_lock = threading.Lock()


def _read_specs_job() -> dict:
    try:
        with open(_SPECS_JOB_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "idle", "progress": [], "results": None, "error": None,
                "started_at": None, "finished_at": None}


def _write_specs_job(data: dict) -> None:
    with _specs_write_lock:
        tmp = _SPECS_JOB_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, _SPECS_JOB_FILE)


def _run_specs_backfill_job(app) -> None:
    """Backfill item specs from cutco_url pages.

    Updates blade_length, overall_length, weight, and msrp
    (only when msrp is not already set).
    """
    from scraping import scrape_item_specs
    from datetime import date as _date

    with app.app_context():
        items = Item.query.filter(Item.cutco_url.isnot(None)).all()
        total = len(items)
        progress = []

        def _log(msg):
            progress.append(msg)
            _write_specs_job({"status": "running", "progress": list(progress),
                              "results": None, "error": None,
                              "started_at": started_at, "finished_at": None})

        started_at = _date.today().isoformat()
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

        finished_at = _date.today().isoformat()
        results = {"updated": updated, "skipped": skipped, "errors": errors, "total": total}
        _log(f"Done — {updated} updated, {skipped} already complete, {errors} errors.")
        _write_specs_job({"status": "done", "progress": progress, "results": results,
                          "error": None, "started_at": started_at, "finished_at": finished_at})


def _read_msrp_job() -> dict:
    try:
        with open(_MSRP_JOB_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"status": "idle", "progress": [], "results": None,
                "error": None, "started_at": None, "finished_at": None,
                "update_db": False}


def _write_msrp_job(data: dict) -> None:
    with _msrp_write_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _MSRP_JOB_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, _MSRP_JOB_FILE)


def _scrape_price_from_page(url: str) -> float | None:
    """Return the price from a Cutco product page, or None if not found."""
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        for ld_tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(ld_tag.string or "")
                entries = data if isinstance(data, list) else [data]
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("@type") != "Product":
                        continue
                    offers = entry.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price_val = offers.get("price") if isinstance(offers, dict) else None
                    if price_val is not None:
                        return float(price_val)
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass

        og_tag = soup.find("meta", property="og:price:amount")
        if og_tag and og_tag.get("content", "").strip():
            try:
                return float(og_tag["content"].replace(",", ""))
            except ValueError:
                pass

        price_el = soup.find(attrs={"itemprop": "price"})
        if price_el:
            raw = price_el.get("content") or price_el.get_text(strip=True)
            price_match = re.search(r"[\d,]+\.?\d*", raw)
            if price_match:
                try:
                    return float(price_match.group().replace(",", ""))
                except ValueError:
                    pass

        for noise in soup.find_all(["script", "style"]):
            noise.decompose()
        dollar_match = re.search(r"\$\s*([\d,]+\.\d{2})", soup.get_text(" ", strip=True))
        if dollar_match:
            try:
                return float(dollar_match.group(1).replace(",", ""))
            except ValueError:
                pass
        return None
    except Exception:
        return None


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
        _write_msrp_job(job)

    try:
        with app.app_context():
            log("Scraping live catalog…")
            live_items, _ = scrape_catalog()
            log(f"Found {len(live_items)} items on cutco.com")

            by_sku: dict[str, dict] = {}
            for live_item in live_items:
                sku = live_item.get("sku")
                if sku and sku not in by_sku:
                    by_sku[sku] = {"name": live_item["name"],
                                   "url": live_item["url"], "price": None}

            log(f"Fetching prices for {len(by_sku)} unique SKUs…")
            fetched = 0
            with ThreadPoolExecutor(max_workers=8) as pool:
                future_map = {
                    pool.submit(_scrape_price_from_page, info["url"]): sku
                    for sku, info in by_sku.items() if info.get("url")
                }
                for future in as_completed(future_map):
                    sku = future_map[future]
                    by_sku[sku]["price"] = future.result()
                    fetched += 1
                    if fetched % 20 == 0:
                        log(f"  …{fetched}/{len(future_map)} prices fetched")

            priced = sum(1 for info in by_sku.values() if info["price"] is not None)
            log(f"Prices found: {priced}/{len(by_sku)}")

            db_items = Item.query.filter(Item.sku.isnot(None)).all()
            log(f"Loaded {len(db_items)} DB items — building diff…")
            diff = _build_msrp_diff(db_items, by_sku)

            changes = len(diff["increased"]) + len(diff["decreased"])
            log(f"Done — {changes} price change(s), {len(diff['new'])} new, "
                f"{len(diff['removed'])} removed")

            if update_db:
                db_by_sku = {item.sku: item for item in db_items}
                updated = sum(
                    1 for sku, info in by_sku.items()
                    if info["price"] is not None and sku in db_by_sku
                    and not setattr(db_by_sku[sku], "msrp", info["price"])  # side-effect
                )
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
                        "finished_at": date.today().isoformat()})
            _write_msrp_job(job)

    except Exception as exc:
        logger.error("MSRP diff job failed: %s", exc)
        job = _read_msrp_job()
        job.update({"status": "error", "error": str(exc),
                    "finished_at": date.today().isoformat()})
        _write_msrp_job(job)

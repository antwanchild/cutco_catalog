"""Specs backfill helpers for MSRP jobs."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from constants import DATA_DIR
from extensions import db
from job_state import read_json_file, write_json_file
from models import Item, record_activity

logger = logging.getLogger(__name__)

_SPECS_JOB_FILE = os.path.join(DATA_DIR, "specs_job.json")
_specs_write_lock = threading.Lock()


def _read_specs_job() -> dict:
    return read_json_file(_SPECS_JOB_FILE, {"status": "idle", "progress": [], "results": None, "error": None,
                "started_at": None, "finished_at": None})


def _write_specs_job(data: dict) -> None:
    write_json_file(_SPECS_JOB_FILE, data, lock=_specs_write_lock)


def _run_specs_backfill_job(app) -> None:
    """Backfill item specs from cutco_url pages."""
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

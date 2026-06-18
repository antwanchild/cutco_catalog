#!/usr/bin/env python3
"""msrp_diff.py — Compare DB MSRP prices against live Cutco prices.

Usage:
    python msrp_diff.py [--update] [--csv PATH] [--discord]

Flags:
    --update        Write newly scraped prices back to the database.
    --csv PATH      Export the full diff table to a CSV file.
    --discord       Post a summary to the DISCORD_WEBHOOK_URL env var.

Environment:
    DATABASE_URL          SQLite path (default: /data/cutco.db)
    DISCORD_WEBHOOK_URL   Webhook URL for --discord flag.
"""

import argparse
import csv
import os
import sys
from datetime import date

import requests

# ── Bootstrap Flask app context ───────────────────────────────────────────────
# Build an app instance so the script gets the same DB/session setup as the web app.
sys.path.insert(0, os.path.dirname(__file__))
from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import Item  # noqa: E402
from msrp_jobs import _build_msrp_price_targets_from_db, _fetch_live_prices_by_sku  # noqa: E402
from helpers import check_wishlist_targets, _notify_discord  # noqa: E402

def scrape_live_prices(
    workers: int = 8,
    *,
    db_items: list[Item] | None = None,
    include_catalog: bool = False,
) -> dict[str, dict]:
    """Return a dict of sku → {name, url, price} for stored Cutco products.

    MSRP diff now uses the database as the source of truth for URLs. The old
    catalog rediscovery mode is disabled here because it was introducing bad
    page guesses and drift.
    """
    if include_catalog:
        print("Catalog rediscovery is disabled for MSRP diff; using stored DB URLs.", flush=True)
    if db_items is None:
        app = create_app()
        with app.app_context():
            db_items = Item.query.filter(Item.sku.isnot(None)).all()
    print(f"Using {len(db_items or [])} stored DB item URLs…", flush=True)
    by_sku = _build_msrp_price_targets_from_db(db_items or [])

    print(f"Fetching prices for {len(by_sku)} unique SKUs…", flush=True)
    fetched, timed_out = _fetch_live_prices_by_sku(by_sku, workers=workers, log_fn=lambda msg: print(msg, flush=True))
    if timed_out:
        print(f"  Continuing with {timed_out} missing live price(s).", flush=True)

    priced = sum(1 for info in by_sku.values() if info["price"] is not None)
    print(f"  Prices found: {priced}/{len(by_sku)}", flush=True)
    return by_sku


# ── Diff logic ────────────────────────────────────────────────────────────────

def build_diff(db_items: list[Item], live: dict[str, dict]) -> dict:
    """Compare DB items against live scraped data.

    Returns a dict with keys:
      new       — SKUs on cutco.com not in DB
      removed   — SKUs in DB not on cutco.com
      increased — price went up
      decreased — price went down
      unchanged — price matches (or neither side has a price)
      no_price  — live item found but price could not be scraped
    """
    db_by_sku = {item.sku: item for item in db_items if item.sku}
    live_skus  = set(live.keys())
    db_skus    = set(db_by_sku.keys())

    result: dict[str, list] = {
        "new":       [],
        "removed":   [],
        "increased": [],
        "decreased": [],
        "unchanged": [],
        "no_price":  [],
    }

    for sku in sorted(live_skus - db_skus):
        info = live[sku]
        result["new"].append({
            "sku": sku, "name": info["name"],
            "db_price": None, "live_price": info["price"],
        })

    for sku in sorted(db_skus - live_skus):
        item = db_by_sku[sku]
        result["removed"].append({
            "sku": sku, "name": item.name,
            "db_price": item.msrp, "live_price": None,
        })

    for sku in sorted(db_skus & live_skus):
        item      = db_by_sku[sku]
        db_price  = item.msrp
        live_price = live[sku]["price"]

        if live_price is None:
            result["no_price"].append({
                "sku": sku, "name": item.name,
                "db_price": db_price, "live_price": None,
            })
        elif db_price is None:
            # First time we've seen a price — treat as new data, not a change
            result["unchanged"].append({
                "sku": sku, "name": item.name,
                "db_price": None, "live_price": live_price,
            })
        elif live_price > db_price + 0.005:
            result["increased"].append({
                "sku": sku, "name": item.name,
                "db_price": db_price, "live_price": live_price,
                "delta": live_price - db_price,
            })
        elif live_price < db_price - 0.005:
            result["decreased"].append({
                "sku": sku, "name": item.name,
                "db_price": db_price, "live_price": live_price,
                "delta": live_price - db_price,
            })
        else:
            result["unchanged"].append({
                "sku": sku, "name": item.name,
                "db_price": db_price, "live_price": live_price,
            })

    return result


def find_stale_msrp_rows(db_items: list[Item]) -> list[dict]:
    """Return DB rows with a stale or placeholder MSRP."""
    stale_rows: list[dict] = []
    for item in db_items:
        if item.msrp is None or item.msrp <= 0:
            stale_rows.append({
                "sku": item.sku,
                "name": item.name,
                "db_price": item.msrp,
                "cutco_url": item.cutco_url,
            })
    return sorted(stale_rows, key=lambda row: row["sku"] or "")


# ── Output helpers ─────────────────────────────────────────────────────────────

def _fmt_price(price: float | None) -> str:
    """Format a price for display."""
    return f"${price:,.2f}" if price is not None else "—"


def _fmt_delta(delta: float | None) -> str:
    """Format a price delta for display."""
    if delta is None:
        return ""
    sign = "+" if delta > 0 else ""
    return f"{sign}${delta:,.2f}"


def print_report(diff: dict) -> None:
    """Print a human-readable MSRP diff report."""
    today = date.today().isoformat()
    total = sum(len(section_rows) for section_rows in diff.values())
    changes = len(diff["increased"]) + len(diff["decreased"])

    print()
    print(f"MSRP Diff Report — {today}")
    print("=" * 60)
    print(f"  Total items compared : {total}")
    print(f"  Price changes        : {changes}")
    print(f"  New on site          : {len(diff['new'])}")
    print(f"  Removed from site    : {len(diff['removed'])}")
    print(f"  Price unavailable    : {len(diff['no_price'])}")
    print()

    sections = [
        ("PRICE INCREASES",  diff["increased"],  True),
        ("PRICE DECREASES",  diff["decreased"],  True),
        ("NEW ON SITE",      diff["new"],         False),
        ("REMOVED FROM SITE",diff["removed"],     False),
    ]

    for title, rows, show_delta in sections:
        if not rows:
            continue
        print(f"{title} ({len(rows)})")
        print(f"  {'SKU':<8}  {'Name':<40}  {'DB':>10}  {'Live':>10}{'  Δ':>8}")
        print(f"  {'-'*8}  {'-'*40}  {'-'*10}  {'-'*10}{'  -'*1:>8}")
        for row in sorted(rows, key=lambda report_row: report_row["sku"]):
            delta_str = _fmt_delta(row.get("delta")) if show_delta else ""
            print(
                f"  {row['sku']:<8}  {row['name'][:40]:<40}  "
                f"{_fmt_price(row['db_price']):>10}  "
                f"{_fmt_price(row['live_price']):>10}"
                f"  {delta_str:>8}"
            )
        print()


def print_stale_msrp_audit(stale_rows: list[dict]) -> None:
    """Print a compact report of rows that need MSRP refresh."""
    today = date.today().isoformat()
    print()
    print(f"Stale MSRP Audit — {today}")
    print("=" * 60)
    print(f"  Rows needing refresh : {len(stale_rows)}")
    print()
    if not stale_rows:
        print("✅ No stale MSRP rows found.")
        return

    print(f"  {'SKU':<8}  {'Name':<40}  {'DB':>10}  {'URL'}")
    print(f"  {'-'*8}  {'-'*40}  {'-'*10}  {'-'*30}")
    for row in sorted(stale_rows, key=lambda report_row: report_row["sku"] or ""):
        print(
            f"  {str(row['sku'])[:8]:<8}  {row['name'][:40]:<40}  "
            f"{_fmt_price(row['db_price']):>10}  "
            f"{(row.get('cutco_url') or '')[:30]}"
        )


def write_csv(diff: dict, path: str) -> None:
    """Write the diff report to a CSV file."""
    all_rows = []
    for category, rows in diff.items():
        for row in rows:
            all_rows.append({
                "category":   category,
                "sku":        row["sku"],
                "name":       row["name"],
                "db_price":   row["db_price"] if row["db_price"] is not None else "",
                "live_price": row["live_price"] if row["live_price"] is not None else "",
                "delta":      row.get("delta", ""),
            })
    all_rows.sort(key=lambda report_row: (report_row["category"], report_row["sku"]))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["category", "sku", "name",
                                                  "db_price", "live_price", "delta"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"CSV written → {path}")


def post_discord(diff: dict, webhook_url: str) -> None:
    """Post a compact MSRP diff summary to Discord."""
    today = date.today().isoformat()
    increased = diff["increased"]
    decreased = diff["decreased"]
    new_items  = diff["new"]
    removed    = diff["removed"]

    lines = [f"**Cutco MSRP Diff — {today}**"]
    if increased:
        lines.append(f"\n📈 **Price increases ({len(increased)})**")
        for row in increased[:10]:
            lines.append(
                f"  • {row['name']} (#{row['sku']}): "
                f"{_fmt_price(row['db_price'])} → {_fmt_price(row['live_price'])} "
                f"({_fmt_delta(row.get('delta'))})"
            )
        if len(increased) > 10:
            lines.append(f"  _…and {len(increased) - 10} more_")

    if decreased:
        lines.append(f"\n📉 **Price decreases ({len(decreased)})**")
        for row in decreased[:10]:
            lines.append(
                f"  • {row['name']} (#{row['sku']}): "
                f"{_fmt_price(row['db_price'])} → {_fmt_price(row['live_price'])} "
                f"({_fmt_delta(row.get('delta'))})"
            )
        if len(decreased) > 10:
            lines.append(f"  _…and {len(decreased) - 10} more_")

    if new_items:
        lines.append(f"\n🆕 **New items ({len(new_items)})**")
        for row in new_items[:5]:
            lines.append(f"  • {row['name']} (#{row['sku']}) — {_fmt_price(row['live_price'])}")
        if len(new_items) > 5:
            lines.append(f"  _…and {len(new_items) - 5} more_")

    if removed:
        lines.append(f"\n🗑️ **Removed from site ({len(removed)})**")
        for row in removed[:5]:
            lines.append(f"  • {row['name']} (#{row['sku']})")
        if len(removed) > 5:
            lines.append(f"  _…and {len(removed) - 5} more_")

    if not (increased or decreased or new_items or removed):
        lines.append("\n✅ No price changes detected.")

    payload = {"content": "\n".join(lines)}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        print("Discord notification sent.")
    except Exception as exc:
        print(f"Discord post failed: {exc}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """Run the MSRP diff CLI."""
    parser = argparse.ArgumentParser(
        description="Compare DB MSRP prices against live Cutco prices."
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Write newly scraped prices back to the database.",
    )
    parser.add_argument(
        "--csv", metavar="PATH",
        help="Export the full diff to a CSV file.",
    )
    parser.add_argument(
        "--discord", action="store_true",
        help="Post a summary to DISCORD_WEBHOOK_URL.",
    )
    parser.add_argument(
        "--audit-stale", action="store_true",
        help="List catalog rows whose stored MSRP is missing or zero.",
    )
    parser.add_argument(
        "--repair-stale", action="store_true",
        help="Refresh only rows whose stored MSRP is missing or zero.",
    )
    parser.add_argument(
        "--catalog", action="store_true",
        help="Deprecated; MSRP diff now always uses stored DB URLs.",
    )
    args = parser.parse_args()

    if args.discord:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
            sys.exit(1)

    app = create_app()
    with app.app_context():
        db_items = Item.query.filter(Item.sku.isnot(None)).all()
        print(f"Loaded {len(db_items)} items from database.")

        stale_rows = find_stale_msrp_rows(db_items)
        if args.audit_stale:
            print_stale_msrp_audit(stale_rows)
            if not args.repair_stale:
                return

        live = scrape_live_prices(db_items=db_items, include_catalog=args.catalog)
        diff = build_diff(db_items, live)

        print_report(diff)

        if args.update or args.repair_stale:
            updated = 0
            db_by_sku = {item.sku: item for item in db_items if item.sku}
            stale_skus = {row["sku"] for row in stale_rows if row.get("sku")}
            for sku, info in live.items():
                if info["price"] is None or sku not in db_by_sku:
                    continue
                if args.repair_stale and sku not in stale_skus:
                    continue
                if db_by_sku[sku].msrp != info["price"]:
                    db_by_sku[sku].msrp = info["price"]
                    updated += 1
            db.session.commit()
            mode = "stale MSRP rows" if args.repair_stale and not args.update else "MSRP prices"
            print(f"Updated {updated} {mode} in database.")

            # After price update, check wishlist targets and notify
            if args.update or args.repair_stale:
                hits = check_wishlist_targets()
                if hits:
                    print(f"\nWishlist targets met: {len(hits)}")
                    for hit in hits:
                        print(f"  🎯 {hit['person']} — {hit['item']} (#{hit['sku']}): "
                              f"${hit['msrp']:.2f} ≤ target ${hit['target']:.2f}")
                    if args.discord:
                        lines = ["**🎯 Cutco Wishlist — Price Targets Met**"]
                        for hit in hits:
                            lines.append(
                                f"• **{hit['person']}** — {hit['item']} (#{hit['sku']}): "
                                f"MSRP ${hit['msrp']:.2f} ≤ target ${hit['target']:.2f} "
                                f"(saves ${hit['savings']:.2f})"
                            )
                        _notify_discord("\n".join(lines))
                else:
                    print("No wishlist targets met at updated prices.")

        if args.csv:
            write_csv(diff, args.csv)

        if args.discord:
            post_discord(diff, webhook_url)


if __name__ == "__main__":
    main()

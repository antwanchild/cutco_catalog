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
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

# ── Bootstrap Flask app context ───────────────────────────────────────────────
# Importing app.py triggers db.create_all() and the startup migrations, which
# is fine — it's idempotent.  We get all models and scraping constants for free.
sys.path.insert(0, os.path.dirname(__file__))
from app import app, db  # noqa: E402
from models import Item  # noqa: E402
from constants import SCRAPE_HEADERS, REQUEST_TIMEOUT  # noqa: E402
from scraping import scrape_catalog  # noqa: E402
from helpers import check_wishlist_targets, _notify_discord  # noqa: E402

# ── Price scraping ─────────────────────────────────────────────────────────────

def _scrape_price_from_page(url: str) -> float | None:
    """Fetch a Cutco product page and return its price.

    Tries, in order:
      1. JSON-LD  offers.price
      2. <meta property="og:price:amount">
      3. [itemprop="price"] content attribute
      4. First $NNN.NN pattern in visible text
    """
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # Strategy 1: JSON-LD offers.price
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

        # Strategy 2: Open Graph meta tag
        og_tag = soup.find("meta", property="og:price:amount")
        if og_tag and og_tag.get("content", "").strip():
            try:
                return float(og_tag["content"].replace(",", ""))
            except ValueError:
                pass

        # Strategy 3: itemprop="price"
        price_el = soup.find(attrs={"itemprop": "price"})
        if price_el:
            raw = price_el.get("content") or price_el.get_text(strip=True)
            price_match = re.search(r"[\d,]+\.?\d*", raw.replace(",", ""))
            if price_match:
                try:
                    return float(price_match.group().replace(",", ""))
                except ValueError:
                    pass

        # Strategy 4: first $NNN.NN in visible text
        for noise in soup.find_all(["script", "style"]):
            noise.decompose()
        page_text = soup.get_text(" ", strip=True)
        dollar_match = re.search(r"\$\s*([\d,]+\.\d{2})", page_text)
        if dollar_match:
            try:
                return float(dollar_match.group(1).replace(",", ""))
            except ValueError:
                pass

        return None
    except Exception:
        return None


def scrape_live_prices(workers: int = 8) -> dict[str, dict]:
    """Return a dict of sku → {name, url, price} for all live Cutco products.

    Uses scrape_catalog() to discover current items, then fetches prices in
    parallel from each product page.
    """
    print("Scraping live catalog…", flush=True)
    live_items, _ = scrape_catalog()
    print(f"  Found {len(live_items)} items on cutco.com", flush=True)

    # Build SKU → item map first (dedup by SKU)
    by_sku: dict[str, dict] = {}
    for item in live_items:
        sku = item.get("sku")
        if sku and sku not in by_sku:
            by_sku[sku] = {"name": item["name"], "url": item["url"], "price": None}

    print(f"Fetching prices for {len(by_sku)} unique SKUs…", flush=True)
    fetched = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_scrape_price_from_page, info["url"]): sku
            for sku, info in by_sku.items()
            if info.get("url")
        }
        for future in as_completed(future_map):
            sku = future_map[future]
            price = future.result()
            by_sku[sku]["price"] = price
            fetched += 1
            if fetched % 20 == 0:
                print(f"  …{fetched}/{len(future_map)}", flush=True)

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


# ── Output helpers ─────────────────────────────────────────────────────────────

def _fmt_price(val: float | None) -> str:
    return f"${val:,.2f}" if val is not None else "—"


def _fmt_delta(val: float | None) -> str:
    if val is None:
        return ""
    sign = "+" if val > 0 else ""
    return f"{sign}${val:,.2f}"


def print_report(diff: dict) -> None:
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


def write_csv(diff: dict, path: str) -> None:
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
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["category", "sku", "name",
                                                  "db_price", "live_price", "delta"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"CSV written → {path}")


def post_discord(diff: dict, webhook_url: str) -> None:
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
    args = parser.parse_args()

    if args.discord:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
            sys.exit(1)

    with app.app_context():
        db_items = Item.query.filter(Item.sku.isnot(None)).all()
        print(f"Loaded {len(db_items)} items from database.")

        live = scrape_live_prices()
        diff = build_diff(db_items, live)

        print_report(diff)

        if args.update:
            updated = 0
            db_by_sku = {item.sku: item for item in db_items if item.sku}
            for sku, info in live.items():
                if info["price"] is not None and sku in db_by_sku:
                    db_by_sku[sku].msrp = info["price"]
                    updated += 1
            db.session.commit()
            print(f"Updated {updated} MSRP prices in database.")

            # After price update, check wishlist targets and notify
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

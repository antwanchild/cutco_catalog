# ⚔️ Cutco Vault

A self-hosted web application for Cutco collectors to catalog, track, and manage their Cutco knife and kitchen product collections.

![Version](https://img.shields.io/badge/version-1.4.1-blue)
![Python](https://img.shields.io/badge/python-3.14-blue)
![Flask](https://img.shields.io/badge/flask-3.1-lightgrey)
![Docker](https://img.shields.io/badge/docker-ready-2496ED)

---

## Features

- **Catalog** — Full product catalog with SKU, category, edge type, and link to Cutco.com
- **Variants** — Track every color/handle variant per item
- **Ownership** — Record who owns what, with statuses: Owned, Wishlist, Sold, Traded
- **Sets & Bundles** — Manage curated Cutco sets and which items belong to each
- **Unicorn Tracking** — Flag rare, discontinued, or limited-run items and variants
- **Wish List** — Track wanted items with target prices; get Discord alerts when MSRP drops to or below target
- **Collection Stats** — Visual dashboard: owned items by category, handle color distribution, edge type breakdown, catalog coverage, and estimated collection value (Chart.js)
- **MSRP Diff** — Compare stored prices against live Cutco.com prices; run from the admin UI or CLI. Supports writing updated prices to the database and optional Discord summary
- **Sharpening Log** — Track sharpening events per knife (date, method, notes); surface overdue reminders in the UI or via Discord
- **Bakeware Tracker** — Log baking sessions per piece (what you made, rating, notes); flag pieces unused for a configurable number of days
- **Catalog Sync** — Scrape Cutco.com to discover new items and sets automatically
- **Import / Export** — Bulk import ownership data via CSV or XLSX; export full collection as CSV
- **Matrix View** — Cross-tabulate items vs. collectors at a glance
- **Admin Controls** — Token-protected admin mode for catalog edits, syncing, and MSRP diffs
- **Discord Notifications** — Optional webhook integration for wishlist price alerts, sharpening reminders, and bakeware reminders
- **Mobile Friendly** — Responsive layout works on phones and tablets

---

## Quick Start (Docker)

```yaml
services:
  cutco-vault:
    image: ghcr.io/antwanchild/cutco_catalog:latest
    ports:
      - "8095:8095"
    volumes:
      - ./data:/data
    environment:
      - ADMIN_TOKEN=your-secret-token
      - SECRET_KEY=your-flask-secret
      - PUID=1000
      - PGID=1000
    restart: unless-stopped
```

Then open `http://localhost:8095` in your browser.

---

## Environment Variables

| Variable | Default | Required | Description |
|---|---|:---:|---|
| `SECRET_KEY` | `cutco-vault-dev-key` | ⚠️ | Flask session secret — **change in production** |
| `ADMIN_TOKEN` | `admin` | ⚠️ | Token required to log in as admin — **change in production** |
| `DATABASE_URL` | `sqlite:////data/cutco.db` | No | SQLAlchemy connection string |
| `DATA_DIR` | `/data` | No | Directory for the database and job state files |
| `LOG_LEVEL` | `INFO` | No | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_DIR` | `/data/logs` | No | Directory for rotating log files |
| `SYNC_BLOCKED_CATEGORIES` | *(empty)* | No | Comma-separated category names to exclude from catalog sync |
| `DISCORD_WEBHOOK_URL` | *(empty)* | No | Incoming webhook URL for Discord notifications |
| `SHARPEN_THRESHOLD_DAYS` | `180` | No | Days before a knife is flagged overdue for sharpening |
| `BAKEWARE_THRESHOLD_DAYS` | `60` | No | Days before a bakeware piece is flagged as idle |
| `BAKEWARE_CATEGORIES` | `Ake Cookware,Cookware,Bakeware` | No | Catalog categories treated as bakeware |
| `PUID` | `0` | No | Run container as this user ID (for correct file ownership on the host) |
| `PGID` | `0` | No | Run container as this group ID |
| `TZ` | `UTC` | No | Container timezone |

⚠️ = has a working default but must be changed before exposing to a network.

---

## Catalog Sync

The **Sync** button (admin only) scrapes Cutco.com and shows a preview of new items and sets before anything is saved. You can:

- Review and deselect items you don't want imported
- Edit category assignments inline
- Import new sets with their member SKUs pre-populated

To block specific categories from ever appearing in the sync preview, set `SYNC_BLOCKED_CATEGORIES`:

```
SYNC_BLOCKED_CATEGORIES=Tableware,Accessories
```

---

## MSRP Diff

Compare prices stored in your database against live Cutco.com prices. Runs from the admin UI (`Admin → MSRP Diff`) or directly from the CLI.

**Web UI** (admin only):
1. Log in as admin
2. Click **MSRP Diff** in the nav
3. Optionally check **Write prices to DB** to persist updated prices
4. Click **Run Diff** — progress streams live; results appear when the scrape completes

**CLI** (inside the container):

```bash
# Report only
docker exec -it cutco-vault python msrp_diff.py

# Update DB prices and notify Discord
docker exec -it cutco-vault python msrp_diff.py --update --discord

# Export to CSV
docker exec -it cutco-vault python msrp_diff.py --update --csv /data/msrp_diff.csv
```

After a DB update, any wishlist items whose MSRP now meets a collector's target price are automatically surfaced (and notified via Discord if `DISCORD_WEBHOOK_URL` is set).

---

## Wish List & Price Alerts

Set a **target price** on any wishlist ownership entry. The Wishlist page (`/wishlist`) shows:

- Current MSRP vs. target for every wishlist item
- Green highlight when a target is met
- **Check Targets** button (admin) to fire Discord alerts for all met targets

Alerts are also triggered automatically after an MSRP diff that writes prices to the DB.

---

## Sharpening Log

Track sharpening events for each knife. The Sharpening page (`/sharpening`) shows:

- Days since last sharpening per knife, with a visual progress bar
- Overdue warnings for knives past `SHARPEN_THRESHOLD_DAYS` (default 180 days)
- **Check Overdue** button (admin) to send a Discord reminder listing all overdue knives

Methods: Home Sharpener, Whetstone, Cutco Service, Professional, Other.

---

## Bakeware Tracker

Log baking sessions per piece. The Bakeware page (`/bakeware`) shows:

- Days since last use per piece
- Idle warnings for pieces past `BAKEWARE_THRESHOLD_DAYS` (default 60 days)
- Never-used panel listing catalog bakeware with no sessions yet
- Per-session rating (1–5 stars) and what you made
- **Check Idle** button (admin) to send a Discord reminder

---

## Discord Notifications

Set `DISCORD_WEBHOOK_URL` to an [incoming webhook](https://support.discord.com/hc/en-us/articles/228383668) URL. All notification types use the same webhook:

| Trigger | Event |
|---|---|
| Wishlist Check | MSRP ≤ target price for any wishlist item |
| MSRP Diff `--update` / web UI | Same as above, plus overall diff summary (CLI only) |
| Sharpening Check Overdue | Any knife past the sharpening threshold |
| Bakeware Check Idle | Any piece past the bakeware threshold |

---

## Import Format

Bulk-import ownership data from a CSV or XLSX file. Download a pre-formatted template from the **Import** page.

### Required Columns

| Column | Description |
|---|---|
| `name` | Item name (must match or be new) |
| `sku` | Cutco model number |
| `color` | Handle color (or leave blank for Unknown) |
| `edge_type` | `Straight`, `Double-D`, `Serrated`, `Micro-D`, `Tec Edge`, or `Unknown` |
| `is_unicorn` | `yes` / `no` |
| `person` | Collector name |
| `status` | `Owned`, `Wishlist`, `Sold`, or `Traded` |
| `category` | Product category |
| `notes` | Free-text notes |

Set membership columns (mark `yes` to assign): `Beast`, `Fanatic`, `Signature`, `Homemaker`, `Gourmet`, `Hunter`, and others as configured.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.14, Flask 3.1 |
| Database | SQLite (via SQLAlchemy) |
| App Server | Gunicorn (4 workers) |
| Scraping | Requests, BeautifulSoup4, lxml |
| Excel Import | openpyxl |
| Charts | Chart.js (CDN, no extra dependency) |
| Container | Docker (`python:3.14-slim`) |
| Frontend | Jinja2 templates, vanilla CSS/JS |

---

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
flask --app app run --debug

# Lint
ruff check app.py
```

The database is created automatically on first run. All schema migrations are applied at startup — no migration tool required.

---

## Data Storage

All persistent data lives in `/data/` inside the container — mount this as a volume:

```
/data/cutco.db        # SQLite database
/data/logs/           # Rotating log files (5 MB × 5 files)
/data/msrp_job.json   # MSRP diff job state (created on first run)
```

---

## Health Check

```
GET /health   → 200 OK  {"status": "ok", "version": "1.4.1"}
GET /version  → {"version": "1.4.1"}
```

---

## License

MIT

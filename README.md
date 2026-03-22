# ⚔️ Cutco Vault

A self-hosted web application for Cutco collectors to catalog, track, and manage their Cutco knife and kitchen product collections.

![Version](https://img.shields.io/badge/version-1.3.6-blue)
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
- **Catalog Sync** — Scrape Cutco.com to discover new items and sets automatically
- **Import / Export** — Bulk import ownership data via CSV or XLSX; export full collection as CSV
- **Matrix View** — Cross-tabulate items vs. collectors at a glance
- **Admin Controls** — Token-protected admin mode for catalog edits and syncing
- **Mobile Friendly** — Responsive layout works on phones and tablets

---

## Quick Start (Docker)

```yaml
services:
  cutco-vault:
    image: ghcr.io/your-org/cutco-vault:latest
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
| `LOG_LEVEL` | `INFO` | No | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_DIR` | `/data/logs` | No | Directory for rotating log files |
| `SYNC_BLOCKED_CATEGORIES` | *(empty)* | No | Comma-separated category names to exclude from catalog sync |
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

The database is created automatically on first run. All schema migrations are applied at startup.

---

## Data Storage

All persistent data lives in `/data/` inside the container — mount this as a volume:

```
/data/cutco.db       # SQLite database
/data/logs/          # Rotating log files
```

---

## Health Check

```
GET /health   → 200 OK
GET /version  → {"version": "1.3.6"}
```

---

## License

MIT

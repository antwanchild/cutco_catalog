# ⚔️ Cutco Vault

A self-hosted web application for Cutco collectors to catalog, track, and manage their Cutco knife and kitchen product collections.

![Version](https://img.shields.io/github/v/release/antwanchild/cutco_catalog)
![CI](https://github.com/antwanchild/cutco_catalog/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.13-blue)
![Flask](https://img.shields.io/badge/flask-3.1-lightgrey)
![Docker](https://img.shields.io/badge/docker-ready-2496ED)

---

## ✨ Features

- 📋 **Catalog** — Full product catalog with SKU, category, edge type, and link to Cutco.com
- 🎨 **Variants** — Track every color/handle variant per item, with a dedicated browse page for colors like red or purple
- 🏠 **Ownership** — Record who owns what, with statuses: Owned, Wishlist, Sold, Traded
- 🗂️ **Sets & Bundles** — Manage curated Cutco sets with member items and quantities (e.g. Galley +6 shows ×6)
- 🦄 **Unicorn Tracking** — Flag rare, discontinued, or limited-run items and variants
- 🎯 **Wish List** — Track wanted items with target prices; get Discord alerts when MSRP drops to or below target
- 📈 **Collection Stats** — Visual dashboard: owned items by category, handle color distribution, edge type breakdown, catalog coverage, and estimated collection value (Chart.js)
- 💲 **MSRP Diff** — Compare stored prices against live Cutco.com prices; run from the admin UI or CLI. Supports writing updated prices to the database and optional Discord summary
- 🔪 **Sharpening Log** — Track sharpening events per knife (date, method, notes); surface overdue reminders in the UI or via Discord
- 🍳 **Cookware Tracker** — Log usage sessions per piece (what you made, rating, notes); flag pieces unused for a configurable number of days
- 🔪 **Knife-to-Task Pairing** — Log which knife you use for which kitchen task; view usage patterns and top tasks per knife (only owned knives shown)
- 🎯 **Suggested Uses** — Sync Cutco.com's recommended uses per knife into the task system; task dropdown highlights tasks suggested for the selected knife; task detail page shows all knives that can perform a given task
- 🗂️ **Set Completion** — Track progress through Cutco sets with per-person completion bars and owned/missing panels; one-click wishlist from missing items
- 🔄 **Catalog Sync** — Scrape Cutco.com to discover new items and sets automatically
- 🎨 **Variant Sync** — Separate page for color-variant cleanup; previews existing/create/retained colors, plus a polished review/result flow, without touching catalog sync
- 📥 **Import / Export** — Bulk import ownership data via CSV or XLSX; export full collection as CSV
- 🧩 **Completion Reports** — Standalone completion-gaps reporting and completion-import rollups for rep-style collection lists
- 📊 **Matrix View** — Cross-tabulate items vs. collectors at a glance
- 🔒 **Admin Controls** — Token-protected admin mode for catalog edits, syncing, and MSRP diffs
- 🔐 **Public / Private Split** — Product browse pages stay public; collector, import, and mutation routes stay private; signed gift/collection links remain shareable
- 🔔 **Discord Notifications** — Optional webhook integration for wishlist price alerts, sharpening reminders, and cookware reminders
- 🌙 **Dark / Light Mode** — Toggle between dark (default) and light themes; preference saved in localStorage
- 🎁 **Gift List Sharing** — Generate a signed shareable link showing missing set items for a person; no login required, print-friendly
- 🃏 **Collection Card** — Shareable public page showing a person's full owned collection grouped by category, with stats and estimated value
- 📊 **Bulk Status Update** — Select multiple ownership entries and change status in one action from the collection page
- 🔒 **Bot Protection** — Rate-limited admin login, CSRF tokens on all forms, security headers, and robots.txt
- 🛡️ **Error Handling** — Friendly 403/404/429/500 error pages; all database writes roll back cleanly on failure
- ❓ **Contextual Help** — ? button on every major page opens an inline help modal explaining the page's features
- 📱 **Mobile Friendly** — Responsive layout works on phones and tablets

---

## 🚀 Quick Start (Docker)

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

Notes:

- Set `PUID` / `PGID` to your host user and group if you want files under the mounted `/data` volume to be owned by your normal account instead of root.
- Gunicorn runtime files are intentionally kept inside the container (`/dev/shm` and `/tmp`) so hidden `.gunicorn` directories do not get created in bind mounts.
- The image defaults to `TZ=UTC`, so diagnostics timestamps and job history show UTC unless you override `TZ` in the container environment.
- After changing the Dockerfile or image tag, rebuild and recreate the container so the running container picks up the new startup command.

### PR Preview Images

If you want to test a branch before merging, add the `docker` label to the pull request.

- The PR checks page shows a `Preview Gate` check so you can see the preview opt-in state.
- The PR workflow builds a preview image whenever the pull request is opened, updated, reopened, or labeled, tagged like `pr-123`.
- Keep the `docker` label on the PR while you test so the preview image stays enabled for new pushes.
- If you add the label after the PR already exists, the next PR update or label event will build the preview image.
- The preview tag stays fixed at `pr-123`, so the app version shown inside the container does not look like a release bump on every push.
- The workflow still tracks a per-PR build number internally for metadata and labels, but the Docker tag itself does not change.
- Same-repo PRs can publish that tag to GHCR so you can `docker pull ghcr.io/antwanchild/cutco_catalog:pr-123`.
- When the PR is merged, or when you remove the `docker` label, the matching preview tag is cleaned up automatically.
- Forked PRs can still run the build check safely, but they do not publish a registry image.

### Release Cleanup

The repo also includes a workflow that can remove stale **GitHub draft releases** after they age out.

- It defaults to a 30-day threshold
- It can be run manually in dry-run mode to preview matches
- It does **not** delete real published release tags like `v1.102.0`
- It only touches draft GitHub Release records, not git tags

---

## 🧪 Testing

Run the lightweight smoke tests against a temporary SQLite database:

```bash
python3 -m unittest discover -s tests
```

The smoke suite is grouped by feature area: public, import, catalog, people, ownership, logs, and tasks.

Regression tests should use one of these prefixes so the workflow picks them up:

- `test_regression_<slug>`
- `test_issue_<issue_number>_<slug>`
- `test_bug_<slug>`

Lint the repo with:

```bash
pip install -r requirements-dev.txt
ruff check .
black --check .
pyright
pre-commit run --all-files
```

`ruff` handles the lint pass and import-order checks, so there is no separate `isort` or `flake8` step in this repo.

To enable the local hooks, install the tooling once and run `pre-commit install`.

---

## ⚙️ Environment Variables

| Variable | Default | Required | Description |
|---|---|:---:|---|
| `SECRET_KEY` | `cutco-vault-dev-key` | ⚠️ | Flask session secret — **change in production** |
| `ADMIN_TOKEN` | `admin` | ⚠️ | One-time bootstrap token used to create the first named admin — **change in production** |
| `ADMIN_SESSION_SECONDS` | `7200` | No | Admin session lifetime in seconds (default 2 h from login time); set to `0` for browser-session only |
| `DATABASE_URL` | `sqlite:////data/cutco.db` | No | SQLAlchemy connection string |
| `DATA_DIR` | `/data` | No | Directory for the database and job state files |
| `LOG_LEVEL` | `INFO` | No | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_DIR` | `/data/logs` | No | Directory for rotating log files |
| `SESSION_COOKIE_SECURE` | `false` | No | Set to `true` when served over HTTPS so session cookies are sent only via TLS |
| `TRUSTED_AUTH_USERNAME_HEADER` | `X-Forwarded-User` | No | Trusted reverse-proxy username header that marks a request as authenticated via your auth proxy |
| `TRUSTED_AUTH_GROUPS_HEADER` | `X-Forwarded-Groups` | No | Trusted reverse-proxy groups header used to recognize proxy-admin users |
| `TRUSTED_AUTH_ADMIN_GROUPS` | *(empty)* | No | Comma-separated group names that should count as admin when reported by the trusted proxy |
| `ALLOW_INSECURE_DEFAULTS` | `false` | No | Set to `true` to bypass startup safety checks that reject default `SECRET_KEY` / `ADMIN_TOKEN` in production |
| `SYNC_BLOCKED_CATEGORIES` | *(empty)* | No | Comma-separated category names to exclude from catalog sync |
| `DISCORD_WEBHOOK_URL` | *(empty)* | No | Incoming webhook URL for Discord notifications |
| `SHARPEN_THRESHOLD_DAYS` | `180` | No | Days before a knife is flagged overdue for sharpening |
| `COOKWARE_THRESHOLD_DAYS` | `60` | No | Days before a cookware-tracked piece is flagged as idle |
| `COOKWARE_CATEGORIES` | `Cookware` | No | Catalog categories tracked on the Cookware page |
| `PUID` | `0` | No | If non-zero, create a matching container user and run the app as that UID for correct ownership on mounted volumes |
| `PGID` | `0` | No | Group ID paired with `PUID` when running the container as a non-root host user |
| `TZ` | `UTC` | No | Container timezone used for diagnostics timestamps and job history displays |

⚠️ = has a working default but must be changed before exposing to a network.

## 🔄 Catalog Sync

The **Sync** button (admin only) scrapes Cutco.com and shows a preview of new items and sets before anything is saved. You can:

- Review and deselect items you don't want imported
- Edit category assignments inline
- Review same-SKU / different-name rows before confirm so you can keep your preferred item names
- Import new sets with member SKUs and quantities pre-populated (e.g. Galley +6 records ×6 steak knives)
- See set completeness cues, including items not yet in the catalog

A separate **Sync Uses** button (admin, on the Manage Tasks page) scrapes Cutco.com's "Uses" tab for every cataloged item and populates the task system with Cutco-recommended pairings.

To block specific categories from ever appearing in the sync preview, set `SYNC_BLOCKED_CATEGORIES`:

```
SYNC_BLOCKED_CATEGORIES=Tableware,Accessories
```

Variant color maintenance lives on a separate **Variant Sync** page. It scans product pages for color options, previews `existing` / `create` / `not seen in sync` states, and only creates missing variants. Missing variants are retained so possible unicorn colors are not deleted by accident. You can open it from the catalog page or the More menu.

## 🔐 Public vs Private Pages

Public pages describe products and can be shared without a password:

- `/`
- `/search`
- `/catalog`
- `/sets/<id>`
- `/views/item/<id>`
- `/attachments/<id>`
- `/gifts/<token>`
- `/collection-card/<token>`
- `/health`
- `/version`

Private pages describe collector data or allow changes:

- `/people/*`
- `/wishlist`
- `/sharpening`
- `/cookware`
- `/tasks`
- `/admin/*`
- import, export, completion, sync, and other mutation routes

If you put an auth proxy in front of the app, authenticated requests can be treated as private by setting `TRUSTED_AUTH_USERNAME_HEADER` to the trusted username header your proxy forwards. If your proxy also forwards group membership, set `TRUSTED_AUTH_GROUPS_HEADER` and `TRUSTED_AUTH_ADMIN_GROUPS` so the app can recognize proxy-based admin access. The legacy `AUTHENTIK_USERNAME_HEADER` and `AUTHENTIK_GROUPS_HEADER` names are still accepted for compatibility, and `AUTHELIA_USERNAME_HEADER` / `AUTHELIA_GROUPS_HEADER` are also supported as fallbacks.

Admin access is hybrid: standalone installations can use named local accounts,
while proxy-authenticated users in the configured admin group can skip the local
login form and go straight to admin pages. On an installation with no users, open
`/setup` and use `ADMIN_TOKEN` once to create the first named administrator. As
soon as a user exists, token login and previously issued token-admin sessions are
disabled. Local users can change their password from the Account/Admin menu;
password changes revoke their other sessions.

### Local account recovery

User recovery commands run inside the application container and use the same
database and account invariants as the web application. Passwords are always
prompted interactively and are never accepted as command-line arguments:

```bash
# Inspect account names, roles, sources, status, and forced-change state.
docker compose exec cutco-vault sh -c 'exec gosu "$PUID:$PGID" flask --app app:create_app users list'

# Create the first or an additional local administrator.
docker compose exec cutco-vault sh -c 'exec gosu "$PUID:$PGID" flask --app app:create_app users create-admin --username owner'

# Set a temporary password, revoke existing sessions, and force a change at login.
docker compose exec cutco-vault sh -c 'exec gosu "$PUID:$PGID" flask --app app:create_app users reset-password owner'

# Reactivate an account or explicitly revoke all of its current sessions.
docker compose exec cutco-vault sh -c 'exec gosu "$PUID:$PGID" flask --app app:create_app users activate owner'
docker compose exec cutco-vault sh -c 'exec gosu "$PUID:$PGID" flask --app app:create_app users revoke-sessions owner'
```

Use an interactive terminal for commands that prompt for a password. Restrict
container-shell access to trusted operators because these recovery commands do
not require a web session. Proxy-managed passwords must be reset at the identity
provider; the application CLI rejects password resets for proxy accounts.

For example, you might set:

- Authentik: `TRUSTED_AUTH_USERNAME_HEADER=X-authentik-username`, `TRUSTED_AUTH_GROUPS_HEADER=X-authentik-groups`
- Authelia: `TRUSTED_AUTH_USERNAME_HEADER=Remote-User`, `TRUSTED_AUTH_GROUPS_HEADER=Remote-Groups`
- Header-normalized Traefik: `TRUSTED_AUTH_USERNAME_HEADER=X-Forwarded-User`, `TRUSTED_AUTH_GROUPS_HEADER=X-Forwarded-Groups`

### Traefik + authentik

This layout keeps product browsing public, while private collector pages and admin/mutation pages require Authentik.

```yaml
services:
  cutco-vault:
    image: ghcr.io/antwanchild/cutco_catalog:latest
    environment:
      - TRUSTED_AUTH_USERNAME_HEADER=X-authentik-username
      - TRUSTED_AUTH_GROUPS_HEADER=X-authentik-groups
      - TRUSTED_AUTH_ADMIN_GROUPS=authentik Admins
    labels:
      - traefik.enable=true
      - traefik.http.services.cutco.loadbalancer.server.port=8095

      # Public pages: catalog browsing, sets, product views, health/version
      - traefik.http.routers.${CUTCO_NAME:-cutco}-public.rule=Host(`cutco.anthonychild.com`)
      - traefik.http.routers.${CUTCO_NAME:-cutco}-public.entrypoints=websecure
      - traefik.http.routers.${CUTCO_NAME:-cutco}-public.tls=true
      - traefik.http.routers.${CUTCO_NAME:-cutco}-public.priority=1
      - traefik.http.routers.${CUTCO_NAME:-cutco}-public.middlewares=chain-no-auth-NOerrors@file
      - traefik.http.routers.${CUTCO_NAME:-cutco}-public.service=cutco

      # Private collector pages
      - traefik.http.routers.${CUTCO_NAME:-cutco}-private.rule=Host(`cutco.anthonychild.com`) && (PathPrefix(`/people`) || PathPrefix(`/wishlist`) || PathPrefix(`/sharpening`) || PathPrefix(`/cookware`) || PathPrefix(`/tasks`) || Path(`/stats`) || PathPrefix(`/views/matrix`))
      - traefik.http.routers.${CUTCO_NAME:-cutco}-private.entrypoints=websecure
      - traefik.http.routers.${CUTCO_NAME:-cutco}-private.tls=true
      - traefik.http.routers.${CUTCO_NAME:-cutco}-private.priority=100
      - traefik.http.routers.${CUTCO_NAME:-cutco}-private.middlewares=chain-auth-shit-NOerrors@file
      - traefik.http.routers.${CUTCO_NAME:-cutco}-private.service=cutco

      # Admin pages and mutating routes
      - traefik.http.routers.${CUTCO_NAME:-cutco}-admin.rule=Host(`cutco.anthonychild.com`) && (PathPrefix(`/admin`) || PathPrefix(`/data/import`) || PathPrefix(`/data/export`) || PathPrefix(`/data/completion-gaps`) || PathPrefix(`/data/completion-import`) || PathPrefix(`/data/variant-sync`) || PathPrefix(`/catalog/add`) || PathPrefix(`/catalog/`) || PathPrefix(`/sets/add`) || PathPrefix(`/sets/`) || PathPrefix(`/views/item/`) || PathPrefix(`/attachments/`))
      - traefik.http.routers.${CUTCO_NAME:-cutco}-admin.entrypoints=websecure
      - traefik.http.routers.${CUTCO_NAME:-cutco}-admin.tls=true
      - traefik.http.routers.${CUTCO_NAME:-cutco}-admin.priority=200
      - traefik.http.routers.${CUTCO_NAME:-cutco}-admin.middlewares=chain-auth-shit-NOerrors@file
      - traefik.http.routers.${CUTCO_NAME:-cutco}-admin.service=cutco
```

Hardcode the public hostname in the Traefik `Host(...)` labels unless `DOMAIN` is defined in the compose project `.env` file or shell environment. A `DOMAIN` value under the service's `environment:` block is only passed into the Cutco container; Docker Compose does not use it to render labels before Traefik reads them.

If you want Authentik to recognize proxy-authenticated users inside the app, make sure your forwardAuth middleware passes these headers through:

```yaml
middlewares-authentik:
  forwardAuth:
    address: http://authentik_server:9000/outpost.goauthentik.io/auth/traefik
    trustForwardHeader: true
    authResponseHeaders:
      - X-authentik-username
      - X-authentik-groups
      - X-authentik-email
      - X-authentik-name
      - X-authentik-uid
```

The app reads the Authentik headers directly from Flask requests, so the important part is that Traefik forwards the Authentik username and groups into the same header names configured above.

One practical note: the admin router above intentionally covers the app's mutating routes, but the public router still handles read-only browsing routes like `/catalog`, `/sets/<id>`, `/views/item/<id>`, `/attachments/<id>`, `/health`, and `/version`.

---

## 💲 MSRP Diff

Compare prices stored in your database against live Cutco.com prices. Runs from the admin UI (`Admin → MSRP Diff`) or directly from the CLI.

**🖥️ Web UI** (admin only):
1. Log in as admin
2. Click **MSRP Diff** in the nav
3. Optionally check **Write prices to DB** to persist updated prices
4. Click **Run Diff** — progress streams live; results appear when the scrape completes

**💻 CLI** (inside the container):

```bash
# Report only
docker exec -it cutco-vault python msrp_diff.py

# Update DB prices and notify Discord
docker exec -it cutco-vault python msrp_diff.py --update --discord

# Export to CSV
docker exec -it cutco-vault python msrp_diff.py --update --csv /data/msrp_diff.csv
```

After a DB update, any wishlist items whose MSRP now meets a collector's target price are automatically surfaced (and notified via Discord if `DISCORD_WEBHOOK_URL` is set).

## 🗂️ Sets

The Sets page helps you track collection progress through Cutco sets:

- Filter to only sets with items not yet in the catalog
- Filter to only incomplete sets
- View imported member item numbers and quantities for each set
- Drill into a set to edit it or return to the same filtered list you came from

---

## 🎯 Wish List & Price Alerts

Set a **target price** on any wishlist ownership entry. The Wishlist page (`/wishlist`) shows:

- Current MSRP vs. target for every wishlist item
- 🟢 Green highlight when a target is met
- **Check Targets** button (admin) to fire Discord alerts for all met targets

Alerts are also triggered automatically after an MSRP diff that writes prices to the DB.

---

## 🔪 Sharpening Log

Track sharpening events for each knife. The Sharpening page (`/sharpening`) shows:

- Days since last sharpening per knife, with a visual progress bar
- ⚠️ Overdue warnings for knives past `SHARPEN_THRESHOLD_DAYS` (default 180 days)
- **Check Overdue** button (admin) to send a Discord reminder listing all overdue knives

Methods: Home Sharpener, Whetstone, Cutco Service, Professional, Other.

---

## 🍳 Cookware Tracker

Log usage sessions per piece. The Cookware page (`/cookware`) shows:

- Days since last use per piece
- ⚠️ Idle warnings for pieces past `COOKWARE_THRESHOLD_DAYS` (default 60 days)
- Never-used panel listing tracked catalog pieces with no sessions yet
- Per-session rating (1–5 ⭐) and what you made
- **Check Idle** button (admin) to send a Discord reminder

---

## 🔔 Discord Notifications

Set `DISCORD_WEBHOOK_URL` to an [incoming webhook](https://support.discord.com/hc/en-us/articles/228383668) URL. All notification types use the same webhook:

| Trigger | Event |
|---|---|
| 🎯 Wishlist Check | MSRP ≤ target price for any wishlist item |
| 💲 MSRP Diff `--update` / web UI | Same as above, plus overall diff summary (CLI only) |
| 🔪 Sharpening Check Overdue | Any knife past the sharpening threshold |
| 🍳 Cookware Check Idle | Any piece past the cookware threshold |

---

## 📥 Import Format

Bulk-import ownership data from a CSV or XLSX file. Download a pre-formatted template from the **Import** page.

Recommended header order:

`name,sku,owned,color,availability,quantity purchased,quantity given away,category,edge,is_sku_unicorn,is_variant_unicorn,is_edge_unicorn,price`

### Common Columns

| Column | Description |
|---|---|
| `name` | Item name (must match or be new) |
| `sku` | Cutco model number |
| `owned` | `yes` / `no` / collector name |
| `color` | Handle color (or leave blank for Unknown) |
| `availability` | `public` / `rep` / `costco` / `non-catalog` (`blank` defaults to `public`) |
| `non_catalog` | Legacy alias for not public; unicorn flags also imply non-catalog |
| `quantity_purchased` | Whole-number ownership count |
| `quantity_given_away` | Whole-number ownership count |
| `category` | Product category |
| `edge` | `Straight`, `Double-D`, `Micro Double-D`, `Serrated`, `Micro-D`, `Tec Edge`, `N/A`, or `Unknown` |
| `is_sku_unicorn` | `yes` / `x` / `true` / `1` item-level unicorn flag |
| `is_variant_unicorn` | `yes` / `x` / `true` / `1` variant/color unicorn flag |
| `is_edge_unicorn` | `yes` / `x` / `true` / `1` edge/blade-type unicorn flag |
| `notes` | Free-text notes |

For XLSX imports, the app also recognizes `Owned?`, `status`, and `person` for older files, plus older auxiliary columns like `Price`, which is merged into notes. `quantity purchased` and `quantity given away` are imported as separate whole-number ownership fields, and rows with decimal values in those columns are rejected. `availability` is the canonical way to mark `public`, `rep only`, `Costco`, or `non-catalog` items, while `non_catalog` remains a legacy alias. Any unicorn flag also implies non-catalog. Legacy files may also include `is_color_unicorn`.

Import headers are matched case-insensitively, so lowercase headers are recommended only for consistency.

You can also store alternate SKUs on an item so imports can match the same product under a different vendor or legacy model number. During import preview, rows where the SKU or an alias already exists but the name differs are grouped into a collapsed `SKU or alias already exists` section so you can review naming differences before confirming.

## 🧩 Completion Reports

The app includes a standalone **Completion Gaps** page for rep-style follow-up lists. It defaults to the last collector you viewed, and you can switch to any collector or all collectors. The report shows public catalog SKUs the collector still does not own in any variant, and it can be viewed on screen or downloaded as CSV.

## 🧾 All SKU Completion Import

This separate import page is for rep-style completion lists, where you want to roll up individual SKUs and set SKUs into summed ownership totals for a person.

It accepts pasted spreadsheet rows or CSV uploads with:

`person,sku,quantity,note`

Set SKUs expand into member item SKUs, duplicate rows roll up, and missing item/set SKUs are skipped rather than created. The result page includes a history log and export buttons for the rolled-up totals and missing-SKU report.

Example:

```text
person,sku,quantity,note
Anthony Child,1726,1,ordered from rep
Anthony Child,326,1,from set
Anthony Child,326,2,ordered again
```

Set SKUs expand into member item SKUs, duplicate rows are summed, unknown SKUs are skipped, and existing ownership rows are updated by adding to `Quantity Purchased`.

---

## 🗺️ Navigation

| Page | URL | Access |
|---|---|---|
| 🏠 Dashboard | `/` | Public |
| 📋 Catalog | `/catalog` | Public |
| 🗂️ Sets | `/sets` | Public |
| 👥 People | `/people` | Public |
| 📊 Ownership Matrix | `/views/matrix` | Public |
| 🎯 Wishlist | `/wishlist` | Public |
| 📈 Collection Stats | `/stats` | Public |
| 🔪 Sharpening Log | `/sharpening` | Public |
| 🍳 Cookware Tracker | `/cookware` | Public |
| 🔪 Knife Task Log | `/tasks` | Public |
| 🛠️ Manage Tasks | `/tasks/manage` | Public |
| 🎯 Task Detail | `/tasks/manage/<id>` | Public |
| 📥 Import | `/import` | 🔒 Admin |
| 📤 Export CSV | `/export` (download endpoint: `/export/csv`) | Public |
| 🔄 Catalog Sync | `/catalog/sync` | 🔒 Admin |
| 💲 MSRP Diff | `/admin/msrp-diff` | 🔒 Admin |
| 🔑 Admin Login | `/admin/login` | Public |

---

## 🗄️ Database Schema

Eleven tables backed by SQLite. All migrations run automatically at startup.

| Table | Key Columns | Notes |
|---|---|---|
| `items` | `id`, `name`, `sku`, `category`, `edge_type`, `is_unicorn`, `in_catalog`, `cutco_url`, `msrp` | One row per catalog item |
| `item_variants` | `id`, `item_id`, `color`, `is_unicorn` | One row per handle color; `Unknown` is kept only when no real colors exist |
| `ownership` | `id`, `variant_id`, `person_id`, `status`, `target_price`, `quantity_purchased`, `quantity_given_away` | Links a person to a variant; status is `Owned`, `Wishlist`, `Sold`, or `Traded` |
| `people` | `id`, `name` | Collectors |
| `sets` | `id`, `name`, `sku` | Named Cutco sets |
| `item_sets` | `item_id`, `set_id`, `quantity` | Many-to-many join between items and sets; quantity tracks how many of an item a set includes |
| `sharpening_log` | `id`, `item_id`, `sharpened_on`, `method`, `notes` | One row per sharpening event |
| `cookware_sessions` | `id`, `item_id`, `used_on`, `made_item`, `rating`, `notes` | One row per cookware usage session |
| `knife_tasks` | `id`, `name`, `is_preset` | Task definitions (e.g. "Slicing bread"); 10 presets seeded on startup |
| `knife_task_log` | `id`, `item_id`, `task_id`, `logged_on`, `notes` | One row per knife-task usage event |
| `item_tasks` | `item_id`, `task_id` | Cutco-sourced suggested uses per item; populated by the Uses Sync |

---

## 🦄 Unicorn Tracking

A **unicorn** is any item, edge type, or variant that is rare, discontinued, or otherwise hard to find. Unicorns can be flagged at three levels:

- **Item level** — marks the entire item as a unicorn regardless of color (set via the catalog Edit form)
- **Edge level** — marks a specific edge / blade-type version of an item as a unicorn
- **Variant level** — marks a specific color variant as a unicorn (set via the variant Edit form or during import via the unicorn import columns)

The `any_unicorn` property on an item returns `true` if the item itself, its edge type, or any of its variants is flagged. The catalog filter and stats page both use this property, so any unicorn flag surfaces the item in unicorn searches.

---

## 💾 Backup

The entire database is a single SQLite file. For a simple consistent backup,
stop the service before copying it:

```bash
docker compose stop cutco-vault
cp ./data/cutco.db ./data/cutco.db.bak
docker compose start cutco-vault
```

For a backup without downtime, use Python's SQLite backup API inside the running
container (the image does not include the `sqlite3` command-line program):

```bash
docker compose exec -T cutco-vault sh -c 'exec gosu "$PUID:$PGID" python -c "import sqlite3; source=sqlite3.connect(\"/data/cutco.db\"); backup=sqlite3.connect(\"/data/cutco.db.bak\"); source.backup(backup); backup.close(); source.close()"'
```

Before deploying an authentication phase, keep a copy of both the database and
the current image tag. Schema migration v15 is additive, so rolling back only the
application image leaves older releases able to ignore the new tables. For a
complete rollback, stop the service, restore the pre-upgrade database copy,
restore the previous image tag, and start the service again. Creating the first
named account invalidates bootstrap-token sessions; resetting a password,
activating an account, or revoking sessions invalidates that user's existing
named sessions.

---

## ⚠️ Known Limitations

- **💲 MSRP scraping** — Price extraction relies on Cutco.com's current page structure (JSON-LD, Open Graph meta tags, and DOM patterns). A site redesign may reduce scraping success rates until extraction strategies are updated.
- **🔄 Catalog sync accuracy** — SKU extraction uses a six-strategy heuristic. Gift sets and bundle pages occasionally return incorrect SKUs; the `CATEGORY_OVERRIDES` dict in `constants.py` handles known exceptions.
- **🌐 Public catalog pages** — Product-facing catalog pages intentionally remain public. Collector data and mutation routes require a named local account or configured trusted-proxy identity; continue to use HTTPS and a trusted network, VPN, or hardened reverse proxy.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| 🐍 Backend | Python 3.13, Flask 3.1 |
| 🗄️ Database | SQLite (via SQLAlchemy) |
| 🚀 App Server | Gunicorn (4 workers) |
| 🕷️ Scraping | Requests, BeautifulSoup4, lxml |
| 📊 Charts | Chart.js (CDN, no extra dependency) |
| 📥 Excel Import | openpyxl |
| 🐳 Container | Docker (`python:3.13-slim`) |
| 🎨 Frontend | Jinja2 templates, vanilla CSS/JS |

---

## 🏗️ Architecture

Routes are split across Flask Blueprints for maintainability:

| Blueprint | Routes |
|---|---|
| `catalog` | `/catalog`, `/variants`, `/sets`, `/catalog/sync` |
| `people` | `/people`, `/ownership`, `/wishlist` |
| `logs` | `/sharpening`, `/cookware`, `/tasks`, `/tasks/manage` |
| `views` | `/views/matrix`, `/stats` |
| `data` | `/import`, `/export` |
| `admin` | `/admin/*`, `/api/variants` |

Shared logic lives in `models.py`, `helpers.py`, `scraping.py`, `msrp_helpers.py`, and `constants.py`. `app.py` is the thin factory that wires everything together.

---

## 💻 Development

```bash
# Install dependencies
pip install -r requirements.txt
pip install ruff

# Run locally
flask --app app:create_app run --debug

# Lint
ruff check app.py blueprints/ models.py helpers.py scraping.py msrp_helpers.py constants.py
```

The database is created automatically on first run. All schema migrations are applied at startup — no migration tool required.

---

## 📁 Data Storage

All persistent data lives in `/data/` inside the container — mount this as a volume:

```
/data/cutco.db        # SQLite database
/data/logs/           # Rotating log files (5 MB × 5 files)
/data/msrp_job.json   # MSRP diff job state (created on first run)
```

---

## 🏥 Health Check

```
GET /health   → 200 OK  {"status": "ok", "version": "x.y.z", "git_sha": "..."}
GET /version  → {"version": "x.y.z", "git_sha": "..."}
```

---

## 🔐 Security

See [SECURITY.md](SECURITY.md) for the vulnerability disclosure policy and hardening checklist.

---

## 📄 License

MIT

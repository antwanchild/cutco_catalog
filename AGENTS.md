# Repository guidance

## Project and code map

Cutco Vault is a Flask application for cataloging Cutco products and collectors'
collections. It uses SQLAlchemy with SQLite, Jinja2 templates, and vanilla CSS/JS.
There is no frontend package build.

- `app.py`: application factories, hooks, blueprint/CLI registration, and global routes.
- `blueprints/`: routes and workflow modules. Keep route-specific work in the
  corresponding blueprint; user administration lives in `blueprints/user_admin.py`.
- `models.py`, `extensions.py`: domain models and shared Flask extensions.
- `helpers.py`: shared authentication, authorization, CSRF, and application helpers.
- `constants.py`: environment configuration and catalog constants.
- `schema_migrations.py`, `startup.py`: versioned schema upgrades and startup initialization.
- `scraping.py`, `msrp_scrape.py`, `msrp_diff.py`: catalog and price extraction/comparison.
- `msrp_jobs.py`, `job_state.py`: background work and persisted job state.
- `templates/`, `static/`: UI templates and assets. Preserve responsive layouts,
  theme support, and contextual help when changing an existing flow.
- `tests/`: unittest suites and shared temporary-database fixtures.

Read the affected implementation and nearby tests before editing. Prefer existing
helpers and patterns; keep changes focused and avoid unrelated formatting churn.
Use `ruff.toml`, the Black configuration/defaults, and `pyrightconfig.json` as the
style and typing authorities. Follow existing docstring conventions.

## Local development and validation

Install development dependencies in an isolated environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

CI's Python version is declared in `.github/workflows/validation.yml`; the Docker
runtime version is declared separately in `Dockerfile`. Check both for runtime
or dependency changes rather than assuming they match.

For local development, use a disposable data directory and local authentication:

```bash
DATA_DIR="$PWD/data" AUTH_MODE=local flask --app app:create_app run --debug
```

Set a local `SECRET_KEY` and, when testing first-account setup, an
`INITIAL_SETUP_TOKEN` in the environment. Never commit credentials or populated
collector databases. See README.md for full setup and authentication configuration.

Validation commands used by CI:

```bash
ruff check .
black --check .
pyright
python -m unittest discover -s tests -p 'test_*.py'
```

Run relevant test modules while developing and the full suite for broad changes.
Add regression coverage for changed behavior, especially authorization, migrations,
imports, scraping, and background jobs. Documentation-only edits need link/path
and diff checks rather than application tests. Report what actually ran and any
limits; do not mark Docker/browser testing complete without running it.
CI uses `coverage run -m scripts.run_tests` to also produce coverage and timing
reports. Avoid committing generated reports from local validation.
For workflow changes, run `actionlint` when available and review permissions,
triggers, and shell quoting. The `docker` PR label opts into preview images after CI.

## Data and security invariants

- Extend the ordered migration registry in `schema_migrations.py` for schema
  changes; do not rewrite applied migrations. Cover both existing-database upgrades
  and fresh startup. Preserve collection data and document backup/rollback effects.
- Preserve transaction boundaries and rollback on failed writes. Keep plain and
  engraved ownership records distinct, including import/export quantities.
- Reuse existing identity and authorization helpers. Product browse pages are
  public; collector data and mutations require the appropriate authenticated role.
  Signed collection/gift links have explicit public sharing behavior to preserve.
- Keep CSRF protection, safe internal return URLs, and authentication-form cache
  controls. Never trust proxy identity headers outside the configured trust boundary.
- `INITIAL_SETUP_TOKEN` only guards first-administrator setup; it is not a login
  credential. Do not reintroduce shared `ADMIN_TOKEN` authentication.
- Preserve last-active-administrator protection and session revocation semantics.
  Keep passwords, tokens, and webhook URLs out of logs and audit events.
- Use mocked HTTP and disposable data in tests rather than live catalog scraping,
  production data, or real Discord notifications.

## Changelog, PRs, and releases

Follow `RELEASING.md` and `.github/PULL_REQUEST_TEMPLATE.md`. Every release-producing
PR needs a user-facing bullet under `CHANGELOG.md`'s `Unreleased` section, including
dependency updates. Use only relevant categories and explain required upgrade
steps for breaking configuration, authentication, or schema changes. Markdown-only
work can add a Docs entry for the next release.

Leave release versions, dates, and finalized sections to the release workflow.
Use Conventional Commit titles: `feat:` for new user capabilities, `fix:` for
corrections, and `feat!:` or a `BREAKING CHANGE:` footer for breaking changes.
Other non-documentation-only changes currently also produce patch releases.
Do not manually run `scripts/release_tag.sh` for validation: it commits and pushes
to master and creates a release tag. Use the disposable changelog preview described
in `RELEASING.md` instead.

When changing shared agent guidance, edit this file. Keep `CLAUDE.md` as the small
entry point that imports it, and keep README development instructions consistent.

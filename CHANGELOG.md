# Changelog

Notable changes to Cutco Vault are documented here, using the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

History below selectively covers recent releases and significant earlier changes,
using published release dates in UTC. Omitted versions and full earlier history
remain available in [GitHub Releases](https://github.com/antwanchild/cutco_catalog/releases).

## [Unreleased]

## [v3.2.0] - 2026-11-25

### Added

- Filter the import preview to rows missing a SKU from the sticky summary bar.
  The filter keeps existing import selections and reports hidden selected rows.

## [v3.1.1] - 2026-11-25

### Fixed

- Keep the import selection summary visible below the navigation while scrolling.

## [v3.1.0] - 2026-11-25

### Added

- Show a live summary of selected import rows by collection status, with separate
  counts for catalog-only rows, sets, and rows flagged as unicorns.

## [v3.0.46] - 2026-11-25

### Fixed

- Fix the Upgrade Check release lookup so the spreadsheet import fix can be
  published in the Docker image.

## [v3.0.45] - 2026-11-25

### Fixed

- Preserve Wishlist and other collection statuses in spreadsheet imports, even
  when assigning rows to a collector, and clarify the preview label.

## [v3.0.44] - 2026-11-25

### Fixed

- Save explicitly limited product colors under their actual color names and flag
  them as variant unicorns when confirming Variant Sync.

## [v3.0.43] - 2026-11-25

### Fixed

- Open the login page from **Sign In** in hybrid mode so local users can sign
  in over the direct LAN address and still choose Authentik on the proxied hostname.

## [v3.0.42] - 2026-11-25

### Changed

- Verify that release images preserve sample catalog and ownership data when
  upgrading from the previous published release.

## [v3.0.41] - 2026-11-24

### Fixed

- Show release-note section names as bold labels in Discord notifications and join
  wrapped bullet lines for cleaner spacing. Preserve full notes in GitHub releases.
- Explicitly select separate editor schemas for composite actions and workflows.

## [v3.0.40] - 2026-11-24

### Fixed

- Publish release tags from the approved commit without pushing changelog commits
  directly to protected master. Validate finalized release notes before tagging.

### Docs

- Add shared coding-agent guidance and a Claude entry point, and refresh local
  development commands and architecture references. Prompt PR authors to review
  related documentation and keep shared agent guidance current.

### Changed

- Publish curated release notes grouped by change type, with dated version history
  maintained in this changelog.

## [v3.0.39] - 2026-11-23

### Changed

- Update the development linter Ruff to 0.17.6. ([#109](https://github.com/antwanchild/cutco_catalog/pull/109))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.39)

## [v3.0.38] - 2026-11-17

### Changed

- Remove build-only pip from the runtime Docker image. ([#108](https://github.com/antwanchild/cutco_catalog/pull/108))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.38)

## [v3.0.35] - 2026-11-17

### Changed

- Include release details and failed-step links in Discord build and release alerts. ([#105](https://github.com/antwanchild/cutco_catalog/pull/105))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.35)

## [v3.0.34] - 2026-11-16

### Changed

- Update the Gunicorn application server to 27.0.1. ([#104](https://github.com/antwanchild/cutco_catalog/pull/104))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.34)

## [v3.0.31] - 2026-11-07

### Changed

- Standardize Discord build and release notifications. ([#100](https://github.com/antwanchild/cutco_catalog/pull/100))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.31)

## [v3.0.30] - 2026-11-07

### Fixed

- Make pull-request preview images wait for CI and repair preview-image cleanup. ([#99](https://github.com/antwanchild/cutco_catalog/pull/99))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.30)

## [v3.0.27] - 2026-11-04

### Changed

- Update the Docker Python base image to 3.15.2-slim-bookworm. ([#96](https://github.com/antwanchild/cutco_catalog/pull/96))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.27)

## [v3.0.25] - 2026-10-26

### Changed

- Update the Docker Python base image from the 3.15 release candidate to 3.15.1-slim-bookworm. ([#93](https://github.com/antwanchild/cutco_catalog/pull/93))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.25)

## [v3.0.22] - 2026-10-18

### Fixed

- Expose the release commit SHA in Docker metadata for Dockwatch. ([#91](https://github.com/antwanchild/cutco_catalog/pull/91))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.22)

## [v3.0.21] - 2026-10-18

### Fixed

- Publish Docker images with a commit SHA tag. ([#90](https://github.com/antwanchild/cutco_catalog/pull/90))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.21)

## [v3.0.13] - 2026-09-29

### Removed

- Remove retired campaign and stale catalog references. ([#82](https://github.com/antwanchild/cutco_catalog/pull/82))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.13)

## [v3.0.12] - 2026-09-29

### Fixed

- Persist completed Authentik sign-ins in a Cutco session so public pages show the appropriate account navigation and preserve the return destination. ([#81](https://github.com/antwanchild/cutco_catalog/pull/81))

**Upgrade:** Custom proxy routing must protect `/admin/auth/proxy-complete`
with Authentik forward-auth. Keep this callback out of the local-login bypass router.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.12)

## [v3.0.11] - 2026-09-29

### Fixed

- Start the Authentik sign-in flow in proxy and hybrid modes, preserve safe return destinations, and retain local recovery login. ([#74](https://github.com/antwanchild/cutco_catalog/pull/74))

**Configuration:** Use `AUTH_MODE=local` for direct localhost or LAN access;
proxy and hybrid modes expect a configured trusted reverse proxy.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.11)

## [v3.0.1] - 2026-07-17

### Fixed

- Prevent CSRF failures caused by cached local login pages. ([#69](https://github.com/antwanchild/cutco_catalog/pull/69))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.1)

## [v3.0.0] - 2026-07-17

### Removed

- Remove shared `ADMIN_TOKEN` authentication and legacy token-admin sessions. ([#68](https://github.com/antwanchild/cutco_catalog/pull/68))

### Upgrade notes

- Verify a working named local or proxy administrator before upgrading. Remove
  `ADMIN_TOKEN` from deployment configuration; it no longer grants login or setup access.
- For a fresh local or hybrid installation, set a long random
  `INITIAL_SETUP_TOKEN`, create the first administrator at `/setup`, then remove
  the token. The setup token does not grant an administrator session.
- If account access has not been verified, roll back the container image. This
  release does not change the database schema.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v3.0.0)

## [v2.0.0] - 2026-07-17

### Changed

- Resolve trusted-proxy identities to persistent user accounts and add explicit local, proxy, and hybrid authentication modes. ([#66](https://github.com/antwanchild/cutco_catalog/pull/66))

### Upgrade notes

- Back up the database and pre-provision a proxy administrator before switching
  an existing deployment to proxy or hybrid authentication. Proxy access now
  requires a stable external subject and a database-backed account.
- Configure `AUTH_MODE` and `TRUSTED_AUTH_SUBJECT_HEADER` for the deployment.
  Normal-user auto-provisioning and proxy-group role synchronization are opt-in.
- Startup migration v16 adds a unique index for non-null proxy subjects. Resolve
  duplicate subjects before the migration can complete.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v2.0.0)

## [v1.106.0] - 2026-07-16

### Added

- Add administrator web management for local accounts, roles, account activation, temporary passwords, and session revocation. ([#65](https://github.com/antwanchild/cutco_catalog/pull/65))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.106.0)

## [v1.105.0] - 2026-07-16

### Added

- Add named local login, first-administrator setup, password changes, CSRF-protected logout, and `flask users` recovery commands. ([#64](https://github.com/antwanchild/cutco_catalog/pull/64))

**Upgrade:** Startup migration v15 adds the first-administrator setup claim.
Creating the first named account disables legacy token login and existing token
sessions. Verify the new account before ending the setup session. This release
predates `INITIAL_SETUP_TOKEN`; its original setup flow used the bootstrap admin token.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.105.0)

## [v1.103.0] - 2026-07-16

### Added

- Add the named-user data foundation, password hashing, roles, revocable sessions, and audit-user linkage without changing the active login flow. ([#62](https://github.com/antwanchild/cutco_catalog/pull/62))

**Database:** Additive migration v14 creates the user table and audit-user linkage.
Back up the database before upgrading; this release leaves existing authentication behavior active.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.103.0)

## [v1.102.12] - 2026-07-15

### Added

- Show a live progress summary during variant sync. ([#60](https://github.com/antwanchild/cutco_catalog/pull/60))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.102.12)

## [v1.102.11] - 2026-07-14

### Fixed

- Correct set expansion in the catalog sync preview. ([#55](https://github.com/antwanchild/cutco_catalog/pull/55))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.102.11)

## [v1.102.10] - 2026-07-13

### Added

- Add an Engrave action that can split selected owned copies into an engraved entry while preserving remaining plain copies. ([#59](https://github.com/antwanchild/cutco_catalog/pull/59))

### Fixed

- Keep plain and engraved ownership entries distinct during collection imports.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.102.10)

## [v1.102.0] - 2026-07-04

### Added

- Track plain and engraved copies separately in ownership, collection views, and import/export data. ([#41](https://github.com/antwanchild/cutco_catalog/pull/41))

**Import/export:** Engraving metadata includes `copy_type`, `engraved`,
`engraving_text`, and `engraving_notes`. This release adds ownership schema fields.

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.102.0)

## [v1.101.25] - 2026-07-03

### Fixed

- Prevent catalog sync background jobs from stalling. ([#38](https://github.com/antwanchild/cutco_catalog/pull/38))

[Release details](https://github.com/antwanchild/cutco_catalog/releases/tag/v1.101.25)

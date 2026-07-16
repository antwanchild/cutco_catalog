# User Authentication and `ADMIN_TOKEN` Retirement Plan

## Status

- Type: proposed breaking feature (`!feat`)
- Scope: planning only; this document does not change runtime behavior
- Target: a production-ready account system that works with or without an
  authentication proxy

## Summary

Replace the shared `ADMIN_TOKEN` login with named user accounts and role-based
authorization. Local username/password login must work as a standalone option.
Trusted reverse-proxy identity remains supported for deployments using Authentik,
Authelia, or a similar provider.

The application will support three modes:

| Mode | Identity source | Intended use |
|---|---|---|
| `local` | Application username and password | Standalone installations |
| `proxy` | Trusted reverse-proxy headers | Centrally managed authentication |
| `hybrid` | Local accounts and trusted proxy headers | Migration, recovery, or mixed access |

`hybrid` is the recommended default during migration. New installations may
select a mode explicitly during setup.

## Goals

- Give every private-page visitor a stable, named identity.
- Support local authentication without requiring an external service.
- Provide `user` and `admin` roles, with authorization enforced server-side.
- Let administrators create, disable, reactivate, and reset local users.
- Attribute audited changes to a stable user identity.
- Prevent account enumeration, brute-force login, session fixation, and unsafe
  password storage.
- Retire `ADMIN_TOKEN` without locking out existing installations.
- Preserve public catalog and signed-share behavior.

## Non-goals for the first release

- Public self-registration.
- Email delivery or email-based password recovery.
- Social login or direct OAuth/OIDC integration. Proxy mode remains the external
  identity integration point.
- Fine-grained permissions beyond the initial `user` and `admin` roles.
- Making collector `Person` records equivalent to login accounts. A person is
  catalog data; a user is an application identity.
- Multi-tenant data isolation.

## Proposed data model

Add a `users` table:

| Column | Notes |
|---|---|
| `id` | Integer primary key |
| `username` | Required, unique after case normalization |
| `display_name` | Optional human-readable label |
| `password_hash` | Nullable; present for local login identities |
| `role` | Required enum-like value: `user` or `admin` |
| `auth_source` | `local` or `proxy` |
| `external_subject` | Nullable stable proxy identity; unique with `auth_source` |
| `is_active` | Disabled accounts cannot start or continue sessions |
| `must_change_password` | Forces a local password change after an admin reset |
| `session_version` | Incrementing integer used to revoke existing sessions |
| `last_login_at` | Nullable UTC timestamp |
| `created_at` | UTC timestamp |
| `updated_at` | UTC timestamp |

Initial constraints:

- Normalize usernames with `strip()` and `casefold()` before uniqueness checks.
- Store a separate display value if original capitalization should be retained.
- Never store plaintext or reversibly encrypted passwords.
- Use Werkzeug's maintained password hash helpers unless a dedicated password
  library is deliberately added and maintained.
- Prevent disabling, deleting, or demoting the last active administrator.
- Prefer deactivation over deletion so audit history remains meaningful.

`ActivityEvent.actor` can continue to render a readable label, but new events
should also gain a nullable `actor_user_id` foreign key. Retaining the label as a
snapshot keeps historical records understandable after a username changes or an
account is deactivated.

## Identity and authorization behavior

Introduce one request-level identity API and route all access checks through it:

- `current_user()` returns the resolved active `User` or `None`.
- `is_authenticated_user()` becomes `current_user() is not None`.
- `is_admin()` checks the resolved user's role.
- `user_required` and `admin_required` remain the route-level enforcement points.
- Templates receive the same resolved identity used by route authorization.

Do not trust a role stored only in the session. Store the user ID and session
version, then load the current role and active state from the database on each
request. This makes deactivation, demotion, and session revocation effective
immediately.

### Local authentication

- Login accepts username and password and returns a generic error for all
  failures.
- Regenerate authentication state on successful login to prevent session
  fixation while retaining a valid CSRF strategy.
- Apply the existing login rate limit and add tests for both username and source
  address throttling behavior.
- Logout is a CSRF-protected `POST`, not a state-changing `GET`.
- Users can change their own password after confirming the current password.
- Admins can issue a temporary password and require it to be changed at next
  login. No email reset flow is required initially.
- Successful password changes and admin resets increment `session_version`.

### Proxy authentication

- Proxy headers are accepted only when proxy authentication is enabled by
  configuration and the deployment strips client-supplied copies of those
  headers.
- Resolve a proxy identity by a stable configured subject/username header.
- Use explicit provisioning policy:
  - `preprovisioned`: an administrator must create/link the account first.
  - `auto`: create an active `user` account on first verified proxy request.
- Default auto-provisioned accounts to `user`, never `admin`.
- Map configured proxy admin groups to the `admin` role only when explicitly
  enabled. Record role changes caused by group mapping in the audit log.
- Do not persist a proxy-derived admin flag in the session.

### Hybrid conflict rules

- Local and proxy identities are separate unless explicitly linked.
- Never link accounts based solely on matching display names.
- A username collision produces an actionable error instead of silently merging
  identities.
- Local login remains available at a stable route so proxy outages do not make
  hybrid deployments unrecoverable.

## Configuration

Add or replace configuration along these lines:

| Variable | Proposed default | Purpose |
|---|---|---|
| `AUTH_MODE` | `hybrid` during migration | `local`, `proxy`, or `hybrid` |
| `PROXY_AUTH_AUTO_PROVISION` | `false` | Allow creation of basic users from trusted headers |
| `TRUSTED_AUTH_SUBJECT_HEADER` | same as username header | Stable external identity key |
| `TRUSTED_AUTH_DISPLAY_NAME_HEADER` | empty | Optional user display name |
| `TRUSTED_AUTH_SYNC_ADMIN_ROLE` | `false` | Allow configured groups to update admin role |

Existing `TRUSTED_AUTH_*` header and group settings remain supported. Document
that the application cannot distinguish forged headers unless a trusted proxy
removes incoming client values and injects its own.

`ADMIN_SESSION_SECONDS` should become `SESSION_SECONDS`, with the old name
accepted for one deprecation cycle.

## Initial setup and recovery

### New installations

If no users exist:

1. Normal private and admin routes redirect to `/setup`.
2. `/setup` is available only until the first administrator is successfully
   committed.
3. The form creates one local admin with a strong password.
4. Setup completion is audited and permanently closes web setup.

For unattended deployments, add a one-shot CLI command such as:

```text
flask users create-admin --username <name>
```

The password should be prompted securely or supplied through a documented
one-time secret mechanism, not exposed as a command-line argument.

### Recovery

Provide CLI commands that work from inside the application container:

- `flask users list`
- `flask users create-admin`
- `flask users reset-password <username>`
- `flask users activate <username>`
- `flask users revoke-sessions <username>`

Recovery commands and their operational requirements must be documented before
the token is removed.

## Migration away from `ADMIN_TOKEN`

Use a staged rollout to avoid lockouts:

### Release A: account foundation

- Add schema, identity helpers, local login, setup flow, CLI recovery, and user
  administration.
- Default existing deployments with `ADMIN_TOKEN` configured to migration-safe
  hybrid behavior.
- Permit the token only to bootstrap the first named admin account.
- Show a prominent admin warning while token bootstrap remains enabled.
- Do not allow token authentication after the first admin is created.

### Release B: deprecation enforcement

- Ignore `ADMIN_TOKEN` when at least one user exists.
- Emit a clear startup warning when the obsolete variable remains configured.
- Update Docker/README examples to use account setup instead of a token.
- Rename session lifetime configuration with backward compatibility.

### Release C: breaking removal

- Remove token login code and the `ADMIN_TOKEN` constant.
- Remove the production startup check tied to the default token.
- Remove obsolete tests, documentation, and deployment examples.
- Call out the required setup/recovery procedure in release notes.

Database migrations must be additive through Releases A and B. Do not rewrite or
delete existing catalog, person, ownership, or audit data.

## User interface

Add these pages or flows:

- Unified login page that presents only the methods enabled by `AUTH_MODE`.
- First-admin setup page.
- Current-user menu with identity, role, password change, and `POST` logout.
- Admin user list with status, role, source, and last-login information.
- Admin create/edit, disable/reactivate, reset-password, and revoke-session
  actions.
- Clear explanation that proxy-sourced passwords are managed externally.
- Confirmation and last-admin safeguards for destructive role/status changes.

Avoid exposing whether a submitted username exists on public login responses.

## Implementation phases

### 1. Schema and domain layer

- Add `User` and the audit foreign key.
- Add schema migrations compatible with supported SQLite and configured SQL
  databases.
- Add username normalization, password hashing, account-state, and last-admin
  invariants.
- Add unit tests for the model and migrations.

### 2. Request identity and authorization

- Centralize local and proxy identity resolution.
- Replace direct `session["is_admin"]` reads/writes throughout application code.
- Keep route decorators as the policy boundary and inventory routes for missing or
  inconsistent protection.
- Update audit actor resolution to use the named identity.

### 3. Local login, setup, and recovery

- Build login/logout/password-change flows.
- Build first-admin setup and CLI recovery commands.
- Add rate-limit, CSRF, inactive-user, session-revocation, and redirect-safety
  tests.

### 4. User administration

- Add admin-only user management routes and templates.
- Enforce last-admin and self-lockout protections in the domain layer, not only
  in forms.
- Audit account creation, activation, deactivation, role changes, password resets,
  and session revocations without recording secrets.

### 5. Proxy and hybrid behavior

- Resolve/provision proxy identities according to configuration.
- Implement explicit account linking and collision handling.
- Test role synchronization, header absence, forged-header deployment warnings,
  and local fallback.

### 6. Token deprecation and documentation

- Implement the staged `ADMIN_TOKEN` bootstrap and removal behavior.
- Update README, SECURITY, Docker examples, environment tables, and release notes.
- Add an upgrade checklist and rollback notes.

## Test matrix

At minimum, automated coverage must include:

- Fresh local setup creates exactly one admin and cannot be reopened.
- Existing database migration preserves all existing records.
- Valid/invalid local login, generic error messages, and rate limiting.
- Password change/reset and forced-change behavior.
- Disabled, demoted, or session-revoked users lose access immediately.
- A normal user cannot reach any admin or mutation route reserved for admins.
- The last active admin cannot be disabled, deleted, or demoted.
- Local, proxy, and hybrid mode behavior.
- Proxy auto-provisioning defaults to `user`.
- Proxy group mapping cannot grant admin unless explicitly enabled.
- Username/external-subject collisions never merge accounts implicitly.
- CSRF protection on login-adjacent state changes and logout.
- Audit records identify actors but never contain passwords or password hashes.
- Signed gift and collection-card links retain current public behavior.
- Production startup validation reflects the new configuration.

Run the complete existing test suite as well as lint, formatting, and static type
checks because authentication helpers are used across most blueprints.

## Release acceptance criteria

The token-removal release is complete only when:

- A clean installation can securely create its first local administrator.
- An installation can run entirely without Authentik or another proxy.
- Proxy-only and hybrid installations have documented, tested behavior.
- Every protected route authorizes against a resolved active user and current
  role.
- Administrators have both web and CLI account recovery paths.
- Existing installations have a tested, documented upgrade path that does not
  require deleting or recreating their database.
- `ADMIN_TOKEN`, token login UI, and `session["is_admin"]` no longer exist in
  runtime code.
- Security documentation covers password handling, trusted-header boundaries,
  session revocation, backup, and recovery.

## Open decisions before implementation

1. Should proxy accounts be preprovisioned by default? Recommendation: yes;
   require an explicit opt-in for auto-provisioning.
2. Should proxy groups continuously synchronize roles or only set the initial
   role? Recommendation: continuous synchronization only when
   `TRUSTED_AUTH_SYNC_ADMIN_ROLE=true`.
3. Should local usernames be renameable? Recommendation: not in the first
   release; allow changing `display_name` instead.
4. Should admins set temporary passwords or generate one-time reset links?
   Recommendation: temporary passwords plus forced change for the first release.
5. Should Release A default to `hybrid` globally or infer a compatibility mode
   only for upgraded deployments? Recommendation: infer migration compatibility
   for existing installations and require an explicit mode for fresh production
   installations.

# Security Policy

## Supported Versions

Only the latest release is actively maintained and receives security fixes.

| Version | Supported |
|---------|-----------|
| Latest  | ✅ |
| Older   | ❌ |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report them privately via [GitHub Security Advisories](https://github.com/antwanchild/cutco_catalog/security/advisories/new).

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix (optional)

You can expect an acknowledgement within a few days. If confirmed, a fix will be prioritised and a patched release issued.

## Security Model

Cutco Vault is designed for **self-hosted, personal or small-group use** behind a trusted network, VPN, or reverse proxy. Its security posture reflects this:

- **Named local accounts** — standalone installations can create a local administrator through the one-time setup flow. Passwords are stored only as Werkzeug password hashes, and account roles/status are loaded from the database on each request.
- **Public browse, private collectors** — product pages stay public; collector, import/export, sync, and mutation routes are private.
- **Local, proxy, or hybrid authentication** — `AUTH_MODE` can disable proxy trust entirely, require a trusted proxy, or retain local password fallback. Proxy identities resolve persistent accounts by an immutable subject, not by a mutable display name.
- **Trusted-header boundary** — the reverse proxy must strip client-supplied identity headers before injecting authenticated values. Pre-provisioning is the default; auto-provisioning is opt-in and initially grants only the user role. Proxy group-to-admin synchronization is separately opt-in and audited.
- **Initial setup + signed sessions** — `INITIAL_SETUP_TOKEN` authorizes creation of the first named local administrator only while no user exists. The setup page permanently closes after that account is created; it never grants a shared administrator session. Once setup completes, local username/password or configured proxy authentication is required. Proxy-admin users can skip the local form.
- **Password and session safety** — local login failures use a generic response and a timing-safe dummy hash path. Password changes require the current password, revoke other sessions, and logout is a CSRF-protected `POST`.
- **Offline recovery boundary** — trusted operators can list users, create an administrator, reset a local password, reactivate an account, or revoke sessions through `flask users`. Passwords are hidden interactive prompts, resets force a change at next login, and recovery actions are audited without credential material. Container-shell access is therefore administrator-equivalent and must be restricted.
- **User administration** — administrators can create local or proxy accounts, explicitly link a proxy subject to a local account, change roles and activation state, issue forced-change temporary passwords, and revoke sessions. Username matches never silently link identities. Named administrators cannot use the web UI to demote, deactivate, reset, or revoke themselves, and the final active administrator is protected by a database-backed domain invariant. Proxy passwords are never set by the application.
- **Session secret** — the `SECRET_KEY` environment variable protects Flask sessions and signed share tokens. Use a long random value and keep it consistent across restarts (changing it invalidates all active sessions and share links).
- **Production startup guard** — in production mode, the app refuses to start with the default `SECRET_KEY` unless explicitly bypassed. It warns when an obsolete `ADMIN_TOKEN` or no-longer-needed `INITIAL_SETUP_TOKEN` remains configured.
- **Write protection** — mutating routes are private; public users can only view product-facing pages and signed share links.

## Hardening Checklist

- [ ] Before the first local web setup, set `INITIAL_SETUP_TOKEN` to a strong random value (e.g. `openssl rand -hex 32`)
- [ ] Complete `/setup`, store the local administrator password securely, then remove `INITIAL_SETUP_TOKEN`
- [ ] Verify `flask --app app:create_app users list` works from the application container and document who may run recovery commands
- [ ] Set `SECRET_KEY` to a strong random value
- [ ] Set `SESSION_COOKIE_SECURE=true` when served over HTTPS
- [ ] Choose `AUTH_MODE=local`, `proxy`, or `hybrid`; use `local` when no identity proxy is deployed
- [ ] For proxy/hybrid mode, forward both username and a stable subject/UID and configure their trusted header names
- [ ] Ensure the reverse proxy strips all inbound trusted identity headers before injecting its own values
- [ ] Pre-provision proxy users, or deliberately enable `PROXY_AUTH_AUTO_PROVISION=true`
- [ ] Enable `TRUSTED_AUTH_SYNC_ADMIN_ROLE` only if provider groups should control admin roles, then verify the configured admin groups
- [ ] Do **not** set `ALLOW_INSECURE_DEFAULTS` in production (dev/temporary bypass only)
- [ ] Run behind a reverse proxy (nginx, Caddy, Traefik) with HTTPS
- [ ] Restrict network access to trusted hosts or a VPN
- [ ] Mount `/data` as a volume and back it up regularly
- [ ] Review `DISCORD_WEBHOOK_URL` permissions — it only needs `Send Messages`

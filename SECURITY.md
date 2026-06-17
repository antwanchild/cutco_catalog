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

- **No app-native user accounts** — the app does not create or manage end-user logins in this phase.
- **Public browse, private collectors** — product pages stay public; collector, import/export, sync, and mutation routes are private.
- **Traefik + authentik friendly** — if you terminate auth at the edge, forward a trusted username header and configure `TRUSTED_AUTH_USERNAME_HEADER` to match. If your proxy forwards groups, set `TRUSTED_AUTH_GROUPS_HEADER` and `TRUSTED_AUTH_ADMIN_GROUPS` so proxy admins can be recognized too. The legacy `AUTHENTIK_USERNAME_HEADER` / `AUTHENTIK_GROUPS_HEADER` settings still work if you already used them, and `AUTHELIA_USERNAME_HEADER` / `AUTHELIA_GROUPS_HEADER` are supported as fallbacks.
- **Admin login + signed session** — admin login is gated by the `ADMIN_TOKEN` environment variable, then elevated actions require a signed Flask admin session. Proxy auth can also mark a request as admin when the configured admin group is present. Use a strong, unique token either way.
- **Session secret** — the `SECRET_KEY` environment variable protects Flask sessions and signed share tokens. Use a long random value and keep it consistent across restarts (changing it invalidates all active sessions and share links).
- **Production startup guard** — in production mode, the app refuses to start with default `ADMIN_TOKEN` / `SECRET_KEY` values unless explicitly bypassed.
- **Write protection** — mutating routes are private; public users can only view product-facing pages and signed share links.

## Hardening Checklist

- [ ] Set `ADMIN_TOKEN` to a strong random value (e.g. `openssl rand -hex 32`)
- [ ] Set `SECRET_KEY` to a strong random value
- [ ] Set `SESSION_COOKIE_SECURE=true` when served over HTTPS
- [ ] If using Traefik + authentik or Authelia, forward the authenticated username into `X-Forwarded-User`/`Remote-User` or your chosen trusted header
- [ ] If you want proxy-based admin access, forward group membership and set `TRUSTED_AUTH_GROUPS_HEADER` plus `TRUSTED_AUTH_ADMIN_GROUPS`
- [ ] Do **not** set `ALLOW_INSECURE_DEFAULTS` in production (dev/temporary bypass only)
- [ ] Run behind a reverse proxy (nginx, Caddy, Traefik) with HTTPS
- [ ] Restrict network access to trusted hosts or a VPN
- [ ] Mount `/data` as a volume and back it up regularly
- [ ] Review `DISCORD_WEBHOOK_URL` permissions — it only needs `Send Messages`

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

- **No user accounts** — all non-admin pages are publicly accessible to anyone who can reach the host. Do not expose this service directly to the internet without a reverse proxy or network-level access control.
- **Admin login + signed session** — admin login is gated by the `ADMIN_TOKEN` environment variable, then elevated actions require a signed Flask admin session. Use a strong, unique token.
- **Session secret** — the `SECRET_KEY` environment variable protects Flask sessions and signed share tokens. Use a long random value and keep it consistent across restarts (changing it invalidates all active sessions and share links).
- **Production startup guard** — in production mode, the app refuses to start with default `ADMIN_TOKEN` / `SECRET_KEY` values unless explicitly bypassed.
- **Write protection** — mutating routes are admin-restricted; public users are read-only.

## Hardening Checklist

- [ ] Set `ADMIN_TOKEN` to a strong random value (e.g. `openssl rand -hex 32`)
- [ ] Set `SECRET_KEY` to a strong random value
- [ ] Set `SESSION_COOKIE_SECURE=true` when served over HTTPS
- [ ] Do **not** set `ALLOW_INSECURE_DEFAULTS` in production (dev/temporary bypass only)
- [ ] Run behind a reverse proxy (nginx, Caddy, Traefik) with HTTPS
- [ ] Restrict network access to trusted hosts or a VPN
- [ ] Mount `/data` as a volume and back it up regularly
- [ ] Review `DISCORD_WEBHOOK_URL` permissions — it only needs `Send Messages`

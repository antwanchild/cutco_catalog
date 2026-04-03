import hmac
import logging
import secrets

import requests
from flask import abort, request, session

from constants import ADMIN_TOKEN, DISCORD_WEBHOOK_URL
from models import Ownership

logger = logging.getLogger(__name__)


def is_admin() -> bool:
    return request.cookies.get("admin_token") == ADMIN_TOKEN


# ── CSRF ──────────────────────────────────────────────────────────────────────

def _csrf_token() -> str:
    """Return (and lazily create) a per-session CSRF token."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def validate_csrf() -> None:
    """Abort 403 if the submitted CSRF token doesn't match the session token."""
    token = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(token, expected):
        abort(403)


def _notify_discord(message: str) -> bool:
    """POST a message to the configured Discord webhook. Returns True on success."""
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        resp.raise_for_status()
        logger.info("Discord notification sent (%d chars)", len(message))
        return True
    except Exception as exc:
        logger.warning("Discord notification failed: %s", exc)
        return False


def check_wishlist_targets() -> list[dict]:
    """Return wishlist entries where current MSRP is at or below the target price."""
    hits = []
    entries = (Ownership.query
               .filter_by(status="Wishlist")
               .filter(Ownership.target_price.isnot(None))
               .all())
    for entry in entries:
        msrp = entry.variant.item.msrp
        if msrp is not None and msrp <= entry.target_price:
            hits.append(dict(
                person  = entry.person.name,
                item    = entry.variant.item.name,
                sku     = entry.variant.item.sku,
                target  = entry.target_price,
                msrp    = msrp,
                savings = entry.target_price - msrp,
            ))
    return hits

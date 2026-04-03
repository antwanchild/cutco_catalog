import base64
import hashlib
import hmac
import logging
import secrets

import requests
from flask import abort, current_app, request, session

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


# ── Gift list tokens ──────────────────────────────────────────────────────────

def _gift_token(set_id: int, person_id: int) -> str:
    """Generate a signed URL-safe token encoding set_id and person_id."""
    payload = base64.urlsafe_b64encode(f"{set_id}:{person_id}".encode()).decode().rstrip("=")
    sig = hmac.new(
        current_app.secret_key.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{payload}.{sig}"


def _verify_gift_token(token: str) -> tuple[int, int] | None:
    """Verify token and return (set_id, person_id), or None if invalid."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            current_app.secret_key.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode()
        set_id, person_id = decoded.split(":", 1)
        return int(set_id), int(person_id)
    except Exception:
        return None


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

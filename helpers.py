import logging

import requests
from flask import request

from constants import ADMIN_TOKEN, DISCORD_WEBHOOK_URL
from extensions import db
from models import Ownership

logger = logging.getLogger(__name__)


def is_admin() -> bool:
    return request.cookies.get("admin_token") == ADMIN_TOKEN


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

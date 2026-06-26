"""Shared authentication, CSRF, persistence, and notification helpers."""

import base64
import hashlib
import hmac
import logging
import secrets
from functools import wraps

import requests
from flask import abort, current_app, flash, redirect, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError

from constants import (
    DISCORD_WEBHOOK_URL,
    TRUSTED_AUTH_ADMIN_GROUPS,
    TRUSTED_AUTH_GROUPS_HEADER,
    TRUSTED_AUTH_USERNAME_HEADER,
)
from models import Ownership

logger = logging.getLogger(__name__)


def _request_header_value(header_name: str) -> str:
    """Return a request header value using a case-insensitive name match."""
    target = header_name.strip().casefold()
    if not target:
        return ""
    for key, value in request.headers.items():
        if key.casefold() == target:
            return value.strip()
    return ""


def _trusted_header_debug_names() -> list[str]:
    """Return auth-related header names present on the current request."""
    interesting = []
    for key, _value in request.headers.items():
        lowered = key.casefold()
        if any(
            token in lowered
            for token in (
                "authentik",
                "forwarded-user",
                "forwarded-groups",
                "remote-user",
            )
        ):
            interesting.append(key)
    return sorted(set(interesting), key=str.casefold)


def is_trusted_proxy_authenticated() -> bool:
    """Return whether the current request came through a trusted auth proxy."""
    header_value = _request_header_value(TRUSTED_AUTH_USERNAME_HEADER)
    authenticated = bool(header_value)
    if not authenticated and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Trusted proxy auth header missing or empty (configured=%r, present=%s)",
            TRUSTED_AUTH_USERNAME_HEADER,
            _trusted_header_debug_names(),
        )
    return authenticated


def _trusted_proxy_groups() -> set[str]:
    """Return normalized group names reported by the trusted auth proxy."""
    raw_groups = _request_header_value(TRUSTED_AUTH_GROUPS_HEADER)
    if not raw_groups:
        return set()
    groups = set()
    for chunk in raw_groups.replace("|", ",").replace(";", ",").split(","):
        cleaned = chunk.strip()
        if cleaned:
            groups.add(cleaned.casefold())
    return groups


def is_admin() -> bool:
    """Return whether the current request has admin access."""
    return session.get("is_admin") is True or is_trusted_proxy_admin()


def is_authenticated_user() -> bool:
    """Return whether the current request is authenticated for private user pages."""
    return is_admin() or is_trusted_proxy_authenticated()


def is_trusted_proxy_admin() -> bool:
    """Return whether the trusted auth proxy says this user should be admin."""
    if not is_trusted_proxy_authenticated() or not TRUSTED_AUTH_ADMIN_GROUPS:
        return False
    allowed_groups = {group.casefold() for group in TRUSTED_AUTH_ADMIN_GROUPS}
    return bool(_trusted_proxy_groups() & allowed_groups)


def user_required(fn):
    """Require a private-user session or trusted auth proxy for a route."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not is_authenticated_user():
            flash("Authentication required.", "error")
            return redirect(url_for("admin.admin_login"))
        return fn(*args, **kwargs)

    return _wrapped


def admin_required(fn):
    """Require an admin session for a route."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("admin.admin_login"))
        return fn(*args, **kwargs)

    return _wrapped


# Backwards compatibility for older references.
is_authentik_authenticated = is_trusted_proxy_authenticated


# ── DB helpers ────────────────────────────────────────────────────────────────


def db_commit(
    session, *, error_msg: str = "Could not save changes — please try again."
) -> bool:
    """Commit the session. On failure, roll back and flash an error. Returns True on success."""
    try:
        session.commit()
        return True
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error("DB commit failed: %s", exc)
        flash(error_msg, "error")
        return False


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


def top_count_rows(
    counts: dict[str, int], *, limit: int = 8, sort_by_name: bool = False
) -> list[dict[str, int | str]]:
    """Return the most common counted values as ``{"color", "count"}`` rows."""
    if sort_by_name:
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    else:
        ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"color": color, "count": count} for color, count in ordered[:limit]]


# ── Gift list tokens ──────────────────────────────────────────────────────────


def _secret_key_bytes() -> bytes:
    """Return the Flask secret key as bytes for HMAC signing."""
    secret_key = current_app.secret_key
    if secret_key is None:
        raise RuntimeError("SECRET_KEY must be configured to use signed tokens.")
    if isinstance(secret_key, str):
        return secret_key.encode("utf-8")
    return bytes(secret_key)


def _gift_token(set_id: int, person_id: int) -> str:
    """Generate a signed URL-safe token encoding set_id and person_id."""
    payload = (
        base64.urlsafe_b64encode(f"{set_id}:{person_id}".encode()).decode().rstrip("=")
    )
    sig = hmac.new(
        _secret_key_bytes(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{payload}.{sig}"


def _verify_gift_token(token: str) -> tuple[int, int] | None:
    """Verify token and return (set_id, person_id), or None if invalid."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            _secret_key_bytes(),
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


# ── Collection card tokens ────────────────────────────────────────────────────


def _collection_token(person_id: int) -> str:
    """Generate a signed URL-safe token encoding person_id."""
    payload = base64.urlsafe_b64encode(f"c:{person_id}".encode()).decode().rstrip("=")
    sig = hmac.new(
        _secret_key_bytes(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{payload}.{sig}"


def _verify_collection_token(token: str) -> int | None:
    """Verify token and return person_id, or None if invalid."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            _secret_key_bytes(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode()
        prefix, person_id = decoded.split(":", 1)
        if prefix != "c":
            return None
        return int(person_id)
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
    entries = (
        Ownership.query.filter_by(status="Wishlist")
        .filter(Ownership.target_price.isnot(None))
        .all()
    )
    for entry in entries:
        msrp = entry.variant.item.msrp
        if msrp is not None and msrp <= entry.target_price:
            hits.append(
                dict(
                    person=entry.person.name,
                    item=entry.variant.item.name,
                    sku=entry.variant.item.sku,
                    target=entry.target_price,
                    msrp=msrp,
                    savings=entry.target_price - msrp,
                )
            )
    return hits

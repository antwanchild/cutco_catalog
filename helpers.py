"""Shared authentication, CSRF, persistence, and notification helpers."""

import base64
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from functools import wraps

import requests
from flask import (
    abort,
    current_app,
    flash,
    g,
    has_request_context,
    redirect,
    request,
    session,
    url_for,
)
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from constants import (
    ADMIN_TOKEN,
    DISCORD_WEBHOOK_URL,
    TRUSTED_AUTH_ADMIN_GROUPS,
    TRUSTED_AUTH_GROUPS_HEADER,
    TRUSTED_AUTH_USERNAME_HEADER,
)
from extensions import db
from models import (
    Ownership,
    USER_AUTH_SOURCE_LOCAL,
    USER_ROLE_ADMIN,
    USER_ROLE_USER,
    User,
    normalize_username,
)

logger = logging.getLogger(__name__)

AUTH_SESSION_KEY = "auth_identity"
LEGACY_ADMIN_SESSION_KEY = "is_admin"
IDENTITY_KIND_TOKEN_ADMIN = "token_admin"
IDENTITY_KIND_PROXY_ADMIN = "proxy_admin"
IDENTITY_KIND_USER = "user"
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """The authenticated identity and current authorization role for a request."""

    username: str
    role: str
    source: str
    user_id: int | None = None
    session_version: int | None = None

    @property
    def is_admin(self) -> bool:
        """Return whether the resolved identity currently has admin access."""
        return self.role == USER_ROLE_ADMIN


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


def trusted_proxy_username() -> str:
    """Return the username asserted by the configured trusted proxy header."""
    return _request_header_value(TRUSTED_AUTH_USERNAME_HEADER)


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


def is_trusted_proxy_admin() -> bool:
    """Return whether the trusted auth proxy says this user should be admin."""
    if not is_trusted_proxy_authenticated() or not TRUSTED_AUTH_ADMIN_GROUPS:
        return False
    allowed_groups = {group.casefold() for group in TRUSTED_AUTH_ADMIN_GROUPS}
    return bool(_trusted_proxy_groups() & allowed_groups)


def _clear_identity_cache() -> None:
    """Clear request-local identity values after the session changes."""
    if not has_request_context():
        return
    g.pop("_auth_identity_cached", None)
    g.pop("_auth_identity", None)
    g.pop("_auth_user", None)


def _store_session_identity(payload: dict) -> None:
    """Persist a signed identity payload in the Flask session."""
    session[AUTH_SESSION_KEY] = payload
    session.pop(LEGACY_ADMIN_SESSION_KEY, None)
    _clear_identity_cache()


def establish_token_admin_session() -> None:
    """Create the compatibility identity used by successful token login."""
    _store_session_identity({"kind": IDENTITY_KIND_TOKEN_ADMIN})


def establish_proxy_admin_session(username: str) -> None:
    """Persist the current proxy-admin compatibility session."""
    cleaned_username = (username or "").strip()
    if not cleaned_username:
        raise ValueError("Proxy username is required.")
    _store_session_identity(
        {
            "kind": IDENTITY_KIND_PROXY_ADMIN,
            "username": cleaned_username,
        }
    )


def establish_user_session(user: User) -> None:
    """Persist a named user identity without storing its role in the session."""
    if user.id is None:
        raise ValueError("A user must be persisted before starting a session.")
    if not user.is_active:
        raise ValueError("An inactive user cannot start a session.")
    _store_session_identity(
        {
            "kind": IDENTITY_KIND_USER,
            "user_id": user.id,
            "session_version": user.session_version,
        }
    )


def users_exist() -> bool:
    """Return whether any named application account has been created."""
    return db.session.execute(db.select(User.id).limit(1)).first() is not None


def admin_token_matches(candidate: str) -> bool:
    """Compare an admin token without content-dependent timing."""
    return hmac.compare_digest(candidate or "", ADMIN_TOKEN)


def authenticate_local_user(username: str, password: str) -> User | None:
    """Validate local credentials with a generic timing path for failures."""
    normalized_username = normalize_username(username)
    user = db.session.execute(
        db.select(User).where(
            User.username == normalized_username,
            User.auth_source == USER_AUTH_SOURCE_LOCAL,
        )
    ).scalar_one_or_none()
    password_hash = user.password_hash if user and user.password_hash else None
    password_valid = check_password_hash(
        password_hash or _DUMMY_PASSWORD_HASH,
        password or "",
    )
    if not user or not user.is_active or not password_valid:
        return None
    return user


def clear_auth_session() -> None:
    """Remove current and legacy authentication state from the session."""
    session.pop(AUTH_SESSION_KEY, None)
    session.pop(LEGACY_ADMIN_SESSION_KEY, None)
    _clear_identity_cache()


def _identity_from_named_user(payload: dict) -> RequestIdentity | None:
    """Resolve and validate a database-backed session identity."""
    user_id = payload.get("user_id")
    session_version = payload.get("session_version")
    if type(user_id) is not int or type(session_version) is not int:
        clear_auth_session()
        return None
    user = db.session.get(User, user_id)
    if user is None or not user.is_active or user.session_version != session_version:
        clear_auth_session()
        return None
    g._auth_user = user
    return RequestIdentity(
        username=user.username,
        role=user.role,
        source=user.auth_source,
        user_id=user.id,
        session_version=user.session_version,
    )


def _identity_from_session() -> RequestIdentity | None:
    """Resolve signed session state, including legacy cookie migration."""
    payload = session.get(AUTH_SESSION_KEY)
    if isinstance(payload, dict):
        kind = payload.get("kind")
        if kind == IDENTITY_KIND_TOKEN_ADMIN:
            if users_exist():
                clear_auth_session()
                return None
            return RequestIdentity(
                username="admin",
                role=USER_ROLE_ADMIN,
                source="token",
            )
        if kind == IDENTITY_KIND_PROXY_ADMIN:
            username = payload.get("username")
            if isinstance(username, str) and username.strip():
                return RequestIdentity(
                    username=username.strip(),
                    role=USER_ROLE_ADMIN,
                    source="proxy",
                )
        if kind == IDENTITY_KIND_USER:
            return _identity_from_named_user(payload)
        clear_auth_session()
    elif payload is not None:
        clear_auth_session()

    if session.get(LEGACY_ADMIN_SESSION_KEY) is True:
        if users_exist():
            clear_auth_session()
            return None
        establish_token_admin_session()
        return RequestIdentity(
            username="admin",
            role=USER_ROLE_ADMIN,
            source="token",
        )
    return None


def _identity_from_proxy_request() -> RequestIdentity | None:
    """Resolve the identity asserted on the current trusted-proxy request."""
    if not is_trusted_proxy_authenticated():
        return None
    username = trusted_proxy_username()
    role = USER_ROLE_ADMIN if is_trusted_proxy_admin() else USER_ROLE_USER
    return RequestIdentity(username=username, role=role, source="proxy")


def current_identity() -> RequestIdentity | None:
    """Return the single resolved identity used for request authorization."""
    if not has_request_context():
        return None
    if getattr(g, "_auth_identity_cached", False):
        return getattr(g, "_auth_identity", None)
    identity = _identity_from_session() or _identity_from_proxy_request()
    g._auth_identity = identity
    g._auth_identity_cached = True
    return identity


def current_user() -> User | None:
    """Return the active database-backed user for the current request, if any."""
    identity = current_identity()
    if identity is None or identity.user_id is None:
        return None
    return getattr(g, "_auth_user", None)


def is_admin() -> bool:
    """Return whether the current resolved identity has admin access."""
    identity = current_identity()
    return bool(identity and identity.is_admin)


def is_authenticated_user() -> bool:
    """Return whether the current request has an authenticated identity."""
    return current_identity() is not None


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

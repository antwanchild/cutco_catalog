"""Admin-only web management for named application users."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from helpers import admin_required, current_identity
from models import (
    USER_AUTH_SOURCE_LOCAL,
    USER_AUTH_SOURCE_PROXY,
    USER_AUTH_SOURCES,
    USER_ROLE_USER,
    USER_ROLES,
    User,
    record_audit_event,
)

user_admin_bp = Blueprint("user_admin", __name__, url_prefix="/admin/users")


def _load_user(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    return user


def _actor_user_id() -> int | None:
    identity = current_identity()
    return identity.user_id if identity is not None else None


def _is_self(user: User) -> bool:
    actor_user_id = _actor_user_id()
    return actor_user_id is not None and user.id == actor_user_id


def _commit(message: str) -> bool:
    try:
        db.session.commit()
        return True
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash(message, "error")
    return False


def _record_user_event(
    user: User,
    *,
    title: str,
    action: str = "update",
    payload: dict | None = None,
) -> None:
    record_audit_event(
        title=title,
        action=action,
        entity_type="User",
        entity_id=user.id,
        entity_name=user.label,
        payload=payload,
    )


@user_admin_bp.route("")
@admin_required
def users_list():
    """List named accounts and their current authorization state."""
    users = db.session.scalars(db.select(User).order_by(User.username)).all()
    return render_template(
        "admin_users.html",
        users=users,
        actor_user_id=_actor_user_id(),
    )


@user_admin_bp.route("/new", methods=["GET", "POST"])
@admin_required
def user_create():
    """Create a pre-provisioned local or trusted-proxy account."""
    if request.method == "POST":
        username = request.form.get("username", "")
        display_name = request.form.get("display_name", "").strip() or None
        role = request.form.get("role", USER_ROLE_USER)
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        auth_source = request.form.get("auth_source", USER_AUTH_SOURCE_LOCAL)
        external_subject = request.form.get("external_subject", "").strip()
        if auth_source not in USER_AUTH_SOURCES:
            flash("Select a supported authentication source.", "error")
        elif auth_source == USER_AUTH_SOURCE_LOCAL and password != password_confirm:
            flash("Passwords do not match.", "error")
        elif auth_source == USER_AUTH_SOURCE_PROXY and not external_subject:
            flash("A stable proxy subject is required.", "error")
        elif display_name and len(display_name) > 160:
            flash("Display name must be 160 characters or fewer.", "error")
        else:
            try:
                user = User(
                    username=username,
                    display_name=display_name,
                    role=role,
                    auth_source=auth_source,
                    external_subject=(
                        external_subject
                        if auth_source == USER_AUTH_SOURCE_PROXY
                        else None
                    ),
                )
                if auth_source == USER_AUTH_SOURCE_LOCAL:
                    user.set_password(password, require_change=True)
                db.session.add(user)
                db.session.flush()
                _record_user_event(
                    user,
                    title="Created account through user administration",
                    action="create",
                    payload={
                        "role": user.role,
                        "auth_source": user.auth_source,
                        "is_active": user.is_active,
                        "must_change_password": user.must_change_password,
                    },
                )
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            except SQLAlchemyError:
                db.session.rollback()
                flash("Could not create the account. The username may exist.", "error")
            else:
                if _commit("Could not create the account."):
                    flash(
                        (
                            "Local account created. The temporary password must be "
                            "changed at login."
                            if user.auth_source == USER_AUTH_SOURCE_LOCAL
                            else "Proxy account pre-provisioned."
                        ),
                        "success",
                    )
                    return redirect(url_for("user_admin.user_edit", user_id=user.id))

    return render_template(
        "admin_user_form.html",
        user=None,
        roles=sorted(USER_ROLES),
        actor_user_id=_actor_user_id(),
        auth_sources=sorted(USER_AUTH_SOURCES),
    )


@user_admin_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(user_id: int):
    """Edit mutable profile and authorization fields for an account."""
    user = _load_user(user_id)
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip() or None
        role = request.form.get("role", user.role)
        if display_name and len(display_name) > 160:
            flash("Display name must be 160 characters or fewer.", "error")
        elif role not in USER_ROLES:
            flash("Select a supported role.", "error")
        else:
            before = {"display_name": user.display_name, "role": user.role}
            try:
                user.update_access(role=role, actor_user_id=_actor_user_id())
                user.display_name = display_name
                role_changed = before["role"] != user.role
                if role_changed:
                    user.revoke_sessions()
                _record_user_event(
                    user,
                    title="Updated account through user administration",
                    payload={
                        "changes": {
                            "display_name": {
                                "before": before["display_name"],
                                "after": user.display_name,
                            },
                            "role": {
                                "before": before["role"],
                                "after": user.role,
                            },
                        },
                        "sessions_revoked": role_changed,
                    },
                )
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            else:
                if _commit("Could not update the account."):
                    flash("Account updated.", "success")
                    return redirect(url_for("user_admin.users_list"))

    return render_template(
        "admin_user_form.html",
        user=user,
        roles=sorted(USER_ROLES),
        actor_user_id=_actor_user_id(),
        auth_sources=sorted(USER_AUTH_SOURCES),
    )


@user_admin_bp.route("/<int:user_id>/link-proxy", methods=["POST"])
@admin_required
def user_link_proxy(user_id: int):
    """Explicitly link a stable proxy subject to a local account."""
    user = _load_user(user_id)
    subject = request.form.get("external_subject", "").strip()
    if user.auth_source != USER_AUTH_SOURCE_LOCAL:
        flash("Proxy-sourced accounts already have an immutable subject.", "error")
    else:
        existing = db.session.execute(
            db.select(User).where(
                User.external_subject == subject,
                User.id != user.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            flash("That proxy subject is already linked to another account.", "error")
        else:
            try:
                user.link_proxy_subject(subject)
                user.revoke_sessions()
                _record_user_event(
                    user,
                    title="Linked proxy identity through user administration",
                    payload={"proxy_linked": True, "sessions_revoked": True},
                )
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            else:
                if _commit("Could not link the proxy identity."):
                    flash(
                        "Proxy identity linked; existing sessions were revoked.",
                        "success",
                    )
    return redirect(url_for("user_admin.user_edit", user_id=user.id))


@user_admin_bp.route("/<int:user_id>/unlink-proxy", methods=["POST"])
@admin_required
def user_unlink_proxy(user_id: int):
    """Remove an explicit trusted-proxy link from a local account."""
    user = _load_user(user_id)
    if user.auth_source != USER_AUTH_SOURCE_LOCAL or not user.external_subject:
        flash("This account does not have a removable proxy link.", "error")
    else:
        user.unlink_proxy_subject()
        user.revoke_sessions()
        _record_user_event(
            user,
            title="Unlinked proxy identity through user administration",
            payload={"proxy_linked": False, "sessions_revoked": True},
        )
        if _commit("Could not unlink the proxy identity."):
            flash("Proxy identity unlinked; existing sessions were revoked.", "success")
    return redirect(url_for("user_admin.user_edit", user_id=user.id))


def _set_active(user: User, *, active: bool) -> bool:
    if user.is_active == active:
        flash(f"Account is already {'active' if active else 'inactive'}.", "info")
        return False
    try:
        before = user.is_active
        user.update_access(is_active=active, actor_user_id=_actor_user_id())
        user.revoke_sessions()
        _record_user_event(
            user,
            title=(
                "Activated account through user administration"
                if active
                else "Deactivated account through user administration"
            ),
            payload={
                "is_active": {"before": before, "after": active},
                "session_version": user.session_version,
            },
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return False
    return _commit("Could not update the account status.")


@user_admin_bp.route("/<int:user_id>/activate", methods=["POST"])
@admin_required
def user_activate(user_id: int):
    """Reactivate an account and invalidate its stale sessions."""
    user = _load_user(user_id)
    if _set_active(user, active=True):
        flash("Account activated and stale sessions revoked.", "success")
    return redirect(url_for("user_admin.users_list"))


@user_admin_bp.route("/<int:user_id>/deactivate", methods=["POST"])
@admin_required
def user_deactivate(user_id: int):
    """Deactivate an account and invalidate all of its sessions."""
    user = _load_user(user_id)
    if _set_active(user, active=False):
        flash("Account deactivated and sessions revoked.", "success")
    return redirect(url_for("user_admin.users_list"))


@user_admin_bp.route("/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def user_reset_password(user_id: int):
    """Set a temporary local password and revoke the target's sessions."""
    user = _load_user(user_id)
    if _is_self(user):
        flash("Use Change Password for your own account.", "error")
    elif user.auth_source != USER_AUTH_SOURCE_LOCAL:
        flash(
            "Proxy-managed passwords must be reset at the identity provider.", "error"
        )
    else:
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        if password != password_confirm:
            flash("Passwords do not match.", "error")
        elif user.check_password(password):
            flash(
                "Choose a temporary password different from the current one.", "error"
            )
        else:
            try:
                user.set_password(password, require_change=True)
                user.revoke_sessions()
                _record_user_event(
                    user,
                    title="Reset account password through user administration",
                    payload={
                        "must_change_password": True,
                        "session_version": user.session_version,
                    },
                )
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            else:
                if _commit("Could not reset the password."):
                    flash(
                        "Temporary password set; existing sessions were revoked.",
                        "success",
                    )
    return redirect(url_for("user_admin.user_edit", user_id=user.id))


@user_admin_bp.route("/<int:user_id>/revoke-sessions", methods=["POST"])
@admin_required
def user_revoke_sessions(user_id: int):
    """Revoke sessions without changing account credentials or status."""
    user = _load_user(user_id)
    if _is_self(user):
        flash("You cannot revoke your current account through this page.", "error")
    else:
        user.revoke_sessions()
        _record_user_event(
            user,
            title="Revoked account sessions through user administration",
            payload={"session_version": user.session_version},
        )
        if _commit("Could not revoke account sessions."):
            flash("Account sessions revoked.", "success")
    return redirect(url_for("user_admin.user_edit", user_id=user.id))

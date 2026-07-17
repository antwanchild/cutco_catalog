"""Container-safe command-line recovery tools for application users."""

from datetime import datetime, timezone

import click
from flask import Flask
from flask.cli import AppGroup
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import (
    AuthSetupState,
    USER_AUTH_SOURCE_LOCAL,
    USER_AUTH_SOURCE_PROXY,
    USER_ROLE_ADMIN,
    USER_ROLE_USER,
    USER_ROLES,
    User,
    normalize_username,
    record_audit_event,
)

AUTH_SETUP_STATE_ID = 1
CLI_ACTOR = "cli"


def _find_user(username: str) -> User:
    """Resolve a canonical username or raise a safe CLI error."""
    normalized = normalize_username(username)
    user = db.session.execute(
        db.select(User).where(User.username == normalized)
    ).scalar_one_or_none()
    if user is None:
        raise click.ClickException(f"User {normalized or username!r} was not found.")
    return user


def _prompt_password(label: str = "Password") -> str:
    """Read and confirm a password without exposing it in process arguments."""
    return click.prompt(label, hide_input=True, confirmation_prompt=True, type=str)


def _commit_or_fail(message: str) -> None:
    """Commit a CLI mutation and convert database failures to clean output."""
    try:
        db.session.commit()
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        raise click.ClickException(message) from exc


users_cli = AppGroup(
    "users",
    help="Manage and recover application user accounts.",
)


@users_cli.command("list")
def list_users() -> None:
    """List accounts without exposing credential material."""
    users = db.session.scalars(db.select(User).order_by(User.username)).all()
    if not users:
        click.echo("No users found.")
        return

    click.echo("USERNAME\tROLE\tSOURCE\tSTATUS\tPASSWORD CHANGE")
    for user in users:
        click.echo(
            "\t".join(
                (
                    user.username,
                    user.role,
                    user.auth_source,
                    "active" if user.is_active else "inactive",
                    "required" if user.must_change_password else "no",
                )
            )
        )


@users_cli.command("create-admin")
@click.option(
    "--username", required=True, help="Unique username for the administrator."
)
@click.option("--display-name", help="Optional human-readable account name.")
def create_admin(username: str, display_name: str | None) -> None:
    """Create a local administrator using a securely prompted password."""
    password = _prompt_password()
    try:
        cleaned_display_name = (display_name or "").strip() or None
        if cleaned_display_name and len(cleaned_display_name) > 160:
            raise ValueError("Display name must be 160 characters or fewer.")
        user = User(
            username=username,
            display_name=cleaned_display_name,
            role=USER_ROLE_ADMIN,
            auth_source=USER_AUTH_SOURCE_LOCAL,
        )
        user.set_password(password)
        first_user = db.session.execute(db.select(User.id).limit(1)).first() is None
        db.session.add(user)
        db.session.flush()

        if first_user:
            db.session.add(
                AuthSetupState(
                    id=AUTH_SETUP_STATE_ID,
                    user_id=user.id,
                    completed_at=datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                )
            )

        record_audit_event(
            title=(
                "Created initial administrator"
                if first_user
                else "Created administrator through recovery CLI"
            ),
            actor=CLI_ACTOR,
            action="create",
            entity_type="User",
            entity_id=user.id,
            entity_name=user.label,
            source="flask users create-admin",
            payload={
                "role": user.role,
                "auth_source": user.auth_source,
                "initial_setup": first_user,
            },
        )
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        raise click.ClickException(
            "Could not create the administrator. Check the username and password policy."
        ) from exc

    _commit_or_fail("Could not create the administrator.")
    click.echo(f"Created administrator {user.username!r}.")


@users_cli.command("create-proxy")
@click.option("--username", required=True, help="Unique application username.")
@click.option(
    "--subject",
    required=True,
    help="Immutable subject asserted by the trusted authentication proxy.",
)
@click.option("--display-name", help="Optional human-readable account name.")
@click.option(
    "--role",
    type=click.Choice(sorted(USER_ROLES), case_sensitive=False),
    default=USER_ROLE_USER,
    show_default=True,
)
def create_proxy(
    username: str,
    subject: str,
    display_name: str | None,
    role: str,
) -> None:
    """Pre-provision a trusted-proxy account without a local password."""
    try:
        cleaned_display_name = (display_name or "").strip() or None
        if cleaned_display_name and len(cleaned_display_name) > 160:
            raise ValueError("Display name must be 160 characters or fewer.")
        user = User(
            username=username,
            display_name=cleaned_display_name,
            role=role,
            auth_source=USER_AUTH_SOURCE_PROXY,
            external_subject=subject,
        )
        db.session.add(user)
        db.session.flush()
        record_audit_event(
            title="Pre-provisioned proxy account through recovery CLI",
            actor=CLI_ACTOR,
            action="create",
            entity_type="User",
            entity_id=user.id,
            entity_name=user.label,
            source="flask users create-proxy",
            payload={"role": user.role, "auth_source": user.auth_source},
        )
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        raise click.ClickException(
            "Could not create the proxy account. Check for a username or subject conflict."
        ) from exc

    _commit_or_fail("Could not create the proxy account.")
    click.echo(f"Created proxy {user.role} {user.username!r}.")


@users_cli.command("reset-password")
@click.argument("username")
def reset_password(username: str) -> None:
    """Set a temporary local password and revoke existing sessions."""
    user = _find_user(username)
    if user.auth_source != USER_AUTH_SOURCE_LOCAL:
        raise click.ClickException(
            "Proxy-managed accounts do not have application passwords."
        )

    password = _prompt_password("Temporary password")
    try:
        user.set_password(password, require_change=True)
        user.revoke_sessions()
        record_audit_event(
            title="Reset account password through recovery CLI",
            actor=CLI_ACTOR,
            action="update",
            entity_type="User",
            entity_id=user.id,
            entity_name=user.label,
            source="flask users reset-password",
            payload={
                "must_change_password": True,
                "session_version": user.session_version,
            },
        )
    except ValueError as exc:
        db.session.rollback()
        raise click.ClickException(str(exc)) from exc

    _commit_or_fail("Could not reset the password.")
    click.echo(
        f"Reset password for {user.username!r}; existing sessions were revoked "
        "and a password change is required at next login."
    )


@users_cli.command("activate")
@click.argument("username")
def activate_user(username: str) -> None:
    """Reactivate an account and invalidate any stale sessions."""
    user = _find_user(username)
    if user.is_active:
        click.echo(f"User {user.username!r} is already active.")
        return

    user.is_active = True
    user.revoke_sessions()
    record_audit_event(
        title="Activated account through recovery CLI",
        actor=CLI_ACTOR,
        action="update",
        entity_type="User",
        entity_id=user.id,
        entity_name=user.label,
        source="flask users activate",
        payload={
            "is_active": True,
            "session_version": user.session_version,
        },
    )
    _commit_or_fail("Could not activate the account.")
    click.echo(f"Activated user {user.username!r}.")


@users_cli.command("revoke-sessions")
@click.argument("username")
def revoke_sessions(username: str) -> None:
    """Invalidate all signed sessions currently issued to an account."""
    user = _find_user(username)
    user.revoke_sessions()
    record_audit_event(
        title="Revoked account sessions through recovery CLI",
        actor=CLI_ACTOR,
        action="update",
        entity_type="User",
        entity_id=user.id,
        entity_name=user.label,
        source="flask users revoke-sessions",
        payload={"session_version": user.session_version},
    )
    _commit_or_fail("Could not revoke account sessions.")
    click.echo(f"Revoked sessions for {user.username!r}.")


def register_user_cli(app: Flask) -> None:
    """Register the user recovery command group on an application instance."""
    app.cli.add_command(users_cli)

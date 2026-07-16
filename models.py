"""Database models and item normalization helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from flask import has_request_context, request, session
from sqlalchemy import delete as sa_delete, event, inspect as sa_inspect
from sqlalchemy.orm import Mapped, Session as SASession, relationship, validates
from werkzeug.security import check_password_hash, generate_password_hash

from constants import UNKNOWN_COLOR
from extensions import db


class BaseModel(db.Model):
    """Shared base class that keeps SQLAlchemy models Pylance-friendly."""

    __abstract__ = True
    __allow_unmapped__ = True

    if TYPE_CHECKING:

        def __init__(self, **kwargs: Any) -> None:
            """Pylance-only constructor signature."""


# Association object: items <-> sets (with quantity per member)
class ItemSetMember(BaseModel):
    """Association row linking items to sets with quantities."""

    __tablename__ = "item_sets"

    if TYPE_CHECKING:
        item: Mapped[Item | None]
        set: Mapped[Set | None]

    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), primary_key=True)
    set_id = db.Column(db.Integer, db.ForeignKey("sets.id"), primary_key=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    item: Mapped[Item | None] = relationship("Item", back_populates="set_memberships")
    set: Mapped[Set | None] = relationship("Set", back_populates="members")


# Many-to-many join table: items <-> knife_tasks (Cutco-sourced suggested uses)
item_tasks = db.Table(
    "item_tasks",
    db.Column("item_id", db.Integer, db.ForeignKey("items.id"), primary_key=True),
    db.Column("task_id", db.Integer, db.ForeignKey("knife_tasks.id"), primary_key=True),
)


class Item(BaseModel):
    """A cataloged item that may have one or more variants."""

    __tablename__ = "items"

    if TYPE_CHECKING:
        variants: Mapped[list[ItemVariant]]
        set_memberships: Mapped[list[ItemSetMember]]
        sets: Mapped[list[Set]]
        suggested_tasks: Mapped[list[KnifeTask]]
        attachments: Mapped[list[ItemAttachment]]
        cookware_sessions: Mapped[list[CookwareSession]]
        sharpening_log: Mapped[list[SharpeningLog]]
        task_log: Mapped[list[KnifeTaskLog]]

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    sku = db.Column(db.String(60), nullable=True, unique=True)
    alternate_skus = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(80), nullable=True)
    availability = db.Column(db.String(40), nullable=False, default="public")
    edge_type = db.Column(db.String(40), nullable=False, default="Unknown")
    is_unicorn = db.Column(db.Boolean, nullable=False, default=False)
    edge_is_unicorn = db.Column(db.Boolean, nullable=False, default=False)
    in_catalog = db.Column(db.Boolean, nullable=False, default=True)
    set_only = db.Column(db.Boolean, nullable=False, default=False)
    cutco_url = db.Column(db.String(300), nullable=True)
    msrp = db.Column(db.Float, nullable=True)
    blade_length = db.Column(db.String(20), nullable=True)
    overall_length = db.Column(db.String(20), nullable=True)
    weight = db.Column(db.String(20), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    variants: Mapped[list[ItemVariant]] = relationship(
        "ItemVariant",
        back_populates="item",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="ItemVariant.color",
    )
    set_memberships: Mapped[list[ItemSetMember]] = relationship(
        "ItemSetMember", back_populates="item", cascade="all, delete-orphan"
    )
    sets: Mapped[list[Set]] = relationship(
        "Set",
        secondary="item_sets",
        back_populates="items",
        viewonly=True,
        lazy="select",
    )
    suggested_tasks: Mapped[list[KnifeTask]] = relationship(
        "KnifeTask",
        secondary="item_tasks",
        back_populates="suggested_items",
        lazy="select",
    )
    attachments: Mapped[list[ItemAttachment]] = relationship(
        "ItemAttachment",
        back_populates="item",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="ItemAttachment.created_at.desc()",
    )
    cookware_sessions: Mapped[list[CookwareSession]] = relationship(
        "CookwareSession",
        back_populates="item",
        lazy=True,
        order_by="CookwareSession.used_on.desc()",
    )
    sharpening_log: Mapped[list[SharpeningLog]] = relationship(
        "SharpeningLog",
        back_populates="item",
        lazy=True,
        order_by="SharpeningLog.sharpened_on.desc()",
    )
    task_log: Mapped[list[KnifeTaskLog]] = relationship(
        "KnifeTaskLog",
        back_populates="item",
        lazy=True,
        order_by="KnifeTaskLog.logged_on.desc()",
    )

    @property
    def any_unicorn(self) -> bool:
        """True if the item, edge type, or any specific variant is flagged unicorn."""
        return (
            self.is_unicorn
            or self.edge_is_unicorn
            or any(variant.is_unicorn for variant in (self.variants or []))
        )

    @property
    def alternate_sku_values(self) -> list[str]:
        """Return the parsed alternate SKU list."""
        return parse_alternate_skus(self.alternate_skus)

    @property
    def availability_label(self) -> str | None:
        """Return a display label for the item's availability."""
        labels = {
            "rep only": "Rep only",
            "Costco": "Costco",
            "non-catalog": "Non-catalog",
        }
        return labels.get((self.availability or "").strip())

    @property
    def availability_badge_class(self) -> str | None:
        """Return the CSS badge class for the item's availability."""
        badge_classes = {
            "rep only": "badge-warning",
            "Costco": "badge-info",
            "non-catalog": "badge-off-catalog",
        }
        return badge_classes.get((self.availability or "").strip())


class ItemVariant(BaseModel):
    """A color-specific variant of a catalog item."""

    __tablename__ = "item_variants"

    if TYPE_CHECKING:
        item: Mapped[Item | None]
        ownerships: Mapped[list[Ownership]]

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    color = db.Column(db.String(80), nullable=False, default=UNKNOWN_COLOR)
    is_unicorn = db.Column(db.Boolean, nullable=False, default=False)
    source = db.Column(db.String(40), nullable=True, default="manual")
    notes = db.Column(db.Text, nullable=True)

    item: Mapped[Item | None] = relationship("Item", back_populates="variants")
    ownerships: Mapped[list[Ownership]] = relationship(
        "Ownership", back_populates="variant", lazy=True, cascade="all, delete-orphan"
    )


class ItemAttachment(BaseModel):
    """An uploaded image or attachment for a catalog item."""

    __tablename__ = "item_attachments"

    if TYPE_CHECKING:
        item: Mapped[Item | None]

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False, unique=True)
    content_type = db.Column(db.String(80), nullable=True)
    caption = db.Column(db.String(200), nullable=True)
    created_at = db.Column(
        db.String(32),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(timespec="seconds"),
    )

    item: Mapped[Item | None] = relationship("Item", back_populates="attachments")


class Set(BaseModel):
    """A knife set or bundle that can contain multiple items."""

    __tablename__ = "sets"

    if TYPE_CHECKING:
        members: Mapped[list[ItemSetMember]]
        items: Mapped[list[Item]]
        variants: Mapped[list[SetVariant]]

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    sku = db.Column(db.String(20), nullable=True)
    cutco_url = db.Column(db.String(300), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    member_data = db.Column(db.Text, nullable=True)

    members: Mapped[list[ItemSetMember]] = relationship(
        "ItemSetMember", back_populates="set", cascade="all, delete-orphan"
    )
    items: Mapped[list[Item]] = relationship(
        "Item",
        secondary="item_sets",
        back_populates="sets",
        viewonly=True,
        lazy="select",
    )
    variants: Mapped[list[SetVariant]] = relationship(
        "SetVariant",
        back_populates="set",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="SetVariant.color",
    )


class SetVariant(BaseModel):
    """A handle-color or block-finish option offered for a set."""

    __tablename__ = "set_variants"

    if TYPE_CHECKING:
        set: Mapped[Set | None]

    id = db.Column(db.Integer, primary_key=True)
    set_id = db.Column(db.Integer, db.ForeignKey("sets.id"), nullable=False)
    color = db.Column(db.String(80), nullable=False)
    kind = db.Column(db.String(24), nullable=False, default="handle")
    source = db.Column(db.String(40), nullable=True, default="manual")

    set: Mapped[Set | None] = relationship("Set", back_populates="variants")

    __table_args__ = (
        db.UniqueConstraint("set_id", "color", name="uq_set_variant_color"),
    )


class Person(BaseModel):
    """A person or collector tracked by the application."""

    __tablename__ = "people"

    if TYPE_CHECKING:
        ownerships: Mapped[list[Ownership]]

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text, nullable=True)

    ownerships: Mapped[list[Ownership]] = relationship(
        "Ownership", back_populates="person", lazy=True, cascade="all, delete-orphan"
    )


class Ownership(BaseModel):
    """A person's ownership record for a specific item variant."""

    __tablename__ = "ownership"

    if TYPE_CHECKING:
        variant: Mapped[ItemVariant | None]
        person: Mapped[Person | None]
        copy_type: Mapped[str]
        engraving_text: Mapped[str | None]
        engraving_notes: Mapped[str | None]
        engraving_signature: Mapped[str]

    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(
        db.Integer, db.ForeignKey("item_variants.id"), nullable=False
    )
    person_id = db.Column(db.Integer, db.ForeignKey("people.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Owned")
    target_price = db.Column(db.Float, nullable=True)
    quantity_purchased = db.Column(db.Integer, nullable=True)
    quantity_given_away = db.Column(db.Integer, nullable=True)
    copy_type = db.Column(db.String(20), nullable=False, default="plain")
    engraving_text = db.Column(db.String(255), nullable=True)
    engraving_notes = db.Column(db.Text, nullable=True)
    engraving_signature = db.Column(
        db.String(255), nullable=False, default="plain", index=True
    )
    notes = db.Column(db.Text, nullable=True)

    variant: Mapped[ItemVariant | None] = relationship(
        "ItemVariant", back_populates="ownerships"
    )
    person: Mapped[Person | None] = relationship("Person", back_populates="ownerships")

    __table_args__ = (
        db.UniqueConstraint(
            "variant_id",
            "person_id",
            "copy_type",
            "engraving_signature",
            name="uq_variant_person_copy",
        ),
    )

    @property
    def is_engraved(self) -> bool:
        """True when this ownership row represents an engraved copy."""
        return self.copy_type == "engraved"

    @property
    def engraving_label(self) -> str:
        """Return a compact display label for the copy type."""
        if self.is_engraved:
            return self.engraving_text or "Engraved"
        return "Plain"

    def sync_engraving_signature(self) -> None:
        """Keep the stored copy type and uniqueness signature aligned."""
        self.copy_type = normalize_engraving_copy_type(self.copy_type)
        self.engraving_signature = normalize_engraving_signature(
            self.copy_type, self.engraving_text
        )


USER_ROLE_USER = "user"
USER_ROLE_ADMIN = "admin"
USER_ROLES = frozenset({USER_ROLE_USER, USER_ROLE_ADMIN})
USER_AUTH_SOURCE_LOCAL = "local"
USER_AUTH_SOURCE_PROXY = "proxy"
USER_AUTH_SOURCES = frozenset({USER_AUTH_SOURCE_LOCAL, USER_AUTH_SOURCE_PROXY})
MIN_PASSWORD_LENGTH = 12


def normalize_username(value: str) -> str:
    """Return the canonical value used to identify a user."""
    return (value or "").strip().casefold()


class User(BaseModel):
    """A named local or trusted-proxy application identity."""

    __tablename__ = "users"

    if TYPE_CHECKING:
        audit_events: Mapped[list[ActivityEvent]]

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(160), nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), nullable=False, default=USER_ROLE_USER)
    auth_source = db.Column(
        db.String(20), nullable=False, default=USER_AUTH_SOURCE_LOCAL
    )
    external_subject = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    last_login_at = db.Column(db.String(32), nullable=True)
    created_at = db.Column(
        db.String(32),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(timespec="seconds"),
    )
    updated_at = db.Column(
        db.String(32),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(timespec="seconds"),
        onupdate=lambda: datetime.now(UTC).isoformat(timespec="seconds"),
    )

    audit_events: Mapped[list[ActivityEvent]] = relationship(
        "ActivityEvent",
        back_populates="actor_user",
        foreign_keys="ActivityEvent.actor_user_id",
        passive_deletes=True,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "auth_source",
            "external_subject",
            name="uq_user_auth_source_subject",
        ),
        db.CheckConstraint(
            "role IN ('user', 'admin')",
            name="ck_user_role",
        ),
        db.CheckConstraint(
            "auth_source IN ('local', 'proxy')",
            name="ck_user_auth_source",
        ),
        db.CheckConstraint(
            "session_version >= 1",
            name="ck_user_session_version",
        ),
    )

    def __init__(self, **kwargs: Any) -> None:
        """Initialize security-sensitive defaults before the first flush."""
        kwargs.setdefault("role", USER_ROLE_USER)
        kwargs.setdefault("auth_source", USER_AUTH_SOURCE_LOCAL)
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("must_change_password", False)
        kwargs.setdefault("session_version", 1)
        super().__init__(**kwargs)

    @validates("username")
    def _normalize_username(self, _key: str, value: str) -> str:
        """Normalize and validate an assigned username."""
        normalized = normalize_username(value)
        if not normalized:
            raise ValueError("Username is required.")
        if len(normalized) > 120:
            raise ValueError("Username must be 120 characters or fewer.")
        state = sa_inspect(self)
        if state is not None and state.persistent and self.username != normalized:
            raise ValueError("User identity field 'username' cannot be changed.")
        return normalized

    @validates("role")
    def _validate_role(self, _key: str, value: str) -> str:
        """Reject unsupported authorization roles."""
        normalized = (value or "").strip().casefold()
        if normalized not in USER_ROLES:
            raise ValueError(f"Unsupported user role: {value!r}")
        return normalized

    @validates("auth_source")
    def _validate_auth_source(self, _key: str, value: str) -> str:
        """Reject unsupported identity sources."""
        normalized = (value or "").strip().casefold()
        if normalized not in USER_AUTH_SOURCES:
            raise ValueError(f"Unsupported authentication source: {value!r}")
        state = sa_inspect(self)
        if state is not None and state.persistent and self.auth_source != normalized:
            raise ValueError("User identity field 'auth_source' cannot be changed.")
        return normalized

    @validates("external_subject")
    def _normalize_external_subject(self, _key: str, value: str | None) -> str | None:
        """Trim an external identity subject without changing its case."""
        normalized = (value or "").strip()
        normalized_value = normalized or None
        state = sa_inspect(self)
        if (
            state is not None
            and state.persistent
            and self.external_subject != normalized_value
        ):
            raise ValueError(
                "User identity field 'external_subject' cannot be changed."
            )
        return normalized_value

    @property
    def label(self) -> str:
        """Return the preferred human-readable identity label."""
        return (self.display_name or "").strip() or self.username

    @property
    def has_admin_role(self) -> bool:
        """Return whether this account currently has the admin role."""
        return self.role == USER_ROLE_ADMIN

    def set_password(self, password: str, *, require_change: bool = False) -> None:
        """Hash and store a local password."""
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        self.password_hash = generate_password_hash(password)
        self.must_change_password = require_change

    def check_password(self, password: str) -> bool:
        """Return whether a candidate matches this account's password hash."""
        return bool(
            self.password_hash
            and password
            and check_password_hash(self.password_hash, password)
        )

    def revoke_sessions(self) -> None:
        """Invalidate sessions issued with the current session version."""
        self.session_version = max(1, self.session_version or 1) + 1


class ActivityEvent(BaseModel):
    """A recorded activity item for dashboard summaries."""

    __tablename__ = "activity_events"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    details = db.Column(db.Text, nullable=True)
    occurred_at = db.Column(db.String(32), nullable=False, index=True)
    actor = db.Column(db.String(40), nullable=True, index=True)
    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = db.Column(db.String(20), nullable=True, index=True)
    entity_type = db.Column(db.String(40), nullable=True, index=True)
    entity_id = db.Column(db.Integer, nullable=True, index=True)
    entity_name = db.Column(db.String(160), nullable=True)
    source = db.Column(db.String(160), nullable=True)
    payload = db.Column(db.Text, nullable=True)

    actor_user: Mapped[User | None] = relationship(
        "User", back_populates="audit_events", foreign_keys=[actor_user_id]
    )


class CookwareSession(BaseModel):
    """A log entry for cookware usage."""

    __tablename__ = "cookware_sessions"

    if TYPE_CHECKING:
        item: Mapped[Item | None]

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    used_on = db.Column(db.String(10), nullable=False)  # ISO date YYYY-MM-DD
    made_item = db.Column(db.String(200), nullable=False)
    rating = db.Column(db.Integer, nullable=True)  # 1–5
    notes = db.Column(db.Text, nullable=True)

    item: Mapped[Item | None] = relationship("Item", back_populates="cookware_sessions")


class SharpeningLog(BaseModel):
    """A log entry for sharpening activity."""

    __tablename__ = "sharpening_log"

    if TYPE_CHECKING:
        item: Mapped[Item | None]

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    sharpened_on = db.Column(db.String(10), nullable=False)  # ISO date YYYY-MM-DD
    method = db.Column(db.String(60), nullable=False, default="Home Sharpener")
    notes = db.Column(db.Text, nullable=True)

    item: Mapped[Item | None] = relationship("Item", back_populates="sharpening_log")


class KnifeTask(BaseModel):
    """A suggested use or task associated with an item."""

    __tablename__ = "knife_tasks"

    if TYPE_CHECKING:
        suggested_items: Mapped[list[Item]]
        log_entries: Mapped[list[KnifeTaskLog]]

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    is_preset = db.Column(db.Boolean, nullable=False, default=False)

    suggested_items: Mapped[list[Item]] = relationship(
        "Item", secondary="item_tasks", back_populates="suggested_tasks", lazy="select"
    )

    log_entries: Mapped[list[KnifeTaskLog]] = relationship(
        "KnifeTaskLog", back_populates="task", lazy=True
    )


class KnifeTaskLog(BaseModel):
    """A history row linking an item to a task log entry."""

    __tablename__ = "knife_task_log"

    if TYPE_CHECKING:
        item: Mapped[Item | None]
        task: Mapped[KnifeTask | None]

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("knife_tasks.id"), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    logged_on = db.Column(db.String(10), nullable=False)  # ISO date YYYY-MM-DD

    item: Mapped[Item | None] = relationship("Item", back_populates="task_log")
    task: Mapped[KnifeTask | None] = relationship(
        "KnifeTask", back_populates="log_entries"
    )


def ensure_unknown_variant(item: Item) -> None:
    """Add the Unknown fallback only when an item has no variants."""
    if item.variants:
        return
    variant = ItemVariant()
    variant.item_id = item.id
    variant.color = UNKNOWN_COLOR
    variant.source = "fallback_unknown"
    db.session.add(variant)
    db.session.flush()


def reconcile_unknown_variant(item: Item) -> None:
    """Keep Unknown only when an item has no real color variants.

    Safety rule: if an Unknown variant already has ownership records,
    keep it to avoid breaking historical links.
    """
    variants = item.variants or []
    real_variants = [variant for variant in variants if variant.color != UNKNOWN_COLOR]
    unknown_variants = [
        variant for variant in variants if variant.color == UNKNOWN_COLOR
    ]
    deleted_unknown_ids: set[int] = set()

    def merge_variant_ownerships(
        source_variant: ItemVariant, target_variant: ItemVariant
    ) -> None:
        for ownership in list(source_variant.ownerships):
            existing_ownership = next(
                (
                    candidate
                    for candidate in target_variant.ownerships
                    if candidate.person_id == ownership.person_id
                ),
                None,
            )
            if existing_ownership:
                existing_ownership.quantity_purchased = (
                    (existing_ownership.quantity_purchased or 0)
                    + (ownership.quantity_purchased or 0)
                ) or None
                existing_ownership.quantity_given_away = (
                    (existing_ownership.quantity_given_away or 0)
                    + (ownership.quantity_given_away or 0)
                ) or None
                if ownership.notes:
                    existing_ownership.notes = (
                        f"{existing_ownership.notes}; {ownership.notes}"
                        if existing_ownership.notes
                        else ownership.notes
                    )
                db.session.delete(ownership)
            else:
                ownership.variant_id = target_variant.id

    if len(unknown_variants) > 1:
        keeper = unknown_variants[0]
        for duplicate_variant in unknown_variants[1:]:
            if duplicate_variant.ownerships:
                merge_variant_ownerships(duplicate_variant, keeper)
            db.session.execute(
                sa_delete(ItemVariant).where(ItemVariant.id == duplicate_variant.id)
            )
            deleted_unknown_ids.add(duplicate_variant.id)

    unknown_variants = [
        variant
        for variant in item.variants
        if variant.color == UNKNOWN_COLOR and variant.id not in deleted_unknown_ids
    ]

    if not real_variants:
        if not unknown_variants:
            ensure_unknown_variant(item)
        return

    for unknown_variant in unknown_variants:
        if not unknown_variant.ownerships:
            db.session.execute(
                sa_delete(ItemVariant).where(ItemVariant.id == unknown_variant.id)
            )

    db.session.flush()


def get_or_create_set(name: str) -> Set:
    """Return existing Set by name or create a new one."""
    item_set = Set.query.filter(db.func.lower(Set.name) == name.lower()).first()
    if not item_set:
        item_set = Set(name=name)
        db.session.add(item_set)
        db.session.flush()
    return item_set


def normalize_sku_value(value: str | None) -> str | None:
    """Normalize an SKU string for comparison and storage."""
    cleaned = re.sub(r"\s+", "", (value or "").strip()).upper()
    return cleaned or None


def parse_alternate_skus(raw_value: str | None) -> list[str]:
    """Parse a comma-separated alternate SKU field."""
    if not raw_value:
        return []
    values: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\n,;]+", raw_value):
        sku = normalize_sku_value(part)
        if not sku or sku in seen:
            continue
        seen.add(sku)
        values.append(sku)
    return values


def normalize_engraving_copy_type(value: str | None) -> str:
    """Normalize an ownership copy type."""
    cleaned = (value or "").strip().lower()
    return "engraved" if cleaned == "engraved" else "plain"


def normalize_engraving_signature(
    copy_type: str | None, engraving_text: str | None
) -> str:
    """Build a stable uniqueness signature for a physical copy."""
    normalized_copy_type = normalize_engraving_copy_type(copy_type)
    if normalized_copy_type != "engraved":
        return "plain"
    cleaned_text = re.sub(r"\s+", " ", (engraving_text or "").strip()).lower()
    return f"engraved:{cleaned_text}" if cleaned_text else "engraved"


def _now_utc() -> str:
    """Return the current UTC timestamp as an ISO string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_dump(value) -> str | None:
    """Serialize a value for storage in the activity payload."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _json_safe(value):
    """Convert a Python value into something JSON can represent cleanly."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _current_actor() -> str:
    """Return a compact label for the current request actor."""
    if not has_request_context():
        return "system"
    if session.get("is_admin") is True:
        return "admin"
    return "user"


def _current_source() -> str | None:
    """Return a compact label for the request or job source."""
    if not has_request_context():
        return None
    endpoint = request.endpoint or ""
    if endpoint:
        return endpoint
    return f"{request.method} {request.path}"


def _entity_label(obj) -> str | None:
    """Return a human-friendly label for a tracked model instance."""
    if isinstance(obj, Item):
        return obj.name
    if isinstance(obj, ItemVariant):
        item_name = obj.item.name if obj.item else "Item variant"
        return f"{item_name} · {obj.color}"
    if isinstance(obj, Set):
        return obj.name
    if isinstance(obj, Person):
        return obj.name
    if isinstance(obj, Ownership):
        item_name = (
            obj.variant.item.name if obj.variant and obj.variant.item else "Item"
        )
        person_name = obj.person.name if obj.person else "Collector"
        return f"{person_name} · {item_name}"
    if isinstance(obj, CookwareSession):
        return f"{obj.item.name if obj.item else 'Cookware'} · {obj.used_on}"
    if isinstance(obj, SharpeningLog):
        return f"{obj.item.name if obj.item else 'Knife'} · {obj.sharpened_on}"
    if isinstance(obj, KnifeTask):
        return obj.name
    if isinstance(obj, KnifeTaskLog):
        task_name = obj.task.name if obj.task else "Task"
        item_name = obj.item.name if obj.item else "Item"
        return f"{task_name} · {item_name}"
    if isinstance(obj, ItemSetMember):
        item_name = obj.item.name if obj.item else "Item"
        set_name = obj.set.name if obj.set else "Set"
        return f"{set_name} · {item_name}"
    if isinstance(obj, ItemAttachment):
        item_name = obj.item.name if obj.item else "Item"
        return f"{item_name} · {obj.original_filename}"
    return obj.__class__.__name__


def _humanize_model_name(model_name: str) -> str:
    """Convert a CamelCase model name into a readable label."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", model_name).strip()


def record_audit_event(
    *,
    kind: str = "audit",
    title: str,
    details: str | None = None,
    actor: str | None = None,
    actor_user_id: int | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    entity_name: str | None = None,
    source: str | None = None,
    payload=None,
    occurred_at: str | None = None,
) -> None:
    """Record an activity event, optionally with audit metadata."""
    db.session.add(
        ActivityEvent(
            kind=kind,
            title=title,
            details=details,
            occurred_at=occurred_at or _now_utc(),
            actor=actor or _current_actor(),
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            source=source or _current_source(),
            payload=_json_dump(payload),
        )
    )


def _validate_user_account(user: User) -> None:
    """Validate source-specific account requirements before persistence."""
    if not user.username:
        raise ValueError("Username is required.")
    if user.role not in USER_ROLES:
        raise ValueError(f"Unsupported user role: {user.role!r}")
    if user.auth_source not in USER_AUTH_SOURCES:
        raise ValueError(f"Unsupported authentication source: {user.auth_source!r}")
    if (user.session_version or 0) < 1:
        raise ValueError("Session version must be at least 1.")
    if user.auth_source == USER_AUTH_SOURCE_LOCAL:
        if not user.password_hash:
            raise ValueError("Local users require a password.")
        if user.external_subject:
            raise ValueError("Local users cannot have an external subject.")
    elif not user.external_subject:
        raise ValueError("Proxy users require an external subject.")
    elif user.password_hash:
        raise ValueError("Proxy users cannot have a local password.")


@event.listens_for(SASession, "before_flush")
def _protect_user_account_invariants(session, flush_context, instances) -> None:
    """Validate accounts and prevent removal of the last active admin."""
    changed_users = {
        user
        for user in (*session.new, *session.dirty, *session.deleted)
        if isinstance(user, User)
    }
    if not changed_users:
        return

    for user in changed_users:
        if user not in session.deleted:
            _validate_user_account(user)

    persisted_admin_ids = set(
        session.scalars(
            db.select(User.id).where(
                User.role == USER_ROLE_ADMIN,
                User.is_active.is_(True),
            )
        ).all()
    )
    if not persisted_admin_ids:
        return

    resulting_admins: set[int | str] = set(persisted_admin_ids)
    for user in changed_users:
        if user.id is not None:
            resulting_admins.discard(user.id)
        if (
            user not in session.deleted
            and user.is_active
            and user.role == USER_ROLE_ADMIN
        ):
            resulting_admins.add(user.id or f"new:{id(user)}")

    if not resulting_admins:
        raise ValueError("Cannot remove, disable, or demote the last active admin.")


def record_activity(
    kind: str, title: str, details: str | None = None, occurred_at: str | None = None
) -> None:
    """Record a dashboard activity event."""
    record_audit_event(kind=kind, title=title, details=details, occurred_at=occurred_at)


def _audit_tracked_models() -> tuple[type[BaseModel], ...]:
    """Return the model classes that should appear in the audit trail."""
    return (
        Item,
        ItemVariant,
        Set,
        SetVariant,
        Person,
        Ownership,
        CookwareSession,
        SharpeningLog,
        KnifeTask,
        KnifeTaskLog,
        ItemSetMember,
        ItemAttachment,
    )


def _audit_snapshot(obj, action: str) -> dict | None:
    """Build a payload snapshot for a tracked ORM object."""
    state = sa_inspect(obj)
    if action == "update":
        changes = {}
        for attr in state.mapper.column_attrs:
            key = attr.key
            if key == "id":
                continue
            history = state.attrs[key].history
            if not history.has_changes():
                continue
            before = history.deleted[-1] if history.deleted else None
            after = history.added[-1] if history.added else getattr(obj, key, None)
            changes[key] = {"before": _json_safe(before), "after": _json_safe(after)}
        if not changes:
            return None
        return {"changes": changes}

    if action == "create":
        fields = {}
        for attr in state.mapper.column_attrs:
            key = attr.key
            if key == "id":
                continue
            fields[key] = _json_safe(getattr(obj, key, None))
        return {"fields": fields}

    if action == "delete":
        fields = {}
        for attr in state.mapper.column_attrs:
            key = attr.key
            if key == "id":
                continue
            fields[key] = _json_safe(getattr(obj, key, None))
        return {"fields": fields}

    return None


@event.listens_for(SASession, "before_flush")
def _collect_audit_events(session, flush_context, instances) -> None:
    """Capture ORM changes before SQLAlchemy flushes them."""
    if session.info.get("_audit_write_in_progress"):
        return

    tracked = _audit_tracked_models()
    pending = session.info.setdefault("_pending_audit_events", [])

    for obj in session.new:
        if isinstance(obj, ActivityEvent) or not isinstance(obj, tracked):
            continue
        snapshot = _audit_snapshot(obj, "create")
        if snapshot is None:
            continue
        pending.append(
            {
                "obj": obj,
                "title": f"Created {_humanize_model_name(obj.__class__.__name__).lower()}",
                "action": "create",
                "entity_type": obj.__class__.__name__,
                "entity_name": _entity_label(obj),
                "payload": snapshot,
            }
        )

    for obj in session.dirty:
        if isinstance(obj, ActivityEvent) or not isinstance(obj, tracked):
            continue
        if not session.is_modified(obj, include_collections=False):
            continue
        snapshot = _audit_snapshot(obj, "update")
        if snapshot is None:
            continue
        pending.append(
            {
                "obj": obj,
                "title": f"Updated {_humanize_model_name(obj.__class__.__name__).lower()}",
                "action": "update",
                "entity_type": obj.__class__.__name__,
                "entity_name": _entity_label(obj),
                "payload": snapshot,
            }
        )

    for obj in session.deleted:
        if isinstance(obj, ActivityEvent) or not isinstance(obj, tracked):
            continue
        snapshot = _audit_snapshot(obj, "delete")
        if snapshot is None:
            continue
        pending.append(
            {
                "obj": obj,
                "title": f"Deleted {_humanize_model_name(obj.__class__.__name__).lower()}",
                "action": "delete",
                "entity_type": obj.__class__.__name__,
                "entity_name": _entity_label(obj),
                "payload": snapshot,
            }
        )


@event.listens_for(SASession, "after_flush_postexec")
def _write_audit_events(session, flush_context) -> None:
    """Persist captured audit events after primary rows have IDs."""
    pending = session.info.pop("_pending_audit_events", [])
    if not pending:
        return

    session.info["_audit_write_in_progress"] = True
    try:
        for entry in pending:
            entity_id = getattr(entry.get("obj"), "id", None)
            session.add(
                ActivityEvent(
                    kind="audit",
                    title=entry["title"],
                    details=entry["entity_name"],
                    occurred_at=_now_utc(),
                    actor=_current_actor(),
                    action=entry["action"],
                    entity_type=entry["entity_type"],
                    entity_id=entity_id,
                    entity_name=entry["entity_name"],
                    source=_current_source(),
                    payload=_json_dump(entry["payload"]),
                )
            )
    finally:
        session.info.pop("_audit_write_in_progress", None)


def get_recent_audit_events(
    limit: int = 50, *, action: str | None = None, entity_type: str | None = None
) -> list[dict]:
    """Return recent audit events as dictionaries."""
    query = db.select(ActivityEvent).where(ActivityEvent.kind == "audit")
    if action:
        query = query.where(ActivityEvent.action == action)
    if entity_type:
        query = query.where(ActivityEvent.entity_type == entity_type)
    rows = (
        db.session.execute(
            query.order_by(
                ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc()
            ).limit(limit)
        )
        .scalars()
        .all()
    )
    events = []
    for row in rows:
        payload = None
        if row.payload:
            try:
                payload = json.loads(row.payload)
            except json.JSONDecodeError:
                payload = {"raw": row.payload}
        events.append(
            {
                "id": row.id,
                "kind": row.kind,
                "title": row.title,
                "details": row.details,
                "occurred_at": row.occurred_at,
                "actor": row.actor,
                "actor_user_id": row.actor_user_id,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "entity_name": row.entity_name,
                "source": row.source,
                "payload": payload,
            }
        )
    return events


def get_latest_activity(kind: str) -> dict | None:
    """Return the most recent activity event for a kind."""
    event = (
        db.session.execute(
            db.select(ActivityEvent)
            .filter_by(kind=kind)
            .order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if event is None:
        return None
    return {
        "kind": event.kind,
        "title": event.title,
        "details": event.details,
        "occurred_at": event.occurred_at,
    }

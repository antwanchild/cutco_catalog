from extensions import db
from constants import COOKWARE_CATEGORIES, UNKNOWN_COLOR
from datetime import UTC, datetime


# Association object: items <-> sets (with quantity per member)
class ItemSetMember(db.Model):
    __tablename__ = "item_sets"

    item_id  = db.Column(db.Integer, db.ForeignKey("items.id"), primary_key=True)
    set_id   = db.Column(db.Integer, db.ForeignKey("sets.id"),  primary_key=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    item = db.relationship("Item", back_populates="set_memberships")
    set  = db.relationship("Set",  back_populates="members")

# Many-to-many join table: items <-> knife_tasks (Cutco-sourced suggested uses)
item_tasks = db.Table(
    "item_tasks",
    db.Column("item_id", db.Integer, db.ForeignKey("items.id"),        primary_key=True),
    db.Column("task_id", db.Integer, db.ForeignKey("knife_tasks.id"),  primary_key=True),
)


class Item(db.Model):
    __tablename__ = "items"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(160), nullable=False)
    sku        = db.Column(db.String(60),  nullable=True, unique=True)
    category   = db.Column(db.String(80),  nullable=True)
    edge_type  = db.Column(db.String(40),  nullable=False, default="Unknown")
    is_unicorn = db.Column(db.Boolean,     nullable=False, default=False)
    edge_is_unicorn = db.Column(db.Boolean, nullable=False, default=False)
    in_catalog = db.Column(db.Boolean,     nullable=False, default=True)
    cutco_url      = db.Column(db.String(300), nullable=True)
    msrp           = db.Column(db.Float,       nullable=True)
    blade_length   = db.Column(db.String(20),  nullable=True)
    overall_length = db.Column(db.String(20),  nullable=True)
    weight         = db.Column(db.String(20),  nullable=True)
    notes          = db.Column(db.Text,        nullable=True)

    variants        = db.relationship("ItemVariant", backref="item",
                                    lazy=True, cascade="all, delete-orphan",
                                    order_by="ItemVariant.color")
    set_memberships = db.relationship("ItemSetMember", back_populates="item",
                                    cascade="all, delete-orphan")
    sets            = db.relationship("Set", secondary="item_sets",
                                    back_populates="items", viewonly=True, lazy="select")
    suggested_tasks = db.relationship("KnifeTask", secondary="item_tasks",
                                    back_populates="suggested_items", lazy="select")

    @property
    def any_unicorn(self) -> bool:
        """True if the item, edge type, or any specific variant is flagged unicorn."""
        return self.is_unicorn or self.edge_is_unicorn or any(variant.is_unicorn for variant in self.variants)


class ItemVariant(db.Model):
    __tablename__ = "item_variants"

    id         = db.Column(db.Integer, primary_key=True)
    item_id    = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    color      = db.Column(db.String(80), nullable=False, default=UNKNOWN_COLOR)
    is_unicorn = db.Column(db.Boolean,    nullable=False, default=False)
    notes      = db.Column(db.Text, nullable=True)

    ownerships = db.relationship("Ownership", backref="variant",
                                 lazy=True, cascade="all, delete-orphan")


class Set(db.Model):
    __tablename__ = "sets"

    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(120), nullable=False, unique=True)
    sku   = db.Column(db.String(20),  nullable=True)
    notes = db.Column(db.Text, nullable=True)

    members = db.relationship("ItemSetMember", back_populates="set",
                             cascade="all, delete-orphan")
    items   = db.relationship("Item", secondary="item_sets",
                             back_populates="sets", viewonly=True, lazy="select")


class Person(db.Model):
    __tablename__ = "people"

    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text, nullable=True)

    ownerships = db.relationship("Ownership", backref="person",
                                 lazy=True, cascade="all, delete-orphan")


class Ownership(db.Model):
    __tablename__ = "ownership"

    id           = db.Column(db.Integer, primary_key=True)
    variant_id   = db.Column(db.Integer, db.ForeignKey("item_variants.id"), nullable=False)
    person_id    = db.Column(db.Integer, db.ForeignKey("people.id"),        nullable=False)
    status       = db.Column(db.String(20), nullable=False, default="Owned")
    target_price = db.Column(db.Float, nullable=True)
    notes        = db.Column(db.Text, nullable=True)

    __table_args__ = (db.UniqueConstraint("variant_id", "person_id",
                                          name="uq_variant_person"),)


class ActivityEvent(db.Model):
    __tablename__ = "activity_events"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    details = db.Column(db.Text, nullable=True)
    occurred_at = db.Column(db.String(32), nullable=False, index=True)


class CookwareSession(db.Model):
    __tablename__ = "cookware_sessions"

    id         = db.Column(db.Integer, primary_key=True)
    item_id    = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    used_on    = db.Column(db.String(10), nullable=False)   # ISO date YYYY-MM-DD
    made_item  = db.Column(db.String(200), nullable=False)
    rating     = db.Column(db.Integer, nullable=True)        # 1–5
    notes      = db.Column(db.Text, nullable=True)

    item = db.relationship("Item", backref=db.backref(
        "cookware_sessions", lazy=True,
        order_by="CookwareSession.used_on.desc()",
    ))


class SharpeningLog(db.Model):
    __tablename__ = "sharpening_log"

    id           = db.Column(db.Integer, primary_key=True)
    item_id      = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    sharpened_on = db.Column(db.String(10), nullable=False)   # ISO date YYYY-MM-DD
    method       = db.Column(db.String(60), nullable=False, default="Home Sharpener")
    notes        = db.Column(db.Text, nullable=True)

    item = db.relationship("Item", backref=db.backref(
        "sharpening_log", lazy=True,
        order_by="SharpeningLog.sharpened_on.desc()",
    ))


class KnifeTask(db.Model):
    __tablename__ = "knife_tasks"

    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(120), nullable=False, unique=True)
    is_preset = db.Column(db.Boolean, nullable=False, default=False)

    suggested_items = db.relationship("Item", secondary="item_tasks",
                                      back_populates="suggested_tasks", lazy="select")


class KnifeTaskLog(db.Model):
    __tablename__ = "knife_task_log"

    id        = db.Column(db.Integer, primary_key=True)
    item_id   = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    task_id   = db.Column(db.Integer, db.ForeignKey("knife_tasks.id"), nullable=False)
    notes     = db.Column(db.Text, nullable=True)
    logged_on = db.Column(db.String(10), nullable=False)   # ISO date YYYY-MM-DD

    item = db.relationship("Item", backref=db.backref(
        "task_log", lazy=True,
        order_by="KnifeTaskLog.logged_on.desc()",
    ))
    task = db.relationship("KnifeTask", backref=db.backref("log_entries", lazy=True))


def ensure_unknown_variant(item: Item) -> None:
    """Guarantee every item has an 'Unknown / Unspecified' color variant."""
    if not any(variant.color == UNKNOWN_COLOR for variant in item.variants):
        db.session.add(ItemVariant(item_id=item.id, color=UNKNOWN_COLOR))
        db.session.flush()


def reconcile_unknown_variant(item: Item) -> None:
    """Keep Unknown only when an item has no real color variants.

    Safety rule: if an Unknown variant already has ownership records,
    keep it to avoid breaking historical links.
    """
    real_variants = [variant for variant in item.variants if variant.color != UNKNOWN_COLOR]
    unknown_variants = [variant for variant in item.variants if variant.color == UNKNOWN_COLOR]

    # Cookware is treated as single-variant: keep Unknown only.
    if (item.category or "") in COOKWARE_CATEGORIES:
        if not unknown_variants:
            ensure_unknown_variant(item)
            unknown_variants = [variant for variant in item.variants if variant.color == UNKNOWN_COLOR]
        for real_variant in real_variants:
            if not real_variant.ownerships:
                db.session.delete(real_variant)
        db.session.flush()
        return

    if not real_variants:
        if not unknown_variants:
            ensure_unknown_variant(item)
        return

    for unknown_variant in unknown_variants:
        if not unknown_variant.ownerships:
            db.session.delete(unknown_variant)

    db.session.flush()


def get_or_create_set(name: str) -> Set:
    """Return existing Set by name or create a new one."""
    item_set = Set.query.filter(db.func.lower(Set.name) == name.lower()).first()
    if not item_set:
        item_set = Set(name=name)
        db.session.add(item_set)
        db.session.flush()
    return item_set


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_activity(kind: str, title: str, details: str | None = None, occurred_at: str | None = None) -> None:
    db.session.add(ActivityEvent(
        kind=kind,
        title=title,
        details=details,
        occurred_at=occurred_at or _now_utc(),
    ))


def get_latest_activity(kind: str) -> dict | None:
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

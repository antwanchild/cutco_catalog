from extensions import db
from constants import UNKNOWN_COLOR


# Many-to-many join table: items <-> sets
item_sets = db.Table(
    "item_sets",
    db.Column("item_id", db.Integer, db.ForeignKey("items.id"), primary_key=True),
    db.Column("set_id",  db.Integer, db.ForeignKey("sets.id"),  primary_key=True),
)


class Item(db.Model):
    __tablename__ = "items"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(160), nullable=False)
    sku        = db.Column(db.String(60),  nullable=True, unique=True)
    category   = db.Column(db.String(80),  nullable=True)
    edge_type  = db.Column(db.String(40),  nullable=False, default="Unknown")
    is_unicorn = db.Column(db.Boolean,     nullable=False, default=False)
    in_catalog = db.Column(db.Boolean,     nullable=False, default=True)
    cutco_url  = db.Column(db.String(300), nullable=True)
    msrp       = db.Column(db.Float,       nullable=True)
    notes      = db.Column(db.Text,        nullable=True)

    variants = db.relationship("ItemVariant", backref="item",
                               lazy=True, cascade="all, delete-orphan",
                               order_by="ItemVariant.color")
    sets     = db.relationship("Set", secondary=item_sets,
                               back_populates="items", lazy="select")

    @property
    def any_unicorn(self) -> bool:
        """True if the item itself is flagged unicorn OR any specific variant is."""
        return self.is_unicorn or any(variant.is_unicorn for variant in self.variants)


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

    items = db.relationship("Item", secondary=item_sets,
                            back_populates="sets", lazy="select")


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


class BakewareSession(db.Model):
    __tablename__ = "bakeware_sessions"

    id         = db.Column(db.Integer, primary_key=True)
    item_id    = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    baked_on   = db.Column(db.String(10), nullable=False)   # ISO date YYYY-MM-DD
    what_made  = db.Column(db.String(200), nullable=False)
    rating     = db.Column(db.Integer, nullable=True)        # 1–5
    notes      = db.Column(db.Text, nullable=True)

    item = db.relationship("Item", backref=db.backref(
        "bakeware_sessions", lazy=True,
        order_by="BakewareSession.baked_on.desc()",
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


def ensure_unknown_variant(item: Item) -> None:
    """Guarantee every item has an 'Unknown / Unspecified' color variant."""
    if not any(v.color == UNKNOWN_COLOR for v in item.variants):
        db.session.add(ItemVariant(item_id=item.id, color=UNKNOWN_COLOR))
        db.session.flush()


def get_or_create_set(name: str) -> Set:
    """Return existing Set by name or create a new one."""
    item_set = Set.query.filter(db.func.lower(Set.name) == name.lower()).first()
    if not item_set:
        item_set = Set(name=name)
        db.session.add(item_set)
        db.session.flush()
    return item_set

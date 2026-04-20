import logging
from datetime import UTC, datetime

from sqlalchemy import inspect as sa_inspect

from constants import KNIFE_TASK_PRESETS, canonicalize_category
from extensions import db
from models import Item, KnifeTask, ensure_unknown_variant

logger = logging.getLogger(__name__)

BOOTSTRAP_STATE_NAME = "bootstrap"
BOOTSTRAP_VERSION = 5


class BootstrapState(db.Model):
    __tablename__ = "bootstrap_state"

    name = db.Column(db.String(40), primary_key=True)
    version = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.String(32), nullable=False)


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_bootstrap_version() -> int:
    state = db.session.get(BootstrapState, BOOTSTRAP_STATE_NAME)
    return state.version if state else 0


def get_bootstrap_state() -> dict:
    state = db.session.get(BootstrapState, BOOTSTRAP_STATE_NAME)
    if state is None:
        return {"name": BOOTSTRAP_STATE_NAME, "version": 0, "updated_at": None}
    return {"name": state.name, "version": state.version, "updated_at": state.updated_at}


def _set_bootstrap_version(version: int) -> None:
    state = db.session.get(BootstrapState, BOOTSTRAP_STATE_NAME)
    if state is None:
        state = BootstrapState(name=BOOTSTRAP_STATE_NAME, version=version, updated_at=_now_utc())
        db.session.add(state)
    else:
        state.version = version
        state.updated_at = _now_utc()


def _apply_column_migrations() -> None:
    column_migrations = [
        ("sets", "sku", "ALTER TABLE sets ADD COLUMN sku VARCHAR(20)"),
        (
            "item_variants",
            "is_unicorn",
            "ALTER TABLE item_variants ADD COLUMN is_unicorn BOOLEAN NOT NULL DEFAULT 0",
        ),
        ("items", "msrp", "ALTER TABLE items ADD COLUMN msrp REAL"),
        ("ownership", "target_price", "ALTER TABLE ownership ADD COLUMN target_price REAL"),
        ("item_sets", "quantity", "ALTER TABLE item_sets ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"),
        ("items", "blade_length", "ALTER TABLE items ADD COLUMN blade_length VARCHAR(20)"),
        ("items", "overall_length", "ALTER TABLE items ADD COLUMN overall_length VARCHAR(20)"),
        ("items", "weight", "ALTER TABLE items ADD COLUMN weight VARCHAR(20)"),
        (
            "items",
            "edge_is_unicorn",
            "ALTER TABLE items ADD COLUMN edge_is_unicorn BOOLEAN NOT NULL DEFAULT 0",
        ),
    ]

    inspector = sa_inspect(db.engine)
    with db.engine.connect() as conn:
        for table_name, column_name, statement in column_migrations:
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            if column_name not in existing:
                conn.execute(db.text(statement))
                conn.commit()
                logger.info("Schema migration: added %s.%s", table_name, column_name)


def _seed_default_tasks() -> None:
    existing_task_names = {task.name for task in KnifeTask.query.all()}
    for preset in KNIFE_TASK_PRESETS:
        if preset not in existing_task_names:
            db.session.add(KnifeTask(name=preset, is_preset=True))
    db.session.flush()


def _cleanup_invalid_items() -> None:
    invalid_items = Item.query.filter(Item.sku.op("GLOB")("[0-9]")).all()
    for item in invalid_items:
        logger.info("Removing item with invalid single-digit SKU: %s (sku=%s)", item.name, item.sku)
        db.session.delete(item)


def _normalize_categories() -> None:
    renamed_categories = 0
    items = Item.query.all()
    for item in items:
        canonical_category = canonicalize_category(item.category)
        if canonical_category != item.category:
            item.category = canonical_category
            renamed_categories += 1
    if renamed_categories:
        logger.info("Category normalization: updated %d item category value(s)", renamed_categories)


def _ensure_unknown_variants() -> None:
    for item in Item.query.all():
        ensure_unknown_variant(item)


def initialize_database() -> None:
    db.Model.metadata.create_all(db.engine, checkfirst=True)

    current_version = _get_bootstrap_version()
    steps = [
        (1, _apply_column_migrations),
        (2, _seed_default_tasks),
        (3, _cleanup_invalid_items),
        (4, _normalize_categories),
        (5, _ensure_unknown_variants),
    ]

    for version, step in steps:
        if current_version >= version:
            continue
        step()
        _set_bootstrap_version(version)
        db.session.commit()
        current_version = version

    if current_version < BOOTSTRAP_VERSION:
        _set_bootstrap_version(BOOTSTRAP_VERSION)
        db.session.commit()

    logger.info("Database ready")

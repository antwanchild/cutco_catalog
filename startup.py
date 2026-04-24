import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from constants import KNIFE_TASK_PRESETS, canonicalize_availability, canonicalize_category
from extensions import db
from models import Item, KnifeTask, Ownership, ensure_unknown_variant
from schema_migrations import apply_schema_migrations

logger = logging.getLogger(__name__)

BOOTSTRAP_STATE_NAME = "bootstrap"


class BootstrapState(db.Model):
    __tablename__ = "bootstrap_state"

    name = db.Column(db.String(40), primary_key=True)
    version = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.String(32), nullable=False)


class BootstrapHistory(db.Model):
    __tablename__ = "bootstrap_history"

    version = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    applied_at = db.Column(db.String(32), nullable=False)


@dataclass(frozen=True, slots=True)
class BootstrapMigration:
    version: int
    name: str
    apply: Callable[[], None]


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


def get_bootstrap_history(limit: int = 10) -> list[dict]:
    history = (
        db.session.execute(
            db.select(BootstrapHistory).order_by(BootstrapHistory.version.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {"version": row.version, "name": row.name, "applied_at": row.applied_at}
        for row in history
    ]


def _set_bootstrap_version(version: int) -> None:
    state = db.session.get(BootstrapState, BOOTSTRAP_STATE_NAME)
    if state is None:
        state = BootstrapState(name=BOOTSTRAP_STATE_NAME, version=version, updated_at=_now_utc())
        db.session.add(state)
    else:
        state.version = version
        state.updated_at = _now_utc()


def _record_history(version: int, name: str) -> None:
    history = db.session.get(BootstrapHistory, version)
    if history is None:
        db.session.add(BootstrapHistory(version=version, name=name, applied_at=_now_utc()))
    else:
        history.name = name
        history.applied_at = _now_utc()


def _backfill_history(current_version: int) -> bool:
    if current_version <= 0:
        return False

    state = db.session.get(BootstrapState, BOOTSTRAP_STATE_NAME)
    applied_at = state.updated_at if state else _now_utc()
    added = False
    for migration in BOOTSTRAP_MIGRATIONS:
        if migration.version > current_version:
            break
        if db.session.get(BootstrapHistory, migration.version) is None:
            db.session.add(
                BootstrapHistory(
                    version=migration.version,
                    name=migration.name,
                    applied_at=applied_at,
                )
            )
            added = True
    return added


BOOTSTRAP_MIGRATIONS: tuple[BootstrapMigration, ...] = (
    BootstrapMigration(1, "seed_tasks", lambda: _seed_default_tasks()),
    BootstrapMigration(2, "cleanup_invalid_skus", lambda: _cleanup_invalid_items()),
    BootstrapMigration(3, "normalize_categories", lambda: _normalize_categories()),
    BootstrapMigration(4, "ensure_unknown_variants", lambda: _ensure_unknown_variants()),
    BootstrapMigration(5, "split_ownership_quantity_notes", lambda: _split_quantity_notes()),
    BootstrapMigration(6, "normalize_availability", lambda: _normalize_availability()),
)

BOOTSTRAP_VERSION = BOOTSTRAP_MIGRATIONS[-1].version


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


def _normalize_availability() -> None:
    updated = 0
    for item in Item.query.all():
        target = "non-catalog" if item.set_only or not item.in_catalog else "public"
        canonical_availability = canonicalize_availability(item.availability)
        if canonical_availability != target:
            item.availability = target
            updated += 1
        elif item.availability != canonical_availability:
            item.availability = canonical_availability
            updated += 1
    if updated:
        logger.info("Availability normalization: updated %d item value(s)", updated)


def _ensure_unknown_variants() -> None:
    for item in Item.query.all():
        ensure_unknown_variant(item)


def _split_quantity_fields_from_notes(notes: str | None) -> tuple[int | None, int | None, str | None]:
    if not notes:
        return None, None, None

    parts = [part.strip() for part in notes.split(";") if part.strip()]
    purchased = None
    given_away = None
    kept_parts: list[str] = []
    for part in parts:
        match = re.fullmatch(r"Quantity Purchased:\s*(\d+)", part, re.IGNORECASE)
        if match:
            purchased = int(match.group(1))
            continue
        match = re.fullmatch(r"(?:Quantity )?Given Away:\s*(\d+)", part, re.IGNORECASE)
        if match:
            given_away = int(match.group(1))
            continue
        kept_parts.append(part)

    cleaned_notes = "; ".join(kept_parts) or None
    return purchased, given_away, cleaned_notes


def _split_quantity_notes() -> None:
    updated = 0
    for ownership in Ownership.query.all():
        purchased, given_away, cleaned_notes = _split_quantity_fields_from_notes(ownership.notes)
        changed = False
        if purchased is not None and ownership.quantity_purchased is None:
            ownership.quantity_purchased = purchased
            changed = True
        if given_away is not None and ownership.quantity_given_away is None:
            ownership.quantity_given_away = given_away
            changed = True
        if cleaned_notes != ownership.notes:
            ownership.notes = cleaned_notes
            changed = True
        if changed:
            updated += 1
    if updated:
        logger.info("Quantity note backfill: updated %d ownership record(s)", updated)


def initialize_database() -> None:
    db.Model.metadata.create_all(db.engine, checkfirst=True)
    apply_schema_migrations()

    current_version = _get_bootstrap_version()
    backfilled = _backfill_history(current_version)
    for migration in BOOTSTRAP_MIGRATIONS:
        version = migration.version
        if current_version >= version:
            continue
        logger.info("Bootstrap migration %s v%d", migration.name, version)
        migration.apply()
        _record_history(version, migration.name)
        _set_bootstrap_version(version)
        db.session.commit()
        current_version = version

    if backfilled:
        db.session.commit()

    if current_version < BOOTSTRAP_VERSION:
        _set_bootstrap_version(BOOTSTRAP_VERSION)
        db.session.commit()

    logger.info("Database ready")

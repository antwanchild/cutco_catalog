import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import inspect as sa_inspect

from extensions import db

logger = logging.getLogger(__name__)

SCHEMA_STATE_NAME = "schema"


class SchemaState(db.Model):
    __tablename__ = "schema_state"

    name = db.Column(db.String(40), primary_key=True)
    version = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.String(32), nullable=False)


class SchemaHistory(db.Model):
    __tablename__ = "schema_history"

    version = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    applied_at = db.Column(db.String(32), nullable=False)


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    version: int
    name: str
    apply: Callable[[], None]


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_schema_version() -> int:
    state = db.session.get(SchemaState, SCHEMA_STATE_NAME)
    return state.version if state else 0


def get_schema_state() -> dict:
    state = db.session.get(SchemaState, SCHEMA_STATE_NAME)
    if state is None:
        return {"name": SCHEMA_STATE_NAME, "version": 0, "updated_at": None}
    return {"name": state.name, "version": state.version, "updated_at": state.updated_at}


def get_schema_history(limit: int = 10) -> list[dict]:
    history = (
        db.session.execute(db.select(SchemaHistory).order_by(SchemaHistory.version.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        {"version": row.version, "name": row.name, "applied_at": row.applied_at}
        for row in history
    ]


def _set_schema_version(version: int) -> None:
    state = db.session.get(SchemaState, SCHEMA_STATE_NAME)
    if state is None:
        state = SchemaState(name=SCHEMA_STATE_NAME, version=version, updated_at=_now_utc())
        db.session.add(state)
    else:
        state.version = version
        state.updated_at = _now_utc()


def _record_history(version: int, name: str) -> None:
    history = db.session.get(SchemaHistory, version)
    if history is None:
        db.session.add(SchemaHistory(version=version, name=name, applied_at=_now_utc()))
    else:
        history.name = name
        history.applied_at = _now_utc()


def _backfill_history(current_version: int, migrations: tuple[SchemaMigration, ...]) -> bool:
    if current_version <= 0:
        return False

    state = db.session.get(SchemaState, SCHEMA_STATE_NAME)
    applied_at = state.updated_at if state else _now_utc()
    added = False
    for migration in migrations:
        if migration.version > current_version:
            break
        if db.session.get(SchemaHistory, migration.version) is None:
            db.session.add(
                SchemaHistory(
                    version=migration.version,
                    name=migration.name,
                    applied_at=applied_at,
                )
            )
            added = True
    return added


def _add_column(table_name: str, column_name: str, statement: str) -> None:
    inspector = sa_inspect(db.engine)
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in existing:
        with db.engine.connect() as conn:
            conn.execute(db.text(statement))
            conn.commit()
            logger.info("Schema migration: added %s.%s", table_name, column_name)


def _schema_column_migrations() -> None:
    _add_column("sets", "sku", "ALTER TABLE sets ADD COLUMN sku VARCHAR(20)")
    _add_column(
        "item_variants",
        "is_unicorn",
        "ALTER TABLE item_variants ADD COLUMN is_unicorn BOOLEAN NOT NULL DEFAULT 0",
    )
    _add_column("items", "msrp", "ALTER TABLE items ADD COLUMN msrp REAL")
    _add_column("ownership", "target_price", "ALTER TABLE ownership ADD COLUMN target_price REAL")
    _add_column("item_sets", "quantity", "ALTER TABLE item_sets ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
    _add_column("items", "blade_length", "ALTER TABLE items ADD COLUMN blade_length VARCHAR(20)")
    _add_column("items", "overall_length", "ALTER TABLE items ADD COLUMN overall_length VARCHAR(20)")
    _add_column("items", "weight", "ALTER TABLE items ADD COLUMN weight VARCHAR(20)")
    _add_column(
        "items",
        "edge_is_unicorn",
        "ALTER TABLE items ADD COLUMN edge_is_unicorn BOOLEAN NOT NULL DEFAULT 0",
    )
    _add_column(
        "items",
        "set_only",
        "ALTER TABLE items ADD COLUMN set_only BOOLEAN NOT NULL DEFAULT 0",
    )


SCHEMA_MIGRATIONS: tuple[SchemaMigration, ...] = (
    SchemaMigration(1, "column_additions", _schema_column_migrations),
)

SCHEMA_VERSION = SCHEMA_MIGRATIONS[-1].version


def apply_schema_migrations() -> None:
    current_version = _get_schema_version()
    backfilled = _backfill_history(current_version, SCHEMA_MIGRATIONS)

    for migration in SCHEMA_MIGRATIONS:
        if current_version >= migration.version:
            continue
        logger.info("Schema migration %s v%d", migration.name, migration.version)
        migration.apply()
        _record_history(migration.version, migration.name)
        _set_schema_version(migration.version)
        db.session.commit()
        current_version = migration.version

    if backfilled:
        db.session.commit()

    if current_version < SCHEMA_VERSION:
        _set_schema_version(SCHEMA_VERSION)
        db.session.commit()

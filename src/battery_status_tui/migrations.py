"""Explicit writer-only SQLite schema migration runner."""

from __future__ import annotations

import sqlite3

from .schema import (
    CURRENT_SCHEMA_VERSION,
    V2_CREATE_STATEMENTS,
    V2_REQUIRED_TABLES,
    V2_SAMPLE_ADDITIONS,
)


BUSY_TIMEOUT_MS = 5_000


class SchemaError(RuntimeError):
    """Base class for database schema compatibility failures."""


class SchemaMigrationRequired(SchemaError):
    """The database is older or uninitialised and needs a writer migration."""


class UnsupportedSchemaVersion(SchemaError):
    """The database was created by a newer, unsupported application."""


class InvalidSchema(SchemaError):
    """The declared schema version does not contain its required objects."""


def schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _configure_common(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA synchronous = FULL")


def configure_reader(db: sqlite3.Connection) -> None:
    """Configure a read-only connection without changing persistent schema state."""
    _configure_common(db)
    db.execute("PRAGMA query_only = ON")


def configure_writer(db: sqlite3.Connection, *, set_journal_mode: bool = False) -> None:
    """Configure a writer; WAL selection must happen outside a transaction."""
    if set_journal_mode:
        if db.in_transaction:
            raise RuntimeError("journal_mode=WAL must be selected outside a transaction")
        mode = str(db.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            raise SchemaError(f"could not enable WAL journal mode (got {mode!r})")
    _configure_common(db)


def _validate_v2(db: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing_tables = V2_REQUIRED_TABLES - tables
    if missing_tables:
        raise InvalidSchema(f"schema v2 is missing tables: {', '.join(sorted(missing_tables))}")
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(samples)")}
    missing_columns = V2_SAMPLE_ADDITIONS.keys() - columns
    if missing_columns:
        raise InvalidSchema(f"schema v2 is missing sample columns: {', '.join(sorted(missing_columns))}")


def validate_reader_schema(db: sqlite3.Connection) -> None:
    """Accept only the current schema; never create or migrate from a reader."""
    version = schema_version(db)
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"database schema {version} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
        )
    if version < CURRENT_SCHEMA_VERSION:
        raise SchemaMigrationRequired(
            f"database schema {version} requires writer migration to {CURRENT_SCHEMA_VERSION}"
        )
    _validate_v2(db)


def _migrate_legacy_to_v2(db: sqlite3.Connection) -> None:
    for statement in V2_CREATE_STATEMENTS:
        db.execute(statement)
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(samples)")}
    for name, declaration in V2_SAMPLE_ADDITIONS.items():
        if name not in columns:
            db.execute(f"ALTER TABLE samples ADD COLUMN {name} {declaration}")
    db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


def run_writer_migrations(db: sqlite3.Connection) -> None:
    """Initialise or migrate a legacy database up to schema v2.

    Schema v2 is the last version this runner produces.  The definitive v1.0
    storage format is schema v4, created directly by ``V1Storage`` (or the
    offline pre-1.0 converter), not by stepwise migration through here.
    """
    configure_writer(db)
    version = schema_version(db)
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"database schema {version} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
        )
    configure_writer(db, set_journal_mode=True)
    if version == CURRENT_SCHEMA_VERSION:
        _validate_v2(db)
        return

    # Versions 0 and 1 both represent the pre-v2 layouts supported by the
    # previous ad-hoc Storage migration.  This dispatch stops at v2; schema v4
    # is produced out of band, not by a v2 -> v3 -> v4 chain here.
    db.execute("BEGIN IMMEDIATE")
    try:
        _migrate_legacy_to_v2(db)
        _validate_v2(db)
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()

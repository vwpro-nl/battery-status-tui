"""Validated, collision-safe backups for the future destructive v2 migration."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .migrations import BUSY_TIMEOUT_MS, configure_writer
from .schema import V2_REQUIRED_TABLES


class DatabaseBackupError(RuntimeError):
    """A pre-migration database backup could not be safely completed."""


class DatabaseBackupValidationError(DatabaseBackupError):
    """The source database or completed backup failed validation."""


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1_000
    )


def _quick_check(db: sqlite3.Connection) -> None:
    rows = tuple(str(row[0]) for row in db.execute("PRAGMA quick_check"))
    if rows != ("ok",):
        detail = "; ".join(rows) if rows else "no result"
        raise DatabaseBackupValidationError(f"SQLite quick_check failed: {detail}")


def _legacy_counts(db: sqlite3.Connection) -> dict[str, int]:
    tables = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = V2_REQUIRED_TABLES - tables
    if missing:
        raise DatabaseBackupValidationError(
            f"schema v2 is missing legacy tables: {', '.join(sorted(missing))}"
        )
    return {
        table: int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        for table in sorted(V2_REQUIRED_TABLES)
    }


def _validate_v2(
    db: sqlite3.Connection,
    *,
    expected_counts: Mapping[str, int] | None = None,
) -> dict[str, int]:
    _quick_check(db)
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version != 2:
        raise DatabaseBackupValidationError(
            f"pre-migration backup requires schema v2, found schema v{version}"
        )
    counts = _legacy_counts(db)
    if expected_counts is not None and counts != dict(expected_counts):
        raise DatabaseBackupValidationError(
            f"backup row counts differ from source: expected {dict(expected_counts)!r}, "
            f"found {counts!r}"
        )
    return counts


def _copy_with_sqlite_backup(source: Path, destination: Path) -> None:
    source_db = _read_only_connection(source)
    destination_db = sqlite3.connect(destination, timeout=BUSY_TIMEOUT_MS / 1_000)
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()


def _fsync_file(path: Path, mode: int) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup_name(source: Path, timestamp: datetime, sequence: int = 0) -> Path:
    stamp = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = "" if sequence == 0 else f"-{sequence}"
    return source.with_name(f"{source.name}.v2-{stamp}{suffix}.backup")


def create_validated_v2_backup(
    source: Path,
    *,
    timestamp: datetime | None = None,
) -> Path:
    """Create and validate a v2 backup while holding the exclusive writer lock.

    This is intentionally not called by the current v2 migration runner.  The
    future destructive v2 -> v3 step must call it before starting migration.
    """
    source = Path(source)
    if not source.is_file():
        raise DatabaseBackupError(f"database does not exist: {source}")

    lock_db: sqlite3.Connection | None = None
    temporary: Path | None = None
    published: Path | None = None
    succeeded = False
    try:
        lock_db = sqlite3.connect(source, timeout=BUSY_TIMEOUT_MS / 1_000)
        configure_writer(lock_db)
        lock_db.execute("BEGIN EXCLUSIVE")
        source_counts = _validate_v2(lock_db)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{source.name}.v2-backup-",
            suffix=".tmp",
            dir=source.parent,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        _copy_with_sqlite_backup(source, temporary)

        with closing(_read_only_connection(temporary)) as backup_db:
            _validate_v2(backup_db, expected_counts=source_counts)

        # Never grant a permission bit absent from the source; also strip
        # execute and group/other write bits from the database backup.
        backup_mode = stat.S_IMODE(source.stat().st_mode) & 0o644
        _fsync_file(temporary, backup_mode)

        created_at = timestamp or datetime.now(timezone.utc)
        sequence = 0
        while True:
            candidate = _backup_name(source, created_at, sequence)
            try:
                # A hard link is an atomic, no-replace publication operation.
                os.link(temporary, candidate)
            except FileExistsError:
                sequence += 1
                continue
            published = candidate
            break
        temporary.unlink()
        temporary = None
        _fsync_directory(source.parent)

        with closing(_read_only_connection(published)) as backup_db:
            _validate_v2(backup_db, expected_counts=source_counts)
        succeeded = True
        return published
    except DatabaseBackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise DatabaseBackupError(f"could not create validated v2 backup: {error}") from error
    finally:
        if lock_db is not None:
            if lock_db.in_transaction:
                lock_db.rollback()
            lock_db.close()
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if published is not None and not succeeded:
            try:
                published.unlink(missing_ok=True)
                _fsync_directory(source.parent)
            except OSError:
                pass

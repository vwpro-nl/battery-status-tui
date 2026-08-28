from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from battery_status_tui import backup
from battery_status_tui.backup import (
    DatabaseBackupError,
    DatabaseBackupValidationError,
    create_validated_v2_backup,
)
from battery_status_tui.schema import V2_REQUIRED_TABLES
from battery_status_tui.storage import Storage


class V2BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "history.sqlite3"
        Storage(self.path).initialize_writer()
        db = sqlite3.connect(self.path)
        db.execute("INSERT INTO metadata(key, value) VALUES('test', 'preserved')")
        db.commit()
        db.close()

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def _counts(path: Path) -> dict[str, int]:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {
                name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                for name in V2_REQUIRED_TABLES
            }
        finally:
            db.close()

    def test_valid_v2_database_is_backed_up_and_read_only_compatible(self):
        result = create_validated_v2_backup(self.path)

        self.assertTrue(result.is_file())
        self.assertEqual(self._counts(result), self._counts(self.path))
        db = sqlite3.connect(f"file:{result}?mode=ro", uri=True)
        try:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT value FROM metadata WHERE key='test'").fetchone()[0],
                "preserved",
            )
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("CREATE TABLE forbidden(value INTEGER)")
        finally:
            db.close()

    def test_backup_includes_committed_uncheckpointed_wal_data(self):
        writer = sqlite3.connect(self.path)
        try:
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute("INSERT INTO metadata(key, value) VALUES('wal', 'present')")
            writer.commit()
            self.assertTrue(Path(f"{self.path}-wal").exists())

            result = create_validated_v2_backup(self.path)
            db = sqlite3.connect(f"file:{result}?mode=ro", uri=True)
            try:
                self.assertEqual(
                    db.execute("SELECT value FROM metadata WHERE key='wal'").fetchone()[0],
                    "present",
                )
            finally:
                db.close()
        finally:
            writer.close()

    def test_source_quick_check_failure_blocks_backup(self):
        original = backup._quick_check
        calls = 0

        def fail_first(db):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DatabaseBackupValidationError("SQLite quick_check failed: corrupt")
            return original(db)

        with mock.patch.object(backup, "_quick_check", side_effect=fail_first):
            with self.assertRaisesRegex(DatabaseBackupValidationError, "quick_check"):
                create_validated_v2_backup(self.path)
        self.assertEqual(tuple(self.path.parent.glob("*.backup")), ())

    def test_backup_failure_leaves_source_intact_and_removes_partial(self):
        before = self._counts(self.path)
        with mock.patch.object(backup, "_copy_with_sqlite_backup", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(DatabaseBackupError, "disk full"):
                create_validated_v2_backup(self.path)
        self.assertEqual(self._counts(self.path), before)
        self.assertEqual(tuple(self.path.parent.glob("*.backup")), ())
        self.assertEqual(tuple(self.path.parent.glob("*.tmp")), ())

    def test_fsync_failure_leaves_source_and_existing_backups_intact(self):
        existing = self.path.with_name("history.sqlite3.v2-existing.backup")
        existing.write_bytes(b"keep")
        before = self._counts(self.path)
        with mock.patch.object(backup, "_fsync_file", side_effect=OSError("fsync failed")):
            with self.assertRaisesRegex(DatabaseBackupError, "fsync failed"):
                create_validated_v2_backup(self.path)
        self.assertEqual(self._counts(self.path), before)
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertEqual(tuple(self.path.parent.glob(".*.tmp")), ())

    def test_backup_validation_failure_is_refused(self):
        original = backup._validate_v2
        calls = 0

        def fail_second(db, *, expected_counts=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DatabaseBackupValidationError("invalid backup")
            return original(db, expected_counts=expected_counts)

        with mock.patch.object(backup, "_validate_v2", side_effect=fail_second):
            with self.assertRaisesRegex(DatabaseBackupValidationError, "invalid backup"):
                create_validated_v2_backup(self.path)
        self.assertEqual(tuple(self.path.parent.glob("*.backup")), ())

    def test_final_read_only_validation_failure_removes_new_backup(self):
        original = backup._validate_v2
        calls = 0

        def fail_third(db, *, expected_counts=None):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise DatabaseBackupValidationError("final validation failed")
            return original(db, expected_counts=expected_counts)

        with mock.patch.object(backup, "_validate_v2", side_effect=fail_third):
            with self.assertRaisesRegex(DatabaseBackupValidationError, "final validation"):
                create_validated_v2_backup(self.path)
        self.assertEqual(tuple(self.path.parent.glob("*.backup")), ())

    def test_exclusive_writer_lock_is_held_during_backup(self):
        original = backup._copy_with_sqlite_backup

        def assert_locked(source, destination):
            contender = sqlite3.connect(source, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    contender.execute(
                        "INSERT INTO metadata(key, value) VALUES('contender', 'blocked')"
                    )
            finally:
                contender.close()
            original(source, destination)

        with mock.patch.object(backup, "_copy_with_sqlite_backup", side_effect=assert_locked):
            result = create_validated_v2_backup(self.path)
        self.assertTrue(result.exists())

    def test_name_collision_never_overwrites_existing_backup(self):
        instant = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
        existing = self.path.with_name(
            "history.sqlite3.v2-20260828T123456.000000Z.backup"
        )
        existing.write_bytes(b"existing backup")

        result = create_validated_v2_backup(self.path, timestamp=instant)

        self.assertEqual(existing.read_bytes(), b"existing backup")
        self.assertEqual(result.name, "history.sqlite3.v2-20260828T123456.000000Z-1.backup")

    def test_backup_permissions_are_no_broader_than_source(self):
        os.chmod(self.path, 0o640)
        result = create_validated_v2_backup(self.path)
        source_mode = stat.S_IMODE(self.path.stat().st_mode)
        backup_mode = stat.S_IMODE(result.stat().st_mode)
        self.assertEqual(backup_mode & ~source_mode, 0)
        self.assertEqual(backup_mode, 0o640)

    def test_backup_does_not_migrate_or_modify_source_schema(self):
        before = self.path.read_bytes()
        result = create_validated_v2_backup(self.path)

        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("samples", tables)
            self.assertNotIn("state_events", tables)
        finally:
            db.close()
        self.assertTrue(result.exists())
        # WAL bookkeeping can change, but the database contents and version do not.
        self.assertEqual(self._counts(self.path), self._counts(result))
        self.assertEqual(before, self.path.read_bytes())

    def test_non_v2_database_is_refused(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA user_version = 1")
        db.close()
        with self.assertRaisesRegex(DatabaseBackupValidationError, "schema v1"):
            create_validated_v2_backup(self.path)
        self.assertEqual(tuple(self.path.parent.glob("*.backup")), ())


if __name__ == "__main__":
    unittest.main()

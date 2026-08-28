from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from battery_status_tui.migrations import (
    BUSY_TIMEOUT_MS,
    SchemaMigrationRequired,
    UnsupportedSchemaVersion,
)
from battery_status_tui.schema import CURRENT_SCHEMA_VERSION, PLANNED_SCHEMA_VERSIONS
from battery_status_tui.storage import Storage


LEGACY_SCHEMA = """
CREATE TABLE samples(
    id INTEGER PRIMARY KEY, timestamp INTEGER, session_id INTEGER,
    percentage REAL, state TEXT, ac_online INTEGER, power_w REAL,
    voltage_v REAL, current_a REAL, upower_remaining_s INTEGER,
    source TEXT, device TEXT, UNIQUE(timestamp, device)
);
CREATE TABLE sessions(
    id INTEGER PRIMARY KEY, kind TEXT, started_at INTEGER, ended_at INTEGER,
    start_percentage REAL, end_percentage REAL, end_reason TEXT
);
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
"""


def legacy_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(LEGACY_SCHEMA)
    db.execute(
        "INSERT INTO samples(timestamp, percentage, state, source, device) "
        "VALUES(1, 50, 'full', 'legacy', 'BAT0')"
    )
    db.commit()
    db.close()


class MigrationTests(unittest.TestCase):
    def test_writer_initializes_current_v2_schema_and_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "history.sqlite3")
            storage.initialize_writer()

            with storage.write_connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                                 CURRENT_SCHEMA_VERSION)
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)

    def test_reader_is_query_only_and_uses_writer_pragmas_where_applicable(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "history.sqlite3")
            storage.initialize_writer()

            with storage.connect() as db:
                self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("CREATE TABLE reader_must_not_write(value INTEGER)")

    def test_reader_does_not_migrate_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            legacy_database(path)
            before = path.read_bytes()

            with self.assertRaises(SchemaMigrationRequired):
                with Storage(path).connect():
                    pass

            self.assertEqual(path.read_bytes(), before)
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
                columns = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
                self.assertNotIn("power_method", columns)
            finally:
                db.close()

    def test_newer_schema_is_rejected_by_reader_and_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            db = sqlite3.connect(path)
            db.execute("PRAGMA user_version = 99")
            db.close()

            with self.assertRaises(UnsupportedSchemaVersion):
                with Storage(path).connect():
                    pass
            with self.assertRaises(UnsupportedSchemaVersion):
                Storage(path).initialize_writer()
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            finally:
                db.close()

    def test_writer_migration_is_idempotent_and_preserves_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            legacy_database(path)
            storage = Storage(path)

            storage.initialize_writer()
            first_schema = self._schema(path)
            first_row = storage.latest()
            Storage(path).initialize_writer()
            second_schema = self._schema(path)

            self.assertEqual(first_schema, second_schema)
            self.assertEqual(first_row.percentage, 50)
            self.assertEqual(storage.latest().percentage, 50)
            self.assertEqual(first_schema[0], CURRENT_SCHEMA_VERSION)

    def test_future_route_is_reserved_without_implementing_it(self):
        self.assertEqual(PLANNED_SCHEMA_VERSIONS, (3, 4))

    @staticmethod
    def _schema(path: Path) -> tuple[int, tuple[tuple[str, str], ...]]:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            objects = tuple(db.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ))
            return version, objects
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

"""Schema-v4 writer, rotating checkpoints, and recovery selection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .recent_series import decode_recent_series
from .schema import V1_SCHEMA_VERSION, create_v1_schema
from .v1_hourly import HourlyAccumulator


BUSY_TIMEOUT_MS = 5_000
CHECKPOINT_FORMAT_VERSION = 1
MAX_GENERATIONS = 3


class V1StorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BatteryCheckpoint:
    battery_id: int
    identity: str
    present: bool
    state: str
    soc_percent: float
    power_now_w: float | None = None
    current_now_a: float | None = None
    voltage_now_v: float | None = None
    energy_now_wh: float | None = None
    charge_now_ah: float | None = None
    upower_energy_rate_w: float | None = None
    resolved_power_w: float | None = None
    power_method: str | None = None
    power_approximate: bool | None = None
    power_confidence: str | None = None
    power_window_s: float | None = None

    def payload(self) -> tuple[object, ...]:
        def real(value: float | None) -> float | None:
            return None if value is None else float(value)

        return (
            self.battery_id, self.identity, int(self.present), self.state,
            float(self.soc_percent), real(self.power_now_w), real(self.current_now_a),
            real(self.voltage_now_v), real(self.energy_now_wh), real(self.charge_now_ah),
            real(self.upower_energy_rate_w), real(self.resolved_power_w),
            self.power_method,
            None if self.power_approximate is None else int(self.power_approximate),
            self.power_confidence, real(self.power_window_s),
        )


@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    generation: int
    created_at_ms: int
    last_poll_at_ms: int
    boot_id: str
    monotonic_ns: int
    boottime_ns: int
    configured_interval_ms: int
    ac_online: bool | None
    power_profile: str | None
    batteries: tuple[BatteryCheckpoint, ...]
    hourly: HourlyAccumulator | None
    recent_series: bytes


@dataclass(frozen=True, slots=True)
class RecoverySelection:
    snapshot: GenerationSnapshot | None
    warnings: tuple[str, ...]


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def generation_digest(snapshot: GenerationSnapshot) -> str:
    hourly = None
    profiles: dict[str, int] = {}
    if snapshot.hourly is not None:
        hourly = snapshot.hourly.checkpoint_values()
        hourly["battery_set_key"] = snapshot.hourly.battery_set_key
        profiles = dict(snapshot.hourly.profiles)
    payload = {
        "format_version": CHECKPOINT_FORMAT_VERSION,
        "generation": snapshot.generation,
        "created_at_ms": snapshot.created_at_ms,
        "last_poll_at_ms": snapshot.last_poll_at_ms,
        "boot_id": snapshot.boot_id,
        "monotonic_ns": snapshot.monotonic_ns,
        "boottime_ns": snapshot.boottime_ns,
        "configured_interval_ms": snapshot.configured_interval_ms,
        "ac_online": None if snapshot.ac_online is None else int(snapshot.ac_online),
        "power_profile": snapshot.power_profile,
        "batteries": [item.payload() for item in sorted(snapshot.batteries,
                                                        key=lambda item: item.battery_id)],
        "hourly": hourly,
        "profiles": profiles,
        "recent_series_sha256": hashlib.sha256(snapshot.recent_series).hexdigest(),
    }
    encoded = json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _insert(db: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    db.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(values.values()))


class V1Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._initialized = False

    def initialize_writer(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1_000)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            mode = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise V1StorageError(f"could not enable WAL mode: {mode}")
            db.execute("PRAGMA synchronous=FULL")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if not existed or version == 0 and not self._user_tables(db):
                db.execute("BEGIN IMMEDIATE")
                try:
                    create_v1_schema(db)
                    db.execute(f"PRAGMA user_version={V1_SCHEMA_VERSION}")
                except BaseException:
                    db.rollback()
                    raise
                else:
                    db.commit()
            elif version != V1_SCHEMA_VERSION:
                raise V1StorageError(
                    f"schema-v4 writer refuses database schema v{version}; no runtime conversion"
                )
            self._validate_schema(db)
        finally:
            db.close()
        self._initialized = True

    @staticmethod
    def _user_tables(db: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}

    @staticmethod
    def _validate_schema(db: sqlite3.Connection) -> None:
        if int(db.execute("PRAGMA user_version").fetchone()[0]) != V1_SCHEMA_VERSION:
            raise V1StorageError("database is not schema v4")
        if tuple(row[0] for row in db.execute("PRAGMA quick_check")) != ("ok",):
            raise V1StorageError("schema-v4 database failed quick_check")

    def _writer(self) -> sqlite3.Connection:
        self.initialize_writer()
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1_000)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._writer()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                             timeout=BUSY_TIMEOUT_MS / 1_000)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA query_only=ON")
        try:
            self._validate_schema(db)
            yield db
        finally:
            db.close()

    @staticmethod
    def next_generation(db: sqlite3.Connection) -> int:
        return int(db.execute(
            "SELECT COALESCE(max(generation),0)+1 FROM checkpoint_generations"
        ).fetchone()[0])

    def write_generation(self, db: sqlite3.Connection, snapshot: GenerationSnapshot) -> None:
        snapshot.hourly.validate() if snapshot.hourly is not None else None
        decode_recent_series(snapshot.recent_series)
        digest = generation_digest(snapshot)
        profile_count = 0 if snapshot.hourly is None else len(snapshot.hourly.profiles)
        _insert(db, "checkpoint_generations", {
            "generation": snapshot.generation,
            "format_version": CHECKPOINT_FORMAT_VERSION,
            "created_at_ms": snapshot.created_at_ms,
            "last_poll_at_ms": snapshot.last_poll_at_ms,
            "boot_id": snapshot.boot_id,
            "monotonic_ns": snapshot.monotonic_ns,
            "boottime_ns": snapshot.boottime_ns,
            "configured_interval_ms": snapshot.configured_interval_ms,
            "ac_online": None if snapshot.ac_online is None else int(snapshot.ac_online),
            "power_profile": snapshot.power_profile,
            "battery_count": len(snapshot.batteries),
            "hourly_count": 0 if snapshot.hourly is None else 1,
            "profile_count": profile_count,
            "recent_series": snapshot.recent_series,
            "payload_digest": digest,
            "complete": 0,
        })
        for item in snapshot.batteries:
            _insert(db, "checkpoint_batteries", {
                "generation": snapshot.generation,
                "battery_id": item.battery_id,
                "present": int(item.present),
                "state": item.state,
                "soc_percent": item.soc_percent,
                "power_now_w": item.power_now_w,
                "current_now_a": item.current_now_a,
                "voltage_now_v": item.voltage_now_v,
                "energy_now_wh": item.energy_now_wh,
                "charge_now_ah": item.charge_now_ah,
                "upower_energy_rate_w": item.upower_energy_rate_w,
                "resolved_power_w": item.resolved_power_w,
                "power_method": item.power_method,
                "power_approximate": (None if item.power_approximate is None
                                      else int(item.power_approximate)),
                "power_confidence": item.power_confidence,
                "power_window_s": item.power_window_s,
            })
        if snapshot.hourly is not None:
            values = snapshot.hourly.checkpoint_values()
            values["generation"] = snapshot.generation
            values["battery_set_key"] = snapshot.hourly.battery_set_key
            _insert(db, "checkpoint_hourly", values)
            for profile, duration in sorted(snapshot.hourly.profiles.items()):
                _insert(db, "checkpoint_hourly_profiles", {
                    "generation": snapshot.generation,
                    "hour_start_ms": snapshot.hourly.hour_start_ms,
                    "profile": profile,
                    "duration_ms": duration,
                })
        db.execute("UPDATE checkpoint_generations SET complete=1 WHERE generation=?",
                   (snapshot.generation,))
        loaded = self._load_generation(db, snapshot.generation)
        if loaded is None or generation_digest(loaded) != digest:
            raise V1StorageError("checkpoint generation failed post-write validation")

    def _load_generation(self, db: sqlite3.Connection,
                         generation: int) -> GenerationSnapshot | None:
        header = db.execute(
            "SELECT * FROM checkpoint_generations WHERE generation=? AND complete=1",
            (generation,),
        ).fetchone()
        if header is None or int(header["format_version"]) != CHECKPOINT_FORMAT_VERSION:
            return None
        battery_rows = db.execute(
            "SELECT c.*, b.identity FROM checkpoint_batteries c JOIN batteries b "
            "ON b.id=c.battery_id WHERE generation=? ORDER BY c.battery_id", (generation,)
        ).fetchall()
        hourly_rows = db.execute(
            "SELECT * FROM checkpoint_hourly WHERE generation=?", (generation,)
        ).fetchall()
        profile_rows = db.execute(
            "SELECT profile,duration_ms FROM checkpoint_hourly_profiles "
            "WHERE generation=? ORDER BY profile", (generation,)
        ).fetchall()
        if (len(battery_rows) != int(header["battery_count"])
                or len(hourly_rows) != int(header["hourly_count"])
                or len(profile_rows) != int(header["profile_count"])):
            return None
        batteries = tuple(BatteryCheckpoint(
            int(row["battery_id"]), str(row["identity"]), bool(row["present"]),
            str(row["state"]), float(row["soc_percent"]), row["power_now_w"],
            row["current_now_a"], row["voltage_now_v"], row["energy_now_wh"],
            row["charge_now_ah"], row["upower_energy_rate_w"], row["resolved_power_w"],
            row["power_method"], (None if row["power_approximate"] is None
                                  else bool(row["power_approximate"])),
            row["power_confidence"], row["power_window_s"],
        ) for row in battery_rows)
        recent = bytes(header["recent_series"])
        decode_recent_series(recent)
        hourly = None
        if hourly_rows:
            row = hourly_rows[0]
            values = {key: row[key] for key in row.keys()
                      if key not in {"generation", "soc_first", "soc_last"}}
            values["soc_first"] = row["soc_first"]
            values["soc_last"] = row["soc_last"]
            values["battery_set_key"] = row["battery_set_key"]
            values["profiles"] = {str(item[0]): int(item[1]) for item in profile_rows}
            hourly = HourlyAccumulator(**values)
            hourly.validate()
        return GenerationSnapshot(
            int(header["generation"]), int(header["created_at_ms"]),
            int(header["last_poll_at_ms"]), str(header["boot_id"]),
            int(header["monotonic_ns"]), int(header["boottime_ns"]),
            int(header["configured_interval_ms"]),
            None if header["ac_online"] is None else bool(header["ac_online"]),
            header["power_profile"], batteries, hourly, recent,
        )

    def recover(self, db: sqlite3.Connection | None = None) -> RecoverySelection:
        if db is None:
            with self.reader() as connection:
                return self._recover_from(connection)
        return self._recover_from(db)

    def valid_generations(self, limit: int = MAX_GENERATIONS) -> tuple[GenerationSnapshot, ...]:
        """Return newest valid checkpoints through a strictly read-only connection."""
        if limit <= 0:
            return ()
        snapshots = []
        with self.reader() as db:
            for row in db.execute(
                "SELECT generation FROM checkpoint_generations WHERE complete=1 "
                "ORDER BY generation DESC"
            ):
                generation = int(row[0])
                try:
                    snapshot = self._load_generation(db, generation)
                    digest = db.execute(
                        "SELECT payload_digest FROM checkpoint_generations WHERE generation=?",
                        (generation,),
                    ).fetchone()[0]
                    if snapshot is not None and generation_digest(snapshot) == digest:
                        snapshots.append(snapshot)
                except (ValueError, sqlite3.Error, V1StorageError):
                    continue
                if len(snapshots) == limit:
                    break
        return tuple(snapshots)

    def _recover_from(self, connection: sqlite3.Connection) -> RecoverySelection:
        warnings: list[str] = []
        generations = [int(row[0]) for row in connection.execute(
            "SELECT generation FROM checkpoint_generations WHERE complete=1 "
            "ORDER BY generation DESC"
        )]
        for generation in generations:
            try:
                snapshot = self._load_generation(connection, generation)
                header = connection.execute(
                    "SELECT payload_digest FROM checkpoint_generations WHERE generation=?",
                    (generation,),
                ).fetchone()
                if snapshot is None or generation_digest(snapshot) != str(header[0]):
                    raise V1StorageError("digest or checkpoint invariant mismatch")
                return RecoverySelection(snapshot, tuple(warnings))
            except (ValueError, sqlite3.Error, V1StorageError) as error:
                warnings.append(f"checkpoint generation {generation} invalid: {error}")
        warnings.append("no valid checkpoint generation; cold start")
        return RecoverySelection(None, tuple(warnings))

    def cleanup_generations(self) -> None:
        with self.transaction() as db:
            valid: list[int] = []
            for row in db.execute(
                "SELECT generation FROM checkpoint_generations WHERE complete=1 "
                "ORDER BY generation DESC"
            ):
                generation = int(row[0])
                try:
                    snapshot = self._load_generation(db, generation)
                    digest = db.execute(
                        "SELECT payload_digest FROM checkpoint_generations WHERE generation=?",
                        (generation,),
                    ).fetchone()[0]
                    if snapshot is not None and generation_digest(snapshot) == digest:
                        valid.append(generation)
                except (ValueError, sqlite3.Error, V1StorageError):
                    continue
            keep = set(valid[:MAX_GENERATIONS])
            if keep:
                placeholders = ",".join("?" for _ in keep)
                db.execute(
                    f"DELETE FROM checkpoint_generations WHERE generation NOT IN ({placeholders})",
                    tuple(sorted(keep)),
                )
            else:
                db.execute("DELETE FROM checkpoint_generations")

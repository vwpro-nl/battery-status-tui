"""Temporary offline converter from the private pre-v1 schema to schema v4.

This module is deliberately not connected to normal writer startup.  It reads
one v2 database, creates a separate v4 database, and never mutates the source.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .schema import V1_SCHEMA_VERSION, V2_REQUIRED_TABLES, create_v1_schema


MAX_CONTINUITY_MS = 180_000
CLOCK_TOLERANCE_MS = 5_000
IMPORT_GENERATION = 0
AGGREGATION_VERSION = 1

REASON_PRESENT = 1
REASON_STATE = 2
REASON_SOC = 4
REASON_AC = 8

QUALITY_UNKNOWN = 1
QUALITY_SLEEP = 2


class PreV1ConversionError(RuntimeError):
    """The private v2 database cannot be safely converted."""


@dataclass(frozen=True)
class AggregateSample:
    timestamp_ms: int
    percentage: float
    state: str
    ac_online: int | None
    power_w: float | None
    power_approximate: int
    energy_wh: float | None
    monotonic_ms: int | None
    boottime_ms: int | None
    boot_id: str
    battery_identity: str


@dataclass
class HourAccumulator:
    hour_start_ms: int
    soc_start: float | None = None
    soc_end: float | None = None
    soc_min: float | None = None
    soc_max: float | None = None
    soc_integral_percent_ms: float = 0.0
    charged_energy_wh: float = 0.0
    discharged_energy_wh: float = 0.0
    observed_ms: int = 0
    sleep_ms: int = 0
    charging_ms: int = 0
    discharging_ms: int = 0
    full_ms: int = 0
    other_state_ms: int = 0
    ac_online_ms: int = 0
    ac_offline_ms: int = 0
    ac_unknown_ms: int = 0
    under_20_ms: int = 0
    above_80_ms: int = 0
    above_95_ms: int = 0
    full_on_ac_ms: int = 0
    charge_power_integral_w_ms: float = 0.0
    charge_power_max_w: float | None = None
    charge_power_valid_ms: int = 0
    discharge_power_integral_w_ms: float = 0.0
    discharge_power_max_w: float | None = None
    discharge_power_valid_ms: int = 0
    direct_power_ms: int = 0
    estimated_power_ms: int = 0
    unknown_power_ms: int = 0
    poll_count: int = 0
    state_event_count: int = 0
    quality_flags: int = 0

    def add_observed(
        self,
        start_ms: int,
        end_ms: int,
        start_soc: float,
        end_soc: float,
        sample: AggregateSample,
        energy_delta_wh: float | None,
    ) -> None:
        duration = end_ms - start_ms
        if duration <= 0:
            return
        self.observed_ms += duration
        if self.soc_start is None:
            self.soc_start = start_soc
        self.soc_end = end_soc
        low, high = sorted((start_soc, end_soc))
        self.soc_min = low if self.soc_min is None else min(self.soc_min, low)
        self.soc_max = high if self.soc_max is None else max(self.soc_max, high)
        self.soc_integral_percent_ms += (start_soc + end_soc) * 0.5 * duration

        state = sample.state.lower()
        if state == "charging":
            self.charging_ms += duration
        elif state == "discharging":
            self.discharging_ms += duration
        elif state in {"full", "fully-charged", "charged"}:
            self.full_ms += duration
        else:
            self.other_state_ms += duration

        if sample.ac_online == 1:
            self.ac_online_ms += duration
        elif sample.ac_online == 0:
            self.ac_offline_ms += duration
        else:
            self.ac_unknown_ms += duration
        self.under_20_ms += _duration_below(start_soc, end_soc, duration, 20)
        self.above_80_ms += _duration_above(start_soc, end_soc, duration, 80)
        self.above_95_ms += _duration_above(start_soc, end_soc, duration, 95)
        if state in {"full", "fully-charged", "charged"} and sample.ac_online == 1:
            self.full_on_ac_ms += duration

        power = sample.power_w
        if power is None or not math.isfinite(power) or power < 0:
            self.unknown_power_ms += duration
        else:
            if sample.power_approximate:
                self.estimated_power_ms += duration
            else:
                self.direct_power_ms += duration
            if state == "charging":
                self.charge_power_integral_w_ms += power * duration
                self.charge_power_valid_ms += duration
                self.charge_power_max_w = power if self.charge_power_max_w is None else max(
                    self.charge_power_max_w, power
                )
            elif state == "discharging":
                self.discharge_power_integral_w_ms += power * duration
                self.discharge_power_valid_ms += duration
                self.discharge_power_max_w = power if self.discharge_power_max_w is None else max(
                    self.discharge_power_max_w, power
                )
        if energy_delta_wh is not None:
            if energy_delta_wh >= 0:
                self.charged_energy_wh += energy_delta_wh
            else:
                self.discharged_energy_wh += -energy_delta_wh


def _ro_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _validate_source(db: sqlite3.Connection) -> None:
    result = tuple(str(row[0]) for row in db.execute("PRAGMA quick_check"))
    if result != ("ok",):
        raise PreV1ConversionError(f"source quick_check failed: {'; '.join(result)}")
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version != 2:
        raise PreV1ConversionError(f"source must use schema v2, found v{version}")
    tables = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = V2_REQUIRED_TABLES - tables
    if missing:
        raise PreV1ConversionError(
            f"source schema v2 is missing tables: {', '.join(sorted(missing))}"
        )


def _finite(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _duration_below(start: float, end: float, duration: int, threshold: float) -> int:
    if start == end:
        return duration if start < threshold else 0
    low, high = sorted((start, end))
    if high <= threshold:
        return duration
    if low >= threshold:
        return 0
    fraction = (threshold - low) / (high - low)
    return round(duration * fraction)


def _duration_above(start: float, end: float, duration: int, threshold: float) -> int:
    if start == end:
        return duration if start > threshold else 0
    low, high = sorted((start, end))
    if low >= threshold:
        return duration
    if high <= threshold:
        return 0
    fraction = (high - threshold) / (high - low)
    return round(duration * fraction)


def _to_ms(value: object) -> int:
    return int(value) * 1_000


def _clock_ms(value: object) -> int | None:
    number = _finite(value)
    return None if number is None else round(number * 1_000)


def _load_samples(db: sqlite3.Connection) -> list[AggregateSample]:
    rows = db.execute("SELECT * FROM samples ORDER BY timestamp, id").fetchall()
    by_timestamp: dict[int, AggregateSample] = {}
    for row in rows:
        timestamp_ms = _to_ms(row["timestamp"])
        sample = AggregateSample(
            timestamp_ms=timestamp_ms,
            percentage=float(row["percentage"]),
            state=str(row["state"]),
            ac_online=None if row["ac_online"] is None else int(row["ac_online"]),
            power_w=_finite(row["power_w"]),
            power_approximate=int(row["power_approximate"] or 0),
            energy_wh=_finite(row["energy_wh"]),
            monotonic_ms=_clock_ms(row["monotonic_s"]),
            boottime_ms=_clock_ms(row["boottime_s"]),
            boot_id=str(row["boot_id"] or "legacy-unknown-boot"),
            battery_identity=str(row["battery_identity"] or row["device"] or "legacy-unknown"),
        )
        previous = by_timestamp.get(timestamp_ms)
        if previous is not None and previous != sample:
            raise PreV1ConversionError(
                f"ambiguous aggregate samples at timestamp {timestamp_ms // 1000}"
            )
        by_timestamp[timestamp_ms] = sample
    return list(by_timestamp.values())


def _continuous(left: AggregateSample, right: AggregateSample) -> bool:
    elapsed = right.timestamp_ms - left.timestamp_ms
    if elapsed <= 0 or elapsed > MAX_CONTINUITY_MS:
        return False
    if left.boot_id != right.boot_id or left.battery_identity != right.battery_identity:
        return False
    for first, second in (
        (left.monotonic_ms, right.monotonic_ms),
        (left.boottime_ms, right.boottime_ms),
    ):
        if first is not None and second is not None:
            if second < first or abs((second - first) - elapsed) > CLOCK_TOLERANCE_MS:
                return False
    return True


def _insert_batteries(db: sqlite3.Connection, source: sqlite3.Connection) -> dict[str, int]:
    seen: dict[str, tuple[int, int, str]] = {}
    for row in source.execute(
        "SELECT timestamp, identity, device FROM battery_samples ORDER BY timestamp, id"
    ):
        identity = str(row["identity"] or row["device"] or "legacy-unknown")
        timestamp_ms = _to_ms(row["timestamp"])
        first, last, native = seen.get(identity, (timestamp_ms, timestamp_ms, str(row["device"] or "")))
        seen[identity] = (min(first, timestamp_ms), max(last, timestamp_ms), native)
    for row in source.execute(
        "SELECT timestamp, battery_identity, device FROM samples ORDER BY timestamp, id"
    ):
        identity = str(row["battery_identity"] or row["device"] or "legacy-unknown")
        timestamp_ms = _to_ms(row["timestamp"])
        first, last, native = seen.get(identity, (timestamp_ms, timestamp_ms, str(row["device"] or "")))
        seen[identity] = (min(first, timestamp_ms), max(last, timestamp_ms), native)
    for identity in sorted(seen):
        first, last, native = seen[identity]
        db.execute(
            "INSERT INTO batteries(identity, native_name, first_seen_ms, last_seen_ms) "
            "VALUES(?, ?, ?, ?)",
            (identity, native or None, first, last),
        )
    return {
        str(row["identity"]): int(row["id"])
        for row in db.execute("SELECT id, identity FROM batteries")
    }


def _insert_state_events(
    db: sqlite3.Connection,
    source: sqlite3.Connection,
    batteries: dict[str, int],
) -> None:
    previous_system: tuple[int | None] | None = None
    for row in source.execute("SELECT * FROM samples ORDER BY timestamp, id"):
        current = (None if row["ac_online"] is None else int(row["ac_online"]),)
        if current != previous_system:
            db.execute(
                "INSERT OR IGNORE INTO state_events(occurred_at_ms, boot_id, scope, "
                "ac_online, reason_mask, source_generation) VALUES(?, ?, 'system', ?, ?, ?)",
                (_to_ms(row["timestamp"]), str(row["boot_id"] or "legacy-unknown-boot"),
                 current[0], REASON_AC, IMPORT_GENERATION),
            )
            previous_system = current

    raw_rows = source.execute("SELECT * FROM battery_samples ORDER BY identity, timestamp, id").fetchall()
    raw_keys = {(int(row["timestamp"]), str(row["identity"] or row["device"] or "legacy-unknown"))
                for row in raw_rows}
    raw_by_time: dict[int, list[sqlite3.Row]] = {}
    for row in raw_rows:
        raw_by_time.setdefault(int(row["timestamp"]), []).append(row)
    absences: dict[int, tuple[set[str], str]] = {}
    previous_set: set[str] | None = None
    for timestamp, rows_at_time in sorted(raw_by_time.items()):
        current_set = {
            str(row["identity"] or row["device"] or "legacy-unknown")
            for row in rows_at_time
        }
        if previous_set is not None and previous_set - current_set:
            absences[timestamp] = (
                previous_set - current_set,
                str(rows_at_time[0]["boot_id"] or "legacy-unknown-boot"),
            )
        previous_set = current_set
    records: list[tuple[int, str, str, float, str]] = []
    for row in raw_rows:
        records.append((int(row["timestamp"]), str(row["identity"] or row["device"] or "legacy-unknown"),
                        str(row["state"]), float(row["percentage"]), str(row["boot_id"] or "legacy-unknown-boot")))
    for row in source.execute("SELECT * FROM samples ORDER BY timestamp, id"):
        key = (int(row["timestamp"]), str(row["battery_identity"] or row["device"] or "legacy-unknown"))
        if key not in raw_keys:
            records.append((key[0], key[1], str(row["state"]), float(row["percentage"]),
                            str(row["boot_id"] or "legacy-unknown-boot")))
    previous: dict[str, tuple[str, float]] = {}
    handled_absences: set[int] = set()
    for timestamp, identity, state, soc, boot_id in sorted(records):
        if timestamp in absences and timestamp not in handled_absences:
            removed, absence_boot_id = absences[timestamp]
            for removed_identity in sorted(removed):
                db.execute(
                    "INSERT INTO state_events(occurred_at_ms, boot_id, scope, battery_id, "
                    "battery_present, reason_mask, source_generation) "
                    "VALUES(?, ?, 'battery', ?, 0, ?, ?)",
                    (timestamp * 1_000, absence_boot_id, batteries[removed_identity],
                     REASON_PRESENT, IMPORT_GENERATION),
                )
                previous.pop(removed_identity, None)
            handled_absences.add(timestamp)
        current = (state, soc)
        if previous.get(identity) == current:
            continue
        old = previous.get(identity)
        reason = REASON_PRESENT if old is None else 0
        if old is None or old[0] != state:
            reason |= REASON_STATE
        if old is None or old[1] != soc:
            reason |= REASON_SOC
        db.execute(
            "INSERT INTO state_events(occurred_at_ms, boot_id, scope, battery_id, "
            "battery_present, battery_state, soc_percent, reason_mask, source_generation) "
            "VALUES(?, ?, 'battery', ?, 1, ?, ?, ?, ?)",
            (timestamp * 1_000, boot_id, batteries[identity], state, soc, reason, IMPORT_GENERATION),
        )
        previous[identity] = current


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _insert_health(
    db: sqlite3.Connection,
    source: sqlite3.Connection,
    batteries: dict[str, int],
) -> None:
    previous: dict[str, tuple[object, ...]] = {}
    for row in source.execute("SELECT * FROM battery_samples ORDER BY identity, timestamp, id"):
        identity = str(row["identity"] or row["device"] or "legacy-unknown")
        values = (
            _positive(row["charge_full_ah"]),
            _positive(row["charge_full_design_ah"]),
            _positive(row["energy_full_wh"]),
            _positive(row["energy_full_design_wh"]),
        )
        if not any(value is not None for value in values) or previous.get(identity) == values:
            continue
        source_name = "legacy-v2-energy" if values[2] is not None or values[3] is not None else "legacy-v2-charge"
        db.execute(
            "INSERT INTO battery_health(battery_id, observed_at_ms, charge_full_ah, "
            "charge_full_design_ah, energy_full_wh, energy_full_design_wh, source, "
            "provenance, source_generation) VALUES(?, ?, ?, ?, ?, ?, ?, 'legacy-v2', ?)",
            (batteries[identity], _to_ms(row["timestamp"]), *values, source_name, IMPORT_GENERATION),
        )
        previous[identity] = values


def _load_battery_sets(source: sqlite3.Connection,
                       samples: list[AggregateSample]) -> dict[int, str]:
    identities: dict[int, set[str]] = {}
    for row in source.execute("SELECT timestamp, identity, device FROM battery_samples"):
        identities.setdefault(_to_ms(row["timestamp"]), set()).add(
            str(row["identity"] or row["device"] or "legacy-unknown")
        )
    for sample in samples:
        identities.setdefault(sample.timestamp_ms, set()).add(sample.battery_identity)
    return {timestamp: "+".join(sorted(values)) for timestamp, values in identities.items()}


def _battery_set_at(battery_sets: dict[int, str], timestamp_ms: int) -> str:
    preceding = [timestamp for timestamp in battery_sets if timestamp <= timestamp_ms]
    return battery_sets[max(preceding)] if preceding else "legacy-unknown"


def _insert_sessions(db: sqlite3.Connection, source: sqlite3.Connection,
                     battery_sets: dict[int, str]) -> None:
    for row in source.execute("SELECT * FROM sessions ORDER BY id"):
        started = _to_ms(row["started_at"])
        db.execute(
            "INSERT INTO sessions(id, kind, started_at_ms, ended_at_ms, start_soc, end_soc, "
            "battery_set_key, end_reason, source_generation) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(row["id"]), str(row["kind"]), started,
             None if row["ended_at"] is None else _to_ms(row["ended_at"]),
             float(row["start_percentage"]),
             None if row["end_percentage"] is None else float(row["end_percentage"]),
             _battery_set_at(battery_sets, started), row["end_reason"], IMPORT_GENERATION),
        )


def _load_sleep(source: sqlite3.Connection) -> list[tuple[int, int]]:
    return [(_to_ms(row[0]), _to_ms(row[1])) for row in source.execute(
        "SELECT started_at, ended_at FROM sleep_intervals ORDER BY started_at, ended_at"
    )]


def _insert_sleep(db: sqlite3.Connection, source: sqlite3.Connection) -> None:
    for row in source.execute("SELECT * FROM sleep_intervals ORDER BY id"):
        db.execute(
            "INSERT INTO sleep_intervals(id, started_at_ms, ended_at_ms, kind, source, "
            "boot_id, detected_at_ms, pre_soc, post_soc, source_generation, revision) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (int(row["id"]), _to_ms(row["started_at"]), _to_ms(row["ended_at"]),
             str(row["kind"]), str(row["source"]), row["boot_id"], _to_ms(row["ended_at"]),
             row["pre_percentage"], row["post_percentage"], IMPORT_GENERATION),
        )


def _subtract(interval: tuple[int, int], excluded: list[tuple[int, int]]) -> list[tuple[int, int]]:
    parts = [interval]
    for excluded_start, excluded_end in excluded:
        next_parts: list[tuple[int, int]] = []
        for start, end in parts:
            if excluded_end <= start or excluded_start >= end:
                next_parts.append((start, end))
            else:
                if start < excluded_start:
                    next_parts.append((start, excluded_start))
                if excluded_end < end:
                    next_parts.append((excluded_end, end))
        parts = next_parts
    return parts


def _merged_duration(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    clipped = sorted((max(start, left), min(end, right)) for left, right in intervals
                     if left < end and right > start)
    total = 0
    cursor_start = cursor_end = None
    for left, right in clipped:
        if cursor_start is None:
            cursor_start, cursor_end = left, right
        elif left <= cursor_end:
            cursor_end = max(cursor_end, right)
        else:
            total += cursor_end - cursor_start
            cursor_start, cursor_end = left, right
    return total + (0 if cursor_start is None else cursor_end - cursor_start)


def _add_sample_interval(hour: HourAccumulator, left: AggregateSample,
                         right: AggregateSample, start: int, end: int,
                         *, allow_energy_delta: bool) -> None:
    full_duration = right.timestamp_ms - left.timestamp_ms
    start_fraction = (start - left.timestamp_ms) / full_duration
    end_fraction = (end - left.timestamp_ms) / full_duration
    start_soc = left.percentage + (right.percentage - left.percentage) * start_fraction
    end_soc = left.percentage + (right.percentage - left.percentage) * end_fraction
    energy_delta = None
    if allow_energy_delta and left.energy_wh is not None and right.energy_wh is not None:
        full_delta = right.energy_wh - left.energy_wh
        state = left.state.lower()
        # A counter movement opposite to the declared direction is a reset or
        # identity ambiguity, not charged/discharged energy.
        if ((state == "charging" and full_delta >= 0)
                or (state == "discharging" and full_delta <= 0)):
            energy_delta = full_delta * ((end - start) / full_duration)
    hour.add_observed(start, end, start_soc, end_soc, left, energy_delta)


def _insert_hourly(db: sqlite3.Connection, samples: list[AggregateSample],
                   sleep: list[tuple[int, int]], battery_sets: dict[int, str]) -> None:
    points = [sample.timestamp_ms for sample in samples]
    points.extend(value for interval in sleep for value in interval)
    if not points:
        return
    first_hour = min(points) // 3_600_000 * 3_600_000
    last_hour = max(points) // 3_600_000 * 3_600_000
    events_by_hour = {
        hour: int(db.execute(
            "SELECT count(*) FROM state_events WHERE occurred_at_ms >= ? AND occurred_at_ms < ?",
            (hour, hour + 3_600_000),
        ).fetchone()[0])
        for hour in range(first_hour, last_hour + 1, 3_600_000)
    }
    for hour_start in range(first_hour, last_hour + 1, 3_600_000):
        hour_end = hour_start + 3_600_000
        acc = HourAccumulator(hour_start)
        acc.sleep_ms = _merged_duration(sleep, hour_start, hour_end)
        acc.poll_count = sum(hour_start <= sample.timestamp_ms < hour_end for sample in samples)
        acc.state_event_count = events_by_hour[hour_start]
        for left, right in zip(samples, samples[1:]):
            if not _continuous(left, right):
                continue
            interval_start = max(hour_start, left.timestamp_ms)
            interval_end = min(hour_end, right.timestamp_ms)
            if interval_start >= interval_end:
                continue
            crosses_sleep = any(
                sleep_start < right.timestamp_ms and sleep_end > left.timestamp_ms
                for sleep_start, sleep_end in sleep
            )
            for start, end in _subtract((interval_start, interval_end), sleep):
                _add_sample_interval(
                    acc, left, right, start, end,
                    allow_energy_delta=not crosses_sleep,
                )
        unknown = 3_600_000 - acc.observed_ms - acc.sleep_ms
        if unknown < 0:
            raise PreV1ConversionError(f"overlapping observed/sleep time in hour {hour_start}")
        acc.quality_flags = (QUALITY_UNKNOWN if unknown else 0) | (QUALITY_SLEEP if acc.sleep_ms else 0)
        sets_in_hour = [value for timestamp, value in sorted(battery_sets.items())
                        if hour_start <= timestamp < hour_end]
        battery_set_key = (sets_in_hour[-1] if sets_in_hour
                           else _battery_set_at(battery_sets, hour_start))
        values = (
            hour_start, 1, IMPORT_GENERATION, AGGREGATION_VERSION, hour_end, 1,
            battery_set_key, acc.soc_start, acc.soc_end, acc.soc_min, acc.soc_max,
            acc.soc_integral_percent_ms, acc.charged_energy_wh, acc.discharged_energy_wh,
            acc.observed_ms, acc.sleep_ms, unknown, acc.charging_ms, acc.discharging_ms,
            acc.full_ms, acc.other_state_ms, acc.ac_online_ms, acc.ac_offline_ms,
            acc.ac_unknown_ms, acc.under_20_ms, acc.above_80_ms, acc.above_95_ms,
            acc.full_on_ac_ms, acc.charge_power_integral_w_ms, acc.charge_power_max_w,
            acc.charge_power_valid_ms, acc.discharge_power_integral_w_ms,
            acc.discharge_power_max_w, acc.discharge_power_valid_ms, acc.direct_power_ms,
            acc.estimated_power_ms, acc.unknown_power_ms, acc.poll_count,
            acc.state_event_count, acc.quality_flags,
        )
        placeholders = ",".join("?" for _ in values)
        db.execute(f"INSERT INTO hourly_history VALUES({placeholders})", values)


def _copy_metadata(db: sqlite3.Connection, source: sqlite3.Connection) -> None:
    for row in source.execute("SELECT key, value FROM metadata ORDER BY key"):
        db.execute("INSERT INTO metadata(key, value) VALUES(?, ?)", tuple(row))
    db.execute(
        "INSERT INTO metadata(key, value) VALUES('converted_from_schema', '2') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _postconditions(db: sqlite3.Connection) -> None:
    if tuple(str(row[0]) for row in db.execute("PRAGMA quick_check")) != ("ok",):
        raise PreV1ConversionError("destination quick_check failed")
    foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise PreV1ConversionError(f"destination foreign-key violations: {foreign_keys!r}")
    invalid_hours = int(db.execute(
        "SELECT count(*) FROM hourly_history WHERE observed_ms + sleep_ms + unknown_ms != 3600000"
    ).fetchone()[0])
    if invalid_hours:
        raise PreV1ConversionError("destination contains incomplete UTC hours")


def convert_v2_to_v1(source_path: Path, destination_path: Path) -> Path:
    """Convert a private v2 database into a newly-created schema-v4 database."""
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if destination_path.exists():
        raise PreV1ConversionError(f"destination already exists: {destination_path}")
    if not source_path.is_file():
        raise PreV1ConversionError(f"source database does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    succeeded = False
    try:
        fd = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        fd = None
        source = _ro_connection(source_path)
        source.row_factory = sqlite3.Row
        _validate_source(source)
        destination = sqlite3.connect(destination_path)
        destination.row_factory = sqlite3.Row
        destination.execute("PRAGMA foreign_keys = ON")
        destination.execute("BEGIN IMMEDIATE")
        create_v1_schema(destination)
        batteries = _insert_batteries(destination, source)
        samples = _load_samples(source)
        battery_sets = _load_battery_sets(source, samples)
        _insert_state_events(destination, source, batteries)
        _insert_health(destination, source, batteries)
        _insert_sessions(destination, source, battery_sets)
        _insert_sleep(destination, source)
        _insert_hourly(destination, samples, _load_sleep(source), battery_sets)
        _copy_metadata(destination, source)
        destination.execute(f"PRAGMA user_version = {V1_SCHEMA_VERSION}")
        _postconditions(destination)
        destination.commit()
        succeeded = True
        return destination_path
    except sqlite3.Error as error:
        if destination is not None:
            destination.rollback()
        raise PreV1ConversionError(f"SQLite conversion failed: {error}") from error
    except BaseException:
        if destination is not None:
            destination.rollback()
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if not succeeded:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(f"{destination_path}{suffix}").unlink(missing_ok=True)
                except OSError:
                    pass

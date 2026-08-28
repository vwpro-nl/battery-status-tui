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

from .energy_integrity import (
    ENERGY_PROVENANCE_REJECTED,
    EnergyCounter,
    compatible_delta,
    plausible_power,
)
from .recent_series import (
    POWER_CONFIDENCE_SHIFT,
    POWER_METHOD_SHIFT,
    BatteryState,
    RecentPoint,
    encode_recent_series,
)
from .schema import V1_SCHEMA_VERSION, V2_REQUIRED_TABLES, create_v1_schema
from .v1_collector import BREAK_BEFORE, CONFIDENCE, METHODS
from .v1_hourly import (
    QUALITY_ENERGY_REJECTED,
    QUALITY_POWER_REJECTED,
    HourlyAccumulator,
)
from .v1_storage import BatteryCheckpoint, GenerationSnapshot, V1Storage


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
    energy_counters: tuple[EnergyCounter, ...] = ()
    power_rejected: bool = False
    power_method: str = "unavailable"
    power_confidence: str = "none"


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
    unknown_ms: int = 0
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
    energy_provenance_mask: int = 0

    def add_observed(
        self,
        start_ms: int,
        end_ms: int,
        start_soc: float,
        end_soc: float,
        sample: AggregateSample,
        energy_delta_wh: float | None,
        energy_provenance_mask: int = 0,
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
            self.energy_provenance_mask |= energy_provenance_mask


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


def _raw_counters(db: sqlite3.Connection) -> dict[int, tuple[EnergyCounter, ...]]:
    grouped: dict[int, list[EnergyCounter] | None] = {}
    for row in db.execute("SELECT * FROM battery_samples ORDER BY timestamp, identity, id"):
        timestamp = _to_ms(row["timestamp"])
        identity = str(row["identity"] or row["device"] or "legacy-unknown")
        counter = None
        energy = _finite(row["energy_now_wh"])
        charge = _finite(row["charge_now_ah"])
        voltage = _finite(row["voltage_now_v"])
        if energy is not None and energy >= 0:
            counter = EnergyCounter(identity, "energy", energy)
        elif charge is not None and charge >= 0 and voltage is not None and voltage > 0:
            counter = EnergyCounter(identity, "charge", charge, voltage)
        values = grouped.setdefault(timestamp, [])
        if counter is None:
            grouped[timestamp] = None
        elif values is not None:
            values.append(counter)
    return {timestamp: tuple(values or ()) for timestamp, values in grouped.items()}


def _load_samples(db: sqlite3.Connection) -> list[AggregateSample]:
    rows = db.execute("SELECT * FROM samples ORDER BY timestamp, id").fetchall()
    counters = _raw_counters(db)
    by_timestamp: dict[int, AggregateSample] = {}
    for row in rows:
        timestamp_ms = _to_ms(row["timestamp"])
        power, power_rejected = plausible_power(
            _finite(row["power_w"]), max(1, len(counters.get(timestamp_ms, ())))
        )
        percentage = float(row["percentage"])
        if not 0 <= percentage <= 100:
            raise PreV1ConversionError(
                f"sample at {timestamp_ms // 1000} has invalid SoC {percentage!r}"
            )
        sample = AggregateSample(
            timestamp_ms=timestamp_ms,
            percentage=percentage,
            state=str(row["state"]),
            ac_online=None if row["ac_online"] is None else int(row["ac_online"]),
            power_w=power,
            power_approximate=int(row["power_approximate"] or 0),
            energy_wh=_finite(row["energy_wh"]),
            monotonic_ms=_clock_ms(row["monotonic_s"]),
            boottime_ms=_clock_ms(row["boottime_s"]),
            boot_id=str(row["boot_id"] or "legacy-unknown-boot"),
            battery_identity=str(row["battery_identity"] or row["device"] or "legacy-unknown"),
            energy_counters=counters.get(timestamp_ms, ()),
            power_rejected=power_rejected,
            power_method=str(row["power_method"] or "unavailable"),
            power_confidence=str(row["power_confidence"] or "none"),
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
        percentage = float(row["percentage"])
        if not 0 <= percentage <= 100:
            raise PreV1ConversionError(
                f"raw sample at {int(row['timestamp'])} has invalid SoC {percentage!r}"
            )
        records.append((int(row["timestamp"]), str(row["identity"] or row["device"] or "legacy-unknown"),
                        str(row["state"]), percentage, str(row["boot_id"] or "legacy-unknown-boot")))
    for row in source.execute("SELECT * FROM samples ORDER BY timestamp, id"):
        key = (int(row["timestamp"]), str(row["battery_identity"] or row["device"] or "legacy-unknown"))
        if key not in raw_keys:
            records.append((key[0], key[1], str(row["state"]), float(row["percentage"]),
                            str(row["boot_id"] or "legacy-unknown-boot")))
    previous: dict[str, str] = {}
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
        if previous.get(identity) == state:
            continue
        old = previous.get(identity)
        reason = REASON_PRESENT if old is None else 0
        if old is None or old != state:
            reason |= REASON_STATE
        db.execute(
            "INSERT INTO state_events(occurred_at_ms, boot_id, scope, battery_id, "
            "battery_present, battery_state, soc_percent, reason_mask, source_generation) "
            "VALUES(?, ?, 'battery', ?, 1, ?, ?, ?, ?)",
            (timestamp * 1_000, boot_id, batteries[identity], state, soc, reason, IMPORT_GENERATION),
        )
        previous[identity] = state


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
    result = None
    if allow_energy_delta:
        result = compatible_delta(
            left.energy_counters, right.energy_counters,
            elapsed_ms=full_duration, direction=_session_kind(left),
        )
    energy_delta = (None if result is None or result.value_wh is None else
                    result.value_wh * ((end - start) / full_duration))
    if left.power_rejected:
        hour.quality_flags |= QUALITY_POWER_REJECTED
    if result is not None and result.rejected:
        hour.quality_flags |= QUALITY_ENERGY_REJECTED
        hour.energy_provenance_mask |= ENERGY_PROVENANCE_REJECTED
    hour.add_observed(
        start, end, start_soc, end_soc, left, energy_delta,
        0 if result is None else result.provenance_mask,
    )


def _session_kind(sample: AggregateSample) -> str | None:
    if sample.ac_online == 0:
        return "discharging"
    if sample.ac_online == 1:
        if sample.state.lower() == "charging" or sample.percentage < 100:
            return "charging"
        return None
    state = sample.state.lower()
    return state if state in {"charging", "discharging"} else None


def _insert_hourly(db: sqlite3.Connection, samples: list[AggregateSample],
                   sleep: list[tuple[int, int]], battery_sets: dict[int, str]
                   ) -> HourAccumulator | None:
    points = [sample.timestamp_ms for sample in samples]
    points.extend(value for interval in sleep for value in interval)
    if not points:
        return None
    if not samples:
        return None
    current_at = samples[-1].timestamp_ms
    first_hour = min(points) // 3_600_000 * 3_600_000
    last_hour = current_at // 3_600_000 * 3_600_000
    events_by_hour = {
        hour: int(db.execute(
            "SELECT count(*) FROM state_events WHERE occurred_at_ms >= ? AND occurred_at_ms < ?",
            (hour, hour + 3_600_000),
        ).fetchone()[0])
        for hour in range(first_hour, last_hour + 1, 3_600_000)
    }
    for hour_start in range(first_hour, last_hour + 1, 3_600_000):
        hour_end = hour_start + 3_600_000
        coverage_end = hour_end if hour_start < last_hour else current_at
        acc = HourAccumulator(hour_start)
        acc.sleep_ms = _merged_duration(sleep, hour_start, coverage_end)
        acc.poll_count = sum(hour_start <= sample.timestamp_ms <= coverage_end for sample in samples)
        acc.state_event_count = events_by_hour[hour_start]
        for left, right in zip(samples, samples[1:]):
            if not _continuous(left, right):
                continue
            interval_start = max(hour_start, left.timestamp_ms)
            interval_end = min(coverage_end, right.timestamp_ms)
            if interval_start >= interval_end:
                continue
            crosses_sleep = any(
                sleep_start < right.timestamp_ms and sleep_end > left.timestamp_ms
                for sleep_start, sleep_end in sleep
            )
            same_session = _session_kind(left) == _session_kind(right)
            for start, end in _subtract((interval_start, interval_end), sleep):
                _add_sample_interval(
                    acc, left, right, start, end,
                    allow_energy_delta=not crosses_sleep and same_session,
                )
        target_coverage = coverage_end - hour_start
        unknown = target_coverage - acc.observed_ms - acc.sleep_ms
        if unknown < 0:
            raise PreV1ConversionError(f"overlapping observed/sleep time in hour {hour_start}")
        acc.unknown_ms = unknown
        acc.quality_flags |= (QUALITY_UNKNOWN if unknown else 0) | (QUALITY_SLEEP if acc.sleep_ms else 0)
        sets_in_hour = [value for timestamp, value in sorted(battery_sets.items())
                        if hour_start <= timestamp < hour_end]
        battery_set_key = (sets_in_hour[-1] if sets_in_hour
                           else _battery_set_at(battery_sets, hour_start))
        if hour_start == last_hour:
            acc.quality_flags |= QUALITY_UNKNOWN if unknown else 0
            return acc
        values = (
            hour_start, 1, IMPORT_GENERATION, AGGREGATION_VERSION, hour_end, 1,
            battery_set_key, acc.soc_start, acc.soc_end, acc.soc_min, acc.soc_max,
            acc.soc_integral_percent_ms, acc.charged_energy_wh, acc.discharged_energy_wh,
            acc.observed_ms, acc.sleep_ms, acc.unknown_ms, acc.charging_ms, acc.discharging_ms,
            acc.full_ms, acc.other_state_ms, acc.ac_online_ms, acc.ac_offline_ms,
            acc.ac_unknown_ms, acc.under_20_ms, acc.above_80_ms, acc.above_95_ms,
            acc.full_on_ac_ms, acc.charge_power_integral_w_ms, acc.charge_power_max_w,
            acc.charge_power_valid_ms, acc.discharge_power_integral_w_ms,
            acc.discharge_power_max_w, acc.discharge_power_valid_ms, acc.direct_power_ms,
            acc.estimated_power_ms, acc.unknown_power_ms, acc.poll_count,
            acc.state_event_count, acc.quality_flags, acc.energy_provenance_mask,
        )
        placeholders = ",".join("?" for _ in values)
        db.execute(f"INSERT INTO hourly_history VALUES({placeholders})", values)
    return None


def _recent_state(state: str) -> BatteryState:
    return {
        "charging": BatteryState.CHARGING,
        "discharging": BatteryState.DISCHARGING,
        "full": BatteryState.FULL,
        "charged": BatteryState.FULL,
        "fully-charged": BatteryState.FULL,
    }.get(state.lower(), BatteryState.OTHER)


def _recent_flags(sample: AggregateSample, *, broken: bool) -> int:
    ac = 0 if sample.ac_online is None else 2 if sample.ac_online else 1
    method = METHODS.get(sample.power_method, 0) << POWER_METHOD_SHIFT
    confidence = CONFIDENCE.get(sample.power_confidence, 0) << POWER_CONFIDENCE_SHIFT
    return (ac | method | confidence | (0x20 if sample.power_approximate else 0)
            | (BREAK_BEFORE if broken else 0))


def _seed_recent_series(samples: list[AggregateSample], sleep: list[tuple[int, int]],
                        battery_sets: dict[int, str]) -> bytes:
    latest = samples[-1].timestamp_ms
    selected = [item for item in samples if item.timestamp_ms >= latest - 8 * 3_600_000][-480:]
    points = []
    previous = None
    for item in selected:
        valid = previous is not None and _continuous(previous, item)
        same_session = previous is not None and _session_kind(previous) == _session_kind(item)
        crosses_sleep = bool(previous and any(
            start < item.timestamp_ms and end > previous.timestamp_ms for start, end in sleep
        ))
        result = (None if not valid or not same_session or crosses_sleep else compatible_delta(
            previous.energy_counters, item.energy_counters,
            elapsed_ms=item.timestamp_ms - previous.timestamp_ms,
            direction=_session_kind(previous),
        ))
        points.append(RecentPoint(
            item.timestamp_ms, round(item.percentage * 1_000),
            None if item.power_w is None else round(item.power_w * 1_000),
            None if result is None else result.value_wh,
            _recent_state(item.state), None, _battery_set_at(battery_sets, item.timestamp_ms),
            _recent_flags(item, broken=previous is None or not valid or not same_session
                          or crosses_sleep),
        ))
        previous = item
    return encode_recent_series(points)


def _runtime_accumulator(value: HourAccumulator, battery_set_key: str) -> HourlyAccumulator:
    names = (
        "soc_min", "soc_max", "soc_integral_percent_ms", "charged_energy_wh",
        "discharged_energy_wh", "observed_ms", "sleep_ms", "unknown_ms",
        "charging_ms", "discharging_ms", "full_ms", "other_state_ms",
        "ac_online_ms", "ac_offline_ms", "ac_unknown_ms", "under_20_ms",
        "above_80_ms", "above_95_ms", "full_on_ac_ms",
        "charge_power_integral_w_ms", "charge_power_max_w", "charge_power_valid_ms",
        "discharge_power_integral_w_ms", "discharge_power_max_w",
        "discharge_power_valid_ms", "direct_power_ms", "estimated_power_ms",
        "unknown_power_ms", "poll_count", "state_event_count", "quality_flags",
        "energy_provenance_mask",
    )
    values = {name: getattr(value, name) for name in names}
    return HourlyAccumulator(
        hour_start_ms=value.hour_start_ms, battery_set_key=battery_set_key,
        soc_first=value.soc_start, soc_last=value.soc_end, **values,
    )


def _seed_batteries(source: sqlite3.Connection, latest: AggregateSample,
                    batteries: dict[str, int]) -> tuple[BatteryCheckpoint, ...]:
    rows = source.execute(
        "SELECT * FROM battery_samples WHERE timestamp=? ORDER BY identity,id",
        (latest.timestamp_ms // 1_000,),
    ).fetchall()
    result = []
    for row in rows:
        identity = str(row["identity"] or row["device"] or "legacy-unknown")
        percentage = float(row["percentage"])
        result.append(BatteryCheckpoint(
            batteries[identity], identity, True, str(row["state"]), percentage,
            _finite(row["power_now_w"]), _finite(row["current_now_a"]),
            _finite(row["voltage_now_v"]), _finite(row["energy_now_wh"]),
            _finite(row["charge_now_ah"]), _finite(row["upower_energy_rate_w"]),
            latest.power_w if len(rows) == 1 else None,
            latest.power_method if len(rows) == 1 else None,
            bool(latest.power_approximate) if len(rows) == 1 else None,
            latest.power_confidence if len(rows) == 1 else None,
        ))
    if result:
        return tuple(result)
    identity = latest.battery_identity
    return (BatteryCheckpoint(
        batteries[identity], identity, True, latest.state, latest.percentage,
        resolved_power_w=latest.power_w, power_method=latest.power_method,
        power_approximate=bool(latest.power_approximate),
        power_confidence=latest.power_confidence,
    ),)


def _write_seed_checkpoint(db: sqlite3.Connection, source: sqlite3.Connection,
                           samples: list[AggregateSample], sleep: list[tuple[int, int]],
                           battery_sets: dict[int, str], batteries: dict[str, int],
                           trailing: HourAccumulator) -> None:
    latest = samples[-1]
    monotonic_ms = latest.monotonic_ms
    boottime_ms = latest.boottime_ms
    if (monotonic_ms is None or boottime_ms is None or monotonic_ms < 0
            or boottime_ms < monotonic_ms):
        monotonic_ms = boottime_ms = 0
    battery_set_key = _battery_set_at(battery_sets, latest.timestamp_ms)
    snapshot = GenerationSnapshot(
        1, latest.timestamp_ms, latest.timestamp_ms, latest.boot_id,
        monotonic_ms * 1_000_000, boottime_ms * 1_000_000, 60_000,
        None if latest.ac_online is None else bool(latest.ac_online), None,
        _seed_batteries(source, latest, batteries),
        _runtime_accumulator(trailing, battery_set_key),
        _seed_recent_series(samples, sleep, battery_sets),
    )
    V1Storage(Path("unused")).write_generation(db, snapshot)


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
        source.execute("BEGIN")
        _validate_source(source)
        destination = sqlite3.connect(destination_path)
        destination.row_factory = sqlite3.Row
        destination.execute("PRAGMA foreign_keys = ON")
        destination.execute("BEGIN IMMEDIATE")
        create_v1_schema(destination)
        batteries = _insert_batteries(destination, source)
        samples = _load_samples(source)
        if not samples:
            raise PreV1ConversionError("source has no aggregate sample for a seed checkpoint")
        battery_sets = _load_battery_sets(source, samples)
        _insert_state_events(destination, source, batteries)
        _insert_health(destination, source, batteries)
        _insert_sessions(destination, source, battery_sets)
        _insert_sleep(destination, source)
        sleep = _load_sleep(source)
        trailing = _insert_hourly(destination, samples, sleep, battery_sets)
        if trailing is None:
            raise PreV1ConversionError("could not construct trailing checkpoint accumulator")
        _write_seed_checkpoint(
            destination, source, samples, sleep, battery_sets, batteries, trailing
        )
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

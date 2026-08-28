"""Independent validation of a temporary pre-v1 v2-to-v4 conversion.

The reference calculations in this module intentionally do not import or call
the converter.  Source facts are reconstructed directly from the private v2
tables and compared with a read-only schema-v4 destination.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .energy_integrity import (
    ENERGY_PROVENANCE_CHARGE,
    ENERGY_PROVENANCE_NATIVE,
    ENERGY_PROVENANCE_REJECTED,
    MAX_PLAUSIBLE_POWER_W_PER_BATTERY,
)
from .recent_series import decode_recent_series

from .schema import V1_SCHEMA_VERSION, V2_REQUIRED_TABLES, create_v1_schema
from .v1_storage import V1Storage, generation_digest


HOUR_MS = 3_600_000
CONTINUITY_LIMIT_MS = 180_000
CLOCK_SKEW_LIMIT_MS = 5_000
FLOAT_FIELDS = frozenset({
    "soc_start", "soc_end", "soc_min", "soc_max", "soc_integral_percent_ms",
    "charged_energy_wh", "discharged_energy_wh", "charge_power_integral_w_ms",
    "charge_power_max_w", "discharge_power_integral_w_ms", "discharge_power_max_w",
})


@dataclass(frozen=True)
class Mismatch:
    table: str
    key: str
    field: str
    expected: Any
    actual: Any


@dataclass
class ValidationReport:
    source_schema: int | None = None
    destination_schema: int | None = None
    source_counts: dict[str, int] = field(default_factory=dict)
    destination_counts: dict[str, int] = field(default_factory=dict)
    checked_hours: int = 0
    checked_batteries: int = 0
    checked_sessions: int = 0
    checked_sleeps: int = 0
    checked_health_events: int = 0
    warnings: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    totals: dict[str, float | int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.mismatches

    def add(self, table: str, key: object, field_name: str,
            expected: Any, actual: Any) -> None:
        self.mismatches.append(Mismatch(table, str(key), field_name, expected, actual))

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": "PASS" if self.passed else "FAIL",
            "source_schema": self.source_schema,
            "destination_schema": self.destination_schema,
            "source_counts": self.source_counts,
            "destination_counts": self.destination_counts,
            "checked": {
                "hours": self.checked_hours,
                "batteries": self.checked_batteries,
                "sessions": self.checked_sessions,
                "sleeps": self.checked_sleeps,
                "health_events": self.checked_health_events,
            },
            "warnings": self.warnings,
            "mismatches": [asdict(item) for item in self.mismatches],
            "totals": self.totals,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_text(self) -> str:
        lines = [
            f"{'PASS' if self.passed else 'FAIL'} pre-v1 conversion",
            f"schemas: source v{self.source_schema} -> destination v{self.destination_schema}",
            ("checked: "
             f"{self.checked_hours} hours, {self.checked_batteries} batteries, "
             f"{self.checked_sessions} sessions, {self.checked_sleeps} sleeps, "
             f"{self.checked_health_events} health events"),
            "source rows: " + ", ".join(
                f"{key}={value}" for key, value in sorted(self.source_counts.items())
            ),
            "destination rows: " + ", ".join(
                f"{key}={value}" for key, value in sorted(self.destination_counts.items())
            ),
            "totals: " + ", ".join(f"{key}={value}" for key, value in sorted(self.totals.items())),
        ]
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        lines.extend(
            f"mismatch: {item.table}[{item.key}].{item.field}: "
            f"expected={item.expected!r} actual={item.actual!r}"
            for item in self.mismatches
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class SourcePoint:
    at: int
    soc: float
    state: str
    ac: int | None
    power: float | None
    approximate: bool
    energy: float | None
    monotonic: int | None
    boottime: int | None
    boot: str
    identity: str
    counters: tuple[tuple[str, str, float, float | None], ...] = ()
    power_rejected: bool = False


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _tables(db: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        for table in sorted(_tables(db)) if not table.startswith("sqlite_")
    }


def _check_database(db: sqlite3.Connection, label: str, expected_version: int,
                    required: set[str] | frozenset[str], report: ValidationReport) -> bool:
    try:
        quick = tuple(str(row[0]) for row in db.execute("PRAGMA quick_check"))
        if quick != ("ok",):
            report.add(label, "database", "quick_check", "ok", "; ".join(quick))
        version = _version(db)
        if label == "source":
            report.source_schema = version
        else:
            report.destination_schema = version
        if version != expected_version:
            report.add(label, "database", "user_version", expected_version, version)
        missing = required - _tables(db)
        if missing:
            report.add(label, "database", "required_tables", [], sorted(missing))
        return quick == ("ok",) and version == expected_version and not missing
    except sqlite3.Error as error:
        report.add(label, "database", "readable", True, str(error))
        return False


def _schema_objects(db: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): " ".join(str(row[2]).split()).lower()
        for row in db.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    }


def _check_v4_contract(destination: sqlite3.Connection, report: ValidationReport) -> None:
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute("PRAGMA foreign_keys=ON")
        create_v1_schema(reference)
        expected = _schema_objects(reference)
    finally:
        reference.close()
    actual = _schema_objects(destination)
    for key in sorted(set(expected) | set(actual)):
        if expected.get(key) != actual.get(key):
            report.add("schema", f"{key[0]}:{key[1]}", "definition",
                       expected.get(key), actual.get(key))


def _number(value: object) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _milliseconds(value: object) -> int | None:
    number = _number(value)
    return None if number is None else round(number * 1_000)


def _source_points(source: sqlite3.Connection, report: ValidationReport) -> list[SourcePoint]:
    raw_counters: dict[int, list[tuple[str, str, float, float | None]] | None] = {}
    for raw in source.execute("SELECT * FROM battery_samples ORDER BY timestamp,identity,id"):
        at = int(raw["timestamp"]) * 1_000
        identity = str(raw["identity"] or raw["device"] or "legacy-unknown")
        energy = _number(raw["energy_now_wh"])
        charge = _number(raw["charge_now_ah"])
        voltage = _number(raw["voltage_now_v"])
        counter = None
        if energy is not None and energy >= 0:
            counter = (identity, "energy", energy, None)
        elif charge is not None and charge >= 0 and voltage is not None and voltage > 0:
            counter = (identity, "charge", charge, voltage)
        values = raw_counters.setdefault(at, [])
        if counter is None:
            raw_counters[at] = None
        elif values is not None:
            values.append(counter)
    selected: dict[int, SourcePoint] = {}
    for row in source.execute("SELECT * FROM samples ORDER BY timestamp, id"):
        at = int(row["timestamp"]) * 1_000
        power = _number(row["power_w"])
        count = max(1, len(raw_counters.get(at) or ()))
        rejected_power = power is not None and (
            power < 0 or power > MAX_PLAUSIBLE_POWER_W_PER_BATTERY * count
        )
        point = SourcePoint(
            at, float(row["percentage"]), str(row["state"]),
            None if row["ac_online"] is None else int(row["ac_online"]),
            None if rejected_power else power, bool(row["power_approximate"] or 0),
            _number(row["energy_wh"]), _milliseconds(row["monotonic_s"]),
            _milliseconds(row["boottime_s"]), str(row["boot_id"] or "legacy-unknown-boot"),
            str(row["battery_identity"] or row["device"] or "legacy-unknown"),
            tuple(raw_counters.get(at) or ()), rejected_power,
        )
        if at in selected and selected[at] != point:
            report.add("samples", at, "aggregate_ambiguity", selected[at], point)
        selected[at] = point
    missing_raw = int(source.execute(
        "SELECT count(*) FROM samples s WHERE NOT EXISTS ("
        "SELECT 1 FROM battery_samples b WHERE b.timestamp=s.timestamp "
        "AND b.identity=COALESCE(NULLIF(s.battery_identity,''), s.device))"
    ).fetchone()[0])
    if missing_raw:
        report.warnings.append(f"{missing_raw} aggregate samples have no matching raw child row")
    return [selected[key] for key in sorted(selected)]


def _is_continuous(left: SourcePoint, right: SourcePoint) -> bool:
    elapsed = right.at - left.at
    if not 0 < elapsed <= CONTINUITY_LIMIT_MS:
        return False
    if left.boot != right.boot or left.identity != right.identity:
        return False
    for before, after in ((left.monotonic, right.monotonic), (left.boottime, right.boottime)):
        if before is not None and after is not None:
            if after < before or abs((after - before) - elapsed) > CLOCK_SKEW_LIMIT_MS:
                return False
    return True


def _source_session_kind(point: SourcePoint) -> str | None:
    if point.ac == 0:
        return "discharging"
    if point.ac == 1:
        if point.state.lower() == "charging" or point.soc < 100:
            return "charging"
        return None
    state = point.state.lower()
    return state if state in {"charging", "discharging"} else None


def _source_batteries(source: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
    observations: dict[str, list[tuple[int, str]]] = {}
    for row in source.execute("SELECT timestamp, identity, device FROM battery_samples ORDER BY id"):
        identity = str(row[1] or row[2] or "legacy-unknown")
        observations.setdefault(identity, []).append((int(row[0]) * 1_000, str(row[2] or "")))
    for row in source.execute("SELECT timestamp, battery_identity, device FROM samples ORDER BY id"):
        identity = str(row[1] or row[2] or "legacy-unknown")
        observations.setdefault(identity, []).append((int(row[0]) * 1_000, str(row[2] or "")))
    return {
        identity: (identity, values[0][1] or None,
                   min(item[0] for item in values), max(item[0] for item in values))
        for identity, values in observations.items()
    }


def _destination_batteries(destination: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
    return {
        str(row[0]): (str(row[0]), row[1], int(row[2]), int(row[3]))
        for row in destination.execute(
            "SELECT identity, native_name, first_seen_ms, last_seen_ms FROM batteries"
        )
    }


def _compare_mapping(report: ValidationReport, table: str,
                     expected: dict[Any, Any], actual: dict[Any, Any]) -> None:
    for key in sorted(set(expected) | set(actual), key=str):
        if key not in expected:
            report.add(table, key, "row", None, actual[key])
        elif key not in actual:
            report.add(table, key, "row", expected[key], None)
        elif expected[key] != actual[key]:
            report.add(table, key, "row", expected[key], actual[key])


def _expected_system_events(source: sqlite3.Connection) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    previous: object = object()
    for row in source.execute("SELECT * FROM samples ORDER BY timestamp, id"):
        ac = None if row["ac_online"] is None else int(row["ac_online"])
        if ac != previous:
            result.append((int(row["timestamp"]) * 1_000,
                           str(row["boot_id"] or "legacy-unknown-boot"), "system", None,
                           ac, None, None, None, 8, 0))
            previous = ac
    return result


def _expected_battery_events(source: sqlite3.Connection) -> list[tuple[Any, ...]]:
    raw = source.execute("SELECT * FROM battery_samples ORDER BY timestamp, identity, id").fetchall()
    raw_keys = {(int(row["timestamp"]), str(row["identity"] or row["device"] or "legacy-unknown"))
                for row in raw}
    present_sets: dict[int, set[str]] = {}
    boots: dict[int, str] = {}
    records: dict[int, list[tuple[str, str, float, str]]] = {}
    for row in raw:
        at = int(row["timestamp"])
        identity = str(row["identity"] or row["device"] or "legacy-unknown")
        present_sets.setdefault(at, set()).add(identity)
        boots[at] = str(row["boot_id"] or "legacy-unknown-boot")
        records.setdefault(at, []).append((identity, str(row["state"]),
                                           float(row["percentage"]), boots[at]))
    for row in source.execute("SELECT * FROM samples ORDER BY timestamp, id"):
        at = int(row["timestamp"])
        identity = str(row["battery_identity"] or row["device"] or "legacy-unknown")
        if (at, identity) not in raw_keys:
            records.setdefault(at, []).append((identity, str(row["state"]),
                                               float(row["percentage"]),
                                               str(row["boot_id"] or "legacy-unknown-boot")))

    result: list[tuple[Any, ...]] = []
    last_values: dict[str, str] = {}
    last_raw_set: set[str] | None = None
    for at in sorted(records):
        if at in present_sets:
            current_set = present_sets[at]
            if last_raw_set is not None:
                for identity in sorted(last_raw_set - current_set):
                    result.append((at * 1_000, boots[at], "battery", identity,
                                   None, 0, None, None, 1, 0))
                    last_values.pop(identity, None)
            last_raw_set = current_set
        for identity, state, soc, boot in sorted(records[at]):
            if last_values.get(identity) != state:
                old = last_values.get(identity)
                reason = (1 if old is None else 0)
                if old is None or old != state:
                    reason |= 2
                result.append((at * 1_000, boot, "battery", identity,
                               None, 1, state, soc, reason, 0))
                last_values[identity] = state
    return sorted(result, key=lambda item: (item[0], item[2], str(item[3])))


def _destination_events(destination: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return sorted((int(row[0]), str(row[1]), str(row[2]), row[3], row[4], row[5],
                   row[6], row[7], int(row[8]), int(row[9]))
                  for row in destination.execute(
        "SELECT e.occurred_at_ms, e.boot_id, e.scope, b.identity, e.ac_online, "
        "e.battery_present, e.battery_state, e.soc_percent, e.reason_mask, "
        "e.source_generation FROM state_events e "
        "LEFT JOIN batteries b ON b.id=e.battery_id"
    ))


def _compare_rows(report: ValidationReport, table: str,
                  expected: list[tuple[Any, ...]], actual: list[tuple[Any, ...]]) -> None:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    for row in sorted(set(expected_counts) | set(actual_counts), key=str):
        if expected_counts.get(row, 0) != actual_counts.get(row, 0):
            report.add(table, row[0] if row else "row", "occurrences",
                       expected_counts.get(row, 0), actual_counts.get(row, 0))


def _expected_sessions(source: sqlite3.Connection, points: list[SourcePoint],
                       raw_sets: dict[int, str]) -> dict[int, tuple[Any, ...]]:
    def battery_set(at: int) -> str:
        prior = [timestamp for timestamp in raw_sets if timestamp <= at]
        return raw_sets[max(prior)] if prior else "legacy-unknown"

    return {
        int(row["id"]): (
            str(row["kind"]), int(row["started_at"]) * 1_000,
            None if row["ended_at"] is None else int(row["ended_at"]) * 1_000,
            float(row["start_percentage"]),
            None if row["end_percentage"] is None else float(row["end_percentage"]),
            None, None, battery_set(int(row["started_at"]) * 1_000), row["end_reason"], 0,
        )
        for row in source.execute("SELECT * FROM sessions")
    }


def _raw_sets(source: sqlite3.Connection, points: list[SourcePoint]) -> dict[int, str]:
    values: dict[int, set[str]] = {}
    for row in source.execute("SELECT timestamp, identity, device FROM battery_samples"):
        values.setdefault(int(row[0]) * 1_000, set()).add(str(row[1] or row[2] or "legacy-unknown"))
    for point in points:
        values.setdefault(point.at, set()).add(point.identity)
    return {at: "+".join(sorted(identities)) for at, identities in values.items()}


def _destination_sessions(destination: sqlite3.Connection) -> dict[int, tuple[Any, ...]]:
    return {int(row[0]): tuple(row[1:]) for row in destination.execute(
        "SELECT id, kind, started_at_ms, ended_at_ms, start_soc, end_soc, "
        "start_event_id, end_event_id, battery_set_key, end_reason, source_generation "
        "FROM sessions"
    )}


def _expected_sleeps(source: sqlite3.Connection) -> dict[int, tuple[Any, ...]]:
    return {int(row["id"]): (
        int(row["started_at"]) * 1_000, int(row["ended_at"]) * 1_000,
        str(row["kind"]), str(row["source"]), row["boot_id"],
        int(row["ended_at"]) * 1_000, None, None,
        row["pre_percentage"], row["post_percentage"], 0, 1,
    ) for row in source.execute("SELECT * FROM sleep_intervals")}


def _destination_sleeps(destination: sqlite3.Connection) -> dict[int, tuple[Any, ...]]:
    return {int(row[0]): tuple(row[1:]) for row in destination.execute(
        "SELECT id, started_at_ms, ended_at_ms, kind, source, boot_id, detected_at_ms, "
        "pre_event_id, post_event_id, pre_soc, post_soc, source_generation, revision "
        "FROM sleep_intervals"
    )}


def _expected_health(source: sqlite3.Connection) -> list[tuple[Any, ...]]:
    previous: dict[str, tuple[Any, ...]] = {}
    result: list[tuple[Any, ...]] = []
    for row in source.execute("SELECT * FROM battery_samples ORDER BY identity, timestamp, id"):
        identity = str(row["identity"] or row["device"] or "legacy-unknown")
        native = tuple(value if value is not None and value > 0 else None for value in (
            _number(row["charge_full_ah"]), _number(row["charge_full_design_ah"]),
            _number(row["energy_full_wh"]), _number(row["energy_full_design_wh"]),
        ))
        if not any(value is not None for value in native) or previous.get(identity) == native:
            continue
        source_name = "legacy-v2-energy" if native[2] is not None or native[3] is not None else "legacy-v2-charge"
        result.append((identity, int(row["timestamp"]) * 1_000, *native,
                       None, None, source_name, "legacy-v2", 0))
        previous[identity] = native
    return result


def _destination_health(destination: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in destination.execute(
        "SELECT b.identity, h.observed_at_ms, h.charge_full_ah, h.charge_full_design_ah, "
        "h.energy_full_wh, h.energy_full_design_wh, h.cycle_count, h.voltage_design_v, "
        "h.source, h.provenance, h.source_generation FROM battery_health h "
        "JOIN batteries b ON b.id=h.battery_id "
        "ORDER BY b.identity, h.observed_at_ms"
    )]


HOURLY_FIELDS = (
    "hour_start_ms", "revision", "source_generation", "aggregation_version",
    "finalized_at_ms", "is_final", "battery_set_key", "soc_start", "soc_end",
    "soc_min", "soc_max", "soc_integral_percent_ms", "charged_energy_wh",
    "discharged_energy_wh", "observed_ms", "sleep_ms", "unknown_ms", "charging_ms",
    "discharging_ms", "full_ms", "other_state_ms", "ac_online_ms", "ac_offline_ms",
    "ac_unknown_ms", "under_20_ms", "above_80_ms", "above_95_ms", "full_on_ac_ms",
    "charge_power_integral_w_ms", "charge_power_max_w", "charge_power_valid_ms",
    "discharge_power_integral_w_ms", "discharge_power_max_w",
    "discharge_power_valid_ms", "direct_power_ms", "estimated_power_ms",
    "unknown_power_ms", "poll_count", "state_event_count", "quality_flags",
    "energy_provenance_mask",
)


def _blank_hour(hour: int) -> dict[str, Any]:
    return {
        "hour_start_ms": hour, "revision": 1, "source_generation": 0,
        "aggregation_version": 1, "finalized_at_ms": hour + HOUR_MS, "is_final": 1,
        "battery_set_key": None, "soc_start": None, "soc_end": None, "soc_min": None,
        "soc_max": None, "soc_integral_percent_ms": 0.0, "charged_energy_wh": 0.0,
        "discharged_energy_wh": 0.0, "observed_ms": 0, "sleep_ms": 0,
        "unknown_ms": 0, "charging_ms": 0, "discharging_ms": 0, "full_ms": 0,
        "other_state_ms": 0, "ac_online_ms": 0, "ac_offline_ms": 0,
        "ac_unknown_ms": 0, "under_20_ms": 0, "above_80_ms": 0,
        "above_95_ms": 0, "full_on_ac_ms": 0, "charge_power_integral_w_ms": 0.0,
        "charge_power_max_w": None, "charge_power_valid_ms": 0,
        "discharge_power_integral_w_ms": 0.0, "discharge_power_max_w": None,
        "discharge_power_valid_ms": 0, "direct_power_ms": 0, "estimated_power_ms": 0,
        "unknown_power_ms": 0, "poll_count": 0, "state_event_count": 0,
        "quality_flags": 0, "energy_provenance_mask": 0,
    }


def _union_duration(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    pieces = sorted((max(start, a), min(end, b)) for a, b in intervals if a < end and b > start)
    merged: list[list[int]] = []
    for left, right in pieces:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return sum(right - left for left, right in merged)


def _active_parts(start: int, end: int, sleeps: list[tuple[int, int]]) -> list[tuple[int, int]]:
    boundaries = {start, end}
    for left, right in sleeps:
        if left < end and right > start:
            boundaries.update((max(start, left), min(end, right)))
    ordered = sorted(boundaries)
    return [(left, right) for left, right in zip(ordered, ordered[1:])
            if not any(a < right and b > left for a, b in sleeps)]


def _threshold_duration(start_soc: float, end_soc: float, duration: int,
                        threshold: float, above: bool) -> int:
    if start_soc == end_soc:
        return duration if (start_soc > threshold if above else start_soc < threshold) else 0
    low, high = sorted((start_soc, end_soc))
    if above:
        if high <= threshold:
            return 0
        if low >= threshold:
            return duration
        return round(duration * (high - threshold) / (high - low))
    if low >= threshold:
        return 0
    if high <= threshold:
        return duration
    return round(duration * (threshold - low) / (high - low))


def _independent_energy_delta(left: SourcePoint, right: SourcePoint
                              ) -> tuple[float | None, int, bool]:
    if not left.counters or not right.counters:
        return None, 0, False
    before = {item[0]: item for item in left.counters}
    after = {item[0]: item for item in right.counters}
    if before.keys() != after.keys():
        return None, ENERGY_PROVENANCE_REJECTED, True
    elapsed = right.at - left.at
    total = 0.0
    provenance = 0
    direction = _source_session_kind(left)
    for identity in sorted(before):
        a, b = before[identity], after[identity]
        if a[1] != b[1]:
            return None, ENERGY_PROVENANCE_REJECTED, True
        if a[1] == "energy":
            delta = b[2] - a[2]
            provenance |= ENERGY_PROVENANCE_NATIVE
        elif a[3] and b[3]:
            delta = (b[2] - a[2]) * (a[3] + b[3]) / 2
            provenance |= ENERGY_PROVENANCE_CHARGE
        else:
            return None, ENERGY_PROVENANCE_REJECTED, True
        if ((direction == "charging" and delta < 0)
                or (direction == "discharging" and delta > 0)
                or direction is None
                or abs(delta) * 3_600_000 / elapsed > MAX_PLAUSIBLE_POWER_W_PER_BATTERY):
            return None, ENERGY_PROVENANCE_REJECTED, True
        total += delta
    return total, provenance, False


def _accumulate_reference(row: dict[str, Any], left: SourcePoint, right: SourcePoint,
                          start: int, end: int, *, energy_allowed: bool) -> None:
    duration = end - start
    whole = right.at - left.at
    soc_a = left.soc + (right.soc - left.soc) * ((start - left.at) / whole)
    soc_b = left.soc + (right.soc - left.soc) * ((end - left.at) / whole)
    row["observed_ms"] += duration
    if row["soc_start"] is None:
        row["soc_start"] = soc_a
    row["soc_end"] = soc_b
    row["soc_min"] = min(soc_a, soc_b) if row["soc_min"] is None else min(row["soc_min"], soc_a, soc_b)
    row["soc_max"] = max(soc_a, soc_b) if row["soc_max"] is None else max(row["soc_max"], soc_a, soc_b)
    row["soc_integral_percent_ms"] += duration * (soc_a + soc_b) / 2
    state = left.state.lower()
    state_field = ({"charging": "charging_ms", "discharging": "discharging_ms"}.get(state)
                   or ("full_ms" if state in {"full", "fully-charged", "charged"} else "other_state_ms"))
    row[state_field] += duration
    row[{1: "ac_online_ms", 0: "ac_offline_ms"}.get(left.ac, "ac_unknown_ms")] += duration
    row["under_20_ms"] += _threshold_duration(soc_a, soc_b, duration, 20, False)
    row["above_80_ms"] += _threshold_duration(soc_a, soc_b, duration, 80, True)
    row["above_95_ms"] += _threshold_duration(soc_a, soc_b, duration, 95, True)
    if state in {"full", "fully-charged", "charged"} and left.ac == 1:
        row["full_on_ac_ms"] += duration
    if left.power is None or left.power < 0:
        row["unknown_power_ms"] += duration
    else:
        row["estimated_power_ms" if left.approximate else "direct_power_ms"] += duration
        if state in {"charging", "discharging"}:
            prefix = "charge" if state == "charging" else "discharge"
            row[f"{prefix}_power_integral_w_ms"] += left.power * duration
            row[f"{prefix}_power_valid_ms"] += duration
            maximum = f"{prefix}_power_max_w"
            row[maximum] = left.power if row[maximum] is None else max(row[maximum], left.power)
    if left.power_rejected:
        row["quality_flags"] |= 8
    if energy_allowed:
        delta, provenance, rejected = _independent_energy_delta(left, right)
        if rejected:
            row["quality_flags"] |= 4
            row["energy_provenance_mask"] |= ENERGY_PROVENANCE_REJECTED
        elif delta is not None:
            portion = delta * duration / whole
            row["charged_energy_wh" if portion >= 0 else "discharged_energy_wh"] += abs(portion)
            row["energy_provenance_mask"] |= provenance


def _expected_hours(source: sqlite3.Connection, points: list[SourcePoint],
                    expected_events: list[tuple[Any, ...]], raw_sets: dict[int, str]
                    ) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    sleeps = [(int(row[0]) * 1_000, int(row[1]) * 1_000) for row in source.execute(
        "SELECT started_at, ended_at FROM sleep_intervals"
    )]
    timeline = [point.at for point in points] + [value for pair in sleeps for value in pair]
    if not timeline:
        return {}, {}
    first = min(timeline) // HOUR_MS * HOUR_MS
    trailing_hour = points[-1].at // HOUR_MS * HOUR_MS
    hours = {hour: _blank_hour(hour) for hour in range(first, trailing_hour + 1, HOUR_MS)}
    for hour, row in hours.items():
        coverage_end = hour + HOUR_MS if hour < trailing_hour else points[-1].at
        row["sleep_ms"] = _union_duration(sleeps, hour, coverage_end)
        row["poll_count"] = sum(hour <= point.at <= coverage_end for point in points)
        row["state_event_count"] = sum(hour <= event[0] < hour + HOUR_MS for event in expected_events)
        sets = [value for at, value in sorted(raw_sets.items()) if hour <= at < hour + HOUR_MS]
        prior = [at for at in raw_sets if at <= hour]
        row["battery_set_key"] = sets[-1] if sets else (raw_sets[max(prior)] if prior else "legacy-unknown")

    for left, right in zip(points, points[1:]):
        if not _is_continuous(left, right):
            continue
        crosses_sleep = any(a < right.at and b > left.at for a, b in sleeps)
        cursor = left.at
        while cursor < right.at:
            hour = cursor // HOUR_MS * HOUR_MS
            if hour not in hours:
                break
            boundary = min(right.at, hour + HOUR_MS)
            for start, end in _active_parts(cursor, boundary, sleeps):
                _accumulate_reference(
                    hours[hour], left, right, start, end,
                    energy_allowed=(not crosses_sleep
                                    and _source_session_kind(left)
                                    == _source_session_kind(right)),
                )
            cursor = boundary
    for row in hours.values():
        target = (HOUR_MS if row["hour_start_ms"] < trailing_hour
                  else points[-1].at - trailing_hour)
        row["unknown_ms"] = target - row["observed_ms"] - row["sleep_ms"]
        row["quality_flags"] |= ((1 if row["unknown_ms"] else 0)
                                 | (2 if row["sleep_ms"] else 0))
    trailing = hours.pop(trailing_hour)
    return hours, trailing


def _actual_hours(destination: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {int(row["hour_start_ms"]): {name: row[name] for name in HOURLY_FIELDS}
            for row in destination.execute("SELECT * FROM hourly_history")}


def _check_seed_checkpoint(source: sqlite3.Connection, destination: sqlite3.Connection,
                           points: list[SourcePoint], trailing: dict[str, Any],
                           report: ValidationReport) -> None:
    headers = destination.execute(
        "SELECT * FROM checkpoint_generations ORDER BY generation"
    ).fetchall()
    if len(headers) != 1:
        report.add("checkpoint_generations", "seed", "count", 1, len(headers))
        return
    header = headers[0]
    latest = points[-1]
    expected_header = (1, 1, latest.at, latest.at, latest.boot, 1)
    actual_header = (
        int(header["generation"]), int(header["format_version"]),
        int(header["created_at_ms"]), int(header["last_poll_at_ms"]),
        str(header["boot_id"]), int(header["complete"]),
    )
    if actual_header != expected_header:
        report.add("checkpoint_generations", 1, "header", expected_header, actual_header)
    try:
        snapshot = V1Storage._load_generation(V1Storage(Path("unused")), destination, 1)
        digest = None if snapshot is None else generation_digest(snapshot)
        if digest != str(header["payload_digest"]):
            report.add("checkpoint_generations", 1, "payload_digest",
                       str(header["payload_digest"]), digest)
    except (ValueError, sqlite3.Error) as error:
        report.add("checkpoint_generations", 1, "payload", "valid", str(error))

    try:
        recent = decode_recent_series(bytes(header["recent_series"]))
        if not recent:
            report.add("checkpoint_generations", 1, "recent_series", "non-empty", 0)
        else:
            last = recent[-1]
            expected_last = (latest.at, round(latest.soc * 1_000))
            actual_last = (last.timestamp_ms, last.soc_millipercent)
            if actual_last != expected_last:
                report.add("checkpoint_generations", 1, "recent_last",
                           expected_last, actual_last)
            if len(recent) > 480 or recent[-1].timestamp_ms - recent[0].timestamp_ms > 8 * HOUR_MS:
                report.add("checkpoint_generations", 1, "recent_window", "<=8h/480", len(recent))
    except ValueError as error:
        report.add("checkpoint_generations", 1, "recent_series", "valid", str(error))

    row = destination.execute(
        "SELECT * FROM checkpoint_hourly WHERE generation=1"
    ).fetchone()
    if row is None:
        report.add("checkpoint_hourly", 1, "row", "present", None)
    else:
        mapping = {
            "hour_start_ms": "hour_start_ms", "soc_first": "soc_start",
            "soc_last": "soc_end", "soc_min": "soc_min", "soc_max": "soc_max",
        }
        fields = (
            "soc_integral_percent_ms", "charged_energy_wh", "discharged_energy_wh",
            "observed_ms", "sleep_ms", "unknown_ms", "charging_ms", "discharging_ms",
            "full_ms", "other_state_ms", "ac_online_ms", "ac_offline_ms",
            "ac_unknown_ms", "under_20_ms", "above_80_ms", "above_95_ms",
            "full_on_ac_ms", "charge_power_integral_w_ms", "charge_power_max_w",
            "charge_power_valid_ms", "discharge_power_integral_w_ms",
            "discharge_power_max_w", "discharge_power_valid_ms", "direct_power_ms",
            "estimated_power_ms", "unknown_power_ms", "poll_count", "state_event_count",
            "quality_flags", "energy_provenance_mask",
        )
        for actual_name, expected_name in tuple(mapping.items()) + tuple((name, name) for name in fields):
            if not _equal(trailing[expected_name], row[actual_name], expected_name):
                report.add("checkpoint_hourly", 1, actual_name,
                           trailing[expected_name], row[actual_name])

    raw = source.execute(
        "SELECT identity,device,state,percentage FROM battery_samples WHERE timestamp=?",
        (latest.at // 1_000,),
    ).fetchall()
    expected_batteries = sorted(
        (str(item[0] or item[1] or "legacy-unknown"), str(item[2]), float(item[3]))
        for item in raw
    ) or [(latest.identity, latest.state, latest.soc)]
    actual_batteries = sorted(tuple(item) for item in destination.execute(
        "SELECT b.identity,c.state,c.soc_percent FROM checkpoint_batteries c "
        "JOIN batteries b ON b.id=c.battery_id WHERE c.generation=1"
    ))
    if expected_batteries != actual_batteries:
        report.add("checkpoint_batteries", 1, "rows", expected_batteries, actual_batteries)


def _equal(expected: Any, actual: Any, field_name: str) -> bool:
    if expected is None or actual is None:
        return expected is actual
    if field_name in FLOAT_FIELDS:
        return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-6)
    return expected == actual


def _compare_hours(report: ValidationReport, expected: dict[int, dict[str, Any]],
                   actual: dict[int, dict[str, Any]]) -> None:
    for hour in sorted(set(expected) | set(actual)):
        if hour not in expected:
            report.add("hourly_history", hour, "row", None, "unexpected")
            continue
        if hour not in actual:
            report.add("hourly_history", hour, "row", "expected", None)
            continue
        for field_name in HOURLY_FIELDS:
            if not _equal(expected[hour][field_name], actual[hour][field_name], field_name):
                report.add("hourly_history", hour, field_name,
                           expected[hour][field_name], actual[hour][field_name])


def _compare_metadata(report: ValidationReport, source: sqlite3.Connection,
                      destination: sqlite3.Connection) -> None:
    expected = {str(row[0]): str(row[1]) for row in source.execute(
        "SELECT key, value FROM metadata"
    )}
    expected["converted_from_schema"] = "2"
    actual = {str(row[0]): str(row[1]) for row in destination.execute(
        "SELECT key, value FROM metadata"
    )}
    _compare_mapping(report, "metadata", expected, actual)


def validate_pre_v1_conversion(source_path: Path, destination_path: Path) -> ValidationReport:
    """Independently compare a read-only private-v2 source and schema-v4 output."""
    report = ValidationReport()
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    required_v4 = {
        "batteries", "state_events", "hourly_history", "hourly_profile_durations",
        "battery_health", "sessions", "sleep_intervals", "metadata",
        "checkpoint_generations", "checkpoint_batteries", "checkpoint_hourly",
        "checkpoint_hourly_profiles",
    }
    try:
        source = _readonly(Path(source_path))
        destination = _readonly(Path(destination_path))
        source.execute("BEGIN")
        destination.execute("BEGIN")
        source_ok = _check_database(source, "source", 2, V2_REQUIRED_TABLES, report)
        destination_ok = _check_database(destination, "destination", V1_SCHEMA_VERSION,
                                         required_v4, report)
        if source_ok:
            report.source_counts = _counts(source)
        if destination_ok:
            report.destination_counts = _counts(destination)
            _check_v4_contract(destination, report)
            try:
                foreign = destination.execute("PRAGMA foreign_key_check").fetchall()
                for row in foreign:
                    report.add("destination", row[0], "foreign_key", "valid", tuple(row))
            except sqlite3.Error as error:
                report.add("destination", "database", "foreign_key_check", "valid", str(error))
        if not source_ok or not destination_ok:
            return report

        expected_batteries = _source_batteries(source)
        actual_batteries = _destination_batteries(destination)
        _compare_mapping(report, "batteries", expected_batteries, actual_batteries)
        report.checked_batteries = len(expected_batteries)

        points = _source_points(source, report)
        discontinuities = sum(not _is_continuous(left, right)
                              for left, right in zip(points, points[1:]))
        if discontinuities:
            report.warnings.append(
                f"{discontinuities} non-continuous aggregate intervals are excluded from observed deltas"
            )
        counter_resets = sum(
            left.energy is not None and right.energy is not None
            and ((left.state.lower() == "charging" and right.energy < left.energy)
                 or (left.state.lower() == "discharging" and right.energy > left.energy))
            for left, right in zip(points, points[1:]) if _is_continuous(left, right)
        )
        if counter_resets:
            report.warnings.append(
                f"{counter_resets} counter movements conflict with battery direction and are excluded"
            )
        expected_events = sorted(_expected_system_events(source) + _expected_battery_events(source),
                                 key=lambda item: (item[0], item[2], str(item[3])))
        actual_events = _destination_events(destination)
        _compare_rows(report, "state_events", expected_events, actual_events)

        sets = _raw_sets(source, points)
        expected_sessions = _expected_sessions(source, points, sets)
        _compare_mapping(report, "sessions", expected_sessions, _destination_sessions(destination))
        report.checked_sessions = len(expected_sessions)

        expected_sleeps = _expected_sleeps(source)
        _compare_mapping(report, "sleep_intervals", expected_sleeps, _destination_sleeps(destination))
        report.checked_sleeps = len(expected_sleeps)

        expected_health = _expected_health(source)
        actual_health = _destination_health(destination)
        _compare_rows(report, "battery_health", expected_health, actual_health)
        report.checked_health_events = len(expected_health)

        expected_hours, trailing = _expected_hours(source, points, expected_events, sets)
        actual_hours = _actual_hours(destination)
        _compare_hours(report, expected_hours, actual_hours)
        report.checked_hours = len(expected_hours)
        _check_seed_checkpoint(source, destination, points, trailing, report)

        profiles = destination.execute(
            "SELECT hour_start_ms, profile, duration_ms FROM hourly_profile_durations"
        ).fetchall()
        for row in profiles:
            report.add("hourly_profile_durations", row[0], str(row[1]), None, int(row[2]))
        if not profiles:
            report.warnings.append("legacy v2 has no historical power-profile timeline; durations remain unknown")

        _compare_metadata(report, source, destination)

        report.totals = {
            key: sum(row[key] for row in expected_hours.values())
            for key in ("observed_ms", "sleep_ms", "unknown_ms",
                        "charged_energy_wh", "discharged_energy_wh")
        }
        return report
    except sqlite3.Error as error:
        report.add("validation", "database", "readable", True, str(error))
        return report
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()

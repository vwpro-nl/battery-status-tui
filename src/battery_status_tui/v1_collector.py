"""Schema-v4 event-driven collector and crash-safe recovery processing."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable

from .energy_integrity import (
    EnergyCounter,
    compatible_delta,
    counters_from_raw,
    plausible_power,
)
from .models import Measurement, RawBatterySnapshot, SleepInterval
from .recent_series import (
    MAX_WINDOW_MS,
    POWER_CONFIDENCE_SHIFT,
    POWER_METHOD_SHIFT,
    BatteryState,
    RecentPoint,
    decode_recent_series,
    encode_recent_series,
)
from .v1_hourly import (
    HOUR_MS,
    QUALITY_ENERGY_REJECTED,
    QUALITY_POWER_REJECTED,
    HourlyAccumulator,
    utc_hour,
)
from .v1_storage import BatteryCheckpoint, GenerationSnapshot, RecoverySelection, V1Storage


WALLCLOCK_TOLERANCE_MS = 5_000
BREAK_BEFORE = 0x0100
METHODS = {
    "unavailable": 0,
    "power-now": 1,
    "current-voltage": 2,
    "upower-energy-rate": 3,
    "energy-delta": 4,
    "charge-delta": 5,
}
CONFIDENCE = {"none": 0, "medium": 1, "high": 2}


@dataclass(frozen=True, slots=True)
class PollResult:
    generation: int
    warnings: tuple[str, ...]
    finalized_hours: tuple[int, ...]
    state_events: int
    health_events: int


class V1CollectorError(ValueError):
    """A poll cannot safely be persisted in schema v4."""


@dataclass(frozen=True, slots=True)
class _PollState:
    timestamp_ms: int
    soc: float
    state: str
    session_kind: str | None
    ac_online: bool | None
    power_w: float | None
    approximate: bool
    power_method: str
    power_confidence: str
    energy_wh: float | None
    boot_id: str
    monotonic_ns: int
    boottime_ns: int
    battery_set_key: str
    profile: str | None
    energy_counters: tuple[EnergyCounter, ...]
    power_rejected: bool


def _insert(db: sqlite3.Connection, table: str, values: dict[str, object]) -> int:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = db.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})",
                        tuple(values.values()))
    return int(cursor.lastrowid)


def _battery_set(measurement: Measurement) -> str:
    identities = sorted({item.identity for item in measurement.raw_batteries if item.identity})
    if not identities and measurement.battery_identity:
        identities.append(measurement.battery_identity)
    return "+".join(identities) or "unknown-battery-set"


def _state(measurement: Measurement, profile: str | None) -> _PollState:
    percentages = [measurement.percentage]
    percentages.extend(item.percentage for item in measurement.raw_batteries)
    if any(not 0 <= value <= 100 for value in percentages):
        raise V1CollectorError(
            f"battery SoC {measurement.percentage!r} is outside the valid 0..100 "
            "percent range; poll was not stored"
        )
    monotonic = measurement.monotonic_s
    boottime = measurement.boottime_s
    if monotonic is None and measurement.raw_batteries:
        monotonic = max(item.monotonic_s for item in measurement.raw_batteries)
    if boottime is None and measurement.raw_batteries:
        boottime = max(item.boottime_s for item in measurement.raw_batteries)
    if monotonic is None or boottime is None or not measurement.boot_id:
        raise ValueError("schema-v4 poll requires boot, monotonic, and boottime clocks")
    power, power_rejected = plausible_power(
        measurement.power_w, len(measurement.raw_batteries)
    )
    return _PollState(
        measurement.timestamp * 1_000, measurement.percentage, measurement.state,
        measurement.session_kind, measurement.ac_online, power,
        measurement.power_approximate, measurement.power_method,
        measurement.power_confidence, measurement.energy_wh, measurement.boot_id,
        round(monotonic * 1_000_000_000), round(boottime * 1_000_000_000),
        _battery_set(measurement), profile, counters_from_raw(measurement.raw_batteries),
        power_rejected,
    )


def _state_from_checkpoint(snapshot: GenerationSnapshot) -> _PollState:
    points = decode_recent_series(snapshot.recent_series)
    if not points:
        raise ValueError("checkpoint has no last recent-series point")
    point = points[-1]
    states = {item.state for item in snapshot.batteries if item.present}
    state = states.pop() if len(states) == 1 else "other"
    raw = tuple(RawBatterySnapshot(
        snapshot.last_poll_at_ms // 1_000, snapshot.monotonic_ns / 1_000_000_000,
        snapshot.boottime_ns / 1_000_000_000, snapshot.boot_id,
        item.identity, item.identity, item.soc_percent, item.state, snapshot.ac_online,
        voltage_now_v=item.voltage_now_v, energy_now_wh=item.energy_now_wh,
        charge_now_ah=item.charge_now_ah,
    ) for item in snapshot.batteries if item.present)
    ac = snapshot.ac_online
    session_kind = "discharging" if ac is False else (
        "charging" if ac is True and (state == "charging" or point.soc_millipercent < 100_000)
        else state if state in {"charging", "discharging"} else None
    )
    return _PollState(
        snapshot.last_poll_at_ms, point.soc_millipercent / 1_000, state, session_kind,
        ac, None if point.resolved_power_mw is None else point.resolved_power_mw / 1_000,
        bool(point.flags & 0x20), "unavailable", "none", None, snapshot.boot_id,
        snapshot.monotonic_ns, snapshot.boottime_ns,
        point.battery_set_key, snapshot.power_profile, counters_from_raw(raw), False,
    )


def _sleep_ranges(intervals: Iterable[SleepInterval]) -> list[tuple[int, int]]:
    return sorted((item.started_at * 1_000, item.ended_at * 1_000) for item in intervals
                  if item.ended_at > item.started_at)


def _recovery_sleeps(recovery: RecoverySelection, current: _PollState,
                     supplied: tuple[SleepInterval, ...]) -> tuple[SleepInterval, ...]:
    """Add the clock-proven resume gap unless supplied journal data covers it."""
    if recovery.snapshot is None:
        return supplied
    previous = _state_from_checkpoint(recovery.snapshot)
    if previous.boot_id != current.boot_id:
        return tuple(
            SleepInterval(
                item.started_at, item.ended_at, item.kind, item.source, item.boot_id,
                previous.soc if item.pre_percentage is None else item.pre_percentage,
                current.soc if item.post_percentage is None else item.post_percentage,
            ) if item.started_at < current.timestamp_ms // 1_000
            and item.ended_at > previous.timestamp_ms // 1_000 else item
            for item in supplied
        )
    suspended_ns = ((current.boottime_ns - previous.boottime_ns)
                    - (current.monotonic_ns - previous.monotonic_ns))
    if suspended_ns <= WALLCLOCK_TOLERANCE_MS * 1_000_000:
        return supplied
    ended_at = current.timestamp_ms // 1_000
    started_at = round(ended_at - suspended_ns / 1_000_000_000)
    overlapping = tuple(item for item in supplied
                        if item.started_at < ended_at and item.ended_at > started_at)
    if overlapping:
        enriched = []
        for item in supplied:
            if item in overlapping:
                item = SleepInterval(
                    item.started_at, item.ended_at, item.kind, item.source, item.boot_id,
                    previous.soc if item.pre_percentage is None else item.pre_percentage,
                    current.soc if item.post_percentage is None else item.post_percentage,
                )
            enriched.append(item)
        return tuple(enriched)
    return supplied + (SleepInterval(
        started_at, ended_at, source="clocks", boot_id=current.boot_id,
        pre_percentage=previous.soc, post_percentage=current.soc,
    ),)


def _overlap_ms(ranges: list[tuple[int, int]], start: int, end: int) -> int:
    pieces = sorted((max(start, left), min(end, right)) for left, right in ranges
                    if left < end and right > start)
    merged: list[list[int]] = []
    for left, right in pieces:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return sum(right - left for left, right in merged)


def _parts(start: int, end: int, sleeps: list[tuple[int, int]]) -> list[tuple[int, int, bool]]:
    boundaries = {start, end}
    for left, right in sleeps:
        if left < end and right > start:
            boundaries.update((max(start, left), min(end, right)))
    ordered = sorted(boundaries)
    return [(left, right, any(a < right and b > left for a, b in sleeps))
            for left, right in zip(ordered, ordered[1:])]


def _continuity(previous: _PollState, current: _PollState,
                poll_interval_ms: int, sleeps: list[tuple[int, int]]) -> tuple[bool, str]:
    wall = current.timestamp_ms - previous.timestamp_ms
    if wall <= 0:
        return False, "non-increasing-wallclock"
    if previous.boot_id != current.boot_id:
        return False, "reboot"
    if previous.battery_set_key != current.battery_set_key:
        return False, "battery-set-change"
    if previous.session_kind != current.session_kind:
        return False, "session-direction-change"
    boottime = (current.boottime_ns - previous.boottime_ns) / 1_000_000
    monotonic = (current.monotonic_ns - previous.monotonic_ns) / 1_000_000
    if boottime < 0 or monotonic < 0 or abs(wall - boottime) > WALLCLOCK_TOLERANCE_MS:
        return False, "wallclock-jump"
    sleep_ms = _overlap_ms(sleeps, previous.timestamp_ms, current.timestamp_ms)
    active_wall = wall - sleep_ms
    limit = max(3 * poll_interval_ms, 180_000)
    if active_wall > limit:
        return False, "unknown-gap"
    if abs(active_wall - monotonic) > WALLCLOCK_TOLERANCE_MS:
        return False, "unproven-suspend-gap"
    return True, "continuous"


def _recent_flags(state: _PollState, *, broken: bool) -> int:
    ac = 0 if state.ac_online is None else 2 if state.ac_online else 1
    method = METHODS.get(state.power_method, 0) << POWER_METHOD_SHIFT
    confidence = CONFIDENCE.get(state.power_confidence, 0) << POWER_CONFIDENCE_SHIFT
    return ac | method | confidence | (0x20 if state.approximate else 0) | (BREAK_BEFORE if broken else 0)


def _recent_state(value: str) -> BatteryState:
    return {
        "charging": BatteryState.CHARGING,
        "discharging": BatteryState.DISCHARGING,
        "full": BatteryState.FULL,
        "charged": BatteryState.FULL,
        "fully-charged": BatteryState.FULL,
    }.get(value.lower(), BatteryState.OTHER)


class V1Collector:
    def __init__(self, storage: V1Storage, configured_interval_ms: int = 60_000):
        if configured_interval_ms <= 0:
            raise ValueError("poll interval must be positive")
        self.storage = storage
        self.configured_interval_ms = configured_interval_ms

    def process_poll(self, measurement: Measurement, *, profile: str | None = None,
                     sleeps: Iterable[SleepInterval] = (), cleanup: bool = True,
                     failpoint: Callable[[str], None] | None = None) -> PollResult:
        current = _state(measurement, profile)
        supplied_sleeps = tuple(sleeps)
        warnings: list[str] = []
        finalized: list[int] = []
        state_events = health_events = 0
        generation = 0
        with self.storage.transaction() as db:
            recovery = self.storage.recover(db)
            warnings.extend(recovery.warnings)
            sleep_items = _recovery_sleeps(recovery, current, supplied_sleeps)
            sleep_ranges = _sleep_ranges(sleep_items)
            generation = self.storage.next_generation(db)
            battery_ids = self._upsert_batteries(db, measurement.raw_batteries, current.timestamp_ms)
            state_events = self._state_events(db, recovery, measurement, current, battery_ids, generation)
            health_events = self._health_events(db, measurement.raw_batteries, battery_ids,
                                                current.timestamp_ms, generation)
            self._sessions(db, recovery, current, generation)
            self._insert_sleeps_and_revise(db, sleep_items, generation, current.timestamp_ms)

            accumulators = self._accumulate(recovery, current, sleep_ranges, warnings)
            current_hour = utc_hour(current.timestamp_ms)
            accumulator = accumulators.setdefault(
                current_hour, HourlyAccumulator(current_hour, current.battery_set_key)
            )
            if accumulator.covered_ms == 0:
                accumulator.battery_set_key = current.battery_set_key
            elif accumulator.battery_set_key != current.battery_set_key:
                accumulator.battery_set_key = None
            accumulator.poll_count += 1
            accumulator.state_event_count += state_events
            for hour in sorted(accumulators):
                if hour < current_hour:
                    item = accumulators[hour]
                    item.finalize()
                    self._write_final_hour(db, item, generation, current.timestamp_ms)
                    finalized.append(hour)

            valid, reason = (False, "cold-start")
            previous = None
            if recovery.snapshot is not None:
                previous = _state_from_checkpoint(recovery.snapshot)
                valid, reason = _continuity(previous, current, self.configured_interval_ms,
                                            sleep_ranges)
            crosses_sleep = bool(previous and _overlap_ms(
                sleep_ranges, previous.timestamp_ms, current.timestamp_ms
            ))
            energy_result = (None if previous is None or not valid or crosses_sleep else
                             compatible_delta(
                                 previous.energy_counters, current.energy_counters,
                                 elapsed_ms=current.timestamp_ms - previous.timestamp_ms,
                                 direction=previous.session_kind,
                             ))
            delta = None if energy_result is None else energy_result.value_wh
            points = list(decode_recent_series(
                recovery.snapshot.recent_series if recovery.snapshot else encode_recent_series(())
            ))
            cutoff = current.timestamp_ms - MAX_WINDOW_MS
            points = [point for point in points if point.timestamp_ms >= cutoff
                      and point.timestamp_ms < current.timestamp_ms]
            points.append(RecentPoint(
                current.timestamp_ms, round(current.soc * 1_000),
                None if current.power_w is None else round(current.power_w * 1_000),
                delta, _recent_state(current.state), current.profile,
                current.battery_set_key,
                _recent_flags(current, broken=not valid or crosses_sleep),
            ))
            series = encode_recent_series(points)
            checkpoint_batteries = self._checkpoint_batteries(
                measurement, current, battery_ids
            )
            snapshot = GenerationSnapshot(
                generation, current.timestamp_ms, current.timestamp_ms, current.boot_id,
                current.monotonic_ns, current.boottime_ns, self.configured_interval_ms,
                current.ac_online, current.profile, checkpoint_batteries,
                accumulator, series,
            )
            self.storage.write_generation(db, snapshot)
            if failpoint:
                failpoint("before-commit")
        if failpoint:
            failpoint("after-generation-commit")
        if cleanup:
            self.storage.cleanup_generations()
        return PollResult(generation, tuple(warnings), tuple(finalized), state_events, health_events)

    def _upsert_batteries(self, db: sqlite3.Connection,
                          batteries: tuple[RawBatterySnapshot, ...], at: int) -> dict[str, int]:
        for item in batteries:
            db.execute(
                "INSERT INTO batteries(identity,native_name,first_seen_ms,last_seen_ms) "
                "VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "native_name=excluded.native_name "
                "WHERE batteries.native_name IS NULL AND excluded.native_name IS NOT NULL",
                (item.identity, item.device, at, at),
            )
        return {str(row[0]): int(row[1]) for row in db.execute(
            "SELECT identity,id FROM batteries"
        )}

    def _state_events(self, db: sqlite3.Connection, recovery: RecoverySelection,
                      measurement: Measurement, current: _PollState,
                      battery_ids: dict[str, int], generation: int) -> int:
        count = 0
        last_system = db.execute(
            "SELECT ac_online,power_profile FROM state_events WHERE scope='system' "
            "ORDER BY occurred_at_ms DESC,id DESC LIMIT 1"
        ).fetchone()
        desired_system = (None if current.ac_online is None else int(current.ac_online), current.profile)
        if last_system is None or tuple(last_system) != desired_system:
            _insert(db, "state_events", {
                "occurred_at_ms": current.timestamp_ms, "boot_id": current.boot_id,
                "scope": "system", "ac_online": desired_system[0],
                "power_profile": current.profile, "reason_mask": 8,
                "source_generation": generation,
            })
            count += 1
        current_identities = {item.identity for item in measurement.raw_batteries}
        previous_identities = ({item.identity for item in recovery.snapshot.batteries if item.present}
                               if recovery.snapshot else set())
        for identity in sorted(previous_identities - current_identities):
            _insert(db, "state_events", {
                "occurred_at_ms": current.timestamp_ms, "boot_id": current.boot_id,
                "scope": "battery", "battery_id": battery_ids[identity],
                "battery_present": 0, "reason_mask": 1, "source_generation": generation,
            })
            count += 1
            db.execute("UPDATE batteries SET last_seen_ms=? WHERE id=?",
                       (current.timestamp_ms, battery_ids[identity]))
        for item in measurement.raw_batteries:
            battery_id = battery_ids[item.identity]
            last = db.execute(
                "SELECT battery_present,battery_state,soc_percent FROM state_events "
                "WHERE scope='battery' AND battery_id=? ORDER BY occurred_at_ms DESC,id DESC LIMIT 1",
                (battery_id,),
            ).fetchone()
            desired = (1, item.state)
            previous_state = None if last is None else (last[0], last[1])
            if previous_state != desired:
                reason = (1 if last is None or not last[0] else 0)
                if last is None or last[1] != item.state:
                    reason |= 2
                _insert(db, "state_events", {
                    "occurred_at_ms": current.timestamp_ms, "boot_id": current.boot_id,
                    "scope": "battery", "battery_id": battery_id,
                    "battery_present": 1, "battery_state": item.state,
                    "soc_percent": item.percentage, "reason_mask": reason,
                    "source_generation": generation,
                })
                count += 1
                db.execute("UPDATE batteries SET last_seen_ms=? WHERE id=?",
                           (current.timestamp_ms, battery_id))
        return count

    def _health_events(self, db: sqlite3.Connection,
                       batteries: tuple[RawBatterySnapshot, ...], ids: dict[str, int],
                       at: int, generation: int) -> int:
        count = 0
        for item in batteries:
            values = (
                item.charge_full_ah
                if item.charge_full_ah and item.charge_full_ah > 0 else None,
                item.charge_full_design_ah
                if item.charge_full_design_ah and item.charge_full_design_ah > 0 else None,
                item.energy_full_wh if item.energy_full_wh and item.energy_full_wh > 0 else None,
                item.energy_full_design_wh if item.energy_full_design_wh and item.energy_full_design_wh > 0 else None,
                item.cycle_count if item.cycle_count is not None and item.cycle_count >= 0 else None,
                item.voltage_design_v if item.voltage_design_v and item.voltage_design_v > 0 else None,
            )
            if not any(value is not None for value in values):
                continue
            last = db.execute(
                "SELECT charge_full_ah,charge_full_design_ah,energy_full_wh,"
                "energy_full_design_wh,cycle_count,voltage_design_v FROM battery_health "
                "WHERE battery_id=? ORDER BY observed_at_ms DESC,id DESC LIMIT 1",
                (ids[item.identity],),
            ).fetchone()
            if last is not None and tuple(last) == values:
                continue
            source = ("sysfs-energy" if values[2] is not None or values[3] is not None
                      else "sysfs-charge")
            _insert(db, "battery_health", {
                "battery_id": ids[item.identity], "observed_at_ms": at,
                "charge_full_ah": values[0], "charge_full_design_ah": values[1],
                "energy_full_wh": values[2], "energy_full_design_wh": values[3],
                "cycle_count": values[4], "voltage_design_v": values[5],
                "source": source, "provenance": ",".join(item.sources) or "runtime",
                "source_generation": generation,
            })
            count += 1
        return count

    def _sessions(self, db: sqlite3.Connection, recovery: RecoverySelection,
                  current: _PollState, generation: int) -> None:
        active = db.execute("SELECT * FROM sessions WHERE ended_at_ms IS NULL").fetchone()
        kind = current.session_kind
        changed_set = bool(recovery.snapshot and
                           _state_from_checkpoint(recovery.snapshot).battery_set_key != current.battery_set_key)
        if active is not None and (
            active["kind"] != kind
            or active["battery_set_key"] != current.battery_set_key
        ):
            reason = "battery-change" if changed_set else (kind or current.state)
            db.execute(
                "UPDATE sessions SET ended_at_ms=?,end_soc=?,end_reason=? WHERE id=?",
                (current.timestamp_ms, current.soc, reason, active["id"]),
            )
            active = None
        if kind is not None and active is None:
            _insert(db, "sessions", {
                "kind": kind, "started_at_ms": current.timestamp_ms,
                "start_soc": current.soc, "battery_set_key": current.battery_set_key,
                "source_generation": generation,
            })

    def _insert_sleeps_and_revise(self, db: sqlite3.Connection,
                                  sleeps: tuple[SleepInterval, ...], generation: int,
                                  detected_at: int) -> None:
        for item in sleeps:
            start = item.started_at * 1_000
            end = item.ended_at * 1_000
            overlap = db.execute(
                "SELECT * FROM sleep_intervals WHERE started_at_ms<=? AND ended_at_ms>=? "
                "AND (boot_id=? OR boot_id IS NULL OR ? IS NULL) ORDER BY id LIMIT 1",
                (end + 10_000, start - 10_000, item.boot_id, item.boot_id),
            ).fetchone()
            affected_start, affected_end = start, end
            changed = False
            if overlap is None:
                _insert(db, "sleep_intervals", {
                    "started_at_ms": start, "ended_at_ms": end, "kind": item.kind,
                    "source": item.source, "boot_id": item.boot_id,
                    "detected_at_ms": detected_at, "pre_soc": item.pre_percentage,
                    "post_soc": item.post_percentage, "source_generation": generation,
                    "revision": 1,
                })
                changed = True
            else:
                priority = {"clocks": 1, "journal": 2, "logind": 3}
                replace_bounds = (priority.get(item.source, 0)
                                  > priority.get(str(overlap["source"]), 0))
                new_start = start if replace_bounds else int(overlap["started_at_ms"])
                new_end = end if replace_bounds else int(overlap["ended_at_ms"])
                new_source = item.source if replace_bounds else str(overlap["source"])
                new_pre = (item.pre_percentage if item.pre_percentage is not None
                           else overlap["pre_soc"])
                new_post = (item.post_percentage if item.post_percentage is not None
                            else overlap["post_soc"])
                desired = (new_start, new_end, item.kind, new_source,
                           overlap["boot_id"] or item.boot_id, new_pre, new_post)
                current = tuple(overlap[key] for key in (
                    "started_at_ms", "ended_at_ms", "kind", "source", "boot_id",
                    "pre_soc", "post_soc",
                ))
                if desired != current:
                    db.execute(
                        "UPDATE sleep_intervals SET started_at_ms=?,ended_at_ms=?,kind=?,"
                        "source=?,boot_id=?,detected_at_ms=?,pre_soc=?,post_soc=?,"
                        "source_generation=?,revision=revision+1 WHERE id=?",
                        (*desired[:5], detected_at, *desired[5:], generation, overlap["id"]),
                    )
                    changed = True
                affected_start = min(start, int(overlap["started_at_ms"]))
                affected_end = max(end, int(overlap["ended_at_ms"]))
            if not changed:
                continue
            first = utc_hour(affected_start)
            last = utc_hour(max(affected_start, affected_end - 1))
            for hour in range(first, last + 1, HOUR_MS):
                row = db.execute("SELECT * FROM hourly_history WHERE hour_start_ms=?", (hour,)).fetchone()
                if row is None:
                    continue
                desired = self._stored_sleep_overlap(db, hour)
                delta = desired - int(row["sleep_ms"])
                if delta > 0:
                    moved = min(delta, int(row["unknown_ms"]))
                else:
                    moved = -min(-delta, int(row["sleep_ms"]))
                if not moved:
                    continue
                new_sleep = int(row["sleep_ms"]) + moved
                new_unknown = int(row["unknown_ms"]) - moved
                quality = int(row["quality_flags"])
                quality = quality | 2 if new_sleep else quality & ~2
                quality = quality | 1 if new_unknown else quality & ~1
                db.execute(
                    "UPDATE hourly_history SET sleep_ms=?,unknown_ms=?,revision=revision+1,"
                    "source_generation=?,quality_flags=? WHERE hour_start_ms=?",
                    (new_sleep, new_unknown, generation, quality, hour),
                )

    @staticmethod
    def _stored_sleep_overlap(db: sqlite3.Connection, hour: int) -> int:
        ranges = [(int(row[0]), int(row[1])) for row in db.execute(
            "SELECT started_at_ms,ended_at_ms FROM sleep_intervals "
            "WHERE started_at_ms<? AND ended_at_ms>?", (hour + HOUR_MS, hour)
        )]
        return _overlap_ms(ranges, hour, hour + HOUR_MS)

    def _accumulate(self, recovery: RecoverySelection, current: _PollState,
                    sleeps: list[tuple[int, int]], warnings: list[str]) -> dict[int, HourlyAccumulator]:
        if recovery.snapshot is None:
            return {utc_hour(current.timestamp_ms): HourlyAccumulator(
                utc_hour(current.timestamp_ms), current.battery_set_key
            )}
        previous = _state_from_checkpoint(recovery.snapshot)
        if current.timestamp_ms <= previous.timestamp_ms:
            raise ValueError("new poll timestamp must be later than recovered poll")
        accumulators: dict[int, HourlyAccumulator] = {}
        if recovery.snapshot.hourly is not None:
            accumulators[recovery.snapshot.hourly.hour_start_ms] = recovery.snapshot.hourly.clone()
        valid, reason = _continuity(previous, current, self.configured_interval_ms, sleeps)
        if not valid:
            warnings.append(f"continuity boundary classified unknown: {reason}")
        crosses_sleep = _overlap_ms(sleeps, previous.timestamp_ms, current.timestamp_ms) > 0
        energy_result = (None if not valid or crosses_sleep else compatible_delta(
            previous.energy_counters, current.energy_counters,
            elapsed_ms=current.timestamp_ms - previous.timestamp_ms,
            direction=previous.session_kind,
        ))
        energy = None if energy_result is None else energy_result.value_wh
        total = current.timestamp_ms - previous.timestamp_ms
        for start, end, sleeping in _parts(previous.timestamp_ms, current.timestamp_ms, sleeps):
            cursor = start
            while cursor < end:
                hour = utc_hour(cursor)
                boundary = min(end, hour + HOUR_MS)
                item = accumulators.setdefault(hour, HourlyAccumulator(hour, previous.battery_set_key))
                if previous.power_rejected:
                    item.quality_flags |= QUALITY_POWER_REJECTED
                if energy_result is not None and energy_result.rejected:
                    item.quality_flags |= QUALITY_ENERGY_REJECTED
                duration = boundary - cursor
                if sleeping:
                    item.add_sleep(duration)
                elif not valid:
                    item.add_unknown(duration)
                else:
                    start_soc = previous.soc + (current.soc - previous.soc) * (
                        (cursor - previous.timestamp_ms) / total
                    )
                    end_soc = previous.soc + (current.soc - previous.soc) * (
                        (boundary - previous.timestamp_ms) / total
                    )
                    delta = None if energy is None else energy * duration / total
                    item.add_observed(
                        duration, start_soc, end_soc, previous.state, previous.ac_online,
                        previous.power_w, previous.approximate, previous.profile, delta,
                        0 if energy_result is None else energy_result.provenance_mask,
                    )
                cursor = boundary
        return accumulators

    @staticmethod
    def _write_final_hour(db: sqlite3.Connection, item: HourlyAccumulator,
                          generation: int, at: int) -> None:
        values = item.finalized_values(generation, at)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        cursor = db.execute(
            f"INSERT OR IGNORE INTO hourly_history({columns}) VALUES({placeholders})",
            tuple(values.values()),
        )
        if cursor.rowcount == 0:
            existing = db.execute("SELECT * FROM hourly_history WHERE hour_start_ms=?",
                                  (item.hour_start_ms,)).fetchone()
            if existing is None or not existing["is_final"]:
                raise ValueError(f"invalid existing hourly row for {item.hour_start_ms}")
            # A prior complete transaction may have finalized this hour even if
            # its checkpoint was later found corrupt.  The immutable committed
            # hour wins; recovery must not append or rewrite it from an older
            # generation.
            return
        for profile, duration in sorted(item.profiles.items()):
            db.execute(
                "INSERT OR IGNORE INTO hourly_profile_durations(hour_start_ms,profile,duration_ms) "
                "VALUES(?,?,?)", (item.hour_start_ms, profile, duration),
            )

    @staticmethod
    def _checkpoint_batteries(measurement: Measurement, current: _PollState,
                              ids: dict[str, int]) -> tuple[BatteryCheckpoint, ...]:
        single = len(measurement.raw_batteries) == 1
        return tuple(BatteryCheckpoint(
            ids[item.identity], item.identity, True, item.state, item.percentage,
            item.power_now_w, item.current_now_a, item.voltage_now_v, item.energy_now_wh,
            item.charge_now_ah, item.upower_energy_rate_w,
            current.power_w if single else None,
            measurement.power_method if single else None,
            measurement.power_approximate if single else None,
            measurement.power_confidence if single else None,
            measurement.power_window_s if single else None,
        ) for item in sorted(measurement.raw_batteries, key=lambda item: item.identity))

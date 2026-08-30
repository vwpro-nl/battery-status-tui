"""Read-only schema-v4 history projection for the existing TUI models."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .models import Measurement, RawBatterySnapshot, Session, SleepInterval
from .recent_series import (
    AC_MASK,
    POWER_CONFIDENCE_MASK,
    POWER_CONFIDENCE_SHIFT,
    POWER_METHOD_MASK,
    POWER_METHOD_SHIFT,
    BatteryState,
    RecentPoint,
    decode_recent_series,
)
from .system_status import HealthReading, resolve_health
from .v1_hourly import HOUR_MS, HourlyAccumulator
from .v1_storage import GenerationSnapshot, V1Storage, V1StorageError


BREAK_BEFORE = 0x0100
# A finalized hour may miss a poll or two (well under the 180 s unknown-gap
# threshold) yet still hold a continuous SoC trajectory. Render its two endpoint
# samples when at least this much of the hour was observed; hours with real
# sleep or a wide unknown span drop below it and stay blank / owned by the
# sleep-interval path.
NEAR_COMPLETE_OBSERVED_MS = HOUR_MS - 5 * 60_000
METHODS = {
    0: "unavailable",
    1: "power-now",
    2: "current-voltage",
    3: "upower-energy-rate",
    4: "energy-delta",
    5: "charge-delta",
}
CONFIDENCE = {0: "none", 1: "medium", 2: "high"}
STATES = {
    BatteryState.UNKNOWN: "unknown",
    BatteryState.CHARGING: "charging",
    BatteryState.DISCHARGING: "discharging",
    BatteryState.FULL: "full",
    BatteryState.OTHER: "other",
}


class V1HistoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class V1HistorySnapshot:
    current: Measurement
    history: tuple[Measurement, ...]
    trend_history: tuple[Measurement, ...]
    session: Session | None
    sleeps: tuple[SleepInterval, ...]
    health: HealthReading | None
    power_profile: str | None
    hourly_accumulator: HourlyAccumulator | None
    hourly_profiles: dict[int, dict[str, int]]
    generation: int
    configured_interval_ms: int
    warnings: tuple[str, ...]


def _ac_state(flags: int) -> bool | None:
    value = flags & AC_MASK
    return False if value == 1 else True if value == 2 else None


def _charge_energy(energy: float | None, charge: float | None,
                   voltage: float | None) -> float | None:
    if energy is not None:
        return energy
    if charge is not None and voltage is not None:
        return charge * voltage
    return None


def _point_measurement(point: RecentPoint, snapshot: GenerationSnapshot,
                       batteries: tuple[RawBatterySnapshot, ...]) -> Measurement:
    method = METHODS.get((point.flags & POWER_METHOD_MASK) >> POWER_METHOD_SHIFT,
                         "unavailable")
    confidence = CONFIDENCE.get(
        (point.flags & POWER_CONFIDENCE_MASK) >> POWER_CONFIDENCE_SHIFT, "none"
    )
    energy_values = [_charge_energy(
        item.energy_now_wh, item.charge_now_ah, item.voltage_now_v,
    ) for item in batteries]
    energy = (sum(value for value in energy_values if value is not None)
              if energy_values and all(value is not None for value in energy_values) else None)
    full_values = [_charge_energy(
        item.energy_full_wh if item.energy_full_wh is not None
        else item.upower_energy_full_wh,
        item.charge_full_ah, item.voltage_now_v,
    ) for item in batteries]
    design_values = [_charge_energy(
        item.energy_full_design_wh if item.energy_full_design_wh is not None
        else item.upower_energy_full_design_wh,
        item.charge_full_design_ah, item.voltage_now_v,
    ) for item in batteries]
    energy_full = (sum(value for value in full_values if value is not None)
                   if full_values and all(value is not None for value in full_values) else None)
    energy_design = (sum(value for value in design_values if value is not None)
                     if design_values and all(value is not None for value in design_values)
                     else None)
    state = STATES.get(point.battery_state, "other")
    return Measurement(
        timestamp=point.timestamp_ms // 1_000,
        percentage=point.soc_millipercent / 1_000,
        state=state,
        ac_online=_ac_state(point.flags),
        power_w=(None if point.resolved_power_mw is None
                 else point.resolved_power_mw / 1_000),
        energy_wh=energy,
        energy_full_wh=energy_full,
        energy_full_design_wh=energy_design,
        source="checkpoint-recent-series",
        device="system-batteries",
        power_method=method,
        power_approximate=bool(point.flags & 0x20),
        power_confidence=confidence,
        monotonic_s=snapshot.monotonic_ns / 1_000_000_000,
        boottime_s=snapshot.boottime_ns / 1_000_000_000,
        boot_id=snapshot.boot_id,
        battery_identity=point.battery_set_key,
        raw_batteries=batteries,
    )


def _hour_state(row: sqlite3.Row) -> tuple[str, bool | None]:
    states = (
        ("charging", int(row["charging_ms"])),
        ("discharging", int(row["discharging_ms"])),
        ("full", int(row["full_ms"])),
        ("other", int(row["other_state_ms"])),
    )
    state = max(states, key=lambda item: item[1])[0]
    ac_values = ((True, int(row["ac_online_ms"])),
                 (False, int(row["ac_offline_ms"])),
                 (None, int(row["ac_unknown_ms"])))
    return state, max(ac_values, key=lambda item: item[1])[0]


class V1History:
    """Expose schema-v4 state without writing or migrating the database."""

    def __init__(self, path: Path):
        self.storage = V1Storage(path)

    def load(self, since: int, *, now: int | None = None) -> V1HistorySnapshot:
        try:
            with self.storage.reader() as db:
                recovery = self.storage.recover(db)
                snapshot = recovery.snapshot
                if snapshot is None:
                    raise V1HistoryError("schema-v4 database has no valid checkpoint")
                batteries = self._raw_batteries(db, snapshot)
                points = decode_recent_series(snapshot.recent_series)
                if not points:
                    raise V1HistoryError("valid checkpoint has no current point")
                recent = tuple(_point_measurement(point, snapshot, batteries) for point in points
                               if point.timestamp_ms >= since * 1_000)
                current = _point_measurement(points[-1], snapshot, batteries)
                effective_now = current.timestamp if now is None else now
                history = self._history(db, since, effective_now, recent)
                session = self._current_session(db)
                sleeps = self._sleeps(db, since)
                health = self._health(db, snapshot)
                trend = self._trend(points, snapshot, batteries, session, since)
                profiles = self._hourly_profiles(db, since, effective_now)
                return V1HistorySnapshot(
                    current, history, trend, session, sleeps, health,
                    snapshot.power_profile, snapshot.hourly, profiles, snapshot.generation,
                    snapshot.configured_interval_ms,
                    recovery.warnings,
                )
        except (sqlite3.Error, V1StorageError) as error:
            raise V1HistoryError(str(error)) from error

    def all_sessions(self) -> tuple[Session, ...]:
        try:
            with self.storage.reader() as db:
                return self._sessions(db)
        except (sqlite3.Error, V1StorageError) as error:
            raise V1HistoryError(str(error)) from error

    def current_session(self) -> Session | None:
        """Return the open session without requiring a valid checkpoint."""
        try:
            with self.storage.reader() as db:
                return self._current_session(db)
        except (sqlite3.Error, V1StorageError) as error:
            raise V1HistoryError(str(error)) from error

    @staticmethod
    def _raw_batteries(db: sqlite3.Connection, snapshot: GenerationSnapshot
                       ) -> tuple[RawBatterySnapshot, ...]:
        result = []
        timestamp = snapshot.last_poll_at_ms // 1_000
        for item in snapshot.batteries:
            if not item.present:
                continue
            health = db.execute(
                "SELECT * FROM battery_health WHERE battery_id=? "
                "ORDER BY observed_at_ms DESC,id DESC LIMIT 1", (item.battery_id,),
            ).fetchone()
            source = None if health is None else str(health["source"])
            result.append(RawBatterySnapshot(
                timestamp, snapshot.monotonic_ns / 1_000_000_000,
                snapshot.boottime_ns / 1_000_000_000, snapshot.boot_id,
                item.identity, item.identity, item.soc_percent, item.state,
                snapshot.ac_online, item.power_now_w, item.current_now_a,
                item.voltage_now_v, item.energy_now_wh,
                energy_full_wh=(health["energy_full_wh"] if health is not None
                                and source != "upower-energy" else None),
                energy_full_design_wh=(health["energy_full_design_wh"] if health is not None
                                       and source != "upower-energy" else None),
                charge_now_ah=item.charge_now_ah,
                charge_full_ah=None if health is None else health["charge_full_ah"],
                charge_full_design_ah=(None if health is None
                                       else health["charge_full_design_ah"]),
                upower_energy_rate_w=item.upower_energy_rate_w,
                cycle_count=None if health is None else health["cycle_count"],
                sources=(("sysfs",) if source and source.startswith("sysfs")
                         else ("upower",) if source and source.startswith("upower") else ()),
                voltage_design_v=None if health is None else health["voltage_design_v"],
                upower_energy_full_wh=(health["energy_full_wh"] if health is not None
                                       and source == "upower-energy" else None),
                upower_energy_full_design_wh=(
                    health["energy_full_design_wh"] if health is not None
                    and source == "upower-energy" else None
                ),
            ))
        return tuple(result)

    @staticmethod
    def _history(db: sqlite3.Connection, since: int, now: int,
                 recent: tuple[Measurement, ...]) -> tuple[Measurement, ...]:
        recent_hours = {sample.timestamp * 1_000 // HOUR_MS * HOUR_MS for sample in recent}
        rows = db.execute(
            "SELECT * FROM hourly_history WHERE hour_start_ms<? AND hour_start_ms+3600000>? "
            "ORDER BY hour_start_ms", (now * 1_000, since * 1_000),
        ).fetchall()
        hourly = []
        for row in rows:
            hour = int(row["hour_start_ms"])
            if hour in recent_hours or int(row["observed_ms"]) < NEAR_COMPLETE_OBSERVED_MS:
                continue
            if row["soc_start"] is None or row["soc_end"] is None:
                continue
            state, ac_online = _hour_state(row)
            for timestamp, percentage in (
                (hour // 1_000, float(row["soc_start"])),
                ((hour + HOUR_MS - 1) // 1_000, float(row["soc_end"])),
            ):
                if since <= timestamp < now:
                    hourly.append(Measurement(
                        timestamp, percentage, state, ac_online,
                        source="hourly-history", device="system-batteries",
                        battery_identity=row["battery_set_key"],
                    ))
        combined = hourly + [sample for sample in recent if since <= sample.timestamp < now]
        return tuple(sorted(combined, key=lambda sample: sample.timestamp))

    @staticmethod
    def _current_session(db: sqlite3.Connection) -> Session | None:
        row = db.execute(
            "SELECT * FROM sessions WHERE ended_at_ms IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else Session(
            int(row["id"]), str(row["kind"]), int(row["started_at_ms"]) // 1_000,
            None, float(row["start_soc"]), None,
        )

    @staticmethod
    def _sessions(db: sqlite3.Connection) -> tuple[Session, ...]:
        return tuple(Session(
            int(row["id"]), str(row["kind"]), int(row["started_at_ms"]) // 1_000,
            None if row["ended_at_ms"] is None else int(row["ended_at_ms"]) // 1_000,
            float(row["start_soc"]),
            None if row["end_soc"] is None else float(row["end_soc"]),
        ) for row in db.execute("SELECT * FROM sessions ORDER BY id"))

    @staticmethod
    def _sleeps(db: sqlite3.Connection, since: int) -> tuple[SleepInterval, ...]:
        return tuple(SleepInterval(
            int(row["started_at_ms"]) // 1_000, int(row["ended_at_ms"]) // 1_000,
            str(row["kind"]), str(row["source"]), row["boot_id"],
            row["pre_soc"], row["post_soc"],
        ) for row in db.execute(
            "SELECT * FROM sleep_intervals WHERE ended_at_ms>=? ORDER BY started_at_ms",
            (since * 1_000,),
        ))

    @staticmethod
    def _health(db: sqlite3.Connection,
                snapshot: GenerationSnapshot) -> HealthReading | None:
        batteries = V1History._raw_batteries(db, snapshot)
        reading = resolve_health(batteries)
        if reading is not None:
            return reading
        # Imported pre-v1 health retains explicit native values but deliberately
        # carries legacy provenance rather than pretending to be live sysfs.
        rows = []
        for item in snapshot.batteries:
            if item.present:
                row = db.execute(
                    "SELECT * FROM battery_health WHERE battery_id=? "
                    "ORDER BY observed_at_ms DESC,id DESC LIMIT 1", (item.battery_id,),
                ).fetchone()
                if row is None:
                    return None
                rows.append(row)
        if rows and all(row["energy_full_wh"] and row["energy_full_design_wh"]
                        for row in rows):
            full = sum(float(row["energy_full_wh"]) for row in rows)
            design = sum(float(row["energy_full_design_wh"]) for row in rows)
            return HealthReading(full / design * 100, "health-events-energy",
                                 full, design, "Wh")
        if len(rows) == 1 and rows[0]["charge_full_ah"] and rows[0]["charge_full_design_ah"]:
            full = float(rows[0]["charge_full_ah"])
            design = float(rows[0]["charge_full_design_ah"])
            return HealthReading(full / design * 100, "health-events-charge",
                                 full, design, "Ah")
        if rows and all(row["charge_full_ah"] and row["charge_full_design_ah"]
                        and row["voltage_design_v"] for row in rows):
            full = sum(float(row["charge_full_ah"]) * float(row["voltage_design_v"])
                       for row in rows)
            design = sum(float(row["charge_full_design_ah"])
                         * float(row["voltage_design_v"]) for row in rows)
            return HealthReading(full / design * 100, "health-events-charge-voltage",
                                 full, design, "Wh")
        return None

    @staticmethod
    def _hourly_profiles(db: sqlite3.Connection, since: int,
                         now: int) -> dict[int, dict[str, int]]:
        result: dict[int, dict[str, int]] = {}
        for row in db.execute(
            "SELECT p.hour_start_ms,p.profile,p.duration_ms "
            "FROM hourly_profile_durations p JOIN hourly_history h "
            "ON h.hour_start_ms=p.hour_start_ms "
            "WHERE p.hour_start_ms<? AND p.hour_start_ms+3600000>? "
            "ORDER BY p.hour_start_ms,p.profile", (now * 1_000, since * 1_000),
        ):
            result.setdefault(int(row[0]), {})[str(row[1])] = int(row[2])
        return result

    @staticmethod
    def _trend(points: tuple[RecentPoint, ...], snapshot: GenerationSnapshot,
               batteries: tuple[RawBatterySnapshot, ...], session: Session | None,
               since: int) -> tuple[Measurement, ...]:
        selected = [point for point in points if point.timestamp_ms >= since * 1_000]
        last_break = max((index for index, point in enumerate(selected)
                          if point.flags & BREAK_BEFORE), default=0)
        selected = selected[last_break:]
        measurements = tuple(_point_measurement(point, snapshot, batteries)
                             for point in selected)
        if session is None:
            return ()
        return tuple(sample for sample in measurements
                     if sample.timestamp >= session.started_at
                     and sample.session_kind == session.kind)

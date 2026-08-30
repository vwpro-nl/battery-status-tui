"""Command-line interface for collection, diagnostics, and the compact TUI."""

from __future__ import annotations

import argparse
import datetime as dt
import signal
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from . import __version__
from .estimate import estimate_remaining, smooth_seconds
from .graph import COLUMN_SECONDS, CSI, MAX_SPAN_SECONDS, RESET, render_dashboard
from .models import Estimate, Measurement, Session
from .models import SleepInterval
from .schema import V1_SCHEMA_VERSION
from .sources import BatterySource, SourceUnavailable, aggregate
from .storage import Storage, default_database_path
from .suspend import LogindMonitor, clock_sleep, journal_intervals
from .system_status import HealthResolver, PowerProfileResolver
from .v1_collector import V1CollectorError
from .v1_history import V1History, V1HistoryError
from .v1_runtime import collect_v1, read_v1_view, render_v1_view
from .v1_storage import V1Storage, V1StorageError


UNICODE_PROBE = """SOLID  : █ ▇ ▆ ▅ ▄ ▃ ▂ ▁
BRAILLE: ⠀ ⠁ ⠂ ⠄ ⡀ ⢀ ⠒ ⠤ ⠦ ⠴
JOIN   : ███▇▆▅│⠴⠦⠤⠒⠂⠁
HEIGHT : ⠀ ⡀ ⣀ ⣄ ⣤ ⣦ ⣶ ⣿
PROFILE: 🥵 😎 😴
AXIS   : ┬─────┬─────┬─────┬─────┬"""

def next_refresh_delay(interval: float, wall_now: float) -> float:
    """Wake no later than the next wall-clock projection boundary."""
    next_projection = (int(wall_now) // COLUMN_SECONDS + 1) * COLUMN_SECONDS
    return min(max(1.0, interval), max(0.05, next_projection - wall_now + 0.05))

def reconcile_journal(storage: Storage, now: int, force: bool = False) -> None:
    checked = storage.metadata_int("journal-checked-at")
    if not force and checked is not None and now - checked < 3600:
        return
    since = max(now - 7 * 3600, (checked or 0) - 60)
    for interval in journal_intervals(since):
        storage.record_sleep(interval)
    storage.set_metadata_int("journal-checked-at", now)


def collect(source: BatterySource, storage: Storage, now: int | None = None) -> Measurement:
    timestamp = int(time.time()) if now is None else now
    raw = source.read_raw(timestamp)
    history = storage.raw_samples_since(timestamp - 600)
    resumed = False
    for current in raw:
        previous = storage.latest_raw_before(timestamp, current.identity)
        if previous and (interval := clock_sleep(previous, current)) is not None:
            storage.record_sleep(interval)
            resumed = True
    if resumed:
        reconcile_journal(storage, timestamp, force=True)
    sleeps = storage.sleep_intervals_since(timestamp - 6 * 3600)
    measurement = aggregate(raw, source.resolver, history,
                            tuple((item.started_at, item.ended_at) for item in sleeps))
    storage.record(measurement)
    storage.prune(timestamp - 30 * 86400)
    return measurement


def current_estimate(storage: Storage, current: Measurement, now: int) -> Estimate | None:
    session = storage.current_session()
    if session is None:
        return None
    samples = storage.samples_since(max(session.started_at, now - 3600), session.id)
    sleeps = storage.sleep_intervals_since(max(session.started_at, now - 3600))
    if sleeps:
        samples = [sample for sample in samples if sample.timestamp >= sleeps[-1].ended_at]
    estimate = estimate_remaining(current, samples, now)
    if estimate is None:
        return None
    key = f"eta-seconds:{session.id}"
    seconds = smooth_seconds(storage.metadata_int(key), estimate.seconds)
    storage.set_metadata_int(key, seconds)
    return Estimate(seconds, estimate.source, estimate.slope_percent_per_hour)


def render_once(source: BatterySource, storage: Storage, now: int | None = None,
                health_resolver: HealthResolver | None = None,
                profile_resolver: PowerProfileResolver | None = None) -> str:
    sample_timestamp = int(time.time()) if now is None else now
    current = collect(source, storage, sample_timestamp)
    render_timestamp = int(time.time()) if now is None else now
    session = storage.current_session()
    history = storage.samples_since(render_timestamp - MAX_SPAN_SECONDS)
    estimate = current_estimate(storage, current, sample_timestamp)
    sleeps = storage.sleep_intervals_since(render_timestamp - MAX_SPAN_SECONDS)
    health = health_resolver.resolve(current.raw_batteries) if health_resolver else None
    profile = profile_resolver.resolve() if profile_resolver else None
    return render_dashboard(current, history, session, estimate, render_timestamp, sleeps,
                            health.percent if health else None, profile.profile if profile else None)


def diagnostic_text(measurement: Measurement, *, session: Session | None,
                    database_path: Path) -> str:
    health = measurement.health_percent
    remaining = measurement.remaining_seconds

    def value(number: float | int | None, suffix: str = "") -> str:
        return "unavailable" if number is None else f"{number}{suffix}"

    lines = [
            f"battery-status-tui {__version__}",
            f"source: {measurement.source}",
            f"device: {measurement.device}",
            f"timestamp: {measurement.timestamp}",
            f"state: {measurement.state}",
            f"AC online: {measurement.ac_online}",
            f"percentage: {measurement.percentage:.1f}%",
            f"power: {value(None if measurement.power_w is None else round(measurement.power_w, 3), ' W')}",
            f"power method: {measurement.power_method}",
            f"power approximate: {measurement.power_approximate}",
            f"power confidence: {measurement.power_confidence}",
            f"power window: {value(None if measurement.power_window_s is None else round(measurement.power_window_s), ' s')}",
            f"voltage: {value(None if measurement.voltage_v is None else round(measurement.voltage_v, 3), ' V')}",
            f"current: {value(None if measurement.current_a is None else round(measurement.current_a, 3), ' A')}",
            f"energy now: {value(measurement.energy_wh, ' Wh')}",
            f"full capacity: {value(measurement.energy_full_wh, ' Wh')}",
            f"design capacity: {value(measurement.energy_full_design_wh, ' Wh')}",
            f"battery health: {value(None if health is None else round(health, 1), '%')}",
            f"cycle count: {value(measurement.cycle_count)}",
            f"source remaining time: {value(remaining, ' s')}",
            f"active session: {session.kind if session else 'none'}",
            f"battery identity: {measurement.battery_identity or 'unavailable'}",
            f"system batteries: {len(measurement.raw_batteries)}",
            f"database: {database_path}",
    ]
    for item in measurement.raw_batteries:
        lines.extend((
            f"[{item.device}] identity: {item.identity}",
            f"[{item.device}] sources: {','.join(item.sources)}",
            f"[{item.device}] power_now: {value(item.power_now_w, ' W')}",
            f"[{item.device}] current_now: {value(item.current_now_a, ' A')}",
            f"[{item.device}] voltage_now: {value(item.voltage_now_v, ' V')}",
            f"[{item.device}] energy_now: {value(item.energy_now_wh, ' Wh')}",
            f"[{item.device}] charge_now: {value(item.charge_now_ah, ' Ah')}",
            f"[{item.device}] UPower EnergyRate: {value(item.upower_energy_rate_w, ' W')}",
        ))
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Compact battery history and forecast TUI")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="render the stored dashboard once")
    mode.add_argument("--sample", action="store_true", help="record one sample without rendering")
    mode.add_argument("--diagnose", action="store_true", help="show source and battery-health details")
    mode.add_argument("--unicode-probe", action="store_true", help="show solid and Braille glyph candidates")
    result.add_argument("--interval", type=float, default=60, help="interactive refresh interval")
    result.add_argument("--database", type=Path, default=default_database_path(), help="SQLite history path")
    result.add_argument("--version", action="version", version=__version__)
    return result


def _on_disk_schema_version(path: Path) -> int | None:
    """Return the persisted ``user_version``; ``None`` for an absent/empty file."""
    try:
        if path.stat().st_size == 0:
            return None
    except FileNotFoundError:
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError:
        return None


def _power_line(measurement: Measurement) -> str:
    if measurement.power_w is None:
        return "--W"
    return f"{'~' if measurement.power_approximate else ''}{measurement.power_w:.2f}W"


V4_RUNTIME_ERRORS = (SourceUnavailable, V1CollectorError, V1HistoryError, V1StorageError)


class V1ViewUnavailable(V1HistoryError):
    """The read-only viewer has no sufficiently recent checkpoint to show."""


def _render_v4_view(storage: V1Storage, now: int) -> str:
    guidance = "run battery-status-tui --sample or enable battery-status-tui.timer"
    if not storage.path.is_file():
        raise V1ViewUnavailable(f"waiting for first sample; {guidance}")
    try:
        view = read_v1_view(storage, now=now)
    except V1HistoryError as error:
        if "no valid checkpoint" in str(error) or "no current point" in str(error):
            raise V1ViewUnavailable(f"waiting for first sample; {guidance}") from error
        raise
    maximum_age_ms = max(3 * view.configured_interval_ms, 180_000)
    age_ms = now * 1_000 - view.current.timestamp * 1_000
    if age_ms > maximum_age_ms:
        timestamp = dt.datetime.fromtimestamp(view.current.timestamp).astimezone().isoformat(
            timespec="seconds"
        )
        raise V1ViewUnavailable(
            f"stale data (last sample {timestamp}); {guidance}"
        )
    return render_v1_view(view)


def _recovered_last_poll_second(storage: V1Storage) -> int:
    """Whole second of the last poll persisted in the recovered checkpoint.

    The schema-v4 collector requires every poll's second to be strictly greater
    than the recovered checkpoint's ``last_poll_at_ms``.  A freshly started
    process must not poll in the same second as the previous run's final poll,
    so the ``--sample`` poll-spacing guard is seeded from this value rather than
    from zero.  Returns 0 when there is no valid checkpoint (cold start).
    """
    snapshot = storage.recover().snapshot
    return 0 if snapshot is None else snapshot.last_poll_at_ms // 1_000


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.unicode_probe:
        print(UNICODE_PROBE)
        return 0

    version = _on_disk_schema_version(args.database)
    if version is not None and version not in (0, 1, 2, V1_SCHEMA_VERSION):
        print(f"battery-status-tui: unsupported database schema v{version} at "
              f"{args.database}; no runtime for this version", file=sys.stderr)
        return 1
    if args.diagnose:
        return _run_diagnose(args, version)
    # Only --sample creates a missing native schema-v4 database.  Viewers treat
    # it as waiting for the timer's first sample.  Legacy schema v0/v1/v2
    # databases are never migrated forward here; conversion is an explicit
    # offline step.
    if version is None or version == V1_SCHEMA_VERSION:
        return _run_v4(args)
    if args.sample:
        return _run_v2(args)
    print("battery-status-tui: read-only viewing requires schema v4; "
          "convert the legacy database using docs/migration.md", file=sys.stderr)
    return 1


def _run_diagnose(args: argparse.Namespace, version: int | None) -> int:
    source = BatterySource()
    try:
        measurement = source.read(int(time.time()))
        session = None
        if version == V1_SCHEMA_VERSION:
            try:
                session = V1History(args.database).current_session()
            except V1HistoryError:
                pass
        elif version in (0, 1, 2):
            try:
                session = Storage(args.database).current_session()
            except (sqlite3.Error, OSError):
                pass
        print(diagnostic_text(measurement, session=session, database_path=args.database))
        return 0
    except SourceUnavailable as error:
        print(f"battery-status-tui: {error}", file=sys.stderr)
        return 1


def _run_v4(args: argparse.Namespace) -> int:
    storage = V1Storage(args.database)
    if args.sample:
        source = BatterySource()
        profile_resolver = PowerProfileResolver()
        interval_ms = max(1, round(args.interval * 1_000))
        try:
            storage.initialize_writer()
            last_poll_ts = _recovered_last_poll_second(storage)
            now = int(time.time())
            if now <= last_poll_ts:
                time.sleep(max(0.0, last_poll_ts + 1 - time.time()))
                now = int(time.time())
            resolved = profile_resolver.resolve()
            measurement, _result = collect_v1(
                source, storage, timestamp=now,
                profile=None if resolved is None else resolved.profile,
                journal_lookup=journal_intervals,
                configured_interval_ms=interval_ms,
            )
            print(f"{measurement.timestamp} {measurement.percentage:.1f}% "
                  f"{measurement.state} {_power_line(measurement)}")
            return 0
        except V4_RUNTIME_ERRORS as error:
            print(f"battery-status-tui: {error}", file=sys.stderr)
            return 1

    if args.once or not sys.stdout.isatty():
        try:
            print(_render_v4_view(storage, int(time.time())))
            return 0
        except (V1HistoryError, V1StorageError, sqlite3.Error) as error:
            print(f"battery-status-tui: {error}", file=sys.stderr)
            return 1

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    sys.stdout.write(CSI + "?25l")
    try:
        while running:
            try:
                output = _render_v4_view(storage, int(time.time()))
            except (V1HistoryError, V1StorageError, sqlite3.Error) as error:
                output = f"battery-status-tui: {error}"
            sys.stdout.write(CSI + "2J" + CSI + "H" + output + "\n")
            sys.stdout.flush()
            deadline = time.monotonic() + next_refresh_delay(args.interval, time.time())
            while running and time.monotonic() < deadline:
                time.sleep(min(0.2, deadline - time.monotonic()))
    finally:
        sys.stdout.write(RESET + CSI + "?25h")
        sys.stdout.flush()
    return 0


def _run_v2(args: argparse.Namespace) -> int:
    source = BatterySource()
    storage = Storage(args.database)
    storage.initialize_writer()
    health_resolver = HealthResolver()
    profile_resolver = PowerProfileResolver()
    reconcile_journal(storage, int(time.time()))
    try:
        if args.sample:
            measurement = collect(source, storage)
            print(f"{measurement.timestamp} {measurement.percentage:.1f}% "
                  f"{measurement.state} {_power_line(measurement)}")
            return 0
        if args.diagnose:
            timestamp = int(time.time())
            measurement = source.read(timestamp, storage.raw_samples_since(timestamp - 600),
                                      tuple((item.started_at, item.ended_at) for item in storage.sleep_intervals_since(timestamp - 600)))
            print(diagnostic_text(measurement, session=storage.current_session(),
                                  database_path=storage.path))
            return 0
        if args.once or not sys.stdout.isatty():
            print(render_once(source, storage, health_resolver=health_resolver,
                              profile_resolver=profile_resolver))
            return 0
    except SourceUnavailable as error:
        print(f"battery-status-tui: {error}", file=sys.stderr)
        return 1

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    monitor = LogindMonitor()
    monitor.start()
    sleep_started: int | None = None
    sys.stdout.write(CSI + "?25l")
    try:
        while running:
            try:
                output = render_once(source, storage, health_resolver=health_resolver,
                                     profile_resolver=profile_resolver)
            except SourceUnavailable as error:
                output = f"battery-status-tui: {error}"
            sys.stdout.write(CSI + "2J" + CSI + "H" + output + "\n")
            sys.stdout.flush()
            shown_profile = profile_resolver.resolve()
            deadline = time.monotonic() + next_refresh_delay(args.interval, time.time())
            while running and time.monotonic() < deadline:
                monitor.wakeup.wait(min(0.2, deadline - time.monotonic()))
                resumed = False
                for sleeping, event_time in monitor.drain():
                    if sleeping:
                        sleep_started = event_time
                        try:
                            collect(source, storage, event_time)
                        except SourceUnavailable:
                            pass
                    elif sleep_started is not None:
                        latest = storage.latest()
                        storage.record_sleep(SleepInterval(sleep_started, event_time, source="logind",
                            pre_percentage=latest.percentage if latest else None))
                        reconcile_journal(storage, event_time, force=True)
                        sleep_started = None
                        resumed = True
                if resumed:
                    health_resolver.invalidate()
                    profile_resolver.invalidate()
                    break
                if profile_resolver.resolve() != shown_profile:
                    break
    finally:
        monitor.close()
        sys.stdout.write(RESET + CSI + "?25h")
        sys.stdout.flush()
    return 0

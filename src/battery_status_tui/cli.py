"""Command-line interface for collection, diagnostics, and the compact TUI."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .estimate import estimate_remaining, smooth_seconds
from .graph import COLUMN_SECONDS, CSI, RESET, render_dashboard
from .models import Estimate, Measurement
from .models import SleepInterval
from .sources import BatterySource, SourceUnavailable, aggregate
from .storage import Storage, default_database_path
from .suspend import LogindMonitor, clock_sleep, journal_intervals
from .system_status import HealthResolver, PowerProfileResolver


UNICODE_PROBE = """SOLID  : █ ▇ ▆ ▅ ▄ ▃ ▂ ▁
BRAILLE: ⠀ ⠁ ⠂ ⠄ ⡀ ⢀ ⠒ ⠤ ⠦ ⠴
JOIN   : ███▇▆▅│⠴⠦⠤⠒⠂⠁
HEIGHT : ⠀ ⡀ ⣀ ⣄ ⣤ ⣦ ⣶ ⣿
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
    history = storage.samples_since(render_timestamp - 6 * 3600)
    estimate = current_estimate(storage, current, sample_timestamp)
    sleeps = storage.sleep_intervals_since(render_timestamp - 6 * 3600)
    health = health_resolver.resolve(current.raw_batteries) if health_resolver else None
    profile = profile_resolver.resolve() if profile_resolver else None
    return render_dashboard(current, history, session, estimate, render_timestamp, sleeps,
                            health.percent if health else None, profile.profile if profile else None)


def diagnostic_text(measurement: Measurement, storage: Storage) -> str:
    health = measurement.health_percent
    remaining = measurement.remaining_seconds
    session = storage.current_session()

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
            f"database: {storage.path}",
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
    mode.add_argument("--once", action="store_true", help="sample and render once")
    mode.add_argument("--sample", action="store_true", help="record one sample without rendering")
    mode.add_argument("--diagnose", action="store_true", help="show source and battery-health details")
    mode.add_argument("--unicode-probe", action="store_true", help="show solid and Braille glyph candidates")
    result.add_argument("--interval", type=float, default=60, help="interactive refresh interval")
    result.add_argument("--database", type=Path, default=default_database_path(), help="SQLite history path")
    result.add_argument("--version", action="version", version=__version__)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.unicode_probe:
        print(UNICODE_PROBE)
        return 0

    source = BatterySource()
    storage = Storage(args.database)
    health_resolver = HealthResolver()
    profile_resolver = PowerProfileResolver()
    reconcile_journal(storage, int(time.time()))
    try:
        if args.sample:
            measurement = collect(source, storage)
            power = "--W" if measurement.power_w is None else f"{'~' if measurement.power_approximate else ''}{measurement.power_w:.2f}W"
            print(f"{measurement.timestamp} {measurement.percentage:.1f}% {measurement.state} {power}")
            return 0
        if args.diagnose:
            timestamp = int(time.time())
            measurement = source.read(timestamp, storage.raw_samples_since(timestamp - 600),
                                      tuple((item.started_at, item.ended_at) for item in storage.sleep_intervals_since(timestamp - 600)))
            print(diagnostic_text(measurement, storage))
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

"""Command-line interface for collection, diagnostics, and the compact TUI."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .estimate import estimate_remaining, smooth_seconds
from .graph import CSI, RESET, render_dashboard
from .models import Estimate, Measurement
from .models import SleepInterval
from .sources import BatterySource, SourceUnavailable, aggregate
from .storage import Storage, default_database_path
from .suspend import LogindMonitor, clock_sleep, journal_intervals


UNICODE_PROBE = """SOLID  : █ ▇ ▆ ▅ ▄ ▃ ▂ ▁
BRAILLE: ⠀ ⠁ ⠂ ⠄ ⡀ ⢀ ⠒ ⠤ ⠦ ⠴
JOIN   : ███▇▆▅│⠴⠦⠤⠒⠂⠁
HEIGHT : ⠀ ⡀ ⣀ ⣄ ⣤ ⣦ ⣶ ⣿
AXIS   : ┬─────┬─────┬─────┬─────┬"""


def collect(source: BatterySource, storage: Storage, now: int | None = None) -> Measurement:
    timestamp = int(time.time()) if now is None else now
    raw = source.read_raw(timestamp)
    history = storage.raw_samples_since(timestamp - 600)
    previous_by_identity = {item.identity: item for item in history}
    for current in raw:
        previous = previous_by_identity.get(current.identity)
        if previous and (interval := clock_sleep(previous, current)) is not None:
            storage.record_sleep(interval)
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
    estimate = estimate_remaining(current, samples, now)
    if estimate is None:
        return None
    key = f"eta-seconds:{session.id}"
    seconds = smooth_seconds(storage.metadata_int(key), estimate.seconds)
    storage.set_metadata_int(key, seconds)
    return Estimate(seconds, estimate.source, estimate.slope_percent_per_hour)


def render_once(source: BatterySource, storage: Storage, now: int | None = None) -> str:
    timestamp = int(time.time()) if now is None else now
    current = collect(source, storage, timestamp)
    session = storage.current_session()
    history = storage.samples_since(timestamp - 6 * 3600)
    estimate = current_estimate(storage, current, timestamp)
    sleeps = storage.sleep_intervals_since(timestamp - 6 * 3600)
    return render_dashboard(current, history, session, estimate, timestamp, sleeps)


def diagnostic_text(measurement: Measurement, storage: Storage) -> str:
    health = measurement.health_percent
    remaining = measurement.remaining_seconds
    session = storage.current_session()

    def value(number: float | int | None, suffix: str = "") -> str:
        return "unavailable" if number is None else f"{number}{suffix}"

    return "\n".join(
        (
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
        )
    )


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
            print(render_once(source, storage))
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
    for interval in journal_intervals(int(time.time()) - 7 * 3600):
        storage.record_sleep(interval)
    monitor = LogindMonitor()
    monitor.start()
    sleep_started: int | None = None
    sys.stdout.write(CSI + "?25l")
    try:
        while running:
            try:
                output = render_once(source, storage)
            except SourceUnavailable as error:
                output = f"battery-status-tui: {error}"
            sys.stdout.write(CSI + "2J" + CSI + "H" + output + "\n")
            sys.stdout.flush()
            deadline = time.monotonic() + max(1.0, args.interval)
            while running and time.monotonic() < deadline:
                monitor.wakeup.wait(min(0.2, deadline - time.monotonic()))
                resumed = False
                for sleeping, event_time in monitor.drain():
                    if sleeping:
                        sleep_started = event_time
                    elif sleep_started is not None:
                        latest = storage.latest()
                        storage.record_sleep(SleepInterval(sleep_started, event_time, source="logind",
                            pre_percentage=latest.percentage if latest else None))
                        sleep_started = None
                        resumed = True
                if resumed:
                    break
    finally:
        monitor.close()
        sys.stdout.write(RESET + CSI + "?25h")
        sys.stdout.flush()
    return 0

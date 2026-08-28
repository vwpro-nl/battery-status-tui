"""Explicit schema-v4 trial collection and rendering runtime."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from .estimate import estimate_remaining
from .graph import HISTORY_SECONDS, render_dashboard
from .models import Measurement, RawBatterySnapshot, SleepInterval
from .sources import BatterySource, aggregate
from .suspend import clock_sleep, journal_intervals
from .v1_collector import PollResult, V1Collector
from .v1_history import V1History, V1HistorySnapshot
from .v1_storage import GenerationSnapshot, V1Storage


JournalLookup = Callable[[int], Iterable[SleepInterval]]


def _snapshot_raw(snapshot: GenerationSnapshot) -> tuple[RawBatterySnapshot, ...]:
    timestamp = snapshot.last_poll_at_ms // 1_000
    return tuple(RawBatterySnapshot(
        timestamp, snapshot.monotonic_ns / 1_000_000_000,
        snapshot.boottime_ns / 1_000_000_000, snapshot.boot_id,
        item.identity, item.identity, item.soc_percent, item.state,
        snapshot.ac_online, item.power_now_w, item.current_now_a, item.voltage_now_v,
        item.energy_now_wh, charge_now_ah=item.charge_now_ah,
        upower_energy_rate_w=item.upower_energy_rate_w,
    ) for item in snapshot.batteries if item.present)


def _checkpoint_raw_history(storage: V1Storage) -> tuple[RawBatterySnapshot, ...]:
    snapshots = reversed(storage.valid_generations())
    return tuple(item for snapshot in snapshots for item in _snapshot_raw(snapshot))


def _new_sleep_intervals(previous: tuple[RawBatterySnapshot, ...],
                         current: tuple[RawBatterySnapshot, ...],
                         journal_lookup: JournalLookup | None) -> tuple[SleepInterval, ...]:
    by_identity = {item.identity: item for item in previous}
    clock_intervals = tuple(
        interval for item in current
        if (old := by_identity.get(item.identity)) is not None
        and (interval := clock_sleep(old, item)) is not None
    )
    if not clock_intervals or journal_lookup is None:
        return clock_intervals
    since = min(interval.started_at for interval in clock_intervals) - 60
    journal = tuple(journal_lookup(since))
    relevant = tuple(item for item in journal if any(
        item.started_at < clock.ended_at and item.ended_at > clock.started_at
        for clock in clock_intervals
    ))
    return relevant or clock_intervals


def collect_v1(source: BatterySource, storage: V1Storage, *, timestamp: int | None = None,
               profile: str | None = None,
               journal_lookup: JournalLookup | None = journal_intervals,
               configured_interval_ms: int = 60_000) -> tuple[Measurement, PollResult]:
    """Poll once into an explicitly supplied schema-v4 database."""
    storage.initialize_writer()
    now = int(time.time()) if timestamp is None else timestamp
    history = _checkpoint_raw_history(storage)
    latest_by_identity = {}
    for item in history:
        latest_by_identity[item.identity] = item
    previous = tuple(latest_by_identity.values())
    raw = source.read_raw(now)
    new_sleeps = _new_sleep_intervals(previous, raw, journal_lookup)
    with storage.reader() as db:
        stored_sleeps = tuple(
            (int(row[0]) // 1_000, int(row[1]) // 1_000)
            for row in db.execute(
                "SELECT started_at_ms,ended_at_ms FROM sleep_intervals WHERE ended_at_ms>=?",
                ((now - 600) * 1_000,),
            )
        )
    sleep_ranges = stored_sleeps + tuple(
        (item.started_at, item.ended_at) for item in new_sleeps
    )
    measurement = aggregate(raw, source.resolver, history, sleep_ranges)
    result = V1Collector(storage, configured_interval_ms).process_poll(
        measurement, profile=profile, sleeps=new_sleeps
    )
    return measurement, result


def read_v1_view(storage: V1Storage, *, now: int | None = None) -> V1HistorySnapshot:
    effective_now = int(time.time()) if now is None else now
    return V1History(storage.path).load(effective_now - HISTORY_SECONDS, now=effective_now)


def render_v1(storage: V1Storage, *, now: int | None = None,
              current: Measurement | None = None) -> str:
    """Render the locked dashboard entirely through read-only schema-v4 accessors."""
    view = read_v1_view(storage, now=now)
    displayed = view.current if current is None else current
    render_now = displayed.timestamp if now is None else now
    estimate = estimate_remaining(displayed, view.trend_history, displayed.timestamp)
    return render_dashboard(
        displayed, view.history, view.session, estimate, render_now, view.sleeps,
        view.health.percent if view.health else None, view.power_profile,
    )

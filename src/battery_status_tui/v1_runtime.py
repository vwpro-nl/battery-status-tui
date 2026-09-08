"""Schema-v4 collection and rendering runtime used by the normal CLI."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from collections import deque
from dataclasses import dataclass, replace

from .estimate import estimate_remaining
from .graph import HISTORY_LOOKBACK_SECONDS, render_dashboard
from .live import LiveOperatingMode, LiveReading, classify_live_mode
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


def _identity_parts(identity: str) -> tuple[str, str, str] | None:
    values = identity.split("|", 2)
    return tuple(values) if len(values) == 3 else None


def _stabilize_identities(
    current: tuple[RawBatterySnapshot, ...],
    previous: tuple[RawBatterySnapshot, ...],
) -> tuple[RawBatterySnapshot, ...]:
    """Reuse prior optional metadata unless new non-empty metadata conflicts."""
    prior = {}
    for item in previous:
        parts = _identity_parts(item.identity)
        if parts is not None:
            prior[parts[0]] = (item.identity, parts[1], parts[2])
    result = []
    for item in current:
        parts = _identity_parts(item.identity)
        old = None if parts is None else prior.get(parts[0])
        if old is not None:
            _, model, serial = parts
            old_identity, old_model, old_serial = old
            conflict = ((model and old_model and model != old_model)
                        or (serial and old_serial and serial != old_serial))
            if not conflict:
                item = replace(item, identity=old_identity)
        result.append(item)
    return tuple(result)


def _new_sleep_intervals(previous: tuple[RawBatterySnapshot, ...],
                         current: tuple[RawBatterySnapshot, ...],
                         journal_lookup: JournalLookup | None) -> tuple[SleepInterval, ...]:
    by_identity = {item.identity: item for item in previous}
    matched = tuple((old, item) for item in current
                    if (old := by_identity.get(item.identity)) is not None)
    clock_intervals = tuple(
        interval for old, item in matched
        if (interval := clock_sleep(old, item)) is not None
    )
    cross_boot = tuple((old, item) for old, item in matched if old.boot_id != item.boot_id)
    if (not clock_intervals and not cross_boot) or journal_lookup is None:
        return clock_intervals
    since = min(
        [interval.started_at for interval in clock_intervals]
        + [old.timestamp for old, _item in cross_boot]
    ) - 60
    journal = tuple(journal_lookup(since))
    relevant = tuple(item for item in journal if any(
        item.started_at < clock.ended_at and item.ended_at > clock.started_at
        for clock in clock_intervals
    ) or any(
        item.started_at < current_item.timestamp and item.ended_at > old.timestamp
        and (item.boot_id or "").replace("-", "") == old.boot_id.replace("-", "")
        for old, current_item in cross_boot
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
    raw = _stabilize_identities(source.read_raw(now), previous)
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
    return V1History(storage.path).load(
        effective_now - HISTORY_LOOKBACK_SECONDS, now=effective_now)


def render_v1(storage: V1Storage, *, now: int | None = None,
              current: Measurement | None = None) -> str:
    """Render the locked dashboard entirely through read-only schema-v4 accessors."""
    view = read_v1_view(storage, now=now)
    return render_v1_view(view, now=now, current=current)


def render_v1_view(view: V1HistorySnapshot, *, now: int | None = None,
                   current: Measurement | None = None,
                   live: LiveReading | None = None,
                   live_now: int | None = None,
                   diagnostic_live_power: bool = False,
                   weighted_live_power: Measurement | None = None,
                   show_persisted_power: bool = False,
                   show_weighted_power: bool = False,
                   power_decimals: int = 1,
                   completed_charge_seconds: int | None = None) -> str:
    """Render one already-loaded, internally consistent read-only snapshot."""
    displayed = view.current if current is None else current
    render_now = displayed.timestamp if now is None else now
    estimate = estimate_remaining(displayed, view.trend_history, displayed.timestamp)
    title_current, presentation_direction = _live_title_overlay(
        displayed, live, displayed.timestamp if live_now is None else live_now,
        view.configured_interval_ms,
    )
    semantic_direction = None
    eta_pending = False
    if live is not None and live.raw is not None:
        live_mode = classify_live_mode(
            live.raw.ac_online, live.raw.state,
            tuple(item.state for item in live.raw.raw_batteries),
        )
        semantic_direction = (
            "charging" if live_mode is LiveOperatingMode.CHARGING else
            "discharging" if live_mode is LiveOperatingMode.DISCHARGING else None
        )
        if semantic_direction is not None:
            compatible = (displayed.session_kind == semantic_direction
                          and view.session is not None
                          and view.session.kind == semantic_direction)
            if not compatible:
                estimate = None
                eta_pending = True
    diagnostic_power = None
    if diagnostic_live_power or show_persisted_power or show_weighted_power:
        actual_live = None if live is None else live.display
        diagnostic_power = (displayed, actual_live, weighted_live_power)
    return render_dashboard(
        displayed, view.history, view.session, estimate, render_now, view.sleeps,
        view.health.percent if view.health else None, view.power_profile,
        title_current=title_current,
        presentation_direction=presentation_direction,
        diagnostic_power=diagnostic_power,
        show_persisted_power=show_persisted_power,
        show_weighted_power=show_weighted_power,
        power_decimals=power_decimals,
        completed_charge_seconds=completed_charge_seconds,
        semantic_direction=semantic_direction,
        eta_pending=eta_pending,
    )


@dataclass(frozen=True, slots=True)
class _WeightedSample:
    watts: float
    approximate: bool


class WeightedLivePowerDisplay:
    """Presentation-only weighted average of genuine live observations."""

    def __init__(self, samples: int = 5) -> None:
        if samples <= 0:
            raise ValueError("weighted power sample count must be positive")
        self._samples: deque[_WeightedSample] = deque(maxlen=samples)
        self._epoch: int | None = None
        self._last_observation: tuple[int, float] | None = None

    def update(self, live: LiveReading | None) -> Measurement | None:
        if live is None:
            return None
        if self._epoch != live.power_epoch:
            self._samples.clear()
            self._last_observation = None
            self._epoch = live.power_epoch
        display = live.display
        if (display is None or display.power_w is None
                or live.power_observation_at is None):
            return None
        observation = (live.power_epoch, live.power_observation_at)
        if observation != self._last_observation:
            self._samples.append(_WeightedSample(
                display.power_w, display.power_approximate))
            self._last_observation = observation
        weights = range(1, len(self._samples) + 1)
        denominator = sum(weights)
        watts = sum(sample.watts * weight
                    for sample, weight in zip(self._samples, weights)) / denominator
        return replace(
            display, power_w=watts,
            power_approximate=any(sample.approximate for sample in self._samples),
        )


class CompletedChargeDisplay:
    """Viewer-only frozen duration for an observed completed charging epoch."""

    def __init__(self) -> None:
        self._epoch: int | None = None
        self._charging_start: int | None = None
        self._charging_identity: tuple[str | None, tuple[str, ...]] | None = None
        self._was_charging = False
        self._completed_seconds: int | None = None

    def update(self, view: V1HistorySnapshot, live: LiveReading | None,
               now: int) -> int | None:
        if live is None or live.raw is None:
            return self._completed_seconds
        raw = live.raw
        explicit_full = raw.ac_online is True and raw.state == "full"
        identity = (raw.boot_id, tuple(sorted(
            item.identity for item in raw.raw_batteries)))
        if self._epoch != live.power_epoch:
            carries_completion = (explicit_full and self._was_charging
                                  and identity == self._charging_identity)
            self._epoch = live.power_epoch
            self._completed_seconds = None
            if not carries_completion:
                self._charging_start = None
                self._charging_identity = None
                self._was_charging = False
        mode = classify_live_mode(
            raw.ac_online, raw.state,
            tuple(item.state for item in raw.raw_batteries),
        )
        if mode is LiveOperatingMode.CHARGING:
            session = view.session
            if session is not None and session.kind == "charging":
                self._charging_start = session.started_at
                self._charging_identity = identity
            self._was_charging = True
            self._completed_seconds = None
            return None
        if explicit_full and self._was_charging and self._charging_start is not None:
            self._completed_seconds = max(0, now - self._charging_start)
            self._was_charging = False
        elif explicit_full and self._completed_seconds is None:
            evidence = view.completed_charge
            same_batteries = (view.current.battery_identity is not None
                              and view.current.battery_identity == raw.battery_identity)
            if (evidence is not None and evidence.boot_id == raw.boot_id
                    and same_batteries
                    and evidence.completed_at >= evidence.started_at):
                self._completed_seconds = evidence.completed_at - evidence.started_at
        elif not explicit_full:
            self._charging_start = None
            self._charging_identity = None
            self._was_charging = False
            self._completed_seconds = None
        return self._completed_seconds


def _live_title_overlay(current: Measurement, live: LiveReading | None, now: int,
                        configured_interval_ms: int) -> tuple[Measurement, str | None]:
    """Overlay live-only title fields without changing persisted render inputs."""
    if live is None:
        return current, None
    if live.raw is None:
        return replace(current, power_w=None, power_approximate=False,
                       power_method="unavailable"), "neutral"
    mode = classify_live_mode(
        live.raw.ac_online, live.raw.state,
        tuple(item.state for item in live.raw.raw_batteries),
    )
    direction = (
        "charging" if mode is LiveOperatingMode.CHARGING else
        "discharging" if mode is LiveOperatingMode.DISCHARGING else "neutral"
    )
    percentage = live.raw.percentage
    if not 0 <= percentage <= 100:
        percentage = current.percentage
    live_power = None if live.display is None else live.display.power_w
    if live_power is not None:
        power = live_power
        approximate = live.display.power_approximate
        method = live.display.power_method
    else:
        power, approximate, method = None, False, "unavailable"
    return replace(current, percentage=percentage, state=live.raw.state,
                   ac_online=live.raw.ac_online, power_w=power,
                   power_approximate=approximate, power_method=method), direction

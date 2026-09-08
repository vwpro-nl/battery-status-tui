"""Read-only, bounded live sysfs sampling and display-power smoothing."""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from .models import Measurement, PowerReading, RawBatterySnapshot
from .power import MAX_DELTA_SECONDS, PowerResolver, is_sysfs_direct_method
from .sources import SourceUnavailable, SysfsObservation, SysfsSource, aggregate

DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
SAMPLE_PERIOD_SECONDS = DEFAULT_SAMPLE_INTERVAL_SECONDS
RAW_RETENTION_SECONDS = 30.0
RAW_RETENTION_MAX_SAMPLES = 4096
DISPLAY_MEDIAN_SECONDS = 4.0
DISPLAY_EWMA_ALPHA = 0.35
MIN_ACTIVE_POWER_W = 0.05
POWER_HOLD_SECONDS = 5.0
ZERO_CONFIRMATION_SECONDS = 4.0
SUSPEND_TOLERANCE_SECONDS = 0.5
CONTINUITY_INTERVAL_MULTIPLIER = 2.5


class LiveOperatingMode(str, Enum):
    CHARGING = "charging"
    DISCHARGING = "discharging"
    CONNECTED_IDLE = "connected-idle"
    TRANSITIONAL = "transitional"
    UNKNOWN = "unknown"


def classify_live_mode(ac_online: bool | None, state: str,
                       pack_states: Sequence[str] = ()) -> LiveOperatingMode:
    """Classify physical live state without persisted session semantics."""
    states = set(pack_states or (state,))
    if "charging" in states and "discharging" in states:
        return LiveOperatingMode.TRANSITIONAL
    if ac_online is True:
        if states == {"charging"}:
            return LiveOperatingMode.CHARGING
        if states <= {"not charging", "full"}:
            return LiveOperatingMode.CONNECTED_IDLE
        return LiveOperatingMode.TRANSITIONAL
    if ac_online is False:
        if states == {"discharging"}:
            return LiveOperatingMode.DISCHARGING
        return LiveOperatingMode.TRANSITIONAL
    return LiveOperatingMode.UNKNOWN


@dataclass(frozen=True, slots=True)
class LiveReading:
    raw: Measurement | None
    display: Measurement | None
    error: str | None = None
    observation: SysfsObservation | None = None
    power_epoch: int = 0
    power_observation_at: float | None = None


@dataclass(frozen=True, slots=True)
class _PowerSample:
    sampled_at: float
    watts: float


class LiveSampler:
    """Sample a persistent sysfs source at fixed, skip-missed deadlines."""

    def __init__(self, source: SysfsSource | None = None,
                 resolver: PowerResolver | None = None, *,
                 interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("live sample interval must be positive and finite")
        self.source = source or SysfsSource()
        self.resolver = resolver or PowerResolver()
        self.interval = interval
        self.clock = clock
        self.wall_clock = wall_clock
        self._raw_samples: deque[tuple[float, Measurement]] = deque(
            maxlen=RAW_RETENTION_MAX_SAMPLES)
        self._counter_history: deque[RawBatterySnapshot] = deque()
        self._counter_signature: tuple[tuple[str, float | None, float | None], ...] | None = None
        self._counter_reading: PowerReading | None = None
        self._counter_reading_at: float | None = None
        self._power_epoch = 0
        self._power_observation_at: float | None = None
        self._power_samples: deque[_PowerSample] = deque()
        self._next_sample_at: float | None = None
        self._last_poll_at: float | None = None
        self._previous_raw: Measurement | None = None
        self._last_mode: LiveOperatingMode | None = None
        self._last_ac: bool | None = None
        self._last_battery_set: tuple[str, ...] | None = None
        self._ewma: float | None = None
        self._last_valid_at: float | None = None
        self._idle_zero_since: float | None = None
        self._idle_zero_count = 0
        self._last_idle_zero_at: float | None = None
        self.current: Measurement | None = None
        self.last_error: str | None = None

    @property
    def next_sample_at(self) -> float | None:
        return self._next_sample_at

    @property
    def raw_samples(self) -> tuple[Measurement, ...]:
        return tuple(item for _sampled_at, item in self._raw_samples)

    @property
    def continuity_seconds(self) -> float:
        return CONTINUITY_INTERVAL_MULTIPLIER * self.interval

    @property
    def zero_confirmation_observations(self) -> int:
        return max(2, math.floor(ZERO_CONFIRMATION_SECONDS / self.interval) + 1)

    def poll(self) -> LiveReading | None:
        """Read at most once when due, advancing from the prior deadline."""
        now = self.clock()
        if self._next_sample_at is None:
            self._next_sample_at = now
        if now < self._next_sample_at:
            return None
        missed = math.floor((now - self._next_sample_at) / self.interval)
        self._next_sample_at += (missed + 1) * self.interval
        scheduler_gap = None if self._last_poll_at is None else now - self._last_poll_at
        self._last_poll_at = now
        try:
            read_observed = getattr(self.source, "read_observed", None)
            if read_observed is None:
                snapshots = self.source.read_raw(int(self.wall_clock()))
                observation = None
            else:
                observation = read_observed(int(self.wall_clock()))
                snapshots = observation.snapshots
            raw = aggregate(snapshots, self.resolver, direct_only=True)
        except SourceUnavailable as error:
            self._hard_reset(clear_raw=True)
            self.current = None
            self.last_error = str(error)
            return LiveReading(None, None, self.last_error,
                               power_epoch=self._power_epoch)

        mode = classify_live_mode(raw.ac_online, raw.state,
                                  tuple(item.state for item in snapshots))
        if mode is LiveOperatingMode.TRANSITIONAL:
            raw = replace(raw, power_w=None, power_method="unavailable",
                          power_approximate=False, power_confidence="none")
        if self._is_discontinuity(raw, scheduler_gap):
            self._hard_reset(clear_raw=True)

        plug_changed = self._last_mode is not None and raw.ac_online != self._last_ac
        mode_changed = self._last_mode is not None and mode != self._last_mode
        if plug_changed or mode_changed:
            self._reset_filter()
            self._reset_counters()
            self._power_epoch += 1

        if mode is LiveOperatingMode.DISCHARGING and raw.power_w is None:
            raw = self._discharge_counter_fallback(raw, snapshots)
        elif raw.power_w is not None:
            self._counter_reading = None
            self._counter_reading_at = None
            self._power_observation_at = now

        self.last_error = None
        self._raw_samples.append((now, raw))
        self._expire_raw(now)
        self.current = self._display_measurement(raw, mode, now)
        self._previous_raw = raw
        self._last_battery_set = self._battery_set(raw)
        return LiveReading(
            raw, self.current, observation=observation,
            power_epoch=self._power_epoch,
            power_observation_at=self._power_observation_at,
        )

    @staticmethod
    def _battery_set(measurement: Measurement) -> tuple[str, ...]:
        return tuple(sorted(item.identity for item in measurement.raw_batteries))

    def _is_discontinuity(self, raw: Measurement,
                          scheduler_gap: float | None) -> bool:
        previous = self._previous_raw
        if scheduler_gap is not None and (scheduler_gap < 0
                                          or scheduler_gap > self.continuity_seconds):
            return True
        if previous is None:
            return False
        if raw.timestamp < previous.timestamp:
            return True
        if previous.boot_id != raw.boot_id:
            return True
        if self._last_battery_set != self._battery_set(raw):
            return True
        clocks = (previous.monotonic_s, previous.boottime_s,
                  raw.monotonic_s, raw.boottime_s)
        if any(value is None for value in clocks):
            return False
        monotonic_delta = raw.monotonic_s - previous.monotonic_s
        boottime_delta = raw.boottime_s - previous.boottime_s
        return (monotonic_delta < 0 or boottime_delta < 0
                or boottime_delta - monotonic_delta > SUSPEND_TOLERANCE_SECONDS)

    def _expire_raw(self, now: float) -> None:
        while self._raw_samples and now - self._raw_samples[0][0] > RAW_RETENTION_SECONDS:
            self._raw_samples.popleft()

    def _reset_filter(self) -> None:
        self._power_samples.clear()
        self._ewma = None
        self._last_valid_at = None
        self._idle_zero_since = None
        self._idle_zero_count = 0
        self._last_idle_zero_at = None

    def _reset_counters(self) -> None:
        self._counter_history.clear()
        self._counter_signature = None
        self._counter_reading = None
        self._counter_reading_at = None

    def _hard_reset(self, *, clear_raw: bool) -> None:
        self._power_epoch += 1
        self._power_observation_at = None
        self._reset_filter()
        self._previous_raw = None
        self._last_mode = None
        self._last_ac = None
        self._last_battery_set = None
        if clear_raw:
            self._raw_samples.clear()
        self._reset_counters()

    def _display_measurement(self, raw: Measurement, mode: LiveOperatingMode,
                             now: float) -> Measurement:
        self._last_ac = raw.ac_online
        self._last_mode = mode
        if mode is LiveOperatingMode.CONNECTED_IDLE:
            return self._idle_display(raw, now)
        if mode in {LiveOperatingMode.CHARGING, LiveOperatingMode.DISCHARGING}:
            return self._active_display(raw, now)
        self._reset_filter()
        return replace(raw, power_w=None, power_approximate=False)

    def _idle_display(self, raw: Measurement, now: float) -> Measurement:
        direct = is_sysfs_direct_method(raw.power_method)
        is_zero = (direct and raw.power_w is not None and math.isfinite(raw.power_w)
                   and raw.power_w < MIN_ACTIVE_POWER_W)
        is_positive = (direct and raw.power_w is not None
                       and math.isfinite(raw.power_w)
                       and raw.power_w >= MIN_ACTIVE_POWER_W)
        continuous = (self._last_idle_zero_at is not None
                      and now - self._last_idle_zero_at <= self.continuity_seconds)
        if is_zero:
            if not continuous:
                self._idle_zero_since = now
                self._idle_zero_count = 1
            else:
                self._idle_zero_count += 1
            self._last_idle_zero_at = now
        elif is_positive:
            self._idle_zero_since = None
            self._idle_zero_count = 0
            self._last_idle_zero_at = None
        elif (self._last_idle_zero_at is not None
              and now - self._last_idle_zero_at > self.continuity_seconds):
            self._idle_zero_since = None
            self._idle_zero_count = 0
            self._last_idle_zero_at = None
        confirmed = (is_zero and self._idle_zero_since is not None
                     and now - self._idle_zero_since >= ZERO_CONFIRMATION_SECONDS
                     and self._idle_zero_count >= self.zero_confirmation_observations)
        if confirmed:
            return replace(raw, power_w=0.0, power_approximate=False)
        return replace(raw, power_w=None, power_approximate=False)

    def _active_display(self, raw: Measurement, now: float) -> Measurement:
        if raw.power_approximate and raw.power_w is not None:
            self._reset_filter()
            return raw
        valid = (is_sysfs_direct_method(raw.power_method)
                 and raw.power_w is not None and math.isfinite(raw.power_w)
                 and raw.power_w >= MIN_ACTIVE_POWER_W)
        if valid:
            self._power_samples.append(_PowerSample(now, raw.power_w))
            while (self._power_samples
                   and now - self._power_samples[0].sampled_at > DISPLAY_MEDIAN_SECONDS):
                self._power_samples.popleft()
            median = statistics.median(item.watts for item in self._power_samples)
            if self._ewma is None or self._last_valid_at is None:
                self._ewma = median
            else:
                elapsed = now - self._last_valid_at
                alpha = 1 - (1 - DISPLAY_EWMA_ALPHA) ** elapsed
                self._ewma = alpha * median + (1 - alpha) * self._ewma
            self._last_valid_at = now
        if (self._ewma is None or self._last_valid_at is None
                or now - self._last_valid_at > POWER_HOLD_SECONDS):
            return replace(raw, power_w=None, power_approximate=False)
        return replace(raw, power_w=self._ewma, power_approximate=False)

    @staticmethod
    def _counter_key(snapshots: Sequence[RawBatterySnapshot]) -> tuple[tuple[str, float | None, float | None], ...]:
        return tuple(sorted((item.identity, item.energy_now_wh, item.charge_now_ah)
                            for item in snapshots))

    def _discharge_counter_fallback(
        self, raw: Measurement, snapshots: Sequence[RawBatterySnapshot],
    ) -> Measurement:
        signature = self._counter_key(snapshots)
        changed = signature != self._counter_signature
        if changed:
            history = tuple(self._counter_history)
            readings = [self.resolver.resolve_counter_delta(item, history)
                        for item in snapshots]
            if readings and all(item.watts is not None for item in readings):
                watts = sum(item.watts for item in readings if item.watts is not None)
                methods = {item.method for item in readings}
                method = (next(iter(methods)) if len(methods) == 1 else
                          "mixed:" + "+".join(sorted(methods)))
                self._counter_reading = PowerReading(
                    watts, method, approximate=True, confidence="medium",
                    window_s=max(item.window_s or 0 for item in readings) or None,
                )
                self._counter_reading_at = snapshots[0].monotonic_s
                self._power_observation_at = snapshots[0].monotonic_s
            for item in snapshots:
                self._counter_history.append(item)
            self._counter_signature = signature
            cutoff = snapshots[0].monotonic_s - MAX_DELTA_SECONDS
            while (self._counter_history
                   and self._counter_history[0].monotonic_s < cutoff):
                self._counter_history.popleft()
        if (self._counter_reading is None or self._counter_reading_at is None
                or snapshots[0].monotonic_s - self._counter_reading_at > MAX_DELTA_SECONDS):
            self._counter_reading = None
            self._counter_reading_at = None
            return raw
        return replace(
            raw, power_w=self._counter_reading.watts,
            power_method=self._counter_reading.method,
            power_approximate=self._counter_reading.approximate,
            power_confidence=self._counter_reading.confidence,
            power_window_s=self._counter_reading.window_s,
        )

"""Resolve direct and time-derived battery power."""
from __future__ import annotations
import math
import statistics
from collections.abc import Sequence
from .models import PowerReading, RawBatterySnapshot

MIN_DELTA_SECONDS = 120
MAX_DELTA_SECONDS = 600
MIN_USEFUL_WATTS = 0.05
MAX_PLAUSIBLE_WATTS = 500.0
SYSFS_DIRECT_METHODS = frozenset({"power-now", "current-voltage"})

def is_sysfs_direct_method(method: str) -> bool:
    if method in SYSFS_DIRECT_METHODS:
        return True
    if not method.startswith("mixed:"):
        return False
    constituents = method.removeprefix("mixed:").split("+")
    return bool(constituents) and all(item in SYSFS_DIRECT_METHODS
                                      for item in constituents)

def _valid(value: float | None, state: str) -> float | None:
    if value is None or value < 0 or value > MAX_PLAUSIBLE_WATTS:
        return None
    if state in {"charging", "discharging"} and value < MIN_USEFUL_WATTS:
        return None
    return value

def _directional_delta(previous: float, current: float, state: str) -> float | None:
    delta = current - previous
    if state == "discharging" and delta >= 0:
        return None
    if state == "charging" and delta <= 0:
        return None
    return abs(delta)

class PowerResolver:
    def resolve_sysfs_direct(self, current: RawBatterySnapshot) -> PowerReading:
        """Resolve only instantaneous sysfs sources suitable for live display."""
        value = current.power_now_w
        if value is not None and (not math.isfinite(value) or value < 0
                                  or value > MAX_PLAUSIBLE_WATTS):
            value = None
        power_now = value
        if value is not None and not (current.state in {"charging", "discharging"}
                                      and value < MIN_USEFUL_WATTS):
            return PowerReading(value, "power-now", confidence="high")
        if current.current_now_a is not None and current.voltage_now_v is not None:
            value = abs(current.current_now_a * current.voltage_now_v)
            if math.isfinite(value) and value <= MAX_PLAUSIBLE_WATTS:
                return PowerReading(value, "current-voltage", confidence="high")
        if power_now is not None:
            return PowerReading(power_now, "power-now", confidence="high")
        return PowerReading(None, "unavailable")

    def resolve(self, current: RawBatterySnapshot, history: Sequence[RawBatterySnapshot] = (),
                sleep_intervals: Sequence[tuple[int, int]] = ()) -> PowerReading:
        direct = self.resolve_sysfs_direct(current)
        if _valid(direct.watts, current.state) is not None:
            return direct
        value = _valid(current.upower_energy_rate_w, current.state)
        if value is not None:
            return PowerReading(abs(value), "upower-energy-rate", confidence="medium")
        return self.resolve_counter_delta(current, history, sleep_intervals)

    def resolve_counter_delta(
        self, current: RawBatterySnapshot,
        history: Sequence[RawBatterySnapshot] = (),
        sleep_intervals: Sequence[tuple[int, int]] = (),
    ) -> PowerReading:
        """Resolve native counter deltas without consulting direct or UPower data."""
        readings: list[tuple[float, float, str]] = []
        for previous in history:
            wall_elapsed = current.timestamp - previous.timestamp
            if previous.identity != current.identity or previous.state != current.state or previous.boot_id != current.boot_id:
                continue
            if not MIN_DELTA_SECONDS <= wall_elapsed <= MAX_DELTA_SECONDS:
                continue
            if any(previous.timestamp < end and current.timestamp > start for start, end in sleep_intervals):
                continue
            elapsed = current.monotonic_s - previous.monotonic_s
            if not MIN_DELTA_SECONDS <= elapsed <= MAX_DELTA_SECONDS:
                continue
            if current.energy_now_wh is not None and previous.energy_now_wh is not None:
                delta = _directional_delta(previous.energy_now_wh, current.energy_now_wh, current.state)
                if delta is not None:
                    readings.append((delta * 3600 / elapsed, elapsed, "energy-delta"))
            elif current.charge_now_ah is not None and previous.charge_now_ah is not None:
                voltages = [v for v in (previous.voltage_now_v, current.voltage_now_v) if v is not None]
                delta = _directional_delta(previous.charge_now_ah, current.charge_now_ah, current.state)
                if voltages and delta is not None:
                    readings.append((delta * statistics.mean(voltages) * 3600 / elapsed, elapsed, "charge-delta"))
        valid = [item for item in readings if _valid(item[0], current.state) is not None]
        if valid:
            method = "energy-delta" if any(item[2] == "energy-delta" for item in valid) else "charge-delta"
            selected = [item for item in valid if item[2] == method]
            return PowerReading(statistics.median(item[0] for item in selected), method, True, "medium", max(item[1] for item in selected))
        return PowerReading(None, "unavailable")

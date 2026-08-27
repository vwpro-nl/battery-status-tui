"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ACTIVE_STATES = {"charging", "discharging"}


@dataclass(frozen=True, slots=True)
class RawBatterySnapshot:
    timestamp: int
    monotonic_s: float
    boottime_s: float
    boot_id: str
    device: str
    identity: str
    percentage: float
    state: str
    ac_online: bool | None
    power_now_w: float | None = None
    current_now_a: float | None = None
    voltage_now_v: float | None = None
    energy_now_wh: float | None = None
    energy_full_wh: float | None = None
    energy_full_design_wh: float | None = None
    charge_now_ah: float | None = None
    charge_full_ah: float | None = None
    charge_full_design_ah: float | None = None
    upower_energy_rate_w: float | None = None
    time_to_empty_s: int | None = None
    time_to_full_s: int | None = None
    cycle_count: int | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PowerReading:
    watts: float | None
    method: Literal["power-now", "current-voltage", "upower-energy-rate", "energy-delta", "charge-delta", "unavailable"]
    approximate: bool = False
    confidence: str = "none"
    window_s: float | None = None


@dataclass(frozen=True, slots=True)
class Measurement:
    timestamp: int
    percentage: float
    state: str
    ac_online: bool | None
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    time_to_empty_s: int | None = None
    time_to_full_s: int | None = None
    energy_wh: float | None = None
    energy_full_wh: float | None = None
    energy_full_design_wh: float | None = None
    cycle_count: int | None = None
    source: str = "unknown"
    device: str = "unknown"
    power_method: str = "unavailable"
    power_approximate: bool = False
    power_confidence: str = "none"
    power_window_s: float | None = None
    charge_ah: float | None = None
    charge_full_ah: float | None = None
    charge_full_design_ah: float | None = None
    monotonic_s: float | None = None
    boottime_s: float | None = None
    boot_id: str | None = None
    battery_identity: str | None = None
    raw_batteries: tuple[RawBatterySnapshot, ...] = ()

    @property
    def session_kind(self) -> str | None:
        if self.ac_online is False:
            return "discharging"
        if self.ac_online is True:
            if self.state == "charging" or self.percentage < 100:
                return "charging"
            return None
        if self.state in ACTIVE_STATES:
            return self.state
        return None

    @property
    def remaining_seconds(self) -> int | None:
        if self.session_kind == "charging":
            return self.time_to_full_s
        if self.session_kind == "discharging":
            return self.time_to_empty_s
        return None

    @property
    def health_percent(self) -> float | None:
        if not self.energy_full_wh or not self.energy_full_design_wh:
            return None
        return self.energy_full_wh / self.energy_full_design_wh * 100


@dataclass(frozen=True, slots=True)
class Session:
    id: int
    kind: str
    started_at: int
    ended_at: int | None
    start_percentage: float
    end_percentage: float | None


@dataclass(frozen=True, slots=True)
class Estimate:
    seconds: int
    source: str
    slope_percent_per_hour: float | None = None


@dataclass(frozen=True, slots=True)
class SleepInterval:
    started_at: int
    ended_at: int
    kind: str = "sleep"
    source: str = "clock"
    boot_id: str | None = None
    pre_percentage: float | None = None
    post_percentage: float | None = None

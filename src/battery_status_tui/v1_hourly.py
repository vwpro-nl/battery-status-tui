"""Time-weighted schema-v4 hourly accumulation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields


HOUR_MS = 3_600_000
QUALITY_UNKNOWN = 1
QUALITY_SLEEP = 2
QUALITY_ENERGY_REJECTED = 4
QUALITY_POWER_REJECTED = 8


def utc_hour(timestamp_ms: int) -> int:
    return timestamp_ms // HOUR_MS * HOUR_MS


def _threshold_duration(start: float, end: float, duration: int,
                        threshold: float, *, above: bool) -> int:
    if start == end:
        return duration if (start > threshold if above else start < threshold) else 0
    low, high = sorted((start, end))
    if above:
        if high <= threshold:
            return 0
        if low >= threshold:
            return duration
        return round(duration * (high - threshold) / (high - low))
    if low >= threshold:
        return 0
    if high <= threshold:
        return duration
    return round(duration * (threshold - low) / (high - low))


@dataclass(slots=True)
class HourlyAccumulator:
    hour_start_ms: int
    battery_set_key: str | None = None
    soc_first: float | None = None
    soc_last: float | None = None
    soc_min: float | None = None
    soc_max: float | None = None
    soc_integral_percent_ms: float = 0.0
    charged_energy_wh: float = 0.0
    discharged_energy_wh: float = 0.0
    observed_ms: int = 0
    sleep_ms: int = 0
    unknown_ms: int = 0
    charging_ms: int = 0
    discharging_ms: int = 0
    full_ms: int = 0
    other_state_ms: int = 0
    ac_online_ms: int = 0
    ac_offline_ms: int = 0
    ac_unknown_ms: int = 0
    under_20_ms: int = 0
    above_80_ms: int = 0
    above_95_ms: int = 0
    full_on_ac_ms: int = 0
    charge_power_integral_w_ms: float = 0.0
    charge_power_max_w: float | None = None
    charge_power_valid_ms: int = 0
    discharge_power_integral_w_ms: float = 0.0
    discharge_power_max_w: float | None = None
    discharge_power_valid_ms: int = 0
    direct_power_ms: int = 0
    estimated_power_ms: int = 0
    unknown_power_ms: int = 0
    poll_count: int = 0
    state_event_count: int = 0
    quality_flags: int = 0
    energy_provenance_mask: int = 0
    profiles: dict[str, int] = field(default_factory=dict)

    def add_observed(self, duration: int, start_soc: float, end_soc: float,
                     state: str, ac_online: bool | None, power_w: float | None,
                     approximate: bool, profile: str | None,
                     energy_delta_wh: float | None = None,
                     energy_provenance_mask: int = 0) -> None:
        if duration <= 0:
            return
        if self.covered_ms + duration > HOUR_MS:
            raise ValueError("hourly accumulator exceeds one UTC hour")
        self.observed_ms += duration
        if self.soc_first is None:
            self.soc_first = start_soc
        self.soc_last = end_soc
        self.soc_min = min(start_soc, end_soc) if self.soc_min is None else min(
            self.soc_min, start_soc, end_soc
        )
        self.soc_max = max(start_soc, end_soc) if self.soc_max is None else max(
            self.soc_max, start_soc, end_soc
        )
        self.soc_integral_percent_ms += (start_soc + end_soc) * duration / 2
        normalized = state.lower()
        if normalized == "charging":
            self.charging_ms += duration
        elif normalized == "discharging":
            self.discharging_ms += duration
        elif normalized in {"full", "charged", "fully-charged"}:
            self.full_ms += duration
        else:
            self.other_state_ms += duration
        if ac_online is True:
            self.ac_online_ms += duration
        elif ac_online is False:
            self.ac_offline_ms += duration
        else:
            self.ac_unknown_ms += duration
        self.under_20_ms += _threshold_duration(start_soc, end_soc, duration, 20, above=False)
        self.above_80_ms += _threshold_duration(start_soc, end_soc, duration, 80, above=True)
        self.above_95_ms += _threshold_duration(start_soc, end_soc, duration, 95, above=True)
        if normalized in {"full", "charged", "fully-charged"} and ac_online is True:
            self.full_on_ac_ms += duration
        if power_w is None or not math.isfinite(power_w) or power_w < 0:
            self.unknown_power_ms += duration
        else:
            if approximate:
                self.estimated_power_ms += duration
            else:
                self.direct_power_ms += duration
            if normalized in {"charging", "discharging"}:
                prefix = "charge" if normalized == "charging" else "discharge"
                integral = f"{prefix}_power_integral_w_ms"
                valid = f"{prefix}_power_valid_ms"
                maximum = f"{prefix}_power_max_w"
                setattr(self, integral, getattr(self, integral) + power_w * duration)
                setattr(self, valid, getattr(self, valid) + duration)
                old_max = getattr(self, maximum)
                setattr(self, maximum, power_w if old_max is None else max(old_max, power_w))
        self.energy_provenance_mask |= energy_provenance_mask
        if energy_delta_wh is not None:
            if energy_delta_wh >= 0:
                self.charged_energy_wh += energy_delta_wh
            else:
                self.discharged_energy_wh += -energy_delta_wh
        if profile:
            self.profiles[profile] = self.profiles.get(profile, 0) + duration

    def add_sleep(self, duration: int) -> None:
        self._add_unobserved(duration, sleep=True)

    def add_unknown(self, duration: int) -> None:
        self._add_unobserved(duration, sleep=False)

    def _add_unobserved(self, duration: int, *, sleep: bool) -> None:
        if duration <= 0:
            return
        if self.covered_ms + duration > HOUR_MS:
            raise ValueError("hourly accumulator exceeds one UTC hour")
        if sleep:
            self.sleep_ms += duration
            self.quality_flags |= QUALITY_SLEEP
        else:
            self.unknown_ms += duration
            self.quality_flags |= QUALITY_UNKNOWN

    @property
    def covered_ms(self) -> int:
        return self.observed_ms + self.sleep_ms + self.unknown_ms

    def finalize(self) -> None:
        self.add_unknown(HOUR_MS - self.covered_ms)
        self.validate(finalized=True)

    def validate(self, *, finalized: bool = False) -> None:
        if self.hour_start_ms % HOUR_MS:
            raise ValueError("hour accumulator is not UTC-hour aligned")
        if self.covered_ms > HOUR_MS or (finalized and self.covered_ms != HOUR_MS):
            raise ValueError("invalid hourly coverage")
        if self.charging_ms + self.discharging_ms + self.full_ms + self.other_state_ms != self.observed_ms:
            raise ValueError("state durations do not equal observed duration")
        if self.ac_online_ms + self.ac_offline_ms + self.ac_unknown_ms != self.observed_ms:
            raise ValueError("AC durations do not equal observed duration")
        if self.direct_power_ms + self.estimated_power_ms + self.unknown_power_ms != self.observed_ms:
            raise ValueError("power durations do not equal observed duration")
        if self.observed_ms == 0:
            if any(value is not None for value in
                   (self.soc_first, self.soc_last, self.soc_min, self.soc_max)):
                raise ValueError("SoC geometry exists without observed time")
        elif any(value is None for value in
                 (self.soc_first, self.soc_last, self.soc_min, self.soc_max)):
            raise ValueError("observed time is missing SoC geometry")
        if sum(self.profiles.values()) > self.observed_ms:
            raise ValueError("profile durations exceed observed time")

    def clone(self) -> "HourlyAccumulator":
        values = {item.name: getattr(self, item.name) for item in fields(self)
                  if item.name != "profiles"}
        values["profiles"] = dict(self.profiles)
        return HourlyAccumulator(**values)

    def checkpoint_values(self) -> dict[str, object]:
        self.validate()
        values = {item.name: getattr(self, item.name) for item in fields(self)
                  if item.name not in {"profiles", "battery_set_key"}}
        for name in (
            "soc_first", "soc_last", "soc_min", "soc_max",
            "soc_integral_percent_ms", "charged_energy_wh", "discharged_energy_wh",
            "charge_power_integral_w_ms", "charge_power_max_w",
            "discharge_power_integral_w_ms", "discharge_power_max_w",
        ):
            if values[name] is not None:
                values[name] = float(values[name])
        return values

    def finalized_values(self, generation: int, finalized_at_ms: int,
                         *, revision: int = 1) -> dict[str, object]:
        self.validate(finalized=True)
        result = self.checkpoint_values()
        result["soc_start"] = result.pop("soc_first")
        result["soc_end"] = result.pop("soc_last")
        result.update({
            "revision": revision,
            "source_generation": generation,
            "aggregation_version": 1,
            # Keep finalization deterministic across crash recovery.  The source
            # generation records when it was produced; the logical hour itself
            # is finalized at its fixed UTC boundary.
            "finalized_at_ms": self.hour_start_ms + HOUR_MS,
            "is_final": 1,
            "battery_set_key": self.battery_set_key,
        })
        return result

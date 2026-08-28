"""Compatible, bounded battery-counter deltas for permanent history."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .models import RawBatterySnapshot


MAX_PLAUSIBLE_POWER_W_PER_BATTERY = 500.0
ENERGY_PROVENANCE_NATIVE = 1
ENERGY_PROVENANCE_CHARGE = 2
ENERGY_PROVENANCE_REJECTED = 4


@dataclass(frozen=True, slots=True)
class EnergyCounter:
    identity: str
    kind: str
    value: float
    voltage_v: float | None = None


@dataclass(frozen=True, slots=True)
class EnergyDelta:
    value_wh: float | None
    provenance_mask: int = 0
    rejected: bool = False


def counters_from_raw(batteries: Iterable[RawBatterySnapshot]) -> tuple[EnergyCounter, ...]:
    """Return one native counter per present battery, or no comparable set."""
    result = []
    for item in sorted(batteries, key=lambda value: value.identity):
        if item.energy_now_wh is not None and math.isfinite(item.energy_now_wh) \
                and item.energy_now_wh >= 0:
            result.append(EnergyCounter(item.identity, "energy", item.energy_now_wh))
        elif (item.charge_now_ah is not None and math.isfinite(item.charge_now_ah)
              and item.charge_now_ah >= 0 and item.voltage_now_v is not None
              and math.isfinite(item.voltage_now_v) and item.voltage_now_v > 0):
            result.append(EnergyCounter(
                item.identity, "charge", item.charge_now_ah, item.voltage_now_v
            ))
        else:
            return ()
    return tuple(result)


def compatible_delta(previous: tuple[EnergyCounter, ...],
                     current: tuple[EnergyCounter, ...], *, elapsed_ms: int,
                     direction: str | None) -> EnergyDelta:
    """Resolve a delta only across identical native counter families.

    A rejected result denotes contradictory provenance, direction, or magnitude;
    an empty, non-rejected result simply means no comparable counters existed.
    """
    if not previous or not current:
        return EnergyDelta(None)
    before = {item.identity: item for item in previous}
    after = {item.identity: item for item in current}
    if before.keys() != after.keys() or elapsed_ms <= 0:
        return EnergyDelta(None, ENERGY_PROVENANCE_REJECTED, True)

    total = 0.0
    provenance = 0
    for identity in sorted(before):
        left, right = before[identity], after[identity]
        if left.kind != right.kind:
            return EnergyDelta(None, ENERGY_PROVENANCE_REJECTED, True)
        if left.kind == "energy":
            delta = right.value - left.value
            provenance |= ENERGY_PROVENANCE_NATIVE
        elif left.kind == "charge" and left.voltage_v and right.voltage_v:
            delta = (right.value - left.value) * (left.voltage_v + right.voltage_v) / 2
            provenance |= ENERGY_PROVENANCE_CHARGE
        else:
            return EnergyDelta(None, ENERGY_PROVENANCE_REJECTED, True)
        if not math.isfinite(delta):
            return EnergyDelta(None, ENERGY_PROVENANCE_REJECTED, True)
        normalized = (direction or "").lower()
        if ((normalized == "charging" and delta < 0)
                or (normalized == "discharging" and delta > 0)
                or normalized not in {"charging", "discharging"}):
            return EnergyDelta(None, ENERGY_PROVENANCE_REJECTED, True)
        watts = abs(delta) * 3_600_000 / elapsed_ms
        if watts > MAX_PLAUSIBLE_POWER_W_PER_BATTERY:
            return EnergyDelta(None, ENERGY_PROVENANCE_REJECTED, True)
        total += delta
    return EnergyDelta(total, provenance)


def plausible_power(value: float | None, battery_count: int) -> tuple[float | None, bool]:
    if value is None:
        return None, False
    if not math.isfinite(value) or value < 0:
        return None, True
    limit = MAX_PLAUSIBLE_POWER_W_PER_BATTERY * max(1, battery_count)
    return (value, False) if value <= limit else (None, True)

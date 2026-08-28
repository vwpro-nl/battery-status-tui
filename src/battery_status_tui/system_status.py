"""Slow-changing battery health and active power-profile metadata."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .models import RawBatterySnapshot


@dataclass(frozen=True, slots=True)
class HealthReading:
    percent: float
    source: str
    full: float
    design: float
    unit: str


@dataclass(frozen=True, slots=True)
class ProfileReading:
    profile: str
    source: str


def _valid_pair(full: float | None, design: float | None) -> bool:
    return full is not None and design is not None and full > 0 and design > 0


def resolve_health(batteries: Sequence[RawBatterySnapshot]) -> HealthReading | None:
    """Resolve one system SoH without mixing charge from unlike battery packs."""
    if not batteries:
        return None

    if all("sysfs" in item.sources and _valid_pair(item.energy_full_wh, item.energy_full_design_wh)
           for item in batteries):
        full = sum(item.energy_full_wh or 0 for item in batteries)
        design = sum(item.energy_full_design_wh or 0 for item in batteries)
        return HealthReading(full / design * 100, "sysfs-energy", full, design, "Wh")

    if all("sysfs" in item.sources and _valid_pair(item.charge_full_ah, item.charge_full_design_ah)
           for item in batteries):
        if len(batteries) == 1:
            item = batteries[0]
            return HealthReading((item.charge_full_ah or 0) / (item.charge_full_design_ah or 1) * 100,
                                 "sysfs-charge", item.charge_full_ah or 0,
                                 item.charge_full_design_ah or 0, "Ah")
        if all(item.voltage_design_v is not None and item.voltage_design_v > 0 for item in batteries):
            full = sum((item.charge_full_ah or 0) * (item.voltage_design_v or 0) for item in batteries)
            design = sum((item.charge_full_design_ah or 0) * (item.voltage_design_v or 0)
                         for item in batteries)
            return HealthReading(full / design * 100, "sysfs-charge-design-voltage", full, design, "Wh")
        return None

    if all(_valid_pair(item.upower_energy_full_wh, item.upower_energy_full_design_wh)
           for item in batteries):
        full = sum(item.upower_energy_full_wh or 0 for item in batteries)
        design = sum(item.upower_energy_full_design_wh or 0 for item in batteries)
        return HealthReading(full / design * 100, "upower-energy", full, design, "Wh")

    if len(batteries) == 1:
        capacity = batteries[0].upower_capacity_percent
        if capacity is not None and 0 < capacity <= 100:
            return HealthReading(capacity, "upower-capacity", capacity, 100, "%")
    return None


class HealthResolver:
    def __init__(self, ttl: float = 3600, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._identities: tuple[str, ...] = ()
        self._checked_at = float("-inf")
        self._reading: HealthReading | None = None

    def invalidate(self) -> None:
        self._checked_at = float("-inf")

    def resolve(self, batteries: Sequence[RawBatterySnapshot]) -> HealthReading | None:
        identities = tuple(item.identity for item in batteries)
        now = self.clock()
        if identities != self._identities or now - self._checked_at >= self.ttl:
            self._identities = identities
            self._reading = resolve_health(batteries)
            self._checked_at = now
        return self._reading


def _normalize_profile(value: str) -> str | None:
    value = value.strip().strip('"').lower()
    if not value:
        return None
    candidate = value.replace("_", "-")
    return candidate if candidate in {"power-saver", "balanced", "performance"} else value


class PowerProfileResolver:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 root: Path = Path("/"), ttl: float = 10,
                 clock: Callable[[], float] = time.monotonic):
        self.runner, self.root, self.ttl, self.clock = runner, root, ttl, clock
        self._checked_at = float("-inf")
        self._reading: ProfileReading | None = None

    def invalidate(self) -> None:
        self._checked_at = float("-inf")

    def resolve(self) -> ProfileReading | None:
        now = self.clock()
        if now - self._checked_at >= self.ttl:
            self._reading = self._read()
            self._checked_at = now
        return self._reading

    def _run(self, command: list[str]) -> str | None:
        try:
            result = self.runner(command, text=True, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def _read(self) -> ProfileReading | None:
        output = self._run(["busctl", "--system", "get-property", "org.freedesktop.UPower.PowerProfiles",
                            "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles",
                            "ActiveProfile"])
        if output:
            match = re.fullmatch(r's\s+"(.*)"', output)
            profile = _normalize_profile(match.group(1) if match else output)
            if profile:
                return ProfileReading(profile, "power-profiles-daemon-dbus")

        if output := self._run(["powerprofilesctl", "get"]):
            if profile := _normalize_profile(output.splitlines()[0]):
                return ProfileReading(profile, "powerprofilesctl")

        candidates = list((self.root / "sys/class/platform-profile").glob("platform-profile-*/profile"))
        candidates.append(self.root / "sys/firmware/acpi/platform_profile")
        for path in candidates:
            try:
                value = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if profile := _normalize_profile(value):
                return ProfileReading(profile, "kernel-platform-profile")
        return None

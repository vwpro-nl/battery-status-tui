"""Battery measurement sources with UPower first and sysfs fallback."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from .models import Measurement


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot produce a system-battery measurement."""


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", value)
    return float(match.group()) if match else None


def _seconds(value: str | None) -> int | None:
    if not value or value.strip().lower() in {"unknown", "0 seconds", "0"}:
        return None
    total = 0.0
    for amount, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(seconds?|minutes?|hours?|days?)", value):
        scale = 1 if unit.startswith("second") else 60 if unit.startswith("minute") else 3600 if unit.startswith("hour") else 86400
        total += float(amount) * scale
    return int(total) if total > 0 else None


def _state(value: str | None) -> str:
    normalized = (value or "unknown").strip().lower()
    return {
        "fully-charged": "full",
        "pending-charge": "charging",
        "pending-discharge": "discharging",
        "empty": "empty",
    }.get(normalized, normalized)


def _parse_upower_devices(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in output.splitlines():
        if raw.startswith("Device:"):
            current = {"object-path": raw.partition(":")[2].strip()}
            devices.append(current)
            continue
        if current is None or ":" not in raw:
            continue
        key, _, value = raw.strip().partition(":")
        if key and value:
            current[key.lower()] = value.strip().strip("'")
    return devices


class UPowerSource:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.runner = runner

    def read(self, now: int | None = None) -> Measurement:
        try:
            result = self.runner(
                ["upower", "--dump"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SourceUnavailable(f"UPower unavailable: {error}") from error
        if result.returncode != 0:
            raise SourceUnavailable(result.stderr.strip() or "UPower returned no data")

        devices = _parse_upower_devices(result.stdout)
        battery = next(
            (
                item
                for item in devices
                if item.get("power supply") == "yes"
                and "percentage" in item
                and item.get("native-path")
            ),
            None,
        )
        if battery is None:
            raise SourceUnavailable("UPower found no system battery")
        line_power = next((item for item in devices if "online" in item and "percentage" not in item), None)
        percentage = _number(battery.get("percentage"))
        if percentage is None:
            raise SourceUnavailable("UPower battery percentage is missing")

        online = None if line_power is None else line_power.get("online") == "yes"
        return Measurement(
            timestamp=int(time.time()) if now is None else now,
            percentage=percentage,
            state=_state(battery.get("state")),
            ac_online=online,
            power_w=_number(battery.get("energy-rate")),
            voltage_v=_number(battery.get("voltage")),
            time_to_empty_s=_seconds(battery.get("time to empty")),
            time_to_full_s=_seconds(battery.get("time to full")),
            energy_wh=_number(battery.get("energy")),
            energy_full_wh=_number(battery.get("energy-full")),
            energy_full_design_wh=_number(battery.get("energy-full-design")),
            cycle_count=int(value) if (value := _number(battery.get("charge-cycles"))) is not None else None,
            source="upower",
            device=battery.get("native-path", "battery"),
        )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _micro(path: Path) -> float | None:
    value = _number(_read(path))
    return value / 1_000_000 if value is not None else None


class SysfsSource:
    def __init__(self, root: Path = Path("/sys/class/power_supply")):
        self.root = root

    def read(self, now: int | None = None) -> Measurement:
        batteries: list[Path] = []
        mains: list[Path] = []
        try:
            supplies = list(self.root.iterdir())
        except OSError as error:
            raise SourceUnavailable(f"sysfs unavailable: {error}") from error
        for supply in supplies:
            supply_type = (_read(supply / "type") or "").lower()
            scope = (_read(supply / "scope") or "System").lower()
            if supply_type == "battery" and scope != "device" and _read(supply / "present") != "0":
                batteries.append(supply)
            elif supply_type in {"mains", "usb", "usb_c"}:
                mains.append(supply)
        if not batteries:
            raise SourceUnavailable("sysfs found no system battery")

        battery = batteries[0]
        percentage = _number(_read(battery / "capacity"))
        if percentage is None:
            raise SourceUnavailable("sysfs battery capacity is missing")
        voltage = _micro(battery / "voltage_now")
        current = _micro(battery / "current_now")
        power = _micro(battery / "power_now")
        if power is None and voltage is not None and current is not None:
            power = voltage * current

        energy = _micro(battery / "energy_now")
        energy_full = _micro(battery / "energy_full")
        energy_design = _micro(battery / "energy_full_design")
        if energy is None and voltage is not None:
            charge = _micro(battery / "charge_now")
            energy = charge * voltage if charge is not None else None
        if energy_full is None:
            charge_full = _micro(battery / "charge_full")
            energy_full = charge_full * voltage if charge_full is not None and voltage is not None else None
        if energy_design is None:
            charge_design = _micro(battery / "charge_full_design")
            energy_design = charge_design * voltage if charge_design is not None and voltage is not None else None

        online_values = [_read(item / "online") for item in mains]
        online = None if not online_values else any(value == "1" for value in online_values)
        cycle = _number(_read(battery / "cycle_count"))
        return Measurement(
            timestamp=int(time.time()) if now is None else now,
            percentage=percentage,
            state=_state(_read(battery / "status")),
            ac_online=online,
            power_w=power,
            voltage_v=voltage,
            current_a=current,
            time_to_empty_s=int(value) if (value := _number(_read(battery / "time_to_empty_now"))) else None,
            time_to_full_s=int(value) if (value := _number(_read(battery / "time_to_full_now"))) else None,
            energy_wh=energy,
            energy_full_wh=energy_full,
            energy_full_design_wh=energy_design,
            cycle_count=int(cycle) if cycle is not None else None,
            source="sysfs",
            device=battery.name,
        )


class BatterySource:
    def __init__(self, upower: UPowerSource | None = None, sysfs: SysfsSource | None = None):
        self.upower = upower or UPowerSource()
        self.sysfs = sysfs or SysfsSource()

    def read(self, now: int | None = None) -> Measurement:
        try:
            return self.upower.read(now)
        except SourceUnavailable:
            return self.sysfs.read(now)


"""Field-level fusion of UPower and Linux power-supply snapshots."""
from __future__ import annotations
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable
from .models import Measurement, RawBatterySnapshot
from .power import PowerResolver

class SourceUnavailable(RuntimeError):
    pass

def _number(value: str | None) -> float | None:
    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", value or "")
    return float(match.group()) if match else None

def _seconds(value: str | None) -> int | None:
    if not value or value.strip().lower() in {"unknown", "0 seconds", "0"}:
        return None
    scales = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    total = sum(float(n) * scales[next(k for k in scales if unit.startswith(k))]
                for n, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(seconds?|minutes?|hours?|days?)", value))
    return int(total) if total > 0 else None

def _state(value: str | None) -> str:
    normalized = (value or "unknown").strip().lower()
    return {"fully-charged": "full", "pending-charge": "charging", "pending-discharge": "discharging", "empty": "empty"}.get(normalized, normalized)

def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

def _micro(path: Path) -> float | None:
    value = _number(_read(path))
    return value / 1_000_000 if value is not None else None

def _boot_id() -> str:
    return _read(Path("/proc/sys/kernel/random/boot_id")) or "unknown"

def _clocks() -> tuple[float, float]:
    mono = time.clock_gettime(time.CLOCK_MONOTONIC)
    return mono, time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))

def _parse_upower_devices(output: str) -> list[dict[str, str]]:
    devices, current = [], None
    for raw in output.splitlines():
        if raw.startswith("Device:"):
            current = {"object-path": raw.partition(":")[2].strip()}
            devices.append(current)
        elif current is not None and ":" in raw:
            key, _, value = raw.strip().partition(":")
            if key and value:
                current[key.lower()] = value.strip().strip("'")
    return devices

class UPowerSource:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.runner = runner

    def read_fields(self) -> tuple[dict[str, dict[str, str]], bool | None]:
        try:
            result = self.runner(["upower", "--dump"], text=True, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise SourceUnavailable(f"UPower unavailable: {error}") from error
        if result.returncode != 0:
            raise SourceUnavailable(result.stderr.strip() or "UPower returned no data")
        devices = _parse_upower_devices(result.stdout)
        batteries = {item["native-path"]: item for item in devices if item.get("power supply") == "yes" and item.get("native-path")}
        lines = [item.get("online") for item in devices if "online" in item and "percentage" not in item]
        return batteries, None if not lines else any(value == "yes" for value in lines)

    def read(self, now: int | None = None) -> Measurement:
        fields, online = self.read_fields()
        if not fields:
            raise SourceUnavailable("UPower found no system battery")
        item = next(iter(fields.values()))
        percentage = _number(item.get("percentage"))
        if percentage is None:
            raise SourceUnavailable("UPower battery percentage is missing")
        state, rate = _state(item.get("state")), _number(item.get("energy-rate"))
        if state in {"charging", "discharging"} and (rate is None or rate <= 0):
            rate = None
        return Measurement(int(time.time()) if now is None else now, percentage, state, online, power_w=rate,
            voltage_v=_number(item.get("voltage")), time_to_empty_s=_seconds(item.get("time to empty")),
            time_to_full_s=_seconds(item.get("time to full")), energy_wh=_number(item.get("energy")),
            energy_full_wh=_number(item.get("energy-full")), energy_full_design_wh=_number(item.get("energy-full-design")),
            cycle_count=int(v) if (v := _number(item.get("charge-cycles"))) is not None else None,
            source="upower", device=item.get("native-path", "battery"),
            power_method="upower-energy-rate" if rate is not None else "unavailable")

    def read_raw(self, now: int | None = None) -> tuple[RawBatterySnapshot, ...]:
        timestamp, (mono, boot) = int(time.time()) if now is None else now, _clocks()
        fields, online = self.read_fields()
        snapshots = []
        for device, item in fields.items():
            percentage = _number(item.get("percentage"))
            if percentage is None:
                continue
            state, rate = _state(item.get("state")), _number(item.get("energy-rate"))
            if state in {"charging", "discharging"} and (rate is None or rate <= 0):
                rate = None
            identity = "|".join((device, item.get("model", ""), item.get("serial", "")))
            snapshots.append(RawBatterySnapshot(timestamp, mono, boot, _boot_id(), device, identity,
                percentage, state, online, voltage_now_v=_number(item.get("voltage")),
                energy_now_wh=_number(item.get("energy")), energy_full_wh=_number(item.get("energy-full")),
                energy_full_design_wh=_number(item.get("energy-full-design")), upower_energy_rate_w=rate,
                time_to_empty_s=_seconds(item.get("time to empty")), time_to_full_s=_seconds(item.get("time to full")),
                cycle_count=int(v) if (v := _number(item.get("charge-cycles"))) is not None else None,
                sources=("upower",), voltage_design_v=_number(item.get("voltage-min-design")),
                upower_energy_full_wh=_number(item.get("energy-full")),
                upower_energy_full_design_wh=_number(item.get("energy-full-design")),
                upower_capacity_percent=_number(item.get("capacity"))))
        if not snapshots:
            raise SourceUnavailable("UPower found no system battery")
        return tuple(snapshots)

class SysfsSource:
    def __init__(self, root: Path = Path("/sys/class/power_supply")):
        self.root = root

    def read_raw(self, now: int | None = None) -> tuple[RawBatterySnapshot, ...]:
        timestamp, (mono, boot) = int(time.time()) if now is None else now, _clocks()
        try:
            supplies = list(self.root.iterdir())
        except OSError as error:
            raise SourceUnavailable(f"sysfs unavailable: {error}") from error
        batteries, mains = [], []
        for supply in supplies:
            kind = (_read(supply / "type") or "").lower()
            if kind == "battery" and (_read(supply / "scope") or "system").lower() != "device" and _read(supply / "present") != "0":
                batteries.append(supply)
            elif kind in {"mains", "usb", "usb_c"}:
                mains.append(supply)
        online_values = [_read(item / "online") for item in mains]
        online = None if not online_values else any(value == "1" for value in online_values)
        snapshots = []
        for battery in sorted(batteries):
            percentage = _number(_read(battery / "capacity"))
            if percentage is None:
                continue
            identity = "|".join((battery.name, _read(battery / "model_name") or "", _read(battery / "serial_number") or ""))
            cycle = _number(_read(battery / "cycle_count"))
            snapshots.append(RawBatterySnapshot(timestamp, mono, boot, _boot_id(), battery.name, identity, percentage,
                _state(_read(battery / "status")), online, power_now_w=_micro(battery / "power_now"),
                current_now_a=_micro(battery / "current_now"), voltage_now_v=_micro(battery / "voltage_now"),
                energy_now_wh=_micro(battery / "energy_now"), energy_full_wh=_micro(battery / "energy_full"),
                energy_full_design_wh=_micro(battery / "energy_full_design"), charge_now_ah=_micro(battery / "charge_now"),
                charge_full_ah=_micro(battery / "charge_full"), charge_full_design_ah=_micro(battery / "charge_full_design"),
                time_to_empty_s=int(v) if (v := _number(_read(battery / "time_to_empty_now"))) else None,
                time_to_full_s=int(v) if (v := _number(_read(battery / "time_to_full_now"))) else None,
                cycle_count=int(cycle) if cycle is not None else None, sources=("sysfs",),
                voltage_design_v=_micro(battery / "voltage_min_design")))
        if not snapshots:
            raise SourceUnavailable("sysfs found no system battery")
        return tuple(snapshots)

    def read(self, now: int | None = None) -> Measurement:
        return aggregate(self.read_raw(now), PowerResolver())

def aggregate(raw: tuple[RawBatterySnapshot, ...], resolver: PowerResolver,
              history: tuple[RawBatterySnapshot, ...] = (), sleep_intervals: tuple[tuple[int, int], ...] = ()) -> Measurement:
    readings = [resolver.resolve(item, history, sleep_intervals) for item in raw]
    powers = [reading.watts for reading in readings]
    power = sum(value for value in powers if value is not None) if all(value is not None for value in powers) else None
    full = [item.energy_full_wh or ((item.charge_full_ah or 0) * (item.voltage_now_v or 0)) for item in raw]
    weights = full if sum(full) > 0 else [1.0] * len(raw)
    percentage = sum(item.percentage * weight for item, weight in zip(raw, weights)) / sum(weights)
    state = "charging" if any(item.state == "charging" for item in raw) else "discharging" if any(item.state == "discharging" for item in raw) else raw[0].state
    methods, approximate = {item.method for item in readings}, any(item.approximate for item in readings)
    method = next(iter(methods)) if len(methods) == 1 else "mixed:" + "+".join(sorted(methods))
    def total(values: list[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return sum(present) if present else None
    voltages = [item.voltage_now_v for item in raw if item.voltage_now_v is not None]
    return Measurement(raw[0].timestamp, percentage, state, raw[0].ac_online, power_w=power,
        voltage_v=sum(voltages) / len(voltages) if voltages else None,
        current_a=sum(item.current_now_a for item in raw if item.current_now_a is not None) if all(item.current_now_a is not None for item in raw) else None,
        time_to_empty_s=max((item.time_to_empty_s for item in raw if item.time_to_empty_s), default=None),
        time_to_full_s=max((item.time_to_full_s for item in raw if item.time_to_full_s), default=None),
        energy_wh=total([item.energy_now_wh if item.energy_now_wh is not None else
                         item.charge_now_ah * item.voltage_now_v if item.charge_now_ah is not None and item.voltage_now_v is not None else None for item in raw]),
        energy_full_wh=total([value if value > 0 else None for value in full]),
        energy_full_design_wh=total([item.energy_full_design_wh if item.energy_full_design_wh is not None else
            item.charge_full_design_ah * item.voltage_now_v if item.charge_full_design_ah is not None and item.voltage_now_v is not None else None for item in raw]),
        cycle_count=max((item.cycle_count for item in raw if item.cycle_count is not None), default=None),
        source="+".join(sorted({source for item in raw for source in item.sources})), device=",".join(item.device for item in raw),
        power_method=method, power_approximate=approximate, power_confidence="medium" if approximate else "high" if power is not None else "none",
        power_window_s=max((item.window_s or 0 for item in readings), default=0) or None,
        charge_ah=total([item.charge_now_ah for item in raw]), charge_full_ah=total([item.charge_full_ah for item in raw]),
        charge_full_design_ah=total([item.charge_full_design_ah for item in raw]), monotonic_s=raw[0].monotonic_s,
        boottime_s=raw[0].boottime_s, boot_id=raw[0].boot_id, battery_identity=";".join(item.identity for item in raw), raw_batteries=raw)

class BatterySource:
    def __init__(self, upower: UPowerSource | None = None, sysfs: SysfsSource | None = None, resolver: PowerResolver | None = None):
        self.upower, self.sysfs, self.resolver = upower or UPowerSource(), sysfs or SysfsSource(), resolver or PowerResolver()

    def read_raw(self, now: int | None = None) -> tuple[RawBatterySnapshot, ...]:
        try:
            raw = self.sysfs.read_raw(now)
        except SourceUnavailable:
            return self.upower.read_raw(now)
        try:
            fields, upower_online = self.upower.read_fields()
        except SourceUnavailable:
            return raw
        merged = []
        for item in raw:
            up = fields.get(item.device)
            if up is None:
                merged.append(item)
                continue
            rate = _number(up.get("energy-rate"))
            if item.state in {"charging", "discharging"} and (rate is None or rate <= 0):
                rate = None
            merged.append(replace(item, ac_online=item.ac_online if item.ac_online is not None else upower_online,
                upower_energy_rate_w=rate, time_to_empty_s=_seconds(up.get("time to empty")) or item.time_to_empty_s,
                time_to_full_s=_seconds(up.get("time to full")) or item.time_to_full_s,
                upower_energy_full_wh=_number(up.get("energy-full")),
                upower_energy_full_design_wh=_number(up.get("energy-full-design")),
                upower_capacity_percent=_number(up.get("capacity")), sources=("sysfs", "upower")))
        return tuple(merged)

    def read(self, now: int | None = None, history: tuple[RawBatterySnapshot, ...] = (),
             sleep_intervals: tuple[tuple[int, int], ...] = ()) -> Measurement:
        return aggregate(self.read_raw(now), self.resolver, history, sleep_intervals)

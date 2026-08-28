"""Locked binary format for temporary checkpoint recent-series data."""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Sequence


MAGIC = b"BRS1"
FORMAT_VERSION = 1
MAX_WINDOW_MS = 8 * 60 * 60 * 1_000
UNKNOWN_POWER_MW = -(2 ** 31)
UNKNOWN_PROFILE_INDEX = 0xFF

HEADER = struct.Struct("<4sBBHHBHqII")
RECORD = struct.Struct("<IIidBBHH")
LENGTH = struct.Struct("<H")

# Flag layout v1: AC state in bits 0..1, power method in 2..4,
# approximate in 5, confidence in 6..7, break-before in 8, and the
# discontinuity reason in 9..14. Bit 15 marks a valid energy delta.
AC_MASK = 0x0003
POWER_METHOD_MASK = 0x001C
POWER_METHOD_SHIFT = 2
POWER_CONFIDENCE_MASK = 0x00C0
POWER_CONFIDENCE_SHIFT = 6
ENERGY_VALID = 0x8000
MAX_POWER_METHOD = 5
MAX_POWER_CONFIDENCE = 2


class BatteryState(IntEnum):
    UNKNOWN = 0
    CHARGING = 1
    DISCHARGING = 2
    FULL = 3
    OTHER = 4


class RecentSeriesError(ValueError):
    """The recent-series payload violates the versioned binary contract."""


@dataclass(frozen=True, slots=True)
class RecentPoint:
    timestamp_ms: int
    soc_millipercent: int
    resolved_power_mw: int | None
    energy_delta_wh: float | None
    battery_state: BatteryState
    profile: str | None
    battery_set_key: str
    flags: int = 0


def payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _dictionary(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if any(not value for value in result):
        raise RecentSeriesError("dictionary values must be non-empty")
    return result


def _encode_dictionary(values: Sequence[str]) -> bytes:
    encoded = bytearray()
    for value in values:
        raw = value.encode("utf-8")
        if not raw or len(raw) > 0xFFFF:
            raise RecentSeriesError("dictionary value has invalid UTF-8 byte length")
        encoded.extend(LENGTH.pack(len(raw)))
        encoded.extend(raw)
    return bytes(encoded)


def _validate_flags(flags: int) -> None:
    if not 0 <= flags <= 0xFFFF:
        raise RecentSeriesError("flags do not fit uint16")
    if flags & AC_MASK == AC_MASK:
        raise RecentSeriesError("invalid AC-state flag enum")
    if (flags & POWER_METHOD_MASK) >> POWER_METHOD_SHIFT > MAX_POWER_METHOD:
        raise RecentSeriesError("invalid power-method flag enum")
    if (flags & POWER_CONFIDENCE_MASK) >> POWER_CONFIDENCE_SHIFT > MAX_POWER_CONFIDENCE:
        raise RecentSeriesError("invalid power-confidence flag enum")


def encode_recent_series(points: Sequence[RecentPoint]) -> bytes:
    """Encode ordered recent polls into deterministic recent-series v1 bytes."""
    if len(points) > 0xFFFF:
        raise RecentSeriesError("too many recent-series points")
    if not points:
        return HEADER.pack(MAGIC, FORMAT_VERSION, 0, RECORD.size, 0, 0, 0, 0, 0, 0)

    timestamps = [point.timestamp_ms for point in points]
    if timestamps[0] < 0 or any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise RecentSeriesError("timestamps must be non-negative and strictly increasing")
    if timestamps[-1] - timestamps[0] > MAX_WINDOW_MS:
        raise RecentSeriesError("recent-series exceeds eight hours")

    profiles = _dictionary([point.profile for point in points if point.profile is not None])
    battery_sets = _dictionary([point.battery_set_key for point in points])
    if len(profiles) > UNKNOWN_PROFILE_INDEX:
        raise RecentSeriesError("too many profile dictionary entries")
    if len(battery_sets) > 0xFFFF:
        raise RecentSeriesError("too many battery-set dictionary entries")
    profile_indexes = {value: index for index, value in enumerate(profiles)}
    battery_indexes = {value: index for index, value in enumerate(battery_sets)}
    dictionary = _encode_dictionary(profiles) + _encode_dictionary(battery_sets)

    base_timestamp = timestamps[0]
    records = bytearray()
    for point in points:
        delta = point.timestamp_ms - base_timestamp
        if not 0 <= delta <= 0xFFFFFFFF:
            raise RecentSeriesError("timestamp delta does not fit uint32")
        if not 0 <= point.soc_millipercent <= 100_000:
            raise RecentSeriesError("SoC must be in 0..100000 millipercent")
        if point.resolved_power_mw is None:
            power = UNKNOWN_POWER_MW
        else:
            power = point.resolved_power_mw
            if not 0 <= power < 2 ** 31:
                raise RecentSeriesError("resolved power must be a non-negative int32")
        try:
            state = BatteryState(point.battery_state)
        except ValueError as error:
            raise RecentSeriesError("invalid battery-state enum") from error
        flags = point.flags
        if point.energy_delta_wh is None:
            energy = 0.0
            flags &= ~ENERGY_VALID
        else:
            energy = point.energy_delta_wh
            if not math.isfinite(energy):
                raise RecentSeriesError("energy delta must be finite")
            flags |= ENERGY_VALID
        _validate_flags(flags)
        profile_index = (UNKNOWN_PROFILE_INDEX if point.profile is None
                         else profile_indexes[point.profile])
        records.extend(RECORD.pack(
            delta, point.soc_millipercent, power, energy, int(state), profile_index,
            battery_indexes[point.battery_set_key], flags,
        ))

    header = HEADER.pack(
        MAGIC, FORMAT_VERSION, 0, RECORD.size, len(points), len(profiles),
        len(battery_sets), base_timestamp, len(dictionary), len(records),
    )
    return header + dictionary + records


def _decode_dictionary(payload: bytes, offset: int, count: int, end: int) -> tuple[tuple[str, ...], int]:
    values = []
    for _ in range(count):
        if offset + LENGTH.size > end:
            raise RecentSeriesError("truncated dictionary length")
        length = LENGTH.unpack_from(payload, offset)[0]
        offset += LENGTH.size
        if length == 0 or offset + length > end:
            raise RecentSeriesError("invalid dictionary value length")
        try:
            value = payload[offset:offset + length].decode("utf-8")
        except UnicodeDecodeError as error:
            raise RecentSeriesError("dictionary contains invalid UTF-8") from error
        values.append(value)
        offset += length
    if values != sorted(set(values)):
        raise RecentSeriesError("dictionary is not canonical")
    return tuple(values), offset


def decode_recent_series(payload: bytes) -> tuple[RecentPoint, ...]:
    """Strictly decode and validate one complete recent-series v1 payload."""
    if len(payload) < HEADER.size:
        raise RecentSeriesError("truncated recent-series header")
    (magic, version, header_flags, record_size, point_count, profile_count,
     battery_count, base_timestamp, dictionary_size, records_size) = HEADER.unpack_from(payload)
    if magic != MAGIC:
        raise RecentSeriesError("invalid recent-series magic")
    if version != FORMAT_VERSION:
        raise RecentSeriesError("unsupported recent-series version")
    if header_flags != 0:
        raise RecentSeriesError("unsupported recent-series header flags")
    if record_size != RECORD.size:
        raise RecentSeriesError("invalid recent-series record size")
    if records_size != point_count * RECORD.size:
        raise RecentSeriesError("record count and byte length disagree")
    if len(payload) != HEADER.size + dictionary_size + records_size:
        raise RecentSeriesError("recent-series payload length mismatch")
    if point_count == 0:
        if profile_count or battery_count or base_timestamp or dictionary_size or records_size:
            raise RecentSeriesError("non-canonical empty recent-series payload")
        return ()
    if base_timestamp < 0 or battery_count == 0:
        raise RecentSeriesError("invalid recent-series base or battery dictionary")

    dictionary_end = HEADER.size + dictionary_size
    profiles, offset = _decode_dictionary(payload, HEADER.size, profile_count, dictionary_end)
    battery_sets, offset = _decode_dictionary(payload, offset, battery_count, dictionary_end)
    if offset != dictionary_end:
        raise RecentSeriesError("unconsumed dictionary bytes")

    points = []
    previous_timestamp = -1
    records_offset = dictionary_end
    for index in range(point_count):
        (delta, soc, power, energy, state_raw, profile_index,
         battery_index, flags) = RECORD.unpack_from(payload, records_offset + index * RECORD.size)
        if index == 0 and delta != 0:
            raise RecentSeriesError("first timestamp delta must be zero")
        timestamp = base_timestamp + delta
        if timestamp <= previous_timestamp:
            raise RecentSeriesError("timestamps are not strictly increasing")
        if timestamp - base_timestamp > MAX_WINDOW_MS:
            raise RecentSeriesError("recent-series exceeds eight hours")
        if soc > 100_000:
            raise RecentSeriesError("SoC is outside 0..100000 millipercent")
        if power != UNKNOWN_POWER_MW and power < 0:
            raise RecentSeriesError("resolved power must be non-negative")
        try:
            state = BatteryState(state_raw)
        except ValueError as error:
            raise RecentSeriesError("invalid battery-state enum") from error
        if profile_index != UNKNOWN_PROFILE_INDEX and profile_index >= len(profiles):
            raise RecentSeriesError("profile dictionary index is out of range")
        if battery_index >= len(battery_sets):
            raise RecentSeriesError("battery-set dictionary index is out of range")
        if not math.isfinite(energy):
            raise RecentSeriesError("energy delta must be finite")
        _validate_flags(flags)
        energy_value = energy if flags & ENERGY_VALID else None
        if energy_value is None and energy != 0:
            raise RecentSeriesError("invalid energy value without validity flag")
        points.append(RecentPoint(
            timestamp, soc, None if power == UNKNOWN_POWER_MW else power,
            energy_value, state,
            None if profile_index == UNKNOWN_PROFILE_INDEX else profiles[profile_index],
            battery_sets[battery_index], flags & ~ENERGY_VALID,
        ))
        previous_timestamp = timestamp
    return tuple(points)

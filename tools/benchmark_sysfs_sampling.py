#!/usr/bin/env python3
"""Capture and compare read-only sysfs battery samples without UPower or SQLite."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import resource
import statistics
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from battery_status_tui.power import PowerResolver, is_sysfs_direct_method
from battery_status_tui.live import LiveOperatingMode, classify_live_mode
from battery_status_tui.sources import SourceUnavailable, SysfsSource, aggregate


REFERENCE_INTERVAL = 0.2
TARGET_INTERVALS = (0.2, 0.5, 1.0, 2.0, 5.0, 10.0)


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def stats(values: Iterable[float]) -> dict[str, float | None]:
    materialized = list(values)
    return {
        "min": min(materialized) if materialized else None,
        "median": statistics.median(materialized) if materialized else None,
        "mean": statistics.mean(materialized) if materialized else None,
        "p95": percentile(materialized, 0.95),
        "p99": percentile(materialized, 0.99),
        "max": max(materialized) if materialized else None,
    }


def _raw_dict(item: Any) -> dict[str, Any]:
    return {
        "device": item.device,
        "identity": item.identity,
        "state": item.state,
        "ac_online": item.ac_online,
        "percentage": item.percentage,
        "power_now_w": item.power_now_w,
        "current_now_a": item.current_now_a,
        "voltage_now_v": item.voltage_now_v,
        "energy_now_wh": item.energy_now_wh,
        "charge_now_ah": item.charge_now_ah,
    }


def capture(output: Path, duration: float, interval: float) -> None:
    source = SysfsSource()
    resolver = PowerResolver()
    started_wall = time.time()
    started_mono = time.monotonic()
    started_cpu = time.process_time()
    deadline = started_mono
    deadline_misses = 0
    sample_count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", buffering=1) as stream:
        stream.write(json.dumps({
            "kind": "header", "interval_s": interval, "duration_target_s": duration,
            "started_wall": started_wall, "pid": os.getpid(),
        }, separators=(",", ":")) + "\n")
        try:
            while True:
                now = time.monotonic()
                if now < deadline:
                    time.sleep(deadline - now)
                read_started = time.monotonic()
                if read_started - started_mono >= duration and sample_count:
                    break
                lateness = read_started - deadline
                skipped = max(0, math.floor(lateness / interval))
                if skipped:
                    deadline_misses += skipped
                    deadline += skipped * interval
                    lateness = read_started - deadline
                wall = time.time()
                error = None
                raws = ()
                measurement = None
                try:
                    raws = source.read_raw(int(wall))
                    measurement = aggregate(raws, resolver, direct_only=True)
                except SourceUnavailable as caught:
                    error = str(caught)
                read_finished = time.monotonic()
                record = {
                    "kind": "sample",
                    "index": sample_count,
                    "scheduled_mono": deadline,
                    "mono": read_started,
                    "wall": wall,
                    "lateness_s": lateness,
                    "read_latency_s": read_finished - read_started,
                    "error": error,
                    "raw": [_raw_dict(item) for item in raws],
                    "aggregate": None if measurement is None else {
                        "ac_online": measurement.ac_online,
                        "state": measurement.state,
                        "session_kind": measurement.session_kind,
                        "percentage": measurement.percentage,
                        "current_a": measurement.current_a,
                        "voltage_v": measurement.voltage_v,
                        "power_w": measurement.power_w,
                        "power_method": measurement.power_method,
                        "power_approximate": measurement.power_approximate,
                        "power_confidence": measurement.power_confidence,
                        "energy_wh": measurement.energy_wh,
                        "charge_ah": measurement.charge_ah,
                        "battery_identity": measurement.battery_identity,
                    },
                }
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                sample_count += 1
                deadline += interval
        finally:
            ended_mono = time.monotonic()
            summary = {
                "kind": "summary",
                "samples": sample_count,
                "wall_duration_s": ended_mono - started_mono,
                "process_cpu_s": time.process_time() - started_cpu,
                "deadline_misses": deadline_misses,
                "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }
            stream.write(json.dumps(summary, separators=(",", ":")) + "\n")
    print(json.dumps(summary, indent=2))


def load_capture(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    header = next(row for row in rows if row["kind"] == "header")
    samples = [row for row in rows if row["kind"] == "sample" and row["aggregate"] is not None]
    summary = next((row for row in reversed(rows) if row["kind"] == "summary"), {})
    return header, samples, summary


def primary_field(sample: dict[str, Any], field: str) -> Any:
    raw = sample["raw"]
    if field == "current_now":
        values = [item["current_now_a"] for item in raw]
        return sum(values) if values and all(value is not None for value in values) else None
    if field == "voltage_now":
        values = [item["voltage_now_v"] for item in raw if item["voltage_now_v"] is not None]
        return statistics.mean(values) if values else None
    if field == "power_now":
        values = [item["power_now_w"] for item in raw]
        return sum(values) if values and all(value is not None for value in values) else None
    mapping = {"ac_online": "ac_online", "status": "state", "soc": "percentage"}
    return sample["aggregate"][mapping[field]]


def update_analysis(samples: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [primary_field(sample, field) for sample in samples]
    identical = sum(left == right for left, right in zip(values, values[1:]))
    changes = [index for index in range(1, len(values)) if values[index] != values[index - 1]]
    change_times = [samples[index]["mono"] for index in changes]
    intervals = [right - left for left, right in zip(change_times, change_times[1:])]
    return {
        "observations": len(values),
        "available": sum(value is not None for value in values),
        "available_percent": 100 * sum(value is not None for value in values) / len(values),
        "identical_previous": identical,
        "identical_percent": 100 * identical / max(1, len(values) - 1),
        "changes": len(changes),
        "change_interval_median_s": statistics.median(intervals) if intervals else None,
        "change_interval_p95_s": percentile(intervals, 0.95),
        "change_interval_max_s": max(intervals) if intervals else None,
        "distinct_values": len({json.dumps(value, sort_keys=True) for value in values}),
    }


def downsample(samples: list[dict[str, Any]], interval: float) -> list[dict[str, Any]]:
    """Select the first captured observation at/after each ideal target instant."""
    if interval == REFERENCE_INTERVAL:
        return list(samples)
    times = [sample["mono"] for sample in samples]
    selected = []
    target = times[0]
    end = times[-1]
    last_index = -1
    while target <= end:
        index = bisect.bisect_left(times, target)
        if index >= len(samples):
            break
        if index != last_index:
            selected.append(samples[index])
            last_index = index
        target += interval
    return selected


def held_comparison(reference: list[dict[str, Any]], lower: list[dict[str, Any]],
                    field: str = "power_w") -> dict[str, Any]:
    lower_times = [sample["mono"] for sample in lower]
    differences = []
    relatives = []
    for sample in reference:
        index = bisect.bisect_right(lower_times, sample["mono"]) - 1
        if index < 0:
            continue
        expected = sample["aggregate"][field]
        observed = lower[index]["aggregate"][field]
        if expected is None or observed is None:
            continue
        difference = abs(observed - expected)
        differences.append(difference)
        if abs(expected) >= 0.05:
            relatives.append(difference / abs(expected) * 100)
    result = stats(differences)
    result.update({
        "relative_mean_percent": statistics.mean(relatives) if relatives else None,
        "relative_p95_percent": percentile(relatives, 0.95),
        "compared": len(differences),
        "reference_changes": sum(
            left["aggregate"][field] != right["aggregate"][field]
            for left, right in zip(reference, reference[1:])
        ),
        "selected_changes": sum(
            left["aggregate"][field] != right["aggregate"][field]
            for left, right in zip(lower, lower[1:])
        ),
    })
    return result


def smooth(samples: list[dict[str, Any]], interval: float) -> list[tuple[float, float | None]]:
    median_horizon_s = 4.0
    values: deque[tuple[float, float]] = deque()
    ewma = None
    last_valid_at = None
    last_time = None
    last_mode = None
    last_ac = None
    last_identity = None
    idle_since = None
    idle_count = 0
    last_idle_at = None
    minimum_idle_count = max(2, math.floor(4.0 / interval) + 1)
    result = []
    for sample in samples:
        item = sample["aggregate"]
        mode = classify_live_mode(item["ac_online"], item["state"],
                                  tuple(raw["state"] for raw in sample["raw"]))
        gap = None if last_time is None else sample["mono"] - last_time
        discontinuity = (gap is not None and (gap < 0 or gap > 2.5 * interval))
        identity_changed = (last_identity is not None
                            and item["battery_identity"] != last_identity)
        mode_changed = last_mode is not None and mode != last_mode
        plug_changed = last_mode is not None and item["ac_online"] != last_ac
        if discontinuity or identity_changed or mode_changed or plug_changed:
            values.clear()
            ewma = None
            last_valid_at = None
            idle_since = None
            idle_count = 0
            last_idle_at = None
        value = item["power_w"]
        direct = is_sysfs_direct_method(item["power_method"])
        if mode in {LiveOperatingMode.CHARGING, LiveOperatingMode.DISCHARGING} \
                and direct and value is not None and value >= 0.05:
            while values and sample["mono"] - values[0][0] > median_horizon_s:
                values.popleft()
            values.append((sample["mono"], value))
            median = statistics.median(item[1] for item in values)
            if ewma is None or last_valid_at is None:
                ewma = median
            else:
                alpha = 1 - (1 - 0.35) ** (sample["mono"] - last_valid_at)
                ewma = alpha * median + (1 - alpha) * ewma
            last_valid_at = sample["mono"]
        elif mode is LiveOperatingMode.CONNECTED_IDLE:
            zero = direct and value is not None and value < 0.05
            positive = direct and value is not None and value >= 0.05
            continuous = (last_idle_at is not None
                          and sample["mono"] - last_idle_at <= 2.5 * interval)
            if zero:
                if not continuous:
                    idle_since, idle_count = sample["mono"], 1
                else:
                    idle_count += 1
                last_idle_at = sample["mono"]
            elif positive:
                idle_since, idle_count, last_idle_at = None, 0, None
            elif (last_idle_at is not None
                  and sample["mono"] - last_idle_at > 2.5 * interval):
                idle_since, idle_count, last_idle_at = None, 0, None
            if (zero and idle_since is not None
                    and sample["mono"] - idle_since >= 4.0
                    and idle_count >= minimum_idle_count):
                ewma = 0.0
            else:
                ewma = None
        elif last_valid_at is None or sample["mono"] - last_valid_at > 5.0:
            ewma = None
        result.append((sample["mono"], ewma))
        last_time = sample["mono"]
        last_mode = mode
        last_ac = item["ac_online"]
        last_identity = item["battery_identity"]
    return result


def smooth_comparison(reference: list[dict[str, Any]], lower: list[dict[str, Any]],
                      interval: float) -> dict[str, Any]:
    ref_smoothed = smooth(reference, REFERENCE_INTERVAL)
    low_smoothed = smooth(lower, interval)
    low_times = [item[0] for item in low_smoothed]
    differences = []
    for timestamp, expected in ref_smoothed:
        index = bisect.bisect_right(low_times, timestamp) - 1
        if index >= 0 and expected is not None and low_smoothed[index][1] is not None:
            differences.append(abs(low_smoothed[index][1] - expected))
    result = stats(differences)
    result.update({
        "compared": len(differences),
        "median_horizon_s": 4.0,
        "nominal_median_samples": math.floor(4.0 / interval) + 1,
        "ewma_alpha": 1 - (1 - 0.35) ** interval,
    })
    return result


def transitions(samples: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    result = []
    for left, right in zip(samples, samples[1:]):
        before = left["aggregate"][field]
        after = right["aggregate"][field]
        if before != after:
            result.append({"time": right["mono"], "before": before, "after": after})
    return result


def transition_comparison(reference: list[dict[str, Any]], lower: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("ac_online", "state"):
        detected = []
        missed = 0
        lower_times = [sample["mono"] for sample in lower]
        reference_events = transitions(reference, field)
        for event_index, event in enumerate(reference_events):
            next_time = (reference_events[event_index + 1]["time"]
                         if event_index + 1 < len(reference_events) else float("inf"))
            index = bisect.bisect_left(lower_times, event["time"])
            while index < len(lower) and lower[index]["mono"] < next_time:
                if lower[index]["aggregate"][field] == event["after"]:
                    detected.append(lower[index]["mono"] - event["time"])
                    break
                index += 1
            else:
                missed += 1
        result[field] = {
            "reference_transitions": len(reference_events),
            "detected": len(detected),
            "missed": missed,
            "delay_s": stats(detected),
        }
    return result


def not_charging_episodes(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    episodes = []
    start = None
    selected: list[dict[str, Any]] = []
    for sample in samples + [None]:
        matches = bool(sample and sample["aggregate"]["ac_online"] is True
                       and sample["aggregate"]["state"] == "not charging")
        if matches:
            if start is None:
                start = sample["mono"]
                selected = []
            selected.append(sample)
        elif start is not None:
            end = selected[-1]["mono"]
            zeros = [item for item in selected if item["aggregate"]["power_w"] == 0]
            raw_zero_current = [item for item in selected if primary_field(item, "current_now") == 0]
            episodes.append({
                "start_mono": start, "end_mono": end,
                "observed_span_s": end - start,
                "samples": len(selected), "resolved_zero_samples": len(zeros),
                "zero_current_samples": len(raw_zero_current),
            })
            start = None
            selected = []
    return episodes


def analyze(path: Path, output: Path | None) -> dict[str, Any]:
    header, samples, summary = load_capture(path)
    if not samples:
        raise SystemExit("capture contains no usable samples")
    intervals = [right["mono"] - left["mono"] for left, right in zip(samples, samples[1:])]
    rates = {}
    for interval in TARGET_INTERVALS:
        selected = downsample(samples, interval)
        rates[f"{1 / interval:g}Hz"] = {
            "interval_s": interval,
            "samples": len(selected),
            "raw_power": held_comparison(samples, selected),
            "smoothed_power": smooth_comparison(samples, selected, interval),
            "transitions": transition_comparison(samples, selected),
        }
    report = {
        "capture": {**header, **summary, "usable_samples": len(samples)},
        "scheduling": {
            "actual_interval_s": stats(intervals),
            "lateness_s": stats(sample["lateness_s"] for sample in samples),
            "read_latency_s": stats(sample["read_latency_s"] for sample in samples),
        },
        "updates": {field: update_analysis(samples, field) for field in (
            "current_now", "voltage_now", "power_now", "ac_online", "status", "soc"
        )},
        "rates": rates,
        "reference_transitions": {
            field: transitions(samples, field) for field in ("ac_online", "state")
        },
        "not_charging_episodes": not_charging_episodes(samples),
        "selection_rule": (
            "For ideal instants anchored at the first actual 5 Hz observation, select "
            "the first recorded observation whose actual monotonic timestamp is at or "
            "after the instant; never average observations. Hold that selected value "
            "until the next derived observation for timeline comparisons."
        ),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if output is not None:
        output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("output", type=Path)
    capture_parser.add_argument("--duration", type=float, default=600)
    capture_parser.add_argument("--interval", type=float, default=REFERENCE_INTERVAL)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("capture", type=Path)
    analyze_parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.output, args.duration, args.interval)
    else:
        analyze(args.capture, args.output)


if __name__ == "__main__":
    main()

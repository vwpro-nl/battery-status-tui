"""Robust, session-local battery time estimation."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence

from .models import Estimate, Measurement


MIN_TREND_SECONDS = 15 * 60
MAX_TREND_SECONDS = 60 * 60
BUCKET_SECONDS = 5 * 60
MIN_PERCENTAGE_SPAN = 1.0


def _bucketed(samples: Sequence[Measurement], now: int) -> list[tuple[int, float]]:
    buckets: dict[int, list[float]] = defaultdict(list)
    cutoff = now - MAX_TREND_SECONDS
    for sample in samples:
        if cutoff <= sample.timestamp <= now:
            buckets[sample.timestamp // BUCKET_SECONDS].append(sample.percentage)
    return [
        (bucket * BUCKET_SECONDS + BUCKET_SECONDS // 2, statistics.median(values))
        for bucket, values in sorted(buckets.items())
    ]


def robust_slope(samples: Sequence[Measurement], kind: str, now: int) -> float | None:
    """Return the median pairwise percentage/hour slope for recent buckets."""
    points = _bucketed(samples, now)
    if len(points) < 4 or points[-1][0] - points[0][0] < MIN_TREND_SECONDS:
        return None
    percentages = [point[1] for point in points]
    if max(percentages) - min(percentages) < MIN_PERCENTAGE_SPAN:
        return None
    slopes: list[float] = []
    for index, (left_time, left_value) in enumerate(points):
        for right_time, right_value in points[index + 1 :]:
            elapsed = right_time - left_time
            if elapsed >= BUCKET_SECONDS:
                slopes.append((right_value - left_value) / elapsed * 3600)
    if not slopes:
        return None
    slope = statistics.median(slopes)
    if kind == "discharging" and slope >= 0:
        return None
    if kind == "charging" and slope <= 0:
        return None
    return slope


def estimate_remaining(
    current: Measurement,
    session_samples: Sequence[Measurement],
    now: int,
) -> Estimate | None:
    kind = current.session_kind
    if kind not in {"charging", "discharging"}:
        return None
    slope = robust_slope(session_samples, kind, now)
    if slope is not None:
        remaining_percent = 100 - current.percentage if kind == "charging" else current.percentage
        seconds = round(remaining_percent / abs(slope) * 3600)
        if seconds > 0:
            return Estimate(seconds, "session-trend", slope)

    if current.remaining_seconds and current.remaining_seconds > 0:
        return Estimate(current.remaining_seconds, "upower")

    if current.power_w and current.power_w > 0 and current.energy_wh is not None:
        if kind == "discharging":
            energy_remaining = current.energy_wh
        elif current.energy_full_wh is not None:
            energy_remaining = current.energy_full_wh - current.energy_wh
        else:
            energy_remaining = 0
        if energy_remaining > 0:
            return Estimate(round(energy_remaining / current.power_w * 3600), "energy-rate")
    return None


def smooth_seconds(previous: int | None, current: int, alpha: float = 0.25) -> int:
    if previous is None or previous <= 0:
        return current
    return round(previous * (1 - alpha) + current * alpha)


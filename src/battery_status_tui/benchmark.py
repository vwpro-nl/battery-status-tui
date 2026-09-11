"""In-memory live-sampling benchmark and time-based downsampling."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .live import LiveOperatingMode, LiveSampler, classify_live_mode


POWER_CHANGE_W = 0.5


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    sampled_at: float
    power_w: float | None
    mode: LiveOperatingMode


@dataclass(frozen=True, slots=True)
class RateResult:
    hz: float
    samples: int
    valid_power_samples: int
    valid_power_fraction: float
    power_changes: int
    mode_transitions: int
    mean_power_w: float | None
    stdev_power_w: float | None
    mean_abs_error_w: float | None
    missed_power_changes: int | None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    reference_hz: float
    requested_duration_s: float
    wall_seconds: float
    cpu_seconds: float
    samples: tuple[BenchmarkSample, ...]
    results: tuple[RateResult, ...]
    recommended_hz: float | None
    recommendation_reason: str

    @property
    def cpu_fraction(self) -> float:
        return 0.0 if self.wall_seconds <= 0 else self.cpu_seconds / self.wall_seconds


def downsample(samples: Sequence[BenchmarkSample], hz: float) -> tuple[BenchmarkSample, ...]:
    """Select the latest observation at or before each elapsed-time boundary."""
    if not math.isfinite(hz) or hz <= 0:
        raise ValueError("downsample rate must be positive and finite")
    if not samples:
        return ()
    period_ns = round(1_000_000_000 / hz)
    origin = samples[0].sampled_at
    elapsed_ns = [round((item.sampled_at - origin) * 1_000_000_000) for item in samples]
    selected: list[BenchmarkSample] = []
    index = 0
    boundary_ns = 0
    while boundary_ns <= elapsed_ns[-1]:
        while index + 1 < len(samples) and elapsed_ns[index + 1] <= boundary_ns:
            index += 1
        if not selected or samples[index] is not selected[-1]:
            selected.append(samples[index])
        boundary_ns += period_ns
    return tuple(selected)


def _changes(samples: Sequence[BenchmarkSample]) -> int:
    return sum(
        left.power_w is not None and right.power_w is not None
        and abs(right.power_w - left.power_w) >= POWER_CHANGE_W
        for left, right in zip(samples, samples[1:])
    )


def _mode_transitions(samples: Sequence[BenchmarkSample]) -> int:
    return sum(left.mode != right.mode for left, right in zip(samples, samples[1:]))


def _held_power(samples: Sequence[BenchmarkSample], at: float) -> float | None:
    value = None
    for sample in samples:
        if sample.sampled_at > at:
            break
        value = sample.power_w
    return value


def summarize(reference: Sequence[BenchmarkSample], candidate: Sequence[BenchmarkSample],
              hz: float) -> RateResult:
    powers = [item.power_w for item in candidate if item.power_w is not None]
    errors = []
    for item in reference:
        held = _held_power(candidate, item.sampled_at)
        if item.power_w is not None and held is not None:
            errors.append(abs(item.power_w - held))
    reference_changes = _changes(reference)
    changes = _changes(candidate)
    return RateResult(
        hz=hz,
        samples=len(candidate),
        valid_power_samples=len(powers),
        valid_power_fraction=0.0 if not candidate else len(powers) / len(candidate),
        power_changes=changes,
        mode_transitions=_mode_transitions(candidate),
        mean_power_w=None if not powers else statistics.fmean(powers),
        stdev_power_w=None if len(powers) < 2 else statistics.pstdev(powers),
        mean_abs_error_w=None if not errors else statistics.fmean(errors),
        missed_power_changes=None if not any(
            item.power_w is not None for item in reference
        ) else max(0, reference_changes - changes),
    )


def recommend(results: Sequence[RateResult]) -> tuple[float | None, str]:
    if not results or max(item.valid_power_fraction for item in results) < 0.5:
        return None, "insufficient valid live-power data"
    reference = max(results, key=lambda item: item.hz)
    tolerance = max(0.25, (reference.stdev_power_w or 0.0) * 0.10)
    allowed_missed = max(1, math.ceil(reference.power_changes * 0.05))
    for item in sorted(results, key=lambda result: result.hz):
        if (item.samples >= 3
                and item.valid_power_fraction >= reference.valid_power_fraction - 0.05
                and item.mean_abs_error_w is not None
                and item.mean_abs_error_w <= tolerance
                and item.missed_power_changes is not None
                and item.missed_power_changes <= allowed_missed
                and item.mode_transitions == reference.mode_transitions):
            return item.hz, (
                f"lowest rate with power MAE <= {tolerance:.2f} W, at most "
                f"{allowed_missed} missed >= {POWER_CHANGE_W:.1f} W change(s), and all mode transitions"
            )
    return reference.hz, "only the reference rate met the conservative retention criteria"


def analyze(samples: Sequence[BenchmarkSample], rates: Sequence[float],
            reference_hz: float, *, requested_duration_s: float = 0.0,
            wall_seconds: float = 0.0, cpu_seconds: float = 0.0) -> BenchmarkReport:
    ordered = tuple(sorted(set((*rates, reference_hz))))
    results = tuple(summarize(samples, downsample(samples, hz), hz) for hz in ordered)
    recommended, reason = recommend(results)
    return BenchmarkReport(reference_hz, requested_duration_s, wall_seconds, cpu_seconds,
                           tuple(samples), results, recommended, reason)


def run_benchmark(reference_hz: float, duration_s: float, rates: Sequence[float], *,
                  sampler_factory: Callable[..., LiveSampler] = LiveSampler,
                  monotonic: Callable[[], float] = time.monotonic,
                  process_time: Callable[[], float] = time.process_time,
                  sleep: Callable[[float], None] = time.sleep) -> BenchmarkReport:
    sampler = sampler_factory(interval=1.0 / reference_hz)
    started = monotonic()
    cpu_started = process_time()
    samples: list[BenchmarkSample] = []
    while True:
        now = monotonic()
        if now - started >= duration_s:
            break
        reading = sampler.poll()
        if reading is not None and reading.raw is not None:
            raw = reading.raw
            samples.append(BenchmarkSample(
                now, raw.power_w,
                classify_live_mode(raw.ac_online, raw.state,
                                   tuple(item.state for item in raw.raw_batteries)),
            ))
        deadline = sampler.next_sample_at
        sleep(max(0.0, min(0.05, (deadline - monotonic()) if deadline else 0.01)))
    wall = monotonic() - started
    cpu = process_time() - cpu_started
    return analyze(samples, rates, reference_hz, requested_duration_s=duration_s,
                   wall_seconds=wall, cpu_seconds=cpu)


def format_report(report: BenchmarkReport) -> str:
    lines = [
        f"reference_sample_hz: {report.reference_hz:g}",
        f"requested_duration_seconds: {report.requested_duration_s:g}",
        f"wall_seconds: {report.wall_seconds:.3f}",
        f"process_cpu_seconds: {report.cpu_seconds:.6f}",
        f"process_cpu_per_wall: {report.cpu_fraction:.6f}",
        "downsampling: latest observed sample at or before each elapsed-time boundary",
        "rate_hz samples valid_power changes mode_transitions mean_w stdev_w mae_vs_ref_w missed_changes",
    ]
    value = lambda number: "n/a" if number is None else f"{number:.3f}"
    for item in report.results:
        lines.append(
            f"{item.hz:g} {item.samples} {item.valid_power_fraction:.1%} "
            f"{item.power_changes} {item.mode_transitions} {value(item.mean_power_w)} "
            f"{value(item.stdev_power_w)} {value(item.mean_abs_error_w)} "
            f"{item.missed_power_changes if item.missed_power_changes is not None else 'n/a'}"
        )
    recommended = "unavailable" if report.recommended_hz is None else f"{report.recommended_hz:g}"
    lines.extend((f"recommended_sample_hz: {recommended}",
                  f"recommendation_rule: {report.recommendation_reason}"))
    return "\n".join(lines)

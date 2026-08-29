"""Unicode battery history and forecast chart."""

from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import defaultdict
from collections.abc import Sequence

from .models import Estimate, Measurement, Session, SleepInterval


CSI = "\x1b["
RESET = CSI + "0m"
BOLD = CSI + "1m"
CYAN = CSI + "38;5;81m"
YELLOW = CSI + "38;5;221m"
MUTED = CSI + "38;5;244m"
DIM = CSI + "2m"

BATTERY_COLOR_STOPS = (
    (0.0, (85, 10, 20)),
    (25.0, (155, 35, 30)),
    (50.0, (175, 110, 25)),
    (75.0, (90, 130, 40)),
    (100.0, (20, 105, 50)),
)

TIME_COLUMNS = 36
COLUMN_SECONDS = 20 * 60
GRAPH_WIDTH = TIME_COLUMNS + 1
NOW_INDEX = TIME_COLUMNS // 2
HISTORY_SECONDS = 6 * 3600
FORECAST_SECONDS = 6 * 3600
TICK_SECONDS = 3600
GRAPH_OFFSET = 6
MIN_EARLY_SLOPE = 0.25
BLOCKS = " ▁▂▃▄▅▆▇█"
BRAILLE_LEFT_BOTTOM_UP = (0x40, 0x04, 0x02, 0x01)
BRAILLE_RIGHT_BOTTOM_UP = (0x80, 0x20, 0x10, 0x08)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def _put(canvas: list[str], position: int, text: str) -> None:
    for offset, character in enumerate(text):
        target = position + offset
        if 0 <= target < len(canvas):
            canvas[target] = character


def _fill_chars(percentage: float) -> tuple[str, str]:
    level = max(0, min(16, round(percentage / 100 * 16)))
    bottom = BLOCKS[min(8, level)]
    top = BLOCKS[max(0, level - 8)]
    return top, bottom


def _braille_mask(left_count: int, right_count: int) -> str:
    mask = sum(BRAILLE_LEFT_BOTTOM_UP[:left_count]) + sum(BRAILLE_RIGHT_BOTTOM_UP[:right_count])
    return " " if mask == 0 else chr(0x2800 + mask)


def _braille_fill(left_percentage: float, right_percentage: float | None = None) -> tuple[str, str]:
    right_percentage = left_percentage if right_percentage is None else right_percentage
    left_levels = _braille_level(left_percentage / 100 * 8)
    right_levels = _braille_level(right_percentage / 100 * 8)
    return _braille_fill_levels(left_levels, right_levels)


def _braille_level(continuous_height: float) -> int:
    """Quantize a valid positive SoC without making its subcolumn disappear."""
    if continuous_height <= 0:
        return 0
    return max(1, min(8, round(continuous_height)))


def _braille_fill_levels(left_levels: int, right_levels: int) -> tuple[str, str]:
    return (
        _braille_mask(max(0, left_levels - 4), max(0, right_levels - 4)),
        _braille_mask(min(4, left_levels), min(4, right_levels)),
    )


def _early_raster(continuous_heights: Sequence[float]) -> list[int]:
    """Shift clear monotone round() transitions one subcolumn earlier."""
    rounded = [_braille_level(height) for height in continuous_heights]
    raster = rounded.copy()
    for index in range(1, len(continuous_heights)):
        earlier = index - 1
        if earlier == 0:
            continue
        slope = continuous_heights[index] - continuous_heights[earlier]
        previous_slope = continuous_heights[earlier] - continuous_heights[earlier - 1]
        if (abs(slope) >= MIN_EARLY_SLOPE and abs(previous_slope) >= MIN_EARLY_SLOPE
                and slope * previous_slope > 0 and abs(rounded[index] - rounded[earlier]) == 1):
            raster[earlier] = rounded[index]
    return raster


def _keep_positive_subcolumns_visible(
    continuous_heights: Sequence[float], raster: Sequence[int]
) -> list[int]:
    """Restore the minimum dot if later contour shaping removed it."""
    return [max(1, level) if height > 0 else level
            for height, level in zip(continuous_heights, raster)]


def _sleep_residual_transfer(continuous_heights: Sequence[float], raster: Sequence[int]) -> list[int]:
    """Expose a shallow monotone sleep trend without changing total dot mass."""
    transferred = list(raster)
    if len(continuous_heights) < 4 or len(set(raster)) != 1:
        return transferred
    deltas = [right - left for left, right in zip(continuous_heights, continuous_heights[1:])]
    rising = all(delta > 0 for delta in deltas)
    falling = all(delta < 0 for delta in deltas)
    if not (rising or falling) or abs(continuous_heights[-1] - continuous_heights[0]) >= 1:
        return transferred
    direction = 1 if rising else -1
    if not (0 <= transferred[0] - direction <= 8 and 0 <= transferred[-1] + direction <= 8):
        return transferred
    transferred[0] -= direction
    transferred[-1] += direction
    return transferred


def _braille_subcolumn_times(bucket_start: float, bucket_duration: int) -> tuple[float, float]:
    return bucket_start + bucket_duration / 4, bucket_start + bucket_duration * 3 / 4

def project_column(timestamp: int, now: int) -> int:
    """Project an exact timestamp onto the shared 20-minute display grid."""
    return NOW_INDEX + timestamp // COLUMN_SECONDS - now // COLUMN_SECONDS


def column_timestamp(column: int, now: int) -> int:
    """Return the exact boundary represented by a display column."""
    return (now // COLUMN_SECONDS + column - NOW_INDEX) * COLUMN_SECONDS


def _history_column(timestamp: int, now: int) -> int | None:
    latest_closed_bucket = now // COLUMN_SECONDS - 1
    sample_bucket = timestamp // COLUMN_SECONDS
    column = NOW_INDEX - 1 - (latest_closed_bucket - sample_bucket)
    return column if sample_bucket <= latest_closed_bucket and 0 <= column < NOW_INDEX else None


def _sleep_fraction(interval: SleepInterval, bucket_start: int, bucket_duration: int) -> float:
    overlap = max(0, min(interval.ended_at, bucket_start + bucket_duration)
                  - max(interval.started_at, bucket_start))
    return overlap / bucket_duration


def _sleep_columns(interval: SleepInterval, now: int) -> list[int]:
    if interval.ended_at <= interval.started_at:
        return []
    first_bucket = interval.started_at // COLUMN_SECONDS
    last_bucket = (interval.ended_at - 1) // COLUMN_SECONDS
    columns = []
    for bucket in range(first_bucket, last_bucket + 1):
        bucket_start = bucket * COLUMN_SECONDS
        column = project_column(bucket_start, now)
        if 0 <= column < NOW_INDEX and _sleep_fraction(interval, bucket_start, COLUMN_SECONDS) > 0.25:
            columns.append(column)
    return columns


def _sleep_boundary_percentages(
    interval: SleepInterval, history: Sequence[Measurement]
) -> tuple[float | None, float | None]:
    before = [sample for sample in history if sample.timestamp <= interval.started_at]
    after = [sample for sample in history if sample.timestamp >= interval.ended_at]
    pre_percentage = max(before, key=lambda sample: sample.timestamp).percentage if before else interval.pre_percentage
    post_percentage = min(after, key=lambda sample: sample.timestamp).percentage if after else interval.post_percentage
    return pre_percentage, post_percentage


def _active_percentage_at(
    timestamp: float,
    history: Sequence[Measurement],
    interval: SleepInterval,
    bucket_start: int,
    bucket_duration: int,
) -> float | None:
    """Interpolate active measurements within one mixed sleep bucket."""
    samples = [sample for sample in history
               if bucket_start <= sample.timestamp < bucket_start + bucket_duration
               and not interval.started_at <= sample.timestamp < interval.ended_at]
    before = [sample for sample in samples if sample.timestamp <= timestamp]
    after = [sample for sample in samples if sample.timestamp >= timestamp]
    left = max(before, key=lambda sample: sample.timestamp) if before else None
    right = min(after, key=lambda sample: sample.timestamp) if after else None
    if left is not None and right is not None and left.timestamp != right.timestamp:
        fraction = (timestamp - left.timestamp) / (right.timestamp - left.timestamp)
        return left.percentage + (right.percentage - left.percentage) * fraction
    sample = left or right
    return sample.percentage if sample is not None else None


def _smooth_sleep_edges(
    render_columns: Sequence[int],
    raster: list[int],
    baseline: Sequence[int],
    top: Sequence[str],
    bottom: Sequence[str],
    percentages: Sequence[float | None],
) -> list[int]:
    """Move a Braille edge one dot toward directly adjacent solid history."""
    if not render_columns:
        return raster

    def solid_level(column: int) -> int | None:
        if not 0 <= column < GRAPH_WIDTH or percentages[column] is None:
            return None
        if top[column] not in BLOCKS or bottom[column] not in BLOCKS:
            return None
        return max(0, min(16, round((percentages[column] or 0) / 100 * 16)))

    def adjust(index: int, neighbor: int, avoid_overshoot: bool = False) -> None:
        level = solid_level(neighbor)
        if level is None:
            return
        raster[index] = baseline[index]
        braille_level = baseline[index] * 2
        if level < braille_level:
            candidate = max(0, baseline[index] - 1)
            if not avoid_overshoot or candidate * 2 >= level:
                raster[index] = candidate
        elif level > braille_level:
            candidate = min(8, baseline[index] + 1)
            if not avoid_overshoot or candidate * 2 <= level:
                raster[index] = candidate

    run_start = 0
    for index in range(1, len(render_columns) + 1):
        if index < len(render_columns) and render_columns[index] == render_columns[index - 1] + 1:
            continue
        adjust(run_start * 2, render_columns[run_start] - 1, avoid_overshoot=True)
        adjust((index - 1) * 2 + 1, render_columns[index - 1] + 1)
        run_start = index
    return raster


def _battery_color(percentage: float) -> str:
    value = max(0.0, min(100.0, percentage))
    for (lower_value, lower_rgb), (upper_value, upper_rgb) in zip(
        BATTERY_COLOR_STOPS, BATTERY_COLOR_STOPS[1:]
    ):
        if value <= upper_value:
            fraction = (value - lower_value) / (upper_value - lower_value)
            red, green, blue = (
                round(lower + (upper - lower) * fraction)
                for lower, upper in zip(lower_rgb, upper_rgb)
            )
            return f"{CSI}38;2;{red};{green};{blue}m"
    raise AssertionError("clamped battery percentage has no color segment")


def _chart_rows_and_percentages(
    current: Measurement,
    history: Sequence[Measurement],
    estimate: Estimate | None,
    now: int,
    sleep_intervals: Sequence[SleepInterval] = (),
) -> tuple[str, str, list[float | None]]:
    top = [" "] * GRAPH_WIDTH
    bottom = [" "] * GRAPH_WIDTH
    percentages_by_column: list[float | None] = [None] * GRAPH_WIDTH
    buckets: dict[int, list[float]] = defaultdict(list)
    for sample in history:
        if now - HISTORY_SECONDS <= sample.timestamp < now:
            column = _history_column(sample.timestamp, now)
            if column is not None:
                buckets[column].append(sample.percentage)
    for column, bucket_percentages in buckets.items():
        percentage = statistics.median(bucket_percentages)
        top[column], bottom[column] = _fill_chars(percentage)
        percentages_by_column[column] = percentage

    for interval in sleep_intervals:
        if interval.ended_at <= now - HISTORY_SECONDS or interval.started_at >= now:
            continue
        columns = _sleep_columns(interval, now)
        pre_percentage, post_percentage = _sleep_boundary_percentages(interval, history)
        if pre_percentage is None or post_percentage is None or interval.ended_at <= interval.started_at:
            continue

        def sleep_percentage(timestamp: float) -> float:
            fraction = max(0.0, min(
                1.0, (timestamp - interval.started_at) / (interval.ended_at - interval.started_at)
            ))
            return pre_percentage + (post_percentage - pre_percentage) * fraction

        render_columns = [column for column in columns if not any(
            interval.started_at <= sample.timestamp < interval.ended_at
            and _history_column(sample.timestamp, now) == column
            for sample in history
        )]
        subcolumn_percentages = []
        for column in render_columns:
            bucket_start = column_timestamp(column, now)
            left_timestamp, right_timestamp = _braille_subcolumn_times(bucket_start, COLUMN_SECONDS)
            for timestamp in (left_timestamp, right_timestamp):
                percentage = sleep_percentage(timestamp)
                if not interval.started_at <= timestamp < interval.ended_at:
                    active = _active_percentage_at(timestamp, history, interval, bucket_start,
                                                   COLUMN_SECONDS)
                    percentage = active if active is not None else percentage
                subcolumn_percentages.append(percentage)
        continuous_heights = [percentage / 100 * 8 for percentage in subcolumn_percentages]
        baseline = _early_raster(continuous_heights)
        raster = _sleep_residual_transfer(continuous_heights, baseline)
        raster = _smooth_sleep_edges(render_columns, raster, baseline, top, bottom,
                                     percentages_by_column)
        raster = _keep_positive_subcolumns_visible(continuous_heights, raster)
        for index, column in enumerate(render_columns):
            bucket_start = column_timestamp(column, now)
            center_timestamp = bucket_start + COLUMN_SECONDS / 2
            top[column], bottom[column] = _braille_fill_levels(*raster[index * 2:index * 2 + 2])
            percentages_by_column[column] = sleep_percentage(center_timestamp)

    top[NOW_INDEX] = "│"
    bottom[NOW_INDEX] = "│"
    kind = current.session_kind
    full_on_ac = current.ac_online is True and (
        current.state in {"full", "charged", "fully-charged"} or current.percentage >= 100
    )
    if full_on_ac or estimate is not None and kind in {"charging", "discharging"}:
        endpoint = now + FORECAST_SECONDS if full_on_ac or kind == "charging" else min(
            now + estimate.seconds, now + FORECAST_SECONDS
        )

        def forecast_percentage(timestamp: float) -> float:
            if full_on_ac:
                return 100.0
            elapsed = max(0.0, timestamp - now)
            fraction = min(1.0, elapsed / estimate.seconds)
            target = 100.0 if kind == "charging" else 0.0
            return current.percentage + (target - current.percentage) * fraction

        last_column = min(GRAPH_WIDTH - 1, project_column(endpoint, now))
        forecast_columns = list(range(NOW_INDEX + 1, min(GRAPH_WIDTH, last_column + 1)))
        subcolumn_percentages = []
        for column in forecast_columns:
            bucket_start = column_timestamp(column, now)
            left_timestamp, right_timestamp = _braille_subcolumn_times(bucket_start, COLUMN_SECONDS)
            subcolumn_percentages.extend((forecast_percentage(left_timestamp), forecast_percentage(right_timestamp)))
        continuous_heights = [percentage / 100 * 8 for percentage in subcolumn_percentages]
        raster = _keep_positive_subcolumns_visible(
            continuous_heights, _early_raster(continuous_heights)
        )
        for index, column in enumerate(forecast_columns):
            bucket_start = column_timestamp(column, now)
            center_timestamp = bucket_start + COLUMN_SECONDS / 2
            top[column], bottom[column] = _braille_fill_levels(*raster[index * 2:index * 2 + 2])
            percentages_by_column[column] = forecast_percentage(center_timestamp)
    return "".join(top), "".join(bottom), percentages_by_column


def chart_rows(
    current: Measurement,
    history: Sequence[Measurement],
    estimate: Estimate | None,
    now: int,
    sleep_intervals: Sequence[SleepInterval] = (),
) -> tuple[str, str]:
    top, bottom, _ = _chart_rows_and_percentages(current, history, estimate, now, sleep_intervals)
    return top, bottom


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    total_minutes = seconds // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d{hours}h"
    return f"{hours}h{minutes:02d}"


def axis_rows(now: int) -> tuple[str, str]:
    axis = ["─"] * GRAPH_WIDTH
    labels = [" "] * GRAPH_WIDTH
    first_visible = column_timestamp(-1, now)
    last_visible = column_timestamp(GRAPH_WIDTH, now)
    timestamp = first_visible // TICK_SECONDS * TICK_SECONDS
    while timestamp <= last_visible:
        position = project_column(timestamp, now)
        if 0 <= position < GRAPH_WIDTH:
            axis[position] = "┬"
        label = dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%H")
        if 0 <= position and position + len(label) <= GRAPH_WIDTH:
            _put(labels, position, label)
        timestamp += TICK_SECONDS
    return "".join(axis), "".join(labels).rstrip()


def _style_battery(row: str, percentages: Sequence[float | None]) -> str:
    return "".join(
        f"{_battery_color(percentage)}{character}{RESET}"
        if percentage is not None and character != " " else character
        for character, percentage in zip(row, percentages)
    )


def title_line(current: Measurement, power_profile: str | None = None) -> str:
    arrow = "↑" if current.session_kind == "charging" else "↓" if current.session_kind == "discharging" else "·"
    arrow_column = GRAPH_OFFSET + NOW_INDEX
    power = "-- W" if current.power_w is None else f"{'~' if current.power_approximate else ''}{current.power_w:.1f} W"
    profile = f" ({power_profile})" if power_profile else ""
    canvas = [" "] * (arrow_column + 2 + len(power) + len(profile))
    _put(canvas, 0, "BATTERY")
    percentage = f"SoC {current.percentage:.0f}%"
    _put(canvas, arrow_column - len(percentage) - 1, percentage)
    _put(canvas, arrow_column, arrow)
    _put(canvas, arrow_column + 2, power + profile)
    plain = "".join(canvas).rstrip()
    return f"{BOLD}{CYAN}{plain[:7]}{RESET}{plain[7:arrow_column]}{YELLOW}{arrow}{RESET}{plain[arrow_column + 1:]}"


def render_dashboard(
    current: Measurement,
    history: Sequence[Measurement],
    session: Session | None,
    estimate: Estimate | None,
    now: int,
    sleep_intervals: Sequence[SleepInterval] = (),
    health_percent: float | None = None,
    power_profile: str | None = None,
) -> str:
    top, bottom, percentages = _chart_rows_and_percentages(current, history, estimate, now, sleep_intervals)
    elapsed = None if session is None else max(0, now - session.started_at)
    left_label = format_duration(elapsed).ljust(GRAPH_OFFSET)
    if estimate is None:
        right_label = "--"
    else:
        end_time = dt.datetime.fromtimestamp(now + estimate.seconds).astimezone().strftime("%H:%M")
        right_label = f"{format_duration(estimate.seconds)} ~{end_time}"
    axis, labels = axis_rows(now)
    left_meaning = "start" if elapsed is not None else ""
    right_meaning = ("full" if current.session_kind == "charging" else "empty") if (
        estimate is not None and current.session_kind in {"charging", "discharging"}
    ) else ""
    return "\n".join(
        (
            title_line(current, power_profile),
            f"{MUTED}{left_label}{RESET}{_style_battery(top, percentages)} {DIM}{right_label}{RESET}",
            f"{MUTED}{DIM}{left_meaning.ljust(GRAPH_OFFSET)}{RESET}{_style_battery(bottom, percentages)} "
            f"{MUTED}{DIM}{right_meaning}{RESET}".rstrip(),
            " " * GRAPH_OFFSET + axis,
            " " * GRAPH_OFFSET + labels.ljust(GRAPH_WIDTH) + (f" SoH {health_percent:.3f}%" if health_percent is not None else ""),
        )
    )

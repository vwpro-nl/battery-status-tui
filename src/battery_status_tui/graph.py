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
BLOCKS = " ▁▂▃▄▅▆▇█"
BRAILLE_ROWS_BOTTOM_UP = (0xC0, 0x24, 0x12, 0x09)
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


def _braille_fill(percentage: float) -> tuple[str, str]:
    levels = max(0, min(8, round(percentage / 100 * 8)))

    def glyph(count: int) -> str:
        return " " if count == 0 else chr(0x2800 + sum(BRAILLE_ROWS_BOTTOM_UP[:count]))

    return glyph(max(0, levels - 4)), glyph(min(4, levels))

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
        for column in columns:
            if any(
                interval.started_at <= sample.timestamp < interval.ended_at
                and _history_column(sample.timestamp, now) == column
                for sample in history
            ):
                continue
            timestamp = column_timestamp(column, now) + COLUMN_SECONDS / 2
            fraction = max(0.0, min(
                1.0, (timestamp - interval.started_at) / (interval.ended_at - interval.started_at)
            ))
            percentage = pre_percentage + (post_percentage - pre_percentage) * fraction
            top[column], bottom[column] = _braille_fill(percentage)
            percentages_by_column[column] = percentage

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
        last_column = min(GRAPH_WIDTH - 1, project_column(endpoint, now))
        for column in range(NOW_INDEX + 1, min(GRAPH_WIDTH, last_column + 1)):
            if full_on_ac:
                percentage = 100.0
            else:
                elapsed = column_timestamp(column, now) - now
                fraction = min(1.0, elapsed / estimate.seconds)
                target = 100.0 if kind == "charging" else 0.0
                percentage = current.percentage + (target - current.percentage) * fraction
            forecast_top, forecast_bottom = _braille_fill(percentage)
            top[column] = forecast_top
            bottom[column] = forecast_bottom
            percentages_by_column[column] = percentage
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


def title_line(current: Measurement) -> str:
    arrow = "↑" if current.session_kind == "charging" else "↓" if current.session_kind == "discharging" else "·"
    arrow_column = GRAPH_OFFSET + NOW_INDEX
    canvas = [" "] * (arrow_column + 16)
    _put(canvas, 0, "BATTERY")
    percentage = f"{current.percentage:.0f}%"
    _put(canvas, arrow_column - len(percentage) - 1, percentage)
    _put(canvas, arrow_column, arrow)
    power = "-- W" if current.power_w is None else f"{'~' if current.power_approximate else ''}{current.power_w:.1f} W"
    _put(canvas, arrow_column + 2, power)
    plain = "".join(canvas).rstrip()
    return f"{BOLD}{CYAN}{plain[:7]}{RESET}{plain[7:arrow_column]}{YELLOW}{arrow}{RESET}{plain[arrow_column + 1:]}"


def render_dashboard(
    current: Measurement,
    history: Sequence[Measurement],
    session: Session | None,
    estimate: Estimate | None,
    now: int,
    sleep_intervals: Sequence[SleepInterval] = (),
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
            title_line(current),
            f"{MUTED}{left_label}{RESET}{_style_battery(top, percentages)} {DIM}{right_label}{RESET}",
            f"{MUTED}{DIM}{left_meaning.ljust(GRAPH_OFFSET)}{RESET}{_style_battery(bottom, percentages)} "
            f"{MUTED}{DIM}{right_meaning}{RESET}".rstrip(),
            " " * GRAPH_OFFSET + axis,
            " " * GRAPH_OFFSET + labels,
        )
    )

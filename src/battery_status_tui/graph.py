"""Unicode battery history and forecast chart."""

from __future__ import annotations

import datetime as dt
import math
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


def _braille_fill(percentage: float, right_dots: bool) -> tuple[str, str]:
    levels = max(0, min(8, round(percentage / 100 * 8)))
    dots = BRAILLE_RIGHT_BOTTOM_UP if right_dots else BRAILLE_LEFT_BOTTOM_UP

    def glyph(count: int) -> str:
        return " " if count == 0 else chr(0x2800 + sum(dots[:count]))

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


def _inside_sleep(timestamp: int, intervals: Sequence[SleepInterval]) -> bool:
    return any(interval.started_at <= timestamp < interval.ended_at for interval in intervals)


def _sleep_columns(interval: SleepInterval, now: int) -> range:
    first_full_bucket = (interval.started_at + COLUMN_SECONDS - 1) // COLUMN_SECONDS
    after_last_full_bucket = interval.ended_at // COLUMN_SECONDS
    if first_full_bucket < after_last_full_bucket:
        first_bucket = first_full_bucket
        last_bucket = after_last_full_bucket - 1
    else:
        midpoint = interval.started_at + (interval.ended_at - interval.started_at) // 2
        first_bucket = last_bucket = midpoint // COLUMN_SECONDS
    latest_closed_bucket = now // COLUMN_SECONDS - 1
    last_bucket = min(last_bucket, latest_closed_bucket)
    if first_bucket > last_bucket:
        return range(0)
    start = NOW_INDEX - 1 - (latest_closed_bucket - first_bucket)
    end = NOW_INDEX - 1 - (latest_closed_bucket - last_bucket)
    start = max(0, min(NOW_INDEX - 1, start))
    end = max(start, min(NOW_INDEX - 1, end))
    return range(start, end + 1)


def _sleep_z_columns(columns: range) -> set[int]:
    width = len(columns)
    if width == 0:
        return set()
    count = max(1, math.ceil(width / 4))
    return {columns.start + (2 * index + 1) * width // (2 * count) for index in range(count)}


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
        if now - HISTORY_SECONDS <= sample.timestamp < now and not _inside_sleep(sample.timestamp, sleep_intervals):
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
        z_columns = _sleep_z_columns(columns)
        for column in columns:
            top[column] = " "
            bottom[column] = "z" if column in z_columns else " "
            percentages_by_column[column] = None

    top[NOW_INDEX] = "│"
    bottom[NOW_INDEX] = "│"
    kind = current.session_kind
    if estimate is not None and kind in {"charging", "discharging"}:
        endpoint = min(now + estimate.seconds, now + FORECAST_SECONDS)
        last_column = min(GRAPH_WIDTH - 1, project_column(endpoint, now))
        for column in range(NOW_INDEX + 1, min(GRAPH_WIDTH, last_column + 1)):
            elapsed = column_timestamp(column, now) - now
            fraction = min(1.0, elapsed / estimate.seconds)
            target = 100.0 if kind == "charging" else 0.0
            percentage = current.percentage + (target - current.percentage) * fraction
            forecast_top, forecast_bottom = _braille_fill(percentage, column % 2 == 0)
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


def _style_sleep(row: str) -> str:
    return re.sub(r"(z+)", rf"{MUTED}{DIM}\1{RESET}", row)


def _style_battery(row: str, percentages: Sequence[float | None]) -> str:
    styled = "".join(
        f"{_battery_color(percentage)}{character}{RESET}"
        if percentage is not None and character != " " else character
        for character, percentage in zip(row, percentages)
    )
    return _style_sleep(styled)


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

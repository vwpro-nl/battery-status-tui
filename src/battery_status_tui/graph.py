"""Unicode battery history and forecast chart."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence

from .models import Estimate, Measurement, Session, SleepInterval


CSI = "\x1b["
RESET = CSI + "0m"
BOLD = CSI + "1m"
CYAN = CSI + "38;5;81m"
GREEN = CSI + "38;5;114m"
YELLOW = CSI + "38;5;221m"
MUTED = CSI + "38;5;244m"
DIM = CSI + "2m"

GRAPH_WIDTH = 49
NOW_INDEX = GRAPH_WIDTH // 2
HISTORY_SECONDS = 6 * 3600
FORECAST_SECONDS = 6 * 3600
GRAPH_OFFSET = 10
MAX_INTERPOLATION_SECONDS = 180
BLOCKS = " ▁▂▃▄▅▆▇█"
BRAILLE_LEFT = ("⠁", "⠂", "⠄", "⡀")
BRAILLE_RIGHT = ("⠈", "⠐", "⠠", "⢀")
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


def _braille_point(percentage: float, right_dot: bool) -> tuple[str, str]:
    position = max(0, min(7, round((100 - percentage) / 100 * 7)))
    glyphs = BRAILLE_RIGHT if right_dot else BRAILLE_LEFT
    if position < 4:
        return glyphs[position], " "
    return " ", glyphs[position - 4]


def _interpolate(samples: Sequence[Measurement], timestamp: int) -> float | None:
    if not samples or timestamp < samples[0].timestamp:
        return None
    before = samples[0]
    for after in samples[1:]:
        if after.timestamp >= timestamp:
            if after.timestamp == before.timestamp:
                return after.percentage
            if after.timestamp - before.timestamp > MAX_INTERPOLATION_SECONDS:
                return None
            fraction = (timestamp - before.timestamp) / (after.timestamp - before.timestamp)
            return before.percentage + fraction * (after.percentage - before.percentage)
        before = after
    return before.percentage


def chart_rows(
    current: Measurement,
    history: Sequence[Measurement],
    estimate: Estimate | None,
    now: int,
    sleep_intervals: Sequence[SleepInterval] = (),
) -> tuple[str, str]:
    ordered = sorted((*history, current), key=lambda sample: sample.timestamp)
    top = [" "] * GRAPH_WIDTH
    bottom = [" "] * GRAPH_WIDTH
    for column in range(NOW_INDEX):
        timestamp = now - HISTORY_SECONDS + round(column / NOW_INDEX * HISTORY_SECONDS)
        percentage = _interpolate(ordered, timestamp)
        if percentage is not None:
            top[column], bottom[column] = _fill_chars(percentage)

    for interval in sleep_intervals:
        start = max(0, round((interval.started_at - (now - HISTORY_SECONDS)) / HISTORY_SECONDS * NOW_INDEX))
        end = min(NOW_INDEX - 1, max(start, round((interval.ended_at - (now - HISTORY_SECONDS)) / HISTORY_SECONDS * NOW_INDEX)))
        if end < 0 or start >= NOW_INDEX or interval.pre_percentage is None:
            continue
        point_top, point_bottom = _braille_point(interval.pre_percentage, False)
        sleep_row = top if point_top.strip() else bottom
        glyph = "⠒" if point_top.strip() else "⠤"
        for column in range(max(0, start), end + 1):
            sleep_row[column] = glyph
        for column in range(max(0, start + (end - start) // 2), end + 1, 7):
            sleep_row[column] = "Z"

    top[NOW_INDEX] = "│"
    bottom[NOW_INDEX] = "│"
    kind = current.session_kind
    if estimate is not None and kind in {"charging", "discharging"}:
        plotted_seconds = min(estimate.seconds, FORECAST_SECONDS)
        last_column = NOW_INDEX + math.ceil(plotted_seconds / FORECAST_SECONDS * NOW_INDEX)
        for column in range(NOW_INDEX + 1, min(GRAPH_WIDTH, last_column + 1)):
            elapsed = (column - NOW_INDEX) / NOW_INDEX * FORECAST_SECONDS
            fraction = min(1.0, elapsed / estimate.seconds)
            target = 100.0 if kind == "charging" else 0.0
            percentage = current.percentage + (target - current.percentage) * fraction
            forecast_top, forecast_bottom = _braille_point(percentage, column % 2 == 0)
            top[column] = forecast_top
            bottom[column] = forecast_bottom
    return "".join(top), "".join(bottom)


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    total_minutes = seconds // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d{hours}h"
    return f"{hours}h{minutes:02d}"


def _clock(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%H")


def axis_rows(now: int) -> tuple[str, str]:
    axis = ["─"] * GRAPH_WIDTH
    labels = [" "] * GRAPH_WIDTH
    for position, offset in zip((0, 12, 24, 36, 48), (-6, -3, 0, 3, 6)):
        axis[position] = "┬"
        label = _clock(now + offset * 3600)
        _put(labels, min(position, GRAPH_WIDTH - len(label)), label)
    return "".join(axis), "".join(labels).rstrip()


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
    top, bottom = chart_rows(current, history, estimate, now, sleep_intervals)
    elapsed = None if session is None else max(0, now - session.started_at)
    left_label = format_duration(elapsed).ljust(GRAPH_OFFSET)
    if estimate is None:
        right_label = "--"
    else:
        end_time = dt.datetime.fromtimestamp(now + estimate.seconds).astimezone().strftime("%H:%M")
        right_label = f"{format_duration(estimate.seconds)} ~{end_time}"
    axis, labels = axis_rows(now)
    return "\n".join(
        (
            title_line(current),
            " " * GRAPH_OFFSET + top,
            f"{MUTED}{left_label}{RESET}{bottom}  {DIM}{right_label}{RESET}",
            " " * GRAPH_OFFSET + axis,
            " " * GRAPH_OFFSET + labels,
        )
    )

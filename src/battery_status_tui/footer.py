"""Viewer frame chrome: one MUTED footer line under the locked dashboard.

Not part of ``graph.render_dashboard``. The interactive and ``--once`` viewers
append this line when terminal height permits; graph and history projection
geometry remain independent of the viewer chrome.
"""

from __future__ import annotations

import datetime as dt

from .graph import GRAPH_OFFSET, MUTED, RESET, display_width

MIN_VIEWER_WIDTH = 62
MIN_VIEWER_HEIGHT = 7

# Hard-coded English names so LC_TIME cannot change the footer.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def format_footer_clock(now: float) -> str:
    """Local time at minute precision: ``Sun 6 Sep 2026 06:07``."""
    local = dt.datetime.fromtimestamp(now).astimezone()
    weekday = _WEEKDAYS[local.weekday()]
    month = _MONTHS[local.month - 1]
    return f"{weekday} {local.day} {month} {local.year} {local.hour:02d}:{local.minute:02d}"


def format_refresh_interval(interval: float) -> str:
    """Configured refresh interval: ``1m`` for whole minutes, otherwise ``30s``."""
    if interval >= 60 and interval % 60 == 0:
        return f"{int(interval // 60)}m"
    return f"{interval:g}s"


def format_countdown(now: float, next_refresh_at: float) -> str:
    return f"{max(0, int(round(next_refresh_at - now)))}s"


def refresh_phrase(interval: float, now: float, next_refresh_at: float) -> str:
    return f"({format_refresh_interval(interval)}) refresh in {format_countdown(now, next_refresh_at)}"


def footer_message(
    now: float,
    interval: float,
    next_refresh_at: float,
    *,
    compact: bool = False,
) -> str:
    refresh = refresh_phrase(interval, now, next_refresh_at)
    if compact:
        return refresh
    return f"{format_footer_clock(now)} · {refresh}"


def attach_footer(
    body: str,
    now: float,
    interval: float,
    next_refresh_at: float,
    width: int,
    height: int,
) -> str:
    """Append one footer line aligned to the graph's left edge, or ``body``.

    The graph cells begin at ``GRAPH_OFFSET`` (the left label gutter), so the
    footer starts in that same column. Date/time is dropped before the refresh
    phrase when the indented full line cannot fit. The footer is omitted when
    even the compact form cannot fit, or when ``height`` cannot hold one extra
    line without clipping ``body``.
    """
    lines = body.splitlines() or [""]
    has_soh_row = len(lines) == 7
    required_height = len(lines) if has_soh_row else len(lines) + 1
    if height < required_height:
        return body
    width = max(1, width)
    full = footer_message(now, interval, next_refresh_at)
    compact = footer_message(now, interval, next_refresh_at, compact=True)
    indent = GRAPH_OFFSET
    if indent + display_width(full) <= width:
        text = full
    elif indent + display_width(compact) <= width:
        text = compact
    elif display_width(compact) <= width:
        indent = 0
        text = compact
    else:
        return body
    footer = MUTED + (" " * indent) + text + RESET
    if not has_soh_row:
        return body.rstrip("\n") + "\n" + footer
    prefix = lines[-1]
    combined = (prefix + " " * max(0, indent - display_width(prefix))
                + MUTED + text + RESET)
    return "\n".join(lines[:-1] + [combined])


def frame_with_margins(
    body: str,
    now: float,
    interval: float,
    next_refresh_at: float,
    width: int,
    height: int,
) -> str:
    """Fit a viewer frame to the terminal with one blank cell on each side."""
    width, height = max(1, width), max(1, height)
    if width < MIN_VIEWER_WIDTH or height < MIN_VIEWER_HEIGHT:
        if width < 3:
            return ""
        message = f"terminal too small ({width}x{height})"
        inner = width - 2
        return " " + message[:inner].ljust(inner) + " "
    inner = width - 2
    framed = attach_footer(body, now, interval, next_refresh_at, inner, height)
    return "\n".join(
        " " + line + " " * max(0, inner - display_width(line)) + " "
        for line in framed.splitlines()
    )


def parked_cursor(frame: str, width: int) -> str:
    """Place the cursor after bottom-row content, clear of deferred wrap."""
    rows = max(1, len(frame.splitlines()))
    return f"\x1b[{rows};{max(1, width - 1)}H"

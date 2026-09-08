"""Viewer footer chrome around the locked dashboard."""

from __future__ import annotations

import datetime as dt
import inspect
import locale
import re
import unittest

from battery_status_tui.footer import (
    attach_footer,
    frame_with_margins,
    footer_message,
    format_footer_clock,
    format_refresh_interval,
    parked_cursor,
    refresh_phrase,
)
from battery_status_tui.graph import (
    CYAN, GRAPH_OFFSET, GRAPH_WIDTH, MUTED, RESET, display_width, render_dashboard,
)
from battery_status_tui.models import Estimate, Measurement, Session

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(value: str) -> str:
    return ANSI.sub("", value)


def stamp(year, month, day, hour, minute, second=0) -> float:
    """Naive local timestamp so the clock string is TZ-independent in tests."""
    return dt.datetime(year, month, day, hour, minute, second).timestamp()


def sample_dashboard(now: int) -> str:
    current = Measurement(now, 48, "discharging", False, power_w=8.4)
    history = [Measurement(now - 3600, 60, "discharging", False)]
    session = Session(1, "discharging", now - 3600, None, 60, None)
    return render_dashboard(current, history, session, Estimate(7200, "test"), now)


class FooterClockTests(unittest.TestCase):
    def test_exact_deterministic_format_single_digit_day(self):
        now = stamp(2026, 9, 6, 6, 7)
        self.assertEqual(format_footer_clock(now), "Sun 6 Sep 2026 06:07")

    def test_two_digit_day_has_no_leading_zero_on_day(self):
        now = stamp(2026, 9, 16, 6, 7)
        self.assertEqual(format_footer_clock(now), "Wed 16 Sep 2026 06:07")
        self.assertNotIn(" 06 Sep ", format_footer_clock(stamp(2026, 9, 6, 6, 7)))

    def test_minute_precision_drops_seconds(self):
        now = stamp(2026, 9, 6, 6, 7, 59)
        self.assertEqual(format_footer_clock(now), "Sun 6 Sep 2026 06:07")
        self.assertNotRegex(format_footer_clock(now), r":\d{2}:\d{2}")

    def test_english_weekday_and_month_ignore_lc_time(self):
        now = stamp(2026, 9, 6, 6, 7)
        source = inspect.getsource(format_footer_clock)
        self.assertNotIn("strftime", source)
        self.assertIn("_WEEKDAYS", source)
        self.assertIn("_MONTHS", source)
        previous = locale.setlocale(locale.LC_TIME)
        try:
            for name in ("C", "POSIX", "de_DE.UTF-8", "fr_FR.UTF-8"):
                try:
                    locale.setlocale(locale.LC_TIME, name)
                except locale.Error:
                    continue
                self.assertEqual(format_footer_clock(now), "Sun 6 Sep 2026 06:07")
        finally:
            locale.setlocale(locale.LC_TIME, previous)


class FooterIntervalTests(unittest.TestCase):
    def test_whole_minutes_use_m_suffix(self):
        self.assertEqual(format_refresh_interval(60), "1m")
        self.assertEqual(format_refresh_interval(120), "2m")

    def test_non_minute_intervals_use_seconds(self):
        self.assertEqual(format_refresh_interval(30), "30s")
        self.assertEqual(format_refresh_interval(90), "90s")
        self.assertEqual(format_refresh_interval(1), "1s")

    def test_configured_interval_appears_in_full_and_compact_forms(self):
        now = stamp(2026, 9, 6, 6, 7)
        full = footer_message(now, 120, now + 47)
        compact = footer_message(now, 120, now + 47, compact=True)
        self.assertEqual(full, "Sun 6 Sep 2026 06:07 · (2m) refresh in 47s")
        self.assertEqual(compact, "(2m) refresh in 47s")
        self.assertIn("(1m)", footer_message(now, 60, now + 47))

    def test_countdown_rounds_to_whole_seconds(self):
        now = stamp(2026, 9, 6, 6, 7)
        self.assertEqual(refresh_phrase(60, now, now + 47.4), "(1m) refresh in 47s")
        self.assertEqual(refresh_phrase(60, now, now + 47.6), "(1m) refresh in 48s")
        self.assertEqual(refresh_phrase(60, now, now - 5), "(1m) refresh in 0s")


class FooterLayoutTests(unittest.TestCase):
    def test_exact_minimum_62_by_7_keeps_content_margins_and_cursor_in_bounds(self):
        framed = frame_with_margins(self.body, self.now, 60, self.now + 47, 62, 7)
        lines = framed.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertIn("refresh", plain(lines[-1]))
        for line in lines:
            self.assertEqual(display_width(line), 62)
            self.assertEqual((plain(line)[0], plain(line)[-1]), (" ", " "))
        self.assertEqual(parked_cursor(framed, 62), "\x1b[7;61H")

    def test_99_and_100_percent_widest_titles_fit_62_with_exact_margins(self):
        for percentage, suffix in ((99, "· 99% P"), (100, "·100% P")):
            with self.subTest(percentage=percentage):
                current = Measurement(
                    int(self.now), percentage, "neutral", True, power_w=123.4,
                    power_approximate=True,
                )
                live = Measurement(
                    int(self.now), percentage, "neutral", True, power_w=123.4,
                    power_approximate=True,
                )
                body = render_dashboard(
                    current, (), None, None, int(self.now),
                    health_percent=65.1, power_profile="performance",
                    presentation_direction="neutral",
                    diagnostic_power=(current, live, live),
                    show_persisted_power=True, show_weighted_power=True,
                )
                framed = frame_with_margins(
                    body, self.now, 60, self.now + 47, 62, 7,
                )
                lines = framed.splitlines()
                title = plain(lines[0])
                self.assertTrue(title.rstrip().endswith(suffix))
                self.assertEqual(display_width(lines[0]), 62)
                self.assertEqual((title[0], title[-1]), (" ", " "))
                self.assertEqual(display_width(lines[0][1:-1]), 60)

    def setUp(self):
        self.now = stamp(2026, 9, 6, 6, 7)
        self.body = sample_dashboard(int(self.now))

    def test_full_footer_matches_the_approved_form(self):
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, 24)
        footer = plain(framed).splitlines()[-1].strip()
        self.assertEqual(footer, "Sun 6 Sep 2026 06:07 · (1m) refresh in 47s")

    def test_viewer_frame_has_exact_one_cell_side_margins(self):
        for width in (62, 81, 100):
            framed = frame_with_margins(
                self.body, self.now, 60, self.now + 47, width, 10,
            )
            for line in framed.splitlines():
                text = plain(line)
                self.assertEqual(len(text), width)
                self.assertEqual(text[0], " ")
                self.assertEqual(text[-1], " ")

    def test_too_small_viewer_is_a_single_safe_line(self):
        framed = frame_with_margins(self.body, self.now, 60, self.now + 47, 61, 6)
        self.assertEqual(len(framed.splitlines()), 1)
        self.assertEqual(len(framed), 61)
        self.assertTrue(framed.startswith(" "))
        self.assertTrue(framed.endswith(" "))

    def test_no_ctrl_c_quit(self):
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, 24)
        self.assertNotIn("Ctrl-C", framed)
        self.assertNotIn("quit", framed.lower())

    def test_muted_palette_wraps_the_footer_only(self):
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, 24)
        lines = framed.splitlines()
        self.assertIn(MUTED, lines[-1])
        self.assertTrue(lines[-1].endswith(RESET))
        self.assertNotIn(CYAN, lines[-1])
        self.assertEqual(plain("\n".join(lines[:-1])), plain(self.body))

    def test_full_footer_starts_at_graph_left_edge(self):
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, 24)
        footer = plain(framed).splitlines()[-1]
        axis = plain(self.body).splitlines()[4]
        graph_left = len(axis) - len(axis.lstrip(" "))
        self.assertEqual(graph_left, GRAPH_OFFSET)
        self.assertEqual(footer.index("Sun"), GRAPH_OFFSET)
        self.assertEqual(footer.index("Sun"), graph_left)
        self.assertEqual(footer[:GRAPH_OFFSET], " " * GRAPH_OFFSET)
        self.assertTrue(footer[GRAPH_OFFSET:].startswith("Sun "))

    def test_left_edge_stays_fixed_from_10s_to_9s(self):
        ten = attach_footer(self.body, self.now, 60, self.now + 10, 80, 24)
        nine = attach_footer(self.body, self.now, 60, self.now + 9, 80, 24)
        ten_line = plain(ten).splitlines()[-1]
        nine_line = plain(nine).splitlines()[-1]
        self.assertEqual(ten_line.index("Sun"), GRAPH_OFFSET)
        self.assertEqual(nine_line.index("Sun"), GRAPH_OFFSET)
        self.assertEqual(ten_line.index("Sun"), nine_line.index("Sun"))
        self.assertTrue(ten_line.endswith("10s"))
        self.assertTrue(nine_line.endswith("9s"))
        self.assertEqual(display_width(ten_line), display_width(nine_line) + 1)
        self.assertEqual(ten_line[:GRAPH_OFFSET], nine_line[:GRAPH_OFFSET])

    def test_day_digit_length_does_not_move_the_left_edge(self):
        short_now = stamp(2026, 9, 6, 6, 7)
        long_now = stamp(2026, 9, 16, 6, 7)
        for now in (short_now, long_now):
            with self.subTest(day=dt.datetime.fromtimestamp(now).day):
                footer = plain(attach_footer(self.body, now, 60, now + 47, 80, 24)).splitlines()[-1]
                self.assertEqual(footer.index(footer.lstrip()[:3]), GRAPH_OFFSET)

    def test_narrow_fallback_drops_datetime_keeps_refresh(self):
        full = footer_message(self.now, 60, self.now + 47)
        compact = footer_message(self.now, 60, self.now + 47, compact=True)
        width = GRAPH_OFFSET + display_width(full) - 1
        self.assertGreater(GRAPH_OFFSET + display_width(full), width)
        self.assertLessEqual(GRAPH_OFFSET + display_width(compact), width)
        framed = attach_footer(self.body, self.now, 60, self.now + 47, width, 24)
        footer = plain(framed).splitlines()[-1]
        self.assertEqual(footer.index("("), GRAPH_OFFSET)
        self.assertEqual(footer[GRAPH_OFFSET:], "(1m) refresh in 47s")
        self.assertNotIn("Sep", footer)
        self.assertNotIn("2026", footer)

    def test_footer_never_wraps(self):
        for width in (80, 42, 30, 19, 10, 5):
            with self.subTest(width=width):
                framed = attach_footer(self.body, self.now, 60, self.now + 47, width, 24)
                for line in framed.splitlines():
                    if "refresh" in plain(line):
                        self.assertLessEqual(display_width(line), width)

    def test_insufficient_height_omits_footer(self):
        body_lines = len(self.body.splitlines())
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, body_lines)
        self.assertEqual(framed, self.body)
        self.assertNotIn("refresh", framed)
        self.assertEqual(len(plain(framed).splitlines()), body_lines)

    def test_sufficient_height_adds_exactly_one_line(self):
        body_lines = len(self.body.splitlines())
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, body_lines + 1)
        self.assertEqual(len(plain(framed).splitlines()), body_lines + 1)
        self.assertFalse(plain(framed).splitlines()[-2].strip() == "")

    def test_locked_graph_geometry_is_unchanged(self):
        self.assertEqual(len(plain(self.body).splitlines()), 6)
        framed = attach_footer(self.body, self.now, 60, self.now + 47, 80, 24)
        graph_lines = plain(framed).splitlines()[:6]
        original = plain(self.body).splitlines()
        self.assertEqual(graph_lines, original)
        self.assertIn("│", graph_lines[1][GRAPH_OFFSET:GRAPH_OFFSET + GRAPH_WIDTH])
        self.assertEqual(len(graph_lines[4][GRAPH_OFFSET:GRAPH_OFFSET + GRAPH_WIDTH]), GRAPH_WIDTH)
        self.assertNotIn("refresh", "\n".join(graph_lines))

    def test_soh_value_shares_footer_row_without_moving_date(self):
        body = render_dashboard(
            Measurement(int(self.now), 48, "discharging", False, power_w=8.4),
            (), None, None, int(self.now), health_percent=65.2,
        )
        framed = attach_footer(body, self.now, 60, self.now + 47, 80, 24)
        lines = plain(framed).splitlines()
        self.assertTrue(lines[-2].startswith("SoH"))
        self.assertTrue(lines[-1].startswith("65.2% "))
        self.assertEqual(lines[-1].index("Sun"), GRAPH_OFFSET)


class FooterHeartbeatContractTests(unittest.TestCase):
    def test_cli_separates_display_heartbeat_from_data_refresh(self):
        import inspect
        from battery_status_tui import cli

        self.assertEqual(cli.DISPLAY_HEARTBEAT, 1.0)
        source = inspect.getsource(cli._run_v4)
        self.assertIn("DISPLAY_HEARTBEAT", source)
        self.assertIn("_load_v4_view", source)
        self.assertIn("next_data_at", source)
        # Persisted data reads remain gated; the heartbeat may rebuild the
        # presentation frame from the cached snapshot and live title overlay.
        self.assertIn("if not persisted_attempted or now >= next_data_at", source)
        self.assertIn("render_v1_view", source)
        self.assertIn("_frame_with_footer", source)

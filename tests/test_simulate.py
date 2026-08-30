from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import re
import sqlite3
import tempfile
import unittest
from itertools import groupby
from pathlib import Path
from unittest.mock import patch

import battery_status_tui.simulate as simulate
from battery_status_tui import graph
from battery_status_tui.graph import (
    MAX_SPAN_SECONDS, UNKNOWN_TRAJECTORY, _chart_rows_and_percentages, now_column,
)
from battery_status_tui.models import Measurement, RawBatterySnapshot
from battery_status_tui.simulate import PowerContext, SimBlock, SocSpec
from battery_status_tui.v1_collector import V1Collector
from battery_status_tui.v1_runtime import read_v1_view
from battery_status_tui.v1_storage import V1Storage

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LIVE_NOW = 1_000_000_000  # fixed epoch for a deterministic seeded "live" database


def plain(value: str) -> str:
    return ANSI.sub("", value)


def _sample(timestamp: int, soc: float) -> Measurement:
    raw = RawBatterySnapshot(
        timestamp, float(timestamp), float(timestamp), "boot-live",
        "/sys/BAT0", "BAT0", soc, "discharging", False, energy_now_wh=40.0,
        energy_full_wh=50.0, energy_full_design_wh=80.0, cycle_count=120,
        sources=("sysfs",),
    )
    return Measurement(
        timestamp, soc, "discharging", False, power_w=10.0, energy_wh=40.0,
        energy_full_wh=50.0, energy_full_design_wh=80.0,
        power_method="power-now", power_confidence="high", source="sysfs",
        monotonic_s=float(timestamp), boottime_s=float(timestamp),
        boot_id="boot-live", battery_identity="BAT0", raw_batteries=(raw,),
    )


def _seed_live_db(path: Path, *, final_soc: float) -> None:
    """A small, non-linear real-ish discharge history ending at ``final_soc``."""
    collector = V1Collector(V1Storage(path))
    socs = [final_soc + n for n in (6.0, 5.2, 5.0, 4.1, 3.0, 2.4, 2.3, 1.5, 0.9, 0.0)]
    for index, soc in enumerate(socs):
        collector.process_poll(_sample(LIVE_NOW - (len(socs) - 1 - index) * 600, soc))


def _chart(inputs):
    return _chart_rows_and_percentages(
        inputs.current, inputs.history, inputs.estimate, inputs.now,
        inputs.sleeps, inputs.unknown_intervals,
    )


def _column_kinds(inputs) -> list[str]:
    marker = now_column(inputs.current, inputs.estimate)
    top, bottom, pct = _chart(inputs)
    kinds = []
    for column in range(marker):
        glyphs = (top[column] + bottom[column]).strip()
        if not glyphs:
            kinds.append("blank")
        elif pct[column] is UNKNOWN_TRAJECTORY:
            kinds.append("gray")            # :nodata neutral-gray reconstruction
        elif all(0x2800 <= ord(c) <= 0x28FF for c in glyphs):
            kinds.append("braille")
        elif all(c in "▁▂▃▄▅▆▇█" for c in glyphs):
            kinds.append("solid")
        else:
            kinds.append(f"other:{glyphs}")
    return kinds


# =====================================================================
#  headings + synthetic mode (must not regress)
# =====================================================================

class HeadingTests(unittest.TestCase):
    def test_synthetic_output_uses_the_simulation_heading(self):
        output = plain(simulate.run(["sleep-drop"]))
        self.assertTrue(output.startswith("SIMULATION"))
        self.assertNotIn("BATTERY", output)

    def test_production_dashboard_heading_default_is_battery(self):
        self.assertEqual(inspect.signature(graph.render_dashboard)
                         .parameters["heading"].default, "BATTERY")
        self.assertEqual(inspect.signature(graph.title_line)
                         .parameters["heading"].default, "BATTERY")


class SyntheticModeTests(unittest.TestCase):
    def test_synthetic_mode_opens_no_database(self):
        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("synthetic simulation opened a database")

        with patch.object(sqlite3, "connect", explode):
            output = simulate.run(["sleep-drop"])
            simulate.build_sleep_drop()
        self.assertIn("SIMULATION", output)

    def test_synthetic_is_deterministic_and_unchanged(self):
        self.assertEqual(simulate.run(["sleep-drop"]), simulate.run(["sleep-drop"]))
        self.assertNotEqual(simulate.run(["sleep-drop"]),
                            simulate.run(["sleep-drop", "--start-soc", "80"]))

    def test_synthetic_reaches_the_locked_sleep_render_path(self):
        inputs = simulate.build_sleep_drop()
        with patch.object(graph, "_chart_rows_and_percentages",
                          wraps=graph._chart_rows_and_percentages) as spy:
            inputs.render()
        sleep_arg = spy.call_args.args[4]
        self.assertEqual(tuple(sleep_arg), inputs.sleeps)
        run_kinds = [k for k, _ in groupby(x for x in _column_kinds(inputs) if x != "blank")]
        self.assertEqual(run_kinds, ["solid", "braille", "solid"])

    def test_source_has_no_writer_collector_or_timer_code(self):
        source = "".join(line for line in inspect.getsource(simulate).splitlines(True)
                         if not line.lstrip().startswith(("#", '"', "*")))
        for forbidden in ("initialize_writer", "write_generation", "_writer(",
                          ".transaction(", "process_poll", "V1Collector",
                          "record_sleep", "BEGIN IMMEDIATE", "systemctl",
                          "mode=rwc", "mode=rw'", 'mode=rw"',
                          "INSERT ", "UPDATE ", "DELETE ", "VACUUM"):
            self.assertNotIn(forbidden, source)

    def test_cli_has_no_database_path_option(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            simulate.build_parser().parse_args(["sleep-drop", "--database", "/x"])
        self.assertNotIn("--database", simulate.build_parser().format_help())


# =====================================================================
#  timeline grammar
# =====================================================================

class TimelineGrammarTests(unittest.TestCase):
    def test_duration_forms(self):
        for text, seconds in (("2h", 7200), ("1h24m", 5040), ("45m", 2700),
                              ("90s", 90), ("2h5m", 7500), ("2h05m", 7500),
                              ("2h02m", 7320)):
            self.assertEqual(simulate.parse_duration(text), seconds)
        for bad in ("", "0", "0s", "2x", "h", "-3h", "3.5h"):
            with self.assertRaises(ValueError):
                simulate.parse_duration(bad)

    def test_soc_spec_absolute_relative_and_invalid(self):
        self.assertEqual(simulate.parse_soc_spec("20%"), SocSpec("absolute", 20.0))
        self.assertEqual(simulate.parse_soc_spec("100"), SocSpec("absolute", 100.0))
        self.assertEqual(simulate.parse_soc_spec("-20%"), SocSpec("relative", -20.0))
        self.assertEqual(simulate.parse_soc_spec("+30%"), SocSpec("relative", 30.0))
        self.assertEqual(simulate.parse_soc_spec("+222%"), SocSpec("relative", 222.0))
        for bad in ("120%", "-1%..", "abc", "%", ""):
            with self.assertRaises(ValueError):
                simulate.parse_soc_spec(bad)

    def test_block_forms(self):
        self.assertEqual(simulate.parse_block("2h=50%"),
                         SimBlock(7200, "normal", SocSpec("absolute", 50.0)))
        self.assertEqual(simulate.parse_block("30m=-5%"),
                         SimBlock(1800, "normal", SocSpec("relative", -5.0)))
        self.assertEqual(simulate.parse_block("2h:sleep"),
                         SimBlock(7200, "sleep", None))
        self.assertEqual(simulate.parse_block("2h:sleep=50%"),
                         SimBlock(7200, "sleep", SocSpec("absolute", 50.0)))
        self.assertEqual(simulate.parse_block("1h:nodata=-10%"),
                         SimBlock(3600, "nodata", SocSpec("relative", -10.0)))
        self.assertEqual(simulate.parse_block("90s=20%").duration, 90)  # sub-column ok
        for bad in ("2h:weird", "2h:sleep:nodata", "=50%", "2h::sleep", "2h=nan%"):
            with self.assertRaises(ValueError):
                simulate.parse_block(bad)

    def test_timeline_optional_final_ac_dc(self):
        blocks, context = simulate.parse_timeline(["2h=50%", "3h:sleep=-20%"])
        self.assertEqual(context, PowerContext(None, None))
        self.assertEqual(len(blocks), 2)
        self.assertEqual(simulate.parse_timeline(["2h=50%", "ac"])[1], PowerContext("ac", None))
        self.assertEqual(simulate.parse_timeline(["2h=50%", "dc"])[1], PowerContext("dc", None))
        for bad in ([], ["ac"], ["2h=50%", "ac", "1h=40%"], ["dc", "2h=50%"],
                    ["1h=50%", "ac=8w", "1h=40%"]):
            with self.assertRaises(ValueError):
                simulate.parse_timeline(bad)

    def test_final_context_with_explicit_wattage(self):
        self.assertEqual(simulate.parse_final_context("dc=8.3w"), PowerContext("dc", 8.3))
        self.assertEqual(simulate.parse_final_context("ac=24.2w"), PowerContext("ac", 24.2))
        self.assertEqual(simulate.parse_final_context("dc=8w"), PowerContext("dc", 8.0))
        self.assertEqual(simulate.parse_timeline(["2h=50%", "dc=8.3w"])[1],
                         PowerContext("dc", 8.3))
        for bad in ("dc=0w", "ac=-5w", "dc=nanw", "ac=infw", "dc=8", "ac=8.3",
                    "dc=8W", "ac=8kw", "dc=w", "acdc"):
            with self.assertRaises(ValueError):
                simulate.parse_final_context(bad)

    def test_timeline_total_is_validated_against_the_shared_graph_window(self):
        ok = int(MAX_SPAN_SECONDS)
        simulate.parse_timeline([f"{ok // 3600}h"])  # exactly the window: allowed
        with self.assertRaises(ValueError) as raised:
            simulate.parse_timeline(["7h=50%", "6h=20%"])  # 13h > 12h
        self.assertIn("history window", str(raised.exception))
        # arbitrary block count within the window is fine
        blocks, _ = simulate.parse_timeline(["30m"] * 20)  # 10 h
        self.assertEqual(len(blocks), 20)


class SocMathTests(unittest.TestCase):
    def test_absolute_and_relative_from_64_percent(self):
        self.assertEqual(SocSpec("absolute", 20.0).apply(64.0), 20.0)
        self.assertEqual(SocSpec("relative", -20.0).apply(64.0), 44.0)
        self.assertEqual(SocSpec("relative", 20.0).apply(64.0), 84.0)
        self.assertEqual(SocSpec("relative", -120.0).apply(64.0), 0.0)
        self.assertEqual(SocSpec("relative", 222.0).apply(64.0), 100.0)


# =====================================================================
#  timeline against a genuine (seeded, read-only) database
# =====================================================================

class TimelineFromLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls._tmp.name) / "history.sqlite3"
        _seed_live_db(cls.db, final_soc=64.0)  # current SoC 64%, read-only hereafter

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.per_test_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.per_test_tmp.cleanup)

    def _build(self, *tokens):
        blocks, context = simulate.parse_timeline(tokens)
        return simulate.build_from_live_timeline(
            blocks=blocks, context=context, database_path=self.db, live_now=LIVE_NOW,
        )

    def _digest(self):
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    # --- genuine history preserved --------------------------------------

    def test_genuine_pre_history_is_preserved_verbatim(self):
        view = read_v1_view(V1Storage(self.db), now=LIVE_NOW)
        inputs = self._build("2h=50%", "3h:sleep=-20%")
        head = inputs.history[:len(view.history)]
        self.assertEqual(head, view.history)                         # identical objects
        self.assertTrue(all(m.source != "simulate-timeline" for m in head))
        self.assertEqual(inputs.sleeps[:len(view.sleeps)], view.sleeps)
        self.assertEqual(inputs.power_profile, view.power_profile)
        self.assertEqual(inputs.health_percent,
                         view.health.percent if view.health else None)
        socs = [m.percentage for m in head]
        self.assertGreater(len({round(b - a, 3) for a, b in zip(socs, socs[1:])}), 1)

    def test_only_simulated_blocks_extend_after_the_genuine_now(self):
        view = read_v1_view(V1Storage(self.db), now=LIVE_NOW)
        inputs = self._build("2h=50%", "45m=82%")
        self.assertTrue(all(m.timestamp < LIVE_NOW for m in view.history))
        added = inputs.history[len(view.history):]
        self.assertTrue(added and all(m.timestamp >= LIVE_NOW for m in added))
        self.assertTrue(all(m.source == "simulate-timeline" for m in added))

    # --- sequential timeline / math -----------------------------------

    def test_durations_accumulate_sequentially(self):
        inputs = self._build("1h24m=20%", "2h02m=82%")
        self.assertEqual(inputs.now - LIVE_NOW, 5040 + 7320)          # 3h26m
        inputs2 = self._build("35m=-4%", "3h12m:sleep=-28%", "27m=+8%",
                              "1h18m:nodata=-12%", "2h05m=100%", "ac")
        self.assertEqual(inputs2.now - LIVE_NOW,
                         2100 + 11520 + 1620 + 4680 + 7500)

    def test_checkpoint_soc_chain_uses_the_preceding_value(self):
        inputs = self._build("1h=64%", "2h:sleep=-13%", "1h=100%")
        sleep = inputs.sleeps[-1]
        self.assertAlmostEqual(sleep.pre_percentage, 64.0)
        self.assertAlmostEqual(sleep.post_percentage, 51.0)           # 64 - 13
        self.assertAlmostEqual(inputs.current.percentage, 100.0)

    def test_sleep_with_omitted_soc_stays_level(self):
        inputs = self._build("2h:sleep")
        sleep = inputs.sleeps[-1]
        self.assertAlmostEqual(sleep.pre_percentage, 64.0)
        self.assertAlmostEqual(sleep.post_percentage, 64.0)
        self.assertAlmostEqual(inputs.current.percentage, 64.0)

    def test_relative_soc_clamps_but_absolute_out_of_range_is_rejected(self):
        self.assertAlmostEqual(self._build("1h=-120%").current.percentage, 0.0)
        self.assertAlmostEqual(self._build("1h=+222%").current.percentage, 100.0)
        with self.assertRaises(ValueError):
            simulate.parse_timeline(["1h=105%"])

    # --- normal / sleep / nodata rendering ----------------------------

    def test_normal_block_creates_the_intended_active_trajectory(self):
        inputs = self._build("3h=20%")
        marker = now_column(inputs.current, inputs.estimate)
        _, _, pct = _chart_rows_and_percentages(
            inputs.current, inputs.history, inputs.estimate, inputs.now, inputs.sleeps
        )
        added = [pct[c] for c in range(marker) if pct[c] is not None][-6:]
        self.assertTrue(all(a >= b - 0.5 for a, b in zip(added, added[1:])))  # descends
        self.assertLess(min(added), 40.0)

    def test_sleep_block_reaches_the_locked_sleep_path_and_may_rise_or_fall(self):
        for tokens, rises in (("5h18m:sleep=100%", True), ("3h:sleep=-30%", False)):
            with self.subTest(tokens=tokens):
                inputs = self._build(tokens)
                with patch.object(graph, "_chart_rows_and_percentages",
                                  wraps=graph._chart_rows_and_percentages) as spy:
                    inputs.render()
                sleep_arg = spy.call_args.args[4]
                self.assertIn(inputs.sleeps[-1], tuple(sleep_arg))
                self.assertIn("braille", _column_kinds(inputs))
                delta = inputs.sleeps[-1].post_percentage - inputs.sleeps[-1].pre_percentage
                self.assertEqual(delta > 0, rises)

    def test_nodata_renders_neutral_gray_braille_between_known_endpoints(self):
        from battery_status_tui import graph
        from battery_status_tui.graph import column_timestamp

        inputs = self._build("1h=64%", "3h:nodata=90%", "dc")
        self.assertEqual(len(inputs.unknown_intervals), 1)
        gap = inputs.unknown_intervals[0]
        self.assertEqual(gap.kind, "nodata")
        self.assertNotIn(gap, inputs.sleeps)   # never a sleep
        self.assertEqual(inputs.sleeps, read_v1_view(V1Storage(self.db), now=LIVE_NOW).sleeps)

        marker = now_column(inputs.current, inputs.estimate)
        top, bottom, pct = _chart(inputs)
        gap_cols = [c for c in range(marker)
                    if gap.started_at <= column_timestamp(c, inputs.now, marker) < gap.ended_at]
        self.assertGreaterEqual(len(gap_cols), 5)
        for c in gap_cols:
            self.assertIs(pct[c], UNKNOWN_TRAJECTORY)                       # tagged unknown
            glyphs = (top[c] + bottom[c]).strip()
            self.assertTrue(glyphs and all(0x2800 <= ord(g) <= 0x28FF for g in glyphs))  # braille

        styled = graph._style_battery(bottom, pct)
        first, last = gap_cols[0], gap_cols[-1]
        self.assertIn(f"{graph.UNKNOWN_GRAY}{bottom[first]}", styled)       # neutral gray
        # the gray cells carry no SoC-gradient colour
        gradient_prefix = graph.CSI + "38;2;"
        gray_run = graph._style_battery("".join(bottom[c] for c in gap_cols),
                                        [UNKNOWN_TRAJECTORY] * len(gap_cols))
        self.assertNotIn(gradient_prefix, gray_run)

    def test_nodata_rising_falling_and_level(self):
        for tokens, relation in (("3h:nodata=90%", "rises"),
                                 ("3h:nodata=30%", "falls"),
                                 ("3h:nodata", "level")):
            with self.subTest(tokens=tokens):
                inputs = self._build("1h=64%", tokens, "dc")
                gap = inputs.unknown_intervals[0]
                marker = now_column(inputs.current, inputs.estimate)
                from battery_status_tui.graph import column_timestamp
                heights = []
                top, bottom, pct = _chart(inputs)
                for c in range(marker):
                    if pct[c] is UNKNOWN_TRAJECTORY:
                        dots = sum((ord(g) - 0x2800).bit_count()
                                   for g in (top[c], bottom[c]) if g != " ")
                        heights.append(dots)
                self.assertGreaterEqual(len(heights), 5)
                if relation == "rises":
                    self.assertGreater(heights[-1], heights[0])
                    self.assertGreater(gap.post_percentage, gap.pre_percentage)
                elif relation == "falls":
                    self.assertLess(heights[-1], heights[0])
                    self.assertLess(gap.post_percentage, gap.pre_percentage)
                else:
                    self.assertEqual(gap.pre_percentage, gap.post_percentage)
                    self.assertLessEqual(max(heights) - min(heights), 1)  # level

    def test_nodata_is_visually_distinct_from_sleep(self):
        from battery_status_tui import graph

        drop = ("1h=64%", "2h:sleep=-20%", "dc")
        gap = ("1h=64%", "2h:nodata=-20%", "dc")
        sleep_inputs, nodata_inputs = self._build(*drop), self._build(*gap)

        # nodata lives in unknown_intervals with kind "nodata"; sleep in sleeps
        self.assertEqual(nodata_inputs.unknown_intervals[0].kind, "nodata")
        self.assertEqual(len(nodata_inputs.sleeps), len(sleep_inputs.sleeps) - 1)

        sleep_pct = [p for p in _chart(sleep_inputs)[2] if p is UNKNOWN_TRAJECTORY]
        nodata_pct = [p for p in _chart(nodata_inputs)[2] if p is UNKNOWN_TRAJECTORY]
        self.assertFalse(sleep_pct)                 # sleep never uses the unknown tag
        self.assertTrue(nodata_pct)
        self.assertNotIn(graph.UNKNOWN_GRAY, sleep_inputs.render())
        self.assertIn(graph.UNKNOWN_GRAY, nodata_inputs.render())

    def test_the_locked_sleep_render_is_unchanged_by_the_nodata_feature(self):
        inputs = self._build("1h=64%", "3h:sleep=-25%", "dc")
        self.assertEqual(inputs.unknown_intervals, ())
        with_default = _chart_rows_and_percentages(
            inputs.current, inputs.history, inputs.estimate, inputs.now, inputs.sleeps)
        with_empty_unknown = _chart_rows_and_percentages(
            inputs.current, inputs.history, inputs.estimate, inputs.now, inputs.sleeps, ())
        self.assertEqual(with_default, with_empty_unknown)   # empty unknowns => byte-identical
        top, bottom, pct = with_default
        marker = now_column(inputs.current, inputs.estimate)
        sleep_cols = [c for c in range(marker)
                      if (top[c] + bottom[c]).strip()
                      and all(0x2800 <= ord(g) <= 0x28FF for g in (top[c] + bottom[c]).strip())]
        self.assertTrue(sleep_cols)
        for c in sleep_cols:
            self.assertIsInstance(pct[c], float)            # real interpolated SoC, gradient-coloured

    def test_real_history_gap_without_a_reliable_endpoint_stays_blank(self):
        from battery_status_tui.models import SleepInterval

        # a genuine gap: two real samples far apart, no sleep, no nodata interval
        history = list(read_v1_view(V1Storage(self.db), now=LIVE_NOW).history)
        current = Measurement(LIVE_NOW, 64.0, "discharging", False)
        top, bottom, pct = _chart_rows_and_percentages(
            current, history[:1] + history[-1:], None, LIVE_NOW)
        self.assertNotIn(UNKNOWN_TRAJECTORY, pct)
        # an unknown interval missing an endpoint checkpoint is also left blank
        half = SleepInterval(LIVE_NOW - 7200, LIVE_NOW - 3600, "nodata", "simulate",
                             "b", pre_percentage=60.0, post_percentage=None)
        top, bottom, pct = _chart_rows_and_percentages(
            current, [], None, LIVE_NOW, (), (half,))
        self.assertNotIn(UNKNOWN_TRAJECTORY, pct)
        self.assertEqual(set(top) | set(bottom), {" ", "│"})

    def test_mixed_timeline_stays_ordered_solid_braille_gray_solid(self):
        inputs = self._build("2h=50%", "3h:sleep=-20%", "1h:nodata", "45m=82%", "ac")
        compact = [k for k, _ in groupby(_column_kinds(inputs))]
        while compact and compact[0] == "blank":   # genuine history may not reach the edge
            compact.pop(0)
        self.assertEqual(compact, ["solid", "braille", "gray", "solid"])
        self.assertTrue(plain(inputs.render()).startswith("SIMULATION"))

    # --- forecast via production estimator ----------------------------

    def test_final_dc_discharge_yields_a_production_discharge_forecast(self):
        inputs = self._build("2h=45%", "1h=30%", "dc")
        self.assertIsNotNone(inputs.estimate)
        self.assertEqual(inputs.estimate.source, "session-trend")
        self.assertEqual(inputs.current.session_kind, "discharging")

    def test_final_ac_charge_yields_a_production_charging_forecast(self):
        inputs = self._build("2h=45%", "1h=80%", "ac")
        self.assertIsNotNone(inputs.estimate)
        self.assertEqual(inputs.estimate.source, "session-trend")
        self.assertEqual(inputs.current.session_kind, "charging")

    def test_final_full_on_ac_has_no_forecast_despite_watts(self):
        full = self._build("1h=100%", "ac=24.2w")
        self.assertEqual(full.current.state, "full")
        self.assertIsNone(full.current.session_kind)
        self.assertIsNone(full.estimate)  # production full/boundary semantics win

    def test_no_simulator_specific_estimator_exists(self):
        source = inspect.getsource(simulate)
        self.assertIn("estimate_remaining(current, final_trend, future_now)", source)
        self.assertNotIn("def _estimate", source)
        self.assertNotIn("Estimate(", source)  # simulator never builds an Estimate itself

    # --- AC/DC context + optional wattage -----------------------------

    def test_no_token_preserves_genuine_context_and_rate(self):
        view = read_v1_view(V1Storage(self.db), now=LIVE_NOW)
        inputs = self._build("3h:sleep=-20%")  # final sleep -> needs a rate to forecast
        self.assertEqual(inputs.current.ac_online, view.current.ac_online)
        self.assertEqual(inputs.current.power_w, view.current.power_w)      # ~genuine rate
        self.assertEqual(inputs.estimate.source, "energy-rate")            # production path

    def test_bare_token_uses_the_genuine_live_magnitude_even_when_reversed(self):
        live_w = read_v1_view(V1Storage(self.db), now=LIVE_NOW).current.power_w
        same = self._build("3h:sleep=-20%", "dc")            # dc == genuine DC
        self.assertEqual(same.current.power_w, live_w)
        self.assertEqual(same.estimate.source, "energy-rate")
        reversed_ctx = self._build("3h:sleep=+10%", "ac")    # DC -> AC, bare token
        self.assertEqual(reversed_ctx.current.power_w, live_w)  # genuine magnitude as default
        self.assertEqual(reversed_ctx.current.session_kind, "charging")
        self.assertEqual(reversed_ctx.estimate.source, "energy-rate")  # production charge forecast

    def test_bare_token_invents_no_rate_when_the_genuine_magnitude_is_none(self):
        no_power_db = Path(self.per_test_tmp.name) / "no-power.sqlite3"
        collector = V1Collector(V1Storage(no_power_db))
        for i in range(6):
            raw = RawBatterySnapshot(
                LIVE_NOW - (5 - i) * 600, float(LIVE_NOW), float(LIVE_NOW), "b",
                "/sys/BAT0", "BAT0", 60.0 - i, "discharging", False,
                energy_full_wh=50.0, energy_full_design_wh=80.0, sources=("sysfs",))
            collector.process_poll(Measurement(
                LIVE_NOW - (5 - i) * 600, 60.0 - i, "discharging", False, source="sysfs",
                energy_full_wh=50.0, monotonic_s=float(LIVE_NOW), boottime_s=float(LIVE_NOW),
                boot_id="b", battery_identity="BAT0", raw_batteries=(raw,)))

        def build(*tokens):
            blocks, ctx = simulate.parse_timeline(tokens)
            return simulate.build_from_live_timeline(
                blocks=blocks, context=ctx, database_path=no_power_db, live_now=LIVE_NOW)

        self.assertIsNone(build("3h:sleep=-20%", "dc").current.power_w)   # no invented rate
        self.assertEqual(build("3h:sleep=-20%", "dc=9w").current.power_w, 9.0)  # explicit ok

    def test_explicit_dc_watts_produces_an_energy_rate_discharge_forecast(self):
        inputs = self._build("2h=50%", "1h:nodata", "dc=8.3w")
        self.assertEqual(inputs.current.power_w, 8.3)
        self.assertEqual(inputs.current.session_kind, "discharging")
        self.assertEqual(inputs.estimate.source, "energy-rate")

    def test_explicit_ac_watts_produces_an_energy_rate_charge_forecast(self):
        inputs = self._build("2h=50%", "1h:nodata", "ac=24.2w")
        self.assertEqual(inputs.current.power_w, 24.2)
        self.assertEqual(inputs.current.session_kind, "charging")
        self.assertEqual(inputs.estimate.source, "energy-rate")

    def test_explicit_wattage_only_touches_the_final_now_not_the_timeline(self):
        without = self._build("2h=50%", "1h:nodata")
        with_watts = self._build("2h=50%", "1h:nodata", "dc=8.3w")
        self.assertEqual(without.history, with_watts.history)   # timeline untouched
        self.assertEqual(without.sleeps, with_watts.sleeps)
        self.assertEqual(without.now, with_watts.now)
        self.assertNotEqual(without.current.power_w, with_watts.current.power_w)

    # --- hard read-only guarantee ------------------------------------

    def test_from_live_does_not_modify_the_database(self):
        before = self._digest()
        self._build("2h=50%", "3h:sleep=-20%", "1h:nodata", "45m=82%", "ac").render()
        self.assertEqual(self._digest(), before)

    def test_the_connection_is_technically_read_only(self):
        with V1Storage(self.db).reader() as db:
            for statement in ("INSERT INTO metadata VALUES('sim','x')",
                              "UPDATE hourly_history SET revision=99",
                              "DELETE FROM state_events", "CREATE TABLE evil(x)",
                              "VACUUM"):
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute(statement)

    def test_no_writer_initialization_or_migration_path_is_reached(self):
        with patch.object(V1Storage, "reader", autospec=True,
                          wraps=V1Storage.reader) as reader, \
             patch.object(V1Storage, "initialize_writer", autospec=True,
                          side_effect=AssertionError("writer opened")), \
             patch.object(V1Storage, "transaction", autospec=True,
                          side_effect=AssertionError("transaction opened")), \
             patch.object(V1Storage, "write_generation", autospec=True,
                          side_effect=AssertionError("generation written")):
            self._build("2h=50%", "1h:sleep=-10%").render()
        self.assertGreaterEqual(reader.call_count, 1)

    def test_missing_database_is_reported_and_never_created(self):
        missing = Path(self.per_test_tmp.name) / "absent.sqlite3"
        blocks, context = simulate.parse_timeline(["1h=50%"])
        with self.assertRaises(simulate.SimulationError):
            simulate.build_from_live_timeline(blocks=blocks, context=context,
                                              database_path=missing)
        self.assertFalse(missing.exists())

    def test_cli_missing_database_exits_without_creating_it(self):
        missing = Path(self.per_test_tmp.name) / "cli-absent.sqlite3"
        with patch("battery_status_tui.storage.default_database_path",
                   return_value=missing), self.assertRaises(SystemExit):
            simulate.run(["sleep-drop", "--simulate", "3h=50%"])
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

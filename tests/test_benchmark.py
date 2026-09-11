from __future__ import annotations

import unittest

from battery_status_tui.benchmark import BenchmarkSample, analyze, downsample, format_report, run_benchmark
from battery_status_tui.live import LiveOperatingMode


def item(at, power, mode=LiveOperatingMode.DISCHARGING):
    return BenchmarkSample(at, power, mode)


class BenchmarkTests(unittest.TestCase):
    def test_time_based_downsampling_respects_jitter(self):
        reference = tuple(item(at, power) for at, power in (
            (0.00, 10), (0.18, 11), (0.43, 12), (0.61, 13), (0.82, 14), (1.03, 15)))
        self.assertEqual([(x.sampled_at, x.power_w) for x in downsample(reference, 2)],
                         [(0.0, 10), (0.43, 12), (0.82, 14)])

    def test_all_rates_use_one_unchanged_reference_dataset(self):
        reference = tuple(item(index * .2, float(index)) for index in range(21))
        report = analyze(reference, (.5, 1, 2, 5), 5)
        self.assertEqual(report.samples, reference)
        self.assertEqual([x.hz for x in report.results], [.5, 1, 2, 5])
        self.assertEqual(report.results[-1].samples, len(reference))

    def test_missing_power_has_no_fabricated_statistics_or_advice(self):
        report = analyze(tuple(item(index * .2, None) for index in range(10)), (1, 5), 5)
        self.assertIsNone(report.recommended_hz)
        for result in report.results:
            self.assertEqual(result.valid_power_fraction, 0)
            self.assertIsNone(result.mean_power_w)
            self.assertIsNone(result.mean_abs_error_w)
            self.assertIsNone(result.missed_power_changes)
        self.assertIn("recommended_sample_hz: unavailable", format_report(report))

    def test_recommendation_is_deterministic(self):
        reference = tuple(item(index * .2, 10.0) for index in range(51))
        first = analyze(reference, (.1, .5, 1, 2, 5), 5)
        second = analyze(reference, (.1, .5, 1, 2, 5), 5)
        # A ten-second 0.1 Hz projection contains fewer than the conservative
        # minimum of three observations, so 0.5 Hz is the lowest eligible rate.
        self.assertEqual(first.recommended_hz, .5)
        self.assertEqual(first.recommendation_reason, second.recommendation_reason)

    def test_cpu_metric_fake_run_does_not_crash(self):
        class Clock:
            now = 0.0
            cpu = 2.0
            def monotonic(self): return self.now
            def process_time(self): self.cpu += .001; return self.cpu
            def sleep(self, seconds): self.now += max(seconds, .01)
        class Raw:
            power_w, ac_online, state, raw_batteries = 10.0, False, "discharging", ()
        class Reading: raw = Raw()
        class Sampler:
            def __init__(self, interval): self.interval, self.next_sample_at = interval, 0.0
            def poll(self): self.next_sample_at += self.interval; return Reading()
        clock = Clock()
        report = run_benchmark(5, .5, (1, 5), sampler_factory=Sampler,
                               monotonic=clock.monotonic, process_time=clock.process_time,
                               sleep=clock.sleep)
        self.assertGreater(report.wall_seconds, 0)
        self.assertGreaterEqual(report.cpu_seconds, 0)

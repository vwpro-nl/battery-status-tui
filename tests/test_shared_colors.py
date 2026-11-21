import os
import tempfile
import unittest
from unittest import mock

from battery_status_tui import graph


class SharedColorsTests(unittest.TestCase):
    def read(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict(os.environ, {"MONITOR_DASHBOARD_COLORS": f.name}):
            return graph._shared_colors()

    def test_values_from_the_shared_file(self):
        shared = self.read("# x\ntitle=82\nmuted=#010203\nok=#0a0b0c\npending=#111213\ndown=#212223\nbad=green\n")
        self.assertEqual(graph._sgr(shared["title"]), "38;5;82")
        self.assertEqual(graph._sgr(shared["muted"]), "38;2;1;2;3")
        self.assertNotIn("bad", shared)
        base = ((0.0, (85, 10, 20)), (25.0, (155, 35, 30)), (50.0, (175, 110, 25)),
                (75.0, (90, 130, 40)), (100.0, (20, 105, 50)))
        stops = dict(graph._shared_stops(base, shared))
        self.assertEqual((stops[25.0], stops[50.0], stops[100.0]), ((33, 34, 35), (17, 18, 19), (10, 11, 12)))
        self.assertEqual((stops[0.0], stops[75.0]), ((85, 10, 20), (90, 130, 40)))   # not shared

    def test_palette_index_does_not_replace_a_gradient_stop(self):
        shared = self.read("ok=28\n")
        self.assertEqual(dict(graph._shared_stops(((100.0, (20, 105, 50)),), shared))[100.0], (20, 105, 50))

    def test_switched_off_or_missing(self):
        with mock.patch.dict(os.environ, {"MONITOR_DASHBOARD_COLORS": ""}):
            self.assertEqual(graph._shared_colors(), {})
        with mock.patch.dict(os.environ, {"MONITOR_DASHBOARD_COLORS": "/nonexistent/colors.conf"}):
            self.assertEqual(graph._shared_colors(), {})


if __name__ == "__main__":
    unittest.main()

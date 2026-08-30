from __future__ import annotations

import math
import struct
import unittest
from dataclasses import replace

from battery_status_tui.recent_series import (
    FORMAT_VERSION,
    HEADER,
    MAX_WINDOW_MS,
    RECORD,
    BatteryState,
    RecentPoint,
    RecentSeriesError,
    decode_recent_series,
    encode_recent_series,
    payload_sha256,
)


def point(timestamp_ms: int, **values) -> RecentPoint:
    defaults = dict(
        soc_millipercent=50_125,
        resolved_power_mw=8_400,
        energy_delta_wh=0.14,
        battery_state=BatteryState.DISCHARGING,
        profile="balanced",
        battery_set_key="battery-set-a",
        flags=0,
    )
    defaults.update(values)
    return RecentPoint(timestamp_ms=timestamp_ms, **defaults)


class RecentSeriesTests(unittest.TestCase):
    def test_record_contract_is_exactly_26_bytes(self):
        self.assertEqual(RECORD.size, 26)

    def test_round_trip_is_deterministic(self):
        points = (
            point(1_000_000),
            point(1_060_000, soc_millipercent=50_000, resolved_power_mw=None,
                  energy_delta_wh=None, profile=None, flags=1),
        )
        first = encode_recent_series(points)
        second = encode_recent_series(points)
        self.assertEqual(first, second)
        self.assertEqual(decode_recent_series(first), points)
        self.assertEqual(payload_sha256(first), payload_sha256(second))
        self.assertEqual(len(payload_sha256(first)), 64)

    def test_empty_series_round_trip(self):
        self.assertEqual(decode_recent_series(encode_recent_series(())), ())

    def test_dense_one_minute_points_fill_the_retention_window(self):
        count = MAX_WINDOW_MS // 60_000  # one-minute polls that fill the whole window
        points = tuple(point(1_000_000 + index * 60_000) for index in range(count))
        decoded = decode_recent_series(encode_recent_series(points))
        self.assertEqual(len(decoded), count)
        self.assertLessEqual(decoded[-1].timestamp_ms - decoded[0].timestamp_ms,
                             MAX_WINDOW_MS)

    def test_exact_retention_window_boundary_is_allowed_but_not_one_ms_more(self):
        encode_recent_series((point(1_000_000), point(1_000_000 + MAX_WINDOW_MS)))
        with self.assertRaises(RecentSeriesError):
            encode_recent_series((point(1_000_000),
                                  point(1_000_000 + MAX_WINDOW_MS + 1)))

    def test_retention_window_backs_the_widest_graph_history_span(self):
        # The widest dynamic-NOW viewport draws MAX_SPAN_SECONDS of history;
        # because 20-minute columns snap to absolute clock boundaries its
        # leftmost column can begin up to one COLUMN_SECONDS before
        # now - MAX_SPAN_SECONDS. recent_series must retain at least that far
        # back so every visible history column is backed by real samples.
        from battery_status_tui.graph import COLUMN_SECONDS, MAX_SPAN_SECONDS

        self.assertGreaterEqual(MAX_WINDOW_MS,
                                (MAX_SPAN_SECONDS + COLUMN_SECONDS) * 1_000)

    def test_encoder_rejects_non_finite_energy_and_invalid_enums(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(RecentSeriesError):
                encode_recent_series((point(1_000_000, energy_delta_wh=value),))
        with self.assertRaises(RecentSeriesError):
            encode_recent_series((replace(point(1_000_000), battery_state=99),))

    def test_decoder_rejects_corrupt_header_fields_and_length(self):
        payload = encode_recent_series((point(1_000_000),))
        corruptions = []
        raw = bytearray(payload); raw[0:4] = b"FAIL"; corruptions.append(raw)
        raw = bytearray(payload); raw[4] = FORMAT_VERSION + 1; corruptions.append(raw)
        raw = bytearray(payload); struct.pack_into("<H", raw, 6, RECORD.size + 1); corruptions.append(raw)
        corruptions.append(bytearray(payload[:-1]))
        for raw in corruptions:
            with self.subTest(raw=bytes(raw[:8])), self.assertRaises(RecentSeriesError):
                decode_recent_series(bytes(raw))

    def test_decoder_rejects_invalid_state_indices_and_non_finite_energy(self):
        payload = encode_recent_series((point(1_000_000),))
        fields = HEADER.unpack_from(payload)
        records_offset = HEADER.size + fields[-2]

        raw = bytearray(payload)
        raw[records_offset + 20] = 99
        with self.assertRaises(RecentSeriesError):
            decode_recent_series(bytes(raw))

        raw = bytearray(payload)
        raw[records_offset + 21] = 42
        with self.assertRaises(RecentSeriesError):
            decode_recent_series(bytes(raw))

        raw = bytearray(payload)
        struct.pack_into("<H", raw, records_offset + 22, 42)
        with self.assertRaises(RecentSeriesError):
            decode_recent_series(bytes(raw))

        raw = bytearray(payload)
        struct.pack_into("<d", raw, records_offset + 12, math.nan)
        with self.assertRaises(RecentSeriesError):
            decode_recent_series(bytes(raw))

        raw = bytearray(payload)
        struct.pack_into("<d", raw, records_offset + 12, math.inf)
        with self.assertRaises(RecentSeriesError):
            decode_recent_series(bytes(raw))

        raw = bytearray(payload)
        struct.pack_into("<H", raw, records_offset + 24, 0x0003)
        with self.assertRaises(RecentSeriesError):
            decode_recent_series(bytes(raw))


if __name__ == "__main__":
    unittest.main()

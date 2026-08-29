# ETA estimation

The remaining-time estimate is derived from the current charging or discharging
session (`estimate.py`):

1. Use at most the most recent 60 minutes of the session. Samples before the
   latest suspend/resume boundary, and before the latest continuity break, are
   excluded.
2. Collapse observations into five-minute buckets using the median percentage.
3. Require at least four buckets, at least 15 minutes of span, and at least one
   percentage point of movement.
4. Compute every pairwise bucket slope and take its median — a compact
   Theil–Sen estimator.
5. Reject a positive slope while discharging or a negative slope while charging.
6. Extrapolate toward 0% while discharging or 100% while charging.

This rejects short load spikes and isolated percentage anomalies without the lag
of averaging a whole session.

## Fallbacks

When the trend is not mature enough, in order:

1. UPower `time to empty` / `time to full` (or the sysfs equivalents);
2. energy ÷ `energy-rate`, or (missing energy to full) ÷ rate;
3. no estimate — the ETA shows `--`.

## Smoothing

The v1.0 renderer uses the Theil–Sen session trend directly. The older
per-session exponential smoothing (`smooth_seconds`, `alpha = 0.25`) is retained
only in the legacy schema-v2 runtime and is not applied on schema v4.

## How the forecast is drawn

The forecast renderer uses the selected ETA only to set the slope toward the
relevant boundary (0% or 100%). It never reverses direction.

The forecast is **not** truncated at the moment of predicted empty or full. It
is drawn across the entire 6-hour forecast window:

- **Discharging:** SoC follows the slope down to 0%, then **holds at 0% for the
  rest of the window**, rendered as a bottom Braille dot in deep red `#550A14`.
  This makes "the battery is predicted to be flat for the next several hours"
  visually distinct from "no forecast".
- **Charging:** SoC follows the slope up to 100%, then plateaus at a full column
  for the rest of the window.
- **Already full on AC:** a flat 100% line across the window.

See [graph.md](graph.md#forecast-behavior) for the rendering details.

## Power estimation is separate

Power (Watts) estimation is independent of ETA estimation. A time-derived Watt
value uses raw energy or charge counters, becomes eligible after 120 seconds,
may extend its window to ten minutes for coarse counters, uses the median of
valid deltas, and never spans a recorded sleep interval. See
[data-sources.md](data-sources.md#power-resolution).

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

The graphical forecast is **sized to the ETA**: it occupies just enough columns
to reach the predicted empty/full time and no more, ending flush against the
right edge. Whatever it does not need is given back to history, so the `NOW`
marker moves (see [graph.md](graph.md#dynamic-now-column)).

- **Discharging:** SoC follows the slope down toward 0% at the predicted empty
  time. If it reaches 0% before the right edge it holds at 0%, drawn as a bottom
  Braille dot in deep red `#550A14`.
- **Charging:** SoC follows the slope up toward a full column at the predicted
  full time.
- **No usable ETA, or already full on AC:** no forecast is drawn and `NOW` sits
  at the far-right column, giving the whole width to history.

`NOW` never moves left of the graph midpoint, so at least half the width is
always history. A forecast whose horizon is longer than that half is **clipped
at the right edge** — the drawn curve simply stops mid-slope. It is never
compressed or rescaled to fit, and the textual remaining duration stays
complete and authoritative regardless of the clip.

See [graph.md](graph.md#forecast-behavior) for the rendering details.

## Power estimation is separate

Power (Watts) estimation is independent of ETA estimation. A time-derived Watt
value uses raw energy or charge counters, becomes eligible after 120 seconds,
may extend its window to ten minutes for coarse counters, uses the median of
valid deltas, and never spans a recorded sleep interval. See
[data-sources.md](data-sources.md#power-resolution).

The live direction is presentation-authoritative before the minute collector
catches up: the arrow and `full`/`empty` target switch immediately, the stale
opposite-direction ETA is suppressed, and `--` is shown until compatible
persisted session data can produce a new ETA.

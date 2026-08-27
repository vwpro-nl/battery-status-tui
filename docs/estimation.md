# ETA estimation

The primary ETA is derived from the current charging or discharging session:

1. Use at most the most recent 60 minutes.
   Samples before the latest suspend/resume boundary are excluded.
2. Collapse observations into five-minute buckets using the median percentage.
3. Require four buckets, at least 15 minutes and at least one percentage point
   of movement.
4. Calculate every pairwise bucket slope and take its median (a compact
   Theil–Sen estimator).
5. Reject a positive discharge slope or negative charge slope.
6. Extrapolate only toward 0% while discharging or 100% while charging.

This rejects short load spikes and isolated percentage anomalies without the
lag of averaging an entire session. The displayed ETA is smoothed per session
with an exponential update (`alpha = 0.25`). Smoothing resets automatically
when a new charge/discharge session starts.

Fallback order when the trend is not mature enough:

1. UPower `TimeToEmpty` or `TimeToFull`;
2. energy divided by `EnergyRate`, or missing energy to full divided by rate;
3. no estimate.

The forecast renderer uses the selected ETA only to reach the relevant physical
boundary. It never reverses direction and never extends a completed forecast
artificially to the right edge of the six-hour future window.

Power estimation is separate from ETA estimation. A temporal Watt value uses
raw energy or charge counters and becomes eligible after 120 seconds. The
resolver can extend its observation window to ten minutes for coarse counters,
uses the median of valid deltas, and never spans a recorded sleep interval.

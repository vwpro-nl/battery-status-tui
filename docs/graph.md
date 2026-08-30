# The graph

The dashboard is five lines of text: a title line, two graph rows, an axis
line, and an axis-label line (which also carries the SoH readout). All of it is
produced by `graph.render_dashboard`.

## Geometry and timing

| Quantity | Value | Constant |
|---|---|---|
| Total columns | 37 | `GRAPH_WIDTH` (`TIME_COLUMNS + 1`) |
| Column width in time | 20 minutes | `COLUMN_SECONDS` |
| Maximum span | 12 hours | `MAX_SPAN_SECONDS` (`TIME_COLUMNS * COLUMN_SECONDS`) |
| NOW column | dynamic, never left of the midpoint | `now_column()` |
| Graph midpoint / minimum history | 18 columns (`TIME_COLUMNS // 2`) | `NOW_INDEX` |
| Axis ticks | every 1 hour | `TICK_SECONDS` |
| Left label gutter | 6 characters | `GRAPH_OFFSET` |

The graph is always 37 columns of 20 minutes each — at most a **12-hour**
window. Columns are aligned to absolute wall-clock 20-minute boundaries, not to
"20 minutes ago" — the grid is stable between refreshes and only shifts when a
real 20-minute boundary passes.

### Dynamic NOW column

The NOW column (`│` in both graph rows) is **not fixed**. Its position is set by
the forecast horizon (`now_column()`):

* The forecast to the **right** of NOW is only as wide as it needs to be to
  reach the predicted `full` (charging) or `empty` (discharging) time —
  `ceil(eta / 20min)` columns, flush against the right edge. No unused future
  space is reserved beyond the ETA.
* Everything to the **left** of NOW is history. Whatever the forecast does not
  need is available to history, so a short ETA pushes NOW right and reveals more
  past; a long ETA pulls NOW left.
* NOW **never moves left of the graph midpoint** (`NOW_INDEX`,
  `TIME_COLUMNS // 2` = 18): at least half the width always stays available to
  history. A forecast longer than the right half (`GRAPH_WIDTH - 1 - NOW_INDEX`
  columns ≈ 6 h) is drawn only up to the right edge — the visible curve simply
  stops mid-slope. It is **not** compressed or rescaled to fit; the exact ETA
  and predicted clock time stay complete and authoritative in the right-hand
  label.
* When there is no meaningful forecast (battery full/stable, or no ETA yet), NOW
  sits at the far right edge and the whole width shows history.

History is always drawn to the left of NOW and forecast always to the right; the
title-line direction arrow sits directly above the NOW column and moves with it.
The axis ticks and labels follow the visible range, so history and forecast
share one time-to-screen mapping.

### Columns and sub-columns

Each column is one character. Solid history uses vertical block characters
(`▁▂▃▄▅▆▇█`, 16 half-levels across the two rows). Braille cells (history during
sleep, and all forecast) split each column into **2 sub-columns × 4 vertical dot
positions = 8 levels**, sampled at 5 minutes and 15 minutes into the 20-minute
column.

## What each cell means

| Cell | Drawn as | Meaning |
|---|---|---|
| Observed solid history | block char `▁`–`█`, gradient-colored | the collector was awake and measured this; median SoC of the 20-minute bucket |
| Observed history near empty | at least `▁`, using the actual SoC color | low battery was measured — **not** a gap |
| Sleep / hibernate | Braille, gradient-colored | a proven suspend/hibernate span; SoC interpolated between the pre- and post-sleep readings |
| Forecast | Braille, gradient-colored | projected SoC to the right of NOW |
| Unknown | blank (space) | the collector was not running, continuity broke with no sleep evidence, or the window reaches back before our earliest sample |

**The general rule: known data is always visible; a blank cell means unknown /
no reliable data.** Concretely:

- Valid observed solid history always shows at least the smallest block `▁`;
  exact 0% uses `#550A14`, while other low values use their actual SoC color.
- Valid sleep/hibernate Braille at 0% keeps one bottom Braille dot per
  sub-column.
- Valid forecast Braille at 0% keeps one bottom Braille dot per sub-column.
- An unknown span stays blank — whether it is a gap inside the recorded range or
  the stretch on the far left older than our earliest sample. Nothing is
  interpolated or extrapolated to fill it; a blank area is preferable to an
  invented line.

Above the minimum block, solid history retains its normal 16-level
quantization. Braille rows retain their separate 8-level geometry.

## Color gradient

SoC maps to an RGB gradient by linear interpolation between fixed stops
(`BATTERY_COLOR_STOPS`, 24-bit color):

| SoC | RGB | Hex |
|---:|---|---|
| 0% | 85, 10, 20 | `#550A14` |
| 25% | 155, 35, 30 | `#9B231E` |
| 50% | 175, 110, 25 | `#AF6E19` |
| 75% | 90, 130, 40 | `#5A8228` |
| 100% | 20, 105, 50 | `#146932` |

Values are clamped to 0–100 before lookup. A cell with unknown SoC is not
colored (and is blank anyway).

## Forecast behavior

The forecast is drawn when there is a usable ETA for the current charging or
discharging session. Its width is the columns needed to reach the ETA, capped at
the right half of the graph (see [Dynamic NOW column](#dynamic-now-column)); it
ends at the right edge. When the ETA fits, the last column lands on the
predicted time; when it is longer than the right half, the drawn curve stops
mid-slope at the edge and the right-hand text label still shows the full ETA.

- **Discharging:** SoC is projected toward 0% along the estimated slope. If the
  ramp reaches 0% before the edge it holds at 0%, drawn as a bottom Braille dot
  in deep red `#550A14`. The forecast is not artificially bent back up.
- **Charging:** SoC is projected toward 100% along the estimated slope, reaching
  a full column at the predicted full time when it fits within the right half.
- **No ETA / full on AC:** no forecast is drawn; NOW sits at the right edge and
  the width is given to history.

The forecast never reverses direction.

## Boundary smoothing

Sleep and forecast Braille share a small raster-shaping stage
(`_early_raster`) that shifts a clear monotone `round()` transition one
sub-column earlier so contours read cleanly at this resolution. Sleep cells get
two extra passes:

- `_sleep_residual_transfer` — exposes a shallow monotone trend across a flat
  sleep span without changing the total dot count;
- `_smooth_sleep_edges` — nudges a Braille edge by one dot toward directly
  adjacent solid history so the sleep segment joins the measured history
  visually.

A final pass restores the minimum one dot for any sub-column with a valid SoC,
so shaping can never make known data vanish.

## Title line, labels, and readouts

```
BATTERY                 SoC 72% ↓  12.4 W 😎
0h48             ▁▂▂▂▂▂▂▂▃▃▃▃▃▃▃│⣀          3h10 ~18:20
start            ███████████████│⣿⣿⣷⣶⣦⣤⣀⣀⣀⣀ empty
      ──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─
        07 08 09 10 11 12 13 14 15 16 17 18   SoH 94.3%
```

Here the ETA needs ten columns of forecast, so `NOW` sits ten from the right.
Records only reach back about five hours, so the columns further left than that
stay blank — nothing is invented to fill them.

- **Title:** `BATTERY` (`render_dashboard(heading=…)`, `SIMULATION` for the
  `simulate` facility), then `SoC N%`, then the direction arrow (`↑` charging,
  `↓` discharging, `·` idle) above NOW, then power. Power is `-- W` when
  unavailable, `X.X W` for a direct reading, `~X.X W` for a time-derived
  estimate. A power profile, when known, follows as an emoji face:
  🥵 performance, 😎 balanced, 😴 power-saver (`graph.POWER_PROFILE_FACES`).
  The face is one code point but two terminal cells wide, which the title
  layout accounts for; a missing or unrecognized profile shows no face.
- **Left of the graph rows:** time since the current session started
  (`format_duration`), with the label `start` under it. Blank when no session
  is open.
- **Right of the graph rows:** the ETA — remaining time and predicted clock
  time (`~HH:MM`) — with the label `full` (charging) or `empty` (discharging)
  under it. `--` when there is no estimate.
- **Axis:** hourly `┬` ticks with two-digit hour labels, in local time.
- **SoH:** `SoH X.X%` at the end of the label line, shown only when a
  State-of-Health value could be resolved (see
  [history-model.md](history-model.md#health--soh-event-storage)).

## `--unicode-probe`

`battery-status-tui --unicode-probe` prints the solid, Braille, joining,
power-profile, and axis glyphs the renderer uses, so you can check your terminal
font renders them
before relying on the graph.

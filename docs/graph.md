# The graph

The dashboard is five lines of text: a title line, two graph rows, an axis
line, and an axis-label line (which also carries the SoH readout). All of it is
produced by `graph.render_dashboard`.

## Geometry and timing

| Quantity | Value | Constant |
|---|---|---|
| Total columns | 37 | `GRAPH_WIDTH` (`TIME_COLUMNS + 1`) |
| Column width in time | 20 minutes | `COLUMN_SECONDS` |
| History region | 18 columns = 6 hours | `HISTORY_SECONDS` |
| NOW column | index 18 | `NOW_INDEX` |
| Forecast region | 18 columns = 6 hours | `FORECAST_SECONDS` |
| Axis ticks | every 1 hour | `TICK_SECONDS` |
| Left label gutter | 6 characters | `GRAPH_OFFSET` |

So the graph always spans a fixed **12 hours: 6 hours of history, the NOW
column, 6 hours of forecast**. Columns are aligned to absolute wall-clock
20-minute boundaries, not to "20 minutes ago" — the grid is stable between
refreshes and only shifts when a real 20-minute boundary passes.

### Fixed NOW column

The NOW column (`│` in both graph rows) is always at index 18 and always
represents the current time. History is drawn to its left, forecast to its
right. The title-line direction arrow sits directly above it. History never
crosses to the right of NOW; forecast never crosses to the left.

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
| Unknown | blank (space) | the collector was not running, or continuity broke with no sleep evidence |

**The general rule: known data is always visible; a blank cell means unknown /
no reliable data.** Concretely:

- Valid observed solid history always shows at least the smallest block `▁`;
  exact 0% uses `#550A14`, while other low values use their actual SoC color.
- Valid sleep/hibernate Braille at 0% keeps one bottom Braille dot per
  sub-column.
- Valid forecast Braille at 0% keeps one bottom Braille dot per sub-column.
- An unknown span stays blank.

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

The forecast is drawn when there is a usable ETA for the current session, or
when the battery is already full on AC.

- **Discharging:** SoC is projected toward 0% along the estimated slope. When it
  reaches 0% it **stays at 0% for the rest of the 6-hour forecast window**,
  drawn as a bottom Braille dot in deep red `#550A14`. The forecast is not
  truncated at the moment of predicted empty, and it is not artificially bent
  back up.
- **Charging:** SoC is projected toward 100%. When it reaches 100% it plateaus
  at a full column for the rest of the window.
- **Full on AC:** a flat 100% line across the whole forecast window.

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
BATTERY         SoC 72% ↓  12.4 W (balanced)
0h48  ▇▇▆▆▆▆▆▅▅▅▅▅▅▄▄▄▄▄│⡀                  3h10 ~16:10
start ██████████████████│⣿⣿⣶⣶⣤⣄⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀ empty
      ┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬
      07 08 09 10 11 12 13 14 15 16 17 18     SoH 94.3%
```

- **Title:** `BATTERY`, then `SoC N%`, then the direction arrow (`↑` charging,
  `↓` discharging, `·` idle) above NOW, then power. Power is `-- W` when
  unavailable, `X.X W` for a direct reading, `~X.X W` for a time-derived
  estimate. A power profile, when known, follows in parentheses.
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

`battery-status-tui --unicode-probe` prints the solid, Braille, joining, and
axis glyphs the renderer uses, so you can check your terminal font renders them
before relying on the graph.

# PROJECT_STATE

Durable technical handoff for `battery-status-tui`. Describes the **current**
implementation and the design constraints behind it. Read this before changing
the project. Update it when an accepted architectural or LOCKED decision changes.

---

## 1. Current baseline

- **Checkpoint commit:** `6e39d0ddc2ebb81c966fa7a0ee40edaceffb23c6`
  — *Add dynamic battery timeline simulation*
- **Production TUI purpose:** a compact terminal dashboard for one Linux
  laptop's internal battery — current SoC, charge/discharge direction, power
  draw, remaining-time ETA, State-of-Health, active power profile, and a
  12-hour Unicode graph that renders measured history, proven suspend/hibernate
  spans, forecast, and unknown gaps as visually distinct things.
- **Simulator purpose (`python -m battery_status_tui.simulate`):** a durable
  visual/manual regression-testing facility. It drives the **real** production
  renderer and the **real** remaining-time estimator with in-memory model
  objects so a developer can eyeball rendering scenarios (sleep drops, data
  gaps, forecast shapes, viewport behaviour) without waiting for real hardware
  events. It shows a `SIMULATION` heading so its output can never be mistaken
  for the live dashboard.
- **Test count:** 343 passing at this checkpoint
  (`PYTHONPATH=src python -m unittest discover -s tests`).
- **Dependencies:** Python standard library only. `pyproject.toml` declares
  `dependencies = []`; no non-stdlib import exists anywhere in
  `src/battery_status_tui/`. Requires Python ≥ 3.11.

---

## 2. Data / storage model (schema v4)

SQLite at `${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3`
(`--database` overrides). A fresh database is created at schema v4; legacy
v0/v1/v2 databases are refused by the viewer and converted only offline.

Three layers, read newest-first:

1. **Event-oriented persistent history — `state_events`.** An append-only log of
   *changes only* (AC online/offline, power-profile name, battery
   presence/state). One sample per minute produces a row only when something
   actually changed.
2. **Permanent hourly aggregates — `hourly_history`.** One immutable row per UTC
   hour, finalized when a later poll crosses the hour boundary. Enforced
   partition invariants as CHECK constraints:
   `observed_ms + sleep_ms + unknown_ms = 3600000`; state, AC, and
   power-method durations each sum to `observed_ms`. SoC geometry
   (`soc_start/end/min/max`, `soc_integral_percent_ms`) exists iff
   `observed_ms > 0`. This is the **permanent canonical record**; older history
   exists only at this resolution.
3. **`recent_series` — fine-grained sub-hour checkpoint data.** A compact binary
   blob (`recent_series.py`, magic `BRS1`) inside each rotating checkpoint
   generation (newest 3 kept, each SHA-256 verified). Holds the recent sub-hour
   points not yet folded into an `hourly_history` row; recoverable after a crash
   or restart.

**Retention of `recent_series`:**
`MAX_WINDOW_MS = 12 h + one 20-minute bucket = 12 h 20 min` (44,400,000 ms),
equal to `(graph.MAX_SPAN_SECONDS + graph.COLUMN_SECONDS) * 1000`.

**Why the fine window exists:** the graph can show at most a 12-hour history
span, and its columns are aligned to absolute wall-clock 20-minute boundaries,
so the leftmost visible column can begin just before `now − 12 h`. Keeping
12 h 20 min of real sub-hour samples means the entire visible viewport is backed
by genuine measurements, while long-term history stays compact as one row per
hour. **There is no permanent 20-minute historical layer** and none is planned;
sub-hour shape older than the window is irreversibly aggregated away.

**Observed / sleep / unknown stay semantically distinct** at every layer:
measured time, reconstructed suspend/hibernate time, and genuinely unknown time
are separate quantities in the hourly partition and separate visual treatments
in the graph (see §3).

**Near-complete finalized hours.** When building the graph, a finalized
`hourly_history` row contributes its `soc_start` / `soc_end` endpoint samples
*unless* that hour is already covered by `recent_series` samples **or**
`observed_ms < NEAR_COMPLETE_OBSERVED_MS` (`HOUR_MS − 5 min`, i.e. under
55 minutes observed). So an hour that merely missed a poll or two still shows
its endpoints and the wider dynamic viewport has no blank band between the
hourly aggregates and the sub-hour history; an hour with substantial sleep or
unknown time does not contribute misleading endpoints.
(`v1_history.py::V1History._history`.)

**Health data — `battery_health`.** Slow-changing capacity/wear facts
(`energy_full_wh`, `energy_full_design_wh`, `charge_full_ah`, `cycle_count`,
`voltage_design_v`, …) appended only when a value actually changes, with
`source` / `provenance`. SoH shown on the axis-label line; `energy_full_wh`
feeds the energy-rate ETA fallback.

**One writer.** `battery-status-tui --sample`, invoked once per minute by a
systemd **user** timer, is the sole write path (single `BEGIN IMMEDIATE`
transaction per poll). All other use — interactive, `--once`, piped, `--diagnose`,
the simulator — is a read-only viewer (`?mode=ro` + `PRAGMA query_only=ON`).

---

## 3. Rendering semantics — LOCKED

**This section is LOCKED. Do not change these meanings, the raster rules, the
interpolation behaviour, the residual-transfer behaviour, or the colour anchors
without inspecting current source and tests first and recording the decision
here.**

| Visual | Meaning |
|---|---|
| Solid / massive block cells | Directly measured history — **or** an explicit *normal* synthetic trajectory when the whole dashboard is clearly marked `SIMULATION`. |
| Colour-gradient Braille, left of `NOW` | A **known** sleep/suspend reconstruction interpolated between two reliable endpoint readings. |
| Neutral-gray Braille, left of `NOW` | An **unknown / unreliable** trajectory drawn straight between two known endpoint SoC checkpoints (simulator `:nodata`). |
| Blank cell | Unknown, with no reliable later endpoint — including real history gaps and the stretch older than the earliest sample. Nothing is interpolated or extrapolated to fill it. |
| Braille, right of `NOW` | Forecast. |

Raster / drawing rules:

- Sleep and forecast both use the established **2×4 Braille raster / fill**
  principles (`_early_raster`, `_braille_fill_levels`,
  `_keep_valid_subcolumns_visible`). A complete Braille cell is **U+28FF**
  (`⣿`). A valid subcolumn never quantizes down to invisible.
- **Sleep** reconstruction additionally uses the sleep-specific contour:
  `_early_raster` → `_sleep_residual_transfer` → `_smooth_sleep_edges` →
  `_keep_valid_subcolumns_visible`.
- **Forecast** must **not** inherit the sleep-specific cosmetic residual
  transfer or edge smoothing. Path: `_early_raster` →
  `_keep_valid_subcolumns_visible` only.
- **Unknown / `:nodata`** must **not** use the sleep-specific
  residual-transfer / smoothing behaviour either. Same basic path as forecast,
  then each affected column's percentage is tagged with the
  `UNKNOWN_TRAJECTORY` sentinel so `_style_battery` paints it neutral gray
  (**ANSI 256 grayscale index 238**, `UNKNOWN_GRAY = "\x1b[38;5;238m"`) instead
  of the SoC gradient.
- Sleep and forecast retain the normal continuous SoC colour gradient.

**SoC colour gradient anchors** (`graph.BATTERY_COLOR_STOPS`, RGB, verified):

| SoC | Hex | RGB |
|---|---|---|
| 0 % | `#550A14` | (85, 10, 20) |
| 25 % | `#9B231E` | (155, 35, 30) |
| 50 % | `#AF6E19` | (175, 110, 25) |
| 75 % | `#5A8228` | (90, 130, 40) |
| 100 % | `#146932` | (20, 105, 50) |

Measured 0 % renders as the smallest solid block in `#550A14` — distinct from an
unknown blank. Valid sleep/forecast Braille at 0 % keeps one bottom dot per
subcolumn.

---

## 4. Dynamic NOW viewport — LOCKED

**This section is LOCKED.**

Graph geometry (verified in `graph.py`):

| Constant | Value |
|---|---|
| `TIME_COLUMNS` | 36 |
| `GRAPH_WIDTH` (`TIME_COLUMNS + 1`) | 37 |
| `COLUMN_SECONDS` | 1200 (20 min) |
| `MAX_SPAN_SECONDS` (`TIME_COLUMNS * COLUMN_SECONDS`) | 43200 (12 h) |
| `NOW_INDEX` (`TIME_COLUMNS // 2`) | 18 — graph midpoint |
| `TICK_SECONDS` | 3600 |
| `GRAPH_OFFSET` (left label gutter) | 6 |

- The `NOW` column (`│` in both graph rows) separates measured history (left)
  from forecast (right). History never crosses right of it; forecast never
  crosses left.
- `NOW` moves **dynamically**: `now_column()` = `GRAPH_WIDTH - 1 -
  _forecast_span_columns(current, estimate)`. The forecast is only as wide as it
  needs to reach the predicted full/empty time (`ceil(eta / COLUMN_SECONDS)`
  columns), flush against the right edge; every remaining column goes to
  history.
- `NOW` may **never** move left of the graph midpoint (`NOW_INDEX` = 18):
  `_forecast_span_columns` is capped at `GRAPH_WIDTH - 1 - NOW_INDEX` = 18
  columns (≈ 6 h). At least half the width is therefore always history.
- A forecast longer than the right half is **clipped at the right edge** — the
  drawn curve simply stops mid-slope. It is **not** compressed or rescaled.
- The **textual ETA and predicted clock time in the right-hand label stay
  complete and authoritative** even when the graphical forecast is clipped.
- With no usable forecast (battery full/stable, no ETA, or session is neither
  charging nor discharging) `NOW` sits at the far-right column and the whole
  width shows history.
- The title-line direction arrow (`↑` / `↓` / `·`) sits directly above the
  actual `NOW` column and moves with it; axis ticks/labels follow the visible
  range, so history and forecast share one time-to-screen mapping.

**Why the midpoint cap exists:** without it, a long ETA (e.g. a slow charge from
low SoC) would push `NOW` far left and consume the measured-history view that is
the dashboard's main value. Guaranteeing half the width to history keeps the
recent past readable regardless of the forecast horizon.

---

## 5. Power-profile indicator

`graph.POWER_PROFILE_FACES` (single source of the mapping):

| Profile | Face |
|---|---|
| `performance` | 🥵 |
| `balanced` | 😎 |
| `power-saver` | 😴 |
| unknown / missing | *(no face)* |

These are **emoji and render two terminal cells wide**. The title layout
measures terminal-cell width (`graph.display_width` / `_char_width`, backed by
`unicodedata`) so the face does not disturb the SoC, wattage, or `NOW`-arrow
columns. Do **not** replace them with assumed one-cell glyphs without explicit
design work and terminal-rendering testing — an earlier one-cell attempt was
rejected. `--unicode-probe` prints these faces among the glyphs to check.

---

## 6. Simulator

`python -m battery_status_tui.simulate` — one subcommand, `sleep-drop`, with two
modes. Both use the **real** `graph.render_dashboard` and the **real**
`estimate.estimate_remaining`; neither starts a collector, timer, or systemd
unit; the simulator never computes its own ETA.

- **Public live-history option is `--simulate`.** There is **no** public
  `--from-live` option and no transitional alias.
- **Default simulator heading is `SIMULATION`**; production heading stays
  `BATTERY` (`render_dashboard(heading=…)`).
- **Fully synthetic deterministic mode** (`sleep-drop` with no `--simulate`):
  builds a measured-history → proven sleep SoC drop → measured-resume scenario
  from scratch in memory. Opens **no** database (there is no `--database`
  option). Output is deterministic.
- **`--simulate` mode:** reads the genuine live view once via the production
  read path (`v1_runtime.read_v1_view` → `V1Storage.reader`), which opens
  `file:<path>?mode=ro` and runs `PRAGMA query_only=ON` — writing is
  technically impossible. It **never** initializes, migrates, creates, or writes
  the production database; a **missing database is reported, never created**
  (`_read_live_view` checks `path.is_file()` and raises `SimulationError`). The
  independently running collector is unaffected. Real history, current state,
  session, sleeps, battery identity, health and power profile are taken
  **verbatim**; only the requested future blocks are synthetic.

### Timeline DSL

```
--simulate <duration>[:<type>][=<soc>] ...  [ac[=<watts>w] | dc[=<watts>w]]
```

- **Sequential blocks:** each `<duration>` is measured from the *previous*
  checkpoint, not the original NOW. `<duration>` is `2h` / `1h24m` / `45m` /
  `90s` / `2h05m`, positive, may be shorter than one 20-minute column.
- **Types:** *(none)* = ordinary active interval (SoC drawn straight between
  endpoints, extends solid history); `:sleep` = known sleep interval (locked
  colour-gradient Braille reconstruction); `:nodata` = endpoints known but
  trajectory unknown (neutral-gray straight Braille via `unknown_intervals`).
- **SoC:** `=82%` / `=100%` absolute (rejected outside 0–100); `=-20%` / `=+30%`
  explicit relative percentage-point change (clamped 0–100); omitted → SoC
  unchanged across the block.
- **Window limit:** the total simulated timeline may not exceed
  `graph.MAX_SPAN_SECONDS` (12 h, the shared production maximum graph span).
  Over-length input is **rejected before rendering** — nothing is silently
  truncated or compressed (`parse_timeline`).

### Final power-source context (optional last token)

`ac` · `dc` · `ac=<W>w` · `dc=<W>w` (wattage must be numeric, finite, > 0;
`dc=0w`, `ac=-5w`, `dc=nanw` are rejected).

- **No final token:** keep the genuine live AC/DC context and the genuine usable
  live power magnitude.
- **Bare `ac` / `dc`:** switch the context, but reuse the genuine usable live
  power magnitude as the default rate — *even when the context is reversed*. If
  the genuine measurement has no usable magnitude (`None`), a bare `ac`/`dc`
  invents none.
- **Explicit `=<W>w`:** overrides the magnitude.
- The final magnitude affects only the fictitious final `NOW` and the
  production-estimator forecast from it; it never alters the preceding synthetic
  timeline.

Do **not** add a `profile=` token to the DSL; the live profile is retained by
default.

---

## 7. Production / simulation isolation — LOCKED

**This section is LOCKED.**

- Simulator-specific unknown intervals (`unknown_intervals` /
  `UNKNOWN_TRAJECTORY`) must **not** alter default production rendering. The
  parameter defaults to `()`, the sentinel is never constructed on the
  production path, and `_style_battery` behaves identically without it.
- Simulation must **not** write, migrate, or create the production database, and
  must not touch any writer / checkpoint / metadata path.
- Production collection and storage behaviour must **not** be changed merely to
  make a simulation easier. If a simulation needs something the production model
  does not already expose, prefer building it in-memory in `simulate.py`.
- Synthetic visualization must stay visibly distinguishable from genuine
  measurement where it matters: `SIMULATION` heading, and `:nodata` gray vs
  `:sleep` gradient vs measured solid.

---

## 8. Known non-blocking cleanup

Recorded for awareness only. **Not** tasks to fix automatically; do not bundle
these into unrelated work.

- `graph.HISTORY_SECONDS` (`6 * 3600`) is currently referenced only by tests;
  no production `src/` code uses it.
- `graph.visible_len` is an unused backward-compatibility alias for
  `display_width`.
- `simulate.SCENARIOS` is a vestigial dispatch dict — `run()` calls the builders
  directly and nothing reads it.
- Internal names `build_from_live_timeline` / `test_from_live_*` retain legacy
  "from_live" wording from a superseded design; the public option is
  `--simulate`.
- Synthetic-only `sleep-drop` flags (`--start-soc`, `--resume-soc`,
  `--sleep-hours`, `--pre-hours`, `--post-hours`, `--after`, `--charge-rate`,
  `--discharge-rate`, `--now`) share the subparser with `--simulate` and are
  silently ignored in timeline mode.
- A known minor cosmetic sleep-Braille edge detail exists. It should **not**
  trigger a renderer redesign without a demonstrated, test-backed regression.

---

## 9. Post-v1 wishlist

The repository documented no roadmap/wishlist before this file, so nothing
pre-existing is carried over. Keep this list factual and scoped to battery
telemetry; do not add unrelated features.

### Battery Care / Battery Health analysis

Long-term, confidence-aware battery-preservation analysis built from the
telemetry already stored (and telemetry that could be added to the collector
without changing the LOCKED rendering model). Signals of interest:

- SoH trend over weeks/months/years;
- SoC-zone dwell time (time spent under 20 %, above 80 %, above 95 %);
- deep discharges;
- prolonged high SoC;
- charging power — slow vs fast charging;
- discharge power / load;
- power profile during DC operation;
- cycle count;
- battery temperature where reliably available;
- battery type / chemistry;
- BMS / vendor behaviour and capabilities;
- supported charge thresholds;
- correlations among the above over long spans.

**Design principle:** `detect → classify → determine confidence → analyse →
advise`. Do not give chemistry- or hardware-specific recommendations unless the
battery identification and its capabilities are known reliably enough. When
confidence is insufficient, present factual observations rather than advice.
Never present battery folklore as fact.

### Adjacent smaller items

- Optional charge-threshold reporting/awareness (read-only) where the driver
  exposes it.
- Longer-horizon history views built strictly on the permanent hourly
  aggregates (no new permanent sub-hour layer).

---

## 10. Change discipline

- **Inspect current source, tests, and this file before changing any LOCKED
  behaviour** (§3, §4, §7). The constants and colour anchors here are verified
  against the checkpoint but re-check them.
- **Preserve the semantic distinction** between measured / sleep / unknown /
  forecast at every layer — storage partition and visual treatment alike.
- **Prefer a regression test first** for any renderer change: reproduce the
  current output, then change it, so a reviewer can see exactly what moved.
- **Use Git checkpoints** for substantial behaviour changes; keep each
  reviewable checkpoint self-consistent (tests green, docs matching).
- **Update this file** whenever an accepted architectural or LOCKED decision
  changes, in the same change that makes it.

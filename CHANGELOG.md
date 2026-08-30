# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/). This project follows
[Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- The graph viewport is now dynamic. The `NOW` marker is no longer fixed at the
  centre: the forecast to its right is sized to reach the predicted full/empty
  time and no further, and every remaining column is given to history on the
  left. A short ETA moves `NOW` toward the right edge and reveals more history;
  with no ETA `NOW` sits at the right edge. The axis and the title arrow follow
  the marker, and history and forecast share one time-to-screen mapping. `NOW`
  never moves left of the graph midpoint, so at least half the width always
  shows history; a forecast longer than the right half is drawn up to the right
  edge (stopping mid-slope) while the text label keeps the full ETA.
- The discharge forecast now runs down to 0% at the predicted empty time and
  the charge forecast up to a full column at the predicted full time, instead of
  holding a flat plateau across a fixed 6-hour window. A battery already full on
  AC draws no forecast.
- The active power profile is shown as an emoji face — 🥵 performance, 😎
  balanced, 😴 power-saver — instead of the spelled-out name in parentheses. The
  title layout measures terminal-cell width, so the two-cell face does not
  disturb the SoC, wattage, or NOW-arrow columns. A missing or unrecognized
  profile shows no face.
- Finalized hourly history that missed a poll or two (still far below the
  unknown-gap threshold) now contributes its endpoint samples to the graph, so
  the wider dynamic viewport no longer shows a blank band between the hourly
  aggregates and the sub-hour history. Hours with real sleep or a wide unknown
  span are unchanged.
- The checkpoint's compact `recent_series` now retains 12 hours 20 minutes of
  sub-hour points (was 8 hours), matching the widest history the dynamic graph
  can draw plus one column of clock-alignment slack, so the whole visible
  history is backed by real measurements. `hourly_history` remains the permanent
  canonical record; no persistent sub-hour layer is added. Applies to samples
  collected from now on.

### Added

- `python -m battery_status_tui.simulate` — a dashboard-simulation facility for
  visual/manual regression testing. It drives the real renderer and estimator
  with in-memory model objects, shows a `SIMULATION` heading, and never starts a
  collector or timer. The deterministic synthetic `sleep-drop` scenario renders
  measured history → a proven sleep SoC drop → measured resume through the
  locked sleep-Braille path and opens no database. `--simulate
  <duration>[:sleep|:nodata][=<soc>] ... [ac[=<watts>w]|dc[=<watts>w]]` appends
  a sequential hypothetical timeline to the *genuine* live graph — real history,
  SoC, colour, sleeps, session, identity, health and profile kept verbatim;
  absolute / relative-point SoC; `:sleep` gets the locked colour-gradient
  Braille reconstruction while `:nodata` draws a straight-line Braille
  connection between its known endpoints in neutral light gray (a genuine
  history gap with no reliable endpoint still stays blank); the fictitious
  final state and forecast come from the production estimator. The total
  timeline is
  validated against `graph.MAX_SPAN_SECONDS`. The production database is read
  once, strictly read-only (`mode=ro`, `PRAGMA query_only=ON`, never created,
  no writer/migration path), leaving the live collector untouched. See the
  README "Simulating the dashboard" section.

## 1.0.0 — unreleased

First public release.

### Monitoring and display

- Compact terminal dashboard: current SoC, charge/discharge direction, power
  draw, remaining-time ETA, State-of-Health, and active power profile.
- Fixed 12-hour graph: 6 hours of measured history, a fixed `NOW` column, and
  6 hours of forecast, on a stable 20-minute grid aligned to clock boundaries.
- Distinct rendering of observed history (solid blocks), proven
  suspend/hibernate spans (Braille), forecast (Braille), and unknown gaps
  (blank). Known data is always visible; a blank cell means unknown.
- Measured empty battery (0%) renders as the smallest solid block in deep red
  `#550A14`, distinct from an unknown gap.
- Discharge forecast holds at 0% in `#550A14` across the whole forecast window
  instead of being truncated at predicted-empty; charge forecast plateaus at
  100%.
- SoC color gradient with fixed stops `#550A14` / `#9B231E` / `#AF6E19` /
  `#5A8228` / `#146932`.
- `start` / `full` / `empty` session and ETA labels; power-profile shown beside
  the power reading; SoH shown on the axis-label line.

### Data sources and estimation

- Field-level fusion of `/sys/class/power_supply` and UPower; UPower-only
  fallback when sysfs is unavailable; peripheral batteries excluded.
- Layered power resolver: `power_now` → current × voltage → UPower energy-rate →
  time-derived energy delta → time-derived charge delta. Direct readings render
  as `X.X W`, estimates as `~X.X W`, unavailable as `-- W`.
- Multi-battery aggregation with capacity-weighted SoC and summed energy.
- Remaining-time ETA from a Theil–Sen slope over 5-minute buckets of the current
  session, with UPower time-remaining and energy-rate fallbacks.

### Storage and reliability (schema v4)

- Event-based persistence: an append-only `state_events` log records only
  changes (AC, power profile, battery presence/state).
- Canonical immutable hourly aggregates with enforced partition invariants
  (`observed + sleep + unknown = 1 hour`; state, AC, and power-method durations
  each sum to observed time).
- Rotating crash-safe checkpoint generations (newest 3 kept), each verified by a
  SHA-256 payload digest on write and on recovery.
- `recent_series`: a bounded 8-hour compact sub-hour history carried in the
  checkpoint, recoverable after a crash or restart.
- WAL journal mode, `synchronous = FULL`, single `BEGIN IMMEDIATE` transaction
  per poll, read-only reader connections with schema and `quick_check`
  validation.
- Automatic recovery: on start the collector selects the newest intact
  checkpoint generation; finalized hours are immutable and never double-counted.
- Suspend/hibernate reconstruction from monotonic-vs-boottime clocks and the
  kernel journal, including hibernation across a full cold boot.
- Sticky battery identity (`base|model|serial`) that tolerates drivers dropping
  optional metadata but detects a real battery swap.
- Slow-changing capacity/wear stored as `battery_health` change events.

### CLI

- `--once`, `--sample`, `--interval`, `--database`, `--diagnose`,
  `--unicode-probe`, `--version`.
- The systemd timer invoking `--sample` once per minute is the canonical and
  sole writer; no resident daemon is used.
- Ordinary interactive use, `--once`, and non-TTY output are read-only SQLite
  viewers. Multiple viewers are safe and never poll collector sources.
- `--diagnose` may inspect live hardware but never records a sample or creates
  or modifies the history database.
- Viewers wait for the first checkpoint and report stale data after
  `max(3 × configured interval, 180 seconds)` without moving stale ETA state.
- A legacy schema-v0/v1/v2 database is refused by the viewer with explicit
  conversion guidance and is never migrated automatically.

### Migration tooling (pre-1.0 users only)

- Offline `tools/convert_v2_to_v1.py` converter (read-only source, new
  destination file).
- Independent `tools/validate_pre_v1_conversion.py` validator that re-derives
  every fact from the source without importing the converter.
- Validated, lock-held pre-migration backup helper.

### Packaging

- MIT licensed. Python standard library only. No root, no system-wide daemon.
- Packaged `battery-status-tui` console entry point and systemd user timer for
  one short-lived sample per minute.

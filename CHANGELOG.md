# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/). This project follows
[Semantic Versioning](https://semver.org/).

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
- Suspend/hibernate reconstruction from three independent sources (monotonic vs.
  boottime clocks, the logind `PrepareForSleep` signal, and the kernel
  journal), including hibernation across a full cold boot.
- Sticky battery identity (`base|model|serial`) that tolerates drivers dropping
  optional metadata but detects a real battery swap.
- Slow-changing capacity/wear stored as `battery_health` change events.

### CLI

- `--once`, `--sample`, `--interval`, `--database`, `--diagnose`,
  `--unicode-probe`, `--version`.
- Non-TTY stdout renders once automatically.
- The normal CLI runs natively on schema v4; a legacy schema-v0/v1/v2 database
  is opened on the legacy runtime and is never migrated automatically.

### Migration tooling (pre-1.0 users only)

- Offline `tools/convert_v2_to_v1.py` converter (read-only source, new
  destination file).
- Independent `tools/validate_pre_v1_conversion.py` validator that re-derives
  every fact from the source without importing the converter.
- Validated, lock-held pre-migration backup helper.

### Packaging

- MIT licensed. Python standard library only. No root, no system-wide daemon.
- Optional systemd user timer for one sample per minute.

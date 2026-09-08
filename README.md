# battery-status-tui

`battery-status-tui` is a compact, standalone battery monitor for Linux. It
records low-overhead local history, separates charging and discharging sessions,
reconstructs suspend and hibernate gaps, and renders up to 15 h 40 m of context —
measured history and a forecast that reaches the predicted full/empty time — in
the compact dashboard and an optional muted viewer footer.

It uses only the Python standard library, needs no root, and runs no
system-wide daemon. History lives in a single SQLite file in your XDG state
directory and is never uploaded. A dashboard or shell widget can consume the
database later without coupling to the collector.

![battery-status-tui dashboard](docs/assets/battery-status-tui.png)

> **Note:** this screenshot predates the current renderer (it still shows a
> centred `NOW` marker, a flat forecast plateau, and a spelled-out power
> profile). It is illustrative only and will be refreshed. The example under
> [Reading the graph](#reading-the-graph) reflects the current output.

## Features

- One glance: current SoC, charge/discharge direction, power draw, ETA,
  State-of-Health, and active power profile.
- A graph up to 15 h 40 m wide on a stable 20-minute grid: a dynamic `NOW` marker
  with history on the left and a forecast on the right sized to the ETA.
- Honest history: measured time, proven sleep/hibernate time, and unknown gaps
  are visually distinct. Known data is always visible; blank means unknown.
- Field-level fusion of `/sys/class/power_supply` and UPower, with a layered
  power resolver that marks estimates as approximate.
- Robust ETA from a Theil–Sen session trend, not a single instantaneous rate.
- Crash-safe schema-v4 storage: append-only event log, immutable hourly
  aggregates, rotating verified checkpoints, and automatic recovery.
- Suspend/hibernate reconstruction from clocks and the kernel journal —
  including hibernation across a cold boot.
- A built-in simulator that drives the real renderer for visual testing without
  waiting for a real battery event.

## Requirements

- Linux with `/sys/class/power_supply` **and/or** UPower (`upower` CLI).
- Python 3.11 or newer, plus `pip` for installation. The application itself has
  no third-party runtime packages.
- A terminal with Unicode (block + Braille glyphs) and 24-bit color for the
  graph. Check yours with `battery-status-tui --unicode-probe`.
- Optional, each degrades gracefully if absent:
  - `journalctl` — durable suspend/hibernate reconstruction;
  - `powerprofilesctl` / `busctl` / `/sys/firmware/acpi/platform_profile` —
    power-profile display.

## Installation

From a release archive or Git checkout, install the console entry point into
your user prefix:

```bash
cd battery-status-tui
if [ -L ~/.local/bin/battery-status-tui ]; then rm ~/.local/bin/battery-status-tui; fi
python3 -m pip install --user .
test -x ~/.local/bin/battery-status-tui
```

Ensure `~/.local/bin` is on `PATH`. No root access is needed. The conditional
removal only clears a pre-1.0 development symlink if one is present; it never
removes an installed regular executable.

## Quick start

```bash
battery-status-tui              # interactive read-only viewer
battery-status-tui --once       # read-only dashboard, then exit
battery-status-tui --sample     # collect and record one sample
battery-status-tui --diagnose   # detailed source and health readout
```

When stdout is not a terminal, the program renders once and exits, so
`battery-status-tui | cat` and cron-style capture work without a flag.

## How it works

Collection and viewing are independent:

- **The systemd user timer collects.** It runs one short-lived
  `battery-status-tui --sample` process per minute, which is the sole writer to
  the history database. History advances only while the timer (or an equivalent
  regular `--sample`) is active.
- **The dashboard only reads.** Plain `battery-status-tui`, `--once`, and piped
  output are read-only viewers of the stored state. Running or closing a viewer
  never starts, stops, or affects collection, and you do not need to keep a
  viewer open for history to be recorded.
- `battery-status-tui --once` renders one dashboard from the stored state and
  exits.

## Keep history running

Install and enable the collector timer:

```bash
install -Dm644 systemd/battery-status-tui.service ~/.config/systemd/user/battery-status-tui.service
install -Dm644 systemd/battery-status-tui.timer ~/.config/systemd/user/battery-status-tui.timer
systemctl --user daemon-reload
systemctl --user enable --now battery-status-tui.timer
```

The service executes `~/.local/bin/battery-status-tui --sample`. Inspect it with
`systemctl --user status battery-status-tui.timer` and
`journalctl --user -u battery-status-tui.service`.

To update from a newer checkout or release archive:

```bash
python3 -m pip install --user --upgrade .
install -Dm644 systemd/battery-status-tui.service ~/.config/systemd/user/battery-status-tui.service
install -Dm644 systemd/battery-status-tui.timer ~/.config/systemd/user/battery-status-tui.timer
systemctl --user daemon-reload
systemctl --user restart battery-status-tui.timer
```

To remove it:

```bash
systemctl --user disable --now battery-status-tui.timer
rm ~/.config/systemd/user/battery-status-tui.{service,timer}
systemctl --user daemon-reload
python3 -m pip uninstall battery-status-tui
```

Nothing is installed or enabled automatically.

## CLI options

| Option | Effect |
|---|---|
| *(none)* | Read-only interactive dashboard; redraws committed SQLite state every `--interval` seconds. `Ctrl-C` to exit. |
| `--once` | Print one read-only dashboard from the latest checkpoint and exit. |
| `--sample` | Take one sample, print a single terse line (`<epoch> <soc>% <state> <power>`), exit. Used by the systemd timer. |
| `--interval SECONDS` | Interactive refresh interval (default `60`). |
| `--database PATH` | Use an alternate SQLite history file (default `${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3`). |
| `--power-decimals N` | Set interactive title power precision (default `1`). |
| `--show-persisted-power` | Diagnostic: prepend minute-level persisted power to the live title power. |
| `--show-weighted-power` | Diagnostic: append a weighted live-power value to the live title power. |
| `--weighted-power-samples N` | Set the diagnostic weighted-power window (default `5`). |
| `--diagnose` | Inspect live sources and print power, health, session, identity, and database details without collecting or modifying the history database. |
| `--unicode-probe` | Print the block/Braille/profile/axis glyphs the renderer uses, to verify terminal font support. |
| `--version` | Print the version and exit. |

`--once`, `--sample`, `--diagnose`, and `--unicode-probe` are mutually
exclusive.

The normal interactive title contains only current live power. Persisted and
weighted values appear only when their diagnostic options are explicitly set;
these live diagnostic options do not change collection or stored history.

## Reading the graph

```
BATTERY                         12.4W ↓ 72% P
                    ▁▂▂▂▂▂▂▂▃▃▃▃▃▃▃│⣀
0h48m               ███████████████│⣿⣿              3h10m
start               ███████████████│⣿⣿⣷⣶⣦⣤⣀⣀⣀     empty
      ──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬
SoH   03 04 05 06 07 08 09 10 11 12 13 14 15 16 17
65.2% Sun 6 Sep 2026 06:07 · (1m) refresh in 47s
```

The last line is viewer chrome, not part of the graph: local date and time
(minute precision), the configured `--interval`, and the countdown to the next
read of stored state. It is omitted on a terminal too short to hold it, and
drops the date/time first if the pane is too narrow.

- **Solid blocks** (`▁`–`█`) are measured history — time the collector was awake
  and recording. They are colored by SoC.
- **Colour-gradient Braille left of `NOW`** is a reconstructed sleep/suspend
  span: the laptop was proven asleep (clock jump or journal record) and the SoC
  is interpolated between the readings before and after.
- **Braille right of `NOW`** is the forecast.
- **Blank** means unknown — the collector was not running, or continuity broke
  with no sleep evidence. Nothing is drawn to fill it.
- A **neutral-gray** Braille trajectory means the endpoints are known but the
  path is not. This is currently produced mainly by the
  [simulator](#simulating-the-dashboard); genuine history with no reliable later
  reading stays blank.
- **`NOW` (`│`) moves.** The forecast to its right is only as wide as it needs
  to reach the predicted `full`/`empty` time; everything else is history. A
  short ETA pushes `NOW` right and reveals more past; with no ETA `NOW` sits at
  the far-right edge. `NOW` never moves left of the graph midpoint, so at least
  half the width is always history. A forecast longer than that half is clipped
  at the right edge — the drawn curve stops mid-slope — while the **text ETA
  duration stays complete and authoritative**. The title arrow sits
  above `NOW`.
- **`start` / `full` / `empty`.** Left of the rows: time since the current
  session began (`start`). Right: the estimated remaining duration,
  labelled `full` when charging or `empty` when discharging; `--` while a
  newly detected live direction is waiting for compatible persisted ETA data.
  A steady persisted session with no usable estimate shows `n/a`.
- **SoH.** `SoH` at the left of the time-label row with `X.X%` directly below it
  on the footer row when a value can be
  resolved from capacity vs. design capacity.
- **Power profile.** A one-cell `P` after the wattage: xterm 238 for power-saver,
  244 for balanced, and 252 for performance. Missing or unknown shows no `P`.
- **Power source.** `X.XW` is a direct reading, `~X.XW` a time-derived
  estimate, `--W` no usable value. The rendered form has no space before `W`;
  values below 100 W use one decimal and values at least 100 W use whole watts.

Exact geometry, the SoC colour gradient, and the raster rules are in
[docs/graph.md](docs/graph.md).

## Simulating the dashboard

The simulator is a **separate entry point** —
`python -m battery_status_tui.simulate`, not the `battery-status-tui` command.
It renders the dashboard from a scenario so you can eyeball graph behaviour
without waiting for a real battery event. It drives the **real** production
renderer and remaining-time estimator with in-memory data, shows a `SIMULATION`
heading so the output is never mistaken for the live dashboard, and never starts
a collector or timer.

```bash
# deterministic synthetic scenario: measured history -> a proven 6 h sleep
# dropping SoC 97% -> 40% -> measured resume. No database is touched.
PYTHONPATH=src python3 -m battery_status_tui.simulate sleep-drop
PYTHONPATH=src python3 -m battery_status_tui.simulate sleep-drop \
    --start-soc 100 --resume-soc 25 --sleep-hours 8 --after discharging

# --simulate: keep the genuine live graph, then append a hypothetical timeline
PYTHONPATH=src python3 -m battery_status_tui.simulate sleep-drop \
    --simulate 2h=50% 3h:sleep=-20% 1h:nodata 45m=82% ac
PYTHONPATH=src python3 -m battery_status_tui.simulate sleep-drop \
    --simulate 35m=-4% 3h12m:sleep=-28% 27m=+8% 1h18m:nodata=-12% 2h05m=82% ac=24.2w
```

**Synthetic mode** builds the `sleep-drop` scenario entirely in memory. It has
no `--database` option and opens no history database; output is deterministic.

**`--simulate` mode** anchors to your genuine live dashboard — real history,
SoC, colours, existing sleeps, session, battery identity, health and power
profile are kept verbatim — and appends a sequential hypothetical timeline.
Grammar:

```
--simulate <duration>[:<type>][=<soc>] ...  [ac[=<watts>w] | dc[=<watts>w]]
```

| Token part | Meaning |
|---|---|
| `<duration>` | `2h` `1h24m` `45m` `90s` `2h05m` — measured from the *previous* block, not the original `NOW`; positive; may be shorter than one 20-minute column |
| `:sleep` | a known sleep/suspend interval — colour-gradient Braille between the endpoints |
| `:nodata` | endpoints known, trajectory not — a straight neutral-gray Braille connection, visually distinct from `:sleep` |
| *(no type)* | an ordinary active interval; SoC is drawn straight between the endpoints |
| `=82%` | end the block at that **absolute** SoC |
| `=-20%` / `=+30%` | change the preceding SoC by that many **percentage points** (clamped 0–100) |
| *(no `=soc`)* | SoC unchanged across the block |
| final `ac` / `dc` | power-source context at the fictitious `NOW`; omitted keeps the genuine live context. The genuine live power magnitude is reused as the rate, even when the context is reversed; if the live reading has no usable magnitude, none is invented. |
| final `ac=24.2w` / `dc=8.3w` | additionally set an explicit battery-power magnitude (positive) for the fictitious `NOW` |

The final block's end is the fictitious `NOW`; the state and forecast there come
from the **production estimator** — the simulator never computes its own ETA.
The whole timeline must fit the 15 h 40 m graph window or the command is rejected
before rendering; nothing is truncated or rescaled.

Two optional flags override title values (they are **CLI flags, not timeline
tokens** — there is no `profile=` or `health=` in the grammar):

| Flag | Synthetic default | `--simulate` default | Effect |
|---|---|---|---|
| `--profile NAME` | `balanced` | the live profile | sets the power-profile face (`performance` / `balanced` / `power-saver`) |
| `--health PERCENT` | `94.3` | the live SoH | sets the `SoH` readout |

When `--simulate` runs, the live history database is opened **read-only** and
read once. It is never written, migrated, or created — a missing database is
reported, not created — and the independently running collector is unaffected.

## Storage & privacy

- History is one SQLite file at
  `${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3`
  (override with `--database`).
- Long-term history is kept permanently as compact **one-row-per-hour**
  aggregates. The **last 16 hours** is also kept at fine sub-hour detail —
  enough for the widest graph plus clock-alignment margin — in a crash-safe
  checkpoint. Only this genuine fine-grained data feeds historical graph cells.
- Every dashboard view is a **read-only** database reader. The simulator's
  live-history access is read-only too.
- The database holds only battery and power-supply telemetry. **Nothing is
  transmitted anywhere** — the application has no network code.

Details: [docs/storage.md](docs/storage.md).

## Limitations

- Linux only; needs sysfs power-supply data or UPower.
- `--sample` is the sole writer. Multiple ordinary viewers are safe, but two
  sampling processes against one database are unsupported.
- Without the timer or another regular `--sample`, history stops advancing and
  the viewer reports stale data rather than presenting it as current.
- The graph needs a Unicode + 24-bit-color terminal; without them it is
  unreadable (use `--diagnose` for plain text).
- Suspend/hibernate is classified as *sleep* only with positive evidence
  (clock discontinuity or journal record). Without it, a gap stays *unknown*
  by design.
- ETA needs a few minutes of consistent trend before it appears; brief spikes
  are rejected rather than smoothed.
- Time-derived power (`~X.XW`) needs at least two minutes of matching awake
  history.
- A pre-1.0 development database (schema v2) is not migrated automatically; see
  [docs/migration.md](docs/migration.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) — schema-v4 storage: event log,
  hourly aggregates, checkpoints, WAL, recovery, backups.
- [docs/history-model.md](docs/history-model.md) — observed/sleep/unknown
  semantics, sessions, suspend/hibernate reconstruction, battery identity,
  health events.
- [docs/graph.md](docs/graph.md) — exact geometry, colors, and rendering rules.
- [docs/data-sources.md](docs/data-sources.md) — sysfs/UPower field fusion and
  the power resolver.
- [docs/estimation.md](docs/estimation.md) — the Theil–Sen ETA and forecast
  display.
- [docs/storage.md](docs/storage.md) — database location, privacy, overhead.
- [docs/migration.md](docs/migration.md) — converting a pre-1.0 database.
- [CHANGELOG.md](CHANGELOG.md)

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
git diff --check
```

The test suite covers the data sources and power resolver, multi-battery
aggregation, schema-v4 storage and recovery, suspend/hibernate reconstruction,
session boundaries, ETA and forecast rendering, the graph's dynamic viewport,
the simulator, and the pre-1.0 database converter with its independent
validator.

## License

MIT — see [LICENSE](LICENSE).

# battery-status-tui

`battery-status-tui` is a compact, standalone battery monitor for Linux. It
records low-overhead local history, separates charging and discharging sessions,
reconstructs suspend and hibernate gaps, and renders twelve hours of context —
six hours of measured history, a fixed `NOW` column, and six hours of forecast —
in five lines of terminal output.

It uses only the Python standard library, needs no root, and runs no
system-wide daemon. History lives in a single SQLite file in your XDG state
directory and is never uploaded.

The project is deliberately independent from any dashboard or shell widget; a
dashboard can consume its database later without coupling to the collector.

## Features

- One glance: current SoC, charge/discharge direction, power draw, ETA,
  State-of-Health, and active power profile.
- A 12-hour graph: 6 h history · fixed `NOW` · 6 h forecast, on a stable
  20-minute grid.
- Honest history: measured time, proven sleep/hibernate time, and unknown gaps
  are visually distinct. Known data is always visible; blank means unknown.
- Field-level fusion of `/sys/class/power_supply` and UPower, with a layered
  power resolver that marks estimates as approximate.
- Robust ETA from a Theil–Sen session trend, not a single instantaneous rate.
- Crash-safe schema-v4 storage: append-only event log, immutable hourly
  aggregates, rotating verified checkpoints, and automatic recovery.
- Suspend/hibernate reconstruction from clocks, the logind signal, and the
  kernel journal — including hibernation across a cold boot.

## Requirements

- Linux with `/sys/class/power_supply` **and/or** UPower (`upower` CLI).
- Python 3.11 or newer. No third-party packages.
- A terminal with Unicode (block + Braille glyphs) and 24-bit color for the
  graph. Check yours with `battery-status-tui --unicode-probe`.
- Optional, each degrades gracefully if absent:
  - `busctl` (systemd) — live suspend/resume detection via logind;
  - `journalctl` — durable suspend/hibernate reconstruction;
  - `powerprofilesctl` / `busctl` / `/sys/firmware/acpi/platform_profile` —
    power-profile display.

## Installation

Run it straight from a checkout — no packaging step:

```bash
git clone <repo-url> battery-status-tui
cd battery-status-tui
./battery-status-tui --sample
./battery-status-tui --once
```

The `battery-status-tui` wrapper script adds `src/` to `sys.path`. If you prefer
a console entry point, `pip install .` also installs a `battery-status-tui`
command.

### Background sampling

The canonical collector is a user timer that runs one short-lived `--sample`
process per minute:

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -s "$PWD/battery-status-tui" ~/.local/bin/battery-status-tui
cp systemd/battery-status-tui.service systemd/battery-status-tui.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now battery-status-tui.timer
```

The symlink resolves back to the checkout so the wrapper can find `src/`. No
root needed. Inspect with `systemctl --user status battery-status-tui.timer` and
`journalctl --user -u battery-status-tui.service`.

To remove just this:

```bash
systemctl --user disable --now battery-status-tui.timer
rm ~/.config/systemd/user/battery-status-tui.{service,timer} ~/.local/bin/battery-status-tui
systemctl --user daemon-reload
```

Nothing is installed or enabled automatically. Continuous history requires the
timer (or an equivalent regular `--sample` invocation). It is the sole writer;
ordinary viewers never collect or modify the database.

## Quick start

```bash
battery-status-tui              # interactive read-only viewer
battery-status-tui --once       # read-only dashboard, then exit
battery-status-tui --sample     # collect and record one sample
battery-status-tui --diagnose   # detailed source and health readout
```

When stdout is not a terminal, the program renders once and exits, so
`battery-status-tui | cat` and cron-style capture work without a flag.

## CLI options

| Option | Effect |
|---|---|
| *(none)* | Read-only interactive dashboard; redraws committed SQLite state every `--interval` seconds. `Ctrl-C` to exit. |
| `--once` | Print one read-only dashboard from the latest checkpoint and exit. |
| `--sample` | Take one sample, print a single terse line (`<epoch> <soc>% <state> <power>`), exit. Used by the systemd timer. |
| `--interval SECONDS` | Interactive refresh interval (default `60`). |
| `--database PATH` | Use an alternate SQLite history file (default `${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3`). |
| `--diagnose` | Print resolved source, power method/confidence/window, per-battery raw candidates, capacity, health, cycles, session, identity, and database path. |
| `--unicode-probe` | Print the block/Braille/axis glyphs the renderer uses, to verify terminal font support. |
| `--version` | Print the version and exit. |

`--once`, `--sample`, `--diagnose`, and `--unicode-probe` are mutually
exclusive.

## Reading the graph

```
BATTERY            SoC 72%   ↑ 12.4 W (balanced)
0h48  ▁▂▃▄▅▆▇█…│⠈⠐⠠     3h10 ~19:40
start ▁▂▃▄▅▆▇█…│⠁⠂⠄     full
      ┬─────┬─────┬─────┬─────┬─────┬─────┬
      13    14    15    16    17    18    19     SoH 94.3%
```

- **12 hours, fixed layout.** 18 columns (6 h) of history, the `NOW` column
  (`│`), 18 columns (6 h) of forecast. Each column is 20 minutes, aligned to
  absolute clock boundaries, so the grid only shifts when a real 20-minute
  boundary passes. The title arrow sits above `NOW`.
- **Observed solid history** — block characters `▁`–`█`, gradient-colored by
  SoC. This is time the collector was awake and measuring.
- **Sleep / hibernate** — Braille cells in the history region, SoC interpolated
  between the readings before and after a *proven* suspend/hibernate span.
- **Forecast** — Braille cells right of `NOW`.
- **Unknown gaps** — blank. The collector was not running, or continuity broke
  with no sleep evidence.
- **Known low SoC vs unknown.** Every measured history bucket shows at least
  the smallest block `▁`, using its actual SoC color; exact 0% is deep red
  `#550A14`. Valid sleep and forecast trajectories at 0% keep a bottom Braille
  dot. Only genuinely-unknown cells are blank. The rule throughout: *known data
  stays visible; empty means no reliable data.*
- **Held-at-empty forecast.** A discharge forecast that reaches 0% stays at 0%
  in `#550A14` for the rest of the 6-hour window rather than being truncated.
- **Charging plateau.** A charge forecast that reaches 100% plateaus at a full
  column to the end of the window; a battery already full on AC shows a flat
  100% line.
- **Color gradient.** `#550A14` at 0% → `#9B231E` at 25% → `#AF6E19` at 50% →
  `#5A8228` at 75% → `#146932` at 100%, linearly interpolated.
- **`start` / `full` / `empty` / ETA.** Left of the rows: time since the current
  session began, labelled `start`. Right: the estimated remaining time and
  target clock time (`~HH:MM`), labelled `full` when charging or `empty` when
  discharging. `--` when there is no estimate.
- **SoH.** `SoH X.X%` on the label line when a State-of-Health value can be
  resolved from capacity vs. design capacity.
- **Power profile.** Shown in parentheses after the power reading when
  power-profiles-daemon or the kernel platform profile is available.
- **Power source.** `X.X W` is a direct reading; `~X.X W` is a time-derived
  estimate; `-- W` means no usable value. sysfs is preferred, UPower fills gaps,
  and a UPower-only snapshot is used if sysfs is unavailable.

Full details: [docs/graph.md](docs/graph.md).

## Screenshots

<!-- Real terminal captures are added in a follow-up step. Intended set:
     1. discharging with a forecast reaching empty
     2. charging with a plateau at 100%
     3. a history window containing a sleep/hibernate gap and an unknown gap
-->

_Screenshots pending._

## Limitations

- Linux only; needs sysfs power-supply data or UPower.
- `--sample` is the sole writer. Multiple ordinary viewers are safe, but two
  sampling processes against one database are unsupported.
- Without the timer or another regular `--sample`, history stops advancing and
  the viewer reports stale data rather than presenting it as current.
- The graph needs a Unicode + 24-bit-color terminal; without them it is
  unreadable (use `--diagnose` for plain text).
- Suspend/hibernate is classified as *sleep* only with positive evidence
  (clock discontinuity, journal record, or logind signal). Without it, a gap
  stays *unknown* by design.
- ETA needs a few minutes of consistent trend before it appears; brief spikes
  are rejected rather than smoothed.
- Time-derived power (`~X.X W`) needs at least two minutes of matching awake
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

The suite covers energy- and charge-based batteries, multi-battery aggregation,
source priority and invalid zeroes, counter resets and replacement batteries,
schema-v4 storage and recovery, suspend/hibernate reconstruction (including
cold-boot hibernation), session boundaries, forecast and 0%-visibility
rendering, the pre-1.0 converter and its independent validator, and the
schema-dispatch behavior of the CLI.

## License

MIT — see [LICENSE](LICENSE).

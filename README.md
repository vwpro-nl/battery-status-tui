# battery-status-tui

`battery-status-tui` is a compact, standalone battery monitor for Linux. It
records low-overhead local history, separates charging and discharging
sessions, and renders six hours of history plus six hours of forecast around a
fixed `NOW` column.

The project is intentionally independent from the Omarchy dashboard. Dashboard
integration can be added later without coupling the data collector to a shell
widget.

## Design goals

- Python standard library only.
- Field-level fusion of `/sys/class/power_supply` and UPower data.
- SQLite history in the user's XDG state directory.
- Robust session-based ETA rather than blindly trusting an instantaneous rate.
- Two terminal rows for the 0–100% graph: solid history and fine Braille forecast.
- No root privileges and no system-wide service.

The MIT license was selected because this is a small standalone command-line
tool whose code should remain easy to reuse in a later dashboard integration.
It permits reuse and modification while keeping the copyright and warranty
terms explicit.

## Quick start

Run directly from the checkout; no package installation is required:

```bash
./battery-status-tui --once
./battery-status-tui --diagnose
./battery-status-tui --unicode-probe
```

Without an option the program refreshes every 60 seconds in an interactive
terminal. When stdout is redirected, it renders once automatically.

```bash
./battery-status-tui
./battery-status-tui --interval 30
```

The graph always covers twelve hours. Its centre column is the current time,
with up to six hours of solid measured history on the left and at most six
hours of fine Braille forecast on the right. The forecast stops at its estimated
0% or 100% endpoint.

## Background sampling

The included user timer records one sample per minute without keeping a Python
process resident. Install it from this checkout as the current user:

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -s "$PWD/battery-status-tui" ~/.local/bin/battery-status-tui
cp systemd/battery-status-tui.service systemd/battery-status-tui.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now battery-status-tui.timer
```

The symlink deliberately resolves back to this checkout, so the wrapper can
find its `src` directory. No root privileges are needed. Inspect it with:

```bash
systemctl --user status battery-status-tui.timer
journalctl --user -u battery-status-tui.service
```

To remove only this optional installation:

```bash
systemctl --user disable --now battery-status-tui.timer
rm ~/.config/systemd/user/battery-status-tui.service ~/.config/systemd/user/battery-status-tui.timer
rm ~/.local/bin/battery-status-tui
systemctl --user daemon-reload
```

These commands are documentation only; the project does not install or enable
the timer automatically.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
git diff --check
```

The suite covers energy- and charge-based batteries, multi-battery aggregation,
source priority and invalid zeroes, counter resets and replacement batteries,
schema migration, suspend reconstruction, sleep gaps, session boundaries,
charging/discharging trends, forecast termination, and Unicode `NOW` alignment.

## Documentation

- [Data sources](docs/data-sources.md)
- [Storage and privacy](docs/storage.md)
- [ETA estimation](docs/estimation.md)

No Omarchy dashboard files are modified by this project.

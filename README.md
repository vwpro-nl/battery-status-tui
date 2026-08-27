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
- UPower D-Bus as the primary source, with `/sys/class/power_supply` fallback.
- SQLite history in the user's XDG state directory.
- Robust session-based ETA rather than blindly trusting an instantaneous rate.
- Two terminal rows for the 0–100% graph: solid history and fine Braille forecast.
- No root privileges and no system-wide service.

The MIT license was selected because this is a small standalone command-line
tool whose code should remain easy to reuse in a later dashboard integration.
It permits reuse and modification while keeping the copyright and warranty
terms explicit.

Installation, commands, storage details and systemd setup are documented as
the implementation is completed.


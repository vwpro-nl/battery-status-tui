# Local storage

History is stored by default at:

```text
${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3
```

Override it for testing with `--database PATH`.

The SQLite database contains:

- timestamped percentage, state, AC state, watts, voltage and current samples;
- the source and native battery name;
- UPower's contemporaneous remaining-time estimate when available;
- charging and discharging session boundaries;
- a small per-session smoothed ETA value.

A transaction closes the old session, opens the new one and inserts the sample
atomically. Switching from battery to AC or vice versa therefore cannot attach
one sample to both sessions. `Full`, `Empty` and battery-idle states close an
active session.

Raw measurements are retained for 30 days. Session summaries remain available
after raw pruning so later trend and lifetime reports can be added without a
schema redesign. At one sample per minute, collection has negligible CPU and
storage overhead. The supplied systemd timer starts a short-lived process each
minute rather than keeping a daemon resident.

The database contains no network, application or user-content data. Nothing is
uploaded.

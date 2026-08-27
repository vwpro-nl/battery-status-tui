# Local storage

History is stored by default at:

```text
${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3
```

Override it for testing with `--database PATH`.

The SQLite database contains:

- timestamped percentage, state, AC state, watts, voltage and current samples;
- the source and native battery name;
- raw energy, charge, voltage and direct-power candidates per physical battery;
- power method, confidence, observation window and approximate/direct status;
- realtime, monotonic and boottime clocks, boot ID and battery identity;
- reconstructed suspend/hibernate intervals and their adjacent percentages;
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

Suspend is detected live through logind's `PrepareForSleep` signal and
independently reconstructed from the difference between boottime and monotonic
time. Kernel suspend entry/exit messages from the journal provide a durable
fallback. Overlapping detections are merged. A battery replacement closes the
active session and starts a new one without joining incompatible counters.

The supplied timer intentionally remains unchanged in this release. Its
monotonic interval pauses during suspend, but the next sample reconstructs the
sleep interval from stored clocks. Whether a calendar timer offers a useful
additional immediate post-resume sample is evaluated separately.

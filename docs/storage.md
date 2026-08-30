# Local storage and privacy

History is stored by default at:

```text
${XDG_STATE_HOME:-~/.local/state}/battery-status-tui/history.sqlite3
```

Override it with `--database PATH`.

A fresh database is created at **schema v4**, the definitive v1.0 storage
format. Its structure, durability guarantees, and recovery behavior are
documented in [architecture.md](architecture.md). The history model it records —
observed vs. sleep vs. unknown time, sessions, suspend/hibernate, battery
identity, and health events — is documented in
[history-model.md](history-model.md).

If you ran a pre-1.0 development build, its database is schema v2. It is not
migrated automatically; see [migration.md](migration.md) for the offline
conversion path.

## Privacy

The database contains only battery and power-supply telemetry: timestamps,
percentages, states, AC state, watts/volts/amps, capacity and cycle counts,
reconstructed suspend/hibernate intervals, power-profile names, and
charge/discharge session boundaries. It contains no network, application, or
user-content data. Nothing is uploaded.

## Overhead

At one sample per minute, collection has negligible CPU and storage cost. Only
*changes* are stored as events; per-hour aggregates are one row each; the
rotating checkpoint keeps at most three generations plus up to 12 hours 20
minutes of compact sub-hour points (enough to back the widest history the graph
can show). The supplied systemd timer starts a short-lived process each minute
rather than keeping a daemon resident.

## One writer

`battery-status-tui --sample` is the sole collection/write path. The supplied
systemd timer invokes it once per minute; do not run a second sampling process
against the same database. Ordinary interactive use, `--once`, and piped output
are read-only, so any number of viewers may run concurrently. Without regular
sampling, committed data eventually becomes stale and the viewer says so. See
[architecture.md](architecture.md#one-writer-expectation).

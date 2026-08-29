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
rotating checkpoint keeps at most three generations plus up to eight hours of
compact sub-hour points. The supplied systemd timer starts a short-lived
process each minute rather than keeping a daemon resident.

## One writer

Run a single writer against a database — either an interactive
`battery-status-tui` session or the `--sample` timer, not both, and not two
resident sessions. Any number of read-only consumers may run concurrently. See
[architecture.md](architecture.md#one-writer-expectation).

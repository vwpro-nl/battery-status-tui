# Storage architecture (schema v4)

This document describes the definitive v1.0 on-disk format. The SQLite database
carries `PRAGMA user_version = 4`; the code refers to it as the "v1" schema
(`V1_SCHEMA_VERSION` in `schema.py`) because it is the schema the 1.0 release is
built on. Application version and schema version are independent numbers.

A fresh database is created directly at schema v4 by `V1Storage`
(`v1_storage.py`). There is no stepwise migration into it; a pre-1.0 schema-v2
database is converted by a separate offline tool (see
[migration.md](migration.md)).

## Design principle: event-based persistence

The collector never stores a dense per-minute sample stream as the source of
truth. Instead it records:

1. **State events** (`state_events`) — an append-only log of *changes*: AC
   online/offline, power-profile switches, a battery appearing or disappearing,
   a battery changing charge/discharge/full state, with the SoC at that moment.
   A poll that observes no change writes no event.
2. **Canonical hourly aggregates** (`hourly_history`) — one immutable row per
   UTC hour, holding time-weighted totals for that hour.
3. **A rotating checkpoint** (`checkpoint_*`) — the full in-progress state
   needed to resume accounting after a restart or crash, including the
   sub-hour history not yet folded into an hourly row.

Everything the dashboard shows is reconstructed from these three: finalized
hours give the older history, the checkpoint's `recent_series` gives the
current hour, and the checkpoint's per-battery snapshot gives "now".

## Tables

### `batteries`
One row per distinct battery identity (`base|model|serial`, see
[history-model.md](history-model.md#battery-identity)). Tracks `native_name`,
optional `manufacturer`/`model`/`serial_hash`, and `first_seen_ms` /
`last_seen_ms`.

### `state_events`
Append-only. `scope` is `'system'` (AC + power profile) or `'battery'`
(presence, state, SoC, tied to a `battery_id`). `reason_mask` is a bitmask of
why the event was recorded (presence change, state change, SoC-only,
AC change). `source_generation` records which checkpoint generation wrote the
row — audit metadata only, not a foreign key. CHECK constraints enforce that
system rows carry no battery columns and vice versa. Unique indexes prevent two
system rows, or two rows for one battery, at the same millisecond.

### `hourly_history`
Primary key `hour_start_ms`, always a UTC-hour boundary
(`hour_start_ms % 3600000 = 0`). Each row is finalized once, when a later poll
crosses its end boundary, and is then immutable except for bounded revisions
when late sleep evidence arrives (see below). The schema encodes the
aggregation invariants as CHECK constraints:

```
observed_ms + sleep_ms + unknown_ms            = 3600000
charging_ms + discharging_ms + full_ms + other_state_ms = observed_ms
ac_online_ms + ac_offline_ms + ac_unknown_ms   = observed_ms
direct_power_ms + estimated_power_ms + unknown_power_ms = observed_ms
```

SoC geometry (`soc_start/end/min/max`, `soc_integral_percent_ms`) is present if
and only if `observed_ms > 0`. `charge_power_max_w` is non-null if and only if
`charge_power_valid_ms > 0` (same for discharge). Other columns: threshold
occupancy (`under_20_ms`, `above_80_ms`, `above_95_ms`, `full_on_ac_ms`),
charged/discharged energy in Wh, `poll_count`, `state_event_count`,
`quality_flags` (unknown / sleep / energy-rejected / power-rejected bits),
`energy_provenance_mask`, and `revision` / `source_generation` /
`aggregation_version` for provenance.

### `hourly_profile_durations`
`(hour_start_ms, profile) -> duration_ms`, `WITHOUT ROWID`, cascade-deleted with
its hour. Records how long each power profile was active within a finalized
hour. `sum(duration_ms) <= observed_ms` for that hour.

### `battery_health`
Slow-changing capacity/wear facts, appended only when a value actually changes:
`charge_full_ah`, `charge_full_design_ah`, `energy_full_wh`,
`energy_full_design_wh`, `cycle_count`, `voltage_design_v`. `source`
(`sysfs-energy` / `sysfs-charge` / a legacy tag) and `provenance` record where
the numbers came from. `UNIQUE(battery_id, observed_at_ms)`; at least one value
must be non-null.

### `sessions`
One charging or discharging session: `kind`, `started_at_ms`, `ended_at_ms`
(null while open), `start_soc` / `end_soc`, `battery_set_key`, `end_reason`. A
partial unique index (`... WHERE ended_at_ms IS NULL`) guarantees **at most one
open session** at any time.

### `sleep_intervals`
Proven suspend / hibernate spans: `started_at_ms`, `ended_at_ms`, `kind`
(`suspend` / `hibernate` / `sleep`), `source` (`clocks` / `journal` /
`logind`), `boot_id`, `pre_soc` / `post_soc`, `revision`. `UNIQUE(started_at_ms,
ended_at_ms)`. When a better source revises the bounds of an existing interval,
the row is updated in place and `revision` is bumped. Source precedence for
bound replacement is `clocks < journal < logind`.

### `metadata`
Free-form `key -> value`. The offline converter sets
`converted_from_schema = '2'`.

### Checkpoint tables
- `checkpoint_generations` — one row per generation: monotonic/boottime clocks,
  `boot_id`, `last_poll_at_ms`, `configured_interval_ms`, AC + profile, the
  `recent_series` BLOB, `battery_count` / `hourly_count` (0 or 1) /
  `profile_count`, a 64-hex `payload_digest`, and `complete` (0/1).
- `checkpoint_batteries` — per-battery snapshot for the generation
  (`WITHOUT ROWID`): present flag, state, SoC, raw counters, and (for
  single-battery systems) the resolved power method / approximate flag /
  confidence / window.
- `checkpoint_hourly` — the in-progress `HourlyAccumulator` for the current UTC
  hour (`WITHOUT ROWID`).
- `checkpoint_hourly_profiles` — profile durations for that in-progress hour.

Two triggers enforce the write protocol: a generation must be inserted with
`complete = 0`, and the flip to `complete = 1` is rejected unless the child row
counts match the header's declared counts.

## `recent_series`: temporary, recoverable sub-hour state

`recent_series` is a compact binary blob (`recent_series.py`, magic `BRS1`)
stored inside each checkpoint generation. It holds up to the last **12 hours and
20 minutes** (`MAX_WINDOW_MS`, and at most 65 535) of poll points: timestamp,
SoC (millipercent), resolved power (mW), a compatible energy delta, battery
state, active profile, battery set, and a `flags` word (AC state, power method,
approximate bit, confidence, and a *break-before* bit marking a discontinuity).

It is "temporary" in the sense that once a UTC hour closes, that hour's points
are folded into an immutable `hourly_history` row and are no longer needed for
correctness — but the still-open hour lives only here until it is finalized.
Losing the newest checkpoint therefore costs at most the current partial hour,
and recovery falls back to an older generation that still covers it. The window
is sized to the widest history the dashboard can draw — 12 h of dynamic-`NOW`
viewport plus one 20-minute column of clock-alignment slack — so every visible
history column is backed by real sub-hour samples; anything older is served from
the permanent `hourly_history` aggregates.

Decoding is strict: any structural violation (non-increasing timestamps, window
past `MAX_WINDOW_MS`, out-of-range enum, dictionary not canonical, length
mismatch) raises `RecentSeriesError` and the generation is treated as invalid.

## Durability: WAL, transactions, digests

The writer opens the database with:

- `PRAGMA journal_mode = WAL` — startup fails hard if WAL cannot be enabled;
- `PRAGMA synchronous = FULL`;
- `PRAGMA foreign_keys = ON`;
- `PRAGMA busy_timeout = 5000`.

Every poll is a single `BEGIN IMMEDIATE` transaction that writes the events,
health rows, session change, sleep rows, finalized hours, and the new checkpoint
generation together, then commits. Readers use a separate `?mode=ro`,
`query_only = ON` connection and validate `user_version = 4` plus
`PRAGMA quick_check` before returning data.

Each checkpoint generation stores `payload_digest`, a deterministic SHA-256 over
a canonical JSON encoding of the whole snapshot (clocks, per-battery payloads,
hourly accumulator, profile durations, and the SHA-256 of the `recent_series`
bytes). After writing a generation the writer reloads it and re-checks the
digest before committing; recovery recomputes and compares it before trusting a
generation.

## Recovery and idempotence

On every poll the collector calls `recover()`, which walks complete generations
newest-first, loads each, and returns the first whose digest and structural
invariants hold (`RecoverySelection`). If none are valid it reports a cold
start.

- **Crash before commit** — the `BEGIN IMMEDIATE` transaction is rolled back
  whole; nothing changed.
- **Crash after commit** — the new generation is already durable and is picked
  up on the next start.
- **Committed transaction, later-corrupt checkpoint** — the finalized hours from
  that transaction are already in `hourly_history`. Finalization uses
  `INSERT OR IGNORE` and the committed immutable hour wins; recovery from an
  older generation will not append or rewrite it.
- **Late sleep evidence** — re-partitioning a finalized hour between `sleep_ms`
  and `unknown_ms` is bounded (it only moves time that is actually there) and
  idempotent (a no-op when the stored value already matches).
- **Same-second restart** — the CLI seeds its poll-spacing guard from the
  recovered checkpoint's `last_poll_at_ms` so a fresh process cannot feed the
  collector a non-increasing timestamp; the collector also rejects
  `current <= previous` outright.

After each successful poll, `cleanup_generations()` (its own transaction) keeps
the newest **3** valid generations and deletes the rest.

## One-writer expectation

WAL plus `BEGIN IMMEDIATE` serializes transactions, but the v1 deployment model
requires **exactly one writer**: the per-minute systemd timer invoking
`battery-status-tui --sample`. Ordinary interactive use, `--once`, and piped
rendering open only read-only SQLite connections. Any number of viewers can run
concurrently; running a second sampling process against the same database is
not supported. If sampling stops, the viewer reports the checkpoint as stale
after `max(3 × configured interval, 180 seconds)`.

## Backup implications

Because the database is in WAL mode, a plain `cp` of only the `.sqlite3` file
can capture a torn state. Copy it while no write transaction is in flight and
include the `-wal` / `-shm` sidecars, or use SQLite's backup API / `VACUUM
INTO`. The bundled `backup.py` helper takes a validated copy under
`BEGIN EXCLUSIVE` via the backup API and re-checks `quick_check` and row counts
on the result.

Even an imperfect copy degrades gracefully: recovery selects the newest intact
checkpoint generation, and finalized hours are immutable, so the worst case is
losing the current partial hour.

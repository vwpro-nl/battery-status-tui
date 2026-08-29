# Migrating a pre-1.0 database

**You only need this if you ran a pre-1.0 development build of
battery-status-tui.** Those builds created a schema-v2
`history.sqlite3`. A fresh 1.0 install creates a schema-v4 database directly and
needs no migration.

The read-only viewer requires schema v4 and refuses a schema-v0/v1/v2 database
with conversion guidance. It never migrates one automatically. Conversion is
an explicit, offline, one-way step that produces a **new** file and never
mutates the original.

## Steps

### 1. Back up the original

```bash
python3 - <<'PY'
from pathlib import Path
from battery_status_tui.backup import create_validated_v2_backup
print(create_validated_v2_backup(Path.home()
      / ".local/state/battery-status-tui/history.sqlite3"))
PY
```

`create_validated_v2_backup` takes the exclusive writer lock, copies the
database with SQLite's backup API, and re-validates `quick_check` and every
table's row count on the copy before publishing it under a UTC-timestamped
name by atomic hard link.

### 2. Convert

```bash
python3 tools/convert_v2_to_v1.py \
    ~/.local/state/battery-status-tui/history.sqlite3 \
    ~/.local/state/battery-status-tui/history.v4.sqlite3
```

The converter reads the v2 database read-only and builds a new schema-v4
database: batteries, the `state_events` log, `battery_health` events, sessions,
`sleep_intervals`, every finalized `hourly_history` hour, and a seed checkpoint
(generation 1) carrying up to 8 hours / 480 points of `recent_series`. It sets
`metadata.converted_from_schema = '2'` and checks `quick_check`,
`foreign_key_check`, and the complete-hour invariant before committing.

### 3. Validate independently

```bash
python3 tools/validate_pre_v1_conversion.py \
    ~/.local/state/battery-status-tui/history.sqlite3 \
    ~/.local/state/battery-status-tui/history.v4.sqlite3
```

Add `--json` for machine-readable output. This validator re-derives every fact
directly from the v2 tables **without importing the converter**, and compares:
batteries, state events, sessions, sleep intervals, health events, every
`hourly_history` field, the seed-checkpoint payload digest, and the
recent-series tail. Exit status `0` and `PASS` mean the conversion is faithful.

### 4. Swap it in

Stop the sole writer (the sampling timer), then atomically replace the live
database. Ordinary viewers are read-only, but close them during the swap so
they do not retain an open handle to the replaced file:

```bash
systemctl --user stop battery-status-tui.timer   # if you installed it
mv ~/.local/state/battery-status-tui/history.v4.sqlite3 \
   ~/.local/state/battery-status-tui/history.sqlite3
systemctl --user start battery-status-tui.timer
```

Keep the backup from step 1 until you are satisfied.

## Status of this tooling

The converter (`pre_v1_converter.py`), the independent validator
(`pre_v1_validator.py`), the backup helper (`backup.py`), and their `tools/`
wrappers are retained for the 1.0.x line for exactly this purpose. They are not
part of the normal runtime and are not invoked by `battery-status-tui`.

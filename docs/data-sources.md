# Battery data sources

The collector inventories system batteries under `/sys/class/power_supply` and
merges matching UPower properties per field. UPower is not allowed to hide a
usable kernel counter merely because another UPower property exists. If sysfs is
inaccessible, a UPower-only snapshot (`upower --dump`) is used instead.

Peripheral batteries are ignored: sysfs entries whose scope is `Device`, and
UPower devices whose `power supply` property is not `yes`. This keeps devices
such as wireless mice from becoming the displayed battery.

## Field mapping

| Measurement | UPower | sysfs fallback |
| --- | --- | --- |
| percentage | `percentage` | `capacity` |
| state | `state` | `status` |
| AC state | line-power `online` | mains/USB `online` |
| watts | validated `energy-rate` | `power_now`, current × voltage, then counter delta |
| voltage | `voltage` | `voltage_now` |
| current | not exposed by the CLI | `current_now` |
| remaining time | `time to empty` / `time to full` | `time_to_empty_now` / `time_to_full_now` |
| energy | `energy` | `energy_now`, otherwise charge × voltage |
| full / design energy | `energy-full(-design)` | energy or charge attributes |
| design voltage | `voltage-min-design` | `voltage_min_design` |
| cycles | `charge-cycles` | `cycle_count` |

UPower quantities are already in human units. Sysfs microvolts, microamps,
microwatts and microamp-hours are converted to SI units. Every stored
observation gets an unambiguous Unix timestamp from the collector's wall clock,
alongside `CLOCK_MONOTONIC`, `CLOCK_BOOTTIME`, and the kernel `boot_id`.

Multiple system batteries are aggregated: SoC is capacity-weighted, energy and
capacity are summed, and the state is charging if any pack is charging, else
discharging if any pack is discharging.

## Power resolution

Power is resolved independently for every system battery, in this order
(`power.py`):

1. usable `power_now` — **direct**, high confidence;
2. usable `current_now × voltage_now` — **direct**, high confidence;
3. non-zero, state-consistent UPower `energy-rate` — **direct**, medium
   confidence;
4. change in `energy_now` over 2–10 minutes of awake time — **estimated**,
   medium confidence;
5. change in `charge_now` × mean voltage over the same awake interval —
   **estimated**, medium confidence.

Missing files, empty reads and driver errors are treated as *unavailable*, not
zero. A zero reading during active charging or discharging is rejected as
suspect. Counter changes in the wrong direction, across a suspend, across a
reboot, or across a battery-identity change are rejected. A time-derived
estimate needs at least 120 seconds of matching awake history and never spans a
recorded sleep interval; the resolver may widen its window to 10 minutes for
coarse counters and takes the median of valid deltas.

### How power is shown

| Rendering | Meaning |
| --- | --- |
| `7.2 W` | direct reading (methods 1–3) |
| `~7.2 W` | time-derived estimate (methods 4–5), the `approximate` flag is set |
| `-- W` | no usable value |

The hourly aggregates keep `direct_power_ms`, `estimated_power_ms` and
`unknown_power_ms` so the split is preserved in history.

## `--diagnose`

`battery-status-tui --diagnose` prints the resolved method, approximate flag,
confidence, observation window and raw candidates per physical battery,
alongside full and design capacity, resolved health, cycle count, the active
session, the battery identity, and the database path. It may inspect live
hardware, but it does not collect a sample or create or modify the history
database.

## See also

- [history-model.md](history-model.md) — battery identity, health/SoH storage,
  suspend/hibernate reconstruction.
- [estimation.md](estimation.md) — how the remaining-time ETA is derived.

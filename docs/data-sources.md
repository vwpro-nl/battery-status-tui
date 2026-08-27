# Battery data sources

The collector inventories system batteries under `/sys/class/power_supply` and
merges matching UPower properties per field. UPower is therefore not allowed to
hide a usable kernel counter merely because another UPower property exists. If
sysfs is inaccessible, a UPower-only snapshot remains available.

Peripheral batteries whose sysfs scope is `Device`, or whose UPower `power
supply` property is false, are ignored. This prevents devices such as wireless
mice from becoming the displayed battery.

## Field mapping

| Measurement | UPower | sysfs fallback |
| --- | --- | --- |
| percentage | `percentage` | `capacity` |
| state | `state` | `status` |
| AC state | line-power `online` | mains/USB `online` |
| watts | validated `energy-rate` | `power_now`, current × voltage, then counter delta |
| voltage | `voltage` | `voltage_now` |
| current | not exposed by CLI | `current_now` |
| remaining time | `time to empty/full` | `time_to_empty/full_now` |
| energy | `energy` | `energy_now`, otherwise charge × voltage |
| full/design energy | `energy-full(-design)` | energy or charge attributes |
| cycles | `charge-cycles` | `cycle_count` |

UPower quantities are already expressed in human units. Sysfs microvolts,
microamps, microwatts and microamp-hours are converted to SI units. A sysfs
sample receives the collector wall-clock timestamp; UPower's human-readable
update time remains available through `--diagnose` indirectly as fresh values,
while each stored observation always gets an unambiguous Unix timestamp.

Power is resolved independently for every system battery in this order:

1. usable `power_now`;
2. usable `current_now × voltage_now`;
3. non-zero, state-consistent UPower `EnergyRate`;
4. change in `energy_now` over 2–10 minutes of awake time;
5. change in `charge_now` times mean voltage over the same awake interval.

Missing files, empty reads and driver errors are unavailable rather than zero.
Zero during active charging or discharging is rejected as suspect. Counter
changes in the wrong direction, changes across suspend, another boot or another
battery identity are also rejected. Direct readings render as `7.2 W`, temporal
counter estimates as `~7.2 W`, and unavailable power as `-- W`.

The `--diagnose` command displays the selected method, approximation flag,
confidence, window and raw candidates per physical battery, alongside full and
design capacity, health and cycle count.

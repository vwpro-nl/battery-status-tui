# Battery data sources

The collector invokes `upower --dump` once per sample. The UPower client reads
the system service over D-Bus and supplies normalized battery energy, rate,
state, percentage and time estimates. If UPower is absent, cannot reach D-Bus,
or has no system battery, the collector reads Linux power-supply attributes
under `/sys/class/power_supply`.

Peripheral batteries whose sysfs scope is `Device`, or whose UPower `power
supply` property is false, are ignored. This prevents devices such as wireless
mice from becoming the displayed battery.

## Field mapping

| Measurement | UPower | sysfs fallback |
| --- | --- | --- |
| percentage | `percentage` | `capacity` |
| state | `state` | `status` |
| AC state | line-power `online` | mains/USB `online` |
| watts | `energy-rate` | `power_now`, otherwise voltage × current |
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

The `--diagnose` command displays which source won and includes actual full
capacity, design capacity, computed health percentage and cycle count.

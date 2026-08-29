# History model: observed, sleep, and unknown

battery-status-tui partitions every span of wall-clock time into exactly one of
three kinds. This partition is the core of the history model and is enforced by
the `hourly_history` invariant `observed_ms + sleep_ms + unknown_ms = 3600000`.

## The three kinds

**Observed.** The span between two consecutive polls that passed the continuity
check. SoC is interpolated linearly across it, energy is integrated from
compatible battery counters, and time is attributed to charge/discharge/full
state, AC state, and power method.

**Sleep.** A span that overlaps a *proven* suspend or hibernate interval
(`sleep_intervals`). It is counted as `sleep_ms`, never as observed time. No
SoC or energy integration crosses a sleep span — the machine was not awake to
measure anything.

**Unknown.** A span where the collector was not running, or was running but the
continuity check failed, and there is no sleep evidence to explain it. It is
counted as `unknown_ms`.

### Unknown stays unknown without evidence

The collector never guesses. A gap becomes `sleep` only if there is positive
proof — a clock discontinuity, a journal suspend/hibernate record, or a live
logind signal. A gap with no such evidence stays `unknown` forever, even if it
"looks like" the laptop was asleep. The renderer shows unknown spans as empty
(see [graph.md](graph.md)), distinct from an observed 0% battery.

## Continuity breaks

Two consecutive polls are treated as continuous only if all of these hold
(`v1_collector._continuity`):

| Reason a boundary is **not** continuous | Meaning |
|---|---|
| `non-increasing-wallclock` | the new poll's timestamp is not after the previous one |
| `reboot` | `boot_id` changed |
| `battery-set-change` | the set of present batteries changed |
| `session-direction-change` | charging⇄discharging⇄idle flipped between the polls |
| `wallclock-jump` | wall elapsed disagrees with the boottime delta by more than 5 s |
| `unknown-gap` | awake time between polls exceeds `max(3 × poll interval, 180 s)` |
| `unproven-suspend-gap` | awake time and the monotonic-clock delta disagree by more than 5 s |

When a boundary is not continuous:

- the intervening time is classified `unknown` (unless a sleep interval covers
  it), not observed;
- no energy delta is integrated across it;
- the ETA trend segment is cut — the `recent_series` point gets a *break-before*
  flag, and the estimator starts a fresh segment after it, so a projection never
  extrapolates across a discontinuity.

## Session boundaries

A session is a maximal run of one direction. `Measurement.session_kind`:

- `discharging` if AC is offline;
- `charging` if AC is online and (the battery reports charging **or** SoC < 100);
- otherwise the battery's own state if it is charging/discharging;
- otherwise none (idle / full).

The collector closes the open session and opens a new one when the direction
changes or when the battery set changes. A battery-set change closes the session
with `end_reason = "battery-change"` and does **not** join counters across the
two batteries.

## Suspend and hibernate reconstruction

Three independent detectors feed `sleep_intervals`:

1. **logind (live).** A background listener on the logind `PrepareForSleep`
   D-Bus signal (`busctl monitor`). Marks the entry and, on resume, closes the
   interval. `source = "logind"`.
2. **Clocks (proven after the fact).** On the first poll after resume the
   collector compares the boottime delta with the monotonic delta since the
   last checkpoint. `CLOCK_BOOTTIME` advances during suspend; `CLOCK_MONOTONIC`
   does not. A difference over 5 s is a proven sleep span of that length,
   ending at the current poll. `source = "clocks"`.
3. **Journal.** `journalctl -b all -k` is parsed for `PM: suspend entry` /
   `PM: suspend exit` and `PM: hibernation: hibernation entry` /
   `PM: hibernation: hibernation exit`. `source = "journal"`,
   `kind = "suspend" | "hibernate"`.

Overlapping detections from different sources are merged into one interval;
bound precedence is `clocks < journal < logind`. `pre_soc` / `post_soc` are
filled from the polls (or events) bracketing the interval.

### Hibernation across a cold boot

Hibernate followed by a full power-off is the hard case: when the machine comes
back, `boot_id` has changed, so the clock comparison alone cannot prove a sleep
span (the monotonic clock reset). The collector detects the `boot_id` mismatch
between the recovered checkpoint and the current poll, then looks back through
the kernel journal (comparing boot IDs with dashes normalized) for a
`hibernation entry` in the old boot and a `hibernation exit`, and attaches a
`hibernate` interval spanning the checkpoint's last poll to the current poll.
The gap is thus classified as sleep, not unknown, even though the process was
completely gone. (Regression: `test_v1_runtime.py::…hibernation_across_restart…`.)

## Battery identity

A battery's identity is the string `base|model|serial`:

- `base` — the sysfs directory name or the UPower native path;
- `model` — `model_name` (sysfs) or `model` (UPower), or empty;
- `serial` — `serial_number` (sysfs) or `serial` (UPower), or empty.

`model` and `serial` are **sticky**: once a non-empty value has been seen for a
`base`, it is retained even if a later read returns empty (some drivers do this
intermittently). The identity only changes when a new non-empty value actually
*conflicts* with the remembered one — a real battery swap. A new identity
produces a new `battery_set_key`, which closes the current session and starts a
fresh one without merging incompatible counters.

Peripheral batteries are excluded: sysfs entries with `scope = Device`, and
UPower devices whose `power supply` property is not `yes` (wireless mice,
headsets, and the like).

## Health / SoH event storage

Capacity and wear facts change slowly, so they are stored as change events in
`battery_health` rather than on every sample. A row is appended only when one of
`charge_full_ah`, `charge_full_design_ah`, `energy_full_wh`,
`energy_full_design_wh`, `cycle_count`, or `voltage_design_v` differs from the
last row for that battery. `source` records the family (`sysfs-energy`,
`sysfs-charge`, or a legacy tag) and `provenance` the underlying field sources.

The State-of-Health percentage shown on the dashboard is `full / design × 100`,
resolved (`system_status.resolve_health`) in this order:

1. sysfs energy-full vs energy-full-design (summed across packs);
2. sysfs charge-full vs charge-full-design (single pack);
3. per-pack charge × design voltage, summed (multi-pack);
4. UPower energy-full vs energy-full-design;
5. UPower `capacity` percentage.

If only imported pre-1.0 health events exist, they are used with their legacy
provenance rather than being presented as live sysfs readings.

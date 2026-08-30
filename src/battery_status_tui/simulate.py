"""Dashboard simulation for visual and manual regression testing.

A reusable development facility, not a one-off script. It drives the real
production renderer (:func:`graph.render_dashboard`) and estimator
(:func:`estimate.estimate_remaining`) with ``Measurement`` / ``SleepInterval`` /
``Session`` objects — the very objects the live runtime builds in
:func:`v1_runtime.render_v1_view`. It renders the ``SIMULATION`` heading so the
output can never be mistaken for the real dashboard.

Two modes:

* **synthetic** ``sleep-drop`` — a deterministic scenario built from scratch in
  memory. No battery-history database is opened, read, written, or created.
* ``--simulate`` — a sequential timeline appended to the *genuine* live graph.
  The real history / current state / session / sleeps / battery identity /
  health / power profile are read once (**read-only**) via
  :func:`v1_runtime.read_v1_view` and kept verbatim; only the requested future
  blocks are added, in memory. The production database is opened ``mode=ro``
  with ``PRAGMA query_only=ON`` — writing is technically impossible — and a
  missing database is reported, never created.

Timeline grammar (one or more space-separated blocks, then an optional final
power-source token)::

    --simulate <duration>[:<type>][=<soc>] ...  [ac[=<watts>w] | dc[=<watts>w]]

    <duration>   2h  1h24m  45m  90s  2h05m   (positive; may be < one graph column)
    :<type>      omitted (normal active interval) | :sleep | :nodata
    =<soc>       20% / 100%     -> absolute SoC at the end of the block
                 -20% / +30%    -> relative percentage-point change (clamped 0..100)
                 omitted        -> SoC unchanged across the block
    ac | dc      final power-source context; omitted -> keep the genuine context
    =<watts>w    optional battery power at the final NOW (e.g. dc=8.3w, ac=24.2w),
                 fed to the production estimator; it never alters earlier blocks

Durations are sequential — each is measured from the previous checkpoint, not
from the original NOW. The end of the last block is the fictitious NOW; state
and forecast at that point come from the production estimator.

Neither mode starts a collector, timer, or systemd unit.

    PYTHONPATH=src python -m battery_status_tui.simulate sleep-drop
    PYTHONPATH=src python -m battery_status_tui.simulate sleep-drop \\
        --simulate 35m=-4% 3h12m:sleep=-28% 27m=+8% 1h18m:nodata=-12% 2h05m=100% ac
"""

from __future__ import annotations

import argparse
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .estimate import estimate_remaining
from .graph import MAX_SPAN_SECONDS, render_dashboard
from .models import Estimate, Measurement, Session, SleepInterval

SIMULATION_HEADING = "SIMULATION"

# A fixed, 20-minute-aligned epoch so repeated runs render byte-identically.
# 1_800_000_000 == 2027-01-15 08:00:00 UTC and is an exact multiple of 1200 s.
DEFAULT_NOW = 1_800_000_000

HOUR = 3600
STEP_SECONDS = 60  # one synthetic Measurement per minute, like the live timer
BLOCK_TYPES = ("normal", "sleep", "nodata")


class SimulationError(RuntimeError):
    """A scenario cannot be built (e.g. --simulate with no live database)."""


@dataclass(frozen=True, slots=True)
class RenderInputs:
    """Everything the production renderer needs — nothing simulator-specific."""

    current: Measurement
    history: tuple[Measurement, ...]
    session: Session | None
    estimate: Estimate | None
    now: int
    sleeps: tuple[SleepInterval, ...]
    health_percent: float | None
    power_profile: str | None
    heading: str = SIMULATION_HEADING
    unknown_intervals: tuple[SleepInterval, ...] = ()

    def render(self) -> str:
        return render_dashboard(
            self.current, self.history, self.session, self.estimate, self.now,
            self.sleeps, self.health_percent, self.power_profile, self.heading,
            unknown_intervals=self.unknown_intervals,
        )


# --- timeline grammar ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SocSpec:
    mode: str  # "absolute" | "relative"
    value: float

    def apply(self, previous: float) -> float:
        if self.mode == "absolute":
            return self.value
        return max(0.0, min(100.0, previous + self.value))


@dataclass(frozen=True, slots=True)
class SimBlock:
    duration: int          # seconds, > 0
    kind: str              # one of BLOCK_TYPES
    soc: SocSpec | None     # None -> SoC unchanged across the block


@dataclass(frozen=True, slots=True)
class PowerContext:
    """The optional final ``ac`` / ``dc`` [``=<watts>w``] token."""

    source: str | None = None      # "ac" | "dc" | None (preserve genuine)
    power_w: float | None = None   # explicit magnitude in W, strictly > 0


_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
_SOC_RE = re.compile(r"(?P<sign>[+-]?)(?P<num>\d+(?:\.\d+)?)%?")
_FINAL_CONTEXT_RE = re.compile(r"(?P<src>ac|dc)(?:=(?P<power>\d+(?:\.\d+)?)w)?")


def parse_duration(text: str) -> int:
    """``"3h24m"`` / ``"6h"`` / ``"45m"`` / ``"90s"`` / ``"2h05m"`` -> positive seconds."""
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None or not any(match.groups()):
        raise ValueError(f"invalid duration {text!r} (use e.g. 3h24m, 6h, 45m, 90s)")
    hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    total = hours * HOUR + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return total


def parse_soc_spec(text: str) -> SocSpec:
    """``"20%"`` -> absolute; ``"-20%"`` / ``"+30%"`` -> relative percentage points."""
    match = _SOC_RE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"invalid SoC {text!r} (use e.g. 20%, 100%, -20%, +30%)")
    value = float(match.group("num"))
    if match.group("sign"):
        return SocSpec("relative", value if match.group("sign") == "+" else -value)
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"absolute SoC must be 0..100: {text!r}")
    return SocSpec("absolute", value)


def parse_block(token: str) -> SimBlock:
    """Parse one ``<duration>[:sleep|:nodata][=<soc>]`` token."""
    rest = token
    soc: SocSpec | None = None
    if "=" in rest:
        rest, _, soc_text = rest.partition("=")
        soc = parse_soc_spec(soc_text)
    kind = "normal"
    if ":" in rest:
        rest, _, kind_text = rest.partition(":")
        if kind_text not in ("sleep", "nodata"):
            raise ValueError(f"unknown block type {kind_text!r} in {token!r}")
        kind = kind_text
    if not rest:
        raise ValueError(f"missing duration in {token!r}")
    return SimBlock(parse_duration(rest), kind, soc)


def _looks_like_final_context(token: str) -> bool:
    return token[:2] in ("ac", "dc") and (len(token) == 2 or token[2:3] == "=")


def parse_final_context(token: str) -> PowerContext:
    """``"ac"`` / ``"dc"`` / ``"ac=24.2w"`` / ``"dc=8.3w"`` -> :class:`PowerContext`."""
    match = _FINAL_CONTEXT_RE.fullmatch(token)
    if match is None:
        raise ValueError(
            f"invalid final power-source token {token!r} "
            "(use ac, dc, ac=24.2w or dc=8.3w)"
        )
    power = None
    if match.group("power") is not None:
        power = float(match.group("power"))
        if not math.isfinite(power) or power <= 0.0:
            raise ValueError(f"battery power must be a finite value above 0 W: {token!r}")
    return PowerContext(match.group("src"), power)


def parse_timeline(tokens: Sequence[str]) -> tuple[tuple[SimBlock, ...], PowerContext]:
    """Parse ``BLOCK...  [ac|dc[=<watts>w]]``; validate the total graph window."""
    items = list(tokens)
    if not items:
        raise ValueError("no simulation blocks given")
    context = PowerContext()
    if _looks_like_final_context(items[-1]):
        context = parse_final_context(items.pop())
    for token in items:
        if _looks_like_final_context(token):
            raise ValueError("an ac/dc power-source token may only be the final token")
    if not items:
        raise ValueError("no simulation blocks before the ac/dc token")
    blocks = tuple(parse_block(token) for token in items)
    total = sum(block.duration for block in blocks)
    if total > MAX_SPAN_SECONDS:
        raise ValueError(
            f"simulated timeline is {_format_seconds(total)}, longer than the "
            f"graph's {_format_seconds(MAX_SPAN_SECONDS)} history window"
        )
    return blocks, context


def _format_seconds(seconds: int) -> str:
    hours, rest = divmod(seconds, HOUR)
    minutes, secs = divmod(rest, 60)
    return "".join(part for part in (f"{hours}h" if hours else "",
                                     f"{minutes}m" if minutes else "",
                                     f"{secs}s" if secs else "") ) or "0s"


def _ramp(start_ts: int, end_ts: int, start_soc: float, end_soc: float,
          state: str, ac_online: bool | None, *, power_w: float | None,
          step: int = STEP_SECONDS) -> list[Measurement]:
    """A run of measurements on a straight SoC line, inclusive of both ends.

    The last sample lands exactly on ``end_ts`` / ``end_soc`` so scenario
    boundary SoC values are represented precisely.
    """
    if end_ts < start_ts:
        return []
    span = end_ts - start_ts
    stamps = list(range(start_ts, end_ts, step)) + [end_ts]
    samples = []
    for stamp in stamps:
        fraction = 0.0 if span == 0 else (stamp - start_ts) / span
        soc = round(start_soc + (end_soc - start_soc) * fraction, 3)
        samples.append(Measurement(
            stamp, soc, state, ac_online, power_w=power_w,
            source="simulate", device="sim-battery",
            power_method="power-now" if power_w else "unavailable",
            power_confidence="high" if power_w else "none",
            battery_identity="sim|battery|0001",
        ))
    return samples


def build_sleep_drop(
    *,
    now: int = DEFAULT_NOW,
    start_soc: float = 97.0,
    resume_soc: float = 40.0,
    sleep_hours: float = 6.0,
    pre_hours: float = 3.0,
    post_hours: float = 1.0,
    after: str = "charging",
    charge_rate: float = 20.0,
    discharge_rate: float = 8.0,
    profile: str | None = "balanced",
    health_percent: float | None = 94.3,
) -> RenderInputs:
    """Measured history → a positively identified sleep SoC drop → measured resume.

    The sleep interval is handed to the renderer via the normal ``sleeps``
    argument, so it goes through the locked sleep-Braille reconstruction path
    exactly as a journal/clock-detected interval from the database would.
    """
    resume_at = now - round(post_hours * HOUR)
    sleep_start = resume_at - round(sleep_hours * HOUR)
    pre_start = sleep_start - round(pre_hours * HOUR)

    # Measured solid history before sleep: a gentle discharge that ends exactly
    # on ``start_soc`` (the last reliable pre-sleep reading).
    pre_from = min(100.0, start_soc + 1.5 * pre_hours)
    pre = _ramp(pre_start, sleep_start - STEP_SECONDS, pre_from, start_soc,
                "discharging", False, power_w=8.0)

    sleep = SleepInterval(sleep_start, resume_at, "hibernate", "journal",
                          "sim-boot", pre_percentage=start_soc,
                          post_percentage=resume_soc)

    # Measured solid history after resume, starting exactly on ``resume_soc``.
    if after == "charging":
        end_soc = min(100.0, resume_soc + charge_rate * post_hours)
        post = _ramp(resume_at, now, resume_soc, end_soc, "charging", True,
                     power_w=30.0)
        session_kind: str | None = "charging"
    elif after == "discharging":
        end_soc = max(0.0, resume_soc - discharge_rate * post_hours)
        post = _ramp(resume_at, now, resume_soc, end_soc, "discharging", False,
                     power_w=8.0)
        session_kind = "discharging"
    else:
        raise ValueError(f"unknown post-resume state: {after!r}")

    history = tuple(pre + post)
    current = post[-1]
    session = Session(1, session_kind, resume_at, None, resume_soc, None)
    estimate = estimate_remaining(current, post, current.timestamp)
    return RenderInputs(current, history, session, estimate, now, (sleep,),
                        health_percent, profile)


def _read_live_view(database_path, live_now):
    """Load the live graph snapshot with a strictly read-only connection.

    Delegates to the production read path (:func:`v1_runtime.read_v1_view` ->
    :meth:`V1Storage.reader`), which opens ``file:<path>?mode=ro`` and runs
    ``PRAGMA query_only=ON``. A missing database is reported, never created, and
    no writer / initialization / migration path is invoked.
    """
    from pathlib import Path

    from .storage import default_database_path
    from .v1_runtime import read_v1_view
    from .v1_storage import V1Storage

    path = Path(default_database_path() if database_path is None else database_path)
    if not path.is_file():
        raise SimulationError(
            f"--simulate needs an existing battery-history database at {path}; "
            "it is opened read-only and is never created"
        )
    return read_v1_view(V1Storage(path), now=live_now)


def _timeline_ramp(start_ts: int, end_ts: int, start_soc: float, end_soc: float,
                   direction: str | None) -> list[Measurement]:
    """Synthetic samples on a straight SoC line for one *normal* block.

    Only ``timestamp``/``percentage`` drive the graph trajectory; the state and
    AC flag are derived from the requested direction. Dense enough that even a
    short block occupies its column and a final block can feed the estimator.
    """
    span = max(1, end_ts - start_ts)
    step = STEP_SECONDS if span >= 8 * 60 else max(1, span // 10)
    ac_online = True if direction == "charging" else False if direction == "discharging" else None
    state = direction or "not charging"
    stamps = list(range(start_ts, end_ts, step)) + [end_ts]
    samples = []
    for stamp in stamps:
        soc = round(start_soc + (end_soc - start_soc) * (stamp - start_ts) / span, 3)
        samples.append(Measurement(
            stamp, soc, state, ac_online, source="simulate-timeline",
            device="sim-battery", battery_identity="sim|timeline|0001",
        ))
    return samples


def _final_state(soc: float, ac_online: bool | None, direction: str | None) -> str:
    if ac_online is False:
        return "discharging"
    if ac_online is True:
        return "full" if soc >= 100.0 else "charging"
    return direction or "not charging"


def build_from_live_timeline(
    *,
    blocks: Sequence[SimBlock],
    context: PowerContext | None = None,
    database_path=None,
    live_now: int | None = None,
    profile: str | None = None,
    health_percent: float | None = None,
) -> RenderInputs:
    """Append a sequential timeline to the genuine live graph.

    The genuine ``V1HistorySnapshot`` (history and its real Measurement values /
    irregular SoC / colour, existing sleeps, session, battery identity, health,
    power profile) is taken verbatim. Each block advances a ``(timestamp, SoC)``
    checkpoint by its own duration:

    * **normal** — synthetic samples interpolate the SoC to the block's end;
    * **sleep** — a :class:`SleepInterval` with known pre/post SoC (locked
      colour-gradient Braille reconstruction; no active-rate interpolation);
    * **nodata** — no measured samples, but the known start/end SoC checkpoints
      are handed to the renderer as an ``unknown_intervals`` entry: a
      straight-line Braille connection drawn in neutral gray, semantically still
      unknown and visually distinct from sleep.

    The end of the last block is the fictitious NOW. The final power-source
    context comes from ``context`` (``ac``/``dc``) or, if unset, the genuine
    live AC state. Battery power *magnitude* at that NOW is the explicit
    ``=<watts>w`` when given, otherwise the genuine live magnitude (a convenient
    default even when ac/dc is reversed); a genuine ``None`` stays ``None``. The
    last active block plus that measurement feed the **production** estimator —
    the simulator never computes its own ETA. The database is read once,
    read-only.
    """
    view = _read_live_view(database_path, live_now)

    anchor_ts = view.current.timestamp
    anchor_soc = view.current.percentage
    boot = view.current.boot_id or "sim-boot"

    history: list[Measurement] = list(view.history)
    sleeps: list[SleepInterval] = list(view.sleeps)
    unknown: list[SleepInterval] = []

    cursor_ts, cursor_soc = anchor_ts, anchor_soc
    run_start_ts, run_start_soc, run_kind = anchor_ts, anchor_soc, None
    final_trend: list[Measurement] = []

    for block in blocks:
        end_ts = cursor_ts + block.duration
        end_soc = cursor_soc if block.soc is None else block.soc.apply(cursor_soc)

        if block.kind == "sleep":
            sleeps.append(SleepInterval(
                cursor_ts, end_ts, "hibernate", "journal", boot,
                pre_percentage=cursor_soc, post_percentage=end_soc,
            ))
            run_start_ts, run_start_soc, run_kind, final_trend = end_ts, end_soc, None, []
        elif block.kind == "nodata":
            # unknown data: no measured/reconstructed samples, but the endpoint
            # SoC checkpoints are known -> a neutral-gray estimated connection.
            unknown.append(SleepInterval(
                cursor_ts, end_ts, "nodata", "simulate", boot,
                pre_percentage=cursor_soc, post_percentage=end_soc,
            ))
            run_start_ts, run_start_soc, run_kind, final_trend = end_ts, end_soc, None, []
        else:
            direction = ("charging" if end_soc > cursor_soc
                         else "discharging" if end_soc < cursor_soc else None)
            samples = _timeline_ramp(cursor_ts, end_ts, cursor_soc, end_soc, direction)
            history.extend(samples)
            if direction != run_kind:
                run_start_ts, run_start_soc, run_kind = cursor_ts, cursor_soc, direction
            final_trend = samples if direction else []

        cursor_ts, cursor_soc = end_ts, end_soc

    context = context or PowerContext()
    future_now, final_soc = cursor_ts, cursor_soc
    final_ac = (True if context.source == "ac"
                else False if context.source == "dc" else view.current.ac_online)

    # Battery power *magnitude* at the fictitious NOW — simulated INPUT for the
    # production estimator, never a simulator-computed ETA, and it never touches
    # the preceding timeline. Explicit "=<W>w" wins; otherwise the genuine live
    # magnitude is a convenient default even when ac/dc is reversed. A genuine
    # None stays None (a bare ac/dc must not invent a rate).
    final_power_w = context.power_w if context.power_w is not None else view.current.power_w

    current = replace(
        view.current,
        timestamp=future_now, percentage=final_soc,
        state=_final_state(final_soc, final_ac, run_kind), ac_online=final_ac,
        power_w=final_power_w,
        power_approximate=(view.current.power_approximate
                           if context.power_w is None else False),
        time_to_empty_s=None, time_to_full_s=None,  # stale absolutes; let production derive
        energy_wh=(None if view.current.energy_full_wh is None
                   else round(view.current.energy_full_wh * final_soc / 100, 3)),
    )

    kind = current.session_kind
    session = (Session(1, kind, run_start_ts, None, run_start_soc, None)
               if kind in {"charging", "discharging"} else None)
    estimate = estimate_remaining(current, final_trend, future_now)

    return RenderInputs(
        current=current,
        history=tuple(history),
        session=session,
        estimate=estimate,
        now=future_now,
        sleeps=tuple(sleeps),
        unknown_intervals=tuple(unknown),
        health_percent=(health_percent if health_percent is not None
                        else (view.health.percent if view.health else None)),
        power_profile=(profile if profile is not None else view.power_profile),
    )


SCENARIOS: dict[str, Callable[..., RenderInputs]] = {
    "sleep-drop": build_sleep_drop,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m battery_status_tui.simulate",
        description="Render the dashboard from a scenario for visual regression "
                    "testing. The synthetic sleep-drop opens no database; "
                    "--simulate reads the production database once, strictly "
                    "read-only, and never writes or creates it.",
    )
    scenarios = parser.add_subparsers(dest="scenario", required=True)

    drop = scenarios.add_parser(
        "sleep-drop",
        help="measured history -> proven sleep SoC drop -> measured resume",
    )
    drop.add_argument(
        "--simulate", nargs="+", metavar="BLOCK",
        help="append a sequential timeline to the genuine live graph "
             "(production database read-only). Each BLOCK is "
             "<duration>[:sleep|:nodata][=<soc>]; an optional final "
             "ac[=<watts>w] | dc[=<watts>w] sets the power-source context and, "
             "when given, the battery power at the final NOW. Unsigned SoC is "
             "absolute, +/- is a relative points change, omitted leaves SoC "
             "unchanged. Example: "
             "--simulate 2h=50%% 3h:sleep=-20%% 1h:nodata 45m=82%% ac=24.2w")
    drop.add_argument("--start-soc", type=float, default=97.0,
                      help="synthetic: SoC when the laptop goes to sleep (default 97)")
    drop.add_argument("--resume-soc", type=float, default=40.0,
                      help="synthetic: SoC when the laptop resumes (default 40)")
    drop.add_argument("--sleep-hours", type=float, default=6.0,
                      help="synthetic: length of the sleep interval (default 6)")
    drop.add_argument("--pre-hours", type=float, default=3.0,
                      help="synthetic: measured history before sleep (default 3)")
    drop.add_argument("--post-hours", type=float, default=1.0,
                      help="synthetic: measured history after resume (default 1)")
    drop.add_argument("--after", choices=("charging", "discharging"),
                      default="charging", help="synthetic: battery direction after resume")
    drop.add_argument("--charge-rate", type=float, default=20.0,
                      help="synthetic: %%/hour while charging after resume")
    drop.add_argument("--discharge-rate", type=float, default=8.0,
                      help="synthetic: %%/hour while discharging after resume")
    drop.add_argument("--profile", default=None,
                      help="power profile in the title (synthetic default balanced; "
                           "--simulate default: the live profile)")
    drop.add_argument("--health", type=float, default=None,
                      help="State-of-Health %% (synthetic default 94.3; "
                           "--simulate default: the live SoH)")
    drop.add_argument("--now", type=int, default=None,
                      help="synthetic: fixed epoch seconds for a deterministic render "
                           f"(default {DEFAULT_NOW})")
    return parser


def run(argv: Sequence[str] | None = None) -> str:
    """Build the selected scenario and return the rendered dashboard string."""
    args = build_parser().parse_args(argv)
    if args.scenario != "sleep-drop":  # pragma: no cover - argparse enforces choices
        raise SystemExit(f"unknown scenario: {args.scenario}")

    if args.simulate is not None:
        try:
            blocks, context = parse_timeline(args.simulate)
        except ValueError as error:
            raise SystemExit(f"--simulate: {error}") from error
        try:
            inputs = build_from_live_timeline(
                blocks=blocks, context=context,
                profile=args.profile, health_percent=args.health,
            )
        except SimulationError as error:
            raise SystemExit(str(error)) from error
        return inputs.render()

    return build_sleep_drop(
        now=DEFAULT_NOW if args.now is None else args.now,
        start_soc=args.start_soc, resume_soc=args.resume_soc,
        sleep_hours=args.sleep_hours, pre_hours=args.pre_hours,
        post_hours=args.post_hours, after=args.after,
        charge_rate=args.charge_rate, discharge_rate=args.discharge_rate,
        profile="balanced" if args.profile is None else args.profile,
        health_percent=94.3 if args.health is None else args.health,
    ).render()


def main(argv: Sequence[str] | None = None) -> int:
    print(run(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Explicit, pre-cutover schema-v4 trial collector and renderer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from battery_status_tui.sources import BatterySource, SourceUnavailable
from battery_status_tui.system_status import PowerProfileResolver
from battery_status_tui.v1_history import V1HistoryError
from battery_status_tui.v1_runtime import collect_v1, render_v1
from battery_status_tui.v1_storage import V1Storage, V1StorageError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run an explicit schema-v4 battery trial")
    result.add_argument("--database", type=Path, required=True,
                        help="separate schema-v4 trial database (required)")
    result.add_argument("--render-only", action="store_true",
                        help="render without collecting or writing")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    storage = V1Storage(args.database)
    try:
        measurement = None
        if not args.render_only:
            profile = PowerProfileResolver().resolve()
            measurement, _result = collect_v1(
                BatterySource(), storage,
                profile=None if profile is None else profile.profile,
            )
        print(render_v1(storage, current=measurement))
        return 0
    except (SourceUnavailable, V1HistoryError, V1StorageError) as error:
        print(f"battery-status-tui v1 trial: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

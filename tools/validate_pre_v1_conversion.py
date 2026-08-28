#!/usr/bin/env python3
"""Validate a temporary pre-v1 v2-to-v4 conversion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from battery_status_tui.pre_v1_validator import validate_pre_v1_conversion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="read-only pre-v1 schema-v2 database")
    parser.add_argument("destination", type=Path, help="read-only converted schema-v4 database")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    report = validate_pre_v1_conversion(args.source, args.destination)
    print(report.to_json() if args.json else report.to_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Temporary developer tool for offline conversion of a pre-v1 database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from battery_status_tui.pre_v1_converter import convert_v2_to_v1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="read-only pre-v1 schema-v2 database")
    parser.add_argument("destination", type=Path, help="new schema-v4 database")
    args = parser.parse_args()
    converted = convert_v2_to_v1(args.source, args.destination)
    print(converted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

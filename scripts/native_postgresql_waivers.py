#!/usr/bin/env python3
"""Serialize the canonical native PostgreSQL skip waivers for other execution paths."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pr_certification


def serialized_waivers(*, test_files: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not test_files:
        return pr_certification.NATIVE_WAIVERS
    return pr_certification._native_waivers_for_selection(
        mode="focused",
        test_files=list(test_files),
    )


def waiver_cli_args(*, test_files: tuple[str, ...] = ()) -> tuple[str, ...]:
    argv: list[str] = []
    for waiver in serialized_waivers(test_files=test_files):
        argv.extend(("--waive-skip", waiver))
    return tuple(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="native_postgresql_waivers.py")
    parser.add_argument("command", choices=("args",))
    parser.add_argument("--test-file", action="append", default=[])
    args = parser.parse_args(argv)
    for value in waiver_cli_args(test_files=tuple(args.test_file)):
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Minimal mock executor used by test_build_executor.

Exposes ``--grid-point`` and ``--output-dir`` so discovery tests can verify
they see the right flags. Exits cleanly without touching the filesystem
unless ``--output-dir`` is writable.
"""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mock executor_a")
    parser.add_argument("--grid-point", required=True, help="grid point identifier")
    parser.add_argument("--output-dir", required=True, help="where results land")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.makedirs(args.output_dir, exist_ok=True)
    print(f"mock executor_a grid_point={args.grid_point} output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

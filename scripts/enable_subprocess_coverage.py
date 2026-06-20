"""Install the ``.pth`` that turns on coverage in spawned subprocesses.

Many CLI tests exercise the wire by spawning a child ``python`` (see
``tests/_subprocess.run_cli`` and the per-contract ``_run`` helpers). Coverage
only sees a child process if, *at interpreter startup*, that child calls
``coverage.process_startup()`` — which it does only when (a) a ``.pth`` in
site-packages invokes it and (b) ``COVERAGE_PROCESS_START`` is set in the
child's environment. This script installs (a); the coverage CI step sets (b),
and the canonical ``run_cli`` helper forwards the env var into the children it
spawns.

The ``.pth`` is idempotent and a no-op unless ``COVERAGE_PROCESS_START`` is set,
so installing it never perturbs a normal (non-coverage) run. Pair it with
``parallel = true`` in ``[tool.coverage.run]`` (already set in pyproject) so
each process writes its own data file and pytest-cov combines them.

Usage::

    python scripts/enable_subprocess_coverage.py          # install
    python scripts/enable_subprocess_coverage.py --check   # verify installed

Run before a coverage sweep that should include subprocess lines::

    python scripts/enable_subprocess_coverage.py
    COVERAGE_PROCESS_START="$PWD/pyproject.toml" \
        pytest -m 'not slow' --cov=src/hpc_agent --cov-report=term-missing
"""

from __future__ import annotations

import argparse
import sys
import sysconfig
from pathlib import Path

_PTH_NAME = "hpc_agent_subprocess_coverage.pth"

# A .pth line may run code only if it starts with ``import``; everything after
# the first ``;`` executes at interpreter startup. Guarded so it is a no-op
# (and import-safe if coverage is absent) unless COVERAGE_PROCESS_START is set.
_PTH_LINE = (
    "import os, sys; "
    "exec('try:\\n"
    " import coverage\\n"
    "except ImportError:\\n"
    " pass\\n"
    "else:\\n"
    ' (os.environ.get("COVERAGE_PROCESS_START") and coverage.process_startup())\')'
)


def _pth_path() -> Path:
    return Path(sysconfig.get_paths()["purelib"]) / _PTH_NAME


def install() -> Path:
    path = _pth_path()
    path.write_text(_PTH_LINE + "\n", encoding="utf-8")
    return path


def is_installed() -> bool:
    path = _pth_path()
    return path.is_file() and path.read_text(encoding="utf-8").strip() == _PTH_LINE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the .pth is not installed (do not write).",
    )
    args = parser.parse_args(argv)

    if args.check:
        if is_installed():
            print(f"subprocess-coverage .pth present: {_pth_path()}")
            return 0
        print(f"subprocess-coverage .pth MISSING: {_pth_path()}", file=sys.stderr)
        return 1

    path = install()
    print(f"installed subprocess-coverage .pth: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

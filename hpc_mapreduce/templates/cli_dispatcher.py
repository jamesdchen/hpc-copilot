"""Auto-generated dispatcher for one experiment's executors.

Copied verbatim into ``.hpc/cli.py`` by ``/submit-hpc`` Step 6 — never
hand-edit. The body never changes per-experiment; what changes is the
sibling ``tasks.py``'s FLAGS dict and ``resolve``/``total`` functions.

Cluster (and local) invocation:

    python -m cli <executor_module> --output-file ... <other flags>

The first positional arg is the importable module path of the executor
(e.g. ``src.ml_ridge``). The dispatcher looks up that module's flag list
in ``tasks.FLAGS``, parses the remainder of argv against it, then imports
the executor and calls ``compute(args)``.

The convention every executor module satisfies is exactly one function:

    def compute(args) -> None: ...

For ``python -m cli ...`` to find this file, ``.hpc/`` must be on
``PYTHONPATH``. The submit-time job script template injects
``PYTHONPATH=.hpc:$PYTHONPATH`` before launching the executor.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
from pathlib import Path

from hpc_mapreduce.executor_cli import build_parser_from_flags


def _load_tasks():
    """Import the sibling ``tasks.py`` via importlib.

    Direct ``import tasks`` would also work once .hpc/ is on PYTHONPATH,
    but the file-spec loader makes this dispatcher self-contained — no
    assumption that an unrelated top-level ``tasks`` module isn't
    shadowing the .hpc one.
    """
    spec = importlib.util.spec_from_file_location(
        "tasks", Path(__file__).parent / "tasks.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_parser(executor_module: str, description: str = "") -> argparse.ArgumentParser:
    """Build the per-executor argparse parser from ``tasks.FLAGS``.

    Errors fast on unknown executor module — easier to spot a typo
    upfront than to debug an empty argparse later.
    """
    tasks = _load_tasks()
    flags_dict = getattr(tasks, "FLAGS", None)
    if not isinstance(flags_dict, dict):
        raise TypeError(
            f"tasks.FLAGS must be a dict[str, list[Flag]]; "
            f"got {type(flags_dict).__name__}"
        )
    if executor_module not in flags_dict:
        raise KeyError(
            f"unknown executor module {executor_module!r}; "
            f"available in tasks.FLAGS: {sorted(flags_dict)}"
        )
    return build_parser_from_flags(
        flags_dict[executor_module],
        description=description or executor_module,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(
            "usage: python -m cli <executor_module> [flags ...]\n"
            "  e.g.: python -m cli src.ml_ridge --output-file out.csv --horizon 1"
        )
    executor_module = sys.argv.pop(1)
    args = get_parser(executor_module).parse_args()
    importlib.import_module(executor_module).compute(args)

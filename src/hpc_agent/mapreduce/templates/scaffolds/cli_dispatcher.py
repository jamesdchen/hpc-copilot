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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ─── inlined from hpc_agent.executor_cli ───────────────────────────────
#
# This file is delivered to the cluster as ``.hpc/cli.py`` and runs in a
# stdlib-only Python: the package ``hpc_agent`` is NOT installed
# there. The same constraint already drives the inline copies in
# ``combine.py`` / ``dispatch.py``. We duplicate the (~30 LOC) Flag /
# build_parser_from_flags surface verbatim rather than push the whole
# ``executor_cli.py`` module to the cluster, which would widen the
# remote runtime footprint.
#
# Keep this in lock-step with hpc_agent.executor_cli.{Flag,
# build_parser_from_flags}; ``tests/test_executor_cli_inline_parity.py``
# (or equivalent) should AST-compare the two when added.


@dataclass(frozen=True)
class Flag:
    """Declarative spec for one argparse flag (inlined from executor_cli)."""

    name: str
    type: type | None = str
    default: Any = None
    required: bool = False
    choices: tuple[Any, ...] | None = None
    help: str = ""
    nargs: str | None = None
    action: str | None = None

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        kwargs: dict[str, Any] = {"help": self.help}
        if self.required:
            kwargs["required"] = True
        if self.default is not None:
            kwargs["default"] = self.default
        elif not self.required and self.action is None:
            # Explicit None default for optional value-flags; lets executors
            # do `if args.foo is not None:` reliably. Skipped when an
            # ``action`` is set — a ``store_true`` flag must keep its
            # natural ``False`` default, not become ``None`` when absent.
            kwargs["default"] = None
        if self.choices is not None:
            kwargs["choices"] = list(self.choices)
        if self.nargs is not None:
            kwargs["nargs"] = self.nargs
        if self.action is not None:
            kwargs["action"] = self.action
        elif self.type is not None:
            kwargs["type"] = self.type
        cli_flag = "--" + self.name.replace("_", "-")
        parser.add_argument(cli_flag, **kwargs)


def build_parser_from_flags(
    flags: list[Flag] | list[dict[str, Any]],
    *,
    description: str = "",
) -> argparse.ArgumentParser:
    """Build an argparse parser from a declarative flag list (inlined)."""
    parser = argparse.ArgumentParser(description=description)
    for f in flags:
        if isinstance(f, Flag):
            f.add_to(parser)
        elif isinstance(f, dict):
            Flag(**f).add_to(parser)
        else:
            raise TypeError(
                f"FLAGS entries must be Flag instances or dicts; got {type(f).__name__}"
            )
    return parser


# ─── end of inlined section ────────────────────────────────────────────────


def _load_tasks():
    """Import the sibling ``tasks.py`` via importlib.

    Direct ``import tasks`` would also work once .hpc/ is on PYTHONPATH,
    but the file-spec loader makes this dispatcher self-contained — no
    assumption that an unrelated top-level ``tasks`` module isn't
    shadowing the .hpc one.
    """
    spec = importlib.util.spec_from_file_location("tasks", Path(__file__).parent / "tasks.py")
    if spec is None or spec.loader is None:
        raise ImportError("could not load tasks.py next to the dispatcher")
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
            f"tasks.FLAGS must be a dict[str, list[Flag]]; got {type(flags_dict).__name__}"
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

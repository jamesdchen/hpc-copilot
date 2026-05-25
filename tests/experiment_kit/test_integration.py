"""End-to-end: a notebook → ``export_notebook`` → executor ``compute(args)``."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from hpc_agent.experiment_kit import export_notebook


def _notebook_json() -> str:
    return json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "source": "# vol forecast experiment"},
                {
                    "cell_type": "code",
                    "source": "from hpc_agent.experiment_kit import register_run\n",
                },
                {"cell_type": "code", "source": "SCALE_BASE = 10\n"},
                {
                    "cell_type": "code",
                    "source": (
                        "@register_run\n"
                        "def run(scale: float = 1.0):\n"
                        "    return {'value': SCALE_BASE * scale}\n"
                    ),
                },
                # An exploratory smoke-test call — must NOT survive export,
                # or importing the executor would run it at import time.
                {"cell_type": "code", "source": "run(scale=3.0)\n"},
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def test_notebook_exports_to_a_dispatchable_executor(tmp_path: Path) -> None:
    nb = tmp_path / "experiment.ipynb"
    nb.write_text(_notebook_json())
    out = tmp_path / "experiment.py"
    export_notebook(nb, out)

    # The exploratory cell was dropped; the surface is importable.
    exported = out.read_text()
    assert "run(scale=3.0)" not in exported

    # Inline mode: the executor is self-contained — imports nothing from
    # hpc_agent, so it runs on a stdlib-only cluster with no install.
    import ast as _ast

    for node in _ast.walk(_ast.parse(exported)):
        if isinstance(node, _ast.ImportFrom) and node.module:
            assert not node.module.startswith("hpc_agent")
        elif isinstance(node, _ast.Import):
            assert not any(a.name.startswith("hpc_agent") for a in node.names)

    # Import the generated module the way the dispatcher would.
    mod_name = "hpc_tmpl_integration_experiment"
    spec = importlib.util.spec_from_file_location(mod_name, out)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)

        # ``register_run`` injected the executor-contract entry point.
        assert callable(mod.compute)
        assert "run" in mod._RUNS

        # Dispatch one task: compute(args) -> JSON results-by-return.
        result_file = tmp_path / "out.json"
        mod.compute(argparse.Namespace(scale=4.0, output_file=str(result_file)))
        assert json.loads(result_file.read_text()) == {"value": 40.0}
    finally:
        sys.modules.pop(mod_name, None)

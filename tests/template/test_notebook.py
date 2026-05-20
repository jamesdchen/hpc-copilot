"""``export_notebook`` strict-AST extraction (Layer 1)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from hpc_agent.template import export_notebook


def _notebook(*code_cells: str, markdown: str = "intro") -> dict:
    cells: list[dict] = [{"cell_type": "markdown", "source": markdown}]
    cells += [
        {"cell_type": "code", "source": c, "execution_count": None, "outputs": []}
        for c in code_cells
    ]
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def test_keeps_imports_defs_classes_and_upper_assignments(tmp_path: Path) -> None:
    nb = tmp_path / "experiment.ipynb"
    nb.write_text(
        json.dumps(
            _notebook(
                "import numpy as np\nfrom hpc_agent.template import register_run\n",
                "WINDOW = 48\nTRAIN_DAYS = 30\n",
                # Exploratory scratch — must all be dropped.
                "df = np.zeros(10)\nprint(df)\ndf.mean()\n",
                "class Model:\n    pass\n",
                "@register_run\ndef run(alpha: float = 1.0):\n    return {'alpha': alpha}\n",
                # A smoke-test call at the bottom of the notebook.
                "run(alpha=2.0)\n",
            )
        )
    )
    out = tmp_path / "experiment.py"
    export_notebook(nb, out)
    text = out.read_text()

    # Kept.
    assert "import numpy as np" in text
    assert "from hpc_agent.template import register_run" in text
    assert "WINDOW = 48" in text
    assert "TRAIN_DAYS = 30" in text
    assert "class Model:" in text
    assert "def run(alpha: float = 1.0):" in text
    assert "@register_run" in text

    # Dropped.
    assert "df = np.zeros" not in text
    assert "print(df)" not in text
    assert "df.mean()" not in text
    assert "run(alpha=2.0)" not in text

    # The result is valid Python.
    ast.parse(text)


def test_skips_non_code_and_unparseable_cells(tmp_path: Path) -> None:
    nb = tmp_path / "n.ipynb"
    nb.write_text(
        json.dumps(
            _notebook(
                "def keep():\n    return 1\n",
                "def broken(:\n",  # syntax error — whole cell skipped
            )
        )
    )
    out = tmp_path / "n.py"
    export_notebook(nb, out)
    text = out.read_text()
    assert "def keep():" in text
    assert "broken" not in text


def test_source_as_string_or_list(tmp_path: Path) -> None:
    # nbformat stores cell source as a list of lines; accept both.
    nb = tmp_path / "n.ipynb"
    nb.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "source": ["X = 1\n", "y = 2\n"]},
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        )
    )
    out = tmp_path / "n.py"
    export_notebook(nb, out)
    text = out.read_text()
    assert "X = 1" in text
    assert "y = 2" not in text

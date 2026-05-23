"""Tests for the ``export-package`` primitive."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._schema_models.actions.export_package import ExportPackageInput
from hpc_agent.atoms.export_package import export_package


def _write_nb(path: Path, cells: list[str]) -> None:
    """Write a minimal .ipynb with one code cell per source string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "cells": [
            {"cell_type": "code", "source": src, "metadata": {}, "outputs": []} for src in cells
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _experiment(tmp_path: Path) -> Path:
    nbs = tmp_path / "notebooks"
    _write_nb(
        nbs / "pipeline" / "01_scaling.ipynb",
        ["# export\ndef scale(x):\n    return x * 2\n", "print('scratch — not exported')\n"],
    )
    _write_nb(
        nbs / "executors" / "ml_ridge.ipynb",
        [
            "from hpc_agent.incorporation.template import register_run\n\n"
            "@register_run\ndef run(alpha: float = 0.1):\n    return [alpha]\n"
        ],
    )
    return tmp_path


def test_export_package_builds_src(tmp_path) -> None:
    out = export_package(_experiment(tmp_path), spec=ExportPackageInput())
    assert set(out["built"]) == {"src/scaling.py", "src/ml_ridge.py"}
    assert out["n_notebooks"] == 2
    src = tmp_path / "src"
    assert (src / "__init__.py").is_file()
    # Marker exporter: the `# export` directive line is stripped.
    scaling = (src / "scaling.py").read_text()
    assert "def scale(x):" in scaling
    assert "# export" not in scaling
    assert "scratch" not in scaling
    # Strict-AST exporter inlines the runtime for the @register_run executor.
    assert "register_run" in (src / "ml_ridge.py").read_text()


def test_export_package_idempotent_and_byte_stable(tmp_path) -> None:
    exp = _experiment(tmp_path)
    export_package(exp, spec=ExportPackageInput())
    first = (exp / "src" / "scaling.py").read_bytes()

    out2 = export_package(exp, spec=ExportPackageInput())
    assert out2["built"] == []
    assert set(out2["cache_hits"]) == {
        "notebooks/pipeline/01_scaling.ipynb",
        "notebooks/executors/ml_ridge.ipynb",
    }

    out3 = export_package(exp, spec=ExportPackageInput(force=True))
    assert set(out3["built"]) == {"src/scaling.py", "src/ml_ridge.py"}
    second = (exp / "src" / "scaling.py").read_bytes()
    assert first == second  # byte-stable across a forced rebuild


def test_export_package_ordering_prefix_stripped(tmp_path) -> None:
    out = export_package(_experiment(tmp_path), spec=ExportPackageInput())
    # 01_scaling.ipynb -> src/scaling.py (the "01_" prefix is dropped).
    assert "src/scaling.py" in out["built"]


def test_export_package_detects_output_collision(tmp_path) -> None:
    nbs = tmp_path / "notebooks" / "pipeline"
    _write_nb(nbs / "01_loading.ipynb", ["# export\nX = 1\n"])
    _write_nb(nbs / "02_loading.ipynb", ["# export\nY = 2\n"])
    with pytest.raises(errors.SpecInvalid, match="collision"):
        export_package(tmp_path, spec=ExportPackageInput())


def test_export_package_no_notebooks(tmp_path) -> None:
    out = export_package(tmp_path, spec=ExportPackageInput())
    assert out["n_notebooks"] == 0
    assert out["built"] == []

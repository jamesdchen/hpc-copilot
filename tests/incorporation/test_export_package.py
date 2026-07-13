"""Tests for the ``export-package`` primitive."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.export_package import ExportPackageInput
from hpc_agent.incorporation.export_package import export_package


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


def _write_pct(path: Path, cells: list[str]) -> None:
    """Write a jupytext percent-format ``.py`` with one ``# %%`` cell per source."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"# %%\n{src.rstrip()}\n" for src in cells)
    path.write_text(body, encoding="utf-8")


def _experiment(tmp_path: Path) -> Path:
    nbs = tmp_path / "notebooks"
    _write_nb(
        nbs / "pipeline" / "01_scaling.ipynb",
        ["# export\ndef scale(x):\n    return x * 2\n", "print('scratch — not exported')\n"],
    )
    _write_nb(
        nbs / "executors" / "ml_ridge.ipynb",
        [
            "from hpc_agent.experiment_kit import register_run\n\n"
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


# ── percent-format .py notebooks (the lane's native format) ────────────────


def _percent_experiment(tmp_path: Path) -> Path:
    """A percent-format twin of ``_experiment``: one pipeline library, one
    ``@register_run`` executor, scratch cells included."""
    nbs = tmp_path / "notebooks"
    _write_pct(
        nbs / "pipeline" / "01_scaling.py",
        ["# export\ndef scale(x):\n    return x * 2\n", "print('scratch — not exported')\n"],
    )
    _write_pct(
        nbs / "executors" / "ml_ridge.py",
        [
            "from hpc_agent.experiment_kit import register_run\n",
            "@register_run\ndef run(alpha: float = 0.1):\n    return [alpha]\n",
            "run(alpha=0.2)  # scratch smoke test\n",
        ],
    )
    return tmp_path


def test_export_package_builds_src_from_percent(tmp_path) -> None:
    """Percent ``.py`` notebooks export through the SAME exporters as ipynb:
    strict-AST (runtime inlined) for the @register_run executor, # export
    marker for the pipeline library."""
    out = export_package(_percent_experiment(tmp_path), spec=ExportPackageInput())
    assert set(out["built"]) == {"src/scaling.py", "src/ml_ridge.py"}
    assert out["n_notebooks"] == 2
    src = tmp_path / "src"
    scaling = (src / "scaling.py").read_text()
    assert "def scale(x):" in scaling
    assert "# export" not in scaling
    assert "scratch" not in scaling
    ridge = (src / "ml_ridge.py").read_text()
    assert "def register_run(" in ridge  # inlined runtime
    assert "def run(alpha" in ridge
    assert "smoke test" not in ridge  # scratch cell dropped


def test_export_package_percent_cache_hits_and_invalidation(tmp_path) -> None:
    """A re-export with no edits is all cache hits (hash = normalized source);
    an edit is a cache miss for that notebook only."""
    exp = _percent_experiment(tmp_path)
    export_package(exp, spec=ExportPackageInput())

    out2 = export_package(exp, spec=ExportPackageInput())
    assert out2["built"] == []
    assert set(out2["cache_hits"]) == {
        "notebooks/pipeline/01_scaling.py",
        "notebooks/executors/ml_ridge.py",
    }

    # CRLF round-trip of the same content is NOT a cache miss — the digest is
    # over the normalized source (sha256_normalized).
    ridge = exp / "notebooks" / "executors" / "ml_ridge.py"
    text = ridge.read_text(encoding="utf-8")
    ridge.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    out3 = export_package(exp, spec=ExportPackageInput())
    assert out3["built"] == []

    # A real edit is a miss for that notebook only.
    ridge.write_text(text.replace("0.1", "0.5"), encoding="utf-8")
    out4 = export_package(exp, spec=ExportPackageInput())
    assert out4["built"] == ["src/ml_ridge.py"]
    assert out4["cache_hits"] == ["notebooks/pipeline/01_scaling.py"]


def test_export_package_py_ipynb_stem_collision_refused(tmp_path) -> None:
    """A ``foo.py`` / ``foo.ipynb`` pair mapping to the same src/ module is a
    loud SpecInvalid, like any duplicate stem."""
    nbs = tmp_path / "notebooks" / "pipeline"
    _write_pct(nbs / "loading.py", ["# export\nX = 1\n"])
    _write_nb(nbs / "01_loading.ipynb", ["# export\nY = 2\n"])
    with pytest.raises(errors.SpecInvalid, match="collision"):
        export_package(tmp_path, spec=ExportPackageInput())


def test_export_package_mixed_formats_coexist(tmp_path) -> None:
    """Back-compat: an existing ipynb repo keeps working alongside percent
    notebooks with distinct stems."""
    exp = _experiment(tmp_path)  # ipynb: scaling + ml_ridge
    _write_pct(
        exp / "notebooks" / "executors" / "ml_lasso.py",
        [
            "from hpc_agent.experiment_kit import register_run\n",
            "@register_run\ndef run(lam: float = 0.1):\n    return [lam]\n",
        ],
    )
    out = export_package(exp, spec=ExportPackageInput())
    assert set(out["built"]) == {"src/scaling.py", "src/ml_ridge.py", "src/ml_lasso.py"}
    assert out["n_notebooks"] == 3

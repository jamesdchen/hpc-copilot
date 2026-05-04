"""Tests for the ``runtime: uv`` template preamble.

Verifies the four shipped templates (SGE CPU/GPU, SLURM CPU/GPU) all
gate the ``uv sync`` preamble on ``HPC_RUNTIME=uv`` and fail fast when
``uv`` is missing from PATH.

Templates are read as static text — no scheduler is invoked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_mapreduce import _PACKAGE_ROOT

if TYPE_CHECKING:
    from pathlib import Path

TEMPLATES = [
    _PACKAGE_ROOT / "templates" / "sge" / "cpu_array.sh",
    _PACKAGE_ROOT / "templates" / "sge" / "gpu_array.sh",
    _PACKAGE_ROOT / "templates" / "slurm" / "cpu_array.slurm",
    _PACKAGE_ROOT / "templates" / "slurm" / "gpu_array.slurm",
]

# Each per-scheduler template now sources the shared preamble for the
# uv-sync block. Check the union of the template body + any preamble it
# sources so the invariants survive the dedup.
COMMON_PREAMBLE = _PACKAGE_ROOT / "templates" / "common" / "hpc_preamble.sh"


def _effective_template_text(template: Path) -> str:
    """Return the per-template text concatenated with any sourced preamble."""
    body = template.read_text(encoding="utf-8")
    sourced = ""
    if 'source "$(dirname "$0")/common/hpc_preamble.sh"' in body:
        sourced += "\n" + COMMON_PREAMBLE.read_text(encoding="utf-8")
    return body + sourced


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_has_hpc_runtime_gate(template: Path) -> None:
    """Every template (or its sourced preamble) gates uv sync on HPC_RUNTIME."""
    text = _effective_template_text(template)
    assert '"${HPC_RUNTIME:-}" = "uv"' in text, (
        f"{template.name} missing HPC_RUNTIME=uv gate"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_runs_uv_sync(template: Path) -> None:
    """Inside the gate, the template (or sourced preamble) runs ``uv sync``."""
    text = _effective_template_text(template)
    assert "uv sync" in text, f"{template.name} missing uv sync"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_fails_fast_without_uv(template: Path) -> None:
    """When HPC_RUNTIME=uv is set but uv is missing, exit non-zero with a
    diagnostic. The plan calls for ``exit 2``."""
    text = _effective_template_text(template)
    assert "command -v uv" in text, f"{template.name} missing uv presence check"
    assert "exit 2" in text, f"{template.name} missing exit 2 on missing uv"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_documents_hpc_runtime(template: Path) -> None:
    """Header comment block must list HPC_RUNTIME alongside other env vars."""
    text = _effective_template_text(template)
    assert "HPC_RUNTIME" in text


def test_submit_input_schema_accepts_runtime() -> None:
    """The submit.input.json schema accepts an optional runtime field."""
    import json

    schema_path = _PACKAGE_ROOT / "schemas" / "submit.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "runtime" in schema["properties"]
    rt = schema["properties"]["runtime"]
    assert "uv" in rt["enum"]
    assert None in rt["enum"]
    # runtime is optional — must not be in required list
    assert "runtime" not in schema.get("required", [])

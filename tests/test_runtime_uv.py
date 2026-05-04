"""Tests for the ``runtime: uv`` template preamble.

Verifies the four shipped templates (SGE CPU/GPU, SLURM CPU/GPU) all
gate the ``uv sync`` preamble on ``HPC_RUNTIME=uv`` and fail fast when
``uv`` is missing from PATH.

Templates are read as static text — no scheduler is invoked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from claude_hpc import _PACKAGE_ROOT

if TYPE_CHECKING:
    from pathlib import Path

TEMPLATES = [
    _PACKAGE_ROOT / "mapreduce" / "templates" / "sge" / "cpu_array.sh",
    _PACKAGE_ROOT / "mapreduce" / "templates" / "sge" / "gpu_array.sh",
    _PACKAGE_ROOT / "mapreduce" / "templates" / "slurm" / "cpu_array.slurm",
    _PACKAGE_ROOT / "mapreduce" / "templates" / "slurm" / "gpu_array.slurm",
]

# Each per-scheduler template now sources the shared preamble for the
# uv-sync block. Check the union of the template body + any preamble it
# sources so the invariants survive the dedup.
COMMON_PREAMBLE = _PACKAGE_ROOT / "mapreduce" / "templates" / "common" / "hpc_preamble.sh"


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
    assert '"${HPC_RUNTIME:-}" = "uv"' in text, f"{template.name} missing HPC_RUNTIME=uv gate"


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


# ─── PR-B: thread caps + NFS staging in the shared preamble ────────────────


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_caps_omp_threads(template: Path) -> None:
    """Every template (via shared preamble) caps OMP_NUM_THREADS to 1 by
    default — survival against the OOM daemon when BLAS spawns 16 threads
    on a 1-core allocation."""
    text = _effective_template_text(template)
    assert 'export OMP_NUM_THREADS="${HPC_OMP_NUM_THREADS:-1}"' in text, (
        f"{template.name} missing OMP_NUM_THREADS cap"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_caps_all_blas_libraries(template: Path) -> None:
    """All five BLAS/OpenMP envs are capped: OMP, MKL, OpenBLAS, NumExpr,
    vecLib. Missing any one of these leaves a hole the OOM daemon can
    drive a truck through."""
    text = _effective_template_text(template)
    for var in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        assert f"export {var}=" in text, f"{template.name} missing {var} cap"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_thread_cap_is_overridable(template: Path) -> None:
    """User can override per-experiment via $HPC_OMP_NUM_THREADS=N in the
    spec's job_env. The ``${HPC_OMP_NUM_THREADS:-1}`` form ensures the
    user's override wins when set."""
    text = _effective_template_text(template)
    # All five caps must use the HPC_-prefixed override knob.
    for var, override in (
        ("OMP_NUM_THREADS", "HPC_OMP_NUM_THREADS"),
        ("MKL_NUM_THREADS", "HPC_MKL_NUM_THREADS"),
        ("OPENBLAS_NUM_THREADS", "HPC_OPENBLAS_NUM_THREADS"),
        ("NUMEXPR_NUM_THREADS", "HPC_NUMEXPR_NUM_THREADS"),
        ("VECLIB_MAXIMUM_THREADS", "HPC_VECLIB_NUM_THREADS"),
    ):
        assert f'export {var}="${{{override}:-1}}"' in text, (
            f"{template.name} {var} not overridable via {override}"
        )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_nfs_staging_gated_on_env(template: Path) -> None:
    """NFS staging is opt-in: gated on $HPC_NFS_DATA_DIR being set, so
    users without an NFS dataset pay nothing."""
    text = _effective_template_text(template)
    assert 'if [ -n "${HPC_NFS_DATA_DIR:-}" ]; then' in text, (
        f"{template.name} NFS staging not gated on HPC_NFS_DATA_DIR"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_nfs_staging_exports_local_data_dir(template: Path) -> None:
    """The contract is that user code reads from $LOCAL_DATA_DIR; the
    preamble must export it inside the staging block (and prefer
    $SLURM_TMPDIR/$TMPDIR over a hard-coded path)."""
    text = _effective_template_text(template)
    assert "LOCAL_DATA_DIR=" in text, f"{template.name} missing LOCAL_DATA_DIR export"
    assert "SLURM_TMPDIR" in text, (
        f"{template.name} should prefer $SLURM_TMPDIR for local staging dir"
    )
    assert "rsync -a" in text, f"{template.name} missing rsync staging copy"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_template_no_nfs_staging_when_env_unset(template: Path) -> None:
    """Static contract: when HPC_NFS_DATA_DIR is unset, $LOCAL_DATA_DIR is
    *not* exported — user code can use ``[ -n "${LOCAL_DATA_DIR:-}" ]``
    to detect whether staging happened. Verified by checking the export
    only appears inside the gating ``if`` block."""
    text = _effective_template_text(template)
    # The export of LOCAL_DATA_DIR must come AFTER the gating "if" line
    # and BEFORE its closing "fi" — a coarse but effective sanity check
    # that the staging block remains opt-in.
    if_idx = text.find('if [ -n "${HPC_NFS_DATA_DIR:-}" ]; then')
    assert if_idx >= 0, f"{template.name} missing gating if"
    # Match a standalone "fi" line, not a substring like "final" — a
    # future contributor adding a comment containing "fi..." inside the
    # staging block would otherwise sneak past this test.
    fi_idx = text.find("\nfi\n", if_idx)
    assert fi_idx >= 0, f"{template.name} missing closing fi for gating block"
    local_idx = text.find("export LOCAL_DATA_DIR=", if_idx)
    assert if_idx < local_idx < fi_idx, (
        f"{template.name} LOCAL_DATA_DIR export not inside the gating block"
    )


def test_submit_input_schema_accepts_runtime() -> None:
    """The submit.input.json schema accepts an optional runtime field."""
    import json

    # Schemas have not yet moved to claude_hpc/ at this point in the
    # reorg; resolve via the legacy alias (will be cleaned up in Step 8).
    schema_path = _PACKAGE_ROOT / "schemas" / "submit.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "runtime" in schema["properties"]
    rt = schema["properties"]["runtime"]
    assert "uv" in rt["enum"]
    assert None in rt["enum"]
    # runtime is optional — must not be in required list
    assert "runtime" not in schema.get("required", [])

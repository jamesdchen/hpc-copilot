"""``gpu_preamble.sh`` honors the user's PYTORCH_CUDA_ALLOC_CONF (F21).

The shared GPU preamble is sourced (not inlined) by every gpu_array template
AFTER the scheduler injects the spec's ``job_env``. It used to *unconditionally*
``export PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128``, clobbering a value the
user set (e.g. ``expandable_segments:True``, the common fragmentation-OOM fix) —
unlike every other knob in the file, which honors the ``HPC_<NAME>`` override
convention. These tests source the real file under bash and pin the fixed
precedence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import hpc_agent

_PREAMBLE = (
    Path(hpc_agent.__file__).parent
    / "execution"
    / "mapreduce"
    / "templates"
    / "runtime"
    / "common"
    / "gpu_preamble.sh"
)


def _alloc_conf_after_source(env_overrides: dict[str, str], *, unset: tuple[str, ...] = ()) -> str:
    """Source the preamble under bash and return the resulting
    ``PYTORCH_CUDA_ALLOC_CONF`` (empty string when left unset)."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI always has bash
        pytest.skip("bash not available")
    setters = "".join(f"export {k}={v!r}\n" for k, v in env_overrides.items())
    unsetters = "".join(f"unset {k}\n" for k in unset)
    script = (
        f"{unsetters}{setters}"
        f'source "{_PREAMBLE}" >/dev/null 2>&1\n'
        'printf "%s" "${PYTORCH_CUDA_ALLOC_CONF-}"\n'
    )
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_user_pytorch_alloc_conf_survives() -> None:
    """F21 fire path: an already-set PYTORCH_CUDA_ALLOC_CONF is NOT clobbered."""
    out = _alloc_conf_after_source({"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
    assert out == "expandable_segments:True"


def test_default_applied_when_unset() -> None:
    """Unset → the framework default still lands (back-compatible)."""
    out = _alloc_conf_after_source(
        {}, unset=("PYTORCH_CUDA_ALLOC_CONF", "HPC_PYTORCH_CUDA_ALLOC_CONF")
    )
    assert out == "max_split_size_mb:128"


def test_hpc_override_wins() -> None:
    """An explicit HPC_ override wins over both the default and a base value."""
    out = _alloc_conf_after_source(
        {
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "HPC_PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:512",
        }
    )
    assert out == "max_split_size_mb:512"


def test_empty_hpc_override_disables_knob() -> None:
    """An explicitly-empty HPC_ override disables the knob (convention: leave
    the variable unset), matching the CUBLAS/XLA behavior in the same file."""
    out = _alloc_conf_after_source(
        {"HPC_PYTORCH_CUDA_ALLOC_CONF": ""},
        unset=("PYTORCH_CUDA_ALLOC_CONF",),
    )
    assert out == ""

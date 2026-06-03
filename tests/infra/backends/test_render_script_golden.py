"""Byte-for-byte golden tests for ``render_script``.

Asserts that ``render_script(PROFILE, kind=...)`` reproduces the historical
runtime array template files exactly.

The reference bytes are committed under ``tests/infra/backends/golden/``
(captured from the last commit where the static templates existed, before
the profile migration deleted them). Golden tests must pin to a *fixed*
committed reference — not ``git show HEAD:`` — so the contract stays stable
as HEAD advances. If a template's content legitimately changes, regenerate
the matching golden fixture in the same commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_GOLDEN_DIR = Path(__file__).parent / "golden"

# (profile_const_name, kind, golden fixture filename)
_GOLDEN = [
    ("SLURM_PROFILE", "cpu", "slurm__cpu_array.slurm"),
    ("SLURM_PROFILE", "gpu", "slurm__gpu_array.slurm"),
    ("SGE_PROFILE", "cpu", "sge__cpu_array.sh"),
    ("SGE_PROFILE", "gpu", "sge__gpu_array.sh"),
    ("PBSPRO_PROFILE", "cpu", "pbspro__cpu_array.pbs"),
    ("PBSPRO_PROFILE", "gpu", "pbspro__gpu_array.pbs"),
    ("TORQUE_PROFILE", "cpu", "torque__cpu_array.pbs"),
    ("TORQUE_PROFILE", "gpu", "torque__gpu_array.pbs"),
]


def _golden_bytes(filename: str) -> bytes:
    return (_GOLDEN_DIR / filename).read_bytes()


def test_golden_fixtures_present():
    """Sanity: every golden fixture exists and is non-empty, else the
    byte-match cases below are silently vacuous."""
    for _name, _kind, filename in _GOLDEN:
        out = _golden_bytes(filename)
        assert out, f"golden fixture empty/missing: {filename}"


@pytest.mark.parametrize(
    ("profile_name", "kind", "filename"),
    _GOLDEN,
    ids=[f"{n}-{k}" for n, k, _ in _GOLDEN],
)
def test_render_script_matches_golden(profile_name, kind, filename):
    """render_script(PROFILE, kind=...) must equal the golden file byte-for-byte."""
    from hpc_agent.infra.backends import profile as profile_mod

    prof = getattr(profile_mod, profile_name)
    rendered = profile_mod.render_script(prof, kind=kind)
    assert isinstance(rendered, str), "render_script must return a str"

    golden = _golden_bytes(filename)
    # Compare as bytes via UTF-8 so any trailing-newline / encoding drift is caught.
    assert rendered.encode("utf-8") == golden, (
        f"render_script({profile_name}, kind={kind!r}) diverged from {filename}"
    )


def test_profile_backend_render_script_classmethod_matches_golden():
    """The engine also exposes render_script as a classmethod reading cls.profile."""
    from hpc_agent.infra.backends import get_backend_class

    cls = get_backend_class("slurm")
    rendered = cls.render_script(kind="cpu")
    assert rendered.encode("utf-8") == _golden_bytes("slurm__cpu_array.slurm")

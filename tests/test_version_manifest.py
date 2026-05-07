"""Tests for :mod:`claude_hpc._internal.version`.

The manifest is the cross-domain source of truth for supported schema
versions. These tests check three things:

* ``compatibility_check`` raises :class:`SchemaIncompat` on unsupported
  versions and returns silently on supported ones.
* The :class:`SchemaIncompat` envelope code is what the CLI promises.
* Greppable enforcement: every ``domain`` in the manifest matches a
  ``SCHEMA_VERSION`` (or ``SIDECAR_SCHEMA_VERSION`` etc.) writer
  constant somewhere in the package, and that writer constant is in
  the supported tuple. This protects against silent drift where a
  writer bumps its constant without updating the manifest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claude_hpc import errors
from claude_hpc._internal import version


def test_compatibility_check_silent_on_supported() -> None:
    # All five domains; pick the highest in each tuple.
    for domain, supported in version._MANIFEST.items():
        for v in supported:
            version.compatibility_check(domain, v)


def test_compatibility_check_raises_on_unsupported() -> None:
    with pytest.raises(errors.SchemaIncompat) as exc:
        version.compatibility_check("sidecar", 999)
    assert "999" in str(exc.value)
    assert exc.value.error_code == "schema_incompat"
    assert exc.value.retry_safe is False


def test_compatibility_check_unknown_domain_keyerror() -> None:
    # Unknown domains are a programmer error, not a runtime data
    # problem; keep them as KeyError rather than wrapping.
    with pytest.raises(KeyError):
        version.compatibility_check("nonexistent_domain", 1)


def test_supported_versions_returns_tuple() -> None:
    t = version.supported_versions("sidecar")
    assert isinstance(t, tuple)
    assert 2 in t


# Map manifest domain → expected writer constants. Each tuple is a list
# of (file, constant_name) pairs. We use one canonical writer per
# domain even when readers exist in multiple files.
_WRITER_CONSTANTS = {
    "sidecar": [("src/claude_hpc/state/runs.py", "SIDECAR_SCHEMA_VERSION")],
    "runtime_prior": [("src/claude_hpc/state/runtime_prior.py", "SCHEMA_VERSION")],
    # calibration_prediction and status_rollup write their schemaversion
    # as inline literals (no module-level constant). Verified by other tests.
    "calibration_prediction": [],
    "status_rollup": [],
    "session": [("src/claude_hpc/_internal/session/run_record.py", "SCHEMA_VERSION")],
}


def test_writer_constants_are_in_supported_set() -> None:
    repo = Path(__file__).resolve().parent.parent
    for domain, pairs in _WRITER_CONSTANTS.items():
        supported = version.supported_versions(domain)
        for relpath, name in pairs:
            text = (repo / relpath).read_text()
            # Match e.g.  ``SCHEMA_VERSION: int = 1`` or ``SCHEMA_VERSION = 2``.
            m = re.search(
                rf"^{re.escape(name)}\s*(?::\s*int)?\s*=\s*(\d+)\s*$",
                text,
                re.MULTILINE,
            )
            assert m is not None, f"could not find {name} in {relpath}"
            value = int(m.group(1))
            assert value in supported, (
                f"{relpath}:{name}={value} not in manifest "
                f"supported={list(supported)} for domain={domain!r}"
            )

"""Kit-internal unit mirror for K7 (``conformance/test_attestation_export.py``).

Two jobs, the same shape as ``tests/conformance_kit/test_negotiation.py``:

* **importorskip-guarded delegations** — each leg calls the kit test function
  directly, inheriting the SAME optional-dep guard (the leg skips when
  ``securesystemslib`` / ``in_toto_attestation`` are absent, so the shipped kit
  stays green without the CI-lane deps);
* **an always-runs guard-can-fire leg** — hides the stock libraries and asserts
  the ``importorskip`` guard raises ``Skipped`` CLEANLY (before any seeding),
  proving the skip is reachable rather than dead. This is the guard-can-fire
  discipline (``docs/internals/engineering-principles.md``): a guard nothing can
  trip is a bug, so the mirror trips it.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance import test_attestation_export as kit

if TYPE_CHECKING:
    from pathlib import Path


def _hide(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make ``import <name>`` (and its submodules) fail, reversibly.

    Blocks both a cached submodule (already-imported ``securesystemslib.dsse``)
    and a fresh import of the top-level package by setting the ``sys.modules``
    entries to ``None`` — the standard "module absent" simulation. ``monkeypatch``
    restores the real modules at teardown.
    """
    for name in names:
        for cached in list(sys.modules):
            if cached == name or cached.startswith(name + "."):
                monkeypatch.setitem(sys.modules, cached, None)
        monkeypatch.setitem(sys.modules, name, None)


# --- importorskip-guarded delegations (skip when the CI-lane deps are absent) ---


def test_reference_envelope_parse_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kit.test_each_envelope_parses_under_stock_dsse_model(tmp_path, monkeypatch)


def test_reference_statement_validate_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kit.test_each_payload_validates_as_in_toto_statement_v1(tmp_path, monkeypatch)


def test_reference_digest_round_trip_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kit.test_subject_digests_round_trip_against_the_dossier_manifest(tmp_path, monkeypatch)


def test_reference_unsigned_posture_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kit.test_unsigned_signatures_posture_is_explicit(tmp_path, monkeypatch)


# --- always-runs: the guard CAN fire ----------------------------------------


def test_guard_fires_cleanly_when_stock_libs_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the stock libs hidden, every guarded seam raises ``Skipped`` — cleanly.

    Both helper guards skip, and each full kit leg skips at its FIRST line (the
    ``importorskip``) — before touching the fixture writers — so an environment
    without the optional deps degrades to a clean skip, never an error.
    """
    _hide(monkeypatch, "securesystemslib", "in_toto_attestation")

    with pytest.raises(pytest.skip.Exception):
        kit._stock_envelope_model()
    with pytest.raises(pytest.skip.Exception):
        kit._stock_statement_model()

    for leg in (
        kit.test_each_envelope_parses_under_stock_dsse_model,
        kit.test_each_payload_validates_as_in_toto_statement_v1,
        kit.test_subject_digests_round_trip_against_the_dossier_manifest,
        kit.test_unsigned_signatures_posture_is_explicit,
    ):
        with pytest.raises(pytest.skip.Exception):
            leg(tmp_path, monkeypatch)

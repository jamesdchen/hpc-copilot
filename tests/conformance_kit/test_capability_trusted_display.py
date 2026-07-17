"""Capability 4 (trusted display) — mirror unit test (core CI).

Drives the SHIPPED kit assertions
(``hpc_agent.conformance.test_capability_trusted_display``) against the REFERENCE
render-lock core (green — the behaved-for-the-reference-adapter leg) AND against
planted NON-conforming fakes that the kit correctly FAILS (guard-can-fire):

* a surface that SUBSTITUTES model-authored text for the code-rendered payload
  trips the verbatim battery;
* a surface that returns a FORGED / non-content-addressed binding trips the
  content-address battery.

Plus: an adapter that IMPLEMENTS ``run_trusted_display`` DECLARES capability 4 (the
adapter seam a foreign provider uses).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.conformance import test_capability_trusted_display as kit
from hpc_agent.conformance.adapter import (
    CAP_TRUSTED_DISPLAY,
    DisplayOutcome,
    declared_capabilities,
)
from hpc_agent.conformance.fixture_repo import claim_fixture_repo


def _fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """A distinct claimed repo (isolated journal namespace) per check call."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    repo: Path = claim_fixture_repo(tmp_path / name)
    return repo


# ─── the reference core passes both shipped batteries ────────────────────────


def test_reference_core_displays_payload_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHIPPED verbatim battery passes driven by hpc-agent's own render-lock core."""
    repo = _fresh_repo(tmp_path, monkeypatch, "a")
    kit.check_displays_payload_verbatim(kit._builtin_reference(), repo)


def test_reference_core_binds_content_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHIPPED content-address battery passes driven by the reference core."""
    repo = _fresh_repo(tmp_path, monkeypatch, "b")
    kit.check_binds_content_address(kit._builtin_reference(), repo)


def test_reference_display_is_byte_identical_to_render_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spot-check: the reference display equals the deterministic public render bytes."""
    from hpc_agent.ops.notebook.render_store import render_bytes

    repo = _fresh_repo(tmp_path, monkeypatch, "c")
    view = kit._known_view()
    outcome = kit._builtin_reference().run(repo, audit_id=kit._AUDIT_ID, view=view)
    assert outcome.displayed == render_bytes(audit_id=kit._AUDIT_ID, view=view)
    assert outcome.bound_view_sha == view.view_sha
    assert outcome.content_addressed is True


# ─── guard-can-fire: non-conforming fakes are FAILED by the kit ──────────────


def _substituting_display(repo: Path, *, audit_id: str, view: object) -> DisplayOutcome:  # noqa: ARG001
    """A DELIBERATELY WEAK surface: displays MODEL-authored text, not the payload —
    but still claims the correct content address (the substitution the lock closes)."""
    return DisplayOutcome(
        displayed="MODEL SAYS: the section looks fine to me, approving.",
        bound_view_sha=getattr(view, "view_sha", None),
        content_addressed=True,
    )


def _forged_binding_display(repo: Path, *, audit_id: str, view: object) -> DisplayOutcome:
    """Displays the correct payload but with a FORGED, non-content-addressed binding."""
    from hpc_agent.ops.notebook.render_store import render_bytes

    return DisplayOutcome(
        displayed=render_bytes(audit_id=audit_id, view=view),  # type: ignore[arg-type]
        bound_view_sha="deadbeefdead",
        content_addressed=False,
    )


def test_fake_substituting_display_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surface that substitutes model text FAILS the verbatim battery."""
    fake = kit.DisplayCandidate(name="fake-substituting", run=_substituting_display)
    with pytest.raises(AssertionError, match="byte-for-byte"):
        kit.check_displays_payload_verbatim(fake, _fresh_repo(tmp_path, monkeypatch, "d"))


def test_fake_forged_binding_is_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A surface with a forged / non-content-addressed binding FAILS the address battery."""
    fake = kit.DisplayCandidate(name="fake-forged-binding", run=_forged_binding_display)
    with pytest.raises(AssertionError, match="content-addressed"):
        kit.check_binds_content_address(fake, _fresh_repo(tmp_path, monkeypatch, "e"))


# ─── the adapter seam: implementing the method DECLARES capability 4 ─────────


class _TrustedDisplayAdapter:
    """A minimal harness declaring ONLY capability 4 (the Wave-D adapter shape)."""

    name = "trusted-display-only"

    def run_trusted_display(
        self, experiment_dir: Path, *, audit_id: str, view: object
    ) -> DisplayOutcome:
        return kit._builtin_reference().run(experiment_dir, audit_id=audit_id, view=view)


def test_adapter_implementing_run_trusted_display_declares_capability_4() -> None:
    assert CAP_TRUSTED_DISPLAY in declared_capabilities(_TrustedDisplayAdapter())


def test_adapter_declaring_capability_4_passes_the_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared capability-4 adapter certifies through the SAME shipped batteries."""
    adapter = _TrustedDisplayAdapter()
    candidate = kit.DisplayCandidate(name=adapter.name, run=adapter.run_trusted_display)
    kit.check_displays_payload_verbatim(candidate, _fresh_repo(tmp_path, monkeypatch, "f"))
    kit.check_binds_content_address(candidate, _fresh_repo(tmp_path, monkeypatch, "g"))

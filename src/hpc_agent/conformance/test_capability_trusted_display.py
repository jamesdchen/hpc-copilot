"""Conformance kit — capability 4 (trusted display).

Asserts a harness's trusted-display surface (``run_trusted_display``) DISPLAYS a
KIT-CHOSEN code-rendered payload to the human so code can PROVE the human saw it
VERBATIM: (1) the displayed bytes equal the code-authored payload byte-for-byte
(no model substitution), and (2) the artifact is CONTENT-ADDRESSED — it lives at
its own ``view_sha`` address and its header binds that sha, so a forged/mismatched
binding cannot pass. This is contract capability 4
(``docs/internals/harness-contract.md``, "Capability 4 — trusted display"): the
audit view an agent relays in chat is model-carried and unforceable; the trusted
artifact is the CODE-WRITTEN, content-addressed render file.

The seam is outcome-shaped (:class:`~hpc_agent.conformance.adapter.DisplayOutcome`:
the displayed bytes + the bound content address + a content-addressed flag), never
mechanism-shaped — a filesystem render store and any other trusted surface certify
through the same seam (the D-K3 outcome-not-mechanism rule).

**HONEST STATUS (T9).** This is the BEHAVED leg only. There is NO passive install
marker for a trusted-display surface, so ``harness-capabilities`` honestly reports
``trusted_display: "unknown"`` (never a self-asserted ``true``); the
passive-detection seam is the still-owed follow-on. This module closes
"declared == behaved" for the REFERENCE render-lock core; a FOREIGN trusted-display
provider's proof, and the passive detection seam, remain owed.

Standalone / reference (the K2 pattern): with no ``--harness-adapter`` — OR an
adapter that does not declare capability 4 — the built-in REFERENCE surface,
hpc-agent's own ``render_store.write_render`` / ``read_render_header`` core driven
IN-PROCESS over a real :class:`SectionView`, is the candidate. When an adapter
DECLARES capability 4, its ``run_trusted_display`` is the candidate instead. It
never SKIPs: capability 4 is not part of the three-capability ``conforming: harness
contract v1`` verdict, so the module always certifies the reference core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.adapter import (
    CAP_TRUSTED_DISPLAY,
    DisplayOutcome,
    declared_capabilities,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hpc_agent.ops.notebook.audit_view import SectionView

_AUDIT_ID = "kit-trusted-display"
_SLUG = "model"

# A fixed percent-format template + a source whose ``model`` section is edited (a
# real diff) with two declared assertions — enough that the code-rendered payload
# is non-trivial and its ``view_sha`` is a genuine content hash.
_TEMPLATE = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
"""

_SOURCE = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 99
assert train() == 99, "sanity"
assert train() > 0
"""


def _known_view() -> SectionView:
    """The KIT-CHOSEN code-rendered payload: a REAL ``SectionView`` for ``model``.

    Built through the pinned projection (``build_audit_view`` over the fixed
    template/source), so ``view_sha`` is a genuine content hash and the render
    bytes are deterministic — the same object the reference and any adapter render.
    """
    from hpc_agent.ops.notebook.audit_view import build_audit_view
    from hpc_agent.state.audit_source import parse_percent_source

    view = build_audit_view(parse_percent_source(_SOURCE), parse_percent_source(_TEMPLATE), [])
    return next(sv for sv in view.sections if sv.slug == _SLUG)


# --- the trusted-display candidate seam --------------------------------------


@dataclass(frozen=True)
class DisplayCandidate:
    """A trusted-display surface under test — the reference core or an adapter."""

    name: str
    run: Callable[..., DisplayOutcome]


def _builtin_reference() -> DisplayCandidate:
    """hpc-agent's own render-lock core driven in-process (the reference provider).

    ``write_render`` emits the code-authored payload to the content-addressed store;
    the surface then DISPLAYS the on-disk trusted artifact (reads it back verbatim),
    and ``read_render_header`` recovers the binding. ``content_addressed`` is True
    only when the file lives at the ``view_sha``-addressed path AND its header binds
    that same sha — the anti-substitution property the sign-off gate locks on.
    """
    from hpc_agent.ops.notebook.render_store import (
        read_render_header,
        render_path,
        write_render,
    )

    def run(experiment_dir: Path, *, audit_id: str, view: SectionView) -> DisplayOutcome:
        path = write_render(experiment_dir, audit_id=audit_id, view=view)
        displayed = path.read_text(encoding="utf-8")
        header = read_render_header(path)
        bound = header.get("view_sha") if header else None
        expected_path = render_path(
            experiment_dir, audit_id=audit_id, section=view.slug, view_sha=view.view_sha
        )
        content_addressed = path == expected_path and bound == view.view_sha
        return DisplayOutcome(
            displayed=displayed, bound_view_sha=bound, content_addressed=content_addressed
        )

    return DisplayCandidate(name="hpc-agent (render_store)", run=run)


@pytest.fixture
def trusted_display_candidate(request: pytest.FixtureRequest) -> DisplayCandidate:
    """The trusted-display seam to certify — the adapter's when declared, else reference.

    With ``--harness-adapter`` AND a declared capability 4, the adapter's
    ``run_trusted_display`` is the candidate. Otherwise the built-in reference core
    runs (no SKIP — capability 4 is not a ``conforming: harness contract v1`` verdict
    capability; a FOREIGN proof is the follow-on).
    """
    spec = request.config.getoption("--harness-adapter", default=None)
    if spec:
        adapter = request.getfixturevalue("harness_adapter")
        if CAP_TRUSTED_DISPLAY in declared_capabilities(adapter):
            return DisplayCandidate(
                name=getattr(adapter, "name", "<adapter>"), run=adapter.run_trusted_display
            )
    return _builtin_reference()


# --- assertions (mirror-drivable: first arg is the candidate, second the repo) --


def check_displays_payload_verbatim(candidate: DisplayCandidate, repo: Path) -> None:
    """What the surface DISPLAYS equals the code-authored payload byte-for-byte.

    The kit derives the KNOWN payload independently from the pinned deterministic
    renderer (``render_store.render_bytes``) and compares — a surface that
    substitutes model-authored text (or corrupts a byte) is FAILED here
    (guard-can-fire).
    """
    from hpc_agent.ops.notebook.render_store import render_bytes

    view = _known_view()
    expected = render_bytes(audit_id=_AUDIT_ID, view=view)
    outcome = candidate.run(repo, audit_id=_AUDIT_ID, view=view)
    assert outcome.displayed == expected, (
        f"[{candidate.name}] the trusted surface MUST display the code-rendered payload "
        "byte-for-byte — a model-substituted or corrupted display is not a trusted display"
    )


def check_binds_content_address(candidate: DisplayCandidate, repo: Path) -> None:
    """The displayed artifact is CONTENT-ADDRESSED by its own ``view_sha``.

    The binding the sign-off gate locks on: the artifact lives at its ``view_sha``
    address and its header binds that sha, so a substitution changes the payload and
    thus the address. A surface that returns a forged / mismatched binding (claims
    verbatim without content-addressing) is FAILED here.
    """
    view = _known_view()
    outcome = candidate.run(repo, audit_id=_AUDIT_ID, view=view)
    assert outcome.content_addressed is True, (
        f"[{candidate.name}] a trusted display MUST be content-addressed — the artifact "
        "must live at its view_sha address with a header binding that sha (no forged binding)"
    )
    assert outcome.bound_view_sha == view.view_sha, (
        f"[{candidate.name}] the bound content address must be the payload's own view_sha "
        f"({view.view_sha!r}), got {outcome.bound_view_sha!r}"
    )


def test_trusted_display_displays_payload_verbatim(
    trusted_display_candidate: DisplayCandidate, fixture_repo: Path
) -> None:
    """Capability 4 behaved leg: the surface displays the code-rendered payload verbatim."""
    check_displays_payload_verbatim(trusted_display_candidate, fixture_repo)


def test_trusted_display_binds_content_address(
    trusted_display_candidate: DisplayCandidate, fixture_repo: Path
) -> None:
    """Capability 4 behaved leg: the displayed artifact is content-addressed by its view_sha."""
    check_binds_content_address(trusted_display_candidate, fixture_repo)

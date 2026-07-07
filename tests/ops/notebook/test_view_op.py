"""Direct-atom tests for the ``notebook-audit-view`` query primitive (T5-verb).

Writes a source + template ``.py`` under the experiment dir, calls the primitive,
and asserts on the per-section projection (classification, tier, view_sha),
markdown non-emptiness, view_sha determinism across two invocations, the
spec_invalid path on a missing source, and the receipt-passthrough tier flip
(mirroring T5's tier matrix at the primitive boundary — one case is enough).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.notebook_audit_view import (
    NotebookAuditViewResult,
    NotebookAuditViewSpec,
)
from hpc_agent.ops.notebook.audit_view import AUTO_CLEARED, HUMAN_REQUIRED, INHERITED
from hpc_agent.ops.notebook.view_op import notebook_audit_view

if TYPE_CHECKING:
    from pathlib import Path

# `setup` byte-identical to the template (inherited, no assertions → auto_cleared).
# `model` byte-identical to the template BUT carries an assertion (inherited, but
# not green without a receipt → human_required until a receipt clears it).
_TEMPLATE = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42
"""

_SOURCE = _TEMPLATE


def _write(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")


def _view(tmp_path: Path, **overrides: object) -> NotebookAuditViewResult:
    spec_dict: dict[str, object] = {"source": "source.py", "template": "template.py"}
    spec_dict.update(overrides)
    return notebook_audit_view(
        experiment_dir=tmp_path,
        spec=NotebookAuditViewSpec.model_validate(spec_dict),
    )


def test_happy_path_sections_tiers_and_markdown(tmp_path: Path) -> None:
    _write(tmp_path)
    result = _view(tmp_path)

    by_slug = {s.slug: s for s in result.sections}
    assert set(by_slug) == {"setup", "model"}
    # Both sections are byte-identical to the template → inherited.
    assert all(s.classification == INHERITED for s in result.sections)
    # `setup` has no assertions → auto_cleared; `model` has an unproven assert →
    # human_required (unverified is not green).
    assert by_slug["setup"].tier == AUTO_CLEARED
    assert by_slug["model"].tier == HUMAN_REQUIRED
    # Each section carries a per-section view_sha (what a sign-off binds).
    assert all(len(s.view_sha) == 64 for s in result.sections)
    assert result.markdown.strip()
    assert not result.dropped_template_slugs


def test_view_sha_stable_across_two_invocations(tmp_path: Path) -> None:
    _write(tmp_path)
    first = _view(tmp_path)
    second = _view(tmp_path)
    assert first.view_sha == second.view_sha
    assert first.markdown == second.markdown
    assert [s.view_sha for s in first.sections] == [s.view_sha for s in second.sections]


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="source"):
        _view(tmp_path)


def test_receipt_passthrough_greens_asserted_section(tmp_path: Path) -> None:
    _write(tmp_path)
    # Without a receipt `model` reads human_required (its assert is unproven).
    assert {s.slug: s.tier for s in _view(tmp_path).sections}["model"] == HUMAN_REQUIRED
    # A receipt marking the section error-free greens its assertions → auto_cleared
    # (inherited + no flags + green).
    result = _view(tmp_path, receipt={"model": {"output_sha": "abc", "error": False}})
    assert {s.slug: s.tier for s in result.sections}["model"] == AUTO_CLEARED

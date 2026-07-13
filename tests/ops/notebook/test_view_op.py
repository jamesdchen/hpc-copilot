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
    NotebookSectionView,
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
    spec_dict: dict[str, object] = {
        "audit_id": "aud-1",
        "source": "source.py",
        "template": "template.py",
    }
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


def test_view_writes_byte_deterministic_renders(tmp_path: Path) -> None:
    """Per section the verb WRITES the content-addressed TRUSTED-DISPLAY render at
    the returned render_path, and the bytes are DETERMINISTIC — the same inputs
    yield a byte-identical file (the content-address / idempotence property the T8
    lock relies on), each carrying a header current at its view_sha."""
    from hpc_agent.ops.notebook.render_store import read_render_header

    _write(tmp_path)
    first = _view(tmp_path)
    # Every section gained a render_path and the file exists with a valid header.
    contents: dict[str, bytes] = {}
    for s in first.sections:
        assert s.render_path
        path = tmp_path / s.render_path
        assert path.is_file()
        header = read_render_header(path)
        assert header is not None
        assert header["section"] == s.slug
        assert header["view_sha"] == s.view_sha
        assert header["section_sha"] == s.section_sha
        assert header["audit_id"] == "aud-1"
        contents[s.slug] = path.read_bytes()

    # A second view over the same inputs rewrites byte-identical files.
    second = _view(tmp_path)
    for s in second.sections:
        assert (tmp_path / s.render_path).read_bytes() == contents[s.slug]
    assert [s.render_path for s in first.sections] == [s.render_path for s in second.sections]


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="source"):
        _view(tmp_path)


def test_template_import_shadow_flows_into_view_flags_and_tier(tmp_path: Path) -> None:
    # The canonical view recomputes the lint server-side, so a source section
    # that shadows a template import surfaces as a lint flag attributed to that
    # section, and the section reads human_required.
    template = """\
# %%
# hpc-audit-section: setup
from toy.engine import compute_stat
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
"""
    source = """\
# %%
# hpc-audit-section: setup
from toy.engine import compute_stat
x = 1

# %%
# hpc-audit-section: model
def compute_stat(y):
    return y + 1
"""
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(template, encoding="utf-8")
    result = _view(tmp_path)

    by_slug = {s.slug: s for s in result.sections}
    flags = [f for f in by_slug["model"].lint_flags if f.get("rule") == "template_import_shadowed"]
    assert len(flags) == 1
    assert flags[0]["section"] == "model"
    assert flags[0]["evidence"]["name"] == "compute_stat"
    assert flags[0]["evidence"]["template_slug"] == "setup"
    assert by_slug["model"].tier == HUMAN_REQUIRED
    # The clean sibling carries no shadow flag.
    assert not any(f.get("rule") == "template_import_shadowed" for f in by_slug["setup"].lint_flags)


def test_explicit_output_roots_override_is_a_preview(tmp_path: Path) -> None:
    # No recorded config → explicit output_roots differ from the recorded
    # (empty) ones, so the view is a PREVIEW the T8 gate may refuse.
    _write(tmp_path)
    assert _view(tmp_path).canonical is True
    assert _view(tmp_path, output_roots=["results"]).canonical is False


# ── B1 payload cut: digest-by-default, full markdown only behind `full: true` ──

# `model` is MODIFIED vs the template (body edited) so the full render carries a
# real unified diff whose bytes the contract test can look for.
_TEMPLATE_MODIFIED = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
"""

_SOURCE_MODIFIED = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 99
"""


def _write_modified(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(_SOURCE_MODIFIED, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE_MODIFIED, encoding="utf-8")


def test_section_wire_shape_carries_no_diff_field(tmp_path: Path) -> None:
    # B1: the `sections[].diff` wire duplication is dropped — the field no longer
    # exists on the model, so it cannot ship the diff bytes a second time.
    assert "diff" not in NotebookSectionView.model_fields
    _write_modified(tmp_path)
    dumped = _view(tmp_path).model_dump()
    for section in dumped["sections"]:
        assert "diff" not in section


def test_default_response_carries_no_diff_or_full_body_bytes(tmp_path: Path) -> None:
    # The DEFAULT (full omitted) response is the DIGEST: its serialization contains
    # NO diff bytes and no full-markdown body anywhere.
    _write_modified(tmp_path)
    result = _view(tmp_path)
    blob = result.model_dump_json()
    # Full-render-only section header — absent from the digest markdown.
    assert "### diff-from-template" not in result.markdown
    # Diff-body bytes — the unified-diff labels and the changed code line — appear
    # nowhere in the whole serialized response (not in markdown, not in a section).
    assert "--- template:" not in blob
    assert "+++ source:" not in blob
    assert "return 99" not in blob
    # The digest is still a complete, signable projection: metadata + counts +
    # render-file pointers + the next-actions footer.
    assert "view_sha:" in result.markdown
    assert "diff +1/-1" in result.markdown  # the COUNT survives; the bytes do not
    assert "## next actions" in result.markdown
    for s in result.sections:
        assert s.render_path
        assert s.render_path in result.markdown  # the pointer to the full render


def test_full_true_response_carries_the_diff_body(tmp_path: Path) -> None:
    # `full: true` restores the whole-body render for a harness that model-relays.
    _write_modified(tmp_path)
    result = _view(tmp_path, full=True)
    assert "### diff-from-template" in result.markdown
    # The actual diff bytes are present in the full render.
    assert "return 99" in result.markdown
    assert "--- template:model" in result.markdown
    # The per-section view_shas and render_paths are identical to the default —
    # `full` selects only how much of the render the RESPONSE carries.
    default = _view(tmp_path)
    assert [s.view_sha for s in result.sections] == [s.view_sha for s in default.sections]
    assert [s.render_path for s in result.sections] == [s.render_path for s in default.sections]
    assert result.view_sha == default.view_sha


def test_receipt_passthrough_greens_asserted_section(tmp_path: Path) -> None:
    _write(tmp_path)
    # Without a receipt `model` reads human_required (its assert is unproven).
    assert {s.slug: s.tier for s in _view(tmp_path).sections}["model"] == HUMAN_REQUIRED
    # A receipt marking the section error-free greens its assertions → auto_cleared
    # (inherited + no flags + green).
    result = _view(tmp_path, receipt={"model": {"output_sha": "abc", "error": False}})
    assert {s.slug: s.tier for s in result.sections}["model"] == AUTO_CLEARED

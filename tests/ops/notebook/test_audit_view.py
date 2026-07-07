"""Tests for ``ops/notebook/audit_view.py`` (T5).

Covers classification-by-hash, the D-attention tier matrix, ``view_sha``
determinism + edit-propagation, canonical-JSON shape, and the code-rendered
markdown projection.
"""

from __future__ import annotations

import json

from hpc_agent.ops.notebook.audit_view import (
    ADDED,
    AUTO_CLEARED,
    HUMAN_REQUIRED,
    INHERITED,
    MODIFIED,
    AuditView,
    build_audit_view,
    render_markdown,
)
from hpc_agent.state.audit_source import parse_percent_source

# ── fixtures ─────────────────────────────────────────────────────────────────

TEMPLATE = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
"""

# Same content, byte-identical sections → both inherited.
SOURCE_INHERITED = TEMPLATE

# `setup` unchanged; `model` body edited → modified. Plus an extra `analysis`
# section absent from the template → added.
SOURCE_MIXED = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 99

# %%
# hpc-audit-section: analysis
print("done")
"""

SOURCE_WITH_ASSERTIONS = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42, "sanity"
"""


def _mods(source_text: str, template_text: str = TEMPLATE):
    return parse_percent_source(source_text), parse_percent_source(template_text)


def _section(view: AuditView, slug: str):
    return next(s for s in view.sections if s.slug == slug)


# ── classification by hash ───────────────────────────────────────────────────


def test_inherited_when_section_hash_equals_template() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    view = build_audit_view(src, tmpl, [])
    setup = _section(view, "setup")
    assert setup.classification == INHERITED
    assert setup.diff == ()  # empty diff ⇔ inherited by construction
    assert setup.section_sha == setup.template_section_sha


def test_added_when_slug_absent_from_template() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    analysis = _section(view, "analysis")
    assert analysis.classification == ADDED
    assert analysis.template_section_sha is None
    assert analysis.diff  # diffed against nothing → nonempty


def test_modified_when_both_exist_and_hashes_differ() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    model = _section(view, "model")
    assert model.classification == MODIFIED
    assert model.template_section_sha is not None
    assert model.section_sha != model.template_section_sha
    assert model.diff  # nonempty unified diff


# ── tier matrix (D-attention) ────────────────────────────────────────────────


def test_tier_clean_no_assert_auto_cleared() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    view = build_audit_view(src, tmpl, [])
    # No diff, no flags, no assertions → both sections auto-cleared.
    assert _section(view, "setup").tier == AUTO_CLEARED
    assert _section(view, "model").tier == AUTO_CLEARED


def test_tier_nonempty_diff_human_required() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    assert _section(view, "model").tier == HUMAN_REQUIRED  # modified
    assert _section(view, "analysis").tier == HUMAN_REQUIRED  # added


def test_tier_lint_flag_forces_human_required() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    findings = [{"slug": "setup", "rule": "executes-live", "detail": "computed path"}]
    view = build_audit_view(src, tmpl, findings)
    setup = _section(view, "setup")
    assert setup.tier == HUMAN_REQUIRED
    assert len(setup.lint_flags) == 1
    # A sibling with no flag still auto-clears.
    assert _section(view, "model").tier == AUTO_CLEARED


def test_tier_assertions_without_receipt_human_required() -> None:
    # `setup` is inherited & clean; `model` inherits template text? No — the
    # source adds an assertion, so `model` is modified. Use a template that also
    # carries the assertion so `model` stays inherited but has an assertion.
    template_with_assert = SOURCE_WITH_ASSERTIONS
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, template_with_assert)
    view = build_audit_view(src, tmpl, [])
    model = _section(view, "model")
    assert model.classification == INHERITED
    assert model.assertions  # has a declared assertion
    # Inherited + no flags BUT an unverified assertion → not green → human.
    assert model.tier == HUMAN_REQUIRED


def test_tier_assertions_with_green_receipt_auto_cleared() -> None:
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    receipt = {"model": {"output_sha": "abc", "error": False}}
    view = build_audit_view(src, tmpl, [], receipt=receipt)
    model = _section(view, "model")
    assert model.assertions
    assert model.tier == AUTO_CLEARED


def test_tier_receipt_error_true_human_required() -> None:
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    receipt = {"model": {"output_sha": "abc", "error": True}}
    view = build_audit_view(src, tmpl, [], receipt=receipt)
    assert _section(view, "model").tier == HUMAN_REQUIRED


# ── sha-freshness of journaled receipt entries (T10) ──────────────────────────


def test_journaled_receipt_greens_only_when_section_sha_matches() -> None:
    # A JOURNALED receipt entry carries a section_sha; it greens only when that
    # sha equals the section's current sha (fresh). A stale sha greens nothing.
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    model_sha = _section(build_audit_view(src, tmpl, []), "model").section_sha

    fresh = {"model": {"output_sha": "abc", "error": False, "section_sha": model_sha}}
    assert _section(build_audit_view(src, tmpl, [], receipt=fresh), "model").tier == AUTO_CLEARED

    stale = {"model": {"output_sha": "abc", "error": False, "section_sha": "0" * 64}}
    assert _section(build_audit_view(src, tmpl, [], receipt=stale), "model").tier == HUMAN_REQUIRED


def test_inline_receipt_without_section_sha_keeps_v1_behavior() -> None:
    # An INLINE preview entry (no section_sha) greens on error==False alone —
    # the read-only VIEW path, which journals nothing.
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    inline = {"model": {"output_sha": "abc", "error": False}}
    assert _section(build_audit_view(src, tmpl, [], receipt=inline), "model").tier == AUTO_CLEARED


# ── attention_order (T12) ─────────────────────────────────────────────────────


def test_attention_order_default_is_source_order() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    assert [s.slug for s in view.sections] == ["setup", "model", "analysis"]


def test_attention_order_reorders_and_moves_view_sha() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    base = build_audit_view(src, tmpl, [])
    reordered = build_audit_view(src, tmpl, [], attention_order=["analysis", "setup", "model"])
    assert [s.slug for s in reordered.sections] == ["analysis", "setup", "model"]
    # It changes what the human saw → the module roll-up moves.
    assert reordered.view_sha != base.view_sha
    # Per-section view_shas are unaffected (only the presentation order changed).
    by_slug_base = {s.slug: s.view_sha for s in base.sections}
    for s in reordered.sections:
        assert s.view_sha == by_slug_base[s.slug]
    # The markdown follows the presented order.
    md = render_markdown(reordered)
    assert md.index("section: analysis") < md.index("section: setup") < md.index("section: model")


def test_attention_order_partial_list_keeps_unlisted_in_source_order() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    # Only 'model' is listed → it comes first; setup + analysis keep source order.
    view = build_audit_view(src, tmpl, [], attention_order=["model"])
    assert [s.slug for s in view.sections] == ["model", "setup", "analysis"]


def test_attention_order_unknown_slug_ignored() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(
        src, tmpl, [], attention_order=["nonexistent", "analysis", "also-missing"]
    )
    # Unknown slugs are ignored; 'analysis' leads, the rest keep source order.
    assert [s.slug for s in view.sections] == ["analysis", "setup", "model"]


def test_attention_order_same_as_source_leaves_view_sha_unchanged() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    base = build_audit_view(src, tmpl, [])
    same = build_audit_view(src, tmpl, [], attention_order=["setup", "model", "analysis"])
    # An order that reproduces source order is what the human saw already → same sha.
    assert same.view_sha == base.view_sha


# ── assertion table ──────────────────────────────────────────────────────────


def test_assertion_table_static_no_execution() -> None:
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    view = build_audit_view(src, tmpl, [])
    model = _section(view, "model")
    (a,) = model.assertions
    assert a.test == "train() == 42"
    assert a.msg == "'sanity'"
    assert isinstance(a.lineno, int)


# ── view_sha determinism + edit propagation ──────────────────────────────────


def test_view_sha_deterministic_same_inputs_twice() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    v1 = build_audit_view(src, tmpl, [])
    v2 = build_audit_view(parse_percent_source(SOURCE_MIXED), parse_percent_source(TEMPLATE), [])
    assert v1.view_sha == v2.view_sha
    for s1, s2 in zip(v1.sections, v2.sections, strict=True):
        assert s1.view_sha == s2.view_sha


def test_one_section_edit_moves_that_section_and_module_sha() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    base = build_audit_view(src, tmpl, [])
    edited_src = SOURCE_INHERITED.replace("return 42", "return 7")
    edited = build_audit_view(parse_percent_source(edited_src), tmpl, [])

    base_setup = _section(base, "setup")
    edit_setup = _section(edited, "setup")
    base_model = _section(base, "model")
    edit_model = _section(edited, "model")

    # The untouched section's view_sha is stable; the edited one moves.
    assert base_setup.view_sha == edit_setup.view_sha
    assert base_model.view_sha != edit_model.view_sha
    # The module roll-up moves too.
    assert base.view_sha != edited.view_sha


def test_receipt_flip_moves_section_view_sha() -> None:
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    without = build_audit_view(src, tmpl, [])
    green = build_audit_view(src, tmpl, [], receipt={"model": {"error": False}})
    assert _section(without, "model").view_sha != _section(green, "model").view_sha
    assert without.view_sha != green.view_sha


# ── canonical JSON shape ─────────────────────────────────────────────────────


def test_canonical_payload_has_sorted_keys_and_no_whitespace() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    serialized = json.dumps(
        _section(view, "model").payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # The payload round-trips through canonical JSON unchanged (already native).
    assert json.loads(serialized) == json.loads(json.dumps(_section(view, "model").payload))
    # Compact — no ", " / ": " separators.
    assert ", " not in serialized
    assert '": ' not in serialized


def test_lint_flags_embedded_opaquely() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    finding = {"slug": "setup", "rule": "linked-sources", "opaque_extra": {"nested": [1, 2]}}
    view = build_audit_view(src, tmpl, [finding])
    setup = _section(view, "setup")
    assert setup.payload["lint_flags"] == [finding]


def test_module_scoped_finding_attributed_to_no_section() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    # No slug key → module-scoped → attaches nowhere, flips no tier.
    view = build_audit_view(src, tmpl, [{"rule": "structural", "detail": "x"}])
    for s in view.sections:
        assert s.lint_flags == ()
        assert s.tier == AUTO_CLEARED


def test_dropped_template_slug_surfaced() -> None:
    source_missing_model = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1
"""
    src, tmpl = _mods(source_missing_model)
    view = build_audit_view(src, tmpl, [])
    assert view.dropped_template_slugs == ("model",)


# ── markdown render ──────────────────────────────────────────────────────────


def test_render_markdown_contains_slug_tier_and_markers() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    md = render_markdown(view)
    assert "## section: model" in md
    assert f"[tier: {HUMAN_REQUIRED}]" in md
    assert "### diff-from-template" in md
    assert "classification: modified" in md
    assert view.view_sha in md
    # Inherited section renders the no-change note, not a diff block.
    assert "(no changes — inherited from template)" in md


def test_render_markdown_deterministic() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    v1 = build_audit_view(src, tmpl, [])
    v2 = build_audit_view(parse_percent_source(SOURCE_MIXED), parse_percent_source(TEMPLATE), [])
    assert render_markdown(v1) == render_markdown(v2)


def test_render_markdown_shows_assertions_and_flags() -> None:
    src, tmpl = _mods(SOURCE_WITH_ASSERTIONS, SOURCE_WITH_ASSERTIONS)
    view = build_audit_view(src, tmpl, [{"slug": "model", "rule": "executes-live"}])
    md = render_markdown(view)
    assert "### assertions" in md
    assert "train() == 42" in md
    assert "### lint flags" in md
    assert "executes-live" in md

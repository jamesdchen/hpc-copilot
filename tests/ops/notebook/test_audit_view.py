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


# ── the next-actions footer (run-#10 hyper-palatable sign-off amendment) ─────


def test_footer_lists_pending_sections_with_copy_ready_utterance() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    md = render_markdown(view)
    pending = [s.slug for s in view.sections if s.tier == "human_required"]
    assert pending, "fixture must carry at least one human_required section"
    assert "## next actions" in md
    footer = md.split("## next actions", 1)[1]
    for slug in pending:
        assert slug in footer
    assert f'"sign {" ".join(pending)}"' in footer
    assert "token-exactly" in footer  # the gate's ACTUAL bar, code-stated


def test_footer_all_auto_cleared_states_no_pending() -> None:
    src, tmpl = _mods(SOURCE_INHERITED)
    view = build_audit_view(src, tmpl, [])
    md = render_markdown(view)
    assert "## next actions" in md
    assert "no sections await sign-off" in md


def test_footer_never_enters_view_sha_payload() -> None:
    # The footer is pure presentation: nothing footer-shaped may enter the
    # hashed payload, so adding/changing it can never move a view_sha.
    src, tmpl = _mods(SOURCE_MIXED)
    view = build_audit_view(src, tmpl, [])
    payload_json = json.dumps(view.payload, sort_keys=True)
    assert "next actions" not in payload_json
    for s in view.sections:
        assert "next actions" not in json.dumps(s.payload, sort_keys=True)


# ── the section join — runtime-evidence summary (Amendment 16 B3-LEAN) ────────

from hpc_agent.state.data_trace import make_record  # noqa: E402


def _model_sha() -> str:
    """The current ``model`` section_sha (a HUMAN_REQUIRED section in SOURCE_MIXED)."""
    src, tmpl = _mods(SOURCE_MIXED)
    sha: str = _section(build_audit_view(src, tmpl, []), "model").section_sha
    return sha


def _rec(
    stage: str,
    seq: int,
    atoms: dict,
    *,
    section: str,
    sha: str | None,
    source: str = "runner",
) -> dict:
    return make_record(
        stage=stage, seq=seq, atoms=atoms, section=section, section_sha=sha, source=source
    )


def _rows(n: int) -> dict:
    return {"row_count": {"rows": n, "dropped": 0}}


def test_section_join_changed_observable_renders_first_to_last() -> None:
    src, tmpl = _mods(SOURCE_MIXED)
    sha = _model_sha()
    # One observable ('df') measured twice across the 'model' section, value MOVED.
    traces = [
        _rec("df", 0, _rows(10), section="model", sha=sha),
        _rec("df", 1, _rows(7), section="model", sha=sha),
    ]
    view = build_audit_view(src, tmpl, [], audit_traces=traces)
    model = _section(view, "model")
    assert model.tier == HUMAN_REQUIRED
    summary = model.trace_summary
    assert summary is not None and summary["stale"] is False
    assert summary["changed"] == [{"observable": "df", "first": _rows(10), "last": _rows(7)}]
    assert isinstance(summary["section_records_sha"], str) and summary["section_records_sha"]
    # It rode into the hashed payload (signed evidence) and the markdown.
    assert model.payload["trace_summary"] == summary
    md = render_markdown(view)
    assert "### runtime evidence (latest execution)" in md
    assert "df:" in md


def test_section_join_unchanged_observable_renders_nothing() -> None:
    # MANDATORY guard: an observable whose value did NOT move contributes no line;
    # with nothing changed the summary is absent and the payload is byte-identical
    # to a trace-free view (present-only convention).
    src, tmpl = _mods(SOURCE_MIXED)
    sha = _model_sha()
    traces = [
        _rec("df", 0, _rows(10), section="model", sha=sha),
        _rec("df", 1, _rows(10), section="model", sha=sha),  # same value → unchanged
    ]
    with_trace = build_audit_view(src, tmpl, [], audit_traces=traces)
    without = build_audit_view(src, tmpl, [])
    model_with = _section(with_trace, "model")
    assert model_with.trace_summary is None
    assert "trace_summary" not in model_with.payload
    # Byte-identical: unchanged evidence moves no view_sha.
    assert model_with.view_sha == _section(without, "model").view_sha
    assert with_trace.view_sha == without.view_sha
    assert "runtime evidence" not in render_markdown(with_trace)


def test_section_join_stale_wrong_sha_is_elided() -> None:
    # MANDATORY guard: a trace stamped with a DIFFERENT section_sha is stale — the
    # summary is elided with a disclosed marker, never rendered as if current.
    src, tmpl = _mods(SOURCE_MIXED)
    traces = [
        _rec("df", 0, _rows(10), section="model", sha="0" * 64),
        _rec("df", 1, _rows(7), section="model", sha="0" * 64),
    ]
    view = build_audit_view(src, tmpl, [], audit_traces=traces)
    model = _section(view, "model")
    assert model.trace_summary == {"stale": True}
    md = render_markdown(view)
    assert "STALE" in md
    assert "elided" in md
    # No values leak into a stale render.
    assert "-> " not in md.split("### runtime evidence", 1)[1].split("##", 1)[0]


def test_section_join_missing_sha_is_stale_elided() -> None:
    # A runner that does NOT stamp section_sha degrades honestly: the reader treats
    # an unbound record as stale (never rendered as current).
    src, tmpl = _mods(SOURCE_MIXED)
    traces = [
        _rec("df", 0, _rows(10), section="model", sha=None),
        _rec("df", 1, _rows(7), section="model", sha=None),
    ]
    model = _section(build_audit_view(src, tmpl, [], audit_traces=traces), "model")
    assert model.trace_summary == {"stale": True}


def test_section_join_only_human_required_sections() -> None:
    # An auto_cleared section carries NO summary even when a fresh trace exists —
    # runtime evidence rides only the sections that route human attention.
    src, tmpl = _mods(SOURCE_INHERITED)  # both sections auto_cleared
    setup_sha = _section(build_audit_view(src, tmpl, []), "setup").section_sha
    traces = [
        _rec("x", 0, _rows(1), section="setup", sha=setup_sha),
        _rec("x", 1, _rows(9), section="setup", sha=setup_sha),
    ]
    view = build_audit_view(src, tmpl, [], audit_traces=traces)
    setup = _section(view, "setup")
    assert setup.tier == AUTO_CLEARED
    assert setup.trace_summary is None
    assert "trace_summary" not in setup.payload


def test_section_join_ignores_non_runner_tier() -> None:
    # A10: sign-off consumes RUNNER-tier records only. Draft/engine records for the
    # section are ignored (they never enter the signed evidence).
    src, tmpl = _mods(SOURCE_MIXED)
    sha = _model_sha()
    traces = [
        _rec("df", 0, _rows(10), section="model", sha=sha, source="draft"),
        _rec("df", 1, _rows(7), section="model", sha=sha, source="engine"),
    ]
    model = _section(build_audit_view(src, tmpl, [], audit_traces=traces), "model")
    assert model.trace_summary is None


def test_section_join_latest_execution_only() -> None:
    # Two executions (seq resets to 0). Only the LATEST execution's values render.
    src, tmpl = _mods(SOURCE_MIXED)
    sha = _model_sha()
    traces = [
        # execution 1
        _rec("df", 0, _rows(100), section="model", sha=sha),
        _rec("df", 1, _rows(50), section="model", sha=sha),
        # execution 2 (seq restarts) — the latest
        _rec("df", 0, _rows(8), section="model", sha=sha),
        _rec("df", 1, _rows(3), section="model", sha=sha),
    ]
    summary = _section(build_audit_view(src, tmpl, [], audit_traces=traces), "model").trace_summary
    assert summary is not None
    assert summary["changed"] == [{"observable": "df", "first": _rows(8), "last": _rows(3)}]


def test_section_join_binds_view_sha() -> None:
    # A changed-observable summary is signed evidence: it moves the section view_sha.
    src, tmpl = _mods(SOURCE_MIXED)
    sha = _model_sha()
    traces = [
        _rec("df", 0, _rows(10), section="model", sha=sha),
        _rec("df", 1, _rows(7), section="model", sha=sha),
    ]
    base = _section(build_audit_view(src, tmpl, []), "model")
    joined = _section(build_audit_view(src, tmpl, [], audit_traces=traces), "model")
    assert joined.view_sha != base.view_sha

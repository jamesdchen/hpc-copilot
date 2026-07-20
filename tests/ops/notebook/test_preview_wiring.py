"""Preview wiring into the audit sign-off chain (lane/notebook-preview-wiring).

The four user rulings (2026-07-20), mechanized:

* **R1** — a SAMPLED dry-run receipt MAY green the assertions leg, but only as a
  DISTINCT NAMED BASIS: the view records the evidence basis (``sampled`` vs
  ``full``) as a first-class field. Full-evidence consumers — ``notebook-auto-clear``
  and the sign-off-readiness reduction — accept ONLY ``full``.
* **R2** — the preview state is a FIRST-CLASS DISCLOSURE BLOCK inside the
  per-section audit-view render, adjacent to the assertions content; absent →
  an honest "no preview" line, never a silent skip.
* **R3** — the disclosure block is PRESENTATION-ONLY, OUTSIDE the hashed span:
  a fresh preview NEVER changes ``view_sha`` (else re-preview would revoke
  pending sign-offs — a trust regression).
* **R4** — the skill OFFERS re-preview at two seats (tested at the skill layer;
  the mechanized surface is the ``notebook-dry-run`` offer this wiring discloses).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearSpec
from hpc_agent._wire.actions.notebook_dry_run import NotebookDryRunSpec
from hpc_agent._wire.actions.notebook_record_receipt import NotebookRecordReceiptSpec
from hpc_agent._wire.queries.notebook_status import NotebookStatusSpec
from hpc_agent.ops.notebook.audit_view import (
    AUTO_CLEARED,
    HUMAN_REQUIRED,
    build_audit_view,
    render_markdown,
)
from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear
from hpc_agent.ops.notebook.canonical import build_canonical_view, read_recorded_config
from hpc_agent.ops.notebook.dry_run_op import notebook_dry_run
from hpc_agent.ops.notebook.record_receipt_op import notebook_record_receipt
from hpc_agent.ops.notebook.render_store import render_bytes
from hpc_agent.ops.notebook.status_op import notebook_status
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.ops.notebook.audit_view import AuditView, SectionView

_AUDIT = "demo-audit"

# One assertion-bearing, template-inherited section (source == template).
_ASSERTED = """\
# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42
"""

# Same shape, but the sampled preview's assert FAILS (error receipt).
_FAILING = """\
# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 43
"""


def _write_audit(tmp_path: Path, source: str = _ASSERTED) -> None:
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")
    block = {"source": "source.py", "template": "template.py", "audit_id": _AUDIT}
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": block}), encoding="utf-8"
    )


def _view(tmp_path: Path) -> AuditView:
    """The CANONICAL view (the one definition the T8 gate + auto-clear share)."""
    return build_canonical_view(
        tmp_path,
        audit_id=_AUDIT,
        source_relpath="source.py",
        template_relpath="template.py",
        cfg=read_recorded_config(tmp_path, _AUDIT),
    )


def _model(view: AuditView) -> SectionView:
    return next(sv for sv in view.sections if sv.slug == "model")


def _clear(tmp_path: Path):
    return notebook_auto_clear(
        experiment_dir=tmp_path,
        spec=NotebookAutoClearSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )


def _dry_run(tmp_path: Path) -> None:
    result = notebook_dry_run(
        experiment_dir=tmp_path,
        spec=NotebookDryRunSpec(source="source.py", audit_id=_AUDIT, sample_n=3),
    )
    assert result.receipts_recorded == ["model"]


# ── t1: a sampled receipt greens the assertions leg with basis=sampled (R1) ──


def test_t1_sampled_preview_greens_assertions_leg_with_distinct_sampled_basis(
    tmp_path: Path,
) -> None:
    _write_audit(tmp_path)
    _dry_run(tmp_path)  # journals a SAMPLED receipt for the asserting section

    sv = _model(_view(tmp_path))
    # R1: the assertions leg IS green on the sampled basis (a fast bounded signal) —
    # recorded as a DISTINCT NAMED BASIS, never conflated with full evidence.
    assert sv.assertions_basis == nb.EXECUTION_SCOPE_SAMPLED
    # But the TIER does not move: auto-clear readiness accepts ONLY full evidence.
    assert sv.tier == HUMAN_REQUIRED
    # The preview entry is carried first-class on the section view.
    assert sv.preview_receipt is not None
    assert sv.preview_receipt["error"] is False
    assert sv.preview_receipt["fresh"] is True
    assert sv.preview_receipt["basis"] == nb.EXECUTION_SCOPE_SAMPLED
    assert sv.preview_receipt["output_sha"]
    # R3 at the data level: neither field enters the hashed payload.
    assert "assertions_basis" not in sv.payload
    assert "preview_receipt" not in sv.payload


# ── t2: auto-clear REFUSES a sampled-greened section (requires full) ─────────


def test_t2_auto_clear_refuses_sampled_greened_section_until_full(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    _dry_run(tmp_path)

    # The view shows the leg green on the sampled basis...
    assert _model(_view(tmp_path)).assertions_basis == nb.EXECUTION_SCOPE_SAMPLED
    # ...yet auto-clear refuses it (full-evidence consumer, R1).
    cleared = _clear(tmp_path)
    assert cleared.cleared == []
    assert [(s.section, s.reason) for s in cleared.skipped] == [("model", "human_required")]

    # A FULL receipt (the plugin's execute path, here via the record verb) clears.
    notebook_record_receipt(
        experiment_dir=tmp_path,
        spec=NotebookRecordReceiptSpec.model_validate(
            {
                "audit_id": _AUDIT,
                "source": "source.py",
                "entries": {"model": {"output_sha": "out-full", "error": False}},
            }
        ),
    )
    assert [c.section for c in _clear(tmp_path).cleared] == ["model"]


# ── t3: a full receipt is byte-identical in behavior to today ────────────────


def test_t3_full_receipt_behavior_byte_identical(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    sha = next(s.section_sha for s in source.sections if s.slug == "model")

    # A FULL receipt journals byte-identically to a pre-dry-run record.
    record = nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="model",
        section_sha=sha,
        recompute=sha,
        output_sha="out-full-1",
        error=False,
    )
    assert "execution_scope" not in record["resolved"]

    # The full receipt greens the leg with basis=full and clears the section.
    sv = _model(_view(tmp_path))
    assert sv.assertions_basis == nb.EXECUTION_SCOPE_FULL
    assert sv.tier == AUTO_CLEARED
    view_sha_full_only = _view(tmp_path).view_sha
    assert [c.section for c in _clear(tmp_path).cleared] == ["model"]

    # A later SAMPLED preview beside the full receipt moves NOTHING in the hashed
    # view (full behavior is byte-identical whether or not a preview exists).
    _dry_run(tmp_path)
    after = _view(tmp_path)
    assert _model(after).assertions_basis == nb.EXECUTION_SCOPE_FULL  # full wins precedence
    assert _model(after).tier == AUTO_CLEARED
    assert _model(after).view_sha == sv.view_sha
    assert after.view_sha == view_sha_full_only
    assert dict(_model(after).payload) == dict(sv.payload)


# ── t4: the disclosure block renders with a preview / an honest line without ─


def test_t4_disclosure_block_present_and_honest_without_preview(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    md = render_markdown(_view(tmp_path))
    # R2: the block is ALWAYS present — never silently skipped.
    assert "### preview (sampled dry-run)" in md
    assert "no preview recorded" in md


def test_t4_disclosure_block_carries_preview_facts_when_present(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    _dry_run(tmp_path)
    view = _view(tmp_path)
    sv = _model(view)
    md = render_markdown(view)

    assert "### preview (sampled dry-run)" in md
    assert "assertions basis: sampled" in md
    assert "- output_sha: " in md
    assert "- error: False" in md
    assert "- recorded_at: " in md
    assert "- fresh: True" in md
    # R2 adjacency: the block sits BETWEEN the assertions and the lint flags.
    idx_assertions = md.index("### assertions")
    idx_preview = md.index("### preview (sampled dry-run)")
    idx_lint = md.index("### lint flags")
    assert idx_assertions < idx_preview < idx_lint
    # The per-section trusted-display render carries the SAME block (R2's seat).
    section_md = render_bytes(audit_id=_AUDIT, view=sv)
    assert "### preview (sampled dry-run)" in section_md
    assert "assertions basis: sampled" in section_md


def test_t4_disclosure_is_parser_safe_for_the_digest_consumers(tmp_path: Path) -> None:
    """No disclosure line starts with the assertion-row prefix ``- L`` (the body
    digest collects assertion rows by that prefix) and the static assertion rows
    count is unchanged by the block."""
    _write_audit(tmp_path)
    _dry_run(tmp_path)
    md = render_markdown(_view(tmp_path))
    block = md.split("### preview (sampled dry-run)", 1)[1].split("###", 1)[0]
    assert not any(line.startswith("- L") for line in block.splitlines())
    # Exactly one static assertion row in the assertions block, as before wiring.
    assertions_block = md.split("### assertions", 1)[1].split("### preview", 1)[0]
    assert [line for line in assertions_block.splitlines() if line.startswith("- L")] == [
        "- L5: train() == 42"
    ]


def test_t4_errored_preview_discloses_but_does_not_green_the_basis(tmp_path: Path) -> None:
    _write_audit(tmp_path, _FAILING)  # the sampled preview's assert FAILS
    result = notebook_dry_run(
        experiment_dir=tmp_path,
        spec=NotebookDryRunSpec(source="source.py", audit_id=_AUDIT, sample_n=3),
    )
    assert result.receipts_recorded == ["model"]

    sv = _model(_view(tmp_path))
    # Unverified-failed ≠ green: the basis stays none, the tier human_required.
    assert sv.assertions_basis is None
    assert sv.tier == HUMAN_REQUIRED
    md = render_markdown(_view(tmp_path))
    assert "assertions basis: none" in md
    assert "- error: True" in md


def test_t4_stale_preview_disclosed_not_greened(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    _dry_run(tmp_path)
    # The section drifts after the preview (a redraft) — the preview is now STALE.
    drifted = _ASSERTED.replace("return 42", "return 44").replace("== 42", "== 44")
    (tmp_path / "source.py").write_text(drifted, encoding="utf-8")
    (tmp_path / "template.py").write_text(drifted, encoding="utf-8")

    sv = _model(_view(tmp_path))
    assert sv.assertions_basis is None  # stale evidence greens nothing
    assert sv.preview_receipt is not None and sv.preview_receipt["fresh"] is False
    md = render_markdown(_view(tmp_path))
    assert "- fresh: False" in md
    assert "assertions basis: none" in md


# ── t5: view_sha IDENTICAL with/without the preview block (the R3 keystone) ──


def test_t5_view_sha_identical_with_and_without_preview_block(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    before = _view(tmp_path)  # no preview journaled
    before_sha = {sv.slug: sv.view_sha for sv in before.sections}
    before_payload = dict(_model(before).payload)

    _dry_run(tmp_path)  # journal a fresh sampled preview
    after = _view(tmp_path)

    # R3 KEYSTONE: per-section AND module view_sha are IDENTICAL.
    assert {sv.slug: sv.view_sha for sv in after.sections} == before_sha
    assert after.view_sha == before.view_sha
    assert dict(_model(after).payload) == before_payload
    # ...yet the rendered bytes DID change (the disclosure block's content moved
    # from the honest-absent line to the preview facts) — proof the block rides
    # OUTSIDE the hashed span.
    assert render_bytes(audit_id=_AUDIT, view=_model(before)) != render_bytes(
        audit_id=_AUDIT, view=_model(after)
    )


def test_t5_builder_level_preview_never_moves_view_sha(tmp_path: Path) -> None:
    """The most direct pin: the SAME builder inputs with and without a
    ``preview_receipt`` produce identical per-section + module view_shas."""
    _write_audit(tmp_path)
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    template = parse_percent_source((tmp_path / "template.py").read_text(encoding="utf-8"))
    sha = next(s.section_sha for s in source.sections if s.slug == "model")
    preview_entry = {
        "output_sha": "out-x",
        "error": False,
        "section_sha": sha,
        "fresh": True,
        "ts": "2026-07-20T00:00:00Z",
        "basis": nb.EXECUTION_SCOPE_SAMPLED,
    }

    plain = build_audit_view(source, template, [])
    previewed = build_audit_view(source, template, [], preview_receipt={"model": preview_entry})

    assert [sv.view_sha for sv in previewed.sections] == [sv.view_sha for sv in plain.sections]
    assert previewed.view_sha == plain.view_sha
    assert [dict(sv.payload) for sv in previewed.sections] == [
        dict(sv.payload) for sv in plain.sections
    ]
    # The basis + disclosure ARE carried (presentation), just not hashed.
    model = next(sv for sv in previewed.sections if sv.slug == "model")
    assert model.assertions_basis == nb.EXECUTION_SCOPE_SAMPLED
    assert "### preview (sampled dry-run)" in render_markdown(previewed)


# ── t6: sign-off readiness never counts sampled as full ──────────────────────


def test_t6_signoff_readiness_never_counts_sampled_as_full(tmp_path: Path) -> None:
    _write_audit(tmp_path)
    _dry_run(tmp_path)

    # The graduation-gate predicate is False: the sampled receipt never enters
    # the T6 reduction (receipts are not attestations of passage).
    status = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec(audit_id=_AUDIT, source="source.py", template="template.py"),
    )
    assert status.passed is False
    model_status = next(s for s in status.sections if s.slug == "model")
    assert model_status.status == nb.UNSIGNED

    # The T6 vocabulary itself: the newest-valid reduction over the journal is
    # still UNSIGNED (a sampled receipt is invisible to audit_section).
    sha = model_status.current_section_sha
    records = nb.read_decisions(tmp_path, "notebook", _AUDIT)
    assert nb.audit_section(records, "model", sha).status == nb.UNSIGNED

    # Full evidence graduates the module.
    notebook_record_receipt(
        experiment_dir=tmp_path,
        spec=NotebookRecordReceiptSpec.model_validate(
            {
                "audit_id": _AUDIT,
                "source": "source.py",
                "entries": {"model": {"output_sha": "out-full", "error": False}},
            }
        ),
    )
    _clear(tmp_path)
    status = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec(audit_id=_AUDIT, source="source.py", template="template.py"),
    )
    assert status.passed is True

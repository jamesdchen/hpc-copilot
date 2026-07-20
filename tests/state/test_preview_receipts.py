"""The SAMPLED preview-receipt read seam (lane/notebook-preview-wiring).

Two pins over the ONE notebook journal's render-receipt records:

* **t7 — the full-evidence chokepoint is byte-unchanged.** ``read_render_receipts``
  (the ONLY reader feeding the D-attention tier / auto-clear / graduation path)
  keeps its exact pre-wiring contract: SAMPLED receipts are filtered out before
  newest-valid selection, a later sampled run NEVER revokes an earlier FULL
  receipt, and every entry keeps the exact four-key shape ``{output_sha, error,
  section_sha, fresh}`` — no preview field leaks into the clearing reader.
* **R1 — the DISTINCT preview seam beside it.** ``read_preview_receipts`` reads
  back exactly the SAMPLED class the full seam filters out, as first-class
  DISCLOSURE provenance the audit view renders: ``{output_sha, error,
  section_sha, fresh, ts, basis}`` with ``basis`` always ``"sampled"`` and ``ts``
  the journal record's own timestamp (deterministic — never a wall-clock read).
  It is a WEAKER basis by construction: nothing downstream greens a tier leg
  through this seam (the tier/auto-clear readers accept only the full seam).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT = "demo-audit"

# One assertion-bearing, template-inherited section (source == template).
_ASSERTED = """\
# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42
"""

# A drifted rewrite of the same section (new sha — the freshness anchor moves).
_DRIFTED = """\
# %%
# hpc-audit-section: model
def train():
    return 43
assert train() == 43
"""


def _write_audit(tmp_path: Path) -> str:
    """Write the source/template/interview trio; return the model section sha."""
    (tmp_path / "source.py").write_text(_ASSERTED, encoding="utf-8")
    (tmp_path / "template.py").write_text(_ASSERTED, encoding="utf-8")
    block = {"source": "source.py", "template": "template.py", "audit_id": _AUDIT}
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": block}), encoding="utf-8"
    )
    return _model_sha(tmp_path)


def _model_sha(tmp_path: Path) -> str:
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    return next(s.section_sha for s in source.sections if s.slug == "model")


def _current_shas(tmp_path: Path) -> dict[str, str]:
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    return {s.slug: s.section_sha for s in source.sections}


def _record_sampled(tmp_path: Path, sha: str, output_sha: str, *, error: bool = False) -> None:
    nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="model",
        section_sha=sha,
        recompute=sha,
        output_sha=output_sha,
        error=error,
        execution_scope=nb.EXECUTION_SCOPE_SAMPLED,
    )


def _record_full(tmp_path: Path, sha: str, output_sha: str, *, error: bool = False) -> dict:
    return nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="model",
        section_sha=sha,
        recompute=sha,
        output_sha=output_sha,
        error=error,
    )


# ── t7: the full-evidence chokepoint is byte-unchanged ───────────────────────


def test_t7_full_seam_filters_sampled_and_keeps_exact_entry_shape(tmp_path: Path) -> None:
    """A FULL receipt reads back with the EXACT pre-wiring entry shape; a sampled
    receipt journaled beside it is invisible to the clearing reader (t7)."""
    sha = _write_audit(tmp_path)
    _record_full(tmp_path, sha, "out-full-1")
    _record_sampled(tmp_path, sha, "out-sampled-1")

    result = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path))
    assert set(result) == {"model"}
    entry = result["model"]
    # The EXACT four-key shape — no preview field (ts / basis) leaks into the
    # full-evidence seam.
    assert set(entry) == {"output_sha", "error", "section_sha", "fresh"}
    assert entry["output_sha"] == "out-full-1"
    assert entry["error"] is False
    assert entry["section_sha"] == sha
    assert entry["fresh"] is True


def test_t7_full_seam_reads_empty_on_a_sampled_only_journal(tmp_path: Path) -> None:
    sha = _write_audit(tmp_path)
    _record_sampled(tmp_path, sha, "out-sampled-1")
    assert nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path)) == {}


def test_t7_a_later_sampled_run_never_revokes_an_earlier_full_receipt(tmp_path: Path) -> None:
    """Newest-valid FULL wins: journal full, THEN sampled — the full receipt still
    reads back (the sampled record is filtered before newest-valid selection, so
    it can neither supersede nor stale the full one)."""
    sha = _write_audit(tmp_path)
    _record_full(tmp_path, sha, "out-full-1")
    _record_sampled(tmp_path, sha, "out-sampled-LATER")

    entry = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path))["model"]
    assert entry["output_sha"] == "out-full-1"
    assert entry["fresh"] is True


def test_full_receipt_record_carries_no_execution_scope_key(tmp_path: Path) -> None:
    """A FULL receipt's journaled ``resolved`` stays byte-identical to a
    pre-dry-run record (the scope field is absent, defaulting to full on read)."""
    sha = _write_audit(tmp_path)
    record = _record_full(tmp_path, sha, "out-full-1")
    assert "execution_scope" not in record["resolved"]


# ── R1: the distinct preview seam reads the sampled class back ───────────────


def test_preview_seam_returns_newest_sampled_with_basis_and_ts(tmp_path: Path) -> None:
    sha = _write_audit(tmp_path)
    _record_sampled(tmp_path, sha, "out-sampled-1")
    _record_sampled(tmp_path, sha, "out-sampled-2")  # newer wins (append order)

    result = nb.read_preview_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path))
    assert set(result) == {"model"}
    entry = result["model"]
    assert set(entry) == {"output_sha", "error", "section_sha", "fresh", "ts", "basis"}
    assert entry["output_sha"] == "out-sampled-2"
    assert entry["error"] is False
    assert entry["section_sha"] == sha
    assert entry["fresh"] is True
    # The DISTINCT NAMED BASIS (R1): every preview entry is labeled sampled.
    assert entry["basis"] == nb.EXECUTION_SCOPE_SAMPLED
    # The journal record's own timestamp (deterministic disclosure, never wall-clock).
    assert isinstance(entry["ts"], str) and entry["ts"]


def test_preview_seam_ignores_full_receipts(tmp_path: Path) -> None:
    sha = _write_audit(tmp_path)
    _record_full(tmp_path, sha, "out-full-1")
    assert nb.read_preview_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path)) == {}


def test_preview_seam_reads_errored_sampled_receipts_honestly(tmp_path: Path) -> None:
    sha = _write_audit(tmp_path)
    _record_sampled(tmp_path, sha, "out-sampled-err", error=True)
    entry = nb.read_preview_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path))[
        "model"
    ]
    assert entry["error"] is True
    assert entry["fresh"] is True
    assert entry["basis"] == nb.EXECUTION_SCOPE_SAMPLED


def test_preview_seam_marks_stale_when_the_section_drifts(tmp_path: Path) -> None:
    """A preview bound at an older sha reads ``fresh=False`` once the section
    moves (stale provenance — still disclosed, greens nothing)."""
    sha = _write_audit(tmp_path)
    _record_sampled(tmp_path, sha, "out-sampled-1")
    (tmp_path / "source.py").write_text(_DRIFTED, encoding="utf-8")  # section drifts

    entry = nb.read_preview_receipts(tmp_path, _AUDIT, current_shas=_current_shas(tmp_path))[
        "model"
    ]
    assert entry["fresh"] is False
    assert entry["section_sha"] == sha  # the sha it WAS bound at
    assert entry["basis"] == nb.EXECUTION_SCOPE_SAMPLED

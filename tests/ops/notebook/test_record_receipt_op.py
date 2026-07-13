"""Direct-atom tests for the ``notebook-record-receipt`` mutate primitive (T10).

Writes a source ``.py`` under the experiment dir, calls the verb, and asserts the
receipt is journaled bound to the FRESHLY-PARSED section sha (read back fresh via
``read_render_receipts``), that unknown slugs are reported skipped (never fatal),
that a missing source is a loud ``spec_invalid``, and that a receipt recorded then
followed by an edit reads stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_record_receipt import NotebookRecordReceiptSpec
from hpc_agent.ops.notebook.record_receipt_op import notebook_record_receipt
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.actions.notebook_record_receipt import NotebookRecordReceiptResult

_AUDIT = "demo-audit"

_SOURCE = """\
# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42

# %%
# hpc-audit-section: report
print("ok")
"""


def _write(tmp_path: Path, source: str = _SOURCE) -> None:
    (tmp_path / "source.py").write_text(source, encoding="utf-8")


def _run(tmp_path: Path, entries: dict) -> NotebookRecordReceiptResult:
    return notebook_record_receipt(
        experiment_dir=tmp_path,
        spec=NotebookRecordReceiptSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "entries": entries}
        ),
    )


def _shas(tmp_path: Path) -> dict[str, str]:
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    return {s.slug: s.section_sha for s in source.sections}


def test_records_receipt_bound_to_fresh_section_sha(tmp_path: Path) -> None:
    _write(tmp_path)
    result = _run(tmp_path, {"model": {"output_sha": "out-abc", "error": False}})

    assert [r.section for r in result.recorded] == ["model"]
    assert result.skipped == []
    shas = _shas(tmp_path)
    assert result.recorded[0].section_sha == shas["model"]

    # Read it back: fresh, error-free, carrying the caller's output_sha.
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=shas)
    assert got["model"]["fresh"] is True
    assert got["model"]["error"] is False
    assert got["model"]["output_sha"] == "out-abc"


def test_unknown_slug_reported_skipped_not_fatal(tmp_path: Path) -> None:
    _write(tmp_path)
    result = _run(
        tmp_path,
        {
            "model": {"output_sha": "out-1", "error": False},
            "no-such-section": {"output_sha": "out-2", "error": False},
        },
    )
    assert [r.section for r in result.recorded] == ["model"]
    assert [(s.section, s.reason) for s in result.skipped] == [("no-such-section", "unknown-slug")]
    # The known-slug receipt was still journaled (a bad entry never strands it).
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_shas(tmp_path))
    assert "model" in got and "no-such-section" not in got


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="source"):
        _run(tmp_path, {"model": {"output_sha": "x", "error": False}})


def test_receipt_binds_current_source_then_reads_stale_after_edit(tmp_path: Path) -> None:
    _write(tmp_path)
    _run(tmp_path, {"model": {"output_sha": "out-1", "error": False}})
    assert nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_shas(tmp_path))["model"]["fresh"]

    # Edit the model section — the journaled receipt now points at the old sha.
    edited = _SOURCE.replace("return 42", "return 7").replace("train() == 42", "train() == 7")
    _write(tmp_path, edited)
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_shas(tmp_path))
    assert got["model"]["fresh"] is False


def test_error_true_receipt_is_recorded_and_read_back(tmp_path: Path) -> None:
    _write(tmp_path)
    result = _run(tmp_path, {"model": {"output_sha": "out-1", "error": True}})
    assert [r.error for r in result.recorded] == [True]
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=_shas(tmp_path))
    assert got["model"]["error"] is True
    assert got["model"]["fresh"] is True  # fresh, but error=True greens nothing downstream

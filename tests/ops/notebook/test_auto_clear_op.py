"""Direct-atom tests for the ``notebook-auto-clear`` mutate primitive.

Exercises the CODE-attestor writer end to end against the REAL journal + the REAL
lint/view recompute (never hand-forged findings): a clean inherited section
clears with an ``attestor="code"`` record that ``notebook-status`` then reads as
``auto_cleared``; a modified section and a lint-flagged section are skipped
``human_required`` and NEVER journaled (the un-fakeability fire test — a caller
cannot pass empty findings because the verb recomputes them server-side); a re-run
is an idempotent no-op; an edit re-clears at the new hash with a NEW append-only
record; a receipt greens an asserted section into a clear.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearSpec
from hpc_agent._wire.actions.notebook_record_receipt import NotebookRecordReceiptSpec
from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear
from hpc_agent.ops.notebook.record_receipt_op import notebook_record_receipt
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearResult

_AUDIT = "demo-audit"

# A clean, assertion-free, path-free section — inherited + no flags + no asserts
# → auto_cleared.
_CLEAN = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1
"""

# Same section, its body edited (sha moves) — still clean when source==template.
_CLEAN_EDITED = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 2
"""

# A section carrying a missing path literal — inherited but the executes-live rule
# flags it → human_required. The literal never exists under input_roots.
_FLAGGED = """\
# %%
# hpc-audit-section: load
import pandas as pd
df = pd.read_csv("inputs/missing.csv")
"""

# A section with a declared assertion — inherited but ungreen without a receipt
# → human_required until a receipt clears it.
_ASSERTED = """\
# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42
"""


def _write(tmp_path: Path, source: str, template: str) -> None:
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(template, encoding="utf-8")


def _write_interview(tmp_path: Path, *, input_roots: list[str] | None = None) -> None:
    """Record the audit's roots on interview.json (the ONLY place lint roots come
    from now — the mutate verb refuses caller-supplied roots, F2)."""
    block: dict[str, object] = {
        "source": "source.py",
        "template": "template.py",
        "audit_id": _AUDIT,
    }
    if input_roots is not None:
        block["input_roots"] = input_roots
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": block}), encoding="utf-8"
    )


def _run(tmp_path: Path, **overrides: object) -> NotebookAutoClearResult:
    spec: dict[str, object] = {
        "audit_id": _AUDIT,
        "source": "source.py",
        "template": "template.py",
    }
    spec.update(overrides)
    return notebook_auto_clear(
        experiment_dir=tmp_path, spec=NotebookAutoClearSpec.model_validate(spec)
    )


def _records(tmp_path: Path) -> list[dict]:
    return read_decisions(tmp_path, "notebook", _AUDIT)


def _status(tmp_path: Path, slug: str) -> str:
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    template = parse_percent_source((tmp_path / "template.py").read_text(encoding="utf-8"))
    rollup = nb.audit_module(tmp_path, _AUDIT, source=source, required_slugs=template.slugs)
    return next(s.status for s in rollup.sections if s.slug == slug)


# ── clean inherited section → cleared + code record + status auto_cleared ─────


def test_clean_inherited_section_clears_with_code_record(tmp_path: Path) -> None:
    _write(tmp_path, _CLEAN, _CLEAN)
    result = _run(tmp_path)

    assert [c.section for c in result.cleared] == ["setup"]
    assert result.skipped == []
    cleared = result.cleared[0]
    assert len(cleared.section_sha) == 64
    assert len(cleared.view_sha) == 64

    # A single code attestation was journaled — never reading as a human ack.
    records = _records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["block"] == nb.AUTO_CLEAR_BLOCK
    assert rec["response"] == nb.AUTO_CLEAR_RESPONSE == "auto_cleared"
    assert rec["response"] != "y"
    assert rec["resolved"]["attestor"] == "code"

    # notebook-status / the reduction now reads the section auto_cleared.
    assert _status(tmp_path, "setup") == nb.AUTO_CLEARED


# ── modified section → skipped human_required, NOT journaled ──────────────────


def test_modified_section_is_skipped_not_cleared(tmp_path: Path) -> None:
    _write(tmp_path, _CLEAN_EDITED, _CLEAN)  # source diverges from template
    result = _run(tmp_path)

    assert result.cleared == []
    assert [(s.section, s.reason) for s in result.skipped] == [("setup", "human_required")]
    assert _records(tmp_path) == []  # nothing journaled


# ── lint-flagged section → skipped (server-side lint cannot be bypassed) ──────


def test_lint_flagged_section_cannot_be_laundered(tmp_path: Path) -> None:
    # The section is byte-identical to the template (inherited) but carries a
    # missing path literal. The RECORDED roots (interview.json) drive the
    # server-side lint recompute, so the section is flagged and stays
    # human_required — the caller supplies no findings and no roots.
    _write(tmp_path, _FLAGGED, _FLAGGED)
    _write_interview(tmp_path, input_roots=["inputs"])
    result = _run(tmp_path)

    assert result.cleared == []
    assert [(s.section, s.reason) for s in result.skipped] == [("load", "human_required")]
    assert _records(tmp_path) == []


# ── F2: caller-supplied roots are REFUSED (journal-effective laundering) ──────


def test_caller_supplied_input_roots_are_refused(tmp_path: Path) -> None:
    """Supplying roots is a loud SpecInvalid — the roots come from the recorded
    config only, so a caller cannot point the lint at planted files."""
    _write(tmp_path, _CLEAN, _CLEAN)
    with pytest.raises(errors.SpecInvalid, match="laundering"):
        _run(tmp_path, input_roots=["planted"])


def test_caller_supplied_source_roots_are_refused(tmp_path: Path) -> None:
    _write(tmp_path, _CLEAN, _CLEAN)
    with pytest.raises(errors.SpecInvalid, match="laundering"):
        _run(tmp_path, source_roots=["planted"])


def test_planted_dummy_root_no_longer_clears_a_flagged_section(tmp_path: Path) -> None:
    """The review's laundering scenario: an agent plants a dummy file under a root
    of its choosing to stop the missing-literal flag. With caller roots refused,
    the flagged section stays human_required and nothing is journaled."""
    _write(tmp_path, _FLAGGED, _FLAGGED)
    _write_interview(tmp_path, input_roots=["inputs"])
    # Plant the dummy the section references, under a caller-chosen root.
    (tmp_path / "planted").mkdir()
    (tmp_path / "planted" / "missing.csv").write_text("x\n", encoding="utf-8")

    # Passing the planted root is refused outright — no clearance, no journal.
    with pytest.raises(errors.SpecInvalid, match="laundering"):
        _run(tmp_path, input_roots=["planted"])
    assert _records(tmp_path) == []

    # And the honest run (recorded roots) keeps the section human_required.
    result = _run(tmp_path)
    assert result.cleared == []
    assert [(s.section, s.reason) for s in result.skipped] == [("load", "human_required")]


# ── idempotent re-run → nothing appended, already-current ─────────────────────


def test_rerun_is_idempotent_no_op(tmp_path: Path) -> None:
    _write(tmp_path, _CLEAN, _CLEAN)
    _run(tmp_path)
    assert len(_records(tmp_path)) == 1

    second = _run(tmp_path)
    assert second.cleared == []
    assert [(s.section, s.reason) for s in second.skipped] == [("setup", "already-current")]
    assert len(_records(tmp_path)) == 1  # journal line count unchanged


# ── edit after clear → stale → re-clears at the new sha with a NEW record ─────


def test_edit_after_clear_reclears_at_new_sha_append_only(tmp_path: Path) -> None:
    _write(tmp_path, _CLEAN, _CLEAN)
    first = _run(tmp_path)
    old_sha = first.cleared[0].section_sha
    assert len(_records(tmp_path)) == 1

    # Edit BOTH source and template identically: the section stays inherited
    # (clean) but its sha moves, so the prior auto-clear goes stale → unsigned.
    _write(tmp_path, _CLEAN_EDITED, _CLEAN_EDITED)
    assert _status(tmp_path, "setup") == nb.UNSIGNED  # stale auto-clear fell back

    second = _run(tmp_path)
    new_sha = second.cleared[0].section_sha
    assert [c.section for c in second.cleared] == ["setup"]
    assert new_sha != old_sha
    # Append-only: a NEW record, not a mutation of the old one.
    records = _records(tmp_path)
    assert len(records) == 2
    assert [r["resolved"]["section_sha"] for r in records] == [old_sha, new_sha]
    # The section reads auto_cleared again at its new hash.
    assert _status(tmp_path, "setup") == nb.AUTO_CLEARED


# ── the laundering fire test FLIPS (T10) ─────────────────────────────────────


def test_inline_receipt_argument_is_impossible(tmp_path: Path) -> None:
    """The v1 laundering vector is GONE: the spec has no receipt field.

    A caller can no longer hand ``notebook-auto-clear`` an opaque
    ``{slug: {error: False}}`` to green an assertion-bearing section — the
    ``extra="forbid"`` wire model rejects the key outright.
    """
    with pytest.raises(ValidationError):
        NotebookAutoClearSpec.model_validate(
            {
                "audit_id": _AUDIT,
                "source": "source.py",
                "template": "template.py",
                "receipt": {"model": {"error": False}},
            }
        )


def _record_receipt(tmp_path: Path, slug: str, *, error: bool) -> None:
    """Journal a render receipt for *slug* via the real record-receipt verb."""
    notebook_record_receipt(
        experiment_dir=tmp_path,
        spec=NotebookRecordReceiptSpec.model_validate(
            {
                "audit_id": _AUDIT,
                "source": "source.py",
                "entries": {slug: {"output_sha": "out-abc", "error": error}},
            }
        ),
    )


def test_journaled_fresh_receipt_greens_asserted_section_end_to_end(tmp_path: Path) -> None:
    _write(tmp_path, _ASSERTED, _ASSERTED)

    # Without any journaled receipt the section's assertion is unproven →
    # human_required (nothing cleared).
    without = _run(tmp_path)
    assert without.cleared == []
    assert [(s.section, s.reason) for s in without.skipped] == [("model", "human_required")]
    assert _records(tmp_path) == []

    # record-receipt → auto-clear → notebook-status reads auto_cleared.
    _record_receipt(tmp_path, "model", error=False)
    withr = _run(tmp_path)
    assert [c.section for c in withr.cleared] == ["model"]
    assert withr.skipped == []
    assert _status(tmp_path, "model") == nb.AUTO_CLEARED


def test_error_true_receipt_never_greens(tmp_path: Path) -> None:
    _write(tmp_path, _ASSERTED, _ASSERTED)
    _record_receipt(tmp_path, "model", error=True)
    result = _run(tmp_path)
    assert result.cleared == []
    assert [(s.section, s.reason) for s in result.skipped] == [("model", "human_required")]


def test_edit_after_receipt_drift_revokes_green(tmp_path: Path) -> None:
    """A receipt journaled at an OLD sha greens nothing after the section drifts."""
    _write(tmp_path, _ASSERTED, _ASSERTED)
    _record_receipt(tmp_path, "model", error=False)

    # Confirm the receipt is fresh right now.
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    shas = {s.slug: s.section_sha for s in source.sections}
    fresh = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=shas)
    assert fresh["model"]["fresh"] is True

    # Edit the asserted section (both source + template so it stays inherited but
    # its sha moves). The receipt now points at the OLD sha → stale.
    _asserted_edited = _ASSERTED.replace("return 42", "return 7").replace(
        "train() == 42", "train() == 7"
    )
    _write(tmp_path, _asserted_edited, _asserted_edited)
    source2 = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    shas2 = {s.slug: s.section_sha for s in source2.sections}
    stale = nb.read_render_receipts(tmp_path, _AUDIT, current_shas=shas2)
    assert stale["model"]["fresh"] is False

    # Auto-clear must NOT green the drifted section (no fresh receipt).
    result = _run(tmp_path)
    assert result.cleared == []
    assert [(s.section, s.reason) for s in result.skipped] == [("model", "human_required")]

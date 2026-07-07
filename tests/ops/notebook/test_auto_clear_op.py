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

from typing import TYPE_CHECKING

from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearSpec
from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear
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
    # missing path literal. The caller passes NO findings — the verb recomputes
    # them server-side, so the section is flagged and stays human_required.
    _write(tmp_path, _FLAGGED, _FLAGGED)
    result = _run(tmp_path, input_roots=["inputs"])

    assert result.cleared == []
    assert [(s.section, s.reason) for s in result.skipped] == [("load", "human_required")]
    assert _records(tmp_path) == []


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


# ── receipt greens an asserted section → cleared ──────────────────────────────


def test_receipt_greens_asserted_section_into_a_clear(tmp_path: Path) -> None:
    _write(tmp_path, _ASSERTED, _ASSERTED)

    # Without a receipt the section's assertion is unproven → human_required.
    without = _run(tmp_path)
    assert without.cleared == []
    assert [(s.section, s.reason) for s in without.skipped] == [("model", "human_required")]
    assert _records(tmp_path) == []

    # A receipt marking it error-free greens the assertion → auto_cleared → cleared.
    withr = _run(tmp_path, receipt={"model": {"output_sha": "abc", "error": False}})
    assert [c.section for c in withr.cleared] == ["model"]
    assert withr.skipped == []
    assert _status(tmp_path, "model") == nb.AUTO_CLEARED

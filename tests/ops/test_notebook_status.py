"""Direct-atom tests for the ``notebook-status`` query primitive (notebook-audit T6).

Writes a source + template ``.py`` under the experiment dir, journals sign-offs /
auto-clears with the real writers, calls the primitive, and asserts on the
per-section statuses + the rollup ``passed`` verdict. Also covers the read-only
contract and the spec_invalid path on a missing source/template.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.notebook_status import NotebookStatusResult, NotebookStatusSpec
from hpc_agent.ops.notebook.status_op import notebook_status
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT = "demo-audit"

_SOURCE = """\
# %%
# hpc-audit-section: load-data
import pandas as pd
df = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(df)
"""

# The template shares both sections' content → identical section shas by
# construction (templates parsed by the same parser).
_TEMPLATE = _SOURCE


def _write(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")


def _status(tmp_path: Path) -> NotebookStatusResult:
    return notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )


def _sign_off(tmp_path: Path, slug: str, section_sha: str) -> None:
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response="y",
        resolved={"audit_id": _AUDIT, "section": slug, "section_sha": section_sha, "view_sha": "v"},
    )


def test_all_unsigned_when_no_records(tmp_path: Path) -> None:
    _write(tmp_path)
    result = _status(tmp_path)
    assert result.passed is False
    assert {s.slug for s in result.sections} == {"load-data", "fit-model"}
    assert all(s.status == nb.UNSIGNED for s in result.sections)


def test_rollup_passes_when_signed_and_auto_cleared(tmp_path: Path) -> None:
    _write(tmp_path)
    source = parse_percent_source(_SOURCE)
    by_slug = {s.slug: s for s in source.sections}
    _sign_off(tmp_path, "load-data", by_slug["load-data"].section_sha)
    nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=by_slug["fit-model"].section_sha,
        recompute=by_slug["fit-model"].section_sha,
    )
    result = _status(tmp_path)
    assert result.passed is True
    statuses = {s.slug: s.status for s in result.sections}
    assert statuses["load-data"] == nb.SIGNED_CURRENT
    assert statuses["fit-model"] == nb.AUTO_CLEARED
    # Section order follows the template inventory.
    assert [s.slug for s in result.sections] == ["load-data", "fit-model"]


def test_edit_after_sign_off_reads_signed_stale_and_fails_rollup(tmp_path: Path) -> None:
    _write(tmp_path)
    source = parse_percent_source(_SOURCE)
    by_slug = {s.slug: s for s in source.sections}
    for slug, sec in by_slug.items():
        _sign_off(tmp_path, slug, sec.section_sha)
    # Edit fit-model on disk → its current sha moves → its sign-off goes stale.
    edited = _SOURCE.replace("model = fit(df)", "model = fit(df, regularize=True)")
    (tmp_path / "source.py").write_text(edited, encoding="utf-8")
    result = _status(tmp_path)
    assert result.passed is False
    statuses = {s.slug: s.status for s in result.sections}
    assert statuses["load-data"] == nb.SIGNED_CURRENT  # untouched section stays current
    assert statuses["fit-model"] == nb.SIGNED_STALE


def test_read_only_writes_nothing(tmp_path: Path) -> None:
    _write(tmp_path)
    _status(tmp_path)
    # A pure read never scaffolds the notebook journal tree.
    assert not (tmp_path / ".hpc" / "notebooks").exists()


# ─── relay-due markers (the omission gate) ───────────────────────────────────


def _relay_due_markers(tmp_path: Path) -> list[dict]:
    from hpc_agent.state.decision_journal import read_decisions

    return [
        r
        for r in read_decisions(tmp_path, "notebook", _AUDIT)
        if r.get("block") == nb.RELAY_DUE_BLOCK
    ]


def _pass_the_audit(tmp_path: Path) -> None:
    source = parse_percent_source(_SOURCE)
    for slug, sec in {s.slug: s for s in source.sections}.items():
        _sign_off(tmp_path, slug, sec.section_sha)


def test_terminal_passed_journals_one_deduplicated_marker(tmp_path: Path) -> None:
    """A passed rollup journals the relay obligation: the design's marker shape,
    keyed on the state word + the source module sha12 — and recomputing the same
    terminal fact appends nothing (the dedup that keeps `idempotent=True` honest)."""
    _write(tmp_path)
    _pass_the_audit(tmp_path)

    assert _status(tmp_path).passed is True
    markers = _relay_due_markers(tmp_path)
    assert len(markers) == 1
    record = markers[0]
    assert record["response"] == nb.RELAY_DUE_RESPONSE
    resolved = record["resolved"]
    module_sha12 = parse_percent_source(_SOURCE).module_sha[:12]
    assert resolved["record_kind"] == nb.RELAY_DUE_RECORD_KIND == "notebook-status"
    assert resolved["audit_id"] == _AUDIT
    assert resolved["key_tokens"] == ["passed", module_sha12]
    assert resolved["created_at"]

    # Re-running the status on the same terminal fact appends NO second marker.
    assert _status(tmp_path).passed is True
    assert len(_relay_due_markers(tmp_path)) == 1


def test_terminal_failed_marker_on_drift_revoked_sign_off(tmp_path: Path) -> None:
    """A signed_stale section is a TERMINAL verdict (a human approval was revoked
    by drift) → a `failed` marker at the EDITED source's module sha12."""
    _write(tmp_path)
    _pass_the_audit(tmp_path)
    edited = _SOURCE.replace("model = fit(df)", "model = fit(df, regularize=True)")
    (tmp_path / "source.py").write_text(edited, encoding="utf-8")

    result = _status(tmp_path)
    assert result.passed is False
    failed = [m for m in _relay_due_markers(tmp_path) if m["resolved"]["key_tokens"][0] == "failed"]
    assert len(failed) == 1
    assert failed[0]["resolved"]["key_tokens"] == [
        "failed",
        parse_percent_source(edited).module_sha[:12],
    ]


def test_non_terminal_status_sets_no_marker(tmp_path: Path) -> None:
    """The narrow set (D8 applied to gates): the ordinary in-loop mix — unsigned
    sections still being drafted/signed — is NOT terminal and journals nothing."""
    _write(tmp_path)
    # All unsigned: nothing journaled at all (and no journal scaffolded — the
    # sibling test_read_only_writes_nothing pins the no-scaffold half).
    assert _status(tmp_path).passed is False
    assert _relay_due_markers(tmp_path) == []

    # Partially signed (one signed_current, one unsigned): still in the loop.
    source = parse_percent_source(_SOURCE)
    by_slug = {s.slug: s for s in source.sections}
    _sign_off(tmp_path, "load-data", by_slug["load-data"].section_sha)
    assert _status(tmp_path).passed is False
    assert _relay_due_markers(tmp_path) == []


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="source"):
        _status(tmp_path)


def test_missing_template_is_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="template"):
        _status(tmp_path)

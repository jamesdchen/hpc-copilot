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
from hpc_agent.ops.notebook_status import notebook_status
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


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="source"):
        _status(tmp_path)


def test_missing_template_is_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="template"):
        _status(tmp_path)

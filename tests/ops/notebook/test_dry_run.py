"""Direct-atom tests for the ``notebook-dry-run`` mutate primitive (the preview run).

Exercises the drafting-loop PREVIEW over a tiny synthetic percent source: sections
run in order in one namespace, the sample cap reaches the source via the documented
env var, a raising section relays its traceback verbatim and STOPS the run (later
sections read ``skipped``), a runaway source is bounded by the timeout, standalone
mode needs no ``.hpc`` state — and the load-bearing TRUST PIN: a sampled receipt is
NON-CLEARING (it never flips an assertion-bearing section to ``auto_cleared`` /
past the graduation gate the way a full run does).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearSpec
from hpc_agent._wire.actions.notebook_dry_run import NotebookDryRunSpec
from hpc_agent._wire.actions.notebook_record_receipt import NotebookRecordReceiptSpec
from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear
from hpc_agent.ops.notebook.dry_run_op import notebook_dry_run
from hpc_agent.ops.notebook.record_receipt_op import notebook_record_receipt
from hpc_agent.ops.notebook_gate import assert_source_audited
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.actions.notebook_dry_run import (
        NotebookDryRunResult,
        NotebookDryRunSection,
    )

_AUDIT = "demo-audit"

# Two clean, assertion-free sections plus one asserting section.
_SOURCE = """\
# %%
# hpc-audit-section: setup
import os
CAP = int(os.environ.get("HPC_NOTEBOOK_SAMPLE_N", "0"))
print("cap", CAP)

# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42
print("trained")
"""

# A middle section that raises; a later section must not run.
_RAISING = """\
# %%
# hpc-audit-section: first
x = 1

# %%
# hpc-audit-section: boom
raise ValueError("kaboom-42")

# %%
# hpc-audit-section: after
y = 2
"""


def _write_source(tmp_path: Path, source: str = _SOURCE) -> None:
    (tmp_path / "source.py").write_text(source, encoding="utf-8")


def _run(tmp_path: Path, **overrides: object) -> NotebookDryRunResult:
    spec: dict[str, object] = {"source": "source.py"}
    spec.update(overrides)
    return notebook_dry_run(experiment_dir=tmp_path, spec=NotebookDryRunSpec.model_validate(spec))


def _by_slug(result: NotebookDryRunResult) -> dict[str, NotebookDryRunSection]:
    return {s.slug: s for s in result.sections}


# ── happy path: sections run, receipts journaled with SAMPLED scope ───────────


def test_happy_path_sections_run_and_sampled_receipts_journaled(tmp_path: Path) -> None:
    _write_source(tmp_path)
    result = _run(tmp_path, audit_id=_AUDIT, sample_n=5)

    assert result.executed_scope == nb.EXECUTION_SCOPE_SAMPLED
    sections = _by_slug(result)
    assert sections["setup"].outcome == "ran"
    assert sections["model"].outcome == "ran"
    assert set(result.receipts_recorded) == {"setup", "model"}

    # The asserting section's assert line RAN and passed (distinct from the static
    # table — this is the executed verdict).
    model_asserts = sections["model"].assertions
    assert [a.outcome for a in model_asserts] == ["passed"]

    # The journaled receipts carry the sampled marker.
    records = [
        r
        for r in nb.read_decisions(tmp_path, "notebook", _AUDIT)
        if r.get("block") == nb.RENDER_RECEIPT_BLOCK
    ]
    assert records, "a receipt should have been journaled"
    assert all(r["resolved"].get("execution_scope") == nb.EXECUTION_SCOPE_SAMPLED for r in records)


def test_sample_cap_visible_to_source_via_env_var(tmp_path: Path) -> None:
    _write_source(tmp_path)
    result = _run(tmp_path, sample_n=7)
    # The source printed the cap it read from HPC_NOTEBOOK_SAMPLE_N.
    assert "cap 7" in (_by_slug(result)["setup"].stdout_tail or "")
    assert result.sample_env_var == "HPC_NOTEBOOK_SAMPLE_N"
    assert "ADVISORY" in result.sample_disclosure


# ── a raising section relays the traceback verbatim; later sections don't run ──


def test_raising_section_relays_traceback_and_stops_run(tmp_path: Path) -> None:
    _write_source(tmp_path, _RAISING)
    result = _run(tmp_path)
    sections = _by_slug(result)

    assert sections["first"].outcome == "ran"
    assert sections["boom"].outcome == "raised"
    assert sections["boom"].error is True
    # Verbatim traceback tail (the source's own crash, never interpreted).
    assert "ValueError" in (sections["boom"].traceback_tail or "")
    assert "kaboom-42" in (sections["boom"].traceback_tail or "")
    # A later section that depended on the crashed one is NOT run (deterministic).
    assert sections["after"].outcome == "skipped"
    assert sections["after"].ran is False


# ── standalone mode: no audit_id, no .hpc state ───────────────────────────────


def test_standalone_mode_needs_no_hpc_state(tmp_path: Path) -> None:
    _write_source(tmp_path)
    result = _run(tmp_path)  # no audit_id

    assert result.audit_id is None
    assert result.receipts_recorded == []
    assert _by_slug(result)["setup"].outcome == "ran"
    # Nothing was written under .hpc (no journal scope to write to).
    assert not (tmp_path / ".hpc").exists()


# ── section filter: runs up to and including the named section ────────────────


def test_section_filter_runs_up_to_named_section(tmp_path: Path) -> None:
    _write_source(tmp_path)
    result = _run(tmp_path, sections=["setup"])
    sections = _by_slug(result)
    assert sections["setup"].outcome == "ran"
    assert sections["model"].outcome == "skipped"


def test_section_filter_unknown_slug_is_spec_invalid(tmp_path: Path) -> None:
    _write_source(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="not in the source"):
        _run(tmp_path, sections=["no-such-section"])


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="source"):
        _run(tmp_path)


# ── timeout kills a runaway source cleanly ────────────────────────────────────


def test_timeout_bounds_a_runaway_source(tmp_path: Path) -> None:
    runaway = """\
# %%
# hpc-audit-section: spin
import time
time.sleep(30)

# %%
# hpc-audit-section: never
z = 1
"""
    _write_source(tmp_path, runaway)
    result = _run(tmp_path, timeout_sec=1)
    assert result.timed_out is True
    sections = _by_slug(result)
    # The in-progress section is marked timeout; the later one skipped. The verb
    # RETURNED within the bound rather than hanging.
    assert sections["spin"].outcome == "timeout"
    assert sections["never"].outcome == "skipped"


# ── THE TRUST PIN: a sampled receipt never auto-clears / passes the gate ──────

_ASSERTED = """\
# %%
# hpc-audit-section: model
def train():
    return 42
assert train() == 42
"""


def _write_audit(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(_ASSERTED, encoding="utf-8")
    (tmp_path / "template.py").write_text(_ASSERTED, encoding="utf-8")
    block = {"source": "source.py", "template": "template.py", "audit_id": _AUDIT}
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": block}), encoding="utf-8"
    )


def _status(tmp_path: Path, slug: str) -> str:
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    template = parse_percent_source((tmp_path / "template.py").read_text(encoding="utf-8"))
    rollup = nb.audit_module(tmp_path, _AUDIT, source=source, required_slugs=template.slugs)
    return next(s.status for s in rollup.sections if s.slug == slug)


def test_sampled_receipt_never_auto_clears_an_asserted_section(tmp_path: Path) -> None:
    _write_audit(tmp_path)

    # A dry-run journals a SAMPLED receipt for the asserting section (its assert ran).
    dr = notebook_dry_run(
        experiment_dir=tmp_path,
        spec=NotebookDryRunSpec(source="source.py", audit_id=_AUDIT, sample_n=3),
    )
    assert dr.receipts_recorded == ["model"]

    # notebook-auto-clear must NOT clear it — the sampled receipt is filtered from
    # the clearing reader, so the assertion is still unproven → human_required.
    cleared = notebook_auto_clear(
        experiment_dir=tmp_path,
        spec=NotebookAutoClearSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )
    assert cleared.cleared == []
    assert [(s.section, s.reason) for s in cleared.skipped] == [("model", "human_required")]
    assert _status(tmp_path, "model") == nb.UNSIGNED

    # The graduation gate still refuses (the section is not signed-current).
    with pytest.raises(errors.SourceUnaudited):
        assert_source_audited(tmp_path)


def test_full_receipt_still_clears_where_sampled_could_not(tmp_path: Path) -> None:
    """The other half of the pin: a FULL run IS clearing evidence — only sampled
    is held back. Journal a sampled receipt first, then a full one; the full clears."""
    _write_audit(tmp_path)

    notebook_dry_run(
        experiment_dir=tmp_path,
        spec=NotebookDryRunSpec(source="source.py", audit_id=_AUDIT, sample_n=3),
    )
    # A FULL receipt (the plugin's execute path, here via the core record verb).
    notebook_record_receipt(
        experiment_dir=tmp_path,
        spec=NotebookRecordReceiptSpec.model_validate(
            {
                "audit_id": _AUDIT,
                "source": "source.py",
                "entries": {"model": {"output_sha": "out-abc", "error": False}},
            }
        ),
    )
    cleared = notebook_auto_clear(
        experiment_dir=tmp_path,
        spec=NotebookAutoClearSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )
    assert [c.section for c in cleared.cleared] == ["model"]
    assert _status(tmp_path, "model") == nb.AUTO_CLEARED
    # And the gate now passes (no raise).
    assert_source_audited(tmp_path)

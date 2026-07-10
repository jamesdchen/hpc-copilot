"""Tests for the ``notebook-record-config`` mutate primitive (run-#10 seat).

Fires-and-passes pairs for every refusal + the disclosure: the happy standalone
record (and the read fallback it feeds), the interview-owns-it refusal, the
immutable-per-audit refusal (superseding = a new audit_id), the loud
late-record warning (vs none on a virgin journal), the interview-wins
precedence, and the standalone-audit integration flow — record-config → lint
(output literal exempt) → view reads the recorded roots → ``canonical: true``
with non-empty roots.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_record_config import (
    NotebookRecordConfigResult,
    NotebookRecordConfigSpec,
)
from hpc_agent.ops.notebook.canonical import read_recorded_config
from hpc_agent.ops.notebook.record_config_op import notebook_record_config
from hpc_agent.state import notebook_audit
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

_SOURCE = """\
# %%
# hpc-audit-section: load
DATA = "data/input.txt"

# %%
# hpc-audit-section: report
OUT = "results/summary.json"
"""


def _record(tmp_path: Path, **overrides: object) -> NotebookRecordConfigResult:
    spec_dict: dict[str, object] = {
        "audit_id": "aud-1",
        "input_roots": ["data"],
        "source_roots": ["src"],
        "output_roots": ["results"],
    }
    spec_dict.update(overrides)
    return notebook_record_config(
        experiment_dir=tmp_path,
        spec=NotebookRecordConfigSpec.model_validate(spec_dict),
    )


def test_record_then_read_falls_back_to_journal(tmp_path: Path) -> None:
    """The standalone seat: no interview block → the journaled record supplies
    the canonical config (non-empty roots — the rootless-canonical fix)."""
    result = _record(tmp_path, attention_order=["report", "load"])
    assert result.warning is None
    cfg = read_recorded_config(tmp_path, "aud-1")
    assert cfg.input_roots == ["data"]
    assert cfg.source_roots == ["src"]
    assert cfg.output_roots == ["results"]
    assert cfg.attention_order == ["report", "load"]


def test_observables_round_trip_through_journal(tmp_path: Path) -> None:
    """The A14 observation plan rides the config seat: record-config persists
    ``observables`` and read_recorded_config returns them (the runner reads here)."""
    result = _record(tmp_path, observables=["frame", "totals"])
    assert result.observables == ["frame", "totals"]
    cfg = read_recorded_config(tmp_path, "aud-1")
    assert cfg.observables == ["frame", "totals"]


def test_observables_absent_reads_none(tmp_path: Path) -> None:
    """No observation plan → the loop is OFF (None, not []) — the D7 byte-identity
    posture mirrored on the read side."""
    _record(tmp_path)
    assert read_recorded_config(tmp_path, "aud-1").observables is None


def test_observables_empty_string_is_refused(tmp_path: Path) -> None:
    """Observable names are opaque but must be NON-EMPTY (a blank binds nothing)."""
    with pytest.raises(ValueError, match="non-empty"):
        NotebookRecordConfigSpec.model_validate(
            {"audit_id": "aud-1", "input_roots": [], "source_roots": [], "observables": ["ok", ""]}
        )


def test_refuses_when_interview_audited_source_owns_the_config(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"audit_id": "aud-1", "source": "s.py", "template": "t.py"}}),
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid, match="audited_source"):
        _record(tmp_path)


def test_interview_block_for_another_audit_does_not_block(tmp_path: Path) -> None:
    """The pass pair: a stray block for a DIFFERENT audit_id never refuses this one."""
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"audit_id": "other", "source": "s.py", "template": "t.py"}}),
        encoding="utf-8",
    )
    assert _record(tmp_path).warning is None
    assert read_recorded_config(tmp_path, "aud-1").input_roots == ["data"]


def test_refuses_second_record_immutable_per_audit(tmp_path: Path) -> None:
    _record(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="NEW audit_id"):
        _record(tmp_path, input_roots=["elsewhere"])
    # The first record still wins on read (never clobbered).
    assert read_recorded_config(tmp_path, "aud-1").input_roots == ["data"]


def test_late_record_succeeds_with_loud_warning(tmp_path: Path) -> None:
    """An audit with prior journal entries accepts the config but discloses:
    every view_sha moves, prior sign-offs read stale."""
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="aud-1",
        block=notebook_audit.SIGN_OFF_BLOCK,
        response="y",
        resolved={"audit_id": "aud-1", "section": "load", "section_sha": "a" * 64},
    )
    result = _record(tmp_path)
    assert result.warning is not None
    assert "view_sha" in result.warning
    assert "stale" in result.warning.lower()
    # The record still landed and reads back.
    assert read_recorded_config(tmp_path, "aud-1").input_roots == ["data"]


def test_no_warning_on_virgin_journal(tmp_path: Path) -> None:
    assert _record(tmp_path).warning is None


def test_interview_wins_over_journal_record(tmp_path: Path) -> None:
    """Precedence: the opt-in path's block wins even over an existing journaled
    config (written via the state helper — the verb itself refuses)."""
    notebook_audit.record_audit_config(
        tmp_path,
        audit_id="aud-1",
        input_roots=["journal-data"],
        source_roots=["journal-src"],
    )
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "audit_id": "aud-1",
                    "source": "s.py",
                    "template": "t.py",
                    "input_roots": ["interview-data"],
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = read_recorded_config(tmp_path, "aud-1")
    assert cfg.input_roots == ["interview-data"]
    assert cfg.source_roots == []  # the interview block's own (absent) value, not the journal's


def test_standalone_audit_flow_record_lint_view_canonical(tmp_path: Path) -> None:
    """The run-#10 close, end to end: record-config → lint (output literal
    exempt, input literal checked against real roots) → the default view reads
    the recorded roots → ``canonical: true`` with NON-EMPTY roots."""
    from hpc_agent._wire.actions.notebook_lint import NotebookLintInput
    from hpc_agent._wire.queries.notebook_audit_view import NotebookAuditViewSpec
    from hpc_agent.ops.notebook.lint import notebook_lint
    from hpc_agent.ops.notebook.view_op import notebook_audit_view

    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "input.txt").write_text("x", encoding="utf-8")

    _record(tmp_path)
    recorded = read_recorded_config(tmp_path, "aud-1")
    assert recorded.input_roots and recorded.source_roots  # never rootless again

    # Lint with the recorded roots: the output literal is a declared output
    # (exempt), the input literal exists under the recorded root → clean.
    lint = notebook_lint(
        experiment_dir=tmp_path,
        spec=NotebookLintInput(
            source="source.py",
            template="template.py",
            input_roots=recorded.input_roots,
            source_roots=recorded.source_roots,
            output_roots=recorded.output_roots,
        ),
    )
    assert lint.findings == []
    assert [(d.path, d.section) for d in lint.declared_outputs] == [
        ("results/summary.json", "report")
    ]

    # The DEFAULT view reads the same recorded (journaled) config → CANONICAL.
    view = notebook_audit_view(
        experiment_dir=tmp_path,
        spec=NotebookAuditViewSpec(audit_id="aud-1", source="source.py", template="template.py"),
    )
    assert view.canonical is True
    assert not any(f for s in view.sections for f in s.lint_flags)

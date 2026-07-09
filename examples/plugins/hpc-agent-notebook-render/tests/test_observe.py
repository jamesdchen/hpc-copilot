"""T-R — the runner's between-cell observation loop (toy vocabulary only).

Drives the pure ``observe_cell`` seam with STUB namespace dicts (no jupyter
kernel) and the full ``observe_source`` transport->ingest pass against a real
audit scope. Observables are toy names (``frame`` / ``totals``); the pack's
frame-aware measurer is a stub that wins over the stdlib fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent_notebook_render import _observe

from hpc_agent.execution.mapreduce.data_trace_contract import (
    TRACE_SOURCE_FIELD,
    TRACE_SOURCE_RUNNER,
)
from hpc_agent.state import data_trace as dt


def test_declared_and_present_measured_with_section_and_runner_tier() -> None:
    ns = {"frame": [1, 2, 3], "unrelated": "ignore me"}
    records, next_seq = _observe.observe_cell(ns, ["frame"], section="load", seq=0)
    assert len(records) == 1
    rec = records[0]
    assert rec["stage"] == "frame"
    assert rec["section"] == "load"
    assert rec[TRACE_SOURCE_FIELD] == TRACE_SOURCE_RUNNER
    assert rec["atoms"] == {"row_count": {"rows": 3, "dropped": 0}}
    assert next_seq == 1
    assert dt.validate_record(rec) == []


def test_declared_but_absent_is_skipped_silently() -> None:
    ns = {"frame": [1, 2]}
    records, next_seq = _observe.observe_cell(ns, ["frame", "totals"], section="load", seq=0)
    # ``totals`` is declared but not in the namespace -> no record, no error.
    assert [r["stage"] for r in records] == ["frame"]
    assert next_seq == 1


def test_unmeasurable_present_observable_is_skipped() -> None:
    # A present name the stdlib measurer cannot size (a plain str) yields nothing.
    ns = {"frame": "just text"}
    records, next_seq = _observe.observe_cell(ns, ["frame"], section="load", seq=0)
    assert records == []
    assert next_seq == 0


def test_injected_measurer_wins_over_stdlib_fallback() -> None:
    def pack_measurer(obj: Any) -> dict[str, Any] | None:
        # A pack impl measures a richer atom set the stdlib fallback never would.
        return {"col_set": {"columns": ["id", "qty"]}}

    ns = {"frame": [1, 2, 3]}
    records, _ = _observe.observe_cell(ns, ["frame"], section="load", seq=0, measurer=pack_measurer)
    # The pack atoms win; the stdlib row_count is NOT what landed.
    assert records[0]["atoms"] == {"col_set": {"columns": ["id", "qty"]}}


def test_injected_measurer_none_falls_through_to_stdlib() -> None:
    def blind_measurer(obj: Any) -> dict[str, Any] | None:
        return None  # "I can't measure this" -> the stdlib fallback runs

    ns = {"frame": [0, 0]}
    records, _ = _observe.observe_cell(
        ns, ["frame"], section="load", seq=0, measurer=blind_measurer
    )
    assert records[0]["atoms"] == {"row_count": {"rows": 2, "dropped": 0}}


def test_seq_is_monotone_across_cells() -> None:
    # Two cell boundaries, each with one present observable -> seq 0 then 1.
    r1, seq = _observe.observe_cell({"frame": [1]}, ["frame", "totals"], section="a", seq=0)
    r2, seq = _observe.observe_cell(
        {"frame": [1], "totals": [1, 2]}, ["frame", "totals"], section="b", seq=seq
    )
    seqs = [r["seq"] for r in (*r1, *r2)]
    assert seqs == sorted(set(seqs))  # strictly increasing, no collisions
    assert dt.check_seq_monotonicity([*r1, *r2]) == []


# --- the full pass: run -> transport -> ingest under the audit scope ----------

_SOURCE = """# %%
# hpc-audit-section: load
frame = [1, 2, 3, 4]

# %%
# hpc-audit-section: report
totals = {"a": 1, "b": 2}
"""


def test_observe_source_ingests_under_the_audit_scope(tmp_path: Path) -> None:
    summary = _observe.observe_source(
        tmp_path,
        audit_id="aud-1",
        source_text=_SOURCE,
        observables=["frame", "totals"],
    )
    assert summary is not None
    assert summary["scope_kind"] == "audit"
    assert summary["scope_id"] == "aud-1"
    # Total coverage = cell boundaries x declared-and-present names: ``frame`` is
    # measured at BOTH boundaries (it persists in the namespace — its per-stage
    # row series), ``totals`` once at the report boundary. So 3 records.
    assert summary["stage_count"] == 3

    # The records land in the audit store (traces/audit/<audit_id>/task-0.jsonl),
    # tagged with their section and runner tier.
    stored = dt.read_trace(tmp_path, "audit", "aud-1", 0)
    sections = [(r["stage"], r["section"]) for r in stored]
    assert sections == [("frame", "load"), ("frame", "report"), ("totals", "report")]
    assert stored[0]["atoms"] == {"row_count": {"rows": 4, "dropped": 0}}
    assert all(r[TRACE_SOURCE_FIELD] == TRACE_SOURCE_RUNNER for r in stored)

    # The transport packet is disposable — ingestion removed it.
    transport = tmp_path / ".hpc" / "traces" / "_transport" / "aud-1" / "_trace.jsonl"
    assert not transport.exists()


def test_no_observables_is_off_no_probes(tmp_path: Path) -> None:
    # The loop is OFF: no observation plan -> None, and no trace store written.
    summary = _observe.observe_source(
        tmp_path, audit_id="aud-1", source_text=_SOURCE, observables=[]
    )
    assert summary is None
    assert dt.read_trace(tmp_path, "audit", "aud-1", 0) == []


def test_render_execute_wires_the_observation_loop(tmp_path: Path) -> None:
    """The render --execute path runs T-R when the SIGNED audit config declares
    observables: trace_stages populates and records land in the audit scope."""
    from hpc_agent_notebook_render._models import NotebookRenderSpec
    from hpc_agent_notebook_render.render import notebook_render

    from hpc_agent._wire.actions.notebook_record_config import NotebookRecordConfigSpec
    from hpc_agent.ops.notebook.record_config_op import notebook_record_config

    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_SOURCE, encoding="utf-8")
    notebook_record_config(
        experiment_dir=tmp_path,
        spec=NotebookRecordConfigSpec.model_validate(
            {
                "audit_id": "aud-1",
                "input_roots": [],
                "source_roots": [],
                "observables": ["frame", "totals"],
            }
        ),
    )

    result = notebook_render(
        experiment_dir=tmp_path,
        spec=NotebookRenderSpec(
            audit_id="aud-1", source="source.py", template="template.py", execute=True
        ),
    )
    assert result.trace_stages == 3  # frame@load, frame@report, totals@report
    stored = dt.read_trace(tmp_path, "audit", "aud-1", 0)
    assert {r["stage"] for r in stored} == {"frame", "totals"}


def test_render_without_observables_leaves_the_loop_off(tmp_path: Path) -> None:
    """No observation plan -> trace_stages is None and no audit trace is written
    (the render is byte-identical to the no-plan world; D7)."""
    from hpc_agent_notebook_render._models import NotebookRenderSpec
    from hpc_agent_notebook_render.render import notebook_render

    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_SOURCE, encoding="utf-8")
    result = notebook_render(
        experiment_dir=tmp_path,
        spec=NotebookRenderSpec(
            audit_id="aud-1", source="source.py", template="template.py", execute=True
        ),
    )
    assert result.trace_stages is None
    assert dt.read_trace(tmp_path, "audit", "aud-1", 0) == []


def test_run_observation_stops_at_a_raising_cell(tmp_path: Path) -> None:
    source = (
        "# %%\n# hpc-audit-section: load\nframe = [1, 2]\n\n"
        "# %%\n# hpc-audit-section: boom\nraise ValueError('nope')\n\n"
        "# %%\n# hpc-audit-section: after\ntotals = [1, 2, 3]\n"
    )
    records = _observe.run_observation(_observe._percent_code_cells(source), ["frame", "totals"])
    # ``frame`` observed before the raise; ``totals`` never reached (disclosure).
    assert [r["stage"] for r in records] == ["frame"]

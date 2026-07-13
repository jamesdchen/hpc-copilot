"""Tests for the ``trace-render`` projection op (``ops/trace_render_op.py``, T5).

Toy text/CSV-shaped traces only — no quant vocabulary: stages are
``load``/``dedup``/``join``, columns are ``id``/``name``/``qty``, labels are
``coord_space`` with values ``raw``/``norm``. Exercises the four views, byte
stability, the conservation flag surfacing in the waterfall, honest absence,
the ``cmd_sha`` reference lookup, the never-judgment vocabulary pin, and the
non-creating (pure) posture.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import hpc_agent.state.data_trace as dt
from hpc_agent import errors
from hpc_agent._wire.queries.trace_render import TraceRenderSpec
from hpc_agent.ops import trace_render_op as tr
from hpc_agent.ops.trace_render_op import trace_render
from hpc_agent.state.runs import write_run_sidecar

_TS = "2026-07-08T12:00:00+00:00"


def _sketch(mean: float, *, mn: float = 0.0, mx: float = 10.0) -> dict:
    return {
        "min": mn,
        "max": mx,
        "mean": mean,
        "std": 1.0,
        "quantiles": {"q05": mn, "q50": mean, "q95": mx},
    }


def _toy_records() -> list[dict]:
    """A three-stage toy pipeline: load -> dedup (drops 2) -> join (adds a col)."""
    return [
        dt.make_record(
            "load",
            0,
            {
                "row_count": {"rows": 10, "dropped": 0},
                "col_set": {"columns": ["id", "name"]},
                "null_count": {"name": 1},
                "value_sketch": {"id": _sketch(5.0)},
                "label_chain": {"coord_space": "raw"},
            },
        ),
        dt.make_record(
            "dedup",
            1,
            {
                "row_count": {"rows": 8, "dropped": 2},
                "col_set": {"columns": ["id", "name"]},
                "label_chain": {"coord_space": "raw"},
            },
        ),
        dt.make_record(
            "join",
            2,
            {
                "row_count": {"rows": 8, "dropped": 0},
                "col_set": {"columns": ["id", "name", "qty"]},
                "value_sketch": {"qty": _sketch(3.0)},
                "label_chain": {"coord_space": "norm"},
            },
        ),
    ]


def _write(
    experiment_dir: Path, records: list[dict], *, scope="run", sid="run-abc", task=0
) -> None:
    dt.write_trace(experiment_dir, scope, sid, task, records)


# --- the four views -----------------------------------------------------------


def test_waterfall_view_and_conservation_arithmetic(tmp_path: Path):
    _write(tmp_path, _toy_records())
    res = trace_render(
        experiment_dir=tmp_path,
        spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc"),
    )
    assert res.present is True
    assert res.stage_count == 3
    wf = {r.stage: r for r in res.waterfall}
    assert (wf["load"].rows_in, wf["load"].rows_out, wf["load"].dropped) == (None, 10, 0)
    assert (wf["dedup"].rows_in, wf["dedup"].rows_out, wf["dedup"].dropped) == (10, 8, 2)
    assert wf["dedup"].expected == 8  # 10 - 2, conserved
    assert wf["join"].expected == 8
    # No conservation flag on a conserved trace.
    assert [f for f in res.flags if f.rule == "row_conservation"] == []


def test_conservation_flag_surfaces_in_waterfall(tmp_path: Path):
    recs = _toy_records()
    # Break conservation: dedup drops 2 but only 5 rows survive from 10.
    recs[1]["atoms"]["row_count"] = {"rows": 5, "dropped": 2}
    _write(tmp_path, recs)
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    )
    conservation = [f for f in res.flags if f.rule == "row_conservation"]
    # The dedup break fires (and cascades to join, whose rows_in is now 5).
    assert any(f.evidence.get("stage") == "dedup" for f in conservation)
    # The flag renders beneath the waterfall table, verbatim.
    waterfall_block = res.render.split("## Label chains")[0]
    assert "row_conservation" in waterfall_block


def test_label_chain_view(tmp_path: Path):
    _write(tmp_path, _toy_records())
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    )
    assert res.label_chains["coord_space"] == ["load=raw", "dedup=raw", "join=norm"]


def test_label_chain_break_flag(tmp_path: Path):
    recs = _toy_records()
    # Drop the label at the final stage — a broken chain.
    del recs[2]["atoms"]["label_chain"]
    _write(tmp_path, recs)
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    )
    breaks = [f for f in res.flags if f.rule == "label_chain_break"]
    assert len(breaks) == 1
    assert "coord_space" in res.render.split("## Feature lineage")[0]


def test_feature_lineage_and_births(tmp_path: Path):
    _write(tmp_path, _toy_records())
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    )
    lin = {r.stage: r for r in res.feature_lineage}
    assert lin["load"].added == ["id", "name"]
    assert lin["join"].added == ["qty"]
    assert lin["join"].dropped == []
    assert res.feature_births == {"id": "load", "name": "load", "qty": "join"}


def test_sketch_view(tmp_path: Path):
    _write(tmp_path, _toy_records())
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    )
    cells = {(r.stage, r.column): r for r in res.sketch}
    assert cells[("load", "id")].mean == 5.0
    assert cells[("load", "name")].null_count == 1
    assert cells[("load", "name")].mean is None  # nulls-only column, no sketch
    assert cells[("join", "qty")].q50 == 3.0


# --- byte stability -----------------------------------------------------------


def test_render_is_byte_stable(tmp_path: Path):
    _write(tmp_path, _toy_records())
    spec = TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    a = trace_render(experiment_dir=tmp_path, spec=spec).render
    b = trace_render(experiment_dir=tmp_path, spec=spec).render
    assert a == b
    assert a.startswith("# Data trace — run:run-abc (task 0)")


# --- honest absence -----------------------------------------------------------


def test_absent_trace_is_honest_not_error(tmp_path: Path):
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="ghost")
    )
    assert res.present is False
    assert res.stage_count == 0
    assert res.trace_sha == ""
    assert "no trace recorded" in res.skipped
    assert "no trace recorded" in res.render


def test_unresolved_reference_lookup_is_honest(tmp_path: Path):
    res = trace_render(experiment_dir=tmp_path, spec=TraceRenderSpec(cmd_sha="deadbeef" * 8))
    assert res.present is False
    assert res.resolved_from == "cmd_sha"
    assert res.scope_id == ""
    assert "no run matches" in res.skipped


# --- the reference lookups ----------------------------------------------------


def _sidecar(experiment_dir: Path, run_id: str, *, cmd_sha: str, profile: str) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0",
        submitted_at=_TS,
        executor="exec.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha="tsha",
        profile=profile,
    )


def test_cmd_sha_reference_lookup(tmp_path: Path):
    cmd_sha = "a" * 64
    _sidecar(tmp_path, "run-xyz", cmd_sha=cmd_sha, profile="exp-a")
    _write(tmp_path, _toy_records(), sid="run-xyz")
    res = trace_render(experiment_dir=tmp_path, spec=TraceRenderSpec(cmd_sha=cmd_sha))
    assert res.present is True
    assert res.resolved_from == "cmd_sha"
    assert res.scope_id == "run-xyz"
    # The self-describing header carries the sidecar identity.
    assert res.header["cmd_sha"] == cmd_sha
    assert res.header["profile"] == "exp-a"


def test_profile_reference_lookup(tmp_path: Path):
    _sidecar(tmp_path, "run-xyz", cmd_sha="b" * 64, profile="exp-b")
    _write(tmp_path, _toy_records(), sid="run-xyz")
    res = trace_render(experiment_dir=tmp_path, spec=TraceRenderSpec(profile="exp-b"))
    assert res.present is True
    assert res.resolved_from == "profile"
    assert res.scope_id == "run-xyz"


# --- the never-judgment vocabulary pin ---------------------------------------

_BANNED = re.compile(r"\b(good|bad|wrong|suspicious|should)\b", re.IGNORECASE)


def test_render_carries_no_verdict_vocabulary(tmp_path: Path):
    # A flagged (non-conserved, broken-chain) trace — the render must still be
    # verdict-free: flags render as the records' own {rule, detail} text.
    recs = _toy_records()
    recs[1]["atoms"]["row_count"] = {"rows": 5, "dropped": 2}
    del recs[2]["atoms"]["label_chain"]
    _write(tmp_path, recs)
    res = trace_render(
        experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc")
    )
    assert not _BANNED.search(res.render), _BANNED.search(res.render)


def test_render_source_has_no_verdict_vocabulary():
    src = Path(tr.__file__).read_text(encoding="utf-8")
    hits = _BANNED.findall(src)
    assert not hits, f"verdict vocabulary in render source literals: {hits}"


# --- the pure (non-creating) posture -----------------------------------------


def test_query_is_non_creating(tmp_path: Path):
    """An absent scope must not create the store file — a query never writes."""
    trace_render(experiment_dir=tmp_path, spec=TraceRenderSpec(scope_kind="run", scope_id="ghost"))
    assert not (tmp_path / ".hpc" / "traces").exists()


def test_markdown_opt_out(tmp_path: Path):
    _write(tmp_path, _toy_records())
    res = trace_render(
        experiment_dir=tmp_path,
        spec=TraceRenderSpec(scope_kind="run", scope_id="run-abc", markdown=False),
    )
    assert res.render == ""
    assert res.waterfall  # structured views still populated


def test_spec_requires_exactly_one_selector():
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        TraceRenderSpec(scope_kind="run", scope_id="r", cmd_sha="x")
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        TraceRenderSpec()
    with pytest.raises(ValueError, match="together"):
        TraceRenderSpec(scope_kind="run")


def test_spec_invalid_scope_key_raises(tmp_path: Path):
    # A path-escaping scope_id trips the store-path guard (SpecInvalid).
    with pytest.raises(errors.SpecInvalid):
        trace_render(
            experiment_dir=tmp_path,
            spec=TraceRenderSpec(scope_kind="run", scope_id="../escape"),
        )

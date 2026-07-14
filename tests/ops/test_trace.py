"""Tests for the ``trace`` query verb — the derived execution DAG.

``trace`` joins the per-run journal records, the per-run sidecars, and the
signable provenance manifest into one replayable DAG. Properties pinned here:

* Campaign scope emits a ``campaign`` root + one ``run`` node per run, with
  ``member`` edges and a provenance fingerprint per run.
* The campaign ``signature`` equals the canonical ``provenance-manifest``
  signature for the same campaign (the two surfaces never drift).
* ``parent_run_ids`` become ``derived-from`` lineage edges; run scope walks
  that lineage transitively.
* ``wave`` nodes carry the live combined / failed / in_flight verdict from the
  journal record.
* ``flat`` format drops edges and wave nodes.
* Validation: exactly one selector is required; an unknown run_id is an error
  but an unknown campaign yields a well-formed empty DAG.
* A sidecar with no journal record (and vice versa) still surfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.provenance_manifest import write_provenance_manifest
from hpc_agent.ops.trace import TRACE_SCHEMA_VERSION, trace
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _sidecar(experiment_dir: Path, run_id: str, **overrides: object) -> None:
    kwargs: dict = dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
    )
    kwargs.update(overrides)
    write_run_sidecar(experiment_dir, **kwargs)


def _record(run_id: str, **overrides: object) -> RunRecord:
    base: dict = {
        "run_id": run_id,
        "profile": "p",
        "cluster": "hoffman2",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 2,
        "submitted_at": "2026-01-01T00:00:00Z",
        "experiment_dir": "/tmp/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


# --- node ids ----------------------------------------------------------------


def _ids(result: dict, kind: str) -> set[str]:
    return {n["id"] for n in result["nodes"] if n["kind"] == kind}


def _edges(result: dict, rel: str) -> set[tuple[str, str]]:
    return {(e["from"], e["to"]) for e in result["edges"] if e["rel"] == rel}


# --- campaign scope ----------------------------------------------------------


def test_campaign_scope_emits_root_run_nodes_and_member_edges(
    journal_home: Path, experiment: Path
) -> None:
    a, b = "20260101-000001-aaaaaaa", "20260101-000002-bbbbbbb"
    for rid in (a, b):
        _sidecar(experiment, rid, campaign_id="camp", data_sha="d" * 64, env_hash="e" * 64)
        upsert_run(experiment, _record(rid, campaign_id="camp"))

    result = trace(experiment_dir=experiment, campaign_id="camp")

    assert result["scope"] == "campaign"
    assert result["format"] == "dag"
    assert result["root"] == "campaign:camp"
    assert _ids(result, "campaign") == {"campaign:camp"}
    assert _ids(result, "run") == {f"run:{a}", f"run:{b}"}
    assert _edges(result, "member") == {
        ("campaign:camp", f"run:{a}"),
        ("campaign:camp", f"run:{b}"),
    }
    campaign_node = next(n for n in result["nodes"] if n["kind"] == "campaign")
    assert campaign_node["run_count"] == 2


def test_run_node_carries_provenance_and_lifecycle(journal_home: Path, experiment: Path) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(experiment, rid, campaign_id="camp", data_sha="d" * 64, env_hash="e" * 64)
    upsert_run(experiment, _record(rid, campaign_id="camp", status="complete"))

    result = trace(experiment_dir=experiment, campaign_id="camp")
    run_node = next(n for n in result["nodes"] if n["kind"] == "run")

    assert run_node["status"] == "complete"
    assert run_node["cluster"] == "hoffman2"
    assert run_node["provenance"]["cmd_sha"] == "0" * 64
    assert run_node["provenance"]["data_sha"] == "d" * 64
    assert run_node["provenance"]["env_hash"] == "e" * 64
    # The provenance projection excludes run_id (the node already carries it).
    assert "run_id" not in run_node["provenance"]


def test_signature_matches_provenance_manifest(journal_home: Path, experiment: Path) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(experiment, rid, campaign_id="camp")
    upsert_run(experiment, _record(rid, campaign_id="camp"))

    result = trace(experiment_dir=experiment, campaign_id="camp")
    _, written = write_provenance_manifest(experiment, "camp")
    assert result["signature"] == written["signature"]
    assert result["trace_schema_version"] == TRACE_SCHEMA_VERSION


def test_unknown_campaign_is_empty_dag_not_error(journal_home: Path, experiment: Path) -> None:
    result = trace(experiment_dir=experiment, campaign_id="nope")
    assert _ids(result, "run") == set()
    campaign_node = next(n for n in result["nodes"] if n["kind"] == "campaign")
    assert campaign_node["run_count"] == 0
    # An empty campaign still has a signable provenance signature.
    assert result["signature"]


# --- lineage -----------------------------------------------------------------


def test_parent_run_ids_become_derived_from_edges(journal_home: Path, experiment: Path) -> None:
    parent, child = "20260101-000001-aaaaaaa", "20260101-000002-bbbbbbb"
    _sidecar(experiment, parent, campaign_id="camp")
    _sidecar(experiment, child, campaign_id="camp", parent_run_ids=[parent])
    upsert_run(experiment, _record(parent, campaign_id="camp"))
    upsert_run(experiment, _record(child, campaign_id="camp"))

    result = trace(experiment_dir=experiment, campaign_id="camp")
    assert (f"run:{child}", f"run:{parent}") in _edges(result, "derived-from")


def test_run_scope_walks_transitive_lineage(journal_home: Path, experiment: Path) -> None:
    a, b, c = "20260101-000001-aaaaaaa", "20260101-000002-bbbbbbb", "20260101-000003-ccccccc"
    _sidecar(experiment, a)
    _sidecar(experiment, b, parent_run_ids=[a])
    _sidecar(experiment, c, parent_run_ids=[b])
    for rid in (a, b, c):
        upsert_run(experiment, _record(rid))

    result = trace(experiment_dir=experiment, run_id=c)
    assert result["scope"] == "run"
    assert result["root"] == f"run:{c}"
    assert result["signature"] is None  # lineage slice is not a signable campaign
    assert _ids(result, "run") == {f"run:{a}", f"run:{b}", f"run:{c}"}
    assert (f"run:{c}", f"run:{b}") in _edges(result, "derived-from")
    assert (f"run:{b}", f"run:{a}") in _edges(result, "derived-from")


# --- waves -------------------------------------------------------------------


def test_wave_nodes_carry_live_verdict(journal_home: Path, experiment: Path) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(
        experiment,
        rid,
        campaign_id="camp",
        wave_map={"0": [0, 1], "1": [2, 3], "2": [4, 5]},
    )
    upsert_run(
        experiment,
        _record(rid, campaign_id="camp", combined_waves=[0], failed_waves=[1]),
    )

    result = trace(experiment_dir=experiment, campaign_id="camp")
    waves = {n["wave"]: n for n in result["nodes"] if n["kind"] == "wave"}
    assert waves[0]["state"] == "combined"
    assert waves[1]["state"] == "failed"
    assert waves[2]["state"] == "in_flight"
    assert waves[0]["task_ids"] == [0, 1]
    assert (f"run:{rid}", f"wave:{rid}:0") in _edges(result, "contains")


# --- format ------------------------------------------------------------------


def test_flat_format_drops_edges_and_wave_nodes(journal_home: Path, experiment: Path) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(experiment, rid, campaign_id="camp", wave_map={"0": [0, 1]})
    upsert_run(experiment, _record(rid, campaign_id="camp"))

    result = trace(experiment_dir=experiment, campaign_id="camp", trace_format="flat")
    assert result["format"] == "flat"
    assert result["edges"] == []
    assert _ids(result, "wave") == set()
    assert _ids(result, "run") == {f"run:{rid}"}
    assert result["dot"] is None


def test_dot_format_renders_graphviz_over_the_full_dag(
    journal_home: Path, experiment: Path
) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(experiment, rid, campaign_id="camp", wave_map={"0": [0, 1]})
    upsert_run(experiment, _record(rid, campaign_id="camp", combined_waves=[0]))

    result = trace(experiment_dir=experiment, campaign_id="camp", trace_format="dot")
    # `dot` carries the same graph as `dag` (edges + wave nodes) ...
    assert result["format"] == "dot"
    assert _ids(result, "wave") == {f"wave:{rid}:0"}
    assert ("campaign:camp", f"run:{rid}") in _edges(result, "member")
    # ... plus a rendered Graphviz string referencing every node id.
    dot = result["dot"]
    assert dot.startswith("digraph hpc_trace {")
    assert dot.rstrip().endswith("}")
    assert '"campaign:camp"' in dot
    assert f'"run:{rid}"' in dot
    assert f'"wave:{rid}:0"' in dot
    assert "-> " in dot  # at least one edge rendered


def test_non_dot_formats_have_null_dot(journal_home: Path, experiment: Path) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(experiment, rid, campaign_id="camp")
    upsert_run(experiment, _record(rid, campaign_id="camp"))
    assert trace(experiment_dir=experiment, campaign_id="camp")["dot"] is None


# --- resilience: one surface present, the other absent -----------------------


def test_sidecar_without_journal_record_still_surfaces(
    journal_home: Path, experiment: Path
) -> None:
    rid = "20260101-000001-aaaaaaa"
    _sidecar(experiment, rid, campaign_id="camp")  # no upsert_run

    result = trace(experiment_dir=experiment, campaign_id="camp")
    run_node = next((n for n in result["nodes"] if n["kind"] == "run"), None)
    assert run_node is not None
    assert run_node["id"] == f"run:{rid}"
    assert run_node["status"] is None  # no journal record → null lifecycle
    assert run_node["provenance"]["cmd_sha"] == "0" * 64  # but sidecar provenance present


# --- validation --------------------------------------------------------------


def test_neither_selector_is_spec_invalid(journal_home: Path, experiment: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="exactly one"):
        trace(experiment_dir=experiment)


def test_both_selectors_is_spec_invalid(journal_home: Path, experiment: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="exactly one"):
        trace(experiment_dir=experiment, campaign_id="camp", run_id="r")


def test_unknown_run_id_is_spec_invalid(journal_home: Path, experiment: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="no journal record or sidecar"):
        trace(experiment_dir=experiment, run_id="20260101-999999-zzzzzzz")

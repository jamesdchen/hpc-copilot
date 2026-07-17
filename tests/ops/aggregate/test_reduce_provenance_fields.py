"""Reduce-time provenance: the table records WHICH runs' pieces fed it, at WHICH
cmd_sha(s), plus the wheel — read from the reduce's OWN inputs at write time.

Clean-reproduction-extraction Task 1 (gap G4a). ``_persist_local_aggregate`` used
to stamp ``provenance = {incomplete_waves, source, reduced_at}`` — the table kept
NO record of which runs' pieces it consumed, so a publication-time extract-recipe
walk (and a human doing archaeology) had to reconstruct the table→run-set link by
grepping the journal. These tests pin the three ADDITIVE fields
(``contributing_run_ids``, ``piece_cmd_shas``, ``hpc_agent_version``) sourced from
the membership the reduce actually consumed:

* the wave-partial ``run_id`` membership under ``_combiner/`` (F05-filtered), and
* the per-task ``.hpc_cmd_sha`` set under ``_per_task_results/`` (the run-13
  graft/stale-cache fingerprint — >1 distinct value discloses a repair re-run),

and that OLD-shape records (without the new keys) still parse byte-unchanged in
every existing reader (verify-reproduction).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.queries.verify_reproduction import VerifyReproductionSpec
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import (
    PER_TASK_CMD_SHA_FILENAME,
    PER_TASK_RESULTS_DIRNAME,
    _reduce_input_provenance,
    aggregate_flow,
)
from hpc_agent.ops.verify_reproduction import verify_reproduction
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

_RUN_ID = "20260623-120000-pi0"
_CMD_SHA = "a" * 64
_GRAFT_SHA = "b" * 64
_VERSION = "0.11.0+gdeadbeef"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_run(experiment: Path) -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=_RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/u/scratch/exp",
            job_name="p",
            job_ids=["12345678"],
            total_tasks=3,
            submitted_at="2026-06-23T12:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )


def _seed_sidecar(experiment: Path, run_id: str = _RUN_ID) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha=_CMD_SHA,
        hpc_agent_version=_VERSION,
        submitted_at="2026-06-23T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=3,
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
    )


def _mk_piece(mirror: Path, name: str, *, summary: str, cmd_sha: str | None) -> None:
    tdir = mirror / name
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / summary).write_text(json.dumps({"pi": 3.14, "n_samples": 1}), encoding="utf-8")
    if cmd_sha is not None:
        (tdir / PER_TASK_CMD_SHA_FILENAME).write_text(cmd_sha, encoding="utf-8")


# --------------------------------------------------------------------------- #
# The helper: what membership it reads, from the reduce's own on-disk inputs
# --------------------------------------------------------------------------- #
def test_helper_combiner_path_reads_wave_run_ids_and_sidecar_sha(journal_home, experiment):
    """Combiner path (wave partials, no per-task mirror): contributing_run_ids is
    this run's own wave membership (a FOREIGN partial is excluded exactly as
    reduce_partials excludes it); piece_cmd_shas falls back to the sidecar cmd_sha
    (wave files carry no per-piece sha); version comes from the sidecar."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    out = experiment / "_aggregated" / _RUN_ID
    combiner = out / "_combiner"
    combiner.mkdir(parents=True)
    (combiner / "wave_0.json").write_text(
        json.dumps({"run_id": _RUN_ID, "grid_points": {"g0": {"pi": 3.14}}}), encoding="utf-8"
    )
    # A foreign run's leftover partial at the shared _combiner/ (F05) — must NOT
    # be counted as contributing.
    (combiner / "wave_9.json").write_text(
        json.dumps({"run_id": "some-other-run", "grid_points": {"g9": {"pi": 9.0}}}),
        encoding="utf-8",
    )

    run_ids, piece_shas, version = _reduce_input_provenance(
        experiment, _RUN_ID, out, summary_name="metrics.json"
    )

    assert run_ids == [_RUN_ID]  # foreign partial excluded
    assert piece_shas == [_CMD_SHA]  # sidecar fallback (no per-task mirror)
    assert version == _VERSION


def test_helper_per_task_graft_records_every_consumed_cmd_sha(journal_home, experiment):
    """Per-task fallback: piece_cmd_shas is the EXACT distinct .hpc_cmd_sha set
    across the consumed pieces. A graft that re-ran one arm under a NEW cmd_sha
    leaves TWO shas in the tree — both are recorded, the run-13 fingerprint."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    out = experiment / "_aggregated" / _RUN_ID
    mirror = out / PER_TASK_RESULTS_DIRNAME
    _mk_piece(mirror, "task-0", summary="metrics.json", cmd_sha=_CMD_SHA)
    _mk_piece(mirror, "task-1", summary="metrics.json", cmd_sha=_CMD_SHA)
    _mk_piece(mirror, "task-2", summary="metrics.json", cmd_sha=_GRAFT_SHA)  # grafted

    run_ids, piece_shas, version = _reduce_input_provenance(
        experiment, _RUN_ID, out, summary_name="metrics.json"
    )

    assert run_ids == [_RUN_ID]
    assert piece_shas == sorted({_CMD_SHA, _GRAFT_SHA})  # both, distinct
    assert version == _VERSION


def test_helper_excludes_canary_family_pieces(journal_home, experiment):
    """A canary-family sibling's piece shares the mirror subtree but is excluded
    from the reduce — so its cmd_sha never contaminates piece_cmd_shas."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    out = experiment / "_aggregated" / _RUN_ID
    mirror = out / PER_TASK_RESULTS_DIRNAME
    _mk_piece(mirror, "task-0", summary="metrics.json", cmd_sha=_CMD_SHA)
    # The double-canary sibling writes under a path carrying its own run_id.
    _mk_piece(
        mirror / f"{_RUN_ID}-canary2",
        "task-0",
        summary="metrics.json",
        cmd_sha="c" * 64,
    )

    _run_ids, piece_shas, _version = _reduce_input_provenance(
        experiment, _RUN_ID, out, summary_name="metrics.json"
    )

    assert piece_shas == [_CMD_SHA]  # the canary's "cccc…" sha is excluded


def test_helper_degrades_without_sidecar(journal_home, experiment):
    """No sidecar / no inputs: the honest defaults — [run_id], [], None — never a
    raise (best-effort harvest-guard write)."""
    out = experiment / "_aggregated" / _RUN_ID
    out.mkdir(parents=True)
    run_ids, piece_shas, version = _reduce_input_provenance(
        experiment, _RUN_ID, out, summary_name="metrics.json"
    )
    assert run_ids == [_RUN_ID]
    assert piece_shas == []
    assert version is None


# --------------------------------------------------------------------------- #
# Through the flow: the persisted table carries the fields
# --------------------------------------------------------------------------- #
def _rsync_combiner_ok(*_a, remote_subdir: str, local_dir: str, **_kw):
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_combiner_flow_persists_reduce_provenance(journal_home, experiment, monkeypatch):
    """End-to-end (combiner-only default): the persisted metrics_aggregate.json
    carries all three additive fields alongside the unchanged legacy keys."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    fixed = {"g0": {"pi_estimate": 3.1415, "n_samples": 5}}
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_combiner_ok)
    monkeypatch.setattr(af_module, "reduce_partials", lambda _dir, **_kw: fixed)
    monkeypatch.setattr(af_module, "collect_wave_errors", lambda _dir, **_kw: [])

    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    data = json.loads(
        (experiment / "_aggregated" / _RUN_ID / "metrics_aggregate.json").read_text("utf-8")
    )
    prov = data["provenance"]
    # Legacy keys unchanged.
    assert prov["source"] == "local_reduce"
    assert prov["incomplete_waves"] == []
    assert isinstance(prov["reduced_at"], str)
    # New additive keys.
    assert prov["contributing_run_ids"] == [_RUN_ID]
    assert prov["piece_cmd_shas"] == [_CMD_SHA]
    assert prov["hpc_agent_version"] == _VERSION


# --------------------------------------------------------------------------- #
# Back-compat: OLD-shape records still parse byte-unchanged in every reader
# --------------------------------------------------------------------------- #
def _agg_path(experiment: Path, run_id: str) -> Path:
    return experiment / "_aggregated" / run_id / "metrics_aggregate.json"


def _write_aggregate(path: Path, *, provenance: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "aggregated_metrics": {"gp": {"pi": 3.14159, "n_samples": 1000}},
                "provenance": provenance,
            }
        ),
        encoding="utf-8",
    )


def test_old_shape_record_still_parses_in_verify_reproduction(journal_home, experiment):
    """An OLD-shape table (only the three legacy provenance keys) is read
    byte-identically by verify-reproduction — the new keys are optional, and the
    comparator only ever touches ``aggregated_metrics``."""
    orig, repro = "orig-run", "repro-run"
    for rid in (orig, repro):
        _seed_run_named(experiment, rid)
    _seed_sidecar_named(experiment, orig)
    _seed_sidecar_named(experiment, repro, reproduces=orig)
    old_prov = {
        "incomplete_waves": [],
        "source": "local_reduce",
        "reduced_at": "2026-06-23T12:00:00Z",
    }
    _write_aggregate(_agg_path(experiment, orig), provenance=old_prov)
    _write_aggregate(_agg_path(experiment, repro), provenance=old_prov)

    res = verify_reproduction(
        experiment, spec=VerifyReproductionSpec(original_run_id=orig, repro_run_id=repro)
    )
    assert res.stage_reached == "match"


def test_new_shape_record_reads_identically_in_verify_reproduction(journal_home, experiment):
    """A NEW-shape table (extra provenance keys present) yields the SAME
    verify-reproduction verdict — the reader ignores the additive fields."""
    orig, repro = "orig-run", "repro-run"
    for rid in (orig, repro):
        _seed_run_named(experiment, rid)
    _seed_sidecar_named(experiment, orig)
    _seed_sidecar_named(experiment, repro, reproduces=orig)
    new_prov = {
        "incomplete_waves": [],
        "source": "local_reduce",
        "reduced_at": "2026-06-23T12:00:00Z",
        "contributing_run_ids": [orig],
        "piece_cmd_shas": [_CMD_SHA],
        "hpc_agent_version": _VERSION,
    }
    _write_aggregate(_agg_path(experiment, orig), provenance=new_prov)
    _write_aggregate(_agg_path(experiment, repro), provenance=new_prov)

    res = verify_reproduction(
        experiment, spec=VerifyReproductionSpec(original_run_id=orig, repro_run_id=repro)
    )
    assert res.stage_reached == "match"


def _seed_run_named(experiment: Path, run_id: str) -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/u/scratch/exp",
            job_name="p",
            job_ids=["12345678"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )


def _seed_sidecar_named(experiment: Path, run_id: str, *, reproduces: str | None = None) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha=_CMD_SHA,
        hpc_agent_version=_VERSION,
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=1,
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
        reproduces=reproduces,
    )

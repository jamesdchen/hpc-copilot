"""Cluster-side FINAL cross-wave reduce in the combiner (#254).

The combiner's ``--final`` mode merges every ``_combiner/wave_*.json`` into a
single ``_aggregated/<run_id>/metrics_aggregate.json`` ON THE CLUSTER, so the
local side pulls one file instead of hundreds. Its ``aggregated_metrics`` must
match the local pull-all-then-``reduce_partials`` path byte-for-byte.
"""

from __future__ import annotations

import json

import pytest

from hpc_agent.execution.mapreduce import combiner
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_partials


@pytest.fixture(autouse=True)
def _legacy_pull_path(monkeypatch):
    """These tests pin REDUCE POLICY, not transport: they mock ``rsync_pull``.
    After the O2 pull-parity merge the adapter prefers ``tar_ssh_pull`` when
    importable, which would route around the mocks into the hermetic guard —
    pin the legacy path (transport behavior is covered by the O2 transport
    suite + the tar-seam tests)."""
    monkeypatch.setenv("HPC_AGGREGATE_TAR_PULL", "0")


def _write_wave(combiner_dir, wave, grid_points, errors=None):
    combiner_dir.mkdir(parents=True, exist_ok=True)
    (combiner_dir / f"wave_{wave}.json").write_text(
        json.dumps(
            {
                "wave": wave,
                "run_id": "r1",
                "task_ids": [],
                "grid_points": grid_points,
                "errors": errors or [],
            }
        ),
        encoding="utf-8",
    )


def _aggregate(tmp_path):
    return json.loads(
        (tmp_path / "_aggregated" / "r1" / "metrics_aggregate.json").read_text(encoding="utf-8")
    )


def test_final_reduce_matches_local_reduce_partials(tmp_path, monkeypatch):
    combiner_dir = tmp_path / "_combiner"
    _write_wave(
        combiner_dir,
        0,
        {"a": {"acc": 0.8, "n_samples": 2}, "b": {"acc": 0.5, "n_samples": 1}},
    )
    _write_wave(combiner_dir, 1, {"a": {"acc": 0.9, "n_samples": 3}})
    # A third wave with a non-overlapping grid point to exercise the merge.
    _write_wave(combiner_dir, 2, {"c": {"acc": 0.1, "n_samples": 5}})

    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])

    agg = _aggregate(tmp_path)
    # The producer location moved cluster-side, but the NUMBERS are identical to
    # the old pull-every-wave-then-reduce-locally path.
    assert agg["aggregated_metrics"] == reduce_partials(combiner_dir)
    assert agg["run_id"] == "r1"
    assert agg["waves"] == [0, 1, 2]
    assert agg["provenance"]["wave_count"] == 3
    assert agg["manifest"]["wave_files"] == [
        "_combiner/wave_0.json",
        "_combiner/wave_1.json",
        "_combiner/wave_2.json",
    ]


def test_runtime_sidecars_are_not_treated_as_wave_partials(tmp_path, monkeypatch):
    combiner_dir = tmp_path / "_combiner"
    _write_wave(combiner_dir, 0, {"a": {"acc": 1.0, "n_samples": 1}})
    # A runtime sidecar must NOT be folded into the reduce.
    (combiner_dir / "wave_0.runtime.json").write_text(
        json.dumps({"wave": 0, "samples": []}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])
    agg = _aggregate(tmp_path)
    assert agg["waves"] == [0]
    assert agg["manifest"]["wave_files"] == ["_combiner/wave_0.json"]


def test_final_reduce_records_incomplete_waves(tmp_path, monkeypatch):
    combiner_dir = tmp_path / "_combiner"
    _write_wave(combiner_dir, 0, {"a": {"acc": 1.0}}, errors=["task 5: metrics.json not found"])
    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])
    agg = _aggregate(tmp_path)
    assert agg["provenance"]["incomplete_waves"] == [0]
    assert "0" in agg["provenance"]["errors_per_wave"]


def test_final_reduce_no_partials_exits_1(tmp_path, monkeypatch):
    (tmp_path / "_combiner").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        combiner.main(argv=["--final", "--run-id", "r1"])
    assert exc.value.code == 1


def test_final_reduce_refuses_overwrite_without_force(tmp_path, monkeypatch):
    combiner_dir = tmp_path / "_combiner"
    _write_wave(combiner_dir, 0, {"a": {"x": 1.0}})
    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])
    with pytest.raises(SystemExit):
        combiner.main(argv=["--final", "--run-id", "r1"])
    # --force overwrites cleanly.
    combiner.main(argv=["--final", "--run-id", "r1", "--force"])


def test_final_reduce_requires_run_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HPC_RUN_ID", raising=False)
    with pytest.raises(SystemExit):
        combiner.main(argv=["--final"])


# --- aggregate_flow opt-in wiring (HPC_CLUSTER_FINAL_REDUCE=1) ---------------


# --- G4a cluster leg: the --final footer mirrors the reduce-time provenance ----


def _write_scoped_wave(combiner_root, run_id, wave, grid_points, *, errors=None):
    """A RUN-SCOPED partial ``_combiner/<run_id>/wave_<N>.json`` (BR-9 layout)."""
    scoped = combiner_root / run_id
    scoped.mkdir(parents=True, exist_ok=True)
    (scoped / f"wave_{wave}.json").write_text(
        json.dumps(
            {
                "wave": wave,
                "run_id": run_id,
                "task_ids": [],
                "grid_points": grid_points,
                "errors": errors or [],
            }
        ),
        encoding="utf-8",
    )


def _write_deployed_sidecar(tmp_path, run_id, *, cmd_sha, version):
    """The DEPLOYED per-run sidecar the cluster combiner reads at ``.hpc/runs/<id>.json``
    (cwd-relative under the remote project root the final reduce runs in)."""
    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(
        json.dumps({"cmd_sha": cmd_sha, "hpc_agent_version": version}),
        encoding="utf-8",
    )


def test_final_reduce_mirrors_reduce_time_provenance(tmp_path, monkeypatch):
    """G4a cluster leg: the cluster ``--final`` footer carries the SAME three
    reduce-time provenance fields the LOCAL ``_reduce_input_provenance`` stamps —
    ``contributing_run_ids`` from the consumed run-scoped partials, ``piece_cmd_shas``
    from the run's sidecar cmd_sha (the combiner pre-reduces cluster-side; wave
    partials carry no per-piece sha), and ``hpc_agent_version`` from the sidecar —
    plus ``source="cluster_final"`` so a reader can disclose which engine reduced it."""
    _write_scoped_wave(tmp_path / "_combiner", "r1", 0, {"a": {"acc": 0.8, "n_samples": 2}})
    _write_scoped_wave(tmp_path / "_combiner", "r1", 1, {"a": {"acc": 0.9, "n_samples": 3}})
    _write_deployed_sidecar(tmp_path, "r1", cmd_sha="a" * 64, version="0.11.0+gdeadbeef")

    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])

    prov = _aggregate(tmp_path)["provenance"]
    assert prov["source"] == "cluster_final"
    assert prov["contributing_run_ids"] == ["r1"]
    assert prov["piece_cmd_shas"] == ["a" * 64]
    assert prov["hpc_agent_version"] == "0.11.0+gdeadbeef"
    # Legacy footer keys are unchanged (additive only).
    assert prov["wave_count"] == 2
    assert prov["incomplete_waves"] == []


def test_final_reduce_provenance_degrades_without_sidecar(tmp_path, monkeypatch):
    """No deployed sidecar (an old tree, or a run whose sidecar wasn't deployed):
    ``contributing_run_ids`` still records the consumed partials, while
    ``piece_cmd_shas``/``hpc_agent_version`` degrade to ``[]``/``None`` — best-effort,
    never failing the reduce (the final reduce needs only the partials)."""
    _write_scoped_wave(tmp_path / "_combiner", "r1", 0, {"a": {"x": 1.0, "n_samples": 1}})
    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])
    prov = _aggregate(tmp_path)["provenance"]
    assert prov["source"] == "cluster_final"
    assert prov["contributing_run_ids"] == ["r1"]
    assert prov["piece_cmd_shas"] == []
    assert prov["hpc_agent_version"] is None


def test_final_reduce_graft_partial_waves_records_exactly_consumed(tmp_path, monkeypatch):
    """A graft/partial-wave cluster reduce records EXACTLY the run-scoped membership
    it consumed: ``contributing_run_ids`` is this run's own set (a foreign leftover
    at the shared ``_combiner/`` is dropped, F05), and ``waves`` names only the
    partials that were present."""
    root = tmp_path / "_combiner"
    _write_scoped_wave(root, "r1", 0, {"a": {"acc": 0.8, "n_samples": 1}})
    # Wave 1 grafted-away / never re-combined — absent; wave 2 present.
    _write_scoped_wave(root, "r1", 2, {"b": {"acc": 0.6, "n_samples": 1}})
    # A foreign run's leftover legacy-flat partial at the shared _combiner/ (F05).
    root.mkdir(parents=True, exist_ok=True)
    (root / "wave_9.json").write_text(
        json.dumps(
            {"wave": 9, "run_id": "other-run", "grid_points": {"z": {"acc": 9.0, "n_samples": 1}}}
        ),
        encoding="utf-8",
    )
    _write_deployed_sidecar(tmp_path, "r1", cmd_sha="c" * 64, version="0.11.0")

    monkeypatch.chdir(tmp_path)
    combiner.main(argv=["--final", "--run-id", "r1"])

    agg = _aggregate(tmp_path)
    assert agg["waves"] == [0, 2]  # exactly the consumed run-scoped partials
    assert agg["provenance"]["contributing_run_ids"] == ["r1"]  # foreign 'other-run' excluded
    assert agg["provenance"]["skipped_foreign_waves"] == [9]
    assert agg["provenance"]["piece_cmd_shas"] == ["c" * 64]


def test_cluster_final_reduce_pulls_only_the_aggregate(tmp_path, monkeypatch):
    """The opt-in path runs the cluster reduce and pulls ONLY metrics_aggregate.json."""
    from pathlib import Path
    from types import SimpleNamespace

    from hpc_agent.ops import aggregate_flow as af

    record = SimpleNamespace(ssh_target="u@c", remote_path="/p")
    out = tmp_path / "out"
    calls: dict[str, object] = {}

    def _fake_final_reduce(*, ssh_target, remote_path, run_id, force, remote_activation):
        calls["final"] = (ssh_target, remote_path, run_id, force)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _fake_pull(*, ssh_target, remote_path, remote_subdir, local_dir, include=None, **_kw):
        calls["pull"] = {"remote_subdir": remote_subdir, "include": include}
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "metrics_aggregate.json").write_text(
            json.dumps(
                {
                    "run_id": "r1",
                    "aggregated_metrics": {"a": {"acc": 0.86}},
                    "provenance": {"incomplete_waves": [2]},
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _fake_final_reduce)
    monkeypatch.setattr(af, "rsync_pull", _fake_pull)

    aggregated, incomplete, foreign = af._cluster_final_reduce(
        tmp_path, "r1", record=record, out=out
    )

    assert aggregated == {"a": {"acc": 0.86}}
    assert incomplete == [2]
    # No cross-run clobber in this fixture (provenance has no
    # skipped_foreign_waves) → the third return is empty (B1).
    assert foreign == []
    # Single aggregate pull, NOT the wave_*.json tree.
    assert calls["pull"] == {
        "remote_subdir": "_aggregated/r1",
        "include": ["metrics_aggregate.json"],
    }
    assert calls["final"][3] is True  # force=True (idempotent refresh)

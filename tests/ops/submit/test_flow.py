"""Unit tests for the cluster-config / NFS-staging resolution branch in
``hpc_agent.ops.submit_flow``.

These don't exercise the full submit pipeline (which needs a live
cluster). They isolate the small bit of logic that decides whether
``$HPC_NFS_DATA_DIR`` gets injected into the job env, since that
branch is the one B-M3 fixes — a malformed clusters.yaml entry
previously zeroed out the entire cluster config (cold_start_mem_buffer,
scheduler routing, ...) instead of just the malformed field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent.infra.clusters import get_nfs_data_dir
from hpc_agent.state.journal import upsert_run


def _resolve_nfs_dir_for_cluster(cluster: str, full_clusters: dict[str, Any]):
    """Mirror the resolution logic in submit_flow.submit_flow().

    Kept here as the unit-test seam: scope the try/except to the
    get_nfs_data_dir call only, so load_clusters_config errors bubble
    up but a malformed nfs_data_dir field survives gracefully with
    the rest of the cluster config still intact.
    """
    cluster_cfg = full_clusters.get(cluster, {})
    try:
        nfs_dir = get_nfs_data_dir(cluster_cfg) if cluster_cfg else None
    except (errors.SpecInvalid, TypeError):
        nfs_dir = None
    return cluster_cfg, nfs_dir


class TestNfsDataDirResolution:
    def test_caller_supplied_job_env_overrides_cluster_config(self) -> None:
        """The submit_flow contract: caller's job_env['HPC_NFS_DATA_DIR']
        wins over the cluster yaml entry via ``setdefault``."""
        # Mirror the submit_flow setdefault pattern.
        job_env = {"HPC_NFS_DATA_DIR": "/per_experiment/dataset_v2"}
        clusters = {"hoffman2": {"nfs_data_dir": "/cluster_default/dataset"}}

        _, nfs_from_cluster = _resolve_nfs_dir_for_cluster("hoffman2", clusters)
        assert nfs_from_cluster == "/cluster_default/dataset"

        # Caller-set value must win.
        job_env.setdefault("HPC_NFS_DATA_DIR", nfs_from_cluster)
        assert job_env["HPC_NFS_DATA_DIR"] == "/per_experiment/dataset_v2"

    def test_malformed_nfs_data_dir_does_not_swallow_other_cluster_fields(
        self,
    ) -> None:
        """B-M3: nfs_data_dir='' is malformed (validator raises
        ValueError). The rest of the cluster's config (e.g.
        cold_start_mem_buffer, scheduler) MUST still be visible to
        downstream callers; previously the broad try/except erased
        the whole cluster_cfg and the campus user's submission silently
        dropped its planner inputs."""
        clusters = {
            "hoffman2": {
                "nfs_data_dir": "",  # malformed: empty string
                "scheduler": "sge",
                "cold_start_mem_buffer": 0.20,
            }
        }
        cluster_cfg, nfs_dir = _resolve_nfs_dir_for_cluster("hoffman2", clusters)
        # Malformed field swallowed → no NFS staging.
        assert nfs_dir is None
        # OTHER fields preserved — this is the bug that B-M3 fixes.
        assert cluster_cfg["scheduler"] == "sge"
        assert cluster_cfg["cold_start_mem_buffer"] == 0.20

    def test_unknown_cluster_yields_empty_cfg_and_no_nfs_dir(self) -> None:
        """An unrecognised cluster name is a no-op; staging is opt-in."""
        clusters = {"hoffman2": {"nfs_data_dir": "/data"}}
        cluster_cfg, nfs_dir = _resolve_nfs_dir_for_cluster("unknown", clusters)
        assert cluster_cfg == {}
        assert nfs_dir is None

    def test_well_formed_nfs_data_dir_is_returned(self) -> None:
        clusters = {"carc": {"nfs_data_dir": "/staging/imagenet"}}
        cluster_cfg, nfs_dir = _resolve_nfs_dir_for_cluster("carc", clusters)
        assert nfs_dir == "/staging/imagenet"
        assert cluster_cfg["nfs_data_dir"] == "/staging/imagenet"


class TestLoadClustersConfigBubblesUp:
    """Errors loading the YAML itself MUST propagate. A malformed
    clusters.yaml is a configuration bug the user needs to know about
    — silently submitting without cluster routing would land the run
    in an unexpected partition and surprise the user."""

    def test_load_error_propagates_through_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point HPC_CLUSTERS_CONFIG at a path that doesn't exist; the
        # loader must raise FileNotFoundError, NOT swallow it.
        bogus = tmp_path / "does_not_exist.yaml"
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(bogus))
        from hpc_agent.infra.clusters import load_clusters_config

        with pytest.raises(FileNotFoundError):
            load_clusters_config()

    def test_spec_invalid_from_get_nfs_data_dir_does_not_propagate(self) -> None:
        """Cross-check the validator's behavior matches the resolver:
        empty-string nfs_data_dir raises ``errors.SpecInvalid``; the
        resolver catches that single path and falls back to None."""
        with pytest.raises(errors.SpecInvalid):
            get_nfs_data_dir({"nfs_data_dir": ""})


# ---------------------------------------------------------------------------
# submit_flow_batch — N specs sharing one (ssh_target, remote_path)
# ---------------------------------------------------------------------------


def _spec(run_id: str, **overrides: Any):
    """Build a :class:`SubmitFlowSpec` with sensible defaults; overrides win."""
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    base = dict(
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/r",
        job_name=run_id,
        run_id=run_id,
        total_tasks=4,
        backend="sge",
        script="run.sh",
        job_env={},
        canary=False,
    )
    base.update(overrides)
    return SubmitFlowSpec(**base)  # type: ignore[arg-type]


def _batch(specs, **overrides: Any):
    """Wrap a list of :class:`SubmitFlowSpec` in a :class:`SubmitFlowBatchSpec`.

    The pipeline only consumes ``SubmitFlowBatchSpec`` now; this keeps
    the per-test boilerplate small.
    """
    from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec

    return SubmitFlowBatchSpec(specs=specs, **overrides)


@pytest.fixture
def _journal_home(tmp_path, monkeypatch):
    """Redirect ~/.claude/hpc/ to tmp_path so journal writes don't pollute home."""
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)


class TestSubmitFlowBatch:
    def test_heterogeneous_targets_raise_spec_invalid(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        from hpc_agent import errors
        from hpc_agent.ops.submit_flow import submit_flow_batch

        a = _spec("r1", ssh_target="u@a", remote_path="/p")
        b = _spec("r2", ssh_target="u@b", remote_path="/p")
        with pytest.raises(errors.SpecInvalid, match="distinct combinations"):
            submit_flow_batch(tmp_path, spec=_batch([a, b]))

    def test_shares_one_rsync_and_one_deploy_across_n_specs(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """The whole point of the batch: rsync + deploy fire once, qsub fires N."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        specs = [_spec(f"r{i}") for i in range(5)]
        with (
            mock.patch.object(sf_module, "_preflight_probe") as preflight,
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id,
                job_ids=[f"job_{spec.run_id}"],
                total_tasks=spec.total_tasks,
                deduped=False,
                canary_done=False,
            )
            results = submit_flow_batch(tmp_path, spec=_batch(specs))

        assert preflight.call_count == 1
        assert push_deploy.call_count == 1
        assert submit_one.call_count == 5
        assert [r.run_id for r in results] == ["r0", "r1", "r2", "r3", "r4"]
        assert all(not r.deduped for r in results)

    def test_skips_prelude_when_every_spec_already_journaled(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """If every spec is already on the journal, NO ssh / rsync runs."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.run_record import RunRecord

        # Seed the journal with both run_ids.
        for rid in ("r0", "r1"):
            rec = RunRecord(
                run_id=rid,
                profile="p",
                cluster="c",
                ssh_target="user@host",
                remote_path="/r",
                job_name=rid,
                job_ids=[f"prior_{rid}"],
                total_tasks=4,
                submitted_at="2026-01-01T00:00:00+00:00",
                experiment_dir=str(tmp_path.resolve()),
            )
            upsert_run(tmp_path, rec)

        specs = [_spec("r0"), _spec("r1")]
        with (
            mock.patch.object(sf_module, "_preflight_probe") as preflight,
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            results = submit_flow_batch(tmp_path, spec=_batch(specs))

        assert preflight.call_count == 0
        assert push_deploy.call_count == 0
        assert submit_one.call_count == 0
        assert all(r.deduped for r in results)
        assert [r.run_id for r in results] == ["r0", "r1"]

    def test_auto_prunes_orphan_sidecars_at_start(self, tmp_path: Any, _journal_home: Any) -> None:
        """Half-baked sidecars from a prior failed batch are silently swept
        before the next batch starts — no manual /prune-orphan-sidecars call."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch
        from hpc_agent.state.runs import run_sidecar_path, write_run_sidecar

        # Seed a half-baked sidecar (no job_ids, no journal record).
        orphan_id = "20260101-000000-orphan01"
        write_run_sidecar(
            tmp_path,
            run_id=orphan_id,
            cmd_sha="0" * 64,
            hpc_agent_version="0.2.0",
            submitted_at="2026-01-01T00:00:00Z",
            executor="python3 src/run.py",
            result_dir_template="results/{seed}",
            task_count=4,
            tasks_py_sha="1" * 64,
        )
        assert run_sidecar_path(tmp_path, orphan_id).is_file()

        # Run the next batch with a fresh spec.
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id,
                job_ids=[f"job_{spec.run_id}"],
                total_tasks=spec.total_tasks,
                deduped=False,
                canary_done=False,
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r_new")]))

        # The orphan sidecar is gone.
        assert not run_sidecar_path(tmp_path, orphan_id).is_file()

    def test_partial_dedup_only_fresh_specs_run(self, tmp_path: Any, _journal_home: Any) -> None:
        """Half the specs are already journaled — only the fresh ones get qsubbed."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch
        from hpc_agent.state.run_record import RunRecord

        upsert_run(
            tmp_path,
            RunRecord(
                run_id="r0",
                profile="p",
                cluster="c",
                ssh_target="user@host",
                remote_path="/r",
                job_name="r0",
                job_ids=["already"],
                total_tasks=4,
                submitted_at="2026-01-01T00:00:00+00:00",
                experiment_dir=str(tmp_path.resolve()),
            ),
        )
        specs = [_spec("r0"), _spec("r1"), _spec("r2")]
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id,
                job_ids=[f"new_{spec.run_id}"],
                total_tasks=spec.total_tasks,
                deduped=False,
                canary_done=False,
            )
            results = submit_flow_batch(tmp_path, spec=_batch(specs))

        assert push_deploy.call_count == 1  # still ONE rsync+deploy for the fresh subset
        assert submit_one.call_count == 2  # only r1 + r2 get qsubbed
        assert results[0].deduped is True
        assert results[0].job_ids == ["already"]
        assert results[1].deduped is False and results[1].job_ids == ["new_r1"]
        assert results[2].deduped is False and results[2].job_ids == ["new_r2"]


class TestKeepGeneratedShippable:
    """submit-flow carves generated-but-needed paths back out of the rsync
    excludes — the carve-out the submit worker prompt used to do by hand."""

    def test_drops_generated_shippable_paths(self) -> None:
        from hpc_agent.ops.submit_flow import _keep_generated_shippable

        excludes = [
            "__pycache__/",
            "src/",
            ".hpc/tasks.py",
            ".hpc/cli.py",
            ".hpc/.build-cache.json",
            "results/",
        ]
        kept = _keep_generated_shippable(excludes)
        # The generated package + dispatch files must ship — no longer excluded.
        assert kept is not None
        assert "src/" not in kept
        assert ".hpc/tasks.py" not in kept
        assert ".hpc/cli.py" not in kept
        # ...but the local-only build cache and everything else stay excluded.
        assert ".hpc/.build-cache.json" in kept
        assert "__pycache__/" in kept
        assert "results/" in kept

    def test_normalises_surrounding_slashes(self) -> None:
        from hpc_agent.ops.submit_flow import _keep_generated_shippable

        # A leading or trailing slash on the .gitignore pattern still matches.
        assert _keep_generated_shippable(["/src/", "src"]) == []

    def test_none_and_empty_pass_through(self) -> None:
        from hpc_agent.ops.submit_flow import _keep_generated_shippable

        assert _keep_generated_shippable(None) is None
        assert _keep_generated_shippable([]) == []

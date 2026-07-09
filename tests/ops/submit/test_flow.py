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


@pytest.fixture(autouse=True)
def _stub_executor_existence_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the post-deploy executor-existence preflight (#S5 / incident 6).

    It issues a real SSH ``test -f "$REPO_DIR/<executor>"`` and is covered
    directly in ``tests/incorporation/build/test_deployment_consistency.py``.
    These flow tests mock deploy at the function level and don't set the probe
    up, so stub it here (mirrors how they stub ``_preflight_probe``)."""
    from hpc_agent.ops import submit_flow as _sf

    monkeypatch.setattr(_sf, "_run_executor_existence_preflight", lambda **_k: None)


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
        # A real per-task executor so submit-flow can synthesize a valid sidecar
        # when Step 6d was skipped. (#162: it must REFUSE to synthesize one whose
        # executor is the dispatcher command — covered in TestSidecarGuarantee.)
        job_env={"EXECUTOR": "python run.py"},
        canary=False,
        # submit-flow guarantees the per-run sidecar at rsync time; a
        # result_dir_template + a real executor let it synthesize one when
        # Step 6d was skipped (these tests submit without pre-writing a sidecar).
        result_dir_template="results/{run_id}/task_{task_id}",
        # These tests exercise stamping/batching mechanics with FAKE executors
        # that were never meant to run; the pre-stage task-0 smoke (queue item
        # 7) would execute them and refuse on their nonzero exit. Opt out —
        # the smoke has its own dedicated suite (test_submit_flow_pre_stage_smoke).
        pre_stage_smoke=False,
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

    def test_runtime_uv_check_passes_when_uv_on_path(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """When HPC_RUNTIME=uv and the activation+`command -v uv` probe
        succeeds, the canary qsub proceeds without raising."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        job_env = {
            "EXECUTOR": "python run.py",
            "HPC_RUNTIME": "uv",
            "CONDA_SOURCE": "/opt/conda/etc/profile.d/conda.sh",
            "CONDA_ENV": "hpc-pi",
            "MODULES": "",
        }
        specs = [_spec(f"r{i}", job_env=job_env) for i in range(2)]
        ok_probe = mock.Mock(returncode=0, stdout="/opt/conda/envs/hpc-pi/bin/uv\n", stderr="")
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            # The probe lives in infra.runtime_preflight now (#275); patch its ssh_run.
            mock.patch("hpc_agent.infra.runtime_preflight.ssh_run", return_value=ok_probe) as ssh,
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
            submit_flow_batch(tmp_path, spec=_batch(specs))

        # ssh_run was called once for the runtime probe (first uv-runtime
        # spec wins); the rest of the batch reuses the verdict.
        assert ssh.call_count == 1
        cmd = ssh.call_args.args[0]
        assert "source /opt/conda/etc/profile.d/conda.sh" in cmd
        assert "conda activate hpc-pi" in cmd
        assert "command -v uv" in cmd

    def test_runtime_uv_check_raises_when_uv_missing(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """When HPC_RUNTIME=uv but the cluster env doesn't have uv on PATH,
        preflight raises SpecInvalid BEFORE any qsub — turning "all 100 tasks
        fail at runtime" into one clear error with an actionable remediation."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch

        job_env = {
            "EXECUTOR": "python run.py",
            "HPC_RUNTIME": "uv",
            "CONDA_SOURCE": "/opt/conda/etc/profile.d/conda.sh",
            "CONDA_ENV": "hpc-pi",
            "MODULES": "",
        }
        specs = [_spec("r0", job_env=job_env)]
        missing_probe = mock.Mock(returncode=1, stdout="", stderr="uv: command not found")
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch("hpc_agent.infra.runtime_preflight.ssh_run", return_value=missing_probe),
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
            pytest.raises(errors.SpecInvalid) as excinfo,
        ):
            submit_flow_batch(tmp_path, spec=_batch(specs))

        msg = str(excinfo.value)
        assert "runtime=uv" in msg and "not found on PATH" in msg
        assert "~/.conda/envs/hpc-pi/bin/pip install uv" in msg
        # #280: the uv probe now runs CONCURRENTLY with rsync+deploy, so the
        # deploy arm may complete before the uv SpecInvalid surfaces — a
        # completed deploy with no qsub is harmless and idempotent (push_deploy
        # is mocked here, so it does nothing either way). The load-bearing
        # safety invariant is unchanged: the uv failure aborts before ANY qsub,
        # so _submit_one_spec (the scheduler submit) never ran.
        assert push_deploy.call_count <= 1
        assert submit_one.call_count == 0

    def test_runtime_uv_check_skipped_when_runtime_not_uv(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """When HPC_RUNTIME is unset or not 'uv', the runtime probe is a no-op
        (no extra ssh_run call). Only the standard reachability probe fires
        (which the test mocks out via _preflight_probe)."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        specs = [_spec("r0", job_env={"EXECUTOR": "python run.py"})]  # no HPC_RUNTIME
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch("hpc_agent.infra.runtime_preflight.ssh_run") as ssh,
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id,
                job_ids=["900"],
                total_tasks=spec.total_tasks,
                deduped=False,
                canary_done=False,
            )
            submit_flow_batch(tmp_path, spec=_batch(specs))

        assert ssh.call_count == 0

    def test_shared_prelude_overlaps_uv_probe_with_deploy(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """#280: the `command -v uv` probe runs CONCURRENTLY with rsync+deploy,
        so the prelude's wall-clock is ~max(uv, deploy), not their sum."""
        import time as _time

        from hpc_agent.ops import submit_flow as sf_module

        def _slow_uv(*, ssh_target: Any, job_envs: Any, skip_preflight: Any) -> None:
            _time.sleep(0.4)

        def _slow_deploy(
            *,
            experiment_dir: Any,
            ssh_target: Any,
            remote_path: Any,
            rsync_excludes: Any,
            scheduler: Any,
        ) -> None:
            _time.sleep(0.4)

        with (
            mock.patch.object(sf_module, "_validate_ssh_target"),
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_run_uv_preflight_for_batch", side_effect=_slow_uv),
            mock.patch.object(sf_module, "_push_and_deploy", side_effect=_slow_deploy),
        ):
            t0 = _time.monotonic()
            sf_module._run_shared_prelude(
                experiment_dir=tmp_path,
                ssh_target="u@h",
                remote_path="/r",
                rsync_excludes=None,
                scheduler="sge",
                job_envs=[{}],
                skip_preflight=False,
                skip_prelude_io=False,
            )
            elapsed = _time.monotonic() - t0
        assert elapsed < 0.7, f"expected concurrent (~0.4s), got {elapsed:.2f}s"

    def test_shared_prelude_uv_failure_aborts_but_tolerates_concurrent_deploy(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """#280: a uv ``SpecInvalid`` still aborts the prelude (no qsub), while
        the concurrent deploy arm is allowed to complete — a finished deploy
        with no qsub is harmless and idempotent."""
        from hpc_agent.ops import submit_flow as sf_module

        deploy_ran: list[bool] = []

        def _fail_uv(*, ssh_target: Any, job_envs: Any, skip_preflight: Any) -> None:
            raise errors.SpecInvalid("preflight: runtime=uv but `uv` not found on PATH")

        def _deploy(**_kwargs: Any) -> None:
            deploy_ran.append(True)

        with (
            mock.patch.object(sf_module, "_validate_ssh_target"),
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_run_uv_preflight_for_batch", side_effect=_fail_uv),
            mock.patch.object(sf_module, "_push_and_deploy", side_effect=_deploy),
            pytest.raises(errors.SpecInvalid),
        ):
            sf_module._run_shared_prelude(
                experiment_dir=tmp_path,
                ssh_target="u@h",
                remote_path="/r",
                rsync_excludes=None,
                scheduler="sge",
                job_envs=[{}],
                skip_preflight=False,
                skip_prelude_io=False,
            )
        assert deploy_ran == [True]  # deploy completed concurrently before the uv error surfaced

    def test_skip_rsync_deploy_skips_push_and_deploy(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """#185/#283: when the operator/internal ``_skip_rsync_deploy`` is set,
        the prelude's rsync+deploy is skipped (Phase 2 of submit.md's two-phase
        canary gate — Phase 1 just deployed the same target). This is a
        batch-level decision threaded by the trusted in-process caller, NOT a
        per-spec agent field (#283 took that lever off the wire)."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        specs = [_spec(f"r{i}") for i in range(3)]
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
            submit_flow_batch(tmp_path, spec=_batch(specs), _skip_rsync_deploy=True)

        # Preflight is governed by its own flag; only the rsync+deploy half
        # is skipped by _skip_rsync_deploy.
        assert preflight.call_count == 1
        assert push_deploy.call_count == 0
        assert submit_one.call_count == 3

    def test_no_skip_rsync_deploy_runs_prelude(self, tmp_path: Any, _journal_home: Any) -> None:
        """#283: without the operator/internal ``_skip_rsync_deploy`` request,
        the rsync+deploy prelude runs — the conservative default. An agent can
        no longer drop it via a spec field (the lever is off the wire)."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        specs = [_spec("r0"), _spec("r1")]
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
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
            submit_flow_batch(tmp_path, spec=_batch(specs))

        assert push_deploy.call_count == 1

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
                job_ids=["901"],
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
        assert results[0].job_ids == ["901"]
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


# ---------------------------------------------------------------------------
# Per-run sidecar guarantee (#148 / #150): submit-flow OWNS the
# cluster-required artifact rather than trusting a prior step to write it.
# ---------------------------------------------------------------------------


def _mock_prelude_and_submit(sf_module):
    """Patch the cluster-touching steps so the sidecar logic runs alone."""
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    def _fake_submit(*, experiment_dir, spec):
        return SubmitFlowResult(
            run_id=spec.run_id,
            job_ids=[f"job_{spec.run_id}"],
            total_tasks=spec.total_tasks,
            deduped=False,
            canary_done=False,
        )

    return (
        mock.patch.object(sf_module, "_preflight_probe"),
        mock.patch.object(sf_module, "_push_and_deploy"),
        mock.patch.object(sf_module, "_submit_one_spec", side_effect=_fake_submit),
    )


class TestSidecarGuarantee:
    def test_synthesizes_sidecar_when_missing(self, tmp_path: Any, _journal_home: Any) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path

        spec = _spec(
            "rX",
            job_env={"EXECUTOR": "python run.py --task $HPC_TASK_ID", "HPC_CMD_SHA": "abcd1234"},
        )
        assert not run_sidecar_path(tmp_path, "rX").is_file()
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        # submit-flow wrote the sidecar from the spec before rsync.
        sc = read_run_sidecar(tmp_path, "rX")
        assert sc["result_dir_template"] == "results/{run_id}/task_{task_id}"
        assert sc["executor"] == "python run.py --task $HPC_TASK_ID"
        assert sc["task_count"] == 4

    def test_fails_loud_when_synthesized_executor_would_self_recurse(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """#162: job_env['EXECUTOR'] is the job-script command (it runs the
        dispatcher), not a per-task command. submit-flow must NOT synthesize a
        sidecar whose `executor` re-invokes the dispatcher — it fails loud so the
        array never launches into the instant self-recursion that burned 8 nodes
        live. Either a correct sidecar or a clean local failure, never a broken
        artifact."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import run_sidecar_path

        spec = _spec(
            "rRecur",
            job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_CMD_SHA": "abcd1234"},
        )
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3, pytest.raises(errors.SpecInvalid, match="dispatcher"):
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        # No structurally broken sidecar was left behind.
        assert not run_sidecar_path(tmp_path, "rRecur").is_file()

    @pytest.mark.parametrize("bad_executor", ["python3 .hpc/_hpc_dispatch.py", ""])
    def test_refuses_present_but_pending_sidecar(
        self, tmp_path: Any, _journal_home: Any, bad_executor: str
    ) -> None:
        """#171: a sidecar can be PRESENT but 'pending' — written with an empty
        or dispatcher-only executor (Step 6d skipped / half-written). Presence
        alone must NOT satisfy the guard: the cluster dispatcher would have
        nothing to run, or would run itself (#162). submit-flow refuses with the
        write-first error rather than shipping it — write-first is a hard
        precondition the primitive owns, not a manual unblock step."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import write_run_sidecar

        write_run_sidecar(
            tmp_path,
            run_id="rPending",
            cmd_sha="x" * 64,
            hpc_agent_version="0.7.4",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor=bad_executor,  # pending: no real per-task command
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="y" * 64,
        )
        # The spec even carries a real executor, but the guard reads the EXISTING
        # sidecar and never silently overwrites it (#148/#150) — it refuses.
        spec = _spec("rPending")
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        # #200: the actionable message names CLI verbs ('write-run-sidecar')
        # instead of Python internals ('write_run_sidecar'). Match the CLI form.
        with p1, p2, p3, pytest.raises(errors.SpecInvalid, match="write-run-sidecar"):
            submit_flow_batch(tmp_path, spec=_batch([spec]))

    def test_refuses_sidecar_that_diverges_from_interview_executor_cmd(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """Proving run #3 layer (b): the sidecar carried the executor NAME
        'run' while the real per-task command sat in interview.json's
        ``_materialized.entry_point.executor_cmd`` the whole time — the
        dispatcher ran ``/bin/sh -c run`` → exit 127, discovered only by a
        cluster round-trip. When the interview materialized an executor_cmd it
        is the source of truth; a divergent sidecar is refused in the
        pre-rsync prelude, before anything is staged or qsub'd."""
        import json

        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import write_run_sidecar

        real_cmd = "python3 -m hpc_agent.executor_cli run --file .hpc/wrappers/run.py " + (
            "--flag value " * 40  # a long materialized command, like the live 477-char one
        )
        (tmp_path / "interview.json").write_text(
            json.dumps(
                {
                    "goal": "g",
                    "_materialized": {
                        "entry_point": {
                            "kind": "register_run",
                            "run_name": "run",
                            "executor_cmd": real_cmd,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        write_run_sidecar(
            tmp_path,
            run_id="rDrift",
            cmd_sha="x" * 64,
            hpc_agent_version="0.10.0",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="run",  # the incident's hand-authored NAME, not the command
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="y" * 64,
        )
        spec = _spec("rDrift")
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with (
            p1,
            p2 as push_deploy,
            p3,
            pytest.raises(errors.SpecInvalid, match="resolve-submit-inputs"),
        ):
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        # Refused at the desk: the divergence never reached rsync (staging).
        assert push_deploy.call_count == 0

    def test_accepts_sidecar_matching_interview_executor_cmd(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """A sidecar whose executor IS the materialized executor_cmd submits
        normally — the provenance check is a drift guard, not a new hoop."""
        import json

        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import write_run_sidecar

        real_cmd = "python3 -m hpc_agent.executor_cli run --file .hpc/wrappers/run.py"
        (tmp_path / "interview.json").write_text(
            json.dumps(
                {
                    "goal": "g",
                    "_materialized": {
                        "entry_point": {"kind": "register_run", "executor_cmd": real_cmd}
                    },
                }
            ),
            encoding="utf-8",
        )
        write_run_sidecar(
            tmp_path,
            run_id="rMatch",
            cmd_sha="x" * 64,
            hpc_agent_version="0.10.0",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor=real_cmd,
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="y" * 64,
        )
        spec = _spec("rMatch")
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            results = submit_flow_batch(tmp_path, spec=_batch([spec]))
        assert results[0].job_ids == ["job_rMatch"]

    def test_sidecar_interview_check_skips_hand_onboarded_repos(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """Fail-open: no interview.json (and, second case, an interview without
        a ``_materialized`` block) means the repo was hand-onboarded and has no
        materialized truth to compare against — the check silently skips and
        the pre-written sidecar submits as before."""
        import json

        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import write_run_sidecar

        for run_id, interview in [
            ("rNoInterview", None),
            ("rNoMaterialized", {"goal": "g"}),  # legacy doc: no _materialized
        ]:
            if interview is not None:
                (tmp_path / "interview.json").write_text(json.dumps(interview), encoding="utf-8")
            write_run_sidecar(
                tmp_path,
                run_id=run_id,
                cmd_sha="x" * 64,
                hpc_agent_version="0.10.0",
                submitted_at="2026-01-01T00:00:00+00:00",
                executor="python train.py --seed $SEED",  # hand-written, legitimate
                result_dir_template="results/{run_id}/task_{task_id}",
                task_count=4,
                tasks_py_sha="y" * 64,
            )
            spec = _spec(run_id)
            p1, p2, p3 = _mock_prelude_and_submit(sf_module)
            with p1, p2, p3:
                results = submit_flow_batch(tmp_path, spec=_batch([spec]))
            assert results[0].job_ids == [f"job_{run_id}"]

    def test_records_resources_on_synthesized_sidecar(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        from hpc_agent._wire.workflows.submit_flow import SubmitResources
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        spec = _spec("rRes", resources=SubmitResources(walltime_sec=7200, mem_mb=8192))
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        assert read_run_sidecar(tmp_path, "rRes")["resources"] == {
            "walltime_sec": 7200,
            "mem_mb": 8192,
        }

    def test_records_env_hash_on_synthesized_sidecar(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        # #222: Step 6d captures ENVIRONMENT identity from the resolved
        # activation in job_env, alongside the param/code shas.
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.run_sha import compute_env_hash
        from hpc_agent.state.runs import read_run_sidecar

        spec = _spec(
            "rEnv",
            job_env={
                "EXECUTOR": "python run.py --task $HPC_TASK_ID",
                "HPC_CMD_SHA": "abcd1234",
                "MODULES": "python/3.11.9 cuda/12.1",
                "CONDA_SOURCE": "/opt/conda/etc/profile.d/conda.sh",
                "CONDA_ENV": "ml",
            },
        )
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rEnv")
        assert sc["env_hash"] == compute_env_hash(
            modules=["python/3.11.9", "cuda/12.1"],
            conda_source="/opt/conda/etc/profile.d/conda.sh",
            conda_envs=["ml"],
            runtime=None,
        )

    def test_records_data_sha_on_synthesized_sidecar(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        # #312: a spec that declares input_datasets gets data_sha captured at
        # sidecar-write time with no manual step, symmetric with env_hash.
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.run_sha import compute_data_sha
        from hpc_agent.state.runs import read_run_sidecar

        dataset = tmp_path / "data" / "train.csv"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        dataset.write_text("a,b\n1,2\n", encoding="utf-8")

        spec = _spec("rData", input_datasets=["data/train.csv"])
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rData")
        assert sc["data_sha"] == compute_data_sha(["data/train.csv"], base_dir=tmp_path)
        assert sc["data_sha"] is not None

    def test_data_sha_stays_null_when_no_dataset_declared(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        # Undeclared → null ("not captured"), distinguishable from the real
        # digest of an empty declaration — the #312 Gap 1 decision.
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        spec = _spec("rNoData")
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        assert read_run_sidecar(tmp_path, "rNoData")["data_sha"] is None

    def test_missing_result_dir_template_synthesizes_the_block_owned_default(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """Proving-run-3 finding (a) / conduct rule 6: a missing template used
        to die SpecInvalid here, and the driving agent papered over it by
        hand-injecting a value — a silent LLM default. The block owns the
        default now: ``{task_id}`` is a reserved dispatcher render key, so
        ``results/{run_id}/task_{task_id}`` is collision-free for any axis."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        spec = _spec("rNo", result_dir_template=None)
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rNo")
        assert sc["result_dir_template"] == "results/{run_id}/task_{task_id}"

    def test_synthesized_sidecar_carries_spec_scopes(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """The submit-flow-owns-the-artifact guarantee carries the caller's
        opaque evidence-scope tags onto the synthesized sidecar even when the
        resolve leg (Step 6d / write-run-sidecar) did not pre-write one."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        spec = _spec("rScopes", scopes=["ci.smoke", "band-A_1"])
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        assert read_run_sidecar(tmp_path, "rScopes")["scopes"] == ["ci.smoke", "band-A_1"]

    def test_does_not_overwrite_existing_sidecar(self, tmp_path: Any, _journal_home: Any) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

        # A real Step-6d sidecar with a distinctive executor / template.
        write_run_sidecar(
            tmp_path,
            run_id="rE",
            cmd_sha="x" * 64,
            hpc_agent_version="0.7.2",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="CUSTOM_DISPATCH",
            result_dir_template="custom/{task_id}",
            task_count=4,
            tasks_py_sha="y" * 64,
        )
        spec = _spec("rE")  # carries the generic default template
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rE")
        # The pre-written sidecar wins — submit-flow never clobbers it.
        assert sc["executor"] == "CUSTOM_DISPATCH"
        assert sc["result_dir_template"] == "custom/{task_id}"


# ---------------------------------------------------------------------------
# extra.spec_kwargs stamping (#234/#240): the synthesized sidecar carries the
# run-constant task kwargs (entry_point.fixed_params from interview.json) so a
# later gpu_oom is discriminated by parallelism/width instead of getting the
# flat fix. A swept axis value must NEVER leak into the pocket.
# ---------------------------------------------------------------------------


def _write_interview(campaign_dir: Path, *, task_generator: dict, entry_point: dict) -> str:
    """Materialize a real generator-mode interview.json via record_interview.

    Goes through the production writer (``ops.memory.interview.record_interview``)
    rather than hand-writing JSON, so the test pins the ACTUAL on-disk shape the
    reader keys on — if the persisted location of ``fixed_params`` ever moves,
    this breaks loudly instead of passing against a stale stub.

    Returns the materialized ``executor_cmd`` (read back from the written doc):
    since the proving-run-3 provenance gate, a submit over an interview'd repo
    must ship a sidecar whose executor matches it, so callers thread it into
    the spec's ``job_env["EXECUTOR"]``.
    """
    import json

    from hpc_agent._wire.actions.interview import InterviewSpec
    from hpc_agent.ops.memory.interview import record_interview

    intent = {
        "goal": "spec_kwargs test",
        "task_count": 3,
        "produced_by": {"kind": "human", "operator": "test"},
        "task_generator": task_generator,
        "entry_point": entry_point,
    }
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=campaign_dir)
    doc = json.loads((campaign_dir / "interview.json").read_text(encoding="utf-8"))
    return str(doc["_materialized"]["entry_point"]["executor_cmd"])


class TestSpecKwargsStamping:
    def test_generator_mode_stamps_fixed_params(self, tmp_path: Any, _journal_home: Any) -> None:
        """A generator-mode run with declared fixed_params → the synthesized
        sidecar's ``extra.spec_kwargs`` contains exactly those constant kwargs."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        executor_cmd = _write_interview(
            tmp_path,
            task_generator={
                "kind": "cartesian_product",
                "params": {"axes": {"seed": [0, 1, 2]}},
            },
            entry_point={
                "kind": "shell_command",
                "run_name": "train",
                "argv": ["python3", "t.py", "--seed", "{seed}", "--tp", "{tp_size}"],
                "signature": {"seed": "int", "tp_size": "int"},
                "fixed_params": {"tp_size": 2},
            },
        )
        spec = _spec("rGen", total_tasks=3, job_env={"EXECUTOR": executor_cmd})
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rGen")
        assert sc["extra"]["spec_kwargs"] == {"tp_size": 2}

    def test_no_interview_no_spec_kwargs_no_error(self, tmp_path: Any, _journal_home: Any) -> None:
        """A hand-written tasks.py run (no interview.json) → no spec_kwargs, no
        error. The documented limitation: only declared fixed_params can be
        stamped; absence is a clean no-op, never a failure."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        assert not (tmp_path / "interview.json").exists()
        spec = _spec("rHand")
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rHand")
        assert "extra" not in sc

    def test_swept_axis_value_does_not_leak_into_spec_kwargs(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """SOUNDNESS GUARD: a per-task SWEPT axis (here ``seed``) is NOT
        run-constant, so a single value would misrepresent the cluster and could
        route a wrong fix. It MUST NOT appear in spec_kwargs — only the declared
        fixed_params do."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar

        executor_cmd = _write_interview(
            tmp_path,
            task_generator={
                "kind": "cartesian_product",
                "params": {"axes": {"seed": [0, 1, 2]}},
            },
            entry_point={
                "kind": "shell_command",
                "run_name": "train",
                "argv": ["python3", "t.py", "--seed", "{seed}", "--samples", "{samples}"],
                "signature": {"seed": "int", "samples": "int"},
                "fixed_params": {"samples": 10000},
            },
        )
        spec = _spec("rSwept", total_tasks=3, job_env={"EXECUTOR": executor_cmd})
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rSwept")
        spec_kwargs = sc["extra"]["spec_kwargs"]
        assert spec_kwargs == {"samples": 10000}
        # The swept axis is absent — never stamped as a run-constant.
        assert "seed" not in spec_kwargs

    def test_resolve_uses_stamped_tp_size_for_increase_parallelism(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """End-to-end-ish: a sidecar stamped via submit-flow flows through
        ``build_failure_features`` → ``resource_spec`` carries ``tp_size``, so
        ``resolve()`` on a gpu_oom returns the ``increase-parallelism`` fix
        instead of the flat ``increase-mem-per-gpu`` — proving the pocket the
        whole change exists for actually fires. No cluster needed."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.recover.features_glue import build_failure_features
        from hpc_agent.ops.recover.resolve import resolve
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.run_record import RunRecord
        from hpc_agent.state.runs import read_run_sidecar

        executor_cmd = _write_interview(
            tmp_path,
            task_generator={
                "kind": "cartesian_product",
                "params": {"axes": {"seed": [0, 1, 2]}},
            },
            entry_point={
                "kind": "shell_command",
                "run_name": "train",
                "argv": ["python3", "t.py", "--seed", "{seed}", "--tp", "{tp_size}"],
                "signature": {"seed": "int", "tp_size": "int"},
                "fixed_params": {"tp_size": 2},
            },
        )
        spec = _spec("rE2E", total_tasks=3, job_env={"EXECUTOR": executor_cmd})
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rE2E")

        cluster = {"error_class": "gpu_oom", "fingerprint": "fp", "task_ids": [0]}
        record = RunRecord(
            run_id="rE2E",
            profile="p",
            cluster="c",
            ssh_target="user@host",
            remote_path="/r",
            job_name="rE2E",
            job_ids=["9001"],
            total_tasks=3,
            submitted_at="2026-06-06T12:00:00+00:00",
            experiment_dir=str(tmp_path),
        )
        features = build_failure_features(cluster, record=record, sidecar=sc)
        assert features.resource_spec is not None
        assert features.resource_spec.get("tp_size") == 2

        resolution = resolve(features)
        assert resolution.decided_by == "code"
        assert resolution.action is not None
        assert resolution.action["action"] == "increase-parallelism"


class TestResourceFlagPlumbing:
    def test_resource_flags_reach_build_command(self, tmp_path: Any) -> None:
        import re
        import subprocess

        from hpc_agent._wire.workflows.submit_flow import SubmitResources
        from hpc_agent.infra.backends import HPCBackend
        from hpc_agent.ops.submit_flow import _make_single_array_submission

        captured: dict[str, Any] = {}

        class _FakeBackend(HPCBackend):
            JOB_ID_REGEX = re.compile(r"job (\d+)")

            def __init__(self) -> None:
                self.log_dir = "/tmp/resflag-stub-logs"

            def _setup_log_dir(self) -> None:
                pass

            def resource_flags(self, resources: Any) -> list[str]:
                return ["-l", f"h_rt={resources.walltime_sec}"] if resources else []

            def _build_command(self, rng, name, env, *, extra_flags=None, array=True):  # type: ignore[no-untyped-def]
                captured["extra_flags"] = extra_flags
                return ["qsub", "..."]

            def _execute_command(self, cmd, env, cwd):  # type: ignore[no-untyped-def]
                return subprocess.CompletedProcess(cmd, 0, stdout="job 42", stderr="")

        ids = _make_single_array_submission(
            _FakeBackend(),  # type: ignore[arg-type]
            job_name="j",
            total_tasks=4,
            job_env={},
            cwd=tmp_path,
            resources=SubmitResources(walltime_sec=3600),
        )
        assert ids == ["42"]
        # The backend's resource_flags output flowed into _build_command.
        assert captured["extra_flags"] == ["-l", "h_rt=3600"]


def test_canary_sidecar_mirrored_before_rsync(tmp_path: Any, _journal_home: Any) -> None:
    """#175: the canary sidecar must exist on disk BEFORE ``_push_and_deploy``
    so it rides the SAME rsync as the main sidecar — otherwise it never reaches
    the cluster and every canary task dies ``sidecar_not_found``.

    ``_submit_one_spec`` (which used to be the ONLY place the canary sidecar was
    written) is mocked out here, so the sidecar can only be present at deploy
    time if the pre-rsync prelude mirrored it.
    """
    from hpc_agent.ops import submit_flow as sf_module
    from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch
    from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path

    spec = _spec("rC", canary=True)  # _spec carries a real per-task EXECUTOR
    seen: dict[str, bool] = {}

    def _capture_at_deploy(
        *, experiment_dir, ssh_target, remote_path, rsync_excludes, scheduler=None
    ):
        seen["canary_on_disk"] = run_sidecar_path(experiment_dir, "rC-canary").is_file()

    with (
        mock.patch.object(sf_module, "_preflight_probe"),
        mock.patch.object(sf_module, "_push_and_deploy", side_effect=_capture_at_deploy),
        mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
    ):
        submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
            run_id=spec.run_id,
            job_ids=["1"],
            total_tasks=spec.total_tasks,
            deduped=False,
            canary_done=True,
        )
        submit_flow_batch(tmp_path, spec=_batch([spec]))

    assert seen.get("canary_on_disk") is True
    # And it mirrors the main run's per-task command, scoped to a single task.
    csc = read_run_sidecar(tmp_path, "rC-canary")
    assert csc["task_count"] == 1
    assert csc["executor"] == "python run.py"


def test_canary_sidecar_mirrors_spec_kwargs(tmp_path: Any, _journal_home: Any) -> None:
    """The canary mirror copies ``extra.spec_kwargs`` from the main sidecar so a
    canary gpu_oom is discriminated by the same parallelism/width knobs as the
    main run, rather than silently falling back to the flat fix."""
    from hpc_agent.ops import submit_flow as sf_module
    from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch
    from hpc_agent.state.runs import read_run_sidecar

    executor_cmd = _write_interview(
        tmp_path,
        task_generator={
            "kind": "cartesian_product",
            "params": {"axes": {"seed": [0, 1, 2]}},
        },
        entry_point={
            "kind": "shell_command",
            "run_name": "train",
            "argv": ["python3", "t.py", "--seed", "{seed}", "--tp", "{tp_size}"],
            "signature": {"seed": "int", "tp_size": "int"},
            "fixed_params": {"tp_size": 2},
        },
    )
    spec = _spec("rCmk", canary=True, total_tasks=3, job_env={"EXECUTOR": executor_cmd})

    with (
        mock.patch.object(sf_module, "_preflight_probe"),
        mock.patch.object(sf_module, "_push_and_deploy"),
        mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
    ):
        submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
            run_id=spec.run_id,
            job_ids=["1"],
            total_tasks=spec.total_tasks,
            deduped=False,
            canary_done=True,
        )
        submit_flow_batch(tmp_path, spec=_batch([spec]))

    assert read_run_sidecar(tmp_path, "rCmk")["extra"]["spec_kwargs"] == {"tp_size": 2}
    assert read_run_sidecar(tmp_path, "rCmk-canary")["extra"]["spec_kwargs"] == {"tp_size": 2}


class TestCheckpointCanaryEnv:
    """#294 PR4: a run that opts into auto_resume_on_kill stamps its CANARY
    submission with HPC_CHECKPOINT_CANARY=1 (so the executor writes→kills→the
    checkpoint round-trip is provable), while the MAIN array never carries it."""

    def _run_one(self, tmp_path: Any, spec: Any) -> dict[str, dict[str, str]]:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import _submit_one_spec

        captured: dict[str, dict[str, str]] = {}

        def _fake_submit(backend, *, job_name, total_tasks, job_env, cwd, **_kw):
            captured[job_name] = dict(job_env)
            return ["101"]

        with (
            mock.patch.object(sf_module, "build_remote_backend", return_value=object()),
            mock.patch.object(sf_module, "_mirror_canary_sidecar"),
            mock.patch.object(sf_module, "_make_single_array_submission", side_effect=_fake_submit),
            mock.patch.object(sf_module, "submit_and_record"),
            mock.patch.object(sf_module, "load_run", return_value=None),
        ):
            _submit_one_spec(experiment_dir=tmp_path, spec=spec)
        return captured

    def test_canary_carries_marker_main_does_not(self, tmp_path: Any, _journal_home: Any) -> None:
        spec = _spec("rCK", canary=True, force_canary=True, auto_resume_on_kill=True)
        captured = self._run_one(tmp_path, spec)
        assert captured["rCK_canary"]["HPC_CHECKPOINT_CANARY"] == "1"
        assert "HPC_CHECKPOINT_CANARY" not in captured["rCK"]

    def test_no_marker_when_auto_resume_off(self, tmp_path: Any, _journal_home: Any) -> None:
        spec = _spec("rCK2", canary=True, force_canary=True, auto_resume_on_kill=False)
        captured = self._run_one(tmp_path, spec)
        assert "HPC_CHECKPOINT_CANARY" not in captured["rCK2_canary"]
        assert "HPC_CHECKPOINT_CANARY" not in captured["rCK2"]


# ---------------------------------------------------------------------------
# #275 — skip_preflight demoted from agent spec to operator-only control
# ---------------------------------------------------------------------------


class TestSkipPreflightDemotion:
    """skip_preflight is no longer an agent-settable spec field (#275).

    It silenced submit-flow's `command -v uv` runtime probe — an agent
    following SKILL.md set `skip_preflight: true` and launched arrays doomed by
    `HPC_RUNTIME=uv but 'uv' not on PATH`. The skip is operator-only now:
    `HPC_AGENT_SKIP_PREFLIGHT=1`, or a Python-only `_skip_preflight` kwarg for
    trusted internal callers. Same operator-vs-agent boundary as `--inline` (#155).
    """

    def test_submit_flow_spec_rejects_skip_preflight(self) -> None:
        from pydantic import ValidationError

        from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

        payload = _spec("r0").model_dump()
        payload["skip_preflight"] = True
        with pytest.raises(ValidationError):
            SubmitFlowSpec(**payload)

    def test_batch_spec_rejects_skip_preflight(self) -> None:
        from pydantic import ValidationError

        from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec

        with pytest.raises(ValidationError):
            SubmitFlowBatchSpec(specs=[_spec("r0")], skip_preflight=True)  # type: ignore[call-arg]

    def test_resolver_internal_kwarg_overrides_env(self, monkeypatch: Any) -> None:
        from hpc_agent.ops import submit_flow as sf

        monkeypatch.delenv("HPC_AGENT_SKIP_PREFLIGHT", raising=False)
        assert sf._skip_preflight_requested(None) is False
        assert sf._skip_preflight_requested(True) is True
        monkeypatch.setenv("HPC_AGENT_SKIP_PREFLIGHT", "1")
        assert sf._skip_preflight_requested(None) is True
        # An explicit internal verdict wins over the env in both directions.
        assert sf._skip_preflight_requested(False) is False

    def test_env_var_skips_the_preflight_probe(
        self, tmp_path: Any, _journal_home: Any, monkeypatch: Any
    ) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        monkeypatch.setenv("HPC_AGENT_SKIP_PREFLIGHT", "1")
        with (
            mock.patch.object(sf_module, "_preflight_probe") as preflight,
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id, job_ids=["1"], total_tasks=4, deduped=False, canary_done=False
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]))
        assert preflight.call_args.kwargs["skip"] is True

    def test_internal_kwarg_skips_the_preflight_probe(
        self, tmp_path: Any, _journal_home: Any, monkeypatch: Any
    ) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        monkeypatch.delenv("HPC_AGENT_SKIP_PREFLIGHT", raising=False)
        with (
            mock.patch.object(sf_module, "_preflight_probe") as preflight,
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id, job_ids=["1"], total_tasks=4, deduped=False, canary_done=False
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]), _skip_preflight=True)
        assert preflight.call_args.kwargs["skip"] is True

    def test_default_runs_the_preflight_probe(
        self, tmp_path: Any, _journal_home: Any, monkeypatch: Any
    ) -> None:
        """No env var, no internal kwarg → the probe runs (skip=False)."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        monkeypatch.delenv("HPC_AGENT_SKIP_PREFLIGHT", raising=False)
        with (
            mock.patch.object(sf_module, "_preflight_probe") as preflight,
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id, job_ids=["1"], total_tasks=4, deduped=False, canary_done=False
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]))
        assert preflight.call_args.kwargs["skip"] is False


# ---------------------------------------------------------------------------
# #283 — skip_rsync_deploy demoted from agent spec to operator/internal control
# ---------------------------------------------------------------------------


class TestSkipRsyncDeployDemotion:
    """skip_rsync_deploy is no longer an agent-settable spec field (#283).

    A hand-authored ``skip_rsync_deploy: true`` on a raw submit-flow spec
    ASSERTED "Phase 1 already deployed the same tree, nothing changed since" —
    but a stale assertion silently ran the cluster on whatever code the previous
    deploy shipped if the local tree drifted (#185). The skip is operator/
    internal-only now: ``HPC_AGENT_SKIP_RSYNC_DEPLOY=1``, or a Python-only
    ``_skip_rsync_deploy`` kwarg for trusted internal callers (the two-phase
    canary gate's in-process main-array launch, where "Phase 1 just deployed" is
    a structural fact the code knows). Same operator-vs-agent boundary as
    ``skip_preflight`` (#275) / ``--inline`` (#155).
    """

    def test_submit_flow_spec_rejects_skip_rsync_deploy(self) -> None:
        from pydantic import ValidationError

        from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

        payload = _spec("r0").model_dump()
        payload["skip_rsync_deploy"] = True
        with pytest.raises(ValidationError):
            SubmitFlowSpec(**payload)

    def test_batch_spec_inner_spec_rejects_skip_rsync_deploy(self) -> None:
        from pydantic import ValidationError

        from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec

        payload = _spec("r0").model_dump()
        payload["skip_rsync_deploy"] = True
        with pytest.raises(ValidationError):
            SubmitFlowBatchSpec(specs=[payload])  # type: ignore[list-item]

    def test_resolver_internal_kwarg_overrides_env(self, monkeypatch: Any) -> None:
        from hpc_agent.ops import submit_flow as sf

        monkeypatch.delenv("HPC_AGENT_SKIP_RSYNC_DEPLOY", raising=False)
        assert sf._skip_rsync_deploy_requested(None) is False
        assert sf._skip_rsync_deploy_requested(True) is True
        monkeypatch.setenv("HPC_AGENT_SKIP_RSYNC_DEPLOY", "1")
        assert sf._skip_rsync_deploy_requested(None) is True
        # An explicit internal verdict wins over the env in both directions.
        assert sf._skip_rsync_deploy_requested(False) is False

    def test_env_var_skips_the_rsync_deploy(
        self, tmp_path: Any, _journal_home: Any, monkeypatch: Any
    ) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        monkeypatch.setenv("HPC_AGENT_SKIP_RSYNC_DEPLOY", "1")
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id, job_ids=["1"], total_tasks=4, deduped=False, canary_done=False
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]))
        assert push_deploy.call_count == 0

    def test_internal_kwarg_skips_the_rsync_deploy(
        self, tmp_path: Any, _journal_home: Any, monkeypatch: Any
    ) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        monkeypatch.delenv("HPC_AGENT_SKIP_RSYNC_DEPLOY", raising=False)
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id, job_ids=["1"], total_tasks=4, deduped=False, canary_done=False
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]), _skip_rsync_deploy=True)
        assert push_deploy.call_count == 0

    def test_default_runs_the_rsync_deploy(
        self, tmp_path: Any, _journal_home: Any, monkeypatch: Any
    ) -> None:
        """No env var, no internal kwarg → the rsync+deploy runs."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        monkeypatch.delenv("HPC_AGENT_SKIP_RSYNC_DEPLOY", raising=False)
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy") as push_deploy,
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id, job_ids=["1"], total_tasks=4, deduped=False, canary_done=False
            )
            submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]))
        assert push_deploy.call_count == 1


# ---------------------------------------------------------------------------
# #276 Bug 1 — an `abandoned` record is a corpse, not a live run to block on
# ---------------------------------------------------------------------------


def _seed_record(tmp_path: Any, run_id: str, status: str, job_ids=("13554560",)) -> None:
    """Seed a journal record then transition it to *status* (via mark_run)."""
    from hpc_agent.state.journal import mark_run, upsert_run
    from hpc_agent.state.run_record import RunRecord

    rec = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/r",
        job_name=run_id,
        job_ids=list(job_ids),
        total_tasks=4,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(tmp_path.resolve()),
    )
    upsert_run(tmp_path, rec)
    if status != "in_flight":
        mark_run(tmp_path, run_id, status=status)


class TestTerminalNotBlocking:
    """#276: a terminal-but-not-`complete` journal entry (`failed` / `abandoned`)
    with populated `job_ids` must NOT block a fresh submit — it is not a live run,
    so its `job_ids` are forensic, not an in-flight marker. A single transient
    status-probe failure used to mint an `abandoned` corpse and wedge every future
    submit until the user nuked `~/.claude/hpc/<hash>/`. `complete` still dedups
    (idempotency); `in_flight` (incl. a timed-out run) still blocks."""

    def test_is_resubmittable_terminal_helper(self) -> None:
        from hpc_agent.state.journal import is_resubmittable_terminal
        from hpc_agent.state.run_record import RunRecord

        def _r(status: str) -> RunRecord:
            return RunRecord(
                run_id="r",
                profile="p",
                cluster="c",
                ssh_target="u@h",
                remote_path="/r",
                job_name="r",
                job_ids=["1"],
                total_tasks=1,
                submitted_at="t",
                experiment_dir="/e",
                status=status,
            )

        # Terminal-but-not-complete → resubmittable (fall through to a fresh submit).
        assert is_resubmittable_terminal(_r("abandoned")) is True
        assert is_resubmittable_terminal(_r("failed")) is True
        # complete still dedups (idempotency); in_flight is still live — incl. a
        # timed-out run, which stays in_flight in the journal (never `timeout`).
        assert is_resubmittable_terminal(_r("complete")) is False
        assert is_resubmittable_terminal(_r("in_flight")) is False

    def test_held_run_still_blocks(self) -> None:
        """A held run (pending_verdict, #231/#234) is parked awaiting a decision —
        even though it is `failed` it is NOT resubmittable, so a plain submit can't
        clobber the hold. The escalation flow owns its resubmission."""
        from hpc_agent.state.journal import is_resubmittable_terminal
        from hpc_agent.state.run_record import RunRecord

        held_failed = RunRecord(
            run_id="r",
            profile="p",
            cluster="c",
            ssh_target="u@h",
            remote_path="/r",
            job_name="r",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="t",
            experiment_dir="/e",
            status="failed",
            pending_verdict={"reason": "ambiguous failure"},
        )
        assert is_resubmittable_terminal(held_failed) is False

    @pytest.mark.parametrize("status", ["abandoned", "failed"])
    def test_terminal_record_does_not_dedup_submit_proceeds(
        self, tmp_path: Any, _journal_home: Any, status: str
    ) -> None:
        """Acceptance (#276): seed a terminal-not-complete record + job_ids, run
        submit-flow → it PROCEEDS (doesn't short-circuit as deduped)."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow_batch

        _seed_record(tmp_path, "r0", status)
        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            submit_one.side_effect = lambda *, experiment_dir, spec: SubmitFlowResult(
                run_id=spec.run_id,
                job_ids=["77"],
                total_tasks=4,
                deduped=False,
                canary_done=False,
            )
            results = submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]))
        # It proceeded to a real submission rather than short-circuiting deduped.
        assert submit_one.call_count == 1
        assert results[0].deduped is False
        assert results[0].job_ids == ["77"]

    def test_complete_record_still_dedups(self, tmp_path: Any, _journal_home: Any) -> None:
        """Idempotency preserved: a `complete` run with the same run_id still dedups."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch

        _seed_record(tmp_path, "r0", "complete")
        with (
            mock.patch.object(sf_module, "_preflight_probe") as preflight,
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "_submit_one_spec") as submit_one,
        ):
            results = submit_flow_batch(tmp_path, spec=_batch([_spec("r0")]))
        assert submit_one.call_count == 0
        assert results[0].deduped is True
        assert preflight.call_count == 0  # fully short-circuited — no cluster traffic

    @pytest.mark.parametrize("status", ["abandoned", "failed"])
    def test_submit_and_record_skips_terminal(
        self, tmp_path: Any, _journal_home: Any, status: str
    ) -> None:
        """runner.submit_and_record: a terminal-not-complete record is not a dedup target."""
        import warnings

        from hpc_agent._wire.actions.submit import SubmitSpec
        from hpc_agent.ops.submit.runner import submit_and_record

        _seed_record(tmp_path, "r0", status, job_ids=["70"])
        spec = SubmitSpec(
            profile="p",
            cluster="c",
            ssh_target="user@host",
            remote_path="/r",
            job_name="r0",
            run_id="r0",
            job_ids=["71"],
            total_tasks=4,
        )
        with warnings.catch_warnings():
            # No sidecar on disk → the post-qsub finalize warns; not under test.
            warnings.simplefilter("ignore")
            record, deduped = submit_and_record(tmp_path, spec=spec)
        assert deduped is False  # terminal-not-complete → fresh record, not a replay
        assert record.job_ids == ["71"]

    def test_backfills_provenance_onto_prewritten_sidecar(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        # #312: a Step 6d-style pre-written sidecar (real executor, no
        # provenance) gets null data_sha/env_hash backfilled at submit time;
        # an explicitly recorded value is never overwritten.
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.run_sha import compute_data_sha
        from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

        dataset = tmp_path / "data" / "train.csv"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        dataset.write_text("a,b\n1,2\n", encoding="utf-8")
        write_run_sidecar(
            tmp_path,
            run_id="rPre",
            cmd_sha="0" * 12,
            hpc_agent_version="0.0.0",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="python run.py --task $HPC_TASK_ID",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="y" * 64,
        )
        spec = _spec("rPre", input_datasets=["data/train.csv"])
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rPre")
        assert sc["data_sha"] == compute_data_sha(["data/train.csv"], base_dir=tmp_path)
        assert sc["env_hash"] is not None
        assert sc["executor"] == "python run.py --task $HPC_TASK_ID"  # untouched

    def test_backfill_never_overwrites_recorded_provenance(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

        write_run_sidecar(
            tmp_path,
            run_id="rKeep",
            cmd_sha="0" * 12,
            hpc_agent_version="0.0.0",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="python run.py --task $HPC_TASK_ID",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="y" * 64,
            data_sha="e" * 64,
            env_hash="f" * 64,
        )
        spec = _spec("rKeep")
        p1, p2, p3 = _mock_prelude_and_submit(sf_module)
        with p1, p2, p3:
            submit_flow_batch(tmp_path, spec=_batch([spec]))
        sc = read_run_sidecar(tmp_path, "rKeep")
        assert sc["data_sha"] == "e" * 64
        assert sc["env_hash"] == "f" * 64


# ---------------------------------------------------------------------------
# 2026-06-11 — post-qsub sidecar pre-stamp (crash-safety in the qsub→record gap)
# ---------------------------------------------------------------------------


class TestPostQsubSidecarPreStamp:
    """The 2026-06-11 demo lost main-array job id 13610902: the worker exited
    with the pipeline auto-backgrounded and the harness killed it ~1s after
    qsub — before ``submit_and_record`` — so the id existed nowhere on disk
    and the orchestrator "recovered" by fabricating ``["purged-completed"]``.
    The pre-stamp persists the parsed ids to the sidecar IMMEDIATELY after
    qsub, so a crash in that window leaves the real ids recoverable through
    every sidecar-reading path (reconcile remediation, cross-machine
    reconstruction, load-context)."""

    def _seed_sidecar(self, tmp_path: Any, run_id: str) -> None:
        from hpc_agent.state.runs import write_run_sidecar

        write_run_sidecar(
            tmp_path,
            run_id=run_id,
            cmd_sha="0" * 64,
            hpc_agent_version="0.0.0",
            submitted_at="2026-06-11T00:00:00Z",
            executor="python3 run.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="1" * 64,
        )

    def test_main_job_ids_stamped_even_when_record_never_runs(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import _submit_one_spec
        from hpc_agent.state.runs import read_run_sidecar

        self._seed_sidecar(tmp_path, "rStamp")
        spec = _spec("rStamp")

        def _killed(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("process died between qsub and record")

        with (
            mock.patch.object(sf_module, "build_remote_backend", return_value=object()),
            mock.patch.object(
                sf_module, "_make_single_array_submission", return_value=["13610902"]
            ),
            mock.patch.object(sf_module, "submit_and_record", side_effect=_killed),
            mock.patch.object(sf_module, "load_run", return_value=None),
            pytest.raises(RuntimeError, match="between qsub and record"),
        ):
            _submit_one_spec(experiment_dir=tmp_path, spec=spec)

        # The whole point: the id survived even though the journal write didn't.
        assert read_run_sidecar(tmp_path, "rStamp")["job_ids"] == ["13610902"]

    def test_canary_job_ids_stamped_before_record(self, tmp_path: Any, _journal_home: Any) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import _submit_one_spec
        from hpc_agent.state.runs import read_run_sidecar

        self._seed_sidecar(tmp_path, "rStampC")
        spec = _spec("rStampC", canary=True, force_canary=True, canary_only=True)

        def _killed(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("died before canary record")

        with (
            mock.patch.object(sf_module, "build_remote_backend", return_value=object()),
            mock.patch.object(
                sf_module, "_make_single_array_submission", return_value=["13610900"]
            ),
            mock.patch.object(sf_module, "submit_and_record", side_effect=_killed),
            mock.patch.object(sf_module, "load_run", return_value=None),
            pytest.raises(RuntimeError, match="before canary record"),
        ):
            _submit_one_spec(experiment_dir=tmp_path, spec=spec)

        # _mirror_canary_sidecar ran for real, so the canary sidecar exists and
        # carries the pre-stamped id.
        assert read_run_sidecar(tmp_path, "rStampC-canary")["job_ids"] == ["13610900"]

    def test_missing_sidecar_does_not_fail_the_submission(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """The stamp is best-effort: no sidecar on disk (legacy caller) must
        not turn a landed qsub into a submit-flow error."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import _submit_one_spec

        spec = _spec("rNoSc")
        with (
            mock.patch.object(sf_module, "build_remote_backend", return_value=object()),
            mock.patch.object(sf_module, "_make_single_array_submission", return_value=["4242"]),
            mock.patch.object(sf_module, "submit_and_record"),
            mock.patch.object(sf_module, "load_run", return_value=None),
        ):
            result = _submit_one_spec(experiment_dir=tmp_path, spec=spec)
        assert result.job_ids == ["4242"]

    def test_production_flow_seeds_sidecar_so_the_stamp_actually_lands(
        self, tmp_path: Any, _journal_home: Any
    ) -> None:
        """The pre-stamp's protection is silent if its precondition is unmet:
        ``update_run_sidecar_job_ids`` no-ops (FileNotFoundError, swallowed)
        when no sidecar exists. The whole Tier-1 guarantee therefore rides on
        ``_ensure_run_sidecar`` running BEFORE the qsub in the real batch flow.

        Drive the real ``submit_flow_batch`` entry point WITHOUT pre-seeding a
        sidecar (only the cluster I/O + the record write are mocked), kill
        ``submit_and_record``, and assert the stamped id is on disk anyway.
        This pins the production ordering: if a refactor moved or dropped the
        ``_ensure_run_sidecar`` prelude, the stamp would silently skip — exactly
        the failure it guards against — and this test would fail on the
        FileNotFoundError from ``read_run_sidecar``."""
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import submit_flow_batch
        from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path

        spec = _spec("rProd")
        # Precondition for the test's meaning: no sidecar exists up front, so a
        # landed stamp can only come from the real _ensure_run_sidecar prelude.
        assert not run_sidecar_path(tmp_path, "rProd").exists()

        def _killed(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("process died between qsub and record")

        with (
            mock.patch.object(sf_module, "_preflight_probe"),
            mock.patch.object(sf_module, "_push_and_deploy"),
            mock.patch.object(sf_module, "build_remote_backend", return_value=object()),
            mock.patch.object(
                sf_module, "_make_single_array_submission", return_value=["13610902"]
            ),
            mock.patch.object(sf_module, "submit_and_record", side_effect=_killed),
            mock.patch.object(sf_module, "load_run", return_value=None),
            pytest.raises(RuntimeError, match="between qsub and record"),
        ):
            submit_flow_batch(tmp_path, spec=_batch([spec]))

        assert read_run_sidecar(tmp_path, "rProd")["job_ids"] == ["13610902"]


class TestPreflightProbe:
    """The 2026-07-04 S2 wedge: ``_preflight_probe``'s ssh subprocess blocked
    with no enforceable deadline, parking the driver silently for hours.
    These tests pin the three guards that replace the hang: a hard per-attempt
    timeout threaded to ``ssh_run``, a bounded attempt budget with a loud
    per-attempt stderr line, and a structured ``SshUnreachable`` envelope
    (never a raw ``TimeoutError``, never a hang) on final failure.
    """

    @staticmethod
    def _probe(**kwargs: Any):
        from hpc_agent.ops.submit_flow import _preflight_probe

        return _preflight_probe("u@cluster", skip=False, **kwargs)

    def test_skip_makes_no_ssh_call(self) -> None:
        from hpc_agent.ops import submit_flow as sf_module
        from hpc_agent.ops.submit_flow import _preflight_probe

        with mock.patch.object(sf_module, "ssh_run") as ssh:
            _preflight_probe("u@cluster", skip=True)
        assert ssh.call_count == 0

    def test_default_timeout_threaded_to_ssh_run(self) -> None:
        """The probe must pass an explicit per-attempt timeout — the wedge
        was a probe subprocess with no enforceable deadline."""
        from hpc_agent.ops import submit_flow as sf_module

        ok = mock.Mock(returncode=0)
        with mock.patch.object(sf_module, "ssh_run", return_value=ok) as ssh:
            self._probe()
        ssh.assert_called_once_with(
            "true", ssh_target="u@cluster", timeout=sf_module._PREFLIGHT_PROBE_TIMEOUT_SEC
        )

    def test_probe_deadline_never_tighter_than_the_ops_it_gates(self) -> None:
        """The probe deadline must be >= SSH_TIMEOUT_SEC — the budget of the
        staging/submit ssh calls the probe predicts. A tighter probe is a
        false-positive machine: run #7 live, Hoffman2's ~31s handshake lost to
        the old hardcoded 30.0s bound by 1s, reading a cluster the 60s-bounded
        real operations could use fine as down (4 straight breaker trips)."""
        from hpc_agent.infra.remote import SSH_TIMEOUT_SEC
        from hpc_agent.ops import submit_flow as sf_module

        assert sf_module._PREFLIGHT_PROBE_TIMEOUT_SEC >= SSH_TIMEOUT_SEC

    def test_explicit_timeout_overrides_default(self) -> None:
        from hpc_agent.ops import submit_flow as sf_module

        ok = mock.Mock(returncode=0)
        with mock.patch.object(sf_module, "ssh_run", return_value=ok) as ssh:
            self._probe(timeout_sec=7.5)
        assert ssh.call_args.kwargs["timeout"] == 7.5

    def test_timeout_budget_is_bounded_and_raises_envelope(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """(a) the timeout fires, (b) the retry budget is bounded, (c) the
        failure surfaces as the structured ssh_unreachable envelope."""
        from hpc_agent.ops import submit_flow as sf_module

        hang = TimeoutError("ssh to u@cluster timed out after 30s: true")
        with (
            mock.patch.object(sf_module, "ssh_run", side_effect=hang) as ssh,
            pytest.raises(errors.SshUnreachable) as exc_info,
        ):
            self._probe()
        # Bounded budget: exactly max_attempts calls, then a raise — no spin.
        assert ssh.call_count == sf_module._PREFLIGHT_PROBE_MAX_ATTEMPTS
        # Structured envelope fields (what the CLI maps to error_code/category).
        err = exc_info.value
        assert err.error_code == "ssh_unreachable"
        assert err.category == "network"
        assert err.retry_safe is True
        assert "timed out on all 2 attempts" in str(err)
        assert "timed out after 30s" in str(err)  # last failure carried
        # Loud per-attempt breadcrumbs on stderr — the wedge was silent.
        stderr = capsys.readouterr().err
        assert "pre-flight probe attempt 1/2 to u@cluster failed" in stderr
        assert "pre-flight probe attempt 2/2 to u@cluster failed" in stderr

    def test_timeout_then_success_recovers(self, capsys: pytest.CaptureFixture[str]) -> None:
        from hpc_agent.ops import submit_flow as sf_module

        ok = mock.Mock(returncode=0)
        with mock.patch.object(
            sf_module, "ssh_run", side_effect=[TimeoutError("timed out"), ok]
        ) as ssh:
            self._probe()  # must not raise
        assert ssh.call_count == 2
        assert "attempt 1/2" in capsys.readouterr().err

    def test_clean_nonzero_exit_fails_immediately_without_burning_budget(self) -> None:
        """Auth refused / unknown host is permanent: ssh_run already retried
        transients internally, so the probe must not re-probe (connection-storm
        guard) — one call, immediate structured raise."""
        from hpc_agent.ops import submit_flow as sf_module

        bad = mock.Mock(returncode=255, stderr="Permission denied (publickey).")
        with (
            mock.patch.object(sf_module, "ssh_run", return_value=bad) as ssh,
            pytest.raises(errors.SshUnreachable, match="exit 255"),
        ):
            self._probe()
        assert ssh.call_count == 1

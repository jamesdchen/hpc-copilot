"""Multi-wave main-array submit path (#339 increments 3 + 4).

Covers the submit-flow seams that the wave path introduces, below the full
``submit_flow_batch`` pipeline:

* **Provenance precedence** — ``_ensure_run_sidecar`` stamps the CAP-DRIVEN
  ``wave_map`` (overriding the axes default) iff the sweep exceeds the effective
  cap; a ≤cap sweep stamps no explicit wave_map.
* **_submit_main_array routing** — ≤cap → one array; >cap → N waves via
  ``submit_plan``, returning one id per wave; the canary afterok gates EVERY
  wave and the inter-wave chain is completion-gated (afterany).
* **Mid-plan failure** — the partial wave ids surface on the typed envelope.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import submit_flow as sf
from hpc_agent.state.runs import read_run_sidecar


@pytest.fixture
def _capped_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster declaring max_array_size=100 (and max_concurrent_jobs=1)."""
    cfg = {
        "c": {
            "scheduler": "sge",
            "constraints": {"max_array_size": 100, "max_concurrent_jobs": 1},
        }
    }
    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", lambda: cfg)


class _WaveBackend(HPCBackend):
    """Wave-capable stub returning a unique id per submitted array."""

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self) -> None:
        self.log_dir = "/tmp/mw-logs"
        self._counter = 500
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    @property
    def supports_afterok(self) -> bool:
        return True

    def _build_afterok_dependency_flag(self, job_ids: list[str]) -> list[str]:
        return ["--dependency", "afterok:" + ":".join(job_ids)] if job_ids else []

    def _build_wave_dependency_flag(self, *, afterok_ids, afterany_ids):  # type: ignore[override]
        if not afterok_ids and not afterany_ids:
            return []
        conds: list[str] = []
        if afterok_ids:
            conds.append("afterok:" + ":".join(afterok_ids))
        if afterany_ids:
            conds.append("afterany:" + ":".join(afterany_ids))
        flags = ["--dependency", ",".join(conds)]
        if afterok_ids:
            flags.append("--kill-on-invalid-dep=yes")
        return flags

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):  # type: ignore[override]
        cmd = ["qsub", "-t", str(task_range), "-N", job_name]
        cmd.extend(extra_flags or [])
        return cmd

    def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
        self.commands.append(list(cmd))
        self.envs.append(dict(job_env))
        self._counter += 1
        return SimpleNamespace(stdout=f"JOB{self._counter}\n", stderr="", returncode=0)

    def _setup_log_dir(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Provenance precedence: cap-driven wave_map wins iff the sweep is multi-wave.
# --------------------------------------------------------------------------- #


def _required_sidecar_spec(run_id: str, total_tasks: int):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    return SubmitFlowSpec(
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/r",
        job_name=run_id,
        run_id=run_id,
        total_tasks=total_tasks,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python run.py"},
        canary=False,
        result_dir_template="results/{run_id}/task_{task_id}",
    )


def test_over_cap_sweep_stamps_cap_plan_wave_map(tmp_path: Path, _capped_cluster: None) -> None:
    # 250 tasks, cap 100 -> n_batches=ceil(250/100)=3, evenly packed at
    # ceil(250/3)=84 per batch -> waves of 84/84/82 (concurrency 1 -> one batch
    # per wave). The cap-plan wave_map must mirror that exact split, GLOBAL
    # 0-based, with no overlap and full coverage of 0..249.
    spec = _required_sidecar_spec("20260101-000000-overcap", total_tasks=250)
    sf._ensure_run_sidecar(tmp_path, spec)

    wave_map = read_run_sidecar(tmp_path, spec.run_id)["wave_map"]
    assert sorted(wave_map.keys()) == ["0", "1", "2"]
    assert wave_map["0"] == list(range(0, 84))
    assert wave_map["1"] == list(range(84, 168))
    assert wave_map["2"] == list(range(168, 250))
    # No overlap, exact coverage.
    all_ids = wave_map["0"] + wave_map["1"] + wave_map["2"]
    assert all_ids == list(range(250))


def test_backend_platform_cap_binds_plan_when_cluster_declares_no_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GHA-like: the BACKEND platform cap (256) is the binding constraint and the
    # cluster declares NO max_array_size (so ClusterConstraints defaults to 1000).
    # The cap-driven plan must pack to the EFFECTIVE cap (256), not the cluster
    # default — otherwise a >256 sweep becomes ONE oversized array the platform
    # rejects, the exact failure waves exist to prevent. Regression for that bug.
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"c": {"scheduler": "gha"}},  # note: no constraints block
    )

    class _CappedBackend:
        max_array_size = 256
        can_wave = True

    monkeypatch.setattr("hpc_agent.infra.backends.get_backend_class", lambda name: _CappedBackend)
    # The cap lookup gates on registered_backend_names() (plugin-aware), so make
    # the fake name resolvable there too.
    monkeypatch.setattr(
        "hpc_agent.infra.backends.registered_backend_names", lambda: frozenset({"gha"})
    )

    # Keyed on the backend cap, a 300-task sweep is multi-wave.
    assert sf._is_multiwave_sweep(backend_name="gha", total_tasks=300, cluster="c")

    plan = sf._main_submission_plan(total_tasks=300, cluster="c", backend_name="gha")
    # 300 tasks at the 256 cap -> 2 batches; every array must be <= 256 (the bug
    # produced a single 300-cell array because the packer used the 1000 default).
    assert plan.total_batches == 2
    assert max(b.array_size for b in plan.batches) <= 256
    assert sum(b.array_size for b in plan.batches) == 300


def test_at_cap_sweep_stamps_no_explicit_wave_map(tmp_path: Path, _capped_cluster: None) -> None:
    # 100 tasks == cap -> single wave; provenance precedence keeps today's
    # behaviour: no cap-driven wave_map (the axes-derived default applies, which
    # with no axes.yaml present resolves to an empty map).
    spec = _required_sidecar_spec("20260101-000000-atcap", total_tasks=100)
    sf._ensure_run_sidecar(tmp_path, spec)
    wave_map = read_run_sidecar(tmp_path, spec.run_id)["wave_map"]
    # No multi-wave cap plan was stamped (axes default → empty here).
    assert wave_map == {}


# --------------------------------------------------------------------------- #
# _submit_main_array routing.
# --------------------------------------------------------------------------- #


def test_submit_main_array_single_wave_under_cap(
    _capped_cluster: None,
) -> None:
    backend = _WaveBackend()
    ids, spans = sf._submit_main_array(
        backend,
        job_name="probe",
        total_tasks=50,
        job_env={},
        cwd=Path("."),
        resources=None,
        gate_job_ids=[],
        backend_name="sge",
        cluster="c",
    )
    assert ids == ["501"]
    # ≤cap single array: local index == global index, so no span map is
    # recorded and fetch_task_logs keeps today's global probe.
    assert spans is None
    assert len(backend.commands) == 1
    assert backend.commands[0][backend.commands[0].index("-t") + 1] == "1-50"


def test_submit_main_array_multi_wave_over_cap_with_canary_gate(
    _capped_cluster: None,
) -> None:
    backend = _WaveBackend()
    ids, spans = sf._submit_main_array(
        backend,
        job_name="probe",
        total_tasks=250,
        job_env={},
        cwd=Path("."),
        resources=None,
        gate_job_ids=["42"],  # the canary gate
        backend_name="sge",
        cluster="c",
    )
    # One id per wave (3 waves of 84/84/82). An SGE backend is index-bounded
    # (uses_global_array_index False), so each wave submits a LOCAL array
    # 1-<size> with a per-wave TASK_OFFSET recovering the global id — NOT a
    # global range that would exceed the scheduler's array-index cap.
    assert ids == ["501", "502", "503"]
    # Per-job GLOBAL task windows (0-based inclusive), parsed from the plan's
    # global task_range per batch — what fetch_task_logs needs to probe each
    # wave's job with the job-LOCAL log index.
    assert spans == {"501": (0, 83), "502": (84, 167), "503": (168, 249)}
    ranges = [c[c.index("-t") + 1] for c in backend.commands]
    assert ranges == ["1-84", "1-84", "1-82"]
    offsets = [e.get("TASK_OFFSET") for e in backend.envs]
    assert offsets == [None, "84", "168"]  # wave 0 omits the offset (byte-identical)
    # EVERY wave success-gates on the canary (42); later waves ALSO completion-gate
    # on their predecessor (afterany), merged into one --dependency. A canary
    # failure thus drops the whole sweep, while a partial failure in one wave does
    # not cancel the independent later waves.
    assert backend.commands[0][backend.commands[0].index("--dependency") + 1] == "afterok:42"
    assert (
        backend.commands[1][backend.commands[1].index("--dependency") + 1]
        == "afterok:42,afterany:501"
    )
    assert (
        backend.commands[2][backend.commands[2].index("--dependency") + 1]
        == "afterok:42,afterany:502"
    )


def test_submit_main_array_mid_plan_failure_surfaces_partial_ids(
    _capped_cluster: None,
) -> None:
    class _FailWave2(_WaveBackend):
        def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
            self.commands.append(list(cmd))
            if len(self.commands) == 3:  # 3rd array == wave 2
                return SimpleNamespace(stdout="", stderr="rejected", returncode=1)
            self._counter += 1
            return SimpleNamespace(stdout=f"JOB{self._counter}\n", stderr="", returncode=0)

    backend = _FailWave2()
    with pytest.raises(errors.RemoteCommandFailed) as exc:
        sf._submit_main_array(
            backend,
            job_name="probe",
            total_tasks=250,
            job_env={},
            cwd=Path("."),
            resources=None,
            gate_job_ids=[],
            backend_name="sge",
            cluster="c",
        )
    # The two waves that landed before the failure are recoverable — ids AND
    # their global task windows, so the crash-safety pre-stamp records spans
    # alongside the ids for the sidecar-reading recovery paths.
    assert exc.value.partial_submit_job_ids == ["501", "502"]  # type: ignore[attr-defined]
    assert exc.value.partial_submit_job_task_spans == {  # type: ignore[attr-defined]
        "501": (0, 83),
        "502": (84, 167),
    }


def test_submit_main_array_global_index_backend_records_no_spans(
    _capped_cluster: None,
) -> None:
    # A global-index backend (GHA-like) names its logs with the GLOBAL index,
    # so the local-index arithmetic a span implies would misroute every
    # wave>=1 probe — no span map is recorded and the global probe stands.
    class _GlobalIndexBackend(_WaveBackend):
        uses_global_array_index = True

    backend = _GlobalIndexBackend()
    ids, spans = sf._submit_main_array(
        backend,
        job_name="probe",
        total_tasks=250,
        job_env={},
        cwd=Path("."),
        resources=None,
        gate_job_ids=[],
        backend_name="sge",
        cluster="c",
    )
    assert ids == ["501", "502", "503"]
    assert spans is None
    # Sanity: the arrays really were submitted with GLOBAL windows.
    ranges = [c[c.index("-t") + 1] for c in backend.commands]
    assert ranges == ["1-84", "85-168", "169-250"]


# --------------------------------------------------------------------------- #
# Sidecar persistence: the waved plan's per-job spans land on the run sidecar.
# --------------------------------------------------------------------------- #


def test_submit_one_spec_stamps_job_task_spans_on_sidecar_for_waved_plan(
    tmp_path: Path,
    _capped_cluster: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through ``_submit_one_spec``: an over-cap (waved) submit
    records ``job_task_spans`` next to ``job_ids`` on the run sidecar, keyed
    by the scheduler job id with the 0-based inclusive GLOBAL window each
    wave's array covers. ``read_job_task_spans`` hands the same map back in
    the tuple shape ``fetch_task_logs`` consumes."""
    from unittest import mock

    from hpc_agent.state import run_record
    from hpc_agent.state.runs import read_job_task_spans, read_run_sidecar

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")

    spec = _required_sidecar_spec("20260101-000000-spans", total_tasks=250)
    sf._ensure_run_sidecar(tmp_path, spec)

    backend = _WaveBackend()
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=backend),
        mock.patch.object(sf, "submit_and_record"),
        mock.patch.object(sf, "load_run", return_value=None),
    ):
        result = sf._submit_one_spec(experiment_dir=tmp_path, spec=spec)

    assert result.job_ids == ["501", "502", "503"]
    sidecar = read_run_sidecar(tmp_path, spec.run_id)
    assert sidecar["job_ids"] == ["501", "502", "503"]
    # JSON round-trips tuples as lists; the sidecar stores [first, last].
    assert sidecar["job_task_spans"] == {
        "501": [0, 83],
        "502": [84, 167],
        "503": [168, 249],
    }
    assert read_job_task_spans(tmp_path, spec.run_id) == {
        "501": (0, 83),
        "502": (84, 167),
        "503": (168, 249),
    }


def test_submit_one_spec_stamps_no_spans_for_single_array(
    tmp_path: Path,
    _capped_cluster: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ≤cap sweep keeps today's sidecar byte-shape: no ``job_task_spans``
    key is written, and the reader resolves to None (global probe)."""
    from unittest import mock

    from hpc_agent.state import run_record
    from hpc_agent.state.runs import read_job_task_spans, run_sidecar_path

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")

    spec = _required_sidecar_spec("20260101-000000-nospans", total_tasks=50)
    sf._ensure_run_sidecar(tmp_path, spec)

    backend = _WaveBackend()
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=backend),
        mock.patch.object(sf, "submit_and_record"),
        mock.patch.object(sf, "load_run", return_value=None),
    ):
        result = sf._submit_one_spec(experiment_dir=tmp_path, spec=spec)

    assert result.job_ids == ["501"]
    import json as _json

    raw = _json.loads(run_sidecar_path(tmp_path, spec.run_id).read_text(encoding="utf-8"))
    assert "job_task_spans" not in raw
    assert read_job_task_spans(tmp_path, spec.run_id) is None

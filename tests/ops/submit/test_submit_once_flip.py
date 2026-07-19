"""The submit-once LIVE FLIP — mint-before-dispatch + promote wired into the real
submit sequence (``submit_flow._submit_one_spec`` / ``_fire_canary``), behind
``HPC_SUBMIT_ONCE`` (default ON since 0.11.3; ``=0`` opts out — the flag-off
tests below set the opt-out explicitly).

Red-then-green against the pre-flip tree (where the flag ON changed NOTHING in
``submit_flow`` — no mint, no promote, no env threading):

* **flag OFF byte-identity** — the record is written by ``submit_and_record``, no
  ``submitting`` record is ever minted, and no ``HPC_SUBMIT_ATTEMPT`` /
  ``HPC_SUBMIT_WAVE_KEY`` leaks into the dispatch env (the ordering, the command
  env, the journal-write path are all unchanged);
* **flag ON mint-before-dispatch ordering** — a dispatch spy observes the journal
  record already ``submitting`` when the ``qsub`` runs (the mint precedes it);
* **promote-after-id** — the record ends ``in_flight`` with the parsed ``job_ids``;
* **attempt + per-wave key threading** — the woven dispatch env carries the REAL
  ``attempt`` and a DISTINCT ``HPC_SUBMIT_WAVE_KEY`` per wave (Δ5), fixing the
  U3-b markers-carry-attempt-0 finding;
* **kill-window → submitting → recovery** — a kill between dispatch and promote
  leaves a durable ``submitting`` record (never a loss / never stranded), and the
  REAL ``reconcile._recover_submitting`` adopts the orphan's id with no re-qsub;
* **canary leg** — the canary mints its own ``submitting`` record and threads the
  ``canary`` wave key + its real attempt.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import submit_flow as sf
from hpc_agent.state import run_record
from hpc_agent.state.index import find_submitting_runs
from hpc_agent.state.journal import load_run

if TYPE_CHECKING:
    pass


# --------------------------------------------------------------------------- #
# Fixtures / stubs (mirror tests/ops/submit/test_multiwave_submit.py).
# --------------------------------------------------------------------------- #


@pytest.fixture
def _capped_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "c": {
            "scheduler": "sge",
            "constraints": {"max_array_size": 100, "max_concurrent_jobs": 1},
        }
    }
    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", lambda: cfg)


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    return tmp_path


class _WaveBackend(HPCBackend):
    """Wave-capable stub returning a unique id per submitted array, recording the
    exact ``job_env`` each dispatch saw."""

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self) -> None:
        self.log_dir = "/tmp/flip-logs"
        self._counter = 500
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    @property
    def supports_afterok(self) -> bool:
        return True

    def _build_afterok_dependency_flag(self, job_ids: list[str]) -> list[str]:
        return ["--dependency", "afterok:" + ":".join(job_ids)] if job_ids else []

    def _build_wave_dependency_flag(self, *, afterok_ids, afterany_ids):  # type: ignore[override]
        conds: list[str] = []
        if afterok_ids:
            conds.append("afterok:" + ":".join(afterok_ids))
        if afterany_ids:
            conds.append("afterany:" + ":".join(afterany_ids))
        return ["--dependency", ",".join(conds)] if conds else []

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


def _spec(run_id: str, total_tasks: int, *, canary: bool = False):
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
        canary=canary,
        result_dir_template="results/{run_id}/task_{task_id}",
    )


def _run_one_spec(exp: Path, spec, backend):
    with mock.patch.object(sf, "build_remote_backend", return_value=backend):
        return sf._submit_one_spec(experiment_dir=exp, spec=spec)


# --------------------------------------------------------------------------- #
# flag OFF — byte-identity (nothing the flip touches fires).
# --------------------------------------------------------------------------- #


def test_flag_off_records_via_submit_and_record_no_submitting_no_env(
    _home: Path, _capped_cluster: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "0")
    exp = _home
    spec = _spec("run-off", total_tasks=50)
    sf._ensure_run_sidecar(exp, spec)
    backend = _WaveBackend()

    with mock.patch.object(sf, "submit_and_record") as sar:
        result = _run_one_spec(exp, spec, backend)

    # The flag-off record path is submit_and_record — NOT a promote — and no
    # submitting record was ever minted.
    assert sar.call_count == 1
    assert load_run(exp, "run-off") is None  # submit_and_record was mocked out
    assert find_submitting_runs(exp) == []
    assert result.job_ids == ["501"]
    # No submit-once env leaked into the dispatch (byte-identical command env).
    assert "HPC_SUBMIT_ATTEMPT" not in backend.envs[0]
    assert "HPC_SUBMIT_WAVE_KEY" not in backend.envs[0]


# --------------------------------------------------------------------------- #
# flag ON — mint-before-dispatch ordering + promote-after-id.
# --------------------------------------------------------------------------- #


class _OrderSpyBackend(_WaveBackend):
    """Records the on-disk JOURNAL status at the moment each dispatch fires."""

    def __init__(self, exp: Path) -> None:
        super().__init__()
        self._exp = exp
        self.status_at_dispatch: list[str | None] = []

    def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
        rid = job_env.get("HPC_RUN_ID", "")
        rec = load_run(self._exp, rid) if rid else None
        self.status_at_dispatch.append(rec.status if rec is not None else None)
        return super()._execute_command(cmd, job_env, cwd)


def test_flag_on_mints_submitting_before_dispatch_then_promotes(
    _home: Path, _capped_cluster: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    exp = _home
    spec = _spec("run-flip", total_tasks=50)
    sf._ensure_run_sidecar(exp, spec)
    backend = _OrderSpyBackend(exp)

    result = _run_one_spec(exp, spec, backend)

    # ORDERING: the record was already ``submitting`` when the qsub ran — the mint
    # precedes the dispatch (the load-bearing flip invariant).
    assert backend.status_at_dispatch == ["submitting"]
    # PROMOTE: the id is in hand → in_flight with the parsed job_ids.
    rec = load_run(exp, "run-flip")
    assert rec is not None
    assert rec.status == "in_flight"
    assert rec.job_ids == ["501"]
    assert result.job_ids == ["501"]
    # Out of the submitting set, into the monitor live set.
    assert find_submitting_runs(exp) == []
    # The REAL attempt (0 for a first submit) rode the dispatch env.
    assert backend.envs[0]["HPC_SUBMIT_ATTEMPT"] == "0"
    assert backend.envs[0]["HPC_RUN_ID"] == "run-flip"


def test_flag_on_promoted_record_carries_keystones(
    _home: Path, _capped_cluster: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promoted in_flight record is field-identical to submit_and_record's for
    the #299/#240 opt-in keystones — a run that opted into auto-resume keeps it."""
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    exp = _home
    # SubmitFlowSpec is a pydantic model — set the opt-in via model_copy.
    spec = _spec("run-keys", total_tasks=50).model_copy(
        update={"auto_resume_on_kill": True, "max_auto_resumes": 5}
    )
    sf._ensure_run_sidecar(exp, spec)
    backend = _WaveBackend()

    _run_one_spec(exp, spec, backend)

    rec = load_run(exp, "run-keys")
    assert rec is not None and rec.status == "in_flight"
    assert rec.auto_resume_on_kill is True
    assert rec.max_auto_resumes == 5


# --------------------------------------------------------------------------- #
# flag ON — attempt + per-wave key threading (Δ5).
# --------------------------------------------------------------------------- #


def test_flag_on_multiwave_threads_distinct_wave_keys_and_attempt(
    _home: Path, _capped_cluster: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    exp = _home
    spec = _spec("run-mw", total_tasks=250)  # cap 100 -> 3 waves
    sf._ensure_run_sidecar(exp, spec)
    backend = _WaveBackend()

    result = _run_one_spec(exp, spec, backend)

    assert result.job_ids == ["501", "502", "503"]
    # Each wave dispatched with a DISTINCT wave key (id-files never collide) and
    # the SAME real attempt (0) — the U3-b markers-carry-attempt-0 fix.
    wave_keys = [e.get("HPC_SUBMIT_WAVE_KEY") for e in backend.envs]
    assert wave_keys == ["wave-0", "wave-1", "wave-2"]
    assert {e.get("HPC_SUBMIT_ATTEMPT") for e in backend.envs} == {"0"}
    assert {e.get("HPC_RUN_ID") for e in backend.envs} == {"run-mw"}
    # And the run promoted to in_flight with every wave id.
    rec = load_run(exp, "run-mw")
    assert rec is not None and rec.status == "in_flight"
    assert rec.job_ids == ["501", "502", "503"]


def test_flag_off_multiwave_threads_no_wave_key(
    _home: Path, _capped_cluster: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-identity: with the flag OFF the multi-wave dispatch env carries no
    ``HPC_SUBMIT_WAVE_KEY`` at all (submit_plan leaves batch_env untouched)."""
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "0")
    exp = _home
    spec = _spec("run-mw-off", total_tasks=250)
    sf._ensure_run_sidecar(exp, spec)
    backend = _WaveBackend()
    with mock.patch.object(sf, "submit_and_record"):
        _run_one_spec(exp, spec, backend)
    assert all("HPC_SUBMIT_WAVE_KEY" not in e for e in backend.envs)


# --------------------------------------------------------------------------- #
# flag ON — the kill-window: mint → (kill before promote) → submitting → recovery.
# --------------------------------------------------------------------------- #


def test_flag_on_kill_before_promote_leaves_submitting_then_reconcile_adopts(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill AFTER the dispatch but BEFORE the promote (the apex drop) leaves a
    durable ``submitting`` record with empty ``job_ids`` — never a loss, never a
    silent strand. The REAL ``reconcile._recover_submitting`` then reads the
    cluster jobmap marker and ADOPTS the id with NO re-qsub."""
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    exp = _home
    # cluster not in yaml → resolve_ssh_target falls back to record.ssh_target.
    spec = _spec("run-kill", total_tasks=1)
    spec = spec.model_copy(update={"cluster": "adhoc-not-in-yaml"})
    sf._ensure_run_sidecar(exp, spec)
    backend = _WaveBackend()

    # Simulate the kill: the promote raises AFTER the dispatch landed the id.
    with (
        mock.patch(
            "hpc_agent.ops.submit.runner.promote_submitting_record",
            side_effect=KeyboardInterrupt,
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        _run_one_spec(exp, spec, backend)

    # The dispatch happened exactly once (no duplicate), and the record is left
    # SUBMITTING with empty job_ids — surfaced to reconcile, not the monitor.
    assert len(backend.commands) == 1
    rec = load_run(exp, "run-kill")
    assert rec is not None and rec.status == "submitting" and rec.job_ids == []
    assert [r.run_id for r in find_submitting_runs(exp)] == ["run-kill"]

    # Recovery: the dispatching shell had persisted the id in the cluster marker;
    # reconcile reads it and adopts with ZERO re-qsub.
    from hpc_agent.ops.monitor import reconcile as R

    ack = "__HPC_JOBMAP_ACK__"
    marker = (
        f"{ack}\n"
        '{"token":"run-kill#0","state":"pending","attempt":0,"waves":{}}\n'
        "__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 987654\n"
    )
    calls: list[str] = []

    def _ssh(cmd: str, *, ssh_target: str | None = None, **_: object):
        calls.append(cmd)
        if ".hpc/submit" in cmd and "rm -f" not in cmd:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=marker, stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(R.remote, "ssh_run", _ssh)
    monkeypatch.setattr(
        R,
        "read_announcements",
        lambda **_: {"present": True, "announced": 0, "complete": 0, "failed": 0, "missing": 0},
    )
    monkeypatch.setattr(R, "resolve_ssh_target", lambda rec: rec.ssh_target)

    kill_rec = load_run(exp, "run-kill")
    assert kill_rec is not None
    # The recovery-side scheduler parser must match the marker's stdout format
    # (the marker carries a Slurm-shaped "Submitted batch job" blob) — it is
    # independent of the submit stub above.
    out = R._recover_submitting(exp, "run-kill", record=kill_rec, scheduler="slurm")
    assert out.status == "in_flight"
    assert out.job_ids == ["987654"]
    # The load-bearing assertion: recovery adopted from the marker, no re-dispatch.
    assert not any(("qsub " in c or "sbatch" in c) for c in calls)


# --------------------------------------------------------------------------- #
# flag ON — the canary leg mints its own record + threads the canary wave key.
# --------------------------------------------------------------------------- #


def test_flag_on_canary_leg_mints_and_threads_canary_wave_key(
    _home: Path, _capped_cluster: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    exp = _home
    spec = _spec("run-can", total_tasks=50)
    sf._ensure_run_sidecar(exp, spec)
    backend = _WaveBackend()

    job_env_full = sf._augment_job_env(
        job_env=dict(spec.job_env or {}),
        runtime=spec.runtime,
        campaign_id=spec.campaign_id,
        cluster=spec.cluster,
    )
    canary_ids = sf._fire_canary(
        experiment_dir=exp,
        spec=spec,
        canary_run_id="run-can-canary",
        backend_obj=backend,
        job_env_full=job_env_full,
    )

    assert canary_ids == ["501"]
    # The canary minted its OWN submitting record then promoted it.
    rec = load_run(exp, "run-can-canary")
    assert rec is not None and rec.status == "in_flight" and rec.job_ids == ["501"]
    # The dispatch carried the CANARY wave key + the real attempt + the canary id.
    from hpc_agent.infra.jobmap import CANARY_WAVE_KEY

    assert backend.envs[0]["HPC_SUBMIT_WAVE_KEY"] == CANARY_WAVE_KEY
    assert backend.envs[0]["HPC_SUBMIT_ATTEMPT"] == "0"
    assert backend.envs[0]["HPC_RUN_ID"] == "run-can-canary"

"""F-J residual — the canary fingerprint pull honors summary_artifact.

``_pull_canary_task0_metrics`` (the determinism-fingerprint sample pull)
hardcoded ``metrics.json`` in its rsync ``include`` and its local ``rglob``. A
canary whose executor emits e.g. ``results_reduce.json`` pulled nothing and the
sample could never be minted. The canary rides the same pipeline as its main
run, so its sidecar carries the SAME declared ``summary_artifact``; the seam
resolves it there and threads it into the pull.

FIRES: a canary emitting results_reduce.json yields the pulled path when the
declared name is honored, and RAISES under the old metrics.json hardcode.
PASSES: an undeclared canary resolves to metrics.json and pulls byte-identical.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.infra import transport
from hpc_agent.ops.submit_and_verify import _pull_canary_task0_metrics
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_CANARY_ID = "20260101-000000-mainrun-canary"


def _common_sidecar_kwargs(run_id: str) -> dict[str, Any]:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="1" * 64,
    )


def _seed_record(experiment_dir: Path, run_id: str) -> None:
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="c",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="p",
            job_ids=["9001"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir),
            status="complete",
            backend="slurm",
        ),
    )


def _install_fake_pull(monkeypatch: pytest.MonkeyPatch, *, remote_emits: str) -> None:
    """Model an executor that wrote ONLY *remote_emits*; the pull honors include."""

    def _fake_pull(*, local_dir: str, include: list[str] | None, **_kw: Any) -> SimpleNamespace:
        from pathlib import Path

        if include and remote_emits in include:
            task_dir = Path(local_dir) / "task_0"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / remote_emits).write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(transport, "rsync_pull", _fake_pull)


@pytest.fixture
def experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    d = tmp_path / "exp"
    d.mkdir()
    return d


def test_fires_declared_summary_artifact_pulls(
    experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRES: a canary emitting results_reduce.json.

    Threading the declared name pulls the file; the old metrics.json hardcode
    would have pulled nothing and raised (no sample mintable).
    """
    _seed_record(experiment, _CANARY_ID)
    _install_fake_pull(monkeypatch, remote_emits="results_reduce.json")
    write_run_sidecar(
        experiment,
        **_common_sidecar_kwargs(_CANARY_ID),
        summary_artifact="results_reduce.json",
    )

    path = _pull_canary_task0_metrics(experiment, _CANARY_ID)
    assert path.name == "results_reduce.json"
    assert path.is_file()


def test_fires_hardcode_would_have_missed(
    experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SAME on-disk state under the old metrics.json assumption pulls nothing.

    An undeclared sidecar resolves to metrics.json, so a run that actually
    emitted results_reduce.json finds no file — the pre-fix miss this closes.
    """
    _seed_record(experiment, _CANARY_ID)
    _install_fake_pull(monkeypatch, remote_emits="results_reduce.json")
    # No summary_artifact declared → resolved default metrics.json.
    write_run_sidecar(experiment, **_common_sidecar_kwargs(_CANARY_ID))

    with pytest.raises(errors.RemoteCommandFailed):
        _pull_canary_task0_metrics(experiment, _CANARY_ID)


def test_default_metrics_json_unchanged(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PASSES: an undeclared canary emitting metrics.json pulls as before."""
    _seed_record(experiment, _CANARY_ID)
    _install_fake_pull(monkeypatch, remote_emits="metrics.json")
    write_run_sidecar(experiment, **_common_sidecar_kwargs(_CANARY_ID))

    path = _pull_canary_task0_metrics(experiment, _CANARY_ID)
    assert path.name == "metrics.json"
    assert path.is_file()

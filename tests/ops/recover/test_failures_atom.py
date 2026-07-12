"""Tests for the auto-retry policy resolver and hardcoded defaults.

The resolver combines a per-run sidecar override (populated at /submit
time when the user supplies a custom policy) with framework defaults
defined in ``hpc_agent.ops.recover.runner_failures.DEFAULT_AUTO_RETRY_POLICY``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.ops.recover.failures_atom import _resolve_auto_retry
from hpc_agent.ops.recover.runner_failures import DEFAULT_AUTO_RETRY_POLICY
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _common_required_kwargs(run_id: str = "20260101-000000-resolve") -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=1,
        tasks_py_sha="1" * 64,
    )


# ---------------------------------------------------------------------------
# Hardcoded defaults
# ---------------------------------------------------------------------------


def test_default_policy_covers_documented_categories() -> None:
    """Defaults must cover every category that classify_failure can emit
    so the resolver always returns useful advice without user config.

    ``ssh_unreachable`` is bucketed by ``cluster_failures_by_fingerprint``
    (not the per-stderr classifier) for entries where the SSH transport
    failed; it's listed here because the policy applies to either path.
    """
    assert set(DEFAULT_AUTO_RETRY_POLICY) == {
        "gpu_oom",
        "system_oom",
        "walltime",
        "node_failure",
        "ssh_unreachable",
    }


def test_default_policy_caps_are_conservative() -> None:
    """Defaults must cap retries low so an auto-retry never compounds bugs."""
    for cat, policy in DEFAULT_AUTO_RETRY_POLICY.items():
        assert policy["max_attempts"] <= 2, f"{cat!r} cap too high"


# ---------------------------------------------------------------------------
# Resolver: sidecar absent -> defaults
# ---------------------------------------------------------------------------


def test_resolve_returns_defaults_when_sidecar_missing(tmp_path: Path) -> None:
    resolved = _resolve_auto_retry(tmp_path, "20260101-000000-nonex0000")
    assert resolved == DEFAULT_AUTO_RETRY_POLICY


def test_resolve_returns_defaults_when_sidecar_has_no_auto_retry(tmp_path: Path) -> None:
    write_run_sidecar(tmp_path, **_common_required_kwargs())
    resolved = _resolve_auto_retry(tmp_path, _common_required_kwargs()["run_id"])
    assert resolved == DEFAULT_AUTO_RETRY_POLICY


# ---------------------------------------------------------------------------
# Resolver: sidecar override
# ---------------------------------------------------------------------------


def test_resolve_returns_sidecar_override(tmp_path: Path) -> None:
    """A user-supplied auto_retry block in the sidecar fully replaces defaults."""
    custom = {
        "gpu_oom": {"max_attempts": 3, "mem_multiplier": 2.0},
        "walltime": {"max_attempts": 0},
    }
    write_run_sidecar(tmp_path, **_common_required_kwargs(), auto_retry=custom)
    resolved = _resolve_auto_retry(tmp_path, _common_required_kwargs()["run_id"])
    # Full replacement: only the keys the user supplied are present.
    assert set(resolved) == set(custom)
    assert resolved["gpu_oom"]["max_attempts"] == 3
    assert resolved["walltime"]["max_attempts"] == 0


def test_resolve_falls_back_to_defaults_when_sidecar_override_is_malformed(
    tmp_path: Path,
) -> None:
    """A non-dict-valued auto_retry entry is filtered out; if everything is
    filtered, the resolver falls back to defaults."""
    # All values are non-dict — every entry gets filtered.
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs(),
        auto_retry={"gpu_oom": "not a dict", "walltime": 0},  # type: ignore[dict-item]
    )
    resolved = _resolve_auto_retry(tmp_path, _common_required_kwargs()["run_id"])
    assert resolved == DEFAULT_AUTO_RETRY_POLICY


# ---------------------------------------------------------------------------
# job_task_spans pass-through: the atom threads the sidecar's per-job global
# task windows into fetch_task_logs (waved runs read the RIGHT job's log with
# the job-LOCAL index); an old sidecar without the field passes None.
# ---------------------------------------------------------------------------


def _seed_journal_record(tmp_path, monkeypatch, experiment, run_id: str, job_ids: list[str]):
    from hpc_agent.state import run_record
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    record = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@h",
        remote_path="/x",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=2000,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    upsert_run(experiment, record)


def _drive_fetch_failures(tmp_path, monkeypatch, *, run_id: str):
    """Drive the real fetch_failures atom with mocked SSH primitives and
    return the kwargs the (monkeypatched) fetch_task_logs received."""
    from hpc_agent.ops.recover import failures_atom

    experiment = tmp_path / "exp"
    experiment.mkdir(exist_ok=True)
    _seed_journal_record(tmp_path, monkeypatch, experiment, run_id, ["100", "200"])

    monkeypatch.setattr(
        failures_atom,
        "_ssh_status_report",
        lambda **_: {"tasks": {"1005": {"status": "failed"}}},
    )
    monkeypatch.setattr(
        failures_atom, "load_clusters_config", lambda: {"c": {"scheduler": "slurm"}}
    )
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return [{"task_id": 1005, "content": "boom", "job_id": "200"}]

    monkeypatch.setattr(failures_atom, "fetch_task_logs", _capture)
    failures_atom.fetch_failures(experiment_dir=experiment, run_id=run_id)
    return experiment, captured


def test_fetch_failures_passes_recorded_job_task_spans_to_fetch_task_logs(
    tmp_path, monkeypatch
) -> None:
    from hpc_agent.state.runs import update_run_sidecar_job_ids, write_run_sidecar

    run_id = "20260101-000000-spans"
    experiment = tmp_path / "exp"
    experiment.mkdir()
    # A waved submit recorded spans next to job_ids on the sidecar.
    write_run_sidecar(experiment, **_common_required_kwargs(run_id))
    update_run_sidecar_job_ids(
        experiment,
        run_id,
        ["100", "200"],
        job_task_spans={"100": (0, 999), "200": (1000, 1999)},
    )

    _, captured = _drive_fetch_failures(tmp_path, monkeypatch, run_id=run_id)
    assert captured["job_task_spans"] == {"100": (0, 999), "200": (1000, 1999)}
    assert captured["job_ids"] == ["100", "200"]
    assert captured["task_ids"] == [1005]


def test_fetch_failures_passes_none_spans_for_old_sidecar(tmp_path, monkeypatch) -> None:
    """Back-compat: a sidecar without the field (pre-feature, or ≤cap single
    array) threads None — fetch_task_logs keeps the global-index probe."""
    from hpc_agent.state.runs import write_run_sidecar

    run_id = "20260101-000000-oldsc"
    experiment = tmp_path / "exp"
    experiment.mkdir()
    write_run_sidecar(experiment, **_common_required_kwargs(run_id))

    _, captured = _drive_fetch_failures(tmp_path, monkeypatch, run_id=run_id)
    assert "job_task_spans" in captured
    assert captured["job_task_spans"] is None


def test_fetch_failures_passes_none_spans_with_no_sidecar_at_all(tmp_path, monkeypatch) -> None:
    """A journal-only run (no sidecar on disk) must not error: the reader is
    best-effort and resolves to None."""
    run_id = "20260101-000000-nosc"
    _, captured = _drive_fetch_failures(tmp_path, monkeypatch, run_id=run_id)
    assert captured["job_task_spans"] is None


def test_fetch_failures_seeds_reporter_activation_from_record_cluster(
    tmp_path, monkeypatch
) -> None:
    """G6 / #13-sibling: ``fetch_failures`` was the SEVENTH, unseeded reporter
    consumer — it ran the login-node status reporter with no ``remote_activation``
    and so exited 127 on conda clusters. The record always knows the cluster; the
    reporter must receive the derived conda activation for a bare sidecar."""
    from hpc_agent.ops.recover import failures_atom

    run_id = "20260101-000000-actv"
    experiment = tmp_path / "exp"
    experiment.mkdir()
    _seed_journal_record(tmp_path, monkeypatch, experiment, run_id, ["100", "200"])

    captured: dict = {}

    def _reporter(**kwargs):
        captured.update(kwargs)
        return {"tasks": {}}  # no failed tasks — short-circuits before log fetch

    monkeypatch.setattr(failures_atom, "_ssh_status_report", _reporter)
    # The record's cluster "c" carries conda config; the bare sidecar has none,
    # so activation MUST derive from the cluster (fallback_cluster arm, #281).
    monkeypatch.setattr(
        failures_atom,
        "load_clusters_config",
        lambda: {"c": {"conda_source": "/c/conda.sh", "conda_envs": ["hpc-env"]}},
    )
    import hpc_agent.infra.clusters as clusters_mod

    monkeypatch.setattr(
        clusters_mod,
        "load_clusters_config",
        lambda: {"c": {"conda_source": "/c/conda.sh", "conda_envs": ["hpc-env"]}},
    )

    failures_atom.fetch_failures(experiment_dir=experiment, run_id=run_id)
    assert "conda activate hpc-env" in captured.get("remote_activation", "")

"""End-to-end opt-in path for the #240 resolve-and-recover composite.

The composite unit tests (``test_resolve_and_recover_flow.py``) seed the
``RunRecord`` *directly* — they assert the composite's routing in isolation but
say nothing about whether a run can actually *opt in*. The monitor-tick tests
(``test_flow_resolve_and_recover.py``) patch the composite with a fake — they
assert the monitor's handling of an outcome, not the composite firing. Between
them sat a gap: nothing proved that ``auto_recover_on_failure`` survives the
real submit plumbing into the persisted journal record and then drives the
*live* composite to a resubmit.

That gap was load-bearing. #316 wired ``maybe_resolve_and_recover`` into the
monitor's FAILED tick, but the gate it reads (``record.auto_recover_on_failure``)
had no producer: the ``SubmitFlowSpec`` field and the ``submit_and_record``
threading did not exist, so the gate could never be True and the freshly-wired
hook could never fire — "wired but unfireable," the inert-primitive pattern one
level down.

These tests pin the whole opt-in chain end to end with the REAL composite (only
the failure fetch and ``resubmit_flow`` injected, exactly as the composite's own
tests do):

* opt-in ON via ``submit_and_record`` → persisted record → ``maybe_resolve_and_recover``
  (loading the record from the journal, NOT passed ``record=``) actually
  resubmits a ``decided_by="code"`` cluster and bumps the counter;
* opt-in OFF (the default) → the same failure is computed-and-surfaced but takes
  no side effect (#283 zero-blast-radius).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.actions.submit import SubmitSpec as _WireSubmitSpec
from hpc_agent.ops.resolve_and_recover_flow import maybe_resolve_and_recover
from hpc_agent.ops.submit.runner import submit_and_record
from hpc_agent.state import run_record
from hpc_agent.state.journal import load_run

_RUN_ID = "ml_ridge_optin01"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _record_via_submit(experiment_dir: Path, **submit_kwargs: Any) -> None:
    """Persist a journal record through the SAME ``submit_and_record`` sink the
    live submit-flow uses, so the opt-in we assert on is the one a real submit
    would write (no ``_seed_record`` shortcut)."""
    submit_and_record(
        experiment_dir,
        spec=_WireSubmitSpec(
            profile="ml_ridge",
            cluster="c",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="myjob",
            run_id=_RUN_ID,
            job_ids=["9001"],
            total_tasks=4,
        ),
        script=".hpc/templates/cpu_array.sh",
        backend="slurm",
        **submit_kwargs,
    )


def _write_sidecar(experiment_dir: Path, *, resources: dict[str, Any]) -> None:
    """A sidecar with a concrete ``mem_mb`` so a gpu_oom code verdict has a knob
    to scale (increase-mem-per-gpu * 1.5)."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.36",
        submitted_at="2026-06-09T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
        resources=resources,
    )


def _fetcher(clusters: list[dict[str, Any]]):
    def _fetch(*, experiment_dir: Path, run_id: str, **kw: Any) -> dict[str, Any]:
        return {"run_id": run_id, "failed_count": 0, "clusters": list(clusters)}

    return _fetch


def _gpu_oom_cluster(task_ids: list[int]) -> dict[str, Any]:
    return {
        "error_class": "gpu_oom",
        "category": "gpu_oom",
        "fingerprint": "fp-oom",
        "task_ids": list(task_ids),
    }


class _Recorder:
    def __init__(self, *, new_job_ids: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._new_job_ids = new_job_ids or ["9100"]

    def __call__(self, experiment_dir: Path, run_id: str, **kwargs: Any) -> Any:
        self.calls.append({"experiment_dir": experiment_dir, "run_id": run_id, **kwargs})

        class _Result:
            deduped = False
            cluster_submitted = True
            new_job_ids = list(self._new_job_ids)

        return _Result()


def test_submit_persists_the_recover_opt_in(journal_home: Path, experiment: Path) -> None:
    """The new ``SubmitFlowSpec`` → ``submit_and_record`` plumbing lands the
    opt-in (and its cap) on the persisted journal record — the producer #316's
    gate was missing."""
    _record_via_submit(experiment, auto_recover_on_failure=True, max_auto_recovers=3)

    loaded = load_run(experiment, _RUN_ID)
    assert loaded is not None
    assert loaded.auto_recover_on_failure is True
    assert loaded.max_auto_recovers == 3
    assert loaded.auto_recover_count == 0


def test_opt_in_drives_the_live_composite_to_a_resubmit(
    journal_home: Path, experiment: Path
) -> None:
    """Full chain: opt-in submitted → persisted record → the REAL composite
    (record loaded from the journal, not injected) resolves the gpu_oom cluster
    to a code verdict and auto-resubmits with the translated mem override,
    bumping the durable counter."""
    _record_via_submit(experiment, auto_recover_on_failure=True, max_auto_recovers=2)
    _write_sidecar(experiment, resources={"mem_mb": 4000})

    rec = _Recorder(new_job_ids=["9100", "9101"])
    fetch = _fetcher([_gpu_oom_cluster([0, 1])])

    # No ``record=`` — the composite loads the opt-in record the submit plumbing
    # persisted. This is the seam that was unfireable before this change.
    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert len(outcome.resubmitted) == 1
    c = outcome.resubmitted[0]
    assert c.decided_by == "code"
    assert c.overrides == {"mem_mb": 6000}  # 4000 * 1.5

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["failed_task_ids"] == [0, 1]
    assert call["category"] == "gpu_oom"
    assert call["overrides"] == {"mem_mb": 6000}
    # Carried from the record the submit plumbing wrote (not a test-seeded one).
    assert call["script"] == ".hpc/templates/cpu_array.sh"
    assert call["backend"] == "slurm"
    assert call["job_name"] == "myjob"

    # The durable cap counter is bumped on the persisted record.
    assert outcome.auto_recover_count == 1
    assert load_run(experiment, _RUN_ID).auto_recover_count == 1


def test_default_off_submit_is_side_effect_free(journal_home: Path, experiment: Path) -> None:
    """A run submitted WITHOUT the opt-in (the default) computes and surfaces the
    same gpu_oom verdict but takes no side effect — no resubmit, counter stays
    0 (#283 zero-blast-radius through the real plumbing)."""
    _record_via_submit(experiment)  # auto_recover_on_failure defaults False
    _write_sidecar(experiment, resources={"mem_mb": 4000})

    loaded = load_run(experiment, _RUN_ID)
    assert loaded is not None
    assert loaded.auto_recover_on_failure is False

    rec = _Recorder()
    fetch = _fetcher([_gpu_oom_cluster([0, 1])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    # The verdict is still computed + surfaced as data, but no resubmit fired.
    assert rec.calls == []
    assert outcome.resubmitted == ()
    assert outcome.auto_recover_count == 0
    assert load_run(experiment, _RUN_ID).auto_recover_count == 0

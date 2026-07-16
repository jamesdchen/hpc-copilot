"""B8 fire-path: the canary qsub→record crash window must REPLAY, not duplicate.

``_fire_canary`` pre-stamps the parsed canary job_ids onto the canary sidecar
(crash-safety) BEFORE ``submit_and_record`` lands the journal entry. A crash in
that window leaves real scheduler ids on the sidecar with no journal record.
``load_run`` (journal-only) then returns ``None``, so the fresh-canary branch
used to re-qsub a DUPLICATE canary under the same run_id — double-writing task 0
and orphaning the first job. ``_refuse_prestamped_canary_without_journal``
(F47 for the canary) refuses that retry loudly instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import update_run_sidecar_job_ids, write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_CANARY = "ml_run_beefcafe-canary"


def _write_canary_sidecar(experiment: Path, *, run_id: str = _CANARY) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="deadbeef",
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="",
        cluster="hoffman2",
    )


def _seed_journal(experiment: Path, *, run_id: str = _CANARY, status: str = "failed") -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="hoffman2",
            ssh_target="user@h",
            remote_path="/x",
            job_name="p_canary",
            job_ids=["job_42"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status=status,
        ),
    )


def test_prestamped_sidecar_without_journal_refuses(tmp_path: Path) -> None:
    """THE B8 REGRESSION: sidecar carries landed job_ids, no journal record →
    the guard refuses (and names the landed id) instead of re-qsubbing."""
    from hpc_agent.ops.submit_flow import _refuse_prestamped_canary_without_journal

    _write_canary_sidecar(tmp_path)
    update_run_sidecar_job_ids(tmp_path, _CANARY, ["77777"])  # the pre-stamp
    with pytest.raises(errors.SpecInvalid) as exc:
        _refuse_prestamped_canary_without_journal(tmp_path, _CANARY)
    assert "77777" in str(exc.value)
    assert _CANARY in str(exc.value)


def test_clean_fresh_sidecar_passes(tmp_path: Path) -> None:
    """A genuine first fire has a mirrored sidecar with NO job_ids yet (ids land
    only via the post-qsub pre-stamp) → the guard is silent."""
    from hpc_agent.ops.submit_flow import _refuse_prestamped_canary_without_journal

    _write_canary_sidecar(tmp_path)  # no update_run_sidecar_job_ids → job_ids empty
    _refuse_prestamped_canary_without_journal(tmp_path, _CANARY)  # must not raise


def test_absent_sidecar_passes(tmp_path: Path) -> None:
    """No sidecar at all (nothing mirrored yet) → the guard is a clean no-op."""
    from hpc_agent.ops.submit_flow import _refuse_prestamped_canary_without_journal

    _refuse_prestamped_canary_without_journal(tmp_path, _CANARY)  # must not raise


def test_terminal_corpse_with_journal_passes(tmp_path: Path) -> None:
    """A #276 resubmittable-terminal corpse has a journal record AND a
    (forensic) job_ids sidecar — the journal check keeps the guard silent so the
    corpse legitimately re-fires a fresh canary."""
    from hpc_agent.ops.submit_flow import _refuse_prestamped_canary_without_journal

    _write_canary_sidecar(tmp_path)
    update_run_sidecar_job_ids(tmp_path, _CANARY, ["11111"])
    _seed_journal(tmp_path, status="failed")  # journal record present
    _refuse_prestamped_canary_without_journal(tmp_path, _CANARY)  # must not raise


def test_fire_second_canary_refuses_on_prestamped_sidecar(tmp_path: Path) -> None:
    """``fire_second_canary`` always fires (no replay branch of its own), so the
    same guard runs before it can qsub a duplicate ‑canary2 across a crash."""
    from hpc_agent.ops import submit_flow as sf

    second = "ml_run_beefcafe-canary2"
    _write_canary_sidecar(tmp_path, run_id=second)
    update_run_sidecar_job_ids(tmp_path, second, ["88888"])  # crash-window pre-stamp

    spec = SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id="ml_run_beefcafe",
        total_tasks=4,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
    )
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=object()),
        mock.patch.object(sf, "_fire_canary") as m_fire,
        pytest.raises(errors.SpecInvalid) as exc,
    ):
        sf.fire_second_canary(tmp_path, spec=spec, canary_run_id=second)
    assert "88888" in str(exc.value)
    m_fire.assert_not_called()  # refused BEFORE the duplicate qsub

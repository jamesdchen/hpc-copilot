"""SchedulerJobId boundary guard — fabricated job ids are refused at intake.

Empirical 2026-06-11 demo: the orchestrator lost the main array's real job id
(process killed between qsub and the journal write) and "recovered" by
recording ``job_ids: ["purged-completed"]`` through submit-spec — an id no
scheduler ever issued, which poisons every downstream alive-check / qacct
probe. ``SchedulerJobId`` (digit-leading) makes that fabrication fail loudly
as ``spec_invalid`` on every journal/sidecar input that carries job ids:
``SubmitSpec.job_ids``, ``ResubmitSpec.new_job_ids``,
``WriteRunSidecarInput.job_ids``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hpc_agent._wire.actions.submit import SubmitSpec

_BASE = dict(
    profile="p",
    cluster="hoffman2",
    ssh_target="user@host",
    remote_path="/scratch/u/demo",
    job_name="monte_carlo_pi",
    run_id="monte_carlo_pi-bdae0357",
    total_tasks=100,
)


@pytest.mark.parametrize(
    "job_id",
    [
        "13610902",  # SGE
        "8570940",  # SLURM
        "8570940_3",  # SLURM array element
        "1234.pbs01",  # PBS (id.server)
        "123+0",  # SLURM het-job component
    ],
)
def test_real_scheduler_ids_accepted(job_id: str) -> None:
    spec = SubmitSpec(job_ids=[job_id], **_BASE)
    assert spec.job_ids == [job_id]


@pytest.mark.parametrize(
    "job_id",
    [
        "purged-completed",  # the empirical fabrication
        "unknown",
        "n/a",
        "",
        "job-13610902",  # prose prefix
        " 13610902",  # leading whitespace
    ],
)
def test_fabricated_or_malformed_ids_refused(job_id: str) -> None:
    with pytest.raises(ValidationError):
        SubmitSpec(job_ids=[job_id], **_BASE)


def test_one_bad_id_poisons_the_list() -> None:
    with pytest.raises(ValidationError):
        SubmitSpec(job_ids=["13610902", "purged-completed"], **_BASE)


def test_resubmit_new_job_ids_guarded() -> None:
    from hpc_agent._wire.actions.resubmit import ResubmitSpec

    fields = {f for f in ResubmitSpec.model_fields}
    assert "new_job_ids" in fields
    with pytest.raises(ValidationError):
        ResubmitSpec.model_validate(
            {"run_id": "r1", "failed_task_ids": [1], "new_job_ids": ["purged-completed"]}
        )


def test_write_run_sidecar_job_ids_guarded() -> None:
    from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput

    base = {
        "run_id": "r1",
        "cmd_sha": "0" * 64,
        "executor": "python3 run.py",
        "result_dir_template": "results/{run_id}/task_{task_id}",
        "task_count": 4,
    }
    ok = WriteRunSidecarInput.model_validate({**base, "job_ids": ["13610902"]})
    assert ok.job_ids == ["13610902"]
    with pytest.raises(ValidationError):
        WriteRunSidecarInput.model_validate({**base, "job_ids": ["purged-completed"]})
